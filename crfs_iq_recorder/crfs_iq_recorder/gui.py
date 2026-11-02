"""Simple CRFS IQ Recorder UI."""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QEvent, QObject, QSize, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QIcon, QImageReader, QPixmap, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__
from .api_client import EmpClient, HttpOutcome
from .cellular_panel import CellularPanel
from .cellular_runner import CellularRunner
from .capture_api import (
    OUTCOME_BUSY,
    OUTCOME_CONFIRMED,
    OUTCOME_FAILED,
    OUTCOME_NOT_SENT,
    OUTCOME_SENSOR_ERROR,
    OUTCOME_UNKNOWN,
    CaptureRequest,
    CaptureSummary,
)
from .class_match import suggest_class
from .collection_catalog import preferred_event, load_workbook_catalog
from .connection_dialog import ConnectionDialog
from .connection_monitor import (
    CHECKING,
    CONNECTED,
    DISCONNECTED,
    LINK_CHECK_INTERVAL_MS,
    LINK_CHECK_TIMEOUT_S,
    RECONNECTING,
    LinkMonitor,
    connection_identity,
)
from .connection_state import ConnectionState
from .constants import (
    APP_ID,
    DEFAULT_FREQ_UNIT,
    DEFAULT_RECORDING_FORMAT,
    FREQ_UNITS,
    VERIFIED_RECORDING_FORMATS,
)
from .demo import DemoTransport
from .demo_sftp import shared_demo_browser
from .excel_log import ExcelLogStore, flush_excel_queue, pending_from_row
from .excel_writer import ExcelRow
from .file_watch import (
    NO_FILE_GRACE_S,
    FileWatchState,
    capture_is_settled,
    folders_for_recording,
    list_watch_entries,
    prune_file_watch,
    update_file_watch,
)
from .frequency import (
    FrequencyError,
    FrequencyPlan,
    display_from_hz,
    from_center_bandwidth,
    from_start_end,
    parse_to_integer_hz,
)
from .paths import (
    brand_icon_path,
    ensure_download_dir,
    load_settings,
    resolved_download_dir,
    save_settings,
    user_config_dir,
)
from .recording_history import (
    add_recording,
    iq_group_key,
    live_capture_files,
    load_recordings,
    new_recording,
    persist_matched_stems,
    recording_group_key,
    remote_directory,
    remove_recording,
)
from .recording_status import (
    CONFIRMED,
    FINALIZING,
    IDLE,
    PHASE_LABELS,
    RECORDING,
    SUBMITTING,
    UNKNOWN,
    WAITING,
)
from .request_builder import RequestBuildError, build_payload_for_recording_time, unique_task_id
from .sanitizer import redact_text
from .sensor_info import SensorInfo, blank_info
from .sensor_label import workbook_sensor_name
from .sensor_storage import (
    LEVEL_CRITICAL,
    LEVEL_UNAVAILABLE,
    LEVEL_WARN,
    STORAGE_POLL_MS,
    StorageSnapshot,
    estimated_recording_bytes,
    query_iq_storage,
    recording_blocked_reason,
    storage_line,
    storage_note,
    warning_level,
)
from .size_estimate import (
    SizeEstimateError,
    estimated_size_mb,
    format_estimate,
    recorded_seconds_from_bytes,
)
from .merge_controller import MergeController
from .sftp_window import SftpWindow, _open_browser
from .theme import (
    DARK_MODE,
    LIGHT_MODE,
    apply_combo_popup_palette,
    apply_theme,
    current_mode,
    fit_window_to_screen,
    polish_combo,
    restyle,
    theme_toggle_icon,
    theme_toggle_tooltip,
)
from .validation import ValidationError, parse_connection, parse_recorded_time
from .worker_threads import stop_worker_threads
from .worker_threads import track as track_worker_thread

def configure_appearance(app: QApplication, mode: str | None = None) -> None:
    apply_theme(app, mode)


class Worker(QObject):
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self._fn())
        except Exception:
            self.failed.emit(traceback.format_exc(limit=6))
        thread = self.thread()
        if thread is not None:
            thread.quit()


def set_app_user_model_id(app_id: str = APP_ID) -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def _icon_path(*, prefer_png: bool = False) -> Path | None:
    return brand_icon_path(prefer_png=prefer_png)


def app_icon() -> QIcon:
    """Sensorz mark for the window, taskbar, and Alt+Tab.

    Prefers the multi-size .ico (16-256 px, so Windows picks a crisp taskbar
    glyph at any DPI). Qt needs its ICO image plugin for that; if the plugin
    is missing from a build, fall back to the PNG so the app never shows the
    generic window icon.

    The returned QIcon stays file-based (lazy). Loading pixmaps eagerly here
    would leave QPixmaps alive past QApplication teardown in tests and crash
    at interpreter exit.
    """
    ico = _icon_path()
    if ico is not None and QImageReader(str(ico)).canRead():
        return QIcon(str(ico))
    png = _icon_path(prefer_png=True)
    icon = QIcon()
    if png is not None and QImageReader(str(png)).canRead():
        for side in (16, 20, 24, 32, 40, 48, 64, 128, 256):
            icon.addFile(str(png), QSize(side, side))
    return icon


