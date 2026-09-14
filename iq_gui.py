import io
import json
import os
import sys
import threading
import time
import traceback
import tkinter as tk
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from iq_data import estimate_conversion_seconds, process_file, wav_info
from iq_theme import (
    DARK_MODE,
    FONT_LOG,
    FONT_UI,
    LIGHT_MODE,
    PrimaryButton,
    ThemeToggle,
    Tooltip,
    apply_option_db,
    apply_ttk_styles,
    apply_windows_titlebar,
    color_combobox_popdown,
    current_mode,
    current_palette,
    fit_window_to_screen,
    hook_combobox_popdown,
    normalize_theme,
    set_current_mode,
    theme_toggle_tooltip,
)

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover
    Image = None
    ImageTk = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _TkBase = TkinterDnD.Tk
    _HAS_DND = True
except Exception:
    DND_FILES = None
    _TkBase = tk.Tk
    _HAS_DND = False


__version__ = "1.2.0"
APP_NAME = "SENSORZ IQ Spectrogram Converter"


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    """Bundled resources (PyInstaller extracts to _MEIPASS)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
DEFAULT_OUTPUT = APP_DIR / "IQ Results"
DEFAULT_INPUT_DIR = APP_DIR / "IQ Collection"
SETTINGS_NAME = ".iq_gui_settings.json"


def _brand_icon_path(name: str) -> Path:
    """CRFS IQ Recorder app mark, then local assets (frozen builds use bundled assets)."""
    here = Path(__file__).resolve().parent
    roots = []
    if getattr(sys, "frozen", False):
        roots.append(resource_dir() / "assets")
    else:
        roots.append(here / "crfs_iq_recorder" / "assets")
        roots.append(here / "assets")
        roots.append(resource_dir() / "assets")
    for folder in roots:
        path = folder / name
        if path.is_file():
            return path
    return (roots[0] if roots else here / "assets") / name


ICON_PNG_PATH = _brand_icon_path("sensorz_icon.png")
ICON_ICO_PATH = _brand_icon_path("sensorz_icon.ico")

RBW_PRESETS = [
    ("5 kHz", "5000"),
    ("15 kHz", "15000"),
    ("30 kHz", "30000"),
    ("50 kHz", "50000"),
]

STATUS_READY = "Ready"
STATUS_CONVERTING = "Converting…"
STATUS_SAVED = "Image saved"
RBW_TOOLTIP = "Lower values show finer frequency detail. Higher values process faster."


def settings_path() -> Path:
    override = os.environ.get("IQ_GUI_SETTINGS")
    if override:
        return Path(override)
    return APP_DIR / SETTINGS_NAME


def set_app_user_model_id(app_id: str = "Sensorz.IQSpectrogramConverter") -> None:
    """Give this process a unique AppUserModelID so Windows taskbar can use our icon."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def enable_dpi_awareness():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def format_duration(seconds):
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_size(n_bytes):
    n_bytes = float(n_bytes)
    if n_bytes >= 1024 ** 3:
        return f"{n_bytes / (1024 ** 3):.2f} GB"
    if n_bytes >= 1024 ** 2:
        return f"{n_bytes / (1024 ** 2):.1f} MB"
    if n_bytes >= 1024:
        return f"{n_bytes / 1024:.0f} KB"
    return f"{int(n_bytes)} B"


def format_rate(fs):
    if fs >= 1e6:
        return f"{fs / 1e6:g} MHz"
    if fs >= 1e3:
        return f"{fs / 1e3:g} kHz"
    return f"{fs:g} Hz"


def list_wavs(folder):
    folder = Path(folder)
    try:
        return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".wav")
    except OSError:
        return []


def user_error_text(exc):
    msg = str(exc).strip() or exc.__class__.__name__
    low = msg.lower()
    if isinstance(exc, MemoryError) or "unable to allocate" in low or "out of memory" in low:
        return (
            "This file is too large to convert with the current settings.\n\n"
            "Try a higher RBW (for example 30 or 50 kHz), close other apps to free RAM, and try again."
        )
    if isinstance(exc, PermissionError) or "being used by another" in low:
        if "cannot write" in low or "close the png" in low:
            return msg
        return "Cannot save the PNG. Close it if it is open in another app, then try again."
    if "need stereo" in low:
        return "Need stereo IQ (I left, Q right)."
    return msg


