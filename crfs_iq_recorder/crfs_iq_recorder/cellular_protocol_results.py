"""Fills the protocol workbook with the results of each round: one worksheet per round.

The protocol worksheet is never modified. Each round gets its own sheet named
after its scheduled local time ("Round 2026-11-02 19-00"); the sheet is rewritten
from the session state whenever something changes, and the sheets of earlier
rounds are left alone, so no previous result is overwritten.

A round sheet copies the protocol header (texts, styles, widths, frozen pane)
and holds one row per planned capture window, in plan order. Each row carries
the protocol row's own cells, then the collection fills in:

* start/stop: the window actually recorded (a split allocation shows one
  window per row; the planned edges are in the Window column),
* the sensor column, when the protocol has one: the sensor that recorded,
* the result column (e.g. "IQ Recording"): the recording's file names, one per
  line, as they exist on the sensor and in the download folder,
* the duration column (e.g. "IQ Duration (s)"): the seconds the files hold,
* the date & time column: the local start time of the capture as a real
  Excel datetime (dd/mm/yyyy hh:mm:ss),
* appended Window / Status / Notes / Round columns (or the protocol's own
  Status column when it has one).

Nothing is invented: a window that was not recorded keeps its result cells
empty and says so in Status. The workbook is saved through a temporary file
and only replaced when the save succeeded; when Excel holds the file the
caller gets `ExcelBusyError` and retries later.
"""

from __future__ import annotations

import os
import re
from copy import copy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .cellular_protocol import UNIT_TO_HZ, ColumnMapping, _normalise_header, format_mhz
from .excel_com import excel_has_workbook
from .excel_writer import ExcelBusyError, backup_workbook
from .recording_history import sort_iq_filenames

SHEET_PREFIX = "Round "
DATETIME_FORMAT = "dd/mm/yyyy hh:mm:ss"
# Headers the collection appends after the protocol's own columns.
EXTRA_HEADERS = ("Window", "Status", "Notes", "Round")
_SENSOR_HEADERS = {"sensor", "sensor name", "sensor id", "receiver", "recorder", "device", "instrument", "unit id", "sensor serial"}
_BAD_SHEET_CHARS = re.compile(r"[\[\]:*?/\\]")
_BAD_TABLE_CHARS = re.compile(r"[^A-Za-z0-9_]")


@dataclass(frozen=True)
class ResultRow:
    """What the collection knows about one planned capture window."""

    protocol_row: int
    direction: str = ""
    start_hz: int | None = None
    stop_hz: int | None = None
    sensor: str = ""
    files: tuple[str, ...] = ()
    duration_s: float | None = None
    recorded_at: datetime | None = None
    window: str = ""
    status: str = ""
    notes: str = ""
    round_id: str = ""


@dataclass
class SyncResult:
    sheet: str
    rows: int
    created: bool
    backup: str = ""
    warnings: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------- names
def round_sheet_name(scheduled_local: datetime, existing: Iterable[str] = ()) -> str:
    """'Round 2026-11-02 19-00', with ' (2)', ' (3)'… when the name is taken."""
    base = f"{SHEET_PREFIX}{scheduled_local:%Y-%m-%d %H-%M}"
    taken = {str(name).casefold() for name in existing}
    name = base
    counter = 2
    while name.casefold() in taken:
        name = f"{base} ({counter})"
        counter += 1
    return _BAD_SHEET_CHARS.sub("-", name)[:31]


def _table_name(sheet_name: str, existing: Iterable[str]) -> str:
    base = _BAD_TABLE_CHARS.sub("_", sheet_name).strip("_") or "Round"
    if base[0].isdigit():
        base = "T_" + base
    taken = {str(name).casefold() for name in existing}
    name = base
    counter = 2
    while name.casefold() in taken:
        name = f"{base}_{counter}"
        counter += 1
    return name


def sensor_column(headers: Iterable[str], mapping: ColumnMapping | None = None) -> str | None:
    """The protocol's sensor column, if it has one (an unmapped 'Sensor' header)."""
    mapped = set(mapping.roles().values()) if mapping is not None else set()
    for header in headers:
        text = str(header or "").strip()
        if not text or text in mapped:
            continue
        key, _unit = _normalise_header(text)
        if key in _SENSOR_HEADERS:
            return text
    return None


