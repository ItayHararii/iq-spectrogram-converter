"""Cellular Collection panel: a compact, collapsible section of the main window.

Protocol workbook → plan preview → schedule → Start / Pause / Resume / Stop,
with progress, countdowns and recovery actions. Everything heavy (import of a
large workbook, downloads, checks) runs off the UI thread through the runner
or the host's job helper; this module only draws and forwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .capture_api import CaptureRequest
from .cellular_capabilities import DEFAULT_WINDOW_HZ, MHZ, capability_for, normalise_window_hz
from .cellular_plan import CellularPlan, build_plan, format_bytes, format_seconds
from .cellular_protocol import (
    LEVEL_WARNING,
    ColumnMapping,
    ProtocolError,
    ProtocolImport,
    import_protocol,
    list_worksheets,
    read_headers,
    save_snapshot,
    suggest_mapping,
)
from .cellular_quality import LEVEL_ERROR as Q_ERROR
from .cellular_quality import LEVEL_INFO as Q_INFO
from .cellular_quality import LEVEL_OK as Q_OK
from .cellular_quality import LEVEL_WARNING as Q_WARNING
from .cellular_quality import Check, coverage_rows, coverage_summary, pre_run_checks, worst_level
from .cellular_runner import (
    OWNER,
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
from .cellular_scheduler import (
    MODE_NOW,
    MODE_TIMES,
    TZ_LOCAL,
    TZ_UTC,
    Schedule,
    ScheduleTimeZone,
    parse_times,
    tight_schedule_warning,
)
from .constants import DEFAULT_RECORDING_FORMAT, VERIFIED_RECORDING_FORMATS
from .json_store import read_json, write_json

SETTINGS_NAME = "settings.json"
TEST_OWNER = "Cellular Collection test"
HISTORICAL_PRESET_S = 2.0
DEFAULT_WAIT_S = 10.0

NETWORKS = ("Unknown", "Cellcom", "Pelephone", "PHI (Partner / HOT)", "Other")
TECHNOLOGIES = ("Unknown", "GSM", "UMTS", "LTE", "5G NR", "Mixed")
ENVIRONMENTS = ("Unknown", "Indoor", "Outdoor")
INDOOR_SOURCES = ("Unknown", "Dedicated indoor system", "Outdoor signal received indoors")
STRENGTHS = ("Unknown", "Strong", "Weak")
PATHS = ("Unknown", "Clear", "Obstructed")
DOMINANCE = ("Unknown", "Dominant cell", "Overlapping cells")

_ROLE_LABELS = (
    ("band", "Band name / number"),
    ("nominal", "Nominal frequency"),
    ("duplex", "Duplex (FDD/TDD)"),
    ("direction", "Direction (DL/UL)"),
    ("start", "Start frequency"),
    ("stop", "Stop frequency"),
    ("dl_start", "DL start (paired layout)"),
    ("dl_stop", "DL stop (paired layout)"),
    ("ul_start", "UL start (paired layout)"),
    ("ul_stop", "UL stop (paired layout)"),
    ("unit", "Unit column"),
    ("duration", "Duration"),
    ("network", "Network / operator"),
    ("technology", "Technology"),
    ("target_class", "Class"),
    ("channel_bw", "Channel bandwidth"),
    ("repeat", "Repeat count"),
    ("notes", "Notes"),
    ("result", "Result (recording name)"),
    ("result_time", "Result (recording date & time)"),
    ("status", "Status"),
)


# ----------------------------------------------------------------------------- settings


@dataclass
class PanelSettings:
    workbook: str = ""
    worksheet: str = ""
    mapping: dict[str, Any] | None = None
    campaign: str = ""
    site: str = ""
    visit_id: str = ""
    fallback_duration_s: float | None = None
    wait_s: float = DEFAULT_WAIT_S
    wait_unit: str = "seconds"
    schedule_mode: str = MODE_NOW
    times_text: str = "08:00, 12:00, 18:00"
    dates_text: str = ""
    daily: bool = False
    tz_kind: str = TZ_LOCAL
    window_mhz: float = DEFAULT_WINDOW_HZ / MHZ
    edge_margin_khz: float = 0.0
    auto_download: bool = True
    verify_checksums: bool = True
    merge_parts: bool = False
    quality_checks: bool = True
    previews: bool = True
    backlog_limit: int = 8
    local_reserve_gb: float = 2.0
    # EMP recording_format for the captures; "" follows the recorder's own Format box.
    recording_format: str = ""
    # Fill the protocol workbook's result columns (one sheet per round).
    protocol_results: bool = True
    scenario: dict[str, str] = field(default_factory=dict)
    sites: dict[str, dict[str, Any]] = field(default_factory=dict)
    expanded: bool = False
    advanced_open: bool = False
    last_sensor: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict | None) -> "PanelSettings":
        settings = cls()
        for key, value in (data or {}).items():
            if hasattr(settings, key):
                setattr(settings, key, value)
        try:
            settings.fallback_duration_s = float(settings.fallback_duration_s) if settings.fallback_duration_s is not None else None
        except (TypeError, ValueError):
            settings.fallback_duration_s = None
        return settings


def settings_path(config_dir: Path) -> Path:
    return Path(config_dir) / "cellular" / SETTINGS_NAME


def load_panel_settings(config_dir: Path) -> PanelSettings:
    try:
        return PanelSettings.from_dict(read_json(settings_path(config_dir)))
    except Exception:  # noqa: BLE001 - a damaged settings file must not block the panel
        return PanelSettings()


def save_panel_settings(config_dir: Path, settings: PanelSettings) -> None:
    try:
        path = settings_path(config_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        write_json(path, settings.to_dict(), backup=False)
    except Exception:  # noqa: BLE001
        pass


def _compact(combo: QComboBox, chars: int = 10) -> QComboBox:
    """Keep long option texts from dictating the panel's minimum width."""
    combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(chars)
    combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    return combo


def parse_dates(text: str) -> tuple[list[date], list[str]]:
    dates: list[date] = []
    bad: list[str] = []
    for token in [t.strip() for t in (text or "").replace(";", ",").split(",") if t.strip()]:
        try:
            dates.append(date.fromisoformat(token))
        except ValueError:
            bad.append(token)
    return dates, bad


# ----------------------------------------------------------------------------- dialogs


class ColumnMappingDialog(QDialog):
    """Assign worksheet headers to roles when the automatic guess is incomplete."""

    def __init__(self, headers: list[str], mapping: ColumnMapping, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Map worksheet columns")
        self._combos: dict[str, QComboBox] = {}
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose which column holds each value. Start/stop (or DL/UL start/stop) must be the exact "
            "allocation edges; a band name or nominal frequency is never used as a centre frequency."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        grid = QGridLayout()
        for row, (role, label) in enumerate(_ROLE_LABELS):
            combo = QComboBox()
            combo.addItem("— not used —", "")
            for header in headers:
                combo.addItem(header, header)
            current = getattr(mapping, role, None)
            index = combo.findData(current) if current else 0
            combo.setCurrentIndex(index if index >= 0 else 0)
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(combo, row, 1)
            self._combos[role] = combo
        layout.addLayout(grid)
        unit_row = QHBoxLayout()
        unit_row.addWidget(QLabel("Unit for bare numbers"))
        self.unit_combo = QComboBox()
        self.unit_combo.addItem("— from header or cell only —", "")
        for unit in ("Hz", "kHz", "MHz", "GHz"):
            self.unit_combo.addItem(unit, unit)
        if mapping.default_unit:
            index = self.unit_combo.findData(mapping.default_unit)
            self.unit_combo.setCurrentIndex(index if index >= 0 else 0)
        unit_row.addWidget(self.unit_combo)
        unit_row.addStretch(1)
        layout.addLayout(unit_row)
        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setObjectName("errorText")
        layout.addWidget(self.problem)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept_if_complete)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._mapping = mapping

    def mapping(self) -> ColumnMapping:
        data = self._mapping.to_dict()
        for role, combo in self._combos.items():
            data[role] = combo.currentData() or None
        data["default_unit"] = self.unit_combo.currentData() or None
        return ColumnMapping.from_dict(data)

    def _accept_if_complete(self) -> None:
        missing = self.mapping().missing()
        if missing:
            self.problem.setText("Still missing: " + "; ".join(missing))
            return
        self.accept()


