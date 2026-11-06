"""Fill the protocol worksheet's own result columns, cell by cell.

The worksheet keeps its nine columns (Band, Direction, Duplex, Freq Start,
Freq Stop, Sensor, IQ Recording, IQ Duration (s), Recording Date & Time; the
last four are filled when present). Nothing is appended to the right and no
other sheet is written.

Blocks. The protocol rows read at import time are *block 0*: the plan. The
first pass fills their result cells in place when they are still empty.
Every later pass (or a first pass over rows that already hold results)
appends a block of the same rows below the last used row: the key columns
are copied with their formats, then the results are filled in. A block is
located again before every write (its first row is stored, and the key
cells are checked), so rows inserted by hand above it do not make the
results land on the wrong row; a block whose rows cannot be found is
reported, not guessed.

Ownership. The application writes only the result cells of the rows it
fills and, for appended blocks, the key cells it created. Everything else
in the workbook (other sheets, manual notes, formats, the table) is left as
it is: writes go through the running Excel instance when the workbook is
open there (unsaved manual edits survive), otherwise through openpyxl with
an atomic save. The first modification of a workbook is preceded by a
backup copy; when that copy cannot be made nothing is written and the
results stay queued.

Queue. Results are queued in a JSON file and flushed by a worker; the queue
survives a restart and is reconciled against the sheet before writing, so a
row is never written twice. Technical details (windows, sensor identity,
recovery identifiers) stay in the session state and the manifests.
"""

from __future__ import annotations

import os
import re
import threading
from copy import copy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .cellular_protocol import ColumnMapping, ProtocolImport, _normalise_header, parse_direction_cell, parse_frequency_cell
from .excel_com import (
    BUSY,
    OPEN,
    XL_BY_ROWS,
    XL_FORMULAS,
    XL_PART,
    XL_PREVIOUS,
    _bind_open_workbook,
    _norm,
    com_busy_reason,
    excel_workbook_state,
)
from .excel_writer import ExcelBusyError, backup_workbook
from .json_store import read_json, write_json
from .recording_history import iq_group_key, sort_iq_filenames
from .xdat_header import is_xdat_sidecar

QUEUE_SCHEMA = 1
QUEUE_FILENAME = "workbook_queue.json"
DATETIME_FORMAT = "dd/mm/yyyy hh:mm:ss"
DURATION_FORMAT = "0.000"
# Written into the time cell when the sensor gave no timestamp for the files
# (neither CRFS metadata nor a UTC time in the file name): never a guess.
TIME_UNKNOWN = "unknown"
# Seconds of IQ are kept to the millisecond: finer digits are the byte model's noise.
DURATION_DIGITS = 3
_SENSOR_HEADERS = {
    "sensor", "sensor name", "sensor id", "receiver", "recorder", "device", "instrument", "unit id", "sensor serial",
}
_STEM_UTC = re.compile(r"(?:^|_)(\d{8})_(\d{6})(?:_|$)")
_EXCEL_EPOCH = datetime(1899, 12, 30)


class WorkbookError(RuntimeError):
    """The workbook cannot take the results as they are (sheet changed, rows missing)."""


class BackupError(WorkbookError):
    """The backup copy before the first modification could not be made."""


# ----------------------------------------------------------------------------- layout
def _band_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _direction_text(value: Any) -> str:
    """Direction as the import understands it ('DL', 'UL', shared), else the raw text."""
    direction, _issue = parse_direction_cell(value)
    return direction or _band_text(value)


@dataclass(frozen=True)
class RowKey:
    """What identifies a protocol row: the allocation it describes."""

    band: str
    direction: str
    duplex: str
    start_hz: int | None
    stop_hz: int | None

    def matches(self, other: "RowKey") -> bool:
        return (
            _band_text(self.band) == _band_text(other.band)
            and _direction_text(self.direction) == _direction_text(other.direction)
            and self.start_hz == other.start_hz
            and self.stop_hz == other.stop_hz
        )

    def label(self) -> str:
        parts = [self.band.strip(), self.direction.strip()]
        if self.start_hz is not None and self.stop_hz is not None:
            parts.append(f"{self.start_hz / 1e6:g}–{self.stop_hz / 1e6:g} MHz")
        return " ".join(p for p in parts if p)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RowKey":
        return cls(
            band=str(data.get("band") or ""),
            direction=str(data.get("direction") or ""),
            duplex=str(data.get("duplex") or ""),
            start_hz=int(data["start_hz"]) if data.get("start_hz") is not None else None,
            stop_hz=int(data["stop_hz"]) if data.get("stop_hz") is not None else None,
        )


@dataclass(frozen=True)
class PlanRow:
    """One protocol row of the stored import (block 0)."""

    index: int
    sheet_row: int
    key: RowKey

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "sheet_row": self.sheet_row, "key": self.key.to_dict()}

    @classmethod
    def from_dict(cls, data: dict) -> "PlanRow":
        return cls(index=int(data.get("index") or 0), sheet_row=int(data.get("sheet_row") or 0), key=RowKey.from_dict(data.get("key") or {}))


@dataclass(frozen=True)
class SheetLayout:
    """Column numbers (1-based) of the protocol worksheet's roles; None when absent."""

    worksheet: str
    header_row: int
    band: int | None = None
    direction: int | None = None
    duplex: int | None = None
    start: int | None = None
    stop: int | None = None
    sensor: int | None = None
    result: int | None = None
    duration: int | None = None
    time: int | None = None
    start_unit: str = "MHz"
    stop_unit: str = "MHz"
    last_column: int = 9

    @property
    def key_columns(self) -> list[int]:
        return [c for c in (self.band, self.direction, self.duplex, self.start, self.stop) if c]

    @property
    def result_columns(self) -> list[int]:
        return [c for c in (self.sensor, self.result, self.duration, self.time) if c]

    @property
    def marker_column(self) -> int | None:
        """The column that says whether a row holds a result: file names, else the time."""
        return self.result or self.time or self.duration

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "SheetLayout":
        fields = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__}
        fields["worksheet"] = str(fields.get("worksheet") or "")
        fields["header_row"] = int(fields.get("header_row") or 1)
        return cls(**fields)


