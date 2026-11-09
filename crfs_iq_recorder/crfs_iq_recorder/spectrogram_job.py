"""One spectrogram conversion in its own process.

The Recorder starts itself with ``--spectrogram-job <job.json>`` (frozen EXE) or
``python -m crfs_iq_recorder --spectrogram-job <job.json>`` (source). The child
reads the IQ WAV only, writes ``<folder>/<stem>.png`` and ``<stem>.thumb.png``
with the converter engine (root ``iq_data.process_file``) and leaves the outcome
in ``<job>.result.json``. A ``<job>.cancel`` file stops it between chunks.
"""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

JOB_FLAG = "--spectrogram-job"
RESULT_SUFFIX = ".result.json"
CANCEL_SUFFIX = ".cancel"
LOG_SUFFIX = ".log"
SPECTROGRAM_FOLDER = "Spectrograms"
THUMB_SUFFIX = ".thumb.png"
SETTINGS_ENV = "IQ_GUI_SETTINGS"
SETTINGS_NAME = ".iq_gui_settings.json"
CALIBRATION_ENV = "IQ_ETA_CALIBRATION"
ALLOWED_CMAPS = ("jet", "turbo", "viridis", "gray")
# The converter window's own defaults (iq_gui) when it has never saved settings.
DEFAULT_SETTINGS: dict[str, Any] = {
    "rbw": "15000",
    "cmap": "jet",
    "freq_smooth": False,
    "db_auto": True,
    "db_vmin": "",
    "db_vmax": "",
    "range_markers": True,
}
_SETTINGS_SEARCH_DEPTH = 4
_LOG_TAIL_LINES = 40


def job_paths(job_file: Path) -> tuple[Path, Path, Path]:
    """(result, cancel, log) files that belong to a job file."""
    job_file = Path(job_file)
    stem = job_file.with_suffix("")
    return (
        stem.with_name(stem.name + RESULT_SUFFIX),
        stem.with_name(stem.name + CANCEL_SUFFIX),
        stem.with_name(stem.name + LOG_SUFFIX),
    )


def output_paths(wav: Path | str) -> tuple[Path, Path]:
    """Full-size PNG and thumbnail for a WAV: ``<WAV folder>/Spectrograms/<stem>.png`` / ``.thumb.png``."""
    wav = Path(wav)
    folder = wav.parent / SPECTROGRAM_FOLDER
    return folder / f"{wav.stem}.png", folder / f"{wav.stem}{THUMB_SUFFIX}"


def _engine_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        roots.append(Path(sys._MEIPASS))
    here = Path(__file__).resolve()
    # Source checkout: <converter>/crfs_iq_recorder/crfs_iq_recorder/spectrogram_job.py
    roots.append(here.parents[2])
    return roots


def import_engine():
    """The converter's ``iq_data`` module (bundled in the EXE, beside the package in a checkout)."""
    try:
        import iq_data  # noqa: PLC0415
    except ImportError:
        for root in _engine_roots():
            if (root / "iq_data.py").is_file() and str(root) not in sys.path:
                sys.path.append(str(root))
        import iq_data  # noqa: PLC0415
    return iq_data


def find_converter_settings(start: Path | None = None) -> Path | None:
    """The converter window's saved settings, if this PC has them.

    IQ_GUI_SETTINGS wins (the converter honours it too); otherwise the first
    ``.iq_gui_settings.json`` in the Recorder's folder or a few folders above it
    (the converter keeps it in its own folder, which holds this package).
    """
    override = os.environ.get(SETTINGS_ENV, "").strip()
    if override:
        path = Path(override)
        return path if path.is_file() else None
    if start is None:
        start = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
    folder = Path(start)
    for _ in range(_SETTINGS_SEARCH_DEPTH + 1):
        candidate = folder / SETTINGS_NAME
        if candidate.is_file():
            return candidate
        if folder.parent == folder:
            break
        folder = folder.parent
    return None


