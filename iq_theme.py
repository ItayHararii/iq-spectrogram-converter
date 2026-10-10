"""Visual system for the CRFS IQ Spectrogram Converter.

Colour tokens, type sizes, spacing, and control language are copied from
CRFS IQ Recorder (`crfs_iq_recorder/crfs_iq_recorder/theme.py`, PySide6):
slate surfaces, teal accent, Segoe UI 13px body / 18px header, 36px theme
toggle with the same sun/moon glyph, dark log panel, surface status bar.
The Recorder is Qt; this app is Tk, so pixel sizes are expressed in points
(13px = 10pt at 96 DPI) and scale with the monitor DPI.
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


def ui_scale(root: tk.Misc) -> float:
    """Pixels per CSS pixel: 1.0 at 96 DPI, 1.5 at 150 % display scaling."""
    try:
        return max(0.75, min(4.0, float(root.winfo_fpixels("1i")) / 96.0))
    except tk.TclError:
        return 1.0


def px(root: tk.Misc, value: float) -> int:
    """Scale a 96-DPI pixel size (as used in the Recorder stylesheet) to this screen."""
    return max(1, int(round(value * ui_scale(root))))


# Recorder stylesheet sizes (px at 96 DPI) expressed as Tk points.
PT_BODY = 10          # 13px body text
PT_SMALL = 9          # 12px headerSub / status bar / log
PT_HINT = 8           # 11px captions
PT_SECTION = 11       # 14px sectionTitle (bold)
PT_HEADER = 13        # 18px headerTitle (bold)
PT_PRIMARY = 11       # 14px primary button (bold)


_checkbox_elements: dict[tuple, str] = {}
_checkbox_images: list = []  # keep PhotoImage references alive


def _checkbox_indicator_element(style: ttk.Style, p: Palette, box: str, size: int, gap: int) -> str | None:
    """Create (once per palette/size) an image-based check indicator like Qt Fusion's.

    ttk cannot restyle an existing element, so each palette gets its own
    element name and apply_ttk_styles swaps the layout between them.
    """
    try:
        from PIL import Image, ImageDraw, ImageTk
    except ImportError:
        return None
    # One Tcl interpreter per Tk root: images from a destroyed root must not be reused.
    try:
        interp = style.master.tk.interpaddr()
    except Exception:
        interp = id(style.master)
    key = (interp, p.accent, p.border, box, size)
    if key in _checkbox_elements:
        return _checkbox_elements[key]

    def make(fill, outline, tick, tick_color="#FFFFFF"):
        ss = 4
        r = size * ss
        img = Image.new("RGBA", (r, r), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((0, 0, r - 1, r - 1), radius=int(r * 0.18), fill=fill, outline=outline, width=ss)
        if tick:
            pts = [(r * 0.26, r * 0.53), (r * 0.44, r * 0.71), (r * 0.76, r * 0.33)]
            d.line(pts, fill=tick_color, width=max(ss, int(r * 0.11)), joint="curve")
        photo = ImageTk.PhotoImage(img.resize((size, size), Image.Resampling.LANCZOS), master=style.master)
        _checkbox_images.append(photo)
        return photo

    unchecked = make(box, p.border, False)
    hover = make(box, p.accent, False)
    checked = make(p.accent, p.accent, True)
    checked_hover = make(p.accent_hover, p.accent_hover, True)
    disabled = make(box, p.border, False)
    disabled_checked = make(p.border, p.border, True, p.muted)
    name = f"IQCheck.{len(_checkbox_elements)}.{abs(hash(key)) % 100000}.indicator"
    try:
        style.element_create(
            name,
            "image",
            unchecked,
            ("disabled", "selected", disabled_checked),
            ("disabled", disabled),
            ("selected", "active", checked_hover),
            ("selected", checked),
            ("active", hover),
            sticky="",
            padding=(0, 0, gap, 0),
        )
    except tk.TclError:
        return None
    _checkbox_elements[key] = name
    return name


def apply_ttk_styles(style: ttk.Style, p: Palette) -> None:
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    # ttk paddings are device pixels; scale them like the Recorder's px values.
    try:
        k = ui_scale(style.master)
    except Exception:
        k = 1.0

    def sp(value: float) -> int:
        return max(1, int(round(value * k)))

    style.configure(".", font=(FONT_UI, PT_BODY))

    style.configure("App.TFrame", background=p.bg)
    style.configure("Surface.TFrame", background=p.surface)
    style.configure("Header.TFrame", background=p.surface)

    style.configure(
        "HeaderTitle.TLabel",
        background=p.surface,
        foreground=p.text,
        font=(FONT_UI, PT_HEADER, "bold"),
    )
    style.configure(
        "HeaderSub.TLabel",
        background=p.surface,
        foreground=p.muted,
        font=(FONT_UI, PT_SMALL),
    )
    style.configure(
        "SectionTitle.TLabel",
        background=p.surface,
        foreground=p.text,
        font=(FONT_UI, PT_SECTION, "bold"),
    )
    style.configure(
        "Muted.TLabel",
        background=p.surface,
        foreground=p.muted,
        font=(FONT_UI, PT_BODY),
    )
    style.configure(
        "Field.TLabel",
        background=p.surface,
        foreground=p.text,
        font=(FONT_UI, PT_BODY),
    )
    style.configure(
        "Hint.TLabel",
        background=p.surface,
        foreground=p.muted,
        font=(FONT_UI, PT_HINT),
    )
    style.configure(
        "StatusBar.TLabel",
        background=p.surface,
        foreground=p.muted,
        font=(FONT_UI, PT_SMALL, "bold"),
    )
    style.configure(
        "StatusBarOk.TLabel",
        background=p.surface,
        foreground=p.ok_fg,
        font=(FONT_UI, PT_SMALL, "bold"),
    )
    style.configure(
        "StatusBarBusy.TLabel",
        background=p.surface,
        foreground=p.accent,
        font=(FONT_UI, PT_SMALL, "bold"),
    )
    style.configure(
        "StatusBarBad.TLabel",
        background=p.surface,
        foreground=p.danger,
        font=(FONT_UI, PT_SMALL, "bold"),
    )
    style.configure("StatusBar.TFrame", background=p.surface)
    style.configure(
        "App.TSizegrip",
        background=p.surface,
    )
    style.configure(
        "Info.TLabel",
        background=p.surface,
        foreground=p.info,
        font=(FONT_UI, PT_HINT),
    )
    style.configure(
        "Warn.TLabel",
        background=p.surface,
        foreground=p.danger,
        font=(FONT_UI, PT_HINT),
    )
    style.configure(
        "Status.TLabel",
        background=p.bg,
        foreground=p.text,
        font=(FONT_UI, PT_SMALL),
    )
    style.configure(
        "Time.TLabel",
        background=p.bg,
        foreground=p.muted,
        font=(FONT_UI_SEMIBOLD, PT_SMALL),
    )
    style.configure(
        "DisplayToggle.TLabel",
        background=p.surface,
        foreground=p.text,
        font=(FONT_UI_SEMIBOLD, PT_BODY),
    )

    # Recorder QLineEdit: 1px border, 8px radius (not available in Tk), 7px 10px padding.
    entry_kw = dict(
        fieldbackground=p.surface,
        background=p.surface,
        foreground=p.text,
        bordercolor=p.border,
        lightcolor=p.border,
        darkcolor=p.border,
        insertcolor=p.text,
        padding=(sp(10), sp(7)),
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
        padding=(sp(10), sp(6)),
        relief="flat",
        borderwidth=1,
        arrowsize=sp(14),
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
    # Recorder QPushButton: 13px weight 600, 8px 14px padding, 32px tall.
    btn_pad = (sp(14), sp(7))
    style.configure("Browse.TButton", font=(FONT_UI_SEMIBOLD, PT_BODY), padding=btn_pad, **btn_common)
    style.configure("Secondary.TButton", font=(FONT_UI_SEMIBOLD, PT_BODY), padding=btn_pad, **btn_common)
    style.configure("Preset.TButton", font=(FONT_UI, PT_SMALL), padding=(sp(10), sp(5)), **btn_common)
    style.configure(
        "PresetSelected.TButton",
        font=(FONT_UI_SEMIBOLD, PT_SMALL),
        padding=(sp(10), sp(5)),
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
    # Recorder QPushButton#ghost: transparent face, 1px border.
    style.configure(
        "Ghost.TButton",
        font=(FONT_UI_SEMIBOLD, PT_BODY),
        padding=btn_pad,
        background=p.bg,
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
        "Ghost.TButton",
        background=[("disabled", p.bg), ("pressed", p.border), ("active", p.button_hover)],
        foreground=[("disabled", p.muted)],
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

    # Recorder QCheckBox (Fusion): 16px box, 8px gap; teal fill + white tick when checked.
    for name, face, box in (
        ("App.TCheckbutton", p.bg, p.surface),
        ("Surface.TCheckbutton", p.surface, p.bg),
    ):
        style.configure(
            name,
            background=face,
            foreground=p.text,
            font=(FONT_UI, PT_BODY),
            focuscolor=face,
            indicatorsize=sp(16),
            indicatormargin=(0, 0, sp(8), 0),
            indicatorbackground=box,
            indicatorforeground="#FFFFFF",
            upperbordercolor=p.border,
            lowerbordercolor=p.border,
            padding=sp(2),
        )
        style.map(
            name,
            background=[("active", face)],
            foreground=[("disabled", p.muted)],
            indicatorbackground=[
                ("disabled", p.border),
                ("pressed", p.accent_pressed),
                ("selected", p.accent),
                ("!selected", box),
            ],
            indicatorforeground=[("disabled", p.muted), ("selected", "#FFFFFF")],
            upperbordercolor=[("selected", p.accent), ("active", p.accent)],
            lowerbordercolor=[("selected", p.accent), ("active", p.accent)],
        )
        element = _checkbox_indicator_element(style, p, box, sp(16), sp(8))
        if element:
            style.layout(
                name,
                [
                    (
                        "Checkbutton.padding",
                        {
                            "sticky": "nswe",
                            "children": [
                                (element, {"side": "left", "sticky": ""}),
                                (
                                    "Checkbutton.focus",
                                    {
                                        "side": "left",
                                        "sticky": "w",
                                        "children": [("Checkbutton.label", {"sticky": "nswe"})],
                                    },
                                ),
                            ],
                        },
                    )
                ],
            )

    # Recorder QProgressBar: bg trough, 1px border, accent chunk, 12px tall.
    style.configure(
        "App.Horizontal.TProgressbar",
        troughcolor=p.bg,
        background=p.accent,
        bordercolor=p.border,
        lightcolor=p.accent,
        darkcolor=p.accent,
        thickness=sp(12),
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
    """Moon in light mode (go Dark); sun in dark mode (go Light).

    Same geometry as the Recorder's `themeToggle` button: a 36x36 bordered
    square with a 20px glyph, hover = button_hover fill + accent border.
    """

    def __init__(self, master, command, **kwargs):
        scale = ui_scale(master)
        self._side = int(round(36 * scale))
        self._glyph = 20.0 * scale
        kwargs.setdefault("width", self._side)
        kwargs.setdefault("height", self._side)
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
        # Recorder _paint_theme_glyph("sun"): rays 0.30-0.44, disc 0.16 of the glyph size.
        side = float(self.winfo_reqwidth() or self._side)
        cx = cy = side / 2.0
        pixel = self._glyph
        radius = pixel * 0.16
        inner = pixel * 0.30
        outer = pixel * 0.44
        width = max(1.5, pixel * 0.09)
        for index in range(8):
            angle = math.radians(index * 45.0)
            self.create_line(
                cx + inner * math.cos(angle),
                cy + inner * math.sin(angle),
                cx + outer * math.cos(angle),
                cy + outer * math.sin(angle),
                fill=color,
                width=width,
                capstyle=tk.ROUND,
            )
        self.create_oval(cx - radius, cy - radius, cx + radius, cy + radius, fill=color, outline="")

    def _draw_moon(self, color: str, bg: str) -> None:
        # Recorder _paint_theme_glyph("moon"): body 0.28 at (-0.04, 0), cut 0.24 at (+0.12, -0.06).
        side = float(self.winfo_reqwidth() or self._side)
        cx = cy = side / 2.0
        pixel = self._glyph
        radius = pixel * 0.28
        x, y = cx - pixel * 0.04, cy
        self.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline="")
        cr = pixel * 0.24
        x2, y2 = cx + pixel * 0.12, cy - pixel * 0.06
        self.create_oval(x2 - cr, y2 - cr, x2 + cr, y2 + cr, fill=bg, outline="")


class PrimaryButton(tk.Button):
    """Recorder QPushButton#primary: accent fill, white bold 14px text, 38px tall."""

    def __init__(self, master, **kwargs):
        kwargs.setdefault("relief", "flat")
        kwargs.setdefault("bd", 0)
        kwargs.setdefault("cursor", "hand2")
        kwargs.setdefault("font", (FONT_UI, PT_PRIMARY, "bold"))
        kwargs.setdefault("padx", px(master, 18))
        # tk.Button adds its own internal padding; 3px here lands at ~38px tall.
        kwargs.setdefault("pady", px(master, 3))
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


