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
OUTCOME_CONFIRMED = "confirmed"  # files settled on the sensor; `result` says what they hold
OUTCOME_UNKNOWN = "unknown"  # request reached the sensor; no settled file confirms it
OUTCOME_NOT_SENT = "not_sent"  # nothing reached the sensor; safe to retry
OUTCOME_SENSOR_ERROR = "sensor_error"  # the sensor reported an error for the task
OUTCOME_FAILED = "failed"  # an application error ended the capture; state uncertain
OUTCOME_BUSY = "busy"  # never started: another recording owns the sensor

# The Node closes its receiver connection about 30 s after a recording's event stream closes. A
# request that reaches it while that connection closes gets no samples: the sensor says "finished"
# within seconds and writes no file (Node 100-18, 2.25: 3 of 14 requests sent 30 s after the
# previous stream closed; none of 74 sent 21-29 s or 31-45 s after it).
RECEIVER_CLOSE_WINDOW_S = (27.5, 32.5)


def receiver_wait_s(since_stream_closed_s: float) -> float:
    """Seconds to hold a request so it does not reach the sensor while it closes its receiver connection."""
    start, end = RECEIVER_CLOSE_WINDOW_S
    if start <= since_stream_closed_s < end:
        return end - since_stream_closed_s
    return 0.0


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
    # Seconds of IQ read from the files' WAVE headers; None when unknown.
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
    # completed / partial / unconfirmed / failed (capture_measure.RESULT_*).
    result: str = ""
    result_reason: str = ""
    # What the files' headers and metadata say (capture_measure.CaptureMeasurement.to_dict()).
    measurement: dict[str, Any] = field(default_factory=dict)
    # What the sensor's own reply said: centre, bandwidth, duration, file count, its error text.
    sensor_reply: dict[str, Any] = field(default_factory=dict)
    sensor_error: str = ""
    # The exact recording_scans entry that was POSTed.
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def confirmed(self) -> bool:
        return self.outcome == OUTCOME_CONFIRMED and self.parts > 0

    @property
    def short(self) -> bool:
        """The recording is partial (measured from the files, never from the clock)."""
        return self.result == "partial"

    @property
    def retry_is_safe(self) -> bool:
        """True only when nothing reached the sensor."""
        return self.outcome in {OUTCOME_NOT_SENT, OUTCOME_BUSY}

    @property
    def sensor_recorded_nothing(self) -> bool:
        """The sensor took the request and said "finished" without starting: no reply naming a file, no
        file on its storage, and no message or error that explains it (so sending it again is no repeat)."""
        return (
            self.outcome == OUTCOME_UNKNOWN
            and self.request_sent
            and self.sensor_finished
            and not self.stream_lost
            and not self.sensor_reply
            and not self.sensor_parts
            and not self.finalized_paths
            and not self.sizes
            and not self.sensor_warnings
            and not self.sensor_error
        )

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
            "result": self.result,
            "result_reason": self.result_reason,
            "measurement": dict(self.measurement),
            "sensor_reply": dict(self.sensor_reply),
            "sensor_error": self.sensor_error,
            "payload": dict(self.payload),
        }
