"""Pedal Set control app - live monitor, calibration, response curves.

Talks to pedal_set.ino over the composite USB CDC serial port (same cable
as the HID joystick). Protocol mirror of pedal_set/pedal_protocol.h.

Requires: pip install pyserial
"""

import ctypes
import hashlib
import json
import os
import queue
import re
import ssl
import struct
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk, messagebox
from tkinter import font as tkfont

import certifi
import serial
import serial.tools.list_ports
from collections import deque
from PIL import Image, ImageTk

try:
    # volgas_dash_bridge.py auto-picks rFactor2 or Forza Horizon 5, whichever is
    # actually live, instead of a manual game selection (see its own
    # docstring for why - a fixed selection was a real bug). It degrades
    # gracefully to Forza-only if volgas_rf2_telemetry.py's Windows-only
    # ctypes.wintypes import fails, so this only needs one guard here,
    # not one per game.
    from volgas_dash_bridge import DashBridge
    DASH_BRIDGE_AVAILABLE = True
except Exception:
    DASH_BRIDGE_AVAILABLE = False


# Bump on every GitHub Release (tag vX.Y.Z) - the in-app updater will compare
# this against the latest release's tag.
VOLGAS_VERSION = "0.2.0"

# In-app updates come from this repo's GitHub Releases: each release carries
# Volgas.exe plus Volgas.exe.sha256 ("<hash>  Volgas.exe"). See the Settings
# page (_build_settings_tab).
UPDATE_API_URL = "https://api.github.com/repos/rhyss08/Volgas/releases/latest"
UPDATE_EXE_ASSET = "Volgas.exe"
UPDATE_SHA_ASSET = "Volgas.exe.sha256"


def _version_tuple(text):
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def _http_get(url, timeout):
    # certifi's CA bundle, not the Windows store: Windows only downloads most
    # roots the first time its own networking needs them, so Python found
    # GitHub's Sectigo root missing ("unable to get issuer certificate").
    context = ssl.create_default_context(cafile=certifi.where())
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": f"Volgas/{VOLGAS_VERSION}"}),
        timeout=timeout, context=context)


def _fetch_latest_release():
    """(version, notes, exe_url, sha_url) of the newest GitHub release; a URL
    is None if that release is missing the asset."""
    with _http_get(UPDATE_API_URL, 10) as r:
        data = json.load(r)
    assets = {a["name"]: a["browser_download_url"] for a in data.get("assets", [])}
    return (data["tag_name"].lstrip("v"), data.get("body") or "",
            assets.get(UPDATE_EXE_ASSET), assets.get(UPDATE_SHA_ASSET))


def _download_update(exe_url, sha_url, progress):
    """Download the new exe next to the running one as Volgas.exe.new and check
    it against the release's SHA-256 file. Returns its path; on a mismatch the
    download is deleted and ValueError raised. progress(fraction) is called
    from this (worker) thread."""
    new_path = sys.executable + ".new"
    with _http_get(sha_url, 15) as r:
        expected = r.read().decode("ascii", "replace").split()[0].lower()
    digest = hashlib.sha256()
    with _http_get(exe_url, 30) as r, open(new_path, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total:
                progress(done / total)
    if digest.hexdigest() != expected:
        os.remove(new_path)
        raise ValueError("the download doesn't match the release's checksum")
    return new_path


def _install_update(new_path):
    """Swap the downloaded exe in and start it. Windows won't overwrite a
    running exe but does let it be renamed (tested on Volgas.exe 2026-10-31):
    the running Volgas.exe becomes Volgas.exe.old - deleted by the next start,
    see _remove_old_exe - and the download takes its name."""
    exe = sys.executable
    old = exe + ".old"
    if os.path.exists(old):
        os.remove(old)
    os.rename(exe, old)
    os.rename(new_path, exe)
    subprocess.Popen([exe], close_fds=True)


def _settings_path():
    # Next to Volgas.exe on disk, not inside the onefile bundle's temp extraction dir.
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "volgas_settings.json")


def _load_settings():
    try:
        with open(_settings_path(), "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_settings(settings):
    try:
        with open(_settings_path(), "w") as f:
            json.dump(settings, f)
    except Exception:
        pass


def asset_path(name):
    """Resolve a bundled asset both when run as a .py and as a PyInstaller onefile exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


# Startup splash, like the dash's showSplashScreen() in esp32_dash.ino: the
# logo alone (no window background, no text) for 3 seconds.
SPLASH_LOGO_SIZE = 300
SPLASH_MS = 3000
SPLASH_KEY_COLOR = "#ff00ff"  # made see-through via -transparentcolor; not used anywhere in the logo


def show_splash(root):
    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    splash.attributes("-topmost", True)
    splash.configure(bg=SPLASH_KEY_COLOR)
    splash.attributes("-transparentcolor", SPLASH_KEY_COLOR)
    x = (splash.winfo_screenwidth() - SPLASH_LOGO_SIZE) // 2
    y = (splash.winfo_screenheight() - SPLASH_LOGO_SIZE) // 2
    splash.geometry(f"{SPLASH_LOGO_SIZE}x{SPLASH_LOGO_SIZE}+{x}+{y}")
    logo = Image.open(asset_path("volgas_logo.png")).convert("RGBA").resize(
        (SPLASH_LOGO_SIZE, SPLASH_LOGO_SIZE), Image.LANCZOS)
    # -transparentcolor only hides pixels that exactly match the key, so a
    # soft alpha edge would blend into a magenta fringe - snap alpha to on/off.
    logo.putalpha(logo.getchannel("A").point(lambda a: 255 if a >= 128 else 0))
    splash.logo_photo = ImageTk.PhotoImage(logo)
    tk.Label(splash, image=splash.logo_photo, bg=SPLASH_KEY_COLOR, borderwidth=0).place(
        relx=0.5, rely=0.5, anchor="center")
    splash.update()  # paint it now - VolgasApp's build runs before mainloop starts
    return splash


# USBComposite defaults to LeafLabs' VID; PID is the one pedal_set.ino
# sets explicitly (bumped to 0x0003 when CDC serial was added). Used to
# find the Pedal Set's COM port automatically instead of picking by name.
PEDAL_VID = 0x1EAF
PEDAL_PID = 0x0003

# FFBeast support was removed (user, 2026-10-31) ahead of Volgas's own
# wheelbase firmware, so nothing talks to a wheelbase right now. The Wheelbase
# tab and the Main tab's wheel controls are kept: they still remember their
# values (volgas_settings.json) and send commands to wb_cmd_queue, ready for a
# new backend. The settings below are still FFBeast's field names.
#
# Wheelbase-tab slider range per FFBeast setting: (min, max), or ((value,
# label), ...) for fields that only take a few values. Ranges follow the field
# comments in FFBeast's wheel_api.h ("0 to 100 in %", "-1 or +1",
# AmplifierGainEnum, MotionRange in degrees) except those marked undocumented,
# where the header gives no range and these just cover this hardware with
# room to spare. A value read from the device outside its range widens that
# slider instead of being clipped (see _add_wb_slider_row). Fields deliberately
# not on the tab (user, 2026-10-23), and left at whatever the board has saved:
# EncoderCPR, ForceEnabled (the Main tab's Forces toggle still sets it),
# CalibrationSpeed, BrakingLimit, EncoderDirection, ForceDirection, PolePairs,
# DebugTorque, SpeedBufferSize, DirectXConstantDirection, CalibrationMagnitude,
# PositionSmoothing. IntegratedSpringStrength is an on/off toggle
# (_add_wb_spring_row); the soft-stop fields were fixed at strength 100,
# range 0, dampening 5 and pushed on every connect.
WB_SLIDER_SPECS = {
    "MotionRange": (90, 1080),
    "StaticDampeningStrength": (0, 100),
    "TotalEffectStrength": (0, 100),
    "DirectXSpringStrength": (0, 100),
    "DirectXConstantStrength": (0, 100),
    "DirectXPeriodicStrength": (0, 100),
    "IntegralGain": (0, 1000),  # undocumented; values used so far 10-98
    "ProportionalGain": (0, 255),  # undocumented; full uint8
    "AmplifierGain": ((0, "80x"), (1, "40x"), (2, "20x"), (3, "10x")),
    "PowerLimit": (0, 100),
}
# Tab order: these effect rows, the Integrated Spring switch, then the rest.
WB_EFFECT_KEYS = ("MotionRange", "StaticDampeningStrength", "TotalEffectStrength",
                  "DirectXSpringStrength", "DirectXConstantStrength", "DirectXPeriodicStrength")
WB_HARDWARE_KEYS = ("IntegralGain", "ProportionalGain", "AmplifierGain", "PowerLimit")
INTEGRATED_SPRING_ON_PCT = 30


def _wb_display_name(field):
    """'SoftStopDampeningStrength' -> 'Soft Stop Dampening Strength',
    'EncoderCPR' -> 'Encoder CPR', keeping 'DirectX' as one word."""
    return re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", field).replace("Direct X", "DirectX")

MAIN_WHEEL_CANVAS_SIZE = 440
WHEEL_CARD_ASPECT = 600 / 480  # the Main tab wheel card's requested width / height
WHEEL_SOURCE_WORKING_SIZE = 640  # keyed once at this resolution, then cheaply resized per on-screen size
WHEEL_IMAGE_NAME = "volgas_wheel.png"
WHEEL_VERTICAL_OFFSET = 24  # px, nudges the photo down clear of the angle text above it
WHEEL_RESIZE_THROTTLE_MS = 33
CARD_PAD = 16  # inset between a card's edge and its contents
WHEEL_CENTRE_BUTTON_Y = CARD_PAD + 20 + 22  # top of Set Centre: just under the angle readout centred at CARD_PAD + 20

# Wheelbase tab card rows (see _add_wb_slider_row)
WB_ROWS_TOP = CARD_PAD + 4
WB_ROW_H = 38
WB_BAR_H = 24
WB_VALUE_W = 80  # right-hand value column
WB_NAME_FONT = ("Segoe UI", 10, "bold")

# Dark "neon gauge" dashboard theme for the Main tab (pc_app/assets - two
# reference images: image_5e9bfec6.png for the overall card layout, a second
# glow-style mockup supplied later for the throttle/brake/FFB pill gauges +
# wheel rotation arc) - plain tk widgets with explicit colors, not ttk, so
# this is fully self-contained and doesn't touch the other tabs' native ttk
# look.
MAIN_BG = "#07080b"
MAIN_PANEL = "#131620"
MAIN_BORDER = "#262b3a"
MAIN_TEXT = "#eef0f6"
MAIN_TEXT_DIM = "#8a91a8"
MAIN_RED = "#ff3b3b"
MAIN_BLUE = "#2f8fff"
MAIN_GREEN = "#39e26a"
MAIN_CYAN = "#3fd4e0"
MAIN_GRAY = "#5a6178"
MAIN_ORANGE_STATUS = "#e0a030"


def _rate_limited(widget, fn, interval_ms):
    """Return a trigger that runs fn at once, then at most once per
    interval_ms while triggers keep coming, with one final run after the
    last trigger. fn should read sizes itself (winfo_width etc.) rather
    than take them from the event."""
    state = {"timer": None, "dirty": False}

    def flush():
        state["timer"] = None
        if state["dirty"]:
            state["dirty"] = False
            fn()
            state["timer"] = widget.after(interval_ms, flush)

    def trigger(*_args):
        if state["timer"] is None:
            fn()
            state["timer"] = widget.after(interval_ms, flush)
        else:
            state["dirty"] = True

    return trigger


def _label_height(font):
    """Height a tk.Label with this font asks for (line height plus Label's
    default 1 px padding and 2 px border on each side), for laying canvas
    text out where an equivalent packed Label used to sit."""
    return tkfont.Font(font=font).metrics("linespace") + 6


def _label_width(master, font, chars):
    """Width a tk.Label(width=chars) with this font asks for - read from a
    throwaway Label, since Tk's average-character width isn't exactly
    measure("0")."""
    probe = tk.Label(master, font=font, width=chars)
    width = probe.winfo_reqwidth()
    probe.destroy()
    return width


def _round_rect_points(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]


def _draw_round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    if x2 - x1 < 1 or y2 - y1 < 1:
        return None
    return canvas.create_polygon(_round_rect_points(x1, y1, x2, y2, r), smooth=True, **kwargs)


def _draw_toggle(canvas, on, x=0, y=0, tag=None):
    """Pill on/off switch, 40x20 with its top-left at (x, y). With no tag it
    owns the whole (40x20) canvas (Main tab's Forces switch); with a tag it
    is drawn among other items on a card (Wheelbase tab's Integrated Spring)."""
    canvas.delete(tag or "all")
    kwargs = {"tags": tag} if tag else {}
    _draw_round_rect(canvas, x + 1, y + 1, x + 39, y + 19, 9, fill=MAIN_GREEN if on else MAIN_BORDER, outline="",
                     **kwargs)
    knob_x = x + (30 if on else 10)
    canvas.create_oval(knob_x - 7, y + 3, knob_x + 7, y + 17, fill="#ffffff", outline="", **kwargs)


def _mix_color(c1, c2, t):
    """Linear-interpolate two '#rrggbb' colors - used to fake a soft glow
    halo (a muted blend toward the panel background) and a bright highlight
    cap (a blend toward white) around the neon gauge fills, since a plain
    Tk canvas has no real alpha blending or blur to draw an actual glow."""
    a = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
    b = tuple(int(c2[i:i + 2], 16) for i in (1, 3, 5))
    mixed = tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))
    return "#%02x%02x%02x" % mixed


def _apply_dark_titlebar(root, caption_hex, border_hex, text_hex):
    """Color the native Windows title bar dark via DWM window attributes.

    root.winfo_id() is NOT the actual top-level HWND on Windows - Tk wraps
    the real content in an inner child window and winfo_id() returns THAT
    (confirmed directly: IsWindow() says it's a valid handle, but
    DwmSetWindowAttribute against it fails with E_HANDLE regardless of
    which attribute; GetParent() on it resolves to a second, different
    handle whose window text matches the actual window title, and DWM
    calls against that one succeed). GetParent() is required here - this
    is also almost certainly why an earlier attempt at removing the
    caption entirely (GWL_STYLE, WS_CAPTION) via winfo_id() directly
    appeared to silently do nothing.
    """
    if sys.platform != "win32":
        return
    try:
        root.update_idletasks()
        user32 = ctypes.windll.user32
        hwnd = user32.GetParent(root.winfo_id())
        dwmapi = ctypes.windll.dwmapi

        def to_colorref(hex_color):
            hex_color = hex_color.lstrip("#")
            r, g, b = int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16)
            return r | (g << 8) | (b << 16)  # COLORREF is 0x00BBGGRR, not RGB order

        flag = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE, id differs by Windows 10 build
            if dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(flag), ctypes.sizeof(flag)) == 0:
                break

        # Windows 11+ only: exact caption/border/text colors, not just
        # "some dark gray". Each call is independent/allowed to fail alone.
        for attr, hex_color in ((35, caption_hex), (34, border_hex), (36, text_hex)):
            value = ctypes.c_int(to_colorref(hex_color))
            dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(value), ctypes.sizeof(value))
    except OSError:
        pass


