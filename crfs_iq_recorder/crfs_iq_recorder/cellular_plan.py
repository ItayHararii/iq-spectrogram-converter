"""Turn the imported allocations into the exact sequence of captures for one round.

Rules (collection task decision of the latest meeting):

* bands in ascending order of nominal frequency, not band number;
* inside an FDD band every DL allocation (ascending frequency), then every
  UL allocation; a TDD band has shared allocations, recorded once each and
  labelled as shared (never duplicated as "DL" and "UL");
* an allocation wider than the capture window becomes consecutive sub-band
  windows whose union covers it; the windows are listed, their overlap is
  stated, and each is a separate capture marked as partial coverage;
* every capture uses the displayed duration (workbook value, else the
  campaign fallback) and the wait between captures applies between sub-band
  windows too.

Nothing is combined into a wider capture and no allocation is cropped,
retuned or resampled: what is recorded is exactly what the preview shows.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .cellular_capabilities import MHZ, SensorCapability, normalise_window_hz
from .cellular_protocol import (
    DIR_DL,
    DIR_SHARED,
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
from .frequency import FrequencyPlan, from_start_end
from .size_estimate import wave_size_model

DURATION_FROM_WORKBOOK = "workbook"
DURATION_FROM_CAMPAIGN = "campaign"

_DIRECTION_RANK = {DIR_DL: 0, DIR_UL: 1, DIR_SHARED: 2}

# Time a capture costs beyond its recording duration: request, the sensor's
# own finalisation and the file confirmation (measured 3-13 s on the Node
# 40-8 for 2-20 s captures; longer at 62.5 MS/s). Used for the estimate only.
DEFAULT_OVERHEAD_S = 10.0


@dataclass(frozen=True)
class PlannedTask:
    """One capture of one frequency window, in execution order."""

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
    def partial(self) -> bool:
        return self.window_count > 1

    @property
    def coverage_label(self) -> str:
        if self.window_count > 1:
            return f"window {self.window_index} of {self.window_count} (sequential coverage, partial capture)"
        return "whole allocation"

    def label(self) -> str:
        text = f"{self.band_label} {self.direction} {format_mhz(self.window_start_hz)}–{format_mhz(self.window_stop_hz)} MHz"
        if self.window_count > 1:
            text += f" ({self.window_index}/{self.window_count})"
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
    """The ordered captures of one complete pass plus how they were derived."""

    protocol_version: str
    tasks: list[PlannedTask]
    issues: list[Issue]
    window_hz: int
    window_note: str
    edge_margin_hz: int
    fallback_duration_s: float | None
    capability: dict[str, Any]
    band_order: list[str]

    @property
    def errors(self) -> list[Issue]:
        return [issue for issue in self.issues if issue.level == LEVEL_ERROR]

    @property
    def ready(self) -> bool:
        return bool(self.tasks) and not self.errors

    @property
    def estimated_bytes(self) -> int:
        return sum(task.estimated_bytes for task in self.tasks)

    @property
    def capture_seconds(self) -> float:
        return sum(task.duration_s for task in self.tasks)

    def estimated_round_seconds(self, wait_s: float, overhead_s: float = DEFAULT_OVERHEAD_S) -> float:
        """Estimate for one pass: captures, the waits between them and per-capture overhead."""
        count = len(self.tasks)
        if not count:
            return 0.0
        return self.capture_seconds + max(count - 1, 0) * max(wait_s, 0.0) + count * max(overhead_s, 0.0)

    def tasks_for_entry(self, entry_id: str) -> list[PlannedTask]:
        return [task for task in self.tasks if task.entry_id == entry_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "window_hz": self.window_hz,
            "window_note": self.window_note,
            "edge_margin_hz": self.edge_margin_hz,
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
            window_hz=int(data.get("window_hz") or 0),
            window_note=str(data.get("window_note") or ""),
            edge_margin_hz=int(data.get("edge_margin_hz") or 0),
            fallback_duration_s=float(fallback) if fallback is not None else None,
            capability=dict(data.get("capability") or {}),
            band_order=[str(item) for item in data.get("band_order") or []],
        )


def _band_sort_key(entries: list[ProtocolEntry]) -> tuple:
    first = entries[0]
    nominal = first.nominal_hz
    if nominal is None:
        starts = [entry.start_hz for entry in entries if entry.start_hz is not None]
        nominal = min(starts) if starts else 0
        return (1, nominal, first.band.casefold())
    return (0, nominal, first.band.casefold())


def order_entries(entries: list[ProtocolEntry]) -> tuple[list[ProtocolEntry], list[str], list[Issue]]:
    """Bands by nominal frequency; DL before UL inside a band; shared TDD as listed."""
    groups: dict[str, list[ProtocolEntry]] = {}
    for entry in entries:
        groups.setdefault(entry.band_key, []).append(entry)
    issues: list[Issue] = []
    ordered_groups = sorted(groups.values(), key=_band_sort_key)
    ordered: list[ProtocolEntry] = []
    band_order: list[str] = []
    for group in ordered_groups:
        first = group[0]
        if first.nominal_hz is None:
            issues.append(Issue(
                LEVEL_INFO,
                "band_order_by_allocation",
                f"{first.band_label()} has no nominal frequency; it is ordered by its lowest allocation.",
                row=first.row,
            ))
        if first.duplex == DUPLEX_TDD and any(entry.direction in {DIR_DL, DIR_UL} for entry in group):
            issues.append(Issue(
                LEVEL_WARNING,
                "tdd_mixed",
                f"{first.band_label()} mixes TDD shared and DL/UL rows; check the workbook.",
                row=first.row,
            ))
        group_sorted = sorted(
            group,
            key=lambda entry: (_DIRECTION_RANK.get(entry.direction, 3), entry.start_hz or 0, entry.row),
        )
        ordered.extend(group_sorted)
        band_order.append(first.band_label())
    return ordered, band_order, issues


def split_windows(start_hz: int, stop_hz: int, window_hz: int, *, edge_margin_hz: int = 0) -> list[tuple[int, int, int]]:
    """(start, stop, overlap_with_previous) windows whose union covers [start, stop].

    Each window is `window_hz` wide. Windows step from `start_hz` by the
    usable width (the window less any edge margin on both sides), so their
    edges stay on the allocation's own grid (whole MHz for whole-MHz edges);
    the last window is moved back to end exactly at `stop_hz`, and the extra
    overlap this gives it is reported instead of leaving a gap.
    """
    width = stop_hz - start_hz
    if width <= 0:
        return []
    if width <= window_hz:
        return [(start_hz, stop_hz, 0)]
    # The step between neighbouring windows may not exceed the usable width,
    # so the trusted centre parts tile the allocation without a gap.
    usable = max(window_hz - 2 * max(edge_margin_hz, 0), window_hz // 2)
    count = math.ceil((width - window_hz) / usable) + 1
    windows: list[tuple[int, int, int]] = []
    previous_stop: int | None = None
    for index in range(count):
        w_start = min(start_hz + index * usable, stop_hz - window_hz)
        w_stop = w_start + window_hz
        overlap = (previous_stop - w_start) if previous_stop is not None else 0
        windows.append((w_start, w_stop, max(overlap, 0)))
        previous_stop = w_stop
    return windows


def _window_plan(start_hz: int, stop_hz: int) -> tuple[FrequencyPlan, tuple[str, ...]]:
    adjustments: list[str] = []
    if (start_hz + stop_hz) % 2:
        stop_hz += 1
        adjustments.append("Window widened by 1 Hz at the top so its centre is a whole number of Hz.")
    return from_start_end(start_hz, stop_hz), tuple(adjustments)


def build_plan(
    imported: ProtocolImport,
    capability: SensorCapability,
    *,
    window_hz: int | None = None,
    fallback_duration_s: float | None = None,
    edge_margin_hz: int = 0,
) -> CellularPlan:
    """The capture sequence for one round. Problems are reported in `issues`, never fixed silently."""
    window, window_notes = normalise_window_hz(window_hz or capability.default_window_hz, capability)
    issues: list[Issue] = [Issue(LEVEL_INFO, "window", note) for note in window_notes]
    window_note = capability.window_note(window)
    entries = imported.valid_entries()
    ordered, band_order, order_issues = order_entries(entries)
    issues.extend(order_issues)
    tasks: list[PlannedTask] = []
    if fallback_duration_s is not None and not fallback_duration_s > 0:
        issues.append(Issue(LEVEL_ERROR, "fallback_invalid", "The campaign recording duration must be positive."))
        fallback_duration_s = None
    for entry in ordered:
        if entry.start_hz is None or entry.stop_hz is None:
            continue
        coverage = capability.coverage_issue(entry.start_hz, entry.stop_hz)
        if coverage:
            issues.append(Issue(LEVEL_ERROR, "outside_coverage", f"{entry.label()}: {coverage}", row=entry.row))
            continue
        if entry.duration_s is not None:
            duration, source = float(entry.duration_s), DURATION_FROM_WORKBOOK
        elif fallback_duration_s is not None:
            duration, source = float(fallback_duration_s), DURATION_FROM_CAMPAIGN
        else:
            issues.append(Issue(
                LEVEL_ERROR,
                "duration_missing",
                f"{entry.label()}: no recording duration in the workbook and no campaign duration set.",
                row=entry.row,
            ))
            continue
        windows = split_windows(entry.start_hz, entry.stop_hz, window, edge_margin_hz=edge_margin_hz)
        if len(windows) > 1:
            overlaps = sorted({w[2] for w in windows[1:]})
            overlap_text = ", ".join(f"{o / MHZ:g}" for o in overlaps) + " MHz"
            issues.append(Issue(
                LEVEL_INFO,
                "expanded",
                f"{entry.label()} ({(entry.stop_hz - entry.start_hz) / MHZ:g} MHz) is wider than the "
                f"{window / MHZ:g} MHz capture window: recorded as {len(windows)} sequential windows "
                f"(overlap {overlap_text}). This is sequential coverage, not a simultaneous full-band capture.",
                row=entry.row,
            ))
        repetitions = max(int(entry.repeat or 1), 1)
        for repetition in range(1, repetitions + 1):
            for w_index, (w_start, w_stop, overlap) in enumerate(windows, start=1):
                plan, adjustments = _window_plan(w_start, w_stop)
                model = wave_size_model(plan.bandwidth_hz, Decimal(str(duration)))
                key = f"{entry.entry_id}#w{w_index}of{len(windows)}#r{repetition}"
                tasks.append(PlannedTask(
                    index=len(tasks),
                    task_key=key,
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
                    window_index=w_index,
                    window_count=len(windows),
                    overlap_prev_hz=overlap,
                    repetition=repetition,
                    repetitions=repetitions,
                    duration_s=duration,
                    duration_source=source,
                    center_hz=plan.center_hz,
                    bandwidth_hz=plan.bandwidth_hz,
                    sample_rate_hz=model.sample_rate_hz,
                    estimated_bytes=model.total_bytes,
                    estimated_parts=model.parts,
                    adjustments=adjustments,
                    extra=dict(entry.extra),
                ))
    if not tasks and not issues:
        issues.append(Issue(LEVEL_ERROR, "empty", "The protocol has no allocation that can be recorded."))
    elif not tasks:
        issues.append(Issue(LEVEL_ERROR, "empty", "No capture could be planned; fix the items above."))
    return CellularPlan(
        protocol_version=imported.protocol_version(),
        tasks=tasks,
        issues=issues,
        window_hz=window,
        window_note=window_note,
        edge_margin_hz=max(int(edge_margin_hz or 0), 0),
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