def converter_settings(path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Settings for the conversion (validated like the converter window does) and where they came from."""
    settings = dict(DEFAULT_SETTINGS)
    source = "converter defaults"
    if path is None:
        path = find_converter_settings()
    if path is None:
        return settings, source
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return settings, source
    if not isinstance(data, dict):
        return settings, source
    source = str(path)
    rbw = data.get("rbw")
    try:
        if rbw is not None and float(rbw) > 0:
            settings["rbw"] = str(rbw).strip()
    except (TypeError, ValueError):
        pass
    cmap = data.get("cmap")
    if isinstance(cmap, str) and cmap.strip().lower() in ALLOWED_CMAPS:
        settings["cmap"] = cmap.strip().lower()
    for key in ("freq_smooth", "db_auto", "range_markers"):
        if isinstance(data.get(key), bool):
            settings[key] = data[key]
    for key in ("db_vmin", "db_vmax"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            settings[key] = str(value)
        elif isinstance(value, str):
            settings[key] = value.strip()
    return settings, source


def _db_limit(text: Any) -> float | None:
    try:
        return float(str(text).strip()) if str(text).strip() else None
    except ValueError:
        return None


def process_memory_mb() -> dict[str, float]:
    """Peak working set and peak private bytes of this process, in MB (Windows)."""
    if sys.platform != "win32":
        return {}
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    class _Counters(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = _Counters()
    counters.cb = ctypes.sizeof(_Counters)
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_Counters), wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            return {}
    except (OSError, AttributeError):
        return {}
    return {
        "peak_working_set_mb": round(counters.PeakWorkingSetSize / 2**20, 1),
        "peak_private_mb": round(counters.PeakPagefileUsage / 2**20, 1),
    }


def _write_result(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _watch_cancel(cancel_file: Path, event: threading.Event, done: threading.Event) -> None:
    while not done.wait(0.5):
        if cancel_file.exists():
            event.set()
            return


def convert(job: dict[str, Any], *, cancel_event: threading.Event | None = None) -> dict[str, Any]:
    """Run one conversion described by a job dict; returns the result dict (never raises for engine errors)."""
    wav = Path(job["wav"])
    png, thumb = output_paths(wav)
    folder = Path(job.get("folder") or png.parent)
    png = folder / png.name
    thumb = folder / thumb.name
    settings = dict(DEFAULT_SETTINGS)
    settings.update(job.get("settings") or {})
    result: dict[str, Any] = {"id": job.get("id"), "ok": False, "wav": str(wav)}
    if job.get("calibration"):
        os.environ[CALIBRATION_ENV] = str(job["calibration"])
    started = time.perf_counter()
    buffer = io.StringIO()
    try:
        before = wav.stat()
        iq = import_engine()
        stats: dict[str, Any] = {}
        folder.mkdir(parents=True, exist_ok=True)
        with redirect_stdout(buffer), redirect_stderr(buffer):
            out = iq.process_file(
                str(wav),
                str(folder),
                settings["rbw"],
                cmap=settings["cmap"],
                freq_smooth=bool(settings["freq_smooth"]),
                db_auto=bool(settings["db_auto"]),
                db_vmin=_db_limit(settings.get("db_vmin")),
                db_vmax=_db_limit(settings.get("db_vmax")),
                cancel_event=cancel_event,
                output_name=png.name,
                stats=stats,
                range_markers=bool(settings["range_markers"]),
                recorder_history=job.get("history") or None,
                thumbnail_name=thumb.name,
                title_centre=True,
            )
        after = wav.stat()
        axis = stats.get("frequency_axis") or {}
        lookup = stats.get("request_lookup") or {}
        result.update(
            {
                "ok": True,
                "png": str(out),
                "thumb": str(stats.get("thumbnail_path") or thumb),
                "absolute_axis": bool(axis.get("absolute")),
                "centre_hz": axis.get("centre_hz"),
                "request_status": lookup.get("status", ""),
                "markers": len(stats.get("range_lines") or []),
                "marker_kinds": sorted({str(line.get("kind") or "") for line in stats.get("range_lines") or []}),
                "title": stats.get("title", ""),
                "fft": iq.fft_backend() if hasattr(iq, "fft_backend") else "",
                "wav_unchanged": (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns),
            }
        )
    except BaseException as exc:  # noqa: BLE001 - every failure goes back to the queue as text
        cancelled = type(exc).__name__ == "ConversionCancelled"
        result.update(
            {
                "ok": False,
                "cancelled": cancelled,
                "error_kind": type(exc).__name__,
                "error": str(exc).strip().splitlines()[0][:300] if str(exc).strip() else type(exc).__name__,
                "traceback": "" if cancelled else traceback.format_exc(limit=6)[-2000:],
            }
        )
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            result["error"] = "stopped"
    result["seconds"] = round(time.perf_counter() - started, 2)
    result["log_tail"] = buffer.getvalue().splitlines()[-_LOG_TAIL_LINES:]
    result.update(process_memory_mb())
    result["pid"] = os.getpid()
    result["meipass"] = getattr(sys, "_MEIPASS", "")
    return result


def run_job(job_file: str | Path) -> int:
    """Child entry point: 0 converted, 1 failed, 2 bad job file, 3 cancelled."""
    job_file = Path(job_file)
    result_file, cancel_file, _log = job_paths(job_file)
    try:
        job = json.loads(job_file.read_text(encoding="utf-8"))
        if not isinstance(job, dict) or not job.get("wav"):
            raise ValueError("job has no WAV")
    except (OSError, ValueError) as exc:
        try:
            _write_result(result_file, {"ok": False, "error_kind": "BadJob", "error": str(exc)[:300]})
        except OSError:
            pass
        return 2
    cancel_event = threading.Event()
    done = threading.Event()
    watcher = threading.Thread(target=_watch_cancel, args=(cancel_file, cancel_event, done), daemon=True)
    watcher.start()
    try:
        result = convert(job, cancel_event=cancel_event)
    finally:
        done.set()
    _write_result(result_file, result)
    if result.get("ok"):
        return 0
    return 3 if result.get("cancelled") else 1


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] != JOB_FLAG:
        print(f"usage: {JOB_FLAG} <job.json>", file=sys.stderr)
        return 2
    return run_job(argv[1])
