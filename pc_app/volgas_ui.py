"""Reusable Volgas interface components, built on plain Tk so they look the
same on Windows, macOS and Linux (native tk.Button ignores colours on
macOS). Every interactive component has hover, pressed, keyboard-focus and
disabled states, all taken from volgas_theme.
"""
import math
import tkinter as tk
from tkinter import font as tkfont

import volgas_theme as T


def round_rect_points(x1, y1, x2, y2, r):
    r = max(0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
    return [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
            x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]


def draw_round_rect(canvas, x1, y1, x2, y2, r, **kwargs):
    return canvas.create_polygon(round_rect_points(x1, y1, x2, y2, r), smooth=True, **kwargs)


# ---------- buttons ----------

_BUTTON_STYLES = {
    # variant: (bg, hover_bg, pressed_bg, fg, border)
    "primary": (T.ACCENT, T.ACCENT_HOVER, "#2bb4bf", T.TEXT_ON_ACCENT, T.ACCENT),
    "secondary": (T.SURFACE_RAISED, "#2a303b", "#343b48", T.TEXT_PRIMARY, T.BORDER_STRONG),
    "ghost": (T.SURFACE_CARD, T.SURFACE_RAISED, "#2a303b", T.TEXT_PRIMARY, T.SURFACE_CARD),
}


class FlatButton(tk.Label):
    """A themed push button. command() runs on click, Enter or Space when
    focused. set_enabled(False) greys it out and ignores input."""

    def __init__(self, master, text, command=None, variant="secondary", font=None, padx=14, pady=5,
                 selected=False, **kwargs):
        self._variant = variant
        self._command = command
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._selected = selected
        bg, _h, _p, fg, border = _BUTTON_STYLES[variant]
        super().__init__(master, text=text, font=font or T.FONT_BODY, bg=bg, fg=fg, padx=padx, pady=pady,
                         cursor="hand2", takefocus=1, highlightthickness=1, highlightbackground=border,
                         highlightcolor=T.TEXT_PRIMARY, **kwargs)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Return>", self._invoke)
        self.bind("<space>", self._invoke)
        self.bind("<FocusIn>", lambda _e: self._paint())
        self.bind("<FocusOut>", lambda _e: self._paint())
        self._paint()

    def _paint(self):
        bg, hover_bg, pressed_bg, fg, border = _BUTTON_STYLES[self._variant]
        if not self._enabled:
            self.config(bg=T.SURFACE_CARD, fg=T.TEXT_DISABLED, highlightbackground=T.BORDER, cursor="")
            return
        if self._selected:
            bg, border = T.SURFACE_RAISED, T.ACCENT
            fg = T.ACCENT
        color = pressed_bg if self._pressed else hover_bg if self._hover else bg
        self.config(bg=color, fg=fg, highlightbackground=border, cursor="hand2")

    def _on_enter(self, _e):
        self._hover = True
        self._paint()

    def _on_leave(self, _e):
        self._hover = self._pressed = False
        self._paint()

    def _on_press(self, _e):
        if self._enabled:
            self._pressed = True
            self._paint()

    def _on_release(self, e):
        was_pressed, self._pressed = self._pressed, False
        self._paint()
        inside = 0 <= e.x < self.winfo_width() and 0 <= e.y < self.winfo_height()
        if was_pressed and inside:
            self._invoke()

    def _invoke(self, _e=None):
        if self._enabled and self._command:
            self._command()
        return "break"

    def set_enabled(self, enabled):
        self._enabled = bool(enabled)
        self.config(takefocus=1 if enabled else 0)
        self._paint()

    def set_selected(self, selected):
        self._selected = bool(selected)
        self._paint()

    def set_text(self, text):
        self.config(text=text)

    def set_command(self, command):
        self._command = command

    def invoke(self):
        self._invoke()


# ---------- status pill ----------

STATUS_STYLES = {
    "ok": (T.STATUS_OK_BG, T.STATUS_OK),
    "warn": (T.STATUS_WARN_BG, T.STATUS_WARN),
    "error": (T.STATUS_ERROR_BG, T.STATUS_ERROR),
    "idle": (T.STATUS_IDLE_BG, T.TEXT_SECONDARY),
    "info": (T.STATUS_INFO_BG, T.ACCENT),
}


class StatusPill(tk.Canvas):
    """Rounded status label: a coloured dot plus text, e.g. "Connected".
    The state always shows as words as well as colour."""

    def __init__(self, master, kind="idle", text="", bg=T.SURFACE_CARD, font=None):
        self._font = tkfont.Font(font=font or T.FONT_CAPTION)
        h = self._font.metrics("linespace") + 8
        super().__init__(master, height=h, width=10, bg=bg, highlightthickness=0, bd=0)
        self._kind, self._text = None, None
        self.set(kind, text)

    def set(self, kind, text):
        if (kind, text) == (self._kind, self._text):
            return
        self._kind, self._text = kind, text
        fill, fg = STATUS_STYLES.get(kind, STATUS_STYLES["idle"])
        h = int(self.cget("height"))
        dot = 7
        text_w = self._font.measure(text)
        w = 10 + dot + 6 + text_w + 12
        self.config(width=w)
        self.delete("all")
        draw_round_rect(self, 0, 0, w, h, h / 2, fill=fill, outline="")
        cy = h / 2
        self.create_oval(10, cy - dot / 2, 10 + dot, cy + dot / 2, fill=fg, outline="")
        self.create_text(10 + dot + 6, cy, text=text, fill=fg, font=self._font, anchor="w")


# ---------- toasts ----------

class Toaster:
    """Non-blocking messages in the bottom-right corner of the window,
    replacing modal pop-ups for information and warnings. Each message
    disappears on its own after a few seconds, or when clicked."""

    MAX_VISIBLE = 3

    def __init__(self, root):
        self.root = root
        self._items = []

    def show(self, text, kind="info", ms=T.TOAST_MS):
        fill, fg = STATUS_STYLES.get(kind, STATUS_STYLES["info"])
        frame = tk.Frame(self.root, bg=fill, highlightthickness=1, highlightbackground=fg, cursor="hand2")
        bar = tk.Frame(frame, bg=fg, width=4)
        bar.pack(side="left", fill="y")
        label = tk.Label(frame, text=text, font=T.FONT_BODY, fg=T.TEXT_PRIMARY, bg=fill, justify="left",
                         wraplength=360, padx=12, pady=10)
        label.pack(side="left")
        for w in (frame, label, bar):
            w.bind("<Button-1>", lambda _e, f=frame: self._dismiss(f))
        self._items.append(frame)
        while len(self._items) > self.MAX_VISIBLE:
            self._dismiss(self._items[0])
        self._relayout()
        self.root.after(ms, lambda f=frame: self._dismiss(f))
        return frame

    def _dismiss(self, frame):
        if frame in self._items:
            self._items.remove(frame)
            frame.destroy()
            self._relayout()

    def _relayout(self):
        y = -T.SPACE_4
        for frame in reversed(self._items):
            frame.update_idletasks()
            frame.place(relx=1.0, rely=1.0, x=-T.SPACE_4, y=y, anchor="se")
            frame.lift()
            y -= frame.winfo_reqheight() + T.SPACE_2


# ---------- navigation rail ----------

def _icon_home(c, cx, cy, col):
    c.create_line(cx - 9, cy - 1, cx, cy - 9, cx + 9, cy - 1, fill=col, width=2, capstyle="round", joinstyle="round")
    c.create_line(cx - 6, cy - 3, cx - 6, cy + 8, cx + 6, cy + 8, cx + 6, cy - 3, fill=col, width=2,
                  joinstyle="round")


def _icon_wheel(c, cx, cy, col):
    c.create_oval(cx - 9, cy - 9, cx + 9, cy + 9, outline=col, width=2)
    c.create_oval(cx - 2.5, cy - 2.5, cx + 2.5, cy + 2.5, outline=col, width=2)
    c.create_line(cx - 9, cy, cx - 2.5, cy, fill=col, width=2)
    c.create_line(cx + 2.5, cy, cx + 9, cy, fill=col, width=2)
    c.create_line(cx, cy + 2.5, cx, cy + 9, fill=col, width=2)


def _icon_pedals(c, cx, cy, col):
    draw_round_rect(c, cx - 9, cy - 8, cx - 2, cy + 9, 2, outline=col, width=2, fill="")
    draw_round_rect(c, cx + 2, cy - 4, cx + 9, cy + 9, 2, outline=col, width=2, fill="")


def _icon_dash(c, cx, cy, col):
    c.create_arc(cx - 10, cy - 8, cx + 10, cy + 12, start=0, extent=180, style="arc", outline=col, width=2)
    angle = math.radians(40)
    c.create_line(cx, cy + 2, cx + 7 * math.cos(angle), cy + 2 - 7 * math.sin(angle), fill=col, width=2,
                  capstyle="round")
    c.create_line(cx - 10, cy + 8, cx + 10, cy + 8, fill=col, width=2)


def _icon_base(c, cx, cy, col):
    c.create_oval(cx - 9, cy - 9, cx + 9, cy + 9, outline=col, width=2)
    c.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=col, outline="")