def sensor_header(headers: Iterable[str], mapping: ColumnMapping | None) -> str | None:
    """The protocol's sensor column: an unmapped header such as 'Sensor'."""
    mapped = set(mapping.roles().values()) if mapping is not None else set()
    for header in headers:
        text = str(header or "").strip()
        if not text or text in mapped:
            continue
        key, _unit = _normalise_header(text)
        if key in _SENSOR_HEADERS:
            return text
    return None


def layout_from_import(imported: ProtocolImport) -> SheetLayout:
    """Where the roles live in the imported worksheet."""
    columns: dict[str, int] = {}
    for index, header in enumerate(imported.headers, start=1):
        text = str(header or "").strip()
        if text and text not in columns:
            columns[text] = index
    mapping = imported.mapping

    def col(header: str | None) -> int | None:
        return columns.get(header) if header else None

    sensor = sensor_header(imported.headers, mapping)
    return SheetLayout(
        worksheet=imported.worksheet,
        header_row=max(int(imported.header_row or 1), 1),
        band=col(mapping.band) or col(mapping.nominal),
        direction=col(mapping.direction),
        duplex=col(mapping.duplex),
        start=col(mapping.start),
        stop=col(mapping.stop),
        sensor=col(sensor),
        result=col(mapping.result),
        duration=col(mapping.duration),
        time=col(mapping.result_time),
        start_unit=mapping.header_units.get("start") or mapping.default_unit or "MHz",
        stop_unit=mapping.header_units.get("stop") or mapping.default_unit or "MHz",
        last_column=max(columns.values()) if columns else 1,
    )


def plan_rows_from_import(imported: ProtocolImport) -> list[PlanRow]:
    """Block 0: the rows that are recorded, in worksheet order."""
    rows: list[PlanRow] = []
    for entry in sorted(imported.valid_entries(), key=lambda e: e.row):
        rows.append(PlanRow(
            index=len(rows),
            sheet_row=int(entry.row),
            key=RowKey(entry.band, entry.direction, entry.duplex, entry.start_hz, entry.stop_hz),
        ))
    return rows


# ----------------------------------------------------------------------------- results
@dataclass
class RowResult:
    """What the collection writes into one protocol row for one round."""

    round_id: str
    plan_index: int
    files: list[str] = field(default_factory=list)
    duration_s: float | None = None
    recorded_at: datetime | None = None
    sensor: str = ""
    complete: bool = True
    # Recovery identifiers; never written to the sheet.
    task_keys: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "plan_index": self.plan_index,
            "files": list(self.files),
            "duration_s": self.duration_s,
            "recorded_at": self.recorded_at.isoformat() if self.recorded_at else None,
            "sensor": self.sensor,
            "complete": self.complete,
            "task_keys": list(self.task_keys),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RowResult":
        when = data.get("recorded_at")
        try:
            recorded = datetime.fromisoformat(str(when)) if when else None
        except ValueError:
            recorded = None
        duration = data.get("duration_s")
        return cls(
            round_id=str(data.get("round_id") or ""),
            plan_index=int(data.get("plan_index") or 0),
            files=[str(f) for f in data.get("files") or []],
            duration_s=float(duration) if duration is not None else None,
            recorded_at=recorded,
            sensor=str(data.get("sensor") or ""),
            complete=bool(data.get("complete", True)),
            task_keys=[str(k) for k in data.get("task_keys") or []],
        )


def capture_start_from_stem(stem: str) -> datetime | None:
    """Local wall-clock start of a capture from the sensor's file stem (UTC time in the name)."""
    match = _STEM_UTC.search(stem or "")
    if not match:
        return None
    try:
        utc = datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return utc.astimezone().replace(tzinfo=None)


def local_naive(value: datetime | str | None) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if value.tzinfo is not None:
        value = value.astimezone().replace(tzinfo=None)
    return value.replace(microsecond=0)


def order_files(names: Iterable[str]) -> list[str]:
    """Distinct file names in capture order: by recording (stem), then by part number."""
    distinct: list[str] = []
    seen: set[str] = set()
    for name in names:
        text = str(name or "").strip()
        if text and text not in seen:
            seen.add(text)
            distinct.append(text)
    groups: dict[str, list[str]] = {}
    for name in distinct:
        groups.setdefault(iq_group_key(name), []).append(name)
    ordered: list[str] = []
    for key in sorted(groups):
        ordered.extend(sort_iq_filenames(groups[key]))
    return ordered


def build_row_result(round_id: str, plan_index: int, tasks: list[dict[str, Any]], *, sensor: str) -> RowResult:
    """One row's result from the session's task dictionaries (every request of the row).

    Files: every part of every recording of the row that has files, in the
    sensor's order. Duration: the seconds of IQ read from those files' WAVE
    headers, added over the row's recordings (the workbook's Repeat column
    may send the same request more than once); unknown (empty) when any of
    them could not be measured - never the requested time. Start: the
    earliest start the sensor wrote into the files (CRFS metadata, else the
    UTC time in the file name); unknown when neither is available.
    """
    files: list[str] = []
    seconds = 0.0
    unknown = False
    starts: list[datetime] = []
    keys: list[str] = []
    confirmed = 0
    for task in tasks:
        keys.append(str(task.get("task_key") or ""))
        if str(task.get("status") or "") != "confirmed":
            continue
        confirmed += 1
        summary = task.get("summary") or {}
        # The sample files only; XDAT header/marker sidecars are kept beside
        # them in the same folder and would otherwise appear or not depending
        # on when the sensor wrote them.
        names = [str(f.get("name") or "") for f in (task.get("files") or []) if f.get("name") and not is_xdat_sidecar(str(f.get("name")))]
        files.extend(names)
        held = summary.get("held_s")
        if held is None:
            unknown = True
        else:
            seconds += float(held)
        start = _metadata_start(task) or capture_start_from_stem(str(summary.get("stem") or ""))
        if start is not None:
            starts.append(start)
    return RowResult(
        round_id=round_id,
        plan_index=plan_index,
        files=order_files(files),
        duration_s=round(seconds, DURATION_DIGITS) if confirmed and not unknown and seconds > 0 else None,
        recorded_at=min(starts) if starts else None,
        sensor=sensor,
        complete=bool(tasks) and confirmed == len(tasks),
        task_keys=keys,
    )


