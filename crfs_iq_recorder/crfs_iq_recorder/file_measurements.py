"""What the application read from each sensor file's own header and metadata.

Sensor Files shows these next to the requested values of the recording
history. Entries are kept by the file's path on the sensor (its date folder
and time-stamped name), so a sensor reached at another address keeps them; an
entry applies only while the file still has the size it had when it was read.
Anything else is "unknown". Nothing here is estimated.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .json_store import read_json, write_json

FILENAME = ".crfs_iq_recorder_file_measurements.json"
MAX_ENTRIES = 5000
_FIELDS = ("size", "duration_s", "sample_rate_hz", "centre_hz", "bandwidth_hz")
# Download workers record from their own threads.
_lock = threading.Lock()


def measurements_path() -> Path:
    from .paths import user_config_dir

    return user_config_dir() / FILENAME


def _key(remote_path: str) -> str:
    return str(remote_path or "").replace("\\", "/")


def load_measurements(path: Path | None = None) -> dict[str, dict[str, Any]]:
    raw = read_json(path or measurements_path())
    files = raw.get("files") if isinstance(raw, dict) else None
    return {str(k): dict(v) for k, v in files.items() if isinstance(v, dict)} if isinstance(files, dict) else {}


def _entry(data: dict[str, Any], host: str) -> dict[str, Any] | None:
    if not data.get("path") or data.get("duration_s") is None:
        return None
    if any("truncated" in str(problem) or "still being written" in str(problem) for problem in data.get("problems") or []):
        return None
    out: dict[str, Any] = {name: data.get(name) for name in _FIELDS}
    out["size"] = int(out.get("size") or 0)
    if out["size"] <= 0:
        return None
    out["host"] = (host or "").strip()
    out["read_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def record_parts(host: str, parts: Iterable[Any], path: Path | None = None) -> int:
    """Keep the readable parts of a measurement (CaptureMeasurement.parts or their dicts)."""
    found: dict[str, dict[str, Any]] = {}
    for part in parts or []:
        data = part if isinstance(part, dict) else part.to_dict()
        entry = _entry(data, host)
        if entry is not None:
            found[_key(str(data["path"]))] = entry
    if not found:
        return 0
    file = path or measurements_path()
    with _lock:
        store = load_measurements(file)
        fresh = {
            key: entry
            for key, entry in found.items()
            if any(store.get(key, {}).get(name) != entry.get(name) for name in _FIELDS)
        }
        if not fresh:
            return 0
        store.update(fresh)
        if len(store) > MAX_ENTRIES:
            newest = sorted(store.items(), key=lambda item: str(item[1].get("read_at_utc") or ""), reverse=True)
            store = dict(newest[:MAX_ENTRIES])
        try:
            file.parent.mkdir(parents=True, exist_ok=True)
            write_json(file, {"files": store})
        except OSError:
            return 0
    return len(fresh)


def reading_from_local_copy(remote_path: str, local_path: Path) -> dict[str, Any] | None:
    """A downloaded WAVE copy's header and metadata, as a reading of the sensor's file.

    Covers files the application did not read when they were recorded (made
    by another program or an older version, downloaded from Sensor Files).
    """
    from .iq_merge import inspect_part

    try:
        info = inspect_part(Path(local_path))
    except Exception:  # noqa: BLE001 - an unreadable copy gives no reading
        return None
    rate = float(info.fmt.sample_rate or 0)
    meta = info.meta
    return {
        "path": remote_path,
        "size": int(info.size),
        "duration_s": info.frames / rate if rate else None,
        "sample_rate_hz": rate or None,
        "centre_hz": meta.rf_frequency_hz if meta is not None else None,
        "bandwidth_hz": meta.bandwidth_hz if meta is not None else None,
        "problems": list(info.problems),
    }


def lookup(store: dict[str, dict[str, Any]], remote_path: str, size: int) -> dict[str, Any] | None:
    """The reading for this file, or None when there is none for its current size."""
    entry = store.get(_key(remote_path))
    if entry is None or int(entry.get("size") or 0) != int(size or 0):
        return None
    return entry


def _agreed(readings: list[dict[str, Any]], name: str) -> float | None:
    values = {reading.get(name) for reading in readings}
    if len(values) != 1:
        return None
    value = next(iter(values))
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mhz(hz: float) -> str:
    text = f"{hz / 1e6:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def recorded_columns(readings: list[dict[str, Any] | None], *, is_iq: bool) -> list[str]:
    """Sensor Files' [Recorded (MHz), Recorded (s), Rate (MS/s)] for one file or all parts of a capture.

    'unknown' unless every file has been read; '' for files that are not IQ recordings.
    """
    if not is_iq:
        return ["", "", ""]
    if not readings or any(reading is None for reading in readings):
        return ["unknown", "unknown", "unknown"]
    known = [reading for reading in readings if reading is not None]
    durations = [reading.get("duration_s") for reading in known]
    seconds = sum(float(value) for value in durations) if all(value is not None for value in durations) else None
    centre = _agreed(known, "centre_hz")
    bandwidth = _agreed(known, "bandwidth_hz")
    rate = _agreed(known, "sample_rate_hz")
    span = (
        f"{_mhz(centre - bandwidth / 2)}–{_mhz(centre + bandwidth / 2)}"
        if centre is not None and bandwidth is not None and bandwidth > 0
        else "unknown"
    )
    return [
        span,
        f"{seconds:.4f}" if seconds is not None else "unknown",
        _mhz(rate) if rate else "unknown",
    ]


__all__ = [
    "FILENAME",
    "load_measurements",
    "lookup",
    "measurements_path",
    "reading_from_local_copy",
    "record_parts",
    "recorded_columns",
]
