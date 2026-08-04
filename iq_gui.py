import io
import os
import sys
import threading
import time
import tkinter as tk
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from iq_data import estimate_conversion_seconds, process_file

try:
    from PIL import Image, ImageTk
except ImportError:  # pragma: no cover
    Image = None
    ImageTk = None


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
LOGO_PATH = resource_dir() / "assets" / "sensorz_logo.png"

# Sensorz-aligned palette
COLORS = {
    "bg": "#eef1f4",
    "surface": "#ffffff",
    "header": "#000000",
    "header_text": "#f4f7fb",
    "muted": "#5c6b7a",
    "text": "#1a2332",
    "accent": "#00c2d4",
    "accent_hover": "#009eb0",
    "accent_text": "#041018",
    "border": "#d0d7de",
    "log_bg": "#0f1720",
    "log_fg": "#c8d4e0",
}

RBW_PRESETS = [
    ("5 kHz", "5000"),
    ("15 kHz", "15000"),
    ("30 kHz", "30000"),
    ("50 kHz", "50000"),
]


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


class IQConverterGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IQ Spectrogram Converter — Sensorz")
        self.geometry("780x680")
        self.minsize(720, 620)
        self.configure(bg=COLORS["bg"])

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        self.rbw_var = tk.StringVar(value="15000")
        self.status_var = tk.StringVar(value="Select a stereo IQ WAV file to begin.")
        self.time_var = tk.StringVar(value="")
        self.progress_var = tk.DoubleVar(value=0.0)

        self._logo_photo = None
        self._converting = False
        self._progress_fraction = 0.0
        self._progress_stage = ""
        self._convert_started_at = 0.0
        self._estimated_total_s = 0.0
        self._timer_job = None

        self._configure_styles()
        self._build_ui()
        self.rbw_var.trace_add("write", lambda *_: self._update_estimate_preview())
        self._center_window()

    def _configure_styles(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure("App.TFrame", background=COLORS["bg"])
        style.configure("Surface.TFrame", background=COLORS["surface"])
        style.configure("Header.TFrame", background=COLORS["header"])

        style.configure(
            "HeaderTitle.TLabel",
            background=COLORS["header"],
            foreground=COLORS["header_text"],
            font=("Segoe UI Semibold", 18),
        )
        style.configure(
            "HeaderSub.TLabel",
            background=COLORS["header"],
            foreground="#8aa0b8",
            font=("Segoe UI", 10),
        )
        style.configure(
            "Field.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 9),
        )
        style.configure(
            "Hint.TLabel",
            background=COLORS["surface"],
            foreground=COLORS["muted"],
            font=("Segoe UI", 8),
        )
        style.configure(
            "Status.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["text"],
            font=("Segoe UI", 9),
        )
        style.configure(
            "Time.TLabel",
            background=COLORS["bg"],
            foreground=COLORS["muted"],
            font=("Segoe UI Semibold", 9),
        )
        style.configure(
            "App.TEntry",
            fieldbackground="#f8fafc",
            foreground=COLORS["text"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            insertcolor=COLORS["text"],
            padding=6,
        )
        style.configure("Browse.TButton", font=("Segoe UI", 9), padding=(12, 6))
        style.configure("Preset.TButton", font=("Segoe UI", 8), padding=(8, 4))
        style.configure(
            "Primary.TButton",
            font=("Segoe UI Semibold", 11),
            background=COLORS["accent"],
            foreground=COLORS["accent_text"],
            bordercolor=COLORS["accent"],
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
            focuscolor=COLORS["accent"],
            padding=(18, 10),
        )
        style.map(
            "Primary.TButton",
            background=[("active", COLORS["accent_hover"]), ("disabled", "#7eb8c0")],
            foreground=[("disabled", "#e8f0ef")],
        )
        style.configure("Secondary.TButton", font=("Segoe UI", 9), padding=(12, 6))
        style.configure(
            "App.Horizontal.TProgressbar",
            troughcolor="#dce3ea",
            background=COLORS["accent"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["accent"],
            darkcolor=COLORS["accent"],
            thickness=12,
        )
        style.configure(
            "Card.TLabelframe",
            background=COLORS["surface"],
            bordercolor=COLORS["border"],
            lightcolor=COLORS["border"],
            darkcolor=COLORS["border"],
            relief="solid",
        )
        style.configure(
            "Card.TLabelframe.Label",
            background=COLORS["surface"],
            foreground=COLORS["text"],
            font=("Segoe UI Semibold", 10),
        )

    def _load_logo(self, max_height=46):
        if not LOGO_PATH.is_file():
            return None
        try:
            if Image is not None and ImageTk is not None:
                img = Image.open(LOGO_PATH)
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGBA")
                ratio = max_height / img.height
                size = (max(1, int(img.width * ratio)), max_height)
                img = img.resize(size, Image.Resampling.LANCZOS)
                self._logo_photo = ImageTk.PhotoImage(img)
                return self._logo_photo
            self._logo_photo = tk.PhotoImage(file=str(LOGO_PATH))
            # Downscale with subsample if very tall
            while self._logo_photo.height() > max_height * 2:
                self._logo_photo = self._logo_photo.subsample(2, 2)
            return self._logo_photo
        except Exception:
            return None

    def _build_ui(self):
        header = ttk.Frame(self, style="Header.TFrame", padding=(20, 14))
        header.pack(fill="x")
        header.columnconfigure(1, weight=1)

        logo = self._load_logo()
        if logo is not None:
            tk.Label(header, image=logo, bg=COLORS["header"], bd=0, highlightthickness=0).grid(
                row=0, column=0, rowspan=2, sticky="w", padx=(0, 18)
            )

        ttk.Label(header, text="IQ Spectrogram Converter", style="HeaderTitle.TLabel").grid(
            row=0, column=1, sticky="sw"
        )
        ttk.Label(
            header,
            text="Turn stereo IQ WAV recordings into spectrogram images",
            style="HeaderSub.TLabel",
        ).grid(row=1, column=1, sticky="nw", pady=(4, 0))

        body = ttk.Frame(self, style="App.TFrame", padding=(20, 16, 20, 12))
        body.pack(fill="both", expand=True)

        files = ttk.LabelFrame(body, text="  Files  ", style="Card.TLabelframe", padding=14)
        files.pack(fill="x", pady=(0, 12))
        files.columnconfigure(0, weight=1)

        ttk.Label(files, text="IQ WAV file", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 4)
        )
        ttk.Entry(files, textvariable=self.input_var, style="App.TEntry").grid(
            row=1, column=0, sticky="ew", pady=(0, 10)
        )
        ttk.Button(files, text="Browse…", style="Browse.TButton", command=self.choose_input).grid(
            row=1, column=1, sticky="e", padx=(10, 0), pady=(0, 10)
        )

        ttk.Label(files, text="Output folder", style="Field.TLabel").grid(
            row=2, column=0, sticky="w", pady=(0, 4)
        )
        ttk.Entry(files, textvariable=self.output_var, style="App.TEntry").grid(
            row=3, column=0, sticky="ew"
        )
        ttk.Button(files, text="Browse…", style="Browse.TButton", command=self.choose_output).grid(
            row=3, column=1, sticky="e", padx=(10, 0)
        )

        params = ttk.LabelFrame(
            body, text="  Resolution bandwidth  ", style="Card.TLabelframe", padding=14
        )
        params.pack(fill="x", pady=(0, 12))
        params.columnconfigure(0, weight=1)

        rbw_row = ttk.Frame(params, style="Surface.TFrame")
        rbw_row.grid(row=0, column=0, sticky="ew")
        ttk.Label(rbw_row, text="RBW (Hz)", style="Field.TLabel").pack(side="left")
        ttk.Entry(rbw_row, textvariable=self.rbw_var, width=14, style="App.TEntry").pack(
            side="left", padx=(10, 14)
        )
        for label, value in RBW_PRESETS:
            ttk.Button(
                rbw_row,
                text=label,
                style="Preset.TButton",
                command=lambda v=value: self.rbw_var.set(v),
            ).pack(side="left", padx=3)

        ttk.Label(
            params,
            text="Lower RBW = finer frequency detail (slower). Higher RBW = faster overview.",
            style="Hint.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))

        actions = ttk.Frame(body, style="App.TFrame")
        actions.pack(fill="x", pady=(2, 10))

        self.convert_button = ttk.Button(
            actions,
            text="Convert & Open Image",
            style="Primary.TButton",
            command=self.start_conversion,
        )
        self.convert_button.pack(side="left")

        ttk.Button(
            actions,
            text="Open Output Folder",
            style="Secondary.TButton",
            command=self.open_output_folder,
        ).pack(side="left", padx=(10, 0))

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

        log_frame = ttk.LabelFrame(body, text="  Activity  ", style="Card.TLabelframe", padding=10)
        log_frame.pack(fill="both", expand=True)

        self.log = tk.Text(
            log_frame,
            height=10,
            wrap="word",
            bg=COLORS["log_bg"],
            fg=COLORS["log_fg"],
            insertbackground=COLORS["log_fg"],
            relief="flat",
            font=("Consolas", 9),
            padx=10,
            pady=8,
            state="disabled",
        )
        self.log.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)

        self._append_log("Ready. Choose a stereo IQ WAV (I on left, Q on right).")

    def _center_window(self):
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() // 2) - (width // 2)
        y = max(20, (self.winfo_screenheight() // 2) - (height // 2) - 40)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _append_log(self, message):
        self.log.configure(state="normal")
        self.log.insert("end", message.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def choose_input(self):
        initial = DEFAULT_INPUT_DIR if DEFAULT_INPUT_DIR.is_dir() else APP_DIR
        selected = filedialog.askopenfilename(
            title="Choose an IQ WAV file",
            initialdir=str(initial),
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if selected:
            self.input_var.set(selected)
            self.status_var.set(f"Selected: {Path(selected).name}")
            self._append_log(f"Input: {selected}")
            self._update_estimate_preview()

    def choose_output(self):
        initial = self.output_var.get().strip() or str(DEFAULT_OUTPUT)
        selected = filedialog.askdirectory(title="Choose output folder", initialdir=initial)
        if selected:
            self.output_var.set(selected)
            self._append_log(f"Output folder: {selected}")

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

    def start_conversion(self):
        input_path = Path(self.input_var.get().strip())
        output_dir = Path(self.output_var.get().strip())

        if not input_path.is_file():
            messagebox.showerror("Invalid file", "Please choose an existing WAV file.")
            return

        if input_path.suffix.lower() != ".wav":
            messagebox.showerror("Invalid file", "The selected input must be a .wav file.")
            return

        try:
            rbw = float(self.rbw_var.get())
            if rbw <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid RBW", "RBW must be a positive number.")
            return

        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            estimated = estimate_conversion_seconds(str(input_path), rbw)
        except Exception:
            estimated = 30.0

        self._converting = True
        self._progress_fraction = 0.0
        self._progress_stage = "Starting…"
        self._convert_started_at = time.monotonic()
        self._estimated_total_s = estimated
        self.progress_var.set(0)
        self.convert_button.config(state="disabled")
        self.status_var.set("Converting…")
        self.time_var.set(
            f"Elapsed 0:00 · Est. {format_duration(estimated)} remaining"
        )
        self._append_log("-" * 48)
        self._append_log(f"Starting conversion: {input_path.name}")
        self._append_log(f"RBW = {rbw:g} Hz → {output_dir}")
        self._append_log(f"Estimated time: ~{format_duration(estimated)}")
        self._schedule_timer()

        thread = threading.Thread(
            target=self._convert_worker,
            args=(input_path, output_dir, rbw),
            daemon=True,
        )
        thread.start()

    def _schedule_timer(self):
        if self._timer_job is not None:
            self.after_cancel(self._timer_job)
        self._tick_timer()

    def _tick_timer(self):
        if not self._converting:
            self._timer_job = None
            return

        elapsed = time.monotonic() - self._convert_started_at
        fraction = self._progress_fraction

        # Blend stage progress with time-based progress so the bar keeps moving
        if self._estimated_total_s > 0:
            time_fraction = min(0.95, elapsed / self._estimated_total_s)
        else:
            time_fraction = 0.0
        display_fraction = max(fraction, time_fraction * 0.85)
        if fraction >= 1.0:
            display_fraction = 1.0

        self.progress_var.set(display_fraction * 100)

        if fraction >= 0.08:
            projected_total = elapsed / max(fraction, 0.08)
            remaining = max(0.0, projected_total - elapsed)
        else:
            remaining = max(0.0, self._estimated_total_s - elapsed)

        stage = self._progress_stage or "Working…"
        self.status_var.set(stage)
        self.time_var.set(
            f"Elapsed {format_duration(elapsed)} · Est. {format_duration(remaining)} remaining"
        )

        self._timer_job = self.after(200, self._tick_timer)

    def _on_progress(self, fraction, message):
        self._progress_fraction = fraction
        self._progress_stage = message

    def _convert_worker(self, input_path, output_dir, rbw):
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                process_file(
                    str(input_path),
                    str(output_dir),
                    rbw,
                    progress_callback=lambda f, m: self.after(0, self._on_progress, f, m),
                )

            details = buffer.getvalue().strip()
            if details:
                self.after(0, self._append_log, details)

            png_path = output_dir / f"{input_path.stem}.png"
            if not png_path.is_file():
                self.after(
                    0,
                    self._conversion_failed,
                    f"Conversion finished, but the expected image was not found:\n{png_path}",
                )
                return

            self.after(0, self._conversion_succeeded, png_path)

        except Exception as exc:
            details = buffer.getvalue().strip()
            message = str(exc)
            if details:
                message = f"{details}\n{message}"
            self.after(0, self._conversion_failed, message)

    def _stop_timer(self):
        self._converting = False
        if self._timer_job is not None:
            self.after_cancel(self._timer_job)
            self._timer_job = None

    def _conversion_succeeded(self, png_path):
        elapsed = time.monotonic() - self._convert_started_at
        self._stop_timer()
        self.progress_var.set(100)
        self.convert_button.config(state="normal")
        self.status_var.set(f"Done — opened {png_path.name}")
        self.time_var.set(f"Finished in {format_duration(elapsed)}")
        self._append_log(f"Saved: {png_path}")
        self._append_log(f"Actual time: {format_duration(elapsed)}")

        try:
            os.startfile(png_path)
        except OSError as exc:
            messagebox.showwarning(
                "Image created",
                f"The PNG was created, but Windows could not open it automatically.\n\n{exc}",
            )

    def _conversion_failed(self, details):
        self._stop_timer()
        self.progress_var.set(0)
        self.convert_button.config(state="normal")
        self.status_var.set("Conversion failed.")
        self.time_var.set("")
        self._append_log(f"ERROR: {details}")
        messagebox.showerror("Conversion failed", details)

    def open_output_folder(self):
        output_dir = Path(self.output_var.get().strip() or DEFAULT_OUTPUT)
        output_dir.mkdir(parents=True, exist_ok=True)
        os.startfile(output_dir)


if __name__ == "__main__":
    enable_dpi_awareness()
    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    app = IQConverterGUI()
    app.mainloop()
