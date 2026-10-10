import io
import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tkinter import filedialog, ttk

from iq_theme import (
    DARK_MODE,
    FONT_LOG,
    FONT_UI,
    LIGHT_MODE,
    PT_BODY,
    PT_HEADER,
    PT_SMALL,
    PrimaryButton,
    ThemeToggle,
    Tooltip,
    apply_option_db,
    apply_ttk_styles,
    apply_windows_titlebar,
    ask_yes_no,
    color_combobox_popdown,
    current_mode,
    current_palette,
    fit_window_to_screen,
    hook_combobox_popdown,
    normalize_theme,
    px,
    set_current_mode,
    show_error,
    show_info,
    show_warning,
    theme_toggle_tooltip,
)

# iq_data pulls in numpy, soundfile, and (lazily) scipy + matplotlib. It is
# imported in a background thread after the window is on screen, and on first
# use if a file is picked before that finishes.
_engine_mod = None
_engine_lock = threading.Lock()


def engine():
    """Return the iq_data module, importing it on first use."""
    global _engine_mod
    if _engine_mod is None:
        with _engine_lock:
            if _engine_mod is None:
                import iq_data

                _engine_mod = iq_data
    return _engine_mod

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


__version__ = "1.4.0"
# Sister application of "CRFS IQ Recorder" (same product family, same Sensorz icon).
APP_NAME = "CRFS IQ Spectrogram Converter"
COMPANY_NAME = "Sensorz"


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
SETTINGS_NAME = ".iq_gui_settings.json"
CONVERTER_FOLDER_NAME = "IQ Spectogram Converter"


def preferred_library_dir() -> Path:
    return APP_DIR


def remap_legacy_library_path(raw: str) -> str:
    """Point old C: Collection/Results defaults at folders next to this app."""
    text = (raw or "").strip()
    if not text:
        return text
    normalized = text.replace("/", "\\")
    c_project = "C:\\" + CONVERTER_FOLDER_NAME
    if normalized.lower().startswith(c_project.lower()):
        suffix = normalized[len(c_project) :].lstrip("\\/")
        return str(APP_DIR / suffix) if suffix else str(APP_DIR)
    low = normalized.casefold()
    marker = "\\crfs iq recorder\\recordings"
    if low.startswith("c:\\users\\") and marker in low:
        return str(preferred_library_dir() / "IQ Collection")
    return text


DEFAULT_OUTPUT = preferred_library_dir() / "IQ Results"
DEFAULT_INPUT_DIR = preferred_library_dir() / "IQ Collection"


def _brand_icon_path(name: str) -> Path:
    """Local Sensorz assets (frozen builds use the bundled assets folder)."""
    here = Path(__file__).resolve().parent
    roots = [resource_dir() / "assets", here / "assets"]
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
STATUS_LOADING = "Loading engine…"
STATUS_CONVERTING = "Converting…"
STATUS_SAVED = "Image saved"
STATUS_CANCELLED = "Cancelled"
STATUS_FAILED = "Conversion failed"
RBW_TOOLTIP = "Lower values show finer frequency detail. Higher values process faster."


def parse_drop_paths(data):
    """Split a tkdnd <<Drop>> string into paths.

    tkdnd hands over a Tcl list: items with spaces are wrapped in braces
    (`{C:/My Files/a.wav} C:/b.wav`), and a literal brace or space may be
    backslash-escaped. This is a pure-Python equivalent of `tk.splitlist` so
    it can be unit-tested without a Tk root.
    """
    text = str(data or "")
    items = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "{":
            depth = 1
            j = i + 1
            buf = []
            while j < n and depth:
                c = text[j]
                if c == "\\" and j + 1 < n:
                    buf.append(text[j + 1])
                    j += 2
                    continue
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                buf.append(c)
                j += 1
            items.append("".join(buf))
            i = j
            continue
        buf = []
        while i < n and not text[i].isspace():
            if text[i] == "\\" and i + 1 < n:
                buf.append(text[i + 1])
                i += 2
                continue
            buf.append(text[i])
            i += 1
        items.append("".join(buf))
    return [item for item in items if item]


