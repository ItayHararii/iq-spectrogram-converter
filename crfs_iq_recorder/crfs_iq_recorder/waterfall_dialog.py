"""Waterfall of a downloaded recording, built by the same preview code as Sensor Files."""

from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QProgressBar, QVBoxLayout

from .diagnostic_log import diag
from .spectrogram_preview import PreviewError, PreviewResult, build_preview_from_path, cache_key
from .worker_threads import track as track_worker_thread


class _PreviewJob(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, path: Path, center_hz: int | None, bandwidth_hz: int | None, requested: tuple | None = None) -> None:
        super().__init__()
        self._path = path
        self._center_hz = center_hz
        self._bandwidth_hz = bandwidth_hz
        self._requested = requested or (None, None)

    @Slot()
    def run(self) -> None:
        try:
            stat = self._path.stat()
            result = build_preview_from_path(
                self._path,
                cache_id=cache_key(str(self._path), stat.st_size, stat.st_mtime),
                center_hz=self._center_hz,
                bandwidth_hz=self._bandwidth_hz,
                requested_start_hz=self._requested[0],
                requested_stop_hz=self._requested[1],
            )
            self.finished.emit(result)
        except (OSError, PreviewError) as exc:
            self.failed.emit(str(exc))
        except Exception:  # noqa: BLE001 - shown as one line, details in the diagnostic log
            diag("waterfall_failed", str(self._path.name), traceback=traceback.format_exc(limit=6))
            self.failed.emit("The waterfall could not be built. See details.")
        finally:
            thread = self.thread()
            if thread is not None:
                thread.quit()


class WaterfallDialog(QDialog):
    def __init__(self, path: Path | str, *, center_hz=None, bandwidth_hz=None, requested_start_hz=None, requested_stop_hz=None, parent=None) -> None:
        super().__init__(parent)
        self.path = Path(path)
        self.setWindowTitle(f"Waterfall - {self.path.name}")
        self.resize(820, 460)
        layout = QVBoxLayout(self)
        self.image = QLabel("Preparing waterfall…")
        self.image.setObjectName("previewImage")
        self.image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image.setMinimumSize(320, 200)
        self.busy = QProgressBar()
        self.busy.setObjectName("thin")
        self.busy.setTextVisible(False)
        self.busy.setRange(0, 0)
        self.caption = QLabel("")
        self.caption.setObjectName("muted")
        self.caption.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(self.image, 1)
        layout.addWidget(self.busy)
        layout.addWidget(self.caption)
        layout.addWidget(buttons)
        self._pixmap: QPixmap | None = None
        self.result: PreviewResult | None = None
        self.error = ""
        self._thread = QThread(self)
        self._job = _PreviewJob(
            self.path,
            int(center_hz) if center_hz else None,
            int(bandwidth_hz) if bandwidth_hz else None,
            (requested_start_hz, requested_stop_hz),
        )
        self._job.moveToThread(self._thread)
        queued = Qt.ConnectionType.QueuedConnection
        self._job.finished.connect(self._on_ready, queued)
        self._job.failed.connect(self._on_failed, queued)
        self._thread.started.connect(self._job.run)
        track_worker_thread(self._thread, self._job)
        self._thread.start()

    def running(self) -> bool:
        return self._thread.isRunning()

    @Slot(object)
    def _on_ready(self, result: object) -> None:
        self.busy.setVisible(False)
        if not isinstance(result, PreviewResult):
            return
        self.result = result
        pixmap = QPixmap()
        pixmap.loadFromData(result.png_bytes, "PNG")
        self._pixmap = pixmap if not pixmap.isNull() else None
        self.image.setText("" if self._pixmap else "No image.")
        self._rescale()
        self.caption.setText(result.caption)

    @Slot(str)
    def _on_failed(self, text: str) -> None:
        self.busy.setVisible(False)
        self.error = text.splitlines()[-1] if text else "Preview failed."
        self.image.setText("")
        self.caption.setText(self.error)

    def _rescale(self) -> None:
        if self._pixmap is None:
            return
        self.image.setPixmap(
            self._pixmap.scaled(
                max(self.image.width(), 40),
                max(self.image.height(), 40),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._rescale()

    def done(self, code: int) -> None:
        self._thread.quit()
        self._thread.wait(5000)
        super().done(code)


__all__ = ["WaterfallDialog"]
