"""Background spectrograms for verified IQ downloads, and their thumbnails in Excel.

After a downloaded IQ file is verified, ``SpectrogramService.request`` queues
it. Conversions run one at a time in a separate process (this application
started with ``--spectrogram-job``, below-normal priority, no window, ended
with the application), so a 2 GB recording never stalls the window, a
download or a recording. The full-size PNG and the thumbnail go to
``<WAV folder>/Spectrograms/``. The IQ file is only read.

When a spectrogram is ready, every Excel row that lists that file (the
collection log and the protocol results) gets a picture entry in the workbook
queue; ``excel_images.flush_images`` places it once the row is in the sheet.
Both queues are JSON files in the settings folder and survive a restart.
A failed conversion is retried a few times, then reported once; it never
stops a collection and never removes a recording entry.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from . import spectrogram_job as job_mod
from .collection_catalog import HEADER_IQ
from .diagnostic_log import diag
from .json_store import read_json, write_json

STORE_NAME = "spectrograms.json"
JOBS_FOLDER = "spectrogram_jobs"
CALIBRATION_NAME = ".iq_eta_calibration.json"
SCHEMA = 1
MAX_PENDING = 500
MAX_HISTORY = 3000
MAX_ATTEMPTS = 3
RETRY_DELAYS_S = (30, 300)
POLL_MS = 500
START_DELAY_MS = 4000
TICK_MS = 5000
IMAGE_DEBOUNCE_MS = 2000
IMAGE_BUSY_RETRY_MS = 15000
IMAGE_RECHECK_S = 60.0
IMAGE_MAX_WAIT_DAYS = 14
RETARGET_HOURS = 48
RETARGET_EVERY_S = 60.0
CANCEL_GRACE_S = 3.0
JOB_TIMEOUT_S = 3 * 3600
_BELOW_NORMAL = 0x00004000
_NO_WINDOW = 0x08000000

ST_PENDING = "pending"
ST_RUNNING = "running"
ST_DONE = "done"
ST_FAILED = "failed"
ST_SKIPPED = "skipped"
# Engine errors that mean "this WAV is not an IQ recording": no spectrogram, no failure report.
_NOT_IQ = ("Need stereo IQ", "WAV file has no samples")


def wav_key(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def file_stamp(path: Path | str) -> str:
    try:
        st = os.stat(path)
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse(text: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(text)) if text else None
    except ValueError:
        return None


def job_command(job_file: Path) -> tuple[list[str], str | None]:
    """(argv, working folder) that runs one job in a child process (frozen EXE or source checkout)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, job_mod.JOB_FLAG, str(job_file)], None
    package_parent = Path(__file__).resolve().parents[1]
    return [sys.executable, "-m", "crfs_iq_recorder", job_mod.JOB_FLAG, str(job_file)], str(package_parent)


