"""Persistent Excel queue and workbook writes. Never stores passwords."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .excel_com import append_rows_com, excel_has_workbook
from .excel_writer import (
    ExcelBusyError,
    ExcelRow,
    append_rows_openpyxl,
    backup_workbook,
    workbook_has_iq_filename,
)
from .frequency import FreqTextStyle
from .paths import excel_log_path
from .recording_history import (
    iq_group_key,
    join_iq_filenames,
    recording_group_key,
    remote_directory,
    sort_iq_filenames,
    split_iq_filenames,
)


def _optional_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def _duration_map(value) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, float] = {}
    for key, item in value.items():
        number = _optional_float(item)
        if number is None:
            continue
        out[str(key)] = number
    return out


def sum_verified_durations(filenames: list[str], durations: dict[str, float]) -> float | None:
    values: list[float] = []
    for name in filenames:
        if name not in durations:
            return None
        values.append(durations[name])
    if not values:
        return None
    return sum(values)


def file_log_key(sensor_id: str, remote_path: str) -> str:
    identity = (sensor_id or "").strip() or "unknown"
    path = (remote_path or "").replace("\\", "/").strip()
    return f"{identity}|{path}"


@dataclass
class PendingExcelItem:
    id: str
    workbook: str
    worksheet: str
    collection_event: str
    target_class: str
    start_hz: int
    end_hz: int
    start_unit: str
    start_space: bool
    stop_unit: str
    stop_space: bool
    sensor: str
    filename: str
    local_path: str
    remote_path: str
    sensor_id: str
    created_at: str
    duration_s: float | None = None
    record_id: str = ""
    group_key: str = ""
    durations: dict[str, float] | None = None

    def filenames(self) -> list[str]:
        return split_iq_filenames(self.filename) or ([self.filename] if self.filename else [])

    def group(self) -> str:
        if self.group_key:
            return self.group_key
        names = self.filenames()
        stem = iq_group_key(names[0]) if names else ""
        return recording_group_key(
            sensor_id=self.sensor_id,
            directory=remote_directory(self.remote_path),
            stem=stem,
            record_id=self.record_id,
        )

    def key(self) -> str:
        return file_log_key(self.sensor_id, self.remote_path)

    def to_row(self) -> ExcelRow:
        return ExcelRow(
            worksheet=self.worksheet,
            collection_event=self.collection_event,
            target_class=self.target_class,
            start_hz=self.start_hz,
            end_hz=self.end_hz,
            start_style=FreqTextStyle(self.start_unit, self.start_space),
            stop_style=FreqTextStyle(self.stop_unit, self.stop_space),
            sensor=self.sensor,
            filename=self.filename,
            local_path=self.local_path,
            remote_path=self.remote_path,
            sensor_id=self.sensor_id,
            duration_s=self.duration_s,
            record_id=self.record_id,
            group_key=self.group(),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workbook": self.workbook,
            "worksheet": self.worksheet,
            "collection_event": self.collection_event,
            "target_class": self.target_class,
            "start_hz": self.start_hz,
            "end_hz": self.end_hz,
            "start_unit": self.start_unit,
            "start_space": self.start_space,
            "stop_unit": self.stop_unit,
            "stop_space": self.stop_space,
            "sensor": self.sensor,
            "filename": self.filename,
            "local_path": self.local_path,
            "remote_path": self.remote_path,
            "sensor_id": self.sensor_id,
            "created_at": self.created_at,
            "duration_s": self.duration_s,
            "record_id": self.record_id,
            "group_key": self.group(),
            "durations": self.durations or {},
        }

    @classmethod
    def from_dict(cls, data: dict) -> PendingExcelItem:
        return cls(
            id=str(data.get("id") or uuid.uuid4()),
            workbook=str(data.get("workbook") or ""),
            worksheet=str(data.get("worksheet") or ""),
            collection_event=str(data.get("collection_event") or ""),
            target_class=str(data.get("target_class") or ""),
            start_hz=int(data["start_hz"]),
            end_hz=int(data["end_hz"]),
            start_unit=str(data.get("start_unit") or "MHz"),
            start_space=bool(data.get("start_space", True)),
            stop_unit=str(data.get("stop_unit") or "MHz"),
            stop_space=bool(data.get("stop_space", True)),
            sensor=str(data.get("sensor") or ""),
            filename=str(data.get("filename") or ""),
            local_path=str(data.get("local_path") or ""),
            remote_path=str(data.get("remote_path") or ""),
            sensor_id=str(data.get("sensor_id") or ""),
            created_at=str(data.get("created_at") or ""),
            duration_s=_optional_float(data.get("duration_s")),
            record_id=str(data.get("record_id") or ""),
            group_key=str(data.get("group_key") or ""),
            durations=_duration_map(data.get("durations")),
        )


class ExcelLogStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or excel_log_path()
        self.logged: set[str] = set()
        self.queue: list[PendingExcelItem] = []
        self.backups: dict[str, str] = {}
        self.groups: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return
        logged = raw.get("logged") or []
        if isinstance(logged, list):
            self.logged = {str(item) for item in logged if item}
        backups = raw.get("backups") or {}
        if isinstance(backups, dict):
            self.backups = {str(k): str(v) for k, v in backups.items()}
        groups = raw.get("groups") or {}
        if isinstance(groups, dict):
            parsed: dict[str, dict] = {}
            for key, value in groups.items():
                if not isinstance(value, dict):
                    continue
                parsed[str(key)] = {
                    "filenames": sort_iq_filenames(value.get("filenames") or []),
                    "durations": _duration_map(value.get("durations")),
                    "workbook": str(value.get("workbook") or ""),
                    "worksheet": str(value.get("worksheet") or ""),
                }
            self.groups = parsed
        rows = raw.get("queue") or []
        if isinstance(rows, list):
            out: list[PendingExcelItem] = []
            for item in rows:
                if not isinstance(item, dict):
                    continue
                try:
                    out.append(PendingExcelItem.from_dict(item))
                except (KeyError, TypeError, ValueError):
                    continue
            self.queue = out

    def save(self) -> None:
        payload = {
            "logged": sorted(self.logged),
            "backups": self.backups,
            "groups": self.groups,
            "queue": [item.to_dict() for item in self.queue],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def is_logged(self, sensor_id: str, remote_path: str) -> bool:
        return file_log_key(sensor_id, remote_path) in self.logged

    def mark_logged(self, sensor_id: str, remote_path: str) -> None:
        self.logged.add(file_log_key(sensor_id, remote_path))

    def ensure_backup(self, workbook: Path) -> Path | None:
        key = str(workbook.resolve())
        existing = self.backups.get(key)
        if existing and Path(existing).is_file():
            return Path(existing)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = workbook.with_name(f"{workbook.stem}.crfs-backup-{stamp}{workbook.suffix}")
        try:
            backup_workbook(workbook, dest)
        except OSError:
            return None
        self.backups[key] = str(dest)
        self.save()
        return dest

    def enqueue(self, item: PendingExcelItem) -> bool:
        if self.is_logged(item.sensor_id, item.remote_path):
            return False
        if any(existing.key() == item.key() for existing in self.queue):
            return False
        self.queue.append(item)
        self.save()
        return True

    def pending_count(self) -> int:
        return len(self.queue)


def pending_from_row(
    *,
    workbook: Path,
    row: ExcelRow,
) -> PendingExcelItem:
    return PendingExcelItem(
        id=str(uuid.uuid4()),
        workbook=str(workbook),
        worksheet=row.worksheet,
        collection_event=row.collection_event,
        target_class=row.target_class,
        start_hz=row.start_hz,
        end_hz=row.end_hz,
        start_unit=row.start_style.unit,
        start_space=row.start_style.space_before_unit,
        stop_unit=row.stop_style.unit,
        stop_space=row.stop_style.space_before_unit,
        sensor=row.sensor,
        filename=row.filename,
        local_path=row.local_path,
        remote_path=row.remote_path,
        sensor_id=row.sensor_id,
        created_at=datetime.now().isoformat(timespec="seconds"),
        duration_s=row.duration_s,
        record_id=row.record_id,
        group_key=row.group_key,
        durations={row.filename: row.duration_s} if row.filename and row.duration_s is not None else {},
    )


def write_excel_rows(path: Path, rows: list[ExcelRow]) -> list[int]:
    if excel_has_workbook(path):
        return append_rows_com(path, rows)
    try:
        return append_rows_openpyxl(path, rows)
    except ExcelBusyError:
        if excel_has_workbook(path):
            return append_rows_com(path, rows)
        raise


def _merged_group_row(store: ExcelLogStore, items: list[PendingExcelItem]) -> ExcelRow:
    first = items[0]
    group = store.groups.get(first.group(), {})
    names = sort_iq_filenames(list(group.get("filenames") or []) + [name for item in items for name in item.filenames()])
    duration = None
    for item in items:
        if item.duration_s is not None:
            duration = item.duration_s
            break
    if duration is None:
        duration = _optional_float((group.get("durations") or {}).get("_recording"))
    row = first.to_row()
    return ExcelRow(
        worksheet=row.worksheet,
        collection_event=row.collection_event,
        target_class=row.target_class,
        start_hz=row.start_hz,
        end_hz=row.end_hz,
        start_style=row.start_style,
        stop_style=row.stop_style,
        sensor=row.sensor,
        filename=join_iq_filenames(names),
        local_path=row.local_path,
        remote_path=row.remote_path,
        sensor_id=row.sensor_id,
        duration_s=duration,
        record_id=row.record_id,
        group_key=first.group(),
    )


def _remember_group(store: ExcelLogStore, item: PendingExcelItem, row: ExcelRow) -> None:
    key = item.group()
    existing = store.groups.get(key) or {}
    durations = dict(existing.get("durations") or {})
    if item.duration_s is not None:
        durations["_recording"] = item.duration_s
    store.groups[key] = {
        "filenames": sort_iq_filenames(list(existing.get("filenames") or []) + split_iq_filenames(row.filename)),
        "durations": durations,
        "workbook": item.workbook,
        "worksheet": item.worksheet,
    }


def flush_excel_queue(store: ExcelLogStore) -> tuple[int, int]:
    """Return (saved, still_pending). Reconcile workbook rows before writing."""
    remaining: list[PendingExcelItem] = []
    saved = 0
    by_book: dict[str, list[PendingExcelItem]] = {}
    for item in store.queue:
        if store.is_logged(item.sensor_id, item.remote_path):
            saved += 1
            continue
        book = Path(item.workbook)
        if book.is_file():
            try:
                if workbook_has_iq_filename(book, item.worksheet, item.filename):
                    store.mark_logged(item.sensor_id, item.remote_path)
                    _remember_group(store, item, item.to_row())
                    saved += 1
                    continue
            except Exception:
                pass
        by_book.setdefault(str(book), []).append(item)
    for book_text, items in by_book.items():
        book = Path(book_text)
        if not book.is_file():
            remaining.extend(items)
            continue
        store.ensure_backup(book)
        grouped: dict[str, list[PendingExcelItem]] = {}
        for item in items:
            grouped.setdefault(item.group(), []).append(item)
        rows = [_merged_group_row(store, batch) for batch in grouped.values()]
        try:
            write_excel_rows(book, rows)
        except ExcelBusyError:
            remaining.extend(items)
            continue
        except Exception:
            remaining.extend(items)
            continue
        for item in items:
            store.mark_logged(item.sensor_id, item.remote_path)
            _remember_group(store, item, _merged_group_row(store, [item]))
            saved += 1
    store.queue = remaining
    store.save()
    return saved, len(remaining)
