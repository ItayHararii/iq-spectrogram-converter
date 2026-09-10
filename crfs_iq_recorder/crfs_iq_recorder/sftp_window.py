"""Simple SFTP file browser for sensor recording folders."""

from __future__ import annotations

import traceback
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .connection_state import ConnectionState
from .constants import DEFAULT_SFTP_PORT
from .recording_history import format_columns, load_recordings, match_recordings_to_entries
from .sftp_client import DemoSftpBrowser, RemoteEntry, SftpBrowser, SftpError, connect_sftp
from .sftp_paths import is_remdata_root, parent_directory, resolve_start_directory, today_remdata_directory
from .validation import ValidationError, parse_optional_port

MAX_PARALLEL_DOWNLOADS = 4
_TABLE_HEADERS = [
    "Name",
    "Size",
    "Modified",
    "Start (MHz)",
    "End (MHz)",
    "Center (MHz)",
    "Bandwidth (MHz)",
    "Total recording duration (s)",
]


def _open_browser(state: ConnectionState) -> SftpBrowser:
    if state.demo:
        return DemoSftpBrowser()
    port = parse_optional_port(state.sftp_port)
    if port is None:
        port = DEFAULT_SFTP_PORT
    host = state.host.strip()
    if not host:
        raise SftpError("Set the sensor IP in Connection Settings.")
    return connect_sftp(host, port, state.sftp_user(), state.sftp_pass())


class SftpBackend(QObject):
    """Listing stays on this object's thread."""

    listed = Signal(str, object, object)
    failed = Signal(str)

    def __init__(self, state: ConnectionState) -> None:
        super().__init__()
        self._state = state
        self._browser: SftpBrowser | None = None
        self._closing = False

    def _list_today(self, browser: SftpBrowser) -> tuple[str, object, object]:
        today = today_remdata_directory()
        exists = browser.directory_exists(today)
        path, warning = resolve_start_directory(today, exists)
        entries = browser.listdir(path)
        return path, entries, warning

    @Slot()
    def open_today(self) -> None:
        try:
            browser = _open_browser(self._state)
            if self._closing:
                browser.close()
                return
            if self._browser:
                self._browser.close()
            self._browser = browser
            path, entries, warning = self._list_today(browser)
        except SftpError as exc:
            self.failed.emit(str(exc))
            return
        except ValidationError as exc:
            self.failed.emit(str(exc))
            return
        except Exception:
            self.failed.emit(traceback.format_exc(limit=4))
            return
        if self._closing:
            return
        self.listed.emit(path, entries, warning)

    @Slot()
    def try_today(self) -> None:
        browser = self._browser
        if browser is None:
            self.open_today()
            return
        try:
            path, entries, warning = self._list_today(browser)
        except SftpError as exc:
            self.failed.emit(str(exc))
            return
        except Exception:
            self.failed.emit(traceback.format_exc(limit=4))
            return
        if self._closing:
            return
        self.listed.emit(path, entries, warning)

    @Slot(str)
    def listdir(self, path: str) -> None:
        browser = self._browser
        if browser is None:
            self.failed.emit("Not connected.")
            return
        try:
            entries = browser.listdir(path)
        except SftpError as exc:
            self.failed.emit(str(exc))
            return
        except Exception:
            self.failed.emit(traceback.format_exc(limit=4))
            return
        if self._closing:
            return
        self.listed.emit(path, entries, None)

    @Slot()
    def shutdown(self) -> None:
        self._closing = True
        if self._browser:
            self._browser.close()
            self._browser = None


class FileDownloadWorker(QObject):
    """One SFTP connection per file so several downloads can run at once."""

    finished = Signal(str, str)
    failed = Signal(str, str)
    progress = Signal(str, int, int)

    def __init__(self, state: ConnectionState, remote_path: str, local_path: str, name: str) -> None:
        super().__init__()
        self._state = state
        self._remote = remote_path
        self._local = local_path
        self._name = name

    @Slot()
    def run(self) -> None:
        browser = None
        try:
            browser = _open_browser(self._state)

            def cb(done: int, total: int) -> None:
                self.progress.emit(self._name, int(done), int(total or 0))

            browser.download(self._remote, Path(self._local), progress=cb)
            self.finished.emit(self._name, self._local)
        except SftpError as exc:
            self.failed.emit(self._name, str(exc))
        except Exception:
            self.failed.emit(self._name, traceback.format_exc(limit=3))
        finally:
            if browser is not None:
                browser.close()
            thread = self.thread()
            if thread is not None:
                thread.quit()


