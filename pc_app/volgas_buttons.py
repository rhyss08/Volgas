"""Wheel button calibration: which physical button is which button number.

The wheel scans a matrix and reports a raw bit per matrix position (bit =
row * columns + column). The hub turns those into the button numbers games
see through a map, raw bit -> button number, stored in the hub (hub_s3.ino).
Calibrating means pressing each button when asked, so Volgas can learn which
raw bit belongs to which number whatever way the matrix was wired.
"""

import math

MAP_SIZE = 32          # raw bits the hub maps
UNUSED = 0xFF          # map entry: this raw bit isn't a button

# The wheel photo (assets/volgas_wheel.png) is 1365x768; the app pads it to a
# square (volgas.py _load_wheel_image) so it can turn about its centre. Shapes
# are kept as fractions of that square: x from the left, y from the top.
PHOTO_W, PHOTO_H = 1365, 768


def square_point(x, y):
    """A pixel of the wheel photo as a fraction of the padded square."""
    side = max(PHOTO_W, PHOTO_H)
    return ((x + (side - PHOTO_W) / 2) / side, (y + (side - PHOTO_H) / 2) / side)


_SIDE = max(PHOTO_W, PHOTO_H)
# The ten round buttons: centre of the coloured (or black) cap, and its radius, in photo pixels.
_CAP_CENTRES = ((372.5, 330.5), (503, 329), (857, 323), (994.5, 326),   # orange, yellow, red, blue
                (462, 224), (906, 224),                                 # the two black ones at the top
                (432, 499.5), (926, 503),                               # red, green
                (527, 631), (828.5, 636))                               # black, white at the bottom (measured)
_CAP_RADIUS = 25
# The left shifter paddle's outline in photo pixels, and the right one's: traced separately, because the two plates
# aren't mirror images in the photo (the right one has a notch at the bottom left and a different top edge).
_LEFT_PADDLE = ((279, 356), (305, 358), (313, 372), (328, 386), (345, 398), (360, 403), (366, 415), (365, 430),
                (358, 460), (350, 483), (348, 498), (300, 498), (283, 490), (273, 475), (269, 450), (269, 415),
                (272, 390), (277, 365))

_RIGHT_PADDLE = ((1066, 356), (1082, 353), (1098, 348), (1114, 343.5), (1118, 343.5), (1120, 368),
                 (1124, 396), (1127, 420), (1127, 456), (1126, 466), (1123, 474), (1118, 480),
                 (1111, 486), (1100, 491), (1088, 493), (1040, 493), (1034, 492), (1031, 482),
                 (1019, 479), (1017, 476), (1012, 460), (1006, 440), (1002, 426), (1000, 418),
                 (1002, 409), (1012, 402), (1023, 399), (1030, 395), (1036, 389), (1050, 375),
                 (1065, 359))

# The 12 parts of the wheel that can be a button: ("circle", x, y, radius) or ("poly", [(x, y), ...]).
PARTS = ([("circle", *square_point(x, y), _CAP_RADIUS / _SIDE) for x, y in _CAP_CENTRES]
         + [("poly", [square_point(x, y) for x, y in _LEFT_PADDLE]),
            ("poly", [square_point(x, y) for x, y in _RIGHT_PADDLE])])

# Which part each button number is: button n is part n-1 (the calibration asks for the buttons in this order).
DEFAULT_PARTS = list(range(len(PARTS)))


def valid_parts(p, count):
    return (isinstance(p, (list, tuple)) and len(p) == count
            and all(v is None or (isinstance(v, int) and 0 <= v < len(PARTS)) for v in p))