def local_naive(value: datetime | str | None) -> datetime | None:
    """A datetime for Excel: local wall-clock time without tzinfo."""
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


# ----------------------------------------------------------------------------- workbook
def _copy_style(src, dst) -> None:
    if src.has_style:
        dst.font = copy(src.font)
        dst.fill = copy(src.fill)
        dst.border = copy(src.border)
        dst.alignment = copy(src.alignment)
        dst.number_format = src.number_format
        dst.protection = copy(src.protection)


def _column_unit(mapping: ColumnMapping, role: str, row_unit_text: str | None) -> str:
    unit = mapping.header_units.get(role)
    if not unit and row_unit_text:
        unit = {name.casefold(): name for name in UNIT_TO_HZ}.get(row_unit_text.strip().casefold())
    return unit or mapping.default_unit or "MHz"


def _frequency_value(hz: int, unit: str, like: Any) -> Any:
    """The window edge in the column's unit; text with the unit when the protocol wrote text."""
    scale = UNIT_TO_HZ.get(unit, UNIT_TO_HZ["MHz"])
    number = hz / scale
    if isinstance(like, str) and like.strip() and not re.match(r"^\s*[+-]?[\d.,\s]+\s*$", like):
        return f"{format_mhz(hz)} MHz"
    return int(number) if float(number).is_integer() else round(number, 6)


def _status_cell(ws, row: int, col: int, value: Any, like=None, *, wrap: bool = False, number_format: str | None = None) -> None:
    cell = ws.cell(row=row, column=col)
    cell.value = value
    if like is not None:
        _copy_style(like, cell)
    if wrap:
        alignment = copy(cell.alignment)
        alignment.wrap_text = True
        alignment.vertical = "top"
        cell.alignment = alignment
    if number_format:
        cell.number_format = number_format


