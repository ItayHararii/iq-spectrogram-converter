"""Persist recording parameters and match them to IQ files."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .frequency import display_from_hz
from .paths import recordings_path
from .sftp_client import RemoteEntry

_MAX_SAVED = 2000
_TS_IN_NAME = re.compile(r"(?:^|_)(\d{8})_(\d{6})(?:_|$|\.)")
_TIME_ONLY_IN_NAME = re.compile(r"(?:^|_)(\d{6})_\d{4}(?:\.[^.]+)?$", re.IGNORECASE)
_FOLDER_DATE = re.compile(r"/(\d{8})(?:/|$)")
_PART_SUFFIX = re.compile(r"^(?P<stem>.+)_(?P<part>\d{4})(?P<ext>\.[^.]+)$", re.IGNORECASE)
_MATCH_BEFORE = timedelta(seconds=180)
_MATCH_AFTER_EXTRA = timedelta(seconds=180)
_OFFSET_PENALTY_S = 10_000.0


@dataclass(frozen=True)
class RecordingParams:
    id: str
    host: str
    started_at: datetime
    start_hz: int
    end_hz: int
    center_hz: int
    bandwidth_hz: int
    duration_s: float
    demo: bool = False
    iq_stem: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "host": self.host,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "start_hz": int(self.start_hz),
            "end_hz": int(self.end_hz),
            "center_hz": int(self.center_hz),
            "bandwidth_hz": int(self.bandwidth_hz),
            "duration_s": float(self.duration_s),
            "demo": bool(self.demo),
            "iq_stem": self.iq_stem,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RecordingParams:
        started = datetime.fromisoformat(str(data["started_at"]))
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            host=str(data.get("host") or ""),
            started_at=started,
            start_hz=int(data["start_hz"]),
            end_hz=int(data["end_hz"]),
            center_hz=int(data["center_hz"]),
            bandwidth_hz=int(data["bandwidth_hz"]),
            duration_s=float(data["duration_s"]),
            demo=bool(data.get("demo", False)),
            iq_stem=str(data.get("iq_stem") or ""),
        )


def new_recording(
    *,
    host: str,
    start_hz: int,
    end_hz: int,
    center_hz: int,
    bandwidth_hz: int,
    duration_s: float,
    started_at: datetime | None = None,
    demo: bool = False,
    iq_stem: str = "",
) -> RecordingParams:
    return RecordingParams(
        id=str(uuid.uuid4()),
        host=host,
        started_at=started_at or datetime.now(),
        start_hz=start_hz,
        end_hz=end_hz,
        center_hz=center_hz,
        bandwidth_hz=bandwidth_hz,
        duration_s=float(duration_s),
        demo=demo,
        iq_stem=str(iq_stem or ""),
    )


def format_mhz(hz: int) -> str:
    return display_from_hz(int(hz), "MHz")


def format_duration(seconds: float) -> str:
    value = float(seconds)
    text = f"{value:g}"
    return text


def format_columns(rec: RecordingParams) -> tuple[str, str, str, str, str]:
    return (
        format_mhz(rec.start_hz),
        format_mhz(rec.end_hz),
        format_mhz(rec.center_hz),
        format_mhz(rec.bandwidth_hz),
        format_duration(rec.duration_s),
    )


def iq_group_key(name: str) -> str:
    """Shared identity for split IQ parts such as `_0001.wav` / `_0002.wav`."""
    match = _PART_SUFFIX.match(name or "")
    if match:
        return match.group("stem").casefold()
    return (name or "").casefold()


def iq_part_number(name: str) -> int | None:
    match = _PART_SUFFIX.match(name or "")
    if not match:
        return None
    return int(match.group("part"))


def expand_group_entries(
    selected: list[RemoteEntry],
    all_entries: list[RemoteEntry],
) -> list[RemoteEntry]:
    """Include every split part that shares a group with the selection."""
    keys = {iq_group_key(entry.name) for entry in selected if not entry.is_dir}
    if not keys:
        return []
    found: list[RemoteEntry] = []
    seen: set[str] = set()
    for entry in all_entries:
        if entry.is_dir:
            continue
        if iq_group_key(entry.name) not in keys:
            continue
        if entry.path in seen:
            continue
        seen.add(entry.path)
        found.append(entry)

    def _order(entry: RemoteEntry) -> tuple[str, int, str]:
        part = iq_part_number(entry.name)
        return (iq_group_key(entry.name), part if part is not None else 0, entry.name.casefold())

    found.sort(key=_order)
    return found


def group_lead_entry(entries: list[RemoteEntry]) -> RemoteEntry:
    """Use the first part (`_0001`) when matching a split recording."""
    if not entries:
        raise ValueError("group_lead_entry requires at least one file.")
    numbered = [(iq_part_number(entry.name), entry) for entry in entries]
    with_part = [item for item in numbered if item[0] is not None]
    if with_part:
        return min(with_part, key=lambda item: item[0])[1]
    dated = [entry for entry in entries if entry.modified is not None]
    if dated:
        return min(dated, key=lambda entry: entry.modified or datetime.max)
    return entries[0]


def folder_date_from_path(path: str) -> str | None:
    match = _FOLDER_DATE.search((path or "").replace("\\", "/"))
    return match.group(1) if match else None


def parse_iq_timestamp(name: str, *, folder_date: str | None = None) -> datetime | None:
    match = _TS_IN_NAME.search(name or "")
    if match:
        try:
            return datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            return None
    if not folder_date:
        return None
    time_only = _TIME_ONLY_IN_NAME.search(name or "")
    if not time_only:
        return None
    try:
        return datetime.strptime(folder_date + time_only.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def candidate_file_times(entry: RemoteEntry) -> list[datetime]:
    """Filename stamps are often UTC; SFTP mtime is usually local."""
    return [when for _penalty, when in ranked_file_times(entry)]


def ranked_file_times(entry: RemoteEntry) -> list[tuple[float, datetime]]:
    """(score_penalty, timestamp). Mtime and UTC conversion beat hour-offset guesses."""
    ranked: list[tuple[float, datetime]] = []
    seen: set[datetime] = set()

    def add(value: datetime | None, penalty: float) -> None:
        if value is None:
            return
        naive = _naive(value).replace(microsecond=0)
        if naive in seen:
            return
        seen.add(naive)
        ranked.append((penalty, naive))

    add(entry.modified, 0.0)
    parsed = parse_iq_timestamp(entry.name, folder_date=folder_date_from_path(entry.path))
    add(parsed, 0.5)
    if parsed is not None:
        try:
            add(parsed.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None), 0.0)
        except (OSError, OverflowError, ValueError):
            pass
        for hours in (1, -1, 2, -2, 3, -3, 4, -4):
            add(parsed + timedelta(hours=hours), _OFFSET_PENALTY_S + abs(hours))
    return ranked


def file_time(entry: RemoteEntry) -> datetime | None:
    times = candidate_file_times(entry)
    return times[0] if times else None


def load_recordings(path: Path | None = None) -> list[RecordingParams]:
    file = path or recordings_path()
    if not file.is_file():
        return []
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = raw.get("recordings") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    out: list[RecordingParams] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        if any(key in item for key in ("password", "http_password", "sftp_password")):
            continue
        try:
            out.append(RecordingParams.from_dict(item))
        except (KeyError, TypeError, ValueError):
            continue
    return out


def save_recordings(records: list[RecordingParams], path: Path | None = None) -> None:
    file = path or recordings_path()
    payload = {
        "recordings": [item.to_dict() for item in records[:_MAX_SAVED]],
    }
    file.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def add_recording(record: RecordingParams, path: Path | None = None) -> None:
    records = [record, *[item for item in load_recordings(path) if item.id != record.id]]
    save_recordings(records, path)


def remove_recording(record_id: str, path: Path | None = None) -> None:
    save_recordings([item for item in load_recordings(path) if item.id != record_id], path)


def _host_matches(record: RecordingParams, host: str) -> bool:
    saved = (record.host or "").strip().lower()
    current = (host or "").strip().lower()
    if not saved or not current:
        return True
    return saved == current


def persist_matched_stems(
    matched: dict[str, RecordingParams],
    path: Path | None = None,
) -> None:
    """Remember file stems so later parts and IP changes keep the same metadata."""
    stems: dict[str, str] = {}
    for file_path, record in matched.items():
        name = str(file_path).replace("\\", "/").rsplit("/", 1)[-1]
        stems[record.id] = iq_group_key(name)
    if not stems:
        return
    records = load_recordings(path)
    out: list[RecordingParams] = []
    changed = False
    for record in records:
        stem = stems.get(record.id, "")
        if stem and not record.iq_stem:
            out.append(replace(record, iq_stem=stem))
            changed = True
        else:
            out.append(record)
    if changed:
        save_recordings(out, path)


def group_total_bytes(entries: list[RemoteEntry]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for entry in entries:
        if entry.is_dir:
            continue
        key = iq_group_key(entry.name)
        totals[key] = totals.get(key, 0) + int(entry.size or 0)
    return totals


def group_part_counts(entries: list[RemoteEntry]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        if entry.is_dir:
            continue
        key = iq_group_key(entry.name)
        counts[key] = counts.get(key, 0) + 1
    return counts


def match_recordings_to_entries(
    entries: list[RemoteEntry],
    recordings: list[RecordingParams],
    *,
    host: str = "",
) -> dict[str, RecordingParams]:
    """Map remote file path -> recording parameters.

    Split captures (`*_0001.wav`, `*_0002.wav`, …) share one recording. One
    recording still matches at most one group of parts. Host is a preference
    only: metadata must still apply after the sensor IP changes.
    """
    files = [entry for entry in entries if not entry.is_dir]
    unused = list(recordings)
    groups: dict[str, list[RemoteEntry]] = {}
    for entry in files:
        groups.setdefault(iq_group_key(entry.name), []).append(entry)

    matched_groups: dict[str, RecordingParams] = {}
    used_ids: set[str] = set()

    by_stem: dict[str, RecordingParams] = {}
    for record in unused:
        stem = (record.iq_stem or "").casefold()
        if stem and stem not in by_stem:
            by_stem[stem] = record
    for stem, members in groups.items():
        record = by_stem.get(stem)
        if record is None or record.id in used_ids:
            continue
        matched_groups[stem] = record
        used_ids.add(record.id)

    pairs: list[tuple[float, str, RecordingParams]] = []
    for stem, members in groups.items():
        if stem in matched_groups:
            continue
        lead = group_lead_entry(members)
        ranked = ranked_file_times(lead)
        if not ranked:
            continue
        for record in unused:
            if record.id in used_ids:
                continue
            start = record.started_at - _MATCH_BEFORE
            end = record.started_at + timedelta(seconds=max(record.duration_s, 0)) + _MATCH_AFTER_EXTRA
            best: float | None = None
            for penalty, when in ranked:
                if start <= when <= end:
                    delta = abs((when - record.started_at).total_seconds()) + penalty
                    if not _host_matches(record, host):
                        delta += 1.0
                    if best is None or delta < best:
                        best = delta
            if best is not None:
                pairs.append((best, stem, record))
    pairs.sort(key=lambda item: item[0])
    for _delta, stem, record in pairs:
        if stem in matched_groups or record.id in used_ids:
            continue
        matched_groups[stem] = record
        used_ids.add(record.id)

    matched: dict[str, RecordingParams] = {}
    for stem, record in matched_groups.items():
        for entry in groups[stem]:
            matched[entry.path] = record
    return matched