class MainWindow(QMainWindow):
    # Events from the sensor's EMP stream, emitted on the HTTP worker thread.
    record_event = Signal(object)
    # A capture ended (confirmed, unknown, not sent, sensor error or failed).
    # Carries a capture_api.CaptureSummary; emitted on the UI thread for every
    # capture, manual or programmatic, exactly once.
    capture_finished = Signal(object)

    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        icon = app_icon()
        if not icon.isNull():
            self.setWindowIcon(icon)

        self._demo_forced = demo
        # Programmatic captures (Cellular Collection): the request being run,
        # and the feature that holds the sensor between its captures so the
        # manual Start button and the repeat series stay out of its way.
        self._external_capture: CaptureRequest | None = None
        self._external_owner: str = ""
        self._summary_emitted = False
        self._sent_recording_format = ""
        self._jobs: list[tuple[QThread, Worker]] = []
        self._alive = True
        self._sensor_info = blank_info(demo=demo)
        self._info_token = 0
        self._link = LinkMonitor()
        self._link_token = 0
        self._link_in_flight = False
        self._http_in_flight = False
        self._recording_active = False
        self._submit_lock = False
        self._ignore_late_http = False
        self._record_gen = 0
        self._record_phase = IDLE
        self._record_client: EmpClient | None = None
        self._record_cancel = threading.Event()
        self._sensor_finished = False
        self._sensor_finished_at = 0.0
        self._stream_lost = False
        self._sensor_parts: list[str] = []
        self._sensor_warnings: list[str] = []
        self._expected_bytes = 0
        self._pending_excel_entries: list = []
        self.record_event.connect(self._on_record_event, Qt.ConnectionType.QueuedConnection)
        self._sftp_win: SftpWindow | None = None
        self._last_recording_s: float | None = None
        self._open_record_id: str | None = None
        self._log_collapsed = False
        self._log_sizes = [680, 300]
        self._catalog = None
        self._excel_store = ExcelLogStore()
        self._merge = MergeController(self)
        self._merge.live_stem = self._live_recording_stem
        self._merge.log.connect(self._on_merge_log)
        self._merge.status_changed.connect(self._on_merge_status)
        self._merge.progress_changed.connect(self._on_merge_progress)
        self._merge.busy_changed.connect(self._on_merge_busy)
        self._excel_flush_async = not demo
        self._excel_flush_in_flight = False
        self._excel_flush_again = False
        self._class_user_set = False
        self._series_active = False
        self._series_remaining: int | None = None
        self._stop_after_current = False
        self._watching = False
        self._waiting = False
        self._wait_deadline = 0.0
        self._recordings_completed = 0
        self._files_found = 0
        self._watch_state = FileWatchState()
        self._elapsed_at: datetime | None = None
        self._watch_browser = None
        self._watch_cancel = threading.Event()
        self._watch_token = 0
        self._watch_in_flight = False
        self._demo_parts = 1
        self._settle_s = 8.0
        self._stable_needed = 2
        self._record_timer = QTimer(self)
        self._record_timer.setSingleShot(True)
        self._record_timer.timeout.connect(self._on_recording_time_elapsed)
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(2000)
        self._watch_timer.timeout.connect(self._poll_collection_files)
        self._excel_timer = QTimer(self)
        self._excel_timer.setInterval(15000)
        self._excel_timer.timeout.connect(self._flush_excel)
        self._excel_timer.start()
        self._wait_timer = QTimer(self)
        self._wait_timer.setInterval(200)
        self._wait_timer.timeout.connect(self._tick_wait)
        self._storage = StorageSnapshot.unavailable()
        self._storage_token = 0
        self._storage_level = ""
        self._storage_timer = QTimer(self)
        self._storage_timer.setInterval(STORAGE_POLL_MS)
        self._storage_timer.timeout.connect(self._refresh_storage)
        self._storage_cancel = threading.Event()
        self._storage_in_flight = False
        self._storage_rerun = False
        self._link_timer = QTimer(self)
        self._link_timer.setInterval(LINK_CHECK_INTERVAL_MS)
        self._link_timer.timeout.connect(self._poll_link)
        self._conn = ConnectionState.from_settings(load_settings(), demo=demo)
        self._plan = self._conn.frequency_plan()
        self._merge.enabled = bool(self._conn.merge_parts)
        # Cellular Collection drives captures through start_capture()/capture_finished
        # and keeps its own session state under the user config folder.
        self._cellular = CellularRunner(
            self,
            config_dir=user_config_dir(),
            download_dir=lambda: resolved_download_dir(self._conn.download_dir),
            parent=self,
        )

        central = QWidget()
        central.setObjectName("central")
        central.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header())
        root.addWidget(self._sensor_strip())

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self._recording_panel())
        self.splitter.addWidget(self._log_panel())
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setCollapsible(0, False)
        self.splitter.setCollapsible(1, True)
        self.splitter.setSizes(self._log_sizes)
        body = QWidget()
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(16, 8, 16, 12)
        body_l.setSpacing(8)
        body_l.addWidget(self.splitter)
        root.addWidget(body, 1)

        self.setStatusBar(QStatusBar())
        self.link_status = QLabel("No sensor IP")
        self.link_status.setObjectName("linkStatusIdle")
        self.statusBar().addWidget(self.link_status, 1)
        self.statusBar().setSizeGripEnabled(True)
        self._restore_session_widgets()
        self._apply_mode_fields()
        self._refresh_size()
        self._sync_connection_label()
        self._catalog_token = 0
        self._update_collection_status()
        if current_mode() != self._conn.ui_theme:
            app = QApplication.instance()
            if app is not None:
                apply_theme(app, self._conn.ui_theme)
            polish_combo(self.unit_combo)
            polish_combo(self.format_combo)
        self.log("Ready.")
        self._sync_theme_button()
        fit_window_to_screen(self, 1100, 720, min_width=640, min_height=420)
        if demo:
            # Demo is in-process and instant; keep it synchronous so the
            # window is complete as soon as it is constructed.
            self._start_background_init()
        else:
            # Sensor HTTP, SFTP storage, the link monitor, the saved workbook,
            # and the Excel queue all wait until the first frame is on screen.
            # A slow or unreachable sensor must not delay the window.
            QTimer.singleShot(0, self._start_background_init)

    def _start_background_init(self) -> None:
        if not self._alive:
            return
        if self._conn.excel_workbook:
            if self._conn.demo:
                self._load_workbook(self._conn.excel_workbook, quiet=True)
            else:
                self._load_workbook_in_background(self._conn.excel_workbook)
        self._refresh_sensor_info()
        self._refresh_storage()
        self._restart_link_monitor()
        try:
            # A merge interrupted by a crash or power loss leaves only a
            # "<name>.wav.partial" file; it is never shown as a result.
            self._merge.sweep_partials(resolved_download_dir(self._conn.download_dir))
        except Exception:  # noqa: BLE001 - housekeeping only
            pass
        if self._conn.demo:
            self._flush_excel()
        else:
            # Excel COM checks run on the UI thread; give the window a moment
            # to paint and settle before the first queue flush.
            QTimer.singleShot(1500, self._flush_excel)

    def _header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("headerBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 10, 16, 10)
        layout.setSpacing(12)
        mark = _icon_path(prefer_png=True)
        if mark is not None:
            pix = QPixmap(str(mark))
            if not pix.isNull():
                logo = QLabel()
                logo.setObjectName("headerLogo")
                logo.setPixmap(
                    pix.scaled(
                        32,
                        32,
                        Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation,
                    )
                )
                layout.addWidget(logo)
        title = QLabel(APP_NAME)
        title.setObjectName("headerTitle")
        layout.addWidget(title)
        layout.addStretch(1)
        ver = QLabel(f"v{__version__}")
        ver.setObjectName("headerSub")
        layout.addWidget(ver)
        self.theme_btn = QPushButton()
        self.theme_btn.setObjectName("themeToggle")
        self.theme_btn.setAutoDefault(False)
        self.theme_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.theme_btn.setIconSize(QSize(20, 20))
        self.theme_btn.setFixedSize(36, 36)
        self.theme_btn.clicked.connect(self._toggle_theme)
        layout.addWidget(self.theme_btn)
        self.activity_btn = QPushButton("Hide log")
        self.activity_btn.setObjectName("ghost")
        self.activity_btn.setAutoDefault(False)
        self.activity_btn.clicked.connect(self._toggle_log)
        layout.addWidget(self.activity_btn)
        settings_btn = QPushButton("Settings")
        settings_btn.setObjectName("ghost")
        settings_btn.setAutoDefault(False)
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(settings_btn)
        return bar

    def _sensor_strip(self) -> QWidget:
        host = QWidget()
        column = QVBoxLayout(host)
        column.setContentsMargins(16, 12, 16, 0)
        column.setSpacing(4)
        card = QFrame()
        card.setObjectName("sensorStrip")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(12)
        facts = QHBoxLayout()
        facts.setSpacing(16)
        self.conn_label = self._sensor_fact(facts, "Sensor")
        self.model_value = self._sensor_fact(facts, "Model")
        self.firmware_value = self._sensor_fact(facts, "Firmware")
        self.serial_value = self._sensor_fact(facts, "Serial")
        self.storage_value = self._sensor_fact(facts, "IQ Storage")
        layout.addLayout(facts, 1)
        self.status_value = QLabel("No sensor IP")
        self.status_value.setObjectName("badgeIdle")
        layout.addWidget(self.status_value, 0, Qt.AlignmentFlag.AlignVCenter)
        column.addWidget(card)
        self.storage_note = QLabel("")
        self.storage_note.setObjectName("storageNoteMuted")
        self.storage_note.setWordWrap(True)
        self.storage_note.setVisible(False)
        column.addWidget(self.storage_note)
        return host

    def _sensor_fact(self, row: QHBoxLayout, label: str) -> QLabel:
        box = QVBoxLayout()
        box.setSpacing(1)
        caption = QLabel(label.upper())
        caption.setObjectName("sensorFactLabel")
        value = QLabel("—")
        value.setObjectName("sensorFactValue")
        box.addWidget(caption)
        box.addWidget(value)
        row.addLayout(box, 1)
        return value

    def _unit_field(self, suffix: str) -> tuple[QFrame, QLineEdit, QLabel]:
        wrap = QFrame()
        wrap.setObjectName("inputWrap")
        wrap.setMinimumHeight(36)
        wrap.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 4, 0)
        row.setSpacing(0)
        field = QLineEdit()
        field.setObjectName("bareField")
        field.setMinimumHeight(32)
        field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        unit = QLabel(suffix)
        unit.setObjectName("unitSuffix")
        row.addWidget(field, 1)
        row.addWidget(unit)
        return wrap, field, unit

    def _recording_panel(self) -> QWidget:
        holder = QWidget()
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 8, 0)
        card = QFrame()
        card.setObjectName("card")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)

        heading = QLabel("Recording")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro = QLabel("Set the band and time, then start.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.mode_start = QRadioButton("Start / End")
        self.mode_center = QRadioButton("Center / Bandwidth")
        self.mode_center.setChecked(True)
        modes = QHBoxLayout()
        modes.setSpacing(16)
        modes.addWidget(self.mode_start)
        modes.addWidget(self.mode_center)
        modes.addStretch()
        unit_caption = QLabel("Units")
        unit_caption.setObjectName("muted")
        modes.addWidget(unit_caption)
        self.unit_combo = QComboBox()
        self.unit_combo.setMinimumWidth(88)
        self.unit_combo.setMinimumHeight(36)
        for unit in FREQ_UNITS:
            self.unit_combo.addItem(unit)
        self.unit_combo.setCurrentText(self._conn.freq_unit or DEFAULT_FREQ_UNIT)
        apply_combo_popup_palette(self.unit_combo, dark=False)
        self.unit_combo.currentTextChanged.connect(self._on_unit_changed)
        modes.addWidget(self.unit_combo)
        layout.addLayout(modes)
        group = QButtonGroup(self)
        group.addButton(self.mode_start)
        group.addButton(self.mode_center)
        self.mode_start.toggled.connect(self._on_mode_changed)

        fields = QWidget()
        fields.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        grid = QGridLayout(fields)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(1, 1)
        grid.setColumnMinimumWidth(0, 140)
        unit = self._freq_unit()
        self.label_a = QLabel("Center frequency")
        self.label_b = QLabel("Bandwidth")
        wrap_a, self.field_a, self.unit_a = self._unit_field(unit)
        wrap_b, self.field_b, self.unit_b = self._unit_field(unit)
        time_wrap, self.time_edit, _time_suffix = self._unit_field("s")
        self.time_edit.setText(f"{self._conn.recorded_time_s:g}")
        self.time_edit.setPlaceholderText("0.1")
        self.format_combo = QComboBox()
        self.format_combo.setMinimumHeight(36)
        self.format_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        for item in VERIFIED_RECORDING_FORMATS:
            self.format_combo.addItem(item.short_label, item.identifier)
            self.format_combo.setItemData(self.format_combo.count() - 1, item.tooltip, Qt.ItemDataRole.ToolTipRole)
        index = self.format_combo.findData(self._conn.recording_format or DEFAULT_RECORDING_FORMAT)
        self.format_combo.setCurrentIndex(index if index >= 0 else 0)
        apply_combo_popup_palette(self.format_combo, dark=False)
        self.format_combo.currentIndexChanged.connect(self._refresh_size)
        for widget in (self.field_a, self.field_b, self.time_edit):
            widget.textChanged.connect(self._refresh_size)
        format_label = QLabel("Recording format")
        time_label = QLabel("Recording time")
        for lab in (self.label_a, self.label_b, format_label, time_label):
            lab.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        time_box = QWidget()
        time_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        time_row = QHBoxLayout(time_box)
        time_row.setContentsMargins(0, 0, 0, 0)
        time_row.setSpacing(16)
        time_row.addWidget(time_wrap, 1)
        self.size_label = QLabel("")
        self.size_label.setObjectName("sizeHint")
        self.size_label.setWordWrap(True)
        time_row.addWidget(self.size_label, 1)
        grid.addWidget(self.label_a, 0, 0)
        grid.addWidget(wrap_a, 0, 1)
        grid.addWidget(self.label_b, 1, 0)
        grid.addWidget(wrap_b, 1, 1)
        grid.addWidget(format_label, 2, 0)
        grid.addWidget(self.format_combo, 2, 1)
        grid.addWidget(time_label, 3, 0)
        grid.addWidget(time_box, 3, 1)
        for row in range(4):
            grid.setRowMinimumHeight(row, 38)
        layout.addWidget(fields)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        layout.addWidget(self.msg)

        layout.addWidget(self._collection_section())

        self.start_btn = QPushButton("Start Recording")
        self.start_btn.setObjectName("primary")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)

        self.stop_btn = QPushButton("Stop after current recording")
        self.stop_btn.setObjectName("ghost")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._on_stop_after_current)
        layout.addWidget(self.stop_btn)

        self.phase_label = QLabel(PHASE_LABELS[IDLE])
        self.phase_label.setObjectName("muted")
        self.phase_label.setWordWrap(True)
        layout.addWidget(self.phase_label)

        self.sftp_btn = QPushButton("Sensor Files")
        self.sftp_btn.setObjectName("ghost")
        self.sftp_btn.clicked.connect(self._open_sftp)
        layout.addWidget(self.sftp_btn)

        merge_row = QHBoxLayout()
        merge_row.setSpacing(8)
        self.merge_check = QCheckBox("Merge recording parts")
        self.merge_check.setToolTip(
            "The sensor splits a recording into numbered WAVE parts (500 MB each on this Node).\n"
            "When on, the parts of a recording are joined into one <name>_merged.wav next to\n"
            "them as soon as all of them have been downloaded. The original parts are kept.\n"
            "Off: nothing changes; a selected recording can still be merged from Sensor Files."
        )
        self.merge_check.setChecked(bool(self._conn.merge_parts))
        self.merge_check.toggled.connect(self._on_merge_toggled)
        self.merge_cancel_btn = QPushButton("Cancel merge")
        self.merge_cancel_btn.setObjectName("ghost")
        self.merge_cancel_btn.setVisible(False)
        self.merge_cancel_btn.clicked.connect(self._cancel_merge)
        merge_row.addWidget(self.merge_check)
        merge_row.addStretch(1)
        merge_row.addWidget(self.merge_cancel_btn)
        layout.addLayout(merge_row)
        self.merge_status = QLabel("")
        self.merge_status.setObjectName("muted")
        self.merge_status.setWordWrap(True)
        self.merge_status.setVisible(False)
        layout.addWidget(self.merge_status)
        self.merge_progress = QProgressBar()
        self.merge_progress.setObjectName("thin")
        self.merge_progress.setTextVisible(False)
        self.merge_progress.setRange(0, 100)
        self.merge_progress.setVisible(False)
        layout.addWidget(self.merge_progress)

        self.cellular_panel = CellularPanel(self._cellular, host=self, config_dir=user_config_dir())
        layout.addWidget(self.cellular_panel)
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        scroll.setWidget(card)
        outer.addWidget(scroll)
        return holder

    def _collection_section(self) -> QWidget:
        box = QFrame()
        box.setObjectName("inputWrap")
        box.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.excel_section = box
        layout = QVBoxLayout(box)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.excel_check = QCheckBox("Log recordings to Excel")
        self.excel_check.setChecked(bool(self._conn.excel_enabled))
        self.excel_check.toggled.connect(self._on_excel_toggled)
        layout.addWidget(self.excel_check)

        details = QWidget()
        details.setObjectName("excelDetails")
        details.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)
        self.excel_details = details
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.setSpacing(8)

        book_row = QHBoxLayout()
        book_row.setSpacing(8)
        book_label = QLabel("Workbook")
        book_label.setObjectName("muted")
        self.workbook_edit = QLineEdit()
        self.workbook_edit.setReadOnly(True)
        self.workbook_edit.setPlaceholderText("Choose a .xlsm collection workbook")
        if self._conn.excel_workbook:
            self.workbook_edit.setText(self._conn.excel_workbook)
        browse = QPushButton("Browse")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._browse_workbook)
        book_row.addWidget(book_label)
        book_row.addWidget(self.workbook_edit, 1)
        book_row.addWidget(browse)
        detail_layout.addLayout(book_row)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self.event_combo = QComboBox()
        self.event_combo.setEditable(True)
        self.event_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        apply_combo_popup_palette(self.event_combo, dark=False)
        self.class_combo = QComboBox()
        self.class_combo.setEditable(True)
        self.class_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        apply_combo_popup_palette(self.class_combo, dark=False)
        self.class_combo.currentTextChanged.connect(self._on_class_edited)
        self.sheet_combo = QComboBox()
        self.sheet_combo.setEditable(True)
        self.sheet_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        apply_combo_popup_palette(self.sheet_combo, dark=False)
        grid.addWidget(QLabel("Collection event"), 0, 0)
        grid.addWidget(self.event_combo, 0, 1)
        grid.addWidget(QLabel("Target class"), 1, 0)
        grid.addWidget(self.class_combo, 1, 1)
        grid.addWidget(QLabel("Worksheet"), 2, 0)
        grid.addWidget(self.sheet_combo, 2, 1)
        detail_layout.addLayout(grid)

        repeat_row = QHBoxLayout()
        repeat_row.setSpacing(12)
        repeat_caption = QLabel("Repeat recording")
        repeat_caption.setObjectName("muted")
        self.repeat_off = QRadioButton("Off")
        self.repeat_n = QRadioButton("Count")
        self.repeat_until = QRadioButton("Until stopped")
        self.repeat_off.setChecked(self._conn.repeat_mode == "off" or not self._conn.repeat_mode)
        self.repeat_n.setChecked(self._conn.repeat_mode == "count")
        self.repeat_until.setChecked(self._conn.repeat_mode == "until")
        group = QButtonGroup(self)
        group.addButton(self.repeat_off)
        group.addButton(self.repeat_n)
        group.addButton(self.repeat_until)
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 999)
        self.repeat_spin.setValue(int(self._conn.repeat_count or 2))
        self.repeat_spin.setEnabled(self.repeat_n.isChecked())
        self.repeat_n.toggled.connect(lambda on: self.repeat_spin.setEnabled(on))
        repeat_row.addWidget(repeat_caption)
        repeat_row.addWidget(self.repeat_off)
        repeat_row.addWidget(self.repeat_n)
        repeat_row.addWidget(self.repeat_spin)
        repeat_row.addWidget(self.repeat_until)
        repeat_row.addStretch(1)
        detail_layout.addLayout(repeat_row)

        wait_row = QHBoxLayout()
        wait_row.setSpacing(8)
        wait_caption = QLabel("Wait between recordings")
        wait_caption.setObjectName("muted")
        self.wait_spin = QDoubleSpinBox()
        self.wait_spin.setRange(0, 10_000)
        self.wait_spin.setDecimals(2)
        self.wait_spin.setSingleStep(1)
        self.wait_unit = QComboBox()
        self.wait_unit.addItems(["Seconds", "Minutes"])
        apply_combo_popup_palette(self.wait_unit, dark=False)
        unit = "Minutes" if self._conn.repeat_wait_unit == "minutes" else "Seconds"
        self.wait_unit.setCurrentText(unit)
        wait_s = max(float(self._conn.repeat_wait_s), 0.0)
        self.wait_spin.setValue(wait_s / 60.0 if unit == "Minutes" else wait_s)
        wait_row.addWidget(wait_caption)
        wait_row.addWidget(self.wait_spin)
        wait_row.addWidget(self.wait_unit)
        wait_row.addStretch(1)
        detail_layout.addLayout(wait_row)

        self.collection_status = QLabel("Recordings completed: 0  ·  Files found: 0  ·  Excel: Idle")
        self.collection_status.setObjectName("muted")
        self.collection_status.setWordWrap(True)
        detail_layout.addWidget(self.collection_status)
        layout.addWidget(details)
        self._sync_excel_details()
        return box

    def _log_panel(self) -> QWidget:
        self.log_panel = QFrame()
        self.log_panel.setObjectName("card")
        layout = QVBoxLayout(self.log_panel)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        head = QHBoxLayout()
        title = QLabel("Activity")
        title.setObjectName("sectionTitle")
        self.log_toggle = QPushButton("Hide")
        self.log_toggle.setObjectName("ghost")
        self.log_toggle.setFixedWidth(72)
        self.log_toggle.clicked.connect(self._toggle_log)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setObjectName("ghost")
        self.clear_btn.clicked.connect(self._clear_log)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.log_toggle)
        head.addWidget(self.clear_btn)
        layout.addLayout(head)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setObjectName("log")
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.log_edit.setMinimumWidth(180)
        layout.addWidget(self.log_edit, 1)
        return self.log_panel

    def _toggle_log(self) -> None:
        if self._log_collapsed:
            self.log_panel.setVisible(True)
            sizes = self._log_sizes if self._log_sizes[1] > 80 else [680, 300]
            self.splitter.setSizes(sizes)
            self._log_collapsed = False
        else:
            self._log_sizes = self.splitter.sizes()
            self.log_panel.setVisible(False)
            self._log_collapsed = True
        self._sync_log_toggle()

    def _sync_log_toggle(self) -> None:
        header = "Show log" if self._log_collapsed else "Hide log"
        if hasattr(self, "activity_btn"):
            self.activity_btn.setText(header)
        if hasattr(self, "log_toggle"):
            self.log_toggle.setText("Show" if self._log_collapsed else "Hide")

    def _sync_theme_button(self) -> None:
        if not hasattr(self, "theme_btn"):
            return
        tip = theme_toggle_tooltip()
        self.theme_btn.setText("")
        self.theme_btn.setIcon(theme_toggle_icon())
        self.theme_btn.setToolTip(tip)
        self.theme_btn.setAccessibleName(tip)
        self.theme_btn.setAccessibleDescription(tip)

    def _toggle_theme(self) -> None:
        next_mode = LIGHT_MODE if current_mode() == DARK_MODE else DARK_MODE
        self._conn.ui_theme = next_mode
        app = QApplication.instance()
        if app is not None:
            apply_theme(app, next_mode)
        polish_combo(self.unit_combo)
        polish_combo(self.format_combo)
        for combo in (self.event_combo, self.class_combo, self.sheet_combo, self.wait_unit):
            polish_combo(combo)
        self._sync_theme_button()
        self._capture_session_into_conn()
        save_settings(self._conn.persistable())
        if self._sftp_win is not None:
            self._sftp_win.apply_theme()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._alive = False
        self._capture_session_into_conn()
        save_settings(self._conn.persistable())
        if hasattr(self, "cellular_panel"):
            try:
                self.cellular_panel.persist()
            except Exception:  # noqa: BLE001 - closing must not fail on settings
                pass
        if hasattr(self, "_cellular"):
            self._cellular.shutdown()
        self._record_timer.stop()
        self._watch_timer.stop()
        self._wait_timer.stop()
        self._excel_timer.stop()
        self._storage_timer.stop()
        self._link_timer.stop()
        self._link_token += 1
        self._link_in_flight = False
        if getattr(self, "_watch_cancel", None) is not None:
            self._watch_cancel.set()
        if getattr(self, "_storage_cancel", None) is not None:
            self._storage_cancel.set()
        self._close_watch_browser()
        self._abort_record_stream()
        if self._pending_excel_entries and self._open_record_id:
            # Closing mid-capture: queue what finished so far with the requested time.
            record = next((item for item in load_recordings() if item.id == self._open_record_id), None)
            if record is not None:
                try:
                    self._log_pending_iq_files(record, duration_s=None)
                except Exception:
                    pass
        if self._sftp_win:
            self._sftp_win.close()
            self._sftp_win = None
        for thread, _worker in list(self._jobs):
            thread.quit()
        super().closeEvent(event)

    def _abort_record_stream(self) -> None:
        """Stop following the sensor's event stream and release its connection.

        The sensor keeps the recording task listed (and its radio session open)
        for as long as a client holds the EMP response, so this runs as soon as
        the application no longer needs the stream.
        """
        self._record_cancel.set()
        client = self._record_client
        self._record_client = None
        if client is None:
            return
        try:
            client.abort()
        except Exception:
            pass
        try:
            client.close()
        except Exception:
            pass

    def _freq_unit(self) -> str:
        if hasattr(self, "unit_combo"):
            text = self.unit_combo.currentText()
            if text in FREQ_UNITS:
                return text
        return self._conn.freq_unit or DEFAULT_FREQ_UNIT

    def _recording_format(self) -> str:
        if hasattr(self, "format_combo"):
            data = self.format_combo.currentData()
            if isinstance(data, str) and data:
                return data
        return self._conn.recording_format or DEFAULT_RECORDING_FORMAT

    def _restore_session_widgets(self) -> None:
        center = self._conn.freq_mode != "start_end"
        self.mode_center.blockSignals(True)
        self.mode_start.blockSignals(True)
        self.mode_center.setChecked(center)
        self.mode_start.setChecked(not center)
        self.mode_center.blockSignals(False)
        self.mode_start.blockSignals(False)
        self.unit_combo.blockSignals(True)
        self.unit_combo.setCurrentText(self._conn.freq_unit or DEFAULT_FREQ_UNIT)
        self.unit_combo.blockSignals(False)
        self.time_edit.blockSignals(True)
        self.time_edit.setText(f"{self._conn.recorded_time_s:g}")
        self.time_edit.blockSignals(False)
        index = self.format_combo.findData(self._conn.recording_format or DEFAULT_RECORDING_FORMAT)
        self.format_combo.blockSignals(True)
        self.format_combo.setCurrentIndex(index if index >= 0 else 0)
        self.format_combo.blockSignals(False)

    def _capture_session_into_conn(self) -> None:
        plan = self._try_plan() or self._plan
        self._plan = plan
        self._conn.freq_unit = self._freq_unit()
        self._conn.freq_mode = "center" if self.mode_center.isChecked() else "start_end"
        self._conn.center_hz = plan.center_hz
        self._conn.bandwidth_hz = plan.bandwidth_hz
        self._conn.start_hz = plan.start_hz
        self._conn.end_hz = plan.end_hz
        try:
            self._conn.recorded_time_s = float(parse_recorded_time(self.time_edit.text()))
        except ValidationError:
            pass
        self._conn.recording_format = self._recording_format()
        if hasattr(self, "merge_check"):
            self._conn.merge_parts = self.merge_check.isChecked()
        if hasattr(self, "excel_check"):
            self._conn.excel_enabled = self.excel_check.isChecked()
            self._conn.excel_workbook = self.workbook_edit.text().strip()
            self._conn.collection_event = self.event_combo.currentText().strip()
            if self.repeat_until.isChecked():
                self._conn.repeat_mode = "until"
            elif self.repeat_n.isChecked():
                self._conn.repeat_mode = "count"
            else:
                self._conn.repeat_mode = "off"
            self._conn.repeat_count = int(self.repeat_spin.value())
            self._conn.repeat_wait_unit = "minutes" if self.wait_unit.currentText() == "Minutes" else "seconds"
            self._conn.repeat_wait_s = self._repeat_wait_s()

    def _repeat_mode(self) -> str:
        if hasattr(self, "repeat_until") and self.repeat_until.isChecked():
            return "until"
        if hasattr(self, "repeat_n") and self.repeat_n.isChecked():
            return "count"
        return "off"

    def _sync_excel_details(self) -> None:
        if not hasattr(self, "excel_details"):
            return
        visible = bool(self.excel_check.isChecked())
        self.excel_details.setVisible(visible)
        section = getattr(self, "excel_section", None)
        if section is None:
            return
        layout = section.layout()
        if layout is not None:
            layout.activate()
        section.updateGeometry()
        section.adjustSize()
        card = section.parentWidget()
        if card is not None:
            if card.layout() is not None:
                card.layout().activate()
            card.updateGeometry()
            card.adjustSize()

    def _on_excel_toggled(self, checked: bool) -> None:
        self._sync_excel_details()
        if checked and self._catalog is None and self.workbook_edit.text().strip():
            self._load_workbook(self.workbook_edit.text().strip(), quiet=True)
        self._update_collection_status()

    def _browse_workbook(self) -> None:
        start = self.workbook_edit.text().strip() or str(Path.home())
        path, _ok = QFileDialog.getOpenFileName(
            self,
            "Collection workbook",
            start,
            "Excel macro workbook (*.xlsm)",
        )
        if not path:
            return
        if self._load_workbook(path):
            self.excel_check.setChecked(True)

    def _load_workbook(self, path: str, *, quiet: bool = False) -> bool:
        file = Path(path)
        if not file.is_file():
            if not quiet:
                QMessageBox.warning(self, "Workbook", "That workbook file was not found.")
            return False
        try:
            catalog = load_workbook_catalog(file)
        except Exception as exc:
            if not quiet:
                QMessageBox.warning(self, "Workbook", f"Could not read the workbook: {exc}")
            return False
        self._catalog_token += 1
        self._apply_catalog(file, catalog, quiet=quiet)
        return True

    def _load_workbook_in_background(self, path: str) -> None:
        """Read the saved workbook on a worker so a large .xlsm never blocks startup."""
        file = Path(path)
        if not file.is_file():
            return
        self._catalog_token += 1
        token = self._catalog_token

        def work() -> tuple[int, Path, Any]:
            return token, file, load_workbook_catalog(file)

        def done(result: Any) -> None:
            if not self._alive or not isinstance(result, tuple) or len(result) != 3:
                return
            got_token, got_file, catalog = result
            if got_token != self._catalog_token:
                return
            if str(got_file) != str(self._conn.excel_workbook or ""):
                return
            self._apply_catalog(got_file, catalog, quiet=True)

        self._start_job(work, done)

    def _apply_catalog(self, file: Path, catalog: Any, *, quiet: bool) -> None:
        self._catalog = catalog
        self.workbook_edit.setText(str(file))
        self._conn.excel_workbook = str(file)
        self.event_combo.blockSignals(True)
        self.sheet_combo.blockSignals(True)
        self.class_combo.blockSignals(True)
        self.event_combo.clear()
        self.event_combo.addItems(list(catalog.events))
        event = self._conn.collection_event or preferred_event(catalog.events)
        if event:
            self.event_combo.setCurrentText(event)
        self.sheet_combo.clear()
        self.sheet_combo.addItems(list(catalog.worksheets))
        classes: list[str] = []
        seen: set[str] = set()
        for item in catalog.mappings:
            if item.target_class in seen:
                continue
            seen.add(item.target_class)
            classes.append(item.target_class)
        self.class_combo.clear()
        self.class_combo.addItems(classes)
        self.event_combo.blockSignals(False)
        self.sheet_combo.blockSignals(False)
        self.class_combo.blockSignals(False)
        self._class_user_set = False
        self._refresh_class_suggestion()
        if not quiet:
            self.log(f"Workbook: {file.name} ({len(catalog.mappings)} class ranges).")
        self._update_collection_status()

    def _on_class_edited(self, _text: str = "") -> None:
        if self.class_combo.signalsBlocked():
            return
        self._class_user_set = True
        text = self.class_combo.currentText().strip()
        if self._catalog and text:
            sheets = [item.worksheet for item in self._catalog.mappings if item.target_class == text]
            unique = list(dict.fromkeys(sheets))
            if len(unique) == 1:
                self.sheet_combo.setCurrentText(unique[0])

    def _refresh_class_suggestion(self) -> None:
        if not hasattr(self, "class_combo") or self._catalog is None:
            return
        plan = self._try_plan()
        if plan is None:
            return
        selected = self.class_combo.currentText().strip() if self._class_user_set else ""
        sheet = self.sheet_combo.currentText().strip() if self._class_user_set else ""
        match = suggest_class(
            plan.start_hz,
            plan.end_hz,
            self._catalog.mappings,
            selected_class=selected,
            selected_sheet=sheet,
        )
        if match.unique and match.mapping is not None and not self._class_user_set:
            self.class_combo.blockSignals(True)
            self.sheet_combo.blockSignals(True)
            self.class_combo.setCurrentText(match.mapping.target_class)
            self.sheet_combo.setCurrentText(match.mapping.worksheet)
            self.class_combo.blockSignals(False)
            self.sheet_combo.blockSignals(False)

    def _selected_mapping(self):
        if self._catalog is None:
            return None
        plan = self._try_plan()
        if plan is None:
            return None
        selected = self.class_combo.currentText().strip()
        sheet = self.sheet_combo.currentText().strip()
        if selected:
            return suggest_class(
                plan.start_hz,
                plan.end_hz,
                self._catalog.mappings,
                selected_class=selected,
                selected_sheet=sheet,
            )
        return suggest_class(plan.start_hz, plan.end_hz, self._catalog.mappings)

    def _update_collection_status(self) -> None:
        if not hasattr(self, "collection_status"):
            return
        pending = self._excel_store.pending_count()
        if pending:
            excel = f"Pending Excel updates: {pending}"
        elif hasattr(self, "excel_check") and self.excel_check.isChecked():
            excel = "Excel: Saved"
        else:
            excel = "Excel: Off"
        self.collection_status.setText(
            f"Recordings completed: {self._recordings_completed}  ·  "
            f"Files found: {self._files_found}  ·  {excel}"
        )

    def _flush_excel(self) -> None:
        """Write queued rows. Real workbooks are written on a worker thread.

        openpyxl loads and Excel COM calls take seconds on a large workbook;
        running them on the UI thread froze the window on every flush. Demo
        mode stays synchronous so the in-process demo is deterministic.
        """
        if not self._alive:
            return
        if self._excel_store.pending_count() == 0:
            self._update_collection_status()
            return
        if not self._excel_flush_async:
            self._show_excel_flush(flush_excel_queue(self._excel_store))
            return
        if self._excel_flush_in_flight:
            self._excel_flush_again = True
            return
        self._excel_flush_in_flight = True
        store = self._excel_store

        def work() -> tuple[int, int]:
            return flush_excel_queue(store)

        self._start_job(work, self._show_excel_flush, excel=True)

    def _show_excel_flush(self, result: Any) -> None:
        self._excel_flush_in_flight = False
        if not self._alive:
            return
        saved, pending = result if isinstance(result, tuple) and len(result) == 2 else (0, 0)
        if saved:
            self.log(f"Excel: saved {saved} row(s).")
        if pending:
            self.log(f"Pending Excel updates: {pending}.")
        self._update_collection_status()
        if self._excel_flush_again:
            self._excel_flush_again = False
            self._flush_excel()

    def _close_watch_browser(self) -> None:
        browser = self._watch_browser
        self._watch_browser = None
        if browser is None:
            return
        try:
            browser.abort()
        except Exception:
            pass

    def _on_stop_after_current(self) -> None:
        if self._waiting:
            self._cancel_wait("Stopped. The next recording was cancelled.")
            self._end_series()
            return
        if not self._series_active:
            return
        self._stop_after_current = True
        self.stop_btn.setEnabled(False)
        self.log("Stop after the current recording. No further recordings will start.")
        self._set_msg("Stopping after this recording.")

    def _sync_connection_label(self) -> None:
        self.conn_label.setText(self._conn.host or "not set")

    def _set_status_style(self, kind: str) -> None:
        names = {"ok": "badgeOk", "wait": "badgeWait", "bad": "badgeBad", "idle": "badgeIdle"}
        self.status_value.setObjectName(names.get(kind, "badgeIdle"))
        restyle(self.status_value)

    def _apply_sensor_info(self, info: SensorInfo) -> None:
        self._sensor_info = info
        self.model_value.setText(info.display_model())
        self.firmware_value.setText(info.display_firmware())
        self.serial_value.setText(info.display_serial())
        lowered = info.status.casefold()
        if info.demo:
            self._link.demo = True
            self._link.note_success()
            self._sync_link_indicators()
        elif info.connected or lowered in {"connected", "online"}:
            if self._link.note_success():
                self._log_link_change()
            self._sync_link_indicators()
        elif lowered in {"checking…", "checking...", "checking"}:
            if self._link.state in {IDLE, CHECKING}:
                self._link.state = CHECKING
                self._sync_link_indicators()
        elif not self._conn.host.strip():
            self._link.reset(self._link_identity(), demo=False, has_host=False)
            self._sync_link_indicators()
        if info.connected or lowered in {"connected", "online", "demo"}:
            self._refresh_storage()

    def _link_identity(self) -> tuple:
        return connection_identity(
            self._conn.host,
            self._conn.http_port,
            self._conn.use_https,
            self._conn.username,
            self._conn.http_password,
            self._conn.demo,
        )

    def _sync_link_indicators(self) -> None:
        text = self._link.label()
        self.status_value.setText(text)
        self._set_status_style(self._link.style())
        if not hasattr(self, "link_status"):
            return
        names = {"ok": "linkStatusOk", "wait": "linkStatusWait", "bad": "linkStatusBad", "idle": "linkStatusIdle"}
        self.link_status.setText(text)
        self.link_status.setObjectName(names.get(self._link.style(), "linkStatusIdle"))
        restyle(self.link_status)

    def _log_link_change(self) -> None:
        state = self._link.state
        if state == CONNECTED:
            self.log("Sensor connected." if not self._link.demo else "Demo sensor ready.")
        elif state == RECONNECTING:
            self.log("Sensor reconnecting...")
        elif state == DISCONNECTED:
            self.log("Sensor disconnected.", level="error")

    def _restart_link_monitor(self) -> None:
        self._link_token += 1
        self._link_in_flight = False
        has_host = bool(self._conn.host.strip()) or self._conn.demo
        self._link.reset(self._link_identity(), demo=self._conn.demo, has_host=has_host)
        self._sync_link_indicators()
        if not has_host:
            self._link_timer.stop()
            return
        self._link_timer.start()
        self._start_link_check(force=True)

    def _poll_link(self) -> None:
        self._start_link_check(force=False)

    def _start_link_check(self, *, force: bool = False) -> None:
        if not self._alive:
            return
        if self._link_in_flight and not force:
            return
        host = self._conn.host.strip()
        if not host and not self._conn.demo:
            return
        try:
            target = self._target()
        except ValidationError:
            self._apply_link_result(self._link_token, False, "Invalid sensor address")
            return
        self._link_in_flight = True
        token = self._link_token
        client = self._client()
        password = self._conn.http_password

        def work() -> tuple[int, bool, str]:
            try:
                ok, detail = client.probe_sensor(target, password, timeout_s=LINK_CHECK_TIMEOUT_S)
                return token, ok, detail
            except Exception as exc:
                return token, False, str(exc) or "Sensor check failed"

        self._start_job(work, self._show_link_result, link=True, link_token=token)

    def _show_link_result(self, result: Any) -> None:
        token = None
        ok = False
        detail = "Sensor check failed"
        if isinstance(result, tuple) and len(result) == 3:
            token, ok, detail = result
        self._apply_link_result(token, bool(ok), str(detail or ""))

    def _apply_link_result(self, token: int | None, ok: bool, detail: str = "") -> None:
        if not self._alive:
            return
        if token is not None and token != self._link_token:
            return
        self._link_in_flight = False
        previous = self._link.state
        changed = self._link.note_success() if ok else self._link.note_failure()
        self._sync_link_indicators()
        if not changed:
            return
        self._log_link_change()
        if ok and previous != CONNECTED:
            self._refresh_sensor_info()
            self._refresh_storage()

    def _refresh_storage(self) -> None:
        if not self._alive:
            return
        if not self._conn.host.strip() and not self._conn.demo:
            self._storage_timer.stop()
            self._apply_storage(StorageSnapshot.unavailable())
            return
        self._storage_timer.start()
        if self._conn.demo:
            try:
                snap = query_iq_storage(shared_demo_browser())
            except Exception as exc:
                snap = StorageSnapshot.unavailable(str(exc))
            if not snap.ok:
                snap = self._failed_storage(snap.detail)
            self._apply_storage(snap)
            return
        from .sftp_window import _open_browser

        state = self._conn
        if getattr(self, "_storage_in_flight", False) and getattr(self, "_storage_state", None) is state:
            # Startup and the first "connected" report both ask within a
            # second or two. Cancelling the first read closed its socket
            # mid-handshake (paramiko logged "Error reading SSH protocol
            # banner") and wasted one of the sensor's few handshake slots.
            # Let the running read finish, then read once more.
            self._storage_rerun = True
            return
        if getattr(self, "_storage_cancel", None) is not None:
            self._storage_cancel.set()  # a read for another sensor: drop it
        self._storage_token += 1
        token = self._storage_token
        cancel = threading.Event()
        self._storage_cancel = cancel
        self._storage_state = state
        self._storage_in_flight = True
        self._storage_rerun = False

        def work() -> tuple[int, StorageSnapshot]:
            browser = None
            try:
                browser = _open_browser(state, cancel=cancel)
                return token, query_iq_storage(browser)
            except Exception as exc:
                return token, StorageSnapshot.unavailable(str(exc))
            finally:
                if browser is not None:
                    try:
                        browser.close()
                    except Exception:
                        pass

        self._start_job(work, self._show_storage_result)

    def _failed_storage(self, detail: str = "") -> StorageSnapshot:
        if self._storage.ok and self._storage.total_bytes > 0:
            return self._storage.as_stale(detail)
        return StorageSnapshot.unavailable(detail)

    def _show_storage_result(self, result: Any) -> None:
        if not self._alive:
            return
        snap: StorageSnapshot
        if isinstance(result, tuple) and len(result) == 2:
            token, snap = result
            if token != self._storage_token:
                return  # superseded by a read for another sensor
        elif isinstance(result, StorageSnapshot):
            snap = result
        else:
            snap = StorageSnapshot.unavailable(str(result))
        self._storage_in_flight = False
        if self._storage_rerun:
            self._storage_rerun = False
            QTimer.singleShot(0, self._refresh_storage)
        if not snap.ok:
            snap = self._failed_storage(snap.detail)
        self._apply_storage(snap)

    def _apply_storage(self, snap: StorageSnapshot) -> None:
        self._storage = snap
        if not hasattr(self, "storage_value"):
            return
        self.storage_value.setText(storage_line(snap))
        level = warning_level(snap)
        if snap.stale:
            name = "muted"
        elif level == LEVEL_CRITICAL:
            name = "storageValueCritical"
        elif level == LEVEL_WARN:
            name = "storageValueWarn"
        else:
            name = "sensorFactValue"
        self.storage_value.setObjectName(name)
        restyle(self.storage_value)
        note = storage_note(snap)
        self.storage_note.setText(note)
        self.storage_note.setVisible(bool(note))
        if note and level == LEVEL_CRITICAL:
            self.storage_note.setObjectName("storageNoteCritical")
        elif note and level == LEVEL_WARN:
            self.storage_note.setObjectName("storageNoteWarn")
        else:
            self.storage_note.setObjectName("storageNoteMuted")
        restyle(self.storage_note)
        key = f"{level}|stale={snap.stale}"
        if key != self._storage_level:
            self._storage_level = key
            if level == LEVEL_CRITICAL:
                self.log(note or "Critical IQ storage: under 5% free.", level="error")
            elif level == LEVEL_WARN:
                self.log(note or "Low IQ storage: under 10% free.")
            elif level == LEVEL_UNAVAILABLE or snap.stale:
                self.log(note or "Storage unavailable.")

    def _refresh_sensor_info(self) -> None:
        host = self._conn.host.strip()
        if not host:
            self._apply_sensor_info(blank_info(demo=self._conn.demo))
            return
        if self._conn.demo:
            try:
                target = self._target()
            except ValidationError:
                self._apply_sensor_info(blank_info(host=host, status="Demo", demo=True))
                return
            info = self._client().fetch_sensor_info(target, self._conn.http_password)
            self._apply_sensor_info(info)
            return
        self._info_token += 1
        token = self._info_token
        try:
            target = self._target()
        except ValidationError:
            self._apply_sensor_info(blank_info(host=host, status="Unavailable"))
            return
        client = self._client()
        password = self._conn.http_password
        host_at_start = host

        def work() -> tuple[int, str, SensorInfo]:
            return token, host_at_start, client.fetch_sensor_info(target, password)

        self._start_job(work, self._show_sensor_info_result, info=True, info_token=token)

    def _show_sensor_info_result(self, result: Any) -> None:
        if not self._alive:
            return
        token = None
        host_at_start = ""
        info = result
        if isinstance(result, tuple) and len(result) == 3:
            token, host_at_start, info = result
            if token != self._info_token:
                return
            if host_at_start and host_at_start.casefold() != self._conn.host.strip().casefold():
                return
        elif isinstance(result, tuple) and len(result) == 2:
            token, info = result
            if token != self._info_token:
                return
        self._show_sensor_info(info)

    def _show_sensor_info(self, info: Any) -> None:
        if not self._alive:
            return
        if not isinstance(info, SensorInfo):
            self._apply_sensor_info(blank_info(host=self._conn.host, status="Unavailable"))
            return
        current = self._conn.host.strip().casefold()
        if info.host and current and info.host.strip().casefold() != current:
            return
        self._apply_sensor_info(info)
        if info.connected:
            self.log(
                f"{info.display_serial()} · {info.display_model()} · {info.display_firmware()}"
            )
        elif info.status not in {"Checking…", "No sensor IP", "Demo"}:
            self.log(f"Could not read sensor details: {info.status}")

    def _open_settings(self) -> None:
        dialog = ConnectionDialog(self._conn, self)
        dialog.test_requested.connect(self._test_connection)
        if dialog.exec():
            self._conn = dialog.result_state()
            if self._demo_forced:
                self._conn.demo = True
            try:
                self._conn.download_dir = str(ensure_download_dir(self._conn.download_dir))
            except OSError as exc:
                self.log(f"Could not create download folder: {exc}", level="error")
            self._capture_session_into_conn()
            save_settings(self._conn.persistable())
            if self._sftp_win is not None:
                self._sftp_win.set_connection_state(self._conn)
            self._sync_connection_label()
            self._restart_link_monitor()
            self._refresh_sensor_info()
            self._refresh_storage()
            self.log(f"Saved settings for {self._conn.host or '(no host)'}.")

    def _on_mode_changed(self) -> None:
        self._apply_mode_fields(self._plan)
        self._refresh_size()

    def _on_unit_changed(self, _unit: str = "") -> None:
        previous = self._conn.freq_unit or DEFAULT_FREQ_UNIT
        plan = self._try_plan_with_unit(previous) or self._plan
        self._plan = plan
        self._conn.freq_unit = self._freq_unit()
        self._apply_mode_fields(plan)
        self._refresh_size()

    def _apply_mode_fields(self, plan: FrequencyPlan | None = None) -> None:
        plan = plan or self._plan
        center_mode = self.mode_center.isChecked()
        unit = self._freq_unit()
        self.field_a.blockSignals(True)
        self.field_b.blockSignals(True)
        try:
            if center_mode:
                self.label_a.setText("Center frequency")
                self.label_b.setText("Bandwidth")
                self.field_a.setText(display_from_hz(plan.center_hz, unit))
                self.field_b.setText(display_from_hz(plan.bandwidth_hz, unit))
            else:
                self.label_a.setText("Start frequency")
                self.label_b.setText("End frequency")
                self.field_a.setText(display_from_hz(plan.start_hz, unit))
                self.field_b.setText(display_from_hz(plan.end_hz, unit))
            if hasattr(self, "unit_a"):
                self.unit_a.setText(unit)
                self.unit_b.setText(unit)
        finally:
            self.field_a.blockSignals(False)
            self.field_b.blockSignals(False)

    def _try_plan_with_unit(self, unit: str) -> FrequencyPlan | None:
        try:
            if self.mode_center.isChecked():
                center = parse_to_integer_hz(self.field_a.text(), unit, field="Center frequency")
                bw = parse_to_integer_hz(self.field_b.text(), unit, field="Bandwidth")
                plan = from_center_bandwidth(center, bw)
            else:
                start = parse_to_integer_hz(self.field_a.text(), unit, field="Start frequency")
                end = parse_to_integer_hz(self.field_b.text(), unit, field="End frequency")
                plan = from_start_end(start, end)
        except (FrequencyError, ValidationError) as exc:
            self._set_msg(str(exc), error=True)
            return None
        self._plan = plan
        return plan

    def _try_plan(self) -> FrequencyPlan | None:
        return self._try_plan_with_unit(self._freq_unit())

    def _try_payload(self) -> tuple[dict[str, Any] | None, str | None]:
        plan = self._try_plan()
        if plan is None:
            return None, self.msg.text() or "Check the frequency values."
        try:
            seconds = float(parse_recorded_time(self.time_edit.text()))
            payload = build_payload_for_recording_time(plan, seconds, recording_format=self._recording_format())
        except (ValidationError, RequestBuildError, FrequencyError) as exc:
            return None, str(exc)
        scan = payload["remote_recording_scans"][0]
        if (
            scan["duration"] != scan["rate"]
            or scan["duration"] != scan["capture_length"]
            or float(scan["duration"]) != seconds
        ):
            return None, "duration, rate, and capture_length must match the recording time."
        self._set_msg("")
        return payload, None

    def _refresh_size(self) -> None:
        payload, error = self._try_payload()
        if payload is None:
            self.size_label.setText("Estimate —")
            if error:
                self._set_msg(error, error=True)
            return
        scan = payload["remote_recording_scans"][0]
        try:
            mb = estimated_size_mb(scan["bandwidth"], scan["duration"])
            self.size_label.setText(f"Estimate ≈ {format_estimate(mb)}")
        except (SizeEstimateError, ValidationError, KeyError):
            self.size_label.setText("Estimate —")
        self._refresh_class_suggestion()

    def _set_msg(self, text: str, *, error: bool = False) -> None:
        self.msg.setText(text)
        self.msg.setObjectName("error" if error and text else "muted")
        restyle(self.msg)

    def log(self, message: str, *, level: str = "info") -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_edit.appendPlainText(f"[{stamp}] {redact_text(message)}")
        self.log_edit.moveCursor(QTextCursor.MoveOperation.End)

    def _clear_log(self) -> None:
        self.log_edit.clear()

    def _client(self) -> EmpClient:
        if self._conn.demo:
            return EmpClient(transport=DemoTransport(), demo=True)
        return EmpClient(demo=False)

    def _target(self, state: ConnectionState | None = None):
        cfg = state or self._conn
        return parse_connection(
            cfg.host,
            cfg.http_port,
            use_https=cfg.use_https,
            username=cfg.username,
            timeout_text=cfg.timeout,
        )

    def _set_phase(self, phase: str, message: str | None = None) -> None:
        self._record_phase = phase
        text = message if message is not None else PHASE_LABELS.get(phase, phase)
        if hasattr(self, "phase_label"):
            self.phase_label.setText(text)
            if phase in {SUBMITTING, RECORDING, FINALIZING, WAITING}:
                name = "phaseBusy"
            elif phase == CONFIRMED:
                name = "phaseOk"
            elif phase == UNKNOWN:
                name = "phaseBad"
            else:
                name = "muted"
            self.phase_label.setObjectName(name)
            restyle(self.phase_label)

    def _set_recording_ui(self, active: bool, *, seconds: float | None = None) -> None:
        self._recording_active = active
        if not active:
            self._submit_lock = False
        if active:
            label = f"{seconds:g} s" if seconds is not None else ""
            self.start_btn.setEnabled(False)
            self.start_btn.setText(f"Recording… {label}".strip())
            self._set_phase(RECORDING)
            self._set_msg(f"Recording {label}.")
            return
        hold = self._series_active or self._watching or self._waiting or bool(self._external_owner)
        self.start_btn.setEnabled(not hold)
        if self._external_owner:
            self.start_btn.setText(f"{self._external_owner} running…")
        elif self._series_active or self._waiting:
            self.start_btn.setText("Recording series…")
        elif self._watching:
            # The capture is being confirmed from the sensor's files; starting
            # another recording now would abandon that check half-way.
            self.start_btn.setText("Confirming files…")
        else:
            self.start_btn.setText("Start Recording")
        if hasattr(self, "stop_btn"):
            self.stop_btn.setEnabled((self._series_active or self._waiting) and not self._stop_after_current)

    def _start_job(
        self,
        fn: Callable[[], Any],
        on_ok: Callable[[Any], None],
        *,
        record: bool = False,
        info: bool = False,
        link: bool = False,
        excel: bool = False,
        record_gen: int | None = None,
        info_token: int | None = None,
        link_token: int | None = None,
    ) -> None:
        thread = QThread()
        worker = Worker(fn)
        worker._on_ok = on_ok  # noqa: SLF001
        worker._is_record = record  # noqa: SLF001
        worker._is_info = info  # noqa: SLF001
        worker._is_link = link  # noqa: SLF001
        worker._is_excel = excel  # noqa: SLF001
        worker._record_gen = record_gen  # noqa: SLF001
        worker._info_token = info_token  # noqa: SLF001
        worker._link_token = link_token  # noqa: SLF001
        worker.moveToThread(thread)
        queued = Qt.ConnectionType.QueuedConnection
        worker.finished.connect(self._job_finished, queued)
        worker.failed.connect(self._job_failed, queued)
        thread.finished.connect(self._job_thread_finished, queued)
        thread.started.connect(worker.run)
        self._jobs.append((thread, worker))
        track_worker_thread(thread, worker)
        thread.start()

    @Slot(object)
    def _job_finished(self, result: Any) -> None:
        worker = self.sender()
        callback = getattr(worker, "_on_ok", None)
        if callback is not None:
            callback(result)

    @Slot(str)
    def _job_failed(self, text: str) -> None:
        worker = self.sender()
        if getattr(worker, "_is_excel", False):
            self._excel_flush_in_flight = False
            if self._alive:
                self.log(f"Excel update failed: {text}", level="error")
                self._update_collection_status()
            return
        if getattr(worker, "_is_record", False):
            gen = getattr(worker, "_record_gen", None)
            if gen is not None and gen != self._record_gen:
                return
            self._http_in_flight = False
            if self._recording_active:
                self._record_timer.stop()
                self._set_recording_ui(False)
                self._set_phase(IDLE)
                self._end_series()
                self.log(text, level="error")
                # The request may or may not have reached the sensor: an
                # exception on the worker says nothing about that.
                self._emit_capture_summary(OUTCOME_FAILED, detail=text)
                return
            if self._ignore_late_http:
                self._ignore_late_http = False
                return
            self.log(text, level="error")
            return
        if getattr(worker, "_is_link", False):
            token = getattr(worker, "_link_token", None)
            if token is not None and token != self._link_token:
                return
            self._apply_link_result(token, False, "Sensor check failed")
            return
        if getattr(worker, "_is_info", False) and self._alive:
            token = getattr(worker, "_info_token", None)
            if token is not None and token != self._info_token:
                return
            self.log("Could not read sensor details.", level="error")
            return
        self.log(text, level="error")

    @Slot()
    def _job_thread_finished(self) -> None:
        thread = self.sender()
        remaining: list[tuple[QThread, Worker]] = []
        for item in self._jobs:
            if item[0] is thread:
                item[1].deleteLater()
                item[0].deleteLater()
            else:
                remaining.append(item)
        self._jobs = remaining

    def _test_connection(self, state: ConnectionState) -> None:
        try:
            target = self._target(state)
        except ValidationError as exc:
            QMessageBox.warning(self, "Connection", str(exc))
            return
        client = EmpClient(transport=DemoTransport(), demo=True) if state.demo else EmpClient(demo=False)
        self.log(f"Testing connection to {target.origin}/ …")

        def work() -> HttpOutcome:
            return client.test_connection(target, state.http_password)

        self._start_job(work, self._show_test_outcome)

    # ------------------------------------------------------------------ programmatic captures
    def excel_logging_target(self) -> tuple[Path, object] | None:
        """Workbook path and class catalog while "Log to Excel" is on with a loaded workbook."""
        if not hasattr(self, "excel_check") or not self.excel_check.isChecked():
            return None
        path = Path(self.workbook_edit.text().strip() or "")
        if not path.is_file():
            return None
        return path, self._catalog

    def capture_busy_reason(self) -> str | None:
        """Why a new capture cannot start now, or None when the sensor is free."""
        if self._external_owner and self._external_capture is None and not self._recording_active:
            # Held between the owner's captures; the owner itself may start.
            return None
        if self._recording_active or self._submit_lock or self._http_in_flight:
            return "A recording is in progress."
        if self._record_phase in {SUBMITTING, RECORDING}:
            return "A recording is in progress."
        if self._watching or self._record_phase == FINALIZING:
            return "The previous recording is still being confirmed from the sensor's files."
        if self._series_active or self._waiting:
            return "A repeat series is running. Stop it first."
        return None

    def set_external_owner(self, name: str) -> str | None:
        """Reserve the sensor for a feature that runs its own capture sequence.

        Returns an error text when a manual recording or series is active;
        None when the reservation took effect. Manual Start is held until
        `release_external_owner()`.
        """
        if self._external_owner and self._external_owner != name:
            return f"{self._external_owner} is using the sensor."
        reason = self.capture_busy_reason()
        if reason and not self._external_owner:
            return reason
        self._external_owner = name
        self._set_recording_ui(self._recording_active)
        return None

    def release_external_owner(self, name: str | None = None) -> None:
        if name is not None and self._external_owner and self._external_owner != name:
            return
        self._external_owner = ""
        if not self._recording_active:
            self._set_recording_ui(False)

    def start_capture(self, request: CaptureRequest) -> str | None:
        """Start one recording for another feature. Returns an error text or None.

        The request's own frequency plan, duration and labels are used; the
        manual fields are not read. The caller learns how the capture ended
        from `capture_finished`.
        """
        if self._external_owner and request.owner != self._external_owner:
            return f"{self._external_owner} is using the sensor."
        if self._recording_active or self._submit_lock or self._http_in_flight:
            return "A recording is in progress."
        if self._watching or self._record_phase in {SUBMITTING, RECORDING, FINALIZING}:
            return "The previous recording is still being confirmed from the sensor's files."
        if self._series_active or self._waiting:
            return "A repeat series is running."
        try:
            seconds = float(request.seconds)
        except (TypeError, ValueError):
            return "Invalid recording time."
        if not (seconds > 0):
            return "Recording time must be positive."
        plan = request.plan
        if plan is None or int(plan.bandwidth_hz) <= 0:
            return "Invalid frequency plan."
        if not self._conn.host.strip() and not self._conn.demo:
            return "Set the sensor IP in Settings."
        try:
            target = self._target()
        except ValidationError as exc:
            return str(exc)
        try:
            estimated = estimated_recording_bytes(int(plan.bandwidth_hz), seconds)
        except Exception:
            estimated = 0
        blocked = recording_blocked_reason(self._storage, estimated)
        if blocked:
            self.log(blocked, level="error")
            return blocked
        self._submit_lock = True
        self._external_capture = request
        try:
            self._launch_capture(plan, seconds, target=target, mapping=None, request=request)
        except Exception as exc:  # noqa: BLE001 - report instead of leaving the lock set
            self._external_capture = None
            self._submit_lock = False
            self.log(f"Could not start the capture: {exc}", level="error")
            return f"Could not start the capture: {exc}"
        return None

    def _emit_capture_summary(self, outcome: str, *, detail: str = "", record=None) -> None:
        """Report the end of the current capture once, to whoever is listening."""
        if self._summary_emitted:
            return
        self._summary_emitted = True
        request = self._external_capture
        self._external_capture = None
        if record is None and self._open_record_id:
            record = next((item for item in load_recordings() if item.id == self._open_record_id), None)
        total, parts, held_s = self._measured_capture(record) if record is not None else (0, 0, None)
        if outcome in {OUTCOME_NOT_SENT, OUTCOME_BUSY}:
            total, parts, held_s = 0, 0, None
        state = self._watch_state
        finalized = sorted(state.finalized)
        stem = ""
        if self._sensor_parts:
            from .recording_history import iq_group_key

            stem = iq_group_key(self._sensor_parts[0])
        elif record is not None and (record.iq_stem or "").strip():
            stem = record.iq_stem.strip()
        summary = CaptureSummary(
            outcome=outcome,
            request=request,
            record_id=record.id if record is not None else self._open_record_id,
            phase=self._record_phase,
            detail=detail or (self.msg.text() if hasattr(self, "msg") else ""),
            requested_s=float(record.duration_s) if record is not None else (
                float(request.seconds) if request is not None else 0.0
            ),
            held_s=held_s,
            parts=parts,
            total_bytes=total,
            expected_bytes=int(self._expected_bytes or 0),
            sensor_finished=bool(self._sensor_finished),
            stream_lost=bool(self._stream_lost),
            request_sent=outcome not in {OUTCOME_NOT_SENT, OUTCOME_BUSY},
            sensor_parts=list(self._sensor_parts),
            finalized_paths=finalized,
            sizes={path: int(state.sizes.get(path, 0)) for path in finalized},
            sensor_warnings=list(self._sensor_warnings),
            started_at=record.started_at if record is not None else None,
            finished_at=datetime.now(),
            stem=stem,
            recording_format="" if outcome in {OUTCOME_NOT_SENT, OUTCOME_BUSY} else self._sent_recording_format,
        )
        try:
            self.capture_finished.emit(summary)
        except Exception:  # noqa: BLE001 - a listener must not break the recorder
            self.log(traceback.format_exc(limit=4), level="error")

    def _on_start(self) -> None:
        if self._recording_active or self._submit_lock:
            return
        if self._external_owner:
            QMessageBox.warning(
                self,
                "Sensor in use",
                f"{self._external_owner} is running its own recording sequence. Stop it before recording manually.",
            )
            return
        if self._watching or self._record_phase == FINALIZING:
            # The manual button is disabled in this state; a programmatic or
            # keyboard trigger must not abandon the confirmation half-way.
            return
        self._submit_lock = True
        payload, error = self._try_payload()
        if payload is None:
            self._submit_lock = False
            if self._series_active:
                self._end_series()
            QMessageBox.warning(self, "Cannot start", error or "Check frequency and time.")
            return
        scan = payload["remote_recording_scans"][0]
        try:
            estimated = estimated_recording_bytes(int(scan["bandwidth"]), scan["duration"])
        except Exception:
            estimated = 0
        blocked = recording_blocked_reason(self._storage, estimated)
        if blocked:
            self._submit_lock = False
            if self._series_active:
                self._end_series()
            self.log(blocked, level="error")
            QMessageBox.warning(self, "Not enough IQ storage", blocked)
            return
        mapping = None
        if hasattr(self, "excel_check") and self.excel_check.isChecked():
            workbook = self.workbook_edit.text().strip()
            if not workbook or self._catalog is None:
                self._submit_lock = False
                if self._series_active:
                    self._end_series()
                QMessageBox.warning(self, "Cannot start", "Choose a collection workbook before logging to Excel.")
                return
            match = self._selected_mapping()
            if match is None or not match.unique or match.mapping is None:
                self._submit_lock = False
                if self._series_active:
                    self._end_series()
                QMessageBox.warning(
                    self,
                    "Cannot start",
                    "Select the target class and worksheet before starting.",
                )
                return
            mapping = match.mapping
        if not self._conn.host.strip():
            self._submit_lock = False
            self._open_settings()
            if not self._conn.host.strip():
                if self._series_active:
                    self._end_series()
                return
            self._submit_lock = True
        try:
            target = self._target()
        except ValidationError as exc:
            self._submit_lock = False
            if self._series_active:
                self._end_series()
            QMessageBox.warning(self, "Connection", str(exc))
            self._open_settings()
            return
        if self._repeat_mode() != "off" and not self._series_active:
            self._series_active = True
            self._stop_after_current = False
            self._recordings_completed = 0
            self._files_found = 0
            mode = self._repeat_mode()
            if mode == "until":
                self._series_remaining = None
            else:
                self._series_remaining = max(int(self.repeat_spin.value()) - 1, 0)
            if hasattr(self, "stop_btn"):
                self.stop_btn.setEnabled(True)
        scan = payload["remote_recording_scans"][0]
        seconds = float(scan["duration"])
        self._capture_session_into_conn()
        save_settings(self._conn.persistable())
        self._launch_capture(self._plan, seconds, target=target, mapping=mapping, request=None)

    def _launch_capture(
        self,
        plan: FrequencyPlan,
        seconds: float,
        *,
        target,
        mapping,
        request: CaptureRequest | None,
    ) -> None:
        """Submit one recording (manual or programmatic) and arm the completion watch."""
        recording_format = self._recording_format()
        if request is not None and request.recording_format:
            recording_format = request.recording_format
        payload = build_payload_for_recording_time(
            plan,
            seconds,
            task_id=unique_task_id(),
            recording_format=recording_format,
        )
        scan = payload["remote_recording_scans"][0]
        self._last_recording_s = seconds
        self._sent_recording_format = str(scan.get("recording_format") or recording_format)
        self._ignore_late_http = False
        self._sensor_finished = False
        self._sensor_finished_at = 0.0
        self._stream_lost = False
        self._sensor_parts = []
        self._sensor_warnings = []
        self._pending_excel_entries = []
        self._summary_emitted = False
        try:
            self._expected_bytes = estimated_recording_bytes(int(scan["bandwidth"]), seconds)
        except Exception:
            self._expected_bytes = 0
        if request is not None:
            collection_event = request.collection_event
            target_class = request.target_class
            worksheet = request.worksheet
        else:
            collection_event = self.event_combo.currentText().strip() if hasattr(self, "event_combo") else ""
            target_class = mapping.target_class if mapping is not None else (
                self.class_combo.currentText().strip() if hasattr(self, "class_combo") else ""
            )
            worksheet = mapping.worksheet if mapping is not None else (
                self.sheet_combo.currentText().strip() if hasattr(self, "sheet_combo") else ""
            )
        record = new_recording(
            host=self._conn.host.strip(),
            start_hz=plan.start_hz,
            end_hz=plan.end_hz,
            center_hz=plan.center_hz,
            bandwidth_hz=plan.bandwidth_hz,
            duration_s=seconds,
            demo=self._conn.demo,
            collection_event=collection_event,
            target_class=target_class,
            worksheet=worksheet,
            sensor_id=self._sensor_info.serial if self._sensor_info.serial not in {"", "—"} else "",
        )
        add_recording(record)
        self._open_record_id = record.id
        if self._sftp_win is not None:
            self._sftp_win.set_recent_record_id(record.id)
            self._sftp_win.relabel_files()
        self._abort_record_stream()
        client = self._client()
        self._record_client = client
        self._record_cancel = threading.Event()
        cancel = self._record_cancel
        self._record_gen += 1
        gen = self._record_gen
        unit = self._freq_unit()
        prefix = f"{request.owner}: {request.label} — " if request is not None and request.label else (
            f"{request.owner}: " if request is not None else ""
        )
        self.log(
            f"{prefix}Start: {display_from_hz(int(scan['center_frequency']), unit)} {unit}, "
            f"{display_from_hz(int(scan['bandwidth']), unit)} {unit} BW, {seconds} s, "
            f"{scan['recording_format']}"
            + (" (demo)" if self._conn.demo else "")
        )
        self._set_phase(SUBMITTING)
        self._set_recording_ui(True, seconds=seconds)
        self._http_in_flight = True
        self._record_timer.start(int(max(seconds, 0.05) * 1000) + 400)
        emit_event = self.record_event.emit

        def on_event(event) -> None:
            emit_event((gen, event))

        def work() -> tuple[int, HttpOutcome]:
            try:
                # Hold the stream after "finished": the Node drops the mission
                # as soon as its client disconnects, and the last WAVE part can
                # still be flushing. _finish_current_capture() closes it.
                outcome = client.start_recording(
                    target,
                    self._conn.http_password,
                    payload,
                    cancel=cancel,
                    on_event=on_event,
                    stop_on_finished=False,
                )
            except TypeError as exc:
                # Stand-in clients in tests only accept the three positional arguments.
                if "argument" not in str(exc):
                    raise
                outcome = client.start_recording(target, self._conn.http_password, payload)
            return gen, outcome

        self._start_job(work, self._show_recording_outcome, record=True, record_gen=gen)

    def _on_recording_time_elapsed(self) -> None:
        if self._http_in_flight and self._record_client is not None and not self._conn.demo:
            # The sensor answers the POST with an event stream that reports
            # "Remote recording is now finished" a few seconds after the
            # nominal end. Keep following it; the file watch runs alongside.
            pass
        else:
            self._http_in_flight = False
            client = self._record_client
            self._record_client = None
            if client is not None:
                self._ignore_late_http = True
                client.close()
        if self._record_phase in {FINALIZING, CONFIRMED, UNKNOWN}:
            return
        self._set_phase(FINALIZING)
        self._elapsed_at = datetime.now()
        self._watch_state = FileWatchState()
        # Completion is always confirmed from the sensor's files, whether or not
        # Excel logging or a repeat series is on. (Before 1.8.10 a plain single
        # recording used a fixed three-attempt check that depended on the Sensor
        # Files window being open and ignored the sensor's own "finished"
        # message and the file names it reported, so it ended in "file not
        # confirmed" while the WAVE file was already on the sensor.)
        self._watching = True
        self._set_recording_ui(False)
        if self._conn.demo and self._open_record_id:
            record = next((item for item in load_recordings() if item.id == self._open_record_id), None)
            if record is not None:
                from dataclasses import replace

                from .recording_history import iq_group_key

                serial = "".join(ch if ch.isalnum() else "_" for ch in (record.sensor_id or "demo")) or "demo"
                stem = f"iq_{serial}_{record.started_at:%Y%m%d_%H%M%S}_{record.id[:8]}"
                parts = shared_demo_browser().publish_capture(
                    stem,
                    parts=max(int(self._demo_parts), 1),
                    when=datetime.now(),
                )
                if parts:
                    keyed = replace(record, iq_stem=iq_group_key(parts[0].name))
                    add_recording(keyed)
        self._set_msg(self._finalizing_text())
        self.log("Time elapsed. Looking for recording files.")
        if self._sftp_win is not None:
            self._sftp_win.retry_today()
        self._watch_timer.start()
        self._poll_collection_files()
        self._refresh_storage()

    def _finalizing_text(self) -> str:
        """Status line while a capture is being confirmed.

        Keeps two facts apart: whether the sensor said its task finished, and
        how many of its files have stopped growing on the sensor's disk.
        """
        found = len(self._watch_state.finalized)
        named = len(self._sensor_parts)
        if self._sensor_finished:
            if found and named and found < named:
                return f"Sensor finished. {found} of {named} file(s) confirmed; waiting for the rest…"
            if found:
                return f"Sensor finished. {found} file(s) confirmed; checking for more…"
            if named:
                return f"Sensor finished. Confirming {self._sensor_parts[-1]}…"
            return "Sensor finished. Looking for its file…"
        if found:
            return f"Recording file found ({found} part(s)). Waiting for more…"
        if named:
            return f"Time elapsed. Sensor is writing {self._sensor_parts[-1]}…"
        return "Time elapsed. Waiting for files…"

    def _refresh_finalizing_ui(self) -> None:
        """Re-render the status line after a sensor message changed what we know."""
        if self._record_phase == FINALIZING and self._watching:
            self._set_msg(self._finalizing_text())

    def _poll_collection_files(self) -> None:
        if not self._alive or self._record_phase not in {FINALIZING, UNKNOWN}:
            return
        record_id = self._open_record_id
        record = next((item for item in load_recordings() if item.id == record_id), None) if record_id else None
        if record is None:
            if self._watching:
                # Nothing to match against (no history entry): the watch cannot
                # confirm anything, so do not hold the Start button for it.
                self._watch_timer.stop()
                self._watching = False
                self._watch_token += 1
                self._set_phase(UNKNOWN)
                self._set_msg("Time elapsed. Check Sensor Files for the recording.", error=True)
                self.log("No history entry for this recording; its files cannot be confirmed automatically.", level="error")
                self._emit_capture_summary(OUTCOME_UNKNOWN, detail="No history entry; files not confirmed.")
                if self._series_active:
                    self._maybe_start_next()
                else:
                    self._set_recording_ui(False)
            return
        folders = folders_for_recording(record.started_at)
        if self._conn.demo:
            try:
                if self._watch_browser is None:
                    self._watch_browser = _open_browser(self._conn)
                entries = list_watch_entries(self._watch_browser, folders)
            except Exception as exc:
                self.log(f"Could not list sensor files: {exc}", level="error")
                return
            self._apply_watch_listing(record, entries)
            return
        if self._watch_in_flight:
            return
        self._watch_in_flight = True
        self._watch_token += 1
        token = self._watch_token
        state = self._conn
        if getattr(self, "_watch_cancel", None) is not None:
            self._watch_cancel.set()
        cancel = threading.Event()
        self._watch_cancel = cancel

        # One SFTP session serves the whole watch: a fresh SSH handshake per poll
        # cost ~2.5 s on the Node 40-8 and made the 2 s poll run every 4 s.
        browser = self._watch_browser

        def work() -> tuple[int, object, str, object]:
            session = browser
            if session is not None:
                try:
                    return token, list_watch_entries(session, folders), "", session
                except Exception:
                    # The sensor may have closed an idle session: reconnect silently.
                    try:
                        session.close()
                    except Exception:
                        pass
                    session = None
            try:
                session = _open_browser(state, cancel=cancel)
                return token, list_watch_entries(session, folders), "", session
            except Exception as exc:
                if session is not None:
                    try:
                        session.close()
                    except Exception:
                        pass
                return token, [], str(exc), None

        self._start_job(work, self._show_watch_listing)

    def _show_watch_listing(self, result: object) -> None:
        self._watch_in_flight = False
        if not self._alive or not isinstance(result, tuple) or len(result) not in (3, 4):
            return
        if len(result) == 4:
            token, entries, error, session = result
        else:
            token, entries, error = result
            session = None
        if token != self._watch_token:
            if session is not None and session is not self._watch_browser:
                try:
                    session.close()
                except Exception:
                    pass
            return
        if session is not None and session is not self._watch_browser:
            self._close_watch_browser()
            self._watch_browser = session
        elif session is None and not self._conn.demo:
            self._watch_browser = None
        if error:
            if self._record_phase in {FINALIZING, UNKNOWN}:
                self.log(f"Could not list sensor files: {error}", level="error")
            return
        record_id = self._open_record_id
        if not record_id:
            return
        record = next((item for item in load_recordings() if item.id == record_id), None)
        if record is None:
            return
        self._apply_watch_listing(record, list(entries or []))

    def _apply_watch_listing(self, record, entries) -> None:
        if self._sensor_finished and not self._sensor_parts and not self._stream_lost:
            # The sensor ran the task to its end and never named a file (e.g.
            # "No suitable antenna found"). Nothing in the folder is ours, so do
            # not adopt a neighbour's file by time proximity.
            mine: list = []
        else:
            mine = live_capture_files(
                entries,
                record,
                known=load_recordings(),
                sensor_parts=self._sensor_parts,
                host=self._conn.host,
            )
        if mine and not (record.iq_stem or "").strip():
            persist_matched_stems({entry.path: record for entry in mine})
        if self._sensor_parts:
            # The sensor has named its file: anything adopted earlier by time
            # proximity belongs to a neighbouring recording, not to this one.
            self._watch_state, dropped = prune_file_watch(self._watch_state, {entry.path for entry in mine})
            if dropped:
                names = {path.rsplit("/", 1)[-1] for path in dropped}
                before = len(self._pending_excel_entries)
                self._pending_excel_entries = [e for e in self._pending_excel_entries if e.path not in set(dropped)]
                self._files_found -= before - len(self._pending_excel_entries)
                self.log(f"Not part of this recording after all: {', '.join(sorted(names))}.")
                self._update_collection_status()
        if self._sftp_win is not None:
            self._sftp_win.refresh()
        self._watch_state, newly = update_file_watch(
            self._watch_state,
            mine,
            now=datetime.now(),
            stable_needed=self._stable_needed,
        )
        for entry in newly:
            self._files_found += 1
            # Excel rows are written once the capture is complete, so the row
            # carries the duration the files actually hold.
            self._pending_excel_entries.append(entry)
        if newly:
            self._update_collection_status()
            self._set_phase(FINALIZING)
            self._set_msg(self._finalizing_text())
            self.log(f"{len(newly)} IQ file(s) finished writing.")
        if (
            self._sensor_finished
            and not self._sensor_parts
            and not self._watch_state.sizes
            and self._sensor_finished_at
            and time.monotonic() - self._sensor_finished_at >= NO_FILE_GRACE_S
        ):
            # e.g. "No suitable antenna found": the sensor ran the task and
            # wrote nothing. No point watching the folder for a minute.
            reason = self._sensor_warnings[-1] if self._sensor_warnings else "no sensor message explains why"
            self.log(f"The sensor finished without writing a file ({reason}).", level="error")
            self._finish_current_capture()
            return
        if capture_is_settled(
            self._watch_state,
            now=datetime.now(),
            elapsed=True,
            settle_s=self._settle_s,
            elapsed_at=self._elapsed_at,
            sensor_finished=self._sensor_finished,
            stream_pending=self._http_in_flight and self._record_client is not None,
            expected_bytes=self._expected_bytes,
            announced_parts=len(self._sensor_parts),
        ):
            self._finish_current_capture()

    def _measured_capture(self, record) -> tuple[int, int, float | None]:
        """(total bytes, parts, seconds of IQ) held by the finalized files of this capture."""
        state = self._watch_state
        sizes = [size for path, size in state.sizes.items() if path in state.finalized]
        total = sum(sizes)
        parts = len(sizes)
        if not parts or record is None or self._conn.demo:
            # Demo files have made-up sizes; the bytes-to-seconds model is for real Node WAVE files.
            return total, parts, None
        try:
            return total, parts, recorded_seconds_from_bytes(total, int(record.bandwidth_hz), parts)
        except Exception:
            return total, parts, None

    def _log_pending_iq_files(self, record, *, duration_s: float | None) -> None:
        entries = list(self._pending_excel_entries)
        self._pending_excel_entries = []
        for entry in entries:
            try:
                self._log_iq_file(record, entry, duration_s=duration_s)
            except Exception as exc:
                self.log(f"Excel log failed for {entry.name}: {exc}", level="error")
        if entries and hasattr(self, "excel_check") and self.excel_check.isChecked():
            self.log(f"Logged {len(entries)} IQ file(s) to Excel.")

    def _log_iq_file(self, record, entry, *, duration_s: float | None = None) -> None:
        self._update_collection_status()
        if self._external_capture is not None and not self._external_capture.excel:
            # The feature that requested this capture writes its own rows.
            return
        if not hasattr(self, "excel_check") or not self.excel_check.isChecked():
            return
        sensor_id = record.sensor_id or self._sensor_info.serial
        if self._excel_store.is_logged(sensor_id, entry.path):
            return
        workbook = Path(self.workbook_edit.text().strip())
        if not workbook.is_file() or self._catalog is None:
            return
        mapping = None
        for item in self._catalog.mappings:
            if item.target_class == record.target_class and item.worksheet == record.worksheet:
                mapping = item
                break
        if mapping is None and self._catalog.mappings:
            mapping = self._catalog.mappings[0]
        if mapping is None:
            return
        local_dir = resolved_download_dir(self._conn.download_dir)
        local = local_dir / entry.name
        row = ExcelRow(
            worksheet=record.worksheet or mapping.worksheet,
            collection_event=record.collection_event or self.event_combo.currentText().strip(),
            target_class=record.target_class or mapping.target_class,
            start_hz=record.start_hz,
            end_hz=record.end_hz,
            start_style=mapping.start_style,
            stop_style=mapping.stop_style,
            sensor=workbook_sensor_name(
                self._sensor_info.model,
                list(self._catalog.sensor_names),
            ),
            filename=entry.name,
            local_path=str(local) if local.is_file() else "",
            remote_path=entry.path,
            sensor_id=sensor_id,
            duration_s=record.duration_s if duration_s is None else duration_s,
            record_id=record.id,
            group_key=recording_group_key(
                sensor_id=sensor_id,
                directory=remote_directory(entry.path),
                stem=iq_group_key(entry.name),
                record_id=record.id,
            ),
        )
        item = pending_from_row(workbook=workbook, row=row)
        if self._excel_store.enqueue(item):
            self._flush_excel()

    def _finish_current_capture(self) -> None:
        if self._record_phase not in {FINALIZING, UNKNOWN}:
            return
        self._watch_timer.stop()
        self._watching = False
        # A folder listing still running on the worker must not re-run this
        # once it lands: it belongs to the capture that has just been closed.
        self._watch_token += 1
        self._abort_record_stream()
        record = None
        if self._open_record_id:
            record = next((item for item in load_recordings() if item.id == self._open_record_id), None)
        total, parts, held_s = self._measured_capture(record)
        excel_duration: float | None = None
        named = len(self._sensor_parts)
        if parts:
            self._set_phase(CONFIRMED)
            self._set_msg(f"Recording finished ({parts} file(s)).")
            source = "sensor confirmed" if self._sensor_finished else "files stable"
            self.log(f"Capture finished with {parts} file(s), {total / 1_000_000:,.1f} MB ({source}).")
            if named and parts < named:
                missing = [name for name in self._sensor_parts if name not in {
                    path.rsplit("/", 1)[-1] for path in self._watch_state.finalized
                }]
                self.log(
                    f"The sensor named {named} file(s) but {parts} finished writing; "
                    f"not found or still growing: {', '.join(missing) or 'unknown'}. Check Sensor Files.",
                    level="error",
                )
                self._set_msg(f"Recording finished ({parts} of {named} file(s) found).", error=True)
            requested = float(record.duration_s) if record is not None else 0.0
            if held_s is not None and requested > 0:
                if held_s < 0.95 * requested:
                    excel_duration = round(held_s, 1)
                    if self._stream_lost:
                        why = (
                            "The connection to the sensor dropped during the capture, and this sensor stops "
                            "recording when its client disconnects."
                        )
                    elif self._sensor_finished:
                        why = "The sensor reported it finished early; at 62.5 MS/s it cannot sustain long captures."
                    else:
                        why = "The sensor stopped early."
                    self.log(
                        f"The files hold about {held_s:.1f} s of IQ, not the {requested:g} s requested. "
                        f"{why} The Excel row gets the measured time.",
                        level="error",
                    )
                    self._set_msg(f"Recording finished short: {held_s:.1f} s of {requested:g} s.", error=True)
                elif held_s > 1.5 * requested:
                    self.log(
                        f"The files hold about {held_s:.1f} s of IQ for a {requested:g} s request; "
                        "check that the sample-rate model fits this sensor.",
                        level="error",
                    )
        else:
            # Task completion and file availability are reported separately:
            # the sensor may have finished its task without a usable file, or we
            # may simply not have found the file it named within the watch time.
            self._set_phase(UNKNOWN)
            if self._sensor_finished and named:
                names = ", ".join(self._sensor_parts)
                self._set_msg(f"Sensor finished, but {names} was not found on the sensor.", error=True)
                self.log(
                    f"File not confirmed: the sensor reported its task finished and named {names}, "
                    "but no such file settled in the recording folder. Check Sensor Files.",
                    level="error",
                )
            elif self._sensor_finished:
                self._set_msg("Sensor finished, but it wrote no file.", error=True)
                self.log("File not confirmed: the sensor finished its task without naming a file.", level="error")
            elif self._stream_lost:
                self._set_msg("Connection to the sensor dropped. No recording file found.", error=True)
                self.log("File not confirmed: the sensor connection dropped and no file appeared.", level="error")
            else:
                self._set_msg("Time elapsed. No matching recording file yet.", error=True)
                self.log("File not confirmed: no file for this recording appeared within the watch time.", level="error")
        if record is not None:
            self._log_pending_iq_files(record, duration_s=excel_duration)
        else:
            self._pending_excel_entries = []
        self._refresh_storage()
        if parts:
            self._recordings_completed += 1
        self._update_collection_status()
        self._emit_capture_summary(OUTCOME_CONFIRMED if parts else OUTCOME_UNKNOWN, record=record)
        if self._series_active:
            self._maybe_start_next()
        else:
            self._set_recording_ui(False)

    def _end_series(self) -> None:
        self._cancel_wait()
        self._series_active = False
        self._series_remaining = 0
        self._stop_after_current = False
        self._watching = False
        self._watch_timer.stop()
        self._watch_token += 1
        if getattr(self, "_watch_cancel", None) is not None:
            self._watch_cancel.set()
        self._close_watch_browser()
        if hasattr(self, "stop_btn"):
            self.stop_btn.setEnabled(False)
        if not self._recording_active:
            self.start_btn.setEnabled(True)
            self.start_btn.setText("Start Recording")
        self._update_collection_status()

    def _repeat_wait_s(self) -> float:
        if not hasattr(self, "wait_spin"):
            return 0.0
        value = float(self.wait_spin.value())
        if value < 0:
            value = 0.0
        if hasattr(self, "wait_unit") and self.wait_unit.currentText() == "Minutes":
            return value * 60.0
        return value

    def _format_countdown(self, seconds: float) -> str:
        total = max(0, int(seconds + 0.999) if seconds > 0 else 0)
        return f"{total // 60:02d}:{total % 60:02d}"

    def _begin_wait(self, seconds: float) -> None:
        self._waiting = True
        self._wait_deadline = time.monotonic() + max(seconds, 0.0)
        self._set_phase(WAITING)
        if hasattr(self, "stop_btn"):
            self.stop_btn.setEnabled(True)
        self.start_btn.setEnabled(False)
        self._tick_wait()
        self._wait_timer.start()

    def _cancel_wait(self, message: str = "") -> None:
        self._wait_timer.stop()
        was_waiting = self._waiting
        self._waiting = False
        self._wait_deadline = 0.0
        if was_waiting and self._record_phase == WAITING:
            # The countdown is gone; show the state of the last capture again.
            self._set_phase(CONFIRMED if self._watch_state.finalized else IDLE)
        if message and was_waiting:
            self.log(message)
            self._set_msg(message)

    def _tick_wait(self) -> None:
        if not self._waiting:
            self._wait_timer.stop()
            return
        left = self._wait_deadline - time.monotonic()
        if left <= 0:
            self._wait_timer.stop()
            self._waiting = False
            self._start_next_now()
            return
        text = f"Next recording in {self._format_countdown(left)}"
        self._set_phase(WAITING, text)
        self._set_msg(text)
        self.start_btn.setText(text)

    def _start_next_now(self) -> None:
        if not self._series_active or self._stop_after_current:
            self._end_series()
            return
        if self._series_remaining is None:
            QTimer.singleShot(0, self._on_start)
            return
        if self._series_remaining > 0:
            self._series_remaining -= 1
            QTimer.singleShot(0, self._on_start)
            return
        self.log("Series complete.")
        self._end_series()

    def _maybe_start_next(self) -> None:
        if not self._series_active:
            return
        if self._stop_after_current:
            self.log("Series stopped after the current recording.")
            self._end_series()
            return
        if self._series_remaining is not None and self._series_remaining <= 0:
            self.log("Series complete.")
            self._end_series()
            return
        wait_s = self._repeat_wait_s()
        if wait_s <= 0:
            self._start_next_now()
            return
        self._begin_wait(wait_s)

    def _show_test_outcome(self, outcome: Any) -> None:
        self._show_http_outcome(outcome, recording=False)

    def _show_recording_outcome(self, outcome: Any) -> None:
        gen = None
        if isinstance(outcome, tuple) and len(outcome) == 2 and isinstance(outcome[0], int):
            gen, outcome = outcome
            if gen != self._record_gen:
                return
        self._http_in_flight = False
        if not self._recording_active:
            self._record_client = None
        if not isinstance(outcome, HttpOutcome):
            self.log(str(outcome), level="error")
            self._record_timer.stop()
            self._set_recording_ui(False)
            self._set_phase(IDLE)
            self._end_series()
            self._emit_capture_summary(OUTCOME_FAILED, detail=str(outcome))
            return
        if outcome.stream_ended:
            self._show_stream_outcome(outcome)
            return
        if self._ignore_late_http and not self._recording_active:
            self._ignore_late_http = False
            if outcome.kind == "accepted":
                self.log("Sensor HTTP reply received.")
            return
        if not outcome.request_sent and not outcome.accepted:
            self._record_timer.stop()
            self._ignore_late_http = False
            self._set_recording_ui(False)
            self._set_phase(IDLE)
            if self._open_record_id:
                remove_recording(self._open_record_id)
                self._open_record_id = None
            self._end_series()
            self._set_msg(outcome.detail, error=True)
            self._show_http_outcome(outcome, recording=True)
            self._emit_capture_summary(OUTCOME_NOT_SENT, detail=outcome.detail)
            return
        if self._ignore_late_http and outcome.kind in (
            "connection_failure",
            "timeout",
            "timeout_after_send",
        ):
            self._ignore_late_http = False
            self.log("Stopped waiting for HTTP. Use Sensor Files if needed.")
            return
        self._show_http_outcome(outcome, recording=True)

    def _show_stream_outcome(self, outcome: HttpOutcome) -> None:
        """The EMP event stream ended (sensor finished, error, budget, or we stopped it)."""
        self._ignore_late_http = False
        self._abort_record_stream()
        if outcome.kind == "sensor_error":
            self.log(outcome.detail, level="error")
            self._record_timer.stop()
            self._watch_timer.stop()
            self._watching = False
            self._watch_token += 1
            self._set_recording_ui(False)
            self._set_phase(UNKNOWN)
            self._set_msg("The sensor reported an error. Check Sensor Files.", error=True)
            self._end_series()
            self._emit_capture_summary(OUTCOME_SENSOR_ERROR, detail=outcome.detail)
            return
        if outcome.sensor_finished:
            for name in outcome.sensor_parts:
                self._note_sensor_part(outcome.sensor_file_dir, name)
            self._mark_sensor_finished()
            return
        if outcome.stream_ended == "cancelled":
            return
        if outcome.stream_ended.startswith("lost"):
            self._stream_lost = True
            self.log(
                "Connection to the sensor dropped while waiting for its completion message. "
                "This sensor stops recording when its client disconnects; completion is confirmed from the files.",
                level="error",
            )
        elif outcome.stream_ended == "budget":
            self.log("No completion message from the sensor within the time budget. Checking files.")
        else:
            self.log("The sensor closed its event stream before reporting completion. Checking files.")
        if self._recording_active:
            # The recording time has not elapsed yet; the timer still drives the next phase.
            return

    def _on_record_event(self, payload: object) -> None:
        """Live event from the sensor's EMP stream (queued onto the UI thread)."""
        if not self._alive or not isinstance(payload, tuple) or len(payload) != 2:
            return
        gen, event = payload
        if gen != self._record_gen:
            return
        from .api_client import (
            SSE_EVENT_HTTP_ACCEPTED,
            sse_message_is_error,
            sse_message_is_finished,
            sse_message_is_noise,
            sse_part_filename,
        )

        name = getattr(event, "event", "")
        if name == SSE_EVENT_HTTP_ACCEPTED:
            status = getattr(event, "data", {}).get("status") if isinstance(getattr(event, "data", None), dict) else None
            self.log(f"Request accepted (HTTP {status}). Recording…" if status else "Request accepted. Recording…")
            if self._sftp_win is not None:
                self._sftp_win.retry_today()
            return
        directory, filename = sse_part_filename(event)
        if filename:
            self._note_sensor_part(directory, filename)
            return
        data = getattr(event, "data", None)
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict) or sse_message_is_noise(item):
                continue
            text = str(item.get("message", "")).strip()
            if sse_message_is_finished(item):
                self._mark_sensor_finished()
            elif sse_message_is_error(item):
                self.log(f"Sensor: {text}", level="error")
            elif text:
                level = "error" if str(item.get("level", "")).upper() == "WARNING" else "info"
                self._sensor_warnings.append(text)
                self.log(f"Sensor: {text}", level=level)

    def _mark_sensor_finished(self) -> None:
        """The sensor said its recording task is over (event stream or final outcome).

        This is task completion only. The files are confirmed by the watch,
        which is nudged here so the status does not wait for the next 2 s tick.
        """
        if not self._sensor_finished:
            self._sensor_finished = True
            self._sensor_finished_at = time.monotonic()
            if self._sensor_parts:
                self.log("Sensor reported: recording finished.")
            else:
                self.log("Sensor reported: recording finished, but it named no file.", level="error")
        self._refresh_finalizing_ui()
        if self._record_phase == FINALIZING and self._watching:
            self._poll_collection_files()

    def _note_sensor_part(self, directory: str, filename: str) -> None:
        """The sensor named a file it is writing for the current task."""
        if filename in self._sensor_parts:
            return
        self._sensor_parts.append(filename)
        self.log(f"Sensor is writing {filename}" + (f" in {directory}" if directory else ""))
        self._refresh_finalizing_ui()
        record_id = self._open_record_id
        if not record_id:
            return
        from .recording_history import iq_group_key, set_recording_stem

        try:
            set_recording_stem(record_id, iq_group_key(filename))
        except Exception as exc:  # noqa: BLE001 - history is best effort
            self.log(f"Could not save the file name for this recording: {exc}", level="error")

    def _show_http_outcome(self, outcome: Any, *, recording: bool) -> None:
        if not isinstance(outcome, HttpOutcome):
            self.log(str(outcome), level="error")
            return
        if outcome.kind == "accepted":
            if self._sftp_win is not None:
                self._sftp_win.retry_today()
            if self._recording_active:
                self.log("Request accepted. Recording…")
            else:
                self.log("Sensor HTTP reply received.")
        elif outcome.kind == "reachable":
            self.log(outcome.detail)
        elif outcome.outcome_unknown:
            self.log("Request sent; no reply in time. Check Sensor Files before starting again.")
        else:
            self.log(outcome.detail, level="error")
        if outcome.status_code is not None:
            self.log(f"HTTP {outcome.status_code} from {outcome.url}")
        if outcome.body_text and outcome.kind not in ("accepted", "reachable"):
            snippet = outcome.body_text.strip().replace("\n", " ")
            if snippet:
                self.log(snippet[:400])

    def _open_sftp(self) -> None:
        if not self._conn.host.strip() and not self._conn.demo:
            self._open_settings()
            if not self._conn.host.strip():
                return
        from .sftp_paths import today_remdata_directory

        today = today_remdata_directory()
        self.log(f"Sensor files: {self._conn.host or 'demo'} {today}")
        try:
            if self._sftp_win is not None:
                self._sftp_win.close()
            self._sftp_win = SftpWindow(self._conn, None)
            self._sftp_win.activity.connect(self.log)
            self._sftp_win.remote_changed.connect(self._refresh_storage)
            self._sftp_win.set_merge_controller(self._merge)
            self._sftp_win.set_recent_record_id(self._open_record_id)
            self._sftp_win.show()
            self._sftp_win.raise_()
        except Exception:
            self.log(traceback.format_exc(limit=6), level="error")
            QMessageBox.warning(self, "SFTP", "Could not open Sensor Files. See the log.")

    # ------------------------------------------------------------------ merge recording parts
    def _live_recording_stem(self) -> str | None:
        """Stem of the recording still being captured or confirmed, if any."""
        if self._record_phase not in {SUBMITTING, RECORDING, FINALIZING}:
            return None
        if self._sensor_parts:
            from .recording_history import iq_group_key

            return iq_group_key(self._sensor_parts[0])
        if self._open_record_id:
            record = next((item for item in load_recordings() if item.id == self._open_record_id), None)
            if record is not None and (record.iq_stem or "").strip():
                return record.iq_stem.strip()
        return None

    def _on_merge_toggled(self, enabled: bool) -> None:
        self._conn.merge_parts = bool(enabled)
        save_settings(self._conn.persistable())
        self._merge.set_enabled(bool(enabled))
        if not enabled and self._merge.is_busy():
            self._set_merge_status(
                f"{self._merge.status_text}  (Merging is off for new recordings; use Cancel merge to stop this one.)",
                error=False,
            )

    def _cancel_merge(self) -> None:
        self._merge.cancel()

    def _on_merge_log(self, message: str, level: str) -> None:
        self.log(message, level=level if level in {"info", "error"} else "info")

    def _set_merge_status(self, text: str, *, error: bool) -> None:
        if not hasattr(self, "merge_status"):
            return
        self.merge_status.setText(text)
        self.merge_status.setVisible(bool(text))
        self.merge_status.setObjectName("error" if error and text else "muted")
        restyle(self.merge_status)

    def _on_merge_status(self, text: str, error: bool) -> None:
        self._set_merge_status(text, error=error)

    def _on_merge_progress(self, percent: int) -> None:
        if not hasattr(self, "merge_progress"):
            return
        if percent < 0:
            self.merge_progress.setVisible(False)
            return
        self.merge_progress.setVisible(True)
        self.merge_progress.setValue(max(0, min(100, int(percent))))

    def _on_merge_busy(self, busy: bool) -> None:
        if hasattr(self, "merge_cancel_btn"):
            self.merge_cancel_btn.setVisible(bool(busy))
        if not busy and hasattr(self, "merge_progress"):
            self.merge_progress.setVisible(False)


