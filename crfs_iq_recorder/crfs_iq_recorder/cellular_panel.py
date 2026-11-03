"""Excel Collection tab: record the rows of a protocol worksheet.

Workflow order, top to bottom: the last recording and the end-of-pass summary
(when there is one) → Excel file and worksheet → recording duration and wait →
run now or schedule (date, start times, repeat daily). The action bar
(Preview, Start, Pause / Resume, Stop) with a one-line status is a separate
widget so the main window can keep it in view under the scrolling form.
Everything heavy (workbook import, identity check, downloads, Excel updates)
runs off the UI thread through the host's job helper or the runner; this
module only draws and forwards. Diagnostics go to the diagnostic log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QPainter
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

from . import desktop_open
from .cellular_capabilities import capability_for, readable_sensor_name
from .cellular_pickers import DateField, TimesField, format_datetime
from .cellular_plan import REQUEST_SENTENCE, CellularPlan, build_plan, format_bytes, format_seconds, summary_line
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
    OWNER,
    CampaignSettings,
    CellularRunner,
)
from .cellular_scheduler import MODE_NOW, MODE_TIMES, Schedule, ScheduleTimeZone, tight_schedule_warning
from .cellular_workbook import layout_from_import, plan_rows_from_import
from .diagnostic_log import diag
from .json_store import read_json, write_json
from .operator_text import clip
from .result_card import ResultCard
from .theme import CONTROL_H, LABEL_W, restyle
from .waterfall_dialog import WaterfallDialog

SETTINGS_NAME = "settings.json"
DEFAULT_WAIT_S = 10.0
DEFAULT_DURATION_S = 5.0
MAX_START_TIMES = 12
SPIN_W = 124  # duration / wait / repeat spin boxes

NO_WORKBOOK = "Load a protocol workbook first."
STOP_TOOLTIP = "Stops after the current recording: it finishes and is written to Excel, nothing else is recorded."


# ----------------------------------------------------------------------------- settings
@dataclass
class PanelSettings:
    """What the panel remembers between runs (no secrets, no results)."""

    workbook: str = ""
    worksheet: str = ""
    duration_s: float = DEFAULT_DURATION_S
    wait_s: float = DEFAULT_WAIT_S  # always seconds; wait_unit is how it is shown
    wait_unit: str = "seconds"
    schedule_mode: str = MODE_NOW
    times: list[str] = field(default_factory=list)
    schedule_date: str = ""
    daily: bool = False
    repeat: int = 1
    until_stopped: bool = False
    last_sensor: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "workbook": self.workbook,
            "worksheet": self.worksheet,
            "duration_s": self.duration_s,
            "wait_s": self.wait_s,
            "wait_unit": self.wait_unit,
            "schedule_mode": self.schedule_mode,
            "times": list(self.times),
            "schedule_date": self.schedule_date,
            "daily": self.daily,
            "repeat": self.repeat,
            "until_stopped": self.until_stopped,
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
        settings.wait_unit = "minutes" if data.get("wait_unit") == "minutes" else "seconds"
        settings.schedule_mode = MODE_TIMES if data.get("schedule_mode") == MODE_TIMES else MODE_NOW
        settings.times = [str(t) for t in data.get("times") or []]
        settings.schedule_date = str(data.get("schedule_date") or "")
        settings.daily = bool(data.get("daily", False))
        try:
            settings.repeat = max(int(data.get("repeat", 1)), 1)
        except (TypeError, ValueError):
            settings.repeat = 1
        settings.until_stopped = bool(data.get("until_stopped", False))
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


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


# ----------------------------------------------------------------------------- small widgets
class ElideLabel(QLabel):
    """A one-line label that elides long text instead of widening its row; the
    full text is the tooltip."""

    def __init__(self, text: str = "", parent=None) -> None:
        super().__init__(text, parent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(40)

    def setText(self, text: str) -> None:  # noqa: N802
        super().setText(text)
        self.setToolTip(text if self.fontMetrics().horizontalAdvance(text) > 160 else "")

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(40, hint.height())

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        self.style().drawItemText(painter, rect, int(self.alignment()), self.palette(), self.isEnabled(), text, self.foregroundRole())


class FieldLabel(QLabel):
    """Right-aligned caption of a form row, sharing one column width with the main form."""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self.setMinimumWidth(LABEL_W)
        self.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(CONTROL_H)


def _ghost(text: str, *, tooltip: str = "") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName("ghost")
    button.setAutoDefault(False)
    button.setFixedHeight(CONTROL_H)
    if tooltip:
        button.setToolTip(tooltip)
    return button


# ----------------------------------------------------------------------------- preview
class PlanPreviewDialog(QDialog):
    """What will be requested, row by row, before Start: one request per protocol row."""

    def __init__(self, plan: CellularPlan, *, wait_s: float, rows_total: int, notes: list[str] | None = None, start_label: str | None = None, passes: int = 1, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Excel Collection")
        self.resize(760, 500)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        summary = QLabel(summary_line(plan, wait_s=wait_s, passes=passes))
        summary.setObjectName("summary")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        rule = QLabel(REQUEST_SENTENCE)
        rule.setWordWrap(True)
        layout.addWidget(rule)
        estimate = QLabel(
            f"Estimate only: about {format_bytes(plan.estimated_bytes)} and "
            f"{format_seconds(plan.estimated_round_seconds(wait_s))} per pass. "
            "What CRFS records is read from its files afterwards."
        )
        estimate.setObjectName("muted")
        estimate.setWordWrap(True)
        layout.addWidget(estimate)
        for text in notes or []:
            if text:
                note = QLabel(text)
                note.setWordWrap(True)
                note.setObjectName("muted")
                layout.addWidget(note)
        tree = QTreeWidget()
        tree.setColumnCount(5)
        tree.setHeaderLabels(["Row", "Band", "Direction", "Requested range", "Duration"])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        tree.setEditTriggers(QTreeWidget.EditTrigger.NoEditTriggers)
        for task in plan.tasks:
            requested = task.range_text
            if task.repetitions > 1:
                requested += f" (repeat {task.repetition} of {task.repetitions})"
            item = QTreeWidgetItem([str(task.row), task.band_label, task.direction, requested, f"{task.duration_s:g} s"])
            tree.addTopLevelItem(item)
        header = tree.header()
        header.setStretchLastSection(False)
        for column in (0, 1, 2, 4):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(tree, 1)
        problems = [i for i in plan.issues if i.level in {"error", LEVEL_WARNING, "info"}]
        if problems:
            box = QPlainTextEdit()
            box.setReadOnly(True)
            box.setMaximumHeight(110)
            box.setPlainText("\n".join(i.message for i in problems))
            layout.addWidget(box)
        buttons = QDialogButtonBox()
        if start_label:
            ok = buttons.addButton(start_label, QDialogButtonBox.ButtonRole.AcceptRole)
            ok.setObjectName("primary")
            ok.setEnabled(plan.ready)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


# ----------------------------------------------------------------------------- panel
class CellularPanel(QFrame):
    """The form of the "Excel Collection" tab.

    ``action_bar`` (state chip, status line, Preview / Start / Pause / Resume /
    Stop) is created at once; the host takes it with ``detach_action_bar()``
    and keeps it under the scrolling form. The form itself is built the first
    time it is needed (``ensure_built()``), so a large saved workbook is not
    read while the window opens on the other tab.
    """

    CHIPS = {
        S_RUNNING: ("Recording", "badgeAccent"),
        S_WAITING: ("Waiting", "badgeAccent"),
        S_WAITING_SCHEDULE: ("Scheduled", "badgeAccent"),
        S_PAUSE_REQUESTED: ("Pausing", "badgeWait"),
        S_PAUSED: ("Paused", "badgeWait"),
        S_PAUSED_BACKLOG: ("Paused", "badgeWait"),
        S_STOP_REQUESTED: ("Stopping", "badgeWait"),
        S_INTERRUPTED: ("Interrupted", "badgeBad"),
        S_DONE: ("Finished", "badgeOk"),
    }

    def __init__(self, runner: CellularRunner, *, host, config_dir: Path, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("section")
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
        self._host_paused = False
        self._built = False
        self._details_open = False
        self._pass_dismissed = ""
        self._waterfall = None
        self._build_frame()
        self._build_action_bar()
        self.runner.status_changed.connect(self._on_status)
        self.runner.session_changed.connect(self._refresh_recovery)
        self.runner.log.connect(self._on_runner_log)
        if self.runner.session is not None:
            self.ensure_built()
        else:
            self._on_status(self.runner.status())

    # ------------------------------------------------------------------ construction
    def _build_frame(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(6)
        self.body = QWidget()
        self.body.setObjectName("section")
        self.body.setVisible(False)
        outer.addWidget(self.body)
        self._bar_slot = outer
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

    def _build_action_bar(self) -> None:
        bar = QFrame()
        bar.setObjectName("actionBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(8)
        self.chip = QLabel("Idle")
        self.chip.setObjectName("badgeIdle")
        self.chip.setToolTip("State of the collection")
        row.addWidget(self.chip, 0, Qt.AlignmentFlag.AlignVCenter)
        texts = QVBoxLayout()
        texts.setContentsMargins(0, 0, 0, 0)
        texts.setSpacing(1)
        self.status_label = ElideLabel("Idle")
        self.status_label.setObjectName("summary")
        self.detail_label = ElideLabel("")
        self.detail_label.setObjectName("muted")
        self.detail_label.setVisible(False)
        # Why Start is not available right now ("Manual recording is running.").
        self.reason_label = ElideLabel("")
        self.reason_label.setObjectName("reasonText")
        self.reason_label.setVisible(False)
        texts.addWidget(self.status_label)
        texts.addWidget(self.detail_label)
        texts.addWidget(self.reason_label)
        row.addLayout(texts, 1)
        self.preview_btn = _ghost("Preview", tooltip="The rows that will be recorded, in order, with the requested range of each")
        self.preview_btn.clicked.connect(lambda: self._show_preview(start=False))
        self.start_btn = QPushButton("Start")
        self.start_btn.setObjectName("primary")
        self.start_btn.setAutoDefault(False)
        self.start_btn.setMinimumWidth(96)
        self.start_btn.setAccessibleName("Start Excel Collection")
        self.start_btn.setToolTip("Check the connected sensor, show the preview, then record the rows")
        self.start_btn.clicked.connect(self._start_clicked)
        self.pause_btn = _ghost("Pause", tooltip="Pauses after the current recording")
        self.pause_btn.clicked.connect(self.runner.pause)
        self.resume_btn = _ghost("Resume", tooltip="Continue with the next recording")
        self.resume_btn.clicked.connect(self.runner.resume)
        self.stop_btn = _ghost("Stop", tooltip=STOP_TOOLTIP)
        self.stop_btn.clicked.connect(self._stop)
        for button in (self.preview_btn, self.start_btn, self.pause_btn, self.resume_btn, self.stop_btn):
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.action_bar = bar
        self._bar_slot.addWidget(bar)

    def detach_action_bar(self) -> QWidget:
        """Hand the action bar to the host (it is removed from the panel's own layout)."""
        self._bar_slot.removeWidget(self.action_bar)
        self.action_bar.setParent(None)
        return self.action_bar

    def ensure_built(self) -> None:
        """Build the form (first use of the tab, or a saved collection to show)."""
        if self._built:
            return
        self._built = True
        self._build_body()
        self.body.setVisible(True)
        self._load_saved_workbook()
        self._refresh_recovery()
        self._on_status(self.runner.status())

    def host_state_changed(self) -> None:
        """The main window's manual recording started or ended: Start follows."""
        self._sync_buttons(self.runner.status())
        self._sync_log_note()

    def host_reason(self) -> str:
        """What the manual tab says while a paused collection still holds the sensor ("" otherwise)."""
        if self.runner.state in {S_PAUSED, S_PAUSED_BACKLOG}:
            return f"{OWNER} is paused. Stop it first."
        return ""

    def _build_body(self) -> None:
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 2, 0, 4)
        body.setSpacing(10)

        # Recovery notice (interrupted session, missed pass) — at the top, where it is seen first.
        self.recovery = QFrame()
        self.recovery.setObjectName("notice")
        rec = QVBoxLayout(self.recovery)
        rec.setContentsMargins(12, 10, 12, 10)
        rec.setSpacing(8)
        self.recovery_label = QLabel("")
        self.recovery_label.setWordWrap(True)
        rec.addWidget(self.recovery_label)
        rec_buttons = QHBoxLayout()
        rec_buttons.setSpacing(8)
        self.reconcile_btn = _ghost("Check with the sensor")
        self.reconcile_btn.clicked.connect(self._reconcile)
        self.resume_session_btn = _ghost("Continue")
        self.resume_session_btn.clicked.connect(self._resume_after_restart)
        self.skip_uncertain_btn = _ghost("Skip uncertain")
        self.skip_uncertain_btn.clicked.connect(self._skip_uncertain)
        self.run_missed_btn = _ghost("Run the missed pass now")
        self.run_missed_btn.clicked.connect(self._run_missed)
        self.skip_missed_btn = _ghost("Skip it")
        self.skip_missed_btn.clicked.connect(self._skip_missed)
        self.discard_btn = _ghost("Dismiss")
        self.discard_btn.clicked.connect(self._discard)
        for button in (self.reconcile_btn, self.resume_session_btn, self.skip_uncertain_btn, self.run_missed_btn, self.skip_missed_btn, self.discard_btn):
            rec_buttons.addWidget(button)
        rec_buttons.addStretch(1)
        rec.addLayout(rec_buttons)
        self.recovery.setVisible(False)
        body.addWidget(self.recovery)

        # End of a pass: counts and "Retry failed rows".
        self.pass_box = QFrame()
        self.pass_box.setObjectName("resultCard")
        pass_l = QVBoxLayout(self.pass_box)
        pass_l.setContentsMargins(12, 10, 12, 10)
        pass_l.setSpacing(6)
        self.pass_title = QLabel("")
        self.pass_title.setObjectName("resultTitle")
        self.pass_counts = QLabel("")
        self.pass_counts.setWordWrap(True)
        pass_l.addWidget(self.pass_title)
        pass_l.addWidget(self.pass_counts)
        pass_buttons = QHBoxLayout()
        pass_buttons.setSpacing(8)
        self.retry_btn = _ghost(
            "Retry failed rows",
            tooltip="Checks uncertain rows with the sensor first, then sends the failed rows again. Completed and partial rows are kept.",
        )
        self.retry_btn.clicked.connect(self._retry_failed)
        self.pass_hint = QLabel("")
        self.pass_hint.setObjectName("muted")
        self.pass_hint.setWordWrap(True)
        self.pass_dismiss_btn = _ghost("Dismiss", tooltip="Hide this summary")
        self.pass_dismiss_btn.clicked.connect(self._dismiss_pass)
        pass_buttons.addWidget(self.retry_btn)
        pass_buttons.addWidget(self.pass_hint, 1)
        pass_buttons.addStretch(0)  # takes the width while the hint is hidden
        pass_buttons.addWidget(self.pass_dismiss_btn)
        pass_l.addLayout(pass_buttons)
        self.pass_box.setVisible(False)
        body.addWidget(self.pass_box)

        self.result_card = ResultCard("Last recording")
        self.result_card.open_folder_clicked.connect(self._open_result_folder)
        self.result_card.waterfall_clicked.connect(self._show_result_waterfall)
        body.addWidget(self.result_card)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)
        grid.setColumnMinimumWidth(0, LABEL_W)
        grid.setColumnStretch(1, 1)
        row = 0

        # 1. Excel file and worksheet
        grid.addWidget(FieldLabel("Excel file"), row, 0)
        file_row = QHBoxLayout()
        file_row.setSpacing(8)
        self.workbook_edit = QLineEdit()
        self.workbook_edit.setPlaceholderText("Protocol workbook (.xlsx / .xlsm)")
        self.workbook_edit.setReadOnly(True)
        self.workbook_edit.setFixedHeight(CONTROL_H)
        self.workbook_edit.setToolTip("The protocol workbook; each row of the worksheet is one recording")
        self.browse_btn = _ghost("Browse…", tooltip="Choose the protocol workbook")
        self.browse_btn.clicked.connect(self._browse_workbook)
        self.reload_btn = _ghost("Reload", tooltip="Read the worksheet again (after editing the protocol)")
        self.reload_btn.clicked.connect(lambda: self._import(force=True))
        file_row.addWidget(self.workbook_edit, 1)
        file_row.addWidget(self.browse_btn)
        file_row.addWidget(self.reload_btn)
        grid.addLayout(file_row, row, 1)
        row += 1

        grid.addWidget(FieldLabel("Worksheet"), row, 0)
        sheet_row = QHBoxLayout()
        sheet_row.setSpacing(8)
        self.sheet_combo = QComboBox()
        self.sheet_combo.setFixedHeight(CONTROL_H)
        self.sheet_combo.setMinimumWidth(220)
        self.sheet_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.sheet_combo.setToolTip("The worksheet whose rows are recorded, in their order")
        self.sheet_combo.currentIndexChanged.connect(self._on_sheet_changed)
        sheet_row.addWidget(self.sheet_combo)
        sheet_row.addStretch(1)
        grid.addLayout(sheet_row, row, 1)
        row += 1

        summary_row = QHBoxLayout()
        summary_row.setSpacing(8)
        self.import_label = QLabel("Load the protocol workbook.")
        self.import_label.setObjectName("muted")
        self.import_label.setWordWrap(True)
        self.import_label.setMinimumHeight(24)
        self.details_btn = QPushButton("Details")
        self.details_btn.setObjectName("linkBtn")
        self.details_btn.setAutoDefault(False)
        self.details_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_btn.setToolTip("Capture window of the sensor, sequential windows and other notes about this plan")
        self.details_btn.setVisible(False)
        self.details_btn.clicked.connect(lambda: self._show_details(not self._details_open))
        summary_row.addWidget(self.import_label, 1)
        summary_row.addWidget(self.details_btn, 0, Qt.AlignmentFlag.AlignTop)
        grid.addLayout(summary_row, row, 1)
        row += 1
        self.details_label = QLabel("")
        self.details_label.setObjectName("muted")
        self.details_label.setWordWrap(True)
        self.details_label.setVisible(False)
        grid.addWidget(self.details_label, row, 1)
        row += 1

        # 2. Duration and wait
        grid.addWidget(FieldLabel("Recording duration"), row, 0)
        duration_row = QHBoxLayout()
        duration_row.setSpacing(8)
        self.duration_spin = QDoubleSpinBox()
        self.duration_spin.setDecimals(1)
        self.duration_spin.setRange(0.5, 3600.0)
        self.duration_spin.setSingleStep(1.0)
        self.duration_spin.setSuffix(" s")
        self.duration_spin.setFixedSize(SPIN_W, CONTROL_H)
        self.duration_spin.setToolTip("Seconds recorded for every row")
        self.duration_spin.setValue(self.settings.duration_s if self.settings.duration_s >= 0.5 else DEFAULT_DURATION_S)
        self.duration_spin.valueChanged.connect(self._rebuild_plan)
        duration_row.addWidget(self.duration_spin)
        duration_hint = QLabel("per row")
        duration_hint.setObjectName("muted")
        duration_row.addWidget(duration_hint)
        duration_row.addStretch(1)
        grid.addLayout(duration_row, row, 1)
        row += 1

        grid.addWidget(FieldLabel("Wait between recordings"), row, 0)
        wait_row = QHBoxLayout()
        wait_row.setSpacing(8)
        self.wait_spin = QDoubleSpinBox()
        self.wait_spin.setDecimals(1)
        self.wait_spin.setSingleStep(1.0)
        self.wait_spin.setFixedSize(SPIN_W, CONTROL_H)
        self.wait_spin.setToolTip("Pause after each recording before the next row")
        self.wait_unit = QComboBox()
        self.wait_unit.addItems(["Seconds", "Minutes"])
        self.wait_unit.setFixedHeight(CONTROL_H)
        self.wait_unit.setMinimumWidth(112)
        minutes = self.settings.wait_unit == "minutes"
        self.wait_unit.setCurrentText("Minutes" if minutes else "Seconds")
        self._apply_wait_range(minutes)
        self.wait_spin.setValue(self.settings.wait_s / 60.0 if minutes else self.settings.wait_s)
        self.wait_unit.currentTextChanged.connect(self._on_wait_unit)
        self.wait_spin.valueChanged.connect(self._rebuild_plan)
        wait_row.addWidget(self.wait_spin)
        wait_row.addWidget(self.wait_unit)
        wait_row.addStretch(1)
        grid.addLayout(wait_row, row, 1)
        row += 1

        # The manual tab's recording log (.xlsm) also receives these recordings when it is on.
        self.log_note = QLabel("")
        self.log_note.setObjectName("muted")
        self.log_note.setWordWrap(True)
        self.log_note.setVisible(False)
        grid.addWidget(self.log_note, row, 1)
        row += 1

        # 3. Run now / schedule
        grid.addWidget(FieldLabel("Start"), row, 0)
        mode_row = QHBoxLayout()
        mode_row.setSpacing(20)
        self.run_now = QRadioButton("Run now")
        self.run_now.setToolTip("Record the list as soon as you press Start")
        self.run_times = QRadioButton("Schedule")
        self.run_times.setToolTip("Record the list at chosen times of day (this computer's local time)")
        (self.run_times if self.settings.schedule_mode == MODE_TIMES else self.run_now).setChecked(True)
        self.run_now.toggled.connect(lambda on: self._on_schedule_mode(not on))
        mode_row.addWidget(self.run_now)
        mode_row.addWidget(self.run_times)
        mode_row.addStretch(1)
        grid.addLayout(mode_row, row, 1)
        row += 1

        # Run now: how many passes.
        self.repeat_label = FieldLabel("Repeat")
        grid.addWidget(self.repeat_label, row, 0)
        repeat_row = QHBoxLayout()
        repeat_row.setSpacing(12)
        self.repeat_spin = QSpinBox()
        self.repeat_spin.setRange(1, 999)
        self.repeat_spin.setSuffix(" pass(es)")
        self.repeat_spin.setFixedSize(SPIN_W + 16, CONTROL_H)
        self.repeat_spin.setToolTip("How many times the whole list is recorded, back to back")
        self.repeat_spin.setValue(self.settings.repeat)
        repeat_row.addWidget(self.repeat_spin)
        self.until_stopped = QCheckBox("Until stopped")
        self.until_stopped.setChecked(self.settings.until_stopped)
        self.until_stopped.toggled.connect(lambda on: self.repeat_spin.setEnabled(not on))
        self.repeat_spin.setEnabled(not self.settings.until_stopped)
        # The summary line counts passes.
        self.repeat_spin.valueChanged.connect(self._rebuild_plan)
        self.until_stopped.toggled.connect(self._rebuild_plan)
        repeat_row.addWidget(self.until_stopped)
        repeat_row.addStretch(1)
        self.repeat_box = QWidget()
        self.repeat_box.setObjectName("section")
        repeat_host = QHBoxLayout(self.repeat_box)
        repeat_host.setContentsMargins(0, 0, 0, 0)
        repeat_host.addLayout(repeat_row)
        grid.addWidget(self.repeat_box, row, 1)
        row += 1

        # Schedule: date, start times, repeat daily (only when Schedule is selected).
        self.schedule_box = QWidget()
        self.schedule_box.setObjectName("section")
        schedule = QGridLayout(self.schedule_box)
        schedule.setContentsMargins(0, 0, 0, 0)
        schedule.setHorizontalSpacing(12)
        schedule.setVerticalSpacing(10)
        schedule.setColumnMinimumWidth(0, LABEL_W)
        schedule.setColumnStretch(1, 1)
        schedule.addWidget(FieldLabel("Date"), 0, 0)
        try:
            saved_date = date.fromisoformat(self.settings.schedule_date) if self.settings.schedule_date else None
        except ValueError:
            saved_date = None
        if saved_date is not None and saved_date < date.today():
            saved_date = None
        date_row = QHBoxLayout()
        date_row.setSpacing(16)
        self.date_field = DateField(saved_date or date.today(), minimum=date.today())
        self.date_field.setFixedWidth(200)
        self.date_field.setToolTip("The day of the scheduled pass (click to open the calendar)")
        date_row.addWidget(self.date_field)
        self.daily_check = QCheckBox("Repeat daily")
        self.daily_check.setToolTip("Record at the start times every day until stopped")
        self.daily_check.setChecked(self.settings.daily)
        self.daily_check.toggled.connect(self._on_daily)
        date_row.addWidget(self.daily_check)
        date_row.addStretch(1)
        schedule.addLayout(date_row, 0, 1)
        times_label = FieldLabel("Start times")
        times_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        schedule.addWidget(times_label, 1, 0, alignment=Qt.AlignmentFlag.AlignTop)
        saved_times = [t for t in (_parse_time(x) for x in self.settings.times) if t is not None]
        self.times_field = TimesField(saved_times, maximum=MAX_START_TIMES)
        self.times_field.setToolTip("One pass starts at each time; times are this computer's local time")
        schedule.addWidget(self.times_field, 1, 1)
        grid.addWidget(self.schedule_box, row, 0, 1, 2)
        row += 1

        body.addLayout(grid)
        self._on_schedule_mode(self.run_times.isChecked())
        self._on_daily(self.daily_check.isChecked())

    # ------------------------------------------------------------------ small helpers
    def _apply_wait_range(self, minutes: bool) -> None:
        self.wait_spin.setRange(0.0, 24 * 60.0 if minutes else 24 * 3600.0)

    def _on_wait_unit(self, text: str) -> None:
        """Switching units keeps the same wait: 90 s becomes 1.5 min."""
        minutes = text == "Minutes"
        seconds = self._wait_from_widgets(not minutes)  # the spin box still shows the previous unit
        self.wait_spin.blockSignals(True)
        self._apply_wait_range(minutes)
        self.wait_spin.setValue(seconds / 60.0 if minutes else seconds)
        self.wait_spin.blockSignals(False)
        self._rebuild_plan()

    def _wait_from_widgets(self, minutes: bool) -> float:
        value = max(float(self.wait_spin.value()), 0.0)
        return value * 60.0 if minutes else value

    def _sync_log_note(self) -> None:
        if not self._built:
            return
        target = getattr(self.host, "excel_logging_target", None)
        try:
            found = target() if callable(target) else None
        except Exception:  # noqa: BLE001 - advisory line only
            found = None
        name = Path(found[0]).name if found else ""
        self.log_note.setText(
            f"Each recording is also logged to {name} (the Excel log option of the Manual Recording tab)." if name else ""
        )
        self.log_note.setVisible(bool(name))

    def _set_chip(self, text: str, style: str) -> None:
        self.chip.setText(text)
        if self.chip.objectName() != style:
            self.chip.setObjectName(style)
            restyle(self.chip)

    def _show_details(self, on: bool) -> None:
        self._details_open = bool(on)
        self.details_label.setVisible(self._details_open and bool(self.details_label.text()))
        self.details_btn.setText("Hide details" if self._details_open else "Details")

    def _save_settings(self) -> None:
        if self._built:
            self.settings.workbook = self.workbook_edit.text().strip()
            self.settings.worksheet = self._current_sheet()
            self.settings.duration_s = float(self.duration_spin.value())
            self.settings.wait_s = self.wait_seconds()
            self.settings.wait_unit = "minutes" if self.wait_unit.currentText() == "Minutes" else "seconds"
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
        if not self._built:
            return float(self.settings.wait_s)
        return self._wait_from_widgets(self.wait_unit.currentText() == "Minutes")

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
            log(message, level=level)
        except TypeError:
            try:
                log(message)
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
        self.repeat_box.setVisible(not times_on)

    def _on_daily(self, daily: bool) -> None:
        self.date_field.setEnabled(not daily)
        self.date_field.setToolTip("Every day from today" if daily else "The day of the scheduled pass (click to open the calendar)")

    # ------------------------------------------------------------------ protocol
    def _load_saved_workbook(self) -> None:
        saved = self.settings.workbook
        if not saved:
            return
        if Path(saved).is_file():
            self.workbook_edit.setText(saved)
            self._fill_sheets(self.settings.worksheet)
            self._import(force=False, quiet=True)
            return
        # The saved file is gone (moved, renamed, drive unplugged): say so once, keep the setting.
        diag("protocol_missing", Path(saved).name, worksheet=self.settings.worksheet)
        self.workbook_edit.setText(saved)
        self._set_import_text("Protocol workbook not found. Choose the file again.", warn=True)
        self.workbook_edit.setToolTip(f"Not found: {Path(saved).name}")

    def _browse_workbook(self) -> None:
        start = self.workbook_edit.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Protocol workbook", start, "Excel workbooks (*.xlsx *.xlsm);;All files (*)")
        if not path:
            return
        self.workbook_edit.setText(path)
        self.workbook_edit.setToolTip("The protocol workbook; each row of the worksheet is one recording")
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
                self._set_import_text(self._kept_text(kept, path, exc))
            elif isinstance(exc, WorkbookUnavailable):
                diag("protocol_unavailable", Path(path).name, error=str(exc))
                self._set_import_text(self._unavailable_text(path, exc), warn=True)
            else:
                diag("protocol_error", Path(path).name, error=str(exc))
                self._set_import_text(clip(str(exc), 160), warn=True)
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
    def _unavailable_text(path: str, error: Exception) -> str:
        name = Path(path).name or "The workbook"
        if "not found" in str(error).casefold():
            return f"Protocol workbook not found: {name}. Choose the file again."
        return f"{name} cannot be opened right now. Is it open in another program?"

    def _kept_text(self, kept: ProtocolImport, path: str, error: Exception) -> str:
        return (
            f"{self._unavailable_text(path, error)} Using the {_plural(len(kept.valid_entries()), 'row')} "
            "imported earlier; results are queued."
        )

    def _set_import_text(self, text: str, *, warn: bool = False, details: str = "") -> None:
        """The compact summary under the worksheet; long explanations go to Details."""
        self.import_label.setText(text)
        name = "warn" if warn else ("summary" if self.plan is not None else "muted")
        if self.import_label.objectName() != name:
            self.import_label.setObjectName(name)
            restyle(self.import_label)
        self.details_label.setText(details)
        self.details_btn.setVisible(bool(details))
        self.details_label.setVisible(self._details_open and bool(details))

    def _import(self, *, force: bool, quiet: bool = False) -> None:
        """Read the worksheet (off the UI thread when the host offers a job helper)."""
        path = self.workbook_edit.text().strip()
        sheet = self._current_sheet()
        if not path or not sheet:
            return
        self._import_token += 1
        token = self._import_token
        self._importing = True
        self._set_import_text("Reading the worksheet…")
        self._sync_buttons(self.runner.status())

        def work():
            try:
                headers, _row = read_headers(path, sheet)
                mapping = suggest_mapping(headers)
                if mapping.missing():
                    return {
                        "error": "This worksheet cannot be used as a protocol: missing " + "; ".join(mapping.missing()) + ".",
                        "short": "This worksheet cannot be used as a protocol: it has no start/stop frequency columns.",
                    }
                return {"imported": import_protocol(path, sheet, mapping)}
            except WorkbookUnavailable as exc:
                diag("protocol_unavailable", Path(path).name, error=str(exc))
                return {"error": self._unavailable_text(path, exc), "unavailable": exc}
            except ProtocolError as exc:
                diag("protocol_error", Path(path).name, sheet=sheet, error=str(exc))
                return {"error": str(exc), "short": clip(str(exc), 120)}
            except Exception as exc:  # noqa: BLE001 - reported, never raised across threads
                diag("protocol_failed", Path(path).name, sheet=sheet, error=f"{type(exc).__name__}: {exc}")
                return {"error": "The worksheet could not be read. See details."}

        def apply(result) -> None:
            if token != self._import_token:
                return
            self._importing = False
            if not isinstance(result, dict) or result.get("error"):
                error = result.get("unavailable") if isinstance(result, dict) else None
                kept = self._kept_import(path, error) if error is not None else None
                if kept is not None and kept.worksheet == sheet:
                    # Locked or unplugged right now: the plan imported earlier stands.
                    self._set_import_text(self._kept_text(kept, path, error), warn=True, details=self._details_text())
                    if not quiet:
                        self._host_log(self.import_label.text(), "error")
                    self._sync_buttons(self.runner.status())
                    return
                self.imported = None
                self.plan = None
                text = str(result.get("error") if isinstance(result, dict) else "The worksheet could not be read.")
                self._set_import_text(text, warn=True)
                if not quiet:
                    short = result.get("short") if isinstance(result, dict) else None
                    self._host_log(str(short or text), "error")
                self._sync_buttons(self.runner.status())
                self._on_status(self.runner.status())
                return
            self.imported = result["imported"]
            try:
                self._snapshot_path = str(save_snapshot(self.imported, self._config_dir))
            except Exception:  # noqa: BLE001 - snapshot failure must not block the preview
                self._snapshot_path = ""
            self._rebuild_plan()
            self._save_settings()
            if not quiet:
                rows = len(self.imported.valid_entries())
                self._host_log(f"Protocol loaded: {self.imported.worksheet}, {_plural(rows, 'row')}.")
            self._on_status(self.runner.status())

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
        errors = len(self.imported.errors) + len(self.plan.errors)
        text = self.summary_text()
        if errors:
            text += " · " + _plural(errors, "problem") + " (see Preview)"
        self._set_import_text(text, warn=bool(errors), details=self._details_text(capability))
        self._on_status(self.runner.status())

    def passes(self) -> int:
        """Passes the current settings ask for (0 = until stopped)."""
        if not self._built:
            return max(int(self.settings.repeat), 1)
        if self.run_now.isChecked():
            return 0 if self.until_stopped.isChecked() else int(self.repeat_spin.value())
        if self.daily_check.isChecked():
            return 0
        return max(len(self.times_field.times()), 1)

    def summary_text(self) -> str:
        """'15 rows · 2 s per request · 10 s between recordings · 1 pass'."""
        if self.plan is None:
            return ""
        return summary_line(self.plan, wait_s=self.wait_seconds(), passes=self.passes())

    def _details_text(self, capability=None) -> str:
        """Estimate, kept result rows and the sensor the plan was checked against."""
        if self.plan is None or self.imported is None:
            return ""
        capability = capability or self._capability()
        lines: list[str] = [
            f"Rows are sent in worksheet order. Estimate only: about {format_bytes(self.plan.estimated_bytes)} "
            f"per pass; what CRFS records is read from its files afterwards."
        ]
        if self._sensor_model():
            name = readable_sensor_name(capability.model) or capability.model or "this sensor"
            lines.append(f"Checked against {name}.")
        else:
            lines.append("The sensor is checked when you press Start.")
        results = len(self.imported.result_rows())
        if results:
            lines.append(f"{_plural(results, 'earlier result row')} kept, not recorded again.")
        layout = layout_from_import(self.imported)
        if not (layout.result or layout.time or layout.duration):
            lines.append("No result column in this worksheet: results stay in the log and the record files.")
        return " ".join(lines)

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
            QMessageBox.information(self, "Excel Collection", "Load a protocol workbook first.")
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
            passes=self.passes(),
            parent=self.window(),
        )
        try:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        finally:
            dialog.deleteLater()
        if not accepted or not start:
            return
        self._start(schedule)

    # ------------------------------------------------------------------ start / stop
    def _start_clicked(self) -> None:
        """Verify the sensor's identity first, then preview (which explains any subdivision) and start."""
        if self.plan is None:
            QMessageBox.information(self, "Excel Collection", "Load a protocol workbook first.")
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
            diag("sensor_identity", "before Excel Collection", model=model, serial=serial, previous=previous)
            if previous.get("serial") and serial and previous["serial"] != serial:
                self._host_log("This is not the sensor of the last collection. The plan is checked again for it.", "error")
            self._host_log(f"Sensor checked: {readable_sensor_name(model)}, serial {serial}.")
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
            QMessageBox.warning(self, "Excel Collection", error)
        else:
            self._pass_dismissed = ""

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
        paused = state in {S_PAUSED, S_PAUSED_BACKLOG}
        if paused != self._host_paused:
            self._host_paused = paused
            sync = getattr(self.host, "_sync_other_tab_reasons", None)
            if callable(sync):
                sync()
        if state in self.CHIPS:
            chip_text, chip_style = self.CHIPS[state]
            if state == S_DONE and text.startswith("Stopped"):
                chip_text, chip_style = "Stopped", "badgeWait"  # stopped by the operator, not finished
            elif state == S_DONE and text.startswith("Finished with problems"):
                chip_style = "badgeWait"
            self._set_chip(chip_text, chip_style)
        elif self.plan is not None and self.plan.ready and not self._importing:
            self._set_chip("Ready", "badgeOk")
        else:
            self._set_chip("Idle", "badgeIdle")
        if not self._built:
            if status.get("active") or state in {S_INTERRUPTED, S_DONE} or status.get("missed_rounds"):
                self.ensure_built()
            else:
                self._sync_bar_texts(status)
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
            # The status line may already say it ("Paused: Downloads: 10 waiting (limit 8)…").
            details.extend(item for item in self._background_details(status) if item not in text)
            reason = str(status.get("pause_reason") or "")
            if reason and state in {S_PAUSED, S_PAUSED_BACKLOG} and reason not in text:
                details.append(reason)
            self.detail_label.setText(" · ".join(details))
            self.detail_label.setVisible(bool(details))
        else:
            self._sync_bar_texts(status)
        self._sync_buttons(status)
        self._update_results(status)
        if state in {S_INTERRUPTED, S_WAITING_SCHEDULE, S_DONE} or status.get("missed_rounds"):
            self._refresh_recovery()

    @staticmethod
    def _background_details(status: dict) -> list[str]:
        """'Downloads: 2 waiting' and the Excel state; recording progress is the status line's job."""
        out: list[str] = []
        downloads = int(status.get("pending_downloads") or 0)
        if downloads:
            out.append(f"Downloads: {downloads} waiting")
        pending = int(status.get("workbook_pending") or 0)
        wb_status = str(status.get("workbook_status") or "")
        if pending:
            out.append(f"Excel updates pending: {pending}" + (" (the workbook is busy)" if wb_status == "busy" else ""))
        elif wb_status == "error":
            out.append("Excel update failed; trying again")
        return out

    def _sync_bar_texts(self, status: dict) -> None:
        """Status line while no collection runs."""
        if not self._identity_pending and not self._importing:
            self.status_label.setToolTip(self.summary_text())
            if self.plan is None:
                self.status_label.setText("Load a protocol workbook")
            elif self.plan.ready:
                # Short enough for the pinned bar; the full summary line is in the form.
                self.status_label.setText(f"Ready to start · {_plural(len(self.plan.tasks), 'row')}")
            else:
                self.status_label.setText("Fix the problems listed in Preview before starting")
        details = self._background_details(status)
        self.detail_label.setText(" · ".join(details))
        self.detail_label.setVisible(bool(details))
        self._sync_buttons(status)

    # ------------------------------------------------------------------ results
    def _update_results(self, status: dict) -> None:
        if not self._built:
            return
        self.result_card.set_view(self.runner.last_task_view())
        round_id = str(status.get("last_round_id") or "")
        counts = status.get("pass_counts") or {}
        finished = bool(round_id) and not status.get("round_id") and any(counts.values())
        if not finished or round_id == self._pass_dismissed:
            self.pass_box.setVisible(False)
            return
        session = self.runner.session
        index = next((i + 1 for i, r in enumerate(session.rounds) if r.round_id == round_id), 0) if session else 0
        self.pass_title.setText(f"Pass {index} finished" if index else "Pass finished")
        self.pass_counts.setText(str(status.get("pass_summary") or ""))
        failed = int(counts.get("failed", 0) or 0)
        uncertain = int(counts.get("uncertain", 0) or 0)
        active = bool(status.get("active"))
        legacy = bool(status.get("legacy"))
        can_retry = (failed or uncertain) and not active and not legacy
        self.retry_btn.setVisible(bool(failed or uncertain))
        self.retry_btn.setEnabled(bool(can_retry))
        if active and (failed or uncertain):
            hint = "Available when the collection has stopped."
        elif uncertain and not failed:
            hint = "Uncertain rows are checked with the sensor first; a row whose file is found is not sent again."
        elif int(counts.get("partial", 0) or 0):
            hint = "Partial rows are kept and not retried."
        else:
            hint = ""
        self.pass_hint.setText(hint)
        self.pass_hint.setVisible(bool(hint))
        self.pass_box.setVisible(True)

    def _retry_failed(self) -> None:
        round_id = str(self.runner.status().get("last_round_id") or "")
        self.retry_btn.setEnabled(False)
        self.pass_hint.setText("Checking with the sensor…")
        self.pass_hint.setVisible(True)

        def done(message: str) -> None:
            self._host_log(message)
            self.pass_hint.setText(message)
            self._on_status(self.runner.status())

        self.runner.retry_failed(round_id or None, done)

    def _dismiss_pass(self) -> None:
        self._pass_dismissed = str(self.runner.status().get("last_round_id") or "")
        self.pass_box.setVisible(False)

    def _open_result_folder(self) -> None:
        view = self.result_card.view
        if view is not None and view.local_paths and not desktop_open.reveal(view.local_paths[0]):
            self._host_log("Could not open the folder.", "error")

    def _show_result_waterfall(self) -> None:
        view = self.result_card.view
        if view is None or not view.local_paths:
            return
        self._waterfall = WaterfallDialog(view.local_paths[0], center_hz=view.centre_hz, bandwidth_hz=view.bandwidth_hz, parent=self.window())
        self._waterfall.show()

    def _host_busy_reason(self) -> str:
        """Why Start waits for the other tab ('Manual recording is running.'), or ''."""
        probe = getattr(self.host, "manual_busy_reason", None)
        try:
            return str(probe() or "") if callable(probe) else ""
        except Exception:  # noqa: BLE001 - advisory
            return ""

    def _start_reason(self, host: str, *, ready: bool, active: bool, blocked: bool, previous: str = "") -> str:
        if active or self._identity_pending:
            return ""
        if host:
            return host
        if blocked:
            return "Continue or dismiss the interrupted collection first."
        if previous:
            return previous
        if self._importing:
            return "Reading the worksheet…"
        if self.plan is None:
            return NO_WORKBOOK
        if not ready:
            return "Fix the problems listed in Preview first."
        return ""

    def _sync_buttons(self, status: dict) -> None:
        state = status.get("state", S_IDLE)
        active = bool(status.get("active"))
        ready = self.plan is not None and self.plan.ready and not self._importing
        # A collection saved by an older version is never continued; a fresh plan may replace it.
        blocked = state == S_INTERRUPTED and not bool(status.get("legacy"))
        host = self._host_busy_reason()
        reason = self._start_reason(host, ready=ready, active=active, blocked=blocked, previous=str(status.get("previous_work") or ""))
        self.start_btn.setEnabled(ready and not active and not blocked and not self._identity_pending and not reason)
        self.start_btn.setToolTip(reason or "Check the connected sensor, show the preview, then record the rows")
        # Without a workbook the status line already says what to do.
        shown = "" if reason == NO_WORKBOOK else reason
        self.reason_label.setText(shown)
        self.reason_label.setVisible(bool(shown))
        self.pause_btn.setEnabled(state in {S_RUNNING, S_WAITING, S_WAITING_SCHEDULE, S_PAUSED_BACKLOG})
        self.pause_btn.setVisible(state not in {S_PAUSED, S_PAUSED_BACKLOG, S_PAUSE_REQUESTED})
        self.resume_btn.setEnabled(state in {S_PAUSED, S_PAUSED_BACKLOG, S_PAUSE_REQUESTED})
        self.resume_btn.setVisible(state in {S_PAUSED, S_PAUSED_BACKLOG, S_PAUSE_REQUESTED})
        self.stop_btn.setEnabled(active)
        self.preview_btn.setEnabled(self.plan is not None and not self._importing)
        self.preview_btn.setToolTip(
            "The rows that will be recorded, in order, with the requested range of each"
            if self.plan is not None else "Load a protocol workbook first."
        )
        if not self._built:
            return
        self.reload_btn.setEnabled(bool(self.workbook_edit.text().strip()) and not active and not self._importing)
        for widget in (self.browse_btn, self.sheet_combo, self.duration_spin, self.wait_spin, self.wait_unit, self.run_now, self.run_times, self.schedule_box, self.repeat_spin, self.until_stopped):
            widget.setEnabled(not active)
        if not active:
            self.repeat_spin.setEnabled(not self.until_stopped.isChecked())
            self.date_field.setEnabled(not self.daily_check.isChecked())

    def _refresh_recovery(self) -> None:
        if not self._built:
            if self.runner.session is not None:
                self.ensure_built()
            return
        session = self.runner.session
        status = self.runner.status()
        missed = status.get("missed_rounds") or []
        interrupted = self.runner.interrupted()
        legacy = bool(session is not None and session.plan.legacy)
        if session is None or (not interrupted and not missed and not legacy):
            # A finished collection is summarised by the pass summary above the form.
            self.recovery.setVisible(False)
            return
        lines = []
        uncertain = self.runner.uncertain_tasks() if interrupted and not legacy else []
        if legacy:
            lines.append(
                "This collection was saved by an older version. Its results are kept, "
                "but it cannot be continued: start a fresh plan or discard it."
            )
        elif interrupted and self.runner.downloads_only():
            uncertain = []
            lines.append(
                f"The application was closed before {_plural(self.runner.unfinished_downloads(), 'download')} finished. "
                "The files are on the sensor; Continue downloads them."
            )
        elif interrupted:
            reason = f" ({session.pause_reason})" if session.pause_reason else ""
            if uncertain:
                lines.append(
                    f"The last collection was interrupted{reason}. {_plural(len(uncertain), 'recording')} not confirmed: "
                    "check with the sensor first, or skip them. Nothing is sent again on its own."
                )
            elif self.runner.current_round() is not None:
                lines.append(f"The last collection was interrupted during a pass{reason}. Continue finishes the pass.")
            else:
                lines.append("The application was closed while waiting for the next pass. Continue keeps the schedule.")
        if missed and not legacy:
            record = session.round(missed[0])
            when = format_datetime(record.scheduled_local.astimezone()) if record is not None else "its time"
            lines.append(f"A scheduled pass was missed at {when}. Run it now or skip it.")
        self.recovery_label.setText("\n".join(lines))
        self.reconcile_btn.setVisible(interrupted and bool(uncertain))
        self.resume_session_btn.setVisible(interrupted and not legacy)
        self.skip_uncertain_btn.setVisible(interrupted and bool(uncertain))
        self.run_missed_btn.setVisible(bool(missed) and not legacy)
        self.skip_missed_btn.setVisible(bool(missed) and not legacy)
        self.discard_btn.setText("Discard" if legacy else "Dismiss")
        self.discard_btn.setToolTip(
            "Forget this collection; its files, record files and Excel results stay" if legacy else "Hide this notice"
        )
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


__all__ = ["CellularPanel", "PanelSettings", "PlanPreviewDialog", "ElideLabel", "FieldLabel", "load_panel_settings", "save_panel_settings", "settings_path"]
