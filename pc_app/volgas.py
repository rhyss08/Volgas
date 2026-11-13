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


def _process_image(pid):
    """Full exe path of a running process, or None if it can't be read."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buf))
        return buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)) else None
    finally:
        kernel32.CloseHandle(handle)


def _restart_if_borrowing_unpack_folder():
    """Updates from 0.2.3 and older start the new exe without the reset in
    _install_update, so it runs from the old process's unpack folder. A
    normal onefile start has its own bootloader (the same exe) as parent;
    here the parent is the old Volgas.exe.old, so start again cleanly and
    let this copy exit before the old folder disappears. Runs before the
    rest of the imports, which load files from that folder."""
    if not getattr(sys, "frozen", False) or os.environ.get("VOLGAS_RESTARTED"):
        return
    parent = _process_image(os.getppid())
    if parent and os.path.normcase(parent) == os.path.normcase(sys.executable):
        return
    subprocess.Popen([sys.executable], close_fds=True,
                     env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1", "VOLGAS_RESTARTED": "1"})
    sys.exit(0)


_restart_if_borrowing_unpack_folder()

import logging
import tkinter as tk
import urllib.error
import urllib.request
from tkinter import ttk, messagebox
from tkinter import font as tkfont

import certifi
import serial
import serial.tools.list_ports
from collections import deque
from PIL import Image, ImageDraw, ImageTk

import volgas_dash_font
import volgas_devices as devs
import volgas_theme as T
import volgas_ui as ui
import volgas_wheel_ota
from volgas_theme import px
from volgas_log import setup_logging
from volgas_pedal_protocol import (
    MSG_STATUS, MSG_SET_CALIBRATION, MSG_SAVE, MSG_GET_CALIBRATION,
    MSG_CALIBRATION, MSG_SAVE_ACK, CAL_FMT, STATUS_FMT, CAL_SIZE, STATUS_SIZE, AXIS_MAX,
    THROTTLE_LIMITS, BRAKE_LIMITS, CAPTURE_SECONDS, find_pedal_port, build_frame, FrameParser,
    summarise, check_calibration, linear_fraction)
from volgas_settings import data_dir, log_dir, load_settings as _load_settings, save_settings as _save_settings

log = logging.getLogger("volgas")

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
    logging.getLogger("volgas").exception("dash bridge unavailable - the dash and game telemetry are disabled")
    DASH_BRIDGE_AVAILABLE = False

try:
    import volgas_ffbeast_api
    FFBEAST_AVAILABLE = True
except Exception:
    logging.getLogger("volgas").exception("FFBeast support unavailable - the wheelbase is disabled")
    FFBEAST_AVAILABLE = False


# Bump on every GitHub Release (tag vX.Y.Z) - the in-app updater will compare
# this against the latest release's tag.
VOLGAS_VERSION = "0.3.2"

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
    try:
        os.rename(new_path, exe)
    except OSError:
        # Put the running exe back under its own name, so a failed swap
        # never leaves the user without a Volgas.exe to start.
        log.exception("update swap failed - restoring the previous Volgas.exe")
        os.rename(old, exe)
        raise
    log.info("installed update from %s", new_path)
    # Without the reset, the new exe - at the path this one was started from -
    # takes the inherited _PYI_* variables as its own and runs from this
    # process's unpack folder, which is deleted when this one exits (seen on
    # the 0.2.2 update: "Failed to remove temporary directory", then
    # FileNotFoundError for the assets).
    subprocess.Popen([exe], close_fds=True, env={**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"})



def asset_path(name):
    """Resolve a bundled asset both when run as a .py and as a PyInstaller onefile exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "assets", name)


# Startup splash, like the dash's showSplashScreen() in esp32_dash.ino: the
# logo alone (no window background, no text) for 3 seconds.
SPLASH_LOGO_SIZE = 300
DEFAULT_WINDOW_W, DEFAULT_WINDOW_H = 1180, 760
SPLASH_MS = 1500  # long enough to register, short enough not to be a wait
SPLASH_KEY_COLOR = "#ff00ff"  # made see-through via -transparentcolor; not used anywhere in the logo


def show_splash(root):
    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    splash.attributes("-topmost", True)
    if sys.platform == "win32":
        splash.configure(bg=SPLASH_KEY_COLOR)
        splash.attributes("-transparentcolor", SPLASH_KEY_COLOR)
    else:
        # -transparentcolor is Windows-only; elsewhere (development on macOS
        # or Linux) show the logo on the app's background instead.
        splash.configure(bg=T.SURFACE_WINDOW)
    x = (splash.winfo_screenwidth() - SPLASH_LOGO_SIZE) // 2
    y = (splash.winfo_screenheight() - SPLASH_LOGO_SIZE) // 2
    splash.geometry(f"{SPLASH_LOGO_SIZE}x{SPLASH_LOGO_SIZE}+{x}+{y}")
    logo = Image.open(asset_path("volgas_logo.png")).convert("RGBA").resize(
        (SPLASH_LOGO_SIZE, SPLASH_LOGO_SIZE), Image.LANCZOS)
    # -transparentcolor only hides pixels that exactly match the key, so a
    # soft alpha edge would blend into a magenta fringe - snap alpha to on/off.
    logo.putalpha(logo.getchannel("A").point(lambda a: 255 if a >= 128 else 0))
    splash.logo_photo = ImageTk.PhotoImage(logo)
    tk.Label(splash, image=splash.logo_photo, bg=splash.cget("bg"), borderwidth=0).place(
        relx=0.5, rely=0.5, anchor="center")
    splash.update()  # paint it now - VolgasApp's build runs before mainloop starts
    return splash



# The wheelbase runs FFBeast (volgas_ffbeast_api). The board is the source of
# truth: Volgas reads its settings on connect and shows them, and every change
# is written straight to the board's RAM. Only Save & Reboot stores them on
# the board; until then a restart of the board loses them.
#
# Wheelbase-tab slider range per FFBeast setting: (min, max), or ((value,
# label), ...) for fields that only take a few values. Ranges follow the field
# comments in FFBeast's wheel_api.h ("0 to 100 in %", "-1 or +1",
# AmplifierGainEnum, MotionRange in degrees) except those marked undocumented,
# where the header gives no range and these just cover this hardware with
# room to spare. A value read from the device outside its range widens that
# slider instead of being clipped (see _add_wb_slider_row). Fields deliberately
# not on the tab (user, 2026-10-23), and left at whatever the board has saved
# (set them in FFBeast's own app): EncoderCPR, CalibrationSpeed, BrakingLimit,
# EncoderDirection, ForceDirection, PolePairs, DebugTorque, SpeedBufferSize,
# DirectXConstantDirection, CalibrationMagnitude, PositionSmoothing and the
# soft-stop fields. ForceEnabled and IntegratedSpringStrength are on/off
# switches (_add_wb_switch_row).
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
WHEEL_SETTLE_MS = 150  # full-quality redraw this long after the wheel or the window stops changing
WHEEL_MIN_ANGLE_STEP = 0.1  # degrees; smaller position changes don't redraw the photo
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
# Legacy names kept for the existing drawing code; the values now come from
# the design tokens in volgas_theme (better card separation and readable
# secondary text - MAIN_GRAY was 2.9:1 against cards, now 4.7:1).
MAIN_BG = T.SURFACE_WINDOW
MAIN_PANEL = T.SURFACE_CARD
MAIN_BORDER = T.BORDER
MAIN_TEXT = T.TEXT_PRIMARY
MAIN_TEXT_DIM = T.TEXT_SECONDARY
MAIN_RED = T.BRAKE
MAIN_BLUE = T.BLUE
MAIN_GREEN = T.THROTTLE
MAIN_CYAN = T.ACCENT
MAIN_GRAY = T.TEXT_HINT
MAIN_ORANGE_STATUS = T.STATUS_WARN


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


# A Label's text sits 3 px in from its edges (2 px border + 1 px padding);
# canvas text replacing a Label is offset by this to stay where it was.
LABEL_INSET = 3


class _CanvasText:
    """Lets canvas text be updated like the Label it replaced:
    config(text=..., fg=...)."""

    def __init__(self, canvas, item):
        self.canvas, self.item = canvas, item

    def config(self, text=None, fg=None):
        options = {}
        if text is not None:
            options["text"] = text
        if fg is not None:
            options["fill"] = fg
        self.canvas.itemconfig(self.item, **options)


class _CanvasImage:
    """config(image=...) for a canvas image item, like the Label it replaced."""

    def __init__(self, canvas, item):
        self.canvas, self.item = canvas, item

    def config(self, image):
        self.canvas.itemconfig(self.item, image=image)


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
    """Pill on/off switch, 40x20 (at 100% display scale) with its top-left at
    (x, y). With no tag it owns the whole canvas (Main tab's Forces switch);
    with a tag it is drawn among other items on a card (Wheelbase tab's
    Integrated Spring)."""
    canvas.delete(tag or "all")
    kwargs = {"tags": tag} if tag else {}
    k = T.SCALE
    _draw_round_rect(canvas, x + 1 * k, y + 1 * k, x + 39 * k, y + 19 * k, 9 * k,
                     fill=MAIN_GREEN if on else MAIN_BORDER, outline="", **kwargs)
    knob_x = x + (30 if on else 10) * k
    canvas.create_oval(knob_x - 7 * k, y + 3 * k, knob_x + 7 * k, y + 17 * k, fill="#ffffff", outline="", **kwargs)


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


# ---- Dashboard tab preview: a copy of esp32_dash.ino's screen drawing ----
# Rebuilt on the PC from what the dash bridge last sent, using the same
# layout numbers, colours and 5x7 font as the firmware. Only the dash knows
# its wheel radio link and held buttons, so the Standby wheel status shows
# as "--" and the buttons line stays blank.
DASH_SIZE = 128
DASH_PREVIEW_SCALE = 3
DASH_RPM_BAR_H = 14
DASH_FOOTER_H = 46
DASH_TELEMETRY_TIMEOUT_S = 1.0  # TELEMETRY_TIMEOUT_MS: Classic screen while telemetry is this fresh
DASH_FOOTER_TIMEOUT_S = 1.5     # FOOTER_DATA_TIMEOUT_MS: lap-time footer while lap times are this fresh
DASH_PREVIEW_MS = 50            # the bridge sends at 20 Hz
DASH_WHITE, DASH_GREEN, DASH_RED, DASH_YELLOW = (255, 255, 255), (0, 255, 0), (255, 0, 0), (255, 255, 0)
DASH_UNKNOWN = (110, 110, 110)
DASH_DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DASH_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _dash_text(draw, x, y, text, size, color):
    """Adafruit GFX's classic-font print(): 6*size px per character, each
    font pixel a size x size block."""
    glyphs = volgas_dash_font.GLYPHS
    for k, ch in enumerate(text):
        code = ord(ch) - volgas_dash_font.FIRST_CHAR
        if not 0 <= code < len(glyphs) // 5:
            continue
        cx = x + k * 6 * size
        for i in range(5):
            column = glyphs[code * 5 + i]
            for j in range(8):
                if column >> j & 1:
                    draw.rectangle((cx + i * size, y + j * size,
                                    cx + (i + 1) * size - 1, y + (j + 1) * size - 1), fill=color)


