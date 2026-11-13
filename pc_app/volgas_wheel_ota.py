"""Wireless firmware update for the wheel, through the hub.

The PC sends the image to the hub over USB serial one chunk at a time; the
hub radios each chunk to the wheel and passes back the wheel's answer, which
names the offset it wants next (see volgas_link.h and hub_s3.ino). A lost
chunk or answer is just sent again. When everything has arrived the wheel
checks the MD5, switches to the new firmware and restarts; it keeps the new
firmware only once it links to the hub again (otherwise it goes back to the
old one by itself).

Volgas's dash bridge holds the hub's port while Volgas runs, so close Volgas
(or pause the bridge) before updating.

Usage: python volgas_wheel_ota.py <firmware.bin> [--port COMx]
"""
import argparse
import hashlib
import struct
import sys
import time

import serial
import serial.tools.list_ports

ESP32_VID, ESP32_PID = 0x303A, 0x1001
SYNC0, SYNC1 = 0xAA, 0x55
USB_OTA_BEGIN, USB_OTA_DATA, USB_OTA_END, USB_OTA_ABORT = 0x40, 0x41, 0x42, 0x43
USB_WHEEL_INFO, USB_OTA_ACK, USB_WHEEL_STATUS = 0x44, 0x50, 0x51
CHUNK = 200

OTA_OK, OTA_DRIVING, OTA_BEGIN_FAILED, OTA_WRITE_FAILED, OTA_VERIFY_FAILED, OTA_NOT_STARTED, OTA_DONE = range(7)
STATUS_TEXT = {
    OTA_DRIVING: "the wheel refused: the car is moving",
    OTA_BEGIN_FAILED: "the wheel couldn't start the update (does the image fit?)",
    OTA_WRITE_FAILED: "the wheel couldn't write the firmware",
    OTA_VERIFY_FAILED: "the firmware arrived damaged or isn't a valid image (MD5/image check failed)",
    OTA_NOT_STARTED: "the wheel lost track of the update",
}


class OtaError(Exception):
    pass


def build_frame(msg_type, payload=b""):
    checksum = (msg_type + len(payload) + sum(payload)) & 0xFF
    return bytes([SYNC0, SYNC1, msg_type, len(payload)]) + payload + bytes([checksum])


