"""Simple CRFS IQ Recorder UI."""

from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QSize, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QIcon, QPixmap, QTextCursor
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
from .class_match import suggest_class
from .collection_catalog import preferred_event, load_workbook_catalog
from .connection_dialog import ConnectionDialog
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
    FileWatchState,
    capture_is_settled,
    folders_for_recording,
    list_watch_entries,
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
    load_recordings,
    match_recordings_to_entries,
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
from .sensor_info import SensorInfo, blank_info, checking_info
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
from .size_estimate import SizeEstimateError, empirical_size_mb, format_estimate
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


class MainWindow(QMainWindow):
    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        icon = _icon_path()
        if icon:
            self.setWindowIcon(QIcon(str(icon)))

        self._demo_forced = demo
        self._jobs: list[tuple[QThread, Worker]] = []
        self._alive = True
        self._sensor_info = blank_info(demo=demo)
        self._info_token = 0
        self._http_in_flight = False
        self._recording_active = False
        self._submit_lock = False
        self._ignore_late_http = False
        self._record_gen = 0
        self._record_phase = IDLE
        self._confirm_attempts = 0
        self._last_match_sizes: dict[str, int] = {}
        self._record_client: EmpClient | None = None
        self._sftp_win: SftpWindow | None = None
        self._last_recording_s: float | None = None
        self._open_record_id: str | None = None
        self._log_collapsed = False
        self._log_sizes = [680, 300]
        self._catalog = None
        self._excel_store = ExcelLogStore()
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
        self._conn = ConnectionState.from_settings(load_settings(), demo=demo)
        self._plan = self._conn.frequency_plan()

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
        self._restore_session_widgets()
        self._apply_mode_fields()
        self._refresh_size()
        self._sync_connection_label()
        if self._conn.excel_workbook:
            self._load_workbook(self._conn.excel_workbook, quiet=True)
        self._flush_excel()
        self._update_collection_status()
        if current_mode() != self._conn.ui_theme:
            app = QApplication.instance()
            if app is not None:
                apply_theme(app, self._conn.ui_theme)
            polish_combo(self.unit_combo)
            polish_combo(self.format_combo)
        self.log("Ready.")
        self._refresh_sensor_info()
        self._refresh_storage()
        self._sync_theme_button()
        self.statusBar().setSizeGripEnabled(True)
        fit_window_to_screen(self, 1100, 720, min_width=640, min_height=420)

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
        self._record_timer.stop()
        self._watch_timer.stop()
        self._wait_timer.stop()
        self._excel_timer.stop()
        self._close_watch_browser()
        if self._record_client is not None:
            self._record_client.close()
        for thread, _worker in list(self._jobs):
            thread.quit()
            thread.wait(2000)
        if self._sftp_win:
            self._sftp_win.close()
        super().closeEvent(event)

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

    def _uses_collection_watch(self) -> bool:
        excel = hasattr(self, "excel_check") and self.excel_check.isChecked()
        return excel or self._repeat_mode() != "off"

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
        return True

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
        saved, pending = flush_excel_queue(self._excel_store)
        if saved:
            self.log(f"Excel: saved {saved} row(s).")
        if pending:
            self.log(f"Pending Excel updates: {pending}.")
        self._update_collection_status()

    def _close_watch_browser(self) -> None:
        browser = self._watch_browser
        self._watch_browser = None
        if browser is None or self._conn.demo:
            return
        try:
            browser.close()
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
        self.status_value.setText(info.status)
        lowered = info.status.casefold()
        if info.connected or lowered in {"connected", "online"}:
            self._set_status_style("ok")
        elif lowered in {"checking…", "checking...", "demo", "no sensor ip"}:
            self._set_status_style("wait")
        elif not self._conn.host.strip():
            self._set_status_style("idle")
        else:
            self._set_status_style("bad")
        if info.connected or lowered in {"connected", "online", "demo"}:
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

        self._storage_token += 1
        token = self._storage_token
        state = self._conn

        def work() -> tuple[int, StorageSnapshot]:
            browser = None
            try:
                browser = _open_browser(state)
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
                return
        elif isinstance(result, StorageSnapshot):
            snap = result
        else:
            snap = StorageSnapshot.unavailable(str(result))
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
        self._apply_sensor_info(checking_info(host))
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
            mb = empirical_size_mb(scan["bandwidth"], scan["duration"])
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
            self.statusBar().showMessage(f"Recording {label}")
            return
        hold = self._series_active or self._watching or self._waiting
        self.start_btn.setEnabled(not hold)
        self.start_btn.setText("Recording series…" if hold else "Start Recording")
        if hasattr(self, "stop_btn"):
            self.stop_btn.setEnabled((self._series_active or self._waiting) and not self._stop_after_current)

    def _start_job(
        self,
        fn: Callable[[], Any],
        on_ok: Callable[[Any], None],
        *,
        record: bool = False,
        info: bool = False,
        record_gen: int | None = None,
        info_token: int | None = None,
    ) -> None:
        thread = QThread()
        worker = Worker(fn)
        worker._on_ok = on_ok  # noqa: SLF001
        worker._is_record = record  # noqa: SLF001
        worker._is_info = info  # noqa: SLF001
        worker._record_gen = record_gen  # noqa: SLF001
        worker._info_token = info_token  # noqa: SLF001
        worker.moveToThread(thread)
        queued = Qt.ConnectionType.QueuedConnection
        worker.finished.connect(self._job_finished, queued)
        worker.failed.connect(self._job_failed, queued)
        thread.finished.connect(self._job_thread_finished, queued)
        thread.started.connect(worker.run)
        self._jobs.append((thread, worker))
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
                return
            if self._ignore_late_http:
                self._ignore_late_http = False
                return
            self.log(text, level="error")
            return
        if getattr(worker, "_is_info", False) and self._alive:
            token = getattr(worker, "_info_token", None)
            if token is not None and token != self._info_token:
                return
            self._apply_sensor_info(blank_info(host=self._conn.host, status="Unavailable"))
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

    def _on_start(self) -> None:
        if self._recording_active or self._submit_lock:
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
        plan = self._plan
        payload = build_payload_for_recording_time(
            plan,
            seconds,
            task_id=unique_task_id(),
            recording_format=self._recording_format(),
        )
        scan = payload["remote_recording_scans"][0]
        self._last_recording_s = seconds
        self._capture_session_into_conn()
        save_settings(self._conn.persistable())
        self._ignore_late_http = False
        self._last_match_sizes = {}
        self._confirm_attempts = 0
        record = new_recording(
            host=self._conn.host.strip(),
            start_hz=plan.start_hz,
            end_hz=plan.end_hz,
            center_hz=plan.center_hz,
            bandwidth_hz=plan.bandwidth_hz,
            duration_s=seconds,
            demo=self._conn.demo,
            collection_event=self.event_combo.currentText().strip() if hasattr(self, "event_combo") else "",
            target_class=mapping.target_class if mapping is not None else (
                self.class_combo.currentText().strip() if hasattr(self, "class_combo") else ""
            ),
            worksheet=mapping.worksheet if mapping is not None else (
                self.sheet_combo.currentText().strip() if hasattr(self, "sheet_combo") else ""
            ),
            sensor_id=self._sensor_info.serial if self._sensor_info.serial not in {"", "—"} else "",
        )
        add_recording(record)
        self._open_record_id = record.id
        if self._sftp_win is not None:
            self._sftp_win.set_recent_record_id(record.id)
            self._sftp_win.relabel_files()
        client = self._client()
        self._record_client = client
        self._record_gen += 1
        gen = self._record_gen
        unit = self._freq_unit()
        self.log(
            f"Start: {display_from_hz(int(scan['center_frequency']), unit)} {unit}, "
            f"{display_from_hz(int(scan['bandwidth']), unit)} {unit} BW, {seconds} s, "
            f"{scan['recording_format']}"
            + (" (demo)" if self._conn.demo else "")
        )
        self._set_phase(SUBMITTING)
        self._set_recording_ui(True, seconds=seconds)
        self._http_in_flight = True
        self._record_timer.start(int(max(seconds, 0.05) * 1000) + 400)

        def work() -> tuple[int, HttpOutcome]:
            return gen, client.start_recording(target, self._conn.http_password, payload)

        self._start_job(work, self._show_recording_outcome, record=True, record_gen=gen)

    def _on_recording_time_elapsed(self) -> None:
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
        if self._uses_collection_watch():
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
        if self._sftp_win is None and not self._uses_collection_watch():
            self._set_msg("Time elapsed. Open Sensor Files to check the recording.")
            self.statusBar().showMessage("Time elapsed")
            self.log("Time elapsed. Open Sensor Files to confirm the file.")
        else:
            self._set_msg("Time elapsed. Waiting for files…")
            self.statusBar().showMessage("Waiting for files")
            self.log("Time elapsed. Looking for recording files.")
            if self._sftp_win is not None:
                self._sftp_win.retry_today()
        if self._uses_collection_watch():
            self._watch_timer.start()
            self._poll_collection_files()
        else:
            self._schedule_sftp_part_refresh()
            self._schedule_file_confirm()
        self._refresh_storage()

    def _refresh_open_sftp(self) -> None:
        if not self._alive or self._sftp_win is None:
            return
        self._sftp_win.refresh()

    def _schedule_sftp_part_refresh(self) -> None:
        for delay_ms in (3000, 12000):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._refresh_open_sftp)
            timer.start(delay_ms)

    def _schedule_file_confirm(self) -> None:
        self._confirm_attempts = 0
        for delay_ms in (2000, 6000, 14000):
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._try_confirm_files)
            timer.start(delay_ms)

    def _try_confirm_files(self) -> None:
        if not self._alive or self._record_phase not in {FINALIZING, UNKNOWN}:
            return
        self._confirm_attempts += 1
        record_id = self._open_record_id
        if not record_id:
            return
        win = self._sftp_win
        if win is None:
            if self._confirm_attempts >= 3:
                self._set_phase(UNKNOWN)
                self._set_msg("Time elapsed. Open Sensor Files to check the recording.")
            return
        entries = win.matching_entries(record_id)
        if not entries:
            if self._confirm_attempts >= 3:
                self._set_phase(UNKNOWN)
                self._set_msg("Time elapsed. No matching recording file yet.")
                self.log("File not confirmed.")
            return
        sizes = {entry.path: int(entry.size or 0) for entry in entries}
        if sizes == self._last_match_sizes and self._last_match_sizes:
            self._set_phase(CONFIRMED)
            self._set_msg(f"Recording file found ({len(entries)} part(s)).")
            self.statusBar().showMessage("Files confirmed")
            self.log(f"Confirmed {len(entries)} recording part(s).")
            return
        self._last_match_sizes = sizes
        self._set_phase(FINALIZING)
        self._set_msg(f"Recording file appeared ({len(entries)} part(s)). Checking size…")

    def _poll_collection_files(self) -> None:
        if not self._alive or self._record_phase not in {FINALIZING, UNKNOWN}:
            return
        record_id = self._open_record_id
        if not record_id:
            return
        record = next((item for item in load_recordings() if item.id == record_id), None)
        if record is None:
            return
        folders = folders_for_recording(record.started_at)
        try:
            if self._watch_browser is None:
                self._watch_browser = _open_browser(self._conn)
            entries = list_watch_entries(self._watch_browser, folders)
        except Exception as exc:
            self.log(f"Could not list sensor files: {exc}", level="error")
            return
        matched = match_recordings_to_entries(entries, [record], host=self._conn.host)
        persist_matched_stems(matched)
        mine = [entry for entry in entries if matched.get(entry.path) and matched[entry.path].id == record_id]
        if self._sftp_win is not None:
            self._sftp_win.refresh()
        self._watch_state, newly = update_file_watch(
            self._watch_state,
            mine,
            now=datetime.now(),
            stable_needed=self._stable_needed,
        )
        for entry in newly:
            try:
                self._log_iq_file(record, entry)
            except Exception as exc:
                self.log(f"Excel log failed for {entry.name}: {exc}", level="error")
        if newly:
            self._set_phase(FINALIZING)
            self._set_msg(f"Recording file found ({len(self._watch_state.finalized)} part(s)). Waiting for more…")
            self.log(f"Logged {len(newly)} finalized IQ file(s).")
        if capture_is_settled(
            self._watch_state,
            now=datetime.now(),
            elapsed=True,
            settle_s=self._settle_s,
            elapsed_at=self._elapsed_at,
        ):
            self._finish_current_capture()

    def _log_iq_file(self, record, entry) -> None:
        self._files_found += 1
        self._update_collection_status()
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
            duration_s=record.duration_s,
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
        parts = len(self._watch_state.finalized)
        if parts:
            self._set_phase(CONFIRMED)
            self._set_msg(f"Recording finished ({parts} file(s)).")
            self.log(f"Capture finished with {parts} file(s).")
        else:
            self._set_phase(UNKNOWN)
            self._set_msg("Time elapsed. No matching recording file yet.")
            self.log("File not confirmed.")
        self._refresh_storage()
        self._recordings_completed += 1
        self._update_collection_status()
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
        if not (recording and self._recording_active):
            self.statusBar().showMessage(outcome.title)

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
            self._sftp_win.set_recent_record_id(self._open_record_id)
            self._sftp_win.show()
            self._sftp_win.raise_()
        except Exception:
            self.log(traceback.format_exc(limit=6), level="error")
            QMessageBox.warning(self, "SFTP", "Could not open Sensor Files. See the log.")


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
    icon = _icon_path()
    if icon:
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow(demo=demo)
    try:
        folder = ensure_download_dir(window._conn.download_dir)
        window._conn.download_dir = str(folder)
    except OSError as exc:
        window.log(f"Could not create download folder: {exc}", level="error")
    window.show()
    return app.exec()
