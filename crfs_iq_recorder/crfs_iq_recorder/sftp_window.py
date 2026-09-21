"""Simple SFTP file browser for sensor recording folders."""

from __future__ import annotations

import tempfile
import threading
import traceback
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QRect,
    QThread,
    QTimer,
    QUrl,
    Qt,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .connection_state import ConnectionState
from .constants import DEFAULT_SFTP_PORT
from .download_util import DownloadError, unique_destination
from .listing_sort import (
    COL_MODIFIED,
    COL_NAME,
    SORTABLE_COLUMNS,
    build_listing_nodes,
    header_label,
    sort_listing_nodes,
)
from .paths import brand_icon_path, ensure_download_dir, resolved_download_dir
from .recording_history import (
    format_columns,
    group_part_counts,
    group_total_bytes,
    iq_group_key,
    iq_part_label,
    load_recordings,
    match_recordings_to_entries,
    persist_matched_stems,
)
from .sftp_client import RemoteEntry, SftpBrowser, SftpError, connect_sftp
from .sftp_paths import parent_directory, resolve_today_directory, today_remdata_directory
from .spectrogram_preview import (
    PREVIEW_MAX_BYTES,
    PreviewError,
    PreviewResult,
    build_preview_from_path,
    cache_key,
    preview_dependencies,
)
from .theme import current_palette, fit_window_to_screen, format_bytes, format_display_datetime
from .validation import ValidationError, parse_optional_port

MAX_PARALLEL_DOWNLOADS = 4
_PREVIEW_CACHE = 8
_TABLE_HEADERS = [
    "New",
    "Name",
    "Size",
    "Modified",
    "Start (MHz)",
    "End (MHz)",
    "Center (MHz)",
    "Bandwidth (MHz)",
    "Duration (s)",
]
ROLE_PATH = int(Qt.ItemDataRole.UserRole)
ROLE_KIND = ROLE_PATH + 1
ROLE_GROUP = ROLE_PATH + 2
ROLE_CHILD_PATHS = ROLE_PATH + 3


def discovered_new_paths(previous: set[str] | None, current: set[str]) -> tuple[set[str], set[str]]:
    """First listing of a folder is the baseline; later additions are NEW."""
    if previous is None:
        return set(), set(current)
    return set(current) - previous, previous | current


def _same_remote_path(left: str, right: str) -> bool:
    def norm(path: str) -> str:
        text = (path or "/").replace("\\", "/")
        if text != "/" and text.endswith("/"):
            text = text.rstrip("/")
        return text or "/"

    return norm(left) == norm(right)


class NewBadgeDelegate(QStyledItemDelegate):
    """Solid green NEW pill that stays readable on selected rows."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = str(index.data() or "")
        opt.text = ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        if text != "NEW":
            return
        palette = current_palette()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        font = QFont(opt.font)
        font.setBold(True)
        font.setPointSize(max(8, font.pointSize() - 1))
        painter.setFont(font)
        metrics = painter.fontMetrics()
        pad_x = 8
        pad_y = 3
        width = metrics.horizontalAdvance("NEW") + pad_x * 2
        height = metrics.height() + pad_y * 2
        badge = QRect(0, 0, width, height)
        badge.moveCenter(opt.rect.center())
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(palette.new_bg))
        painter.drawRoundedRect(badge, 6, 6)
        painter.setPen(QColor(palette.new_fg))
        painter.drawText(badge, int(Qt.AlignmentFlag.AlignCenter), "NEW")
        painter.restore()


def _open_browser(
    state: ConnectionState,
    *,
    cancel: threading.Event | None = None,
    on_client=None,
) -> SftpBrowser:
    if cancel is not None and cancel.is_set():
        raise SftpError("SFTP cancelled.")
    if state.demo:
        from .demo_sftp import shared_demo_browser

        return shared_demo_browser()
    port = parse_optional_port(state.sftp_port)
    if port is None:
        port = DEFAULT_SFTP_PORT
    host = state.host.strip()
    if not host:
        raise SftpError("Set the sensor IP in Settings.")
    return connect_sftp(
        host,
        port,
        state.sftp_user(),
        state.sftp_pass(),
        cancel=cancel,
        on_client=on_client,
    )


_RETAINED_THREADS: list[object] = []
_RETAINED_WINDOWS: list[object] = []
_PREVIEW_COMPUTE = threading.Semaphore(1)


def _retain_until_finished(thread: QThread, *objects: QObject) -> None:
    """Keep worker objects alive after the window closes, without blocking the UI."""
    bag: list[object] = [thread, *objects]
    _RETAINED_THREADS.append(bag)

    def _drop() -> None:
        try:
            _RETAINED_THREADS.remove(bag)
        except ValueError:
            pass
        for obj in objects:
            try:
                obj.deleteLater()
            except RuntimeError:
                pass
        try:
            thread.deleteLater()
        except RuntimeError:
            pass

    if thread.isFinished():
        QTimer.singleShot(0, _drop)
    else:
        thread.finished.connect(_drop)


def _hold_closed_window(window: QWidget, threads: list[QThread]) -> None:
    """Keep a closed Sensor Files window alive until its workers finish."""
    bag: dict[str, object] = {"window": window, "left": 0}
    _RETAINED_WINDOWS.append(bag)

    def _one_done() -> None:
        bag["left"] = int(bag["left"]) - 1
        if int(bag["left"]) <= 0:
            try:
                _RETAINED_WINDOWS.remove(bag)
            except ValueError:
                pass

    pending = False
    for thread in threads:
        if thread is None:
            continue
        bag["left"] = int(bag["left"]) + 1
        pending = True
        if thread.isFinished():
            QTimer.singleShot(0, _one_done)
        else:
            thread.finished.connect(_one_done)
    if not pending:
        QTimer.singleShot(0, _one_done)


def _disconnect_signal(signal, slot) -> None:
    try:
        signal.disconnect(slot)
    except (RuntimeError, TypeError):
        pass


class SftpBackend(QObject):
    """Listing stays on this object's thread."""

    listed = Signal(str, object, object)
    failed = Signal(str)
    removed = Signal(object, object)

    def __init__(self, state: ConnectionState) -> None:
        super().__init__()
        self._state = state
        self._browser: SftpBrowser | None = None
        self._closing = False
        self._cancel = threading.Event()
        self._connecting = None
        self._lock = threading.Lock()

    def abort(self) -> None:
        self._closing = True
        self._cancel.set()
        from .sftp_client import _close_ssh_client

        with self._lock:
            client = self._connecting
            browser = self._browser
        if client is not None:
            _close_ssh_client(client)
        if browser is not None:
            try:
                browser.abort()
            except Exception:
                pass

    def _list_today(self, browser: SftpBrowser) -> tuple[str, object, object]:
        path, warning = resolve_today_directory(browser.directory_exists)
        entries = browser.listdir(path)
        return path, entries, warning

    @Slot()
    def open_today(self) -> None:
        try:
            def on_client(client) -> None:
                with self._lock:
                    self._connecting = client

            browser = _open_browser(self._state, cancel=self._cancel, on_client=on_client)
            with self._lock:
                self._connecting = None
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

    @Slot(object)
    def remove_files(self, paths: object) -> None:
        browser = self._browser
        wanted = [str(item) for item in (paths or []) if str(item).strip()]
        if browser is None:
            self.removed.emit([], [(path, "Not connected.") for path in wanted])
            return
        ok: list[str] = []
        errors: list[tuple[str, str]] = []
        for path in wanted:
            try:
                browser.remove_file(path)
                ok.append(path)
            except SftpError as exc:
                errors.append((path, str(exc)))
            except Exception as exc:
                errors.append((path, str(exc)))
        if self._closing:
            return
        self.removed.emit(ok, errors)

    @Slot()
    def shutdown(self) -> None:
        self.abort()
        with self._lock:
            self._browser = None
            self._connecting = None