def _install_exception_hook() -> None:
    def _write(text: str) -> None:
        try:
            path = user_config_dir() / "crfs_iq_recorder_error.log"
            path.write_text(redact_text(text), encoding="utf-8")
        except Exception:
            pass

    def hook(exc_type, exc, tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        _write(text)
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def run(*, demo: bool = False) -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    try:
        QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass
    set_app_user_model_id()
    _install_exception_hook()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    saved = load_settings()
    configure_appearance(app, saved.get("ui_theme") if isinstance(saved, dict) else None)
    icon = app_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)
    window = MainWindow(demo=demo)
    try:
        folder = ensure_download_dir(window._conn.download_dir)
        window._conn.download_dir = str(folder)
    except OSError as exc:
        window.log(f"Could not create download folder: {exc}", level="error")
    window.show()
    rc = app.exec()
    # Worker threads must be finished before Python deletes their QThread
    # objects, otherwise Qt aborts the process with 0xC0000409 on every exit
    # that still has a sensor check or SFTP connect in flight.
    leaked = stop_worker_threads(timeout_s=8.0)
    app.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    if leaked:
        # A worker is still blocked (for example a sensor that stopped
        # answering mid-request). Settings are already saved; skip interpreter
        # teardown so the blocked thread is never deleted from under Qt.
        for stream in (sys.stdout, sys.stderr):
            try:
                if stream is not None:
                    stream.flush()
            except Exception:  # noqa: BLE001
                pass
        os._exit(rc)
    return rc
