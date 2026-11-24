"""Updating the Black Pill pedals' firmware from Volgas.

The pedals can restart themselves into the STM32's built-in USB bootloader
(opening their serial port at 1200 baud and closing it), so no button is
needed. The flashing itself is done by ST's own command-line tool,
STM32CubeProgrammer, which Volgas finds and runs.

The whole update:
  1. read the pedals' calibration (the update erases the whole flash, and the
     calibration lives in it) and their firmware version
  2. 1200 baud touch -> the pedals reappear as an "STM32 BOOTLOADER" device
  3. erase, write, verify, start the new firmware
  4. wait for the pedals to come back, put the calibration back and check it

If a step fails after the erase, the pedals stay in the bootloader, where
they can't be damaged: running the update again finds them there and
finishes the job.
"""
import os
import re
import shutil
import subprocess
import time

import serial

import volgas_pedal_protocol as pp

PROGRAMMER = "STM32_Programmer_CLI.exe"
PROGRAMMER_DIRS = (
    r"C:\Program Files\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin",
    r"C:\Program Files (x86)\STMicroelectronics\STM32Cube\STM32CubeProgrammer\bin",
)
FLASH_START = 0x08000000
FIRMWARE_LIMIT = 0x0800C000 - FLASH_START   # the saved calibration starts here (sectors 3 and 4)
RAM_START, RAM_END = 0x20000000, 0x20020000
MISSING_PROGRAMMER = ("Updating the pedals needs ST's free STM32CubeProgrammer, which isn't installed. "
                      "Install it from st.com, then try again.")


class PedalUpdateError(Exception):
    pass


def find_programmer():
    """Path of STM32_Programmer_CLI.exe: on PATH, or in its usual install folder. None if absent."""
    found = shutil.which(PROGRAMMER)
    if found:
        return found
    for folder in PROGRAMMER_DIRS:
        path = os.path.join(folder, PROGRAMMER)
        if os.path.isfile(path):
            return path
    return None


def check_image(image):
    """Raise PedalUpdateError unless these bytes look like firmware for the Black Pill, small enough
    not to run into the saved calibration."""
    if len(image) < 1024:
        raise PedalUpdateError("that file is too small to be pedal firmware")
    if len(image) > FIRMWARE_LIMIT:
        raise PedalUpdateError(f"that firmware is {len(image)} bytes; the pedals have room for {FIRMWARE_LIMIT}")
    stack = int.from_bytes(image[0:4], "little")
    reset = int.from_bytes(image[4:8], "little")
    if not (RAM_START < stack <= RAM_END) or not (FLASH_START <= (reset & ~1) < FLASH_START + FIRMWARE_LIMIT) \
            or not reset & 1:
        raise PedalUpdateError("that file isn't firmware for the STM32F411 pedals")


class PedalLink:
    """The pedals' serial port, for the few request/reply exchanges an update needs."""

    def __init__(self, port, serial_factory=serial.Serial, sleep=time.sleep, clock=time.monotonic):
        self._sleep, self._clock = sleep, clock
        self.frames = []
        self.parser = pp.FrameParser(lambda t, p: self.frames.append((t, bytes(p))))
        self.ser = serial_factory(port, 115200, timeout=0.05)

    def close(self):
        try:
            self.ser.close()
        except (serial.SerialException, OSError):
            pass

    def _pump(self):
        data = self.ser.read(256)
        if data:
            self.parser.feed(data)

    def request(self, msg_type, reply_type, payload=b"", timeout=2.0):
        """Send a frame and return the payload of the next frame of reply_type, or None."""
        self.frames.clear()
        self.ser.write(pp.build_frame(msg_type, payload))
        end = self._clock() + timeout
        while self._clock() < end:
            self._pump()
            for t, p in self.frames:
                if t == reply_type:
                    return p
        return None

    def wait_for(self, reply_type, timeout=2.0):
        """The payload of the next frame of that type that arrives (the pedals push some on their own)."""
        end = self._clock() + timeout
        while self._clock() < end:
            self._pump()
            for t, p in self.frames:
                if t == reply_type:
                    return p
        return None


