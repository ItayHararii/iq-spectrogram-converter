"""Simple CRFS IQ Recorder UI."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QSize, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QGuiApplication, QIcon, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
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
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from . import APP_NAME, __version__
from .api_client import EmpClient, HttpOutcome
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
from .frequency import (
    FrequencyError,
    FrequencyPlan,
    display_from_hz,
    from_center_bandwidth,
    from_start_end,
    parse_to_integer_hz,
)
from .paths import ensure_download_dir, load_settings, save_settings, user_config_dir
from .recording_history import add_recording, new_recording, remove_recording
from .recording_status import (
    CONFIRMED,
    FINALIZING,
    IDLE,
    PHASE_LABELS,
    RECORDING,
    SUBMITTING,
    UNKNOWN,
)
from .request_builder import RequestBuildError, build_payload_for_recording_time, unique_task_id
from .sanitizer import redact_text
from .sensor_info import SensorInfo, blank_info, checking_info
from .size_estimate import SizeEstimateError, empirical_size_mb, format_estimate
from .sftp_window import SftpWindow
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


def _icon_path() -> Path | None:
    from .paths import resource_dir

    names = ("sensorz_icon.ico", "sensorz_icon.png")
    roots = (
        resource_dir() / "assets",
        Path(__file__).resolve().parents[1] / "assets",
        Path(__file__).resolve().parents[2] / "assets",
    )
    for root in roots:
        for name in names:
            path = root / name
            if path.is_file():
                return path
    return None


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
        self._record_timer = QTimer(self)
        self._record_timer.setSingleShot(True)
        self._record_timer.timeout.connect(self._on_recording_time_elapsed)
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
        if current_mode() != self._conn.ui_theme:
            app = QApplication.instance()
            if app is not None:
                apply_theme(app, self._conn.ui_theme)
            polish_combo(self.unit_combo)
            polish_combo(self.format_combo)
        self.log("Ready.")
        self._refresh_sensor_info()
        self._sync_theme_button()
        self.statusBar().setSizeGripEnabled(True)
        fit_window_to_screen(self, 1100, 720, min_width=640, min_height=420)

    def _header(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("headerBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(20, 10, 16, 10)
        layout.setSpacing(12)
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
        wrap = QWidget()
        outer = QHBoxLayout(wrap)
        outer.setContentsMargins(16, 12, 16, 0)
        outer.setSpacing(0)
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
        layout.addLayout(facts, 1)
        self.status_value = QLabel("No sensor IP")
        self.status_value.setObjectName("badgeIdle")
        layout.addWidget(self.status_value, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addWidget(card)
        return wrap

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

        self.start_btn = QPushButton("Start Recording")
        self.start_btn.setObjectName("primary")
        self.start_btn.setDefault(True)
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)

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
            payload = build_payload_for_recording_time(
                plan,
                seconds,
                recording_format=self._recording_format(),
            )
        except (ValidationError, RequestBuildError, FrequencyError) as exc:
            return None, str(exc)
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
            if phase in {SUBMITTING, RECORDING, FINALIZING}:
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
        self.start_btn.setEnabled(True)
        self.start_btn.setText("Start Recording")

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
            QMessageBox.warning(self, "Cannot start", error or "Check frequency and time.")
            return
        if not self._conn.host.strip():
            self._submit_lock = False
            self._open_settings()
            if not self._conn.host.strip():
                return
            self._submit_lock = True
        try:
            target = self._target()
        except ValidationError as exc:
            self._submit_lock = False
            QMessageBox.warning(self, "Connection", str(exc))
            self._open_settings()
            return
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
        self._set_recording_ui(False)
        self._http_in_flight = False
        client = self._record_client
        self._record_client = None
        if client is not None:
            self._ignore_late_http = True
            client.close()
        self._set_phase(FINALIZING)
        if self._sftp_win is None:
            self._set_msg("Time elapsed. Open Sensor Files to check the recording.")
            self.statusBar().showMessage("Time elapsed")
            self.log("Time elapsed. Open Sensor Files to confirm the file.")
        else:
            self._set_msg("Time elapsed. Waiting for files…")
            self.statusBar().showMessage("Waiting for files")
            self.log("Time elapsed. Looking for recording files.")
            self._sftp_win.retry_today()
        self._schedule_sftp_part_refresh()
        self._schedule_file_confirm()

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
