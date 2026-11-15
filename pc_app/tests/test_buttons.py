import re

import volgas_buttons as b
import volgas_wheel_ota as ota
from conftest import REPO_DIR  # noqa: F401  (pytest puts tests/ on the path)


def press(cal, raw, start, seconds=0.3):
    """Hold `raw` from `start` for `seconds`, then let go; returns the time after."""
    cal.feed(0, start)               # the wizard feeds the live state, so it always sees "let go" first
    t = start + 0.05
    while t < start + 0.05 + seconds:
        cal.feed(raw, t)
        t += 0.02
    cal.feed(0, t)
    return t + 0.05


def test_calibration_learns_each_button():
    cal = b.ButtonCalibration(3)
    t = 0.0
    for raw_bit in (5, 0, 9):          # button 1 is wired at matrix position 5, and so on
        t = press(cal, 1 << raw_bit, t)
    assert cal.done
    m = cal.result()
    assert (m[5], m[0], m[9]) == (0, 1, 2)
    assert m.count(b.UNUSED) == b.MAP_SIZE - 3


def test_a_brief_touch_or_two_buttons_do_not_count():
    cal = b.ButtonCalibration(2)
    cal.feed(0, 0.0)
    cal.feed(1 << 3, 0.1)
    cal.feed(0, 0.15)                   # let go after 50 ms: not held long enough
    assert cal.step == 0
    cal.feed((1 << 3) | (1 << 4), 0.3)
    assert cal.step == 0 and "one button at a time" in cal.message
    cal.feed(0, 0.4)
    assert cal.message == ""


def test_same_button_twice_is_refused_and_must_be_released_between():
    cal = b.ButtonCalibration(3)
    t = press(cal, 1 << 2, 0.0)
    assert cal.step == 1
    # holding the first button on into the next step doesn't assign it again
    cal.feed(1 << 2, t)
    cal.feed(1 << 2, t + 0.5)
    assert cal.step == 1 and "already button 1" in cal.message
    t = press(cal, 1 << 6, t + 1)
    assert cal.step == 2


def test_skip_leaves_a_number_unassigned():
    cal = b.ButtonCalibration(3)
    t = press(cal, 1 << 1, 0.0)
    cal.skip()
    t = press(cal, 1 << 4, t)
    assert cal.done
    m = cal.result()
    assert m[1] == 0 and m[4] == 2
    assert 1 not in m


def test_map_helpers():
    assert b.valid_map(b.identity_map())
    assert not b.valid_map([0] * 10)
    assert not b.valid_map([40] + [0] * 31)
    assert "not calibrated" in b.map_text(b.identity_map())
    assert "Calibrated" in b.map_text([0, 1] + [b.UNUSED] * 30)


def test_usb_ids_match_the_hub_firmware():
    with open(REPO_DIR + "/hub_s3/hub_s3.ino", encoding="utf-8") as f:
        text = f.read()
    ino = {name: int(value, 16) for name, value in re.findall(r"#define (USB_\w+)\s+(0x[0-9A-Fa-f]+)", text)}
    for name in ("USB_BTN_MAP_GET", "USB_BTN_MAP_SET", "USB_BTN_MAP", "USB_BUTTONS", "USB_WHEEL_INFO",
                 "USB_WHEEL_STATUS"):
        assert ino[name] == getattr(ota, name), name


def test_bridge_reads_the_hub_button_frames():
    import struct
    import volgas_dash_bridge as bridge
    br = bridge.DashBridge()
    status = struct.pack("<BBHBb", 1, 1, 5, 12, -40)
    br._on_hub_frame(ota.USB_WHEEL_STATUS, status)                       # an older hub: no button state
    assert br.hub_status[1]["buttons"] == 12 and br.hub_buttons is None
    br._on_hub_frame(ota.USB_WHEEL_STATUS, status + struct.pack("<II", 0b101, 0b11))
    assert br.hub_buttons[1:] == (0b101, 0b11)
    br._on_hub_frame(ota.USB_BUTTONS, struct.pack("<II", 0b1000, 0b10))
    assert br.hub_buttons[1:] == (0b1000, 0b10)
    br._on_hub_frame(ota.USB_BTN_MAP, bytes(range(32)))
    assert br.hub_btn_map == list(range(32))
    br._on_hub_frame(ota.USB_BTN_MAP, b"short")                          # malformed: ignored
    assert br.hub_btn_map == list(range(32))
    br.send_to_hub(ota.USB_BTN_MAP_SET, bytes(32))
    assert br._to_hub.get_nowait() == (ota.USB_BTN_MAP_SET, bytes(32))


def test_the_wheel_has_twelve_parts_and_clicks_find_them():
    assert len(b.PARTS) == 12 and b.valid_parts(b.DEFAULT_PARTS, 12)
    assert not b.valid_parts([0, 1], 12) and not b.valid_parts([99] * 12, 12)
    assert b.valid_parts([None] * 12, 12)
    # the photo is padded top and bottom to a square, so its centre stays the centre of the square
    assert b.square_point(b.PHOTO_W / 2, b.PHOTO_H / 2) == (0.5, 0.5)
    for part in range(12):                       # a click on a part's own centre finds that part
        assert b.part_at(*b.part_centre(part)) == part
    assert b.part_at(*b.square_point(682, 440)) is None      # the logo, between the buttons
    assert b.part_at(*b.square_point(375 + 40, 330)) == 0    # just off the orange cap still finds it
    assert b.part_at(*b.square_point(300, 440)) == 10         # inside the left paddle
    assert b.part_at(*b.square_point(1065, 440)) == 11        # and the right one


def test_rotation_follows_the_photo():
    dx, dy = b.rotator(90)(1.0, 0.5)             # a point at the right edge, wheel turned 90 degrees right
    assert abs(dx) < 1e-9 and abs(dy - 0.5) < 1e-9   # ends up at the bottom
    dx, dy = b.rotator(180)(0.5, 0.0)
    assert abs(dx) < 1e-9 and abs(dy - 0.5) < 1e-9
    assert b.rotator(0)(0.3, 0.8) == (0.3 - 0.5, 0.8 - 0.5)
