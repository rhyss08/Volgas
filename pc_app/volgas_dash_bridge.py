"""Feeds the ESP32-S3 dash from whichever game actually has live
telemetry - rFactor2 (shared memory), Assetto Corsa (its own shared memory,
see volgas_ac_telemetry) or Forza Horizon 5 (UDP Data Out) - tried every
frame, with no manual game selection needed.

This replaces having to pick a game in the Dash Telemetry tab and
remember to switch it. That selector caused a real bug (2026-08): the
app defaulted to rFactor2 on every launch regardless of which game was
actually running, silently locked the dash's COM port with a bridge that
had nothing to send, and Forza's own bridge then failed outright with
"Access is denied" trying to open the same port. Only one of the two
games is ever actually running at a time in practice (can't play both
at once), so there's no real "which one wins" conflict to resolve here -
this just tries both every frame and sends whichever responds.

Lap time/delta are only ever sent when rFactor2 or Assetto Corsa is the
active source, and only from the second lap on (see FIRST_SHOWN_LAP).
Forza's Data Out has no lap-time concept wired up here at all (per
request, 2026-08) - unlike rFactor2's rF2SharedMemoryMapPlugin, most
Forza Horizon activities (freeroam, most events) don't have a dedicated-
timed-lap structure the way a rFactor2 session does, so there's nothing
meaningful to show; MSG_LAP_TIME/MSG_LAP_DELTA are simply never sent for
Forza, which the dash already treats as "nothing to show" rather than
freezing on stale data (see esp32_dash.ino's footerDrawLapTime()/
footerDrawDelta()).

rFactor2 support degrades gracefully if volgas_rf2_telemetry.py's Windows-only
ctypes.wintypes import fails - this file then just never attempts that
source, same as if Forza were simply the only game being played.

Usage: python volgas_dash_bridge.py
  Auto-detects the ESP32-S3 by USB VID:PID (303A:1001).

Requires: pip install pyserial
"""
import logging
import struct
import sys
import threading
import time

import serial
import serial.tools.list_ports

from volgas_forza_telemetry import ForzaUDPReader
from volgas_wheel_ota import FrameReader, USB_WHEEL_INFO, USB_WHEEL_STATUS

try:
    from volgas_rf2_telemetry import RF2SharedMemoryReader
    # LapRecorder/compute_game_sector_delta live in volgas_rf2_dash.py,
    # not duplicated here - reused as-is, including its own docstring's
    # explanation of why the delta is computed the way it is.
    from volgas_rf2_dash import LapRecorder, compute_game_sector_delta
    RF2_AVAILABLE = True
except Exception:
    RF2_AVAILABLE = False

try:
    from volgas_ac_telemetry import ACSharedMemoryReader, MAX_TYRES_OUT
    from volgas_rf2_dash import LapRecorder
    AC_AVAILABLE = sys.platform == "win32"
except Exception:
    AC_AVAILABLE = False

# Around the start/finish line AC's lap counter and its position along the
# lap (normalizedCarPosition) don't change on exactly the same frame: a
# position near the end of the lap less than this far into a new lap (or
# near the start, more than twice this into the lap) means they disagree,
# and the sample is skipped rather than recorded at the wrong end of the lap.
AC_LINE_LAP_TIME_S = 10.0

ESP32_VID = 0x303A
ESP32_PID = 0x1001

SYNC0, SYNC1 = 0xAA, 0x55
MSG_DASH_TELEMETRY = 0x01
MSG_TIME_SYNC = 0x02
MSG_LAP_TIME = 0x03
MSG_LAP_DELTA = 0x04
TELEMETRY_FMT = "<HHHb"
TIME_SYNC_FMT = "<BBBB"
LAP_TIME_FMT = "<IB"
LAP_DELTA_FMT = "<iB"
SEND_HZ = 20
TIME_SYNC_PERIOD_S = 5
TELEMETRY_STALE_S = 1.0
RF2_RETRY_PERIOD_S = 2   # how often to retry opening rF2's shared memory while it's not there yet
# Lap time and delta go to the dash only once the game has counted this many
# completed laps: the first lap is the out lap (user, 2026-11-13), and on
# maps without a start/finish line (e.g. No Hesi in Assetto Corsa) no lap is
# ever completed, so the game's lap timer is just time since joining. Lap
# timing still runs in the background meanwhile.
FIRST_SHOWN_LAP = 1
HUB_STATUS_PERIOD_S = 1.0   # how often the hub is asked for the wheel's link status
HUB_STATUS_STALE_S = 4.0    # no answer for this long -> not a hub (or the hub is gone)