def _metadata_start(task: dict[str, Any]) -> datetime | None:
    """Earliest signal time in the files' CRFS metadata, as local wall-clock time."""
    values: list[datetime] = []
    candidates = [f.get("signal_time") for f in task.get("files") or []]
    measurement = (task.get("summary") or {}).get("measurement") or {}
    candidates += [p.get("signal_time") for p in measurement.get("parts") or [] if isinstance(p, dict)]
    for value in candidates:
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        values.append(parsed.astimezone().replace(tzinfo=None, microsecond=0))
    return min(values) if values else None


# ----------------------------------------------------------------------------- sheet adapters
def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _as_naive(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        return value.replace(microsecond=0)
    if isinstance(value, (int, float)) and value > 20000:
        # An Excel serial date read back from a cell.
        try:
            return (_EXCEL_EPOCH + timedelta(days=float(value))).replace(microsecond=0)
        except (OverflowError, ValueError):
            return None
    return None


def _split_names(value: Any) -> list[str]:
    text = _text(value)
    return [part.strip() for part in re.split(r"[\r\n;,]+", text) if part.strip()] if text else []


def _row_height(lines: int, current: float | None) -> float:
    return max(18.0, 16.0 * max(1, lines) + 4.0, float(current or 0.0))


class _OpenpyxlSheet:
    """Cell access on an openpyxl worksheet."""

    def __init__(self, ws) -> None:
        self.ws = ws

    def value(self, row: int, col: int) -> Any:
        return self.ws.cell(row=row, column=col).value

    def set_value(self, row: int, col: int, value: Any, *, number_format: str | None = None, wrap: bool = False) -> None:
        cell = self.ws.cell(row=row, column=col)
        cell.value = value
        if number_format:
            cell.number_format = number_format
        if wrap:
            alignment = copy(cell.alignment)
            alignment.wrap_text = True
            alignment.vertical = "top"
            cell.alignment = alignment

    def copy_cells(self, src_row: int, dst_row: int, first_col: int, last_col: int, *, values: bool) -> None:
        for col in range(first_col, last_col + 1):
            src = self.ws.cell(row=src_row, column=col)
            dst = self.ws.cell(row=dst_row, column=col)
            if values:
                dst.value = src.value
            if src.has_style:
                dst.font = copy(src.font)
                dst.fill = copy(src.fill)
                dst.border = copy(src.border)
                dst.alignment = copy(src.alignment)
                dst.number_format = src.number_format
                dst.protection = copy(src.protection)

    def clear(self, row: int, col: int) -> None:
        self.ws.cell(row=row, column=col).value = None

    def set_row_height(self, row: int, lines: int) -> None:
        dims = self.ws.row_dimensions[row]
        dims.height = _row_height(lines, dims.height)

    def last_used_row(self, columns: Iterable[int]) -> int:
        cols = [c for c in columns if c] or [1]
        last = 0
        for row in self.ws.iter_rows(min_row=1, max_row=self.ws.max_row or 1, min_col=min(cols), max_col=max(cols)):
            for cell in row:
                if cell.column in cols and cell.value not in (None, ""):
                    last = max(last, cell.row)
        return last

    def resize_table(self, header_row: int, last_row: int, last_col: int) -> None:
        from openpyxl.utils import get_column_letter

        for table in list(self.ws.tables.values()):
            ref = str(getattr(table, "ref", "") or "")
            if not ref or not ref.split(":")[0].rstrip("0123456789").isalpha():
                continue
            first = ref.split(":")[0]
            first_row = int(re.sub(r"[A-Z]+", "", first) or 1)
            if first_row != header_row:
                continue
            first_col = first.rstrip("0123456789")
            new_ref = f"{first_col}{header_row}:{get_column_letter(last_col)}{max(last_row, header_row + 1)}"
            table.ref = new_ref
            auto = getattr(table, "autoFilter", None)
            if auto is not None:
                auto.ref = new_ref


class _ComSheet:
    """Cell access on a worksheet of a running Excel instance."""

    def __init__(self, sheet) -> None:
        self.sheet = sheet

    def value(self, row: int, col: int) -> Any:
        return self.sheet.Cells(row, col).Value

    def set_value(self, row: int, col: int, value: Any, *, number_format: str | None = None, wrap: bool = False) -> None:
        cell = self.sheet.Cells(row, col)
        if number_format:
            cell.NumberFormat = number_format
        if isinstance(value, datetime):
            cell.Value2 = (value - _EXCEL_EPOCH).total_seconds() / 86400.0
        else:
            cell.Value = value
        if wrap:
            try:
                cell.WrapText = True
                cell.VerticalAlignment = -4160  # xlTop
            except Exception:  # noqa: BLE001 - presentation only
                pass

    def copy_cells(self, src_row: int, dst_row: int, first_col: int, last_col: int, *, values: bool) -> None:
        source = self.sheet.Range(self.sheet.Cells(src_row, first_col), self.sheet.Cells(src_row, last_col))
        target = self.sheet.Range(self.sheet.Cells(dst_row, first_col), self.sheet.Cells(dst_row, last_col))
        if values:
            # Copy(Destination) carries values and formats without the clipboard.
            source.Copy(target)
        else:
            source.Copy()
            target.PasteSpecial(Paste=-4122)  # xlPasteFormats
            try:
                self.sheet.Application.CutCopyMode = False
            except Exception:  # noqa: BLE001
                pass

    def clear(self, row: int, col: int) -> None:
        self.sheet.Cells(row, col).ClearContents()

    def set_row_height(self, row: int, lines: int) -> None:
        try:
            current = float(self.sheet.Rows(row).RowHeight or 0)
        except Exception:  # noqa: BLE001
            current = 0.0
        self.sheet.Rows(row).RowHeight = _row_height(lines, current)

    def last_used_row(self, columns: Iterable[int]) -> int:
        try:
            found = self.sheet.Cells.Find(
                What="*", After=self.sheet.Cells(1, 1), LookIn=XL_FORMULAS, LookAt=XL_PART,
                SearchOrder=XL_BY_ROWS, SearchDirection=XL_PREVIOUS, MatchCase=False,
            )
            if found is not None:
                return max(1, int(found.Row))
        except Exception:  # noqa: BLE001
            pass
        return max(1, int(self.sheet.UsedRange.Rows.Count))

    def resize_table(self, header_row: int, last_row: int, last_col: int) -> None:
        try:
            tables = self.sheet.ListObjects
            count = int(tables.Count)
        except Exception:  # noqa: BLE001
            return
        for index in range(1, count + 1):
            table = tables(index)
            try:
                first_row = int(table.Range.Row)
                first_col = int(table.Range.Column)
            except Exception:  # noqa: BLE001
                continue
            if first_row != header_row:
                continue
            table.Resize(self.sheet.Range(self.sheet.Cells(header_row, first_col), self.sheet.Cells(max(last_row, header_row + 1), last_col)))


# ----------------------------------------------------------------------------- blocks
@dataclass
class BlockRef:
    """Where one round's rows are in the sheet."""

    round_id: str
    ordinal: int  # 0 = the plan rows in place; 1, 2, ... = appended blocks in order
    first_row: int  # first data row of the block (ordinal 0: the first plan row)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BlockRef":
        return cls(round_id=str(data.get("round_id") or ""), ordinal=int(data.get("ordinal") or 0), first_row=int(data.get("first_row") or 0))


def _row_key(sheet, layout: SheetLayout, row: int) -> RowKey:
    def freq(col: int | None, unit: str) -> int | None:
        if not col:
            return None
        value = sheet.value(row, col)
        if value in (None, ""):
            return None
        hz, _issue = parse_frequency_cell(value, unit_hint=unit, field_name="frequency")
        return hz

    return RowKey(
        band=_text(sheet.value(row, layout.band)) if layout.band else "",
        direction=_text(sheet.value(row, layout.direction)) if layout.direction else "",
        duplex=_text(sheet.value(row, layout.duplex)) if layout.duplex else "",
        start_hz=freq(layout.start, layout.start_unit),
        stop_hz=freq(layout.stop, layout.stop_unit),
    )


def _block_rows(block: BlockRef, plan: list[PlanRow]) -> list[int]:
    if block.ordinal == 0:
        return [row.sheet_row for row in plan]
    return [block.first_row + row.index for row in plan]


def _block_matches(sheet, layout: SheetLayout, plan: list[PlanRow], rows: list[int]) -> bool:
    return all(_row_key(sheet, layout, row).matches(plan_row.key) for plan_row, row in zip(plan, rows))


def _appended_blocks(sheet, layout: SheetLayout, plan: list[PlanRow]) -> list[int]:
    """First rows of every appended block (a run of rows whose keys repeat the plan), in order."""
    if not plan:
        return []
    last = sheet.last_used_row(layout.key_columns + layout.result_columns)
    after = max(row.sheet_row for row in plan)
    found: list[int] = []
    row = after + 1
    while row + len(plan) - 1 <= last:
        if _block_matches(sheet, layout, plan, [row + r.index for r in plan]):
            found.append(row)
            row += len(plan)
        else:
            row += 1
    return found


def _has_results(sheet, layout: SheetLayout, rows: list[int]) -> bool:
    marker = layout.marker_column
    if not marker:
        return False
    return any(sheet.value(row, marker) not in (None, "") for row in rows)


def locate_block(sheet, layout: SheetLayout, plan: list[PlanRow], block: BlockRef) -> list[int] | None:
    """The block's rows, re-found when rows moved; None when they are gone."""
    rows = _block_rows(block, plan)
    if _block_matches(sheet, layout, plan, rows):
        return rows
    if block.ordinal == 0:
        return None
    appended = _appended_blocks(sheet, layout, plan)
    if len(appended) >= block.ordinal:
        block.first_row = appended[block.ordinal - 1]
        return _block_rows(block, plan)
    return None


def allocate_block(sheet, layout: SheetLayout, plan: list[PlanRow], round_id: str, taken: list[BlockRef]) -> BlockRef:
    """Choose where a round's results go: the empty plan rows, else a new block below everything."""
    plan_rows = [row.sheet_row for row in plan]
    in_place_taken = any(b.ordinal == 0 for b in taken)
    if not in_place_taken and _block_matches(sheet, layout, plan, plan_rows) and not _has_results(sheet, layout, plan_rows):
        return BlockRef(round_id=round_id, ordinal=0, first_row=plan_rows[0] if plan_rows else layout.header_row + 1)
    existing = _appended_blocks(sheet, layout, plan)
    first = sheet.last_used_row(layout.key_columns + layout.result_columns) + 1
    block = BlockRef(round_id=round_id, ordinal=len(existing) + 1, first_row=first)
    for plan_row in plan:
        dst = first + plan_row.index
        # Key cells with their values and formats; the result cells with their formats only.
        sheet.copy_cells(plan_row.sheet_row, dst, 1, layout.last_column, values=True)
        for col in layout.result_columns:
            if col != layout.sensor:
                sheet.clear(dst, col)
    return block


# ----------------------------------------------------------------------------- writing
@dataclass
class WriteReport:
    written: list[tuple[str, int, int]] = field(default_factory=list)  # (round_id, plan_index, sheet row)
    unchanged: int = 0
    skipped: list[str] = field(default_factory=list)
    skipped_keys: list[tuple[str, int]] = field(default_factory=list)  # (round_id, plan_index) not written
    conflicts: list[tuple[str, int]] = field(default_factory=list)  # ... because the row holds other entries
    blocks: dict[str, BlockRef] = field(default_factory=dict)
    last_row: int = 0


def _write_row(sheet, layout: SheetLayout, row: int, result: RowResult) -> tuple[bool, str | None]:
    """Write one row's result cells; (changed, problem).

    Entries the application did not write are never overwritten: a row that
    lists other recording files, or that has no file but a duration or time
    typed in, is left as it is and reported.
    """
    changed = False
    if layout.result:
        existing = _split_names(sheet.value(row, layout.result))
        wanted = list(result.files)
        if existing and set(existing) - set(wanted):
            return False, f"row {row} already lists other recording files ({', '.join(existing[:2])}…); left unchanged"
        if not existing and any(col and _text(sheet.value(row, col)) for col in (layout.duration, layout.time)):
            return False, f"row {row} already holds a duration or time entered by hand; left unchanged"
        if wanted and existing != wanted:
            sheet.set_value(row, layout.result, "\n".join(wanted), wrap=True)
            sheet.set_row_height(row, len(wanted))
            changed = True
    if layout.sensor and result.sensor:
        if _text(sheet.value(row, layout.sensor)) != result.sensor:
            sheet.set_value(row, layout.sensor, result.sensor)
            changed = True
    if layout.duration and result.duration_s is not None:
        current = sheet.value(row, layout.duration)
        try:
            same = current is not None and abs(float(current) - float(result.duration_s)) < 0.0005
        except (TypeError, ValueError):
            same = False
        if not same:
            sheet.set_value(row, layout.duration, float(result.duration_s), number_format=DURATION_FORMAT)
            changed = True
    if layout.time and result.recorded_at is not None:
        wanted_dt = result.recorded_at.replace(microsecond=0)
        if _as_naive(sheet.value(row, layout.time)) != wanted_dt:
            sheet.set_value(row, layout.time, wanted_dt, number_format=DATETIME_FORMAT)
            changed = True
    elif layout.time and result.files and result.recorded_at is None:
        # Files exist but the sensor gave no start time: say so in the cell
        # rather than leave it empty (an empty cell reads as "not yet written").
        # A later reconciliation that finds the time replaces the word.
        current = sheet.value(row, layout.time)
        if current in (None, "") and _text(current) != TIME_UNKNOWN:
            sheet.set_value(row, layout.time, TIME_UNKNOWN)
            changed = True
    return changed, None


def apply_results(
    sheet,
    *,
    layout: SheetLayout,
    plan: list[PlanRow],
    results: list[RowResult],
    blocks: dict[str, BlockRef],
) -> WriteReport:
    """Reconcile and write every result into the sheet (no save here)."""
    report = WriteReport(blocks=dict(blocks))
    if not plan:
        report.skipped.extend(f"{r.round_id}: the plan has no rows" for r in results)
        return report
    by_round: dict[str, list[RowResult]] = {}
    for result in results:
        by_round.setdefault(result.round_id, []).append(result)
    for round_id, items in by_round.items():
        block = report.blocks.get(round_id)
        rows: list[int] | None = None
        if block is not None:
            rows = locate_block(sheet, layout, plan, block)
            if rows is None:
                report.skipped.extend(
                    f"{round_id}: the rows of this round are no longer where they were in {layout.worksheet!r}; "
                    f"row {r.plan_index + 1} not written" for r in items
                )
                report.skipped_keys.extend((round_id, r.plan_index) for r in items)
                continue
        else:
            block = allocate_block(sheet, layout, plan, round_id, list(report.blocks.values()))
            report.blocks[round_id] = block
            rows = _block_rows(block, plan)
        for result in items:
            if not 0 <= result.plan_index < len(plan):
                report.skipped.append(f"{round_id}: plan row {result.plan_index + 1} does not exist")
                report.skipped_keys.append((round_id, result.plan_index))
                continue
            row = rows[result.plan_index]
            if not _row_key(sheet, layout, row).matches(plan[result.plan_index].key):
                report.skipped.append(f"{round_id}: row {row} does not hold {plan[result.plan_index].key.label()}; not written")
                report.skipped_keys.append((round_id, result.plan_index))
                continue
            changed, problem = _write_row(sheet, layout, row, result)
            if problem:
                report.skipped.append(f"{round_id}: {problem}")
                report.skipped_keys.append((round_id, result.plan_index))
                report.conflicts.append((round_id, result.plan_index))
                continue
            if changed:
                report.written.append((round_id, result.plan_index, row))
            else:
                report.unchanged += 1
        report.last_row = max(report.last_row, max(rows))
    if report.written or any(b.ordinal > 0 for b in report.blocks.values()):
        last = sheet.last_used_row(layout.key_columns + layout.result_columns)
        sheet.resize_table(layout.header_row, max(last, report.last_row), layout.last_column)
    return report


def write_results_openpyxl(path: Path, *, layout: SheetLayout, plan: list[PlanRow], results: list[RowResult], blocks: dict[str, BlockRef]) -> WriteReport:
    """Write through openpyxl with an atomic save; raises ExcelBusyError when the file is locked."""
    from openpyxl import load_workbook

    file = Path(path)
    keep_vba = file.suffix.casefold() == ".xlsm"
    try:
        workbook = load_workbook(file, keep_vba=keep_vba)
    except PermissionError as exc:
        raise ExcelBusyError(f"Could not open {file.name}: {_busy_reason(exc)}") from exc
    try:
        if layout.worksheet not in workbook.sheetnames:
            raise WorkbookError(f"Worksheet {layout.worksheet!r} is not in {file.name}.")
        report = apply_results(_OpenpyxlSheet(workbook[layout.worksheet]), layout=layout, plan=plan, results=results, blocks=blocks)
        if report.written or report.blocks != blocks:
            temp = file.with_name(f"{file.stem}.crfs-tmp-{os.getpid()}{file.suffix}")
            try:
                workbook.save(temp)
                os.replace(temp, file)
            except PermissionError as exc:
                raise ExcelBusyError(f"Could not save {file.name}: {_busy_reason(exc)}") from exc
            finally:
                try:
                    if temp.exists():
                        temp.unlink()
                except OSError:
                    pass
        return report
    finally:
        vba = getattr(workbook, "vba_archive", None)
        if vba is not None:
            try:
                vba.close()
            except Exception:  # noqa: BLE001
                pass
        workbook.close()


def write_results_com(path: Path, *, layout: SheetLayout, plan: list[PlanRow], results: list[RowResult], blocks: dict[str, BlockRef]) -> WriteReport:
    """Write into the workbook as it is open in the running Excel instance, then save it there."""
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
    except ImportError as exc:
        raise ExcelBusyError("Excel COM is not available on this PC.") from exc
    pythoncom.CoInitialize()
    try:
        try:
            app, book = _bind_open_workbook(win32com.client, Path(path))
        except Exception as exc:  # noqa: BLE001
            reason = com_busy_reason(exc)
            if reason:
                raise ExcelBusyError(f"{reason}; {Path(path).name} is written when Excel is free.") from exc
            from .diagnostic_log import diag

            diag("excel_bind_failed", Path(path).name, type=type(exc).__name__, detail=str(exc)[:300])
            raise ExcelBusyError(f"Excel could not be reached for {Path(path).name}") from exc
        if book is None:
            raise ExcelBusyError(f"{Path(path).name} is not open in Excel any more.")
        try:
            read_only = bool(book.ReadOnly)
        except Exception as exc:  # noqa: BLE001
            raise ExcelBusyError(f"{com_busy_reason(exc) or 'Excel refused the call'}; {Path(path).name} is written when Excel is free.") from exc
        if read_only:
            raise ExcelBusyError(
                f"{Path(path).name} is open read-only in Excel; the results stay queued until it is opened for editing or closed."
            )
        try:
            sheet = book.Worksheets(layout.worksheet)
        except Exception as exc:  # noqa: BLE001
            if com_busy_reason(exc):
                raise ExcelBusyError(f"{com_busy_reason(exc)}; {Path(path).name} is written when Excel is free.") from exc
            raise WorkbookError(f"Worksheet {layout.worksheet!r} is not in {Path(path).name}.") from exc
        try:
            alerts = app.DisplayAlerts
            app.DisplayAlerts = False
        except Exception as exc:  # noqa: BLE001 - the first property call is where a busy Excel refuses
            raise ExcelBusyError(f"{com_busy_reason(exc) or 'Excel refused the call'}; {Path(path).name} is written when Excel is free.") from exc
        try:
            try:
                report = apply_results(_ComSheet(sheet), layout=layout, plan=plan, results=results, blocks=blocks)
            except Exception as exc:  # noqa: BLE001
                reason = com_busy_reason(exc)
                if reason:
                    # Cells written so far are in the open workbook; the next
                    # flush finds them unchanged and only saves. Nothing is
                    # marked done until Excel confirms the save.
                    raise ExcelBusyError(f"{reason}; the results stay queued and are written when Excel is free.") from exc
                raise
            # Save when something was written now, or when an earlier flush
            # wrote cells that Excel has not confirmed as saved yet.
            if report.written or report.blocks != blocks or not _book_saved(book):
                try:
                    book.Save()
                except Exception as exc:  # noqa: BLE001 - the save is what counts as "updated"
                    reason = com_busy_reason(exc) or f"Excel could not save it ({exc})"
                    raise ExcelBusyError(f"{Path(path).name}: written into the open workbook, but {reason}; saved again when Excel is free.") from exc
                if not _book_saved(book):
                    raise ExcelBusyError(f"{Path(path).name}: Excel did not confirm the save (Workbook.Saved is false); tried again shortly.")
        finally:
            try:
                app.DisplayAlerts = alerts
            except Exception:  # noqa: BLE001
                pass
        return report
    finally:
        import gc

        gc.collect()
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def _book_saved(book) -> bool:
    """Excel's own word that the workbook has no unsaved changes (Workbook.Saved)."""
    try:
        return bool(book.Saved)
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------------------- queue
@dataclass
class PendingItem:
    workbook: str
    layout: SheetLayout
    plan: list[PlanRow]
    result: RowResult
    queued_at: str = ""
    attempts: int = 0
    last_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "workbook": self.workbook,
            "layout": self.layout.to_dict(),
            "plan": [row.to_dict() for row in self.plan],
            "result": self.result.to_dict(),
            "queued_at": self.queued_at,
            "attempts": self.attempts,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PendingItem":
        return cls(
            workbook=str(data.get("workbook") or ""),
            layout=SheetLayout.from_dict(data.get("layout") or {}),
            plan=[PlanRow.from_dict(item) for item in data.get("plan") or []],
            result=RowResult.from_dict(data.get("result") or {}),
            queued_at=str(data.get("queued_at") or ""),
            attempts=int(data.get("attempts") or 0),
            last_error=str(data.get("last_error") or ""),
        )


@dataclass
class FlushReport:
    written: int = 0
    unchanged: int = 0
    pending: int = 0
    busy: str = ""
    error: str = ""
    backups: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    blocks_created: list[tuple[str, int, int]] = field(default_factory=list)  # (round_id, ordinal, first_row)
    workbooks: list[str] = field(default_factory=list)  # workbooks that received rows in this flush

    @property
    def ok(self) -> bool:
        return not self.error


class WorkbookQueue:
    """Persistent queue of row results waiting to reach the protocol workbook."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        data = read_json(self.path)
        if not isinstance(data, dict):
            data = {}
        data.setdefault("schema", QUEUE_SCHEMA)
        data.setdefault("pending", [])
        data.setdefault("blocks", {})
        data.setdefault("backups", {})
        data.setdefault("written", [])
        data.setdefault("conflicts", [])
        return data

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json(self.path, self._data)

    # ---- queries
    def pending_count(self) -> int:
        with self._lock:
            return len(self._data["pending"])

    def pending_items(self) -> list[PendingItem]:
        with self._lock:
            return [PendingItem.from_dict(item) for item in self._data["pending"]]

    def blocks_for(self, workbook: Path) -> dict[str, BlockRef]:
        with self._lock:
            return {rid: BlockRef.from_dict(b) for rid, b in (self._data["blocks"].get(_norm(Path(workbook))) or {}).items()}

    def block_for_round(self, workbook: Path, round_id: str) -> BlockRef | None:
        return self.blocks_for(workbook).get(round_id)

    def backup_for(self, workbook: Path) -> str:
        with self._lock:
            return str((self._data["backups"].get(_norm(Path(workbook))) or {}).get("path") or "")

    def was_written(self, round_id: str, plan_index: int) -> bool:
        with self._lock:
            return any(w.get("round_id") == round_id and int(w.get("plan_index", -1)) == plan_index for w in self._data["written"])

    def written_workbook(self) -> str:
        """Path of the workbook that most recently received results ('' when none has).

        This is the file the rows went into, kept with every written row, so it
        stays right when the operator later picks another protocol workbook.
        """
        with self._lock:
            for entry in reversed(self._data["written"]):
                path = str(entry.get("workbook") or "")
                if path:
                    return path
        return ""

    def written_workbooks(self) -> list[str]:
        """Every workbook that received results, most recent first, without repeats."""
        out: list[str] = []
        with self._lock:
            for entry in reversed(self._data["written"]):
                path = str(entry.get("workbook") or "")
                if path and _norm(Path(path)) not in {_norm(Path(p)) for p in out}:
                    out.append(path)
        return out

    def row_state(self, round_id: str, plan_index: int) -> str:
        """'written', 'pending', 'retrying' (a write failed), 'conflict' (row holds other entries) or ''."""
        with self._lock:
            for item in self._data["pending"]:
                got = item.get("result") or {}
                if got.get("round_id") == round_id and int(got.get("plan_index", -1)) == plan_index:
                    return "retrying" if item.get("last_error") else "pending"
            if any(c.get("round_id") == round_id and int(c.get("plan_index", -1)) == plan_index for c in self._data.get("conflicts") or []):
                return "conflict"
        return "written" if self.was_written(round_id, plan_index) else ""

    # ---- changes
    def enqueue(self, workbook: Path, layout: SheetLayout, plan: list[PlanRow], result: RowResult) -> bool:
        """Queue a row result; a newer result for the same round and row replaces the older one."""
        with self._lock:
            item = PendingItem(workbook=str(Path(workbook)), layout=layout, plan=plan, result=result, queued_at=datetime.now().isoformat(timespec="seconds"))
            pending = self._data["pending"]
            replaced = False
            for index, existing in enumerate(pending):
                got = existing.get("result") or {}
                if got.get("round_id") == result.round_id and int(got.get("plan_index", -1)) == result.plan_index and _norm(Path(existing.get("workbook") or "")) == _norm(Path(workbook)):
                    pending[index] = item.to_dict()
                    replaced = True
                    break
            if not replaced:
                pending.append(item.to_dict())
            self._save()
            return True

    def record_blocks(self, workbook: Path, blocks: dict[str, BlockRef]) -> None:
        with self._lock:
            self._data["blocks"][_norm(Path(workbook))] = {rid: b.to_dict() for rid, b in blocks.items()}
            self._save()

    def record_backup(self, workbook: Path, backup: Path) -> None:
        with self._lock:
            self._data["backups"][_norm(Path(workbook))] = {"path": str(backup), "at": datetime.now().isoformat(timespec="seconds")}
            self._save()

    def forget_backup(self, workbook: Path) -> None:
        """A new collection session starts: its first change gets a fresh backup copy."""
        with self._lock:
            if self._data["backups"].pop(_norm(Path(workbook)), None) is not None:
                self._save()

    def mark_done(self, items: list[PendingItem], *, written_rows: list[tuple[str, int, int]]) -> None:
        with self._lock:
            keys = {(_norm(Path(i.workbook)), i.result.round_id, i.result.plan_index) for i in items}
            self._data["pending"] = [
                p for p in self._data["pending"]
                if (_norm(Path(p.get("workbook") or "")), (p.get("result") or {}).get("round_id"), int((p.get("result") or {}).get("plan_index", -1))) not in keys
            ]
            rows = {(round_id, plan_index): row for round_id, plan_index, row in written_rows}
            now = datetime.now().isoformat(timespec="seconds")
            for item in items:
                # A result already in the sheet unchanged counts as written too (row 0: not changed now).
                key = (item.result.round_id, item.result.plan_index)
                self._data["written"].append(
                    {"round_id": key[0], "plan_index": key[1], "row": rows.get(key, 0), "at": now, "workbook": str(item.workbook)}
                )
            self._data["conflicts"] = [
                c for c in self._data.get("conflicts") or []
                if (c.get("round_id"), int(c.get("plan_index", -1))) not in {(i.result.round_id, i.result.plan_index) for i in items}
            ]
            self._data["written"] = self._data["written"][-5000:]
            self._save()

    def mark_conflict(self, items: list[PendingItem], reason: str) -> None:
        """Rows holding entries the application did not write: reported once, not retried."""
        with self._lock:
            keys = {(_norm(Path(i.workbook)), i.result.round_id, i.result.plan_index) for i in items}
            self._data["pending"] = [
                p for p in self._data["pending"]
                if (_norm(Path(p.get("workbook") or "")), (p.get("result") or {}).get("round_id"), int((p.get("result") or {}).get("plan_index", -1))) not in keys
            ]
            conflicts = self._data.setdefault("conflicts", [])
            now = datetime.now().isoformat(timespec="seconds")
            for item in items:
                conflicts.append({"round_id": item.result.round_id, "plan_index": item.result.plan_index, "reason": reason[:300], "at": now})
            self._data["conflicts"] = conflicts[-2000:]
            self._save()

    def mark_failed(self, items: list[PendingItem], error: str) -> None:
        with self._lock:
            keys = {(_norm(Path(i.workbook)), i.result.round_id, i.result.plan_index) for i in items}
            for p in self._data["pending"]:
                key = (_norm(Path(p.get("workbook") or "")), (p.get("result") or {}).get("round_id"), int((p.get("result") or {}).get("plan_index", -1)))
                if key in keys:
                    p["attempts"] = int(p.get("attempts") or 0) + 1
                    p["last_error"] = error[:300]
            self._save()

    def drop_round(self, round_id: str) -> int:
        with self._lock:
            before = len(self._data["pending"])
            self._data["pending"] = [p for p in self._data["pending"] if (p.get("result") or {}).get("round_id") != round_id]
            self._save()
            return before - len(self._data["pending"])


_LOCK_WINERRORS = {32, 33}  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION


def _file_locked(exc: OSError) -> bool:
    return isinstance(exc, PermissionError) or getattr(exc, "winerror", None) in _LOCK_WINERRORS


def _busy_reason(exc: OSError) -> str:
    """One readable sentence for a lock or permission error (no temp-file paths in the log)."""
    code = getattr(exc, "winerror", None)
    if code in _LOCK_WINERRORS or code == 5:  # sharing/lock violation, access denied
        return "another program has the file open (it is written when the file is free)"
    return exc.strerror or exc.__class__.__name__


def ensure_backup(queue: WorkbookQueue, workbook: Path) -> Path | None:
    """Copy the workbook once before its first modification; None when a copy already exists."""
    existing = queue.backup_for(workbook)
    if existing and Path(existing).is_file():
        return None
    file = Path(workbook)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = file.with_name(f"{file.stem}.crfs-backup-{stamp}{file.suffix}")
    serial = 1
    while target.exists():  # a second collection within the same second never overwrites a backup
        serial += 1
        target = file.with_name(f"{file.stem}.crfs-backup-{stamp}-{serial}{file.suffix}")
    try:
        backup_workbook(file, target)
    except OSError as exc:
        if _file_locked(exc):
            # Another program holds the file (lock violation / sharing violation): a busy
            # state, retried; not a backup failure.
            raise ExcelBusyError(f"Could not read {file.name} for its backup copy: {exc}") from exc
        raise BackupError(f"Backup of {file.name} could not be written ({exc}); nothing was changed and the results stay queued.") from exc
    if not target.is_file() or target.stat().st_size == 0:
        raise BackupError(f"Backup of {file.name} is missing or empty; nothing was changed and the results stay queued.")
    queue.record_backup(workbook, target)
    return target


def _open_state(excel_open: Callable[[Path], Any], file: Path) -> str:
    """'open' / 'closed' / 'busy' from either a tri-state or a boolean probe."""
    state = excel_open(file)
    if isinstance(state, str):
        return state
    return OPEN if state else "closed"


def flush_queue(queue: WorkbookQueue, *, excel_open: Callable[[Path], Any] | None = None) -> FlushReport:
    """Write every pending result. Runs on a worker; never raises for a busy or missing workbook.

    The workbook is written through the running Excel when it is open there,
    through openpyxl when it is closed, and not at all while Excel cannot be
    asked which it is (a busy instance): a file open in an Excel that is in
    edit mode must never be rewritten on disk behind it.
    """
    report = FlushReport()
    items = queue.pending_items()
    if not items:
        return report
    if excel_open is None:
        # Resolved at call time so a test can replace the probe on the module.
        from . import excel_com

        excel_open = excel_com.excel_workbook_state
    by_book: dict[str, list[PendingItem]] = {}
    for item in items:
        by_book.setdefault(_norm(Path(item.workbook)), []).append(item)
    for _key, group in by_book.items():
        file = Path(group[0].workbook)
        if not file.is_file():
            report.error = f"Protocol workbook not found: {file}. {len(group)} result(s) stay queued."
            queue.mark_failed(group, report.error)
            continue
        try:
            state = _open_state(excel_open, file)
            if state == BUSY:
                raise ExcelBusyError(f"Excel is busy (a cell is being edited or a dialog is open); {file.name} is written when it is free.")
            backup = ensure_backup(queue, file)
            if backup is not None:
                report.backups.append(str(backup))
            layout = group[0].layout
            plan = group[0].plan
            results = [item.result for item in group if item.layout.worksheet == layout.worksheet]
            blocks = queue.blocks_for(file)
            before = {rid: b.to_dict() for rid, b in blocks.items()}
            if state == OPEN:
                written = write_results_com(file, layout=layout, plan=plan, results=results, blocks=blocks)
                report.notes.append(f"written through the open Excel window into {file.name}")
            else:
                written = write_results_openpyxl(file, layout=layout, plan=plan, results=results, blocks=blocks)
            for rid, block in written.blocks.items():
                if rid not in before:
                    report.blocks_created.append((rid, block.ordinal, block.first_row))
            queue.record_blocks(file, written.blocks)
            skipped = set(written.skipped_keys)
            conflicts = set(written.conflicts)
            done = [
                item for item in group
                if item.result.round_id in written.blocks and (item.result.round_id, item.result.plan_index) not in skipped
            ]
            queue.mark_done(done, written_rows=written.written)
            report.written += len(written.written)
            report.unchanged += written.unchanged
            if (written.written or done) and str(file) not in report.workbooks:
                report.workbooks.append(str(file))
            report.notes.extend(written.skipped)
            clashing = [item for item in group if (item.result.round_id, item.result.plan_index) in conflicts]
            if clashing:
                queue.mark_conflict(clashing, "; ".join(written.skipped)[:300])
            left = [item for item in group if item not in done and item not in clashing]
            if left:
                queue.mark_failed(left, "; ".join(written.skipped)[:300] or "not written")
        except ExcelBusyError as exc:
            report.busy = str(exc)
        except BackupError as exc:
            report.error = str(exc)
            queue.mark_failed(group, str(exc))
        except WorkbookError as exc:
            report.error = str(exc)
            queue.mark_failed(group, str(exc))
        except Exception as exc:  # noqa: BLE001 - keep the queue; report
            report.error = f"{type(exc).__name__}: {exc}"
            queue.mark_failed(group, report.error)
    report.pending = queue.pending_count()
    return report


def queue_path(config_dir: Path) -> Path:
    return Path(config_dir) / "cellular" / QUEUE_FILENAME
