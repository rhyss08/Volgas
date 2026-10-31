"""Bridges rFactor2 shared-memory telemetry to the ESP32-S3 dash's
Classic-screen display, over its USB CDC serial (composited alongside
its USB HID gamepad on the same cable - see esp32_dash.ino).

Also periodically pushes the local day/time (MSG_TIME_SYNC) so the dash's
Standby screen has something to show before/after a session, independent
of whether rFactor2's shared memory is even available yet - the board has
no RTC or WiFi/NTP of its own.

Protocol mirror of esp32_dash/esp32_dash_protocol.h - the minimal
gear/speed/RPM-only screen, not the full dash plan's TelemetryPacket
(docs/dash_multiscreen_plan.md). See that header for why.

The actual bridge loop lives in DashBridge below - a stoppable class (via
threading.Event) so volgas.py can drive it on a background thread
inside its GUI, sharing this exact logic rather than re-implementing it.
This file's own __main__ block is a thin standalone wrapper around it.

Usage: python volgas_rf2_dash.py
  Auto-detects the ESP32-S3 by USB VID:PID (303A:1001, Espressif's VID).

Requires: pip install pyserial
"""

import bisect
import struct
import threading
import time

import serial
import serial.tools.list_ports

from volgas_rf2_telemetry import RF2SharedMemoryReader

# 303A:1001 - confirmed empirically 2026-07-16 against the working
# explicit-USBCDC firmware: the TinyUSB composite device on the generic
# esp32s3 board reports this PID (the board build overrides USB.cpp's
# 0x0002 default). Caveat: the ROM bootloader's USB-Serial/JTAG interface
# (download mode, BOOT+RESET) uses the SAME 303A:1001 - so this match can
# also hit the bootloader's port if the board is sitting in download mode.
# Fine in practice: that port only exists during flashing.
ESP32_VID = 0x303A
ESP32_PID = 0x1001

SYNC0, SYNC1 = 0xAA, 0x55
MSG_DASH_TELEMETRY = 0x01
MSG_TIME_SYNC = 0x02
MSG_LAP_TIME = 0x03
MSG_LAP_DELTA = 0x04   # was MSG_SECTOR_SPLIT - now a continuous interpolated delta, see LapRecorder
TELEMETRY_FMT = "<HHHb"   # speedKph, rpm, rpmMax, gear - must match DashTelemetry exactly
TIME_SYNC_FMT = "<BBBB"   # dayOfWeek, hour, minute, second - must match TimeSync exactly
LAP_TIME_FMT = "<IB"      # lapTimeMs, valid - must match LapTime exactly
LAP_DELTA_FMT = "<iB"     # cumulativeDeltaMs, valid - must match LapDelta exactly
SEND_HZ = 20
TIME_SYNC_PERIOD_S = 5    # well under the dash's 15s "app disconnected" threshold (esp32_dash.ino APP_TIMEOUT_MS);
                          # short enough that a single lost packet (see send_time_sync_burst) doesn't leave the clock missing for long
# Windows keeps rFactor2's shared memory mapped (with its last-written bytes)
# even after the game exits, so get_player_telemetry() alone can't tell
# "game closed" from "car sitting still" - see RF2SharedMemoryReader.telemetry_age().
# Stop sending once a frame is this old, so the dash's own 1s timeout
# (esp32_dash.ino TELEMETRY_TIMEOUT_MS) reverts it to Standby instead of
# freezing on the last values forever.
TELEMETRY_STALE_S = 1.0


def find_esp32_port():
    for p in serial.tools.list_ports.comports():
        if p.vid == ESP32_VID and p.pid == ESP32_PID:
            return p.device
    return None


def build_frame(msg_type, payload):
    checksum = (msg_type + len(payload) + sum(payload)) & 0xFF
    return bytes([SYNC0, SYNC1, msg_type, len(payload)]) + payload + bytes([checksum])


def send_time_sync(ser):
    lt = time.localtime()   # tm_wday: Monday=0 .. Sunday=6, matches TimeSync's dayOfWeek convention
    payload = struct.pack(TIME_SYNC_FMT, lt.tm_wday, lt.tm_hour, lt.tm_min, lt.tm_sec)
    ser.write(build_frame(MSG_TIME_SYNC, payload))


