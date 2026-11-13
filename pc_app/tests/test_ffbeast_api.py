"""FFBeast report layouts, checked against wheel_api.h (64-byte payloads
after the report id) and against settings read from a real board."""
import struct

import pytest

hid = pytest.importorskip("hid")
import volgas_ffbeast_api as f  # noqa: E402


def test_report_formats_fit_in_64_bytes():
    assert struct.calcsize(f.EFFECT_SETTINGS_FMT) == 64 - 50   # 50 bytes of padding
    assert struct.calcsize(f.HARDWARE_SETTINGS_FMT) == 64 - 47
    assert struct.calcsize(f.DEVICE_STATE_FMT) == 64 - 55


def test_setting_report_layout():
    report = f.setting_report("MotionRange", 900)
    assert len(report) == 65
    assert report[:4] == bytes([0xA3, 0x14, 5, 0])
    assert struct.unpack_from("<H", report, 4)[0] == 900
    assert not any(report[6:])


def test_signed_setting():
    report = f.setting_report("EncoderDirection", -1)
    assert report[2] == 21 and report[4] == 0xFF


def test_command_report():
    assert f.command_report(f.DATA_COMMAND_SAVE_SETTINGS)[:3] == bytes([0xA3, 0x02, 0])


def test_parse_hardware_settings_from_a_real_board():
    # Read from an ODESC 4.2 on FFBeast RC.26.1.1 (2026-11-12).
    payload = struct.pack(f.HARDWARE_SETTINGS_FMT, 2880, 100, 10, 1, 0, 0, 5, 100, 52, 0, 0, 2, -1, 1, 15)
    settings = f.parse_hardware_settings(bytes([0x21]) + payload + bytes(47))
    assert settings["EncoderCPR"] == 2880
    assert settings["PowerLimit"] == 52
    assert settings["EncoderDirection"] == -1
    assert settings["PolePairs"] == 15


def test_parse_state():
    raw = bytes([0x01]) + struct.pack(f.DEVICE_STATE_FMT, 1, 26, 1, 1, 0, -395, 120) + bytes(55)
    state = f.parse_state(raw)
    assert (state["ReleaseMajor"], state["Position"], state["Torque"]) == (26, -395, 120)
