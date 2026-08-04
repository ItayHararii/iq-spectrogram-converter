import io
import os
import sys
import threading
import tkinter as tk
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from iq_data import process_file


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = app_dir()
DEFAULT_OUTPUT = APP_DIR / "IQ Results"
DEFAULT_INPUT_DIR = APP_DIR / "IQ Collection"

COLORS = {
    "bg": "#eef1f4",
    "surface": "#ffffff",
    "header": "#1a2332",
    "header_text": "#f4f7fb",
    "muted": "#5c6b7a",
    "text": "#1a2332",
    "accent": "#0d9488",
    "accent_hover": "#0f766e",
    "accent_text": "#ffffff",
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


class IQConverterGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("IQ Spectrogram Converter")
        self.geometry("760x640")
        self.minsize(700, 580)
        self.configure(bg=COLORS["bg"])

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar(value=str(DEFAULT_OUTPUT))
        self.rbw_var = tk.StringVar(value="15000")
        self.status_var = tk.StringVar(value="Select a stereo IQ WAV file to begin.")

        self._configure_styles()
        self._build_ui()
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
            foreground="#94a3b8",
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
            background=[("active", COLORS["accent_hover"]), ("disabled", "#99b8b4")],
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
            thickness=8,
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

    def _build_ui(self):
        header = ttk.Frame(self, style="Header.TFrame", padding=(24, 18))
        header.pack(fill="x")
        ttk.Label(header, text="IQ Spectrogram Converter", style="HeaderTitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            header,
            text="Turn stereo IQ WAV recordings into spectrogram images",
            style="HeaderSub.TLabel",
        ).pack(anchor="w", pady=(4, 0))

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

        self.progress = ttk.Progressbar(
            body, mode="indeterminate", style="App.Horizontal.TProgressbar"
        )
        self.progress.pack(fill="x", pady=(0, 8))

        ttk.Label(body, textvariable=self.status_var, style="Status.TLabel", wraplength=700).pack(
            anchor="w", pady=(0, 8)
        )

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

    def choose_output(self):
        initial = self.output_var.get().strip() or str(DEFAULT_OUTPUT)
        selected = filedialog.askdirectory(title="Choose output folder", initialdir=initial)
        if selected:
            self.output_var.set(selected)
            self._append_log(f"Output folder: {selected}")

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

        self.convert_button.config(state="disabled")
        self.progress.start(12)
        self.status_var.set("Converting… large IQ files can take a while.")
        self._append_log("-" * 48)
        self._append_log(f"Starting conversion: {input_path.name}")
        self._append_log(f"RBW = {rbw:g} Hz → {output_dir}")

        thread = threading.Thread(
            target=self._convert_worker,
            args=(input_path, output_dir, rbw),
            daemon=True,
        )
        thread.start()

    def _convert_worker(self, input_path, output_dir, rbw):
        buffer = io.StringIO()
        try:
            with redirect_stdout(buffer), redirect_stderr(buffer):
                process_file(str(input_path), str(output_dir), rbw)

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

    def _conversion_succeeded(self, png_path):
        self.progress.stop()
        self.convert_button.config(state="normal")
        self.status_var.set(f"Done — opened {png_path.name}")
        self._append_log(f"Saved: {png_path}")

        try:
            os.startfile(png_path)
        except OSError as exc:
            messagebox.showwarning(
                "Image created",
                f"The PNG was created, but Windows could not open it automatically.\n\n{exc}",
            )

    def _conversion_failed(self, details):
        self.progress.stop()
        self.convert_button.config(state="normal")
        self.status_var.set("Conversion failed.")
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
