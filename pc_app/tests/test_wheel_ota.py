"""volgas_wheel_ota against a simulated hub + wheel that loses packets."""
import hashlib
import random
import struct

import pytest

import volgas_wheel_ota as ota


def test_frame_round_trip():
    reader = ota.FrameReader()
    data = b"\x00junk" + ota.build_frame(0x50, b"\x00\x10\x00\x00\x00") + ota.build_frame(0x51, b"abcdef")
    frames = reader.feed(data[:7]) + reader.feed(data[7:])
    assert frames == [(0x50, b"\x00\x10\x00\x00\x00"), (0x51, b"abcdef")]


class FakeHub:
    """Behaves like hub_s3 + the wheel's OTA code, dropping some requests and
    some answers at random."""

    def __init__(self, port, loss=0.2, seed=1):
        self.rng = random.Random(seed)
        self.loss = loss
        self.fw = 2
        self.received = bytearray()
        self.size = None
        self.md5 = None
        self.restarted = False
        self.ser = type("S", (), {"write": lambda *_: None})()

    def close(self):
        pass

    def wheel_status(self, timeout=0.5):
        return {"linked": True, "model": 1, "fw": self.fw, "buttons": 12, "rssi": -40}

    def ota(self, msg_type, payload=b"", timeout=0.3, tries=20):
        for _ in range(tries):
            if self.rng.random() < self.loss:
                continue   # request lost on the way
            answer = self._handle(msg_type, payload)
            if self.rng.random() < self.loss:
                continue   # answer lost on the way back (the wheel still acted on it)
            return answer
        raise ota.OtaError("no answer")

    def _handle(self, msg_type, payload):
        if msg_type == ota.USB_OTA_BEGIN:
            size = struct.unpack("<I", payload[:4])[0]
            if self.size != size:
                self.size, self.md5, self.received = size, payload[4:].decode(), bytearray()
            return ota.OTA_OK, 0
        if msg_type == ota.USB_OTA_DATA:
            offset = struct.unpack("<I", payload[:4])[0]
            if offset == len(self.received):
                self.received += payload[4:]
            return ota.OTA_OK, len(self.received)
        if msg_type == ota.USB_OTA_END:
            if hashlib.md5(self.received).hexdigest() != self.md5:
                return ota.OTA_VERIFY_FAILED, len(self.received)
            self.fw = 3
            return ota.OTA_DONE, len(self.received)
        return ota.OTA_OK, len(self.received)


@pytest.fixture
def fake(monkeypatch):
    hubs = []

    def make(port):
        hubs.append(FakeHub(port))
        return hubs[-1]

    monkeypatch.setattr(ota, "Hub", make)
    monkeypatch.setattr(ota.time, "sleep", lambda s: None)
    return hubs


def test_update_survives_lost_packets(fake):
    image = bytes(random.Random(5).randrange(256) for _ in range(10_000))
    old, new = ota.update_wheel(image, port="COMFAKE", log=lambda *_: None)
    assert (old, new) == (2, 3)
    assert bytes(fake[0].received) == image


def test_driving_is_refused(fake, monkeypatch):
    monkeypatch.setattr(FakeHub, "_handle", lambda self, t, p: (ota.OTA_DRIVING, 0))
    with pytest.raises(ota.OtaError, match="moving"):
        ota.update_wheel(b"x" * 500, port="COMFAKE", log=lambda *_: None)