CUSTOM_TITLE_BAR = True  # False puts the native Windows title bar back above the tab strip
TITLE_BAR_H = 40
CAPTION_BUTTON_W = 46
TOP_RESIZE_BAND = 5
TOP_RESIZE_CORNER = 16
CAPTION_GLYPHS = {"min": "", "max": "", "restore": "", "close": ""}
CLOSE_HOVER_BG = "#c42b1c"

GWLP_WNDPROC = -4
WM_GETMINMAXINFO = 0x0024
WM_NCCALCSIZE = 0x0083
WM_NCLBUTTONDOWN = 0x00A1
WM_SYSCOMMAND = 0x0112
SC_MINIMIZE, SC_MAXIMIZE, SC_CLOSE, SC_RESTORE = 0xF020, 0xF030, 0xF060, 0xF120
HTCAPTION, HTTOP, HTTOPLEFT, HTTOPRIGHT = 2, 12, 13, 14
TOP_EDGE_CURSORS = {HTTOP: "sb_v_double_arrow", HTTOPLEFT: "size_nw_se", HTTOPRIGHT: "size_ne_sw"}


def _hook_caption(root):
    """Remove the native caption but keep the rest of the window frame, so the
    tab strip can sit at the very top as the title bar. Returns the top-level
    HWND, or None if the native caption stays.

    Subclasses the real top-level window (GetParent of winfo_id, see
    _apply_dark_titlebar) and answers WM_NCCALCSIZE with the default frame
    minus the caption: the side/bottom borders stay native, so resizing,
    Aero Snap, maximize, the minimize animation and the taskbar all behave as
    before. Maximized windows hang a frame's width off every screen edge, so
    the top is inset by that same width there (taken from the left inset).
    Tk still counts the default top border (caption + frame) in its frame
    size, so its minimum track height (WM_GETMINMAXINFO) is taken back down
    by that much.
    The two earlier attempts - overrideredirect, then stripping WS_CAPTION -
    each broke Snap/maximize or did nothing; this leaves the window styles
    exactly as Tk set them."""
    if sys.platform != "win32" or not CUSTOM_TITLE_BAR:
        return None
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT,
                                           wintypes.WPARAM, wintypes.LPARAM]
        user32.SetWindowLongPtrW.restype = ctypes.c_void_p
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.IsZoomed.argtypes = [wintypes.HWND]
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                        ctypes.c_int, ctypes.c_int, wintypes.UINT]
        root.update_idletasks()
        hwnd = user32.GetParent(root.winfo_id())
        caption_h = [0]

        def proc(h, msg, wparam, lparam):
            if msg == WM_NCCALCSIZE and wparam:
                rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT))[0]
                top, left = rect.top, rect.left
                result = user32.CallWindowProcW(prev, h, msg, wparam, lparam)
                inset = rect.left - left
                if not user32.IsZoomed(h):
                    caption_h[0] = rect.top - top
                rect.top = top + (inset if user32.IsZoomed(h) else 0)
                return result
            if msg == WM_GETMINMAXINFO:
                result = user32.CallWindowProcW(prev, h, msg, wparam, lparam)
                # MINMAXINFO is five POINTs; ptMinTrackSize.y is the 8th LONG.
                ctypes.cast(lparam, ctypes.POINTER(wintypes.LONG))[7] -= caption_h[0]
                return result
            return user32.CallWindowProcW(prev, h, msg, wparam, lparam)

        callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)(proc)
        prev = user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, ctypes.cast(callback, ctypes.c_void_p))
        if not prev:
            return None
        root._volgas_caption_hook = callback  # Windows calls it for the window's whole life
        # SWP_FRAMECHANGED | NOMOVE | NOSIZE | NOZORDER | NOACTIVATE: re-run WM_NCCALCSIZE now.
        user32.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x0020 | 0x0002 | 0x0001 | 0x0004 | 0x0010)
        return hwnd
    except (OSError, AttributeError):
        return None


SYNC0, SYNC1 = 0xAA, 0x55
MSG_STATUS = 0x01
MSG_SET_CALIBRATION = 0x02
MSG_SAVE = 0x03
MSG_GET_CALIBRATION = 0x04
MSG_CALIBRATION = 0x84
MSG_SAVE_ACK = 0x83

# Must match pedal_protocol.h byte-for-byte (packed, little-endian).
CAL_FMT = "<hhii5B5B"        # hallMin hallMax loadMin loadMax throttleCurve[5] brakeCurve[5]
STATUS_FMT = "<hhiHH"        # hallRaw hallFiltered loadRaw throttleAxis brakeAxis

# Curve presets (output % at input breakpoints 0/25/50/75/100%) offered as
# one-click buttons next to each pedal's 5 curve sliders.
CURVE_PRESET_LINEAR = (0, 25, 50, 75, 100)
CURVE_PRESET_S_CURVE = (0, 16, 50, 84, 100)
CURVE_PRESET_EXPONENTIAL = (0, 6, 25, 56, 100)
CURVE_PRESET_REVERSE_EXPONENTIAL = (0, 50, 71, 87, 100)
# Starting values shown before a device is ever read - match the firmware's
# own compiled-in defaults (pedal_set.ino) so the sliders aren't misleading
# pre-connect.
DEFAULT_THROTTLE_CURVE = (0, 44, 66, 84, 100)
DEFAULT_BRAKE_CURVE = CURVE_PRESET_LINEAR
CAL_SIZE = struct.calcsize(CAL_FMT)
STATUS_SIZE = struct.calcsize(STATUS_FMT)


def find_pedal_port():
    """Return the COM port of the Pedal Set by USB VID/PID, or None."""
    for p in serial.tools.list_ports.comports():
        if p.vid == PEDAL_VID and p.pid == PEDAL_PID:
            return p.device
    return None


def build_frame(msg_type, payload=b""):
    checksum = (msg_type + len(payload) + sum(payload)) & 0xFF
    return bytes([SYNC0, SYNC1, msg_type, len(payload)]) + payload + bytes([checksum])


class FrameParser:
    """Same resync state machine as the firmware side."""

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
                self.state = "TYPE" if b == SYNC1 else "SYNC0"
            elif self.state == "TYPE":
                self.msg_type = b
                self.checksum = b
                self.state = "LEN"
            elif self.state == "LEN":
                if b > 32:
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


def _key_out_checkerboard_background(img, fill, brightness_min=150, spread_max=18, interior_leak_min_area=400):
    """Matte out volgas_wheel.png's "transparency" checkerboard (baked into
    the actual pixels as flat gray/white squares, not real alpha - the
    source PNG is fully opaque, see the render that produced it), so only
    the wheel shape itself stays opaque.

    Neighbor-to-neighbor gradient tolerance (the technique the previous
    wheel photo's background removal used) doesn't work here - adjacent
    checker squares jump sharply from one gray tone to another, which a
    small tolerance can't cross, so a gradient-following fill would stop
    dead at the first square boundary instead of covering the whole board.
    Classifying each pixel independently (light + low-saturation = a
    checker square) fixes that.

    Two passes, both flood fills using that same classifier:
      1. Border-seeded - removes checkerboard actually connected to the
         image edge. Still requiring connectivity (not a global threshold)
         keeps this safe for the wheel's own light-gray details (rivets,
         the brushed-metal VG logo, the white button) - those sit inside
         the dark carbon-fiber body, never touching a path of checker-
         colored pixels back to the border, so they survive regardless of
         how light they are.
      2. Interior leaks - this specific photo has checkerboard visible
         *inside* the negative space between each grip and the body too,
         enclosed on every side by opaque material (grip/body/hardware) so
         pass 1 can't reach it, the same "hole inside a printed O" problem
         `_apply_manual_patches` used to hand-fix on the old wheel photo.
         Rather than hardcoded patch rectangles, this finds any remaining
         checker-classified connected component and keys it out too *if*
         it's large - real leaks here are 1000+ px, while the legitimate
         small light details (rivet highlights etc.) that must NOT be
         touched are all under ~300px, so a size cutoff cleanly tells them
         apart without needing to hand-locate anything.
    """
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    keyed = bytearray(w * h)

    def is_bg(r, g, b):
        return (max(r, g, b) - min(r, g, b)) <= spread_max and (r + g + b) / 3 >= brightness_min

    def flood(qx, qy, visited):
        comp = [(qx, qy)]
        q = deque(comp)
        while q:
            cx, cy = q.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = cx + dx, cy + dy
                if 0 <= nx < w and 0 <= ny < h:
                    ni = ny * w + nx
                    if not visited[ni]:
                        visited[ni] = 1
                        if is_bg(*px[nx, ny][:3]):
                            q.append((nx, ny))
                            comp.append((nx, ny))
        return comp

    # Pass 1: border-seeded.
    visited = bytearray(w * h)
    for x in range(w):
        for y in (0, h - 1):
            i = y * w + x
            if not visited[i]:
                visited[i] = 1
                if is_bg(*px[x, y][:3]):
                    for cx, cy in flood(x, y, visited):
                        keyed[cy * w + cx] = 1
    for y in range(h):
        for x in (0, w - 1):
            i = y * w + x
            if not visited[i]:
                visited[i] = 1
                if is_bg(*px[x, y][:3]):
                    for cx, cy in flood(x, y, visited):
                        keyed[cy * w + cx] = 1

    # Pass 2: large interior leaks.
    visited2 = bytearray(w * h)
    for y in range(h):
        for x in range(w):
            i = y * w + x
            if visited2[i] or keyed[i]:
                continue
            visited2[i] = 1
            if not is_bg(*px[x, y][:3]):
                continue
            comp = flood(x, y, visited2)
            if len(comp) >= interior_leak_min_area:
                for cx, cy in comp:
                    keyed[cy * w + cx] = 1

    out = img.copy()
    out_px = out.load()
    for y in range(h):
        for x in range(w):
            if keyed[y * w + x]:
                # `fill` (not the source pixel's own color) so any residual
                # blending at the rotated edge matches the canvas behind it
                # (its actual resolved theme color, passed in by the caller)
                # instead of the checkerboard's gray/white.
                out_px[x, y] = fill + (0,)

    # No alpha blur here (there used to be one, "to soften the edge") - a
    # blurred edge leaves a ring of semi-transparent pixels, and Tk's own
    # canvas alpha compositing isn't precise enough to blend that ring back
    # onto the card's background without a visible color-mismatched fringe
    # (confirmed by report: "a border... isn't properly colour matched to
    # the background"). A hard 0-or-255 edge needs no blending at all - the
    # background shows through cleanly wherever the mask says background.
    return out


