"""SENSORZ visual system for the IQ Spectrogram Converter.

Colour tokens, spacing, and control language match CRFS IQ Recorder
(`crfs_iq_recorder/crfs_iq_recorder/theme.py`): slate surfaces, teal accent,
Segoe UI, 8px controls / 12px cards, sun/moon theme toggle.
"""

from __future__ import annotations

import math
import tkinter as tk
from dataclasses import dataclass
from tkinter import ttk

LIGHT_MODE = "light"
DARK_MODE = "dark"


@dataclass(frozen=True)
class Palette:
    bg: str
    surface: str
    text: str
    muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    border: str
    focus: str
    danger: str
    ok_fg: str
    log_bg: str
    log_text: str
    primary_disabled_bg: str
    primary_disabled_fg: str
    button_hover: str
    scrollbar: str
    info: str


# Copied from CRFS IQ Recorder LIGHT / DARK palettes (teal-on-slate).
LIGHT = Palette(
    bg="#F4F6F8",
    surface="#FFFFFF",
    text="#0F172A",
    muted="#64748B",
    accent="#0F766E",
    accent_hover="#0D9488",
    accent_pressed="#115E59",
    border="#E2E8F0",
    focus="#0F766E",
    danger="#B91C1C",
    ok_fg="#065F46",
    log_bg="#F8FAFC",
    log_text="#334155",
    primary_disabled_bg="#99F6E4",
    primary_disabled_fg="#0F766E",
    button_hover="#E2E8F0",
    scrollbar="#CBD5E1",
    info="#0F766E",
)

DARK = Palette(
    bg="#0B1220",
    surface="#1E293B",
    text="#F8FAFC",
    muted="#94A3B8",
    accent="#14B8A6",
    accent_hover="#2DD4BF",
    accent_pressed="#0F766E",
    border="#334155",
    focus="#2DD4BF",
    danger="#F87171",
    ok_fg="#6EE7B7",
    log_bg="#0F172A",
    log_text="#CBD5E1",
    primary_disabled_bg="#134E4A",
    primary_disabled_fg="#5EEAD4",
    button_hover="#334155",
    scrollbar="#475569",
    info="#5EEAD4",
)

THEMES = {LIGHT_MODE: LIGHT, DARK_MODE: DARK}

FONT_UI = "Segoe UI"
FONT_UI_SEMIBOLD = "Segoe UI Semibold"
FONT_LOG = "Consolas"

_current_mode = LIGHT_MODE


def normalize_theme(mode: str | None) -> str:
    return DARK_MODE if str(mode or "").strip().casefold() == DARK_MODE else LIGHT_MODE


def current_mode() -> str:
    return _current_mode


def current_palette() -> Palette:
    return THEMES[_current_mode]


def set_current_mode(mode: str | None) -> str:
    global _current_mode
    _current_mode = normalize_theme(mode)
    return _current_mode


def theme_toggle_tooltip(mode: str | None = None) -> str:
    current = normalize_theme(mode or current_mode())
    if current == DARK_MODE:
        return "Switch to Light Mode"
    return "Switch to Dark Mode"


