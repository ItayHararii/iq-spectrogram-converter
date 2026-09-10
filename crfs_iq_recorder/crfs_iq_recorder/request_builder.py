"""Build the exact EMP remote_recording_scans POST body."""

from __future__ import annotations

from typing import Any

from .constants import DEFAULT_RECORDING_FORMAT, FORMAT_BY_ID
from .frequency import FrequencyPlan


class RequestBuildError(ValueError):
    """Payload could not be constructed."""


def build_recording_payload(
    plan: FrequencyPlan,
    *,
    task_id: str,
    duration: float,
    rate: float,
    capture_length: float,
    recording_format: str,
) -> dict[str, Any]:
    task = (task_id or "").strip()
    if not task:
        raise RequestBuildError("task_id is empty.")
    fmt = (recording_format or "").strip()
    if not fmt:
        raise RequestBuildError("recording_format is empty.")
    info = FORMAT_BY_ID.get(fmt)
    if info is not None and not info.enabled:
        raise RequestBuildError(
            f"recording_format {fmt!r} is listed but not enabled until its EMP identifier is verified."
        )
    for name, value in (
        ("duration", duration),
        ("rate", rate),
        ("capture_length", capture_length),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise RequestBuildError(f"{name} must be a JSON number.")
        if value != value or value in (float("inf"), float("-inf")):
            raise RequestBuildError(f"{name} must be a finite number.")
    timing = float(duration)
    # Key order matches the user-supplied contract.
    return {
        "remote_recording_scans": [
            {
                "center_frequency": int(plan.center_hz),
                "bandwidth": int(plan.bandwidth_hz),
                "task_id": task,
                "duration": timing,
                "rate": float(rate),
                "capture_length": float(capture_length),
                "recording_format": fmt,
            }
        ]
    }


def build_payload_for_recording_time(
    plan: FrequencyPlan,
    recording_seconds: float,
    *,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Map one recording-time value onto duration, rate, and capture_length."""
    from .constants import DEFAULT_RECORDING_FORMAT, DEFAULT_TASK_ID

    seconds = float(recording_seconds)
    duration = rate = capture_length = seconds
    return build_recording_payload(
        plan,
        task_id=task_id or DEFAULT_TASK_ID,
        duration=duration,
        rate=rate,
        capture_length=capture_length,
        recording_format=DEFAULT_RECORDING_FORMAT,
    )


def default_payload() -> dict[str, Any]:
    from .frequency import from_center_bandwidth
    from .constants import (
        DEFAULT_BANDWIDTH_HZ,
        DEFAULT_CAPTURE_LENGTH,
        DEFAULT_CENTER_HZ,
        DEFAULT_DURATION,
        DEFAULT_RATE,
        DEFAULT_TASK_ID,
    )

    return build_recording_payload(
        from_center_bandwidth(DEFAULT_CENTER_HZ, DEFAULT_BANDWIDTH_HZ),
        task_id=DEFAULT_TASK_ID,
        duration=DEFAULT_DURATION,
        rate=DEFAULT_RATE,
        capture_length=DEFAULT_CAPTURE_LENGTH,
        recording_format=DEFAULT_RECORDING_FORMAT,
    )