def sync_round_sheet(
    path: str | Path,
    *,
    protocol_sheet: str,
    header_row: int,
    mapping: ColumnMapping,
    sheet_name: str | None,
    rows: list[ResultRow],
    scheduled_local: datetime | None = None,
    backup_dir: str | Path | None = None,
    make_backup: bool = False,
) -> SyncResult:
    """Write (or rewrite) the round's worksheet in the protocol workbook.

    With `sheet_name=None` a free name is chosen from `scheduled_local`
    (see `round_sheet_name`) and returned in the result; callers keep it so
    later writes of the same round reuse the sheet.

    Raises `ExcelBusyError` when the workbook is open in Excel or locked, and
    `ValueError` when the protocol sheet is missing. Idempotent: calling it
    again with the same rows produces the same sheet.
    """
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    file = Path(path)
    if not file.is_file():
        raise FileNotFoundError(f"Protocol workbook not found: {file}")
    if excel_has_workbook(file):
        raise ExcelBusyError(f"{file.name} is open in Excel; the results sheet is written when it is closed.")
    keep_vba = file.suffix.casefold() == ".xlsm"
    backup = ""
    if make_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        folder = Path(backup_dir) if backup_dir else file.parent
        try:
            backup = str(backup_workbook(file, folder / f"{file.stem}.crfs-backup-{stamp}{file.suffix}"))
        except OSError:
            backup = ""
    try:
        workbook = load_workbook(file, keep_vba=keep_vba)
    except PermissionError as exc:
        raise ExcelBusyError(f"Could not open {file.name}: {exc}") from exc
    warnings: list[str] = []
    try:
        if protocol_sheet not in workbook.sheetnames:
            raise ValueError(f"Worksheet {protocol_sheet!r} is not in {file.name}.")
        src = workbook[protocol_sheet]
        header_row = max(int(header_row or 1), 1)
        src_cols = max(int(src.max_column or 1), 1)
        headers = [src.cell(row=header_row, column=col).value for col in range(1, src_cols + 1)]
        header_texts = [str(value).strip() if value is not None else "" for value in headers]
        columns: dict[str, int] = {}
        for index, text in enumerate(header_texts):
            if text and text not in columns:
                columns[text] = index + 1
        if not sheet_name:
            sheet_name = round_sheet_name(scheduled_local or datetime.now(), workbook.sheetnames)
        created = sheet_name not in workbook.sheetnames
        ws = workbook.create_sheet(sheet_name) if created else workbook[sheet_name]

        # Header: the protocol's own columns, then the collection's.
        last_header = src.cell(row=header_row, column=src_cols)
        for col in range(1, src_cols + 1):
            source = src.cell(row=header_row, column=col)
            target = ws.cell(row=header_row, column=col, value=source.value)
            _copy_style(source, target)
            letter = get_column_letter(col)
            width = src.column_dimensions[letter].width if letter in src.column_dimensions else None
            if width:
                ws.column_dimensions[letter].width = width
        extra_cols: dict[str, int] = {}
        next_col = src_cols + 1
        for name in EXTRA_HEADERS:
            if name == "Status" and mapping.status and mapping.status in columns:
                extra_cols[name] = columns[mapping.status]
                continue
            title = name
            while title in header_texts:
                title = f"Collection {title.casefold()}"
            target = ws.cell(row=header_row, column=next_col, value=title)
            _copy_style(last_header, target)
            ws.column_dimensions[get_column_letter(next_col)].width = {"Window": 46, "Status": 40, "Notes": 50, "Round": 24}[name]
            extra_cols[name] = next_col
            header_texts.append(title)
            next_col += 1
        total_cols = next_col - 1
        if header_row > 1:
            for r in range(1, header_row):
                for col in range(1, src_cols + 1):
                    source = src.cell(row=r, column=col)
                    target = ws.cell(row=r, column=col, value=source.value)
                    _copy_style(source, target)
        ws.freeze_panes = ws.cell(row=header_row + 1, column=1)

        # Which columns the collection fills.
        result_col = columns.get(mapping.result or "")
        time_col = columns.get(mapping.result_time or "")
        duration_col = columns.get(mapping.duration or "")
        sensor_col = columns.get(sensor_column(header_texts[:src_cols], mapping) or "")
        unit_col = columns.get(mapping.unit or "")
        layout = mapping.layout()
        if mapping.result and result_col is None:
            warnings.append(f"Result column {mapping.result!r} is not in the protocol sheet; file names were not written.")

        # Rows in plan order.
        for index, item in enumerate(rows):
            r = header_row + 1 + index
            src_row = int(item.protocol_row or 0)
            for col in range(1, src_cols + 1):
                target = ws.cell(row=r, column=col)
                if src_row > header_row:
                    source = src.cell(row=src_row, column=col)
                    target.value = source.value
                    _copy_style(source, target)
                else:
                    target.value = None
            row_unit = ws.cell(row=r, column=unit_col).value if unit_col else None
            row_unit_text = str(row_unit).strip() if row_unit is not None else None
            if item.start_hz is not None and item.stop_hz is not None:
                if layout == "rows":
                    pairs = (("start", mapping.start, item.start_hz), ("stop", mapping.stop, item.stop_hz))
                    clear: tuple[str | None, ...] = ()
                elif str(item.direction).upper().startswith("UL"):
                    pairs = (("ul_start", mapping.ul_start, item.start_hz), ("ul_stop", mapping.ul_stop, item.stop_hz))
                    clear = (mapping.dl_start, mapping.dl_stop)
                else:
                    pairs = (("dl_start", mapping.dl_start, item.start_hz), ("dl_stop", mapping.dl_stop, item.stop_hz))
                    clear = (mapping.ul_start, mapping.ul_stop)
                for role, header, hz in pairs:
                    col = columns.get(header or "")
                    if col:
                        cell = ws.cell(row=r, column=col)
                        cell.value = _frequency_value(hz, _column_unit(mapping, role, row_unit_text), cell.value)
                for header in clear:
                    col = columns.get(header or "")
                    if col:
                        ws.cell(row=r, column=col).value = None
            if layout == "paired" and mapping.direction and columns.get(mapping.direction) and item.direction:
                ws.cell(row=r, column=columns[mapping.direction]).value = item.direction
            if sensor_col and item.sensor:
                ws.cell(row=r, column=sensor_col).value = item.sensor
            if result_col:
                names = sort_iq_filenames(list(item.files))
                _status_cell(ws, r, result_col, "\n".join(names) if names else None, wrap=bool(names))
            if duration_col:
                cell = ws.cell(row=r, column=duration_col)
                cell.value = round(float(item.duration_s), 1) if item.duration_s is not None else None
                if item.duration_s is not None:
                    cell.number_format = "0.0"
            if time_col:
                when = local_naive(item.recorded_at)
                _status_cell(ws, r, time_col, when, number_format=DATETIME_FORMAT if when else None)
            _status_cell(ws, r, extra_cols["Window"], item.window or None, wrap=True)
            _status_cell(ws, r, extra_cols["Status"], item.status or None, wrap=True)
            _status_cell(ws, r, extra_cols["Notes"], item.notes or None, wrap=True)
            _status_cell(ws, r, extra_cols["Round"], item.round_id or None)
        # Leftover rows from an earlier, longer write (plan rows never shrink, but stay safe).
        last_row = header_row + len(rows)
        for r in range(last_row + 1, (ws.max_row or last_row) + 1):
            for col in range(1, total_cols + 1):
                ws.cell(row=r, column=col).value = None

        # An Excel table like the protocol's, so the round can be filtered.
        _sync_table(workbook, src, ws, header_row=header_row, header_texts=header_texts, last_row=last_row, total_cols=total_cols, warnings=warnings)

        temp = file.with_name(f"{file.stem}.crfs-tmp-{os.getpid()}{file.suffix}")
        try:
            workbook.save(temp)
            os.replace(temp, file)
        except PermissionError as exc:
            raise ExcelBusyError(f"Could not save {file.name}: {exc}") from exc
        finally:
            try:
                if temp.exists():
                    temp.unlink()
            except OSError:
                pass
        return SyncResult(sheet=sheet_name, rows=len(rows), created=created, backup=backup, warnings=warnings)
    finally:
        vba = getattr(workbook, "vba_archive", None)
        if vba is not None:
            try:
                vba.close()
            except Exception:  # noqa: BLE001
                pass
        workbook.close()