class FileDownloadWorker(QObject):
    """One SFTP connection per file so several downloads can run at once."""

    finished = Signal(str, str)
    failed = Signal(str, str)
    progress = Signal(str, int, int)

    def __init__(
        self,
        state: ConnectionState,
        remote_path: str,
        local_path: str,
        name: str,
        *,
        expected_size: int = 0,
        cancel: threading.Event | None = None,
    ) -> None:
        super().__init__()
        self._state = state
        self._remote = remote_path
        self._local = local_path
        self._name = name
        self._expected_size = int(expected_size or 0)
        self._cancel = cancel
        self._browser: SftpBrowser | None = None

    def abort(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
        browser = self._browser
        if browser is not None:
            try:
                browser.abort()
            except Exception:
                pass

    @Slot()
    def run(self) -> None:
        try:
            self._browser = _open_browser(self._state, cancel=self._cancel)

            def cb(done: int, total: int) -> None:
                self.progress.emit(self._name, int(done), int(total or 0))

            self._browser.download(
                self._remote,
                Path(self._local),
                progress=cb,
                cancel=self._cancel,
                expected_size=self._expected_size,
            )
            self.finished.emit(self._name, self._local)
        except SftpError as exc:
            self.failed.emit(self._name, str(exc))
        except Exception:
            self.failed.emit(self._name, traceback.format_exc(limit=3))
        finally:
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            thread = self.thread()
            if thread is not None:
                thread.quit()


class PreviewWorker(QObject):
    finished = Signal(int, object)
    failed = Signal(int, str)

    def __init__(
        self,
        state: ConnectionState,
        token: int,
        entry: RemoteEntry,
        *,
        center_hz: int | None = None,
        bandwidth_hz: int | None = None,
    ) -> None:
        super().__init__()
        self._state = state
        self._token = token
        self._entry = entry
        self._center_hz = center_hz
        self._bandwidth_hz = bandwidth_hz
        self._cancel = threading.Event()
        self._browser: SftpBrowser | None = None

    def abort(self) -> None:
        self._cancel.set()
        browser = self._browser
        if browser is not None:
            try:
                browser.abort()
            except Exception:
                pass

    @Slot()
    def run(self) -> None:
        tmp = None
        try:
            if self._cancel.is_set():
                raise PreviewError("Preview cancelled.")
            ok, reason = preview_dependencies()
            if not ok:
                raise PreviewError(reason)
            fd, name = tempfile.mkstemp(prefix="crfs-iq-preview-", suffix=".wav")
            import os

            os.close(fd)
            tmp = Path(name)
            self._browser = _open_browser(self._state, cancel=self._cancel)
            self._browser.read_prefix(self._entry.path, tmp, PREVIEW_MAX_BYTES)
            if self._cancel.is_set():
                raise PreviewError("Preview cancelled.")
            mtime_ts = self._entry.modified.timestamp() if self._entry.modified else 0
            with _PREVIEW_COMPUTE:
                if self._cancel.is_set():
                    raise PreviewError("Preview cancelled.")
                result = build_preview_from_path(
                    tmp,
                    cache_id=cache_key(self._entry.path, self._entry.size, mtime_ts),
                    center_hz=self._center_hz,
                    bandwidth_hz=self._bandwidth_hz,
                )
            if self._cancel.is_set():
                raise PreviewError("Preview cancelled.")
            self.finished.emit(self._token, result)
        except (SftpError, PreviewError) as exc:
            self.failed.emit(self._token, str(exc))
        except Exception:
            self.failed.emit(self._token, traceback.format_exc(limit=3))
        finally:
            if self._browser is not None:
                self._browser.close()
                self._browser = None
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            thread = self.thread()
            if thread is not None:
                thread.quit()


class SftpWindow(QWidget):
    activity = Signal(str)
    remote_changed = Signal()
    _cmd_open = Signal()
    _cmd_try_today = Signal()
    _cmd_list = Signal(str)
    _cmd_remove = Signal(object)
    _cmd_close = Signal()

    def __init__(self, state: ConnectionState, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("sftpRoot")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle("Sensor files (SFTP)")
        icon = brand_icon_path()
        if icon is not None:
            self.setWindowIcon(QIcon(str(icon)))
        self.setWindowFlag(Qt.WindowType.Window, True)
        flags = self.windowFlags()
        self.setWindowFlags(
            flags
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowCloseButtonHint
        )
        self._state = state
        self._path = today_remdata_directory()
        self._entries: list[RemoteEntry] = []
        self._closing = False
        self._busy = False
        self._dl_pending: list[tuple[RemoteEntry, str]] = []
        self._dl_active: list[tuple[QThread, FileDownloadWorker]] = []
        self._dl_ok = 0
        self._dl_fail = 0
        self._dl_cancel_count = 0
        self._dl_total = 0
        self._dl_bytes: dict[str, tuple[int, int]] = {}
        self._dl_folder: Path | None = None
        self._dl_cancel = threading.Event()
        self._seen_by_dir: dict[str, set[str]] = {}
        self._new_paths: set[str] = set()
        self._recent_record_id = ""
        self._matched: dict[str, object] = {}
        self._preview_token = 0
        self._preview_active = False
        self._preview_pending: tuple[int, RemoteEntry] | None = None
        self._preview_jobs: list[tuple[QThread, PreviewWorker]] = []
        self._preview_cache: OrderedDict[tuple, PreviewResult] = OrderedDict()
        self._preview_source: QPixmap | None = None
        self._preview_scaled_key: tuple[int, int, int] | None = None
        self._sort_column: int | None = None
        self._sort_descending = True
        self._user_sort = False
        self._expanded_groups: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        toolbar = QFrame()
        toolbar.setObjectName("toolbar")
        tools = QHBoxLayout(toolbar)
        tools.setContentsMargins(12, 10, 12, 10)
        tools.setSpacing(8)
        path_caption = QLabel("Path")
        path_caption.setObjectName("sensorFactLabel")
        self.path_label = QLabel(self._path)
        self.path_label.setObjectName("pathLabel")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.path_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.path_label.setWordWrap(True)
        tools.addWidget(path_caption)
        tools.addWidget(self.path_label, 1)
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("ghost")
        self.up_btn = QPushButton("Up")
        self.up_btn.setObjectName("ghost")
        self.download_btn = QPushButton("Download")
        self.download_btn.setObjectName("accent")
        self.open_folder_btn = QPushButton("Open recordings folder")
        self.open_folder_btn.setObjectName("ghost")
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("ghost")
        self.cancel_btn.setEnabled(False)
        self.refresh_btn.clicked.connect(self.refresh)
        self.up_btn.clicked.connect(self._go_parent)
        self.download_btn.clicked.connect(self._download_selected)
        self.open_folder_btn.clicked.connect(self._open_recordings_folder)
        self.cancel_btn.clicked.connect(self._cancel_downloads)
        for btn in (
            self.refresh_btn,
            self.up_btn,
            self.download_btn,
            self.open_folder_btn,
            self.cancel_btn,
        ):
            btn.setAutoDefault(False)
            btn.setDefault(False)
        tools.addWidget(self.refresh_btn)
        tools.addWidget(self.up_btn)
        tools.addWidget(self.download_btn)
        tools.addWidget(self.open_folder_btn)
        tools.addWidget(self.cancel_btn)
        layout.addWidget(toolbar)

        self.save_label = QLabel("")
        self.save_label.setObjectName("muted")
        self.save_label.setWordWrap(True)
        self.save_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.save_label)
        self._sync_download_label()

        self.status = QLabel("Connecting…")
        self.status.setObjectName("muted")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.table = QTreeWidget()
        self.table.setColumnCount(len(_TABLE_HEADERS))
        self.table.setHeaderLabels(_TABLE_HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSortingEnabled(False)
        self.table.setAlternatingRowColors(True)
        self.table.setRootIsDecorated(True)
        self.table.setItemsExpandable(True)
        self.table.setExpandsOnDoubleClick(True)
        self.table.setUniformRowHeights(True)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setIndentation(18)
        header = self.table.header()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignCenter)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(72)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        header.setSectionsClickable(True)
        header.setHighlightSections(False)
        header.setSortIndicatorShown(False)
        header.sectionClicked.connect(self._on_header_clicked)
        self.table.setColumnWidth(0, 72)
        self.table.setItemDelegateForColumn(0, NewBadgeDelegate(self.table))
        duration_header = self.table.headerItem()
        if duration_header is not None:
            duration_header.setToolTip(8, "Total recording time, not this file.")
            duration_header.setToolTip(2, "This file, or combined size for a split recording.")
            for col in range(len(_TABLE_HEADERS)):
                duration_header.setTextAlignment(col, Qt.AlignmentFlag.AlignCenter)
        self._sync_sort_headers()
        self.table.itemDoubleClicked.connect(self._open_item)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemExpanded.connect(self._on_item_expanded)
        self.table.itemCollapsed.connect(self._on_item_collapsed)
        delete_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Delete), self.table)
        delete_shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        delete_shortcut.activated.connect(self._delete_selected_remote)

        self.empty_page = QLabel("Connecting…")
        self.empty_page.setObjectName("emptyState")
        self.empty_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_page.setWordWrap(True)
        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(self.table)
        self.table_stack.addWidget(self.empty_page)
        self.table_stack.setCurrentIndex(1)

        preview = QFrame()
        preview.setObjectName("previewPane")
        preview_l = QVBoxLayout(preview)
        preview_l.setContentsMargins(12, 10, 12, 12)
        preview_l.setSpacing(8)
        preview_head = QHBoxLayout()
        preview_title = QLabel("Waterfall")
        preview_title.setObjectName("sectionTitle")
        self.preview_file = QLabel("Select a WAVE file")
        self.preview_file.setObjectName("previewFile")
        self.preview_file.setWordWrap(True)
        preview_head.addWidget(preview_title)
        preview_head.addStretch(1)
        preview_head.addWidget(self.preview_file, 2)
        preview_l.addLayout(preview_head)
        self.preview_busy = QProgressBar()
        self.preview_busy.setObjectName("thin")
        self.preview_busy.setTextVisible(False)
        self.preview_busy.setRange(0, 0)
        self.preview_busy.setVisible(False)
        preview_l.addWidget(self.preview_busy)
        self.preview_caption = QLabel("Select a WAVE file.")
        self.preview_caption.setObjectName("muted")
        self.preview_caption.setWordWrap(True)
        preview_l.addWidget(self.preview_caption)
        self.preview_image = QLabel("The waterfall appears here.")
        self.preview_image.setObjectName("previewImage")
        self.preview_image.setMinimumHeight(160)
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        preview_l.addWidget(self.preview_image, 1)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.table_stack)
        self.splitter.addWidget(preview)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, True)
        self.splitter.setSizes([360, 320])
        layout.addWidget(self.splitter, 1)

        self.progress = QProgressBar()
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self._preview_resize_timer = QTimer(self)
        self._preview_resize_timer.setSingleShot(True)
        self._preview_resize_timer.setInterval(80)
        self._preview_resize_timer.timeout.connect(self._rescale_preview)

        self._thread = QThread()
        self._worker = SftpBackend(state)
        self._worker.moveToThread(self._thread)
        queued = Qt.ConnectionType.QueuedConnection
        self._cmd_open.connect(self._worker.open_today, queued)
        self._cmd_try_today.connect(self._worker.try_today, queued)
        self._cmd_list.connect(self._worker.listdir, queued)
        self._cmd_remove.connect(self._worker.remove_files, queued)
        self._cmd_close.connect(self._worker.shutdown, queued)
        self._worker.listed.connect(self._on_listed, queued)
        self._worker.failed.connect(self._on_failed, queued)
        self._worker.removed.connect(self._on_removed, queued)
        self._thread.start()
        self._set_busy(True)
        self._show_list_state("loading", f"Connecting to {state.host or 'the sensor'}…")
        self.status.setText(f"Connecting to {state.host}…")
        fit_window_to_screen(self, 1240, 760, min_width=720, min_height=480)
        self._cmd_open.emit()

    def set_recent_record_id(self, record_id: str | None) -> None:
        self._recent_record_id = str(record_id or "")
        if self._entries:
            self._fill(self._entries)

    def matching_entries(self, record_id: str) -> list[RemoteEntry]:
        if not record_id:
            return []
        out: list[RemoteEntry] = []
        for entry in self._entries:
            rec = self._matched.get(entry.path)
            if rec is not None and getattr(rec, "id", "") == record_id:
                out.append(entry)
        return out

    def relabel_files(self) -> None:
        if self._entries:
            self._fill(self._entries)

    def retry_today(self) -> None:
        if self._closing or self._dl_active or self._dl_pending:
            return
        self._set_busy(True)
        self._show_list_state("loading", "Looking for today's folder…")
        self.status.setText("Looking for today's folder…")
        self._cmd_try_today.emit()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._dl_pending.clear()
        self._preview_token += 1
        self._preview_pending = None
        _disconnect_signal(self._worker.listed, self._on_listed)
        _disconnect_signal(self._worker.failed, self._on_failed)
        _disconnect_signal(self._worker.removed, self._on_removed)
        try:
            self._worker.abort()
        except Exception:
            pass
        self._cmd_close.emit()
        self._thread.quit()
        keep = [self._thread]
        _retain_until_finished(self._thread, self._worker)
        for thread, worker in list(self._dl_active):
            _disconnect_signal(worker.finished, self._on_file_downloaded)
            _disconnect_signal(worker.failed, self._on_file_download_failed)
            _disconnect_signal(worker.progress, self._on_file_progress)
            _disconnect_signal(thread.finished, self._on_download_thread_finished)
            _retain_until_finished(thread, worker)
            keep.append(thread)
        self._dl_active.clear()
        for thread, worker in list(self._preview_jobs):
            try:
                worker.abort()
            except Exception:
                pass
            _disconnect_signal(worker.finished, self._on_preview_ready)
            _disconnect_signal(worker.failed, self._on_preview_failed)
            _disconnect_signal(thread.finished, self._on_preview_thread_finished)
            thread.quit()
            _retain_until_finished(thread, worker)
            keep.append(thread)
        self._preview_jobs.clear()
        _hold_closed_window(self, keep)
        QCoreApplication.removePostedEvents(self)
        super().closeEvent(event)

    def _show_list_state(self, kind: str, message: str = "") -> None:
        if kind == "ready":
            self.table_stack.setCurrentIndex(0)
            return
        text = message
        if kind == "loading":
            text = message or "Loading files…"
        elif kind == "empty":
            text = message or "This folder is empty."
        elif kind == "error":
            text = message or "Could not list files. Check the sensor IP and SFTP password in Settings."
        elif kind == "disconnected":
            text = message or "Not connected. Set the sensor IP in Settings."
        self.empty_page.setText(text)
        self.table_stack.setCurrentIndex(1)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        downloading = bool(self._dl_pending or self._dl_active)
        locked = busy or downloading
        self.refresh_btn.setEnabled(not locked)
        self.download_btn.setEnabled(not locked)
        self.cancel_btn.setEnabled(downloading)
        self.open_folder_btn.setEnabled(True)
        self._sync_up_button()
        self._update_download_button()

    def _sync_up_button(self) -> None:
        locked = self._busy or bool(self._dl_pending or self._dl_active)
        at_root = (self._path or "/").replace("\\", "/").rstrip("/") in ("",)
        self.up_btn.setEnabled(not locked and not at_root)

    @Slot(str, object, object)
    def _on_listed(self, path: str, entries: object, warning: object) -> None:
        if self._closing:
            return
        if not _same_remote_path(path, self._path):
            self._reset_default_sort()
        self._path = path
        self.path_label.setText(path)
        self._fill(list(entries or []))
        if not self._entries:
            self._show_list_state("empty", "This folder is empty.")
        else:
            self._show_list_state("ready")
        if warning:
            self.status.setText(str(warning))
        elif self._state.demo:
            self.status.setText("Demo listing.")
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
        detail = (text or "SFTP failed.").splitlines()[-1]
        if "cancel" in detail.casefold():
            self._show_list_state("error", "Connection cancelled.")
            self.status.setText("SFTP cancelled.")
            return
        if not self._state.host.strip():
            self._show_list_state("disconnected")
        else:
            self._show_list_state("error", f"Could not list files.\n{detail}")
        self.status.setText("SFTP error.")
        self.activity.emit(f"SFTP: {detail}")
        QMessageBox.warning(self, "SFTP", detail)

    def refresh(self) -> None:
        self._request_list(self._path)

    def _request_list(self, path: str) -> None:
        if self._busy or self._closing or self._dl_active or self._dl_pending:
            return
        if not _same_remote_path(path, self._path):
            self._reset_default_sort()
        self._path = path
        self.path_label.setText(path)
        self._sync_up_button()
        self._set_busy(True)
        if not self._entries:
            self._show_list_state("loading", "Loading files…")
        self.status.setText("Loading…")
        self._cmd_list.emit(path)

    def _go_parent(self) -> None:
        if self._busy or self._closing or self._dl_active:
            return
        current = (self._path or "/").replace("\\", "/")
        if current.rstrip("/") in ("",):
            self.status.setText("Already at the filesystem root.")
            self._sync_up_button()
            return
        self._request_list(parent_directory(current))

    def _iter_tree_items(self):
        root = self.table.invisibleRootItem()
        stack = [root.child(i) for i in range(root.childCount())]
        while stack:
            item = stack.pop(0)
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _item_identity(self, item: QTreeWidgetItem) -> str:
        kind = str(item.data(0, ROLE_KIND) or "")
        if kind == "group":
            return f"g:{item.data(0, ROLE_GROUP) or ''}"
        path = str(item.data(0, ROLE_PATH) or "")
        if kind == "dir":
            return f"d:{path}"
        return f"f:{path}"

    def _paths_for_item(self, item: QTreeWidgetItem) -> list[str]:
        kind = str(item.data(0, ROLE_KIND) or "")
        if kind == "group":
            return [str(path) for path in (item.data(0, ROLE_CHILD_PATHS) or ()) if path]
        if kind == "dir":
            return []
        path = str(item.data(0, ROLE_PATH) or "").strip()
        return [path] if path else []

    def _open_item(self, item: QTreeWidgetItem, _col: int) -> None:
        kind = str(item.data(0, ROLE_KIND) or "")
        if kind == "dir":
            if self._busy or self._dl_active:
                return
            path = str(item.data(0, ROLE_PATH) or "")
            if not path:
                return
            folder = path if path.endswith("/") else path + "/"
            self._request_list(folder)
            return
        files = []
        by_path = {entry.path: entry for entry in self._entries if not entry.is_dir}
        for path in self._paths_for_item(item):
            entry = by_path.get(path)
            if entry is not None:
                files.append(entry)
        if files:
            self._request_preview(files[0])

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        key = str(item.data(0, ROLE_GROUP) or "")
        if key:
            self._expanded_groups.add(key)

    def _on_item_collapsed(self, item: QTreeWidgetItem) -> None:
        key = str(item.data(0, ROLE_GROUP) or "")
        if key:
            self._expanded_groups.discard(key)

    def _selected_items(self) -> list[QTreeWidgetItem]:
        return [item for item in self._iter_tree_items() if item.isSelected()]

    def select_paths(self, paths: list[str]) -> None:
        wanted = {str(path) for path in paths if path}
        self.table.clearSelection()
        items: list[QTreeWidgetItem] = []
        for item in self._iter_tree_items():
            if str(item.data(0, ROLE_KIND) or "") != "file":
                continue
            if str(item.data(0, ROLE_PATH) or "") not in wanted:
                continue
            items.append(item)
        for item in items:
            item.setSelected(True)
        if items:
            self.table.scrollToItem(items[0])

    def _selected_files(self) -> list[RemoteEntry]:
        paths = self._selected_file_paths()
        by_path = {entry.path: entry for entry in self._entries if not entry.is_dir}
        return [by_path[path] for path in paths if path in by_path]

    def _selected_file_paths(self) -> list[str]:
        """Resolve selected parents to listed parts and selected children to those files only."""
        seen: list[str] = []
        found: set[str] = set()
        by_path = {entry.path: entry for entry in self._entries}
        for item in self._selected_items():
            for path in self._paths_for_item(item):
                if not path or path in found:
                    continue
                entry = by_path.get(path)
                if entry is None or entry.is_dir:
                    continue
                found.add(path)
                seen.append(path)
        return seen

    def _delete_selected_remote(self) -> None:
        if self._busy or self._closing or self._dl_active or self._dl_pending:
            return
        if not self.table.hasFocus():
            return
        paths = self._selected_file_paths()
        if not paths:
            return
        self._deleting_paths = list(paths)
        self._set_busy(True)
        self.status.setText(f"Deleting {len(paths)} file(s)…")
        self._cmd_remove.emit(list(paths))

    @Slot(object, object)
    def _on_removed(self, ok: object, errors: object) -> None:
        deleted = [str(item) for item in (ok or [])]
        failed = [(str(path), str(message)) for path, message in (errors or [])]
        preview_path = str(self.preview_file.property("remote_path") or "")
        for path in deleted:
            name = path.replace("\\", "/").rsplit("/", 1)[-1]
            self.activity.emit(f"Deleted {name} from the sensor.")
            self._new_paths.discard(path)
        for path, message in failed:
            name = path.replace("\\", "/").rsplit("/", 1)[-1]
            self.activity.emit(f"Could not delete {name}: {message}")
        if preview_path in deleted or (self.preview_file.text() and any(
            path.endswith("/" + self.preview_file.text()) or path.endswith(self.preview_file.text())
            for path in deleted
        )):
            self._clear_preview()
        if deleted and not failed:
            self.status.setText(f"Deleted {len(deleted)} file(s).")
        elif deleted and failed:
            self.status.setText(f"Deleted {len(deleted)}; {len(failed)} failed.")
        elif failed:
            self.status.setText(f"Could not delete {len(failed)} file(s).")
        if deleted:
            self.remote_changed.emit()
        self._set_busy(False)
        self.refresh()

    def _clear_preview(self) -> None:
        self._preview_token += 1
        self._preview_pending = None
        self._preview_source = None
        self._preview_scaled_key = None
        self.preview_file.setText("Select a WAVE file")
        self.preview_file.setProperty("remote_path", "")
        self.preview_caption.setText("Select a WAVE file.")
        self.preview_image.setPixmap(QPixmap())
        self.preview_image.setText("The waterfall appears here.")
        self._set_preview_busy(False)

    def _on_selection_changed(self) -> None:
        files = self._selected_files()
        selected = self._selected_items()
        one_group = len(selected) == 1 and str(selected[0].data(0, ROLE_KIND) or "") == "group"
        if one_group and files:
            self._request_preview(files[0])
            rec = self._matched.get(files[0].path)
            total = sum(item.size for item in files)
            extra = f"{len(files)} parts, {total:,} B"
            name = selected[0].text(1)
            if rec is not None:
                self.status.setText(f"{name}  ·  {rec.duration_s:g} s  ·  {extra}")
            else:
                self.status.setText(f"{name}  ·  {extra}")
        elif len(files) == 1:
            self._request_preview(files[0])
            rec = self._matched.get(files[0].path)
            stem = iq_group_key(files[0].name)
            parts = group_part_counts(self._entries).get(stem, 1)
            combined = group_total_bytes(self._entries).get(stem, files[0].size)
            extra = ""
            if parts > 1:
                extra = f"  {files[0].size:,} B this file, {combined:,} B all parts."
            if rec is not None:
                self.status.setText(f"{files[0].name}  ·  {rec.duration_s:g} s{extra}")
            elif extra:
                self.status.setText(f"{files[0].name}.{extra}")
        elif len(files) > 1:
            total = sum(item.size for item in files)
            self.status.setText(f"{len(files)} selected, {total:,} B.")
        self._update_download_button()

    def set_connection_state(self, state: ConnectionState) -> None:
        self._state = state
        self._sync_download_label()

    def _sync_download_label(self) -> None:
        folder = resolved_download_dir(self._state.download_dir)
        self.save_label.setText(f"Save to {folder}")

    def _update_download_button(self) -> None:
        files = self._selected_files()
        if not files:
            self.download_btn.setText("Download")
            return
        selected = self._selected_items()
        one_group = len(selected) == 1 and str(selected[0].data(0, ROLE_KIND) or "") == "group"
        if one_group:
            self.download_btn.setText("Download this recording")
            return
        if len(files) > 1:
            self.download_btn.setText(f"Download Selected ({len(files)})")
            return
        self.download_btn.setText("Download")

    def _open_recordings_folder(self, folder: Path | None = None) -> None:
        try:
            path = folder if isinstance(folder, Path) else ensure_download_dir(self._state.download_dir)
        except OSError as exc:
            QMessageBox.warning(self, "Recordings folder", f"Could not open download folder.\n{exc}")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _download_selected(self) -> None:
        if self._busy or self._dl_active or self._closing:
            return
        files = self._selected_files()
        if not files:
            QMessageBox.information(self, "Download", "Select files first.")
            return
        self._begin_downloads(files)

    def _begin_downloads(self, files: list[RemoteEntry]) -> None:
        if self._busy or self._dl_active or self._closing:
            return
        try:
            folder = ensure_download_dir(self._state.download_dir)
        except OSError as exc:
            QMessageBox.warning(self, "Download", f"Could not create download folder.\n{exc}")
            return
        self._sync_download_label()
        jobs: list[tuple[RemoteEntry, str]] = []
        try:
            for entry in files:
                jobs.append((entry, str(unique_destination(folder / entry.name))))
        except DownloadError as exc:
            QMessageBox.warning(self, "Download", str(exc))
            return
        self._queue_downloads(jobs)

    def _queue_downloads(self, jobs: list[tuple[RemoteEntry, str]]) -> None:
        if not jobs or self._closing:
            return
        self._dl_cancel = threading.Event()
        self._dl_pending = list(jobs)
        self._dl_ok = 0
        self._dl_fail = 0
        self._dl_cancel_count = 0
        self._dl_total = len(jobs)
        self._dl_bytes = {}
        self._dl_folder = Path(jobs[0][1]).parent
        self.progress.setVisible(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.status.setText(f"Downloading {self._dl_total} file(s) to {self._dl_folder}…")
        self._set_busy(True)
        self._pump_downloads()

    def _cancel_downloads(self) -> None:
        if not (self._dl_pending or self._dl_active):
            return
        self._dl_cancel.set()
        skipped = len(self._dl_pending)
        self._dl_pending.clear()
        self._dl_cancel_count += skipped
        for _thread, worker in list(self._dl_active):
            try:
                worker.abort()
            except Exception:
                pass
        self.status.setText("Cancelling…")

    def _pump_downloads(self) -> None:
        if self._closing:
            return
        while self._dl_pending and len(self._dl_active) < MAX_PARALLEL_DOWNLOADS:
            entry, dest = self._dl_pending.pop(0)
            thread = QThread()
            worker = FileDownloadWorker(
                self._state,
                entry.path,
                dest,
                entry.name,
                expected_size=int(entry.size or 0),
                cancel=self._dl_cancel,
            )
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
        done = self._dl_ok + self._dl_fail + self._dl_cancel_count
        self.status.setText(f"Saved {name} ({done}/{self._dl_total})")

    @Slot(str, str)
    def _on_file_download_failed(self, name: str, text: str) -> None:
        if self._closing:
            return
        message = text or "Download failed."
        if "cancel" in message.casefold():
            self._dl_cancel_count += 1
            self.status.setText(f"Cancelled {name}")
            return
        self._dl_fail += 1
        self.status.setText(f"Failed {name} ({self._dl_ok + self._dl_fail}/{self._dl_total})")
        QMessageBox.warning(self, "Download", f"{name}: {message.splitlines()[-1]}")

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
        parts = []
        if self._dl_ok:
            loc = f" to {self._dl_folder}" if self._dl_folder else ""
            parts.append(f"Downloaded {self._dl_ok} file(s){loc}")
        if self._dl_fail:
            parts.append(f"{self._dl_fail} failed")
        if self._dl_cancel_count:
            parts.append(f"{self._dl_cancel_count} cancelled")
        self.status.setText("; ".join(parts) + "." if parts else "Download stopped.")
        self._set_busy(False)
        if self._dl_ok and self._dl_folder is not None:
            self._open_recordings_folder(self._dl_folder)

    def _reset_default_sort(self) -> None:
        self._sort_column = None
        self._sort_descending = True
        self._user_sort = False

    def _sync_sort_headers(self) -> None:
        active = self._sort_column if self._user_sort else COL_MODIFIED
        descending = self._sort_descending
        header = self.table.headerItem()
        if header is None:
            return
        for col, base in enumerate(_TABLE_HEADERS):
            header.setText(col, header_label(base, active=col == active, descending=descending))
            header.setTextAlignment(col, Qt.AlignmentFlag.AlignCenter)
        header.setToolTip(2, "This file, or combined size for a split recording.")
        header.setToolTip(8, "Total recording time, not this file.")

    def _on_header_clicked(self, section: int) -> None:
        if section not in SORTABLE_COLUMNS:
            return
        already = (self._user_sort and self._sort_column == section) or (
            not self._user_sort and section == COL_MODIFIED
        )
        if already:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_descending = section != COL_NAME
        self._user_sort = True
        self._sort_column = section
        if self._entries:
            self._fill(self._entries)
        else:
            self._sync_sort_headers()

    def _selected_identities(self) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for item in self._selected_items():
            ident = self._item_identity(item)
            if not ident or ident in seen:
                continue
            seen.add(ident)
            found.append(ident)
        return found

    def _restore_selected_identities(self, identities: list[str]) -> None:
        self.table.clearSelection()
        if not identities:
            return
        wanted = set(identities)
        current = None
        for item in self._iter_tree_items():
            ident = self._item_identity(item)
            if ident not in wanted:
                continue
            parent = item.parent()
            if parent is not None:
                parent.setExpanded(True)
                key = str(parent.data(0, ROLE_GROUP) or "")
                if key:
                    self._expanded_groups.add(key)
            item.setSelected(True)
            if current is None:
                current = item
        if current is not None:
            self.table.setCurrentItem(current)
            self.table.scrollToItem(current)

    def _set_item_roles(
        self,
        item: QTreeWidgetItem,
        *,
        path: str,
        kind: str,
        group_key: str = "",
        child_paths: tuple[str, ...] = (),
    ) -> None:
        for col in range(len(_TABLE_HEADERS)):
            item.setTextAlignment(col, Qt.AlignmentFlag.AlignCenter)
            item.setData(col, ROLE_PATH, path)
            item.setData(col, ROLE_KIND, kind)
            item.setData(col, ROLE_GROUP, group_key)
            item.setData(col, ROLE_CHILD_PATHS, child_paths)

    def _fill_row_values(
        self,
        *,
        name: str,
        size_text: str,
        modified,
        rec,
        is_new: bool,
    ) -> list[str]:
        mtime = format_display_datetime(modified) if modified else ""
        values = ["NEW" if is_new else "", name, size_text, mtime, "", "", "", "", ""]
        if rec is not None:
            start, end, center_hz, bw, duration = format_columns(rec)
            values[4:] = [start, end, center_hz, bw, duration]
        return values

    def _is_new_entry(self, entry: RemoteEntry, rec, new_paths: set[str]) -> bool:
        if entry.is_dir:
            return False
        return entry.path in new_paths or (
            rec is not None and rec.id == self._recent_record_id and bool(self._recent_record_id)
        )

    def _fill(self, entries: list[RemoteEntry]) -> None:
        selected = self._selected_identities()
        current_paths = {entry.path for entry in entries if not entry.is_dir}
        new_paths, seen = discovered_new_paths(self._seen_by_dir.get(self._path), current_paths)
        self._seen_by_dir[self._path] = seen
        self._new_paths = new_paths
        matched = match_recordings_to_entries(
            entries,
            load_recordings(),
            host=self._state.host,
        )
        persist_matched_stems(matched)
        self._matched = matched
        self._entries = list(entries)
        nodes = sort_listing_nodes(
            build_listing_nodes(entries, matched),
            matched=matched,
            column=self._sort_column if self._user_sort else None,
            descending=self._sort_descending,
        )
        totals = group_total_bytes(entries)
        counts = group_part_counts(entries)
        self.table.blockSignals(True)
        while self.table.topLevelItemCount():
            self.table.takeTopLevelItem(0)
        for node in nodes:
            if node.kind == "group":
                lead = node.lead
                rec = next((matched.get(entry.path) for entry in node.entries if matched.get(entry.path)), None)
                combined = sum(int(entry.size or 0) for entry in node.entries)
                newest = max((entry.modified for entry in node.entries if entry.modified), default=None)
                is_new = any(self._is_new_entry(entry, matched.get(entry.path), new_paths) for entry in node.entries)
                parent = QTreeWidgetItem(
                    self._fill_row_values(
                        name=node.name,
                        size_text=format_bytes(combined),
                        modified=newest,
                        rec=rec,
                        is_new=is_new,
                    )
                )
                child_paths = tuple(entry.path for entry in node.entries)
                self._set_item_roles(
                    parent,
                    path=lead.path,
                    kind="group",
                    group_key=node.group_key,
                    child_paths=child_paths,
                )
                parent.setChildIndicatorPolicy(QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator)
                parent.setToolTip(1, f"{len(node.entries)} parts")
                parent.setToolTip(2, f"Combined size: {combined:,} B")
                if newest:
                    parent.setToolTip(3, format_display_datetime(newest))
                if rec is not None:
                    parent.setToolTip(8, "Total recording time for this capture.")
                for entry in node.entries:
                    child_rec = matched.get(entry.path)
                    child_new = self._is_new_entry(entry, child_rec, new_paths)
                    child = QTreeWidgetItem(
                        self._fill_row_values(
                            name=iq_part_label(entry.name),
                            size_text=format_bytes(entry.size),
                            modified=entry.modified,
                            rec=child_rec,
                            is_new=child_new,
                        )
                    )
                    self._set_item_roles(child, path=entry.path, kind="file", group_key=node.group_key)
                    child.setChildIndicatorPolicy(
                        QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
                    )
                    child.setToolTip(1, entry.name)
                    child.setToolTip(2, f"{entry.size:,} B")
                    if entry.modified:
                        child.setToolTip(3, format_display_datetime(entry.modified))
                    if child_rec is not None:
                        child.setToolTip(8, "Total recording time, not this part.")
                    parent.addChild(child)
                self.table.addTopLevelItem(parent)
                parent.setExpanded(node.group_key in self._expanded_groups)
                continue
            entry = node.lead
            rec = matched.get(entry.path)
            is_new = self._is_new_entry(entry, rec, new_paths)
            name = entry.name + ("/" if entry.is_dir else "")
            size_text = "" if entry.is_dir else format_bytes(entry.size)
            item = QTreeWidgetItem(
                self._fill_row_values(
                    name=name,
                    size_text=size_text,
                    modified=entry.modified,
                    rec=rec,
                    is_new=is_new,
                )
            )
            self._set_item_roles(
                item,
                path=entry.path,
                kind="dir" if entry.is_dir else "file",
            )
            item.setChildIndicatorPolicy(
                QTreeWidgetItem.ChildIndicatorPolicy.DontShowIndicatorWhenChildless
            )
            if not entry.is_dir:
                stem = iq_group_key(entry.name)
                parts = counts.get(stem, 1)
                combined = totals.get(stem, entry.size)
                if parts > 1:
                    item.setToolTip(2, f"This file: {entry.size:,} B. All {parts} parts: {combined:,} B.")
                else:
                    item.setToolTip(2, f"{entry.size:,} B")
            if entry.modified:
                item.setToolTip(3, format_display_datetime(entry.modified))
            if rec is not None:
                item.setToolTip(8, "Total recording time, not this part.")
            self.table.addTopLevelItem(item)
        self._sync_sort_headers()
        self._restore_selected_identities(selected)
        self.table.blockSignals(False)
        self._on_selection_changed()

    def apply_theme(self) -> None:
        from .theme import restyle

        restyle(self)
        self.table.viewport().update()

    def _request_preview(self, entry: RemoteEntry) -> None:
        if entry.is_dir or self._closing:
            return
        self._preview_token += 1
        token = self._preview_token
        mtime_ts = entry.modified.timestamp() if entry.modified else 0
        key = cache_key(entry.path, entry.size, mtime_ts)
        self.preview_file.setText(entry.name)
        self.preview_file.setProperty("remote_path", entry.path)
        cached = self._preview_cache.get(key)
        if cached is not None:
            self._set_preview_busy(False)
            self._show_preview(cached, token)
            return
        self._set_preview_busy(True)
        self.preview_caption.setText("Preparing waterfall…")
        self._preview_source = None
        self._preview_scaled_key = None
        self.preview_image.setPixmap(QPixmap())
        self.preview_image.setText("Preparing waterfall…")
        if self._preview_jobs:
            for thread, worker in list(self._preview_jobs):
                try:
                    worker.abort()
                except Exception:
                    pass
            self._preview_pending = (token, entry)
            return
        self._start_preview(token, entry)

    def _set_preview_busy(self, busy: bool) -> None:
        self.preview_busy.setVisible(busy)
        if busy:
            self.preview_busy.setRange(0, 0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._preview_source is not None and not self._preview_source.isNull():
            self._preview_resize_timer.start()

    def _rescale_preview(self) -> None:
        pixmap = self._preview_source
        if pixmap is None or pixmap.isNull():
            return
        width = max(self.preview_image.width(), 40)
        height = max(self.preview_image.height(), 40)
        key = (width, height, id(pixmap))
        if key == self._preview_scaled_key:
            return
        self._preview_scaled_key = key
        self.preview_image.setPixmap(
            pixmap.scaled(
                width,
                height,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _start_preview(self, token: int, entry: RemoteEntry) -> None:
        rec = self._matched.get(entry.path)
        center = int(rec.center_hz) if rec is not None else None
        bandwidth = int(rec.bandwidth_hz) if rec is not None else None
        self._preview_active = True
        thread = QThread()
        worker = PreviewWorker(self._state, token, entry, center_hz=center, bandwidth_hz=bandwidth)
        worker.moveToThread(thread)
        queued = Qt.ConnectionType.QueuedConnection
        worker.finished.connect(self._on_preview_ready, queued)
        worker.failed.connect(self._on_preview_failed, queued)
        thread.finished.connect(self._on_preview_thread_finished, queued)
        thread.started.connect(worker.run)
        self._preview_jobs.append((thread, worker))
        thread.start()

    @Slot(int, object)
    def _on_preview_ready(self, token: int, result: object) -> None:
        if self._closing or not isinstance(result, PreviewResult):
            return
        self._preview_cache[result.cache_key] = result
        while len(self._preview_cache) > _PREVIEW_CACHE:
            self._preview_cache.popitem(last=False)
        if token != self._preview_token:
            return
        self._show_preview(result, token)

    @Slot(int, str)
    def _on_preview_failed(self, token: int, text: str) -> None:
        if self._closing or token != self._preview_token:
            return
        detail = text.splitlines()[-1] if text else "Preview failed."
        if "cancel" in detail.casefold():
            return
        self._set_preview_busy(False)
        self._preview_source = None
        self.preview_image.setPixmap(QPixmap())
        self.preview_image.setText("")
        self.preview_caption.setText(detail)

    def _show_preview(self, result: PreviewResult, token: int) -> None:
        if token != self._preview_token:
            return
        self._set_preview_busy(False)
        pixmap = QPixmap()
        pixmap.loadFromData(result.png_bytes, "PNG")
        if pixmap.isNull():
            self.preview_caption.setText(result.caption)
            return
        self._preview_source = pixmap
        self._preview_scaled_key = None
        self.preview_image.setText("")
        self._rescale_preview()
        self.preview_caption.setText(result.caption)

    @Slot()
    def _on_preview_thread_finished(self) -> None:
        thread = self.sender()
        remaining: list[tuple[QThread, PreviewWorker]] = []
        for item in self._preview_jobs:
            if item[0] is thread:
                item[1].deleteLater()
                item[0].deleteLater()
            else:
                remaining.append(item)
        self._preview_jobs = remaining
        self._preview_active = False
        if self._closing:
            return
        pending = self._preview_pending
        self._preview_pending = None
        if pending is None:
            return
        token, entry = pending
        if token != self._preview_token:
            return
        self._start_preview(token, entry)
