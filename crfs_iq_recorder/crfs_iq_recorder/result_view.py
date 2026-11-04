"""What the operator sees after a recording: what was asked next to what came back.

Built from a capture summary (manual recordings) or a collection task (Excel
Collection). Nothing is inferred: the received duration and the recorded
range come from the files' own headers and metadata, otherwise they read
"unknown". Recording, transfer and Excel are three separate states, so a slow
download never looks like a recording still running.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .capture_measure import (
    RESULT_COMPLETED,
    RESULT_FAILED,
    RESULT_PARTIAL,
    RESULT_UNCONFIRMED,
    CaptureMeasurement,
    coverage_differs,
    coverage_text,
    format_mhz_value,
    received_text,
)
from .operator_text import files_text, requested_text, seconds_text, size_text

TAGS = {
    RESULT_COMPLETED: ("Complete", "badgeOk"),
    RESULT_PARTIAL: ("Partial", "badgeWait"),
    RESULT_UNCONFIRMED: ("Uncertain", "badgeWait"),
    RESULT_FAILED: ("Failed", "badgeBad"),
}
# A full-length recording of another range than requested: still counted as completed (the class is about
# time and data), but the badge must not read as if the requested range was covered.
OTHER_RANGE_TAG = ("Other range", "badgeWait")
OTHER_RANGE = ", not the requested range"
RECORDING_STATES = {
    RESULT_COMPLETED: "Recording complete",
    RESULT_PARTIAL: "Partial recording",
    RESULT_UNCONFIRMED: "Recording not confirmed",
    RESULT_FAILED: "Not recorded",
}

ON_SENSOR = "On the sensor"
DOWNLOAD_QUEUED = "Download queued"
DOWNLOADING = "Downloading"
SAVED_LOCALLY = "Download saved"
DOWNLOAD_FAILED = "Download failed"
LOCAL_MISSING = "Local copy missing"
# Kept for callers; the card itself no longer shows it (the states line says what the download is doing).
DOWNLOAD_REQUIRED = "Download required."

EXCEL_UPDATED = "Excel updated"
EXCEL_PENDING = "Excel update pending"
EXCEL_FAILED = "Excel update failed"


@dataclass
class ResultView:
    title: str = ""
    requested: str = ""
    received: str = ""
    recorded: str = ""
    result: str = ""
    tag: str = ""
    tag_style: str = "badgeIdle"
    recording_state: str = ""
    transfer_state: str = ""
    excel_state: str = ""
    remote_paths: list[str] = field(default_factory=list)
    local_paths: list[str] = field(default_factory=list)
    centre_hz: float | None = None
    bandwidth_hz: float | None = None
    attempt: int = 1
    # The range the operator asked for, exactly as sent (markers on the waterfall).
    requested_start_hz: float | None = None
    requested_stop_hz: float | None = None
    # Compact facts for the card: '1 file · 0.503 s · 251.4 MB' (measured values only).
    facts_text: str = ""
    # 'Sensor reported 3690-3790 MHz' when the file's own range differs from the requested one; '' otherwise.
    coverage_note: str = ""

    @property
    def local_ready(self) -> bool:
        """Every file of the recording is in the download folder."""
        return bool(self.remote_paths) and len(self.local_paths) == len(self.remote_paths)

    @property
    def states_text(self) -> str:
        """'Recording complete · Download saved · Excel updated'."""
        return " · ".join(item for item in (self.recording_state, self.transfer_state, self.excel_state) if item)


def local_copies(remote_paths: Iterable[str], local_dir: Path | str | None) -> list[str]:
    """The recording's files found in the download folder, in the sensor's order.

    Looks in the sensor's dated sub-folder first, then in the flat root used by
    earlier versions, so older downloads are still recognised where they are.
    """
    if not local_dir:
        return []
    from .download_paths import candidate_paths

    folder = Path(local_dir)
    out: list[str] = []
    for path in remote_paths:
        for candidate in candidate_paths(folder, str(path)):
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    out.append(str(candidate))
                    break
            except OSError:
                continue
    return out


def _facts(measurement: CaptureMeasurement | None, files: int) -> str:
    """'1 file · 0.503 s · 251.4 MB'; 'duration unknown' when the files could not be read."""
    if files <= 0:
        return "No files"
    parts = [files_text(files)]
    seconds = measurement.duration_s if measurement is not None else None
    parts.append(f"{seconds:.3f} s" if seconds is not None else "duration unknown")
    total = sum(int(part.size or 0) for part in measurement.parts) if measurement is not None else 0
    if total:
        parts.append(size_text(total))
    return " · ".join(parts)


def _coverage_note(measurement: CaptureMeasurement | None, centre_hz: Any, bandwidth_hz: Any) -> str:
    """The sensor-reported range, only when it is known and differs from the requested one."""
    if measurement is None:
        return ""
    text = coverage_text(measurement)
    if text == "unknown":
        return ""
    try:
        start = int(float(centre_hz) - float(bandwidth_hz) / 2)
        stop = int(float(centre_hz) + float(bandwidth_hz) / 2)
    except (TypeError, ValueError):
        return ""
    if coverage_differs(measurement, start, stop):
        return f"Sensor reported {text}, not the requested range"
    return ""


def _requested(bandwidth_hz: Any, seconds: float) -> str:
    try:
        bandwidth = float(bandwidth_hz)
    except (TypeError, ValueError):
        bandwidth = 0.0
    if bandwidth > 0:
        return f"Requested: {requested_text(bandwidth, seconds)}"
    return f"Requested: {seconds_text(seconds)}"


RECEIVED = "Received (read from the files)"
RECORDED = "Recorded (file metadata)"


def _received(measurement: CaptureMeasurement | None, files: int) -> str:
    """'Received (read from the files): 1 file, 0.503 s at 125 MS/s, 251.4 MB'."""
    if files <= 0:
        return "Received: no files"
    seconds = measurement.duration_s if measurement is not None else None
    if seconds is None:
        return f"{RECEIVED}: {received_text(measurement, files)}"
    text = f"{files_text(files)}, {seconds:.3f} s"
    rate = measurement.sample_rate_hz
    if rate:
        text += f" at {format_mhz_value(rate)} MS/s"
    total = sum(int(part.size or 0) for part in measurement.parts)
    if total:
        text += f", {size_text(total)}"
    return f"{RECEIVED}: {text}"


def _recorded(measurement: CaptureMeasurement | None, centre_hz: Any, bandwidth_hz: Any) -> str:
    text = coverage_text(measurement)
    if text == "unknown":
        return f"{RECORDED}: unknown"
    try:
        start = int(float(centre_hz) - float(bandwidth_hz) / 2)
        stop = int(float(centre_hz) + float(bandwidth_hz) / 2)
    except (TypeError, ValueError):
        return f"{RECORDED}: {text}"
    if coverage_differs(measurement, start, stop):
        return f"{RECORDED}: {text}{OTHER_RANGE}"
    return f"{RECORDED}: {text}"


def _tag(result: str) -> tuple[str, str]:
    return TAGS.get(result, ("", "badgeIdle"))


def view_from_summary(
    summary: Any,
    *,
    title: str = "",
    local_dir: Path | str | None = None,
    bandwidth_hz: Any = None,
    centre_hz: Any = None,
) -> ResultView:
    """A manual recording (or any capture) from its CaptureSummary or its dict."""
    data = summary if isinstance(summary, dict) else summary.as_dict()
    payload = dict(data.get("payload") or {})
    bandwidth = payload.get("bandwidth", bandwidth_hz)
    centre = payload.get("center_frequency", centre_hz)
    seconds = float(data.get("requested_s") or payload.get("duration") or 0.0)
    measurement = CaptureMeasurement.from_dict(data.get("measurement") or {})
    paths = [str(p) for p in data.get("finalized_paths") or []]
    result = str(data.get("result") or (RESULT_UNCONFIRMED if paths else RESULT_FAILED))
    recorded = _recorded(measurement if paths else None, centre, bandwidth)
    tag, style = OTHER_RANGE_TAG if result == RESULT_COMPLETED and recorded.endswith(OTHER_RANGE) else _tag(result)
    local = local_copies(paths, local_dir)
    req_start = req_stop = None
    try:
        if centre is not None and bandwidth:
            req_start = float(centre) - float(bandwidth) / 2
            req_stop = float(centre) + float(bandwidth) / 2
    except (TypeError, ValueError):
        req_start = req_stop = None
    return ResultView(
        title=title,
        requested=_requested(bandwidth, seconds),
        received=_received(measurement, len(paths)),
        recorded=recorded,
        result=result,
        tag=tag,
        tag_style=style,
        recording_state=RECORDING_STATES.get(result, ""),
        transfer_state=(SAVED_LOCALLY if len(local) == len(paths) else ON_SENSOR) if paths else "",
        remote_paths=paths,
        local_paths=local,
        centre_hz=measurement.centre_hz,
        bandwidth_hz=measurement.bandwidth_hz,
        requested_start_hz=req_start,
        requested_stop_hz=req_stop,
        facts_text=_facts(measurement if paths else None, len(paths)),
        coverage_note=_coverage_note(measurement if paths else None, centre, bandwidth),
    )


def transfer_state(files: list[dict[str, Any]]) -> str:
    """'Downloading 1 of 2' / 'Download saved' / 'Download failed' / 'Download queued' / 'On the sensor'."""
    if not files:
        return ""
    statuses = [str(item.get("download_status") or "") for item in files]
    saved = sum(1 for s in statuses if s == "verified")
    if saved == len(files):
        return SAVED_LOCALLY
    if any(s == "failed" for s in statuses):
        return DOWNLOAD_FAILED
    if any(s in {"downloading", "present"} for s in statuses):
        return f"{DOWNLOADING} {saved} of {len(files)}" if len(files) > 1 else DOWNLOADING
    if any(s == "pending" for s in statuses):
        return DOWNLOAD_QUEUED
    return ON_SENSOR


def view_from_task(
    state: Any,
    *,
    title: str,
    bandwidth_hz: Any,
    centre_hz: Any,
    seconds: float,
    excel_state: str = "",
    local_dir: Path | str | None = None,
    requested_start_hz: Any = None,
    requested_stop_hz: Any = None,
) -> ResultView:
    """An Excel Collection row from its task state (cellular_runner.TaskState)."""
    data = dict(getattr(state, "summary", None) or {})
    files = [dict(item) for item in getattr(state, "files", None) or []]
    view = view_from_summary(
        {**data, "requested_s": data.get("requested_s") or seconds, "result": getattr(state, "result", "") or data.get("result")},
        title=title,
        local_dir=local_dir,
        bandwidth_hz=bandwidth_hz,
        centre_hz=centre_hz,
    )
    if requested_start_hz is not None and requested_stop_hz is not None:
        try:
            view.requested_start_hz = float(requested_start_hz)
            view.requested_stop_hz = float(requested_stop_hz)
        except (TypeError, ValueError):
            pass
    if not data:
        # Rejected or not sent: there is no capture summary, only the task's own outcome.
        view.received = "Received: no files"
        view.recorded = f"{RECORDED}: unknown"
        view.facts_text = "No files"
    if files:
        view.transfer_state = transfer_state(files)
        recorded_local = [str(item.get("local_path")) for item in files if item.get("local_path") and Path(str(item.get("local_path"))).is_file()]
        if len(recorded_local) == len(files):
            view.local_paths = recorded_local
        elif view.transfer_state == SAVED_LOCALLY:
            # Verified once, but a file is gone from the download folder: say so, never "saved" next to "required".
            view.transfer_state = LOCAL_MISSING
            view.local_paths = []
    view.excel_state = excel_state
    view.attempt = max(int(getattr(state, "attempts", 1) or 1), 1)
    return view


__all__ = [
    "DOWNLOADING",
    "DOWNLOAD_FAILED",
    "DOWNLOAD_QUEUED",
    "DOWNLOAD_REQUIRED",
    "EXCEL_FAILED",
    "EXCEL_PENDING",
    "EXCEL_UPDATED",
    "LOCAL_MISSING",
    "ON_SENSOR",
    "SAVED_LOCALLY",
    "ResultView",
    "local_copies",
    "transfer_state",
    "view_from_summary",
    "view_from_task",
]