class FrameReader:
    """Splits the hub's replies into (type, payload) frames."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, data):
        self.buf += data
        frames = []
        while True:
            start = self.buf.find(bytes([SYNC0, SYNC1]))
            if start < 0:
                self.buf = self.buf[-1:]
                return frames
            del self.buf[:start]
            if len(self.buf) < 5 or len(self.buf) < 5 + self.buf[3]:
                return frames
            msg_type, length = self.buf[2], self.buf[3]
            payload = bytes(self.buf[4:4 + length])
            if (msg_type + length + sum(payload)) & 0xFF == self.buf[4 + length]:
                frames.append((msg_type, payload))
                del self.buf[:5 + length]
            else:
                del self.buf[:2]


class Hub:
    def __init__(self, port):
        self.ser = serial.Serial(port, 115200, timeout=0.02)
        self.reader = FrameReader()

    def close(self):
        self.ser.close()

    def request(self, msg_type, payload, want, timeout):
        """Send one frame and wait for a reply of type `want`; None on timeout."""
        self.ser.reset_input_buffer()
        self.reader = FrameReader()
        self.ser.write(build_frame(msg_type, payload))
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for t, p in self.reader.feed(self.ser.read(256)):
                if t == want:
                    return p
        return None

    def wheel_status(self, timeout=0.5):
        p = self.request(USB_WHEEL_INFO, b"", USB_WHEEL_STATUS, timeout)
        if p is None or len(p) < 6:
            return None
        linked, model, fw, buttons, rssi = struct.unpack("<BBHBb", p[:6])
        return {"linked": bool(linked), "model": model, "fw": fw, "buttons": buttons, "rssi": rssi}

    def ota(self, msg_type, payload=b"", timeout=0.3, tries=20):
        """Send an update command until the wheel answers; returns (status, next_offset)."""
        for _ in range(tries):
            p = self.request(msg_type, payload, USB_OTA_ACK, timeout)
            if p is not None and len(p) >= 5:
                return p[0], struct.unpack("<I", p[1:5])[0]
        raise OtaError("the wheel stopped answering - is it powered and near the hub?")


def find_hub_port():
    """The hub is the Espressif board that answers a wheel-info request (the
    wheel's own USB port ignores it)."""
    for p in serial.tools.list_ports.comports():
        if p.vid != ESP32_VID or p.pid != ESP32_PID:
            continue
        try:
            hub = Hub(p.device)
        except serial.SerialException:
            continue
        try:
            if hub.wheel_status() is not None:
                return p.device
        finally:
            hub.close()
    return None


def update_wheel(image, port=None, progress=lambda done, total: None, log=print):
    """Send `image` (bytes of an esp32_dash .bin) to the wheel. Returns
    (old_fw_version, new_fw_version)."""
    port = port or find_hub_port()
    if not port:
        raise OtaError("no hub found (is Volgas closed? it holds the hub's port)")
    hub = Hub(port)
    try:
        status = hub.wheel_status()
        if not status or not status["linked"]:
            raise OtaError("the hub doesn't hear the wheel - power it and check it shows 'hub: ok'")
        old_fw = status["fw"]
        log(f"hub on {port}; wheel firmware {old_fw}, signal {status['rssi']} dBm")

        md5 = hashlib.md5(image).hexdigest().encode("ascii")
        st, _ = hub.ota(USB_OTA_BEGIN, struct.pack("<I", len(image)) + md5, timeout=1.0, tries=5)
        if st != OTA_OK:
            raise OtaError(STATUS_TEXT.get(st, f"the wheel refused (status {st})"))

        offset, started, last_report = 0, time.monotonic(), 0.0
        while offset < len(image):
            chunk = image[offset:offset + CHUNK]
            st, nxt = hub.ota(USB_OTA_DATA, struct.pack("<I", offset) + chunk)
            if st != OTA_OK:
                raise OtaError(STATUS_TEXT.get(st, f"the wheel stopped the update (status {st})"))
            offset = nxt
            progress(offset, len(image))
            if time.monotonic() - last_report > 2:
                log(f"  {offset * 100 // len(image)}%  ({offset} / {len(image)} bytes)")
                last_report = time.monotonic()
        log(f"sent in {time.monotonic() - started:.1f} s; checking...")

        try:
            st, _ = hub.ota(USB_OTA_END, timeout=3.0, tries=3)
        except OtaError:
            st = OTA_DONE   # it may have restarted before its answer got out - the version check below decides
        if st not in (OTA_DONE, OTA_OK):
            raise OtaError(STATUS_TEXT.get(st, f"the wheel rejected the firmware (status {st})"))

        log("restarting the wheel...")
        time.sleep(3)
        end = time.monotonic() + 40
        last = None
        while time.monotonic() < end:
            s = hub.wheel_status()
            if s and s["linked"]:
                last = s
                if s["fw"] != old_fw:
                    log(f"the wheel is back on firmware {s['fw']}")
                    return old_fw, s["fw"]
            time.sleep(1)
        if last is not None:
            raise OtaError(f"the wheel restarted but is still on firmware {last['fw']}: the file has the same "
                           "version number as the one already installed")
        raise OtaError("the wheel didn't come back on the new firmware (it may have gone back to the old one)")
    except Exception:
        try:
            hub.ser.write(build_frame(USB_OTA_ABORT))
        except serial.SerialException:
            pass
        raise
    finally:
        hub.close()


def main():
    ap = argparse.ArgumentParser(description="Update the wheel's firmware wirelessly through the hub.")
    ap.add_argument("image", help="esp32_dash .bin built by arduino-cli")
    ap.add_argument("--port", help="the hub's COM port (found automatically if left out)")
    args = ap.parse_args()
    with open(args.image, "rb") as f:
        image = f.read()
    try:
        old, new = update_wheel(image, args.port)
    except (OtaError, serial.SerialException) as e:
        sys.exit(f"update failed: {e}")
    print(f"done: firmware {old} -> {new}")


if __name__ == "__main__":
    main()
