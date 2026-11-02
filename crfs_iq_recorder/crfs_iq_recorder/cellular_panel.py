"""Cellular Collection panel: a compact, collapsible section of the main window.

Excel file and worksheet → recording duration → wait → run now or schedule →
Start / Pause / Resume / Stop after current, with a one-line progress text.
Everything heavy (workbook import, identity check, downloads, Excel updates)
runs off the UI thread through the host's job helper or the runner; this
module only draws and forwards. Diagnostics go to the activity log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .cellular_capabilities import capability_for, readable_sensor_name
from .cellular_pickers import DateField, TimesField, format_datetime
from .cellular_plan import CellularPlan, build_plan, format_bytes, format_seconds
from .cellular_protocol import (
    LEVEL_WARNING,
    ProtocolError,
    ProtocolImport,
    WorkbookUnavailable,
    import_protocol,
    list_worksheets,
    read_headers,
    save_snapshot,
    suggest_mapping,
)
from .cellular_runner import (
    S_DONE,
    S_IDLE,
    S_INTERRUPTED,
    S_PAUSE_REQUESTED,
    S_PAUSED,
    S_PAUSED_BACKLOG,
    S_RUNNING,
    S_STOP_REQUESTED,
    S_WAITING,
    S_WAITING_SCHEDULE,
    CampaignSettings,
    CellularRunner,
)
from .cellular_scheduler import MODE_NOW, MODE_TIMES, Schedule, ScheduleTimeZone, tight_schedule_warning
from .cellular_workbook import layout_from_import, plan_rows_from_import
from .json_store import read_json, write_json

SETTINGS_NAME = "settings.json"
DEFAULT_WAIT_S = 10.0
DEFAULT_DURATION_S = 5.0
MAX_START_TIMES = 12


# ----------------------------------------------------------------------------- settings
@dataclass
class PanelSettings:
    """What the panel remembers between runs (no secrets, no results)."""

    workbook: str = ""
    worksheet: str = ""
    duration_s: float = DEFAULT_DURATION_S
    wait_s: float = DEFAULT_WAIT_S
    schedule_mode: str = MODE_NOW
    times: list[str] = field(default_factory=list)
    schedule_date: str = ""
    daily: bool = False
    repeat: int = 1
    until_stopped: bool = False
    expanded: bool = False
    last_sensor: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workbook": self.workbook,
            "worksheet": self.worksheet,
            "duration_s": self.duration_s,
            "wait_s": self.wait_s,
            "schedule_mode": self.schedule_mode,
            "times": list(self.times),
            "schedule_date": self.schedule_date,
            "daily": self.daily,
            "repeat": self.repeat,
            "until_stopped": self.until_stopped,
            "expanded": self.expanded,
            "last_sensor": dict(self.last_sensor),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "PanelSettings":
        data = data or {}
        settings = cls()
        settings.workbook = str(data.get("workbook") or "")
        settings.worksheet = str(data.get("worksheet") or "")
        try:
            settings.duration_s = float(data.get("duration_s") or DEFAULT_DURATION_S)
        except (TypeError, ValueError):
            settings.duration_s = DEFAULT_DURATION_S
        try:
            settings.wait_s = max(float(data.get("wait_s", DEFAULT_WAIT_S)), 0.0)
        except (TypeError, ValueError):
            settings.wait_s = DEFAULT_WAIT_S
        settings.schedule_mode = MODE_TIMES if data.get("schedule_mode") == MODE_TIMES else MODE_NOW
        settings.times = [str(t) for t in data.get("times") or []]
        settings.schedule_date = str(data.get("schedule_date") or "")
        settings.daily = bool(data.get("daily", False))
        try:
            settings.repeat = max(int(data.get("repeat", 1)), 1)
        except (TypeError, ValueError):
            settings.repeat = 1
        settings.until_stopped = bool(data.get("until_stopped", False))
        settings.expanded = bool(data.get("expanded", False))
        settings.last_sensor = {str(k): str(v) for k, v in (data.get("last_sensor") or {}).items()}
        return settings


def settings_path(config_dir: Path) -> Path:
    return Path(config_dir) / "cellular" / SETTINGS_NAME


def load_panel_settings(config_dir: Path) -> PanelSettings:
    data = read_json(settings_path(config_dir))
    return PanelSettings.from_dict(data if isinstance(data, dict) else None)


def save_panel_settings(config_dir: Path, settings: PanelSettings) -> None:
    path = settings_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, settings.to_dict())


def _parse_time(text: str) -> time | None:
    try:
        hour, minute = text.strip().split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, AttributeError):
        return None


def _same_file(first: str | Path, second: str | Path) -> bool:
    try:
        return Path(first).resolve() == Path(second).resolve()
    except OSError:
        return str(first).strip().lower() == str(second).strip().lower()


# ----------------------------------------------------------------------------- preview
class PlanPreviewDialog(QDialog):
    """What will be recorded, row by row, before Start — including any sequential windows."""

    def __init__(self, plan: CellularPlan, *, wait_s: float, rows_total: int, notes: list[str] | None = None, start_label: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cellular Collection")
        self.resize(760, 480)
        layout = QVBoxLayout(self)
        windows = len(plan.tasks)
        summary = QLabel(
            f"{rows_total} protocol row(s)"
            + (f", recorded as {windows} captures per pass" if windows != rows_total else "")
            + f" · {format_seconds(plan.capture_seconds)} of IQ per pass · about {format_bytes(plan.estimated_bytes)} per pass"
            + f" · ≈ {format_seconds(plan.estimated_round_seconds(wait_s))} per pass with {format_seconds(wait_s)} waits"
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        for text in [plan.window_note, *(notes or [])]:
            if text:
                note = QLabel(text)
                note.setWordWrap(True)
                note.setObjectName("muted")
                layout.addWidget(note)
        tree = QTreeWidget()
        tree.setColumnCount(5)
        tree.setHeaderLabels(["Row", "Band", "Direction", "Recorded as", "Duration"])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for task in plan.tasks:
            recorded = f"{task.window_start_hz / 1e6:g}–{task.window_stop_hz / 1e6:g} MHz"
            if task.window_count > 1:
                recorded += f" (window {task.window_index} of {task.window_count}, sequential)"
            item = QTreeWidgetItem([str(task.row), task.band_label, task.direction, recorded, f"{task.duration_s:g} s"])
            tree.addTopLevelItem(item)
        header = tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(tree, 1)
        problems = [i for i in plan.issues if i.level in {"error", LEVEL_WARNING}]
        if problems:
            box = QPlainTextEdit()
            box.setReadOnly(True)
            box.setMaximumHeight(110)
            box.setPlainText("\n".join((f"row {i.row}: " if i.row else "") + i.message for i in problems))
            layout.addWidget(box)
        buttons = QDialogButtonBox()
        if start_label:
            ok = buttons.addButton(start_label, QDialogButtonBox.ButtonRole.AcceptRole)
            ok.setEnabled(plan.ready)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


# ----------------------------------------------------------------------------- panel
class CellularPanel(QFrame):
    """Collapsible "Cellular Collection" section for the main window."""

    def __init__(self, runner: CellularRunner, *, host, config_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("inputWrap")
        self.runner = runner
        self.host = host
        self._config_dir = Path(config_dir)
        self.settings = load_panel_settings(self._config_dir)
        self.imported: ProtocolImport | None = None
        self.plan: CellularPlan | None = None
        self._snapshot_path = ""
        self._import_token = 0
        self._importing = False
        self._identity_pending = False
        self._built = False
        self._build_header()
        self.runner.status_changed.connect(self._on_status)
        self.runner.session_changed.connect(self._refresh_recovery)
        self.runner.log.connect(self._on_runner_log)
        if self.settings.expanded or self.runner.session is not None:
            self._set_expanded(True)
        else:
            self._on_status(self.runner.status())

    # ------------------------------------------------------------------ construction
    def _build_header(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 10)
        outer.setSpacing(8)
        head = QHBoxLayout()
        self.toggle_btn = QPushButton("▸ Cellular Collection")
        self.toggle_btn.setObjectName("ghost")
        self.toggle_btn.setAutoDefault(False)
        self.toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_btn.clicked.connect(lambda: self._set_expanded(self.body.isHidden()))
        head.addWidget(self.toggle_btn)
        head.addStretch(1)
        self.chip = QLabel("Off")
        self.chip.setObjectName("muted")
        head.addWidget(self.chip)
        outer.addLayout(head)
        self.body = QWidget()
        self.body.setVisible(False)
        outer.addWidget(self.body)

    def _ensure_built(self) -> None:
        if self._built:
            return
        self._built = True
        self._build_body()
        self._load_saved_workbook()
        self._refresh_recovery()
        self._on_status(self.runner.status())

    def _build_body(self) -> None:
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)

        # Excel file and worksheet
        grid.addWidget(QLabel("Excel file"), 0, 0)
        file_row = QHBoxLayout()
        file_row.setSpacing(6)
        self.workbook_edit = QLineEdit()
        self.workbook_edit.setPlaceholderText("Protocol workbook (.xlsx / .xlsm)")
        self.workbook_edit.setReadOnly(True)
        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setObjectName("ghost")
        self.browse_btn.setAutoDefault(False)
        self.browse_btn.clicked.connect(self._browse_workbook)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.setObjectName("ghost")
        self.reload_btn.setAutoDefault(False)
        self.reload_btn.setToolTip("Read the worksheet again (after editing the protocol)")
        self.reload_btn.clicked.connect(lambda: self._import(force=True))
        file_row.addWidget(self.workbook_edit, 1)
        file_row.addWidget(self.browse_btn)
        file_row.addWidget(self.reload_btn)
        grid.addLayout(file_row, 0, 1)
        grid.addWidget(QLabel("Worksheet"), 1, 0)
        self.sheet_combo = QComboBox()
        self.sheet_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.sheet_combo.currentIndexChanged.connect(self._on_sheet_changed)
        grid.addWidget(self.sheet_combo, 1, 1, alignment=Qt.AlignmentFlag.AlignLeft)
        self.import_label = QLabel("Load the protocol workbook.")
        self.import_label.setObjectName("muted")
        self.import_label.setWordWrap(True)
        grid.addWidget(self.import_label, 2, 1)

        # Duration and wait
        grid.addWidget(QLabel("Recording duration"), 3, 0)
        duration_row = QHBoxLayout()
        duration_row.setSpacing(6)
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setDecimals(1)
        self.duration_spin.setRange(0.5, 3600.0)
        self.duration_spin.setSingleStep(1.0)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setValue(self.settings.duration_s if self.settings.duration_s >= 0.5 else DEFAULT_DURATION_S)
        self.duration_spin.valueChanged.connect(self._rebuild_plan)
        duration_row.addWidget(self.duration_spin)
        self.window_label = QLabel("")
        self.window_label.setObjectName("muted")
        self.window_label.setWordWrap(True)
        duration_row.addWidget(self.window_label, 1)
        grid.addLayout(duration_row, 3, 1)
        grid.addWidget(QLabel("Wait between recordings"), 4, 0)
        wait_row = QHBoxLayout()
        wait_row.setSpacing(6)
        self.wait_spin = QSpinBox()
        self.wait_spin.setRange(0, 24 * 3600)
        self.wait_spin.setSuffix(" s")
        self.wait_spin.setValue(int(round(self.settings.wait_s)))
        wait_row.addWidget(self.wait_spin)
        wait_row.addStretch(1)
        grid.addLayout(wait_row, 4, 1)

        # Run now / schedule
        grid.addWidget(QLabel("When"), 5, 0, alignment=Qt.AlignmentFlag.AlignTop)
        when = QVBoxLayout()
        when.setSpacing(6)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(12)
        self.run_now = QRadioButton("Run now")
        self.run_times = QRadioButton("Schedule")
        (self.run_times if self.settings.schedule_mode == MODE_TIMES else self.run_now).setChecked(True)
        self.run_now.toggled.connect(lambda on: self._on_schedule_mode(not on))
        mode_row.addWidget(self.run_now)
        mode_row.addWidget(self.run_times)
        mode_row.addStretch(1)
        when.addLayout(mode_row)
        self.schedule_box = QWidget()
        schedule = QGridLayout(self.schedule_box)
        schedule.setContentsMargins(0, 0, 0, 0)
        schedule.setHorizontalSpacing(8)
        schedule.setVerticalSpacing(6)
        schedule.addWidget(QLabel("Start times"), 0, 0, alignment=Qt.AlignmentFlag.AlignTop)
        saved_times = [t for t in (_parse_time(x) for x in self.settings.times) if t is not None]
        self.times_field = TimesField(saved_times, maximum=MAX_START_TIMES)
        schedule.addWidget(self.times_field, 0, 1)
        schedule.addWidget(QLabel("Date"), 1, 0)
        try:
            saved_date = date.fromisoformat(self.settings.schedule_date) if self.settings.schedule_date else None
        except ValueError:
            saved_date = None
        if saved_date is not None and saved_date < date.today():
            saved_date = None
        date_row = QHBoxLayout()
        date_row.setSpacing(10)
        self.date_field = DateField(saved_date or date.today(), minimum=date.today())
        self.date_field.setMaximumWidth(220)
        date_row.addWidget(self.date_field)
        self.daily_check = QCheckBox("Repeat daily")
        self.daily_check.setChecked(self.settings.daily)
        self.daily_check.toggled.connect(self._on_daily)
        date_row.addWidget(self.daily_check)
        date_row.addStretch(1)
        schedule.addLayout(date_row, 1, 1)
        when.addWidget(self.schedule_box)
        repeat_row = QHBoxLayout()
        repeat_row.setSpacing(8)
        self.repeat_label = QLabel("Repeat")
        repeat_row.addWidget(self.repeat_label)
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 999)
        self.repeat_spin.setSuffix(" pass(es)")
        self.repeat_spin.setValue(self.settings.repeat)
        repeat_row.addWidget(self.repeat_spin)
        self.until_stopped = QCheckBox("Until stopped")
        self.until_stopped.setChecked(self.settings.until_stopped)
        self.until_stopped.toggled.connect(lambda on: self.repeat_spin.setEnabled(not on))
        self.repeat_spin.setEnabled(not self.settings.until_stopped)
        repeat_row.addWidget(self.until_stopped)
        repeat_row.addStretch(1)
        when.addLayout(repeat_row)
        grid.addLayout(when, 5, 1)
        body.addLayout(grid)

        # Buttons
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.preview_btn = QPushButton("Preview")
        self.preview_btn.setObjectName("ghost")
        self.preview_btn.setAutoDefault(False)
        self.preview_btn.clicked.connect(lambda: self._show_preview(start=False))
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("primary")
        self.start_btn.setAutoDefault(False)
        self.start_btn.clicked.connect(self._start_clicked)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setObjectName("ghost")
        self.pause_btn.setAutoDefault(False)
        self.pause_btn.clicked.connect(self.runner.pause)
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setObjectName("ghost")
        self.resume_btn.setAutoDefault(False)
        self.resume_btn.clicked.connect(self.runner.resume)
        self.stop_btn = QPushButton("Stop after current recording")
        self.stop_btn.setObjectName("ghost")
        self.stop_btn.setAutoDefault(False)
        self.stop_btn.clicked.connect(self._stop)
        for button in (self.preview_btn, self.start_btn, self.pause_btn, self.resume_btn, self.stop_btn):
            buttons.addWidget(button)
        buttons.addStretch(1)
        body.addLayout(buttons)

        # Progress line and pending indicator
        self.status_label = QLabel("Idle")
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("muted")
        self.detail_label.setWordWrap(True)
        self.detail_label.setVisible(False)
        body.addWidget(self.detail_label)

        # Recovery (interrupted session, missed pass)
        self.recovery = QFrame()
        self.recovery.setObjectName("inputWrap")
        rec = QVBoxLayout(self.recovery)
        rec.setContentsMargins(10, 8, 10, 8)
        rec.setSpacing(6)
        self.recovery_label = QLabel("")
        self.recovery_label.setWordWrap(True)
        rec.addWidget(self.recovery_label)
        rec_buttons = QHBoxLayout()
        rec_buttons.setSpacing(8)
        self.reconcile_btn = QPushButton("Check with the sensor")
        self.reconcile_btn.clicked.connect(self._reconcile)
        self.resume_session_btn = QPushButton("Continue")
        self.resume_session_btn.clicked.connect(self._resume_after_restart)
        self.skip_uncertain_btn = QPushButton("Skip uncertain")
        self.skip_uncertain_btn.clicked.connect(self._skip_uncertain)
        self.run_missed_btn = QPushButton("Run the missed pass now")
        self.run_missed_btn.clicked.connect(self._run_missed)
        self.skip_missed_btn = QPushButton("Skip it")
        self.skip_missed_btn.clicked.connect(self._skip_missed)
        self.discard_btn = QPushButton("Dismiss")
        self.discard_btn.clicked.connect(self._discard)
        for button in (self.reconcile_btn, self.resume_session_btn, self.skip_uncertain_btn, self.run_missed_btn, self.skip_missed_btn, self.discard_btn):
            button.setObjectName("ghost")
            button.setAutoDefault(False)
            rec_buttons.addWidget(button)
        rec_buttons.addStretch(1)
        rec.addLayout(rec_buttons)
        self.recovery.setVisible(False)
        body.addWidget(self.recovery)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        self._on_schedule_mode(self.run_times.isChecked())
        self._on_daily(self.daily_check.isChecked())

    # ------------------------------------------------------------------ small helpers
    def _set_expanded(self, on: bool) -> None:
        if on:
            self._ensure_built()
        self.body.setVisible(on)
        self.toggle_btn.setText(("▾ " if on else "▸ ") + "Cellular Collection")
        self.settings.expanded = on
        self._save_settings()

    def _save_settings(self) -> None:
        if self._built:
            self.settings.workbook = self.workbook_edit.text().strip()
            self.settings.worksheet = self._current_sheet()
            self.settings.duration_s = float(self.duration_spin.value())
            self.settings.wait_s = float(self.wait_spin.value())
            self.settings.schedule_mode = MODE_TIMES if self.run_times.isChecked() else MODE_NOW
            self.settings.times = [t.strftime("%H:%M") for t in self.times_field.times()]
            chosen = self.date_field.date()
            self.settings.schedule_date = chosen.isoformat() if chosen else ""
            self.settings.daily = self.daily_check.isChecked()
            self.settings.repeat = int(self.repeat_spin.value())
            self.settings.until_stopped = self.until_stopped.isChecked()
        try:
            save_panel_settings(self._config_dir, self.settings)
        except OSError:
            pass

    def duration_seconds(self) -> float:
        return float(self.duration_spin.value())

    def wait_seconds(self) -> float:
        return float(self.wait_spin.value())

    def _sensor_model(self) -> str:
        """The connected sensor's model, '' until its identity has been read ('—' is the window's placeholder)."""
        info = getattr(self.host, "_sensor_info", None)
        return str(getattr(info, "model", "") or "").replace("—", "").strip()

    def _capability(self):
        return capability_for(self._sensor_model())

    def _download_dir(self) -> Path:
        from .paths import resolved_download_dir

        conn = getattr(self.host, "_conn", None)
        return resolved_download_dir(getattr(conn, "download_dir", None))

    def _host_log(self, message: str, level: str = "info") -> None:
        log = getattr(self.host, "log", None)
        if not callable(log):
            return
        try:
            log(f"Cellular: {message}", level=level)
        except TypeError:
            try:
                log(f"Cellular: {message}")
            except Exception:  # noqa: BLE001 - the log widget may not exist yet
                pass
        except Exception:  # noqa: BLE001
            pass

    def _on_runner_log(self, message: str, level: str) -> None:
        self._host_log(message, level)

    def _on_schedule_mode(self, times_on: bool) -> None:
        self.schedule_box.setVisible(times_on)
        # Run now: passes back to back. Schedule: every listed time (daily until stopped).
        self.repeat_label.setVisible(not times_on)
        self.repeat_spin.setVisible(not times_on)
        self.until_stopped.setVisible(not times_on)

    def _on_daily(self, daily: bool) -> None:
        self.date_field.setEnabled(not daily)
        self.date_field.setToolTip("Every day from today" if daily else "")

    # ------------------------------------------------------------------ protocol
    def _load_saved_workbook(self) -> None:
        if self.settings.workbook and Path(self.settings.workbook).is_file():
            self.workbook_edit.setText(self.settings.workbook)
            self._fill_sheets(self.settings.worksheet)
            self._import(force=False, quiet=True)

    def _browse_workbook(self) -> None:
        start = self.workbook_edit.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Protocol workbook", start, "Excel workbooks (*.xlsx *.xlsm);;All files (*)")
        if not path:
            return
        self.workbook_edit.setText(path)
        self._fill_sheets("")
        self._import(force=True)

    def _fill_sheets(self, preferred: str) -> None:
        path = self.workbook_edit.text().strip()
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        try:
            sheets = list_worksheets(path)
        except ProtocolError as exc:
            kept = self._kept_import(path, exc)
            if kept is not None:
                # The worksheet stays selected so results are queued for it.
                self.sheet_combo.addItem(kept.worksheet, kept.worksheet)
                self.sheet_combo.setCurrentIndex(0)
                self.import_label.setText(self._kept_text(kept, exc))
            else:
                self.import_label.setText(str(exc))
            self.sheet_combo.blockSignals(False)
            return
        for name, visible in sheets:
            if visible and not name.startswith("Round "):
                self.sheet_combo.addItem(name, name)
        index = self.sheet_combo.findData(preferred) if preferred else -1
        self.sheet_combo.setCurrentIndex(index if index >= 0 else 0)
        self.sheet_combo.blockSignals(False)

    def _on_sheet_changed(self, _index: int) -> None:
        self._import(force=True)

    def _current_sheet(self) -> str:
        chosen = str(self.sheet_combo.currentData() or self.sheet_combo.currentText() or "")
        if not chosen and self.imported is not None and _same_file(self.imported.source, self.workbook_edit.text().strip() or "-"):
            chosen = self.imported.worksheet  # the list could not be read; the imported worksheet stands
        return chosen

    def _kept_import(self, path: str, error: Exception) -> ProtocolImport | None:
        """The rows imported earlier from this very file, kept while the file cannot be opened
        (locked by another program, drive unplugged). Any other problem drops them: the
        worksheet itself changed and must be read again."""
        if not isinstance(error, WorkbookUnavailable) or self.imported is None:
            return None
        return self.imported if _same_file(self.imported.source, path) else None

    @staticmethod
    def _kept_text(kept: ProtocolImport, error: Exception) -> str:
        return (
            f"{error} Using the {len(kept.valid_entries())} row(s) imported earlier from '{kept.worksheet}'; "
            "results are queued until the workbook can be written."
        )

    def _import(self, *, force: bool, quiet: bool = False) -> None:
        """Read the worksheet (off the UI thread when the host offers a job helper)."""
        path = self.workbook_edit.text().strip()
        sheet = self._current_sheet()
        if not path or not sheet:
            return
        self._import_token += 1
        token = self._import_token
        self._importing = True
        self.import_label.setText("Reading the worksheet…")
        self._sync_buttons(self.runner.status())

        def work():
            try:
                headers, _row = read_headers(path, sheet)
                mapping = suggest_mapping(headers)
                if mapping.missing():
                    return {"error": "This worksheet cannot be used as a protocol: missing " + "; ".join(mapping.missing()) + "."}
                return {"imported": import_protocol(path, sheet, mapping)}
            except WorkbookUnavailable as exc:
                return {"error": str(exc), "unavailable": exc}
            except ProtocolError as exc:
                return {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - reported, never raised across threads
                return {"error": f"{type(exc).__name__}: {exc}"}

        def apply(result) -> None:
            if token != self._import_token:
                return
            self._importing = False
            if not isinstance(result, dict) or result.get("error"):
                error = result.get("unavailable") if isinstance(result, dict) else None
                kept = self._kept_import(path, error) if error is not None else None
                if kept is not None and kept.worksheet == sheet:
                    # Locked or unplugged right now: the plan imported earlier stands.
                    self.import_label.setText(self._kept_text(kept, error))
                    if not quiet:
                        self._host_log(self.import_label.text(), "error")
                    self._sync_buttons(self.runner.status())
                    return
                self.imported = None
                self.plan = None
                self.import_label.setText(str(result.get("error") if isinstance(result, dict) else "The worksheet could not be read."))
                if not quiet:
                    self._host_log(self.import_label.text(), "error")
                self._sync_buttons(self.runner.status())
                return
            self.imported = result["imported"]
            try:
                self._snapshot_path = str(save_snapshot(self.imported, self._config_dir))
            except Exception:  # noqa: BLE001 - snapshot failure must not block the preview
                self._snapshot_path = ""
            self._rebuild_plan()
            self._save_settings()
            if not quiet:
                self._host_log(self.import_label.text())

        runner = getattr(self.host, "_start_job", None)
        if runner is None:
            apply(work())
        else:
            runner(work, apply)

    def _rebuild_plan(self, *_args) -> None:
        if self.imported is None:
            return
        capability = self._capability()
        self.plan = build_plan(self.imported, capability, fallback_duration_s=self.duration_seconds())
        rows = len(self.imported.valid_entries())
        windows = len(self.plan.tasks)
        parts = [f"{rows} row(s) from '{self.imported.worksheet}'"]
        if windows != rows:
            parts.append(f"recorded as {windows} captures per pass")
        parts.append(f"≈ {format_bytes(self.plan.estimated_bytes)} per pass")
        results = len(self.imported.result_rows())
        if results:
            parts.append(f"{results} earlier result row(s) kept, not recorded again")
        errors = len(self.imported.errors) + len(self.plan.errors)
        if errors:
            parts.append(f"{errors} problem(s) — see Preview")
        layout = layout_from_import(self.imported)
        if not (layout.result or layout.time or layout.duration):
            parts.append("no result column in this sheet: results stay in the log and the manifests")
        self.import_label.setText(" · ".join(parts))
        self.window_label.setText(self._window_text(capability))
        self._sync_buttons(self.runner.status())

    def _window_text(self, capability) -> str:
        if self.plan is None:
            return ""
        window_mhz = self.plan.window_hz / 1e6
        split = [t for t in self.plan.tasks if t.window_count > 1]
        rows_split = len({t.row for t in split})
        if self._sensor_model():
            name = readable_sensor_name(capability.model) or capability.model or "this sensor"
            text = f"{name}: up to {window_mhz:g} MHz per capture at {self.duration_seconds():g} s."
        else:
            text = (
                f"Sensor not verified yet: up to {window_mhz:g} MHz per capture at {self.duration_seconds():g} s "
                "(conservative figure; the plan is rebuilt for the connected sensor when you press Start)."
            )
        if rows_split:
            text += f" {rows_split} row(s) wider than that are recorded as sequential windows, each for the full duration."
        return text

    # ------------------------------------------------------------------ schedule / settings
    def _schedule(self) -> tuple[Schedule | None, str | None]:
        tz = ScheduleTimeZone()  # the computer's local time, always
        repeat = 0 if self.until_stopped.isChecked() else int(self.repeat_spin.value())
        if self.run_now.isChecked():
            return Schedule(mode=MODE_NOW, tz=tz, repeat=repeat), None
        times = self.times_field.times()
        if not times:
            return None, "Add at least one start time."
        daily = self.daily_check.isChecked()
        chosen = self.date_field.date()
        if not daily and chosen is None:
            return None, "Pick a date or tick Repeat daily."
        dates = [] if daily else [chosen]
        return Schedule(mode=MODE_TIMES, times=times, dates=dates, daily=daily, tz=tz, repeat=1), None

    def _campaign_settings(self) -> CampaignSettings:
        info = getattr(self.host, "_sensor_info", None)
        layout = layout_from_import(self.imported) if self.imported is not None else None
        rows = plan_rows_from_import(self.imported) if self.imported is not None else []
        results_possible = bool(layout and (layout.result or layout.time or layout.duration))
        return CampaignSettings(
            campaign="Cellular",
            fallback_duration_s=self.duration_seconds(),
            wait_s=self.wait_seconds(),
            protocol_path=self.workbook_edit.text().strip(),
            protocol_sheet=self._current_sheet(),
            protocol_results=results_possible,
            protocol_layout=layout.to_dict() if layout is not None else {},
            plan_rows=[row.to_dict() for row in rows],
            sensor_model=str(getattr(info, "model", "") or "").replace("—", "").strip(),
            sensor_serial=str(getattr(info, "serial", "") or "").replace("—", "").strip(),
        )

    def _notes_before_start(self, schedule: Schedule | None) -> list[str]:
        notes: list[str] = []
        if self.plan is None:
            return notes
        split_rows = sorted({t.row for t in self.plan.tasks if t.window_count > 1})
        if split_rows:
            notes.append(
                f"{len(split_rows)} row(s) are wider than one capture at this duration and are recorded as sequential "
                f"frequency windows, each for the full {self.duration_seconds():g} s. The row lists all its files; the windows are "
                "recorded one after another, not at the same time."
            )
        if schedule is not None and schedule.mode == MODE_TIMES:
            warning = tight_schedule_warning(schedule, self.plan.estimated_round_seconds(self.wait_seconds()), now=datetime.now(timezone.utc))
            if warning:
                notes.append(warning)
        path = self.workbook_edit.text().strip()
        if path:
            try:
                from .excel_com import excel_has_workbook

                if excel_has_workbook(Path(path)):
                    notes.append(f"{Path(path).name} is open in Excel: results are written through Excel into the open workbook.")
            except Exception:  # noqa: BLE001 - advisory
                pass
        return notes

    def _show_preview(self, *, start: bool) -> None:
        if self.plan is None:
            QMessageBox.information(self, "Cellular Collection", "Load a protocol workbook first.")
            return
        self._rebuild_plan()
        schedule, error = self._schedule() if start else (None, None)
        if start and error:
            QMessageBox.warning(self, "Schedule", error)
            return
        dialog = PlanPreviewDialog(
            self.plan,
            wait_s=self.wait_seconds(),
            rows_total=len(self.imported.valid_entries()) if self.imported is not None else len({t.row for t in self.plan.tasks}),
            notes=self._notes_before_start(schedule),
            start_label="Start" if start else None,
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not start:
            return
        self._start(schedule)

    # ------------------------------------------------------------------ start / stop
    def _start_clicked(self) -> None:
        """Verify the sensor's identity first, then preview (which explains any subdivision) and start."""
        if self.plan is None:
            QMessageBox.information(self, "Cellular Collection", "Load a protocol workbook first.")
            return
        if self._identity_pending:
            return
        fetch = getattr(self.host, "fetch_sensor_identity", None)
        if fetch is None:
            self._show_preview(start=True)
            return
        self._identity_pending = True
        self.status_label.setText("Checking the sensor's identity…")
        self._sync_buttons(self.runner.status())

        def on_identity(info) -> None:
            self._identity_pending = False
            self._sync_buttons(self.runner.status())
            connected = bool(getattr(info, "connected", False)) or bool(getattr(info, "demo", False))
            model = str(getattr(info, "model", "") or "").replace("—", "").strip()
            serial = str(getattr(info, "serial", "") or "").replace("—", "").strip()
            if not connected or not model:
                self.status_label.setText("Idle")
                QMessageBox.warning(
                    self,
                    "Sensor not verified",
                    "The sensor's identity could not be read, so the recording settings cannot be tied to it. "
                    f"Check the connection and try again. ({getattr(info, 'status', '') or 'no answer'})",
                )
                return
            previous = self.settings.last_sensor or {}
            if previous.get("serial") and serial and previous["serial"] != serial:
                self._host_log(
                    f"The connected sensor is {readable_sensor_name(model)} {serial}; the last collection used "
                    f"{readable_sensor_name(previous.get('model', ''))} {previous['serial']}. The plan is rebuilt for this sensor.",
                    "error",
                )
            self._host_log(f"Sensor verified: {readable_sensor_name(model)} (model {model}, serial {serial}).")
            self._rebuild_plan()
            self._show_preview(start=True)

        fetch(on_identity)

    def _start(self, schedule: Schedule | None) -> None:
        if self.plan is None or schedule is None:
            return
        self._save_settings()
        settings = self._campaign_settings()
        self.settings.last_sensor = {"model": settings.sensor_model, "serial": settings.sensor_serial}
        save_panel_settings(self._config_dir, self.settings)
        error = self.runner.start_session(self.plan, schedule, settings, protocol_snapshot=self._snapshot_path)
        if error:
            QMessageBox.warning(self, "Cellular Collection", error)

    def _stop(self) -> None:
        status = self.runner.status()
        if status.get("state") in {S_RUNNING, S_PAUSE_REQUESTED}:
            answer = QMessageBox.question(
                self,
                "Stop collection",
                "The current recording finishes and is written to Excel; nothing else is recorded. Stop?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.runner.stop()

    # ------------------------------------------------------------------ status
    def _on_status(self, status: dict) -> None:
        state = status.get("state", S_IDLE)
        text = status.get("text", "")
        chip = {
            S_RUNNING: "Recording", S_WAITING: "Waiting", S_WAITING_SCHEDULE: "Scheduled", S_PAUSE_REQUESTED: "Pausing",
            S_PAUSED: "Paused", S_PAUSED_BACKLOG: "Paused", S_STOP_REQUESTED: "Stopping", S_INTERRUPTED: "Interrupted",
            S_DONE: "Finished", S_IDLE: "Off",
        }.get(state, state)
        self.chip.setText(chip)
        if not self._built:
            if status.get("active") or state in {S_INTERRUPTED, S_DONE} or status.get("missed_rounds"):
                self._set_expanded(True)
            return
        if status.get("active") or state in {S_INTERRUPTED, S_DONE}:
            if state == S_WAITING_SCHEDULE and status.get("next_round"):
                try:
                    when = datetime.fromisoformat(str(status["next_round"]))
                    local = when.astimezone() if when.tzinfo else when
                    if "at" in text:
                        text = f"Next pass at {format_datetime(local)}"
                except ValueError:
                    pass
            if not self._identity_pending:
                self.status_label.setText(text)
            details: list[str] = []
            if status.get("rounds_total", 0) > 1 and status.get("round_index"):
                details.append(f"pass {status['round_index']} of {status['rounds_total']}")
            pending = int(status.get("workbook_pending") or 0)
            wb_status = str(status.get("workbook_status") or "")
            if pending:
                details.append(f"{pending} Excel update(s) pending" + (" — the workbook is busy; written when it is free" if wb_status == "busy" else ""))
            elif wb_status == "error":
                details.append("Excel update failed — see the activity log")
            if status.get("pending_downloads"):
                details.append(f"{status['pending_downloads']} download(s) in progress")
            if status.get("pause_reason") and state in {S_PAUSED, S_PAUSED_BACKLOG}:
                details.append(str(status["pause_reason"]))
            self.detail_label.setText(" · ".join(details))
            self.detail_label.setVisible(bool(details))
        else:
            if not self._identity_pending and not self._importing:
                self.status_label.setText("Idle" if self.plan is None else "Ready")
            pending = int(status.get("workbook_pending") or 0)
            if pending:
                # Results left over from an earlier run are still waiting for the workbook.
                self.detail_label.setText(f"{pending} Excel update(s) pending from the last collection — written when the workbook is free")
                self.detail_label.setVisible(True)
            else:
                self.detail_label.setText("")
                self.detail_label.setVisible(False)
        self._sync_buttons(status)
        if state in {S_INTERRUPTED, S_WAITING_SCHEDULE, S_DONE} or status.get("missed_rounds"):
            self._refresh_recovery()

    def _sync_buttons(self, status: dict) -> None:
        if not self._built:
            return
        state = status.get("state", S_IDLE)
        active = bool(status.get("active"))
        ready = self.plan is not None and self.plan.ready and not self._importing
        self.start_btn.setEnabled(ready and not active and state != S_INTERRUPTED and not self._identity_pending)
        self.pause_btn.setEnabled(state in {S_RUNNING, S_WAITING, S_WAITING_SCHEDULE, S_PAUSED_BACKLOG})
        self.pause_btn.setVisible(state not in {S_PAUSED, S_PAUSED_BACKLOG, S_PAUSE_REQUESTED})
        self.resume_btn.setEnabled(state in {S_PAUSED, S_PAUSED_BACKLOG, S_PAUSE_REQUESTED})
        self.resume_btn.setVisible(state in {S_PAUSED, S_PAUSED_BACKLOG, S_PAUSE_REQUESTED})
        self.stop_btn.setEnabled(active)
        self.preview_btn.setEnabled(self.plan is not None and not self._importing)
        self.reload_btn.setEnabled(bool(self.workbook_edit.text().strip()) and not active and not self._importing)
        for widget in (self.browse_btn, self.sheet_combo, self.duration_spin, self.wait_spin, self.run_now, self.run_times, self.schedule_box, self.repeat_spin, self.until_stopped):
            widget.setEnabled(not active)
        if not active:
            self.repeat_spin.setEnabled(not self.until_stopped.isChecked())
            self.date_field.setEnabled(not self.daily_check.isChecked())

    def _refresh_recovery(self) -> None:
        if not self._built:
            if self.runner.session is not None:
                self._set_expanded(True)
            return
        session = self.runner.session
        status = self.runner.status()
        missed = status.get("missed_rounds") or []
        interrupted = self.runner.interrupted()
        if session is None or (not interrupted and not missed and status.get("state") != S_DONE):
            self.recovery.setVisible(False)
            return
        lines = []
        if interrupted:
            uncertain = self.runner.uncertain_tasks()
            lines.append(
                "The last collection was interrupted"
                + (f" ({session.pause_reason})" if session.pause_reason else "")
                + ". "
                + (f"{len(uncertain)} recording(s) may or may not exist on the sensor: check with the sensor before continuing, or skip them. " if uncertain else "")
                + "Nothing is recorded again on its own."
            )
        if missed:
            record = session.round(missed[0])
            when = format_datetime(record.scheduled_local.astimezone()) if record is not None else "its time"
            lines.append(f"A scheduled pass was missed at {when} (the application was closed or the computer asleep). Run it now or skip it.")
        if status.get("state") == S_DONE:
            lines.append(status.get("text") or "Finished.")
        self.recovery_label.setText("\n".join(lines))
        self.reconcile_btn.setVisible(interrupted)
        self.resume_session_btn.setVisible(interrupted)
        self.skip_uncertain_btn.setVisible(interrupted and bool(self.runner.uncertain_tasks()))
        self.run_missed_btn.setVisible(bool(missed))
        self.skip_missed_btn.setVisible(bool(missed))
        self.discard_btn.setVisible(not self.runner.active())
        self.recovery.setVisible(True)

    # ------------------------------------------------------------------ recovery actions
    def _reconcile(self) -> None:
        self.reconcile_btn.setEnabled(False)
        self.recovery_label.setText("Checking the sensor's files…")

        def done(message: str) -> None:
            self.reconcile_btn.setEnabled(True)
            self._host_log(message)
            self._refresh_recovery()
            QMessageBox.information(self, "Check with the sensor", message)

        self.runner.reconcile(done)

    def _resume_after_restart(self) -> None:
        error = self.runner.resume_after_restart()
        if error:
            QMessageBox.warning(self, "Continue", error)
        self._refresh_recovery()

    def _skip_uncertain(self) -> None:
        rid = self.runner.session.current_round_id if self.runner.session else ""
        for state, _planned in self.runner.uncertain_tasks():
            self.runner.skip_task(rid or state.round_id, state.task_key)
        self._refresh_recovery()

    def _run_missed(self) -> None:
        missed = self.runner.status().get("missed_rounds") or []
        if not missed:
            return
        error = self.runner.run_missed_round(missed[0])
        if error:
            QMessageBox.warning(self, "Missed pass", error)
        self._refresh_recovery()

    def _skip_missed(self) -> None:
        missed = self.runner.status().get("missed_rounds") or []
        if not missed:
            return
        self.runner.skip_missed_round(missed[0])
        self._refresh_recovery()

    def _discard(self) -> None:
        if self.runner.active():
            return
        self.runner.discard_session()
        self._refresh_recovery()

    def persist(self) -> None:
        """Called by the main window on close."""
        self._save_settings()