def _icon_settings(c, cx, cy, col):
    for i in range(8):
        a = i * math.pi / 4
        c.create_line(cx + 6 * math.cos(a), cy + 6 * math.sin(a), cx + 10 * math.cos(a), cy + 10 * math.sin(a),
                      fill=col, width=3, capstyle="round")
    c.create_oval(cx - 6.5, cy - 6.5, cx + 6.5, cy + 6.5, outline=col, width=2)
    c.create_oval(cx - 2.5, cy - 2.5, cx + 2.5, cy + 2.5, outline=col, width=2)


NAV_ICONS = {"home": _icon_home, "wheel": _icon_wheel, "pedals": _icon_pedals, "dash": _icon_dash,
             "base": _icon_base, "settings": _icon_settings}


class NavRail(tk.Frame):
    """Vertical page navigation down the left edge. add(key, label, icon)
    adds an item; select(key) marks it current. on_select(key) runs on
    click or keyboard activation."""

    ITEM_H = 62

    def __init__(self, master, on_select):
        super().__init__(master, bg=T.SURFACE_RAIL, width=T.NAV_RAIL_W)
        self.pack_propagate(False)
        self.grid_propagate(False)
        self._on_select = on_select
        self._items = {}
        self._current = None
        self._top = tk.Frame(self, bg=T.SURFACE_RAIL)
        self._top.pack(side="top", fill="x", pady=(T.SPACE_2, 0))
        self._bottom = tk.Frame(self, bg=T.SURFACE_RAIL)
        self._bottom.pack(side="bottom", fill="x", pady=(0, T.SPACE_2))

    def add(self, key, label, icon, bottom=False):
        c = tk.Canvas(self._bottom if bottom else self._top, width=T.NAV_RAIL_W, height=self.ITEM_H,
                      bg=T.SURFACE_RAIL, highlightthickness=0, cursor="hand2", takefocus=1)
        c.pack(side="top")
        item = {"canvas": c, "label": label, "icon": icon, "hover": False, "badge": False}
        self._items[key] = item
        c.bind("<Enter>", lambda _e, k=key: self._set_hover(k, True))
        c.bind("<Leave>", lambda _e, k=key: self._set_hover(k, False))
        c.bind("<Button-1>", lambda _e, k=key: self._on_select(k))
        c.bind("<Return>", lambda _e, k=key: self._on_select(k))
        c.bind("<space>", lambda _e, k=key: self._on_select(k))
        c.bind("<FocusIn>", lambda _e, k=key: self._paint(k))
        c.bind("<FocusOut>", lambda _e, k=key: self._paint(k))
        self._paint(key)

    def set_badge(self, key, on):
        if key in self._items:
            self._items[key]["badge"] = bool(on)
            self._paint(key)

    def _set_hover(self, key, on):
        self._items[key]["hover"] = on
        self._paint(key)

    def select(self, key):
        self._current = key
        for k in self._items:
            self._paint(k)

    def _paint(self, key):
        item = self._items[key]
        c = item["canvas"]
        selected = key == self._current
        focused = c.focus_displayof() is c if c.winfo_exists() else False
        c.delete("all")
        w, h = T.NAV_RAIL_W, self.ITEM_H
        if selected or item["hover"]:
            draw_round_rect(c, 6, 3, w - 6, h - 3, T.RADIUS_CONTROL,
                            fill=T.SURFACE_RAISED if selected else "#181c23", outline="")
        if selected:
            c.create_rectangle(0, 14, 3, h - 14, fill=T.ACCENT, outline="")
        if focused:
            draw_round_rect(c, 5, 2, w - 5, h - 2, T.RADIUS_CONTROL, outline=T.TEXT_SECONDARY, fill="", width=1)
        col = T.TEXT_PRIMARY if selected else T.TEXT_SECONDARY
        NAV_ICONS[item["icon"]](c, w / 2, 24, T.ACCENT if selected else col)
        c.create_text(w / 2, h - 13, text=item["label"], fill=col, font=T.FONT_NAV)
        if item["badge"]:
            c.create_oval(w / 2 + 9, 11, w / 2 + 17, 19, fill=T.STATUS_WARN, outline=T.SURFACE_RAIL, width=2)
