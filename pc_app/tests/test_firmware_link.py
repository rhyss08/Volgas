"""The hub and the wheel firmware each carry a copy of volgas_link.h (Arduino
sketches can't share a file); the two copies must stay identical."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_link_header_copies_match():
    with open(os.path.join(ROOT, "esp32_dash", "volgas_link.h"), "rb") as wheel, \
            open(os.path.join(ROOT, "hub_s3", "volgas_link.h"), "rb") as hub:
        assert wheel.read() == hub.read()