class ThemedDialog(tk.Toplevel):
    """Modal message box drawn with the app palette (the Recorder's QDialog look).

    kind: "info" | "warning" | "error" | "question". buttons: labels left to
    right; the last one is the default (Enter). Escape picks `cancel_label`
    when given, else the first button. `result` holds the pressed label.
    """

    _GLYPH = {"info": "i", "warning": "!", "error": "!", "question": "?"}

    def __init__(
        self,
        parent: tk.Misc,
        title: str,
        message: str,
        *,
        kind: str = "info",
        buttons: tuple[str, ...] = ("OK",),
        cancel_label: str | None = None,
        palette_getter=current_palette,
    ):
        super().__init__(parent)
        p = palette_getter()
        self.result: str | None = None
        self._cancel_label = cancel_label or buttons[0]
        self.withdraw()
        self.title(title)
        self.configure(bg=p.surface, highlightthickness=1, highlightbackground=p.border)
        self.resizable(False, False)
        try:
            self.transient(parent.winfo_toplevel())
        except tk.TclError:
            pass

        s = ui_scale(self)
        body = tk.Frame(self, bg=p.surface, bd=0, highlightthickness=0)
        body.pack(fill="both", expand=True, padx=int(20 * s), pady=(int(18 * s), int(12 * s)))

        glyph_color = {"error": p.danger, "warning": p.danger, "question": p.accent}.get(kind, p.accent)
        size = int(30 * s)
        glyph = tk.Canvas(body, width=size, height=size, bg=p.surface, bd=0, highlightthickness=0)
        glyph.grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, int(14 * s)))
        glyph.create_oval(1, 1, size - 1, size - 1, fill=glyph_color, outline="")
        glyph.create_text(
            size / 2,
            size / 2,
            text=self._GLYPH.get(kind, "i"),
            fill="#FFFFFF",
            font=(FONT_UI, PT_SECTION, "bold"),
        )

        tk.Label(
            body,
            text=title,
            bg=p.surface,
            fg=p.text,
            font=(FONT_UI, PT_SECTION, "bold"),
            anchor="w",
            justify="left",
        ).grid(row=0, column=1, sticky="w")
        tk.Label(
            body,
            text=message,
            bg=p.surface,
            fg=p.text,
            font=(FONT_UI, PT_BODY),
            anchor="w",
            justify="left",
            wraplength=int(440 * s),
        ).grid(row=1, column=1, sticky="w", pady=(int(6 * s), 0))

        row = tk.Frame(self, bg=p.surface, bd=0, highlightthickness=0)
        row.pack(fill="x", padx=int(20 * s), pady=(0, int(16 * s)))
        self._buttons: list[tk.Widget] = []
        for index, label in enumerate(buttons):
            is_default = index == len(buttons) - 1
            if is_default:
                btn = PrimaryButton(
                    row,
                    text=label,
                    command=lambda l=label: self._choose(l),
                    padx=int(18 * s),
                    pady=int(2 * s),
                    font=(FONT_UI, PT_BODY, "bold"),
                )
                btn.apply_palette(p)
            else:
                btn = ttk.Button(
                    row,
                    text=label,
                    style="Secondary.TButton",
                    command=lambda l=label: self._choose(l),
                )
            btn.pack(side="right", padx=(int(8 * s), 0))
            self._buttons.append(btn)

        self.bind("<Return>", lambda e: self._choose(buttons[-1]))
        self.bind("<KP_Enter>", lambda e: self._choose(buttons[-1]))
        self.bind("<Escape>", lambda e: self._choose(self._cancel_label))
        self.protocol("WM_DELETE_WINDOW", lambda: self._choose(self._cancel_label))

        self.update_idletasks()
        self._center_over(parent)
        apply_windows_titlebar(self, dark=(current_mode() == DARK_MODE))
        self.deiconify()
        try:
            self._buttons[0].focus_set()
        except (tk.TclError, IndexError):
            pass

    def _center_over(self, parent: tk.Misc) -> None:
        try:
            top = parent.winfo_toplevel()
            px_, py_ = top.winfo_rootx(), top.winfo_rooty()
            pw, ph = top.winfo_width(), top.winfo_height()
            if pw < 50 or ph < 50:
                raise tk.TclError
        except tk.TclError:
            left, top_, aw, ah = available_workarea(self)
            px_, py_, pw, ph = left, top_, aw, ah
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        left, top_, aw, ah = available_workarea(self)
        x = min(max(left, px_ + (pw - w) // 2), left + aw - w)
        y = min(max(top_, py_ + (ph - h) // 2), top_ + ah - h)
        self.geometry(f"+{x}+{y}")

    def _choose(self, label: str) -> None:
        self.result = label
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()

    def run(self) -> str | None:
        """Block (modal) until a button is pressed; return its label."""
        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.wait_window(self)
        return self.result


def _parent_alive(parent) -> bool:
    try:
        return parent is not None and bool(parent.winfo_exists())
    except tk.TclError:
        return False


def _message(parent, title, message, kind, buttons=("OK",), cancel_label=None):
    if not _parent_alive(parent):
        from tkinter import messagebox

        if kind == "question":
            return "Yes" if messagebox.askyesno(title, message) else "No"
        {"error": messagebox.showerror, "warning": messagebox.showwarning}.get(
            kind, messagebox.showinfo
        )(title, message)
        return "OK"
    dialog = ThemedDialog(
        parent, title, message, kind=kind, buttons=buttons, cancel_label=cancel_label
    )
    return dialog.run()


def show_info(parent, title: str, message: str) -> None:
    _message(parent, title, message, "info")


def show_warning(parent, title: str, message: str) -> None:
    _message(parent, title, message, "warning")


def show_error(parent, title: str, message: str) -> None:
    _message(parent, title, message, "error")


def ask_yes_no(parent, title: str, message: str) -> bool:
    return _message(parent, title, message, "question", buttons=("No", "Yes"), cancel_label="No") == "Yes"