class SpectrogramStore:
    """Conversion jobs, one per local WAV (JSON, atomic writes)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        data = read_json(self.path)
        jobs = data.get("jobs") if isinstance(data, dict) else None
        self.jobs: list[dict[str, Any]] = [dict(j) for j in jobs or [] if isinstance(j, dict) and j.get("wav")]

    def save(self) -> None:
        pending = [j for j in self.jobs if j.get("state") in {ST_PENDING, ST_RUNNING}]
        history = [j for j in self.jobs if j.get("state") not in {ST_PENDING, ST_RUNNING}]
        self.jobs = history[-MAX_HISTORY:] + pending
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, {"schema": SCHEMA, "jobs": self.jobs})

    def find(self, key: str) -> dict[str, Any] | None:
        for job in self.jobs:
            if job.get("key") == key:
                return job
        return None

    def pending(self) -> list[dict[str, Any]]:
        return [j for j in self.jobs if j.get("state") == ST_PENDING]

    def next_due(self, now: datetime) -> dict[str, Any] | None:
        for job in self.jobs:
            if job.get("state") != ST_PENDING:
                continue
            due = _parse(job.get("not_before"))
            if due is None or due <= now:
                return job
        return None


class _KillOnClose:
    """Windows job object: a child assigned to it ends when this process ends (crash included)."""

    def __init__(self) -> None:
        self.handle = None
        if sys.platform != "win32":
            return
        try:
            import ctypes  # noqa: PLC0415
            from ctypes import wintypes  # noqa: PLC0415

            class _Basic(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_int64),
                    ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class _Io(ctypes.Structure):
                _fields_ = [(name, ctypes.c_uint64) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
                )]

            class _Extended(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", _Basic),
                    ("IoInfo", _Io),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return
            info = _Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                kernel32.CloseHandle(handle)
                return
            self.handle = handle
            self._kernel32 = kernel32
        except (OSError, AttributeError):
            self.handle = None

    def assign(self, proc: subprocess.Popen) -> bool:
        if self.handle is None:
            return False
        try:
            return bool(self._kernel32.AssignProcessToJobObject(self.handle, int(proc._handle)))  # noqa: SLF001
        except (OSError, AttributeError):
            return False


class SpectrogramService(QObject):
    """Queue, child process and Excel pictures for spectrograms of verified downloads."""

    changed = Signal()

    def __init__(
        self,
        host=None,
        *,
        config_dir: Path,
        workbook_queue: Callable[[], Any],
        excel_store: Callable[[], Any] | None = None,
        start_job: Callable[..., None] | None = None,
        log: Callable[..., None] | None = None,
        history_path: Callable[[], Path] | None = None,
        launcher: Callable[[Path], Any] | None = None,
        image_flusher: Callable[..., Any] | None = None,
        settings: Callable[[], tuple[dict[str, Any], str]] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.host = host
        self._config_dir = Path(config_dir)
        self._workbook_queue = workbook_queue
        self._excel_store = excel_store or (lambda: None)
        self._start_job = start_job or (getattr(host, "_start_job", None) if host is not None else None)
        self._log_fn = log or (getattr(host, "log", None) if host is not None else None)
        if history_path is None:
            from .paths import recordings_path  # noqa: PLC0415

            history_path = recordings_path
        self._history_path = history_path
        self._launcher = launcher or self._launch_child
        self._image_flusher = image_flusher
        self._settings = settings or job_mod.converter_settings
        self.store = SpectrogramStore(self._config_dir / STORE_NAME)
        self._jobs_dir = self._config_dir / JOBS_FOLDER
        self._proc = None
        self._proc_job: dict[str, Any] | None = None
        self._proc_started = 0.0
        self._kill_on_close = None
        self._alive = True
        self._images_flushing = False
        self._images_again = False
        self._images_last = 0.0
        self._retarget_last = 0.0
        self._deferred_noted: set[str] = set()
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._check_child)
        self._retry = QTimer(self)
        self._retry.setSingleShot(True)
        self._retry.timeout.connect(self._pump)
        self._images_timer = QTimer(self)
        self._images_timer.setSingleShot(True)
        self._images_timer.timeout.connect(self._flush_images)
        self._tick = QTimer(self)
        self._tick.setInterval(TICK_MS)
        self._tick.timeout.connect(self._on_tick)
        self._recover()
        QTimer.singleShot(START_DELAY_MS, self, self._start)

    # ------------------------------------------------------------------ public
    def request(self, wav: Path | str, *, sensor_id: str = "", remote_path: str = "", origin: str = "") -> bool:
        """Queue a verified local IQ WAV; False when it is already queued or converted unchanged."""
        if not self._alive:
            return False
        path = Path(wav)
        if path.suffix.casefold() != ".wav" or not path.is_file():
            return False
        key, stamp = wav_key(path), file_stamp(path)
        job = self.store.find(key)
        if job is not None and job.get("stamp") == stamp:
            if job.get("state") == ST_DONE and Path(str(job.get("thumb") or "")).is_file():
                self._attach(job)
            return False
        if job is not None and job.get("state") == ST_RUNNING:
            # Replaced on disk while it converts: convert again when this run ends.
            job["again"] = True
            return True
        fresh = {
            "id": uuid.uuid4().hex[:12],
            "key": key,
            "wav": str(path),
            "name": path.name,
            "stamp": stamp,
            "sensor_id": sensor_id,
            "remote_path": remote_path,
            "origin": origin,
            "state": ST_PENDING,
            "attempts": 0,
            "queued_at": _now(),
        }
        if job is not None:
            job.clear()
            job.update(fresh)
        else:
            self.store.jobs.append(fresh)
            pending = self.store.pending()
            if len(pending) > MAX_PENDING:
                for stale in pending[: len(pending) - MAX_PENDING]:
                    stale["state"] = ST_FAILED
                    stale["error"] = "queue full"
                    diag("spectrogram_dropped", stale.get("name", ""), reason="queue full")
        self._save()
        diag("spectrogram_queued", path.name, origin=origin, pending=len(self.store.pending()))
        QTimer.singleShot(0, self, self._pump)
        return True

    def pending_count(self) -> int:
        return len(self.store.pending()) + (1 if self._proc is not None else 0)

    def running_name(self) -> str:
        return str((self._proc_job or {}).get("name") or "")

    def flush_images_soon(self, delay_ms: int = IMAGE_DEBOUNCE_MS) -> None:
        if self._alive:
            self._images_timer.start(max(0, int(delay_ms)))

    def shutdown(self) -> None:
        """Stop timers; a running conversion is cancelled (resumed from the start after the next launch)."""
        self._alive = False
        for timer in (self._poll, self._retry, self._images_timer, self._tick):
            timer.stop()
        proc, job = self._proc, self._proc_job
        if proc is None or job is None:
            return
        _result, cancel, _log = job_mod.job_paths(self._job_file(job))
        try:
            cancel.write_text("cancel", encoding="utf-8")
        except OSError:
            pass
        deadline = time.monotonic() + CANCEL_GRACE_S
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.05)
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
        job["state"] = ST_PENDING
        job.pop("started_at", None)
        self._proc = self._proc_job = None
        self._cleanup(job)
        self._save()
        diag("spectrogram_interrupted", job.get("name", ""), reason="application closed")

    # ------------------------------------------------------------------ queue
    def _save(self) -> None:
        try:
            self.store.save()
        except OSError as exc:
            diag("spectrogram_store_error", str(exc)[:200])
        self.changed.emit()

    def _log(self, message: str, level: str = "info") -> None:
        if self._log_fn is not None and self._alive:
            try:
                self._log_fn(message, level=level)
                return
            except TypeError:
                self._log_fn(message)
                return
        diag("activity", message, level=level)

    def _recover(self) -> None:
        """A job left 'running' by the last session: take its result if the child finished, else run it again."""
        changed = False
        for job in self.store.jobs:
            if job.get("state") != ST_RUNNING:
                continue
            result_file, _cancel, _log = job_mod.job_paths(self._job_file(job))
            result = read_json(result_file, backup=False) if result_file.is_file() else None
            if isinstance(result, dict) and result.get("ok"):
                self._finish(job, result, quiet=True)
            else:
                job["state"] = ST_PENDING
                job.pop("started_at", None)
                self._cleanup(job)
            changed = True
        if changed:
            self._save()

    def _start(self) -> None:
        if not self._alive:
            return
        self._tick.start()
        self._pump()
        if self._images_pending():
            self.flush_images_soon(0)

    def _job_file(self, job: dict[str, Any]) -> Path:
        return self._jobs_dir / f"{job['id']}.json"

    def _cleanup(self, job: dict[str, Any]) -> None:
        job_file = self._job_file(job)
        for path in (job_file, job_file.with_name(job_file.name + ".bak"), *job_mod.job_paths(job_file)):
            try:
                path.unlink()
            except OSError:
                pass

    def _pump(self) -> None:
        if not self._alive or self._proc is not None:
            return
        now = datetime.now()
        job = self.store.next_due(now)
        if job is None:
            waiting = [(_parse(j.get("not_before")) or now) for j in self.store.pending()]
            if waiting:
                delay = max(1.0, (min(waiting) - now).total_seconds())
                self._retry.start(int(min(delay, 3600) * 1000))
            return
        wav = Path(job["wav"])
        if not wav.is_file():
            job["state"] = ST_FAILED
            job["error"] = "the IQ file is no longer there"
            self._save()
            diag("spectrogram_failed", job["name"], error=job["error"])
            QTimer.singleShot(0, self, self._pump)
            return
        settings, source = self._settings()
        payload = {
            "id": job["id"],
            "wav": str(wav),
            "history": str(self._history_path()),
            "settings": settings,
            "settings_source": source,
            "calibration": str(self._config_dir / CALIBRATION_NAME),
        }
        job_file = self._job_file(job)
        try:
            self._jobs_dir.mkdir(parents=True, exist_ok=True)
            self._cleanup(job)
            write_json(job_file, payload, backup=False)
            proc = self._launcher(job_file)
        except Exception as exc:  # noqa: BLE001 - counted as a failed attempt, retried
            self._failed_attempt(job, f"could not start the conversion ({type(exc).__name__}: {exc})"[:300])
            return
        self._proc, self._proc_job, self._proc_started = proc, job, time.monotonic()
        job["state"] = ST_RUNNING
        job["started_at"] = _now()
        job["settings_source"] = source
        self._save()
        diag("spectrogram_started", job["name"], attempt=int(job.get("attempts") or 0) + 1, settings=source, rbw=settings.get("rbw"), cmap=settings.get("cmap"))
        self._log(f"Generating spectrogram: {job['name']}.")
        self._poll.start()

    def _launch_child(self, job_file: Path):
        argv, cwd = job_command(job_file)
        _result, _cancel, log_file = job_mod.job_paths(job_file)
        flags = (_BELOW_NORMAL | _NO_WINDOW) if sys.platform == "win32" else 0
        with open(log_file, "wb") as log:
            proc = subprocess.Popen(  # noqa: S603 - our own executable with our own arguments
                argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, creationflags=flags, close_fds=True,
            )
        if self._kill_on_close is None:
            self._kill_on_close = _KillOnClose()
        self._kill_on_close.assign(proc)
        return proc

    def _check_child(self) -> None:
        proc, job = self._proc, self._proc_job
        if proc is None or job is None:
            self._poll.stop()
            return
        code = proc.poll()
        if code is None:
            if time.monotonic() - self._proc_started > JOB_TIMEOUT_S:
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                self._proc = self._proc_job = None
                self._poll.stop()
                self._failed_attempt(job, f"stopped after {JOB_TIMEOUT_S // 3600} h")
            return
        self._poll.stop()
        self._proc = self._proc_job = None
        result_file, _cancel, log_file = job_mod.job_paths(self._job_file(job))
        result = read_json(result_file, backup=False) if result_file.is_file() else None
        if not isinstance(result, dict):
            tail = ""
            try:
                tail = log_file.read_text(encoding="utf-8", errors="replace")[-600:]
            except OSError:
                pass
            result = {"ok": False, "error": f"the conversion process ended (exit code {code}) without a result", "log_tail": tail.splitlines()[-8:]}
        self._finish(job, result)
        QTimer.singleShot(0, self, self._pump)

    def _finish(self, job: dict[str, Any], result: dict[str, Any], *, quiet: bool = False) -> None:
        if job.pop("again", False):
            self._cleanup(job)
            job.update(state=ST_PENDING, stamp=file_stamp(job["wav"]), attempts=0, error="")
            job.pop("not_before", None)
            job.pop("attached", None)
            self._save()
            return
        info = {k: result.get(k) for k in (
            "seconds", "peak_working_set_mb", "peak_private_mb", "fft", "absolute_axis", "centre_hz",
            "request_status", "markers", "wav_unchanged",
        ) if result.get(k) is not None}
        if result.get("ok"):
            job.update(state=ST_DONE, png=result.get("png"), thumb=result.get("thumb"), finished_at=_now(), error="", info=info)
            job["thumb_stamp"] = file_stamp(job["thumb"]) if job.get("thumb") else ""
            self._cleanup(job)
            self._save()
            diag("spectrogram_done", job["name"], **info)
            attached = self._attach(job)
            if not attached and not quiet:
                self._log(f"Spectrogram saved: {job['name']}.")
            return
        error = str(result.get("error") or "unknown error")
        if any(token in error for token in _NOT_IQ):
            job.update(state=ST_SKIPPED, error=error, finished_at=_now())
            self._cleanup(job)
            self._save()
            diag("spectrogram_skipped", job["name"], reason=error)
            return
        diag(
            "spectrogram_error", job["name"], error=error, kind=result.get("error_kind"),
            traceback=result.get("traceback", ""), log_tail=result.get("log_tail"), **info,
        )
        self._failed_attempt(job, error)

    def _failed_attempt(self, job: dict[str, Any], error: str) -> None:
        job["attempts"] = int(job.get("attempts") or 0) + 1
        job["error"] = error[:300]
        job.pop("started_at", None)
        if job["attempts"] >= MAX_ATTEMPTS:
            job["state"] = ST_FAILED
            job["finished_at"] = _now()
            self._cleanup(job)
            self._save()
            diag("spectrogram_failed", job["name"], error=job["error"], attempts=job["attempts"])
            self._log(f"Spectrogram failed: {job['name']}. See details.", level="error")
        else:
            delay = RETRY_DELAYS_S[min(job["attempts"] - 1, len(RETRY_DELAYS_S) - 1)]
            job["state"] = ST_PENDING
            job["not_before"] = (datetime.now() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            self._save()
            diag("spectrogram_retry", job["name"], error=job["error"], attempt=job["attempts"], retry_in_s=delay)
        QTimer.singleShot(0, self, self._pump)

    # ------------------------------------------------------------------ Excel
    def _queue(self):
        try:
            return self._workbook_queue()
        except Exception as exc:  # noqa: BLE001
            diag("spectrogram_queue_unavailable", str(exc)[:200])
            return None

    def targets_for(self, filename: str) -> list[dict[str, Any]]:
        """Every Excel row that lists this IQ file (collection log and protocol results)."""
        want = str(filename or "").casefold()
        return self._targets_many({want}).get(want, [])

    def _targets_many(self, names: set[str]) -> dict[str, list[dict[str, Any]]]:
        from .cellular_workbook import image_key  # noqa: PLC0415
        from .excel_images import KIND_LOG, KIND_PROTOCOL  # noqa: PLC0415

        found: dict[str, dict[str, dict[str, Any]]] = {}
        store = self._excel_store()
        if store is not None and hasattr(store, "rows_listing_many"):
            for name, rows in store.rows_listing_many(names).items():
                for book, sheet, _queued in rows:
                    if book and sheet:
                        found.setdefault(name, {}).setdefault(image_key(book, sheet, name), {
                            "kind": KIND_LOG, "workbook": book, "sheet": sheet, "header_row": 1, "iq_header": HEADER_IQ,
                            "iq_col": 0, "row_hint": 0,
                        })
        queue = self._queue()
        if queue is not None:
            for name, rows in queue.rows_listing_many(names).items():
                for target in rows:
                    found.setdefault(name, {}).setdefault(image_key(target["workbook"], target["sheet"], name), {
                        "kind": KIND_PROTOCOL, "workbook": target["workbook"], "sheet": target["sheet"],
                        "header_row": int(target.get("header_row") or 1), "iq_header": str(target.get("iq_header") or ""),
                        "iq_col": int(target.get("iq_col") or 0), "row_hint": int(target.get("row") or 0),
                    })
        return {name: list(rows.values()) for name, rows in found.items()}

    def _attach(self, job: dict[str, Any], targets: list[dict[str, Any]] | None = None) -> int:
        """Queue a picture for every row that lists this file and has none yet; the number of rows it is in."""
        from .cellular_workbook import image_key  # noqa: PLC0415

        thumb, png = str(job.get("thumb") or ""), str(job.get("png") or "")
        if not thumb or not Path(thumb).is_file():
            return 0
        if targets is None:
            targets = self.targets_for(job["name"])
        if not targets:
            return 0
        queue = self._queue()
        if queue is None:
            return len(targets)
        attached = set(job.get("attached") or [])
        added = 0
        for target in targets:
            key = image_key(target["workbook"], target["sheet"], job["name"])
            if key in attached:
                continue
            item = dict(target, file=job["name"], thumb=thumb, png=png, thumb_stamp=job.get("thumb_stamp") or file_stamp(thumb))
            if queue.enqueue_image(item):
                added += 1
            attached.add(key)
        if added or len(attached) != len(job.get("attached") or []):
            job["attached"] = sorted(attached)
            self._save()
        if added:
            diag("spectrogram_images_queued", job["name"], rows=added)
            self.flush_images_soon()
        return len(targets)

    def _retarget(self) -> None:
        """Rows logged after their spectrogram was made (or a log enabled later) still get the picture."""
        now = time.monotonic()
        if now - self._retarget_last < RETARGET_EVERY_S:
            return
        self._retarget_last = now
        cutoff = datetime.now() - timedelta(hours=RETARGET_HOURS)
        recent = [
            job for job in self.store.jobs
            if job.get("state") == ST_DONE and not job.get("attached") and (_parse(job.get("finished_at")) or cutoff) > cutoff
        ]
        if not recent:
            return
        index = self._targets_many({str(job["name"]).casefold() for job in recent})
        for job in recent:
            targets = index.get(str(job["name"]).casefold())
            if targets:
                self._attach(job, targets)

    def _queued_names(self) -> set[str]:
        """IQ files whose Excel row is still waiting to be written (their pictures wait with them)."""
        names: set[str] = set()
        store = self._excel_store()
        if store is not None and hasattr(store, "snapshot_queue"):
            for item in store.snapshot_queue():
                names.update(name.casefold() for name in item.filenames())
        queue = self._queue()
        if queue is not None:
            for item in queue.pending_items():
                names.update(str(name).casefold() for name in item.result.files)
        return names

    def _images_pending(self) -> bool:
        queue = self._queue()
        return bool(queue is not None and queue.image_count())

    def _on_tick(self) -> None:
        if not self._alive:
            return
        self._retarget()
        if self._images_flushing:
            return
        queue = self._queue()
        if queue is None or not queue.image_count():
            return
        queued = self._queued_names()
        items = queue.pending_images()
        ready = [item for item in items if str(item.get("file") or "").casefold() not in queued]
        fresh = [item for item in ready if not item.get("last_try")]
        if fresh or (ready and time.monotonic() - self._images_last >= IMAGE_RECHECK_S):
            self._flush_images()
        self._drop_stale(queue, items)

    def _drop_stale(self, queue, items: list[dict[str, Any]]) -> None:
        cutoff = datetime.now() - timedelta(days=IMAGE_MAX_WAIT_DAYS)
        stale = [item["key"] for item in items if (_parse(item.get("queued_at")) or datetime.now()) < cutoff]
        if stale:
            for item in queue.drop_images(stale, f"its row was not found for {IMAGE_MAX_WAIT_DAYS} days"):
                diag("spectrogram_image_dropped", item.get("file", ""), workbook=Path(str(item.get("workbook") or "")).name, reason=item.get("last_error"))

    def _backup(self, workbook: Path) -> Path | None:
        """Once per workbook, as the row writers do (a backup the Excel log already made counts)."""
        from .cellular_workbook import ensure_backup  # noqa: PLC0415

        store = self._excel_store()
        known = getattr(store, "backups", None) if store is not None else None
        if isinstance(known, dict):
            try:
                existing = known.get(str(Path(workbook).resolve()))
            except OSError:
                existing = None
            if existing and Path(existing).is_file():
                return None
        queue = self._queue()
        return ensure_backup(queue, Path(workbook)) if queue is not None else None

    def _flush_images(self) -> None:
        if not self._alive:
            return
        if self._images_flushing:
            self._images_again = True
            return
        queue = self._queue()
        if queue is None or not queue.image_count():
            return
        from .excel_images import ImageFlushReport, flush_images  # noqa: PLC0415

        flusher = self._image_flusher or flush_images
        queued = self._queued_names()

        def row_queued(item: dict[str, Any]) -> bool:
            return str(item.get("file") or "").casefold() in queued

        def work() -> Any:
            try:
                return flusher(queue, backup=self._backup, row_queued=row_queued)
            except Exception as exc:  # noqa: BLE001 - the pictures stay queued
                report = ImageFlushReport()
                report.errors.append(f"{type(exc).__name__}: {exc}"[:300])
                return report

        self._images_flushing = True
        self._images_last = time.monotonic()
        if self._start_job is None:
            self._images_done(work())
        else:
            self._start_job(work, self._images_done)

    def _images_done(self, report: Any) -> None:
        self._images_flushing = False
        if not self._alive:
            return
        written = list(getattr(report, "written", []) or [])
        if written:
            diag("spectrogram_images_written", "", count=len(written), workbooks=[Path(b).name for b in report.workbooks], columns=report.columns_created)
            if len(written) == 1:
                self._log("Spectrogram added.")
            else:
                self._log(f"Spectrograms added: {len(written)}.")
        for note in getattr(report, "deferred", []) or []:
            diag("spectrogram_images_deferred", note)
        for book in getattr(report, "deferred_books", []) or []:
            if book.casefold() not in self._deferred_noted:
                self._deferred_noted.add(book.casefold())
                self._log(f"Spectrograms for {book} are added when it is open in Excel. See details.")
        for item in getattr(report, "given_up", []) or []:
            diag("spectrogram_image_failed", item.get("file", ""), workbook=Path(str(item.get("workbook") or "")).name, reason=item.get("last_error"))
            self._log(f"Spectrogram failed: {item.get('file', '')}. See details.", level="error")
        for error in getattr(report, "errors", []) or []:
            diag("spectrogram_images_error", error)
        if getattr(report, "busy", ""):
            diag("spectrogram_images_busy", report.busy)
            self._images_timer.start(IMAGE_BUSY_RETRY_MS)
        if self._images_again:
            self._images_again = False
            self.flush_images_soon()