def apply_ttk_styles(style: ttk.Style, p: Palette) -> None:
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", font=(FONT_UI, 10))

    style.configure("App.TFrame", background=p.bg)
    style.configure("Surface.TFrame", background=p.surface)
    style.configure("Header.TFrame", background=p.surface)

    style.configure(
        "HeaderTitle.TLabel",
        background=p.surface,
        foreground=p.text,
        font=(FONT_UI_SEMIBOLD, 14),
    )
    style.configure(
        "HeaderSub.TLabel",
        background=p.surface,
        foreground=p.muted,
        font=(FONT_UI, 9),
    )
    style.configure(
        "Field.TLabel",
        background=p.surface,
        foreground=p.muted,
        font=(FONT_UI, 9),
    )
    style.configure(
        "Hint.TLabel",
        background=p.surface,
        foreground=p.muted,
        font=(FONT_UI, 8),
    )
    style.configure(
        "Info.TLabel",
        background=p.surface,
        foreground=p.info,
        font=(FONT_UI, 8),
    )
    style.configure(
        "Warn.TLabel",
        background=p.surface,
        foreground=p.danger,
        font=(FONT_UI, 8),
    )
    style.configure(
        "Status.TLabel",
        background=p.bg,
        foreground=p.text,
        font=(FONT_UI, 9),
    )
    style.configure(
        "Time.TLabel",
        background=p.bg,
        foreground=p.muted,
        font=(FONT_UI_SEMIBOLD, 9),
    )
    style.configure(
        "DisplayToggle.TLabel",
        background=p.surface,
        foreground=p.text,
        font=(FONT_UI_SEMIBOLD, 10),
    )

    entry_kw = dict(
        fieldbackground=p.surface,
        background=p.surface,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        insertcolor=p.text,
        padding=6,
        relief="flat",
        borderwidth=1,
    )
    style.configure("App.TEntry", **entry_kw)
    style.map(
        "App.TEntry",
        fieldbackground=[("disabled", p.bg), ("focus", p.surface)],
        foreground=[("disabled", p.muted)],
        bordercolor=[("focus", p.focus), ("disabled", p.border)],
        lightcolor=[("focus", p.focus), ("disabled", p.border)],
        darkcolor=[("focus", p.focus), ("disabled", p.border)],
    )

    style.configure(
        "App.TCombobox",
        fieldbackground=p.surface,
        background=p.surface,
        foreground=p.text,
        arrowcolor=p.text,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        insertcolor=p.text,
        padding=5,
        relief="flat",
        borderwidth=1,
        arrowsize=13,
    )
    style.map(
        "App.TCombobox",
        fieldbackground=[
            ("disabled", p.bg),
            ("readonly", p.surface),
            ("focus", p.surface),
        ],
        background=[
            ("disabled", p.bg),
            ("readonly", p.surface),
            ("active", p.button_hover),
        ],
        foreground=[("disabled", p.muted), ("readonly", p.text)],
        arrowcolor=[("disabled", p.muted), ("readonly", p.text)],
        bordercolor=[("focus", p.focus), ("hover", p.accent), ("disabled", p.border)],
        lightcolor=[("focus", p.focus), ("disabled", p.border)],
        darkcolor=[("focus", p.focus), ("disabled", p.border)],
        selectbackground=[("readonly", p.surface), ("!readonly", p.accent)],
        selectforeground=[("readonly", p.text), ("!readonly", "#FFFFFF")],
    )

    btn_common = dict(
        background=p.surface,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        focusthickness=1,
        focuscolor=p.focus,
        relief="flat",
        borderwidth=1,
    )
    style.configure("Browse.TButton", font=(FONT_UI, 9), padding=(12, 6), **btn_common)
    style.configure("Secondary.TButton", font=(FONT_UI, 9, "bold"), padding=(12, 6), **btn_common)
    style.configure("Preset.TButton", font=(FONT_UI, 8), padding=(8, 4), **btn_common)
    style.configure(
        "PresetSelected.TButton",
        font=(FONT_UI, 8, "bold"),
        padding=(8, 4),
        background=p.accent,
        foreground="#FFFFFF",
        bordercolor=p.accent,
        lightcolor=p.accent,
        darkcolor=p.accent,
        focusthickness=1,
        focuscolor=p.focus,
        relief="flat",
        borderwidth=1,
    )
    style.configure(
        "Ghost.TButton",
        font=(FONT_UI, 9, "bold"),
        padding=(8, 4),
        background=p.surface,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        focusthickness=1,
        focuscolor=p.focus,
        relief="flat",
        borderwidth=1,
    )
    for name in ("Browse.TButton", "Secondary.TButton", "Preset.TButton", "Ghost.TButton"):
        style.map(
            name,
            background=[
                ("disabled", p.bg),
                ("pressed", p.border),
                ("active", p.button_hover),
            ],
            foreground=[("disabled", p.muted)],
            bordercolor=[("focus", p.focus), ("disabled", p.border), ("active", p.border)],
            lightcolor=[("focus", p.focus), ("active", p.border)],
            darkcolor=[("focus", p.focus), ("active", p.border)],
        )
    style.map(
        "PresetSelected.TButton",
        background=[
            ("disabled", p.primary_disabled_bg),
            ("pressed", p.accent_pressed),
            ("active", p.accent_hover),
        ],
        foreground=[("disabled", p.primary_disabled_fg)],
        bordercolor=[("focus", p.focus), ("active", p.accent_hover)],
    )

    style.configure(
        "App.TCheckbutton",
        background=p.bg,
        foreground=p.text,
        font=(FONT_UI, 9),
        focuscolor=p.bg,
        indicatorcolor=p.surface,
        indicatorrelief="flat",
        indicatormargin=4,
        padding=2,
    )
    style.map(
        "App.TCheckbutton",
        background=[("active", p.bg)],
        foreground=[("disabled", p.muted)],
        indicatorcolor=[
            ("disabled", p.border),
            ("pressed", p.accent_pressed),
            ("selected", p.accent),
            ("!selected", p.surface),
        ],
    )
    style.configure(
        "Surface.TCheckbutton",
        background=p.surface,
        foreground=p.text,
        font=(FONT_UI, 9),
        focuscolor=p.surface,
        indicatorcolor=p.surface,
        indicatorrelief="flat",
        indicatormargin=4,
        padding=2,
    )
    style.map(
        "Surface.TCheckbutton",
        background=[("active", p.surface)],
        foreground=[("disabled", p.muted)],
        indicatorcolor=[
            ("disabled", p.border),
            ("pressed", p.accent_pressed),
            ("selected", p.accent),
            ("!selected", p.bg),
        ],
    )

    style.configure(
        "App.Horizontal.TProgressbar",
        troughcolor=p.border,
        background=p.accent,
        bordercolor=p.border,
        lightcolor=p.accent,
        darkcolor=p.accent,
        thickness=10,
    )

    style.configure(
        "Card.TLabelframe",
        background=p.surface,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        relief="solid",
        borderwidth=1,
    )
    style.configure(
        "Card.TLabelframe.Label",
        background=p.surface,
        foreground=p.text,
        font=(FONT_UI_SEMIBOLD, 10),
    )
    style.configure(
        "Log.TLabelframe",
        background=p.surface,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        relief="solid",
        borderwidth=1,
    )
    style.configure(
        "Log.TLabelframe.Label",
        background=p.surface,
        foreground=p.muted,
        font=(FONT_UI, 9),
    )

    style.configure(
        "App.Vertical.TScrollbar",
        background=p.scrollbar,
        troughcolor=p.bg,
        bordercolor=p.bg,
        lightcolor=p.scrollbar,
        darkcolor=p.scrollbar,
        arrowcolor=p.text,
        relief="flat",
        borderwidth=0,
    )
    style.map(
        "App.Vertical.TScrollbar",
        background=[("active", p.muted), ("disabled", p.border)],
        arrowcolor=[("disabled", p.muted)],
    )


