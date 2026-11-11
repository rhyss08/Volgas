"""Pedal serial protocol: framing, parsing and agreement with the firmware header."""
import os
import re
import struct

import pytest

from conftest import REPO_DIR
from volgas_pedal_protocol import (
    CAL_FMT, CAL_SIZE, MAX_PAYLOAD, MSG_CALIBRATION, MSG_STATUS, STATUS_FMT, STATUS_SIZE, SYNC0, SYNC1,
    FrameParser, build_frame)


def collect():
    frames = []
    return frames, FrameParser(lambda t, p: frames.append((t, p)))


def test_round_trip():
    frames, parser = collect()
    payload = struct.pack(STATUS_FMT, 2400, 2401, -120000, 512, 300)
    parser.feed(build_frame(MSG_STATUS, payload))
    assert frames == [(MSG_STATUS, payload)]


def test_byte_at_a_time_and_back_to_back():
    frames, parser = collect()
    data = build_frame(MSG_STATUS, b"\x01\x02") + build_frame(MSG_CALIBRATION, b"")
    for b in data:
        parser.feed(bytes([b]))
    assert frames == [(MSG_STATUS, b"\x01\x02"), (MSG_CALIBRATION, b"")]


def test_bad_checksum_dropped_and_parser_recovers():
    frames, parser = collect()
    bad = bytearray(build_frame(MSG_STATUS, b"\x05"))
    bad[-1] ^= 0xFF
    parser.feed(bytes(bad) + build_frame(MSG_STATUS, b"\x06"))
    assert frames == [(MSG_STATUS, b"\x06")]


def test_garbage_before_frame_and_repeated_sync_byte():
    frames, parser = collect()
    parser.feed(b"\x00\x13\xAA" + build_frame(MSG_STATUS, b"\x07"))  # stray AA right before the real AA 55
    assert frames == [(MSG_STATUS, b"\x07")]


def test_oversized_length_is_rejected():
    frames, parser = collect()
    parser.feed(bytes([SYNC0, SYNC1, MSG_STATUS, MAX_PAYLOAD + 1]) + b"\x00" * 40 + build_frame(MSG_STATUS, b"\x08"))
    assert frames[-1] == (MSG_STATUS, b"\x08")


def test_build_frame_refuses_oversized_payload():
    with pytest.raises(ValueError):
        build_frame(MSG_STATUS, b"\x00" * (MAX_PAYLOAD + 1))


C_TYPES = {"int16_t": "h", "uint16_t": "H", "int32_t": "i", "uint32_t": "I", "uint8_t": "B", "int8_t": "b"}


def struct_format_from_header(name):
    """Turn a packed struct in pedal_protocol.h into a Python struct format."""
    with open(os.path.join(REPO_DIR, "pedal_set", "pedal_protocol.h")) as f:
        text = f.read()
    body = re.search(r"struct __attribute__\(\(packed\)\) " + name + r" \{(.*?)\};", text, re.S).group(1)
    fmt = "<"
    for ctype, field, count in re.findall(r"(\w+_t)\s+(\w+)(?:\[(\d+)\])?;", body):
        fmt += (count or "") + C_TYPES[ctype]
    return fmt


def test_formats_match_firmware_header():
    assert struct.calcsize(struct_format_from_header("CalibrationData")) == CAL_SIZE
    assert struct_format_from_header("CalibrationData").replace("<", "") == CAL_FMT.replace("<", "")
    assert struct_format_from_header("StatusPacket").replace("<", "") == STATUS_FMT.replace("<", "")
    assert STATUS_SIZE == 12 and CAL_SIZE == 22