def send_time_sync_burst(ser, stop_event, count=3, gap_s=0.3):
    # A freshly-enumerated USB CDC port can accept open()/write() without
    # raising any error even before the device side is actually ready to
    # receive - a single write right after connecting can go nowhere with no
    # exception to catch (2026-07-25: this is the suspected cause of the
    # dash's clock never appearing when it's plugged in after the PC app is
    # already running - a scenario that specifically exercises this
    # just-connected window). Sending a few times in quick succession right
    # after connecting makes a single lost packet harmless.
    for _ in range(count):
        send_time_sync(ser)
        stop_event.wait(gap_s)


class LapRecorder:
    """Builds a distance-vs-time reference curve from the player's best
    (fastest, fully valid) lap this session, and interpolates it live for a
    smooth, continuously-updating delta-to-best (2026-07-25, replaces the
    sector-crossing-only delta above - that only updated 3x/lap and, once
    mSector's real indexing was found, was confirmed working but still a
    coarse approximation compared to this).

    Uses lap_dist (mLapDist, "distance around track") as the interpolation
    key and the already-confirmed-correct telemetry-based lap_time_s (see
    RF2SharedMemoryReader.get_player_lap_timing()) as the compared value -
    NOT mTimeIntoLap, which is itself derived from distance (per rF2State.h:
    "ESTIMATED time into lap"), so using it here would just compare a
    distance-based estimate against itself and always trend toward zero
    regardless of actual driving.
    """

    # A completed lap only counts as a reference candidate if its recording
    # started within this many meters of the start/finish line - filters out
    # a partial lap from connecting mid-session (see _on_lap_complete),
    # without needing an extra "skip the first lap" gate that would otherwise
    # delay the delta showing up until lap 3 instead of lap 2 as expected.
    MAX_START_DIST_FOR_VALID_LAP = 20.0

    def __init__(self):
        self._current_samples = []       # [(dist, time_s), ...] for the lap in progress
        self._current_valid = True
        self._best_samples = None        # [(dist, time_s), ...] for the fastest complete valid lap, or None
        self._best_distances = None      # same x-values as _best_samples, precomputed for bisect
        self._best_lap_time = None
        self._last_total_laps = None

    def update(self, lap_dist, lap_time_s, total_laps, valid):
        if self._last_total_laps is not None and total_laps != self._last_total_laps:
            self._on_lap_complete()
            self._current_valid = True
        self._last_total_laps = total_laps

        if lap_dist < 0:
            # Car reset (or genuinely in the pits) - mLapDist goes negative
            # either way per rF2State.h. Abandon whatever partial lap was
            # being recorded rather than leaving stale high-water-mark
            # samples in place: without this, the monotonic-distance guard
            # below would keep rejecting every new sample once back on
            # track until dist climbs back past wherever it was pre-reset,
            # silently losing a chunk of the restarted attempt's data.
            self._current_samples = []
            self._current_valid = True
            return
        if not valid:
            self._current_valid = False
        # Keep the recorded curve strictly increasing in distance - a driver
        # reversing after a spin could otherwise produce a non-monotonic
        # sample that breaks the interpolation below.
        if self._current_samples and lap_dist <= self._current_samples[-1][0]:
            return
        self._current_samples.append((lap_dist, lap_time_s))

    def _on_lap_complete(self):
        if (self._current_samples and self._current_valid
                and self._current_samples[0][0] <= self.MAX_START_DIST_FOR_VALID_LAP):
            lap_time = self._current_samples[-1][1]
            if self._best_lap_time is None or lap_time < self._best_lap_time:
                self._best_samples = self._current_samples
                self._best_distances = [s[0] for s in self._current_samples]
                self._best_lap_time = lap_time
        self._current_samples = []

    def compute_delta(self, lap_dist, lap_time_s):
        """Returns the live delta in seconds (negative = faster/ahead), or
        None if there's no best-lap reference yet."""
        if self._best_samples is None or len(self._best_samples) < 2 or lap_dist < 0:
            return None
        idx = bisect.bisect_left(self._best_distances, lap_dist)
        if idx <= 0:
            ref_time = self._best_samples[0][1]
        elif idx >= len(self._best_samples):
            ref_time = self._best_samples[-1][1]
        else:
            d0, t0 = self._best_samples[idx - 1]
            d1, t1 = self._best_samples[idx]
            ref_time = t0 if d1 == d0 else t0 + (lap_dist - d0) / (d1 - d0) * (t1 - t0)
        return lap_time_s - ref_time