def _render_dash(state):
    """The dash's 128x128 screen as an RGB image. state is
    ("classic", (speed_kph, rpm, rpm_max, gear), lap or None, delta or None)
    with lap = (lap_time_ms, valid) / delta = (delta_ms, valid), or
    ("standby", has_time, app_connected, (weekday, hour, minute, day, month),
    hub_lost)."""
    img = Image.new("RGB", (DASH_SIZE, DASH_SIZE))
    draw = ImageDraw.Draw(img)
    mid = DASH_SIZE // 2
    if state[0] == "classic":
        _, (speed, rpm, rpm_max, gear), lap, delta = state
        footer_h = DASH_FOOTER_H if lap else 0
        half = max(0, min(mid, rpm * mid // rpm_max)) if rpm_max else 0
        frac = rpm / rpm_max if rpm_max else 0
        bar_color = DASH_RED if frac > 0.92 else DASH_YELLOW if frac > 0.75 else DASH_GREEN
        if half:
            draw.rectangle((mid - half, 0, mid + half - 1, DASH_RPM_BAR_H - 1), fill=bar_color)
        col_h = DASH_SIZE - DASH_RPM_BAR_H - footer_h
        gear_char = "R" if gear < 0 else "N" if gear == 0 else chr(ord("0") + gear)
        _dash_text(draw, (mid - 6 * 7) // 2, DASH_RPM_BAR_H + (col_h - 8 * 7) // 2, gear_char, 7, DASH_WHITE)
        _dash_text(draw, mid + (mid - 3 * 6 * 3) // 2, DASH_RPM_BAR_H + (col_h - 8 * 3) // 2,
                   f"{min(speed, 999):3d}", 3, DASH_WHITE)
        if lap:
            line1_y = DASH_SIZE - DASH_FOOTER_H + 3
            lap_ms, lap_valid = lap
            ds = lap_ms // 100
            text = f"{min(99, ds // 600):02d}:{ds // 10 % 60:02d}.{ds % 10}" if lap_valid else "--:--,-"
            _dash_text(draw, (DASH_SIZE - 7 * 12) // 2, line1_y, text, 2, DASH_WHITE)
            if delta and delta[1]:
                delta_ms = delta[0]
                sign = "-" if delta_ms < 0 else "+"
                text = f"{sign}{min(99, abs(delta_ms) // 1000):02d}.{abs(delta_ms) % 1000:03d}"
                _dash_text(draw, (DASH_SIZE - 7 * 12) // 2, line1_y + 16 + 3, text, 2,
                           DASH_GREEN if delta_ms <= 0 else DASH_RED)
        return img

    # The clock (time and day/date) is centred on its own; the hub and app
    # lines are only there while there's a problem, in fixed slots below it
    # (esp32_dash.ino's standby screen).
    _, has_time, app_connected, now, hub_lost = state
    top = (DASH_SIZE - (40 if has_time else 42)) // 2
    if has_time:
        weekday, hour, minute, mday, month = now
        clock = f"{hour:02d}:{minute:02d}"
        _dash_text(draw, (DASH_SIZE - len(clock) * 18) // 2, top + 2, clock, 3, DASH_WHITE)
        day = f"{DASH_DAY_NAMES[weekday % 7]} {mday} {DASH_MONTH_NAMES[(month - 1) % 12]}"
        _dash_text(draw, (DASH_SIZE - len(day) * 6) // 2, top + 30, day, 1, DASH_WHITE)
    hub_y = top + (44 if has_time else 0)
    app_y = top + (56 if has_time else 16)
    if hub_lost:
        _dash_text(draw, (DASH_SIZE - len("hub: lost") * 6) // 2, hub_y, "hub: ", 1, DASH_WHITE)
        _dash_text(draw, (DASH_SIZE - len("hub: lost") * 6) // 2 + 5 * 6, hub_y, "lost", 1, DASH_RED)
    if not app_connected:
        text = "app: disconnected"
        _dash_text(draw, (DASH_SIZE - len(text) * 6) // 2, app_y, text, 1, DASH_RED)
    return img


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
                         padding=(px(16), px(8)), borderwidth=0)
        style.map("TNotebook.Tab",
                  background=[("selected", MAIN_BG)],
                  foreground=[("selected", MAIN_TEXT)])

        self.ser = None
        self.reader = None
        self.rx_queue = queue.Queue()
        self._pedal_stop = None   # stop event of the current pedal connection's reader
        self._cal_loaded = False  # True once the pedals' own calibration has been read this connection
        self._pedal_live = {"throttle": None, "brake": None}  # 0..1 input position, for the curve markers
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
        self.wb_status_queue = queue.Queue()
        # field name -> the IntVars of every control showing that field
        # (_add_wb_slider_row / _add_wb_switch_row), kept in step by _on_wb_settings.
        self.wb_field_vars = {}
        self.wb_connected = False
        self._wb_unsaved = False  # settings changed in the board's RAM since the last Save & Reboot

        # The last values read from (or written to) the wheelbase, kept in
        # volgas_settings.json ("wheelbase": {field: value}) so the page shows
        # something sensible while it's unplugged. Never pushed to the board:
        # on connect the board's own values replace them (see _wb_poll).
        self._wb_settings = _load_settings()
        self._wb_remembered = self._wb_settings.setdefault("wheelbase", {})

        # Layout: the title bar across the top (row 0), a navigation rail
        # down the left and the current page beside it (row 1). Pages are
        # device-centred, Home first - see _build_home_tab.
        self._show_wheelbase = bool(self._wb_settings.get("show_wheelbase", FFBEAST_AVAILABLE))
        self.root.rowconfigure(1, weight=1)
        self.root.columnconfigure(1, weight=1)
        content = tk.Frame(self.root, bg=MAIN_BG)
        content.grid(row=1, column=1, sticky="nsew")
        content.grid_rowconfigure(0, weight=1)
        content.grid_columnconfigure(0, weight=1)

        page_names = ["Home", "Pedals", "Dash"]
        if self._show_wheelbase:
            # Settings -> Support can hide it ("show_wheelbase" in the
            # settings file).
            page_names.append("Wheelbase")
        page_names.append("Settings")
        self._tab_frames = {name: tk.Frame(content, bg=MAIN_BG) for name in page_names}
        for frame in self._tab_frames.values():
            frame.grid(row=0, column=0, sticky="nsew")

        self._wb_fw = None             # the wheelbase's firmware version, from its state reports
        self._dev_manifest = None      # firmware list of the latest release; None = not checked
        self._dev_checked = set()      # devices whose check result is shown
        self._dev_error = None         # why the last device check failed
        self._dev_busy = False         # a device check is running
        self._dev_update_busy = False  # a wheel firmware update is running
        self._dev_text = {}            # device -> status text of the running/last update
        self._dev_queue = queue.Queue()
        self._update_queue = queue.Queue()
        self._update_busy = False
        self._dash_bridge = DashBridge() if DASH_BRIDGE_AVAILABLE else None
        self._build_home_tab(self._tab_frames["Home"])
        self._build_pedals_tab(self._tab_frames["Pedals"])
        if self._show_wheelbase:
            self._build_wheelbase_tab(self._tab_frames["Wheelbase"])
        self._build_dashboard_tab(self._tab_frames["Dash"])
        self._build_settings_tab(self._tab_frames["Settings"])
        self._caption_hwnd = _hook_caption(self.root)
        self._build_window_chrome()
        self._build_nav_rail()
        self.toaster = ui.Toaster(self.root)

        # Floor the window at the size its content actually asks for. The
        # native resize border is kept (see _hook_caption), so Windows
        # enforces this during an interactive resize like any other app.
        self.root.update_idletasks()
        req_w, req_h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.minsize(req_w, req_h)
        # Measured with every page still gridded, so the floor fits the
        # biggest page. The explicit geometry stops the window following its
        # requested size once only one page is gridded, so switching pages
        # doesn't resize it.
        # Open roomier than the minimum when the screen allows it.
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        start_w = max(req_w, min(DEFAULT_WINDOW_W, int(screen_w * 0.9)))
        start_h = max(req_h, min(DEFAULT_WINDOW_H, int(screen_h * 0.85)))
        self.root.geometry(f"{start_w}x{start_h}")
        self._select_tab("Home")
        _apply_dark_titlebar(self.root, T.SURFACE_RAIL, MAIN_BORDER, MAIN_TEXT)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._closing = False

        self._poll()
        self._auto_scan()
        self._bridge_stop = threading.Event()
        self._bridge_thread = None
        if DASH_BRIDGE_AVAILABLE:
            # The bridge runs in the background for the app's whole lifetime,
            # finding the dash, picking up rFactor2 or Forza and reconnecting
            # by itself (see volgas_dash_bridge). _on_close stops it.
            self._bridge_thread = threading.Thread(target=self._dash_bridge.run, args=(self._bridge_stop,),
                                                   name="dash-bridge", daemon=True)
            self._bridge_thread.start()
        self._wb_stop = threading.Event()
        self._wb_thread = None
        if FFBEAST_AVAILABLE:
            # Owns the wheelbase's HID handle for the app's whole lifetime;
            # _on_close stops it.
            self._wb_thread = threading.Thread(target=self._wb_worker_loop, name="wheelbase", daemon=True)
            self._wb_thread.start()
        self._wb_poll()
        self._status_poll()
        self._remove_old_exe()
        if self._wb_settings.get("check_updates_on_start", True):
            self._start_update_check()
            self._check_updates()
        self._updates_poll()

    # ---------- window chrome (dark tab strip as the title bar) ----------

    def _build_window_chrome(self):
        """The title bar: the Volgas mark, the current page's name, and live
        status pills for the pedals, the dash and the game - so the state of
        the rig is visible from every page. With the native caption removed
        (see _hook_caption) it is also the window's title bar: its empty
        space drags the window (handing off to Windows' own move loop, so
        Snap and drag-to-restore work), double-click maximizes, its top few
        pixels resize, and it carries the min/max/close buttons. If the hook
        isn't in place the native title bar stays above it instead."""
        bar = tk.Frame(self.root, bg=T.SURFACE_RAIL, height=TITLE_BAR_H)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        bar.pack_propagate(False)

        logo_size = px(24)
        logo_im = Image.open(asset_path("volgas_logo.png")).convert("RGBA").resize((logo_size, logo_size), Image.LANCZOS)
        self._logo_photo = ImageTk.PhotoImage(logo_im)
        logo = tk.Label(bar, image=self._logo_photo, bg=T.SURFACE_RAIL)
        # Centred over the navigation rail below it.
        logo.pack(side="left", padx=((T.NAV_RAIL_W - logo_size) // 2, T.SPACE_3))
        self._page_title = tk.Label(bar, text="", font=T.FONT_LABEL, fg=T.TEXT_PRIMARY, bg=T.SURFACE_RAIL)
        self._page_title.pack(side="left")

        # Shown when an update check finds a newer release.
        self._update_badge = ui.StatusPill(bar, "warn", "Update available", bg=T.SURFACE_RAIL)
        self._update_badge.config(cursor="hand2")
        self._update_badge.bind("<Button-1>", lambda _e: self._select_tab("Settings"))

        if self._caption_hwnd:
            self._build_caption_buttons(bar)
        pills = tk.Frame(bar, bg=T.SURFACE_RAIL)
        pills.pack(side="right", padx=(0, T.SPACE_3))
        self._game_pill = ui.StatusPill(pills, "idle", "No game", bg=T.SURFACE_RAIL)
        # Device pills (dash, hub, pedals, wheelbase) are only on screen while
        # that device is NOT connected - a connected rig has nothing to
        # report (see _apply_title_pills).
        self._title_pills = {key: ui.StatusPill(pills, "idle", devs.DEVICE_NAMES[key], bg=T.SURFACE_RAIL)
                             for key in ("dash", "hub", "pedals")}
        if FFBEAST_AVAILABLE:
            self._title_pills["wheelbase"] = ui.StatusPill(pills, "idle", "Wheelbase", bg=T.SURFACE_RAIL)
        self._title_pills_shown = None
        self._game_pill.pack(side="right", padx=(T.SPACE_2, 0))

        if not self._caption_hwnd:
            return
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

    def _apply_title_pills(self, shown):
        """Show exactly the device pills named in `shown`, in a fixed order
        (right to left: dash, hub, pedals, wheelbase, after the game pill)."""
        shown = tuple(k for k in ("dash", "hub", "pedals", "wheelbase") if k in shown and k in self._title_pills)
        if shown == self._title_pills_shown:
            return
        self._title_pills_shown = shown
        for pill in self._title_pills.values():
            pill.pack_forget()
        for key in shown:
            self._title_pills[key].pack(side="right", padx=(T.SPACE_2, 0))

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
            btn = tk.Canvas(bar, width=CAPTION_BUTTON_W, height=TITLE_BAR_H, bg=T.SURFACE_RAIL, highlightthickness=0)
            btn.create_text(CAPTION_BUTTON_W // 2, TITLE_BAR_H // 2, text=CAPTION_GLYPHS[key],
                            font=glyph_font, fill=MAIN_TEXT, tags="glyph")
            btn.pack(side="right", fill="y")
            btn.bind("<Enter>", lambda _e, b=btn, bg=hover_bg: b.config(bg=bg))
            btn.bind("<Leave>", lambda _e, b=btn: b.config(bg=T.SURFACE_RAIL))
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

    PAGE_TITLES = {"Home": "Home", "Pedals": "Pedals", "Dash": "Dash", "Wheelbase": "Wheelbase",

                   "Settings": "Settings"}

    def _build_nav_rail(self):
        """Page navigation down the left edge, Settings pinned at the bottom
        where people expect it (it used to be reachable only by clicking the
        logo)."""
        self._nav = ui.NavRail(self.root, on_select=self._select_tab)
        self._nav.grid(row=1, column=0, sticky="ns")
        self._nav.add("Home", "Home", "home")
        self._nav.add("Pedals", "Pedals", "pedals")
        self._nav.add("Dash", "Dash", "dash")
        if self._show_wheelbase:
            self._nav.add("Wheelbase", "Base", "base")
        self._nav.add("Settings", "Settings", "settings", bottom=True)

    def _select_tab(self, name):
        if name not in self._tab_frames:
            return
        self._current_page = name
        if hasattr(self, "_nav"):
            self._nav.select(name)
        if hasattr(self, "_page_title"):
            self._page_title.config(text=self.PAGE_TITLES.get(name, name))
        # Hidden pages are taken out of the grid, not just stacked underneath
        # with tkraise(): stacked ones still got laid out and redrawn on
        # every resize event, which made dragging the window edge lag.
        for n, frame in self._tab_frames.items():
            if n == name:
                frame.grid()
            else:
                frame.grid_remove()
        # A wheel card on the page just shown skipped redraws while hidden -
        # catch it up once grid has mapped it (idle, after the layout pass).
        self.root.after_idle(self._refresh_wheel_cards)
        if name == "Wheelbase" and self.wb_connected:
            # Refresh its fields from the device every time it's opened (FFBeast's
            # own app may have changed them).
            self.wb_cmd_queue.put(("read_settings",))

    def _show_update_badge(self, show):
        if show:
            self._update_badge.pack(side="left", padx=(T.SPACE_3, 0))
        else:
            self._update_badge.pack_forget()

    def _notify(self, text, kind="info"):
        """Non-blocking message in the window corner (replaces pop-ups for
        anything that doesn't need a decision)."""
        log.info("notice (%s): %s", kind, text)
        if hasattr(self, "toaster"):
            self.toaster.show(text, kind)

    # ---------- UI ----------

    def _build_home_tab(self, parent):
        """Home: the whole rig at a glance, one card per device. The wheel
        fills the left side with its photo; the pedals and the dash share the
        right. Every card shows its device's real connection state."""
        root = tk.Frame(parent, bg=MAIN_BG)
        root.pack(fill="both", expand=True, padx=T.SPACE_5, pady=(T.SPACE_4, T.SPACE_5))
        root.grid_columnconfigure(0, weight=23, uniform="home")
        root.grid_columnconfigure(1, weight=20, uniform="home")
        root.grid_rowconfigure(0, weight=1, uniform="home-rows")
        root.grid_rowconfigure(1, weight=1, uniform="home-rows")

        # Wheelbase values the optional Wheelbase page shares (no controls on
        # Home while the wheelbase isn't available).
        self.main_forces_enabled_var = tk.BooleanVar(value=bool(self._wb_remembered.get("ForceEnabled", 0)))
        self.main_force_strength_var = tk.DoubleVar(value=self._wb_remembered.get("PowerLimit", 15))
        self.main_motion_range_var = tk.DoubleVar(value=self._wb_remembered.get("MotionRange", 360))

        wheel_card = self._build_wheel_card(root, width=470, height=470, wheelbase_controls=False)  # 100% sizes
        wheel_card.grid(row=0, column=0, rowspan=2, sticky="nsew", padx=(0, T.SPACE_3))

        pedals_card = self._build_home_pedals_card(root)
        pedals_card.grid(row=0, column=1, sticky="nsew", pady=(0, T.SPACE_2))

        dash_card = self._build_home_dash_card(root)
        dash_card.grid(row=1, column=1, sticky="nsew", pady=(T.SPACE_2, 0))

    def _make_frame_card(self, parent, width, height):
        """A rounded card holding an ordinary Frame, for content laid out with
        regular widgets. width/height are 100%-display sizes. Returns
        (card, inner_frame)."""
        card = self._make_flat_card(parent, px(width), px(height))
        inner = tk.Frame(card, bg=MAIN_PANEL)
        win = card.create_window(T.SPACE_4, T.SPACE_4, window=inner, anchor="nw")
        card.bind("<Configure>", lambda e: card.itemconfig(
            win, width=max(1, e.width - 2 * T.SPACE_4), height=max(1, e.height - 2 * T.SPACE_4)), add="+")
        return card, inner

    def _home_card(self, parent, title):
        """A Home card drawn on one canvas (no frames of packed widgets inside
        it: those re-laid-out on every resize event and made resizing the
        window lag). Title on the left, status pill on the right, and a
        button frame bottom right. Returns (card, pill, footer, top of the
        body, layout list); each layout entry is called as fn(width, height)
        on every resize."""
        card = self._make_flat_card(parent, px(420), px(262))
        pill = ui.StatusPill(card, "idle", "Checking...", bg=MAIN_PANEL)
        header_h = max(_label_height(T.FONT_TITLE), int(pill.cget("height")))
        card.create_text(T.SPACE_4 + LABEL_INSET, T.SPACE_4 + header_h / 2, text=title, font=T.FONT_TITLE,
                         fill=T.TEXT_PRIMARY, anchor="w")
        pill_id = card.create_window(0, T.SPACE_4 + header_h / 2, window=pill, anchor="e")
        pill._card, pill._item = card, pill_id   # for _show_card_pill
        footer = tk.Frame(card, bg=MAIN_PANEL)
        footer_id = card.create_window(0, 0, window=footer, anchor="e")
        layout = []

        def on_resize(event):
            w, h = event.width, event.height
            if w <= 1 or h <= 1:
                return
            card.coords(pill_id, w - T.SPACE_4, T.SPACE_4 + header_h / 2)
            foot_h = max(_label_height(T.FONT_CAPTION), footer.winfo_reqheight())
            card.coords(footer_id, w - T.SPACE_4, h - T.SPACE_4 - foot_h / 2)
            for fn in layout:
                fn(w, h)

        card.bind("<Configure>", on_resize, add="+")
        return card, pill, footer, T.SPACE_4 + header_h + T.SPACE_3, layout

    def _build_home_pedals_card(self, parent):
        card, self._home_pedal_pill, footer, body_top, layout = self._home_card(parent, "Pedals")
        caption_h, value_h = _label_height(T.FONT_CAPTION), _label_height(T.FONT_DISPLAY)
        bar_h = px(10)
        bar_top = body_top + caption_h + value_h + T.SPACE_1

        def gauge(column, name, color):
            caption_id = card.create_text(0, body_top + caption_h / 2, text=name, font=T.FONT_CAPTION,
                                          fill=T.TEXT_SECONDARY, anchor="w")
            value_id = card.create_text(0, body_top + caption_h + value_h / 2, text="--", font=T.FONT_DISPLAY,
                                        fill=T.TEXT_PRIMARY, anchor="w")
            track = ui.draw_round_rect(card, 0, 0, bar_h, bar_h, bar_h / 2, fill=T.SURFACE_RAISED, outline="")
            fill = ui.draw_round_rect(card, 0, 0, 0, bar_h, bar_h / 2, fill=color, outline="")
            state = {"raw": None, "shown": None, "x0": 0, "x1": 0}

            def redraw():
                x0, x1 = state["x0"], state["x1"]
                if x1 - x0 <= bar_h:
                    return
                card.coords(track, *ui.round_rect_points(x0, bar_top, x1, bar_top + bar_h, bar_h / 2))
                frac = 0.0 if state["raw"] is None else max(0.0, min(1.0, state["raw"] / AXIS_MAX))
                if frac <= 0.01:
                    card.itemconfig(fill, state="hidden")
                else:
                    card.itemconfig(fill, state="normal")
                    card.coords(fill, *ui.round_rect_points(x0, bar_top, x0 + max(bar_h, (x1 - x0) * frac),
                                                            bar_top + bar_h, bar_h / 2))

            def place(w, _h):
                # Two equal columns across the card's padded width, with
                # 2 x SPACE_3 between them.
                half = (w - 2 * T.SPACE_4) / 2
                x0 = T.SPACE_4 + (0 if column == 0 else half + T.SPACE_3)
                state["x0"], state["x1"] = x0, x0 + half - T.SPACE_3
                card.coords(caption_id, x0 + LABEL_INSET, body_top + caption_h / 2)
                card.coords(value_id, x0 + LABEL_INSET, body_top + caption_h + value_h / 2)
                redraw()

            def update(raw):
                state["raw"] = raw
                text = "--" if raw is None else f"{round(max(0.0, min(1.0, raw / AXIS_MAX)) * 100)}%"
                if text != state["shown"]:
                    state["shown"] = text
                    card.itemconfig(value_id, text=text, fill=T.TEXT_PRIMARY if raw is not None else T.TEXT_HINT)
                if card.winfo_viewable():
                    redraw()

            layout.append(place)
            return update

        self.main_throttle_update = gauge(0, "Throttle", MAIN_GREEN)
        self.main_brake_update = gauge(1, "Brake", MAIN_RED)
        ui.FlatButton(footer, "Open pedals", command=lambda: self._select_tab("Pedals"),
                      font=T.FONT_CAPTION).pack(side="right")
        self.main_throttle_update(None)
        self.main_brake_update(None)
        return card

    def _build_home_dash_card(self, parent):
        card, self._home_dash_pill, footer, body_top, _layout = self._home_card(parent, "Dash")
        size = px(DASH_SIZE)
        card.create_rectangle(T.SPACE_4, body_top, T.SPACE_4 + size + 1, body_top + size + 1, outline=MAIN_BORDER,
                              fill=MAIN_BG)
        image_id = card.create_image(T.SPACE_4 + 1, body_top + 1, anchor="nw")
        self._home_dash_image = _CanvasImage(card, image_id)
        self._home_dash_shown = None

        x = T.SPACE_4 + size + 2 + T.SPACE_4 + LABEL_INSET
        caption_h = _label_height(T.FONT_CAPTION)
        y = body_top
        card.create_text(x, y + caption_h / 2, text="Game", font=T.FONT_CAPTION, fill=T.TEXT_SECONDARY, anchor="w")
        y += caption_h
        label_h = _label_height(T.FONT_LABEL)
        self._home_game_label = _CanvasText(card, card.create_text(
            x, y + label_h / 2, text="--", font=T.FONT_LABEL, fill=T.TEXT_PRIMARY, anchor="w"))
        y += label_h + T.SPACE_2
        card.create_text(x, y + caption_h / 2, text="Showing", font=T.FONT_CAPTION, fill=T.TEXT_SECONDARY,
                         anchor="w")
        y += caption_h
        self._home_dash_detail = _CanvasText(card, card.create_text(
            x, y + LABEL_INSET, text="--", font=T.FONT_BODY, fill=T.TEXT_PRIMARY, anchor="nw", width=px(210)))
        ui.FlatButton(footer, "Open dash", command=lambda: self._select_tab("Dash"),
                      font=T.FONT_CAPTION).pack(side="right")
        return card

    def _make_flat_card(self, parent, width, height, radius=None, outline=MAIN_BORDER):
        """A rounded-corner dark card (real tk.Frame has square corners
        only), drawn as a polygon on a Canvas. `width`/`height` are just the
        initial/minimum size - cards are gridded/packed to stretch, and the
        background reshapes via `coords` on every resize. The caller draws
        the contents as items on the card canvas itself (or embeds the odd
        real widget with create_window) and adds its own <Configure> handler
        (add="+") to reposition them. Not a Frame of packed widgets inside
        the canvas: every extra nested window re-lays-out and re-paints on
        each resize event, which made drag-resizing the window stutter.
        `outline=""` for a border-free card (used for the wheel card).
        width/height are real (already scaled) pixels."""
        if radius is None:
            radius = T.RADIUS_CARD  # read here, not as a default: it's scaled at start-up
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
            _draw_round_rect(card, left, top, right, bottom, r, fill=T.SURFACE_WELL, outline=MAIN_BORDER, width=1,
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
            _draw_round_rect(card, x0 + pad, y0, x1 - pad, y1, r, fill=T.SURFACE_WELL, outline=MAIN_BORDER, width=1,
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

    def _build_wheel_card(self, parent, width=600, height=480, wheelbase_controls=True):
        """A wheel card - the card IS the wheel canvas: the rounded
        background, the photo, the angle readout and Set Centre are all on
        one canvas. (Separate widgets for the readout once showed a faint
        rectangular seam at the widget edge, and nesting a canvas inside the
        card made every resize repaint the card area twice.) The Main and
        Wheelbase tabs each build one; they share the keyed photo and all
        follow the wheelbase position (_update_main_wheel). width/height are
        100%-display sizes. Returns the card."""
        width, height = px(width), px(height)
        if not hasattr(self, "_wheel_cards"):
            self._wheel_cards = []  # one draw(angle) per card
            self._last_wheel_angle = 0.0
            r16, g16, b16 = parent.winfo_rgb(MAIN_PANEL)
            self._wheel_fill_rgb = (r16 // 256, g16 // 256, b16 // 256)
            self._load_wheel_image()

        canvas = self._make_flat_card(parent, width, height, outline="" if wheelbase_controls else MAIN_BORDER)
        state = {"size": None, "base": None, "base_fast": False, "photo": None, "drawn": None, "settle": None}
        # Without wheelbase controls (Home) the card has a title row, then the
        # steering range and power rows and a row of range presets and Set
        # Centre (_add_home_wheel_rows), and the angle readout sits under the photo.
        title_h = px(44)
        header_h = 0 if wheelbase_controls else title_h + 3 * WB_ROW_H
        footer_h = 0 if wheelbase_controls else px(52)

        def resize_photo(size, fast):
            size = max(px(40), int(size))
            if size != state["size"] or (state["base_fast"] and not fast):
                state["size"], state["base_fast"] = size, fast
                state["base"] = self._wheel_flat_full.resize((size, size), Image.BILINEAR if fast else Image.LANCZOS)

        def draw(angle_deg, fast=False):
            """fast: a cheaper rotation while the wheel moves or the window
            is being resized (a full-quality rotation of a large photo took
            20-35 ms); a full-quality frame follows once things settle."""
            key = (round(angle_deg, 2), state["size"], fast or state["base_fast"])
            if key == state["drawn"]:
                return
            state["drawn"] = key
            # Always rotate from the pristine base image, never from the
            # previous rotated frame - repeatedly re-rotating an already-rotated
            # image would compound resampling blur across updates.
            # Sign: PIL rotates counter-clockwise for positive angles; negated
            # here so a positive angle_deg (matching the FFBeast Position/turning
            # right) turns the picture clockwise. Flip the sign if that turns
            # out backwards against the real wheel.
            rotated = state["base"].rotate(-angle_deg, resample=Image.BILINEAR if fast else Image.BICUBIC,
                                           expand=False, fillcolor=self._wheel_fill_rgb)
            photo = state["photo"]
            if photo is not None and (photo.width(), photo.height()) == rotated.size:
                photo.paste(rotated)  # updates the shown image in place
            else:
                state["photo"] = ImageTk.PhotoImage(rotated)  # keep a reference - Tk drops GC'd images
                canvas.itemconfig(image_id, image=state["photo"])
            canvas.itemconfig(text_id, text=f"{angle_deg:+.0f}°")

        def settle():
            state["settle"] = None
            resize_photo(state["size"], fast=False)
            draw(self._last_wheel_angle)

        def apply_resize():
            w, h = canvas.winfo_width(), canvas.winfo_height()
            # Same size as when the photo sat in a canvas inset CARD_PAD inside
            # the card (minus a 4 px margin), except horizontally it never comes
            # within the corner radius of the card's sides: the photo is an
            # opaque square, and its card-coloured corners mustn't cover the
            # card's rounded corners.
            resize_photo(min(w - px(40), h - 2 * CARD_PAD - px(4) - header_h - footer_h), fast=True)
            draw(self._last_wheel_angle, fast=True)
            if state["settle"] is not None:
                canvas.after_cancel(state["settle"])
            state["settle"] = canvas.after(WHEEL_SETTLE_MS, settle)

        # Resampling the photo costs several ms and a drag-resize sends dozens
        # of resize events a second, so it's rate-limited to
        # WHEEL_RESIZE_THROTTLE_MS, at whatever size the canvas has by then,
        # with cheap resampling until the resize stops (settle).
        resize_soon = _rate_limited(canvas, apply_resize, WHEEL_RESIZE_THROTTLE_MS)

        def on_resize(event):
            w, h = event.width, event.height
            if w <= 1 or h <= 1:
                return
            # Nudged down from dead-center so the photo's own top edge doesn't
            # sit under the angle text above it.
            if wheelbase_controls:
                canvas.coords(image_id, w / 2, h / 2 + WHEEL_VERTICAL_OFFSET)
                canvas.coords(text_id, w / 2, CARD_PAD + px(20))
                canvas.coords(centre_id, w / 2, WHEEL_CENTRE_BUTTON_Y)
            else:
                canvas.coords(image_id, w / 2, (header_h + h - footer_h) / 2)
                canvas.coords(pill_id, w - T.SPACE_4, T.SPACE_4 + px(12))
                canvas.coords(text_id, w / 2, h - footer_h / 2)
            resize_soon()

        resize_photo(MAIN_WHEEL_CANVAS_SIZE, fast=False)
        state["photo"] = ImageTk.PhotoImage(state["base"])
        image_id = canvas.create_image(0, 0, image=state["photo"])
        if not wheelbase_controls:
            canvas.create_text(T.SPACE_4, T.SPACE_4 + px(12), text="Steering wheel", font=T.FONT_TITLE,
                               fill=T.TEXT_PRIMARY, anchor="w")
            # The wheel's radio link to the hub; only on screen while it's down.
            pill = ui.StatusPill(canvas, "idle", "Not linked", bg=MAIN_PANEL)
            pill_id = canvas.create_window(0, 0, window=pill, anchor="e")
            pill._card, pill._item = canvas, pill_id
            self._home_wheel_pill = pill
            # The steering angle, under the photo.
            text_id = canvas.create_text(0, 0, text="+0\u00b0", fill=MAIN_TEXT, font=("Segoe UI", 16, "bold"))
            self._add_home_wheel_rows(canvas, title_h)
            canvas.bind("<Configure>", on_resize, add="+")
            self._wheel_cards.append((canvas, draw))
            return canvas
        text_id = canvas.create_text(0, CARD_PAD + px(20), text="0°", fill=MAIN_TEXT, font=("Segoe UI", 16, "bold"))
        # Set Centre sits right under the angle readout it zeroes. Card
        # background colour (MAIN_BORDER on MAIN_PANEL) so it stays visible on
        # the panel, unlike the top bar's MAIN_PANEL-on-MAIN_BG it used to be.
        centre_btn = tk.Button(canvas, text="Set Centre", font=("Segoe UI", 9), fg=MAIN_TEXT,
                               bg=MAIN_BORDER, activebackground=MAIN_CYAN, activeforeground=MAIN_BG,
                               relief="flat", padx=px(12), pady=px(4), bd=0, cursor="hand2",
                               command=lambda: self._wb_send_command(("reset_center",)))
        centre_id = canvas.create_window(0, WHEEL_CENTRE_BUTTON_Y, window=centre_btn, anchor="n")
        canvas.bind("<Configure>", on_resize, add="+")
        self._wheel_cards.append((canvas, draw))
        return canvas

    def _add_home_wheel_rows(self, card, top):
        """Home's wheel controls under the card title: steering range and
        power (the wheelbase's MotionRange and PowerLimit, the same rows as
        on the Wheelbase page and kept in step with them), then 360/540/900
        presets under the range bar and Set Centre on the right."""
        rows = (("MotionRange", "STEERING RANGE"), ("PowerLimit", "POWER"))
        name_w = max(tkfont.Font(font=WB_NAME_FONT).measure(label) for _n, label in rows) + px(24)
        for i, (name, label) in enumerate(rows):
            self._add_wb_slider_row(card, top + i * WB_ROW_H, name, name_w, label=label)
        presets = tk.Frame(card, bg=MAIN_PANEL)
        for deg in (360, 540, 900):
            ui.FlatButton(presets, f"{deg}°", font=T.FONT_CAPTION,
                          command=lambda d=deg: self._set_motion_range_preset(d)).pack(side="left", padx=(0, T.SPACE_2))
        row_mid = top + 2 * WB_ROW_H + WB_ROW_H / 2
        card.create_window(CARD_PAD + name_w, row_mid, window=presets, anchor="w")
        # Set Centre on the same row, lined up with the right end of the value column.
        centre = ui.FlatButton(card, "Set Centre", font=T.FONT_CAPTION,
                               command=lambda: self._wb_send_command(("reset_center",)))
        centre_id = card.create_window(0, row_mid, window=centre, anchor="e")
        card.bind("<Configure>", lambda e: card.coords(centre_id, e.width - CARD_PAD - px(4), row_mid), add="+")
        card.bind("<Enter>", lambda _e: card.focus_set(), add="+")  # mouse wheel / arrow keys on the rows

    def _redraw_forces_toggle(self):
        if hasattr(self, "main_forces_toggle"):
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
        """Remember a wheelbase setting for display while it's unplugged (see
        __init__'s _wb_remembered comment)."""
        self._wb_remembered[name] = value
        _save_settings(self._wb_settings)

    def _set_motion_range_preset(self, deg):
        if not self._wb_require_connected():
            return
        self._remember_wb("MotionRange", deg)
        self._on_wb_settings({"MotionRange": deg})
        self._wb_write("MotionRange", deg)
        self._wb_verify("MotionRange", deg)

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
        """New wheelbase position (the board streams ~500 a second, the same
        value while the wheel is still). Rotating a large photo is expensive,
        so the wheel is only redrawn when the angle changes, with the cheap
        rotation while it moves, at most often enough to use about half the
        interface's time, and once at full quality after it stops."""
        angle = (position / 10000.0) * (self.main_motion_range_var.get() / 2.0)
        if abs(angle - self._last_wheel_angle) < WHEEL_MIN_ANGLE_STEP:
            return
        self._last_wheel_angle = angle
        if getattr(self, "_wheel_draw_timer", None) is None:
            delay = max(0.0, getattr(self, "_wheel_next_draw", 0.0) - time.monotonic())
            self._wheel_draw_timer = self.root.after(int(delay * 1000), self._draw_moving_wheel)

    def _draw_moving_wheel(self):
        self._wheel_draw_timer = None
        start = time.monotonic()
        for canvas, draw in getattr(self, "_wheel_cards", ()):
            if canvas.winfo_viewable():
                draw(self._last_wheel_angle, fast=True)
        cost = time.monotonic() - start
        self._wheel_next_draw = start + max(1 / 60, 2 * cost)
        if getattr(self, "_wheel_settle_timer", None) is not None:
            self.root.after_cancel(self._wheel_settle_timer)
        self._wheel_settle_timer = self.root.after(WHEEL_SETTLE_MS, self._settle_wheel)

    def _settle_wheel(self):
        self._wheel_settle_timer = None
        self._refresh_wheel_cards()

    def _refresh_wheel_cards(self):
        """Draw the wheel at the latest angle, at full quality, on every card
        that's on screen. A card on a hidden tab is skipped and catches up
        when its tab is shown (_select_tab)."""
        for canvas, draw in getattr(self, "_wheel_cards", ()):
            if canvas.winfo_viewable():
                draw(self._last_wheel_angle)

    @staticmethod
    def _show_card_pill(pill, visible):
        pill._card.itemconfig(pill._item, state="normal" if visible else "hidden")

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
        bars_card = self._make_flat_card(root, px(900), px(56))
        bars_card.pack(fill="x", padx=px(12), pady=(T.SPACE_3, px(10)))
        self.pedal_throttle_update = self._add_card_mini_bar(bars_card, 0, "THROTTLE", MAIN_GREEN)
        self.pedal_brake_update = self._add_card_mini_bar(bars_card, 1, "BRAKE", MAIN_RED)

        # Per-pedal response curve editors, side by side. Tighter outer/inter-card
        # padding than the rest of the tab so the plots themselves get the space.
        curves_row = tk.Frame(root, bg=MAIN_BG)
        curves_row.pack(fill="both", expand=True, padx=px(12), pady=(0, px(8)))
        curves_row.columnconfigure(0, weight=1)
        curves_row.columnconfigure(1, weight=1)
        curves_row.rowconfigure(0, weight=1)
        throttle_card = self._build_pedal_curve_card(curves_row, "throttle", "THROTTLE CURVE", MAIN_GREEN)
        throttle_card.grid(row=0, column=0, sticky="nsew", padx=(0, px(4)))
        brake_card = self._build_pedal_curve_card(curves_row, "brake", "BRAKE CURVE", MAIN_RED)
        brake_card.grid(row=0, column=1, sticky="nsew", padx=(px(4), 0))

        # Actions - just a status line now; every calibration/curve change
        # applies AND saves to flash immediately (see _send_calibration),
        # so there's no separate "Save to flash" step left to expose, and
        # the raw hall/load numbers were only ever useful for eyeballing
        # calibration by hand, which the wizard replaced.
        act = tk.Frame(root, bg=MAIN_BG)
        act.pack(fill="x", padx=px(12), pady=(0, px(12)))
        self.status_label = tk.Label(act, text="Waiting for the pedals", font=T.FONT_CAPTION, fg=MAIN_TEXT_DIM,
                                     bg=MAIN_BG)
        self.status_label.pack(side="left", padx=px(8))

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
        height = px(18)
        pad = px(2)
        r = (height - 2 * pad) / 2
        state = {"raw": 0}

        def redraw(*_args):
            w = card.winfo_width()
            if w <= 1:
                return
            col_gap = px(32)
            col_w = (w - 2 * CARD_PAD - col_gap) / 2
            row_x, row_y = CARD_PAD + column * (col_w + col_gap) + px(4), CARD_PAD + px(4)
            row_w = col_w - px(8)
            card.coords(name_id, row_x + px(3), row_y + px(1) + name_h / 2)
            card.coords(value_id, row_x + row_w - px(3), row_y + value_h / 2)
            card.delete(tag)
            bx0, by0 = row_x + name_w + px(8), row_y + px(2)
            bx1, by1 = row_x + row_w - value_w - px(8), by0 + height
            if bx1 - bx0 <= 2 * pad:
                return
            _draw_round_rect(card, bx0 + pad, by0 + pad, bx1 - pad, by1 - pad, r, fill=T.SURFACE_WELL, outline=MAIN_BORDER,
                              width=1, tags=tag)
            frac = max(0.0, min(1.0, state["raw"] / 1023.0))
            fill_x = bx0 + pad + frac * (bx1 - bx0 - 2 * pad)
            if fill_x > bx0 + pad + r:
                halo_pad = px(2)
                _draw_round_rect(card, bx0 + pad - halo_pad, by0 + pad - halo_pad, fill_x + halo_pad,
                                  by1 - pad + halo_pad, r, fill=halo_color, outline="", tags=tag)
                _draw_round_rect(card, bx0 + pad, by0 + pad, fill_x, by1 - pad, r, fill=color, outline="", tags=tag)
                cap_w = px(3)
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
        card = self._make_flat_card(parent, px(420), px(340))
        title_font = ("Segoe UI", 11, "bold")
        card.create_text(CARD_PAD + px(3), CARD_PAD + px(2) + _label_height(title_font) / 2, text=title,
                         font=title_font, fill=MAIN_TEXT, anchor="w")
        calibrate = ui.FlatButton(card, "Calibrate", variant="primary", font=T.FONT_CAPTION, padx=12, pady=3,
                                  command=lambda k=pedal_key: self._open_calibrate_wizard(k))
        calibrate_id = card.create_window(0, CARD_PAD + px(2), window=calibrate, anchor="ne")

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
        canvas = tk.Canvas(card, height=px(220), bg=T.SURFACE_WELL, highlightthickness=1,
                           highlightbackground=MAIN_BORDER, cursor="hand2")
        plot_top = CARD_PAD + px(34)
        plot_id = card.create_window(CARD_PAD + px(2), plot_top, window=canvas, anchor="nw")
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
            ui.FlatButton(preset_row, label, font=T.FONT_CAPTION, padx=10, pady=4,
                          command=lambda k=pedal_key, v=values: self._apply_curve_preset(k, v)).pack(side="left",
                                                                                                    padx=px(3))
        preset_id = card.create_window(0, 0, window=preset_row, anchor="n")

        def layout(event):
            w, h = event.width, event.height
            preset_h = preset_row.winfo_reqheight()  # only known once Tk has laid the buttons out
            card.coords(calibrate_id, w - CARD_PAD, CARD_PAD + px(2))
            # 8 px under the plot, then 2 px above the preset row, as packed before
            card.itemconfig(plot_id, width=max(10, w - 2 * CARD_PAD - px(4)),
                            height=max(10, h - CARD_PAD - preset_h - px(10) - plot_top))
            card.coords(preset_id, w / 2, h - CARD_PAD - preset_h)

        card.bind("<Configure>", layout, add="+")
        self._redraw_pedal_curve(pedal_key)
        return card

    # Plot margins: room for the point labels above the top row and for the
    # input-axis labels under the bottom row, so nothing is clipped. Scaled
    # with the display at start-up (_scale_layout_constants).
    CURVE_PAD_X = 20
    CURVE_PAD_TOP = 22
    CURVE_PAD_BOTTOM = 26

    def _curve_plot_geometry(self, canvas, n):
        """Shared margin/point-position math between drawing and hit-testing,
        so a drag always grabs exactly the point it looks like it's over.
        Returns (w, h, (left, top, right, bottom), xs)."""
        w, h = canvas.winfo_width(), canvas.winfo_height()
        box = (self.CURVE_PAD_X, self.CURVE_PAD_TOP, w - self.CURVE_PAD_X, h - self.CURVE_PAD_BOTTOM)
        xs = [box[0] + (box[2] - box[0]) * (i / (n - 1)) for i in range(n)] if w > 1 else None
        return w, h, box, xs

    def _curve_widgets(self, pedal_key):
        if pedal_key == "throttle":
            return self.throttle_curve_canvas, self.throttle_curve_vars, MAIN_GREEN
        return self.brake_curve_canvas, self.brake_curve_vars, MAIN_RED

    def _curve_mouse_down(self, event, pedal_key):
        canvas, curve_vars, _color = self._curve_widgets(pedal_key)
        _w, _h, _box, xs = self._curve_plot_geometry(canvas, len(curve_vars))
        if not xs:
            return
        nearest_i = min(range(len(xs)), key=lambda i: abs(xs[i] - event.x))
        if abs(xs[nearest_i] - event.x) <= px(28):  # only grab a point if the click is reasonably close to it
            self._curve_drag = (pedal_key, nearest_i)
            self._curve_mouse_drag(event, pedal_key)

    def _curve_mouse_drag(self, event, pedal_key):
        if self._curve_drag is None or self._curve_drag[0] != pedal_key:
            return
        _, i = self._curve_drag
        canvas, curve_vars, _color = self._curve_widgets(pedal_key)
        _w, _h, (_left, top, _right, bottom), _xs = self._curve_plot_geometry(canvas, len(curve_vars))
        frac = 1.0 - (event.y - top) / max(1, bottom - top)
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

    @staticmethod
    def _curve_output(curve, frac):
        """Output fraction for an input fraction, the same piecewise-linear
        interpolation through 5 points as the firmware's applyCurve()."""
        pos = max(0.0, min(1.0, frac)) * (len(curve) - 1)
        i = min(int(pos), len(curve) - 2)
        t = pos - i
        return (curve[i] + (curve[i + 1] - curve[i]) * t) / 100.0

    def _redraw_pedal_curve(self, pedal_key):
        canvas, curve_vars, color = self._curve_widgets(pedal_key)
        n = len(curve_vars)
        w, h, (left, top, right, bottom), xs = self._curve_plot_geometry(canvas, n)
        if w <= 1 or h <= 1:
            return
        canvas.delete("all")
        canvas.create_line(left, bottom, right, top, fill=MAIN_BORDER, dash=(3, 2))  # linear reference
        for frac in (0.25, 0.5, 0.75):  # faint gridlines - helps eyeball where a drag will land
            y = bottom - (bottom - top) * frac
            canvas.create_line(left, y, right, y, fill=MAIN_BORDER, dash=(1, 3))
        canvas.create_line(left, bottom, right, bottom, fill=MAIN_BORDER)
        pts = []
        for i, var in enumerate(curve_vars):
            y = bottom - (bottom - top) * (var.get() / 100.0)
            pts += [xs[i], y]
        canvas.create_line(*pts, fill=color, width=px(2))
        dot, label_gap, edge = px(6), px(14), px(8)
        for i in range(0, len(pts), 2):
            x, y = pts[i], pts[i + 1]
            canvas.create_oval(x - dot, y - dot, x + dot, y + dot, fill=color, outline=MAIN_TEXT, width=1)
            # Label above the point, unless that would leave the plot.
            label_y = y - label_gap if y - label_gap >= edge else y + label_gap
            anchor = "center"
            if i == 0:
                anchor, x = "w", x + px(9)
            elif i == len(pts) - 2:
                anchor, x = "e", x - px(9)
            canvas.create_text(x, label_y, text=f"{curve_vars[i // 2].get()}%", fill=MAIN_TEXT,
                               font=T.FONT_CAPTION, anchor=anchor)
        for i, pct_label in enumerate(("0%", "25%", "50%", "75%", "100%")):
            anchor = "sw" if i == 0 else "se" if i == 4 else "s"
            x = xs[i] - px(4) if i == 0 else xs[i] + px(4) if i == 4 else xs[i]
            canvas.create_text(x, h - px(5), text=pct_label, fill=MAIN_TEXT_DIM, font=T.FONT_CAPTION, anchor=anchor)
        self._draw_curve_live_marker(pedal_key)

    def _draw_curve_live_marker(self, pedal_key):
        """A ring on the curve where the pedal is right now, so the effect of
        the curve can be seen while pressing the pedal."""
        if not hasattr(self, "throttle_curve_canvas"):
            return
        canvas, curve_vars, color = self._curve_widgets(pedal_key)
        canvas.delete("live")
        frac = self._pedal_live.get(pedal_key)
        if frac is None or not canvas.winfo_viewable():
            return
        w, h, (left, top, right, bottom), _xs = self._curve_plot_geometry(canvas, len(curve_vars))
        if w <= 1:
            return
        out = self._curve_output([v.get() for v in curve_vars], frac)
        x = left + (right - left) * frac
        y = bottom - (bottom - top) * out
        canvas.create_line(x, bottom, x, y, fill=T.ACCENT, dash=(2, 3), tags="live")
        ring = px(8)
        canvas.create_oval(x - ring, y - ring, x + ring, y + ring, outline=T.ACCENT, width=px(2), tags="live")

    def _open_calibrate_wizard(self, pedal_key):
        """Guided 2-step calibration for one pedal: capture it at rest, then
        fully pressed. Each capture averages a short window of readings
        (taking the median, so one spike can't skew it) and the result is
        checked before anything is sent: too little travel, or a pedal that
        moved during a capture, is explained and nothing is written. Only
        this pedal's endpoints change; the other pedal and both curves are
        untouched."""
        if not self.ser or not self.last_status:
            self._notify("Connect the pedals first - Volgas needs live readings to calibrate.", "warn")
            return
        if not self._cal_loaded:
            self._request_cal()
            self._notify("Still reading the pedals' settings - try again in a moment.", "warn")
            return

        pedal_name = "throttle" if pedal_key == "throttle" else "brake"
        limits = THROTTLE_LIMITS if pedal_key == "throttle" else BRAKE_LIMITS
        capture_s = CAPTURE_SECONDS[pedal_key]
        steps = [("rest", f"Take your foot off the {pedal_name} and let it rest."),
                 ("full", f"Press the {pedal_name} all the way down and hold it there.")]
        state = {"step": 0, "captures": {}, "samples": None, "deadline": None, "seen": None, "done": False}

        win = tk.Toplevel(self.root)
        win.title(f"Calibrate {pedal_name}")
        win.configure(bg=MAIN_PANEL)
        win.resizable(False, False)
        win.transient(self.root)
        win.grab_set()
        body = tk.Frame(win, bg=MAIN_PANEL)
        body.pack(fill="both", expand=True, padx=T.SPACE_5, pady=T.SPACE_5)

        tk.Label(body, text=f"Calibrate {pedal_name}", font=T.FONT_HEADING, fg=T.TEXT_PRIMARY,
                 bg=MAIN_PANEL).pack(anchor="w")
        step_label = tk.Label(body, text="", font=T.FONT_CAPTION, fg=T.TEXT_SECONDARY, bg=MAIN_PANEL)
        step_label.pack(anchor="w", pady=(T.SPACE_1, T.SPACE_4))
        text_w, bar_w, bar_h = px(380), px(380), px(6)
        instruction = tk.Label(body, text="", font=T.FONT_BODY, fg=T.TEXT_PRIMARY, bg=MAIN_PANEL,
                               wraplength=text_w, justify="left")
        instruction.pack(anchor="w")
        reading = tk.Label(body, text="", font=T.FONT_CAPTION, fg=T.TEXT_SECONDARY, bg=MAIN_PANEL)
        reading.pack(anchor="w", pady=(T.SPACE_3, 0))
        progress = tk.Canvas(body, width=bar_w, height=bar_h, bg=MAIN_PANEL, highlightthickness=0)
        progress.pack(anchor="w", pady=(T.SPACE_2, 0))
        ui.draw_round_rect(progress, 0, 0, bar_w, bar_h, bar_h / 2, fill=T.SURFACE_RAISED, outline="")
        progress_fill = ui.draw_round_rect(progress, 0, 0, 0, bar_h, bar_h / 2, fill=T.ACCENT, outline="",
                                           state="hidden")
        message = tk.Label(body, text="", font=T.FONT_BODY, fg=T.STATUS_ERROR, bg=MAIN_PANEL, wraplength=text_w,
                           justify="left")
        message.pack(anchor="w", pady=(T.SPACE_3, 0))

        buttons = tk.Frame(body, bg=MAIN_PANEL)
        buttons.pack(fill="x", pady=(T.SPACE_4, 0))
        primary = ui.FlatButton(buttons, "Capture", variant="primary", font=T.FONT_LABEL, padx=18, pady=6)
        primary.pack(side="right")
        cancel = ui.FlatButton(buttons, "Cancel", command=win.destroy, padx=18, pady=6)
        cancel.pack(side="right", padx=(0, T.SPACE_2))

        def current_reading():
            if not self.last_status:
                return None
            _hall_raw, hall_f, load_raw, _throttle, _brake = self.last_status
            return hall_f if pedal_key == "throttle" else load_raw

        def show_step():
            key, text = steps[state["step"]]
            step_label.config(text=f"Step {state['step'] + 1} of {len(steps)}")
            instruction.config(text=text + f" Then choose Capture and keep still for {capture_s:g} seconds.")
            primary.set_text("Capture")
            primary.set_command(start_capture)
            primary.set_enabled(True)

        def start_capture():
            message.config(text="")
            state["samples"] = []
            state["seen"] = None
            state["deadline"] = time.monotonic() + capture_s
            primary.set_enabled(False)
            primary.set_text("Hold still...")
            progress.itemconfig(progress_fill, state="normal")

        def finish_capture():
            key = steps[state["step"]][0]
            try:
                cap = summarise(state["samples"])
            except ValueError:
                message.config(text="No readings arrived during the capture. Check that the pedals are "
                                    "connected and try again.")
                show_step()
                return
            state["captures"][key] = cap
            state["step"] += 1
            if state["step"] < len(steps):
                show_step()
                return
            error = check_calibration(state["captures"]["rest"], state["captures"]["full"], limits)
            if error:
                log.info("calibration rejected (%s): %s", pedal_key, error)
                message.config(text=error, fg=T.STATUS_ERROR)
                state["step"], state["captures"] = 0, {}
                show_step()
                primary.set_text("Start again")
                return
            rest, full = state["captures"]["rest"], state["captures"]["full"]
            step_label.config(text="Ready to save")
            instruction.config(text=f"Rest {rest.value}, fully pressed {full.value} "
                                    f"({abs(full.value - rest.value)} {limits.unit} of travel). "
                                    "Save this calibration to the pedals?")
            message.config(text="")
            primary.set_text("Save")
            primary.set_command(save)
            primary.set_enabled(True)

        def save():
            rest, full = state["captures"]["rest"], state["captures"]["full"]
            keys = ("hallMin", "hallMax") if pedal_key == "throttle" else ("loadMin", "loadMax")
            previous = {k: self.cal_state[k] for k in keys}
            self.cal_state[keys[0]], self.cal_state[keys[1]] = rest.value, full.value
            if self._send_calibration():  # applies AND saves to flash - see _send_calibration
                log.info("calibrated %s: rest=%s full=%s", pedal_key, rest.value, full.value)
                state["done"] = True
                win.destroy()
            else:
                self.cal_state.update(previous)
                message.config(text="Couldn't send the calibration to the pedals. Nothing was changed.")

        def tick():
            if not win.winfo_exists():
                return
            if self.ser is None:
                message.config(text="The pedals were disconnected. Close this window and try again once "
                                    "they're back.")
                primary.set_enabled(False)
                return
            v = current_reading()
            if v is not None:
                reading.config(text=f"Sensor reading: {v}")
            if state["deadline"] is not None:
                stamp = getattr(self, "last_status_time", None)
                if v is not None and stamp != state["seen"]:
                    state["seen"] = stamp
                    state["samples"].append(v)
                remaining = state["deadline"] - time.monotonic()
                frac = 1.0 - max(0.0, remaining) / capture_s
                progress.coords(progress_fill, *ui.round_rect_points(0, 0, max(bar_h, bar_w * frac), bar_h, bar_h / 2))
                if remaining <= 0:
                    state["deadline"] = None
                    progress.itemconfig(progress_fill, state="hidden")
                    finish_capture()
            win.after(15, tick)

        win.bind("<Return>", lambda _e: primary.invoke())
        win.bind("<Escape>", lambda _e: win.destroy())
        show_step()
        tick()
        primary.focus_set()

    def _build_wheelbase_tab(self, parent):
        # Same look as the Main tab: flat buttons on the dark background, then
        # a rounded flat card with one full-width glow-bar row per setting,
        # all stacked in a single column.
        top = tk.Frame(parent, bg=MAIN_BG)
        top.pack(fill="x", padx=px(20), pady=(px(12), px(8)))
        self.wb_status_label = tk.Label(top, text="", font=T.FONT_CAPTION, fg=T.TEXT_SECONDARY, bg=MAIN_BG)
        self.wb_status_label.pack(side="left")
        # Every change applies to the board's RAM as it happens, so there's
        # no Apply button. Set Centre is on the wheel card below.
        self._wb_save_btn = ui.FlatButton(top, "Save & Reboot", variant="primary", font=T.FONT_CAPTION,
                                          command=lambda: self._wb_send_command(("save_and_reboot",)))
        self._wb_save_btn.pack(side="right")

        effect_keys, hardware_keys = list(WB_EFFECT_KEYS), list(WB_HARDWARE_KEYS)
        switches = {"ForceEnabled": ("FORCES", 1), "IntegratedSpringStrength": ("INTEGRATED SPRING",
                                                                                 INTEGRATED_SPRING_ON_PCT)}
        # One name column wide enough for the longest name, so every bar
        # starts at the same x.
        names = [_wb_display_name(k).upper() for k in effect_keys + hardware_keys] + [s[0] for s in switches.values()]
        name_w = max(tkfont.Font(font=WB_NAME_FONT).measure(n) for n in names) + px(24)

        # One card, one list: the Forces switch, the effect settings, the
        # Integrated Spring switch, then the hardware settings, with no
        # section headings (user, 2026-10-23), and the Power Limit caution as
        # the last line.
        rows = ([("switch", "ForceEnabled")] + [("slider", k) for k in effect_keys]
                + [("switch", "IntegratedSpringStrength")] + [("slider", k) for k in hardware_keys])
        # A copy of the Main tab's wheel card at the top, centred, filling all
        # the height the settings card below doesn't need. The settings card
        # is packed first (from the bottom) so if the window is short it's the
        # wheel card that gives up height, never the settings. Small
        # requested sizes so this tab doesn't raise the window's minimum size.
        card = self._make_flat_card(parent, px(520), WB_ROWS_TOP + (len(rows) + 1) * WB_ROW_H + CARD_PAD)
        card.pack(side="bottom", fill="x", padx=px(20), pady=(0, px(20)))
        wheel_card = self._build_wheel_card(parent, 200, 160)  # 100% sizes
        wheel_card.pack(side="top", fill="y", expand=True, pady=(0, px(12)))

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
            if kind == "switch":
                label, on_value = switches[key]
                self._add_wb_switch_row(card, WB_ROWS_TOP + i * WB_ROW_H, name_w, key, label, on_value)
            else:
                self._add_wb_slider_row(card, WB_ROWS_TOP + i * WB_ROW_H, key, name_w)
        card.create_text(CARD_PAD + px(4), WB_ROWS_TOP + len(rows) * WB_ROW_H + WB_ROW_H / 2,
                         text="Power Limit is a torque-limiting safety field - raise with care.",
                         font=("Segoe UI", 9), fill=MAIN_ORANGE_STATUS, anchor="w")

        # Tk on Windows sends <MouseWheel> / key presses to the focused widget,
        # not the one under the pointer - so the card takes focus on hover and
        # its rows' wheel / arrow-key nudges work.
        card.bind("<Enter>", lambda _e: card.focus_set(), add="+")
        self._update_wb_status()

    def _build_dashboard_tab(self, parent):
        """A live picture of the ESP32 dash's screen (see _render_dash), on a
        flat card like the other pages."""
        size = DASH_SIZE * DASH_PREVIEW_SCALE
        card = self._make_flat_card(parent, size + 2 * CARD_PAD + px(40), size + 2 * CARD_PAD + px(40))
        card.pack(padx=px(20), pady=px(20))
        self._dash_image_id = card.create_image(0, 0, anchor="center")
        frame_id = card.create_rectangle(0, 0, 0, 0, outline=MAIN_BORDER)

        def layout(event):
            cx, cy = event.width / 2, CARD_PAD + px(20) + size / 2
            card.coords(self._dash_image_id, cx, cy)
            card.coords(frame_id, cx - size / 2 - 1, cy - size / 2 - 1, cx + size / 2, cy + size / 2)

        card.bind("<Configure>", layout, add="+")
        self._dash_card = card
        self._dash_shown_state = None
        self._dash_preview_poll()

    def _dash_preview_state(self):
        bridge, now = self._dash_bridge, time.monotonic()
        if bridge is None or not bridge.connected:
            return ("standby", False, False, None, False)
        tel = bridge.last_telemetry
        if tel and now - tel[0] < DASH_TELEMETRY_TIMEOUT_S:
            lap, delta = bridge.last_lap_time, bridge.last_lap_delta
            footer = lap is not None and now - lap[0] < DASH_FOOTER_TIMEOUT_S
            return ("classic", tel[1], lap[1] if footer else None,
                    delta[1] if footer and delta else None)
        t = time.localtime()
        hub_lost = bridge.is_hub and not bridge.hub_status[1]["linked"]
        return ("standby", bridge.time_synced, True, (t.tm_wday, t.tm_hour, t.tm_min, t.tm_mday, t.tm_mon),
                bool(hub_lost))

    def _dash_preview_poll(self):
        if self._tab_frames["Dash"].winfo_ismapped():
            state = self._dash_preview_state()
            if state != self._dash_shown_state:
                self._dash_shown_state = state
                size = DASH_SIZE * DASH_PREVIEW_SCALE
                self._dash_photo = ImageTk.PhotoImage(_render_dash(state).resize((size, size), Image.NEAREST))
                self._dash_card.itemconfig(self._dash_image_id, image=self._dash_photo)
        self.root.after(DASH_PREVIEW_MS, self._dash_preview_poll)

    def _build_settings_tab(self, parent):
        """Opened by clicking the logo (no tab button). Holds the updater, on a
        flat card like the other pages: version, a check-on-startup switch,
        status, Check / Update buttons and the newer release's notes."""
        # Two sections, switched at the top: the app's own update and support
        # info, and the firmware updates for each device.
        switch = tk.Frame(parent, bg=MAIN_BG)
        switch.pack(anchor="w", padx=px(20), pady=(px(12), 0))
        general = tk.Frame(parent, bg=MAIN_BG)
        devices = tk.Frame(parent, bg=MAIN_BG)
        sections = {"Volgas": general, "Device updates": devices}
        section_buttons = {}

        def show_section(name):
            for key, frame in sections.items():
                frame.pack_forget()
                section_buttons[key].set_selected(key == name)
            sections[name].pack(fill="both", expand=True)

        for name in sections:
            section_buttons[name] = ui.FlatButton(switch, name, command=lambda n=name: show_section(n),
                                                  font=T.FONT_CAPTION)
            section_buttons[name].pack(side="left", padx=(0, T.SPACE_2))
        self._show_settings_section = show_section
        self._build_updates_tab(devices)
        parent = general   # the rest builds the Volgas section
        names = ["VERSION", "CHECK ON STARTUP", "STATUS", "WHAT'S NEW"]
        name_w = max(tkfont.Font(font=WB_NAME_FONT).measure(n) for n in names) + px(24)
        rows = 8  # version, switch, status, buttons, then four for release notes
        card = self._make_flat_card(parent, px(520), WB_ROWS_TOP + rows * WB_ROW_H + CARD_PAD)
        card.pack(padx=px(20), pady=px(20))  # its own 520 px, not stretched to the window
        x0 = CARD_PAD + name_w  # where values start, like the Wheelbase bars

        version_id, _ = self._add_wb_row_label(card, WB_ROWS_TOP, "VERSION")
        card.itemconfig(version_id, text=f"v{VOLGAS_VERSION}")

        switch_value_id, switch_mid = self._add_wb_row_label(card, WB_ROWS_TOP + WB_ROW_H, "CHECK ON STARTUP")
        on = {"value": self._wb_settings.get("check_updates_on_start", True)}

        def redraw_switch():
            _draw_toggle(card, on["value"], x0, switch_mid - px(10), tag="update_switch")
            card.itemconfig(switch_value_id, text="On" if on["value"] else "Off")

        def press(event):
            if x0 - px(4) <= event.x <= x0 + px(44) and switch_mid - px(14) <= event.y <= switch_mid + px(14):
                on["value"] = not on["value"]
                self._wb_settings["check_updates_on_start"] = on["value"]
                _save_settings(self._wb_settings)
                redraw_switch()

        card.bind("<Button-1>", press, add="+")
        redraw_switch()

        status_mid = WB_ROWS_TOP + 2 * WB_ROW_H + WB_ROW_H / 2
        card.create_text(CARD_PAD + px(4), status_mid, text="STATUS", font=WB_NAME_FONT, fill=MAIN_TEXT_DIM,
                         anchor="w")
        self._update_status_id = card.create_text(x0, status_mid, text="Not checked yet", font=("Segoe UI", 10),
                                                  fill=MAIN_TEXT, anchor="w")

        buttons = tk.Frame(card, bg=MAIN_PANEL)
        ui.FlatButton(buttons, "Check for updates", command=self._start_update_check,
                      font=T.FONT_CAPTION).pack(side="left")
        self._update_install_btn = ui.FlatButton(buttons, "Update", command=self._start_update_install,
                                                 variant="primary", font=T.FONT_CAPTION)
        card.create_window(x0, WB_ROWS_TOP + 3 * WB_ROW_H + WB_ROW_H / 2, window=buttons, anchor="w")

        notes_top = WB_ROWS_TOP + 4 * WB_ROW_H
        self._update_notes_title_id = card.create_text(CARD_PAD + px(4), notes_top + WB_ROW_H / 2, text="",
                                                       font=WB_NAME_FONT, fill=MAIN_TEXT_DIM, anchor="w")
        self._update_notes_id = card.create_text(x0, notes_top + px(8), text="", font=("Segoe UI", 9),
                                                 fill=MAIN_TEXT_DIM, anchor="nw")
        card.bind("<Configure>", lambda e: card.itemconfig(
            self._update_notes_id, width=max(px(50), e.width - x0 - CARD_PAD - px(8))), add="+")
        self._settings_card = card
        self._fit_settings_card()
        self._build_support_card(parent)
        show_section("Volgas")

    def _build_support_card(self, parent):
        """Where Volgas keeps its files, a shortcut to the log for bug
        reports, and the Wheelbase page switch."""
        card, inner = self._make_frame_card(parent, 520, 190)  # 100% sizes
        card.pack(padx=px(20), pady=(0, px(20)))
        tk.Label(inner, text="Support", font=T.FONT_TITLE, fg=T.TEXT_PRIMARY, bg=MAIN_PANEL).pack(anchor="w")
        tk.Label(inner, text="Settings and logs are kept in:", font=T.FONT_CAPTION, fg=T.TEXT_SECONDARY,
                 bg=MAIN_PANEL).pack(anchor="w", pady=(T.SPACE_2, 0))
        tk.Label(inner, text=data_dir(), font=T.FONT_CAPTION, fg=T.TEXT_PRIMARY, bg=MAIN_PANEL, wraplength=px(460),
                 justify="left").pack(anchor="w")
        row = tk.Frame(inner, bg=MAIN_PANEL)
        row.pack(anchor="w", pady=(T.SPACE_3, 0))
        ui.FlatButton(row, "Open log folder", command=lambda: self._open_folder(log_dir()),
                      font=T.FONT_CAPTION).pack(side="left")
        show = {"value": self._show_wheelbase}
        toggle = ui.FlatButton(row, "", font=T.FONT_CAPTION)

        def refresh():
            toggle.set_text("Wheelbase page: " + ("shown" if show["value"] else "hidden"))

        def flip():
            show["value"] = not show["value"]
            self._wb_settings["show_wheelbase"] = show["value"]
            _save_settings(self._wb_settings)
            refresh()
            self._notify("The Wheelbase page change takes effect the next time Volgas starts.", "info")

        toggle.set_command(flip)
        toggle.pack(side="left", padx=(T.SPACE_2, 0))
        refresh()

    def _open_folder(self, path):
        try:
            os.makedirs(path, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - opening a folder the app owns
            elif sys.platform == "darwin":
                subprocess.Popen(["open", path])
            else:
                subprocess.Popen(["xdg-open", path])
        except OSError as e:
            log.warning("could not open %s", path, exc_info=True)
            self._notify(f"Couldn't open the folder ({e}). It's at {path}", "warn")

    def _fit_settings_card(self):
        """Height = what's showing plus the same gap below the last line as
        above the first, so the card shrinks when there are no release notes."""
        card = self._settings_card
        card.update_idletasks()
        shown = [i for i in card.find_all()[1:]  # [0] is _make_flat_card's background
                 if card.type(i) != "text" or card.itemcget(i, "text")]
        top, bottom = card.bbox(*shown)[1::2]
        card.config(height=bottom + top)

    def _set_update_status(self, text):
        self._settings_card.itemconfig(self._update_status_id, text=text)

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
            self._set_update_status("Updating only works in Volgas.exe, not when running volgas.py")
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
                        self._set_update_status(f"Couldn't install the update: {e}")
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
                    self._set_update_status(text)
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
            self._set_update_status(f"Volgas v{version} is available")
            self._update_install_btn.config(text=f"Update to v{version}")
            self._update_install_btn.pack(side="left", padx=(px(8), 0))
            card.itemconfig(self._update_notes_title_id, text="WHAT'S NEW")
            card.itemconfig(self._update_notes_id, text=notes.strip()[:600])
        else:
            text = (f"v{version} is out, but its download files are missing" if newer
                    else f"Volgas is up to date (v{VOLGAS_VERSION})")
            self._set_update_status(text)
            self._update_install_btn.pack_forget()
            card.itemconfig(self._update_notes_title_id, text="")
            card.itemconfig(self._update_notes_id, text="")
        self._fit_settings_card()
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
        card.create_text(CARD_PAD + px(4), mid, text=text, font=WB_NAME_FONT, fill=MAIN_TEXT_DIM, anchor="w")
        value_id = card.create_text(0, mid, text="", font=("Segoe UI", 11, "bold"), fill=MAIN_TEXT, anchor="e")
        card.bind("<Configure>", lambda e: card.coords(value_id, e.width - CARD_PAD - px(4), mid), add="+")
        return value_id, mid

    def _add_wb_slider_row(self, card, row_top, name, name_w, label=None):
        """One Wheelbase setting (range from WB_SLIDER_SPECS) as a full-width
        row on a flat card: name, glow-pill bar (same halo + cap look as the
        Main tab's sliders), value. Click or drag the bar to set it; fields
        with only a few valid values step between them. Sends every change
        straight away, while dragging too, and remembers the value once the
        drag ends - not on every intermediate position, which would rewrite
        the settings file at drag rate. Mouse wheel / arrow keys over the row
        nudge by exactly one step, so precise values are reachable. While the
        wheelbase is disconnected the row can't be changed."""
        spec = WB_SLIDER_SPECS[name]
        choices = spec if isinstance(spec[0], tuple) else None
        bounds = None if choices else list(spec)
        unit = "%" if spec == (0, 100) else "°" if name == "MotionRange" else ""
        value_var = tk.IntVar(value=self._wb_remembered.get(name, choices[0][0] if choices else spec[0]))
        self.wb_field_vars.setdefault(name, []).append(value_var)
        value_id, mid = self._add_wb_row_label(card, row_top, label or _wb_display_name(name).upper())

        color = MAIN_CYAN if name == "MotionRange" else MAIN_BLUE  # matching the Main tab's Steering Range / FFB bars
        halo_color = _mix_color(MAIN_PANEL, color, 0.35)
        cap_color = _mix_color(color, "#ffffff", 0.55)
        pad = px(3)
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
            _draw_round_rect(card, x0 + pad, y0, x1 - pad, y1, r, fill=T.SURFACE_WELL, outline=MAIN_BORDER, width=1,
                              tags=tag)
            fill_x = x0 + pad + max(0.0, min(1.0, frac_of(value))) * (x1 - x0 - 2 * pad)
            if fill_x > x0 + pad + r:
                halo_pad = px(3)
                _draw_round_rect(card, x0 + pad - halo_pad, y0 - halo_pad, fill_x + halo_pad, y1 + halo_pad,
                                  r, fill=halo_color, outline="", tags=tag)
                _draw_round_rect(card, x0 + pad, y0, fill_x, y1, r, fill=color, outline="", tags=tag)
                _draw_round_rect(card, max(x0 + pad, fill_x - px(4)), y0, fill_x, y1, r, fill=cap_color,
                                  outline="", tags=tag)

        def set_value(value):
            if value != value_var.get():
                value_var.set(value)
                self._wb_write(name, value)

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
            self._wb_verify(name, value)

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
            if in_row(event) and x0 - px(6) <= event.x <= x1 + px(6) and self._wb_require_connected():
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
            if state["hover"] and self.wb_connected:
                nudge(steps)

        def wheel(event):
            if in_row(event) and self.wb_connected:
                nudge(1 if event.delta > 0 else -1)

        card.bind("<Configure>", redraw, add="+")
        card.bind("<Button-1>", press, add="+")
        card.bind("<B1-Motion>", motion, add="+")
        card.bind("<Motion>", motion, add="+")
        card.bind("<Leave>", lambda _e: state.update(hover=False), add="+")
        card.bind("<ButtonRelease-1>", release, add="+")
        card.bind("<MouseWheel>", wheel, add="+")
        card.bind("<Left>", lambda _e: key(-1), add="+")
        card.bind("<Right>", lambda _e: key(1), add="+")
        value_var.trace_add("write", redraw)
        redraw()

    def _add_wb_switch_row(self, card, row_top, name_w, name, label, on_value):
        """An on/off switch row instead of a slider: on sets the field to
        on_value, off to 0. Shows On for any non-zero value the board reports
        (e.g. a spring strength set in FFBeast's own app)."""
        var = tk.IntVar(value=self._wb_remembered.get(name, 0))
        self.wb_field_vars.setdefault(name, []).append(var)
        value_id, mid = self._add_wb_row_label(card, row_top, label)
        x, y = CARD_PAD + name_w, mid - px(10)  # top-left of the 40x20 switch, where the bars start
        tag = f"wb_switch_{name}"

        def redraw(*_args):
            _draw_toggle(card, var.get() > 0, x, y, tag=tag)
            if var.get() <= 0:
                text = "Off"
            else:
                text = f"{var.get()}%" if on_value > 1 else "On"
            card.itemconfig(value_id, text=text)

        def press(event):
            if (x - px(4) <= event.x <= x + px(44) and y - px(4) <= event.y <= y + px(24)
                    and self._wb_require_connected()):
                var.set(0 if var.get() > 0 else on_value)
                self._remember_wb(name, var.get())
                self._on_wb_settings({name: var.get()})
                self._wb_write(name, var.get())
                self._wb_verify(name, var.get())

        var.trace_add("write", redraw)
        card.bind("<Button-1>", press, add="+")
        redraw()

    # ---------- serial ----------

    def _connect_to(self, port):
        """Open the pedals and start a reader that belongs to this connection
        only. Each connection gets its own stop event and its own parser, so
        an unplug/replug can never leave two readers on one port or let an
        old reader pick up the new port."""
        try:
            ser = serial.Serial(port, 115200, timeout=0.05)
        except serial.SerialException:
            return  # auto-scan retries silently every second
        stop = threading.Event()
        parser = FrameParser(lambda t, p, s=stop: None if s.is_set() else self.rx_queue.put((t, p)))
        reader = threading.Thread(target=self._read_loop, args=(ser, parser, stop), name="pedal-reader",
                                  daemon=True)
        self.ser, self._pedal_stop, self.reader = ser, stop, reader
        self._cal_loaded = False
        self.last_status = None
        log.info("pedals connected on %s", port)
        reader.start()
        self._request_cal()

    def _disconnect(self, reason=None):
        ser, self.ser = self.ser, None
        stop, self._pedal_stop = getattr(self, "_pedal_stop", None), None
        reader, self.reader = self.reader, None
        if stop:
            stop.set()
        if ser:
            try:
                ser.close()
            except (serial.SerialException, OSError):
                log.warning("error closing the pedal port", exc_info=True)
        if reader and reader is not threading.current_thread():
            reader.join(timeout=0.5)
        if ser:
            log.info("pedals disconnected%s", f" ({reason})" if reason else "")
        self._cal_loaded = False
        self.last_status = None
        if hasattr(self, "main_throttle_update"):
            self.main_throttle_update(None)
            self.main_brake_update(None)

    def _auto_scan(self):
        if self._closing:
            return
        if not self.ser:
            port = find_pedal_port()
            if port:
                self._connect_to(port)
        self.root.after(1000, self._auto_scan)

    def _read_loop(self, ser, parser, stop):
        """Runs on its own thread for one connection. Only touches its own
        port and parser; reports a lost link through the queue."""
        while not stop.is_set():
            try:
                data = ser.read(64)
            except (serial.SerialException, OSError, TypeError, AttributeError) as e:
                if not stop.is_set():
                    self.rx_queue.put(("lost", (stop, str(e))))
                return
            if data:
                parser.feed(data)

    def _send(self, frame, quiet=False):
        if not self.ser:
            if not quiet:
                self._notify("The pedals aren't connected. Plug them in and Volgas will find them.", "warn")
            return False
        try:
            self.ser.write(frame)
            return True
        except (serial.SerialException, OSError) as e:
            log.warning("pedal write failed", exc_info=True)
            self._notify(f"Couldn't send to the pedals ({e}). Reconnecting...", "error")
            self._disconnect("write failed")
            return False

    # ---------- commands ----------

    def _request_cal(self):
        if self.ser:
            self._send(build_frame(MSG_GET_CALIBRATION), quiet=True)

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
            self._notify("The pedals aren't connected, so the change wasn't saved.", "warn")
            return False
        if not self._cal_loaded:
            # Every send carries the whole calibration, so sending before the
            # pedals' own values have arrived would overwrite their saved
            # calibration with Volgas's defaults. Ask again and wait instead.
            self._request_cal()
            self._notify("Still reading the pedals' settings - try again in a moment.", "warn")
            return False
        payload = struct.pack(
            CAL_FMT,
            self.cal_state["hallMin"], self.cal_state["hallMax"],
            self.cal_state["loadMin"], self.cal_state["loadMax"],
            *(v.get() for v in self.throttle_curve_vars),
            *(v.get() for v in self.brake_curve_vars),
        )
        if self._send(build_frame(MSG_SET_CALIBRATION, payload)):
            self._save()
            return True
        return False

    def _save(self):
        if self._send(build_frame(MSG_SAVE)):
            self._set_pedal_status("Saving to the pedals...", "idle")

    # ---------- wheelbase (HID PID) ----------

    def _wb_require_connected(self):
        if not self.wb_connected:
            self._notify("The wheelbase isn't connected.", "warn")
        return self.wb_connected

    def _wb_send_command(self, cmd):
        if self._wb_require_connected():
            self.wb_cmd_queue.put(cmd)

    def _wb_write(self, name, value):
        """Write one setting to the board's RAM (see the note above
        WB_SLIDER_SPECS)."""
        if self.wb_connected:
            self.wb_cmd_queue.put(("send_setting", name, value))
            if not self._wb_unsaved:
                self._wb_unsaved = True
                self._update_wb_status()

    def _wb_verify(self, name, value):
        """Read the board back after a change: FFBeast silently ignores some
        writes (on RC.26.1.1 in free mode, the three DirectX strengths), and
        the page should show what the board really has."""
        if self.wb_connected:
            self.wb_cmd_queue.put(("verify", name, value))

    def _update_wb_status(self):
        if not hasattr(self, "wb_status_label"):
            return
        if not FFBEAST_AVAILABLE:
            text, color = "Wheelbase support couldn't load - see the log", T.STATUS_ERROR
        elif not self.wb_connected:
            text, color = "Looking for the wheelbase...", T.TEXT_SECONDARY
        elif self._wb_unsaved:
            text, color = "Changed, not saved - Save & Reboot keeps the changes after a restart", T.STATUS_WARN
        else:
            text, color = "Connected - showing the wheelbase's own settings", T.STATUS_OK
        self.wb_status_label.config(text=text, fg=color)
        self._wb_save_btn.set_enabled(self.wb_connected)

    def _wb_worker_loop(self):
        """Owns the hid.device for the app's whole lifetime - hidapi handles
        aren't meant to be shared between threads - so the UI's commands
        (wb_cmd_queue) and the live position stream both go through here.
        Results go back to the UI thread through wb_status_queue."""
        wb = volgas_ffbeast_api.FFBeastWheel()
        stop = self._wb_stop
        while not stop.is_set():
            if not wb.connected:
                try:
                    if not wb.connect():
                        stop.wait(1.0)
                        continue
                    settings = {**wb.read_effect_settings(), **wb.read_hardware_settings()}
                except Exception as e:
                    wb.close()
                    log.info("wheelbase not ready: %s", e)
                    stop.wait(1.0)
                    continue
                log.info("wheelbase connected: %s", settings)
                # Anything queued while disconnected was meant for the old
                # connection's state.
                self._drain(self.wb_cmd_queue)
                self.wb_status_queue.put(("connected", settings))
            try:
                while True:
                    try:
                        cmd = self.wb_cmd_queue.get_nowait()
                    except queue.Empty:
                        break
                    self._wb_run_command(wb, cmd)
                state = wb.read_state(timeout_ms=20)
                if state:
                    self.wb_status_queue.put(("state", state))
            except Exception as e:
                # Save & Reboot lands here too: the board drops off USB as it
                # restarts, and the loop reconnects once it's back.
                log.info("wheelbase disconnected: %s", e)
                wb.close()
                self.wb_status_queue.put(("disconnected", str(e)))
                stop.wait(1.0)
        wb.close()

    @staticmethod
    def _drain(q):
        try:
            while True:
                q.get_nowait()
        except queue.Empty:
            pass

    def _wb_run_command(self, wb, cmd):
        kind = cmd[0]
        if kind == "send_setting":
            _, name, value = cmd
            wb.send_setting(name, value)
        elif kind == "save_and_reboot":
            log.info("wheelbase: save and reboot")
            wb.save_and_reboot()
            self.wb_status_queue.put(("saved",))
        elif kind == "reset_center":
            wb.reset_center()
        elif kind in ("read_settings", "verify"):
            if kind == "verify":
                self._wb_stop.wait(0.05)  # give the board a moment to apply the write
            settings = {**wb.read_effect_settings(), **wb.read_hardware_settings()}
            if kind == "verify" and settings.get(cmd[1]) != cmd[2]:
                log.info("wheelbase kept %s=%s, ignoring the write of %s", cmd[1], settings.get(cmd[1]), cmd[2])
                self.wb_status_queue.put(("rejected", cmd[1]))
            self.wb_status_queue.put(("settings", settings))

    def _wb_poll(self):
        # The board streams its state much faster than this runs: keep only
        # the newest one, so the wheel photo is rotated at most once per poll
        # however many arrived (rotating for every one once backed the queue
        # up until the app stopped responding).
        latest_state = None
        changed = False
        try:
            while True:
                msg = self.wb_status_queue.get_nowait()
                kind = msg[0]
                if kind == "state":
                    latest_state = msg[1]
                elif kind == "connected":
                    self.wb_connected = True
                    self._wb_unsaved = False
                    self._adopt_wb_settings(msg[1])
                    changed = True
                elif kind == "settings":
                    self._adopt_wb_settings(msg[1])
                elif kind == "rejected":
                    self._notify(f"The wheelbase didn't accept the change to {_wb_display_name(msg[1])} and kept "
                                 "its own value. FFBeast's free mode may lock this setting.", "warn")
                elif kind == "saved":
                    self._wb_unsaved = False
                    changed = True
                    self._notify("Saved to the wheelbase. It restarts and reconnects in a few seconds.", "info")
                elif kind == "disconnected":
                    self.wb_connected = False
                    self._wb_fw = None
                    changed = True
        except queue.Empty:
            pass
        if changed:
            self._update_wb_status()
        if latest_state is not None:
            self._wb_fw = "{ReleaseMajor}.{ReleaseMinor}.{ReleasePatch}".format(**latest_state)
            self._update_main_wheel(latest_state["Position"])
        if not self._closing:
            self.root.after(16, self._wb_poll)

    def _adopt_wb_settings(self, settings):
        """Show the board's own values and remember the ones Volgas displays."""
        self._on_wb_settings(settings)
        shown = {n: v for n, v in settings.items() if n in getattr(self, "wb_field_vars", {}) or n in
                 ("ForceEnabled", "PowerLimit", "MotionRange")}
        if any(self._wb_remembered.get(n) != v for n, v in shown.items()):
            self._wb_remembered.update(shown)
            _save_settings(self._wb_settings)

    def _on_wb_settings(self, settings):
        """Show wheelbase values on every control for them (the Wheelbase
        page and Home's range and power rows) from a real
        read_effect_settings()+read_hardware_settings() dict, or a single
        field just changed on one of them."""
        for name, value in settings.items():
            for var in self.wb_field_vars.get(name, ()):
                if var.get() != value:
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
                    stop, reason = payload
                    if stop is getattr(self, "_pedal_stop", None):  # ignore news from an old connection
                        self._disconnect(reason)
                        self._notify("Lost contact with the pedals. Volgas will reconnect when they're back.",
                                     "warn")
                elif msg_type == MSG_STATUS and len(payload) == STATUS_SIZE:
                    self._on_status(struct.unpack(STATUS_FMT, payload))
                elif msg_type == MSG_CALIBRATION and len(payload) == CAL_SIZE:
                    self._on_calibration(struct.unpack(CAL_FMT, payload))
                elif msg_type == MSG_SAVE_ACK and len(payload) == 1:
                    ok = payload[0] == 1
                    if ok:
                        self._set_pedal_status("Saved to the pedals", "ok")
                    else:
                        log.error("pedal flash save failed (device reported failure)")
                        self._set_pedal_status("The pedals couldn't save - the change is active until they "
                                               "restart", "error")
                        self._notify("The pedals reported that saving failed. The change is active now but "
                                     "will be lost when they restart.", "error")
        except queue.Empty:
            pass
        self.root.after(30, self._poll)

    def _on_status(self, st):
        self.last_status = st  # raw hall/load fields still consumed directly by the calibrate wizard
        self.last_status_time = time.monotonic()
        _hall_raw, hall_f, load_raw, throttle, brake = st
        self.pedal_throttle_update(throttle)
        self.pedal_brake_update(brake)
        self.main_throttle_update(throttle)
        self.main_brake_update(brake)
        if self._cal_loaded:
            # Where each pedal sits on its curve's input axis - the same
            # clamped mapping the firmware applies before the curve.
            self._pedal_live["throttle"] = linear_fraction(hall_f, self.cal_state["hallMin"],
                                                           self.cal_state["hallMax"])
            self._pedal_live["brake"] = linear_fraction(load_raw, self.cal_state["loadMin"],
                                                        self.cal_state["loadMax"])
            self._draw_curve_live_marker("throttle")
            self._draw_curve_live_marker("brake")

    def _set_pedal_status(self, text, kind="idle"):
        color = {"ok": T.STATUS_OK, "warn": T.STATUS_WARN, "error": T.STATUS_ERROR}.get(kind, T.TEXT_SECONDARY)
        if hasattr(self, "status_label"):
            self.status_label.config(text=text, fg=color)

    # ---------- live status (title bar pills, Home cards) ----------

    GAME_NAMES = {"rf2": "rFactor 2", "ac": "Assetto Corsa", "forza": "Forza Horizon"}
    STATUS_POLL_MS = 250

    def _device_states(self):
        """{device: (kind, text, connected)} for every device, the one source
        for the title-bar pills, the Home cards and the Updates page.
        `connected` devices have nothing to report, so their pills are hidden
        (user, 2026-11-13: link status only shows while disconnected)."""
        now = time.monotonic()
        states = {}
        if self.ser is None:
            states["pedals"] = ("idle", "Pedals not found", False)
        elif self.last_status is None or now - getattr(self, "last_status_time", 0) > 1.0:
            states["pedals"] = ("warn", "Pedals not responding", False)
        else:
            states["pedals"] = ("ok", "Pedals connected", True)

        bridge = self._dash_bridge
        if bridge is None:
            states["dash"] = ("error", "Dash unavailable", False)
            states["hub"] = ("error", "Hub unavailable", False)
        elif not bridge.connected:
            states["dash"] = ("idle", "Dash not found", False)
            states["hub"] = ("idle", "Hub not found", False)
        elif bridge.is_hub:
            states["hub"] = ("ok", "Hub connected", True)
            if bridge.hub_status[1]["linked"]:
                states["dash"] = ("ok", "Dash connected", True)
            else:
                states["dash"] = ("warn", "Dash not linked", False)
        elif now - bridge.connected_at < 3.0:
            # The hub hasn't answered yet; don't flash "hub not found".
            states["hub"] = ("idle", "Hub connecting", True)
            states["dash"] = ("ok", "Dash connected", True)
        else:
            # A dash plugged straight into the PC (no hub).
            states["hub"] = ("idle", "Hub not found", False)
            states["dash"] = ("ok", "Dash connected", True)

        if FFBEAST_AVAILABLE:
            states["wheelbase"] = (("ok", "Wheelbase connected", True) if self.wb_connected
                                   else ("idle", "Wheelbase not found", False))
        return states

    def _status_poll(self):
        if self._closing:
            return
        now = time.monotonic()
        states = self._device_states()
        # Device pills: only while the device is disconnected.
        self._apply_title_pills({k for k, (_kind, _text, ok) in states.items() if not ok})
        for key, pill in self._title_pills.items():
            kind, text, _ok = states[key]
            pill.set(kind, text)
        pedal, dash = states["pedals"], states["dash"]
        self._home_pedal_pill.set(pedal[0], pedal[1].replace("Pedals ", "").capitalize())
        self._show_card_pill(self._home_pedal_pill, not pedal[2])

        # Dash and game
        bridge = self._dash_bridge
        game_kind, game_text, detail = "idle", "No game", "--"
        if bridge is not None and bridge.connected:
            src = getattr(bridge, "active_source", None)
            if src and now - src[0] < 1.5:
                game_kind, game_text = "info", self.GAME_NAMES.get(src[1], src[1])
                detail = "Gear, speed, RPM" + (", lap time and delta" if src[1] in ("rf2", "ac") else "")
            else:
                detail = "Clock and date (standby)"
        self._game_pill.set(game_kind, game_text)
        self._home_dash_pill.set(dash[0], dash[1].replace("Dash ", "").capitalize())
        self._show_card_pill(self._home_dash_pill, not dash[2])
        # The wheel's own radio link (wheel <-> hub), on the wheel card.
        hub = states["hub"]
        self._home_wheel_pill.set("warn" if hub[2] else "idle", "Not linked" if hub[2] else "No hub")
        self._show_card_pill(self._home_wheel_pill, not dash[2])
        self._home_game_label.config(text=game_text if game_kind != "idle" else "None detected",
                                     fg=T.TEXT_PRIMARY if game_kind != "idle" else T.TEXT_HINT)
        self._home_dash_detail.config(text=detail)
        if self._tab_frames["Home"].winfo_ismapped():
            state = self._dash_preview_state()
            if state != self._home_dash_shown:
                self._home_dash_shown = state
                image = _render_dash(state)
                if T.SCALE != 1.0:
                    image = image.resize((px(DASH_SIZE), px(DASH_SIZE)), Image.NEAREST)
                self._home_dash_photo = ImageTk.PhotoImage(image)
                self._home_dash_image.config(image=self._home_dash_photo)
        self.root.after(self.STATUS_POLL_MS, self._status_poll)

    # ---------- updates page (firmware for every device) ----------

    # Devices Volgas can't update itself yet: the card and the check are
    # there, the Update button is disabled (user, 2026-11-13).
    UPDATE_LATER = "updating this from Volgas comes later"

    def _build_updates_tab(self, parent):
        """One card per thing that has software: the Volgas app (updated in
        Settings, so its card only links there) and each device. A card shows
        the installed version, the result of the last check, a Check button
        and an Update button; Check all at the top."""
        self._dev_ui = {}
        root = tk.Frame(parent, bg=MAIN_BG)
        root.pack(fill="both", expand=True, padx=px(20), pady=(px(12), px(8)))
        top = tk.Frame(root, bg=MAIN_BG)
        top.pack(fill="x", pady=(0, T.SPACE_2))
        tk.Label(top, text="Firmware for each device.", font=T.FONT_CAPTION, fg=T.TEXT_SECONDARY,
                 bg=MAIN_BG).pack(side="left")
        ui.FlatButton(top, "Check all", command=self._check_all_updates, variant="primary",
                      font=T.FONT_CAPTION).pack(side="right")
        for key in devs.DEVICES:
            if key != "wheelbase" or FFBEAST_AVAILABLE:
                self._build_update_card(root, key, devs.DEVICE_NAMES[key])

    def _build_update_card(self, parent, key, name):
        card, inner = self._make_frame_card(parent, 520, 96)
        card.pack(fill="x", pady=(0, T.SPACE_2))
        left = tk.Frame(inner, bg=MAIN_PANEL)
        left.pack(side="left", fill="both", expand=True)
        head = tk.Frame(left, bg=MAIN_PANEL)
        head.pack(anchor="w")
        tk.Label(head, text=name, font=T.FONT_TITLE, fg=T.TEXT_PRIMARY, bg=MAIN_PANEL).pack(side="left")
        pill = ui.StatusPill(head, "idle", "", bg=MAIN_PANEL)   # packed only while the device is disconnected
        info = tk.Label(left, text="", font=T.FONT_CAPTION, fg=T.TEXT_SECONDARY, bg=MAIN_PANEL, anchor="w")
        info.pack(anchor="w", pady=(px(2), 0))
        result = tk.Label(left, text="", font=T.FONT_CAPTION, fg=T.TEXT_PRIMARY, bg=MAIN_PANEL, anchor="w")
        result.pack(anchor="w")
        right = tk.Frame(inner, bg=MAIN_PANEL)
        right.pack(side="right")
        buttons = tk.Frame(right, bg=MAIN_PANEL)
        buttons.pack()
        ui_ = {"pill": pill, "pill_shown": False, "info": info, "result": result, "buttons": buttons}
        ui_["file"] = None
        if key == "dash":
            ui_["file"] = ui.FlatButton(buttons, "From file...", command=self._update_dash_from_file,
                                        font=T.FONT_CAPTION)
            ui_["file"].pack(side="left", padx=(0, T.SPACE_2))
        ui.FlatButton(buttons, "Check", command=lambda: self._check_updates({key}),
                      font=T.FONT_CAPTION).pack(side="left")
        ui_["update"] = ui.FlatButton(buttons, "Update", command=lambda: self._install_device_update(key),
                                      variant="primary", font=T.FONT_CAPTION)
        ui_["update"].pack(side="left", padx=(T.SPACE_2, 0))
        ui_["bar"] = ttk.Progressbar(right, length=px(200), maximum=100)   # only while updating
        self._dev_ui[key] = ui_

    def _device_versions(self):
        """The firmware version each connected device reports (None = not
        connected or doesn't report one)."""
        versions = {key: None for key in devs.DEVICES}
        bridge = self._dash_bridge
        if bridge is not None and bridge.is_hub and bridge.hub_status[1]["linked"]:
            versions["dash"] = devs.format_wheel_version(bridge.hub_status[1]["fw"])
        if self.wb_connected:
            versions["wheelbase"] = self._wb_fw
        return versions

    def _check_text(self, key, installed):
        """The result line of a device card."""
        if self._dev_text.get(key):
            return self._dev_text[key]
        if self._dev_error and key in self._dev_checked:
            return f"Couldn't check: {self._dev_error}"
        if key not in self._dev_checked or self._dev_manifest is None:
            return "Not checked yet"
        entry = self._dev_manifest.get(key)
        if entry is None:
            return "No firmware published yet"
        outcome = devs.compare(installed, entry["version"])
        if outcome == devs.UPDATE:
            return f"Update available: firmware {entry['version']}"
        if outcome == devs.CURRENT:
            return "Up to date"
        if outcome == devs.NEWER:
            return f"Newer than the published firmware ({entry['version']})"
        return f"Latest published: firmware {entry['version']}"

    def _update_available(self, key, installed):
        entry = (self._dev_manifest or {}).get(key)
        return (key in self._dev_checked and entry is not None
                and devs.compare(installed, entry["version"]) == devs.UPDATE)

    def _refresh_updates_page(self):
        states = self._device_states()
        versions = self._device_versions()
        release = getattr(self, "_latest_release", None)
        any_update = bool(release) and _version_tuple(release[0]) > _version_tuple(VOLGAS_VERSION)
        for key, ui_ in self._dev_ui.items():
            kind, text, connected = states[key]
            if connected == ui_["pill_shown"]:   # shown exactly while disconnected
                if connected:
                    ui_["pill"].pack_forget()
                else:
                    ui_["pill"].pack(side="left", padx=(T.SPACE_3, 0))
                ui_["pill_shown"] = not connected
            ui_["pill"].set(kind, text.replace(devs.DEVICE_NAMES[key] + " ", "").capitalize())
            installed = versions[key]
            if not connected:
                info = "Not connected"
            elif installed is not None:
                info = f"Installed: firmware {installed}"
            else:
                info = "Installed: version not reported"
            if key not in devs.UPDATABLE:
                info += f"  \u00b7  {self.UPDATE_LATER}"
            ui_["info"].config(text=info)
            ui_["result"].config(text=self._check_text(key, installed))
            can = (key in devs.UPDATABLE and connected and not self._dev_update_busy
                   and self._update_available(key, installed))
            ui_["update"].set_enabled(can)
            any_update = any_update or (self._update_available(key, installed) and key in devs.UPDATABLE)
            if ui_["file"] is not None:
                ui_["file"].set_enabled(connected and not self._dev_update_busy)
            busy = self._dev_update_busy and key == "dash"
            if busy and not ui_["bar"].winfo_ismapped():
                ui_["buttons"].pack_forget()
                ui_["bar"].pack()
            elif not busy and ui_["bar"].winfo_ismapped():
                ui_["bar"].pack_forget()
                ui_["buttons"].pack()
        if hasattr(self, "_nav"):
            self._nav.set_badge("Settings", any_update)   # the app or a device has an update

    def _updates_poll(self):
        """Worker messages and the page's live values, on the UI thread."""
        if self._closing:
            return
        try:
            while True:
                msg = self._dev_queue.get_nowait()
                kind = msg[0]
                if kind == "manifest":
                    self._dev_busy = False
                    self._dev_manifest = msg[1]
                    self._dev_error = None
                elif kind == "check_error":
                    self._dev_busy = False
                    err = msg[1]
                    self._dev_error = ("no public release found" if isinstance(err, urllib.error.HTTPError)
                                       and err.code == 404 else str(err))
                    log.info("device update check failed: %s", err)
                elif kind == "progress":   # (progress, fraction, text)
                    self._dev_text["dash"] = msg[2]
                    self._dev_ui["dash"]["bar"].config(value=msg[1] * 100)
                elif kind == "done":       # (done, old, new)
                    self._dev_update_busy = False
                    self._dev_text.pop("dash", None)
                    self._notify(f"Dash updated: firmware {devs.format_wheel_version(msg[1])} to "
                                 f"{devs.format_wheel_version(msg[2])}.", "info")
                elif kind == "failed":     # (failed, text)
                    self._dev_update_busy = False
                    self._dev_text.pop("dash", None)
                    self._notify(f"Dash update failed: {msg[1]}", "error")
        except queue.Empty:
            pass
        self._refresh_updates_page()   # also keeps the nav badge current from any page
        self.root.after(150 if self._dev_update_busy else 500, self._updates_poll)

    def _check_all_updates(self):
        self._start_update_check()
        self._check_updates(set(devs.DEVICES))

    def _check_updates(self, keys=None):
        """Fetch the latest release's firmware list and show how each of the
        given devices compares (all of them if none are named). One download
        covers every device; only the named cards show their result."""
        if self._dev_busy:
            return
        self._dev_busy = True
        self._dev_checked |= set(keys or devs.DEVICES)
        self._dev_error = None

        def work():
            try:
                self._dev_queue.put(("manifest", devs.fetch_manifest(_http_get, UPDATE_API_URL)))
            except Exception as e:
                self._dev_queue.put(("check_error", e))

        threading.Thread(target=work, daemon=True, name="device-update-check").start()

    def _install_device_update(self, key):
        if key != "dash":
            return
        entry = (self._dev_manifest or {}).get("dash")
        if entry is None:
            return
        self._start_dash_update(f"firmware {entry['version']}", entry=entry)

    def _update_dash_from_file(self):
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="Dash firmware", filetypes=[("Firmware", "*.bin"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                image = f.read()
        except OSError as e:
            self._notify(f"Couldn't read the file ({e}).", "error")
            return
        self._start_dash_update(os.path.basename(path), image=image)

    def _start_dash_update(self, what, entry=None, image=None):
        """Send firmware to the wheel through the hub. The dash bridge lets go
        of the hub's port for the duration (it normally holds it)."""
        bridge = self._dash_bridge
        if self._dev_update_busy or bridge is None:
            return
        if not (bridge.is_hub and bridge.hub_status[1]["linked"]):
            self._notify("The hub isn't linked to the wheel, so the dash can't be updated wirelessly.", "warn")
            return
        if not messagebox.askyesno(
                "Update dash", f"Send {what} to the dash through the hub?\n\nThe car must be parked. The dash "
                               "shows its progress and restarts when it's done; this takes a few minutes."):
            return
        self._dev_update_busy = True
        self._dev_text["dash"] = "Starting..."
        port = bridge.port

        def work():
            try:
                data = image
                if data is None:
                    self._dev_queue.put(("progress", 0.0, "Downloading..."))
                    data = devs.download_firmware(_http_get, entry)
                self._dev_queue.put(("progress", 0.0, "Waiting for the hub's port..."))
                if not bridge.pause():
                    raise volgas_wheel_ota.OtaError("couldn't free the hub's port")
                try:
                    last = [-1]

                    def progress(done, total):
                        if done * 100 // total != last[0]:
                            last[0] = done * 100 // total
                            self._dev_queue.put(("progress", done / total, f"Sending... {last[0]}%"))

                    old, new = volgas_wheel_ota.update_wheel(data, port=port, progress=progress, log=log.info)
                finally:
                    bridge.resume()
                self._dev_queue.put(("done", old, new))
            except Exception as e:
                log.exception("dash update failed")
                self._dev_queue.put(("failed", str(e)))

        threading.Thread(target=work, daemon=True, name="dash-update").start()

    # ---------- shutdown ----------

    def _on_close(self):
        """Stop every background worker and close every port before the
        window goes, so nothing is left holding a COM port or running on
        after the app has gone."""
        if self._closing:
            return
        self._closing = True
        log.info("closing")
        self._bridge_stop.set()
        self._wb_stop.set()
        self._disconnect("app closing")
        for thread, name in ((self._bridge_thread, "dash bridge"), (self._wb_thread, "wheelbase")):
            if thread is not None:
                thread.join(timeout=2.0)
                if thread.is_alive():
                    log.warning("%s did not stop within 2 s", name)
        self.root.destroy()

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
        self._cal_loaded = True
        self._redraw_pedal_curve("throttle")
        self._redraw_pedal_curve("brake")
        self._set_pedal_status("Calibration read from the pedals", "ok")


DPI_AWARENESS_CONTEXT_SYSTEM_AWARE = -2
PROCESS_SYSTEM_DPI_AWARE = 1


def _enable_dpi_awareness():
    """On a scaled display (125%, 150%...) Windows drew this app at 100% and
    stretched the picture, so everything looked fuzzy. Declaring it DPI aware
    stops that: Windows draws it at the real resolution, and Volgas enlarges
    its own pixel sizes instead (volgas_theme.init_scale,
    _scale_layout_constants). System aware, not per-monitor: on a second
    screen with a different scale Windows stretches it again, rather than Tk
    having to re-lay-out the window on the fly. Must run before the first
    window is created."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.windll.user32
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_AWARENESS_CONTEXT_SYSTEM_AWARE)):
            return
    except (AttributeError, OSError):
        pass  # before Windows 10 1703
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(PROCESS_SYSTEM_DPI_AWARE)  # Windows 8.1+
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            log.warning("could not make Volgas DPI aware - it may look fuzzy on a scaled display")


# Layout sizes in pixels for a 100% display, multiplied by the display scale
# at start-up. DASH_SIZE and the other DASH_* sizes are the dash's own screen
# pixels, not the window's, so they stay as they are.
_SCALED_LAYOUT_CONSTANTS = (
    "SPLASH_LOGO_SIZE", "DEFAULT_WINDOW_W", "DEFAULT_WINDOW_H", "MAIN_WHEEL_CANVAS_SIZE",
    "WHEEL_SOURCE_WORKING_SIZE", "WHEEL_VERTICAL_OFFSET", "CARD_PAD", "WHEEL_CENTRE_BUTTON_Y",
    "WB_ROWS_TOP", "WB_ROW_H", "WB_BAR_H", "WB_VALUE_W",
    "TITLE_BAR_H", "CAPTION_BUTTON_W", "TOP_RESIZE_BAND", "TOP_RESIZE_CORNER",
)


def _scale_layout_constants():
    """Apply volgas_theme.SCALE to the layout constants. Call once, after
    T.init_scale() and before building any interface."""
    module = globals()
    for name in _SCALED_LAYOUT_CONSTANTS:
        module[name] = px(module[name])
    # The dash preview is pixel art blown up with nearest-neighbour, so its
    # magnification stays a whole number.
    module["DASH_PREVIEW_SCALE"] = max(1, int(DASH_PREVIEW_SCALE * T.SCALE + 0.5))
    for name in ("CURVE_PAD_X", "CURVE_PAD_TOP", "CURVE_PAD_BOTTOM"):
        setattr(VolgasApp, name, px(getattr(VolgasApp, name)))


# Same AppId as installer/Volgas.iss; Inno Setup names the uninstall entry after it.
INSTALLER_APP_ID = "{A20B26BF-623F-447E-8E63-8A219892D06C}"


def _sync_installed_version():
    """The installer records the version in Windows' installed-apps list, but
    the in-app updater swaps the exe without the installer, so that entry
    would keep showing the old version. Correct it on start - only for the
    copy the installer put there, not a separately downloaded Volgas.exe."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return
    import winreg
    path = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{INSTALLER_APP_ID}_is1"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            location, _ = winreg.QueryValueEx(key, "InstallLocation")
            here = os.path.dirname(sys.executable)
            if os.path.normcase(os.path.normpath(location)) != os.path.normcase(os.path.normpath(here)):
                return
            shown, _ = winreg.QueryValueEx(key, "DisplayVersion")
            if shown != VOLGAS_VERSION:
                winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, VOLGAS_VERSION)
                log.info("installed-apps entry updated from %s to %s", shown, VOLGAS_VERSION)
    except OSError:
        pass  # not installed with the installer


def _report_callback_exception(exc_type, exc, tb):
    """Tk swallows exceptions raised in event handlers and timers into
    stderr, which a windowed exe doesn't have - log them instead."""
    log.error("error in an interface callback", exc_info=(exc_type, exc, tb))


if __name__ == "__main__":
    log_path = setup_logging(log_dir())
    log.info("Volgas %s starting (Python %s, %s), log: %s", VOLGAS_VERSION, sys.version.split()[0], sys.platform,
             log_path)
    _enable_dpi_awareness()
    _sync_installed_version()
    root = tk.Tk()
    T.init_scale(root)
    _scale_layout_constants()
    log.info("display scale %.2f", T.SCALE)
    root.report_callback_exception = _report_callback_exception
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
