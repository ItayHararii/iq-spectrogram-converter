"""Programmatic capture requests and their results.

The main window owns the sensor: it submits one recording at a time and
confirms it from the sensor's files. Other features (the Cellular Collection
runner) ask it to start a capture with a `CaptureRequest` and learn how it
ended from the `capture_finished` signal, which carries a `CaptureSummary`.
Nothing here talks to the sensor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .frequency import FrequencyPlan

# How a capture ended. Task completion (the sensor's word) and file
# availability (the files settled on its disk) are reported separately in
# the summary; this is the overall verdict the caller acts on.
OUTCOME_CONFIRMED = "confirmed"  # files settled on the sensor (may be short; see held_s)
OUTCOME_UNKNOWN = "unknown"  # request reached the sensor; no settled file confirms it
OUTCOME_NOT_SENT = "not_sent"  # nothing reached the sensor; safe to retry
OUTCOME_SENSOR_ERROR = "sensor_error"  # the sensor reported an error for the task
OUTCOME_FAILED = "failed"  # an application error ended the capture; state uncertain
OUTCOME_BUSY = "busy"  # never started: another recording owns the sensor


@dataclass(frozen=True)
class CaptureRequest:
    """One recording another part of the application asks the main window to run."""

    plan: FrequencyPlan
    seconds: float
    owner: str
    label: str = ""
    collection_event: str = ""
    target_class: str = ""
    worksheet: str = ""
    recording_format: str | None = None
    # False: the owner writes its own Excel rows; the main window's
    # "Log recordings to Excel" setting does not apply to this capture.
    excel: bool = False
    tag: Any = None


@dataclass
class CaptureSummary:
    """Everything the main window learned about one capture, in one place."""

    outcome: str
    request: CaptureRequest | None = None
    record_id: str | None = None
    phase: str = ""
    detail: str = ""
    requested_s: float = 0.0
    held_s: float | None = None
    parts: int = 0
    total_bytes: int = 0
    expected_bytes: int = 0
    sensor_finished: bool = False
    stream_lost: bool = False
    request_sent: bool = False
    sensor_parts: list[str] = field(default_factory=list)
    finalized_paths: list[str] = field(default_factory=list)
    sizes: dict[str, int] = field(default_factory=dict)
    sensor_warnings: list[str] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stem: str = ""
    # EMP recording_format identifier actually sent (WAVE, XDAT, ...); "" when nothing was sent.
    recording_format: str = ""

    @property
    def confirmed(self) -> bool:
        return self.outcome == OUTCOME_CONFIRMED and self.parts > 0

    @property
    def short(self) -> bool:
        """The files hold clearly less IQ than requested."""
        return (
            self.held_s is not None
            and self.requested_s > 0
            and self.held_s < 0.95 * self.requested_s
        )

    @property
    def retry_is_safe(self) -> bool:
        """True only when nothing reached the sensor."""
        return self.outcome in {OUTCOME_NOT_SENT, OUTCOME_BUSY}

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "record_id": self.record_id,
            "phase": self.phase,
            "detail": self.detail,
            "requested_s": self.requested_s,
            "held_s": self.held_s,
            "parts": self.parts,
            "total_bytes": self.total_bytes,
            "expected_bytes": self.expected_bytes,
            "sensor_finished": self.sensor_finished,
            "stream_lost": self.stream_lost,
            "request_sent": self.request_sent,
            "sensor_parts": list(self.sensor_parts),
            "finalized_paths": list(self.finalized_paths),
            "sizes": dict(self.sizes),
            "sensor_warnings": list(self.sensor_warnings),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "stem": self.stem,
            "recording_format": self.recording_format,
        }