class VolgasApp:
    def __init__(self, root):
        self.root = root
        # No text in the native caption - the Volgas branding lives in the
        # app's own title bar (see _build_window_chrome), not the OS caption.
        root.title("")
        try:
            icon_path = asset_path("volgas.ico")
            if sys.platform == "win32" and os.path.exists(icon_path):
                root.iconbitmap(icon_path)
            else:
                root.iconphoto(True, tk.PhotoImage(file=asset_path("volgas_logo.png")))
        except tk.TclError:
            pass  # missing/corrupt icon file shouldn't stop the app from starting

        # Dark theme app-wide (not just the Main tab): "clam" is the only
        # built-in ttk theme that actually honors color style overrides on
        # Windows - the native "vista"/"xpnative" theme ignores most of
        # them. Configuring the base style names (TFrame/TLabel/etc, not a
        # custom style name) means every plain ttk widget in the Pedals/
        # Wheelbase tabs picks this up automatically without
        # having to touch how each of those tabs builds its widgets.
        self.root.configure(bg=MAIN_BG)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        for name in ("TFrame", "TLabelframe", "TLabelframe.Label"):
            style.configure(name, background=MAIN_BG, foreground=MAIN_TEXT, bordercolor=MAIN_BORDER)
        style.configure("TLabel", background=MAIN_BG, foreground=MAIN_TEXT)
        style.configure("TCheckbutton", background=MAIN_BG, foreground=MAIN_TEXT)
        style.map("TCheckbutton", background=[("active", MAIN_BG)])
        style.configure("TButton", background=MAIN_PANEL, foreground=MAIN_TEXT, bordercolor=MAIN_BORDER)
        style.map("TButton", background=[("active", MAIN_BORDER)])
        style.configure("TEntry", fieldbackground=MAIN_PANEL, foreground=MAIN_TEXT, bordercolor=MAIN_BORDER,
                         insertcolor=MAIN_TEXT)
        style.configure("TCombobox", fieldbackground=MAIN_PANEL, background=MAIN_PANEL, foreground=MAIN_TEXT)
        style.configure("TScale", background=MAIN_BG, troughcolor=MAIN_PANEL)
        style.configure("TProgressbar", background=MAIN_BLUE, troughcolor=MAIN_PANEL)
        style.configure("TNotebook", background=MAIN_BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=MAIN_PANEL, foreground=MAIN_TEXT_DIM,
                         padding=(16, 8), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", MAIN_BG)],
                  foreground=[("selected", MAIN_TEXT)])

        self.ser = None
        self.reader = None
        self.rx_queue = queue.Queue()
        self.parser = FrameParser(lambda t, p: self.rx_queue.put((t, p)))
        self.last_status = None

        # Raw calibration endpoints - not user-editable text fields anymore
        # (see _open_calibrate_wizard), just the min/max half of
        # CalibrationData; the curve half lives in throttle_curve_vars/
        # brake_curve_vars (tk vars, built in _build_pedal_curve_card).
        # Defaults match pedal_set.ino's compiled-in `cal` until a real
        # device is read.
        self.cal_state = {"hallMin": 2180, "hallMax": 3000, "loadMin": -2000, "loadMax": -350000}
        self._curve_drag = None  # (pedal_key, point_index) while a curve point is being dragged, else None

        self.wb_cmd_queue = queue.Queue()
        self.wb_connected = False

        # Volgas is the source of truth for the wheelbase settings it shows:
        # every change is remembered in volgas_settings.json ("wheelbase":
        # {field: value}), meant to be pushed to the board on every connect
        # once a wheelbase backend exists again (see WB_SLIDER_SPECS).
        self._wb_settings = _load_settings()
        self._wb_remembered = self._wb_settings.setdefault("wheelbase", {})

        # A plain custom tab bar instead of ttk.Notebook, styled to match
        # the dark theme, that doubles as the title bar - see
        # _build_window_chrome and _hook_caption.
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(0, weight=1)
        content = tk.Frame(self.root, bg=MAIN_BG)
        content.grid(row=1, column=0, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        main_tab = tk.Frame(content, bg=MAIN_BG)
        pedals_tab = tk.Frame(content, bg=MAIN_BG)
        wheelbase_tab = tk.Frame(content, bg=MAIN_BG)
        # Settings has no tab button - it's opened by clicking the logo.
        settings_tab = tk.Frame(content, bg=MAIN_BG)
        self._tab_frames = {"Main": main_tab, "Pedals": pedals_tab, "Wheelbase": wheelbase_tab,
                            "Settings": settings_tab}
        for frame in self._tab_frames.values():
            frame.grid(row=0, column=0, sticky="nsew")

        self._update_queue = queue.Queue()
        self._update_busy = False
        self._build_main_tab(main_tab)
        self._build_pedals_tab(pedals_tab)
        self._build_wheelbase_tab(wheelbase_tab)
        self._build_settings_tab(settings_tab)
        self._caption_hwnd = _hook_caption(self.root)
        self._build_window_chrome([n for n in self._tab_frames if n != "Settings"])

        # Floor the window at the size its content actually asks for (driven
        # by the Main tab's card/gauge minimum sizes, see _build_main_tab).
        # The native resize border is kept (see _hook_caption), so Windows
        # enforces this during an interactive resize like any other app.
        self.root.update_idletasks()
        req_w, req_h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.minsize(req_w, req_h)
        # Measured with every tab still gridded, so the floor fits the
        # biggest tab. The explicit geometry stops the window following its
        # requested size once only one tab is gridded, so switching tabs
        # doesn't resize it.
        self.root.geometry(f"{req_w}x{req_h}")
        self._select_tab(next(iter(self._tab_frames)))
        _apply_dark_titlebar(self.root, MAIN_BG, MAIN_BORDER, MAIN_TEXT)

        self._poll()
        self._auto_scan()
        if DASH_BRIDGE_AVAILABLE:
            # No tab of its own any more: the bridge runs in the background for
            # the app's whole lifetime, finding the dash, picking up rFactor2 or
            # Forza and reconnecting by itself (see volgas_dash_bridge).
            threading.Thread(target=lambda: DashBridge().run(threading.Event()), daemon=True).start()
        self._remove_old_exe()
        if self._wb_settings.get("check_updates_on_start", True):
            self._start_update_check()

    # ---------- window chrome (dark tab strip as the title bar) ----------

    def _build_window_chrome(self, tab_names):
        """The tab strip, styled to match the dark theme and replacing
        ttk.Notebook's own tab widget. With the native caption removed (see
        _hook_caption) it is also the title bar: its empty space drags the
        window (handing off to Windows' own move loop, so Snap and
        drag-to-restore work), double-click maximizes, its top few pixels
        resize, and it carries the min/max/close buttons. If the hook isn't
        in place the native title bar stays above it instead."""
        bar = tk.Frame(self.root, bg=MAIN_PANEL, height=TITLE_BAR_H)
        bar.grid(row=0, column=0, sticky="ew")
        bar.pack_propagate(False)
        self.root.columnconfigure(0, weight=1)

        # The Volgas mark leads the bar; clicking it opens the Settings page.
        logo_size = 24
        logo_im = Image.open(asset_path("volgas_logo.png")).convert("RGBA").resize((logo_size, logo_size), Image.LANCZOS)
        self._logo_photo = ImageTk.PhotoImage(logo_im)
        logo = tk.Label(bar, image=self._logo_photo, bg=MAIN_PANEL, cursor="hand2")
        logo.pack(side="left", padx=(14, 8))
        logo.bind("<Button-1>", lambda _e: self._select_tab("Settings"))

        self._tab_buttons = {}
        for name in tab_names:
            btn = tk.Label(bar, text=name.upper(), font=("Segoe UI", 9, "bold"),
                            fg=MAIN_TEXT_DIM, bg=MAIN_PANEL, padx=14, cursor="hand2")
            btn.pack(side="left", fill="y")
            btn.bind("<Button-1>", lambda _e, n=name: self._select_tab(n))
            self._tab_buttons[name] = btn

        # Shown after the tabs when an update check finds a newer release.
        self._update_badge = tk.Label(bar, text="Update available", font=("Segoe UI", 9, "bold"),
                                      fg=MAIN_CYAN, bg=MAIN_PANEL, cursor="hand2")
        self._update_badge.bind("<Button-1>", lambda _e: self._select_tab("Settings"))

        if not self._caption_hwnd:
            return
        self._build_caption_buttons(bar)
        self._bar_press = None
        bar.bind("<ButtonPress-1>", self._on_bar_press)
        bar.bind("<B1-Motion>", self._on_bar_drag)
        bar.bind("<Double-Button-1>", self._on_bar_double)
        bar.bind("<ButtonRelease-1>", self._on_bar_release)
        self._bar_double = False
        # Every widget in the bar gets the top-edge resize band, ahead of its
        # own bindings so a press there resizes instead of clicking a tab.
        self._title_cursors = {}
        for w in (bar, *bar.winfo_children()):
            self._title_cursors[w] = w.cget("cursor")
            w.bindtags(("VolgasTitleBar",) + w.bindtags())
        self.root.bind_class("VolgasTitleBar", "<Motion>", self._on_title_motion)
        self.root.bind_class("VolgasTitleBar", "<ButtonPress-1>", self._on_title_press)
        self.root.bind("<Configure>", self._sync_max_glyph, add="+")

    def _build_caption_buttons(self, bar):
        families = set(tkfont.families(self.root))
        glyph_font = ("Segoe Fluent Icons" if "Segoe Fluent Icons" in families else "Segoe MDL2 Assets", 8)
        user32 = ctypes.windll.user32
        buttons = (("close", lambda: user32.PostMessageW(self._caption_hwnd, WM_SYSCOMMAND, SC_CLOSE, 0),
                    CLOSE_HOVER_BG),
                   ("max", self._toggle_maximize, MAIN_BORDER),
                   ("min", lambda: user32.PostMessageW(self._caption_hwnd, WM_SYSCOMMAND, SC_MINIMIZE, 0),
                    MAIN_BORDER))
        for key, action, hover_bg in buttons:
            btn = tk.Canvas(bar, width=CAPTION_BUTTON_W, height=TITLE_BAR_H, bg=MAIN_PANEL, highlightthickness=0)
            btn.create_text(CAPTION_BUTTON_W // 2, TITLE_BAR_H // 2, text=CAPTION_GLYPHS[key],
                            font=glyph_font, fill=MAIN_TEXT, tags="glyph")
            btn.pack(side="right", fill="y")
            btn.bind("<Enter>", lambda _e, b=btn, bg=hover_bg: b.config(bg=bg))
            btn.bind("<Leave>", lambda _e, b=btn: b.config(bg=MAIN_PANEL))
            # Acts on release over the button, like the native ones.
            btn.bind("<ButtonRelease-1>",
                     lambda e, a=action: a() if 0 <= e.x < e.widget.winfo_width()
                     and 0 <= e.y < e.widget.winfo_height() else None)
            if key == "max":
                self._max_button = btn
        self._max_glyph_zoomed = False

    def _is_zoomed(self):
        return bool(ctypes.windll.user32.IsZoomed(self._caption_hwnd))

    def _toggle_maximize(self):
        ctypes.windll.user32.PostMessageW(self._caption_hwnd, WM_SYSCOMMAND,
                                          SC_RESTORE if self._is_zoomed() else SC_MAXIMIZE, 0)

    def _sync_max_glyph(self, event):
        if event.widget is not self.root:
            return
        zoomed = self._is_zoomed()
        if zoomed != self._max_glyph_zoomed:
            self._max_glyph_zoomed = zoomed
            self._max_button.itemconfig("glyph", text=CAPTION_GLYPHS["restore" if zoomed else "max"])

    def _on_bar_press(self, event):
        """Empty bar space only arms a drag; Windows' move loop starts once
        the mouse moves past the drag threshold (_on_bar_drag). Starting it
        on the press made the loop swallow a plain click, so a double-click
        never reached Tk."""
        self._bar_press = event

    def _on_bar_drag(self, event):
        press = self._bar_press
        user32 = ctypes.windll.user32
        if press and (abs(event.x_root - press.x_root) >= user32.GetSystemMetrics(68)  # SM_CXDRAG
                      or abs(event.y_root - press.y_root) >= user32.GetSystemMetrics(69)):  # SM_CYDRAG
            self._bar_press = None
            # The press point, so the window keeps the grab offset it had.
            self._start_native_drag(HTCAPTION, press)

    def _on_bar_double(self, _event):
        self._bar_press = None
        self._bar_double = True

    def _on_bar_release(self, _event):
        # Maximize on the second release, not the second press: an SC_MAXIMIZE
        # handled while the button is still down (Tk holding the mouse) was
        # received but ignored (seen in a debug log).
        self._bar_press = None
        if self._bar_double:
            self._bar_double = False
            self._toggle_maximize()

    def _start_native_drag(self, hit, event):
        """Hand the press to Windows as if it landed on the native caption
        (or top border): its own modal move/size loop takes over. Posted,
        not sent: a SendMessage would run the loop inside this Python
        callback, and Tk's timers firing Python callbacks inside that loop
        kill the interpreter (fatal "PyEval_RestoreThread ... GIL is
        released"). Posted messages are retrieved before input, so the loop
        still starts while the button is down."""
        user32 = ctypes.windll.user32
        user32.ReleaseCapture()  # Tk captures the mouse on press
        user32.PostMessageW(self._caption_hwnd, WM_NCLBUTTONDOWN, hit,
                            ((event.y_root & 0xFFFF) << 16) | (event.x_root & 0xFFFF))

    def _top_edge_hit(self, event):
        if event.y_root - self.root.winfo_rooty() >= TOP_RESIZE_BAND or self._is_zoomed():
            return None
        x = event.x_root - self.root.winfo_rootx()
        if x < TOP_RESIZE_CORNER:
            return HTTOPLEFT
        if x >= self.root.winfo_width() - TOP_RESIZE_CORNER:
            return HTTOPRIGHT
        return HTTOP

    def _on_title_motion(self, event):
        cursor = TOP_EDGE_CURSORS.get(self._top_edge_hit(event), self._title_cursors[event.widget])
        if event.widget.cget("cursor") != cursor:
            event.widget.config(cursor=cursor)

    def _on_title_press(self, event):
        hit = self._top_edge_hit(event)
        if hit:
            self._start_native_drag(hit, event)
            return "break"

    def _select_tab(self, name):
        for n, btn in self._tab_buttons.items():
            selected = n == name
            btn.config(bg=MAIN_BG if selected else MAIN_PANEL, fg=MAIN_TEXT if selected else MAIN_TEXT_DIM)
        # Hidden tabs are taken out of the grid, not just stacked underneath
        # with tkraise(): stacked ones still got laid out and redrawn on
        # every resize event, which made dragging the window edge lag.
        for n, frame in self._tab_frames.items():
            if n == name:
                frame.grid()
            else:
                frame.grid_remove()
        # A wheel card on the tab just shown skipped redraws while hidden -
        # catch it up once grid has mapped it (idle, after the layout pass).
        self.root.after_idle(self._refresh_wheel_cards)
        if name == "Wheelbase" and self.wb_connected:
            # Refresh its fields from the device every time it's opened -
            # they otherwise only got populated once, right after connect,
            # and would show stale values after anything changed them
            # elsewhere (e.g. the Main tab's sliders). Queued directly
            # (not via _wb_send_command) so switching tabs while
            # disconnected doesn't pop up a warning dialog.
            self.wb_cmd_queue.put(("read_settings",))

    def _show_update_badge(self, show):
        if show:
            self._update_badge.pack(side="left", fill="y", padx=(12, 0))
        else:
            self._update_badge.pack_forget()

    # ---------- UI ----------

    def _build_main_tab(self, parent):
        root = tk.Frame(parent, bg=MAIN_BG)
        root.pack(fill="both", expand=True)
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(1, weight=1)  # the card row is the only part that grows tall

        # ---- top bar: compact controls (status/enable/range/centre) ----
        # No brand/logo here - the window chrome bar (see
        # _build_window_chrome) is the only branding-free header now.
        top = tk.Frame(root, bg=MAIN_BG)
        top.grid(row=0, column=0, sticky="ew", padx=20, pady=(12, 8))

        right_box = tk.Frame(top, bg=MAIN_BG)
        right_box.pack(side="right")
        self.main_wb_status_label = tk.Label(right_box, text="wheelbase: --", font=("Segoe UI", 9),
                                              fg=MAIN_GRAY, bg=MAIN_BG, padx=12)
        self.main_wb_status_label.pack(side="right")

        self.main_forces_enabled_var = tk.BooleanVar(value=bool(self._wb_remembered.get("ForceEnabled", 0)))
        enable_box = tk.Frame(right_box, bg=MAIN_BG)
        enable_box.pack(side="right", padx=(0, 16))
        tk.Label(enable_box, text="Forces", font=("Segoe UI", 9),
                 fg=MAIN_TEXT, bg=MAIN_BG).pack(side="left", padx=(0, 4))
        self.main_forces_toggle = tk.Canvas(enable_box, width=40, height=20, bg=MAIN_BG, highlightthickness=0)
        self.main_forces_toggle.pack(side="left")
        self.main_forces_toggle.bind("<Button-1>", self._on_forces_enabled_toggle)
        self._redraw_forces_toggle()

        # ---- middle row: brake | wheel rotation | throttle glow cards ----
        # Grid (not pack) with column/row weights so the cards genuinely
        # resize - not just sit centered with extra blank space - as the
        # window grows, matching the rest of this tab's resizable layout.
        row = tk.Frame(root, bg=MAIN_BG)
        row.grid(row=1, column=0, sticky="nsew", padx=20, pady=8)
        row.grid_columnconfigure(0, weight=1)
        row.grid_columnconfigure(1, weight=2)
        row.grid_columnconfigure(2, weight=1)
        row.grid_rowconfigure(0, weight=1)

        brake_card, self.main_brake_update = self._build_glow_pedal_card(row, "BRAKE", MAIN_RED)
        brake_card.grid(row=0, column=0, sticky="nsew", padx=(0, 12))

        wheel_card = self._build_wheel_card(row)
        wheel_card.grid(row=0, column=1, sticky="nsew", padx=12)

        throttle_card, self.main_throttle_update = self._build_glow_pedal_card(row, "THROTTLE", MAIN_GREEN)
        throttle_card.grid(row=0, column=2, sticky="nsew", padx=(12, 0))

        # ---- bottom: Force Feedback Intensity + Steering Range, sharing one card ----
        # Fixed height, so only x positions change on resize. y offsets are
        # the ones the old packed Label/Canvas/Label stack had.
        ffb_card = self._make_flat_card(root, 190 + 12 + 600 + 12 + 190, 290)
        ffb_card.grid(row=2, column=0, sticky="ew", padx=20, pady=(8, 20))
        saved_strength = self._wb_remembered.get("PowerLimit", 15)
        self.main_force_strength_var = tk.DoubleVar(value=saved_strength)
        self._add_card_hbar(
            ffb_card, 0, "FORCE FEEDBACK INTENSITY", self.main_force_strength_var, 1, 100, MAIN_BLUE,
            lambda v: f"{round(v)}% FFB", self._on_force_strength_release)

        saved_range = self._wb_remembered.get("MotionRange", 360)
        self.main_motion_range_var = tk.DoubleVar(value=saved_range)
        self._add_card_hbar(
            ffb_card, 101, "STEERING RANGE", self.main_motion_range_var, 90, 1080, MAIN_CYAN,
            lambda v: f"{int(round(v))}°", self._on_motion_range_release)

        preset_box = tk.Frame(ffb_card, bg=MAIN_PANEL)
        for label, deg in (("360°", 360), ("540°", 540), ("900°", 900)):
            tk.Button(preset_box, text=label, font=("Segoe UI", 9), fg=MAIN_TEXT,
                      bg=MAIN_BORDER, activebackground=MAIN_CYAN, activeforeground=MAIN_BG,
                      relief="flat", padx=16, pady=4, bd=0, cursor="hand2",
                      command=lambda d=deg: self._set_motion_range_preset(d)).pack(side="left", padx=6)
        preset_y = CARD_PAD + 208 + 14
        preset_id = ffb_card.create_window(ffb_card.winfo_reqwidth() / 2, preset_y, window=preset_box)
        ffb_card.bind("<Configure>", lambda e: ffb_card.coords(preset_id, e.width / 2, preset_y), add="+")

    def _make_flat_card(self, parent, width, height, radius=20, outline=MAIN_BORDER):
        """A rounded-corner dark card (real tk.Frame has square corners
        only), drawn as a polygon on a Canvas. `width`/`height` are just the
        initial/minimum size - cards are gridded/packed to stretch, and the
        background reshapes via `coords` on every resize. The caller draws
        the contents as items on the card canvas itself (or embeds the odd
        real widget with create_window) and adds its own <Configure> handler
        (add="+") to reposition them. Not a Frame of packed widgets inside
        the canvas: every extra nested window re-lays-out and re-paints on
        each resize event, which made drag-resizing the window stutter.
        `outline=""` for a border-free card (used for the wheel card)."""
        canvas = tk.Canvas(parent, width=width, height=height, bg=MAIN_BG, highlightthickness=0)
        bg_id = _draw_round_rect(canvas, 1, 1, width - 1, height - 1, radius,
                                  fill=MAIN_PANEL, outline=outline, width=1)
        canvas.bind("<Configure>", lambda e: canvas.coords(
            bg_id, *_round_rect_points(1, 1, e.width - 1, e.height - 1, radius)))
        return canvas

    def _build_glow_pedal_card(self, parent, label, color):
        """Card holding a vertical neon-glow pill gauge (0-1023 raw range) - a
        dim halo rect behind a bright core fill plus a lighter "cap" line at
        the fill's leading edge fake the glow, since a plain Tk canvas has no
        real blur/alpha to draw one for real. Title on top, percentage at the
        bottom, gauge filling the space between, all drawn on the card canvas
        at the positions the old packed Label/Canvas/Label stack had.
        Returns (card, update(raw_value))."""
        card = self._make_flat_card(parent, 190, 400)
        title_font, pct_font = ("Segoe UI", 10, "bold"), ("Segoe UI", 16, "bold")
        title_h, pct_h = _label_height(title_font), _label_height(pct_font)
        title_id = card.create_text(0, 0, text=label, font=title_font, fill=MAIN_TEXT_DIM)
        pct_id = card.create_text(0, 0, text="0%", font=pct_font, fill=color)

        halo_color = _mix_color(MAIN_PANEL, color, 0.35)
        cap_color = _mix_color(color, "#ffffff", 0.6)
        state = {"raw": 0}

        def redraw(*_args):
            w, h = card.winfo_width(), card.winfo_height()
            if w <= 1 or h <= 1:
                return
            card.coords(title_id, w / 2, CARD_PAD + 4 + title_h / 2)
            card.coords(pct_id, w / 2, h - CARD_PAD - 4 - pct_h / 2)
            card.delete("gauge")
            x0, y0 = CARD_PAD, CARD_PAD + 4 + title_h + 10
            gw, gh = w - 2 * CARD_PAD, h - CARD_PAD - 4 - pct_h - 10 - y0
            if gw <= 1 or gh <= 1:
                return
            pad = max(5, gw * 0.07)
            r = (gw - 2 * pad) / 2
            left, right, top, bottom = x0 + pad, x0 + gw - pad, y0 + pad, y0 + gh - pad
            _draw_round_rect(card, left, top, right, bottom, r, fill="#0a0b10", outline=MAIN_BORDER, width=1,
                              tags="gauge")
            frac = max(0.0, min(1.0, state["raw"] / 1023.0))
            fill_top = bottom - frac * (bottom - top)
            if frac > 0.02:
                halo_pad = 3
                _draw_round_rect(card, left - halo_pad, fill_top - halo_pad, right + halo_pad, bottom + halo_pad,
                                  r, fill=halo_color, outline="", tags="gauge")
                _draw_round_rect(card, left, fill_top, right, bottom, r, fill=color, outline="", tags="gauge")
                cap_h = 4
                _draw_round_rect(card, left, fill_top, right, min(fill_top + cap_h, bottom), r, fill=cap_color,
                                  outline="", tags="gauge")

        def update(raw_value):
            state["raw"] = raw_value
            card.itemconfig(pct_id, text=f"{round(max(0.0, min(1.0, raw_value / 1023.0)) * 100)}%")
            redraw()

        card.bind("<Configure>", redraw, add="+")
        update(0)
        return card, update

    def _add_card_hbar(self, card, y_offset, title, var, from_, to, color, format_fn, on_release):
        """Horizontal neon-glow pill slider drawn on a flat card - same
        halo+cap technique as the vertical pedal gauges. Used for both Force
        Feedback Intensity and Steering Range, sharing one card; y_offset
        stacks the second one under the first. Spans the card's width and
        redraws on every <Configure>, so it scales with the window."""
        title_font, value_font = ("Segoe UI", 10, "bold"), ("Segoe UI", 11, "bold")
        title_y = CARD_PAD + y_offset + 6 + _label_height(title_font) / 2
        bar_top = CARD_PAD + y_offset + 37
        value_y = CARD_PAD + y_offset + 71 + _label_height(value_font) / 2
        bar_inset = CARD_PAD + 4  # the old bar canvas was packed with padx=4 inside the card padding
        height = 26
        title_id = card.create_text(0, title_y, text=title, font=title_font, fill=MAIN_TEXT_DIM)
        value_id = card.create_text(0, value_y, text=format_fn(var.get()), font=value_font, fill=MAIN_TEXT)
        tag = f"hbar{y_offset}"

        halo_color = _mix_color(MAIN_PANEL, color, 0.35)
        cap_color = _mix_color(color, "#ffffff", 0.55)
        pad = 3
        r = (height - 2 * pad) / 2
        state = {"dragging": False}

        def redraw(*_args):
            w = card.winfo_width()
            if w <= 1:
                return
            card.coords(title_id, w / 2, title_y)
            card.coords(value_id, w / 2, value_y)
            card.itemconfig(value_id, text=format_fn(var.get()))
            card.delete(tag)
            x0, x1 = bar_inset, w - bar_inset
            y0, y1 = bar_top + pad, bar_top + height - pad
            _draw_round_rect(card, x0 + pad, y0, x1 - pad, y1, r, fill="#0a0b10", outline=MAIN_BORDER, width=1,
                              tags=tag)
            frac = max(0.0, min(1.0, (var.get() - from_) / (to - from_)))
            fill_x = x0 + pad + frac * (x1 - x0 - 2 * pad)
            if fill_x > x0 + pad + r:
                halo_pad = 3
                _draw_round_rect(card, x0 + pad - halo_pad, y0 - halo_pad, fill_x + halo_pad, y1 + halo_pad,
                                  r, fill=halo_color, outline="", tags=tag)
                _draw_round_rect(card, x0 + pad, y0, fill_x, y1, r, fill=color, outline="", tags=tag)
                cap_w = 4
                _draw_round_rect(card, max(x0 + pad, fill_x - cap_w), y0, fill_x, y1, r, fill=cap_color,
                                  outline="", tags=tag)

        def set_from_x(event):
            span = card.winfo_width() - 2 * bar_inset
            if span <= 1:
                return
            frac = max(0.0, min(1.0, (event.x - bar_inset) / span))
            var.set(from_ + frac * (to - from_))

        # Both sliders share the card's mouse events; each only reacts to
        # drags that started inside its own bar's band.
        def press(event):
            if bar_top <= event.y <= bar_top + height:
                state["dragging"] = True
                set_from_x(event)

        def motion(event):
            if state["dragging"]:
                set_from_x(event)

        def release(event):
            if state["dragging"]:
                state["dragging"] = False
                if on_release is not None:
                    on_release(event)

        card.bind("<Configure>", redraw, add="+")
        card.bind("<Button-1>", press, add="+")
        card.bind("<B1-Motion>", motion, add="+")
        card.bind("<ButtonRelease-1>", release, add="+")
        var.trace_add("write", redraw)
        redraw()

    def _build_wheel_card(self, parent, width=600, height=480):
        """A wheel card - the card IS the wheel canvas: the rounded
        background, the photo, the angle readout and Set Centre are all on
        one canvas. (Separate widgets for the readout once showed a faint
        rectangular seam at the widget edge, and nesting a canvas inside the
        card made every resize repaint the card area twice.) The Main and
        Wheelbase tabs each build one; they share the keyed photo and all
        follow the wheelbase position (_update_main_wheel). Returns the card."""
        if not hasattr(self, "_wheel_cards"):
            self._wheel_cards = []  # one draw(angle) per card
            self._last_wheel_angle = 0.0
            r16, g16, b16 = parent.winfo_rgb(MAIN_PANEL)
            self._wheel_fill_rgb = (r16 // 256, g16 // 256, b16 // 256)
            self._load_wheel_image()

        canvas = self._make_flat_card(parent, width, height, outline="")
        state = {"size": None, "base": None, "photo": None}

        def resize_photo(size):
            size = max(40, int(size))
            if size != state["size"]:
                state["size"] = size
                state["base"] = self._wheel_flat_full.resize((size, size), Image.LANCZOS)

        def draw(angle_deg):
            # Always rotate from the pristine base image, never from the
            # previous rotated frame - repeatedly re-rotating an already-rotated
            # image would compound resampling blur across updates.
            # Sign: PIL rotates counter-clockwise for positive angles; negated
            # here so a positive angle_deg (matching the FFBeast Position/turning
            # right) turns the picture clockwise. Flip the sign if that turns
            # out backwards against the real wheel.
            rotated = state["base"].rotate(-angle_deg, resample=Image.BICUBIC, expand=False,
                                           fillcolor=self._wheel_fill_rgb)
            state["photo"] = ImageTk.PhotoImage(rotated)  # keep a reference - Tk drops GC'd images
            canvas.itemconfig(image_id, image=state["photo"])
            canvas.itemconfig(text_id, text=f"{angle_deg:+.0f}°")

        def apply_resize():
            w, h = canvas.winfo_width(), canvas.winfo_height()
            # Same size as when the photo sat in a canvas inset CARD_PAD inside
            # the card (minus a 4 px margin), except horizontally it never comes
            # within the corner radius of the card's sides: the photo is an
            # opaque square, and its card-coloured corners mustn't cover the
            # card's rounded corners.
            resize_photo(min(w - 40, h - 2 * CARD_PAD - 4))
            draw(self._last_wheel_angle)

        # Resampling the photo costs several ms and a drag-resize sends dozens
        # of resize events a second, so it's rate-limited to
        # WHEEL_RESIZE_THROTTLE_MS, at whatever size the canvas has by then.
        resize_soon = _rate_limited(canvas, apply_resize, WHEEL_RESIZE_THROTTLE_MS)

        def on_resize(event):
            w, h = event.width, event.height
            if w <= 1 or h <= 1:
                return
            # Nudged down from dead-center so the photo's own top edge doesn't
            # sit under the angle text above it.
            canvas.coords(image_id, w / 2, h / 2 + WHEEL_VERTICAL_OFFSET)
            canvas.coords(text_id, w / 2, CARD_PAD + 20)
            canvas.coords(centre_id, w / 2, WHEEL_CENTRE_BUTTON_Y)
            resize_soon()

        resize_photo(MAIN_WHEEL_CANVAS_SIZE)
        state["photo"] = ImageTk.PhotoImage(state["base"])
        image_id = canvas.create_image(0, 0, image=state["photo"])
        text_id = canvas.create_text(0, CARD_PAD + 20, text="0°", fill=MAIN_TEXT, font=("Segoe UI", 16, "bold"))
        # Set Centre sits right under the angle readout it zeroes. Card
        # background colour (MAIN_BORDER on MAIN_PANEL) so it stays visible on
        # the panel, unlike the top bar's MAIN_PANEL-on-MAIN_BG it used to be.
        centre_btn = tk.Button(canvas, text="Set Centre", font=("Segoe UI", 9), fg=MAIN_TEXT,
                               bg=MAIN_BORDER, activebackground=MAIN_CYAN, activeforeground=MAIN_BG,
                               relief="flat", padx=12, pady=4, bd=0, cursor="hand2",
                               command=lambda: self._wb_send_command(("reset_center",)))
        centre_id = canvas.create_window(0, WHEEL_CENTRE_BUTTON_Y, window=centre_btn, anchor="n")
        canvas.bind("<Configure>", on_resize, add="+")
        self._wheel_cards.append((canvas, draw))
        return canvas

    def _redraw_forces_toggle(self):
        _draw_toggle(self.main_forces_toggle, self.main_forces_enabled_var.get())

    def _on_forces_enabled_toggle(self, _event=None):
        self.main_forces_enabled_var.set(not self.main_forces_enabled_var.get())
        self._redraw_forces_toggle()
        # ForceEnabled is a HardwareSettingsTypeDef field (RAM-only until
        # Save & Reboot on the Wheelbase tab), not a runtime enable/disable
        # command - FFBeast has no separate concept of the two.
        value = 1 if self.main_forces_enabled_var.get() else 0
        self._remember_wb("ForceEnabled", value)
        self._wb_send_command(("send_setting", "ForceEnabled", value))

    def _on_force_strength_release(self, _event):
        # PowerLimit is a torque-limiting safety field (HardwareSettingsTypeDef)
        # whose real values run roughly 0-100 on this hardware (confirmed by
        # direct reads - never anywhere near the field's full 0-255 wire
        # range), so unlike TotalEffectStrength this is sent as the
        # percentage directly, not scaled onto 0-255.
        pct = round(self.main_force_strength_var.get())
        self._remember_wb("PowerLimit", pct)
        self._on_wb_settings({"PowerLimit": pct})  # keep the Wheelbase tab's row in step
        self._wb_send_command(("send_setting", "PowerLimit", pct))

    def _on_motion_range_release(self, _event):
        deg = round(self.main_motion_range_var.get())
        # MotionRange is FFBeast's own field, in whole degrees directly -
        # same units the slider already uses, no conversion needed.
        self._remember_wb("MotionRange", deg)
        self._on_wb_settings({"MotionRange": deg})  # keep the Wheelbase tab's row in step
        self._wb_send_command(("send_setting", "MotionRange", deg))

    def _remember_wb(self, name, value):
        """Remember a wheelbase setting so it's re-applied on every connect
        (see __init__'s _wb_remembered comment) - also while disconnected."""
        self._wb_remembered[name] = value
        _save_settings(self._wb_settings)

    def _set_motion_range_preset(self, deg):
        self.main_motion_range_var.set(deg)
        self._on_motion_range_release(None)

    def _load_wheel_image(self):
        """Load + key out the wheel photo ONCE at a fairly high working
        resolution, so it can be cheaply resized to whatever the wheel
        card's current on-screen size is (window resizing) without
        re-running the relatively expensive checkerboard flood fill each
        time - just a plain PIL resize, the same cost class as the
        per-frame rotate this already does."""
        fill = self._wheel_fill_rgb
        im = Image.open(asset_path(WHEEL_IMAGE_NAME)).convert("RGBA")
        w, h = im.size
        side = max(w, h)
        paste_x, paste_y = (side - w) // 2, (side - h) // 2
        square = Image.new("RGBA", (side, side), fill + (255,))
        square.paste(im, (paste_x, paste_y))
        working = square.resize((WHEEL_SOURCE_WORKING_SIZE, WHEEL_SOURCE_WORKING_SIZE), Image.LANCZOS)
        keyed = _key_out_checkerboard_background(working, fill=fill)

        # The source photo (1365x768) isn't square, so squaring it up above
        # (for a clean rotation pivot) padded it with letterbox bars -
        # opaque and colored to match the background, but a solid opaque
        # square that then gets BICUBIC-rotated doesn't always land
        # pixel-perfectly on the canvas's own background once resampled,
        # showing up as a faint rectangular "floating block" edge around the
        # wheel at non-zero angles. Forcing the padding fully TRANSPARENT
        # (not just background-colored) removes it outright regardless of
        # rotation/resampling, rather than relying on an exact color match.
        scale = WHEEL_SOURCE_WORKING_SIZE / side
        photo_box = (
            round(paste_x * scale), round(paste_y * scale),
            round((paste_x + w) * scale), round((paste_y + h) * scale),
        )
        new_alpha = Image.new("L", keyed.size, 0)
        new_alpha.paste(keyed.getchannel("A").crop(photo_box), (photo_box[0], photo_box[1]))
        keyed.putalpha(new_alpha)

        # Flattened onto the card colour once here (and rotated with that
        # same exact colour as fillcolor, see _build_wheel_card's draw), so Tk blits a
        # plain opaque image instead of alpha-blending ~200k pixels on every
        # repaint - that blend was ~9 ms of every resize event - and each
        # resize resamples 3 channels instead of resampling 4 and
        # re-flattening (8.6 -> 4.8 ms). Everything outside the wheel is
        # exactly _wheel_fill_rgb, so no square edge can show.
        flat = Image.new("RGB", keyed.size, fill)
        flat.paste(keyed, mask=keyed.getchannel("A"))
        self._wheel_flat_full = flat

    def _update_main_wheel(self, position):
        motion_range = self.main_motion_range_var.get()
        self._last_wheel_angle = (position / 10000.0) * (motion_range / 2.0)
        self._refresh_wheel_cards()

    def _refresh_wheel_cards(self):
        """Draw the wheel at the latest angle on every card that's on screen.
        A card on a hidden tab is skipped - rotating the photo costs several
        ms per card at the ~60 Hz position rate - and catches up when its tab
        is shown (_select_tab)."""
        for canvas, draw in getattr(self, "_wheel_cards", ()):
            if canvas.winfo_viewable():
                draw(self._last_wheel_angle)

    def _build_pedals_tab(self, parent):
        root = tk.Frame(parent, bg=MAIN_BG)
        root.pack(fill="both", expand=True)

        # No manual port/connect UI - the app auto-connects by VID:PID
        # (see _auto_scan) same as the Dash/Wheelbase tabs, so there's
        # nothing here for the user to have to look at or operate.

        # Small live glow-bar strip - same halo+cap pill look as the Main
        # tab's throttle/brake gauges (_build_glow_pedal_card), just a
        # compact horizontal cut of it (_add_card_mini_bar) so the curve
        # cards below still have room without the tab needing to scroll.
        bars_card = self._make_flat_card(root, 900, 56)
        bars_card.pack(fill="x", padx=12, pady=(0, 10))
        self.pedal_throttle_update = self._add_card_mini_bar(bars_card, 0, "THROTTLE", MAIN_GREEN)
        self.pedal_brake_update = self._add_card_mini_bar(bars_card, 1, "BRAKE", MAIN_RED)

        # Per-pedal response curve editors, side by side. Tighter outer/inter-card
        # padding than the rest of the tab so the plots themselves get the space.
        curves_row = tk.Frame(root, bg=MAIN_BG)
        curves_row.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        curves_row.columnconfigure(0, weight=1)
        curves_row.columnconfigure(1, weight=1)
        curves_row.rowconfigure(0, weight=1)
        throttle_card = self._build_pedal_curve_card(curves_row, "throttle", "THROTTLE CURVE", MAIN_GREEN)
        throttle_card.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        brake_card = self._build_pedal_curve_card(curves_row, "brake", "BRAKE CURVE", MAIN_RED)
        brake_card.grid(row=0, column=1, sticky="nsew", padx=(4, 0))

        # Actions - just a status line now; every calibration/curve change
        # applies AND saves to flash immediately (see _send_calibration),
        # so there's no separate "Save to flash" step left to expose, and
        # the raw hall/load numbers were only ever useful for eyeballing
        # calibration by hand, which the wizard replaced.
        act = tk.Frame(root, bg=MAIN_BG)
        act.pack(fill="x", padx=12, pady=(0, 12))
        self.status_label = tk.Label(act, text="", font=("Segoe UI", 9), fg=MAIN_TEXT_DIM, bg=MAIN_BG)
        self.status_label.pack(side="left", padx=8)

    def _add_card_mini_bar(self, card, column, label, color):
        """Compact single-row cut of the glow-pill bar style shared with
        the Main tab (halo + core fill + bright leading-edge cap) - name,
        bar and %% inline, two of them side by side (column 0 or 1) on the
        Pedals tab's flat bars card, so the whole strip stays ~56px tall.
        Read-only, just a live monitor. Laid out where the old grid of
        Label / Canvas / Label sat. Returns update(raw_value_0_1023)."""
        name_font, value_font = ("Segoe UI", 9, "bold"), ("Segoe UI", 10, "bold")
        # Those Labels were `width=8` / `width=5` characters wide.
        name_w, value_w = _label_width(card, name_font, 8), _label_width(card, value_font, 5)
        name_h, value_h = _label_height(name_font), _label_height(value_font)
        name_id = card.create_text(0, 0, text=label, font=name_font, fill=MAIN_TEXT_DIM, anchor="w")
        value_id = card.create_text(0, 0, text="0%", font=value_font, fill=MAIN_TEXT, anchor="e")
        tag = f"minibar{column}"

        halo_color = _mix_color(MAIN_PANEL, color, 0.35)
        cap_color = _mix_color(color, "#ffffff", 0.55)
        height = 18
        pad = 2
        r = (height - 2 * pad) / 2
        state = {"raw": 0}

        def redraw(*_args):
            w = card.winfo_width()
            if w <= 1:
                return
            col_gap = 32
            col_w = (w - 2 * CARD_PAD - col_gap) / 2
            row_x, row_y = CARD_PAD + column * (col_w + col_gap) + 4, CARD_PAD + 4
            row_w = col_w - 8
            card.coords(name_id, row_x + 3, row_y + 1 + name_h / 2)
            card.coords(value_id, row_x + row_w - 3, row_y + value_h / 2)
            card.delete(tag)
            bx0, by0 = row_x + name_w + 8, row_y + 2
            bx1, by1 = row_x + row_w - value_w - 8, by0 + height
            if bx1 - bx0 <= 2 * pad:
                return
            _draw_round_rect(card, bx0 + pad, by0 + pad, bx1 - pad, by1 - pad, r, fill="#0a0b10", outline=MAIN_BORDER,
                              width=1, tags=tag)
            frac = max(0.0, min(1.0, state["raw"] / 1023.0))
            fill_x = bx0 + pad + frac * (bx1 - bx0 - 2 * pad)
            if fill_x > bx0 + pad + r:
                halo_pad = 2
                _draw_round_rect(card, bx0 + pad - halo_pad, by0 + pad - halo_pad, fill_x + halo_pad,
                                  by1 - pad + halo_pad, r, fill=halo_color, outline="", tags=tag)
                _draw_round_rect(card, bx0 + pad, by0 + pad, fill_x, by1 - pad, r, fill=color, outline="", tags=tag)
                cap_w = 3
                _draw_round_rect(card, max(bx0 + pad, fill_x - cap_w), by0 + pad, fill_x, by1 - pad, r,
                                  fill=cap_color, outline="", tags=tag)

        def update(raw_value):
            state["raw"] = raw_value
            card.itemconfig(value_id, text=f"{round(max(0.0, min(1.0, raw_value / 1023.0)) * 100)}%")
            redraw()

        card.bind("<Configure>", redraw, add="+")
        update(0)
        return update

    def _build_pedal_curve_card(self, parent, pedal_key, title, color):
        """One pedal's response-curve editor: a live preview plot, 5
        vertical sliders (output %% at 0/25/50/75/100%% input - matches
        CalibrationData's throttleCurve/brakeCurve), and preset buttons
        that just set the 5 sliders to a known shape. Sliders redraw the
        preview on every drag but only push the new curve to the device
        on release (same pattern as the Main tab's glow sliders) - a
        button-mash of intermediate drag positions would otherwise spam
        the serial link and wear the flash if Save were hit right after."""
        # A flat card: the title is canvas text, and the Calibrate button,
        # plot and preset row are embedded straight in the card canvas
        # rather than packed in an inner Frame - one fewer layer to lay out
        # and repaint on every resize event. Offsets are the ones the old
        # packed layout had.
        card = self._make_flat_card(parent, 420, 340)
        title_font = ("Segoe UI", 11, "bold")
        card.create_text(CARD_PAD + 3, CARD_PAD + 2 + _label_height(title_font) / 2, text=title, font=title_font,
                         fill=MAIN_TEXT, anchor="w")
        calibrate = tk.Button(card, text="Calibrate", font=("Segoe UI", 9, "bold"), fg=MAIN_BG, bg=MAIN_CYAN,
                              activebackground=MAIN_CYAN, activeforeground=MAIN_BG, relief="flat", padx=12, pady=3,
                              bd=0, cursor="hand2", command=lambda k=pedal_key: self._open_calibrate_wizard(k))
        calibrate_id = card.create_window(0, CARD_PAD + 2, window=calibrate, anchor="ne")

        # Curve is edited by dragging its 5 points directly on the plot
        # (see _curve_mouse_down/_drag/_up) rather than separate slider
        # widgets - x positions are fixed at the 0/25/50/75/100% input
        # breakpoints, only each point's output %% (its y) moves.
        #
        # Sized to fill the card between the header and the presets (see
        # layout() below): the card itself is gridded sticky="nsew" in a
        # weighted row, so making the window taller enlarges the plot instead
        # of leaving dead space under it - a bigger plot for the same tab
        # footprint, not a taller tab.
        canvas = tk.Canvas(card, height=220, bg="#0a0b10", highlightthickness=1, highlightbackground=MAIN_BORDER,
                           cursor="hand2")
        plot_top = CARD_PAD + 34
        plot_id = card.create_window(CARD_PAD + 2, plot_top, window=canvas, anchor="nw")
        if pedal_key == "throttle":
            self.throttle_curve_canvas = canvas
        else:
            self.brake_curve_canvas = canvas
        default = DEFAULT_THROTTLE_CURVE if pedal_key == "throttle" else DEFAULT_BRAKE_CURVE
        curve_vars = [tk.IntVar(value=v) for v in default]
        if pedal_key == "throttle":
            self.throttle_curve_vars = curve_vars
        else:
            self.brake_curve_vars = curve_vars

        canvas.bind("<Configure>", lambda _e, k=pedal_key: self._redraw_pedal_curve(k))
        canvas.bind("<Button-1>", lambda e, k=pedal_key: self._curve_mouse_down(e, k))
        canvas.bind("<B1-Motion>", lambda e, k=pedal_key: self._curve_mouse_drag(e, k))
        canvas.bind("<ButtonRelease-1>", lambda e, k=pedal_key: self._curve_mouse_up(e, k))

        preset_row = tk.Frame(card, bg=MAIN_PANEL)
        presets = (
            ("Linear", CURVE_PRESET_LINEAR),
            ("S-Curve", CURVE_PRESET_S_CURVE),
            ("Exponential", CURVE_PRESET_EXPONENTIAL),
            ("Rev. Exponential", CURVE_PRESET_REVERSE_EXPONENTIAL),
        )
        for label, values in presets:
            tk.Button(preset_row, text=label, font=("Segoe UI", 8), fg=MAIN_TEXT, bg=MAIN_BORDER,
                      activebackground=color, activeforeground=MAIN_BG, relief="flat", padx=8, pady=4,
                      bd=0, cursor="hand2",
                      command=lambda k=pedal_key, v=values: self._apply_curve_preset(k, v)).pack(side="left", padx=3)
        preset_id = card.create_window(0, 0, window=preset_row, anchor="n")

        def layout(event):
            w, h = event.width, event.height
            preset_h = preset_row.winfo_reqheight()  # only known once Tk has laid the buttons out
            card.coords(calibrate_id, w - CARD_PAD, CARD_PAD + 2)
            # 8 px under the plot, then 2 px above the preset row, as packed before
            card.itemconfig(plot_id, width=max(10, w - 2 * CARD_PAD - 4),
                            height=max(10, h - CARD_PAD - preset_h - 10 - plot_top))
            card.coords(preset_id, w / 2, h - CARD_PAD - preset_h)

        card.bind("<Configure>", layout, add="+")
        self._redraw_pedal_curve(pedal_key)
        return card

    def _curve_plot_geometry(self, canvas, n):
        """Shared pad/point-x-position math between drawing and hit-testing,
        so a drag always grabs exactly the point it looks like it's over."""
        w, h = canvas.winfo_width(), canvas.winfo_height()
        pad = 14
        xs = [pad + (w - 2 * pad) * (i / (n - 1)) for i in range(n)] if w > 1 else None
        return w, h, pad, xs

    def _curve_mouse_down(self, event, pedal_key):
        canvas = self.throttle_curve_canvas if pedal_key == "throttle" else self.brake_curve_canvas
        curve_vars = self.throttle_curve_vars if pedal_key == "throttle" else self.brake_curve_vars
        _w, _h, _pad, xs = self._curve_plot_geometry(canvas, len(curve_vars))
        if not xs:
            return
        nearest_i = min(range(len(xs)), key=lambda i: abs(xs[i] - event.x))
        if abs(xs[nearest_i] - event.x) <= 28:  # only grab a point if the click is reasonably close to it
            self._curve_drag = (pedal_key, nearest_i)
            self._curve_mouse_drag(event, pedal_key)

    def _curve_mouse_drag(self, event, pedal_key):
        if self._curve_drag is None or self._curve_drag[0] != pedal_key:
            return
        _, i = self._curve_drag
        canvas = self.throttle_curve_canvas if pedal_key == "throttle" else self.brake_curve_canvas
        curve_vars = self.throttle_curve_vars if pedal_key == "throttle" else self.brake_curve_vars
        _w, h, pad, _xs = self._curve_plot_geometry(canvas, len(curve_vars))
        frac = 1.0 - (event.y - pad) / (h - 2 * pad)
        curve_vars[i].set(max(0, min(100, round(frac * 100))))
        self._redraw_pedal_curve(pedal_key)

    def _curve_mouse_up(self, _event, pedal_key):
        if self._curve_drag is not None and self._curve_drag[0] == pedal_key:
            self._curve_drag = None
            self._send_calibration()

    def _apply_curve_preset(self, pedal_key, preset):
        curve_vars = self.throttle_curve_vars if pedal_key == "throttle" else self.brake_curve_vars
        for var, v in zip(curve_vars, preset):
            var.set(v)
        self._redraw_pedal_curve(pedal_key)
        self._send_calibration()

    def _redraw_pedal_curve(self, pedal_key):
        if pedal_key == "throttle":
            canvas, curve_vars, color = self.throttle_curve_canvas, self.throttle_curve_vars, MAIN_GREEN
        else:
            canvas, curve_vars, color = self.brake_curve_canvas, self.brake_curve_vars, MAIN_RED
        n = len(curve_vars)
        w, h, pad, xs = self._curve_plot_geometry(canvas, n)
        if w <= 1 or h <= 1:
            return
        canvas.delete("all")
        canvas.create_line(pad, h - pad, w - pad, pad, fill=MAIN_BORDER, dash=(3, 2))  # linear reference
        for frac in (0.25, 0.5, 0.75):  # faint gridlines - helps eyeball where a drag will land
            y = (h - pad) - (h - 2 * pad) * frac
            canvas.create_line(pad, y, w - pad, y, fill=MAIN_BORDER, dash=(1, 3))
        pts = []
        for i, var in enumerate(curve_vars):
            y = (h - pad) - (h - 2 * pad) * (var.get() / 100.0)
            pts += [xs[i], y]
        canvas.create_line(*pts, fill=color, width=2)
        for i in range(0, len(pts), 2):
            canvas.create_oval(pts[i] - 6, pts[i + 1] - 6, pts[i] + 6, pts[i + 1] + 6,
                               fill=color, outline=MAIN_TEXT, width=1)
            above = pts[i + 1] > pad + 10
            canvas.create_text(pts[i], pts[i + 1] + (12 if above else -12),
                               text=f"{curve_vars[i // 2].get()}%", fill=MAIN_TEXT_DIM, font=("Segoe UI", 8))
        for i, pct_label in enumerate(("0%", "25%", "50%", "75%", "100%")):
            canvas.create_text(xs[i], h - 4, text=pct_label, fill=MAIN_TEXT_DIM, font=("Segoe UI", 7), anchor="s")

    def _open_calibrate_wizard(self, pedal_key):
        """Guided 2-step calibration for one pedal at a time: rest, then
        pressed fully. Only touches that pedal's hallMin/hallMax or
        loadMin/loadMax in cal_state - the other pedal's calibration (and
        both curves) are untouched. Replaces the old manual "type in the
        raw number" fields - the live raw values are already streaming in
        over STATUS packets, so there's nothing for the user to read off
        and transcribe themselves."""
        if not self.ser:
            messagebox.showwarning("Not connected", "Connect to the pedals first.")
            return

        pedal_name = "Throttle" if pedal_key == "throttle" else "Brake"
        raw_field = "hall" if pedal_key == "throttle" else "load"

        win = tk.Toplevel(self.root)
        win.title(f"Calibrate {pedal_name}")
        win.configure(bg=MAIN_PANEL)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()

        steps = [
            ("rest", f"Release the {pedal_name.upper()} pedal fully and leave it at rest."),
            ("full", f"Press the {pedal_name.upper()} pedal all the way to the floor and hold it."),
        ]
        state = {"step": 0, "min": None, "max": None}

        label = tk.Label(win, text=steps[0][1], font=("Segoe UI", 12), fg=MAIN_TEXT, bg=MAIN_PANEL,
                          wraplength=320, justify="center")
        label.pack(padx=24, pady=(24, 8))
        reading_label = tk.Label(win, text="", font=("Segoe UI", 10), fg=MAIN_TEXT_DIM, bg=MAIN_PANEL)
        reading_label.pack(pady=(0, 16))
        btn_row = tk.Frame(win, bg=MAIN_PANEL)
        btn_row.pack(pady=(0, 20))

        def current_reading():
            if not self.last_status:
                return None
            hall_raw, hall_f, load_raw, _throttle, _brake = self.last_status
            return hall_f if pedal_key == "throttle" else load_raw

        def refresh_reading():
            if not win.winfo_exists():
                return
            v = current_reading()
            if v is not None:
                reading_label.config(text=f"live: {raw_field}={v}")
            win.after(100, refresh_reading)

        def do_next():
            v = current_reading()
            if v is None:
                messagebox.showwarning("No data", "No live data yet.", parent=win)
                return
            step_key = steps[state["step"]][0]
            if step_key == "rest":
                state["min"] = v
            else:
                state["max"] = v

            state["step"] += 1
            if state["step"] < len(steps):
                label.config(text=steps[state["step"]][1])
                next_btn.config(text="Finish")
            else:
                if pedal_key == "throttle":
                    self.cal_state["hallMin"] = state["min"]
                    self.cal_state["hallMax"] = state["max"]
                else:
                    self.cal_state["loadMin"] = state["min"]
                    self.cal_state["loadMax"] = state["max"]
                self._send_calibration()  # applies AND saves to flash - see _send_calibration
                win.destroy()

        next_btn = tk.Button(btn_row, text="Next", font=("Segoe UI", 10, "bold"), fg=MAIN_BG, bg=MAIN_CYAN,
                              activebackground=MAIN_CYAN, activeforeground=MAIN_BG, relief="flat", padx=20,
                              pady=6, bd=0, cursor="hand2", command=do_next)
        next_btn.pack(side="left", padx=6)
        tk.Button(btn_row, text="Cancel", font=("Segoe UI", 10), fg=MAIN_TEXT, bg=MAIN_BORDER,
                  activebackground=MAIN_BORDER, activeforeground=MAIN_TEXT, relief="flat", padx=20, pady=6,
                  bd=0, cursor="hand2", command=win.destroy).pack(side="left", padx=6)

        refresh_reading()

    def _build_wheelbase_tab(self, parent):
        # Same look as the Main tab: flat buttons on the dark background, then
        # a rounded flat card with one full-width glow-bar row per setting,
        # all stacked in a single column.
        top = tk.Frame(parent, bg=MAIN_BG)
        top.pack(fill="x", padx=20, pady=(12, 8))
        self.wb_status_label = tk.Label(top, text="no wheelbase support yet", font=("Segoe UI", 9),
                                        fg="gray", bg=MAIN_BG)
        self.wb_status_label.pack(side="left")
        # Only Save & Reboot: every change applies as it happens and Volgas
        # re-applies its remembered values on connect, so there's no Apply
        # button; Reboot and Reset Center were dropped too (Set Centre is on
        # the Main tab).
        tk.Button(top, text="Save & Reboot", font=("Segoe UI", 9), fg=MAIN_TEXT, bg=MAIN_PANEL,
                  activebackground=MAIN_BORDER, activeforeground=MAIN_TEXT, relief="flat",
                  padx=12, pady=4, bd=0, cursor="hand2",
                  command=lambda: self._wb_send_command(("save_and_reboot",))).pack(side="right")

        # name -> IntVar holding the field's real value for every setting on
        # this tab - written by the read_effect_settings()/
        # read_hardware_settings() sync (_on_wb_settings) and by the rows.
        self.wb_field_vars = {}
        effect_keys, hardware_keys = list(WB_EFFECT_KEYS), list(WB_HARDWARE_KEYS)
        # One name column wide enough for the longest name, so every bar
        # starts at the same x.
        names = [_wb_display_name(k).upper() for k in effect_keys + hardware_keys] + ["INTEGRATED SPRING"]
        name_w = max(tkfont.Font(font=WB_NAME_FONT).measure(n) for n in names) + 24

        # One card, one list: the effect settings, the Integrated Spring
        # switch, then the hardware settings, with no section headings
        # (user, 2026-10-23), and the Power Limit caution as the last line.
        rows = [("slider", k) for k in effect_keys] + [("spring", None)] + [("slider", k) for k in hardware_keys]
        # A copy of the Main tab's wheel card at the top, centred, filling all
        # the height the settings card below doesn't need. The settings card
        # is packed first (from the bottom) so if the window is short it's the
        # wheel card that gives up height, never the settings. Small
        # requested sizes so this tab doesn't raise the window's minimum size.
        card = self._make_flat_card(parent, 520, WB_ROWS_TOP + (len(rows) + 1) * WB_ROW_H + CARD_PAD)
        card.pack(side="bottom", fill="x", padx=20, pady=(0, 20))
        wheel_card = self._build_wheel_card(parent, 200, 160)
        wheel_card.pack(side="top", fill="y", expand=True, pady=(0, 12))

        # Width follows height in the Main tab card's 600x480 proportions, so
        # the wheel fills its card the same way instead of sitting in a
        # full-width strip. Only re-requests when it's off by more than a
        # pixel, so the width change's own <Configure> doesn't loop.
        def keep_proportions(event):
            want = round(event.height * WHEEL_CARD_ASPECT)
            if abs(int(wheel_card.cget("width")) - want) > 1:
                wheel_card.configure(width=want)

        wheel_card.bind("<Configure>", keep_proportions, add="+")
        for i, (kind, key) in enumerate(rows):
            if kind == "spring":
                self._add_wb_spring_row(card, WB_ROWS_TOP + i * WB_ROW_H, name_w)
            else:
                self._add_wb_slider_row(card, WB_ROWS_TOP + i * WB_ROW_H, key, name_w)
        card.create_text(CARD_PAD + 4, WB_ROWS_TOP + len(rows) * WB_ROW_H + WB_ROW_H / 2,
                         text="Power Limit is a torque-limiting safety field - raise with care.",
                         font=("Segoe UI", 9), fill=MAIN_ORANGE_STATUS, anchor="w")

        # Tk on Windows sends <MouseWheel> / key presses to the focused widget,
        # not the one under the pointer - so the card takes focus on hover and
        # its rows' wheel / arrow-key nudges work.
        card.bind("<Enter>", lambda _e: card.focus_set(), add="+")

    def _build_settings_tab(self, parent):
        """Opened by clicking the logo (no tab button). Holds the updater, on a
        flat card like the other pages: version, a check-on-startup switch,
        status, Check / Update buttons and the newer release's notes."""
        names = ["VERSION", "CHECK ON STARTUP", "STATUS", "WHAT'S NEW"]
        name_w = max(tkfont.Font(font=WB_NAME_FONT).measure(n) for n in names) + 24
        rows = 8  # version, switch, status, buttons, then four for release notes
        card = self._make_flat_card(parent, 520, WB_ROWS_TOP + rows * WB_ROW_H + CARD_PAD)
        card.pack(fill="x", padx=20, pady=20)
        x0 = CARD_PAD + name_w  # where values start, like the Wheelbase bars

        version_id, _ = self._add_wb_row_label(card, WB_ROWS_TOP, "VERSION")
        card.itemconfig(version_id, text=f"v{VOLGAS_VERSION}")

        switch_value_id, switch_mid = self._add_wb_row_label(card, WB_ROWS_TOP + WB_ROW_H, "CHECK ON STARTUP")
        on = {"value": self._wb_settings.get("check_updates_on_start", True)}

        def redraw_switch():
            _draw_toggle(card, on["value"], x0, switch_mid - 10, tag="update_switch")
            card.itemconfig(switch_value_id, text="On" if on["value"] else "Off")

        def press(event):
            if x0 - 4 <= event.x <= x0 + 44 and switch_mid - 14 <= event.y <= switch_mid + 14:
                on["value"] = not on["value"]
                self._wb_settings["check_updates_on_start"] = on["value"]
                _save_settings(self._wb_settings)
                redraw_switch()

        card.bind("<Button-1>", press, add="+")
        redraw_switch()

        status_mid = WB_ROWS_TOP + 2 * WB_ROW_H + WB_ROW_H / 2
        card.create_text(CARD_PAD + 4, status_mid, text="STATUS", font=WB_NAME_FONT, fill=MAIN_TEXT_DIM, anchor="w")
        self._update_status_id = card.create_text(x0, status_mid, text="Not checked yet", font=("Segoe UI", 10),
                                                  fill=MAIN_TEXT, anchor="w")

        button_style = dict(font=("Segoe UI", 9), fg=MAIN_TEXT, bg=MAIN_BORDER, activebackground=MAIN_CYAN,
                            activeforeground=MAIN_BG, relief="flat", padx=12, pady=4, bd=0, cursor="hand2")
        buttons = tk.Frame(card, bg=MAIN_PANEL)
        tk.Button(buttons, text="Check for updates", command=self._start_update_check, **button_style).pack(side="left")
        self._update_install_btn = tk.Button(buttons, text="Update", command=self._start_update_install,
                                             **button_style)
        card.create_window(x0, WB_ROWS_TOP + 3 * WB_ROW_H + WB_ROW_H / 2, window=buttons, anchor="w")

        notes_top = WB_ROWS_TOP + 4 * WB_ROW_H
        self._update_notes_title_id = card.create_text(CARD_PAD + 4, notes_top + WB_ROW_H / 2, text="",
                                                       font=WB_NAME_FONT, fill=MAIN_TEXT_DIM, anchor="w")
        self._update_notes_id = card.create_text(x0, notes_top + 8, text="", font=("Segoe UI", 9),
                                                 fill=MAIN_TEXT_DIM, anchor="nw")
        card.bind("<Configure>", lambda e: card.itemconfig(
            self._update_notes_id, width=max(50, e.width - x0 - CARD_PAD - 8)), add="+")
        self._settings_card = card

    def _set_update_status(self, text, color=MAIN_TEXT):
        self._settings_card.itemconfig(self._update_status_id, text=text, fill=color)

    def _start_update_check(self):
        if self._update_busy:
            return
        self._update_busy = True
        self._update_phase = "check"
        self._set_update_status("Checking for updates...")

        def work():
            try:
                self._update_queue.put(("checked", _fetch_latest_release()))
            except Exception as e:
                self._update_queue.put(("error", e))

        threading.Thread(target=work, daemon=True).start()
        self._update_poll()

    def _start_update_install(self):
        if self._update_busy:
            return
        version, _notes, exe_url, sha_url = self._latest_release
        if not getattr(sys, "frozen", False):
            self._set_update_status("Updating only works in Volgas.exe, not when running volgas.py", MAIN_ORANGE_STATUS)
            return
        if not messagebox.askyesno("Update Volgas", f"Download and install Volgas v{version}? "
                                                     "Volgas will close and restart itself."):
            return
        self._update_busy = True
        self._update_phase = "download"
        self._set_update_status("Downloading v{}... 0%".format(version))

        def work():
            try:
                path = _download_update(exe_url, sha_url,
                                        lambda frac: self._update_queue.put(("progress", frac)))
                self._update_queue.put(("downloaded", path))
            except Exception as e:
                self._update_queue.put(("error", e))

        threading.Thread(target=work, daemon=True).start()
        self._update_poll()

    def _update_poll(self):
        """Handle the update worker's messages on the UI thread; keeps polling
        only while a check or download is running."""
        try:
            while True:
                kind, value = self._update_queue.get_nowait()
                if kind == "checked":
                    self._update_busy = False
                    self._on_update_checked(value)
                elif kind == "progress":
                    self._set_update_status(f"Downloading v{self._latest_release[0]}... {round(value * 100)}%")
                elif kind == "downloaded":
                    self._set_update_status("Installing...")
                    try:
                        _install_update(value)
                    except OSError as e:
                        self._update_busy = False
                        self._set_update_status(f"Couldn't install the update: {e}", MAIN_RED)
                        return
                    self.root.destroy()
                    return
                elif kind == "error":
                    self._update_busy = False
                    if self._update_phase == "download":
                        try:  # a failed download can leave a partial file
                            os.remove(sys.executable + ".new")
                        except OSError:
                            pass
                        text = f"Update failed: {value}"
                    elif isinstance(value, urllib.error.HTTPError) and value.code == 404:
                        text = "Couldn't check for updates: no public release found"
                    else:
                        text = f"Couldn't check for updates: {value}"
                    self._set_update_status(text, MAIN_RED)
        except queue.Empty:
            pass
        if self._update_busy:
            self.root.after(100, self._update_poll)

    def _on_update_checked(self, release):
        self._latest_release = release
        version, notes, exe_url, sha_url = release
        newer = _version_tuple(version) > _version_tuple(VOLGAS_VERSION)
        card = self._settings_card
        if newer and exe_url and sha_url:
            self._set_update_status(f"Volgas v{version} is available", MAIN_CYAN)
            self._update_install_btn.config(text=f"Update to v{version}")
            self._update_install_btn.pack(side="left", padx=(8, 0))
            card.itemconfig(self._update_notes_title_id, text="WHAT'S NEW")
            card.itemconfig(self._update_notes_id, text=notes.strip()[:600])
        else:
            text = (f"v{version} is out, but its download files are missing" if newer
                    else f"Volgas is up to date (v{VOLGAS_VERSION})")
            self._set_update_status(text, MAIN_ORANGE_STATUS if newer else MAIN_GREEN)
            self._update_install_btn.pack_forget()
            card.itemconfig(self._update_notes_title_id, text="")
            card.itemconfig(self._update_notes_id, text="")
        self._show_update_badge(newer and bool(exe_url and sha_url))

    def _remove_old_exe(self, tries=10):
        """Delete the Volgas.exe.old a previous in-app update left behind
        (_install_update). It can still be in use for a moment while the old
        process closes, so retry a few times."""
        old = sys.executable + ".old"
        if not getattr(sys, "frozen", False) or not os.path.exists(old):
            return
        try:
            os.remove(old)
        except OSError:
            if tries > 1:
                self.root.after(2000, lambda: self._remove_old_exe(tries - 1))

    def _add_wb_row_label(self, card, row_top, text):
        """Name on the left and value text on the right of one Wheelbase card
        row; returns (value_text_id, vertical centre of the row)."""
        mid = row_top + WB_ROW_H / 2
        card.create_text(CARD_PAD + 4, mid, text=text, font=WB_NAME_FONT, fill=MAIN_TEXT_DIM, anchor="w")
        value_id = card.create_text(0, mid, text="", font=("Segoe UI", 11, "bold"), fill=MAIN_TEXT, anchor="e")
        card.bind("<Configure>", lambda e: card.coords(value_id, e.width - CARD_PAD - 4, mid), add="+")
        return value_id, mid

    def _add_wb_slider_row(self, card, row_top, name, name_w):
        """One Wheelbase setting (range from WB_SLIDER_SPECS) as a full-width
        row on a flat card: name, glow-pill bar (same halo + cap look as the
        Main tab's sliders), value. Click or drag the bar to set it; fields
        with only a few valid values step between them. Sends every change
        straight away, while dragging too, and remembers the value (re-applied
        on every connect) once the drag ends - not on every intermediate
        position, which would rewrite the settings file at drag rate. Mouse
        wheel / arrow keys over the row nudge by exactly one step, so precise
        values are reachable. Sending silently does nothing while
        disconnected; the remembered value is applied on the next connect."""
        spec = WB_SLIDER_SPECS[name]
        choices = spec if isinstance(spec[0], tuple) else None
        bounds = None if choices else list(spec)
        unit = "%" if spec == (0, 100) else "°" if name == "MotionRange" else ""
        value_var = tk.IntVar(value=self._wb_remembered.get(name, choices[0][0] if choices else spec[0]))
        self.wb_field_vars[name] = value_var
        value_id, mid = self._add_wb_row_label(card, row_top, _wb_display_name(name).upper())

        color = MAIN_CYAN if name == "MotionRange" else MAIN_BLUE  # matching the Main tab's Steering Range / FFB bars
        halo_color = _mix_color(MAIN_PANEL, color, 0.35)
        cap_color = _mix_color(color, "#ffffff", 0.55)
        pad = 3
        y0, y1 = mid - WB_BAR_H / 2 + pad, mid + WB_BAR_H / 2 - pad
        r = (y1 - y0) / 2
        tag = f"wbrow_{name}"
        state = {"dragging": False, "hover": False}

        def bar_x():
            return CARD_PAD + name_w, card.winfo_width() - CARD_PAD - WB_VALUE_W

        def index_of(value):
            return next((i for i, (v, _label) in enumerate(choices) if v == value), 0)

        def frac_of(value):
            if choices:
                return index_of(value) / (len(choices) - 1)
            return (value - bounds[0]) / (bounds[1] - bounds[0])

        def redraw(*_args):
            value = value_var.get()
            if bounds and not bounds[0] <= value <= bounds[1]:
                bounds[0], bounds[1] = min(bounds[0], value), max(bounds[1], value)
            card.itemconfig(value_id, text=dict(choices).get(value, str(value)) if choices else f"{value}{unit}")
            card.delete(tag)
            x0, x1 = bar_x()
            if x1 - x0 <= 2 * pad + 2 * r:
                return
            _draw_round_rect(card, x0 + pad, y0, x1 - pad, y1, r, fill="#0a0b10", outline=MAIN_BORDER, width=1,
                              tags=tag)
            fill_x = x0 + pad + max(0.0, min(1.0, frac_of(value))) * (x1 - x0 - 2 * pad)
            if fill_x > x0 + pad + r:
                halo_pad = 3
                _draw_round_rect(card, x0 + pad - halo_pad, y0 - halo_pad, fill_x + halo_pad, y1 + halo_pad,
                                  r, fill=halo_color, outline="", tags=tag)
                _draw_round_rect(card, x0 + pad, y0, fill_x, y1, r, fill=color, outline="", tags=tag)
                _draw_round_rect(card, max(x0 + pad, fill_x - 4), y0, fill_x, y1, r, fill=cap_color,
                                  outline="", tags=tag)

        def set_value(value):
            if value != value_var.get():
                value_var.set(value)
                if self.wb_connected:
                    self.wb_cmd_queue.put(("send_setting", name, value))

        def set_from_x(x):
            x0, x1 = bar_x()
            frac = max(0.0, min(1.0, (x - x0 - pad) / max(1, x1 - x0 - 2 * pad)))
            if choices:
                set_value(choices[round(frac * (len(choices) - 1))][0])
            else:
                set_value(round(bounds[0] + frac * (bounds[1] - bounds[0])))

        def commit():
            value = value_var.get()
            self._remember_wb(name, value)
            self._on_wb_settings({name: value})  # keep the Main tab's matching controls in step

        def nudge(steps):
            if choices:
                set_value(choices[max(0, min(len(choices) - 1, index_of(value_var.get()) + steps))][0])
            else:
                set_value(max(bounds[0], min(bounds[1], value_var.get() + steps)))
            commit()

        def in_row(event):
            return row_top <= event.y < row_top + WB_ROW_H

        def press(event):
            x0, x1 = bar_x()
            if in_row(event) and x0 - 6 <= event.x <= x1 + 6:
                state["dragging"] = True
                set_from_x(event.x)

        def motion(event):
            state["hover"] = in_row(event)
            if state["dragging"]:
                set_from_x(event.x)

        def release(_event):
            if state["dragging"]:
                state["dragging"] = False
                commit()

        def key(steps):
            if state["hover"]:
                nudge(steps)

        card.bind("<Configure>", redraw, add="+")
        card.bind("<Button-1>", press, add="+")
        card.bind("<B1-Motion>", motion, add="+")
        card.bind("<Motion>", motion, add="+")
        card.bind("<Leave>", lambda _e: state.update(hover=False), add="+")
        card.bind("<ButtonRelease-1>", release, add="+")
        card.bind("<MouseWheel>", lambda e: nudge(1 if e.delta > 0 else -1) if in_row(e) else None, add="+")
        card.bind("<Left>", lambda _e: key(-1), add="+")
        card.bind("<Right>", lambda _e: key(1), add="+")
        value_var.trace_add("write", redraw)
        redraw()

    def _add_wb_spring_row(self, card, row_top, name_w):
        """Integrated Spring as an on/off switch row instead of a slider: on
        sets it to INTEGRATED_SPRING_ON_PCT, off to 0. Shows On if the device
        reports any non-zero strength (e.g. one set in FFBeast's own app)."""
        var = tk.IntVar(value=self._wb_remembered.get("IntegratedSpringStrength", 0))
        self.wb_field_vars["IntegratedSpringStrength"] = var
        value_id, mid = self._add_wb_row_label(card, row_top, "INTEGRATED SPRING")
        x, y = CARD_PAD + name_w, mid - 10  # top-left of the 40x20 switch, where the bars start

        def redraw(*_args):
            _draw_toggle(card, var.get() > 0, x, y, tag="wb_spring_toggle")
            card.itemconfig(value_id, text=f"{var.get()}%" if var.get() > 0 else "Off")

        def press(event):
            if x - 4 <= event.x <= x + 44 and y - 4 <= event.y <= y + 24:
                var.set(0 if var.get() > 0 else INTEGRATED_SPRING_ON_PCT)
                self._remember_wb("IntegratedSpringStrength", var.get())
                if self.wb_connected:
                    self.wb_cmd_queue.put(("send_setting", "IntegratedSpringStrength", var.get()))

        var.trace_add("write", redraw)
        card.bind("<Button-1>", press, add="+")
        redraw()

    # ---------- serial ----------

    def _connect_to(self, port):
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.05)
        except serial.SerialException:
            self.ser = None  # auto-scan retries silently every second, nothing to report here
            return
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()
        self._request_cal()

    def _disconnect(self):
        ser, self.ser = self.ser, None
        if ser:
            try:
                ser.close()
            except serial.SerialException:
                pass

    def _auto_scan(self):
        if not self.ser:
            port = find_pedal_port()
            if port:
                self._connect_to(port)
        self.root.after(1000, self._auto_scan)

    def _read_loop(self):
        while self.ser:
            try:
                data = self.ser.read(64)
            except (serial.SerialException, TypeError, AttributeError):
                self.rx_queue.put(("lost", b""))
                return
            if data:
                self.parser.feed(data)

    def _send(self, frame):
        if not self.ser:
            messagebox.showwarning("Not connected", "Connect to the device first.")
            return False
        try:
            self.ser.write(frame)
            return True
        except serial.SerialException as e:
            messagebox.showerror("Write failed", str(e))
            self._disconnect()
            return False

    # ---------- commands ----------

    def _request_cal(self):
        if self.ser:
            self._send(build_frame(MSG_GET_CALIBRATION))

    def _send_calibration(self):
        """Push the complete CalibrationData struct - hallMin/Max +
        loadMin/Max from cal_state (set by the calibrate wizard) plus both
        curves' current point values - then immediately persist it to
        flash. The wire format has no way to update just one field, so
        every curve-point drag release, every preset click, and every
        wizard finish resends the whole thing; folding the flash save in
        here too (rather than a separate button) means a change always
        survives a power cycle without an extra step. Flash wear isn't a
        real concern - this only fires on deliberate user edits, nowhere
        near the emulated EEPROM's write-cycle budget."""
        if not self.ser:
            return
        payload = struct.pack(
            CAL_FMT,
            self.cal_state["hallMin"], self.cal_state["hallMax"],
            self.cal_state["loadMin"], self.cal_state["loadMax"],
            *(v.get() for v in self.throttle_curve_vars),
            *(v.get() for v in self.brake_curve_vars),
        )
        if self._send(build_frame(MSG_SET_CALIBRATION, payload)):
            self._save()

    def _save(self):
        if self._send(build_frame(MSG_SAVE)):
            self.status_label.config(text="saving...", foreground=MAIN_TEXT)

    # ---------- wheelbase (HID PID) ----------

    def _wb_send_command(self, cmd):
        if not self.wb_connected:
            messagebox.showwarning("Not connected", "Wheelbase not connected.")
            return
        self.wb_cmd_queue.put(cmd)

    def _on_wb_settings(self, settings):
        """Populate the Wheelbase tab's sliders and sync the Main tab's
        Forces Enabled / Force Strength / Max steering angle controls from a
        real read_effect_settings()+read_hardware_settings() dict (or a
        single field just changed on the Wheelbase tab)."""
        for name, value in settings.items():
            var = self.wb_field_vars.get(name)
            if var is not None:
                var.set(value)
        if "ForceEnabled" in settings:
            self.main_forces_enabled_var.set(bool(settings["ForceEnabled"]))
            self._redraw_forces_toggle()
        if "PowerLimit" in settings:
            self.main_force_strength_var.set(settings["PowerLimit"])  # slider redraw + label text via its own trace
        if "MotionRange" in settings:
            deg = settings["MotionRange"]
            self.main_motion_range_var.set(deg)  # slider redraw + label text via its own trace

    # ---------- incoming ----------

    def _poll(self):
        try:
            while True:
                msg_type, payload = self.rx_queue.get_nowait()
                if msg_type == "lost":
                    self._disconnect()
                elif msg_type == MSG_STATUS and len(payload) == STATUS_SIZE:
                    self._on_status(struct.unpack(STATUS_FMT, payload))
                elif msg_type == MSG_CALIBRATION and len(payload) == CAL_SIZE:
                    self._on_calibration(struct.unpack(CAL_FMT, payload))
                elif msg_type == MSG_SAVE_ACK and len(payload) == 1:
                    ok = payload[0] == 1
                    self.status_label.config(
                        text="saved to flash" if ok else "FLASH SAVE FAILED",
                        foreground="green" if ok else "red")
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    def _on_status(self, st):
        self.last_status = st  # raw hall/load fields still consumed directly by the calibrate wizard
        _hall_raw, _hall_f, _load_raw, throttle, brake = st
        self.pedal_throttle_update(throttle)
        self.pedal_brake_update(brake)
        self.main_throttle_update(throttle)
        self.main_brake_update(brake)

    def _on_calibration(self, cal):
        hall_min, hall_max, load_min, load_max, *curves = cal
        self.cal_state["hallMin"] = hall_min
        self.cal_state["hallMax"] = hall_max
        self.cal_state["loadMin"] = load_min
        self.cal_state["loadMax"] = load_max
        for var, v in zip(self.throttle_curve_vars, curves[:5]):
            var.set(v)
        for var, v in zip(self.brake_curve_vars, curves[5:]):
            var.set(v)
        self._redraw_pedal_curve("throttle")
        self._redraw_pedal_curve("brake")
        self.status_label.config(text="calibration read from device", foreground=MAIN_TEXT)


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    splash = show_splash(root)
    shown_at = time.monotonic()
    VolgasApp(root)
    # Building the app counts toward the splash time rather than adding to it.
    remaining_ms = max(0, SPLASH_MS - int((time.monotonic() - shown_at) * 1000))

    def end_splash():
        splash.destroy()
        root.deiconify()

    root.after(remaining_ms, end_splash)
    root.mainloop()
