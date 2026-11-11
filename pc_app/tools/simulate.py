"""Run Volgas against simulated hardware - no pedals, dash or game needed.

    python tools/simulate.py                 # pedals + dash + Forza
    python tools/simulate.py --no-game       # pedals + dash, no game running
    python tools/simulate.py --no-pedals     # dash only
    python tools/simulate.py --unplug 20     # pedals disappear after 20 s, return 10 s later

Works on Windows, macOS and Linux. The simulated pedal board speaks the real
pedal protocol (pedal_set/pedal_protocol.h), answers calibration reads and
saves, and moves the throttle and brake on its own. The simulated dash
accepts whatever the app sends. The simulated game sends Forza "Dash"
packets to UDP port 5300, exactly as Forza would.

Everything here is simulated evidence: it checks the app's own logic and
interface, not the real hardware's timing or behaviour.
"""
import argparse
import math
import os
import runpy
import socket
import struct
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
sys.path.insert(0, APP_DIR)

import serial  # noqa: E402
import serial.tools.list_ports  # noqa: E402

from volgas_pedal_protocol import (  # noqa: E402
    CAL_FMT, CAL_SIZE, MSG_CALIBRATION, MSG_GET_CALIBRATION, MSG_SAVE, MSG_SAVE_ACK, MSG_SET_CALIBRATION,
    MSG_STATUS, PEDAL_PID, PEDAL_VID, STATUS_FMT, FrameParser, build_frame)

DASH_VID, DASH_PID = 0x303A, 0x1001
PEDAL_PORT, DASH_PORT = "SIM-PEDALS", "SIM-DASH"


class FakePortInfo:
    def __init__(self, device, vid, pid, description):
        self.device, self.vid, self.pid, self.description = device, vid, pid, description
        self.name = device


class Rig:
    """Which simulated devices are currently plugged in."""

    def __init__(self, pedals=True, dash=True):
        self.pedals = pedals
        self.dash = dash

    def ports(self):
        found = []
        if self.pedals:
            found.append(FakePortInfo(PEDAL_PORT, PEDAL_VID, PEDAL_PID, "Simulated Volgas pedals"))
        if self.dash:
            found.append(FakePortInfo(DASH_PORT, DASH_VID, DASH_PID, "Simulated Volgas dash"))
        return found


class FakePedals:
    """The pedal board: streams MSG_STATUS at 50 Hz and behaves like the
    firmware for calibration get/set/save."""

    def __init__(self, rig):
        self.rig = rig
        self.is_open = True
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        # The firmware's compiled-in defaults (pedal_set.ino `cal`).
        self.cal = [2180, 3000, -2000, -350000, 0, 44, 66, 84, 100, 0, 25, 50, 75, 100]
        self._parser = FrameParser(self._on_frame)
        threading.Thread(target=self._stream, name="sim-pedals", daemon=True).start()

    def _push(self, frame):
        with self._lock:
            self._buf += frame

    def _stream(self):
        while self.is_open:
            t = time.monotonic() - self._t0
            throttle_pos = max(0.0, math.sin(t * 1.3))
            brake_pos = max(0.0, math.sin(t * 1.3 + 2.6)) ** 2
            hall = int(self.cal[0] + (self.cal[1] - self.cal[0]) * throttle_pos)
            load = int(self.cal[2] + (self.cal[3] - self.cal[2]) * brake_pos)
            throttle_axis = int(1023 * self._curve(self.cal[4:9], throttle_pos))
            brake_axis = int(1023 * self._curve(self.cal[9:14], brake_pos))
            self._push(build_frame(MSG_STATUS, struct.pack(STATUS_FMT, hall, hall, load, throttle_axis, brake_axis)))
            time.sleep(0.02)

    @staticmethod
    def _curve(points, frac):
        pos = frac * 4
        i = min(int(pos), 3)
        return (points[i] + (points[i + 1] - points[i]) * (pos - i)) / 100.0

    def _on_frame(self, msg_type, payload):
        if msg_type == MSG_GET_CALIBRATION:
            self._push(build_frame(MSG_CALIBRATION, struct.pack(CAL_FMT, *self.cal)))
        elif msg_type == MSG_SET_CALIBRATION and len(payload) == CAL_SIZE:
            self.cal = list(struct.unpack(CAL_FMT, payload))
            self._push(build_frame(MSG_CALIBRATION, struct.pack(CAL_FMT, *self.cal)))
        elif msg_type == MSG_SAVE:
            self._push(build_frame(MSG_SAVE_ACK, b"\x01"))

    # pyserial-compatible surface used by the app
    def read(self, n=1):
        if not self.rig.pedals:
            raise serial.SerialException("simulated unplug")
        time.sleep(0.005)
        with self._lock:
            out = bytes(self._buf[:n])
            del self._buf[:n]
        return out

    def write(self, data):
        if not self.rig.pedals:
            raise serial.SerialException("simulated unplug")
        self._parser.feed(data)
        return len(data)

    def close(self):
        self.is_open = False

    @property
    def in_waiting(self):
        return len(self._buf)