class IQConverterGUI(_TkBase):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.resizable(True, True)

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        self.rbw_var = tk.StringVar(value="15000")
        self.cmap_var = tk.StringVar(value="jet")
        self.freq_smooth_var = tk.BooleanVar(value=False)
        self.db_auto_var = tk.BooleanVar(value=True)
        self.db_vmin_var = tk.StringVar(value="")
        self.db_vmax_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value=STATUS_READY)
        self.time_var = tk.StringVar(value="")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.file_info_var = tk.StringVar(value="")
        self.open_when_done_var = tk.BooleanVar(value=True)

        self._logo_photo = None
        self._window_icon_photo = None
        self._preview_photo = None
        self._last_png_path = None
        self._converting = False
        self._closing = False
        self._progress_fraction = 0.0
        self._progress_stage = ""
        self._convert_started_at = 0.0
        self._estimated_total_s = 0.0
        self._timer_job = None
        self._last_display_fraction = 0.0
        self._job_id = 0
        self._open_when_done = True
        self._job_cmap = "jet"
        self._job_freq_smooth = False
        self._job_db_auto = True
        self._job_db_vmin = None
        self._job_db_vmax = None
        self._last_input_dir = str(DEFAULT_INPUT_DIR if DEFAULT_INPUT_DIR.is_dir() else APP_DIR)
        self._busy_widgets = []
        self._preset_buttons = []
        self._cards = []
        self._tooltips = []
        self._display_open = False
        self._style = ttk.Style(self)

        self._load_settings()
        set_current_mode(getattr(self, "_ui_theme", LIGHT_MODE))
        self._set_window_icon()
        # Re-apply after the window is mapped — Windows often locks taskbar icon then.
        self.after_idle(self._set_window_icon)
        self._configure_styles()
        self._build_ui()
        self.apply_theme(current_mode())
        self.rbw_var.trace_add("write", lambda *_: self._on_rbw_changed())
        self.input_var.trace_add("write", lambda *_: self._on_input_changed())
        self.bind("<Return>", self._on_return_key)
        self.bind("<Escape>", self._on_escape_key)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._enable_drag_drop()
        fit_window_to_screen(self, 840, 700, min_width=640, min_height=520)
        self._on_input_changed()

    def theme_mode(self):
        return current_mode()

    def _configure_styles(self):
        apply_ttk_styles(self._style, current_palette())
        apply_option_db(self, current_palette())

    def apply_theme(self, mode=None):
        mode = set_current_mode(mode if mode is not None else current_mode())
        self._ui_theme = mode
        p = current_palette()
        try:
            self.configure(bg=p.bg)
        except tk.TclError:
            return
        apply_ttk_styles(self._style, p)
        apply_option_db(self, p)
        self._recolor_tk(p)
        if hasattr(self, "theme_btn"):
            self.theme_btn.set_state(mode, p)
            tip = theme_toggle_tooltip(mode)
            self.theme_btn.configure(takefocus=1)
            self._set_widget_tooltip(self.theme_btn, tip)
        if hasattr(self, "convert_button"):
            self.convert_button.apply_palette(p)
        if hasattr(self, "cmap_combo"):
            color_combobox_popdown(self.cmap_combo, p)
        self._sync_preset_highlight()
        apply_windows_titlebar(self, dark=(mode == DARK_MODE))

    def _toggle_theme(self):
        next_mode = DARK_MODE if current_mode() != DARK_MODE else LIGHT_MODE
        self.apply_theme(next_mode)
        self._save_settings()

    def _recolor_tk(self, p):
        if getattr(self, "_header", None) is not None:
            try:
                self._header.configure(bg=p.surface)
            except tk.TclError:
                pass
        for frame in (getattr(self, "_header_left", None), getattr(self, "_header_right", None)):
            if frame is not None:
                try:
                    frame.configure(bg=p.surface)
                except tk.TclError:
                    pass
        if getattr(self, "_logo_label", None) is not None:
            try:
                self._logo_label.configure(bg=p.surface)
            except tk.TclError:
                pass
        if getattr(self, "_title_label", None) is not None:
            try:
                self._title_label.configure(bg=p.surface, fg=p.text)
            except tk.TclError:
                pass
        if getattr(self, "_version_label", None) is not None:
            try:
                self._version_label.configure(bg=p.surface, fg=p.muted)
            except tk.TclError:
                pass
        if getattr(self, "_header_rule", None) is not None:
            try:
                self._header_rule.configure(bg=p.border)
            except tk.TclError:
                pass
        if getattr(self, "_display_toggle", None) is not None:
            try:
                self._display_toggle.configure(bg=p.surface, fg=p.text)
            except tk.TclError:
                pass
        for card in self._cards:
            try:
                card.configure(bg=p.surface, highlightbackground=p.border, highlightcolor=p.border)
            except tk.TclError:
                pass
        if getattr(self, "log", None) is not None:
            try:
                self.log.configure(
                    bg=p.log_bg,
                    fg=p.log_text,
                    insertbackground=p.log_text,
                    highlightbackground=p.border,
                    highlightcolor=p.focus,
                    selectbackground=p.accent,
                    selectforeground="#FFFFFF",
                )
            except tk.TclError:
                pass
        if getattr(self, "preview_wrap", None) is not None:
            try:
                self.preview_wrap.configure(bg=p.surface)
            except tk.TclError:
                pass
        if getattr(self, "preview_label", None) is not None:
            try:
                self.preview_label.configure(
                    bg=p.bg,
                    fg=p.muted,
                    highlightbackground=p.border,
                )
            except tk.TclError:
                pass

    def _set_widget_tooltip(self, widget, text):
        existing = getattr(widget, "_iq_tooltip", None)
        if existing is not None:
            existing.text = text
            return
        tip = Tooltip(widget, text)
        widget._iq_tooltip = tip  # type: ignore[attr-defined]
        self._tooltips.append(tip)

    def _make_card(self, parent):
        p = current_palette()
        card = tk.Frame(
            parent,
            bg=p.surface,
            highlightthickness=1,
            highlightbackground=p.border,
            highlightcolor=p.border,
            bd=0,
        )
        self._cards.append(card)
        return card

    def _set_window_icon(self):
        """Set taskbar/window icon (Windows prefers .ico via iconbitmap)."""
        if not self._alive():
            return
        try:
            if ICON_ICO_PATH.is_file():
                self.iconbitmap(default=str(ICON_ICO_PATH))
                self.iconbitmap(str(ICON_ICO_PATH))
        except Exception:
            pass
        try:
            if Image is not None and ImageTk is not None and ICON_PNG_PATH.is_file():
                img = Image.open(ICON_PNG_PATH)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                # Multiple sizes help Windows pick a crisp taskbar glyph
                photos = []
                for side in (16, 32, 48, 64):
                    resized = img.resize((side, side), Image.Resampling.LANCZOS)
                    photos.append(ImageTk.PhotoImage(resized))
                if photos:
                    self._window_icon_photo = photos  # keep refs alive
                    self.iconphoto(True, *photos)
            elif ICON_PNG_PATH.is_file():
                self._window_icon_photo = tk.PhotoImage(file=str(ICON_PNG_PATH))
                self.iconphoto(True, self._window_icon_photo)
        except Exception:
            pass

    def _load_logo(self, max_height=32):
        """Load Sensorz icon mark for the header (top-left brand)."""
        if not ICON_PNG_PATH.is_file():
            return None
        try:
            if Image is not None and ImageTk is not None:
                img = Image.open(ICON_PNG_PATH)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                ratio = max_height / img.height
                size = (max(1, int(img.width * ratio)), max_height)
                img = img.resize(size, Image.Resampling.LANCZOS)
                self._logo_photo = ImageTk.PhotoImage(img)
                return self._logo_photo
            self._logo_photo = tk.PhotoImage(file=str(ICON_PNG_PATH))
            while self._logo_photo.height() > max_height * 2:
                self._logo_photo = self._logo_photo.subsample(2, 2)
            return self._logo_photo
        except Exception:
            return None

    def _settings_path(self):
        return settings_path()

    def _load_settings(self):
        self._ui_theme = LIGHT_MODE
        try:
            data = json.loads(self._settings_path().read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        out = data.get("output_dir")
        if isinstance(out, str) and out.strip():
            self.output_var.set(out.strip())
        rbw = data.get("rbw")
        if rbw is not None:
            try:
                if float(rbw) > 0:
                    self.rbw_var.set(str(rbw).strip())
            except (TypeError, ValueError):
                pass
        indir = data.get("input_dir")
        if isinstance(indir, str) and Path(indir).is_dir():
            self._last_input_dir = indir
        open_done = data.get("open_when_done")
        if isinstance(open_done, bool):
            self.open_when_done_var.set(open_done)
        cmap = data.get("cmap")
        if isinstance(cmap, str) and cmap.strip().lower() in ("jet", "turbo", "viridis", "gray"):
            self.cmap_var.set(cmap.strip().lower())
        freq_smooth = data.get("freq_smooth")
        if isinstance(freq_smooth, bool):
            self.freq_smooth_var.set(freq_smooth)
        db_auto = data.get("db_auto")
        if isinstance(db_auto, bool):
            self.db_auto_var.set(db_auto)
        db_vmin = data.get("db_vmin")
        if isinstance(db_vmin, str):
            self.db_vmin_var.set(db_vmin)
        elif isinstance(db_vmin, (int, float)):
            self.db_vmin_var.set(str(db_vmin))
        db_vmax = data.get("db_vmax")
        if isinstance(db_vmax, str):
            self.db_vmax_var.set(db_vmax)
        elif isinstance(db_vmax, (int, float)):
            self.db_vmax_var.set(str(db_vmax))
        theme = data.get("ui_theme")
        if isinstance(theme, str):
            self._ui_theme = normalize_theme(theme)

    def _save_settings(self):
        payload = {
            "input_dir": self._last_input_dir,
            "output_dir": self.output_var.get().strip(),
            "rbw": self.rbw_var.get().strip(),
            "open_when_done": bool(self.open_when_done_var.get()),
            "cmap": self.cmap_var.get().strip().lower(),
            "freq_smooth": bool(self.freq_smooth_var.get()),
            "db_auto": bool(self.db_auto_var.get()),
            "db_vmin": self.db_vmin_var.get().strip(),
            "db_vmax": self.db_vmax_var.get().strip(),
            "ui_theme": normalize_theme(getattr(self, "_ui_theme", current_mode())),
        }
        try:
            path = self._settings_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except OSError:
            pass

    def _build_ui(self):
        p = current_palette()

        self._header = tk.Frame(self, bg=p.surface, bd=0, highlightthickness=0)
        self._header.pack(fill="x")
        self._header_left = tk.Frame(self._header, bg=p.surface)
        self._header_left.pack(side="left", padx=(20, 12), pady=10)
        self._header_right = tk.Frame(self._header, bg=p.surface)
        self._header_right.pack(side="right", padx=(8, 16), pady=10)

        logo = self._load_logo()
        if logo is not None:
            self._logo_label = tk.Label(
                self._header_left, image=logo, bg=p.surface, bd=0, highlightthickness=0
            )
            self._logo_label.pack(side="left", padx=(0, 12))
        else:
            self._logo_label = None

        self._title_label = tk.Label(
            self._header_left,
            text=APP_NAME,
            bg=p.surface,
            fg=p.text,
            font=("Segoe UI Semibold", 14),
            anchor="w",
        )
        self._title_label.pack(side="left")

        self.theme_btn = ThemeToggle(self._header_right, command=self._toggle_theme)
        self.theme_btn.pack(side="right")
        self._version_label = tk.Label(
            self._header_right,
            text=f"v{__version__}",
            bg=p.surface,
            fg=p.muted,
            font=(FONT_UI, 9),
        )
        self._version_label.pack(side="right", padx=(0, 10))

        self._header_rule = tk.Frame(self, bg=p.border, height=1, bd=0, highlightthickness=0)
        self._header_rule.pack(fill="x")

        body = ttk.Frame(self, style="App.TFrame", padding=(20, 14, 20, 12))
        body.pack(fill="both", expand=True)

        files_card = self._make_card(body)
        files_card.pack(fill="x", pady=(0, 10))
        files = ttk.Frame(files_card, style="Surface.TFrame", padding=14)
        files.pack(fill="x")
        files.columnconfigure(0, weight=1)

        ttk.Label(files, text="Choose file", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        self.input_entry = ttk.Entry(files, textvariable=self.input_var, style="App.TEntry")
        self.input_entry.grid(row=1, column=0, sticky="ew")
        self.browse_input_btn = ttk.Button(
            files, text="Browse", style="Browse.TButton", command=self.choose_input
        )
        self.browse_input_btn.grid(row=1, column=1, sticky="e", padx=(10, 0))

        self.file_info_label = ttk.Label(
            files, textvariable=self.file_info_var, style="Hint.TLabel"
        )
        self.file_info_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 10))

        ttk.Label(files, text="Output folder", style="Field.TLabel").grid(
            row=3, column=0, sticky="w", pady=(0, 4)
        )
        self.output_entry = ttk.Entry(files, textvariable=self.output_var, style="App.TEntry")
        self.output_entry.grid(row=4, column=0, sticky="ew")
        self.browse_output_btn = ttk.Button(
            files, text="Browse", style="Browse.TButton", command=self.choose_output
        )
        self.browse_output_btn.grid(row=4, column=1, sticky="e", padx=(10, 0))

        rbw_card = self._make_card(body)
        rbw_card.pack(fill="x", pady=(0, 10))
        params = ttk.Frame(rbw_card, style="Surface.TFrame", padding=14)
        params.pack(fill="x")
        params.columnconfigure(0, weight=1)

        rbw_row = ttk.Frame(params, style="Surface.TFrame")
        rbw_row.grid(row=0, column=0, sticky="ew")
        rbw_label = ttk.Label(rbw_row, text="RBW", style="Field.TLabel")
        rbw_label.pack(side="left")
        self.rbw_entry = ttk.Entry(rbw_row, textvariable=self.rbw_var, width=12, style="App.TEntry")
        self.rbw_entry.pack(side="left", padx=(10, 6))
        ttk.Label(rbw_row, text="Hz", style="Hint.TLabel").pack(side="left", padx=(0, 14))
        self._preset_buttons = []
        for label, value in RBW_PRESETS:
            btn = ttk.Button(
                rbw_row,
                text=label,
                style="Preset.TButton",
                command=lambda v=value: self.rbw_var.set(v),
            )
            btn.pack(side="left", padx=3)
            self._preset_buttons.append(btn)

        display_card = self._make_card(body)
        display_card.pack(fill="x", pady=(0, 10))
        d_inner = ttk.Frame(display_card, style="Surface.TFrame", padding=(12, 8))
        d_inner.pack(fill="x")

        d_head = ttk.Frame(d_inner, style="Surface.TFrame")
        d_head.pack(fill="x")
        self._display_toggle = tk.Label(
            d_head,
            text="▸  Display",
            bg=p.surface,
            fg=p.text,
            font=("Segoe UI Semibold", 10),
            cursor="hand2",
        )
        self._display_toggle.pack(side="left")
        self._display_toggle.bind("<Button-1>", lambda e: self._toggle_display())
        ttk.Label(d_head, text="advanced", style="Hint.TLabel").pack(side="left", padx=(8, 0))

        self._display_body = ttk.Frame(d_inner, style="Surface.TFrame")

        display_row = ttk.Frame(self._display_body, style="Surface.TFrame")
        display_row.pack(fill="x", pady=(10, 0))

        cmap_label = ttk.Label(display_row, text="Colormap", style="Field.TLabel")
        cmap_label.pack(side="left")
        self.cmap_combo = ttk.Combobox(
            display_row,
            textvariable=self.cmap_var,
            values=("jet", "turbo", "viridis", "gray"),
            state="readonly",
            width=10,
            style="App.TCombobox",
        )
        self.cmap_combo.pack(side="left", padx=(8, 16))
        self.cmap_combo.bind("<<ComboboxSelected>>", lambda *_: self._save_settings())
        hook_combobox_popdown(self.cmap_combo, current_palette)

        self.freq_smooth_check = ttk.Checkbutton(
            display_row,
            text="Frequency smooth",
            variable=self.freq_smooth_var,
            style="Surface.TCheckbutton",
            command=self._save_settings,
        )
        self.freq_smooth_check.pack(side="left", padx=(0, 16))

        self.db_auto_check = ttk.Checkbutton(
            display_row,
            text="Auto dB",
            variable=self.db_auto_var,
            style="Surface.TCheckbutton",
            command=self._on_db_auto_changed,
        )
        self.db_auto_check.pack(side="left")

        db_row = ttk.Frame(self._display_body, style="Surface.TFrame")
        db_row.pack(fill="x", pady=(10, 0))
        db_min_label = ttk.Label(db_row, text="dB min", style="Field.TLabel")
        db_min_label.pack(side="left")
        self.db_vmin_entry = ttk.Entry(db_row, textvariable=self.db_vmin_var, width=8, style="App.TEntry")
        self.db_vmin_entry.pack(side="left", padx=(8, 14))
        db_max_label = ttk.Label(db_row, text="dB max", style="Field.TLabel")
        db_max_label.pack(side="left")
        self.db_vmax_entry = ttk.Entry(db_row, textvariable=self.db_vmax_var, width=8, style="App.TEntry")
        self.db_vmax_entry.pack(side="left", padx=(8, 14))

        extra_row = ttk.Frame(self._display_body, style="Surface.TFrame")
        extra_row.pack(fill="x", pady=(10, 0))
        self.open_check = ttk.Checkbutton(
            extra_row,
            text="Open when done",
            variable=self.open_when_done_var,
            style="Surface.TCheckbutton",
            command=self._save_settings,
        )
        self.open_check.pack(side="left")

        actions = ttk.Frame(body, style="App.TFrame")
        actions.pack(fill="x", pady=(2, 10))

        self.convert_button = PrimaryButton(actions, text="Convert", command=self.start_conversion)
        self.convert_button.pack(side="left")

        self.open_last_btn = ttk.Button(
            actions,
            text="Open image",
            style="Secondary.TButton",
            command=self.open_last_image,
            state="disabled",
        )
        self.open_last_btn.pack(side="left", padx=(10, 0))

        self.open_folder_btn = ttk.Button(
            actions,
            text="Open output folder",
            style="Secondary.TButton",
            command=self.open_output_folder,
        )
        self.open_folder_btn.pack(side="left", padx=(10, 0))

        self.folder_button = ttk.Button(
            actions,
            text="Convert folder",
            style="Secondary.TButton",
            command=self.start_folder_conversion,
        )
        self.folder_button.pack(side="left", padx=(10, 0))

        progress_header = ttk.Frame(body, style="App.TFrame")
        progress_header.pack(fill="x", pady=(4, 4))
        ttk.Label(progress_header, textvariable=self.status_var, style="Status.TLabel").pack(
            side="left", anchor="w"
        )
        ttk.Label(progress_header, textvariable=self.time_var, style="Time.TLabel").pack(
            side="right", anchor="e"
        )

        self.progress = ttk.Progressbar(
            body,
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
            style="App.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(0, 10))

        log_frame = ttk.LabelFrame(body, text="  Activity  ", style="Log.TLabelframe", padding=8)
        log_frame.pack(fill="both", expand=True)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        log_wrap = ttk.Frame(log_frame, style="Surface.TFrame")
        log_wrap.grid(row=0, column=0, sticky="nsew")
        log_wrap.columnconfigure(0, weight=1)
        log_wrap.rowconfigure(0, weight=1)

        self.log = tk.Text(
            log_wrap,
            height=5,
            wrap="word",
            bg=p.log_bg,
            fg=p.log_text,
            insertbackground=p.log_text,
            relief="flat",
            font=(FONT_LOG, 8),
            padx=8,
            pady=6,
            state="disabled",
            highlightthickness=1,
            highlightbackground=p.border,
            highlightcolor=p.focus,
        )
        self.log.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(log_wrap, command=self.log.yview, style="App.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        self.preview_wrap = tk.Frame(log_frame, bg=p.surface, width=200)
        self.preview_wrap.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        self.preview_wrap.grid_propagate(False)
        ttk.Label(self.preview_wrap, text="Last image", style="Field.TLabel").pack(anchor="w")
        self.preview_label = tk.Label(
            self.preview_wrap,
            text="No preview yet",
            bg=p.bg,
            fg=p.muted,
            font=(FONT_UI, 8),
            width=24,
            height=6,
            bd=0,
            highlightthickness=1,
            highlightbackground=p.border,
        )
        self.preview_label.pack(fill="both", expand=True, pady=(6, 0))

        self._busy_widgets = [
            self.convert_button,
            self.folder_button,
            self.browse_input_btn,
            self.browse_output_btn,
            self.open_check,
            self.cmap_combo,
            self.freq_smooth_check,
            self.db_auto_check,
            *self._preset_buttons,
        ]

        self._set_widget_tooltip(rbw_label, RBW_TOOLTIP)
        self._set_widget_tooltip(self.rbw_entry, RBW_TOOLTIP)
        for btn in self._preset_buttons:
            self._set_widget_tooltip(btn, RBW_TOOLTIP)
        self._set_widget_tooltip(cmap_label, "Color mapping for the spectrogram.")
        self._set_widget_tooltip(self.cmap_combo, "Color mapping for the spectrogram.")
        self._set_widget_tooltip(
            self.freq_smooth_check, "Smooths neighbouring frequency bins for a cleaner look."
        )
        self._set_widget_tooltip(
            self.db_auto_check, "Fit the colour scale to this recording automatically."
        )
        self._set_widget_tooltip(db_min_label, "Lower end of the colour scale, in dB.")
        self._set_widget_tooltip(db_max_label, "Upper end of the colour scale, in dB.")
        self._set_widget_tooltip(self.db_vmin_entry, "Lower end of the colour scale, in dB.")
        self._set_widget_tooltip(self.db_vmax_entry, "Upper end of the colour scale, in dB.")
        self._set_widget_tooltip(
            self.open_check, "Open the PNG in the default viewer when conversion finishes."
        )
        self._set_widget_tooltip(self._display_toggle, "Colormap, dB scale, and open-when-done.")
        self._set_widget_tooltip(self.theme_btn, theme_toggle_tooltip())

        ready = "Ready. Choose a stereo IQ WAV (I on left, Q on right). Enter converts when a file is selected."
        if _HAS_DND:
            ready += " You can also drop a file or folder onto this window."
        self._append_log(ready)
        self._apply_db_entry_state()
        self._sync_preset_highlight()

    def _toggle_display(self):
        self._display_open = not self._display_open
        if self._display_open:
            self._display_body.pack(fill="x")
            self._display_toggle.configure(text="▾  Display")
        else:
            self._display_body.pack_forget()
            self._display_toggle.configure(text="▸  Display")

    def _sync_preset_highlight(self):
        current = self.rbw_var.get().strip()
        for btn, (_label, value) in zip(self._preset_buttons, RBW_PRESETS):
            try:
                btn.configure(style="PresetSelected.TButton" if value == current else "Preset.TButton")
            except tk.TclError:
                pass

    def _enable_drag_drop(self):
        if not _HAS_DND:
            return
        try:
            self.drop_target_register(DND_FILES)
            self.dnd_bind("<<Drop>>", self._on_drop)
        except Exception:
            pass

    def _alive(self):
        try:
            return bool(self.winfo_exists())
        except tk.TclError:
            return False

    def _safe_after(self, func, *args):
        if self._closing or not self._alive():
            return
        try:
            self.after(0, self._run_if_alive, func, *args)
        except tk.TclError:
            pass

    def _run_if_alive(self, func, *args):
        if self._closing or not self._alive():
            return
        try:
            func(*args)
        except tk.TclError:
            pass

    def _append_log(self, message):
        if not self._alive():
            return
        try:
            self.log.configure(state="normal")
            self.log.insert("end", message.rstrip() + "\n")
            self.log.see("end")
            self.log.configure(state="disabled")
        except tk.TclError:
            pass

    def _set_busy(self, busy):
        state = "disabled" if busy else "normal"
        for widget in self._busy_widgets:
            try:
                if widget is getattr(self, "cmap_combo", None):
                    widget.configure(state="disabled" if busy else "readonly")
                else:
                    widget.configure(state=state)
            except tk.TclError:
                pass
        if hasattr(self, "convert_button"):
            try:
                self.convert_button.apply_palette(current_palette())
            except tk.TclError:
                pass
        entry_state = "disabled" if busy else "normal"
        for entry in (self.input_entry, self.output_entry, self.rbw_entry):
            try:
                entry.configure(state=entry_state)
            except tk.TclError:
                pass
        self._apply_db_entry_state()

    def _apply_db_entry_state(self):
        if self._converting or self.db_auto_var.get():
            state = "disabled"
        else:
            state = "normal"
        for entry in (getattr(self, "db_vmin_entry", None), getattr(self, "db_vmax_entry", None)):
            if entry is None:
                continue
            try:
                entry.configure(state=state)
            except tk.TclError:
                pass

    def _on_db_auto_changed(self):
        self._apply_db_entry_state()
        self._save_settings()

    def _on_return_key(self, event):
        if self._converting or self._closing:
            return "break"
        widget = event.widget
        if isinstance(widget, (tk.Text, ttk.Combobox)):
            return None
        path = Path(self.input_var.get().strip())
        if path.is_file() and path.suffix.lower() == ".wav":
            self.start_conversion()
            return "break"
        return None

    def _on_escape_key(self, event):
        # Escape must not quit the app.
        try:
            self.focus_set()
        except tk.TclError:
            pass
        return "break"

    def _on_close(self):
        self._closing = True
        self._converting = False
        self._job_id += 1
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except tk.TclError:
                pass
            self._timer_job = None
        self._save_settings()
        self.destroy()

    def _on_rbw_changed(self):
        self._sync_preset_highlight()
        if self._converting:
            return
        self._update_estimate_preview()

    def _on_input_changed(self):
        if self._converting:
            return
        self._update_file_info()
        self._update_estimate_preview()

    def _update_file_info(self):
        path = Path(self.input_var.get().strip())
        if not path.is_file():
            self.file_info_var.set("Drop or browse a stereo IQ WAV to see sample rate, duration, and size.")
            self.file_info_label.configure(style="Hint.TLabel")
            return
        try:
            info = wav_info(str(path))
        except Exception as exc:
            self.file_info_var.set(f"Could not read WAV header: {exc}")
            self.file_info_label.configure(style="Warn.TLabel")
            return

        channels = info["channels"]
        if channels == 2:
            ch_txt = "stereo"
        elif channels == 1:
            ch_txt = "mono"
        else:
            ch_txt = f"{channels} channels"

        duration = info["duration"]
        dur_txt = f"{duration * 1000:.1f} ms" if duration < 1 else f"{duration:.3f} s"
        bits = [
            ch_txt,
            format_rate(info["samplerate"]),
            dur_txt,
            format_size(info["size"]),
        ]
        if channels < 2:
            self.file_info_var.set(
                f"Need stereo IQ (I left, Q right) — this file is {ch_txt}.  ·  "
                + "  ·  ".join(bits[1:])
            )
            self.file_info_label.configure(style="Warn.TLabel")
            return
        self.file_info_var.set("  ·  ".join(bits))
        self.file_info_label.configure(style="Info.TLabel")

    def choose_input(self):
        initial = self._last_input_dir
        if not Path(initial).is_dir():
            initial = str(DEFAULT_INPUT_DIR if DEFAULT_INPUT_DIR.is_dir() else APP_DIR)
        selected = filedialog.askopenfilename(
            title="Choose an IQ WAV file",
            initialdir=str(initial),
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if selected:
            self._set_input_file(Path(selected))

    def choose_output(self):
        initial = self.output_var.get().strip() or str(DEFAULT_OUTPUT)
        selected = filedialog.askdirectory(title="Choose output folder", initialdir=initial)
        if selected:
            self.output_var.set(selected)
            self._append_log(f"Output folder: {selected}")
            self._save_settings()

    def _set_input_file(self, path):
        path = Path(path)
        self.input_var.set(str(path))
        if path.parent.is_dir():
            self._last_input_dir = str(path.parent)
        self.status_var.set(STATUS_READY)
        self._append_log(f"Input: {path}")
        self._save_settings()

    def _on_drop(self, event):
        if self._converting:
            return
        try:
            raw_paths = self.tk.splitlist(event.data)
        except Exception:
            return
        paths = [Path(p) for p in raw_paths]
        wavs = [p for p in paths if p.is_file() and p.suffix.lower() == ".wav"]
        dirs = [p for p in paths if p.is_dir()]

        if len(wavs) == 1 and not dirs:
            self._set_input_file(wavs[0])
            return
        if len(wavs) > 1:
            names = "\n".join(p.name for p in wavs[:8])
            extra = f"\n… and {len(wavs) - 8} more" if len(wavs) > 8 else ""
            if messagebox.askyesno(
                "Convert dropped files",
                f"Convert {len(wavs)} WAV files?\n\n{names}{extra}",
            ):
                self._start_jobs(wavs)
            return
        if len(dirs) == 1:
            folder = dirs[0]
            found = list_wavs(folder)
            if not found:
                messagebox.showinfo("No WAV files", f"No .wav files in:\n{folder}")
                return
            if messagebox.askyesno(
                "Convert folder",
                f"Convert {len(found)} WAV file(s) in:\n{folder}?",
            ):
                self._last_input_dir = str(folder)
                self._start_jobs(found)
            return

    def _update_estimate_preview(self):
        if self._converting:
            return
        path = Path(self.input_var.get().strip())
        if not path.is_file():
            self.time_var.set("")
            return
        try:
            rbw = float(self.rbw_var.get())
            if rbw <= 0:
                raise ValueError
            est = estimate_conversion_seconds(str(path), rbw)
            self.time_var.set(f"Est. ~{format_duration(est)}")
        except Exception:
            self.time_var.set("")

    def _parse_rbw(self):
        try:
            rbw = float(self.rbw_var.get())
            if rbw <= 0:
                raise ValueError
            return rbw
        except ValueError:
            messagebox.showerror("Invalid RBW", "RBW must be a positive number.")
            return None

    def _parse_optional_db(self, raw, label):
        text = str(raw).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            messagebox.showerror("Invalid dB range", f"{label} must be a number.")
            return False

    def _parse_display_options(self):
        cmap = (self.cmap_var.get() or "jet").strip().lower()
        if cmap not in ("jet", "turbo", "viridis", "gray"):
            cmap = "jet"
        freq_smooth = bool(self.freq_smooth_var.get())
        db_auto = bool(self.db_auto_var.get())
        db_vmin = None
        db_vmax = None
        if not db_auto:
            db_vmin = self._parse_optional_db(self.db_vmin_var.get(), "dB min")
            if db_vmin is False:
                return None
            db_vmax = self._parse_optional_db(self.db_vmax_var.get(), "dB max")
            if db_vmax is False:
                return None
            if db_vmin is not None and db_vmax is not None and db_vmax <= db_vmin:
                messagebox.showerror("Invalid dB range", "dB max must be greater than dB min.")
                return None
        return cmap, freq_smooth, db_auto, db_vmin, db_vmax

    def _output_dir(self):
        raw = self.output_var.get().strip()
        return Path(raw) if raw else DEFAULT_OUTPUT

    def start_conversion(self):
        input_path = Path(self.input_var.get().strip())
        if not input_path.is_file():
            messagebox.showerror("Invalid file", "Please choose an existing WAV file.")
            return
        if input_path.suffix.lower() != ".wav":
            messagebox.showerror("Invalid file", "The selected input must be a .wav file.")
            return
        try:
            info = wav_info(str(input_path))
        except Exception as exc:
            messagebox.showerror(
                "Invalid WAV",
                f"Could not read this WAV file (it may be truncated or invalid):\n{exc}",
            )
            return
        if info["channels"] < 2:
            messagebox.showerror("Need stereo IQ (I left, Q right)", "Need stereo IQ (I left, Q right).")
            return
        if info["frames"] < 1:
            messagebox.showerror("Invalid WAV", "WAV file has no samples.")
            return
        self._start_jobs([input_path])

    def start_folder_conversion(self):
        initial = self._last_input_dir
        if not Path(initial).is_dir():
            initial = str(DEFAULT_INPUT_DIR if DEFAULT_INPUT_DIR.is_dir() else APP_DIR)
        selected = filedialog.askdirectory(title="Choose a folder of IQ WAV files", initialdir=initial)
        if not selected:
            return
        folder = Path(selected)
        wavs = list_wavs(folder)
        if not wavs:
            messagebox.showinfo("No WAV files", f"No .wav files in:\n{folder}")
            return
        self._last_input_dir = str(folder)
        self._save_settings()
        self._start_jobs(wavs)

    def _start_jobs(self, wav_paths):
        if self._converting or self._closing:
            return
        wav_paths = [Path(p) for p in wav_paths]
        if not wav_paths:
            messagebox.showerror("No files", "No WAV files to convert.")
            return

        rbw = self._parse_rbw()
        if rbw is None:
            return

        display = self._parse_display_options()
        if display is None:
            return
        cmap, freq_smooth, db_auto, db_vmin, db_vmax = display

        output_dir = self._output_dir()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "Output folder",
                f"Cannot create or write to the output folder:\n{output_dir}\n\n{exc}",
            )
            return

        estimated = 0.0
        for path in wav_paths:
            try:
                estimated += estimate_conversion_seconds(str(path), rbw)
            except Exception:
                estimated += 30.0
        estimated = max(2.0, estimated)

        self._job_id += 1
        job_id = self._job_id
        self._converting = True
        self._progress_fraction = 0.0
        self._last_display_fraction = 0.0
        self._progress_stage = "Starting…"
        self._convert_started_at = time.monotonic()
        self._estimated_total_s = estimated
        self._open_when_done = bool(self.open_when_done_var.get()) and len(wav_paths) == 1
        self._job_cmap = cmap
        self._job_freq_smooth = freq_smooth
        self._job_db_auto = db_auto
        self._job_db_vmin = db_vmin
        self._job_db_vmax = db_vmax
        self.progress_var.set(0)
        self._set_busy(True)
        self.status_var.set(STATUS_CONVERTING)
        self.time_var.set(f"Elapsed 0:00 · Est. {format_duration(estimated)} remaining")
        self._append_log("-" * 48)
        if len(wav_paths) == 1:
            self._append_log(f"Starting conversion: {wav_paths[0].name}")
        else:
            self._append_log(f"Batch conversion: {len(wav_paths)} WAV files")
        self._append_log(f"RBW = {rbw:g} Hz → {output_dir}")
        self._append_log(
            f"Display: {cmap}  ·  freq smooth {'on' if freq_smooth else 'off'}  ·  "
            + ("auto dB" if db_auto else "manual dB")
        )
        self._append_log(f"Estimated time: ~{format_duration(estimated)}")
        self._save_settings()
        self._schedule_timer()

        thread = threading.Thread(
            target=self._convert_worker,
            args=(wav_paths, output_dir, rbw, job_id),
            daemon=True,
        )
        thread.start()

    def _schedule_timer(self):
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except tk.TclError:
                pass
        self._tick_timer()

    def _tick_timer(self):
        if not self._converting or self._closing or not self._alive():
            self._timer_job = None
            return

        elapsed = time.monotonic() - self._convert_started_at
        fraction = self._progress_fraction

        display_fraction = max(self._last_display_fraction, fraction)
        if fraction < 0.05 and self._estimated_total_s > 0:
            synthetic = min(0.04, elapsed / self._estimated_total_s)
            display_fraction = max(display_fraction, synthetic)
        if fraction >= 1.0:
            display_fraction = 1.0
        self._last_display_fraction = display_fraction
        self.progress_var.set(display_fraction * 100)

        if fraction >= 0.995:
            remaining = 0.0
        elif fraction >= 0.08:
            projected_total = elapsed / max(fraction, 1e-6)
            remaining = max(1.0, projected_total - elapsed)
        else:
            remaining = max(1.0, self._estimated_total_s - elapsed)

        self.status_var.set(STATUS_CONVERTING)
        stage = self._progress_stage or "Working…"
        self.time_var.set(
            f"{stage} · Elapsed {format_duration(elapsed)} · Est. {format_duration(remaining)} remaining"
        )

        try:
            self._timer_job = self.after(200, self._tick_timer)
        except tk.TclError:
            self._timer_job = None

    def _on_progress(self, fraction, message):
        fraction = max(0.0, min(1.0, float(fraction)))
        self._progress_fraction = max(self._progress_fraction, fraction)
        self._progress_stage = message

    def _convert_worker(self, wav_paths, output_dir, rbw, job_id):
        ok = []
        failed = []
        n = len(wav_paths)

        for i, input_path in enumerate(wav_paths):
            if self._closing or job_id != self._job_id:
                return

            buffer = io.StringIO()

            def cb(frac, msg, index=i, name=input_path.name):
                if self._closing or job_id != self._job_id:
                    return
                overall = (index + max(0.0, min(1.0, frac))) / n
                if n == 1:
                    stage = msg
                else:
                    stage = f"File {index + 1} of {n} — {name}: {msg}"
                self._safe_after(self._on_progress, overall, stage)

            try:
                with redirect_stdout(buffer), redirect_stderr(buffer):
                    process_file(
                        str(input_path),
                        str(output_dir),
                        rbw,
                        progress_callback=cb,
                        cmap=self._job_cmap,
                        freq_smooth=self._job_freq_smooth,
                        db_auto=self._job_db_auto,
                        db_vmin=self._job_db_vmin,
                        db_vmax=self._job_db_vmax,
                    )

                details = buffer.getvalue().strip()
                if details:
                    self._safe_after(self._append_log, details)

                png_path = output_dir / f"{input_path.stem}.png"
                if not png_path.is_file():
                    failed.append((input_path.name, f"Expected image was not found:\n{png_path}"))
                    self._safe_after(
                        self._append_log,
                        f"ERROR {input_path.name}: PNG was not created",
                    )
                    continue

                ok.append(png_path)

            except Exception as exc:
                details = buffer.getvalue().strip()
                tech = traceback.format_exc()
                if details:
                    tech = f"{details}\n{tech}"
                user_message = user_error_text(exc)
                failed.append((input_path.name, user_message))
                self._safe_after(self._append_log, f"ERROR {input_path.name}: {user_message}")
                self._safe_after(self._append_log, tech)

        if self._closing or job_id != self._job_id:
            return
        self._safe_after(self._conversion_finished, ok, failed)

    def _stop_timer(self):
        self._converting = False
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except tk.TclError:
                pass
            self._timer_job = None

    def _conversion_finished(self, ok, failed):
        if not self._alive():
            return
        elapsed = time.monotonic() - self._convert_started_at
        estimated = self._estimated_total_s
        self._stop_timer()
        self._set_busy(False)
        self._save_settings()

        timing = self._format_run_timing(elapsed, estimated)
        if ok:
            self._last_png_path = ok[-1]
            self._set_preview(ok[-1])
            try:
                self.open_last_btn.configure(state="normal")
            except tk.TclError:
                pass

        if ok and not failed:
            self.progress_var.set(100)
            if len(ok) == 1:
                png_path = ok[0]
                self.status_var.set(f"{STATUS_SAVED} — {png_path.name}")
                self.time_var.set(timing)
                self._append_log(f"Saved spectrum + spectrogram: {png_path}")
                self._append_log(timing)
                if self._open_when_done:
                    try:
                        os.startfile(png_path)
                    except OSError as exc:
                        messagebox.showwarning(
                            "Image created",
                            f"The PNG was created, but Windows could not open it automatically.\n\n{exc}",
                        )
            else:
                self.status_var.set(f"{STATUS_SAVED} — {len(ok)} files")
                self.time_var.set(timing)
                for png_path in ok:
                    self._append_log(f"Saved spectrum + spectrogram: {png_path}")
                self._append_log(f"Batch complete: {len(ok)} files. {timing}")
                messagebox.showinfo(
                    "Batch complete",
                    f"Converted {len(ok)} files to:\n{ok[0].parent}",
                )
            return

        if ok and failed:
            self.progress_var.set(100)
            self.status_var.set(f"{STATUS_SAVED} — {len(ok)} ok, {len(failed)} failed")
            self.time_var.set(timing)
            for png_path in ok:
                self._append_log(f"Saved spectrum + spectrogram: {png_path}")
            self._append_log(timing)
            messagebox.showwarning("Batch finished with errors", self._batch_summary(ok, failed))
            return

        self.progress_var.set(0)
        self.status_var.set("Conversion failed.")
        self.time_var.set("")
        if len(failed) == 1:
            messagebox.showerror("Conversion failed", failed[0][1])
        else:
            messagebox.showerror("Conversion failed", self._batch_summary(ok, failed))

    def _format_run_timing(self, elapsed, estimated):
        if elapsed < 60:
            actual = f"{elapsed:.1f} s"
        else:
            actual = format_duration(elapsed)
        if estimated and estimated > 0:
            return f"Finished in {actual}  ·  estimated {format_duration(estimated)}"
        return f"Finished in {actual}"

    def _set_preview(self, png_path):
        png_path = Path(png_path)
        if Image is None or ImageTk is None or not png_path.is_file():
            return
        try:
            img = Image.open(png_path)
            img.thumbnail((188, 100), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self._preview_photo = photo
            self.preview_label.configure(image=photo, text="", width=188, height=100)
        except Exception:
            pass

    def open_last_image(self):
        path = self._last_png_path
        if path is None or not Path(path).is_file():
            messagebox.showinfo("No image yet", "Convert a file first, then you can open the last PNG.")
            return
        try:
            os.startfile(path)
        except OSError as exc:
            messagebox.showerror("Open image", f"Windows could not open:\n{path}\n\n{exc}")

    def _batch_summary(self, ok, failed):
        lines = [f"Converted {len(ok)} of {len(ok) + len(failed)} files."]
        if failed:
            lines.append("")
            lines.append("Failed:")
            shown = failed[:12]
            lines.extend(f"• {name}: {err.splitlines()[0]}" for name, err in shown)
            if len(failed) > 12:
                lines.append(f"• … and {len(failed) - 12} more")
        return "\n".join(lines)

    def open_output_folder(self):
        output_dir = self._output_dir()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror("Output folder", f"Cannot open the output folder:\n{exc}")
            return
        try:
            os.startfile(output_dir)
        except OSError as exc:
            messagebox.showerror("Output folder", f"Windows could not open:\n{output_dir}\n\n{exc}")


if __name__ == "__main__":
    # Must run before Tk root exists so the taskbar groups under our icon, not pythonw.exe.
    set_app_user_model_id()
    enable_dpi_awareness()
    try:
        DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    app = IQConverterGUI()
    app.mainloop()
