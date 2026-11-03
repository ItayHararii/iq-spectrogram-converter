"""Turn the imported protocol rows into the recording requests of one pass.

Rule: each protocol row is sent to the sensor as one recording request, in
worksheet order, with the row's own start and stop frequencies and the
duration chosen in the app. The only conversions are the ones the API needs:
units to Hz and start/stop to centre/bandwidth. A row whose centre is not a
whole number of Hz cannot be sent exactly and is reported, not changed.

Nothing is split, narrowed, shortened, shifted or rounded, and the request
count does not depend on the duration. What the sensor actually records is
measured from its files afterwards; the plan only says what is requested.

A TDD row is one shared allocation (never duplicated as "DL" and "UL"). The
workbook's duration column is a result of an earlier pass, never an
instruction. A "Repeat" column, when the worksheet has one, sends the same
unchanged request that many times.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .cellular_capabilities import MHZ, SensorCapability
from .cellular_protocol import (
    DIR_DL,
    DIR_UL,
    DUPLEX_TDD,
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_WARNING,
    Issue,
    ProtocolEntry,
    ProtocolImport,
    format_mhz,
)
from .frequency import FrequencyPlan
from .size_estimate import wave_size_model

DURATION_FROM_WORKBOOK = "workbook"
DURATION_FROM_CAMPAIGN = "campaign"

# Saved plans carry the rule they were built with. Plans saved by 1.10.0-1.12.0
# have none (and may hold automatically generated sub-band requests).
REQUEST_RULE = "one_request_per_row"
REQUEST_SENTENCE = "Each Excel row is sent to CRFS as one recording request using the listed frequency range and selected duration."

# Time a recording costs beyond its duration: request, the sensor's own
# finalisation and the file confirmation. Used for the time estimate only.
DEFAULT_OVERHEAD_S = 30.0


@dataclass(frozen=True)
class PlannedTask:
    """One recording request for one protocol row, in execution order.

    `window_*` hold the requested range (kept under these names so plans saved
    by older versions still load); new plans always have one request per row.
    """

    index: int
    task_key: str
    entry_id: str
    row: int
    band: str
    band_label: str
    nominal_hz: int | None
    duplex: str
    direction: str
    network: str
    technology: str
    target_class: str
    notes: str
    requested_start_hz: int
    requested_stop_hz: int
    window_start_hz: int
    window_stop_hz: int
    window_index: int
    window_count: int
    overlap_prev_hz: int
    repetition: int
    repetitions: int
    duration_s: float
    duration_source: str
    center_hz: int
    bandwidth_hz: int
    sample_rate_hz: int
    estimated_bytes: int
    estimated_parts: int
    adjustments: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def range_text(self) -> str:
        """'3300-3400 MHz' (the requested range, plain hyphen for the activity log)."""
        return f"{format_mhz(self.window_start_hz)}-{format_mhz(self.window_stop_hz)} MHz"

    @property
    def request_text(self) -> str:
        """'100 MHz for 2 s' (what is asked of the sensor)."""
        return f"{format_mhz(self.bandwidth_hz)} MHz for {self.duration_s:g} s"

    def label(self) -> str:
        text = f"{self.band_label} {self.direction} {format_mhz(self.window_start_hz)}–{format_mhz(self.window_stop_hz)} MHz"
        if self.window_count > 1:
            text += f" (part {self.window_index}/{self.window_count})"
        if self.repetitions > 1:
            text += f" rep {self.repetition}/{self.repetitions}"
        return text

    def frequency_plan(self) -> FrequencyPlan:
        return FrequencyPlan(
            start_hz=self.window_start_hz,
            end_hz=self.window_stop_hz,
            center_hz=self.center_hz,
            bandwidth_hz=self.bandwidth_hz,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "task_key": self.task_key,
            "entry_id": self.entry_id,
            "row": self.row,
            "band": self.band,
            "band_label": self.band_label,
            "nominal_hz": self.nominal_hz,
            "duplex": self.duplex,
            "direction": self.direction,
            "network": self.network,
            "technology": self.technology,
            "target_class": self.target_class,
            "notes": self.notes,
            "requested_start_hz": self.requested_start_hz,
            "requested_stop_hz": self.requested_stop_hz,
            "window_start_hz": self.window_start_hz,
            "window_stop_hz": self.window_stop_hz,
            "window_index": self.window_index,
            "window_count": self.window_count,
            "overlap_prev_hz": self.overlap_prev_hz,
            "repetition": self.repetition,
            "repetitions": self.repetitions,
            "duration_s": self.duration_s,
            "duration_source": self.duration_source,
            "center_hz": self.center_hz,
            "bandwidth_hz": self.bandwidth_hz,
            "sample_rate_hz": self.sample_rate_hz,
            "estimated_bytes": self.estimated_bytes,
            "estimated_parts": self.estimated_parts,
            "adjustments": list(self.adjustments),
            "extra": dict(self.extra),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PlannedTask":
        return cls(
            index=int(data.get("index") or 0),
            task_key=str(data.get("task_key") or ""),
            entry_id=str(data.get("entry_id") or ""),
            row=int(data.get("row") or 0),
            band=str(data.get("band") or ""),
            band_label=str(data.get("band_label") or ""),
            nominal_hz=int(data["nominal_hz"]) if data.get("nominal_hz") is not None else None,
            duplex=str(data.get("duplex") or ""),
            direction=str(data.get("direction") or ""),
            network=str(data.get("network") or ""),
            technology=str(data.get("technology") or ""),
            target_class=str(data.get("target_class") or ""),
            notes=str(data.get("notes") or ""),
            requested_start_hz=int(data.get("requested_start_hz") or 0),
            requested_stop_hz=int(data.get("requested_stop_hz") or 0),
            window_start_hz=int(data.get("window_start_hz") or 0),
            window_stop_hz=int(data.get("window_stop_hz") or 0),
            window_index=int(data.get("window_index") or 1),
            window_count=int(data.get("window_count") or 1),
            overlap_prev_hz=int(data.get("overlap_prev_hz") or 0),
            repetition=int(data.get("repetition") or 1),
            repetitions=int(data.get("repetitions") or 1),
            duration_s=float(data.get("duration_s") or 0.0),
            duration_source=str(data.get("duration_source") or DURATION_FROM_CAMPAIGN),
            center_hz=int(data.get("center_hz") or 0),
            bandwidth_hz=int(data.get("bandwidth_hz") or 0),
            sample_rate_hz=int(data.get("sample_rate_hz") or 0),
            estimated_bytes=int(data.get("estimated_bytes") or 0),
            estimated_parts=int(data.get("estimated_parts") or 1),
            adjustments=tuple(str(item) for item in data.get("adjustments") or []),
            extra={str(k): str(v) for k, v in (data.get("extra") or {}).items()},
        )


@dataclass
class CellularPlan:
    """The ordered requests of one complete pass."""

    protocol_version: str
    tasks: list[PlannedTask]
    issues: list[Issue]
    fallback_duration_s: float | None
    capability: dict[str, Any]
    band_order: list[str]
    request_rule: str = REQUEST_RULE

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == LEVEL_ERROR]

    @property
    def ready(self) -> bool:
        return bool(self.tasks) and not self.errors

    @property
    def legacy(self) -> bool:
        """Built by an older version (possibly with automatic sub-band requests): never resumed."""
        return self.request_rule != REQUEST_RULE or any(task.window_count > 1 for task in self.tasks)

    @property
    def row_count(self) -> int:
        return len({task.entry_id for task in self.tasks})

    @property
    def estimated_bytes(self) -> int:
        return sum(task.estimated_bytes for task in self.tasks)

    @property
    def capture_seconds(self) -> float:
        return sum(task.duration_s for task in self.tasks)

    def estimated_round_seconds(self, wait_s: float, overhead_s: float = DEFAULT_OVERHEAD_S) -> float:
        """Estimate for one pass: recordings, the waits between them and per-request overhead."""
        count = len(self.tasks)
        if not count:
            return 0.0
        return self.capture_seconds + max(count - 1, 0) * max(wait_s, 0.0) + count * max(overhead_s, 0.0)

    def tasks_for_entry(self, entry_id: str) -> list[PlannedTask]:
        return [task for task in self.tasks if task.entry_id == entry_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_rule": self.request_rule,
            "protocol_version": self.protocol_version,
            "fallback_duration_s": self.fallback_duration_s,
            "capability": dict(self.capability),
            "band_order": list(self.band_order),
            "issues": [issue.to_dict() for issue in self.issues],
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CellularPlan":
        fallback = data.get("fallback_duration_s")
        return cls(
            protocol_version=str(data.get("protocol_version") or ""),
            tasks=[PlannedTask.from_dict(item) for item in data.get("tasks") or []],
            issues=[Issue.from_dict(item) for item in data.get("issues") or []],
            fallback_duration_s=float(fallback) if fallback is not None else None,
            capability=dict(data.get("capability") or {}),
            band_order=[str(item) for item in data.get("band_order") or []],
            request_rule=str(data.get("request_rule") or ""),
        )


def order_entries(entries: list[ProtocolEntry]) -> tuple[list[ProtocolEntry], list[str], list[Issue]]:
    """Rows in worksheet order, top to bottom: the workbook is the plan.

    Bands are listed in the order they first appear. A TDD band that also
    has DL/UL rows is flagged, nothing is reordered.
    """
    issues: list[Issue] = []
    ordered = sorted(entries, key=lambda entry: entry.row)
    band_order: list[str] = []
    seen_bands: dict[str, list[ProtocolEntry]] = {}
    for entry in ordered:
        group = seen_bands.setdefault(entry.band_key, [])
        if not group:
            band_order.append(entry.band_label())
        group.append(entry)
    for group in seen_bands.values():
        first = group[0]
        if first.duplex == DUPLEX_TDD and any(entry.direction in {DIR_DL, DIR_UL} for entry in group):
            issues.append(Issue(
                LEVEL_WARNING,
                "tdd_mixed",
                f"{first.band_label()} mixes TDD shared and DL/UL rows; check the workbook.",
                row=first.row,
            ))
    return ordered, band_order, issues


def request_range(start_hz: int, stop_hz: int) -> tuple[FrequencyPlan | None, str]:
    """Exact centre/bandwidth for a row's range, or why it cannot be sent unchanged."""
    if stop_hz <= start_hz:
        return None, "the stop frequency is not above the start frequency"
    if (start_hz + stop_hz) % 2:
        centre = Decimal(start_hz + stop_hz) / 2
        return None, (
            f"its centre would be {centre} Hz, which the sensor API cannot take (whole Hz only); "
            "correct the start or stop frequency in the workbook"
        )
    return FrequencyPlan(start_hz=start_hz, end_hz=stop_hz, center_hz=(start_hz + stop_hz) // 2, bandwidth_hz=stop_hz - start_hz), ""


def build_plan(
    imported: ProtocolImport,
    capability: SensorCapability,
    *,
    fallback_duration_s: float | None = None,
) -> CellularPlan:
    """The requests of one pass. Problems are reported in `issues`, never fixed.

    Every row becomes one request with its own start/stop and the duration
    chosen in the app (`fallback_duration_s`); the workbook's duration column
    is a result of an earlier pass, never an instruction.
    """
    issues: list[Issue] = []
    invalid = fallback_duration_s is not None and not fallback_duration_s > 0
    if invalid:
        issues.append(Issue(LEVEL_ERROR, "fallback_invalid", "The recording duration must be positive."))
        fallback_duration_s = None
    entries = imported.valid_entries()
    ordered, band_order, order_issues = order_entries(entries)
    issues.extend(order_issues)
    if fallback_duration_s is None and ordered and not invalid:
        issues.append(Issue(LEVEL_ERROR, "duration_missing", "Choose the recording duration (seconds) in the app."))
    tasks: list[PlannedTask] = []
    for entry in ordered:
        if entry.start_hz is None or entry.stop_hz is None:
            continue
        plan, problem = request_range(entry.start_hz, entry.stop_hz)
        if plan is None:
            issues.append(Issue(LEVEL_ERROR, "unrepresentable", f"Row {entry.row} ({entry.label()}): {problem}.", row=entry.row))
            continue
        tuning = capability.tuning_note(plan.start_hz, plan.end_hz)
        if tuning:
            issues.append(Issue(
                LEVEL_WARNING,
                "outside_tuning_range",
                f"Row {entry.row} ({entry.label()}) {tuning}. It is sent as listed; CRFS decides.",
                row=entry.row,
            ))
        if capability.wider_than_receiver(plan.bandwidth_hz):
            issues.append(Issue(
                LEVEL_INFO,
                "wider_than_receiver",
                f"Row {entry.row}: {format_mhz(plan.bandwidth_hz)} MHz is wider than the "
                f"{format_mhz(int(capability.ibw_hz or 0))} MHz receiver of this model. It is sent as one request; "
                "the files show what CRFS recorded.",
                row=entry.row,
            ))
        if fallback_duration_s is None:
            continue
        duration = float(fallback_duration_s)
        model = wave_size_model(plan.bandwidth_hz, Decimal(str(duration)), max_rate_hz=capability.max_sample_rate_hz)
        repetitions = max(int(entry.repeat or 1), 1)
        for repetition in range(1, repetitions + 1):
            tasks.append(PlannedTask(
                index=len(tasks),
                task_key=f"{entry.entry_id}#r{repetition}",
                entry_id=entry.entry_id,
                row=entry.row,
                band=entry.band,
                band_label=entry.band_label(),
                nominal_hz=entry.nominal_hz,
                duplex=entry.duplex,
                direction=entry.direction,
                network=entry.network,
                technology=entry.technology,
                target_class=entry.target_class,
                notes=entry.notes,
                requested_start_hz=entry.start_hz,
                requested_stop_hz=entry.stop_hz,
                window_start_hz=plan.start_hz,
                window_stop_hz=plan.end_hz,
                window_index=1,
                window_count=1,
                overlap_prev_hz=0,
                repetition=repetition,
                repetitions=repetitions,
                duration_s=duration,
                duration_source=DURATION_FROM_CAMPAIGN,
                center_hz=plan.center_hz,
                bandwidth_hz=plan.bandwidth_hz,
                sample_rate_hz=model.sample_rate_hz,
                estimated_bytes=model.total_bytes,
                estimated_parts=model.parts,
                extra=dict(entry.extra),
            ))
    if not tasks and not issues:
        issues.append(Issue(LEVEL_ERROR, "empty", "The protocol has no row that can be recorded."))
    elif not tasks and not any(issue.level == LEVEL_ERROR for issue in issues):
        issues.append(Issue(LEVEL_ERROR, "empty", "No request could be planned; fix the items above."))
    return CellularPlan(
        protocol_version=imported.protocol_version(),
        tasks=tasks,
        issues=issues,
        fallback_duration_s=fallback_duration_s,
        capability=capability.to_dict(),
        band_order=band_order,
    )


def format_bytes(count: int) -> str:
    value = float(max(int(count or 0), 0))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1000.0
    return f"{value:,.1f} TB"


def format_seconds(seconds: float) -> str:
    total = int(round(max(seconds, 0.0)))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} h {minutes:02d} min"
    if minutes:
        return f"{minutes} min {secs:02d} s"
    return f"{secs} s"


def _wait_text(seconds: float) -> str:
    seconds = max(float(seconds or 0.0), 0.0)
    return f"{seconds:g} s" if seconds < 60 else format_seconds(seconds)


def summary_line(plan: CellularPlan, *, wait_s: float, passes: int) -> str:
    """'15 rows · 2 s per request · 10 s between recordings · 1 pass' (passes 0 = until stopped)."""
    rows = plan.row_count
    parts = [f"{rows} row" + ("" if rows == 1 else "s")]
    if len(plan.tasks) != rows:
        parts.append(f"{len(plan.tasks)} requests")
    if plan.fallback_duration_s:
        parts.append(f"{plan.fallback_duration_s:g} s per request")
    parts.append(f"{_wait_text(wait_s)} between recordings")
    parts.append("until stopped" if passes <= 0 else f"{passes} pass" + ("" if passes == 1 else "es"))
    return " · ".join(parts)


__all__ = [
    "DEFAULT_OVERHEAD_S",
    "MHZ",
    "REQUEST_RULE",
    "REQUEST_SENTENCE",
    "CellularPlan",
    "PlannedTask",
    "build_plan",
    "format_bytes",
    "format_seconds",
    "order_entries",
    "request_range",
    "summary_line",
]