class PlanPreviewDialog(QDialog):
    """The actual execution order with direction, window, duration and estimate per task."""

    def __init__(
        self,
        plan: CellularPlan,
        imported: ProtocolImport | None,
        *,
        wait_s: float,
        checks=None,
        start_label: str | None = None,
        format_text: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Cellular collection plan")
        self.resize(900, 560)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"{len(plan.tasks)} capture(s) per round · {format_seconds(plan.capture_seconds)} of IQ · "
            f"about {format_bytes(plan.estimated_bytes)} per round (estimate from the sample-rate model) · "
            f"≈ {format_seconds(plan.estimated_round_seconds(wait_s))} per round with {format_seconds(wait_s)} waits"
            + (f" · {format_text}" if format_text else "")
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        if plan.window_note:
            note = QLabel(plan.window_note)
            note.setWordWrap(True)
            note.setObjectName("muted")
            layout.addWidget(note)
        tree = QTreeWidget()
        tree.setColumnCount(9)
        tree.setHeaderLabels(["#", "Band", "Duplex", "Direction", "Window (MHz)", "Coverage", "Duration", "Est. size", "Row / network"])
        tree.setRootIsDecorated(False)
        tree.setAlternatingRowColors(True)
        for task in plan.tasks:
            item = QTreeWidgetItem(
                [
                    str(task.index),
                    task.band_label,
                    task.duplex,
                    task.direction,
                    f"{task.window_start_hz / 1e6:g} – {task.window_stop_hz / 1e6:g}",
                    task.coverage_label if task.window_count == 1 else f"{task.window_index}/{task.window_count} sequential (overlap {task.overlap_prev_hz / 1e6:g} MHz)",
                    f"{task.duration_s:g} s ({task.duration_source})",
                    format_bytes(task.estimated_bytes) + (f", {task.estimated_parts} part(s)" if task.estimated_parts > 1 else ""),
                    f"row {task.row}" + (f" · {task.network}" if task.network else ""),
                ]
            )
            if task.adjustments:
                item.setToolTip(5, "\n".join(task.adjustments))
            tree.addTopLevelItem(item)
        header = tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(tree, 1)
        issues = [i for i in plan.issues] + ([i for i in imported.issues] if imported is not None else [])
        if issues:
            box = QPlainTextEdit()
            box.setReadOnly(True)
            box.setMaximumHeight(120)
            box.setPlainText("\n".join(f"[{i.level}] " + (f"row {i.row}: " if i.row else "") + i.message for i in issues))
            layout.addWidget(box)
        if checks:
            check_box = QPlainTextEdit()
            check_box.setReadOnly(True)
            check_box.setMaximumHeight(140)
            check_box.setPlainText("\n".join(f"[{c.level}] {c.name}: {c.message}" for c in checks))
            layout.addWidget(QLabel("Pre-run checks"))
            layout.addWidget(check_box)
        buttons = QDialogButtonBox()
        if start_label:
            ok = buttons.addButton(start_label, QDialogButtonBox.ButtonRole.AcceptRole)
            ok.setEnabled(plan.ready and (worst_level(checks) != Q_ERROR if checks else True))
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class CoverageDialog(QDialog):
    """Coverage by round / site / band / direction with simple filters."""

    def __init__(self, rows: list[dict[str, Any]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Coverage summary")
        self.resize(820, 480)
        self._rows = rows
        layout = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.round_filter = QComboBox()
        self.band_filter = QComboBox()
        self.direction_filter = QComboBox()
        for combo, key, label in ((self.round_filter, "round_id", "Round"), (self.band_filter, "band", "Band"), (self.direction_filter, "direction", "Direction")):
            combo.addItem(f"All ({label.lower()}s)", "")
            for value in sorted({str(r.get(key) or "") for r in rows if r.get(key)}):
                combo.addItem(value, value)
            combo.currentIndexChanged.connect(self._refresh)
            filters.addWidget(QLabel(label))
            filters.addWidget(combo)
        filters.addStretch(1)
        layout.addLayout(filters)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(9)
        self.tree.setHeaderLabels(["Round", "Site", "Band", "Direction", "Confirmed / planned", "Files (verified)", "Images", "Uncertain", "Failed"])
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        layout.addWidget(self.tree, 1)
        self.note = QLabel("Logical captures, split files and derived images are counted separately.")
        self.note.setObjectName("muted")
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)
        self._refresh()

    def _refresh(self) -> None:
        from .cellular_quality import filter_rows

        rows = filter_rows(
            self._rows,
            round_id=self.round_filter.currentData() or None,
            band=self.band_filter.currentData() or None,
            direction=self.direction_filter.currentData() or None,
        )
        self.tree.clear()
        for g in coverage_summary(rows):
            self.tree.addTopLevelItem(
                QTreeWidgetItem(
                    [
                        str(g.get("round_id") or ""),
                        str(g.get("site") or ""),
                        str(g.get("band") or ""),
                        str(g.get("direction") or ""),
                        f"{g['confirmed']} / {g['planned']}",
                        f"{g['files']} ({g['files_verified']})",
                        str(g["images"]),
                        str(g["uncertain"]),
                        str(g["failed"]),
                    ]
                )
            )
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)


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
        self._mapping: ColumnMapping | None = ColumnMapping.from_dict(self.settings.mapping) if self.settings.mapping else None
        self._snapshot_path = ""
        self._test_running = False
        # The body (about 180 widgets) is built the first time the panel is
        # opened, so the main window starts as fast as before for operators
        # who never use the feature.
        self._built = False
        self._build_header()
        self.runner.status_changed.connect(self._on_status)
        self.runner.session_changed.connect(self._refresh_recovery)
        self.runner.log.connect(self._on_runner_log)
        host_signal = getattr(host, "capture_finished", None)
        if host_signal is not None:
            host_signal.connect(self._on_capture_finished)
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

        intro = QLabel(
            "Load the protocol workbook, preview the plan, choose when to run. Every allocation in the sheet is "
            "recorded in order (bands by frequency, DL before UL, TDD once as shared) with a wait after each confirmed capture."
        )
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        body.addWidget(intro)

        # Protocol
        book_row = QHBoxLayout()
        book_row.setSpacing(8)
        book_label = QLabel("Protocol")
        book_label.setObjectName("muted")
        self.workbook_edit = QLineEdit()
        self.workbook_edit.setReadOnly(True)
        self.workbook_edit.setPlaceholderText("Choose the protocol workbook (.xlsx / .xlsm)")
        browse = QPushButton("Browse")
        browse.setObjectName("ghost")
        browse.clicked.connect(self._browse_workbook)
        self.template_btn = QPushButton("Template…")
        self.template_btn.setObjectName("ghost")
        self.template_btn.setToolTip("Write a protocol workbook with the expected headers (reference rows marked 'verify before use').")
        self.template_btn.clicked.connect(self._write_template)
        book_row.addWidget(book_label)
        book_row.addWidget(self.workbook_edit, 1)
        book_row.addWidget(browse)
        book_row.addWidget(self.template_btn)
        body.addLayout(book_row)

        sheet_row = QHBoxLayout()
        sheet_row.setSpacing(8)
        sheet_label = QLabel("Worksheet")
        sheet_label.setObjectName("muted")
        self.sheet_combo = _compact(QComboBox(), 12)
        self.sheet_combo.currentIndexChanged.connect(self._on_sheet_changed)
        self.reload_btn = QPushButton("Reload")
        self.reload_btn.setObjectName("ghost")
        self.reload_btn.setToolTip("Re-read the workbook. A running round keeps the plan it started with.")
        self.reload_btn.clicked.connect(lambda: self._import(force=True))
        self.map_btn = QPushButton("Map columns…")
        self.map_btn.setObjectName("ghost")
        self.map_btn.clicked.connect(self._map_columns)
        self.preview_btn = QPushButton("Preview plan…")
        self.preview_btn.setObjectName("ghost")
        self.preview_btn.clicked.connect(lambda: self._show_preview(start=False))
        sheet_row.addWidget(sheet_label)
        sheet_row.addWidget(self.sheet_combo, 1)
        sheet_row.addWidget(self.reload_btn)
        body.addLayout(sheet_row)
        tools_row = QHBoxLayout()
        tools_row.setSpacing(8)
        tools_row.addWidget(self.map_btn)
        tools_row.addWidget(self.preview_btn)
        tools_row.addStretch(1)
        body.addLayout(tools_row)
        self.import_label = QLabel("No protocol loaded.")
        self.import_label.setObjectName("muted")
        self.import_label.setWordWrap(True)
        body.addWidget(self.import_label)

        # Campaign
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(1, 1)
        self.campaign_edit = QLineEdit(self.settings.campaign)
        self.campaign_edit.setPlaceholderText("Campaign name")
        self.site_combo = _compact(QComboBox(), 8)
        self.site_combo.setEditable(True)
        self.site_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        for name in sorted(self.settings.sites):
            self.site_combo.addItem(name)
        self.site_combo.setCurrentText(self.settings.site)
        self.site_combo.currentTextChanged.connect(self._on_site_selected)
        self.save_site_btn = QPushButton("Save site")
        self.save_site_btn.setObjectName("ghost")
        self.save_site_btn.setToolTip("Remember this site name with its scenario details for the next visit.")
        self.save_site_btn.clicked.connect(self._save_site)
        self.visit_edit = QLineEdit(self.settings.visit_id)
        self.visit_edit.setPlaceholderText("optional")
        grid.addWidget(QLabel("Campaign"), 0, 0)
        grid.addWidget(self.campaign_edit, 0, 1)
        grid.addWidget(QLabel("Site"), 1, 0)
        site_box = QHBoxLayout()
        site_box.setSpacing(6)
        site_box.addWidget(self.site_combo, 1)
        site_box.addWidget(self.save_site_btn)
        grid.addLayout(site_box, 1, 1)
        duration_box = QHBoxLayout()
        duration_box.setSpacing(6)
        self.duration_edit = QLineEdit("" if self.settings.fallback_duration_s is None else f"{self.settings.fallback_duration_s:g}")
        self.duration_edit.setPlaceholderText("seconds")
        self.duration_edit.textChanged.connect(self._rebuild_plan)
        preset = QPushButton(f"{HISTORICAL_PRESET_S:g} s")
        preset.setObjectName("ghost")
        preset.setToolTip("Historical preset; the workbook's own duration column wins when present.")
        preset.clicked.connect(lambda: self.duration_edit.setText(f"{HISTORICAL_PRESET_S:g}"))
        duration_box.addWidget(self.duration_edit, 1)
        duration_box.addWidget(preset)
        self.duration_label = QLabel("Campaign duration")
        grid.addWidget(self.duration_label, 2, 0)
        grid.addLayout(duration_box, 2, 1)
        wait_box = QHBoxLayout()
        wait_box.setSpacing(6)
        self.wait_spin = QDoubleSpinBox()
        self.wait_spin.setRange(0, 10_000)
        self.wait_spin.setDecimals(1)
        self.wait_unit = _compact(QComboBox(), 7)
        self.wait_unit.addItems(["Seconds", "Minutes"])
        unit = "Minutes" if self.settings.wait_unit == "minutes" else "Seconds"
        self.wait_unit.setCurrentText(unit)
        self.wait_spin.setValue(self.settings.wait_s / 60.0 if unit == "Minutes" else self.settings.wait_s)
        wait_box.addWidget(self.wait_spin, 1)
        wait_box.addWidget(self.wait_unit)
        grid.addWidget(QLabel("Wait after each capture"), 3, 0)
        grid.addLayout(wait_box, 3, 1)
        grid.addWidget(QLabel("Visit ID"), 4, 0)
        grid.addWidget(self.visit_edit, 4, 1)
        body.addLayout(grid)

        # Schedule
        sched = QGridLayout()
        sched.setHorizontalSpacing(10)
        sched.setVerticalSpacing(6)
        sched.setColumnStretch(1, 1)
        self.run_now = QRadioButton("Run now")
        self.run_times = QRadioButton("At times of day")
        (self.run_times if self.settings.schedule_mode == MODE_TIMES else self.run_now).setChecked(True)
        self.times_edit = QLineEdit(self.settings.times_text)
        self.times_edit.setPlaceholderText("08:00, 12:00, 18:00")
        self.dates_edit = QLineEdit(self.settings.dates_text or date.today().isoformat())
        self.dates_edit.setPlaceholderText("YYYY-MM-DD, …")
        self.daily_check = QCheckBox("Repeat daily")
        self.daily_check.setChecked(bool(self.settings.daily))
        self.tz_combo = _compact(QComboBox(), 12)
        self.tz_combo.addItem(ScheduleTimeZone(TZ_LOCAL).describe(), TZ_LOCAL)
        self.tz_combo.addItem("UTC", TZ_UTC)
        index = self.tz_combo.findData(self.settings.tz_kind)
        self.tz_combo.setCurrentIndex(index if index >= 0 else 0)
        for widget in (self.times_edit, self.dates_edit, self.daily_check, self.tz_combo):
            widget.setEnabled(self.run_times.isChecked())
        self.run_times.toggled.connect(self._on_schedule_mode)
        mode_box = QHBoxLayout()
        mode_box.setSpacing(12)
        mode_box.addWidget(self.run_now)
        mode_box.addWidget(self.run_times)
        mode_box.addStretch(1)
        sched.addLayout(mode_box, 0, 0, 1, 2)
        sched.addWidget(QLabel("Times"), 1, 0)
        sched.addWidget(self.times_edit, 1, 1)
        sched.addWidget(QLabel("Dates"), 2, 0)
        dates_box = QHBoxLayout()
        dates_box.setSpacing(8)
        dates_box.addWidget(self.dates_edit, 1)
        dates_box.addWidget(self.daily_check)
        sched.addLayout(dates_box, 2, 1)
        sched.addWidget(QLabel("Time zone"), 3, 0)
        sched.addWidget(self.tz_combo, 3, 1)
        body.addLayout(sched)
        self.schedule_note = QLabel("Scheduled rounds need this application running and the computer awake. Rounds missed while it was closed or asleep are listed for you to run or skip; they never start on their own.")
        self.schedule_note.setObjectName("muted")
        self.schedule_note.setWordWrap(True)
        body.addWidget(self.schedule_note)

        # Controls
        buttons = QGridLayout()
        buttons.setHorizontalSpacing(8)
        buttons.setVerticalSpacing(6)
        self.start_btn = QPushButton("Start collection")
        self.start_btn.setObjectName("primary")
        self.start_btn.setAutoDefault(False)
        self.start_btn.clicked.connect(lambda: self._show_preview(start=True))
        self.pause_btn = QPushButton("Pause after current")
        self.pause_btn.setObjectName("ghost")
        self.pause_btn.clicked.connect(self.runner.pause)
        self.resume_btn = QPushButton("Resume")
        self.resume_btn.setObjectName("ghost")
        self.resume_btn.clicked.connect(self.runner.resume)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setObjectName("ghost")
        self.stop_btn.clicked.connect(self._stop)
        self.test_btn = QPushButton("Test capture")
        self.test_btn.setObjectName("ghost")
        self.test_btn.setToolTip("One short capture of the first planned window to check the sensor before a session.")
        self.test_btn.clicked.connect(self._test_capture)
        buttons.addWidget(self.start_btn, 0, 0, 1, 3)
        buttons.addWidget(self.pause_btn, 1, 0)
        buttons.addWidget(self.resume_btn, 1, 1)
        buttons.addWidget(self.stop_btn, 1, 2)
        body.addLayout(buttons)

        self.status_label = QLabel("Idle")
        self.status_label.setWordWrap(True)
        body.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setObjectName("thin")
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        body.addWidget(self.progress)
        self.detail_label = QLabel("")
        self.detail_label.setObjectName("muted")
        self.detail_label.setWordWrap(True)
        body.addWidget(self.detail_label)

        # Recovery banner
        self.recovery = QFrame()
        self.recovery.setObjectName("inputWrap")
        rec = QVBoxLayout(self.recovery)
        rec.setContentsMargins(10, 8, 10, 8)
        rec.setSpacing(6)
        self.recovery_label = QLabel("")
        self.recovery_label.setWordWrap(True)
        rec.addWidget(self.recovery_label)
        rec_buttons = QGridLayout()
        rec_buttons.setHorizontalSpacing(8)
        rec_buttons.setVerticalSpacing(6)
        self.reconcile_btn = QPushButton("Reconcile with sensor")
        self.reconcile_btn.setObjectName("ghost")
        self.reconcile_btn.clicked.connect(self._reconcile)
        self.resume_session_btn = QPushButton("Resume session")
        self.resume_session_btn.setObjectName("ghost")
        self.resume_session_btn.clicked.connect(self._resume_after_restart)
        self.skip_uncertain_btn = QPushButton("Skip uncertain")
        self.skip_uncertain_btn.setObjectName("ghost")
        self.skip_uncertain_btn.clicked.connect(self._skip_uncertain)
        self.run_missed_btn = QPushButton("Run missed round now")
        self.run_missed_btn.setObjectName("ghost")
        self.run_missed_btn.clicked.connect(self._run_missed)
        self.skip_missed_btn = QPushButton("Skip missed round")
        self.skip_missed_btn.setObjectName("ghost")
        self.skip_missed_btn.clicked.connect(self._skip_missed)
        self.discard_btn = QPushButton("Discard session")
        self.discard_btn.setObjectName("ghost")
        self.discard_btn.clicked.connect(self._discard)
        for index, b in enumerate((self.reconcile_btn, self.resume_session_btn, self.skip_uncertain_btn, self.run_missed_btn, self.skip_missed_btn, self.discard_btn)):
            rec_buttons.addWidget(b, index // 2, index % 2)
        rec.addLayout(rec_buttons)
        self.recovery.setVisible(False)
        body.addWidget(self.recovery)

        # Results
        results = QGridLayout()
        results.setHorizontalSpacing(8)
        results.setVerticalSpacing(6)
        self.coverage_btn = QPushButton("Coverage summary…")
        self.coverage_btn.setObjectName("ghost")
        self.coverage_btn.clicked.connect(self._show_coverage)
        self.folder_btn = QPushButton("Open results folder")
        self.folder_btn.setObjectName("ghost")
        self.folder_btn.clicked.connect(self._open_results_folder)
        self.advanced_btn = QPushButton("Advanced ▸")
        self.advanced_btn.setObjectName("ghost")
        self.advanced_btn.clicked.connect(lambda: self._set_advanced(self.advanced.isHidden()))
        results.addWidget(self.test_btn, 0, 0)
        results.addWidget(self.coverage_btn, 0, 1)
        results.addWidget(self.folder_btn, 1, 0)
        results.addWidget(self.advanced_btn, 1, 1)
        body.addLayout(results)

        # Advanced
        self.advanced = QWidget()
        adv = QGridLayout(self.advanced)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.setHorizontalSpacing(10)
        adv.setVerticalSpacing(6)
        adv.setColumnStretch(1, 1)
        self.window_spin = QDoubleSpinBox()
        self.window_spin.setRange(0.1, 1000.0)
        self.window_spin.setDecimals(3)
        self.window_spin.setSuffix(" MHz")
        self.window_spin.setValue(float(self.settings.window_mhz))
        self.window_spin.valueChanged.connect(self._rebuild_plan)
        self.window_note = QLabel("")
        self.window_note.setObjectName("muted")
        self.window_note.setWordWrap(True)
        self.margin_spin = QDoubleSpinBox()
        self.margin_spin.setRange(0.0, 20_000.0)
        self.margin_spin.setDecimals(1)
        self.margin_spin.setSuffix(" kHz")
        self.margin_spin.setToolTip("Edge margin reserved on both sides of a window when a wide allocation is split; adjacent windows overlap by this amount.")
        self.margin_spin.setValue(float(self.settings.edge_margin_khz))
        self.margin_spin.valueChanged.connect(self._rebuild_plan)
        self.auto_download = QCheckBox("Download parts after each capture")
        self.auto_download.setChecked(bool(self.settings.auto_download))
        self.verify_check = QCheckBox("Verify SHA-256 of downloads")
        self.verify_check.setChecked(bool(self.settings.verify_checksums))
        self.merge_check = QCheckBox("Merge recording parts (originals kept)")
        self.merge_check.setChecked(bool(self.settings.merge_parts))
        self.quality_check = QCheckBox("Check files after download")
        self.quality_check.setChecked(bool(self.settings.quality_checks))
        self.preview_check = QCheckBox("Draw sampled previews")
        self.preview_check.setChecked(bool(self.settings.previews))
        self.protocol_results_check = QCheckBox("Fill the protocol workbook (one results sheet per round)")
        self.protocol_results_check.setChecked(bool(self.settings.protocol_results))
        self.protocol_results_check.setToolTip(
            "Writes a 'Round <date> <time>' sheet into the protocol workbook with one row per capture window: "
            "the window recorded, the sensor, the recording file names, the seconds held and the local date & time. "
            "The protocol sheet itself and earlier round sheets are never changed. Needs a result column such as "
            "'IQ Recording' or 'Recording Date & Time' in the protocol."
        )
        self.protocol_results_check.toggled.connect(lambda _on: self._rebuild_plan())
        self.backlog_spin = QSpinBox()
        self.backlog_spin.setRange(1, 200)
        self.backlog_spin.setValue(int(self.settings.backlog_limit))
        self.backlog_spin.setToolTip("Pause new captures when this many downloads are waiting.")
        self.reserve_spin = QDoubleSpinBox()
        self.reserve_spin.setRange(0.0, 10_000.0)
        self.reserve_spin.setDecimals(1)
        self.reserve_spin.setSuffix(" GB")
        self.reserve_spin.setValue(float(self.settings.local_reserve_gb))
        self.reserve_spin.setToolTip("Free space to keep on the local destination; captures pause when the next one would eat into it.")
        self.format_combo = _compact(QComboBox(), 12)
        self.format_combo.addItem("Same as recorder", "")
        for item in VERIFIED_RECORDING_FORMATS:
            self.format_combo.addItem(item.short_label, item.identifier)
        index = self.format_combo.findData(self.settings.recording_format or "")
        self.format_combo.setCurrentIndex(index if index >= 0 else 0)
        self.format_combo.setToolTip(
            "The EMP recording_format the sensor writes. 'Same as recorder' follows the Format box of the recording card. "
            "File checks, previews and the Converter read WAVE; other formats are downloaded and checksummed but their content is not inspected."
        )
        self.format_combo.currentIndexChanged.connect(lambda _i: self._update_format_note())
        self.format_note = QLabel("")
        self.format_note.setObjectName("muted")
        self.format_note.setWordWrap(True)
        adv.addWidget(QLabel("Capture window"), 0, 0)
        adv.addWidget(self.window_spin, 0, 1)
        adv.addWidget(QLabel("Edge margin"), 1, 0)
        adv.addWidget(self.margin_spin, 1, 1)
        adv.addWidget(self.window_note, 2, 0, 1, 2)
        adv.addWidget(QLabel("Recording format"), 3, 0)
        adv.addWidget(self.format_combo, 3, 1)
        adv.addWidget(self.format_note, 4, 0, 1, 2)
        adv.addWidget(self.auto_download, 5, 0, 1, 2)
        adv.addWidget(self.verify_check, 6, 0, 1, 2)
        adv.addWidget(self.quality_check, 7, 0, 1, 2)
        adv.addWidget(self.preview_check, 8, 0, 1, 2)
        adv.addWidget(self.merge_check, 9, 0, 1, 2)
        adv.addWidget(self.protocol_results_check, 10, 0, 1, 2)
        adv.addWidget(QLabel("Download backlog limit"), 11, 0)
        adv.addWidget(self.backlog_spin, 11, 1)
        adv.addWidget(QLabel("Local reserve"), 12, 0)
        adv.addWidget(self.reserve_spin, 12, 1)
        scenario_title = QLabel("Scenario (optional; nothing is inferred from the frequency)")
        scenario_title.setObjectName("muted")
        scenario_title.setWordWrap(True)
        adv.addWidget(scenario_title, 13, 0, 1, 2)
        self.scenario_fields: dict[str, QComboBox | QLineEdit] = {}
        combos = (
            ("network", "Network", NETWORKS),
            ("technology", "Technology", TECHNOLOGIES),
            ("environment", "Environment", ENVIRONMENTS),
            ("indoor_source", "Indoor source", INDOOR_SOURCES),
            ("signal", "Signal", STRENGTHS),
            ("path", "Path", PATHS),
            ("dominance", "Cells", DOMINANCE),
        )
        row = 14
        for key, label, options in combos:
            combo = _compact(QComboBox(), 8)
            combo.setEditable(True)
            combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            combo.addItems(list(options))
            combo.setCurrentText(str(self.settings.scenario.get(key) or options[0]))
            adv.addWidget(QLabel(label), row, 0)
            adv.addWidget(combo, row, 1)
            self.scenario_fields[key] = combo
            row += 1
        for key, label in (("channel_bw", "Channel bandwidth"), ("evidence", "Identification evidence"), ("notes", "Notes")):
            edit = QLineEdit(str(self.settings.scenario.get(key) or ""))
            adv.addWidget(QLabel(label), row, 0)
            adv.addWidget(edit, row, 1)
            self.scenario_fields[key] = edit
            row += 1
        body.addWidget(self.advanced)
        self.advanced.setVisible(False)
        self._set_advanced(bool(self.settings.advanced_open))
        self._update_window_note()
        self._update_format_note()
        self._sync_buttons(self.runner.status())

    # ------------------------------------------------------------------ small helpers
    def _set_expanded(self, on: bool) -> None:
        if on:
            self._ensure_built()
        self.body.setVisible(on)
        self.toggle_btn.setText(("▾ " if on else "▸ ") + "Cellular Collection")
        self.settings.expanded = on
        self._save_settings()

    def _set_advanced(self, on: bool) -> None:
        self.advanced.setVisible(on)
        self.advanced_btn.setText("Advanced ▾" if on else "Advanced ▸")
        self.settings.advanced_open = on

    def _save_settings(self) -> None:
        s = self.settings
        if not self._built:
            save_panel_settings(self._config_dir, s)
            return
        s.workbook = self.workbook_edit.text().strip()
        s.worksheet = self.sheet_combo.currentText().strip()
        s.mapping = self._mapping.to_dict() if self._mapping else None
        s.campaign = self.campaign_edit.text().strip()
        s.site = self.site_combo.currentText().strip()
        s.visit_id = self.visit_edit.text().strip()
        s.fallback_duration_s = self._fallback_duration()
        s.wait_s = self.wait_seconds()
        s.wait_unit = "minutes" if self.wait_unit.currentText() == "Minutes" else "seconds"
        s.schedule_mode = MODE_TIMES if self.run_times.isChecked() else MODE_NOW
        s.times_text = self.times_edit.text().strip()
        s.dates_text = self.dates_edit.text().strip()
        s.daily = self.daily_check.isChecked()
        s.tz_kind = self.tz_combo.currentData() or TZ_LOCAL
        s.window_mhz = float(self.window_spin.value())
        s.edge_margin_khz = float(self.margin_spin.value())
        s.auto_download = self.auto_download.isChecked()
        s.verify_checksums = self.verify_check.isChecked()
        s.merge_parts = self.merge_check.isChecked()
        s.quality_checks = self.quality_check.isChecked()
        s.previews = self.preview_check.isChecked()
        s.protocol_results = self.protocol_results_check.isChecked()
        s.backlog_limit = int(self.backlog_spin.value())
        s.local_reserve_gb = float(self.reserve_spin.value())
        s.recording_format = str(self.format_combo.currentData() or "")
        s.scenario = self.scenario()
        save_panel_settings(self._config_dir, s)

    def wait_seconds(self) -> float:
        value = float(self.wait_spin.value())
        return value * 60.0 if self.wait_unit.currentText() == "Minutes" else value

    def _fallback_duration(self) -> float | None:
        text = self.duration_edit.text().strip().replace(",", ".")
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
        return value if value > 0 else None

    def scenario(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for key, widget in self.scenario_fields.items():
            text = widget.currentText() if isinstance(widget, QComboBox) else widget.text()
            text = (text or "").strip()
            out[key] = text if text and text.casefold() != "unknown" else "unknown"
        return out

    def _capability(self):
        info = getattr(self.host, "_sensor_info", None)
        model = str(getattr(info, "model", "") or "")
        return capability_for(model)

    def _update_window_note(self) -> None:
        capability = self._capability()
        window_hz, notes = normalise_window_hz(int(self.window_spin.value() * MHZ), capability)
        text = capability.window_note(window_hz)
        if notes:
            text = " ".join(notes) + " " + text
        self.window_note.setText(text)

    def _chosen_format(self) -> str:
        """The panel's own choice; "" follows the recorder's Format box."""
        if not self._built:
            return str(self.settings.recording_format or "")
        return str(self.format_combo.currentData() or "")

    def _effective_format(self) -> tuple[str, str]:
        """(EMP identifier that will be sent, where it comes from)."""
        chosen = self._chosen_format()
        if chosen:
            return chosen, "chosen here"
        getter = getattr(self.host, "_recording_format", None)
        current = ""
        if callable(getter):
            try:
                current = str(getter() or "")
            except Exception:  # noqa: BLE001 - a host without the recorder fields
                current = ""
        if not current:
            conn = getattr(self.host, "_conn", None)
            current = str(getattr(conn, "recording_format", "") or "")
        return (current or DEFAULT_RECORDING_FORMAT), "recorder setting"

    def format_text(self) -> str:
        fmt, source = self._effective_format()
        text = f"Recording format {fmt} ({source})."
        if fmt.upper() != "WAVE":
            text += " File checks, previews and the Converter read WAVE; these files are downloaded and checksummed, but their content is not inspected."
        return text

    def _update_format_note(self) -> None:
        self.format_note.setText(self.format_text())

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
        self._mapping = None
        self._fill_sheets("")
        self._import(force=True)

    def _fill_sheets(self, preferred: str) -> None:
        path = self.workbook_edit.text().strip()
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        try:
            sheets = list_worksheets(path)
        except ProtocolError as exc:
            self.import_label.setText(str(exc))
            self.sheet_combo.blockSignals(False)
            return
        for name, visible in sheets:
            self.sheet_combo.addItem(name if visible else f"{name} (hidden)", name)
        index = self.sheet_combo.findData(preferred) if preferred else -1
        self.sheet_combo.setCurrentIndex(index if index >= 0 else 0)
        self.sheet_combo.blockSignals(False)

    def _on_sheet_changed(self, _index: int) -> None:
        self._mapping = None
        self._import(force=True)

    def _current_sheet(self) -> str:
        return str(self.sheet_combo.currentData() or self.sheet_combo.currentText() or "")

    def _import(self, *, force: bool, quiet: bool = False) -> None:
        path = self.workbook_edit.text().strip()
        sheet = self._current_sheet()
        if not path or not sheet:
            return
        try:
            headers, _row = read_headers(path, sheet)
        except ProtocolError as exc:
            self.imported = None
            self.plan = None
            self.import_label.setText(str(exc))
            self._sync_buttons(self.runner.status())
            return
        mapping = self._mapping or suggest_mapping(headers)
        if mapping.missing():
            self.imported = None
            self.plan = None
            self.import_label.setText(
                "This worksheet cannot be used as a protocol: missing " + "; ".join(mapping.missing()) + ". Use Map columns… or the Template."
            )
            self.map_btn.setEnabled(True)
            self._sync_buttons(self.runner.status())
            if not quiet:
                self._host_log(self.import_label.text(), "error")
            return
        try:
            imported = import_protocol(path, sheet, mapping)
        except ProtocolError as exc:
            self.imported = None
            self.plan = None
            self.import_label.setText(str(exc))
            self._sync_buttons(self.runner.status())
            return
        self.imported = imported
        self._mapping = mapping
        try:
            self._snapshot_path = str(save_snapshot(imported, self._config_dir))
        except Exception:  # noqa: BLE001 - snapshot failure must not block the preview
            self._snapshot_path = ""
        self._rebuild_plan()
        self._save_settings()
        if not quiet:
            self._host_log(self.import_label.text())

    def _rebuild_plan(self, *_args) -> None:
        if self.imported is None:
            return
        capability = self._capability()
        window_hz = int(round(self.window_spin.value() * MHZ))
        self.plan = build_plan(
            self.imported,
            capability,
            window_hz=window_hz,
            fallback_duration_s=self._fallback_duration(),
            edge_margin_hz=int(round(self.margin_spin.value() * 1000)),
        )
        self._update_window_note()
        errors = len(self.imported.errors) + len(self.plan.errors)
        warnings = len(self.imported.warnings) + len([i for i in self.plan.issues if i.level == LEVEL_WARNING])
        results = len(self.imported.result_rows())
        parts = [
            f"{len(self.imported.valid_entries())} allocation(s) from '{self.imported.worksheet}' (protocol {self.imported.protocol_version()})",
            f"{len(self.plan.tasks)} capture(s) per round",
            f"≈ {format_bytes(self.plan.estimated_bytes)}",
        ]
        if results:
            parts.append(f"{results} result row(s) ignored")
        if errors:
            parts.append(f"{errors} error(s) — see Preview")
        if warnings:
            parts.append(f"{warnings} warning(s)")
        has_result_columns = self.imported.mapping.has_result_columns()
        self.protocol_results_check.setEnabled(has_result_columns)
        if has_result_columns:
            parts.append(
                "results → one sheet per round in the workbook" if self.protocol_results_check.isChecked() else "results not written to the workbook"
            )
        else:
            parts.append("no result column in the protocol (results stay in manifests)")
        needs_duration = any(i.code == "duration_missing" for i in self.plan.issues)
        self.duration_label.setText("Campaign duration (required)" if needs_duration else "Campaign duration")
        self.duration_label.setStyleSheet("color: #c0392b; font-weight: 600;" if needs_duration else "")
        self.import_label.setText(" · ".join(parts))
        self._sync_buttons(self.runner.status())

    def _map_columns(self) -> None:
        path = self.workbook_edit.text().strip()
        sheet = self._current_sheet()
        if not path or not sheet:
            return
        try:
            headers, _row = read_headers(path, sheet)
        except ProtocolError as exc:
            QMessageBox.warning(self, "Map columns", str(exc))
            return
        dialog = ColumnMappingDialog(headers, self._mapping or suggest_mapping(headers), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self._mapping = dialog.mapping()
            self._import(force=True)

    def _write_template(self) -> None:
        from .cellular_reference import write_template_workbook

        start = str(Path(self.workbook_edit.text().strip()).parent if self.workbook_edit.text().strip() else Path.home() / "Cellular protocol template.xlsx")
        path, _ = QFileDialog.getSaveFileName(self, "Write protocol template", start, "Excel workbook (*.xlsx)")
        if not path:
            return
        try:
            out = write_template_workbook(path, duration_s=self._fallback_duration())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Template", f"Could not write the template: {exc}")
            return
        QMessageBox.information(
            self,
            "Template written",
            f"{out}\n\nThe rows carry reference-chart values marked 'verify before use'. Edit them to your protocol, then load the workbook.",
        )

    # ------------------------------------------------------------------ plan / start
    def _schedule(self) -> tuple[Schedule | None, str | None]:
        tz = ScheduleTimeZone(self.tz_combo.currentData() or TZ_LOCAL)
        if self.run_now.isChecked():
            return Schedule(mode=MODE_NOW, tz=tz), None
        times, bad = parse_times(self.times_edit.text())
        if bad:
            return None, "Times not understood: " + ", ".join(bad) + " (use HH:MM)."
        if not times:
            return None, "Enter at least one time of day."
        dates, bad_dates = parse_dates(self.dates_edit.text())
        if bad_dates:
            return None, "Dates not understood: " + ", ".join(bad_dates) + " (use YYYY-MM-DD)."
        if not dates and not self.daily_check.isChecked():
            return None, "Enter at least one date or tick Repeat daily."
        return Schedule(mode=MODE_TIMES, times=times, dates=dates, daily=self.daily_check.isChecked(), tz=tz), None

    def _campaign_settings(self) -> CampaignSettings:
        return CampaignSettings(
            campaign=self.campaign_edit.text().strip() or "Cellular",
            site=self.site_combo.currentText().strip(),
            site_id=str((self.settings.sites.get(self.site_combo.currentText().strip()) or {}).get("site_id") or ""),
            visit_id=self.visit_edit.text().strip(),
            fallback_duration_s=self._fallback_duration(),
            wait_s=self.wait_seconds(),
            wait_unit="minutes" if self.wait_unit.currentText() == "Minutes" else "seconds",
            window_hz=int(round(self.window_spin.value() * MHZ)),
            edge_margin_hz=int(round(self.margin_spin.value() * 1000)),
            auto_download=self.auto_download.isChecked(),
            verify_checksums=self.verify_check.isChecked(),
            merge_parts=self.merge_check.isChecked(),
            quality_checks=self.quality_check.isChecked(),
            previews=self.preview_check.isChecked(),
            backlog_limit=int(self.backlog_spin.value()),
            local_reserve_bytes=int(self.reserve_spin.value() * 1024 ** 3),
            recording_format=self._chosen_format(),
            scenario=self.scenario(),
            protocol_path=self.workbook_edit.text().strip(),
            protocol_sheet=self._current_sheet(),
            protocol_results=self.protocol_results_enabled(),
            protocol_header_row=int(self.imported.header_row) if self.imported is not None else 1,
            protocol_mapping=self.imported.mapping.to_dict() if self.imported is not None else {},
        )

    def protocol_results_enabled(self) -> bool:
        """True when the results go into the protocol workbook (ticked and the protocol has result columns)."""
        return bool(
            self.protocol_results_check.isChecked()
            and self.imported is not None
            and self.imported.mapping.has_result_columns()
        )

    def _protocol_results_check(self):
        """Pre-run line about the protocol workbook results."""
        path = self.workbook_edit.text().strip()
        name = Path(path).name if path else "the protocol workbook"
        if self.imported is None or not self.imported.mapping.has_result_columns():
            return Check(
                "protocol_results",
                Q_INFO,
                f"{name} has no result column (such as 'IQ Recording' or 'Recording Date & Time'); results go to the "
                "manifests, the handover summary and the collection workbook only.",
            )
        if not self.protocol_results_check.isChecked():
            return Check("protocol_results", Q_INFO, f"Results are not written to {name} (Advanced → Fill the protocol workbook is off).")
        columns = ", ".join(f"'{header}'" for header in self.imported.mapping.result_columns().values())
        text = (
            f"Results are written to {name}: one 'Round <date> <time>' sheet per round filling {columns}; "
            "the protocol sheet is never changed and a backup copy is made before the first write."
        )
        try:
            from .excel_com import excel_has_workbook

            busy = bool(path) and excel_has_workbook(Path(path))
        except Exception:  # noqa: BLE001 - the check is advisory
            busy = False
        if busy:
            return Check("protocol_results", Q_WARNING, f"{name} is open in Excel: close it, or the results sheet is deferred until it is closed. " + text)
        return Check("protocol_results", Q_OK, text)

    def _pre_run_checks(self):
        info = getattr(self.host, "_sensor_info", None)
        storage = getattr(self.host, "_storage", None)
        checks = pre_run_checks(
            sensor_info=info,
            sensor_storage=storage,
            local_dir=self._download_dir(),
            plan=self.plan,
            local_reserve_bytes=int(self.reserve_spin.value() * 1024 ** 3),
            previous_identity=self.settings.last_sensor or None,
        )
        fmt, _source = self._effective_format()
        checks.append(Check("recording_format", Q_OK if fmt.upper() == "WAVE" else Q_INFO, self.format_text()))
        checks.append(self._protocol_results_check())
        return checks

    def _show_preview(self, *, start: bool) -> None:
        if self.plan is None:
            QMessageBox.information(self, "Cellular collection", "Load a protocol workbook first.")
            return
        self._rebuild_plan()
        checks = self._pre_run_checks() if start else None
        schedule, error = self._schedule() if start else (None, None)
        if start and error:
            QMessageBox.warning(self, "Schedule", error)
            return
        if start and schedule is not None and schedule.mode == MODE_TIMES:
            warning = tight_schedule_warning(schedule, self.plan.estimated_round_seconds(self.wait_seconds()), now=datetime.now(timezone.utc))
            if warning:
                checks = list(checks or [])
                from .cellular_quality import Check

                checks.append(Check("schedule", Q_WARNING, warning))
        dialog = PlanPreviewDialog(
            self.plan,
            self.imported,
            wait_s=self.wait_seconds(),
            checks=checks,
            start_label="Start" if start else None,
            format_text=self.format_text(),
            parent=self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted or not start:
            return
        self._start(schedule)

    def _start(self, schedule: Schedule | None) -> None:
        if self.plan is None or schedule is None:
            return
        self._save_settings()
        info = getattr(self.host, "_sensor_info", None)
        self.settings.last_sensor = {"model": str(getattr(info, "model", "") or ""), "serial": str(getattr(info, "serial", "") or "")}
        save_panel_settings(self._config_dir, self.settings)
        snapshot = self._snapshot_path
        error = self.runner.start_session(self.plan, schedule, self._campaign_settings(), protocol_snapshot=snapshot)
        if error:
            QMessageBox.warning(self, "Cellular collection", error)

    def _stop(self) -> None:
        status = self.runner.status()
        if status.get("state") in {S_RUNNING, S_PAUSE_REQUESTED}:
            answer = QMessageBox.question(
                self,
                "Stop collection",
                "The current capture finishes and is logged; nothing else is recorded. Stop?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.runner.stop()

    # ------------------------------------------------------------------ test capture
    def _test_capture(self) -> None:
        if self.plan is None or not self.plan.tasks:
            QMessageBox.information(self, "Test capture", "Load a protocol first; the test uses the first planned window.")
            return
        if self.runner.active() or self._test_running:
            return
        reserve = getattr(self.host, "set_external_owner", None)
        if reserve is not None:
            error = reserve(OWNER)
            if error:
                QMessageBox.warning(self, "Test capture", error)
                return
        task = self.plan.tasks[0]
        request = CaptureRequest(
            plan=task.frequency_plan(),
            seconds=HISTORICAL_PRESET_S,
            owner=OWNER,
            label=f"Test capture {task.label()}",
            collection_event="Cellular test capture",
            target_class=task.target_class or f"Cellular {task.band_label} {task.direction}",
            recording_format=self._chosen_format() or None,
            excel=False,
            tag=("test",),
        )
        error = self.host.start_capture(request)
        if error:
            release = getattr(self.host, "release_external_owner", None)
            if release is not None:
                release(OWNER)
            QMessageBox.warning(self, "Test capture", error)
            return
        self._test_running = True
        self.status_label.setText(f"Test capture running: {task.label()} ({HISTORICAL_PRESET_S:g} s)…")
        self._sync_buttons(self.runner.status())

    def _on_capture_finished(self, summary) -> None:
        request = getattr(summary, "request", None)
        if request is None or getattr(request, "tag", None) != ("test",) or not self._built:
            return
        self._test_running = False
        release = getattr(self.host, "release_external_owner", None)
        if release is not None:
            release(OWNER)
        outcome = getattr(summary, "outcome", "unknown")
        held = getattr(summary, "held_s", None)
        parts = getattr(summary, "parts", 0)
        if outcome == "confirmed":
            text = f"Test capture confirmed: {parts} part(s), {held if held is not None else '?'} s held of {HISTORICAL_PRESET_S:g} s requested."
        else:
            text = f"Test capture ended with outcome '{outcome}': {getattr(summary, 'detail', '')}"
        self.status_label.setText(text)
        self._host_log(text, "info" if outcome == "confirmed" else "error")
        self._sync_buttons(self.runner.status())

    # ------------------------------------------------------------------ status
    def _on_status(self, status: dict) -> None:
        state = status.get("state", S_IDLE)
        text = status.get("text", "")
        chip = {
            S_RUNNING: "Recording", S_WAITING: "Waiting", S_WAITING_SCHEDULE: "Scheduled", S_PAUSE_REQUESTED: "Pausing",
            S_PAUSED: "Paused", S_PAUSED_BACKLOG: "Paused (backlog)", S_STOP_REQUESTED: "Stopping", S_INTERRUPTED: "Interrupted",
            S_DONE: "Finished", S_IDLE: "Off",
        }.get(state, state)
        self.chip.setText(chip)
        if not self._built:
            if status.get("active") or state in {S_INTERRUPTED, S_DONE} or status.get("missed_rounds"):
                # A session exists: open the panel so its progress and recovery choices are visible.
                self._set_expanded(True)
            return
        if status.get("active") or state in {S_INTERRUPTED, S_DONE}:
            pieces = [text]
            if status.get("tasks_total"):
                pieces.append(f"round {status.get('round_index', 0)}/{status.get('rounds_total', 0)} · task {min(status.get('task_done', 0) + 1, status['tasks_total'])}/{status['tasks_total']}")
            if status.get("current_window"):
                pieces.append(f"{status.get('current_direction', '')} {status['current_window']} ({status.get('current_coverage', '')})")
            if not self._test_running:
                self.status_label.setText("  ·  ".join(p for p in pieces if p))
            details = []
            if status.get("next_round_text") and state != S_RUNNING:
                details.append(f"Next round: {status['next_round_text']} ({status.get('timezone', '')})")
            if status.get("delayed_rounds"):
                details.append(f"{len(status['delayed_rounds'])} round(s) delayed because the previous one was still running")
            if status.get("pending_downloads") or status.get("pending_excel"):
                details.append(f"Pending: {status.get('pending_downloads', 0)} download(s), {status.get('pending_excel', 0)} Excel row(s)")
            if status.get("protocol_results"):
                sheet = status.get("protocol_sheet") or "(sheet not created yet)"
                written = {
                    "saved": "up to date", "writing": "writing…", "deferred": "deferred — the workbook is open in Excel; it is written when closed",
                    "failed": "could not be written (see log; manifests hold the results)",
                }.get(str(status.get("protocol_status") or ""), "pending")
                details.append(f"Protocol workbook: {sheet} — {written}")
            if status.get("remaining_s"):
                details.append(f"Remaining this round ≈ {format_seconds(status['remaining_s'])}, {format_bytes(status.get('remaining_bytes', 0))} (estimate)")
            details.append(f"Confirmed {status.get('confirmed_total', 0)} · uncertain {status.get('uncertain_total', 0)} · failed {status.get('failed_total', 0)}")
            if status.get("pause_reason") and state in {S_PAUSED, S_PAUSED_BACKLOG}:
                details.append(str(status["pause_reason"]))
            self.detail_label.setText("\n".join(details))
            total = int(status.get("tasks_total") or 0)
            self.progress.setVisible(total > 0 and status.get("active", False))
            if total:
                self.progress.setValue(int(100 * int(status.get("task_done") or 0) / total))
        else:
            if not self._test_running:
                self.status_label.setText("Idle" if self.plan is None else "Ready. Preview the plan, then start.")
            self.detail_label.setText("")
            self.progress.setVisible(False)
        self._sync_buttons(status)
        if state in {S_INTERRUPTED, S_WAITING_SCHEDULE, S_DONE} or status.get("missed_rounds"):
            self._refresh_recovery()

    def _sync_buttons(self, status: dict) -> None:
        if not self._built:
            return
        state = status.get("state", S_IDLE)
        active = bool(status.get("active"))
        ready = self.plan is not None and self.plan.ready
        self.start_btn.setEnabled(ready and not active and state != S_INTERRUPTED and not self._test_running)
        self.pause_btn.setEnabled(state in {S_RUNNING, S_WAITING, S_WAITING_SCHEDULE, S_PAUSED_BACKLOG})
        self.resume_btn.setEnabled(state in {S_PAUSED, S_PAUSED_BACKLOG, S_PAUSE_REQUESTED})
        self.stop_btn.setEnabled(active)
        self.test_btn.setEnabled(ready and not active and not self._test_running and state != S_INTERRUPTED)
        self.preview_btn.setEnabled(self.plan is not None)
        self.map_btn.setEnabled(bool(self.workbook_edit.text().strip()))
        self.reload_btn.setEnabled(bool(self.workbook_edit.text().strip()))
        self.coverage_btn.setEnabled(self.runner.session is not None)
        for widget in (self.workbook_edit, self.sheet_combo, self.duration_edit, self.window_spin, self.margin_spin):
            widget.setEnabled(not active)

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
                f"A previous session was interrupted ({session.pause_reason or 'application closed'}). "
                + (f"{len(uncertain)} capture(s) have an uncertain outcome: reconcile with the sensor's files before continuing, or skip them. " if uncertain else "")
                + "Nothing is re-sent automatically."
            )
        if missed:
            lines.append(f"{len(missed)} scheduled round(s) were missed while the application was not running or the computer was asleep: run now or skip. Missed rounds never start on their own.")
        if status.get("state") == S_DONE:
            lines.append("Session finished. Coverage summary and the handover file are in the results folder.")
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
        self.recovery_label.setText("Reconciling with the sensor's files…")

        def done(message: str) -> None:
            self.reconcile_btn.setEnabled(True)
            self._host_log(message)
            self._refresh_recovery()
            QMessageBox.information(self, "Reconcile", message)

        self.runner.reconcile(done)

    def _resume_after_restart(self) -> None:
        error = self.runner.resume_after_restart()
        if error:
            QMessageBox.warning(self, "Resume session", error)
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
            QMessageBox.warning(self, "Missed round", error)
        self._refresh_recovery()

    def _skip_missed(self) -> None:
        missed = self.runner.status().get("missed_rounds") or []
        if not missed:
            return
        self.runner.skip_missed_round(missed[0])
        self._refresh_recovery()

    def _discard(self) -> None:
        answer = QMessageBox.question(
            self,
            "Discard session",
            "Forget this session's progress? Recordings, manifests and the handover file stay on disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.runner.discard_session()
            self._refresh_recovery()

    # ------------------------------------------------------------------ results
    def _show_coverage(self) -> None:
        if self.runner.session is None:
            return
        CoverageDialog(coverage_rows(self.runner.session), self).exec()

    def _open_results_folder(self) -> None:
        folder = self._download_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # noqa: S606 - opens Explorer on the operator's own folder
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])  # noqa: S603,S607
            else:
                subprocess.Popen(["xdg-open", str(folder)])  # noqa: S603,S607
        except OSError as exc:
            QMessageBox.warning(self, "Results folder", str(exc))

    # ------------------------------------------------------------------ sites
    def _on_site_selected(self, name: str) -> None:
        profile = self.settings.sites.get(name.strip())
        if not profile:
            return
        for key, value in (profile.get("scenario") or {}).items():
            widget = self.scenario_fields.get(key)
            if isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
            elif isinstance(widget, QLineEdit):
                widget.setText(str(value))

    def _save_site(self) -> None:
        name = self.site_combo.currentText().strip()
        if not name:
            QMessageBox.information(self, "Save site", "Enter a site name first.")
            return
        existing = self.settings.sites.get(name) or {}
        self.settings.sites[name] = {"site_id": existing.get("site_id") or name, "scenario": self.scenario(), "saved": datetime.now().isoformat(timespec="seconds")}
        if self.site_combo.findText(name) < 0:
            self.site_combo.addItem(name)
        self._save_settings()
        self._host_log(f"Site profile '{name}' saved.")

    def _on_schedule_mode(self, times_on: bool) -> None:
        for widget in (self.times_edit, self.dates_edit, self.daily_check, self.tz_combo):
            widget.setEnabled(times_on)

    def persist(self) -> None:
        """Called by the main window on close."""
        self._save_settings()