def _sync_table(workbook, src, ws, *, header_row: int, header_texts: list[str], last_row: int, total_cols: int, warnings: list[str]) -> None:
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.table import Table, TableStyleInfo

    source_table = next((table for table in src.tables.values()), None) if getattr(src, "tables", None) else None
    if source_table is None or last_row <= header_row:
        return
    texts = [str(t).strip() for t in header_texts[:total_cols]]
    if any(not t for t in texts) or len({t.casefold() for t in texts}) != len(texts):
        warnings.append("The round sheet has no filter table: the protocol's headers are not unique.")
        return
    ref = f"A{header_row}:{get_column_letter(total_cols)}{last_row}"
    existing = next((table for table in ws.tables.values()), None)
    if existing is not None:
        existing.ref = ref
        # Header texts must match the cells exactly.
        from openpyxl.worksheet.table import TableColumn

        existing.tableColumns = [TableColumn(id=i + 1, name=t) for i, t in enumerate(texts)]
        return
    names = [name for sheet in workbook.worksheets for name in getattr(sheet, "tables", {}).keys()]
    table = Table(displayName=_table_name(ws.title, names), ref=ref)
    style = getattr(source_table, "tableStyleInfo", None)
    table.tableStyleInfo = TableStyleInfo(
        name=getattr(style, "name", None) or "TableStyleMedium2",
        showFirstColumn=bool(getattr(style, "showFirstColumn", False)),
        showLastColumn=bool(getattr(style, "showLastColumn", False)),
        showRowStripes=bool(getattr(style, "showRowStripes", True)),
        showColumnStripes=bool(getattr(style, "showColumnStripes", False)),
    )
    ws.add_table(table)