def find_esp32_port():
    for p in serial.tools.list_ports.comports():
        if p.vid == ESP32_VID and p.pid == ESP32_PID:
            return p.device
    return None


def build_frame(msg_type, payload):
    checksum = (msg_type + len(payload) + sum(payload)) & 0xFF
    return bytes([SYNC0, SYNC1, msg_type, len(payload)]) + payload + bytes([checksum])


def send_time_sync(ser):
    lt = time.localtime()
    payload = struct.pack(TIME_SYNC_FMT, lt.tm_wday, lt.tm_hour, lt.tm_min, lt.tm_sec)
    ser.write(build_frame(MSG_TIME_SYNC, payload))


def send_time_sync_burst(ser, stop_event, count=3, gap_s=0.3):
    for _ in range(count):
        send_time_sync(ser)
        stop_event.wait(gap_s)


log = logging.getLogger(__name__)


class DashBridge:
    """status_cb(str) is called with human-readable status changes (port
    found, which game is sending, frame counts, ...) instead of printing
    directly, so a GUI can show them in a label."""

    def __init__(self, status_cb=lambda msg: None, forza_port=None):
        self.status_cb = status_cb
        self.forza_port = forza_port   # UDP port for Forza; None = ForzaUDPReader.DEFAULT_PORT
        # What the dash was last sent, for the app's Dashboard preview, which
        # rebuilds the screen from it. Values are (time.monotonic(), data)
        # tuples, replaced whole so another thread can read them safely.
        self.connected = False
        self.time_synced = False   # a time sync went out on this connection
        self.last_telemetry = None   # (t, (speed_kph, rpm, rpm_max, gear))
        self.last_lap_time = None    # (t, (lap_time_ms, valid))
        self.last_lap_delta = None   # (t, (delta_ms, valid))
        self.active_source = None    # (t, "rf2" | "ac" | "forza") - which game the last telemetry came from
        self.port = None             # COM port of the connected hub/dash
        self.connected_at = 0.0      # time.monotonic() of the current connection
        # Latest answer to our wheel-status request: (time.monotonic(), {"linked", "model", "fw", "buttons",
        # "rssi"}). Only the hub answers; a wheel plugged in directly ignores the request.
        self.hub_status = None
        self._pause_req = threading.Event()
        self._paused_ack = threading.Event()

    @property
    def is_hub(self):
        s = self.hub_status
        return self.connected and s is not None and time.monotonic() - s[0] < HUB_STATUS_STALE_S

    def pause(self, timeout=5.0):
        """Let go of the serial port (e.g. for a wheel firmware update).
        Returns True once the port is free."""
        self._pause_req.set()
        return self._paused_ack.wait(timeout)

    def resume(self):
        self._pause_req.clear()
        self._paused_ack.clear()

    def _send_lap(self, ser, lap_time_ms, lap_valid, delta_s):
        ser.write(build_frame(MSG_LAP_TIME, struct.pack(LAP_TIME_FMT, lap_time_ms, lap_valid)))
        self.last_lap_time = (time.monotonic(), (lap_time_ms, lap_valid))
        delta_valid = 1 if delta_s is not None else 0
        delta_ms = max(-2**31, min(2**31 - 1, round(delta_s * 1000))) if delta_s is not None else 0
        ser.write(build_frame(MSG_LAP_DELTA, struct.pack(LAP_DELTA_FMT, delta_ms, delta_valid)))
        self.last_lap_delta = (time.monotonic(), (delta_ms, delta_valid))

    def run(self, stop_event):
        # Same outer-retry-loop shape as volgas_rf2_dash.DashBridge/
        # forza_to_esp32_dash.ForzaDashBridge - see either's docstring for
        # the reconnect rationale, identical here on the dash-USB side.
        while not stop_event.is_set():
            if self._pause_req.is_set():
                self._paused_ack.set()   # the port is closed at this point (see the finally below)
                stop_event.wait(0.2)
                continue
            ser = None
            rf2_reader = None
            ac_reader = None
            forza_reader = None
            try:
                self.status_cb("looking for the ESP32-S3 dash...")
                port = None
                while port is None and not stop_event.is_set() and not self._pause_req.is_set():
                    port = find_esp32_port()
                    if port is None:
                        stop_event.wait(2)
                if stop_event.is_set():
                    break
                if port is None:
                    continue   # paused

                self.status_cb(f"found dash on {port}")
                ser = serial.Serial(port, 115200, timeout=0.05)
                self.connected = True
                self.connected_at = time.monotonic()
                self.port = port
                hub_reader = FrameReader()
                last_status_request = 0.0
                send_time_sync_burst(ser, stop_event)
                self.time_synced = True
                last_time_sync = time.time()

                forza_kwargs = {} if self.forza_port is None else {"port": self.forza_port}
                forza_reader = ForzaUDPReader(**forza_kwargs)   # binds immediately, game doesn't need to be running yet
                last_rf2_attempt = 0   # rf2_reader stays None until this succeeds - shared memory needs the game+plugin already up
                last_ac_attempt = 0    # same for Assetto Corsa: its shared memory only exists while it runs
                ac_recorder = LapRecorder() if AC_AVAILABLE else None
                ac_laps = None
                ac_lap_valid = True
                ac_last_delta_s = None

                self.status_cb("bridging telemetry to the dash (rFactor2, Assetto Corsa or Forza Horizon 5, "
                               "whichever is live)")

                period = 1.0 / SEND_HZ
                sent = 0
                last_report = time.time()
                last_active_source = None
                lap_recorder = LapRecorder() if RF2_AVAILABLE else None
                last_sector = None
                last_fallback_delta_s = None

                while not stop_event.is_set() and not self._pause_req.is_set():
                    start = time.time()

                    # The hub answers this with its wheel status; a wheel on
                    # USB ignores it. Replies are read here (nothing else
                    # reads this port).
                    if start - last_status_request >= HUB_STATUS_PERIOD_S:
                        last_status_request = start
                        ser.write(build_frame(USB_WHEEL_INFO, b""))
                    if ser.in_waiting:
                        for msg_type, payload in hub_reader.feed(ser.read(ser.in_waiting)):
                            if msg_type == USB_WHEEL_STATUS and len(payload) >= 6:
                                linked, model, fw, buttons, rssi = struct.unpack("<BBHBb", payload[:6])
                                self.hub_status = (time.monotonic(), {
                                    "linked": bool(linked), "model": model, "fw": fw, "buttons": buttons,
                                    "rssi": rssi})

                    if RF2_AVAILABLE and rf2_reader is None and start - last_rf2_attempt >= RF2_RETRY_PERIOD_S:
                        last_rf2_attempt = start
                        try:
                            rf2_reader = RF2SharedMemoryReader()
                        except OSError:
                            pass   # rFactor2 not running (yet) - keep trying, keep sending Forza in the meantime

                    if AC_AVAILABLE and ac_reader is None and start - last_ac_attempt >= RF2_RETRY_PERIOD_S:
                        last_ac_attempt = start
                        try:
                            ac_reader = ACSharedMemoryReader()
                        except OSError:
                            pass   # Assetto Corsa not running (yet)

                    forza_reader.poll()
                    if rf2_reader is not None:
                        rf2_reader.poll()
                    if ac_reader is not None:
                        ac_reader.poll()
                        if ac_reader.telemetry_age() > 30:
                            # Let go of a closed game's memory, so the next
                            # start of AC is picked up fresh.
                            ac_reader.close()
                            ac_reader = None

                    rf2_pt = rf2_reader.get_player_telemetry() if rf2_reader is not None else None
                    rf2_fresh = rf2_reader is not None and rf2_reader.telemetry_age() < TELEMETRY_STALE_S
                    ac_pt = ac_reader.get_player_telemetry() if ac_reader is not None else None
                    ac_fresh = ac_reader is not None and ac_reader.telemetry_age() < TELEMETRY_STALE_S
                    forza_pt = forza_reader.get_player_telemetry()
                    forza_fresh = forza_reader.telemetry_age() < TELEMETRY_STALE_S

                    # In practice only one of these is ever fresh at once
                    # (can't play two games simultaneously) - rF2, then AC,
                    # win on the (should-never-happen) chance more are.
                    if rf2_pt is not None and rf2_fresh:
                        pt, source = rf2_pt, "rf2"
                    elif ac_pt is not None and ac_fresh:
                        pt, source = ac_pt, "ac"
                    elif forza_pt is not None and forza_fresh:
                        pt, source = forza_pt, "forza"
                    else:
                        pt, source = None, None

                    if pt is not None:
                        speed_kph = max(0, min(65535, round(pt.speed_kph)))
                        rpm = max(0, min(65535, round(pt.rpm)))
                        rpm_max = max(0, min(65535, round(pt.rpm_max)))
                        gear = max(-128, min(127, pt.gear))
                        payload = struct.pack(TELEMETRY_FMT, speed_kph, rpm, rpm_max, gear)
                        ser.write(build_frame(MSG_DASH_TELEMETRY, payload))
                        self.last_telemetry = (time.monotonic(), (speed_kph, rpm, rpm_max, gear))
                        sent += 1
                        last_active_source = source
                        self.active_source = (time.monotonic(), source)

                        if source == "rf2":
                            # Same lap-time/delta logic as
                            # volgas_rf2_dash.DashBridge - see that
                            # class for the full explanation of each piece.
                            timing = rf2_reader.get_player_lap_timing()
                            if timing is not None:
                                lap_time_ms = max(0, min(0xFFFFFFFF, round(timing.lap_time_s * 1000)))
                                lap_valid = 1 if timing.count_lap_flag == 2 else 0
                                lap_recorder.update(timing.lap_dist, timing.lap_time_s, timing.total_laps, lap_valid == 1)

                                if last_sector is not None and timing.sector != last_sector:
                                    fallback = compute_game_sector_delta(last_sector, timing)
                                    if fallback is not None:
                                        last_fallback_delta_s = fallback
                                last_sector = timing.sector

                                if timing.lap_dist < 0:
                                    delta_s = None
                                    last_fallback_delta_s = None
                                else:
                                    delta_s = lap_recorder.compute_delta(timing.lap_dist, timing.lap_time_s)
                                    if delta_s is None:
                                        delta_s = last_fallback_delta_s

                                if timing.total_laps >= FIRST_SHOWN_LAP:
                                    self._send_lap(ser, lap_time_ms, lap_valid, delta_s)
                        elif source == "ac":
                            timing = ac_reader.get_lap_timing()
                            if timing is not None:
                                if timing.completed_laps != ac_laps:
                                    ac_laps, ac_lap_valid = timing.completed_laps, True
                                if timing.tyres_out > MAX_TYRES_OUT:
                                    ac_lap_valid = False
                                lap_time_ms = max(0, min(0xFFFFFFFF, round(timing.lap_time_s * 1000)))
                                # Across the line, one of AC's lap counter and its
                                # lap position can update a frame before the other.
                                at_line = ((timing.fraction > 0.5 and timing.lap_time_s < AC_LINE_LAP_TIME_S)
                                           or (timing.fraction < 0.1 and timing.lap_time_s > 2 * AC_LINE_LAP_TIME_S))
                                if timing.lap_dist < 0:
                                    ac_recorder.update(-1.0, timing.lap_time_s, timing.completed_laps, False)
                                    ac_last_delta_s = None
                                elif not at_line:
                                    ac_recorder.update(timing.lap_dist, timing.lap_time_s, timing.completed_laps,
                                                       ac_lap_valid)
                                    ac_last_delta_s = ac_recorder.compute_delta(timing.lap_dist, timing.lap_time_s)
                                if timing.completed_laps >= FIRST_SHOWN_LAP:
                                    self._send_lap(ser, lap_time_ms, 1 if ac_lap_valid else 0, ac_last_delta_s)
                        # source == "forza": no MSG_LAP_TIME/MSG_LAP_DELTA at
                        # all - see this module's docstring.

                    if start - last_time_sync >= TIME_SYNC_PERIOD_S:
                        send_time_sync(ser)
                        last_time_sync = start

                    if time.time() - last_report >= 2.0:
                        if last_active_source:
                            status = f"sending ({last_active_source}) - {sent} frames/2s"
                        else:
                            games = (["rFactor2"] if RF2_AVAILABLE else []) + (["Assetto Corsa"] if AC_AVAILABLE else [])
                            waiting_for = " or ".join(games + ["Forza Horizon 5"])
                            status = f"waiting for {waiting_for}..."
                        self.status_cb(status)
                        sent = 0
                        last_active_source = None
                        last_report = time.time()

                    elapsed = time.time() - start
                    remaining = period - elapsed
                    if remaining > 0:
                        stop_event.wait(remaining)
            except (serial.SerialException, OSError) as e:
                self.status_cb(f"lost connection to the dash ({e}) - reconnecting...")
                log.info("dash connection lost: %s", e)
                stop_event.wait(2)
            except Exception:
                # Anything unexpected (e.g. a malformed game packet) used to end
                # this thread silently, leaving the dash dead until a restart.
                # Log it and start the connection over instead.
                log.exception("dash bridge error - restarting the connection")
                self.status_cb("unexpected error - restarting the dash connection...")
                stop_event.wait(2)
            finally:
                self.connected = False
                self.time_synced = False
                self.hub_status = None
                self.port = None
                if ser:
                    ser.close()
                if rf2_reader:
                    rf2_reader.close()
                if ac_reader:
                    ac_reader.close()
                if forza_reader:
                    forza_reader.close()
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
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        input("\nSomething went wrong (see above). Press Enter to close...")