def apply_option_db(root: tk.Misc, p: Palette) -> None:
    """Theme Combobox listboxes (ttk popdowns) without native black chrome."""
    for pattern, value in (
        ("*TCombobox*Listbox.background", p.surface),
        ("*TCombobox*Listbox.foreground", p.text),
        ("*TCombobox*Listbox.selectBackground", p.accent),
        ("*TCombobox*Listbox.selectForeground", "#FFFFFF"),
        ("*TCombobox*Listbox.activeStyle", "none"),
        ("*TCombobox*Listbox.relief", "flat"),
        ("*TCombobox*Listbox.borderWidth", 0),
        ("*TCombobox*Listbox.highlightThickness", 0),
        ("*TCombobox*Listbox.font", f"{{{FONT_UI}}} 10"),
    ):
        try:
            root.option_add(pattern, value, 80)
        except tk.TclError:
            pass


def color_combobox_popdown(combo: ttk.Combobox, p: Palette) -> None:
    try:
        popdown = combo.tk.call("ttk::combobox::PopdownWindow", combo)
    except tk.TclError:
        return
    try:
        combo.tk.call(
            popdown,
            "configure",
            "-background",
            p.border,
            "-highlightbackground",
            p.border,
            "-highlightcolor",
            p.border,
        )
    except tk.TclError:
        pass
    try:
        combo.tk.call(f"{popdown}.f", "configure", "-background", p.surface, "-bd", 0)
    except tk.TclError:
        pass
    try:
        combo.tk.call(
            f"{popdown}.f.l",
            "configure",
            "-background",
            p.surface,
            "-foreground",
            p.text,
            "-selectbackground",
            p.accent,
            "-selectforeground",
            "#FFFFFF",
            "-borderwidth",
            0,
            "-highlightthickness",
            0,
            "-relief",
            "flat",
            "-font",
            f"{{{FONT_UI}}} 10",
        )
    except tk.TclError:
        pass


