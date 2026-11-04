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
    RESULT_COMPLETED: "Files ready",
    RESULT_PARTIAL: "Files ready",
    RESULT_UNCONFIRMED: "Not confirmed",
    RESULT_FAILED: "Not recorded",
}

ON_SENSOR = "On the sensor"
DOWNLOADING = "Downloading"
SAVED_LOCALLY = "Saved locally"
DOWNLOAD_FAILED = "Download failed"
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

    @property
    def local_ready(self) -> bool:
        """Every file of the recording is in the download folder."""
        return bool(self.remote_paths) and len(self.local_paths) == len(self.remote_paths)

    @property
    def states_text(self) -> str:
        """'Files ready · Saved locally · Excel updated'."""
        return " · ".join(item for item in (self.recording_state, self.transfer_state, self.excel_state) if item)


def local_copies(remote_paths: Iterable[str], local_dir: Path | str | None) -> list[str]:
    """The recording's files found in the download folder, in the sensor's order."""
    if not local_dir:
        return []
    folder = Path(local_dir)
    out: list[str] = []
    for path in remote_paths:
        candidate = folder / str(path).replace("\\", "/").rsplit("/", 1)[-1]
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                out.append(str(candidate))
        except OSError:
            continue
    return out


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
    )


def transfer_state(files: list[dict[str, Any]]) -> str:
    """'Downloading 1 of 2' / 'Saved locally' / 'Download failed' / 'On the sensor' from a task's files."""
    if not files:
        return ""
    statuses = [str(item.get("download_status") or "") for item in files]
    saved = sum(1 for s in statuses if s == "verified")
    if saved == len(files):
        return SAVED_LOCALLY
    if any(s == "failed" for s in statuses):
        return DOWNLOAD_FAILED
    if any(s in {"pending", "downloading", "present"} for s in statuses):
        return f"{DOWNLOADING} {saved} of {len(files)}" if len(files) > 1 else DOWNLOADING
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
    if not data:
        # Rejected or not sent: there is no capture summary, only the task's own outcome.
        view.received = "Received: no files"
        view.recorded = f"{RECORDED}: unknown"
    if files:
        view.transfer_state = transfer_state(files)
        recorded_local = [str(item.get("local_path")) for item in files if item.get("local_path") and Path(str(item.get("local_path"))).is_file()]
        if len(recorded_local) == len(files):
            view.local_paths = recorded_local
    view.excel_state = excel_state
    view.attempt = max(int(getattr(state, "attempts", 1) or 1), 1)
    return view


__all__ = [
    "DOWNLOADING",
    "DOWNLOAD_FAILED",
    "DOWNLOAD_REQUIRED",
    "EXCEL_FAILED",
    "EXCEL_PENDING",
    "EXCEL_UPDATED",
    "ON_SENSOR",
    "SAVED_LOCALLY",
    "ResultView",
    "local_copies",
    "transfer_state",
    "view_from_summary",
    "view_from_task",
]
