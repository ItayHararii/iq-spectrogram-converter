"""Copy one recording's files from the sensor into the download folder, one file at a time."""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

from .diagnostic_log import diag
from .worker_threads import track as track_worker_thread


class RecordingDownload(QObject):
    progress = Signal(str)  # "Downloading 1 of 2"
    finished = Signal(list)  # local paths, in the sensor's order
    failed = Signal(str)

    def __init__(self, conn, remote_paths: list[str], sizes: dict[str, int], folder: Path, parent=None, *, sensor: str = "") -> None:
        super().__init__(parent)
        self._conn = conn
        self._remote = list(remote_paths)
        self._sizes = dict(sizes)
        self._folder = Path(folder)
        self._sensor = str(sensor or "")
        self._index = 0
        self._local: list[str] = []
        self._cancel = threading.Event()
        self._thread: QThread | None = None
        self._worker = None
        # Destinations mirror the sensor's dated folder; all parts of the recording share one folder.
        self._destinations: dict[str, Path] = {}

    def start(self) -> None:
        from .download_paths import plan_destinations

        try:
            self._folder.mkdir(parents=True, exist_ok=True)
            self._destinations = plan_destinations(self._folder, self._remote, lambda p: int(self._sizes.get(p, 0) or 0), sensor=self._sensor)
            folders = {path.parent for path in self._destinations.values()}
            for sub in folders:
                sub.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            diag("download_failed", "download folder unavailable", error=str(exc))
            self.failed.emit("The download folder cannot be used. Check Settings.")
            return
        self._next()

    def abort(self) -> None:
        self._cancel.set()
        worker = self._worker
        if worker is not None:
            worker.abort()

    def busy(self) -> bool:
        return self._thread is not None

    def _next(self) -> None:
        from .sftp_window import FileDownloadWorker

        while self._index < len(self._remote):
            remote = self._remote[self._index]
            name = remote.replace("\\", "/").rsplit("/", 1)[-1]
            local = self._destinations.get(remote) or (self._folder / name)
            size = int(self._sizes.get(remote, 0) or 0)
            if local.is_file() and size > 0 and local.stat().st_size == size:
                diag("download_present", name, size=size)
                self._local.append(str(local))
                self._index += 1
                continue
            self.progress.emit(f"Downloading {self._index + 1} of {len(self._remote)}" if len(self._remote) > 1 else "Downloading")
            thread = QThread()
            worker = FileDownloadWorker(self._conn, remote, str(local), name, expected_size=size, cancel=self._cancel)
            worker.moveToThread(thread)
            queued = Qt.ConnectionType.QueuedConnection
            worker.finished.connect(self._on_file_done, queued)
            worker.failed.connect(self._on_file_failed, queued)
            worker.unreachable.connect(self._on_file_unreachable, queued)
            worker.waiting.connect(self._on_file_waiting, queued)
            thread.started.connect(worker.run)
            self._thread, self._worker = thread, worker
            track_worker_thread(thread, worker)
            thread.start()
            return
        self._thread = self._worker = None
        diag("download_done", "recording downloaded", files=[Path(p).name for p in self._local])
        self.finished.emit(list(self._local))

    @Slot(str, str)
    def _on_file_done(self, name: str, path: str) -> None:
        diag("download_file", name)
        self._local.append(path)
        self._index += 1
        self._thread = self._worker = None
        self._next()

    @Slot(str, str)
    def _on_file_failed(self, name: str, text: str) -> None:
        self._thread = self._worker = None
        diag("download_failed", name, error=text)
        cancelled = "cancel" in (text or "").casefold()
        self.failed.emit("Download cancelled." if cancelled else f"Download of {name} failed. See details.")

    @Slot(str, float, str)
    def _on_file_waiting(self, name: str, delay_s: float, detail: str) -> None:
        diag("download_waiting", name, delay_s=round(float(delay_s), 1), detail=detail)
        self.progress.emit("Waiting for the sensor to accept a connection")

    @Slot(str, str)
    def _on_file_unreachable(self, name: str, detail: str) -> None:
        self._thread = self._worker = None
        diag("download_unreachable", name, detail=detail)
        self.failed.emit("Not downloaded: the sensor accepted no new connection. The files stay on the sensor.")


__all__ = ["RecordingDownload"]