class FakeDash:
    """The dash: accepts everything, reads nothing back (the real protocol
    is one-way, PC to dash)."""

    def __init__(self, rig):
        self.rig = rig
        self.is_open = True
        self.bytes_received = 0

    def write(self, data):
        if not self.rig.dash:
            raise serial.SerialException("simulated unplug")
        self.bytes_received += len(data)
        return len(data)

    def read(self, n=1):
        time.sleep(0.01)
        return b""

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        pass


def forza_sender(stop):
    """Send Forza Horizon 'Dash' packets to localhost:5300 at 60 Hz."""
    from volgas_forza_telemetry import ForzaDashPacket, ForzaUDPReader
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    t0 = time.monotonic()
    while not stop.is_set():
        t = time.monotonic() - t0
        pkt = ForzaDashPacket()
        pkt.IsRaceOn = 1
        pkt.EngineMaxRpm = 8000.0
        pkt.CurrentEngineRpm = 3000.0 + 4500.0 * (0.5 + 0.5 * math.sin(t * 0.9))
        pkt.Speed = (60 + 40 * math.sin(t * 0.3)) / 3.6
        pkt.Gear = 2 + int(2 + 2 * math.sin(t * 0.3))
        pkt.LapNumber = int(t // 90)
        sock.sendto(bytes(pkt), ("127.0.0.1", ForzaUDPReader.DEFAULT_PORT))
        stop.wait(1 / 60)


def install(rig):
    serial.tools.list_ports.comports = lambda *a, **k: rig.ports()

    def fake_serial(port=None, *a, **k):
        if port == PEDAL_PORT and rig.pedals:
            return FakePedals(rig)
        if port == DASH_PORT and rig.dash:
            return FakeDash(rig)
        raise serial.SerialException(f"could not open port {port!r} (simulated)")

    serial.Serial = fake_serial


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-pedals", action="store_true")
    ap.add_argument("--no-dash", action="store_true")
    ap.add_argument("--no-game", action="store_true")
    ap.add_argument("--unplug", type=float, metavar="SECONDS",
                    help="unplug the pedals after this many seconds, then plug them back in 10 s later")
    ap.add_argument("--keep-settings", action="store_true",
                    help="use your real settings folder instead of a temporary one")
    args = ap.parse_args()

    if not args.keep_settings:
        os.environ.setdefault("VOLGAS_DATA_DIR", tempfile.mkdtemp(prefix="volgas-sim-"))
        os.makedirs(os.environ["VOLGAS_DATA_DIR"], exist_ok=True)
        settings_file = os.path.join(os.environ["VOLGAS_DATA_DIR"], "volgas_settings.json")
        if not os.path.exists(settings_file):
            with open(settings_file, "w") as f:
                f.write('{"check_updates_on_start": false}')

    rig = Rig(pedals=not args.no_pedals, dash=not args.no_dash)
    install(rig)
    stop = threading.Event()
    if not args.no_game:
        threading.Thread(target=forza_sender, args=(stop,), name="sim-forza", daemon=True).start()
    if args.unplug:
        def unplug_cycle():
            stop.wait(args.unplug)
            rig.pedals = False
            stop.wait(10)
            rig.pedals = True
        threading.Thread(target=unplug_cycle, name="sim-unplug", daemon=True).start()

    os.chdir(APP_DIR)
    sys.argv = [os.path.join(APP_DIR, "volgas.py")]
    try:
        runpy.run_path(sys.argv[0], run_name="__main__")
    finally:
        stop.set()


if __name__ == "__main__":
    main()
