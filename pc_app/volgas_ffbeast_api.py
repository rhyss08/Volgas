"""FFBeast wheelbase vendor HID protocol - Python port of FFBeast's own
wheel_api.h/wheel_api.cpp (ffbeast-wheel-api-lib, identical in RC.24.1.5 and
RC.26.1.1, ffbeast.github.io/docs/en/downloads_wheel.html). Byte layout
verified against a real ODESC 4.2 running FFBeast.

Settings written with send_setting() only change the board's RAM; they are
lost when it restarts unless save_and_reboot() stores them.

Requires: pip install hidapi
"""

import struct

import hid

VID = 1115
WHEEL_PID = 22999
INTERFACE_VENDOR = 0  # interface 1 is the standard DirectInput joystick

REPORT_HARDWARE_SETTINGS_FEATURE = 0x21
REPORT_EFFECT_SETTINGS_FEATURE = 0x22
REPORT_GENERIC_INPUT_OUTPUT = 0xA3

DATA_COMMAND_REBOOT = 0x01
DATA_COMMAND_SAVE_SETTINGS = 0x02
DATA_COMMAND_DFU_MODE = 0x03
DATA_COMMAND_RESET_CENTER = 0x04
DATA_SETTINGS_FIELD_DATA = 0x14

# name -> (SettingsFieldEnum id, struct format char). The type matches the
# field in EffectSettingsTypeDef / HardwareSettingsTypeDef.
SETTINGS_FIELDS = {
    # EffectSettingsTypeDef
    "DirectXConstantDirection": (0, "b"),
    "DirectXSpringStrength": (1, "B"),
    "DirectXConstantStrength": (2, "B"),
    "DirectXPeriodicStrength": (3, "B"),
    "TotalEffectStrength": (4, "B"),
    "MotionRange": (5, "H"),
    "SoftStopStrength": (6, "B"),
    "SoftStopRange": (7, "B"),
    "StaticDampeningStrength": (8, "H"),
    "SoftStopDampeningStrength": (9, "H"),
    "IntegratedSpringStrength": (43, "B"),
    # HardwareSettingsTypeDef
    "ForceEnabled": (11, "B"),
    "DebugTorque": (12, "B"),
    "AmplifierGain": (13, "B"),
    "CalibrationMagnitude": (15, "B"),
    "CalibrationSpeed": (16, "B"),
    "PowerLimit": (17, "B"),
    "BrakingLimit": (18, "B"),
    "PositionSmoothing": (19, "B"),
    "SpeedBufferSize": (20, "B"),
    "EncoderDirection": (21, "b"),
    "ForceDirection": (22, "b"),
    "PolePairs": (23, "B"),
    "EncoderCPR": (24, "H"),
    "ProportionalGain": (25, "B"),
    "IntegralGain": (26, "H"),
}

# Feature report payloads, after the leading report-id byte.
EFFECT_SETTINGS_FMT = "<HHHBBBBbBBB"
EFFECT_SETTINGS_KEYS = (
    "MotionRange", "StaticDampeningStrength", "SoftStopDampeningStrength",
    "TotalEffectStrength", "IntegratedSpringStrength", "SoftStopRange", "SoftStopStrength",
    "DirectXConstantDirection", "DirectXSpringStrength", "DirectXConstantStrength", "DirectXPeriodicStrength",
)

HARDWARE_SETTINGS_FMT = "<HHBBBBBBBBBBbbB"
HARDWARE_SETTINGS_KEYS = (
    "EncoderCPR", "IntegralGain", "ProportionalGain", "ForceEnabled", "DebugTorque",
    "AmplifierGain", "CalibrationMagnitude", "CalibrationSpeed", "PowerLimit", "BrakingLimit",
    "PositionSmoothing", "SpeedBufferSize", "EncoderDirection", "ForceDirection", "PolePairs",
)

# DeviceStateTypeDef, streamed as an interrupt IN report (also report-id prefixed).
DEVICE_STATE_FMT = "<BBBBBhh"
DEVICE_STATE_KEYS = (
    "ReleaseType", "ReleaseMajor", "ReleaseMinor", "ReleasePatch", "IsRegistered", "Position", "Torque",
)


class FFBeastError(Exception):
    pass


def setting_report(name, value):
    """The 65-byte output report that writes one settings field."""
    field_id, fmt = SETTINGS_FIELDS[name]
    buf = bytearray(65)
    buf[0] = REPORT_GENERIC_INPUT_OUTPUT
    buf[1] = DATA_SETTINGS_FIELD_DATA
    buf[2] = field_id
    buf[3] = 0  # index - none of these fields are arrays
    struct.pack_into("<" + fmt, buf, 4, value)
    return bytes(buf)


def command_report(data_command):
    buf = bytearray(65)
    buf[0] = REPORT_GENERIC_INPUT_OUTPUT
    buf[1] = data_command
    return bytes(buf)


def parse_effect_settings(raw):
    return dict(zip(EFFECT_SETTINGS_KEYS, struct.unpack_from(EFFECT_SETTINGS_FMT, bytes(raw), 1)))


def parse_hardware_settings(raw):
    return dict(zip(HARDWARE_SETTINGS_KEYS, struct.unpack_from(HARDWARE_SETTINGS_FMT, bytes(raw), 1)))


def parse_state(raw):
    return dict(zip(DEVICE_STATE_KEYS, struct.unpack_from(DEVICE_STATE_FMT, bytes(raw), 1)))


def find_vendor_path():
    """hidapi path of the FFBeast vendor interface, or None."""
    for d in hid.enumerate(VID, WHEEL_PID):
        if d.get("interface_number") == INTERFACE_VENDOR:
            return d.get("path")
    return None


class FFBeastWheel:
    """The WheelApi class from wheel_api.h, on the hidapi package. Not
    thread-safe: one thread should own an instance."""

    def __init__(self):
        self._h = None

    @property
    def connected(self):
        return self._h is not None

    def connect(self):
        path = find_vendor_path()
        if not path:
            return False
        h = hid.device()
        h.open_path(path)
        self._h = h
        return True

    def close(self):
        if self._h is not None:
            try:
                self._h.close()
            except OSError:
                pass
            self._h = None

    def _require(self):
        if self._h is None:
            raise FFBeastError("not connected")

    def _write(self, report):
        self._require()
        if self._h.write(report) < 0:
            raise FFBeastError("write failed")

    def read_effect_settings(self):
        self._require()
        return parse_effect_settings(self._h.get_feature_report(REPORT_EFFECT_SETTINGS_FEATURE, 65))

    def read_hardware_settings(self):
        self._require()
        return parse_hardware_settings(self._h.get_feature_report(REPORT_HARDWARE_SETTINGS_FEATURE, 65))

    def read_state(self, timeout_ms=100):
        """One DeviceStateTypeDef report (position/torque), or None if none arrived."""
        self._require()
        raw = self._h.read(65, timeout_ms)
        return parse_state(raw) if raw else None

    def send_setting(self, name, value):
        self._write(setting_report(name, value))

    def reboot(self):
        self._write(command_report(DATA_COMMAND_REBOOT))

    def save_and_reboot(self):
        self._write(command_report(DATA_COMMAND_SAVE_SETTINGS))

    def switch_to_dfu(self):
        self._write(command_report(DATA_COMMAND_DFU_MODE))

    def reset_center(self):
        self._write(command_report(DATA_COMMAND_RESET_CENTER))
