"""The date message and the wheel firmware version format."""
import struct
import time

import volgas_dash_bridge as bridge
import volgas_devices as devs


class Recorder:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(bytes(data))


def test_time_sync_is_followed_by_the_date(monkeypatch):
    fake = time.struct_time((2026, 11, 13, 14, 5, 9, 4, 317, 0))   # Friday 13 Nov 2026
    monkeypatch.setattr(bridge.time, "localtime", lambda: fake)
    rec = Recorder()
    bridge.send_time_sync(rec)
    assert [f[2] for f in rec.frames] == [bridge.MSG_TIME_SYNC, bridge.MSG_DATE_SYNC]
    date = rec.frames[1]
    assert date[3] == struct.calcsize(bridge.DATE_SYNC_FMT) == 4
    assert struct.unpack(bridge.DATE_SYNC_FMT, date[4:8]) == (13, 11, 2026)
    assert date[8] == (date[2] + date[3] + sum(date[4:8])) & 0xFF


def test_wheel_version_text_and_order():
    assert devs.format_wheel_version(5) == "5"
    assert devs.format_wheel_version(0x0501) == "5.1"
    assert devs.format_wheel_version(0) is None
    assert devs.compare(devs.format_wheel_version(5), "5.1") == devs.UPDATE
    assert devs.compare(devs.format_wheel_version(0x0501), "5.1") == devs.CURRENT
    assert devs.compare(devs.format_wheel_version(0x0501), "5") == devs.NEWER
    assert 0x0501 > 5   # the wheel's own raw numbers keep their order too
