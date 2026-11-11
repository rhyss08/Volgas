"""Calibration wizard checks: what gets accepted before anything is written to the pedals."""
import pytest

from volgas_pedal_protocol import (BRAKE_LIMITS, THROTTLE_LIMITS, Capture, check_calibration, linear_fraction,
                                   summarise)


def cap(value, spread=0, count=10):
    return Capture(value=value, spread=spread, count=count)


def test_good_throttle_calibration_passes():
    assert check_calibration(cap(2180, 4), cap(3000, 6), THROTTLE_LIMITS) is None


def test_inverted_brake_range_passes():
    # The load cell reads more negative as force increases.
    assert check_calibration(cap(-2000, 300), cap(-350000, 900), BRAKE_LIMITS) is None


def test_identical_ends_rejected():
    msg = check_calibration(cap(2500), cap(2500), THROTTLE_LIMITS)
    assert msg and "barely changed" in msg


def test_too_little_travel_rejected():
    assert check_calibration(cap(2500), cap(2550), THROTTLE_LIMITS) is not None


def test_pedal_moving_during_capture_rejected():
    msg = check_calibration(cap(2180, 2), cap(3000, 400), THROTTLE_LIMITS)
    assert msg and "fully pressed" in msg


def test_too_few_samples_rejected():
    msg = check_calibration(cap(2180, count=1), cap(3000), THROTTLE_LIMITS)
    assert msg and "Too few" in msg


def test_summarise_uses_median_and_spread():
    c = summarise([2400, 2402, 2401, 9999, 2400])
    assert c.value == 2401 and c.spread == 9999 - 2400 and c.count == 5


def test_summarise_empty_raises():
    with pytest.raises(ValueError):
        summarise([])


@pytest.mark.parametrize("raw,lo,hi,expected", [
    (2180, 2180, 3000, 0.0), (3000, 2180, 3000, 1.0), (2590, 2180, 3000, 0.5),
    (100, 2180, 3000, 0.0), (5000, 2180, 3000, 1.0),          # clamped like the firmware
    (-176000, -2000, -350000, 0.5),                           # inverted range
    (1234, 2500, 2500, 0.0),                                  # zero-width range, like the firmware guard
])
def test_linear_fraction_matches_firmware_mapping(raw, lo, hi, expected):
    assert linear_fraction(raw, lo, hi) == pytest.approx(expected)