def compute_game_sector_delta(completed_sector, timing):
    """Coarse fallback delta, sourced entirely from rFactor2's own
    persistent best-lap fields (mBestSector1/2, mBestLapTime) - no local
    recording needed, so unlike LapRecorder's smoothly interpolated delta,
    this is available immediately even right after the bridge (re)starts,
    and reflects the actual session-best regardless of when the app
    started. Only updates at the 3 sector-boundary points per lap though
    (held between them), so it's a coarser reading - LapRecorder's
    interpolated delta is preferred whenever it's available; this is only
    used as a fallback until that curve exists.

    completed_sector: the raw rF2 mSector value from just before the
    transition that was just detected - i.e. which sector the player was
    driving through and has now finished. mSector's real mapping (confirmed
    2026-07-25 from docs/rf2_reference/rF2State.h line 428, "don't ask
    why"): 0=sector3, 1=sector1, 2=sector2. Returns the delta in seconds, or
    None if the best-lap reference isn't available yet (rF2 uses -1.0 for
    "not set this session").
    """
    if completed_sector == 1:
        cur_cumulative, best_cumulative = timing.cur_sector1, timing.best_sector1
    elif completed_sector == 2:
        cur_cumulative, best_cumulative = timing.cur_sector2, timing.best_sector2
    else:   # completed_sector == 0: sector 3 (the whole lap) just finished
        cur_cumulative, best_cumulative = timing.last_lap_time, timing.best_lap_time

    if cur_cumulative < 0 or best_cumulative < 0:
        return None   # rF2's -1.0 "not available yet" sentinel
    return cur_cumulative - best_cumulative