def _segment_distance(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def part_distance(part, fx, fy):
    """How far (as a fraction of the image) a point is from a part: 0 on or inside it."""
    shape = PARTS[part]
    if shape[0] == "circle":
        return max(0.0, math.hypot(fx - shape[1], fy - shape[2]) - shape[3])
    pts = shape[1]
    edges = list(zip(pts, pts[1:] + pts[:1]))
    inside = False
    for (ax, ay), (bx, by) in edges:
        if (ay > fy) != (by > fy) and fx < ax + (fy - ay) / (by - ay) * (bx - ax):
            inside = not inside
    if inside:
        return 0.0
    return min(_segment_distance(fx, fy, ax, ay, bx, by) for (ax, ay), (bx, by) in edges)


def part_at(fx, fy, tolerance=0.03):
    """The part of the wheel nearest a click at (fx, fy), or None if nothing is within tolerance."""
    best = min(range(len(PARTS)), key=lambda i: part_distance(i, fx, fy))
    return best if part_distance(best, fx, fy) <= tolerance else None


def part_centre(part):
    shape = PARTS[part]
    if shape[0] == "circle":
        return shape[1], shape[2]
    pts = shape[1]
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def rotator(angle_deg):
    """A function turning a point of the square wheel image into its offset from the image centre, in image
    fractions, once the wheel has turned angle_deg clockwise (the photo is rotated that way as the wheel turns
    right; y runs downwards)."""
    a = math.radians(angle_deg)
    cos_a, sin_a = math.cos(a), math.sin(a)

    def turn(fx, fy):
        dx, dy = fx - 0.5, fy - 0.5
        return dx * cos_a - dy * sin_a, dx * sin_a + dy * cos_a
    return turn


def identity_map():
    """Wiring order: raw bit n is button n + 1."""
    return list(range(MAP_SIZE))


def valid_map(m):
    return len(m) == MAP_SIZE and all(v == UNUSED or 0 <= v < MAP_SIZE for v in m)


def map_text(m):
    """A short description of a stored map, for the Settings card."""
    if list(m) == identity_map():
        return "Wiring order (not calibrated)"
    used = sum(1 for v in m if v != UNUSED)
    return f"Calibrated ({used} buttons)"


class ButtonCalibration:
    """Asks for button 1, 2, 3... in turn and learns which raw bit each one is.

    feed(raw, now) takes the wheel's raw button mask every time it changes
    (and now and then in between). A press counts once exactly one new bit has
    been held for HOLD_S; the next button is only asked for after everything
    has been let go. message holds a hint while something's wrong (two buttons
    at once, a button that was already assigned).
    """

    HOLD_S = 0.12

    def __init__(self, count):
        self.count = count
        self.step = 0              # the button number being asked for, from 0
        self.assigned = {}         # raw bit -> button number (from 0)
        self.message = ""
        self._candidate = None     # (raw bit, when it first showed alone)
        self._wait_release = True  # start from "everything let go"

    @property
    def done(self):
        return self.step >= self.count

    def feed(self, raw, now):
        """True when a button was accepted by this call."""
        if self.done:
            return False
        if raw == 0:
            self._wait_release = False
            self._candidate = None
            self.message = ""
            return False
        if self._wait_release:
            return False
        bits = [i for i in range(MAP_SIZE) if raw >> i & 1]
        if len(bits) != 1:
            self.message = "Press one button at a time."
            self._candidate = None
            return False
        bit = bits[0]
        if bit in self.assigned:
            self.message = f"That one is already button {self.assigned[bit] + 1}. Press a different button."
            self._candidate = None
            return False
        if self._candidate is None or self._candidate[0] != bit:
            self._candidate = (bit, now)
            return False
        if now - self._candidate[1] < self.HOLD_S:
            return False
        self.assigned[bit] = self.step
        self.step += 1
        self.message = ""
        self._candidate = None
        self._wait_release = True
        return True

    def skip(self):
        """Leave this button number without a physical button."""
        if not self.done:
            self.step += 1
            self._candidate = None
            self.message = ""

    def result(self):
        """The map to send to the hub: calibrated raw bits get their number,
        every other raw bit is unused."""
        m = [UNUSED] * MAP_SIZE
        for raw, number in self.assigned.items():
            m[raw] = number
        return m
