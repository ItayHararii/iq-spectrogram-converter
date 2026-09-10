"""Simple CRFS IQ Recorder UI."""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
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
    DEFAULT_BANDWIDTH_HZ,
    DEFAULT_CENTER_HZ,
    DEFAULT_FREQ_UNIT,
    DEFAULT_RECORDED_TIME_S,
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
from .paths import load_settings, save_settings
from .recording_history import add_recording, new_recording, remove_recording
from .request_builder import RequestBuildError, build_payload_for_recording_time
from .sanitizer import redact_text
from .sensor_info import SensorInfo, blank_info, checking_info
from .size_estimate import SizeEstimateError, empirical_size_mb, format_mb
from .sftp_window import SftpWindow
from .validation import ValidationError, parse_connection, parse_recorded_time

STYLESHEET = """
QMainWindow, QWidget#central, QSplitter {
    background: #eef1f4;
    color: #1a2332;
    font-size: 13px;
}
QLabel, QCheckBox, QRadioButton, QStatusBar, QMenuBar, QMenu {
    color: #1a2332;
}
QLineEdit, QPlainTextEdit {
    background: #ffffff;
    color: #1a2332;
    border: 1px solid #c5ced6;
    border-radius: 4px;
    padding: 6px 8px;
    min-height: 28px;
    selection-background-color: #00c2d4;
    selection-color: #041018;
}
QRadioButton { spacing: 8px; font-weight: 500; }
QPushButton {
    background: #e8eef2;
    color: #1a2332;
    border: 1px solid #c5ced6;
    border-radius: 5px;
    padding: 8px 16px;
}
QPushButton:hover { background: #dde7ec; }
QPushButton:disabled { color: #8a97a3; }
QPushButton#primary {
    background: #00c2d4;
    color: #041018;
    font-weight: 700;
    min-height: 36px;
    font-size: 14px;
    border: 1px solid #00a9b8;
}
QPushButton#primary:hover { background: #00d4e8; }
QPushButton#primary:disabled { background: #9fdbe3; color: #3a5560; }
QLabel#headerTitle { color: #f4f7fb; font-size: 18px; font-weight: 700; background: transparent; }
QLabel#headerSub { color: #c5d0da; background: transparent; }
QLabel#sensorFactLabel {
    color: #8fa0b0;
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.4px;
    background: transparent;
}
QLabel#sensorFactValue {
    color: #f4f7fb;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}
QLabel#sensorStatusOk { color: #7ddea0; font-size: 13px; font-weight: 700; background: transparent; }
QLabel#sensorStatusWait { color: #e6c15c; font-size: 13px; font-weight: 700; background: transparent; }
QLabel#sensorStatusBad { color: #f08080; font-size: 13px; font-weight: 700; background: transparent; }
QLabel#demoBanner {
    background: #fff4cc;
    color: #5c4a00;
    border: 1px solid #e6d48a;
    border-radius: 6px;
    padding: 8px;
    font-weight: 600;
}
QLabel#muted { color: #3d4c5c; font-weight: 400; }
QLabel#error { color: #b42318; font-weight: 400; }
QLabel#sizeHint { color: #1a2332; font-size: 13px; }
QPlainTextEdit#log, QPlainTextEdit#log:read-only {
    background: #0f1720;
    color: #c8d4e0;
    border-radius: 6px;
    font-family: Consolas, "Courier New", monospace;
}
QStatusBar { background: #e8eef2; color: #1a2332; }
QMenuBar { background: #eef1f4; color: #1a2332; }
QHeaderView::section { background: #e8eef2; color: #1a2332; }
"""


def configure_appearance(app: QApplication) -> None:
    app.setStyle("Fusion")
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Light)
    except Exception:
        pass
    pal = QPalette()
    window = QColor("#eef1f4")
    text = QColor("#1a2332")
    pal.setColor(QPalette.ColorRole.Window, window)
    pal.setColor(QPalette.ColorRole.WindowText, text)
    pal.setColor(QPalette.ColorRole.Base, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor("#f4f6f8"))
    pal.setColor(QPalette.ColorRole.Text, text)
    pal.setColor(QPalette.ColorRole.Button, QColor("#e8eef2"))
    pal.setColor(QPalette.ColorRole.ButtonText, text)
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor("#6b7785"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor("#00c2d4"))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#041018"))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.ToolTipText, text)
    app.setPalette(pal)
    app.setStyleSheet(STYLESHEET)


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
    here = Path(__file__).resolve()
    for path in (
        here.parents[1] / "assets" / "sensorz_icon.ico",
        here.parents[2] / "assets" / "sensorz_icon.ico",
        here.parents[2] / "assets" / "sensorz_icon.png",
    ):
        if path.is_file():
            return path
    return None