class DashBridge:
    """Runs the rF2 -> ESP32-S3 dash bridge loop until stop_event is set.

    status_cb(str) is called with human-readable status changes (port
    found, waiting for rFactor2, frame counts, ...) instead of printing
    directly, so a GUI can show them in a label.
    """

    def __init__(self, status_cb=lambda msg: None):
        self.status_cb = status_cb

    def run(self, stop_event):
        # Outer retry loop (2026-07-25): the original version had no
        # reconnect logic at all - a single SerialException anywhere (the
        # initial connect racing the port not being fully ready yet right
        # after the dash is plugged in, or any transient USB hiccup during
        # the send loop - board reset, brief unplug, a reflash) just let the
        # exception kill the thread, with nothing to restart it short of
        # relaunching the whole app. That's what caused both observed bugs:
        # the clock never appearing unless the app was restarted after the
        # dash was plugged in, and telemetry "randomly" stopping mid-session.
        # Now any SerialException/OSError at any stage cleans up and loops
        # back to searching for the port, instead of exiting for good.
        while not stop_event.is_set():
            ser = None
            reader = None
            try:
                self.status_cb("looking for the ESP32-S3 dash...")
                port = None
                while port is None and not stop_event.is_set():
                    port = find_esp32_port()
                    if port is None:
                        stop_event.wait(2)
                if stop_event.is_set():
                    break

                self.status_cb(f"found dash on {port}")
                ser = serial.Serial(port, 115200, timeout=0.05)
                send_time_sync_burst(ser, stop_event)   # Standby screen gets a clock even before/without rFactor2 running
                last_time_sync = time.time()

                self.status_cb("opening rFactor2 shared memory (needs the game running with the plugin loaded)...")
                while reader is None and not stop_event.is_set():
                    try:
                        reader = RF2SharedMemoryReader()
                    except OSError:
                        stop_event.wait(2)
                        if time.time() - last_time_sync >= TIME_SYNC_PERIOD_S:
                            send_time_sync(ser)
                            last_time_sync = time.time()
                if stop_event.is_set():
                    break

                self.status_cb("bridging telemetry to the dash")
                period = 1.0 / SEND_HZ
                sent = 0
                last_report = time.time()
                lap_recorder = LapRecorder()   # reset per-connection, like sent/last_report above
                last_sector = None              # for the game-data fallback delta
                last_fallback_delta_s = None
                while not stop_event.is_set():
                    start = time.time()
                    reader.poll()
                    pt = reader.get_player_telemetry()
                    fresh = reader.telemetry_age() < TELEMETRY_STALE_S
                    if pt is not None and fresh:
                        speed_kph = max(0, min(65535, round(pt.speed_kph)))
                        rpm = max(0, min(65535, round(pt.rpm)))
                        rpm_max = max(0, min(65535, round(pt.rpm_max)))
                        gear = max(-128, min(127, pt.gear))
                        payload = struct.pack(TELEMETRY_FMT, speed_kph, rpm, rpm_max, gear)
                        ser.write(build_frame(MSG_DASH_TELEMETRY, payload))
                        sent += 1

                        # Lap/sector timing - only sent alongside fresh telemetry so
                        # they stay in sync with what the dash is actively showing.
                        timing = reader.get_player_lap_timing()
                        if timing is not None:
                            lap_time_ms = max(0, min(0xFFFFFFFF, round(timing.lap_time_s * 1000)))
                            # Confirmed against live testing 2026-07-25: mCountLapFlag
                            # 1 = invalid lap, 2 = valid lap (the field itself was fine -
                            # the earlier "always 1" observation was just because that
                            # particular test never left an invalid lap while watching).
                            lap_valid = 1 if timing.count_lap_flag == 2 else 0
                            ser.write(build_frame(MSG_LAP_TIME, struct.pack(LAP_TIME_FMT, lap_time_ms, lap_valid)))

                            # Continuous delta-to-best, updated every frame (2026-07-25) -
                            # see LapRecorder for why this replaced the sector-crossing
                            # version (3 updates/lap, a coarse approximation), and
                            # compute_game_sector_delta() for the fallback used until a
                            # recorded reference curve exists (e.g. right after the app
                            # (re)starts mid-session).
                            lap_recorder.update(timing.lap_dist, timing.lap_time_s, timing.total_laps, lap_valid == 1)

                            if last_sector is not None and timing.sector != last_sector:
                                fallback = compute_game_sector_delta(last_sector, timing)
                                if fallback is not None:
                                    last_fallback_delta_s = fallback
                            last_sector = timing.sector

                            if timing.lap_dist < 0:
                                # Car reset (or genuinely in the pits) - explicitly
                                # blank the dash instead of freezing on whatever delta
                                # was last computed before the reset. Also drop the
                                # stale fallback value so it doesn't reappear stuck at
                                # a pre-reset reading once back on track.
                                delta_s = None
                                last_fallback_delta_s = None
                            else:
                                delta_s = lap_recorder.compute_delta(timing.lap_dist, timing.lap_time_s)
                                if delta_s is None:
                                    delta_s = last_fallback_delta_s

                            delta_valid = 1 if delta_s is not None else 0
                            delta_ms = max(-2**31, min(2**31 - 1, round(delta_s * 1000))) if delta_s is not None else 0
                            ser.write(build_frame(MSG_LAP_DELTA, struct.pack(LAP_DELTA_FMT, delta_ms, delta_valid)))

                    if start - last_time_sync >= TIME_SYNC_PERIOD_S:
                        send_time_sync(ser)
                        last_time_sync = start

                    if time.time() - last_report >= 2.0:
                        if pt is None:
                            status = "no player vehicle yet (in a session?)"
                        elif not fresh:
                            status = "rFactor2 closed or telemetry stopped - not sending"
                        else:
                            status = f"sending - {sent} frames/2s"
                        self.status_cb(status)
                        sent = 0
                        last_report = time.time()

                    elapsed = time.time() - start
                    remaining = period - elapsed
                    if remaining > 0:
                        stop_event.wait(remaining)   # sleeps, but wakes early if stopped
            except (serial.SerialException, OSError) as e:
                self.status_cb(f"lost connection to the dash ({e}) - reconnecting...")
                stop_event.wait(2)   # avoid a tight loop if the failure is immediate/persistent
            finally:
                if ser:
                    ser.close()
                if reader:
                    reader.close()
        self.status_cb("stopped")


def main():
    stop_event = threading.Event()
    bridge = DashBridge(status_cb=print)
    print("Ctrl+C to stop.")
    try:
        bridge.run(stop_event)
    except KeyboardInterrupt:
        stop_event.set()


if __name__ == "__main__":
    # Double-clicking a .py file on Windows opens a console that closes the
    # instant the script exits - including on a crash, before the traceback
    # can be read. Catch anything unexpected and pause so the window stays
    # open long enough to actually see what happened.
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        input("\nSomething went wrong (see above). Press Enter to close...")
