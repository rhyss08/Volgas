"""Updating the pedals: the whole sequence against a simulated pedal board and a simulated
STM32CubeProgrammer, so every step and every failure can be exercised without hardware."""
import io
import os
import struct

import pytest

from conftest import REPO_DIR
import volgas_pedal_protocol as pp
import volgas_pedal_update as pu

DEFAULT_CAL = struct.pack(pp.CAL_FMT, 2180, 3000, -2000, -350000, 0, 44, 66, 84, 100, 0, 25, 50, 75, 100)
USER_CAL = struct.pack(pp.CAL_FMT, 2283, 2960, -1500, -280000, 0, 44, 66, 84, 100, 0, 30, 55, 80, 100)


def make_image(version=0x0100, size=4000):
    image = bytearray(size)
    image[0:4] = (0x20020000).to_bytes(4, "little")
    image[4:8] = (0x08000101).to_bytes(4, "little")
    image[32:34] = version.to_bytes(2, "little")      # where the simulated board reads "its" version from
    return bytes(image)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        self.now += 0.05
        return self.now


class FakeProc:
    def __init__(self, text, code):
        self.stdout, self.code = io.BytesIO(text.encode()), code

    def wait(self):
        return self.code

    def kill(self):
        pass


class Board:
    """The pedals: app firmware (optionally old, with no info message) or sitting in the USB bootloader."""

    def __init__(self, fw=None, cal=USER_CAL, restore_ok=True):
        self.mode, self.fw, self.cal, self.restore_ok = "app", fw, cal, restore_ok
        self.flash_exit_code, self.touched, self.programmer_calls, self.pending = 0, 0, [], cal

    def serial_factory(self, port, baud, timeout=None):
        assert self.mode == "app", "the port only exists while the app runs"
        return Board.Touch(self) if baud == 1200 else Board.Port(self)

    class Touch:
        def __init__(self, board):
            self.board = board

        def close(self):
            self.board.touched += 1
            self.board.mode = "dfu"

    class Port:
        def __init__(self, board):
            self.board, self.out = board, bytearray()
            self.parser = pp.FrameParser(self.on_frame)

        def write(self, data):
            self.parser.feed(data)

        def reply(self, t, payload):
            self.out += pp.build_frame(t, payload)

        def on_frame(self, t, p):
            b = self.board
            if t == pp.MSG_GET_CALIBRATION:
                self.reply(pp.MSG_CALIBRATION, b.cal)
            elif t == pp.MSG_GET_INFO and b.fw is not None:
                self.reply(pp.MSG_INFO, struct.pack(pp.INFO_FMT, b.fw, 0, 1, 2))
            elif t == pp.MSG_SET_CALIBRATION:
                b.pending = bytes(p)
                self.reply(pp.MSG_CALIBRATION, b.pending)
            elif t == pp.MSG_SAVE:
                if b.restore_ok:
                    b.cal = b.pending
                self.reply(pp.MSG_SAVE_ACK, b"\x01" if b.restore_ok else b"\x00")

        def read(self, n):
            data, self.out = bytes(self.out[:n]), self.out[n:]
            return data

        def close(self):
            pass

    def popen(self, args, **kwargs):
        self.programmer_calls.append(args)
        if args[1:3] == ["-l", "usb"]:
            if self.mode == "dfu":
                return FakeProc("=====  DFU Interface   =====\n  USB1\n", 0)
            return FakeProc("No STM32 device in DFU mode connected\n", 0)
        image = open(args[args.index("-w") + 1], "rb").read()
        if self.flash_exit_code == 0:
            self.fw, self.cal, self.mode = int.from_bytes(image[32:34], "little"), DEFAULT_CAL, "app"
            sweeps = "".join(f" {n}%\r" for n in range(0, 101, 5)) * 3   # erase, write and verify each count 0-100
            return FakeProc(sweeps + "\nDownload verified successfully\n", 0)
        return FakeProc("Error: could not open the device\n", self.flash_exit_code)

    def find_port(self):
        return "COM7" if self.mode == "app" else None


