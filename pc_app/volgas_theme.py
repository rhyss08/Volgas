"""Volgas design tokens - the one place colours, type sizes, spacing and
corner radii are defined. Screens and components read from here instead of
hard-coding values, so the look stays consistent and can be tuned in one
place.

Contrast ratios below are measured against SURFACE_CARD (WCAG formula);
4.5:1 is the usual minimum for readable text.

Pixel sizes here and throughout the interface are for a 100% (96 DPI)
display. init_scale() enlarges them for a scaled display (125%, 150%...);
code with its own pixel numbers wraps them in px().
"""
import os
import sys

# ---- surfaces (four depth levels, darkest at the back) ----
SURFACE_WINDOW = "#0b0d11"    # window background
SURFACE_RAIL = "#111419"      # title bar and navigation rail
SURFACE_CARD = "#171b22"      # cards and panels
SURFACE_RAISED = "#1f242d"    # hover / selected rows, inputs
SURFACE_WELL = "#0d1015"      # recessed areas inside cards (gauge tracks, plots)

BORDER = "#2f3542"            # card outlines
BORDER_STRONG = "#454c5a"     # input and button outlines (visible, not loud)

# ---- text ----
TEXT_PRIMARY = "#eceef2"      # 14.9:1
TEXT_SECONDARY = "#a3aab6"    # 7.4:1
TEXT_HINT = "#7d8594"         # 4.7:1 - the faintest text allowed
TEXT_DISABLED = "#5d6470"     # disabled controls only, never information
TEXT_ON_ACCENT = "#071216"    # 10.6:1 on ACCENT

# ---- Volgas identity (kept from the original app) ----
ACCENT = "#3fd4e0"            # Volgas cyan - primary actions, selection
ACCENT_HOVER = "#6fe0e9"
THROTTLE = "#39e26a"          # data colour, also used for "connected"
BRAKE = "#ff4d4d"             # data colour
BLUE = "#2f8fff"

# ---- status (always paired with a label or icon, never colour alone) ----
STATUS_OK = "#39e26a"
STATUS_WARN = "#f0a832"
STATUS_ERROR = "#ff5c5c"
STATUS_IDLE = "#7d8594"
STATUS_OK_BG = "#12291b"
STATUS_WARN_BG = "#2b2414"
STATUS_ERROR_BG = "#2e1517"
STATUS_IDLE_BG = "#1f242d"
STATUS_INFO_BG = "#13262a"

# ---- type scale (points; Tk scales points with the display DPI) ----
FONT_FAMILY = "Segoe UI"
FONT_CAPTION = (FONT_FAMILY, 9)
FONT_BODY = (FONT_FAMILY, 10)
FONT_LABEL = (FONT_FAMILY, 10, "bold")
FONT_TITLE = (FONT_FAMILY, 13, "bold")
FONT_HEADING = (FONT_FAMILY, 16, "bold")
FONT_DISPLAY = (FONT_FAMILY, 26, "bold")
FONT_NAV = (FONT_FAMILY, 9)

# ---- spacing (px, 4-based) ----
SPACE_1 = 4
SPACE_2 = 8
SPACE_3 = 12
SPACE_4 = 16
SPACE_5 = 24
SPACE_6 = 32

# ---- corners ----
RADIUS_CONTROL = 6
RADIUS_CARD = 12

# ---- layout ----
NAV_RAIL_W = 76
TOAST_MS = 4500

# ---- display scaling ----
# Volgas tells Windows it handles scaling itself (see volgas.py), so Windows
# no longer stretches the window - which made it fuzzy - and every pixel size
# is multiplied by SCALE instead. Font sizes are in points, which Tk already
# converts at the display's real DPI, so they need nothing.
SCALE = 1.0
_PIXEL_TOKENS = ("SPACE_1", "SPACE_2", "SPACE_3", "SPACE_4", "SPACE_5", "SPACE_6",
                 "RADIUS_CONTROL", "RADIUS_CARD", "NAV_RAIL_W")
_scaled = False


def px(value):
    """A pixel size for a 100% display, at the current display scale."""
    return int(round(value * SCALE))


def init_scale(root):
    """Read the display scale and enlarge the pixel tokens above. Call once,
    right after creating the Tk root and before building any interface.
    VOLGAS_UI_SCALE (e.g. 1.5) overrides it, so the scaled layout can be
    checked on any machine; macOS and Linux otherwise stay at 1, since Tk
    already handles high-resolution screens there."""
    global SCALE, _scaled
    if _scaled:
        return SCALE
    _scaled = True
    override = os.environ.get("VOLGAS_UI_SCALE")
    if override:
        scale = float(override)
    elif sys.platform == "win32":
        scale = root.winfo_fpixels("1i") / 96.0
    else:
        scale = 1.0
    SCALE = min(4.0, max(1.0, round(scale, 3)))
    module = globals()
    for name in _PIXEL_TOKENS:
        module[name] = px(module[name])
    return SCALE