class SftpWindow(QWidget):
    _cmd_open = Signal()
    _cmd_try_today = Signal()
    _cmd_list = Signal(str)
    _cmd_close = Signal()

    def __init__(self, state: ConnectionState, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sensor files (SFTP)")
        self.resize(1180, 520)
        self.setWindowFlag(Qt.WindowType.Window, True)
        self._state = state
        self._path = today_remdata_directory()
        self._entries: list[RemoteEntry] = []
        self._closing = False
        self._busy = False
        self._dl_pending: list[tuple[RemoteEntry, str]] = []
        self._dl_active: list[tuple[QThread, FileDownloadWorker]] = []
        self._dl_ok = 0
        self._dl_fail = 0
        self._dl_total = 0
        self._dl_bytes: dict[str, tuple[int, int]] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.path_label = QLabel(self._path)
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.path_label)

        self.status = QLabel("Connecting…")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        btns = QHBoxLayout()
        self.refresh_btn = QPushButton("Refresh")
        self.up_btn = QPushButton("Parent folder")
        self.download_btn = QPushButton("Download Selected Files")
        self.refresh_btn.clicked.connect(self.refresh)
        self.up_btn.clicked.connect(self._go_parent)
        self.download_btn.clicked.connect(self._download_selected)
        btns.addWidget(self.refresh_btn)
        btns.addWidget(self.up_btn)
        btns.addWidget(self.download_btn)
        btns.addStretch()
        layout.addLayout(btns)

        hint = QLabel(
            "Shift-click or Ctrl-click to select several IQ files. "
            "Split parts (_0001, _0002, …) share the original recording. "
            "Duration is the total recording time, not the length of each file."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, len(_TABLE_HEADERS))
        self.table.setHorizontalHeaderLabels(_TABLE_HEADERS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 150)
        for col in range(3, 7):
            self.table.setColumnWidth(col, 110)
        self.table.setColumnWidth(7, 170)
        duration_header = self.table.horizontalHeaderItem(7)
        if duration_header is not None:
            duration_header.setToolTip(
                "Total recording duration entered when the capture was started. "
                "Split files (_0001, _0002, …) each hold only part of that time."
            )
        self.table.cellDoubleClicked.connect(self._open_row)
        layout.addWidget(self.table, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self._thread = QThread()
        self._worker = SftpBackend(state)
        self._worker.moveToThread(self._thread)
        queued = Qt.ConnectionType.QueuedConnection
        self._cmd_open.connect(self._worker.open_today, queued)
        self._cmd_try_today.connect(self._worker.try_today, queued)
        self._cmd_list.connect(self._worker.listdir, queued)
        self._cmd_close.connect(self._worker.shutdown, queued)
        self._worker.listed.connect(self._on_listed, queued)
        self._worker.failed.connect(self._on_failed, queued)
        self._thread.start()
        self._set_busy(True)
        self.status.setText(f"Connecting to {state.host} as {state.sftp_user()}…")
        self._cmd_open.emit()

    def relabel_files(self) -> None:
        if self._entries:
            self._fill(self._entries)

    def retry_today(self) -> None:
        if self._closing or self._dl_active or self._dl_pending:
            return
        self._set_busy(True)
        self.status.setText("Looking for today's recording folder…")
        self._cmd_try_today.emit()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._closing = True
        self._dl_pending.clear()
        for signal, slot in (
            (self._worker.listed, self._on_listed),
            (self._worker.failed, self._on_failed),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._cmd_close.emit()
        self._thread.quit()
        finished = self._thread.wait(15000)
        for thread, _worker in list(self._dl_active):
            thread.quit()
            thread.wait(5000)
        QCoreApplication.removePostedEvents(self)
        if finished:
            self._thread.deleteLater()
        super().closeEvent(event)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        downloading = bool(self._dl_pending or self._dl_active)
        locked = busy or downloading
        for btn in (self.refresh_btn, self.up_btn, self.download_btn):
            btn.setEnabled(not locked)

    @Slot(str, object, object)
    def _on_listed(self, path: str, entries: object, warning: object) -> None:
        if self._closing:
            return
        self._path = path
        self.path_label.setText(path)
        self._fill(list(entries or []))
        if warning:
            self.status.setText(str(warning))
        elif self._state.demo:
            self.status.setText("Demo listing — no sensor was contacted.")
        else:
            self.status.setText(f"{len(self._entries)} item(s).")
        self.progress.setVisible(False)
        self._set_busy(False)

    @Slot(str)
    def _on_failed(self, text: str) -> None:
        if self._closing:
            return
        self.progress.setVisible(False)
        self._set_busy(False)
        self.status.setText("SFTP error.")
        QMessageBox.warning(self, "SFTP", (text or "SFTP failed.").splitlines()[-1])

    def refresh(self) -> None:
        if self._busy or self._closing or self._dl_active or self._dl_pending:
            return
        self._set_busy(True)
        self.status.setText("Refreshing…")
        if is_remdata_root(self._path):
            self._cmd_try_today.emit()
        else:
            self._cmd_list.emit(self._path)

    def _go_parent(self) -> None:
        if self._busy or self._path in ("/",) or self._dl_active:
            return
        self._path = parent_directory(self._path)
        self.path_label.setText(self._path)
        self.refresh()

    def _open_row(self, row: int, _col: int) -> None:
        entry = self._entry_at(row)
        if entry is None:
            return
        if entry.is_dir:
            if self._busy or self._dl_active:
                return
            self._path = entry.path if entry.path.endswith("/") else entry.path + "/"
            self.path_label.setText(self._path)
            self.refresh()
            return
        self._begin_downloads([entry])

    def _selected_files(self) -> list[RemoteEntry]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        files: list[RemoteEntry] = []
        for row in rows:
            entry = self._entry_at(row)
            if entry is not None and not entry.is_dir:
                files.append(entry)
        return files

    def _download_selected(self) -> None:
        files = self._selected_files()
        if not files:
            QMessageBox.information(self, "Download", "Select one or more files first.")
            return
        self._begin_downloads(files)

    def _begin_downloads(self, files: list[RemoteEntry]) -> None:
        if self._busy or self._dl_active or self._closing:
            return
        jobs: list[tuple[RemoteEntry, str]] = []
        if len(files) == 1:
            dest, _ = QFileDialog.getSaveFileName(self, "Save file", files[0].name)
            if not dest:
                return
            jobs.append((files[0], dest))
        else:
            folder = QFileDialog.getExistingDirectory(self, "Save files in folder")
            if not folder:
                return
            root = Path(folder)
            jobs = [(entry, str(root / entry.name)) for entry in files]
        self._queue_downloads(jobs)

    def _queue_downloads(self, jobs: list[tuple[RemoteEntry, str]]) -> None:
        if not jobs or self._closing:
            return
        self._dl_pending = list(jobs)
        self._dl_ok = 0
        self._dl_fail = 0
        self._dl_total = len(jobs)
        self._dl_bytes = {}
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(f"Downloading {self._dl_total} file(s)…")
        self._set_busy(True)
        self._pump_downloads()

    def _pump_downloads(self) -> None:
        if self._closing:
            return
        while self._dl_pending and len(self._dl_active) < MAX_PARALLEL_DOWNLOADS:
            entry, dest = self._dl_pending.pop(0)
            thread = QThread()
            worker = FileDownloadWorker(self._state, entry.path, dest, entry.name)
            worker.moveToThread(thread)
            queued = Qt.ConnectionType.QueuedConnection
            worker.finished.connect(self._on_file_downloaded, queued)
            worker.failed.connect(self._on_file_download_failed, queued)
            worker.progress.connect(self._on_file_progress, queued)
            thread.finished.connect(self._on_download_thread_finished, queued)
            thread.started.connect(worker.run)
            self._dl_active.append((thread, worker))
            thread.start()

    @Slot(str, str)
    def _on_file_downloaded(self, name: str, path: str) -> None:
        if self._closing:
            return
        self._dl_ok += 1
        self.status.setText(f"Saved {name} ({self._dl_ok + self._dl_fail}/{self._dl_total})")

    @Slot(str, str)
    def _on_file_download_failed(self, name: str, text: str) -> None:
        if self._closing:
            return
        self._dl_fail += 1
        self.status.setText(f"Failed {name} ({self._dl_ok + self._dl_fail}/{self._dl_total})")
        QMessageBox.warning(self, "Download", f"{name}: {(text or 'Download failed.').splitlines()[-1]}")

    @Slot(str, int, int)
    def _on_file_progress(self, name: str, done: int, total: int) -> None:
        if self._closing:
            return
        self._dl_bytes[name] = (done, total)
        sum_done = sum(item[0] for item in self._dl_bytes.values())
        sum_total = sum(item[1] for item in self._dl_bytes.values())
        self.progress.setVisible(True)
        if sum_total > 0:
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, int(sum_done * 100 / sum_total)))
        else:
            self.progress.setRange(0, 0)

    @Slot()
    def _on_download_thread_finished(self) -> None:
        thread = self.sender()
        remaining: list[tuple[QThread, FileDownloadWorker]] = []
        for item in self._dl_active:
            if item[0] is thread:
                item[1].deleteLater()
                item[0].deleteLater()
            else:
                remaining.append(item)
        self._dl_active = remaining
        if self._closing:
            return
        self._pump_downloads()
        if self._dl_pending or self._dl_active:
            return
        self.progress.setVisible(False)
        if self._dl_fail:
            self.status.setText(f"Downloaded {self._dl_ok} file(s), {self._dl_fail} failed.")
        elif self._dl_ok:
            self.status.setText(f"Downloaded {self._dl_ok} file(s).")
        self._set_busy(False)

    def _entry_at(self, row: int) -> RemoteEntry | None:
        if row < 0 or row >= len(self._entries):
            return None
        return self._entries[row]

    def _fill(self, entries: list[RemoteEntry]) -> None:
        self._entries = entries
        matched = match_recordings_to_entries(
            entries,
            load_recordings(),
            host=self._state.host,
        )
        self.table.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            name = entry.name + ("/" if entry.is_dir else "")
            size_text = "" if entry.is_dir else f"{entry.size:,}"
            mtime = entry.modified.strftime("%Y-%m-%d %H:%M:%S") if entry.modified else ""
            values = [name, size_text, mtime, "", "", "", "", ""]
            rec = matched.get(entry.path)
            if rec is not None:
                start, end, center, bw, duration = format_columns(rec)
                values[3:] = [start, end, center, bw, duration]
            for col, text in enumerate(values):
                item = QTableWidgetItem(text)
                if col >= 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(row, col, item)