def unique_output_names(wav_paths):
    """PNG names for a batch; equal stems from different folders get (2), (3), ..."""
    names = []
    seen = {}
    for path in wav_paths:
        stem = Path(path).stem
        count = seen.get(stem.casefold(), 0) + 1
        seen[stem.casefold()] = count
        names.append(f"{stem}.png" if count == 1 else f"{stem} ({count}).png")
    return names


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
        # Test hook: IQ_GUI_TK_SCALING=2.0 simulates a 150 % display (points per pixel * 1.5).
        forced_scaling = os.environ.get("IQ_GUI_TK_SCALING")
        if forced_scaling:
            try:
                self.tk.call("tk", "scaling", float(forced_scaling))
            except (tk.TclError, ValueError):
                pass

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
        self._win_hicon = None
        self._preview_photo = None
        self._last_png_path = None
        self._converting = False
        self._closing = False
        self._cancel_event = threading.Event()
        self._cancel_requested = False
        self._ui_queue = queue.Queue()
        self._pump_job = None
        self._engine_ready = False
        self._engine_error = None
        self._inspect_token = 0
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
        self._status_kind = "idle"
        self._style = ttk.Style(self)

        self._load_settings()
        set_current_mode(getattr(self, "_ui_theme", LIGHT_MODE))
        self._set_window_icon()
        # Re-apply after the window is mapped — Windows often locks taskbar icon then.
        self.after_idle(self._set_window_icon)
        self.bind("<Map>", self._on_window_mapped)
        self._configure_styles()
        self._build_ui()
        self.apply_theme(current_mode())
        self.rbw_var.trace_add("write", lambda *_: self._on_rbw_changed())
        self.input_var.trace_add("write", lambda *_: self._on_input_changed())
        self.bind("<Return>", self._on_return_key)
        self.bind("<Escape>", self._on_escape_key)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._enable_drag_drop()
        self._fit_to_screen()
        self._set_status(STATUS_LOADING, "busy")
        self._pump_ui_queue()
        # Heavy imports (numpy, soundfile, scipy, matplotlib) load after the first paint.
        self.after(30, self._start_engine_preload)
        self._on_input_changed()

    def _fit_to_screen(self):
        """Open at the Recorder's proportions; never smaller than the controls need."""
        self.update_idletasks()
        req_w = int(self.winfo_reqwidth())
        req_h = int(self.winfo_reqheight())
        self._min_height = max(px(self, 480), req_h)
        fit_window_to_screen(
            self,
            max(px(self, 840), req_w),
            max(px(self, 700), req_h + px(self, 170)),
            min_width=max(px(self, 640), req_w),
            min_height=self._min_height,
        )

    def _ensure_height_for_display(self):
        """Raise the minimum height (and grow if needed) while Display is open."""
        try:
            self.update_idletasks()
            from iq_theme import available_workarea

            _left, _top, _aw, ah = available_workarea(self)
            need = min(int(self.winfo_reqheight()), max(200, ah - 24))
            min_w, _min_h = self.minsize()
            self.minsize(min_w, max(self._min_height, need))
            if int(self.winfo_height()) < need:
                self.geometry(f"{self.winfo_width()}x{need}")
        except tk.TclError:
            pass

    def _restore_min_height(self):
        try:
            min_w, _min_h = self.minsize()
            self.minsize(min_w, self._min_height)
        except (tk.TclError, AttributeError):
            pass

    # ----- engine (iq_data) -------------------------------------------------
    def _start_engine_preload(self):
        if self._closing or not self._alive() or self._engine_ready:
            return
        threading.Thread(target=self._engine_preload_worker, daemon=True).start()

    def _engine_preload_worker(self):
        try:
            engine().preload_engine()
        except Exception as exc:  # pragma: no cover - import problems are fatal anyway
            self._safe_after(self._on_engine_failed, f"{exc.__class__.__name__}: {exc}")
            return
        self._safe_after(self._on_engine_ready)

    def _on_engine_ready(self):
        self._engine_ready = True
        if not self._converting and self._status_kind == "busy":
            self._set_status(STATUS_READY, "idle")
        self._on_input_changed()

    def _on_engine_failed(self, text):
        self._engine_error = text
        self._set_status("Engine failed to load", "bad")
        self._append_log(f"ERROR: the conversion engine could not be loaded: {text}")

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
        for rule in (getattr(self, "_header_rule", None), getattr(self, "_statusbar_rule", None)):
            if rule is not None:
                try:
                    rule.configure(bg=p.border)
                except tk.TclError:
                    pass
        if getattr(self, "_statusbar", None) is not None:
            try:
                self._statusbar.configure(bg=p.surface)
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

    def _on_window_mapped(self, event):
        if event.widget is self:
            self._set_window_icon()

    def _apply_windows_hwnd_icon(self):
        """Same ICO as CRFS IQ Recorder: set the Win32 window icon, not pythonw.exe."""
        if sys.platform != "win32" or not ICON_ICO_PATH.is_file():
            return
        try:
            import ctypes
            from ctypes import wintypes

            hwnd = int(self.winfo_id())
            if not hwnd:
                return
            user32 = ctypes.windll.user32
            GA_ROOT = 2
            WM_SETICON = 0x0080
            ICON_SMALL = 0
            ICON_BIG = 1
            IMAGE_ICON = 1
            LR_LOADFROMFILE = 0x0010
            LR_DEFAULTSIZE = 0x0040
            root = user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT) or user32.GetParent(
                wintypes.HWND(hwnd)
            )
            if not root:
                root = hwnd
            user32.LoadImageW.argtypes = [
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.LoadImageW.restype = wintypes.HANDLE
            hicon = user32.LoadImageW(
                None,
                str(ICON_ICO_PATH.resolve()),
                IMAGE_ICON,
                0,
                0,
                LR_LOADFROMFILE | LR_DEFAULTSIZE,
            )
            if not hicon:
                return
            self._win_hicon = hicon
            user32.SendMessageW(wintypes.HWND(root), WM_SETICON, ICON_SMALL, hicon)
            user32.SendMessageW(wintypes.HWND(root), WM_SETICON, ICON_BIG, hicon)
        except Exception:
            pass

    def _set_window_icon(self):
        """Set taskbar/window icon (Windows prefers .ico via iconbitmap + WM_SETICON)."""
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
                    photos.append(ImageTk.PhotoImage(resized, master=self))
                if photos:
                    self._window_icon_photo = photos  # keep refs alive
                    self.iconphoto(True, *photos)
            elif ICON_PNG_PATH.is_file():
                self._window_icon_photo = tk.PhotoImage(master=self, file=str(ICON_PNG_PATH))
                self.iconphoto(True, self._window_icon_photo)
        except Exception:
            pass
        self._apply_windows_hwnd_icon()

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
                self._logo_photo = ImageTk.PhotoImage(img, master=self)
                return self._logo_photo
            self._logo_photo = tk.PhotoImage(master=self, file=str(ICON_PNG_PATH))
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
            self.output_var.set(remap_legacy_library_path(out.strip()))
        rbw = data.get("rbw")
        if rbw is not None:
            try:
                if float(rbw) > 0:
                    self.rbw_var.set(str(rbw).strip())
            except (TypeError, ValueError):
                pass
        indir = data.get("input_dir")
        if isinstance(indir, str) and indir.strip():
            mapped = remap_legacy_library_path(indir.strip())
            if Path(mapped).is_dir():
                self._last_input_dir = mapped
            elif DEFAULT_INPUT_DIR.is_dir():
                self._last_input_dir = str(DEFAULT_INPUT_DIR)
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
        remapped = False
        if isinstance(out, str) and out.strip() and self.output_var.get().strip() != out.strip():
            remapped = True
        if isinstance(indir, str) and indir.strip() and self._last_input_dir != indir.strip():
            remapped = True
        if remapped:
            self._save_settings()

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

        s = px  # shorthand: s(self, 20) -> 20 CSS px scaled to this screen

        # Header bar: Recorder `headerBar` (surface, 1px bottom border, 20/10/16/10 margins).
        self._header = tk.Frame(self, bg=p.surface, bd=0, highlightthickness=0)
        self._header.pack(fill="x")
        self._header_left = tk.Frame(self._header, bg=p.surface)
        self._header_left.pack(side="left", padx=(s(self, 20), s(self, 12)), pady=s(self, 10))
        self._header_right = tk.Frame(self._header, bg=p.surface)
        self._header_right.pack(side="right", padx=(s(self, 8), s(self, 16)), pady=s(self, 10))

        logo = self._load_logo(max_height=s(self, 32))
        if logo is not None:
            self._logo_label = tk.Label(
                self._header_left, image=logo, bg=p.surface, bd=0, highlightthickness=0
            )
            self._logo_label.pack(side="left", padx=(0, s(self, 12)))
        else:
            self._logo_label = None

        self._title_label = tk.Label(
            self._header_left,
            text=APP_NAME,
            bg=p.surface,
            fg=p.text,
            font=(FONT_UI, PT_HEADER, "bold"),
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
            font=(FONT_UI, PT_SMALL),
        )
        self._version_label.pack(side="right", padx=(0, s(self, 12)))

        self._header_rule = tk.Frame(self, bg=p.border, height=1, bd=0, highlightthickness=0)
        self._header_rule.pack(fill="x")

        # Status bar: Recorder QStatusBar (surface, 1px top border, muted bold 12px, size grip).
        self._statusbar_rule = tk.Frame(self, bg=p.border, height=1, bd=0, highlightthickness=0)
        self._statusbar_rule.pack(side="bottom", fill="x")
        self._statusbar = tk.Frame(self, bg=p.surface, bd=0, highlightthickness=0)
        self._statusbar.pack(side="bottom", fill="x")
        self.status_label = ttk.Label(
            self._statusbar, textvariable=self.status_var, style="StatusBar.TLabel"
        )
        self.status_label.pack(side="left", padx=(s(self, 16), s(self, 8)), pady=s(self, 5))
        try:
            ttk.Sizegrip(self._statusbar, style="App.TSizegrip").pack(side="right", anchor="se")
        except tk.TclError:
            pass

        body = ttk.Frame(self, style="App.TFrame", padding=(s(self, 16), s(self, 12), s(self, 16), s(self, 12)))
        body.pack(fill="both", expand=True)

        # Files card: Recorder `card` (surface, 1px border, 16/12 padding, 8px spacing).
        files_card = self._make_card(body)
        files_card.pack(fill="x", pady=(0, s(self, 8)))
        files = ttk.Frame(files_card, style="Surface.TFrame", padding=(s(self, 16), s(self, 12)))
        files.pack(fill="x")
        files.columnconfigure(0, weight=1)

        ttk.Label(files, text="Conversion", style="SectionTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(
            files,
            text="Pick a stereo IQ WAV (I left, Q right), where the PNG goes, and the RBW.",
            style="Muted.TLabel",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, s(self, 8)))

        ttk.Label(files, text="IQ WAV file", style="Field.TLabel").grid(
            row=2, column=0, sticky="w", pady=(0, s(self, 4))
        )
        self.input_entry = ttk.Entry(files, textvariable=self.input_var, style="App.TEntry")
        self.input_entry.grid(row=3, column=0, sticky="ew")
        self.browse_input_btn = ttk.Button(
            files, text="Browse", style="Browse.TButton", command=self.choose_input
        )
        self.browse_input_btn.grid(row=3, column=1, sticky="e", padx=(s(self, 10), 0))

        self.file_info_label = ttk.Label(
            files, textvariable=self.file_info_var, style="Hint.TLabel"
        )
        self.file_info_label.grid(row=4, column=0, columnspan=2, sticky="w", pady=(s(self, 6), s(self, 10)))

        ttk.Label(files, text="Output folder", style="Field.TLabel").grid(
            row=5, column=0, sticky="w", pady=(0, s(self, 4))
        )
        self.output_entry = ttk.Entry(files, textvariable=self.output_var, style="App.TEntry")
        self.output_entry.grid(row=6, column=0, sticky="ew")
        self.browse_output_btn = ttk.Button(
            files, text="Browse", style="Browse.TButton", command=self.choose_output
        )
        self.browse_output_btn.grid(row=6, column=1, sticky="e", padx=(s(self, 10), 0))

        rbw_row = ttk.Frame(files, style="Surface.TFrame")
        rbw_row.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(s(self, 12), 0))
        rbw_label = ttk.Label(rbw_row, text="RBW", style="Field.TLabel")
        rbw_label.pack(side="left")
        self.rbw_entry = ttk.Entry(rbw_row, textvariable=self.rbw_var, width=12, style="App.TEntry")
        self.rbw_entry.pack(side="left", padx=(s(self, 10), s(self, 6)))
        ttk.Label(rbw_row, text="Hz", style="Muted.TLabel").pack(side="left", padx=(0, s(self, 14)))
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
        display_card.pack(fill="x", pady=(0, s(self, 8)))
        d_inner = ttk.Frame(display_card, style="Surface.TFrame", padding=(s(self, 16), s(self, 10)))
        d_inner.pack(fill="x")

        d_head = ttk.Frame(d_inner, style="Surface.TFrame")
        d_head.pack(fill="x")
        self._display_toggle = tk.Label(
            d_head,
            text="▸  Display",
            bg=p.surface,
            fg=p.text,
            font=(FONT_UI, PT_BODY + 1, "bold"),
            cursor="hand2",
        )
        self._display_toggle.pack(side="left")
        self._display_toggle.bind("<Button-1>", lambda e: self._toggle_display())
        ttk.Label(d_head, text="Colormap, dB scale, open when done", style="Muted.TLabel").pack(
            side="left", padx=(s(self, 10), 0)
        )

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
        actions.pack(fill="x", pady=(s(self, 4), s(self, 8)))

        self.convert_button = PrimaryButton(actions, text="Convert", command=self.start_conversion)
        self.convert_button.pack(side="left")

        self.cancel_button = ttk.Button(
            actions,
            text="Cancel",
            style="Secondary.TButton",
            command=self.cancel_conversion,
            state="disabled",
        )
        self.cancel_button.pack(side="left", padx=(s(self, 10), 0))

        self.folder_button = ttk.Button(
            actions,
            text="Convert folder",
            style="Secondary.TButton",
            command=self.start_folder_conversion,
        )
        self.folder_button.pack(side="left", padx=(s(self, 10), 0))

        self.open_folder_btn = ttk.Button(
            actions,
            text="Open output folder",
            style="Ghost.TButton",
            command=self.open_output_folder,
        )
        self.open_folder_btn.pack(side="right")

        self.open_last_btn = ttk.Button(
            actions,
            text="Open image",
            style="Ghost.TButton",
            command=self.open_last_image,
            state="disabled",
        )
        self.open_last_btn.pack(side="right", padx=(0, s(self, 10)))

        progress_header = ttk.Frame(body, style="App.TFrame")
        progress_header.pack(fill="x", pady=(s(self, 2), s(self, 4)))
        ttk.Label(progress_header, textvariable=self.time_var, style="Time.TLabel").pack(
            side="left", anchor="w"
        )

        self.progress = ttk.Progressbar(
            body,
            mode="determinate",
            maximum=100,
            variable=self.progress_var,
            style="App.Horizontal.TProgressbar",
        )
        self.progress.pack(fill="x", pady=(0, s(self, 8)))

        log_frame = ttk.LabelFrame(body, text="  Activity  ", style="Log.TLabelframe", padding=s(self, 8))
        log_frame.pack(fill="both", expand=True)
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)

        log_wrap = ttk.Frame(log_frame, style="Surface.TFrame")
        log_wrap.grid(row=0, column=0, sticky="nsew")
        log_wrap.columnconfigure(0, weight=1)
        log_wrap.rowconfigure(0, weight=1)

        # Recorder QPlainTextEdit#log: log_bg, Consolas 12px, 8px padding.
        # height=2 keeps the window's minimum size small; pack(expand) gives the
        # log all remaining space at normal sizes.
        self.log = tk.Text(
            log_wrap,
            height=2,
            wrap="word",
            bg=p.log_bg,
            fg=p.log_text,
            insertbackground=p.log_text,
            relief="flat",
            font=(FONT_LOG, PT_SMALL),
            padx=s(self, 8),
            pady=s(self, 6),
            state="disabled",
            highlightthickness=1,
            highlightbackground=p.border,
            highlightcolor=p.focus,
        )
        self.log.grid(row=0, column=0, sticky="nsew")

        scroll = ttk.Scrollbar(log_wrap, command=self.log.yview, style="App.Vertical.TScrollbar")
        scroll.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=scroll.set)

        self._preview_size = (s(self, 188), s(self, 100))
        self.preview_wrap = tk.Frame(log_frame, bg=p.surface, width=self._preview_size[0] + s(self, 12))
        self.preview_wrap.grid(row=0, column=1, sticky="ns", padx=(s(self, 8), 0))
        self.preview_wrap.grid_propagate(False)
        ttk.Label(self.preview_wrap, text="Last image", style="Hint.TLabel").pack(anchor="w")
        self.preview_label = tk.Label(
            self.preview_wrap,
            text="No preview yet",
            bg=p.bg,
            fg=p.muted,
            font=(FONT_UI, PT_SMALL),
            width=24,
            height=2,
            bd=0,
            highlightthickness=1,
            highlightbackground=p.border,
        )
        self.preview_label.pack(fill="both", expand=True, pady=(s(self, 6), 0))

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
        self._set_widget_tooltip(self.cancel_button, "Stop the current conversion. No PNG is written for it.")

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

    # ----- status bar -------------------------------------------------------
    def _set_status(self, text, kind="idle"):
        """kind: idle | busy | ok | bad (Recorder status colours)."""
        self._status_kind = kind
        self.status_var.set(text)
        style = {
            "busy": "StatusBarBusy.TLabel",
            "ok": "StatusBarOk.TLabel",
            "bad": "StatusBarBad.TLabel",
        }.get(kind, "StatusBar.TLabel")
        label = getattr(self, "status_label", None)
        if label is not None:
            try:
                label.configure(style=style)
            except tk.TclError:
                pass

    # ----- dialogs (themed, modal) -----------------------------------------
    def _info(self, title, message):
        show_info(self, title, message)

    def _warn(self, title, message):
        show_warning(self, title, message)

    def _error(self, title, message):
        show_error(self, title, message)

    def _ask(self, title, message):
        return ask_yes_no(self, title, message)

    def _toggle_display(self):
        self._display_open = not self._display_open
        if self._display_open:
            self._display_body.pack(fill="x")
            self._display_toggle.configure(text="▾  Display")
            if getattr(self, "_min_height", None):
                self._ensure_height_for_display()
        else:
            self._display_body.pack_forget()
            self._display_toggle.configure(text="▸  Display")
            if getattr(self, "_min_height", None):
                self._restore_min_height()

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
        """Run func(*args) on the Tk thread.

        Worker threads must not touch Tk at all (not even winfo_exists), so
        they enqueue; the main thread drains the queue every 40 ms.
        """
        if self._closing:
            return
        if threading.current_thread() is not threading.main_thread():
            self._ui_queue.put((func, args))
            return
        if not self._alive():
            return
        try:
            self.after(0, self._run_if_alive, func, *args)
        except tk.TclError:
            pass

    def _pump_ui_queue(self):
        if self._closing or not self._alive():
            return
        for _ in range(200):
            try:
                func, args = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            self._run_if_alive(func, *args)
        try:
            self._pump_job = self.after(40, self._pump_ui_queue)
        except tk.TclError:
            self._pump_job = None

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
        if hasattr(self, "cancel_button"):
            try:
                self.cancel_button.configure(state="normal" if busy else "disabled")
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
        self._inspect_token += 1
        # Stop the worker's chunk loop; a partial PNG is never left behind.
        self._cancel_event.set()
        for job in (self._timer_job, self._pump_job):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._timer_job = None
        self._pump_job = None
        self._save_settings()
        self.destroy()

    def cancel_conversion(self):
        if not self._converting or self._cancel_requested:
            return
        self._cancel_requested = True
        self._cancel_event.set()
        self._progress_stage = "Cancelling…"
        self._set_status("Cancelling…", "busy")
        self._append_log("Cancel requested. Stopping after the current chunk…")
        try:
            self.cancel_button.configure(state="disabled")
        except tk.TclError:
            pass

    def _on_rbw_changed(self):
        self._sync_preset_highlight()
        if self._converting:
            return
        self._schedule_inspect()

    def _on_input_changed(self):
        if self._converting:
            return
        self._schedule_inspect()

    # ----- file inspection (header read + ETA) in a worker thread ----------
    def _schedule_inspect(self):
        """Read the WAV header and estimate the run time off the UI thread."""
        self._inspect_token += 1
        token = self._inspect_token
        raw = self.input_var.get().strip()
        path = Path(raw) if raw else None
        if path is None or not path.is_file():
            self.file_info_var.set("Drop or browse a stereo IQ WAV to see sample rate, duration, and size.")
            self.file_info_label.configure(style="Hint.TLabel")
            self.time_var.set("")
            return
        try:
            rbw = float(self.rbw_var.get())
            if rbw <= 0:
                rbw = None
        except ValueError:
            rbw = None
        self.file_info_var.set("Reading WAV header…")
        self.file_info_label.configure(style="Hint.TLabel")
        threading.Thread(
            target=self._inspect_worker, args=(str(path), rbw, token), daemon=True
        ).start()

    def _inspect_worker(self, path, rbw, token):
        result = {"path": path}
        try:
            result["info"] = engine().wav_info(path)
        except Exception as exc:
            result["error"] = str(exc).strip() or exc.__class__.__name__
        if "info" in result and rbw is not None and result["info"]["channels"] >= 2:
            try:
                result["estimate"] = engine().estimate_conversion_seconds(path, rbw)
            except Exception:
                pass
        self._safe_after(self._on_inspect_done, token, result)

    def _on_inspect_done(self, token, result):
        if token != self._inspect_token or self._converting:
            return
        if result["path"] != self.input_var.get().strip():
            return
        if "error" in result:
            self.file_info_var.set(f"Could not read WAV header: {result['error']}")
            self.file_info_label.configure(style="Warn.TLabel")
            self.time_var.set("")
            return

        info = result["info"]
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
            self.time_var.set("")
            return
        self.file_info_var.set("  ·  ".join(bits))
        self.file_info_label.configure(style="Info.TLabel")
        est = result.get("estimate")
        self.time_var.set(f"Est. ~{format_duration(est)}" if est else "")

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
        if self._engine_ready and not self._converting:
            self._set_status(STATUS_READY, "idle")
        self._append_log(f"Input: {path}")
        self._save_settings()

    def _on_drop(self, event):
        if self._converting:
            return
        raw_paths = parse_drop_paths(getattr(event, "data", ""))
        if not raw_paths:
            try:
                raw_paths = list(self.tk.splitlist(event.data))
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
            if self._ask(
                "Convert dropped files",
                f"Convert {len(wavs)} WAV files?\n\n{names}{extra}",
            ):
                self._start_jobs(wavs)
            return
        if len(dirs) == 1:
            folder = dirs[0]
            found = list_wavs(folder)
            if not found:
                self._info("No WAV files", f"No .wav files in:\n{folder}")
                return
            if self._ask(
                "Convert folder",
                f"Convert {len(found)} WAV file(s) in:\n{folder}?",
            ):
                self._last_input_dir = str(folder)
                self._start_jobs(found)
            return
        if paths:
            self._info("Not a WAV file", "Drop a .wav file or a folder that contains .wav files.")

    def _parse_rbw(self):
        try:
            return engine().validate_rbw(self.rbw_var.get())
        except ValueError as exc:
            self._error("Invalid RBW", str(exc))
            return None

    def _parse_optional_db(self, raw, label):
        text = str(raw).strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            self._error("Invalid dB range", f"{label} must be a number.")
            return False
        if value != value or value in (float("inf"), float("-inf")):
            self._error("Invalid dB range", f"{label} must be a finite number.")
            return False
        return value

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
                self._error("Invalid dB range", "dB max must be greater than dB min.")
                return None
        return cmap, freq_smooth, db_auto, db_vmin, db_vmax

    def _output_dir(self):
        raw = self.output_var.get().strip()
        return Path(raw) if raw else DEFAULT_OUTPUT

    def start_conversion(self):
        if self._converting or self._closing:
            return
        input_path = Path(self.input_var.get().strip())
        if not input_path.is_file():
            self._error("Invalid file", "Please choose an existing WAV file.")
            return
        if input_path.suffix.lower() != ".wav":
            self._error("Invalid file", "The selected input must be a .wav file.")
            return
        try:
            info = engine().wav_info(str(input_path))
        except Exception as exc:
            self._error(
                "Invalid WAV",
                f"Could not read this WAV file (it may be truncated or invalid):\n{exc}",
            )
            return
        if info["channels"] < 2:
            self._error("Need stereo IQ", "Need stereo IQ (I left, Q right).")
            return
        if info["frames"] < 1:
            self._error("Invalid WAV", "WAV file has no samples.")
            return
        self._start_jobs([input_path])

    def start_folder_conversion(self):
        if self._converting or self._closing:
            return
        initial = self._last_input_dir
        if not Path(initial).is_dir():
            initial = str(DEFAULT_INPUT_DIR if DEFAULT_INPUT_DIR.is_dir() else APP_DIR)
        selected = filedialog.askdirectory(title="Choose a folder of IQ WAV files", initialdir=initial)
        if not selected:
            return
        folder = Path(selected)
        wavs = list_wavs(folder)
        if not wavs:
            self._info("No WAV files", f"No .wav files in:\n{folder}")
            return
        self._last_input_dir = str(folder)
        self._save_settings()
        self._start_jobs(wavs)

    @staticmethod
    def _folder_writable(folder):
        """True if a file can be created in `folder` (catches read-only ACLs early)."""
        probe = Path(folder) / f".iq_write_test_{os.getpid()}.tmp"
        try:
            with open(probe, "wb"):
                pass
        except OSError:
            return False
        try:
            probe.unlink()
        except OSError:
            pass
        return True

    def _start_jobs(self, wav_paths):
        if self._converting or self._closing:
            return
        wav_paths = [Path(p) for p in wav_paths]
        if not wav_paths:
            self._error("No files", "No WAV files to convert.")
            return
        missing = [p for p in wav_paths if not p.is_file()]
        if missing:
            self._error("Missing file", f"This file no longer exists:\n{missing[0]}")
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
            self._error(
                "Output folder",
                f"Cannot create or write to the output folder:\n{output_dir}\n\n{exc}",
            )
            return
        if not self._folder_writable(output_dir):
            self._error(
                "Output folder",
                f"The output folder is read-only or not writable:\n{output_dir}\n\n"
                "Choose another folder.",
            )
            return

        estimated = 0.0
        for path in wav_paths:
            try:
                estimated += engine().estimate_conversion_seconds(str(path), rbw)
            except Exception:
                estimated += 30.0
        estimated = max(2.0, estimated)

        output_names = unique_output_names(wav_paths)
        jobs = list(zip(wav_paths, output_names))

        self._job_id += 1
        job_id = self._job_id
        self._converting = True
        self._cancel_requested = False
        self._cancel_event = threading.Event()
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
        self._set_status(STATUS_CONVERTING, "busy")
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
        renamed = [(p.name, n) for p, n in jobs if n != f"{p.stem}.png"]
        for src, name in renamed:
            self._append_log(f"Note: {src} has the same name as another file; saving as {name}")
        self._append_log(f"Estimated time: ~{format_duration(estimated)}")
        self._save_settings()
        self._schedule_timer()

        thread = threading.Thread(
            target=self._convert_worker,
            args=(jobs, output_dir, rbw, job_id, self._cancel_event),
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

        if not self._cancel_requested:
            self._set_status(STATUS_CONVERTING, "busy")
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

    def _convert_worker(self, jobs, output_dir, rbw, job_id, cancel_event):
        ok = []
        failed = []
        cancelled = False
        n = len(jobs)
        mod = engine()

        for i, (input_path, out_name) in enumerate(jobs):
            if self._closing or job_id != self._job_id:
                return
            if cancel_event.is_set():
                cancelled = True
                break

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
                    result = mod.process_file(
                        str(input_path),
                        str(output_dir),
                        rbw,
                        progress_callback=cb,
                        cmap=self._job_cmap,
                        freq_smooth=self._job_freq_smooth,
                        db_auto=self._job_db_auto,
                        db_vmin=self._job_db_vmin,
                        db_vmax=self._job_db_vmax,
                        cancel_event=cancel_event,
                        output_name=out_name,
                    )

                details = buffer.getvalue().strip()
                if details:
                    self._safe_after(self._append_log, details)

                png_path = Path(result) if result else output_dir / out_name
                if not png_path.is_file():
                    failed.append((input_path.name, f"Expected image was not found:\n{png_path}"))
                    self._safe_after(
                        self._append_log,
                        f"ERROR {input_path.name}: PNG was not created",
                    )
                    continue

                ok.append(png_path)

            except mod.ConversionCancelled:
                cancelled = True
                self._safe_after(self._append_log, f"Cancelled: {input_path.name} (no PNG written)")
                break
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
        self._safe_after(self._conversion_finished, ok, failed, cancelled)

    def _stop_timer(self):
        self._converting = False
        if self._timer_job is not None:
            try:
                self.after_cancel(self._timer_job)
            except tk.TclError:
                pass
            self._timer_job = None

    def _conversion_finished(self, ok, failed, cancelled=False):
        if not self._alive():
            return
        elapsed = time.monotonic() - self._convert_started_at
        estimated = self._estimated_total_s
        self._stop_timer()
        self._cancel_requested = False
        self._set_busy(False)
        self._save_settings()
        # Refresh header info / ETA now that controls are enabled again.
        self._schedule_inspect()

        timing = self._format_run_timing(elapsed, estimated)
        if ok:
            self._last_png_path = ok[-1]
            self._set_preview(ok[-1])
            try:
                self.open_last_btn.configure(state="normal")
            except tk.TclError:
                pass

        if cancelled:
            self.progress_var.set(0)
            if ok:
                self._set_status(f"{STATUS_CANCELLED} — {len(ok)} file(s) finished first", "idle")
                for png_path in ok:
                    self._append_log(f"Saved spectrum + spectrogram: {png_path}")
            else:
                self._set_status(STATUS_CANCELLED, "idle")
            self.time_var.set(f"Cancelled after {format_duration(elapsed)}")
            self._append_log("Conversion cancelled.")
            return

        if ok and not failed:
            self.progress_var.set(100)
            if len(ok) == 1:
                png_path = ok[0]
                self._set_status(f"{STATUS_SAVED} — {png_path.name}", "ok")
                self.time_var.set(timing)
                self._append_log(f"Saved spectrum + spectrogram: {png_path}")
                self._append_log(timing)
                if self._open_when_done:
                    try:
                        os.startfile(png_path)
                    except OSError as exc:
                        self._warn(
                            "Image created",
                            f"The PNG was created, but Windows could not open it automatically.\n\n{exc}",
                        )
            else:
                self._set_status(f"{STATUS_SAVED} — {len(ok)} files", "ok")
                self.time_var.set(timing)
                for png_path in ok:
                    self._append_log(f"Saved spectrum + spectrogram: {png_path}")
                self._append_log(f"Batch complete: {len(ok)} files. {timing}")
                self._info(
                    "Batch complete",
                    f"Converted {len(ok)} files to:\n{ok[0].parent}",
                )
            return

        if ok and failed:
            self.progress_var.set(100)
            self._set_status(f"{STATUS_SAVED} — {len(ok)} ok, {len(failed)} failed", "bad")
            self.time_var.set(timing)
            for png_path in ok:
                self._append_log(f"Saved spectrum + spectrogram: {png_path}")
            self._append_log(timing)
            self._warn("Batch finished with errors", self._batch_summary(ok, failed))
            return

        self.progress_var.set(0)
        self._set_status(STATUS_FAILED, "bad")
        self.time_var.set("")
        if len(failed) == 1:
            self._error("Conversion failed", failed[0][1])
        else:
            self._error("Conversion failed", self._batch_summary(ok, failed))

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
            w, h = getattr(self, "_preview_size", (188, 100))
            img = Image.open(png_path)
            img.thumbnail((w, h), Image.Resampling.LANCZOS)
            photo = ImageTk.PhotoImage(img, master=self)
            self._preview_photo = photo
            self.preview_label.configure(image=photo, text="", width=w, height=h)
        except Exception:
            pass

    def open_last_image(self):
        path = self._last_png_path
        if path is None or not Path(path).is_file():
            self._info("No image yet", "Convert a file first, then you can open the last PNG.")
            return
        try:
            os.startfile(path)
        except OSError as exc:
            self._error("Open image", f"Windows could not open:\n{path}\n\n{exc}")

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
            self._error("Output folder", f"Cannot open the output folder:\n{exc}")
            return
        try:
            os.startfile(output_dir)
        except OSError as exc:
            self._error("Output folder", f"Windows could not open:\n{output_dir}\n\n{exc}")


def run_headless(argv):
    """`--input file.wav [--output dir] [--rbw hz] [--log file]`: convert without a window.

    Used by the packaged EXE for scripted runs and by the QA suite to prove the
    bundle converts on a machine without Python. Exit code 0 on success.
    """
    import argparse

    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=True)
    parser.add_argument("--input", required=True, help="Stereo IQ WAV file")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Output folder")
    parser.add_argument("--rbw", default="15000", help="Resolution bandwidth in Hz")
    parser.add_argument("--cmap", default="jet")
    parser.add_argument("--log", default=None, help="Write the conversion log to this file")
    args = parser.parse_args(argv)

    buffer = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buffer), redirect_stderr(buffer):
            out = engine().process_file(args.input, args.output, args.rbw, cmap=args.cmap)
        buffer.write(f"OK {out}\n")
    except Exception as exc:
        buffer.write(f"ERROR {user_error_text(exc)}\n{traceback.format_exc()}")
        code = 1
    text = buffer.getvalue()
    if args.log:
        try:
            Path(args.log).write_text(text, encoding="utf-8")
        except OSError:
            pass
    if sys.stdout is not None:
        try:
            sys.stdout.write(text)
        except (OSError, ValueError):
            pass
    return code


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--input" in argv:
        return run_headless(argv)
    # Must run before Tk root exists so the taskbar groups under our icon, not pythonw.exe.
    set_app_user_model_id()
    enable_dpi_awareness()
    try:
        DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
        DEFAULT_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    app = IQConverterGUI()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