def run(board, image=None, port="COM7", work_dir=None):
    steps = []
    result = pu.update_pedals(image or make_image(), port=port, programmer="prog.exe",
                              progress=lambda f, t: steps.append((f, t)), serial_factory=board.serial_factory,
                              popen=board.popen, find_port=board.find_port, sleep=lambda s: None, clock=FakeClock(),
                              work_dir=work_dir)
    return result, steps


def test_update_from_old_firmware_keeps_the_calibration(tmp_path):
    board = Board(fw=None)
    (old, new, kept), steps = run(board, work_dir=str(tmp_path))
    assert (old, new, kept) == (None, 0x0100, True)
    assert board.touched == 1 and board.cal == USER_CAL      # touched once, calibration put back
    fractions = [f for f, _t in steps]
    assert fractions == sorted(fractions) and fractions[-1] == 1.0
    texts = [t for _f, t in steps]
    assert any(t.startswith("Erasing") for t in texts) and any(t.startswith("Checking") for t in texts)
    flash = [c for c in board.programmer_calls if "-w" in c][0]
    assert flash[flash.index("-e") + 1] == "all" and "-v" in flash and "0x8000000" in flash
    assert not list(tmp_path.iterdir())                       # the temporary .bin is cleaned up


def test_update_reports_the_old_version(tmp_path):
    (old, new, kept), _ = run(Board(fw=0x0100), make_image(0x0101), work_dir=str(tmp_path))
    assert (old, new, kept) == (0x0100, 0x0101, True)


def test_pedals_already_in_the_bootloader_are_finished_off(tmp_path):
    board = Board(fw=None)
    board.mode = "dfu"                                         # an earlier update stopped half way
    (old, new, kept), _ = run(board, port=None, work_dir=str(tmp_path))
    assert (old, new, kept) == (None, 0x0100, False) and board.touched == 0


def test_not_connected_is_an_error(tmp_path):
    board = Board()
    board.mode = "unplugged"                                   # neither the app nor the bootloader is on the bus
    with pytest.raises(pu.PedalUpdateError, match="aren't connected"):
        run(board, port=None, work_dir=str(tmp_path))


def test_missing_programmer_says_what_to_install(monkeypatch):
    monkeypatch.setattr(pu, "find_programmer", lambda: None)
    with pytest.raises(pu.PedalUpdateError, match="STM32CubeProgrammer"):
        pu.update_pedals(make_image(), port="COM7")


def test_a_failed_write_says_to_run_it_again(tmp_path):
    board = Board()
    board.flash_exit_code = 1
    with pytest.raises(pu.PedalUpdateError, match="run the update again"):
        run(board, work_dir=str(tmp_path))
    assert board.mode == "dfu"                                 # still in the bootloader, so a retry works


def test_calibration_that_cant_be_restored_is_reported(tmp_path):
    with pytest.raises(pu.PedalUpdateError, match="Calibrate the pedals again"):
        run(Board(restore_ok=False), work_dir=str(tmp_path))


@pytest.mark.parametrize("image", [
    b"", b"\x00" * 2000, make_image(size=0xC001),
    (0x20020000).to_bytes(4, "little") + (0x08000100).to_bytes(4, "little") + b"\x00" * 2000,   # reset vector not Thumb
    (0x08000000).to_bytes(4, "little") + (0x08000101).to_bytes(4, "little") + b"\x00" * 2000,   # stack not in RAM
])
def test_images_that_arent_pedal_firmware_are_refused(image):
    with pytest.raises(pu.PedalUpdateError):
        pu.check_image(image)


def test_the_built_firmware_passes_the_image_check():
    path = os.path.join(REPO_DIR, "pedal_set_f4", "build", "pedal_set_f4.ino.bin")
    if not os.path.exists(path):
        pytest.skip("firmware not built")
    with open(path, "rb") as f:
        pu.check_image(f.read())