def read_state(port, serial_factory=serial.Serial, sleep=time.sleep, clock=time.monotonic):
    """(calibration bytes or None, info dict or None) as the pedals report them now. Firmware older
    than 1.0 has no info message, so info is None for it."""
    link = PedalLink(port, serial_factory, sleep, clock)
    try:
        cal = link.request(pp.MSG_GET_CALIBRATION, pp.MSG_CALIBRATION, timeout=2.0)
        if cal is not None and len(cal) != pp.CAL_SIZE:
            cal = None
        info = pp.parse_info(link.request(pp.MSG_GET_INFO, pp.MSG_INFO, timeout=1.2) or b"")
        return cal, info
    finally:
        link.close()


def touch_bootloader(port, serial_factory=serial.Serial, sleep=time.sleep):
    """Open the port at 1200 baud and close it: the pedals restart into the USB bootloader."""
    ser = serial_factory(port, 1200)
    sleep(0.3)
    ser.close()


def _run(programmer, args, popen, on_output=None, timeout=120.0):
    """Run the programmer; (exit code, all its output). Output is passed to on_output as it comes."""
    proc = popen([programmer] + list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    chunks, start = [], time.monotonic()
    while True:
        data = proc.stdout.read(64)
        if not data:
            break
        text = data.decode("utf-8", "replace") if isinstance(data, bytes) else data
        chunks.append(text)
        if on_output:
            on_output(text)
        if time.monotonic() - start > timeout:
            proc.kill()
            raise PedalUpdateError("ST's programming tool didn't finish in time")
    return proc.wait(), "".join(chunks)


def bootloader_present(programmer, popen=subprocess.Popen):
    """Is a pedal board sitting in the USB bootloader?"""
    _code, out = _run(programmer, ["-l", "usb"], popen, timeout=20)
    return "USB1" in out


def wait_for_bootloader(programmer, popen=subprocess.Popen, timeout=20.0, sleep=time.sleep, clock=time.monotonic):
    end = clock() + timeout
    while clock() < end:
        if bootloader_present(programmer, popen):
            return True
        sleep(0.5)
    return False


def wait_for_pedals(find_port=pp.find_pedal_port, timeout=25.0, sleep=time.sleep, clock=time.monotonic):
    end = clock() + timeout
    while clock() < end:
        port = find_port()
        if port:
            return port
        sleep(0.5)
    return None


def update_pedals(image, port=None, programmer=None, progress=lambda fraction, text: None, log=lambda text: None,
                  serial_factory=serial.Serial, popen=subprocess.Popen, find_port=pp.find_pedal_port,
                  sleep=time.sleep, clock=time.monotonic, work_dir=None):
    """Put `image` (the firmware's bytes) on the pedals. Returns (old version, new version, calibration
    kept) where a version is the 16-bit major << 8 | minor, or None (older firmware doesn't report one),
    and calibration kept says whether the saved calibration was put back. Raises PedalUpdateError with a
    message fit for the user."""
    check_image(image)
    programmer = programmer or find_programmer()
    if not programmer:
        raise PedalUpdateError(MISSING_PROGRAMMER)
    port = port or find_port()

    saved_cal, old_info = None, None
    if port:
        progress(0.02, "Reading the pedals' calibration...")
        try:
            saved_cal, old_info = read_state(port, serial_factory, sleep, clock)
        except (serial.SerialException, OSError) as e:
            raise PedalUpdateError(f"couldn't talk to the pedals ({e})") from e
        if saved_cal is None:
            raise PedalUpdateError("the pedals didn't answer, so their calibration couldn't be saved first. "
                                   "Unplug and replug them, then try again.")
        log("old firmware: %s, calibration %s" % (old_info and hex(old_info["fw"]), saved_cal.hex()))
        progress(0.06, "Restarting the pedals into update mode...")
        try:
            touch_bootloader(port, serial_factory, sleep)
        except (serial.SerialException, OSError) as e:
            raise PedalUpdateError(f"couldn't restart the pedals into update mode ({e})") from e
    elif not bootloader_present(programmer, popen):
        raise PedalUpdateError("the pedals aren't connected.")
    else:
        log("the pedals are already in update mode")

    progress(0.10, "Waiting for update mode...")
    if not wait_for_bootloader(programmer, popen, 20.0, sleep, clock):
        raise PedalUpdateError("the pedals didn't switch to update mode. If ST's drivers aren't installed, "
                               "installing STM32CubeProgrammer adds them. Otherwise unplug the pedals, hold "
                               "the BOOT0 button while plugging them in, and try again.")

    work = work_dir or os.environ.get("TEMP") or "."
    path = os.path.join(work, "volgas_pedals_firmware.bin")
    with open(path, "wb") as f:
        f.write(image)
    try:
        # The tool prints a 0-100% run for each stage (erase, write, verify) and the text arrives in
        # arbitrary pieces: collect it, read only complete "NN%" numbers, count a stage per restart.
        state = {"buffer": "", "done": 0, "stage": 0, "last": -1, "best": 0.0}
        stages = ("Erasing", "Writing the firmware", "Checking the firmware")

        def on_output(text):
            state["buffer"] += text
            for m in re.finditer(r"(?<!\d)(\d{1,3})%", state["buffer"][state["done"]:]):
                pct = min(100, int(m.group(1)))
                if pct < state["last"] - 20 and state["stage"] < len(stages) - 1:
                    state["stage"] += 1
                state["last"] = pct
                fraction = 0.12 + 0.68 * (state["stage"] + pct / 100) / len(stages)
                state["best"] = max(state["best"], fraction)
                progress(state["best"], f"{stages[state['stage']]}... {pct}%")
            state["done"] = max(0, len(state["buffer"]) - 4)   # keep a tail: a number may continue in the next piece

        progress(0.12, "Erasing and writing the firmware...")
        code, out = _run(programmer, ["-c", "port=USB1", "-e", "all", "-w", path, hex(FLASH_START), "-v",
                                      "-s", hex(FLASH_START)], popen, on_output)
        log(out[-600:])
        if code != 0 or "verified successfully" not in out.lower():
            raise PedalUpdateError("writing the firmware failed. The pedals are still in update mode: run "
                                   "the update again to finish it.")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    progress(0.82, "Waiting for the pedals to restart...")
    new_port = wait_for_pedals(find_port, 25.0, sleep, clock)
    if not new_port:
        raise PedalUpdateError("the firmware was written, but the pedals didn't come back. Unplug them and "
                               "plug them in again.")
    sleep(1.0)   # let the new firmware finish starting before the first question

    kept = False
    link = PedalLink(new_port, serial_factory, sleep, clock)
    try:
        new_info = pp.parse_info(link.request(pp.MSG_GET_INFO, pp.MSG_INFO, timeout=3.0) or b"")
        if new_info is None:
            raise PedalUpdateError("the pedals restarted but didn't report a firmware version, so the update "
                                   "can't be confirmed.")
        if saved_cal is not None:
            progress(0.90, "Putting your calibration back...")
            now = link.request(pp.MSG_GET_CALIBRATION, pp.MSG_CALIBRATION, timeout=2.0)
            if now != saved_cal:
                echo = link.request(pp.MSG_SET_CALIBRATION, pp.MSG_CALIBRATION, saved_cal, timeout=2.0)
                ack = link.request(pp.MSG_SAVE, pp.MSG_SAVE_ACK, timeout=3.0)
                again = link.request(pp.MSG_GET_CALIBRATION, pp.MSG_CALIBRATION, timeout=2.0)
                kept = echo == saved_cal and ack == b"\x01" and again == saved_cal
                if not kept:
                    log("calibration to restore: " + saved_cal.hex())
                    raise PedalUpdateError("the firmware is updated, but the pedals' calibration couldn't be "
                                           "put back. Calibrate the pedals again in Volgas.")
            else:
                kept = True
    finally:
        link.close()
    progress(1.0, "Done")
    return (old_info["fw"] if old_info else None), new_info["fw"], kept