def hook_combobox_popdown(combo: ttk.Combobox, palette_getter) -> None:
    if getattr(combo, "_iq_popup_hooked", False):
        return

    def _open(_event=None):
        p = palette_getter()
        combo.after(1, lambda: color_combobox_popdown(combo, p))
        combo.after(20, lambda: color_combobox_popdown(combo, p))

    combo.bind("<ButtonPress-1>", _open, add="+")
    combo.bind("<KeyPress-Down>", _open, add="+")
    combo.bind("<KeyPress-space>", _open, add="+")
    combo.bind("<<ComboboxSelected>>", lambda e: None, add="+")
    combo._iq_popup_hooked = True  # type: ignore[attr-defined]


def available_workarea(root: tk.Misc | None = None) -> tuple[int, int, int, int]:
    try:
        import ctypes

        class RECT(ctypes.Structure):
            _fields_ = [
                ("left", ctypes.c_long),
                ("top", ctypes.c_long),
                ("right", ctypes.c_long),
                ("bottom", ctypes.c_long),
            ]

        rect = RECT()
        if ctypes.windll.user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0):
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top
    except Exception:
        pass
    if root is not None:
        try:
            return 0, 0, int(root.winfo_screenwidth()), int(root.winfo_screenheight())
        except tk.TclError:
            pass
    return 0, 0, 1280, 720