class MainWindow(QMainWindow):
    def __init__(self, *, demo: bool = False) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 580)
        self.setStyleSheet(STYLESHEET)
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
        self._ignore_late_http = False
        self._record_client: EmpClient | None = None
        self._sftp_win: SftpWindow | None = None
        self._last_recording_s: float | None = None
        self._open_record_id: str | None = None
        self._record_timer = QTimer(self)
        self._record_timer.setSingleShot(True)
        self._record_timer.timeout.connect(self._on_recording_time_elapsed)
        self._plan = from_center_bandwidth(DEFAULT_CENTER_HZ, DEFAULT_BANDWIDTH_HZ)
        self._conn = ConnectionState.from_settings(load_settings(), demo=demo)

        central = QWidget()
        central.setObjectName("central")
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header())

        self.demo_banner = QLabel("Demo mode — nothing is sent to a real sensor.")
        self.demo_banner.setObjectName("demoBanner")
        self.demo_banner.setContentsMargins(16, 8, 16, 8)
        root.addWidget(self.demo_banner)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._recording_panel())
        splitter.addWidget(self._log_panel())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([580, 360])
        body = QWidget()
        body_l = QVBoxLayout(body)
        body_l.setContentsMargins(16, 12, 16, 12)
        body_l.addWidget(splitter)
        root.addWidget(body, 1)

        self.setStatusBar(QStatusBar())
        self._apply_mode_fields()
        self._refresh_size()
        self._sync_connection_label()
        self._sync_demo_banner()
        self.log("Ready. Set frequencies and recording time, then click Start Recording.")
        self._refresh_sensor_info()

    def _header(self) -> QWidget:
        bar = QWidget()
        bar.setObjectName("headerBar")
        bar.setStyleSheet(
            "QWidget#headerBar { background: #000000; }"
            "QLabel#headerTitle { color: #f4f7fb; font-size: 18px; font-weight: 700; background: transparent; }"
            "QLabel#headerSub { color: #c5d0da; background: transparent; }"
            "QLabel#sensorFactLabel { color: #8fa0b0; font-size: 10px; font-weight: 600; background: transparent; }"
            "QLabel#sensorFactValue { color: #f4f7fb; font-size: 13px; font-weight: 600; background: transparent; }"
            "QPushButton { background: #1f2933; color: #f4f7fb; border: 1px solid #3d4c5c; }"
        )
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(16, 10, 16, 10)
        titles = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("headerTitle")
        self.conn_label = QLabel("")
        self.conn_label.setObjectName("headerSub")
        titles.addWidget(title)
        titles.addWidget(self.conn_label)
        layout.addLayout(titles, 1)

        facts = QHBoxLayout()
        facts.setSpacing(22)
        self.model_value = self._sensor_fact(facts, "Model")
        self.firmware_value = self._sensor_fact(facts, "Firmware")
        self.serial_value = self._sensor_fact(facts, "Serial")
        self.status_value = self._sensor_fact(facts, "Status")
        layout.addLayout(facts)

        settings_btn = QPushButton("Connection Settings")
        settings_btn.clicked.connect(self._open_settings)
        layout.addWidget(settings_btn)
        ver = QLabel(f"v{__version__}")
        ver.setObjectName("headerSub")
        layout.addWidget(ver)
        return bar

    def _sensor_fact(self, row: QHBoxLayout, label: str) -> QLabel:
        box = QVBoxLayout()
        box.setSpacing(1)
        caption = QLabel(label.upper())
        caption.setObjectName("sensorFactLabel")
        value = QLabel("—")
        value.setObjectName("sensorFactValue")
        box.addWidget(caption)
        box.addWidget(value)
        row.addLayout(box)
        return value

    def _recording_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(0, 0, 10, 0)
        layout.setSpacing(12)

        intro = QLabel("Choose a band, set recording time, then start. Files are stored on the sensor.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.mode_start = QRadioButton("Start / End Frequency")
        self.mode_center = QRadioButton("Center Frequency / Bandwidth")
        self.mode_center.setChecked(True)
        modes = QHBoxLayout()
        modes.addWidget(self.mode_start)
        modes.addWidget(self.mode_center)
        modes.addStretch()
        layout.addLayout(modes)
        group = QButtonGroup(self)
        group.addButton(self.mode_start)
        group.addButton(self.mode_center)
        self.mode_start.toggled.connect(self._on_mode_changed)

        form = QFormLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.label_a = QLabel("Center frequency (MHz)")
        self.label_b = QLabel("Bandwidth (MHz)")
        self.field_a = QLineEdit(display_from_hz(DEFAULT_CENTER_HZ, DEFAULT_FREQ_UNIT))
        self.field_b = QLineEdit(display_from_hz(DEFAULT_BANDWIDTH_HZ, DEFAULT_FREQ_UNIT))
        self.time_edit = QLineEdit(str(DEFAULT_RECORDED_TIME_S))
        self.time_edit.setPlaceholderText("0.1")
        for widget in (self.field_a, self.field_b, self.time_edit):
            widget.textChanged.connect(self._refresh_size)
        form.addRow(self.label_a, self.field_a)
        form.addRow(self.label_b, self.field_b)
        form.addRow("Recording time (seconds)", self.time_edit)
        layout.addLayout(form)

        self.msg = QLabel("")
        self.msg.setWordWrap(True)
        layout.addWidget(self.msg)

        self.size_label = QLabel("")
        self.size_label.setObjectName("sizeHint")
        layout.addWidget(self.size_label)

        self.start_btn = QPushButton("Start Recording")
        self.start_btn.setObjectName("primary")
        self.start_btn.clicked.connect(self._on_start)
        layout.addWidget(self.start_btn)

        self.sftp_btn = QPushButton("Open Sensor Files (SFTP)")
        self.sftp_btn.clicked.connect(self._open_sftp)
        layout.addWidget(self.sftp_btn)

        layout.addStretch()
        return box

    def _log_panel(self) -> QWidget:
        box = QWidget()
        layout = QVBoxLayout(box)
        layout.setContentsMargins(6, 0, 0, 0)
        head = QHBoxLayout()
        title = QLabel("Activity Log")
        title.setStyleSheet("font-weight: 700;")
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear_log)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(self.clear_btn)
        layout.addLayout(head)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setObjectName("log")
        self.log_edit.setReadOnly(True)
        self.log_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.log_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self.log_edit, 1)
        return box

    def closeEvent(self, event) -> None:  # noqa: N802
        self._alive = False
        data = self._conn.persistable()
        if self._demo_forced:
            data["demo"] = False
        save_settings(data)
        self._record_timer.stop()
        if self._record_client is not None:
            self._record_client.close()
        if self._sftp_win:
            self._sftp_win.close()
        super().closeEvent(event)

    def _sync_demo_banner(self) -> None:
        self.demo_banner.setVisible(self._conn.demo)
        if self._demo_forced:
            self._conn.demo = True
            self.demo_banner.setVisible(True)

    def _sync_connection_label(self) -> None:
        host = self._conn.host or "not set"
        mode = "demo" if self._conn.demo else self._conn.username
        self.conn_label.setText(f"Sensor {host}  ·  {mode}")

    def _set_status_style(self, kind: str) -> None:
        names = {"ok": "sensorStatusOk", "wait": "sensorStatusWait", "bad": "sensorStatusBad"}
        self.status_value.setObjectName(names.get(kind, "sensorFactValue"))
        self.status_value.style().unpolish(self.status_value)
        self.status_value.style().polish(self.status_value)

    def _apply_sensor_info(self, info: SensorInfo) -> None:
        self._sensor_info = info
        self.model_value.setText(info.display_model())
        self.firmware_value.setText(info.display_firmware())
        self.serial_value.setText(info.display_serial())
        self.status_value.setText(info.status)
        lowered = info.status.casefold()
        if info.connected or lowered in {"connected", "online"}:
            self._set_status_style("ok")
        elif lowered in {"checking…", "checking...", "demo"}:
            self._set_status_style("wait")
        elif lowered in {"no sensor ip"}:
            self._set_status_style("wait")
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

        def work() -> tuple[int, SensorInfo]:
            return token, self._client().fetch_sensor_info(self._target(), self._conn.http_password)

        self._start_job(work, self._show_sensor_info_result, info=True)

    def _show_sensor_info_result(self, result: Any) -> None:
        if not self._alive:
            return
        token = None
        info = result
        if isinstance(result, tuple) and len(result) == 2:
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
        self._apply_sensor_info(info)
        if info.connected:
            self.log(
                f"Sensor {info.display_serial()} · model {info.display_model()} · "
                f"firmware {info.display_firmware()}"
            )
        elif info.status not in {"Checking…", "No sensor IP", "Demo"}:
            self.log(f"Could not read sensor details: {info.status}")

    def _open_settings(self) -> None:
        dialog = ConnectionDialog(self._conn, self)
        if self._demo_forced:
            dialog.demo_check.setChecked(True)
            dialog.demo_check.setEnabled(False)
        dialog.test_requested.connect(self._test_connection)
        if dialog.exec():
            self._conn = dialog.result_state()
            if self._demo_forced:
                self._conn.demo = True
            save_settings(self._conn.persistable())
            self._sync_connection_label()
            self._sync_demo_banner()
            self._refresh_sensor_info()
            self.log(f"Saved connection settings for {self._conn.host or '(no host)'}.")

    def _on_mode_changed(self) -> None:
        self._apply_mode_fields(self._plan)
        self._refresh_size()

    def _apply_mode_fields(self, plan: FrequencyPlan | None = None) -> None:
        plan = plan or self._plan
        center_mode = self.mode_center.isChecked()
        self.field_a.blockSignals(True)
        self.field_b.blockSignals(True)
        try:
            if center_mode:
                self.label_a.setText("Center frequency (MHz)")
                self.label_b.setText("Bandwidth (MHz)")
                self.field_a.setText(display_from_hz(plan.center_hz, "MHz"))
                self.field_b.setText(display_from_hz(plan.bandwidth_hz, "MHz"))
            else:
                self.label_a.setText("Start frequency (MHz)")
                self.label_b.setText("End frequency (MHz)")
                self.field_a.setText(display_from_hz(plan.start_hz, "MHz"))
                self.field_b.setText(display_from_hz(plan.end_hz, "MHz"))
        finally:
            self.field_a.blockSignals(False)
            self.field_b.blockSignals(False)

    def _try_plan(self) -> FrequencyPlan | None:
        try:
            if self.mode_center.isChecked():
                center = parse_to_integer_hz(self.field_a.text(), "MHz", field="Center frequency")
                bw = parse_to_integer_hz(self.field_b.text(), "MHz", field="Bandwidth")
                plan = from_center_bandwidth(center, bw)
            else:
                start = parse_to_integer_hz(self.field_a.text(), "MHz", field="Start frequency")
                end = parse_to_integer_hz(self.field_b.text(), "MHz", field="End frequency")
                plan = from_start_end(start, end)
        except (FrequencyError, ValidationError) as exc:
            self._set_msg(str(exc), error=True)
            return None
        self._plan = plan
        return plan

    def _try_payload(self) -> tuple[dict[str, Any] | None, str | None]:
        plan = self._try_plan()
        if plan is None:
            return None, self.msg.text() or "Check the frequency values."
        try:
            seconds = float(parse_recorded_time(self.time_edit.text()))
            payload = build_payload_for_recording_time(plan, seconds)
        except (ValidationError, RequestBuildError, FrequencyError) as exc:
            return None, str(exc)
        self._set_msg("")
        return payload, None

    def _refresh_size(self) -> None:
        payload, error = self._try_payload()
        if payload is None:
            self.size_label.setText("Est. size —")
            if error:
                self._set_msg(error, error=True)
            return
        scan = payload["remote_recording_scans"][0]
        try:
            mb = empirical_size_mb(scan["bandwidth"], scan["duration"])
            self.size_label.setText(f"Est. size ≈ {format_mb(mb)} MB")
        except (SizeEstimateError, ValidationError, KeyError):
            self.size_label.setText("Est. size —")

    def _set_msg(self, text: str, *, error: bool = False) -> None:
        self.msg.setText(text)
        self.msg.setObjectName("error" if error and text else "muted")
        self.msg.style().unpolish(self.msg)
        self.msg.style().polish(self.msg)

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

    def _set_recording_ui(self, active: bool, *, seconds: float | None = None) -> None:
        self._recording_active = active
        if active:
            label = f"{seconds:g} s" if seconds is not None else ""
            self.start_btn.setEnabled(False)
            self.start_btn.setText(f"Recording… {label}".strip())
            self._set_msg(f"Recording for {label}. The file is stored on the sensor.")
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
    ) -> None:
        thread = QThread()
        worker = Worker(fn)
        worker._on_ok = on_ok  # noqa: SLF001
        worker._is_record = record  # noqa: SLF001
        worker._is_info = info  # noqa: SLF001
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
            self._http_in_flight = False
            self._record_timer.stop()
            self._set_recording_ui(False)
        if getattr(worker, "_is_info", False) and self._alive:
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
        if self._recording_active:
            return
        if self._http_in_flight:
            QMessageBox.information(
                self,
                "Recording",
                "The previous HTTP request is still open. The recording may already be finished — "
                "open Sensor Files to check.",
            )
            return
        payload, error = self._try_payload()
        if payload is None:
            QMessageBox.warning(self, "Cannot start", error or "Check the frequency and time values.")
            return
        if not self._conn.host.strip():
            self._open_settings()
            if not self._conn.host.strip():
                return
        try:
            target = self._target()
        except ValidationError as exc:
            QMessageBox.warning(self, "Connection", str(exc))
            self._open_settings()
            return
        scan = payload["remote_recording_scans"][0]
        seconds = float(scan["duration"])
        self._last_recording_s = seconds
        self._ignore_late_http = False
        plan = self._plan
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
            self._sftp_win.relabel_files()
        client = self._client()
        self._record_client = client
        self.log(
            f"Starting recording: {scan['center_frequency'] / 1e6:g} MHz center, "
            f"{scan['bandwidth'] / 1e6:g} MHz bandwidth, {seconds} s"
            + (" (demo)" if self._conn.demo else "")
        )
        self._set_recording_ui(True, seconds=seconds)
        self._http_in_flight = True
        self._record_timer.start(int(max(seconds, 0.05) * 1000) + 400)

        def work() -> HttpOutcome:
            return client.start_recording(target, self._conn.http_password, payload)

        self._start_job(work, self._show_recording_outcome, record=True)

    def _on_recording_time_elapsed(self) -> None:
        self._set_recording_ui(False)
        self._set_msg("Recording finished. Open Sensor Files to download the WAVE file.")
        self.statusBar().showMessage("Recording finished")
        self.log("Recording time elapsed. Open Sensor Files to get the WAVE file.")
        if self._sftp_win is not None:
            self._sftp_win.retry_today()
            self._schedule_sftp_part_refresh()
        if self._http_in_flight and self._record_client is not None:
            self._ignore_late_http = True
            self._record_client.close()

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

    def _show_test_outcome(self, outcome: Any) -> None:
        self._show_http_outcome(outcome, recording=False)

    def _show_recording_outcome(self, outcome: Any) -> None:
        self._http_in_flight = False
        self._record_client = None
        if not isinstance(outcome, HttpOutcome):
            self.log(str(outcome), level="error")
            self._record_timer.stop()
            self._set_recording_ui(False)
            return
        if not outcome.request_sent and not outcome.accepted:
            self._record_timer.stop()
            self._ignore_late_http = False
            self._set_recording_ui(False)
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
            self.log("Stopped waiting for the sensor HTTP reply. Use Sensor Files if you need the recording.")
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
                self.log(
                    "Request accepted by the sensor. Recording is still in progress; "
                    "this app will mark it finished when the recording time elapses."
                )
            else:
                self.log("Sensor HTTP reply received. That is not required to download the file.")
        elif outcome.kind == "reachable":
            self.log(outcome.detail)
        elif outcome.outcome_unknown:
            self.log(
                "The recording request was sent, but the sensor did not answer in time. "
                "Check Sensor Files before starting another recording."
            )
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
        user = self._conn.sftp_user()
        self.log(f"Opening sensor files for {self._conn.host or 'demo'} as {user} at {today}")
        try:
            if self._sftp_win is not None:
                self._sftp_win.close()
            self._sftp_win = SftpWindow(self._conn, None)
            self._sftp_win.show()
            self._sftp_win.raise_()
        except Exception:
            self.log(traceback.format_exc(limit=6), level="error")
            QMessageBox.warning(self, "SFTP", "Could not open the sensor file window. See the Activity Log.")


def _install_exception_hook() -> None:
    def _write(text: str) -> None:
        try:
            from .paths import app_dir

            path = app_dir() / "crfs_iq_recorder_error.log"
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
    set_app_user_model_id()
    _install_exception_hook()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    configure_appearance(app)
    icon = _icon_path()
    if icon:
        app.setWindowIcon(QIcon(str(icon)))
    window = MainWindow(demo=demo)
    window.show()
    return app.exec()
