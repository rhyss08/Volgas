"""PC side of the pedal board's serial protocol (see pedal_set/pedal_protocol.h),
plus the checks the calibration wizard applies before anything is written
to the pedals.

Kept free of any interface code so it can be tested without a window or the
hardware (see tests/).
"""
import statistics
import struct
from dataclasses import dataclass

import serial.tools.list_ports

# USBComposite defaults to LeafLabs' VID; PID is the one pedal_set.ino
# sets explicitly (bumped to 0x0003 when CDC serial was added). Used to
# find the Pedal Set's COM port automatically instead of picking by name.
PEDAL_VID = 0x1EAF
PEDAL_PID = 0x0003

SYNC0, SYNC1 = 0xAA, 0x55
MAX_PAYLOAD = 32  # matches the firmware parser's limit
MSG_STATUS = 0x01
MSG_SET_CALIBRATION = 0x02
MSG_SAVE = 0x03
MSG_GET_CALIBRATION = 0x04
MSG_CALIBRATION = 0x84
MSG_SAVE_ACK = 0x83

# Must match pedal_protocol.h byte-for-byte (packed, little-endian).
CAL_FMT = "<hhii5B5B"        # hallMin hallMax loadMin loadMax throttleCurve[5] brakeCurve[5]
STATUS_FMT = "<hhiHH"        # hallRaw hallFiltered loadRaw throttleAxis brakeAxis
CAL_SIZE = struct.calcsize(CAL_FMT)
STATUS_SIZE = struct.calcsize(STATUS_FMT)
AXIS_MAX = 1023              # the joystick axes are 10-bit


def find_pedal_port():
    """Return the COM port of the Pedal Set by USB VID/PID, or None."""
    for p in serial.tools.list_ports.comports():
        if p.vid == PEDAL_VID and p.pid == PEDAL_PID:
            return p.device
    return None


def build_frame(msg_type, payload=b""):
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(f"payload too long ({len(payload)} > {MAX_PAYLOAD})")
    checksum = (msg_type + len(payload) + sum(payload)) & 0xFF
    return bytes([SYNC0, SYNC1, msg_type, len(payload)]) + payload + bytes([checksum])


class FrameParser:
    """Same resync state machine as the firmware side. Bytes arrive in any
    chunking; on_frame(msg_type, payload) is called once per complete frame
    whose checksum matches. Corrupt or truncated frames are dropped."""

    def __init__(self, on_frame):
        self.on_frame = on_frame
        self.reset()

    def reset(self):
        self.state = "SYNC0"
        self.msg_type = 0
        self.length = 0
        self.payload = bytearray()
        self.checksum = 0

    def feed(self, data):
        for b in data:
            if self.state == "SYNC0":
                if b == SYNC0:
                    self.state = "SYNC1"
            elif self.state == "SYNC1":
                if b == SYNC1:
                    self.state = "TYPE"
                elif b != SYNC0:  # "AA AA 55" still syncs on the second AA
                    self.state = "SYNC0"
            elif self.state == "TYPE":
                self.msg_type = b
                self.checksum = b
                self.state = "LEN"
            elif self.state == "LEN":
                if b > MAX_PAYLOAD:
                    self.state = "SYNC0"
                    continue
                self.length = b
                self.checksum = (self.checksum + b) & 0xFF
                self.payload = bytearray()
                self.state = "PAYLOAD" if b else "CHECKSUM"
            elif self.state == "PAYLOAD":
                self.payload.append(b)
                self.checksum = (self.checksum + b) & 0xFF
                if len(self.payload) >= self.length:
                    self.state = "CHECKSUM"
            elif self.state == "CHECKSUM":
                if b == self.checksum:
                    self.on_frame(self.msg_type, bytes(self.payload))
                self.state = "SYNC0"


# ---------- calibration checks ----------

@dataclass(frozen=True)
class CalibrationLimits:
    """What counts as a usable calibration for one sensor.

    min_span: smallest acceptable distance between the rest and full-press
        readings, in the sensor's raw units. Below it the pedal would have
        almost no usable travel, and at zero the firmware's mapping divides
        by zero (output stuck at 0).
    max_noise_ratio: how much the readings may wander while the pedal is
        held still, as a fraction of the span. More than that means the
        pedal moved during the capture, or the sensor is unstable.
    min_samples: readings needed in a capture window.
    """
    sensor: str
    unit: str
    min_span: int
    max_noise_ratio: float = 0.15
    min_samples: int = 3


# Assumptions based on the firmware's compiled-in defaults (hall 2180-3000 of a
# 12-bit ADC; load cell 0 to about -350,000 counts) - roughly 1/8 and 1/35 of
# those default spans. Adjust here if real hardware needs different values.
THROTTLE_LIMITS = CalibrationLimits(sensor="hall", unit="ADC counts", min_span=100)
BRAKE_LIMITS = CalibrationLimits(sensor="load cell", unit="counts", min_span=10_000, min_samples=3)

CAPTURE_SECONDS = {"throttle": 0.6, "brake": 1.2}  # the brake sensor only reports about 10 times a second


@dataclass(frozen=True)
class Capture:
    value: int    # median of the window - robust against a single spike
    spread: int   # max - min within the window
    count: int


def summarise(samples):
    """Collapse a capture window into one reading. Raises ValueError when
    there is nothing to summarise."""
    if not samples:
        raise ValueError("no readings were received")
    return Capture(value=int(round(statistics.median(samples))),
                   spread=int(max(samples) - min(samples)), count=len(samples))


def check_calibration(rest, full, limits):
    """Return None if the two captures make a usable calibration, else a
    plain-language reason for the user. Rest and full may be in either
    order (the load cell reads more negative as force increases)."""
    for name, cap in (("at rest", rest), ("fully pressed", full)):
        if cap.count < limits.min_samples:
            return (f"Too few readings {name} ({cap.count}). "
                    "Check that the pedals are connected and try again.")
    span = abs(full.value - rest.value)
    if span < limits.min_span:
        return (f"The pedal barely changed between rest and full press "
                f"({span} {limits.unit}, at least {limits.min_span} needed). "
                "Make sure you press it all the way and hold it until the capture ends.")
    for name, cap in (("at rest", rest), ("fully pressed", full)):
        if cap.spread > limits.max_noise_ratio * span:
            return (f"The reading moved too much while the pedal was {name}. "
                    "Hold it completely still until the capture ends, then try again.")
    return None


def linear_fraction(raw, lo, hi):
    """Where a raw reading sits between two calibration points, 0..1 - the
    same clamped mapping the firmware applies before the curve. Works for
    inverted ranges; a zero-width range gives 0, like the firmware."""
    if hi == lo:
        return 0.0
    frac = (raw - lo) / (hi - lo)
    return max(0.0, min(1.0, frac))
