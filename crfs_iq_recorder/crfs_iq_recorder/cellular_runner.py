"""Runs a cellular collection: rounds of captures, waits, downloads, Excel and manifests.

The runner owns the sensor for the whole session (the main window's manual
Start is held) and asks the main window to record one task at a time with
`start_capture`. It learns how each capture ended from the window's
`capture_finished` signal, which fires only after the sensor's files have
settled, and starts the wait interval from that moment. Every state change
is written to ``<config>/cellular/session.json`` so an interrupted session
can be reconciled with the sensor after a restart instead of being resent
blindly.

Everything here runs on the UI thread except downloads and checksums,
which use worker threads with queued signals back to the runner.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal, Slot

from .capture_api import (
    OUTCOME_CONFIRMED,
    OUTCOME_NOT_SENT,
    OUTCOME_SENSOR_ERROR,
    CaptureRequest,
    CaptureSummary,
)
from .cellular_excel import (
    RESULTS_SHEET,
    class_text,
    collection_event_text,
    ensure_results_sheet,
    queue_capture_rows,
    worksheet_for_task,
)
from .cellular_manifest import (
    build_capture_manifest,
    build_handover,
    handover_markdown,
    manifest_path,
    sha256_of_file,
    write_manifest,
)
from .cellular_capabilities import readable_sensor_name
from .cellular_plan import CellularPlan, PlannedTask, format_bytes, format_seconds, summary_line
from .cellular_scheduler import (
    MODE_NOW,
    MODE_TIMES,
    ROUND_DELAYED,
    ROUND_DONE,
    ROUND_DUE,
    ROUND_FAILED,
    ROUND_MISSED,
    ROUND_PARTIAL,
    ROUND_PENDING,
    ROUND_RUNNING,
    ROUND_SKIPPED,
    ROUND_STOPPED,
    RoundRecord,
    Schedule,
    classify_pending,
    make_round,
    materialise_rounds,
    next_due,
    next_pending,
)
from .cellular_workbook import (
    FlushReport,
    PlanRow,
    SheetLayout,
    WorkbookQueue,
    build_row_result,
    flush_queue,
    queue_path,
)
from .diagnostic_log import diag
from .excel_writer import ExcelBusyError
from .operator_text import clip, seconds_text
from .result_view import EXCEL_FAILED, EXCEL_PENDING, EXCEL_UPDATED, ResultView, view_from_task
from .sensor_label import workbook_sensor_name
from .worker_threads import track as track_worker_thread

OWNER = "Excel Collection"
SESSION_SCHEMA = 1
SESSION_FILENAME = "session.json"

# Task states. "confirmed" is the only state that counts as collected.
T_QUEUED = "queued"
T_SUBMITTING = "submitting"
T_RECORDING = "recording"
T_CONFIRMED = "confirmed"
T_UNKNOWN = "unknown"
T_FAILED = "failed"
T_NOT_SENT = "not_sent"
T_SENSOR_ERROR = "sensor_error"
T_SKIPPED = "skipped"
IN_FLIGHT = {T_SUBMITTING, T_RECORDING}
UNCERTAIN = {T_UNKNOWN, T_SUBMITTING, T_RECORDING}

# Runner states.
S_IDLE = "idle"
S_RUNNING = "running"
S_WAITING = "waiting"
S_WAITING_SCHEDULE = "waiting_schedule"
S_PAUSE_REQUESTED = "pause_requested"
S_PAUSED = "paused"
S_PAUSED_BACKLOG = "paused_backlog"
S_STOP_REQUESTED = "stop_requested"
S_INTERRUPTED = "interrupted"
S_DONE = "done"

DL_PENDING = "pending"
DL_ACTIVE = "downloading"
DL_VERIFIED = "verified"
DL_FAILED = "failed"

MAX_DOWNLOADS = 2
DOWNLOAD_RETRIES = 2
NOT_SENT_RETRIES = 1
DEFAULT_BACKLOG_LIMIT = 8
DEFAULT_LOCAL_RESERVE_BYTES = 2 * 1024 * 1024 * 1024
# Seconds between a confirmed capture and the fresh listing that precedes its
# downloads (the sensor finishes headers and sidecars after reporting done).
FINALIZE_SETTLE_S = 5.0
TICK_MS = 500
# When the sensor still lists the previous remote-recording mission after the
# bounded poll, the next request waits this long before the list is read again.
IDLE_RETRY_WAIT_S = 10.0
IDLE_RETRIES = 3
# A gap between two ticks longer than this means the computer slept or the
# application was frozen; the schedule is checked again and the user is told.
SLEEP_GAP_S = 120.0
# Bursts of workbook results (several rows in a few seconds) are written together.
WORKBOOK_COALESCE_S = 3.0
WORKBOOK_BUSY_RETRY_S = 15.0
# A write that failed for another reason (file missing, backup folder full) is retried too,
# more slowly; the queue is on disk and every write reconciles, so nothing is duplicated.
WORKBOOK_ERROR_RETRY_S = 60.0


@dataclass
class CampaignSettings:
    campaign: str = ""
    campaign_id: str = ""
    site: str = ""
    site_id: str = ""
    visit_id: str = ""
    fallback_duration_s: float | None = None
    wait_s: float = 10.0
    wait_unit: str = "seconds"
    window_hz: int = 0
    edge_margin_hz: int = 0
    auto_download: bool = True
    verify_checksums: bool = True
    merge_parts: bool = False
    quality_checks: bool = True
    previews: bool = True
    backlog_limit: int = DEFAULT_BACKLOG_LIMIT
    local_reserve_bytes: int = DEFAULT_LOCAL_RESERVE_BYTES
    # EMP recording_format identifier; "" means the recorder's own Format setting.
    recording_format: str = ""
    scenario: dict[str, Any] = field(default_factory=dict)
    protocol_path: str = ""
    protocol_sheet: str = ""
    # Fill the protocol worksheet's own result columns (needs the layout and
    # the plan rows read at import time; appended result blocks are never
    # read back as instructions).
    protocol_results: bool = False
    protocol_layout: dict[str, Any] = field(default_factory=dict)
    plan_rows: list[dict[str, Any]] = field(default_factory=list)
    # Sensor the plan and its capability figures were made for.
    sensor_model: str = ""
    sensor_serial: str = ""

    def layout(self) -> SheetLayout | None:
        if not self.protocol_layout:
            return None
        try:
            return SheetLayout.from_dict(self.protocol_layout)
        except (TypeError, ValueError):
            return None

    def rows(self) -> list[PlanRow]:
        out: list[PlanRow] = []
        for item in self.plan_rows:
            try:
                out.append(PlanRow.from_dict(item))
            except (TypeError, ValueError):
                continue
        return out

    def to_dict(self) -> dict[str, Any]:
        return {
            "campaign": self.campaign,
            "campaign_id": self.campaign_id,
            "site": self.site,
            "site_id": self.site_id,
            "visit_id": self.visit_id,
            "fallback_duration_s": self.fallback_duration_s,
            "wait_s": self.wait_s,
            "wait_unit": self.wait_unit,
            "window_hz": self.window_hz,
            "edge_margin_hz": self.edge_margin_hz,
            "auto_download": self.auto_download,
            "verify_checksums": self.verify_checksums,
            "merge_parts": self.merge_parts,
            "quality_checks": self.quality_checks,
            "previews": self.previews,
            "backlog_limit": self.backlog_limit,
            "local_reserve_bytes": self.local_reserve_bytes,
            "recording_format": self.recording_format,
            "scenario": dict(self.scenario),
            "protocol_path": self.protocol_path,
            "protocol_sheet": self.protocol_sheet,
            "protocol_results": self.protocol_results,
            "protocol_layout": dict(self.protocol_layout),
            "plan_rows": [dict(item) for item in self.plan_rows],
            "sensor_model": self.sensor_model,
            "sensor_serial": self.sensor_serial,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "CampaignSettings":
        data = data or {}
        fallback = data.get("fallback_duration_s")
        try:
            fallback_value = float(fallback) if fallback is not None else None
        except (TypeError, ValueError):
            fallback_value = None
        settings = cls(
            campaign=str(data.get("campaign") or ""),
            campaign_id=str(data.get("campaign_id") or ""),
            site=str(data.get("site") or ""),
            site_id=str(data.get("site_id") or ""),
            visit_id=str(data.get("visit_id") or ""),
            fallback_duration_s=fallback_value,
            wait_s=max(float(data.get("wait_s", 10.0) or 0.0), 0.0),
            wait_unit="minutes" if str(data.get("wait_unit") or "").casefold() == "minutes" else "seconds",
            window_hz=int(data.get("window_hz") or 0),
            edge_margin_hz=max(int(data.get("edge_margin_hz") or 0), 0),
            auto_download=bool(data.get("auto_download", True)),
            verify_checksums=bool(data.get("verify_checksums", True)),
            merge_parts=bool(data.get("merge_parts", False)),
            quality_checks=bool(data.get("quality_checks", True)),
            previews=bool(data.get("previews", True)),
            backlog_limit=max(int(data.get("backlog_limit") or DEFAULT_BACKLOG_LIMIT), 1),
            local_reserve_bytes=max(int(data.get("local_reserve_bytes") or DEFAULT_LOCAL_RESERVE_BYTES), 0),
            recording_format=str(data.get("recording_format") or ""),
            scenario={str(k): v for k, v in (data.get("scenario") or {}).items()},
            protocol_path=str(data.get("protocol_path") or ""),
            protocol_sheet=str(data.get("protocol_sheet") or ""),
            protocol_results=bool(data.get("protocol_results", False)),
            protocol_layout={str(k): v for k, v in (data.get("protocol_layout") or {}).items()},
            plan_rows=[dict(item) for item in data.get("plan_rows") or [] if isinstance(item, dict)],
            sensor_model=str(data.get("sensor_model") or ""),
            sensor_serial=str(data.get("sensor_serial") or ""),
        )
        return settings


@dataclass
class TaskState:
    task_key: str
    round_id: str
    status: str = T_QUEUED
    attempts: int = 0
    record_id: str | None = None
    started_utc: str | None = None
    finished_utc: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    manifest_path: str = ""
    excel_status: str = "not_applicable"
    quality: dict[str, Any] = field(default_factory=dict)
    merged_output: str = ""
    short: bool = False
    notes: list[str] = field(default_factory=list)
    # completed / partial / unconfirmed / failed, measured from the files (capture_measure).
    result: str = ""
    result_reason: str = ""
    # Earlier attempts of this task (a retry is a new attempt; the original result is kept here).
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_key": self.task_key,
            "round_id": self.round_id,
            "status": self.status,
            "attempts": self.attempts,
            "record_id": self.record_id,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "summary": dict(self.summary),
            "files": [dict(item) for item in self.files],
            "manifest_path": self.manifest_path,
            "excel_status": self.excel_status,
            "quality": dict(self.quality),
            "merged_output": self.merged_output,
            "short": self.short,
            "notes": list(self.notes),
            "result": self.result,
            "result_reason": self.result_reason,
            "history": [dict(item) for item in self.history],
        }

    def attempt_record(self) -> dict[str, Any]:
        """This attempt as kept in `history` when the task is tried again."""
        return {
            "attempt": self.attempts,
            "status": self.status,
            "result": self.result,
            "result_reason": self.result_reason,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "record_id": self.record_id,
            "files": [dict(item) for item in self.files],
            "manifest_path": self.manifest_path,
            "excel_status": self.excel_status,
            "summary": dict(self.summary),
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskState":
        return cls(
            task_key=str(data.get("task_key") or ""),
            round_id=str(data.get("round_id") or ""),
            status=str(data.get("status") or T_QUEUED),
            attempts=int(data.get("attempts") or 0),
            record_id=data.get("record_id") or None,
            started_utc=data.get("started_utc") or None,
            finished_utc=data.get("finished_utc") or None,
            summary=dict(data.get("summary") or {}),
            files=[dict(item) for item in data.get("files") or []],
            manifest_path=str(data.get("manifest_path") or ""),
            excel_status=str(data.get("excel_status") or "not_applicable"),
            quality=dict(data.get("quality") or {}),
            merged_output=str(data.get("merged_output") or ""),
            short=bool(data.get("short", False)),
            notes=[str(item) for item in data.get("notes") or []],
            result=str(data.get("result") or ""),
            result_reason=str(data.get("result_reason") or ""),
            history=[dict(item) for item in data.get("history") or [] if isinstance(item, dict)],
        )


# How a task ended, as the end-of-pass summary counts it.
C_COMPLETED = "completed"
C_PARTIAL = "partial"
C_FAILED = "failed"
C_UNCERTAIN = "uncertain"
C_SKIPPED = "skipped"
C_PENDING = "pending"


def task_class(state: "TaskState") -> str:
    """completed / partial / failed / uncertain / skipped / pending."""
    if state.status == T_CONFIRMED:
        result = state.result or (C_PARTIAL if state.short else C_COMPLETED)
        if result == C_COMPLETED:
            return C_COMPLETED
        if result == C_PARTIAL:
            return C_PARTIAL
        return C_UNCERTAIN  # files found, duration unknown
    if state.status in {T_NOT_SENT, T_SENSOR_ERROR}:
        return C_FAILED
    if state.status in UNCERTAIN or state.status == T_FAILED:
        return C_UNCERTAIN
    if state.status == T_SKIPPED:
        return C_SKIPPED
    return C_PENDING


@dataclass
class SessionState:
    settings: CampaignSettings
    plan: CellularPlan
    schedule: Schedule
    protocol_snapshot: str = ""
    protocol_version: str = ""
    rounds: list[RoundRecord] = field(default_factory=list)
    tasks: dict[str, list[TaskState]] = field(default_factory=dict)
    current_round_id: str | None = None
    runner_state: str = S_IDLE
    pause_reason: str = ""
    created_utc: str = ""
    updated_utc: str = ""
    sensor: dict[str, Any] = field(default_factory=dict)
    timezone_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SESSION_SCHEMA,
            "settings": self.settings.to_dict(),
            "plan": self.plan.to_dict(),
            "schedule": self.schedule.to_dict(),
            "protocol_snapshot": self.protocol_snapshot,
            "protocol_version": self.protocol_version,
            "rounds": [record.to_dict() for record in self.rounds],
            "tasks": {rid: [task.to_dict() for task in items] for rid, items in self.tasks.items()},
            "current_round_id": self.current_round_id,
            "runner_state": self.runner_state,
            "pause_reason": self.pause_reason,
            "created_utc": self.created_utc,
            "updated_utc": self.updated_utc,
            "sensor": dict(self.sensor),
            "timezone_label": self.timezone_label,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionState":
        return cls(
            settings=CampaignSettings.from_dict(data.get("settings")),
            plan=CellularPlan.from_dict(data.get("plan") or {}),
            schedule=Schedule.from_dict(data.get("schedule")),
            protocol_snapshot=str(data.get("protocol_snapshot") or ""),
            protocol_version=str(data.get("protocol_version") or ""),
            rounds=[RoundRecord.from_dict(item) for item in data.get("rounds") or []],
            tasks={
                str(rid): [TaskState.from_dict(item) for item in items]
                for rid, items in (data.get("tasks") or {}).items()
            },
            current_round_id=data.get("current_round_id") or None,
            runner_state=str(data.get("runner_state") or S_IDLE),
            pause_reason=str(data.get("pause_reason") or ""),
            created_utc=str(data.get("created_utc") or ""),
            updated_utc=str(data.get("updated_utc") or ""),
            sensor=dict(data.get("sensor") or {}),
            timezone_label=str(data.get("timezone_label") or ""),
        )

    def round(self, round_id: str | None) -> RoundRecord | None:
        if not round_id:
            return None
        return next((record for record in self.rounds if record.round_id == round_id), None)

    def task_by_key(self, round_id: str, task_key: str) -> TaskState | None:
        return next((task for task in self.tasks.get(round_id, []) if task.task_key == task_key), None)

    def planned(self, task_key: str) -> PlannedTask | None:
        return next((task for task in self.plan.tasks if task.task_key == task_key), None)


def session_path(config_dir: Path) -> Path:
    folder = Path(config_dir) / "cellular"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / SESSION_FILENAME


def load_session(config_dir: Path) -> SessionState | None:
    from .json_store import read_json

    path = session_path(config_dir)
    if not path.is_file():
        return None
    data = read_json(path)
    if not isinstance(data, dict) or not data.get("plan"):
        return None
    try:
        return SessionState.from_dict(data)
    except Exception:  # noqa: BLE001 - a damaged session file must not block the application
        return None


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _ChecksumWorker(QObject):
    done = Signal(str, str, str)  # task_key, name, sha256 or ""

    def __init__(self, task_key: str, name: str, path: str) -> None:
        super().__init__()
        self._task_key = task_key
        self._name = name
        self._path = path

    @Slot()
    def run(self) -> None:
        try:
            digest = sha256_of_file(Path(self._path))
        except OSError:
            digest = ""
        self.done.emit(self._task_key, self._name, digest)
        thread = self.thread()
        if thread is not None:
            thread.quit()


class _QualityWorker(QObject):
    """Validates the downloaded parts of one capture and draws its sampled preview."""

    done = Signal(str, str, dict)  # round_id, task_key, quality dict

    def __init__(
        self,
        round_id: str,
        task_key: str,
        paths: list[str],
        *,
        expected_duration_s: float | None,
        expected_center_hz: int | None,
        expected_bandwidth_hz: int | None,
        expected_sample_rate_hz: int | None,
        expected_parts: list[str],
        preview_path: str,
        label: str,
    ) -> None:
        super().__init__()
        self._round_id = round_id
        self._task_key = task_key
        self._paths = paths
        self._duration = expected_duration_s
        self._center = expected_center_hz
        self._bandwidth = expected_bandwidth_hz
        self._rate = expected_sample_rate_hz
        self._parts = expected_parts
        self._preview = preview_path
        self._label = label

    @Slot()
    def run(self) -> None:
        from . import cellular_quality as cq

        try:
            report = cq.validate_capture(
                [Path(p) for p in self._paths],
                expected_duration_s=self._duration,
                expected_center_hz=self._center,
                expected_bandwidth_hz=self._bandwidth,
                expected_sample_rate_hz=self._rate,
                expected_parts=self._parts,
            )
            if self._preview and report.level not in {cq.LEVEL_ERROR, cq.LEVEL_NOT_CHECKED}:
                try:
                    report.preview_path = str(cq.write_preview(self._paths, self._preview, center_hz=self._center, label=self._label))
                except Exception as exc:  # noqa: BLE001 - the preview is optional
                    report.warnings.append(f"Preview not drawn: {exc}")
            payload = report.to_dict()
        except Exception as exc:  # noqa: BLE001 - never let a check crash the session
            payload = {"level": "unknown", "flags": ["check_failed"], "errors": [f"Quality check failed: {exc}"], "warnings": [], "files": []}
        self.done.emit(self._round_id, self._task_key, payload)
        thread = self.thread()
        if thread is not None:
            thread.quit()


class CellularRunner(QObject):
    """Executes a cellular collection session against the main window's recorder."""

    log = Signal(str, str)  # message, level
    status_changed = Signal(dict)
    session_changed = Signal()
    round_finished = Signal(str)

    def __init__(
        self,
        host,
        *,
        config_dir: Path,
        download_dir: Callable[[], Path],
        clock: Callable[[], datetime] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.host = host
        self._config_dir = Path(config_dir)
        self._download_dir = download_dir
        self._clock = clock or (lambda: datetime.now().astimezone())
        self.session: SessionState | None = None
        self.state = S_IDLE
        self._wait_deadline = 0.0
        self._wait_total = 0.0
        self._capture_in_flight = False
        self._busy_logged = False
        self._downloads_pending: list[tuple[str, str, dict[str, Any]]] = []
        self._downloads_active: list[tuple[QThread, QObject, str, str]] = []
        self._checksums: list[tuple[QThread, QObject]] = []
        self._quality_jobs: list[tuple[QThread, QObject]] = []
        self._quality_pending: list[QObject] = []
        self._refreshes = 0  # listings scheduled before a capture's downloads start
        # A finished session reopened with downloads that never completed: Continue only downloads them.
        self._downloads_only = False
        self._excel_retry: set[tuple[str, str]] = set()
        # Protocol workbook results: a persistent queue flushed on a worker,
        # one flush at a time, bursts coalesced.
        self._workbook_queue: WorkbookQueue | None = None
        self._workbook_flushing = False
        self._workbook_flush_again = False
        self._workbook_not_before = 0.0
        self._workbook_busy_logged = False
        self._workbook_status = ""
        self._workbook_note = ""
        # Pre-request check of the sensor's mission list (one at a time).
        self._idle_check_pending = False
        self._idle_retries = 0
        self._last_tick_wall = 0.0
        self._save_error_noted = False
        self._status_text = "Idle"
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        signal = getattr(host, "capture_finished", None)
        if signal is not None:
            signal.connect(self.on_capture_finished, Qt.ConnectionType.QueuedConnection)
        self.session = load_session(self._config_dir)
        if self.session is not None:
            if self.session.runner_state in {S_RUNNING, S_WAITING, S_PAUSE_REQUESTED, S_STOP_REQUESTED, S_WAITING_SCHEDULE, S_PAUSED_BACKLOG}:
                self.state = S_INTERRUPTED
            elif self.session.runner_state in {S_PAUSED, S_INTERRUPTED}:
                self.state = S_INTERRUPTED
            else:
                self.state = self.session.runner_state if self.session.runner_state == S_DONE else S_IDLE
            if self.state == S_DONE and not self.session.plan.legacy and self.unfinished_downloads():
                # Closed before its downloads finished: the files are on the sensor, nothing says so otherwise.
                self.state = S_INTERRUPTED
                self._downloads_only = True
            if self.session.plan.legacy:
                # Saved by 1.10.0-1.12.0 (possibly with automatic sub-band requests):
                # its results are kept, it is never continued.
                QTimer.singleShot(0, self, self._note_legacy_session)
            elif self.state == S_INTERRUPTED and not self._downloads_only and self._mark_missed_while_closed(self._clock()):
                # Logged once the panel listens; the window is still being built here.
                QTimer.singleShot(0, self, self._note_missed_rounds)
        # Results that could not be written before the last exit (Excel had the
        # workbook, the application was closed) are written now, whether or not
        # the collection itself is finished; a busy workbook is retried.
        if queue_path(self._config_dir).is_file() and self.workbook_pending() > 0:
            QTimer.singleShot(1500, self, self._flush_workbook)

    # ------------------------------------------------------------------ helpers
    def _log(self, message: str, level: str = "info") -> None:
        """One short operator message (Activity panel); details go to the diagnostic log."""
        self.log.emit(message, level)

    def _save(self) -> None:
        if self.session is None:
            return
        from .json_store import write_json

        self.session.runner_state = self.state
        self.session.updated_utc = _utc_now_text()
        try:
            write_json(session_path(self._config_dir), self.session.to_dict())
            self._save_error_noted = False
        except OSError as exc:
            diag("session_save_failed", error=f"{type(exc).__name__}: {exc}")
            if not self._save_error_noted:
                self._save_error_noted = True
                self._log("Could not save the collection state. See details.", "error")
        self.session_changed.emit()

    def _sensor_dict(self) -> dict[str, Any]:
        info = getattr(self.host, "_sensor_info", None)
        conn = getattr(self.host, "_conn", None)
        host_text = getattr(conn, "host", "") if conn is not None else ""
        model = _identity_text(getattr(info, "model", ""))
        return {
            "model": model,
            "serial": _identity_text(getattr(info, "serial", "")),
            "firmware": _identity_text(getattr(info, "firmware", "")),
            "name": readable_sensor_name(model),
            "host_label": "sensor" if host_text else "",
        }

    def _sensor_name(self) -> str:
        """Readable sensor name for the worksheet ('CRFS 100-18'), from the verified identity."""
        sensor = (self.session.sensor if self.session is not None else None) or self._sensor_dict()
        return str(sensor.get("name") or readable_sensor_name(str(sensor.get("model") or "")) or "")

    def identity_mismatch(self, settings: CampaignSettings | None = None) -> str | None:
        """Text when the connected sensor is not the one the plan was made for; None when it is."""
        settings = settings or (self.session.settings if self.session is not None else None)
        if settings is None:
            return None
        info = getattr(self.host, "_sensor_info", None)
        model = _identity_text(getattr(info, "model", ""))
        serial = _identity_text(getattr(info, "serial", ""))
        connected = bool(getattr(info, "connected", False)) or bool(model or serial)
        wanted_model, wanted_serial = settings.sensor_model.strip(), settings.sensor_serial.strip()
        if not (wanted_model or wanted_serial):
            return None
        if not connected:
            return (
                f"The sensor's identity could not be read. The plan was made for {readable_sensor_name(wanted_model) or wanted_model}"
                + (f" (serial {wanted_serial})" if wanted_serial else "")
                + "; check the connection and open the preview again."
            )
        if wanted_serial and serial and wanted_serial.casefold() != serial.casefold() or (
            wanted_model and model and wanted_model.casefold() != model.casefold()
        ):
            return (
                f"The connected sensor is {readable_sensor_name(model) or model or 'unknown'}"
                + (f" (serial {serial})" if serial else "")
                + f", but the plan and its recording settings were made for {readable_sensor_name(wanted_model) or wanted_model}"
                + (f" (serial {wanted_serial})" if wanted_serial else "")
                + ". Open the preview again so the plan is rebuilt for this sensor."
            )
        return None

    def current_round(self) -> RoundRecord | None:
        if self.session is None:
            return None
        return self.session.round(self.session.current_round_id)

    def current_tasks(self) -> list[TaskState]:
        if self.session is None or not self.session.current_round_id:
            return []
        return self.session.tasks.get(self.session.current_round_id, [])

    def active(self) -> bool:
        return self.state not in {S_IDLE, S_DONE, S_INTERRUPTED}

    # ------------------------------------------------------------------ session control
    def start_session(
        self,
        plan: CellularPlan,
        schedule: Schedule,
        settings: CampaignSettings,
        *,
        protocol_snapshot: str = "",
    ) -> str | None:
        """Begin a new session. Returns an error text when it cannot start."""
        if self.active():
            return "A cellular collection is already running."
        previous = self.previous_work_reason()
        if previous:
            return previous
        if plan.legacy:
            return "This plan was made by an older version. Open the preview to build a fresh plan."
        if not plan.ready:
            return "The plan has errors; fix them in the preview before starting."
        mismatch = self.identity_mismatch(settings)
        if mismatch:
            return mismatch
        reserve = getattr(self.host, "set_external_owner", None)
        if reserve is not None:
            error = reserve(OWNER)
            if error:
                return error
        now = self._clock()
        session = SessionState(
            settings=settings,
            plan=plan,
            schedule=schedule,
            protocol_snapshot=protocol_snapshot,
            protocol_version=plan.protocol_version,
            created_utc=_utc_now_text(),
            sensor=self._sensor_dict(),
            timezone_label=schedule.tz.describe(now),
        )
        if schedule.mode == MODE_NOW:
            record = make_round(now, settings.campaign, len(plan.tasks))
            record.status = ROUND_DUE
            session.rounds = [record]
        else:
            session.rounds = self._cap_rounds(
                materialise_rounds(schedule, [], campaign=settings.campaign, tasks_total=len(plan.tasks), now=now), schedule
            )
            if not session.rounds:
                self._release()
                return "The schedule has no rounds."
            classify_pending(session.rounds, now=now, busy=False)
        self._archive_previous_session()
        self.session = session
        self.state = S_WAITING_SCHEDULE
        self._downloads_only = False
        self._capture_in_flight = False
        self._idle_check_pending = False
        self._idle_retries = 0
        self._last_tick_wall = time.time()
        if self.protocol_results_enabled():
            # Every session backs the workbook up once more before its first change.
            self.workbook_queue().forget_backup(Path(settings.protocol_path))
        elif settings.protocol_path:
            missing = (
                "no worksheet selected" if not settings.protocol_sheet
                else "no result columns in this worksheet" if settings.layout() is None or not settings.protocol_results
                else "no stored plan rows"
            )
            diag("workbook_not_updated", missing)
            self._log("The protocol workbook is not updated this session. Results stay in the manifests.", "error")
        diag(
            "collection_started",
            rows=plan.row_count,
            requests=len(plan.tasks),
            duration_s=plan.fallback_duration_s,
            wait_s=settings.wait_s,
            repeat=schedule.repeat,
            schedule=schedule.describe(),
            sensor=session.sensor,
        )
        self._log(f"Collection started: {summary_line(plan, wait_s=settings.wait_s, passes=schedule.repeat)}.")
        self._save()
        self._timer.start()
        self._tick()
        return None

    def _cap_rounds(self, rounds: list[RoundRecord], schedule: Schedule) -> list[RoundRecord]:
        """Timed schedules: keep at most `schedule.repeat` passes when a count above one is set.

        0 and 1 mean "every listed time" (a daily schedule then runs until it
        is stopped or its end date passes).
        """
        if schedule.repeat <= 1:
            return rounds
        return rounds[: schedule.repeat]

    def _extend_schedule(self, now: datetime) -> None:
        """Materialise further daily rounds as the horizon moves (bounded by the repeat count)."""
        session = self.session
        if session is None or session.schedule.mode != MODE_TIMES or not session.schedule.daily:
            return
        if session.schedule.repeat > 1 and len(session.rounds) >= session.schedule.repeat:
            return
        before = len(session.rounds)
        extended = materialise_rounds(
            session.schedule, session.rounds, campaign=session.settings.campaign, tasks_total=len(session.plan.tasks), now=now
        )
        session.rounds = self._cap_rounds(extended, session.schedule)
        if len(session.rounds) != before:
            self._save()

    def _next_pass_after(self, now: datetime) -> None:
        """Run-now mode: queue the next pass after the wait interval (repeat count or until stopped)."""
        session = self.session
        if session is None or session.schedule.mode != MODE_NOW:
            return
        finished = sum(1 for r in session.rounds if r.finished)
        if session.schedule.repeat > 0 and finished >= session.schedule.repeat:
            return
        from datetime import timedelta

        when = now + timedelta(seconds=max(session.settings.wait_s, 1.0))
        record = make_round(when, session.settings.campaign, len(session.plan.tasks))
        if any(r.round_id == record.round_id for r in session.rounds):
            record.round_id += f"-{finished + 1}"
        session.rounds.append(record)
        total = "" if session.schedule.repeat == 0 else f" of {session.schedule.repeat}"
        self._log(f"Pass {finished + 1}{total} starts at {when:%H:%M:%S}.")

    def pause(self) -> None:
        if self.state in {S_RUNNING}:
            self.state = S_PAUSE_REQUESTED
            self._log("Pausing after the current recording.")
        elif self.state in {S_WAITING, S_WAITING_SCHEDULE, S_PAUSED_BACKLOG}:
            self._wait_deadline = 0.0
            self.state = S_PAUSED
            self.session.pause_reason = "Paused by the operator."
            self._log("Paused.")
        self._save()
        self._emit_status()

    def resume(self) -> None:
        if self.state == S_PAUSE_REQUESTED:
            self.state = S_RUNNING
            self._log("Pause cancelled. The collection continues.")
        elif self.state in {S_PAUSED, S_PAUSED_BACKLOG}:
            self.session.pause_reason = ""
            self.state = S_WAITING_SCHEDULE if self.current_round() is None else S_RUNNING
            self._log("Resumed.")
            if self.current_round() is not None and self.session.settings.wait_s > 0 and self._last_task_finished():
                self._begin_wait(self.session.settings.wait_s)
        self._save()
        self._tick()

    def stop(self) -> None:
        """Let the current capture finish and be logged; cancel anything pending."""
        if self.session is None or self.state in {S_IDLE, S_DONE}:
            return
        if self._capture_in_flight:
            self.state = S_STOP_REQUESTED
            self._log("Stopping after the current recording.")
            self._save()
            self._emit_status()
            return
        self._wait_deadline = 0.0
        self._log("Stopped. Nothing else was sent.")
        if self.current_round() is not None:
            self._finish_round(stopped=True)
        self._finish_session(stopped=True)

    def discard_session(self) -> None:
        """Forget an interrupted or finished session (files and manifests stay)."""
        if self.active() or self.previous_work_reason():
            return
        path = session_path(self._config_dir)
        try:
            if path.is_file():
                path.replace(path.with_name(f"session_{datetime.now():%Y%m%d_%H%M%S}.discarded.json"))
        except OSError:
            pass
        self.session = None
        self.state = S_IDLE
        self._downloads_only = False
        self._emit_status()
        self.session_changed.emit()

    def _archive_previous_session(self) -> None:
        """Keep the state file of the session a new one replaces (its results stay readable)."""
        path = session_path(self._config_dir)
        if self.session is None or not path.is_file():
            return
        try:
            path.replace(path.with_name(f"session_{datetime.now():%Y%m%d_%H%M%S}.previous.json"))
        except OSError as exc:
            diag("session_archive_failed", error=f"{type(exc).__name__}: {exc}")

    def _release(self) -> None:
        release = getattr(self.host, "release_external_owner", None)
        if release is not None:
            release(OWNER)

    def _last_task_finished(self) -> bool:
        tasks = self.current_tasks()
        return any(task.status not in {T_QUEUED} for task in tasks)

    # ------------------------------------------------------------------ main loop
    @Slot()
    def _tick(self) -> None:
        if self.session is None:
            self._timer.stop()
            return
        self._drain_downloads()
        self._refresh_excel_status()
        self._flush_workbook()
        self._note_sleep_gap()
        if self.state in {S_IDLE, S_DONE, S_INTERRUPTED, S_PAUSED}:
            self._emit_status()
            return
        if self.state == S_PAUSED_BACKLOG:
            reason = self._backlog_reason()
            if reason is None:
                self.session.pause_reason = ""
                self._log("Downloads caught up. The collection continues.")
                self.state = S_RUNNING if self.current_round() is not None else S_WAITING_SCHEDULE
            else:
                self.session.pause_reason = reason
                self._emit_status()
                return
        now = self._clock()
        if self.current_round() is None:
            self._extend_schedule(now)
            classify_pending(self.session.rounds, now=now, busy=False)
            self._note_missed_rounds()
            due = next_due(self.session.rounds)
            if due is not None:
                self._start_round(due, now)
            elif next_pending(self.session.rounds, now=now) is None and not any(
                record.status == ROUND_MISSED for record in self.session.rounds
            ):
                self._finish_session()
                return
            else:
                self.state = S_WAITING_SCHEDULE
            self._emit_status()
            return
        classify_pending(self.session.rounds, now=now, busy=True)
        if self._capture_in_flight:
            self._emit_status()
            return
        if self.state == S_WAITING:
            if time.monotonic() >= self._wait_deadline:
                self.state = S_RUNNING
                self._start_next_task()
            self._emit_status()
            return
        if self.state in {S_RUNNING, S_PAUSE_REQUESTED, S_STOP_REQUESTED}:
            # With no capture in flight these requests take effect at once.
            self._start_next_task()
        self._emit_status()

    def _start_round(self, record: RoundRecord, now: datetime) -> None:
        delayed = record.status == ROUND_DELAYED
        record.status = ROUND_RUNNING
        record.actual_start_utc = now.astimezone(timezone.utc)
        self.session.current_round_id = record.round_id
        self.session.tasks[record.round_id] = [
            TaskState(task_key=task.task_key, round_id=record.round_id) for task in self.session.plan.tasks
        ]
        self.state = S_RUNNING
        late = (record.actual_start_utc - record.scheduled_utc).total_seconds()
        index = self.session.rounds.index(record) + 1 if record in self.session.rounds else len(self.session.rounds)
        rows = self.session.plan.row_count
        text = f"Pass {index} started: {rows} row" + ("" if rows == 1 else "s")
        if delayed:
            text += f", {format_seconds(late)} late (the previous pass was still running)"
        self._log(text + ".")
        self._save()
        self._start_next_task()

    def _next_queued(self) -> tuple[TaskState, PlannedTask] | None:
        for state in self.current_tasks():
            if state.status == T_QUEUED:
                planned = self.session.planned(state.task_key)
                if planned is not None:
                    return state, planned
        return None

    def _start_next_task(self) -> None:
        if self._capture_in_flight or self.session is None:
            return
        if self.state == S_STOP_REQUESTED:
            self._finish_round(stopped=True)
            self._finish_session(stopped=True)
            return
        if self.state == S_PAUSE_REQUESTED:
            self.state = S_PAUSED
            self.session.pause_reason = "Paused by the operator after the last recording."
            self._log("Paused.")
            self._save()
            return
        item = self._next_queued()
        if item is None:
            self._finish_round()
            return
        state, planned = item
        reason = self._backlog_reason(planned)
        if reason is not None:
            self.state = S_PAUSED_BACKLOG
            self.session.pause_reason = reason
            self._log(reason, "error")
            self._save()
            return
        if self._idle_check_pending:
            return
        verify = getattr(self.host, "verify_sensor_idle", None)
        if verify is None:
            self._submit_task(state, planned)
            return
        # The Node keeps the previous remote-recording mission open for a
        # moment after the stream closes and refuses a new one meanwhile
        # ("Unable to schedule the command"). Read its mission list first.
        self._idle_check_pending = True
        round_id, task_key = state.round_id, state.task_key

        def on_result(ok: bool, detail: str) -> None:
            self._idle_check_pending = False
            if self.session is None or self._capture_in_flight:
                return
            current = self.session.task_by_key(round_id, task_key)
            if current is None or current.status != T_QUEUED or self.session.current_round_id != round_id:
                return
            if self.state not in {S_RUNNING, S_PAUSE_REQUESTED, S_STOP_REQUESTED}:
                return
            if self.state != S_RUNNING:
                # Pause/Stop arrived while the list was read: honour it now.
                self._start_next_task()
                return
            if not ok:
                self._idle_retries += 1
                diag("sensor_busy", detail, retry=self._idle_retries)
                if self._idle_retries > IDLE_RETRIES:
                    self.state = S_PAUSED
                    self.session.pause_reason = "The sensor is still busy with another recording. Check it, then Resume."
                    self._log("Paused: the sensor is still busy with another recording. See details.", "error")
                    self._save()
                    self._emit_status()
                    return
                self._log(f"The sensor is still busy. Checking again in {IDLE_RETRY_WAIT_S:g} s.", "error")
                self._begin_wait(IDLE_RETRY_WAIT_S)
                self._emit_status()
                return
            self._idle_retries = 0
            if detail:
                diag("sensor_idle", detail)
            self._submit_task(current, planned)

        try:
            verify(on_result)
        except Exception as exc:  # noqa: BLE001 - the check is advisory; the request itself is verified by the files
            self._idle_check_pending = False
            diag("sensor_idle_check_skipped", f"{type(exc).__name__}: {exc}")
            self._submit_task(state, planned)

    def _submit_task(self, state: TaskState, planned: PlannedTask) -> None:
        if self._capture_in_flight or self.session is None:
            return
        settings = self.session.settings
        request = CaptureRequest(
            plan=planned.frequency_plan(),
            seconds=planned.duration_s,
            owner=OWNER,
            label=planned.label(),
            collection_event=collection_event_text(settings.campaign, settings.site, state.round_id),
            target_class=class_text(planned.to_dict()),
            worksheet="",
            recording_format=settings.recording_format or None,
            excel=False,
            tag=(state.round_id, state.task_key),
        )
        error = self.host.start_capture(request)
        if error:
            lowered = error.casefold()
            if "in progress" in lowered or "still being confirmed" in lowered or "series" in lowered:
                if not self._busy_logged:
                    diag("recorder_busy", error)
                    self._busy_logged = True
                return
            if "storage" in lowered:
                self.state = S_PAUSED_BACKLOG
                self.session.pause_reason = error
                self._log(f"Paused: {error}", "error")
                self._save()
                return
            if "invalid" in lowered or "recording time" in lowered:
                # Only this task is affected: record the refusal and move on.
                state.status = T_NOT_SENT
                state.attempts += 1
                state.result = "failed"
                state.result_reason = "not sent"
                state.notes.append(f"Not sent: {error}")
                state.finished_utc = _utc_now_text()
                self._log(f"{self._row_position(planned).capitalize()} not sent: {error}", "error")
                self._write_task_manifest(state, planned)
                self._save()
                self._after_task(state, planned)
                return
            # Connection settings or another condition the operator must fix:
            # pause instead of failing every remaining task in turn.
            self.state = S_PAUSED
            self.session.pause_reason = f"{error} Fix it, then Resume."
            self._log(f"Paused: {error}", "error")
            self._save()
            return
        self._busy_logged = False
        self._capture_in_flight = True
        state.status = T_SUBMITTING
        state.attempts += 1
        state.started_utc = _utc_now_text()
        state.result = ""
        state.result_reason = ""
        repeat = f", repeat {planned.repetition} of {planned.repetitions}" if planned.repetitions > 1 else ""
        attempt = f" (attempt {state.attempts})" if state.attempts > 1 else ""
        self._status_text = f"Recording {self._row_position(planned)}"
        self._log(
            f"Recording {self._row_position(planned)}{repeat}: {planned.range_text} "
            f"for {seconds_text(planned.duration_s)}{attempt}."
        )
        self._save()

    def _row_position(self, planned: PlannedTask) -> str:
        """'row 3 of 15': the row's place among the rows of the plan (worksheet order)."""
        if self.session is None:
            return f"row {planned.row}"
        order: list[str] = []
        for task in self.session.plan.tasks:
            if task.entry_id not in order:
                order.append(task.entry_id)
        if planned.entry_id in order:
            return f"row {order.index(planned.entry_id) + 1} of {len(order)}"
        return f"row {planned.row}"

    # ------------------------------------------------------------------ capture results
    @Slot(object)
    def on_capture_finished(self, summary: object) -> None:
        """Record how a capture ended. Nothing is resent automatically, except a
        request that provably never reached the sensor (sent once more, unchanged)."""
        if not isinstance(summary, CaptureSummary) or self.session is None:
            return
        request = summary.request
        if request is None or request.owner != OWNER or not isinstance(request.tag, tuple) or len(request.tag) != 2:
            return
        round_id, task_key = request.tag
        state = self.session.task_by_key(round_id, task_key)
        planned = self.session.planned(task_key)
        if state is None or planned is None:
            return
        self._capture_in_flight = False
        state.finished_utc = _utc_now_text()
        state.record_id = summary.record_id or state.record_id
        state.summary = summary.as_dict()
        state.result = summary.result
        state.result_reason = summary.result_reason
        measured = {
            str(part.get("path") or ""): part
            for part in (summary.measurement or {}).get("parts") or []
            if isinstance(part, dict)
        }
        state.files = [
            {
                "name": path.rsplit("/", 1)[-1],
                "remote_path": path,
                "size": int(summary.sizes.get(path, 0)),
                "duration_s": (measured.get(path) or {}).get("duration_s"),
                "signal_time": (measured.get(path) or {}).get("signal_time"),
                "download_status": DL_PENDING if self.session.settings.auto_download else "not_requested",
                "local_path": "",
                "sha256": "",
                "attempts": 0,
            }
            for path in summary.finalized_paths
        ]
        diag(
            "task_result",
            summary.result,
            round=round_id,
            task=task_key,
            row=planned.row,
            attempt=state.attempts,
            outcome=summary.outcome,
            reason=summary.result_reason,
            payload=summary.payload,
            sensor_reply=summary.sensor_reply,
            files=[item["name"] for item in state.files],
        )
        if summary.outcome == OUTCOME_CONFIRMED and summary.parts > 0:
            state.status = T_CONFIRMED
            state.short = summary.result == "partial"
            if state.short:
                held = f"{summary.held_s:.3f} s" if summary.held_s is not None else "an unknown duration"
                state.notes.append(f"Partial: {summary.result_reason}; files hold {held} of {summary.requested_s:g} s requested.")
            self._queue_excel(state, planned, summary)
            if self.session.settings.auto_download:
                self._schedule_download_refresh(state.round_id, state.task_key)
        elif summary.outcome == OUTCOME_NOT_SENT and not summary.request_sent:
            if state.attempts <= NOT_SENT_RETRIES:
                # Nothing reached the sensor (no connection): the same request,
                # unchanged, is sent once more. Anything that may have reached
                # the sensor is never resent automatically.
                state.history.append(state.attempt_record())
                state.status = T_QUEUED
                state.notes.append(f"Attempt {state.attempts} not sent ({summary.detail}); sent once more, unchanged.")
                self._log(f"Nothing reached the sensor. {self._row_position(planned).capitalize()} is sent once more.", "error")
            else:
                state.status = T_NOT_SENT
                state.notes.append(f"Not sent after {state.attempts} attempts: {summary.detail}")
        elif summary.outcome == OUTCOME_SENSOR_ERROR:
            state.status = T_SENSOR_ERROR
            state.notes.append(f"Rejected by the sensor: {summary.sensor_error or summary.detail}")
        else:
            state.status = T_UNKNOWN
            state.notes.append(f"Not confirmed: {summary.result_reason or summary.detail}")
        if state.status != T_QUEUED:
            self._write_task_manifest(state, planned)
        self._save()
        if state.status == T_CONFIRMED:
            self._queue_row_result(state.round_id, planned)
        self._drain_downloads()
        self._after_task(state, planned)

    def _after_task(self, state: TaskState, planned: PlannedTask) -> None:
        if self.session is None:
            return
        if self.state == S_STOP_REQUESTED:
            self._finish_round(stopped=True)
            self._finish_session(stopped=True)
            return
        if self.state == S_PAUSE_REQUESTED:
            self.state = S_PAUSED
            self.session.pause_reason = "Paused by the operator after the last recording."
            self._log("Paused.")
            self._save()
            self._emit_status()
            return
        if self._next_queued() is None:
            self._finish_round()
            return
        wait_s = self.session.settings.wait_s
        if wait_s > 0:
            self._log(f"Next recording in {seconds_text(wait_s) if wait_s < 60 else format_seconds(wait_s)}.")
            self._begin_wait(wait_s)
        else:
            self.state = S_RUNNING
            self._start_next_task()
        self._emit_status()

    def _begin_wait(self, seconds: float) -> None:
        self.state = S_WAITING
        self._wait_total = max(seconds, 0.0)
        self._wait_deadline = time.monotonic() + self._wait_total
        self._save()

    def pass_counts(self, round_id: str | None) -> dict[str, int]:
        """Completed / partial / failed / uncertain / skipped / pending tasks of one pass."""
        counts = {C_COMPLETED: 0, C_PARTIAL: 0, C_FAILED: 0, C_UNCERTAIN: 0, C_SKIPPED: 0, C_PENDING: 0}
        if self.session is None or not round_id:
            return counts
        for state in self.session.tasks.get(round_id, []):
            counts[task_class(state)] += 1
        return counts

    def pass_summary_text(self, round_id: str | None) -> str:
        """'Completed 13 · Partial 1 · Failed 1 · Uncertain 0'."""
        counts = self.pass_counts(round_id)
        text = (
            f"Completed {counts[C_COMPLETED]} · Partial {counts[C_PARTIAL]} · "
            f"Failed {counts[C_FAILED]} · Uncertain {counts[C_UNCERTAIN]}"
        )
        not_recorded = counts[C_SKIPPED] + counts[C_PENDING]
        if not_recorded:
            text += f" · Not recorded {not_recorded}"
        return text

    # ------------------------------------------------------------------ result cards
    def row_excel_state(self, state: TaskState, planned: PlannedTask) -> str:
        """The protocol row's Excel state (or the recording log's); '' when Excel is not involved."""
        if state.status != T_CONFIRMED:
            return ""
        if self.protocol_results_enabled():
            index = self._plan_index_for(planned)
            row = self.workbook_queue().row_state(state.round_id, index) if index is not None else ""
            if row == "written":
                return EXCEL_UPDATED
            if row == "conflict":
                return "Excel not updated: the row holds other entries"
            if row == "retrying" or self._workbook_status == "error":
                return EXCEL_FAILED + "; trying again"
            return EXCEL_PENDING
        return {
            "saved": EXCEL_UPDATED,
            "already_logged": EXCEL_UPDATED,
            "queued": EXCEL_PENDING,
            "deferred": EXCEL_PENDING,
            "failed": EXCEL_FAILED,
        }.get(state.excel_status, "")

    def task_view(self, round_id: str, task_key: str) -> ResultView | None:
        """Requested / received / states of one recording of the collection."""
        if self.session is None:
            return None
        state = self.session.task_by_key(round_id, task_key)
        planned = self.session.planned(task_key)
        if state is None or planned is None:
            return None
        view = view_from_task(
            state,
            title=f"{self._row_position(planned).capitalize()}: {planned.range_text}",
            bandwidth_hz=planned.bandwidth_hz,
            centre_hz=planned.center_hz,
            seconds=planned.duration_s,
            excel_state=self.row_excel_state(state, planned),
            local_dir=self._download_dir(),
        )
        if state.status in {T_SENSOR_ERROR, T_NOT_SENT, T_FAILED}:
            view.recording_state = "Rejected by the sensor" if state.status == T_SENSOR_ERROR else "Not recorded"
        if state.attempts > 1:
            view.title += f" (attempt {state.attempts})"
        return view

    def last_task_view(self) -> ResultView | None:
        """The most recently finished recording of the latest pass."""
        if self.session is None:
            return None
        round_id = self._last_round_id()
        finished = [t for t in self.session.tasks.get(round_id or "", []) if t.finished_utc and t.status not in IN_FLIGHT and t.status != T_QUEUED]
        if not finished:
            return None
        # Stable sort: of two recordings finished within the same second, the later row wins.
        latest = sorted(finished, key=lambda t: t.finished_utc)[-1]
        return self.task_view(latest.round_id, latest.task_key)

    def _finish_round(self, *, stopped: bool = False) -> None:
        record = self.current_round()
        if record is None or self.session is None:
            return
        tasks = self.current_tasks()
        counts = self.pass_counts(record.round_id)
        record.tasks_confirmed = counts[C_COMPLETED]
        record.tasks_partial = counts[C_PARTIAL]
        record.tasks_failed = counts[C_FAILED]
        record.tasks_uncertain = counts[C_UNCERTAIN]
        record.tasks_skipped = counts[C_SKIPPED] + counts[C_PENDING]
        record.finished_utc = datetime.now(timezone.utc)
        if stopped:
            record.status = ROUND_STOPPED
        elif tasks and record.tasks_confirmed == len(tasks):
            record.status = ROUND_DONE
        elif record.tasks_confirmed or record.tasks_partial:
            record.status = ROUND_PARTIAL
        else:
            record.status = ROUND_FAILED
        index = self.session.rounds.index(record) + 1 if record in self.session.rounds else len(self.session.rounds)
        diag("pass_finished", record.status, round=record.round_id, counts=counts)
        clean = record.status == ROUND_DONE
        self._log(
            f"Pass {index} {'stopped' if stopped else 'finished'}: {self.pass_summary_text(record.round_id)}.",
            "info" if clean else "error",
        )
        self.session.current_round_id = None
        self.state = S_WAITING_SCHEDULE
        if not stopped:
            self._next_pass_after(self._clock())
        self._write_handover()
        self._save()
        self.round_finished.emit(record.round_id)

    def _finish_session(self, *, stopped: bool = False) -> None:
        self.state = S_DONE
        self._timer.stop()
        if self.session is not None:
            pending = sum(1 for item in self._downloads_pending) + len(self._downloads_active)
            text = "Collection stopped." if stopped else "Collection finished."
            if pending:
                text += f" Downloads: {pending} waiting."
            queued = self._workbook_queue.pending_count() if self._workbook_queue is not None else 0
            if queued:
                text += " Excel update pending."
            self._log(text)
            self._write_handover()
        self._save()
        self._release()
        self._emit_status()
        if self._downloads_pending or self._downloads_active:
            self._timer.start()

    # ------------------------------------------------------------------ backlog and storage
    def _backlog_reason(self, planned: PlannedTask | None = None) -> str | None:
        if self.session is None:
            return None
        settings = self.session.settings
        pending = len(self._downloads_pending) + len(self._downloads_active)
        if settings.auto_download and pending > settings.backlog_limit:
            return f"Downloads: {pending} waiting (limit {settings.backlog_limit}). Recording resumes when they catch up."
        try:
            folder = Path(self._download_dir())
            folder.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(str(folder)).free
        except OSError:
            return None
        need = (planned.estimated_bytes if planned is not None else 0) + settings.local_reserve_bytes
        if settings.auto_download and free < need:
            diag(
                "local_space_low",
                free=free,
                estimate=planned.estimated_bytes if planned else 0,
                reserve=settings.local_reserve_bytes,
            )
            return (
                f"Download folder: {format_bytes(free)} free, about {format_bytes(need)} needed. "
                "Free some space; nothing is deleted automatically."
            )
        return None

    # ------------------------------------------------------------------ downloads
    def _schedule_download_refresh(self, round_id: str, task_key: str) -> None:
        """List the capture's files again before downloading them.

        Seen on the R40-8: the sensor reports the recording done and the sizes
        stop changing, then a few seconds later it finishes the WAVE header and
        trailer (+648 B) or writes the XDAT sidecars (.xhdr appears, .xmrk grows).
        Downloading from a fresh listing keeps the manifest honest and picks up
        the late files; if the listing fails the files are downloaded as listed.
        """
        self._refreshes += 1
        QTimer.singleShot(int(FINALIZE_SETTLE_S * 1000), lambda: self._refresh_files_then_download(round_id, task_key))

    def _refresh_files_then_download(self, round_id: str, task_key: str) -> None:
        state = self.session.task_by_key(round_id, task_key) if self.session is not None else None
        if state is None or not state.files:
            self._refreshes = max(self._refreshes - 1, 0)
            return
        conn = getattr(self.host, "_conn", None)
        first = str(state.files[0]["remote_path"])
        folder = first.rsplit("/", 1)[0] if "/" in first else "/"
        stem = _stem_of(first)

        def work():
            from .sftp_window import _open_browser

            try:
                browser = _open_browser(conn)
                try:
                    return [(e.path, int(e.size or 0)) for e in browser.listdir(folder) if not e.is_dir and _stem_of(e.path) == stem]
                finally:
                    try:
                        browser.close()
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001 - reported to apply(), never raised across threads
                return {"error": str(exc)}

        def apply(result) -> None:
            # Counted as background work until the downloads are queued, so the
            # handover is not written in the gap between listing and download.
            self._refreshes = max(self._refreshes - 1, 0)
            if self.session is None:
                return
            current = self.session.task_by_key(round_id, task_key)
            if current is None:
                return
            if isinstance(result, list) and result:
                known = {str(item["remote_path"]): item for item in current.files}
                for path, size in result:
                    item = known.get(path)
                    if item is None:
                        current.files.append(
                            {
                                "name": path.rsplit("/", 1)[-1],
                                "remote_path": path,
                                "size": size,
                                "download_status": DL_PENDING,
                                "local_path": "",
                                "sha256": "",
                                "attempts": 0,
                                "note": "Listed after the capture was confirmed (written by the sensor while finishing the recording).",
                            }
                        )
                        current.notes.append(f"{path.rsplit('/', 1)[-1]} appeared on the sensor after the capture was confirmed; it is downloaded with the capture.")
                    elif size and size != int(item.get("size") or 0):
                        item["listed_size"] = int(item.get("size") or 0)
                        item["size"] = size
            else:
                detail = result.get("error") if isinstance(result, dict) else "no entries"
                current.notes.append(f"The sensor's folder could not be listed again before the download ({detail}); files are downloaded as listed at confirmation.")
            for item in current.files:
                if item.get("download_status") == DL_PENDING and not any(entry[2] is item for entry in self._downloads_pending):
                    self._downloads_pending.append((current.task_key, current.round_id, item))
            self._save()
            self._drain_downloads()
            if self.state == S_DONE and self.background_jobs() == 0:
                self._write_handover()

        runner = getattr(self.host, "_start_job", None)
        if runner is None:
            apply(work())
        else:
            runner(work, apply)

    def _drain_downloads(self) -> None:
        if self.session is None:
            return
        while self._downloads_pending and len(self._downloads_active) < MAX_DOWNLOADS:
            task_key, round_id, item = self._downloads_pending.pop(0)
            self._start_download(task_key, round_id, item)

    def _start_download(self, task_key: str, round_id: str, item: dict[str, Any]) -> None:
        from .sftp_window import FileDownloadWorker

        conn = getattr(self.host, "_conn", None)
        if conn is None:
            item["download_status"] = DL_FAILED
            item["note"] = "No connection settings."
            return
        folder = Path(self._download_dir())
        folder.mkdir(parents=True, exist_ok=True)
        local = folder / str(item["name"])
        if local.is_file() and local.stat().st_size == int(item.get("size") or 0) and int(item.get("size") or 0) > 0:
            item["download_status"] = DL_VERIFIED if not self.session.settings.verify_checksums else "present"
            item["local_path"] = str(local)
            diag("download_present", str(item["name"]), size=int(item.get("size") or 0))
            if self.session.settings.verify_checksums:
                self._start_checksum(task_key, str(item["name"]), str(local))
            self._save()
            return
        item["download_status"] = DL_ACTIVE
        item["attempts"] = int(item.get("attempts") or 0) + 1
        thread = QThread()
        worker = FileDownloadWorker(conn, str(item["remote_path"]), str(local), str(item["name"]), expected_size=int(item.get("size") or 0))
        worker.moveToThread(thread)
        queued = Qt.ConnectionType.QueuedConnection
        worker.finished.connect(self._on_download_finished, queued)
        worker.failed.connect(self._on_download_failed, queued)
        thread.started.connect(worker.run)
        thread.finished.connect(self._on_download_thread_finished, queued)
        self._downloads_active.append((thread, worker, task_key, round_id))
        track_worker_thread(thread, worker)
        thread.start()
        self._save()

    def _find_file(self, name: str) -> tuple[TaskState, dict[str, Any]] | None:
        if self.session is None:
            return None
        for tasks in self.session.tasks.values():
            for state in tasks:
                for item in state.files:
                    if item.get("name") == name and item.get("download_status") in {DL_ACTIVE, "present"}:
                        return state, item
        return None

    @Slot(str, str)
    def _on_download_finished(self, name: str, path: str) -> None:
        found = self._find_file(name)
        if found is None:
            return
        state, item = found
        item["local_path"] = path
        # The sensor finishes a file a few seconds after it reports the recording
        # done (WAVE header/trailer, XDAT sidecars), so the size listed at
        # confirmation can be a few hundred bytes short of the final file. The
        # download reads to the end; keep the final size and the listed one apart.
        try:
            actual = Path(path).stat().st_size
        except OSError:
            actual = 0
        listed = int(item.get("size") or 0)
        if actual and actual != listed:
            item["listed_size"] = listed
            item["size"] = actual
        if self.session.settings.verify_checksums:
            item["download_status"] = "present"
            self._start_checksum(state.task_key, name, path)
        else:
            item["download_status"] = DL_VERIFIED
            diag("download_verified", name, size=int(item.get("size") or 0))
            self._after_file_verified(state)
        self._save()
        self._emit_status()

    @Slot(str, str)
    def _on_download_failed(self, name: str, detail: str) -> None:
        found = self._find_file(name)
        if found is None:
            return
        state, item = found
        diag("download_failed", name, attempt=int(item.get("attempts") or 0), detail=str(detail))
        if int(item.get("attempts") or 0) <= DOWNLOAD_RETRIES:
            item["download_status"] = DL_PENDING
            self._log("Download failed. Trying again. See details.", "error")
            self._downloads_pending.append((state.task_key, state.round_id, item))
        else:
            item["download_status"] = DL_FAILED
            item["note"] = detail
            state.notes.append(f"Download failed for {name}: {detail}")
            self._log(f"Download failed {item['attempts']} times. The file stays on the sensor. See details.", "error")
            self._rewrite_manifest(state)
        self._save()
        self._emit_status()

    @Slot()
    def _on_download_thread_finished(self) -> None:
        thread = self.sender()
        remaining = []
        for entry in self._downloads_active:
            if entry[0] is thread:
                entry[1].deleteLater()
                entry[0].deleteLater()
            else:
                remaining.append(entry)
        self._downloads_active = remaining
        self._drain_downloads()
        if self.state == S_DONE and self.background_jobs() == 0:
            self._timer.stop()
            self._write_handover()
        # The Start button waits for the last download (previous_work_reason).
        self._emit_status()

    def _start_checksum(self, task_key: str, name: str, path: str) -> None:
        thread = QThread()
        worker = _ChecksumWorker(task_key, name, path)
        worker.moveToThread(thread)
        worker.done.connect(self._on_checksum_done, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)
        thread.finished.connect(self._on_checksum_thread_finished, Qt.ConnectionType.QueuedConnection)
        self._checksums.append((thread, worker))
        track_worker_thread(thread, worker)
        thread.start()

    @Slot(str, str, str)
    def _on_checksum_done(self, task_key: str, name: str, digest: str) -> None:
        if self.session is None:
            return
        for tasks in self.session.tasks.values():
            for state in tasks:
                if state.task_key != task_key:
                    continue
                for item in state.files:
                    if item.get("name") == name:
                        if digest:
                            item["sha256"] = digest
                            item["download_status"] = DL_VERIFIED
                            diag("download_verified", name, sha256=digest, size=int(item.get("size") or 0))
                        else:
                            item["download_status"] = DL_FAILED
                            item["note"] = "Checksum could not be computed."
                            diag("checksum_failed", name)
                            self._log("Could not verify a downloaded file. See details.", "error")
                        self._after_file_verified(state)
                        self._save()
                        self._emit_status()
                        return

    @Slot()
    def _on_checksum_thread_finished(self) -> None:
        thread = self.sender()
        remaining = []
        for entry in self._checksums:
            if entry[0] is thread:
                entry[1].deleteLater()
                entry[0].deleteLater()
            else:
                remaining.append(entry)
        self._checksums = remaining
        self._emit_status()

    def _after_file_verified(self, state: TaskState) -> None:
        self._rewrite_manifest(state)
        if all(item.get("download_status") == DL_VERIFIED for item in state.files) and state.files:
            planned = self.session.planned(state.task_key) if self.session is not None else None
            where = self._row_position(planned) if planned is not None else "recording"
            count = len(state.files)
            self._log(f"Saved locally: {where}, {count} file" + ("" if count == 1 else "s") + ".")
            self._start_quality_check(state)
            self._maybe_merge(state)

    # ------------------------------------------------------------------ quality
    def _start_quality_check(self, state: TaskState) -> None:
        if self.session is None or not self.session.settings.quality_checks:
            return
        if state.quality.get("level") not in (None, "", "pending"):
            return
        paths = [str(item["local_path"]) for item in state.files if item.get("local_path")]
        if len(paths) != len(state.files):
            return
        planned = self.session.planned(state.task_key)
        from .cellular_quality import preview_path_for

        stem = (state.summary or {}).get("stem") or ""
        preview = str(preview_path_for(paths[0], stem)) if self.session.settings.previews else ""
        state.quality = {"level": "pending"}
        worker = _QualityWorker(
            state.round_id,
            state.task_key,
            paths,
            expected_duration_s=float(planned.duration_s) if planned else None,
            expected_center_hz=int(planned.center_hz) if planned else None,
            expected_bandwidth_hz=int(planned.bandwidth_hz) if planned else None,
            expected_sample_rate_hz=int(planned.sample_rate_hz) if planned and planned.sample_rate_hz else None,
            expected_parts=[str(n) for n in (state.summary or {}).get("sensor_parts") or []],
            preview_path=preview,
            label=planned.label() if planned else state.task_key,
        )
        # One check at a time: file reads stay sequential on the disk and the
        # preview renderer is only used from one thread.
        self._quality_pending.append(worker)
        self._pump_quality()

    def _pump_quality(self) -> None:
        if self._quality_jobs or not self._quality_pending:
            return
        worker = self._quality_pending.pop(0)
        thread = QThread()
        worker.moveToThread(thread)
        worker.done.connect(self._on_quality_done, Qt.ConnectionType.QueuedConnection)
        thread.started.connect(worker.run)
        thread.finished.connect(self._on_quality_thread_finished, Qt.ConnectionType.QueuedConnection)
        self._quality_jobs.append((thread, worker))
        track_worker_thread(thread, worker)
        thread.start()

    @Slot(str, str, dict)
    def _on_quality_done(self, round_id: str, task_key: str, payload: dict) -> None:
        if self.session is None:
            return
        state = self.session.task_by_key(round_id, task_key)
        if state is None:
            return
        state.quality = dict(payload)
        level = payload.get("level")
        planned = self.session.planned(task_key)
        where = self._row_position(planned) if planned is not None else "a recording"
        diag("quality", str(level), task=task_key, warnings=payload.get("warnings"), errors=payload.get("errors"), notes=payload.get("notes"))
        if level == "warning":
            self._log(f"File check for {where}: warnings. See details.", "error")
        elif level == "error":
            self._log(f"File check for {where} found problems. See details.", "error")
            state.notes.append("Quality check found problems: " + "; ".join(payload.get("errors") or []))
        elif level not in {"ok", "not_checked"}:
            self._log(f"File check for {where} did not finish. See details.", "error")
        self._rewrite_manifest(state)
        self._save()
        self.session_changed.emit()

    @Slot()
    def _on_quality_thread_finished(self) -> None:
        thread = self.sender()
        remaining = []
        for entry in self._quality_jobs:
            if entry[0] is thread:
                entry[1].deleteLater()
                entry[0].deleteLater()
            else:
                remaining.append(entry)
        self._quality_jobs = remaining
        self._pump_quality()
        if self.state == S_DONE and self.background_jobs() == 0:
            self._write_handover()
        self._emit_status()

    def _maybe_merge(self, state: TaskState) -> None:
        if self.session is None or not self.session.settings.merge_parts:
            return
        merge = getattr(self.host, "_merge", None)
        if merge is None or len(state.files) < 2:
            return
        paths = [Path(item["local_path"]) for item in state.files if item.get("local_path")]
        if len(paths) != len(state.files):
            return
        stem = (state.summary or {}).get("stem") or ""
        if not stem:
            return
        folder = paths[0].parent
        group_key = f"cellular|{state.round_id}|{state.task_key}"
        try:
            output = merge.request(group_key, stem, paths, folder, auto=True)
        except Exception as exc:  # noqa: BLE001 - merging is optional and separate from capture success
            state.notes.append(f"Merge not started: {exc}")
            return
        if output:
            state.merged_output = str(output)
            state.notes.append("Merge requested for the downloaded parts (original parts are kept).")
            self._rewrite_manifest(state)

    # ------------------------------------------------------------------ Excel
    def _excel_target(self) -> tuple[Path, Any] | None:
        asker = getattr(self.host, "excel_logging_target", None)
        if callable(asker):
            target = asker()
            return (Path(target[0]), target[1]) if target else None
        conn = getattr(self.host, "_conn", None)
        if conn is None or not getattr(conn, "excel_enabled", False):
            return None
        workbook = Path(str(getattr(conn, "excel_workbook", "") or ""))
        if not workbook.is_file():
            return None
        return workbook, getattr(self.host, "_catalog", None)

    def _queue_excel(self, state: TaskState, planned: PlannedTask, summary: CaptureSummary) -> None:
        target = self._excel_target()
        if target is None:
            state.excel_status = "not_applicable"
            return
        workbook, catalog = target
        store = getattr(self.host, "_excel_store", None)
        if store is None:
            state.excel_status = "not_applicable"
            return
        worksheet = worksheet_for_task(planned.to_dict(), catalog)
        if worksheet == RESULTS_SHEET:
            try:
                if not ensure_results_sheet(store, workbook, RESULTS_SHEET):
                    state.excel_status = "failed"
                    state.notes.append("Excel: the workbook could not be prepared for a results sheet.")
                    return
            except ExcelBusyError as exc:
                state.excel_status = "deferred"
                state.notes.append(f"Excel deferred: {exc}")
                self._excel_retry.add((state.round_id, state.task_key))
                diag("excel_deferred", str(exc))
                self._log("Excel is busy. Update queued.")
                return
        info = getattr(self.host, "_sensor_info", None)
        sensor_name = workbook_sensor_name(getattr(info, "model", "") or "", list(getattr(catalog, "sensor_names", []) or []))
        sensor_id = getattr(info, "serial", "") or ""
        # The measured seconds only; an unknown duration stays empty (never the requested value).
        duration = summary.held_s
        queued = queue_capture_rows(
            store,
            workbook=workbook,
            worksheet=worksheet,
            task=planned.to_dict(),
            collection_event=collection_event_text(self.session.settings.campaign, self.session.settings.site, state.round_id),
            sensor_name=sensor_name,
            sensor_id=sensor_id,
            record_id=state.record_id or "",
            files=state.files,
            duration_s=round(duration, 3) if duration is not None else None,
            local_dir=Path(self._download_dir()),
        )
        state.excel_status = "queued" if queued else "already_logged"
        flush = getattr(self.host, "_flush_excel", None)
        if queued and flush is not None:
            try:
                flush()
            except Exception:  # noqa: BLE001 - the queue is persistent; the timer flushes it later
                pass

    def _refresh_excel_status(self) -> None:
        if self.session is None:
            return
        store = getattr(self.host, "_excel_store", None)
        if store is None:
            return
        info = getattr(self.host, "_sensor_info", None)
        sensor_id = getattr(info, "serial", "") or ""
        for key in list(self._excel_retry):
            state = self.session.task_by_key(*key)
            planned = self.session.planned(key[1]) if state is not None else None
            if state is None or planned is None:
                self._excel_retry.discard(key)
                continue
            summary = CaptureSummary(outcome=state.summary.get("outcome", ""), requested_s=float(state.summary.get("requested_s") or 0), held_s=state.summary.get("held_s"))
            self._queue_excel(state, planned, summary)
            if state.excel_status != "deferred":
                self._excel_retry.discard(key)
        for tasks in self.session.tasks.values():
            for state in tasks:
                if state.excel_status == "queued" and state.files:
                    if all(store.is_logged(sensor_id, item.get("remote_path", "")) for item in state.files):
                        state.excel_status = "saved"

    # ------------------------------------------------------------------ protocol workbook results
    def protocol_results_enabled(self) -> bool:
        settings = self.session.settings if self.session is not None else None
        return bool(
            settings and settings.protocol_results and settings.protocol_path and settings.protocol_sheet
            and settings.layout() is not None and settings.plan_rows
        )

    def workbook_queue(self) -> WorkbookQueue:
        if self._workbook_queue is None:
            self._workbook_queue = WorkbookQueue(queue_path(self._config_dir))
        return self._workbook_queue

    def _plan_index_for(self, planned: PlannedTask) -> int | None:
        """Position of the task's protocol row in the stored plan (block 0)."""
        if self.session is None:
            return None
        for index, row in enumerate(self.session.settings.rows()):
            if row.sheet_row == planned.row:
                return index
        return None

    def _row_tasks(self, round_id: str, planned: PlannedTask) -> list[tuple[TaskState, PlannedTask]]:
        """Every capture (window, repetition) of the task's protocol row in that round."""
        if self.session is None:
            return []
        out: list[tuple[TaskState, PlannedTask]] = []
        for state in self.session.tasks.get(round_id, []):
            other = self.session.planned(state.task_key)
            if other is not None and other.row == planned.row:
                out.append((state, other))
        return out

    def row_label(self, planned: PlannedTask) -> str:
        """'700 MHz DL' — the protocol row as the user names it."""
        band = planned.band_label or planned.band
        direction = planned.direction if planned.duplex != "TDD" or planned.direction in {"DL", "UL"} else "TDD"
        return " ".join(part for part in (band, direction) if part)

    def _queue_row_result(self, round_id: str, planned: PlannedTask) -> None:
        """Queue the row's result cells (its files, measured seconds, start time); a row without files writes nothing."""
        if not self.protocol_results_enabled() or self.session is None:
            return
        index = self._plan_index_for(planned)
        if index is None:
            self._log(f"Worksheet row {planned.row} is not in the stored plan, so its Excel cells are not updated.", "error")
            return
        tasks = [state.to_dict() for state, _planned in self._row_tasks(round_id, planned)]
        result = build_row_result(round_id, index, tasks, sensor=self._sensor_name())
        if not result.files:
            return
        settings = self.session.settings
        layout = settings.layout()
        if layout is None:
            return
        self.workbook_queue().enqueue(Path(settings.protocol_path), layout, settings.rows(), result)
        self._workbook_status = "pending"
        self._flush_workbook()

    def _flush_workbook(self) -> None:
        """Write pending row results; one flush at a time, bursts coalesced, retried while busy."""
        if self._workbook_flushing:
            self._workbook_flush_again = True
            return
        queue = self._workbook_queue
        if queue is None:
            # Nothing was queued in this process; a restart may have left results pending.
            path = queue_path(self._config_dir)
            if not path.is_file():
                return
            queue = self.workbook_queue()
        if queue.pending_count() == 0:
            return
        now = time.monotonic()
        if now < self._workbook_not_before:
            QTimer.singleShot(int((self._workbook_not_before - now) * 1000) + 50, self, self._flush_workbook)
            return
        self._workbook_flushing = True
        self._workbook_flush_again = False
        self._workbook_not_before = now + WORKBOOK_COALESCE_S
        self._workbook_status = "writing"

        def work():
            try:
                return flush_queue(queue)
            except Exception as exc:  # noqa: BLE001 - reported to apply(), never raised across threads
                report = FlushReport()
                report.error = f"{type(exc).__name__}: {exc}"
                report.pending = queue.pending_count()
                return report

        def apply(result) -> None:
            self._workbook_flushing = False
            report = result if isinstance(result, FlushReport) else None
            if report is None:
                self._workbook_status = "error"
                self._workbook_note = "no result"
            else:
                diag(
                    "workbook_flush",
                    written=report.written,
                    pending=report.pending,
                    busy=report.busy,
                    error=report.error,
                    notes=list(report.notes),
                    blocks=[list(item) for item in report.blocks_created],
                    backups=[Path(b).name for b in report.backups],
                )
                for backup in report.backups:
                    self._log(f"Workbook backup saved: {Path(backup).name}.")
                for round_id, ordinal, first_row in report.blocks_created:
                    if ordinal > 0:
                        self._log(f"Pass results added below the protocol rows, from row {first_row}.")
                for note in report.notes:
                    problem = "not written" in note or "unchanged" in note or "could not" in note
                    self._log(f"Excel: {clip(note, 150)}", "error" if problem else "info")
                if report.written:
                    self._log(f"Excel updated: {report.written} row" + ("" if report.written == 1 else "s") + ".")
                if report.busy:
                    self._workbook_status = "busy"
                    self._workbook_note = report.busy
                    if not self._workbook_busy_logged:
                        self._log("Excel is busy. Update queued.")
                        self._workbook_busy_logged = True
                    self._workbook_not_before = time.monotonic() + WORKBOOK_BUSY_RETRY_S
                    QTimer.singleShot(int(WORKBOOK_BUSY_RETRY_S * 1000) + 50, self, self._flush_workbook)
                elif report.error:
                    self._workbook_status = "error"
                    if report.error != self._workbook_note:
                        self._log(
                            f"Excel update failed. Trying again every {WORKBOOK_ERROR_RETRY_S:g} s. See details.",
                            "error",
                        )
                    self._workbook_note = report.error
                    self._workbook_not_before = time.monotonic() + WORKBOOK_ERROR_RETRY_S
                    if report.pending:
                        QTimer.singleShot(int(WORKBOOK_ERROR_RETRY_S * 1000) + 50, self, self._flush_workbook)
                else:
                    self._workbook_busy_logged = False
                    self._workbook_status = "pending" if report.pending else "saved"
                    self._workbook_note = ""
            self._emit_status()
            if self.state == S_DONE and self.background_jobs() == 0:
                self._write_handover()
            if self._workbook_flush_again:
                self._flush_workbook()

        runner = getattr(self.host, "_start_job", None)
        if runner is None:
            apply(work())
        else:
            runner(work, apply)

    def workbook_pending(self) -> int:
        queue = self._workbook_queue
        if queue is None:
            path = queue_path(self._config_dir)
            if not path.is_file():
                return 0
            queue = self.workbook_queue()
        return queue.pending_count()

    # ------------------------------------------------------------------ sleep and missed passes
    def _note_sleep_gap(self) -> None:
        now = time.time()
        last = self._last_tick_wall
        self._last_tick_wall = now
        if last and now - last > SLEEP_GAP_S:
            minutes = int((now - last) // 60)
            self._log(
                f"The computer was asleep for about {minutes} min. A pass whose time went by is listed as missed.",
                "error",
            )

    def _mark_missed_while_closed(self, now: datetime) -> int:
        """Passes whose time went by while the collection was not running are missed.

        Applies after a restart (and before an interrupted session continues):
        the grace that lets a pass start a few minutes late while the
        application runs does not apply here. Such a pass is listed for the
        operator - Run the missed pass now / Skip it - and never started late
        on its own. The round being recorded when the application closed is
        not touched; reconciliation deals with it.
        """
        if self.session is None:
            return 0
        now_utc = now.astimezone(timezone.utc)
        count = 0
        for record in self.session.rounds:
            if record.status in {ROUND_PENDING, ROUND_DUE, ROUND_DELAYED} and record.scheduled_utc <= now_utc:
                late = (now_utc - record.scheduled_utc).total_seconds()
                record.status = ROUND_MISSED
                record.note = f"Scheduled time passed {int(late // 60)} min ago while the application was closed or the collection interrupted."
                count += 1
        return count

    def _note_legacy_session(self) -> None:
        if self.session is not None and self.session.plan.legacy:
            diag("legacy_session", rule=self.session.plan.request_rule, tasks=len(self.session.plan.tasks))
            self._log(
                "A collection saved by an older version was found. Its results are kept; "
                "start a fresh plan or discard it.",
                "error",
            )

    def _note_missed_rounds(self) -> None:
        if self.session is None:
            return
        for record in self.session.rounds:
            if record.status == ROUND_MISSED and "logged" not in record.note:
                record.note = (record.note + " (logged)").strip()
                self._log(
                    f"Missed pass: {record.scheduled_local:%d/%m/%Y %H:%M} did not start. Run it now or skip it.",
                    "error",
                )

    # ------------------------------------------------------------------ manifests
    def _write_task_manifest(self, state: TaskState, planned: PlannedTask) -> None:
        if self.session is None:
            return
        record = self.session.round(state.round_id)
        data = build_capture_manifest(
            campaign={**self.session.settings.to_dict(), "scenario": self.session.settings.scenario},
            round_record=record.to_dict() if record is not None else {"round_id": state.round_id},
            task=planned.to_dict(),
            task_state=state.to_dict(),
            sensor=self.session.sensor or self._sensor_dict(),
            protocol_version=self.session.protocol_version,
            timezone_label=self.session.timezone_label,
        )
        stem = (state.summary or {}).get("stem") or ""
        fallback = f"{state.round_id}_{planned.index + 1:03d}_{planned.task_key.replace('!', '_').replace('#', '_').replace(':', '_')}"
        if state.attempts > 1:
            # A later attempt never overwrites the manifest of an earlier one.
            fallback += f"_attempt{state.attempts}"
        try:
            path = write_manifest(manifest_path(Path(self._download_dir()), stem, fallback_name=fallback), data)
            state.manifest_path = str(path)
        except OSError as exc:
            state.notes.append(f"Manifest not written: {exc}")
            diag("manifest_failed", planned.label(), error=f"{type(exc).__name__}: {exc}")
            self._log(f"Could not save the record file for {self._row_position(planned)}. See details.", "error")

    def _rewrite_manifest(self, state: TaskState) -> None:
        planned = self.session.planned(state.task_key) if self.session is not None else None
        if planned is not None:
            self._write_task_manifest(state, planned)

    def _write_handover(self) -> None:
        if self.session is None:
            return
        try:
            data = build_handover(
                campaign=self.session.settings.to_dict(),
                rounds=[record.to_dict() for record in self.session.rounds],
                tasks_by_round={rid: [task.to_dict() for task in tasks] for rid, tasks in self.session.tasks.items()},
                plan_tasks={task.task_key: task.to_dict() for task in self.session.plan.tasks},
                sensor=self.session.sensor or self._sensor_dict(),
                protocol_version=self.session.protocol_version,
                timezone_label=self.session.timezone_label,
            )
            folder = Path(self._download_dir()) / "cellular_manifests"
            folder.mkdir(parents=True, exist_ok=True)
            slug = (self.session.rounds[0].round_id.split("_")[0] if self.session.rounds else "campaign")
            stamp = self.session.created_utc.replace(":", "").replace("-", "")[:15] or "session"
            base = folder / f"handover_{slug}_{stamp}"
            write_manifest(base.with_suffix(".json"), data)
            base.with_suffix(".md").write_text(handover_markdown(data), encoding="utf-8")
        except OSError as exc:
            diag("handover_failed", error=f"{type(exc).__name__}: {exc}")
            self._log("Could not save the collection summary file. See details.", "error")

    # ------------------------------------------------------------------ recovery
    def interrupted(self) -> bool:
        return self.state == S_INTERRUPTED and self.session is not None

    def uncertain_tasks(self) -> list[tuple[TaskState, PlannedTask]]:
        out = []
        if self.session is None:
            return out
        for tasks in self.session.tasks.values():
            for state in tasks:
                if state.status in UNCERTAIN:
                    planned = self.session.planned(state.task_key)
                    if planned is not None:
                        out.append((state, planned))
        return out

    def reconcile(self, on_done: Callable[[str], None] | None = None) -> None:
        """Check the sensor for every task whose outcome is uncertain.

        First the sensor's mission list (a recording still running is left
        alone), then its files: a task whose file turns up is measured from
        the file's header and counted; it is never sent again. Nothing is
        resent here.
        """
        if self.session is None:
            return
        if not self.uncertain_tasks():
            if on_done:
                on_done("No uncertain recording to check.")
            return
        verify = getattr(self.host, "verify_sensor_idle", None)
        if verify is None:
            self._reconcile_files(on_done)
            return

        def after_idle(ok: bool, detail: str) -> None:
            if not ok:
                diag("reconcile_sensor_busy", detail)
                text = "The sensor is still recording. Check again when it is free."
                self._log(text, "error")
                if on_done:
                    on_done(text)
                return
            self._reconcile_files(on_done)

        try:
            verify(after_idle)
        except Exception as exc:  # noqa: BLE001 - the mission list is advisory; the files decide
            diag("reconcile_idle_check_failed", f"{type(exc).__name__}: {exc}")
            self._reconcile_files(on_done)

    def _reconcile_files(self, on_done: Callable[[str], None] | None) -> None:
        pending = self.uncertain_tasks()
        if self.session is None or not pending:
            if on_done:
                on_done("No uncertain recording to check.")
            return
        from .capture_measure import CaptureMeasurement, classify, measure_parts
        from .file_watch import folders_for_recording, list_watch_entries
        from .recording_history import live_capture_files, load_recordings
        from .sftp_window import _open_browser

        conn = getattr(self.host, "_conn", None)
        records = load_recordings()
        wanted = [(state.task_key, state.round_id, state.record_id, list((state.summary or {}).get("sensor_parts") or [])) for state, _planned in pending]

        def work():
            results = {}
            try:
                browser = _open_browser(conn)
            except Exception as exc:  # noqa: BLE001 - reported through apply(), never lost in a worker
                return {"error": f"{type(exc).__name__}: {exc}"}
            try:
                for task_key, round_id, record_id, parts in wanted:
                    record = next((item for item in records if item.id == record_id), None)
                    if record is None:
                        results[(round_id, task_key)] = ([], None, "no history entry")
                        continue
                    entries = list_watch_entries(browser, folders_for_recording(record.started_at))
                    mine = [(e.path, int(e.size or 0)) for e in live_capture_files(
                        entries, record, known=records, sensor_parts=parts, host=getattr(conn, "host", "")
                    ) if not e.is_dir]
                    measurement = measure_parts(browser, [p for p, _s in mine], dict(mine)) if mine else None
                    results[(round_id, task_key)] = (mine, measurement, "")
            except Exception as exc:  # noqa: BLE001
                return {"error": f"{type(exc).__name__}: {exc}"}
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
            return results

        def apply(results) -> None:
            if not isinstance(results, dict) or "error" in results:
                detail = results.get("error") if isinstance(results, dict) else "no result"
                diag("reconcile_failed", str(detail))
                text = "Could not check the sensor's files. Nothing was changed. See details."
                self._log(text, "error")
                if on_done:
                    on_done(text)
                return
            found = 0
            for state, planned in self.uncertain_tasks():
                files, measurement, note = results.get((state.round_id, state.task_key), ([], None, "not checked"))
                diag("reconcile_task", state.task_key, files=[p for p, _s in files], note=note,
                     measurement=measurement.to_dict() if isinstance(measurement, CaptureMeasurement) else None)
                if files:
                    verdict = classify(
                        requested_s=planned.duration_s,
                        measurement=measurement,
                        named_parts=list((state.summary or {}).get("sensor_parts") or []),
                    )
                    held = measurement.duration_s if isinstance(measurement, CaptureMeasurement) else None
                    state.status = T_CONFIRMED
                    state.result = verdict.result
                    state.result_reason = verdict.reason
                    state.short = verdict.result == "partial"
                    state.summary = {
                        **state.summary,
                        "outcome": OUTCOME_CONFIRMED,
                        "parts": len(files),
                        "total_bytes": sum(size for _path, size in files),
                        "held_s": held,
                        "requested_s": planned.duration_s,
                        "finalized_paths": [path for path, _size in files],
                        "sizes": {path: size for path, size in files},
                        "stem": _stem_of(files[0][0]),
                        "result": verdict.result,
                        "result_reason": verdict.reason,
                        "measurement": measurement.to_dict() if isinstance(measurement, CaptureMeasurement) else {},
                        "detail": "Found later on the sensor; measured from the file headers.",
                    }
                    parts = {str(p.get("path")): p for p in (state.summary["measurement"] or {}).get("parts") or []}
                    state.files = [
                        {
                            "name": path.rsplit("/", 1)[-1],
                            "remote_path": path,
                            "size": size,
                            "duration_s": (parts.get(path) or {}).get("duration_s"),
                            "signal_time": (parts.get(path) or {}).get("signal_time"),
                            "download_status": DL_PENDING if self.session.settings.auto_download else "not_requested",
                            "local_path": "",
                            "sha256": "",
                            "attempts": 0,
                        }
                        for path, size in files
                    ]
                    state.notes.append("Found later on the sensor; the duration is read from the file headers. Not sent again.")
                    if self.session.settings.auto_download:
                        for item in state.files:
                            self._downloads_pending.append((state.task_key, state.round_id, item))
                    found += 1
                    self._queue_row_result(state.round_id, planned)
                else:
                    state.status = T_UNKNOWN
                    state.result = "unconfirmed"
                    state.result_reason = "no file on the sensor"
                    state.notes.append(f"Checked with the sensor: no file for this recording ({note or 'none found'}).")
                self._write_task_manifest(state, planned)
            self._recount_finished_rounds()
            self._save()
            self._drain_downloads()
            still = len(self.uncertain_tasks())
            text = f"Checked with the sensor: {found} recording" + ("" if found == 1 else "s") + f" found, {still} without a file."
            self._log(text)
            if on_done:
                on_done(text)

        runner = getattr(self.host, "_start_job", None)
        if runner is None:
            try:
                apply(work())
            except Exception as exc:  # noqa: BLE001
                diag("reconcile_failed", f"{type(exc).__name__}: {exc}")
                if on_done:
                    on_done("Could not check the sensor's files. See details.")
            return
        runner(work, apply)

    def _recount_finished_rounds(self) -> None:
        """Keep the counts of finished passes in step after a check or a retry."""
        if self.session is None:
            return
        for record in self.session.rounds:
            if not record.finished or record.round_id == self.session.current_round_id:
                continue
            counts = self.pass_counts(record.round_id)
            record.tasks_confirmed = counts[C_COMPLETED]
            record.tasks_partial = counts[C_PARTIAL]
            record.tasks_failed = counts[C_FAILED]
            record.tasks_uncertain = counts[C_UNCERTAIN]
            record.tasks_skipped = counts[C_SKIPPED] + counts[C_PENDING]
            if record.status in {ROUND_DONE, ROUND_PARTIAL, ROUND_FAILED}:
                tasks = self.session.tasks.get(record.round_id, [])
                if tasks and record.tasks_confirmed == len(tasks):
                    record.status = ROUND_DONE
                elif record.tasks_confirmed or record.tasks_partial:
                    record.status = ROUND_PARTIAL
                else:
                    record.status = ROUND_FAILED

    def _legacy_refusal(self) -> str | None:
        if self.session is not None and self.session.plan.legacy:
            return (
                "This collection was saved by an older version. Its results are kept, "
                "but it cannot be continued: start a fresh plan or discard it."
            )
        return None

    def resume_after_restart(self) -> str | None:
        """Continue an interrupted session with its queued tasks (after reconciliation)."""
        if self.session is None or self.state != S_INTERRUPTED:
            return "No interrupted session."
        legacy = self._legacy_refusal()
        if legacy:
            return legacy
        if self._downloads_only:
            return self._resume_downloads()
        if self.uncertain_tasks():
            return "Check with the sensor or skip the uncertain recording(s) first."
        mismatch = self.identity_mismatch()
        if mismatch:
            return mismatch
        reserve = getattr(self.host, "set_external_owner", None)
        if reserve is not None:
            error = reserve(OWNER)
            if error:
                return error
        self._capture_in_flight = False
        requeued = self._requeue_unfinished_downloads()
        # A pass whose time went by meanwhile is never started by Continue.
        if self._mark_missed_while_closed(self._clock()):
            self._note_missed_rounds()
        record = self.current_round()
        if record is not None and self._next_queued() is None:
            self._finish_round()
        else:
            self.state = S_RUNNING if record is not None else S_WAITING_SCHEDULE
        self._log("Collection continued." + (f" Downloads: {requeued} waiting." if requeued else ""))
        self._save()
        self._timer.start()
        self._tick()
        self._drain_downloads()
        return None

    def _resume_downloads(self) -> str | None:
        """A finished session's downloads that did not complete before the application closed."""
        mismatch = self.identity_mismatch()
        if mismatch:
            return mismatch
        requeued = self._requeue_unfinished_downloads()
        self._downloads_only = False
        self.state = S_DONE
        self._log(f"Downloading the rest of the last collection: {requeued} waiting.")
        self._save()
        self._timer.start()
        self._drain_downloads()
        self._emit_status()
        return None

    def unfinished_downloads(self) -> int:
        """Files of confirmed recordings the session still has to download (its saved state)."""
        if self.session is None or not self.session.settings.auto_download:
            return 0
        return sum(
            1
            for tasks in self.session.tasks.values()
            for state in tasks
            if state.status == T_CONFIRMED
            for item in state.files
            if item.get("download_status") in {DL_PENDING, DL_ACTIVE, "present"}
        )

    def downloads_only(self) -> bool:
        """The interrupted session only has downloads left (Continue downloads them)."""
        return self._downloads_only and self.state == S_INTERRUPTED

    def previous_work_reason(self) -> str:
        """Why a new collection waits for the last one ('' when it need not).

        Downloads, checksums and quality checks belong to the session that
        recorded the files; a new session would leave them unrecorded.
        """
        if self.active():
            return ""
        files = len(self._downloads_pending) + len(self._downloads_active)
        if files or self._refreshes:
            waiting = f" ({files} waiting)" if files else ""
            return f"The last collection is still downloading{waiting}. Start the next one when the downloads finish."
        if self._checksums or self._quality_jobs or self._quality_pending:
            return "The last collection is still checking its downloaded files. Start the next one when it finishes."
        return ""

    def _requeue_unfinished_downloads(self) -> int:
        """Downloads that were pending or running when the application closed."""
        if self.session is None or not self.session.settings.auto_download:
            return 0
        count = 0
        for tasks in self.session.tasks.values():
            for state in tasks:
                if state.status != T_CONFIRMED:
                    continue
                for item in state.files:
                    if item.get("download_status") in {DL_PENDING, DL_ACTIVE, "present"} and not any(entry[2] is item for entry in self._downloads_pending):
                        item["download_status"] = DL_PENDING
                        self._downloads_pending.append((state.task_key, state.round_id, item))
                        count += 1
        return count

    def skip_task(self, round_id: str, task_key: str) -> None:
        state = self.session.task_by_key(round_id, task_key) if self.session else None
        if state is None or state.status == T_CONFIRMED:
            return
        state.status = T_SKIPPED
        state.notes.append("Skipped by the operator.")
        planned = self.session.planned(task_key)
        if planned is not None:
            self._write_task_manifest(state, planned)
        self._save()
        self._emit_status()

    def retry_task(self, round_id: str, task_key: str) -> str | None:
        """Queue a failed, uncertain or skipped task again (explicit operator action).

        A retry is a new attempt of the same unchanged request: the earlier
        attempt is kept in the task's history and its manifest, never overwritten.
        """
        return self._retry([(round_id, task_key)])

    def retryable_tasks(self, round_id: str | None) -> list[tuple[TaskState, PlannedTask]]:
        """Failed tasks of a pass, and uncertain ones the sensor check found no file for."""
        out: list[tuple[TaskState, PlannedTask]] = []
        if self.session is None or not round_id:
            return out
        for state in self.session.tasks.get(round_id, []):
            cls = task_class(state)
            no_file = cls == C_UNCERTAIN and state.status == T_UNKNOWN and state.result_reason == "no file on the sensor"
            if cls == C_FAILED or no_file:
                planned = self.session.planned(state.task_key)
                if planned is not None:
                    out.append((state, planned))
        return out

    def retry_failed(self, round_id: str | None = None, on_done: Callable[[str], None] | None = None) -> None:
        """'Retry failed rows': check uncertain rows with the sensor first, then send the failed ones again.

        Partial recordings are kept and not retried; an uncertain row whose
        file turns up is counted, not sent again.
        """
        round_id = round_id or self._last_round_id()
        if self.session is None or not round_id:
            if on_done:
                on_done("No finished pass.")
            return
        legacy = self._legacy_refusal()
        if legacy:
            if on_done:
                on_done(legacy)
            return

        def after_check(_text: str = "") -> None:
            items = self.retryable_tasks(round_id)
            if not items:
                text = "Nothing to retry: no failed row in this pass."
                self._log(text)
                if on_done:
                    on_done(text)
                return
            error = self._retry([(state.round_id, state.task_key) for state, _planned in items])
            text = error or f"Retrying {len(items)} row" + ("" if len(items) == 1 else "s") + "."
            if on_done:
                on_done(text)

        if any(state.round_id == round_id for state, _planned in self.uncertain_tasks()):
            self.reconcile(after_check)
        else:
            after_check()

    def _retry(self, keys: list[tuple[str, str]]) -> str | None:
        if self.session is None:
            return "No collection."
        legacy = self._legacy_refusal()
        if legacy:
            return legacy
        states: list[TaskState] = []
        for round_id, task_key in keys:
            state = self.session.task_by_key(round_id, task_key)
            if state is None or state.status in {T_CONFIRMED, T_QUEUED} or state.status in IN_FLIGHT:
                return "This recording cannot be retried."
            if self.session.current_round_id not in {None, round_id}:
                return "Another pass is running."
            states.append(state)
        if not states:
            return "Nothing to retry."
        if self.state in {S_DONE, S_INTERRUPTED, S_IDLE}:
            reserve = getattr(self.host, "set_external_owner", None)
            if reserve is not None:
                error = reserve(OWNER)
                if error:
                    return error
        for state in states:
            state.history.append(state.attempt_record())
            state.notes.append(f"Attempt {state.attempts} ended as {task_class(state)}; tried again by the operator.")
            state.status = T_QUEUED
            state.result = ""
            state.result_reason = ""
            state.files = []
            state.summary = {}
            state.short = False
            state.excel_status = "not_applicable"
            state.quality = {}
            state.merged_output = ""
            planned = self.session.planned(state.task_key)
            if planned is not None:
                # The manifest of the earlier attempt stays; the new attempt writes its own.
                state.manifest_path = ""
        round_id = states[0].round_id
        if self.session.current_round_id is None:
            record = self.session.round(round_id)
            if record is not None:
                record.status = ROUND_RUNNING
                self.session.current_round_id = round_id
        if self.state in {S_DONE, S_INTERRUPTED, S_IDLE}:
            self.state = S_RUNNING
            self._timer.start()
        self._save()
        self._tick()
        return None

    def _last_round_id(self) -> str | None:
        """The most recent pass that has tasks (running or finished)."""
        if self.session is None:
            return None
        if self.session.current_round_id:
            return self.session.current_round_id
        for record in reversed(self.session.rounds):
            if record.round_id in self.session.tasks:
                return record.round_id
        return None

    def run_missed_round(self, round_id: str) -> str | None:
        """Explicitly run a round whose time was missed (never done automatically)."""
        record = self.session.round(round_id) if self.session else None
        if record is None or record.status != ROUND_MISSED:
            return "This round is not marked as missed."
        legacy = self._legacy_refusal()
        if legacy:
            return legacy
        if self.state == S_INTERRUPTED:
            if self.current_round() is not None or self.uncertain_tasks():
                return "Check with the sensor or skip the uncertain capture(s) and press Continue first; the missed pass can be run after that."
            # Same path as Continue: identity check, sensor reserved, downloads from before the restart queued again.
            error = self.resume_after_restart()
            if error:
                return error
        record.status = ROUND_DUE
        record.note = (record.note + " Run late on the operator's request.").strip()
        if self.state in {S_DONE, S_IDLE}:
            reserve = getattr(self.host, "set_external_owner", None)
            if reserve is not None:
                error = reserve(OWNER)
                if error:
                    record.status = ROUND_MISSED
                    return error
            self.state = S_WAITING_SCHEDULE
            self._timer.start()
        self._save()
        self._tick()
        return None

    def skip_missed_round(self, round_id: str) -> None:
        record = self.session.round(round_id) if self.session else None
        if record is None or record.status != ROUND_MISSED:
            return
        record.status = ROUND_SKIPPED
        record.note = (record.note + " Skipped by the operator.").strip()
        self._save()
        self._emit_status()

    # ------------------------------------------------------------------ status for the panel
    def status(self) -> dict[str, Any]:
        session = self.session
        if session is None:
            return {
                "state": self.state,
                "text": "No cellular collection.",
                "active": False,
                "workbook_pending": self.workbook_pending(),
                "workbook_status": self._workbook_status,
                "workbook_note": self._workbook_note,
            }
        tasks = self.current_tasks()
        record = self.current_round()
        done = sum(1 for t in tasks if t.status not in {T_QUEUED} and t.status not in IN_FLIGHT)
        current = next((t for t in tasks if t.status in IN_FLIGHT), None)
        planned = session.planned(current.task_key) if current is not None else None
        remaining_tasks = [session.planned(t.task_key) for t in tasks if t.status == T_QUEUED]
        remaining_tasks = [t for t in remaining_tasks if t is not None]
        wait_s = session.settings.wait_s
        remaining_s = sum(t.duration_s for t in remaining_tasks) + len(remaining_tasks) * (wait_s + 10.0)
        if planned is not None:
            remaining_s += planned.duration_s + 10.0
        remaining_bytes = sum(t.estimated_bytes for t in remaining_tasks) + (planned.estimated_bytes if planned else 0)
        countdown = max(self._wait_deadline - time.monotonic(), 0.0) if self.state == S_WAITING else None
        now = self._clock()
        upcoming = next_pending(session.rounds, now=now)
        missed = [r for r in session.rounds if r.status == ROUND_MISSED]
        delayed = [r for r in session.rounds if r.status == ROUND_DELAYED]
        pending_dl = len(self._downloads_pending) + len(self._downloads_active)
        store = getattr(self.host, "_excel_store", None)
        pending_excel = store.pending_count() if store is not None else 0
        classes = [task_class(t) for items in session.tasks.values() for t in items]
        confirmed_total = sum(1 for c in classes if c in {C_COMPLETED, C_PARTIAL})
        failed_total = sum(1 for c in classes if c == C_FAILED)
        uncertain_total = sum(1 for items in session.tasks.values() for t in items if t.status in UNCERTAIN)
        rows_total = session.plan.row_count or len(session.settings.rows())
        row_position = self._row_position(planned) if planned is not None else ""
        recording = f"Recording {row_position}: {planned.range_text}" if planned is not None else ""
        if self._idle_check_pending and not recording:
            recording = "Checking the sensor is free"
        if self.state == S_WAITING and countdown is not None:
            text = f"Next recording in {max(int(round(countdown)), 0)} s"
        elif self.state == S_PAUSE_REQUESTED:
            text = "Pausing after this recording" + (f" - {recording}" if recording else "")
        elif self.state == S_STOP_REQUESTED:
            text = "Stopping after this recording" + (f" - {recording}" if recording else "")
        elif recording:
            text = recording
        elif self.state == S_PAUSED:
            text = "Paused"
        elif self.state == S_PAUSED_BACKLOG:
            text = "Paused: " + (session.pause_reason or "backlog")
        elif self.state == S_WAITING_SCHEDULE:
            if upcoming is not None:
                seconds = (upcoming.scheduled_utc - now.astimezone(timezone.utc)).total_seconds()
                text = f"Next pass in {max(int(round(seconds)), 0)} seconds" if seconds < 120 else f"Next pass at {upcoming.scheduled_local:%H:%M}"
            else:
                text = "Waiting for the schedule"
        elif self.state == S_INTERRUPTED and session.plan.legacy:
            text = "Saved plan from an older version: start a fresh plan or discard it"
        elif self.downloads_only():
            text = f"Downloads did not finish: press Continue to download {self.unfinished_downloads()} more"
        elif self.state == S_INTERRUPTED:
            if uncertain_total:
                text = "Interrupted: check with the sensor before continuing"
            elif record is not None:
                text = "Interrupted during a pass: press Continue to finish it"
            elif missed:
                text = "Interrupted: a scheduled pass was missed - run it now or skip it"
            elif upcoming is not None:
                text = f"Interrupted: press Continue to keep the schedule (next pass at {upcoming.scheduled_local:%H:%M})"
            else:
                text = "Interrupted: press Continue to finish"
        elif self.state == S_DONE:
            # A finished session is not the same as a successful one.
            statuses = [r.status for r in session.rounds]
            if any(s == ROUND_STOPPED for s in statuses):
                text = "Stopped"
            elif any(s in {ROUND_PARTIAL, ROUND_FAILED} for s in statuses):
                text = "Finished with problems"
            else:
                text = "Finished"
        else:
            text = "Idle"
        workbook_pending = self.workbook_pending() if self.protocol_results_enabled() or self._workbook_queue is not None else 0
        return {
            "state": self.state,
            "active": self.active(),
            "text": text,
            "round_id": record.round_id if record else "",
            "round_index": (session.rounds.index(record) + 1) if record in session.rounds else 0,
            "rounds_total": len(session.rounds),
            "task_done": done,
            "tasks_total": len(tasks),
            "current_label": planned.label() if planned else "",
            "current_direction": planned.direction if planned else "",
            "current_window": planned.range_text if planned else "",
            "current_request": planned.request_text if planned else "",
            "legacy": session.plan.legacy,
            "previous_work": self.previous_work_reason(),
            "downloads_only": self.downloads_only(),
            "pass_counts": self.pass_counts(record.round_id if record else self._last_round_id()),
            "pass_summary": self.pass_summary_text(record.round_id if record else self._last_round_id()),
            "last_round_id": self._last_round_id(),
            "retryable": len(self.retryable_tasks(self._last_round_id())) if record is None else 0,
            "countdown_s": countdown,
            "wait_total_s": self._wait_total,
            "next_round": upcoming.scheduled_local.isoformat() if upcoming else "",
            "next_round_text": f"{upcoming.scheduled_local:%a %d %b %H:%M}" if upcoming else "",
            "delayed_rounds": [r.round_id for r in delayed],
            "missed_rounds": [r.round_id for r in missed],
            "pending_downloads": pending_dl,
            "pending_excel": pending_excel,
            "remaining_s": remaining_s,
            "remaining_bytes": remaining_bytes,
            "confirmed_total": confirmed_total,
            "failed_total": failed_total,
            "uncertain_total": uncertain_total,
            "pause_reason": session.pause_reason,
            "timezone": session.timezone_label,
            "rows_total": rows_total,
            "row_position": row_position,
            "protocol_results": self.protocol_results_enabled(),
            "workbook_pending": workbook_pending,
            "workbook_status": self._workbook_status,
            "workbook_note": self._workbook_note,
        }

    def _emit_status(self) -> None:
        try:
            self.status_changed.emit(self.status())
        except Exception:  # noqa: BLE001 - status is informational
            pass

    def shutdown(self) -> None:
        """Called when the main window closes: persist, release, stop timers."""
        self._timer.stop()
        if self.session is not None and self.active():
            if self._capture_in_flight:
                self.session.pause_reason = "Application closed during a capture; reconcile with the sensor before continuing."
            self._save()
        for thread, worker, _key, _rid in list(self._downloads_active):
            try:
                worker.abort()
            except Exception:
                pass
            thread.quit()
        self._quality_pending.clear()
        for thread, _worker in list(self._checksums) + list(self._quality_jobs):
            thread.quit()

    def background_jobs(self) -> int:
        """Downloads, checksums, quality checks and workbook writes still running off the UI thread."""
        return (
            len(self._downloads_active)
            + len(self._downloads_pending)
            + self._refreshes
            + len(self._checksums)
            + len(self._quality_jobs)
            + len(self._quality_pending)
            + (1 if self._workbook_flushing else 0)
        )


def _identity_text(value: Any) -> str:
    """Model/serial text; the window's '—' placeholder counts as unknown."""
    text = str(value or "").strip()
    return "" if text in {"—", "-", "?"} else text


def _stem_of(path: str) -> str:
    from .recording_history import iq_group_key

    return iq_group_key(path.rsplit("/", 1)[-1])