# ----------------------------------------------------------------------------- rows from the session
def task_status_text(state: dict[str, Any], *, round_finished: bool) -> str:
    """One readable line for the Status column of a planned capture."""
    status = str(state.get("status") or "queued")
    if status == "confirmed":
        parts = ["SHORT capture confirmed" if state.get("short") else "confirmed"]
        files = list(state.get("files") or [])
        if files:
            statuses = [str(f.get("download_status") or "") for f in files]
            if all(s == "verified" for s in statuses):
                parts.append("downloaded, SHA-256 verified" if any(f.get("sha256") for f in files) else "downloaded, size verified")
            elif all(s == "not_requested" for s in statuses):
                parts.append("on the sensor (download not requested)")
            elif any(s == "failed" for s in statuses):
                parts.append("download failed for " + ", ".join(f["name"] for f, s in zip(files, statuses) if s == "failed") + " (file stays on the sensor)")
            else:
                done = sum(1 for s in statuses if s == "verified")
                parts.append(f"downloading {done}/{len(files)} file(s) copied")
        quality = state.get("quality") or {}
        level = quality.get("level")
        if level == "ok":
            parts.append("files checked OK")
        elif level == "warning":
            parts.append("files checked with warnings")
        elif level == "error":
            parts.append("file problems found")
        elif level == "not_checked":
            parts.append("content not inspected")
        elif level == "pending":
            parts.append("file check running")
        if state.get("merged_output"):
            parts.append("merge requested")
        return "; ".join(parts)
    if status in {"submitting", "recording"}:
        return "recording…" if not round_finished else "uncertain — application interrupted during the capture; reconcile with the sensor"
    if status == "unknown":
        return "uncertain — not counted as collected"
    if status == "not_sent":
        return "not sent (nothing reached the sensor)"
    if status == "sensor_error":
        return "failed — the sensor reported an error"
    if status == "failed":
        return "failed"
    if status == "skipped":
        return "skipped by the operator"
    return "not recorded" if round_finished else "queued"


def window_text(task: dict[str, Any]) -> str:
    start = int(task.get("window_start_hz") or 0)
    stop = int(task.get("window_stop_hz") or 0)
    req_start = int(task.get("requested_start_hz") or 0)
    req_stop = int(task.get("requested_stop_hz") or 0)
    count = int(task.get("window_count") or 1)
    text = f"{format_mhz(start)}–{format_mhz(stop)} MHz"
    if count > 1:
        text += (
            f": window {int(task.get('window_index') or 1)} of {count} of the allocation "
            f"{format_mhz(req_start)}–{format_mhz(req_stop)} MHz (sequential coverage, partial capture)"
        )
        overlap = int(task.get("overlap_prev_hz") or 0)
        if overlap:
            text += f"; overlaps the previous window by {format_mhz(overlap)} MHz"
    else:
        text += " (whole allocation)"
    reps = int(task.get("repetitions") or 1)
    if reps > 1:
        text += f"; repetition {int(task.get('repetition') or 1)} of {reps}"
    return text


def result_rows(
    plan_tasks: list[dict[str, Any]],
    task_states: dict[str, dict[str, Any]],
    *,
    round_id: str,
    round_finished: bool,
    sensor_text: str,
) -> list[ResultRow]:
    """One `ResultRow` per planned task, from the session's dictionaries."""
    rows: list[ResultRow] = []
    for task in sorted(plan_tasks, key=lambda t: int(t.get("index") or 0)):
        state = task_states.get(str(task.get("task_key") or ""), {})
        summary = state.get("summary") or {}
        confirmed = str(state.get("status") or "") == "confirmed"
        files = tuple(str(f.get("name") or "") for f in (state.get("files") or []) if f.get("name")) if confirmed else ()
        # Only the measured seconds go in the duration column; a capture whose
        # duration was not measured leaves it empty (the Notes say so).
        held = summary.get("held_s") if confirmed else None
        duration = float(held) if held is not None else None
        recorded_at = None
        if confirmed or str(state.get("status") or "") in {"submitting", "recording", "unknown"}:
            recorded_at = local_naive(summary.get("started_at")) or local_naive(state.get("started_utc"))
        notes = [str(n) for n in (state.get("notes") or []) if str(n).strip()]
        if confirmed and held is None:
            notes.append("Recorded seconds not measured; the duration cell is left empty.")
        rows.append(
            ResultRow(
                protocol_row=int(task.get("row") or 0),
                direction=str(task.get("direction") or ""),
                start_hz=int(task.get("window_start_hz") or 0) or None,
                stop_hz=int(task.get("window_stop_hz") or 0) or None,
                sensor=sensor_text if confirmed else "",
                files=files,
                duration_s=duration if duration and duration > 0 else None,
                recorded_at=recorded_at,
                window=window_text(task),
                status=task_status_text(state, round_finished=round_finished),
                notes="; ".join(notes),
                round_id=round_id,
            )
        )
    return rows


def sensor_text(sensor: dict[str, Any] | None) -> str:
    sensor = sensor or {}
    model = str(sensor.get("model") or "").strip()
    serial = str(sensor.get("serial") or "").strip()
    if model and serial:
        return f"{model} ({serial})"
    return model or serial