def fit_window_to_screen(
    root: tk.Tk,
    width: int,
    height: int,
    *,
    min_width: int = 640,
    min_height: int = 520,
    margin: int = 24,
) -> None:
    """Open inside the usable desktop (taskbar / DPI), still resizable."""
    try:
        root.update_idletasks()
    except tk.TclError:
        return
    left, top, aw, ah = available_workarea(root)
    max_w = max(320, aw - margin)
    max_h = max(280, ah - margin)
    min_w = min(min_width, max_w)
    min_h = min(min_height, max_h)
    try:
        root.minsize(min_w, min_h)
        root.resizable(True, True)
    except tk.TclError:
        return
    w = min(max(int(width), min_w), max_w)
    h = min(max(int(height), min_h), max_h)
    x = left + max(0, (aw - w) // 2)
    y = top + max(0, (ah - h) // 2)
    try:
        root.geometry(f"{w}x{h}+{x}+{y}")
    except tk.TclError:
        pass


def apply_windows_titlebar(root: tk.Misc, dark: bool) -> None:
    if root.tk.call("tk", "windowingsystem") != "win32":
        return
    try:
        import ctypes

        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        value = ctypes.c_int(1 if dark else 0)
        # 20 = DWMWA_USE_IMMERSIVE_DARK_MODE (Win 11 / late Win 10)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(value), ctypes.sizeof(value))
        ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 19, ctypes.byref(value), ctypes.sizeof(value))
    except Exception:
        pass


class Tooltip:
    """Small overlay tooltip; follows the current palette."""

    def __init__(self, widget: tk.Widget, text: str, palette_getter=current_palette, delay_ms: int = 420):
        self.widget = widget
        self.text = text
        self._palette_getter = palette_getter
        self._delay_ms = delay_ms
        self._tip: tk.Toplevel | None = None
        self._after: str | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        try:
            self._after = self.widget.after(self._delay_ms, self._show)
        except tk.TclError:
            self._after = None

    def _cancel(self) -> None:
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None

    def _show(self) -> None:
        self._after = None
        if self._tip is not None:
            return
        try:
            if not self.widget.winfo_ismapped():
                return
        except tk.TclError:
            return
        p = self._palette_getter()
        try:
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        except tk.TclError:
            return
        tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            tw.transient(self.widget.winfo_toplevel())
        except tk.TclError:
            pass
        wrap = tk.Frame(tw, bg=p.border, bd=0, highlightthickness=0)
        wrap.pack()
        inner = tk.Frame(wrap, bg=p.text, bd=0, padx=9, pady=6)
        inner.pack(padx=1, pady=1)
        tk.Label(
            inner,
            text=self.text,
            bg=p.text,
            fg=p.surface,
            font=(FONT_UI, 9),
            justify="left",
            wraplength=280,
        ).pack()
        tw.geometry(f"+{x}+{y}")
        self._tip = tw


class ThemeToggle(tk.Canvas):
    """Moon in light mode (go Dark); sun in dark mode (go Light)."""

    def __init__(self, master, command, **kwargs):
        kwargs.setdefault("width", 36)
        kwargs.setdefault("height", 36)
        kwargs.setdefault("highlightthickness", 1)
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("cursor", "hand2")
        kwargs.setdefault("takefocus", 1)
        super().__init__(master, **kwargs)
        self._command = command
        self._mode = LIGHT_MODE
        self._p = LIGHT
        self._hover = False
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self.bind("<FocusIn>", lambda e: self._redraw())
        self.bind("<FocusOut>", lambda e: self._redraw())
        self._redraw()

    def set_state(self, mode: str, palette: Palette) -> None:
        self._mode = normalize_theme(mode)
        self._p = palette
        self._redraw()

    def _click(self, _event=None):
        if self._command:
            self._command()
        return "break"

    def _enter(self, _event=None):
        self._hover = True
        self._redraw()

    def _leave(self, _event=None):
        self._hover = False
        self._redraw()

    def _redraw(self) -> None:
        p = self._p
        bg = p.button_hover if self._hover else p.surface
        border = p.accent if self._hover else p.focus if self.focus_get() is self else p.border
        self.configure(bg=bg, highlightbackground=border, highlightcolor=p.focus)
        self.delete("all")
        color = p.text
        if self._mode == DARK_MODE:
            self._draw_sun(color)
        else:
            self._draw_moon(color, bg)

    def _draw_sun(self, color: str) -> None:
        cx, cy, pixel = 18.0, 18.0, 18.0
        radius = pixel * 0.18
        inner = pixel * 0.28
        outer = pixel * 0.42
        for index in range(8):
            angle = math.radians(index * 45.0)
            self.create_line(
                cx + inner * math.cos(angle),
                cy + inner * math.sin(angle),
                cx + outer * math.cos(angle),
                cy + outer * math.sin(angle),
                fill=color,
                width=2,
                capstyle=tk.ROUND,
            )
        self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, fill=color, outline="")

    def _draw_moon(self, color: str, bg: str) -> None:
        cx, cy, pixel = 18.0, 18.0, 18.0
        radius = pixel * 0.32
        x, y = cx - pixel * 0.04, cy
        self.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")
        cr = radius * 0.88
        x2, y2 = cx + pixel * 0.14, cy - pixel * 0.08
        self.create_oval(x2 - cr, y2 - cr, x2 + cr, y2 + cr, fill=bg, outline="")


class PrimaryButton(tk.Button):
    def __init__(self, master, **kwargs):
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("cursor", "hand2")
        kwargs.setdefault("font", (FONT_UI_SEMIBOLD, 11))
        kwargs.setdefault("padx", 20)
        kwargs.setdefault("pady", 8)
        kwargs.setdefault("highlightthickness", 1)
        super().__init__(master, **kwargs)
        self._p = LIGHT
        self._hover = False
        self.bind("<Enter>", self._on_enter, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.apply_palette(LIGHT)

    def apply_palette(self, p: Palette) -> None:
        self._p = p
        self._paint()

    def _on_enter(self, _event=None):
        self._hover = True
        self._paint()

    def _on_leave(self, _event=None):
        self._hover = False
        self._paint()

    def _paint(self) -> None:
        p = self._p
        disabled = str(self.cget("state")) == "disabled"
        if disabled:
            bg, fg = p.primary_disabled_bg, p.primary_disabled_fg
        elif self._hover:
            bg, fg = p.accent_hover, "#FFFFFF"
        else:
            bg, fg = p.accent, "#FFFFFF"
        try:
            self.configure(
                bg=bg,
                fg=fg,
                activebackground=p.accent_pressed,
                activeforeground="#FFFFFF",
                disabledforeground=p.primary_disabled_fg,
                highlightbackground=bg,
                highlightcolor=p.focus,
            )
        except tk.TclError:
            pass
