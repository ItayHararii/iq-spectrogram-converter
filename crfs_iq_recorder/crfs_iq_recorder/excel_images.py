"""Spectrogram thumbnails in workbooks: column, placement, links.

Every pending image (kept in the workbook queue, see
``cellular_workbook.WorkbookQueue.enqueue_image``) names a workbook, a sheet,
the header row, the header of the IQ file-name column and one IQ part file.
At write time the row is found again by that file name (rows may have been
sorted), the "IQ Spectrogram" column right of the file names is reused or
created once, and the thumbnail is placed inside the cell: anchored to it
(move and size with the cell), one picture per part side by side, named after
the part file so a retry replaces it instead of adding a second one, with a
click link to the full-size PNG.

An open workbook is written through the running Excel (its own column insert,
so formulas, formats, validation, tables and other pictures follow). A closed
workbook is written with openpyxl only when nothing in it would be lost and no
column has to be inserted; otherwise the pictures wait until it is open in Excel.
"""

from __future__ import annotations

import os
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote

from .collection_catalog import HEADER_IQ, HEADER_SPECTROGRAM
from .excel_writer import ExcelBusyError

KIND_LOG = "log"
KIND_PROTOCOL = "protocol"

SHAPE_PREFIX = "CRFS spectrogram "
THUMB_WIDTH_PT = 96.0
GAP_PT = 3.0
PAD_PT = 3.0
PER_LINE = 4
MAX_ROW_HEIGHT_PT = 409.0
MAX_COLUMN_CHARS = 255.0
DEFAULT_ASPECT = 0.5  # converter thumbnails are 2:1
_EMU_PER_PT = 12700
_XL_SHIFT_RIGHT = -4161
_XL_FORMAT_FROM_LEFT = 0
_XL_MOVE_AND_SIZE = 1
_MSO_TRUE = -1
_MSO_COMMENT = 4
# Headers that already mean "the spectrogram column" when found right of the file names.
_ADJACENT_HEADERS = {"spectrogram", "spectrograms", "iq image", "iq images", "iq spectrogram image", "spectrogram image"}
_NAME_SPLIT = re.compile(r"[\r\n;,]+")

LOCAL_TIP = "Full-size spectrogram on this PC: {path}"
SHARED_TIP = "Open the full-size spectrogram"


# ------------------------------------------------------------------ links
@dataclass(frozen=True)
class Link:
    address: str  # SharePoint/OneDrive URL or a local path
    tooltip: str
    shared: bool

    @property
    def uri(self) -> str:
        """The address as a hyperlink target (file:/// URI for a local file)."""
        if self.shared:
            return self.address
        return Path(self.address).resolve().as_uri()


def onedrive_mappings() -> list[tuple[Path, str]]:
    """(local mount folder, web URL) for every folder OneDrive syncs on this PC (from the sync engine's registry)."""
    if sys.platform != "win32":
        return []
    try:
        import winreg  # noqa: PLC0415
    except ImportError:
        return []
    out: list[tuple[Path, str]] = []
    root = r"Software\SyncEngines\Providers\OneDrive"
    try:
        providers = winreg.OpenKey(winreg.HKEY_CURRENT_USER, root)
    except OSError:
        return []
    with providers:
        index = 0
        while True:
            try:
                name = winreg.EnumKey(providers, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(providers, name) as key:
                    mount = str(winreg.QueryValueEx(key, "MountPoint")[0] or "")
                    url = str(winreg.QueryValueEx(key, "UrlNamespace")[0] or "")
            except OSError:
                continue
            if mount and url.lower().startswith("https://"):
                out.append((Path(mount), url))
    # Deepest mount first: a library synced inside another wins.
    out.sort(key=lambda item: len(str(item[0])), reverse=True)
    return out


def _inside(path: Path, folder: Path) -> Path | None:
    try:
        return path.resolve().relative_to(folder.resolve())
    except (ValueError, OSError):
        return None


def link_for(png: Path | str, *, mappings: Iterable[tuple[Path, str]] | None = None) -> Link | None:
    """Where a click on the thumbnail goes; None when the full-size PNG does not exist.

    A web URL only when the PNG itself lies in a OneDrive/SharePoint folder this
    PC syncs (URL = that folder's UrlNamespace + the relative path); otherwise
    the local file, with a ScreenTip that says it is on this PC.
    """
    file = Path(png)
    if not file.is_file():
        return None
    for mount, url in (onedrive_mappings() if mappings is None else mappings):
        relative = _inside(file, Path(mount))
        if relative is None:
            continue
        base = url if url.endswith("/") else url + "/"
        base = base.replace(" ", "%20")
        return Link(base + "/".join(quote(part) for part in relative.parts), SHARED_TIP, True)
    resolved = file.resolve()
    return Link(str(resolved), LOCAL_TIP.format(path=resolved), False)


# ------------------------------------------------------------------ pure helpers
def shape_name(filename: str) -> str:
    return f"{SHAPE_PREFIX}{filename}"


def alt_text(filename: str) -> str:
    return f"Spectrogram of {filename}. Click to open the full-size image."


def file_of_shape(name: str) -> str | None:
    return name[len(SHAPE_PREFIX):] if str(name or "").startswith(SHAPE_PREFIX) else None


def split_cell_names(value: Any) -> list[str]:
    """File names in a cell, in the order written (newline-separated, also ; or ,)."""
    if value in (None, ""):
        return []
    return [part.strip() for part in _NAME_SPLIT.split(str(value)) if part.strip()]


def match_rows(values: list[Any], first_row: int, filename: str) -> list[tuple[int, list[str]]]:
    """(sheet row, names in its cell) for every row whose file-name cell lists this file."""
    want = filename.casefold()
    found: list[tuple[int, list[str]]] = []
    for offset, value in enumerate(values):
        names = split_cell_names(value)
        if any(name.casefold() == want for name in names):
            found.append((first_row + offset, names))
    return found


def choose_row(matches: list[tuple[int, list[str]]], hint: int = 0) -> tuple[int, list[str]] | None:
    if not matches:
        return None
    for row, names in matches:
        if row == hint:
            return row, names
    return matches[0]


def png_aspect(path: Path | str) -> float:
    """Height / width of a PNG from its header (no image library needed)."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(24)
        if head[:8] == b"\x89PNG\r\n\x1a\n" and head[12:16] == b"IHDR":
            width, height = struct.unpack(">II", head[16:24])
            if width > 0 and height > 0:
                return height / width
    except OSError:
        pass
    return DEFAULT_ASPECT


def thumb_size(aspect: float = DEFAULT_ASPECT) -> tuple[float, float]:
    return THUMB_WIDTH_PT, round(THUMB_WIDTH_PT * aspect, 2)


def cell_size_needed(count: int, aspect: float = DEFAULT_ASPECT) -> tuple[float, float]:
    """(width, height) in points a cell needs for ``count`` thumbnails side by side (wrapping after PER_LINE)."""
    count = max(1, count)
    width, height = thumb_size(aspect)
    per_line = min(count, PER_LINE)
    lines = (count + PER_LINE - 1) // PER_LINE
    return (
        2 * PAD_PT + per_line * width + (per_line - 1) * GAP_PT,
        min(MAX_ROW_HEIGHT_PT, 2 * PAD_PT + lines * height + (lines - 1) * GAP_PT),
    )


def slot_geometry(index: int, count: int, aspect: float = DEFAULT_ASPECT) -> tuple[float, float, float, float]:
    """(left offset, top offset, width, height) in points of part ``index`` inside its cell."""
    width, height = thumb_size(aspect)
    index = max(0, min(index, max(count, 1) - 1))
    line, column = divmod(index, PER_LINE)
    return PAD_PT + column * (width + GAP_PT), PAD_PT + line * (height + GAP_PT), width, height


def part_slot(filename: str, names: list[str]) -> tuple[int, int]:
    """(index, count) of a part file among the IQ files listed in its row's cell."""
    files = [name for name in names if name.casefold().endswith(".wav")] or names
    want = filename.casefold()
    for index, name in enumerate(files):
        if name.casefold() == want:
            return index, len(files)
    return 0, max(1, len(files))


def _norm_header(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def find_header(headers: list[Any], text: str, hint: int = 0) -> int:
    """1-based column whose header is ``text`` (case/space-insensitive); the hint wins on a tie; 0 when absent."""
    want = _norm_header(text)
    if not want:
        return 0
    found = [index for index, value in enumerate(headers, start=1) if _norm_header(value) == want]
    if not found:
        return 0
    return hint if hint in found else found[0]


def iq_column(headers: list[Any], item: dict) -> int:
    """The file-name column: by its header text; the remembered column only when no header text is known."""
    header = str(item.get("iq_header") or "")
    hint = int(item.get("iq_col") or 0)
    found = find_header(headers, header or HEADER_IQ, hint)
    if found:
        return found
    return hint if not header and 0 < hint <= len(headers) else 0


def resolve_column(headers: list[Any], iq_col: int, *, adjacent_free: bool) -> tuple[int, str]:
    """(column, how): 'reuse' an existing spectrogram column, use the free 'adjacent' column, or 'insert' one."""
    existing = find_header(headers, HEADER_SPECTROGRAM)
    if existing:
        return existing, "reuse"
    right = headers[iq_col] if iq_col < len(headers) else None
    if _norm_header(right) in _ADJACENT_HEADERS:
        return iq_col + 1, "reuse"
    if adjacent_free and _norm_header(right) == "":
        return iq_col + 1, "adjacent"
    return iq_col + 1, "insert"


# ------------------------------------------------------------------ results
@dataclass
class ImageOutcome:
    key: str
    status: str  # written | waiting | skipped | failed
    note: str = ""
    row: int = 0
    column: int = 0


@dataclass
class BookResult:
    outcomes: list[ImageOutcome] = field(default_factory=list)
    columns_created: list[tuple[str, int]] = field(default_factory=list)  # (sheet, column)
    notes: list[str] = field(default_factory=list)


class DeferredError(RuntimeError):
    """The pictures cannot be added to this workbook while it is closed; they wait until it is open in Excel."""


def _group_by_sheet(items: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for item in items:
        out.setdefault(str(item.get("sheet") or ""), []).append(item)
    return out


def _outcome(item: dict, status: str, note: str = "", row: int = 0, column: int = 0) -> ImageOutcome:
    return ImageOutcome(key=str(item.get("key") or ""), status=status, note=note, row=row, column=column)


# ------------------------------------------------------------------ Excel COM
def _com_last_row(sheet) -> int:
    from .excel_com import last_row_counting, sheet_filtered  # noqa: PLC0415

    if sheet_filtered(sheet):  # Find skips rows a filter hides
        return last_row_counting(sheet, _com_last_col(sheet))
    try:
        found = sheet.Cells.Find(What="*", After=sheet.Cells(1, 1), LookIn=-4123, LookAt=2, SearchOrder=1, SearchDirection=2, MatchCase=False)
        if found is not None:
            return max(1, int(found.Row))
    except Exception:  # noqa: BLE001
        pass
    used = sheet.UsedRange
    return max(1, int(used.Row) + int(used.Rows.Count) - 1)


def _com_last_col(sheet) -> int:
    used = sheet.UsedRange
    return max(1, int(used.Column) + int(used.Columns.Count) - 1)


def _com_headers(sheet, header_row: int) -> list[Any]:
    from .excel_com import _range_values  # noqa: PLC0415

    values = _range_values(sheet, header_row, 1, header_row, _com_last_col(sheet))
    return list(values[0]) if values else []


def _com_column(sheet, first_row: int, last_row: int, col: int) -> list[Any]:
    from .excel_com import _range_values  # noqa: PLC0415

    if last_row < first_row:
        return []
    return [row[0] if row else None for row in _range_values(sheet, first_row, col, last_row, col)]


def _com_shapes(sheet) -> tuple[dict[str, Any], dict[tuple[int, int], list[str]]]:
    """Our pictures by name, and every other shape's name by its top-left cell."""
    ours: dict[str, Any] = {}
    others: dict[tuple[int, int], list[str]] = {}
    shapes = sheet.Shapes
    for index in range(1, int(shapes.Count) + 1):
        shape = shapes.Item(index)
        name = str(shape.Name)
        if file_of_shape(name) is not None:
            ours[name.casefold()] = shape
            continue
        try:
            if int(shape.Type) == _MSO_COMMENT:
                continue
            cell = shape.TopLeftCell
            others.setdefault((int(cell.Row), int(cell.Column)), []).append(name)
        except Exception:  # noqa: BLE001 - a shape without a cell position is not in our way
            continue
    return ours, others


def _com_table_at(sheet, row: int, col: int):
    try:
        tables = sheet.ListObjects
        count = int(tables.Count)
    except Exception:  # noqa: BLE001
        return None
    for index in range(1, count + 1):
        table = tables(index)
        rng = table.Range
        top, left = int(rng.Row), int(rng.Column)
        if top <= row < top + int(rng.Rows.Count) and left <= col < left + int(rng.Columns.Count):
            return table
    return None


def _com_set_header(sheet, header_row: int, iq_col: int, col: int) -> None:
    try:
        sheet.Cells(header_row, iq_col).Copy(sheet.Cells(header_row, col))
    except Exception:  # noqa: BLE001 - formatting only
        pass
    sheet.Cells(header_row, col).Value = HEADER_SPECTROGRAM


def _com_extend_filter(sheet, col: int) -> None:
    """Keep the new column inside a plain AutoFilter range so sorting moves its pictures too."""
    try:
        if not bool(sheet.AutoFilterMode) or bool(sheet.FilterMode):
            return
        rng = sheet.AutoFilter.Range
        left, width = int(rng.Column), int(rng.Columns.Count)
        if left + width != col:
            return
        wider = rng.Resize(int(rng.Rows.Count), width + 1)
        sheet.AutoFilterMode = False
        wider.AutoFilter()
    except Exception:  # noqa: BLE001 - presentation only; the pictures are placed either way
        pass


def _com_spectrogram_column(sheet, header_row: int, iq_col: int, last_row: int) -> tuple[int, bool]:
    """(column, created) - reuse, take the free neighbour, or insert with Excel's own column insert."""
    headers = _com_headers(sheet, header_row)
    table = _com_table_at(sheet, header_row, iq_col)
    if table is not None:
        rng = table.Range
        left, width = int(rng.Column), int(rng.Columns.Count)
        inside = headers[left - 1:left - 1 + width]
        found = find_header(inside, HEADER_SPECTROGRAM)
        if found:
            return left + found - 1, False
        right = headers[iq_col] if iq_col < len(headers) and iq_col < left + width - 1 else None
        if iq_col < left + width - 1 and _norm_header(right) in _ADJACENT_HEADERS:
            return iq_col + 1, False
        column = table.ListColumns.Add(iq_col - left + 2)
        column.Name = HEADER_SPECTROGRAM
        return int(column.Range.Column), True
    neighbour = _com_column(sheet, header_row + 1, last_row, iq_col + 1)
    free = all(value in (None, "") for value in neighbour)
    col, how = resolve_column(headers, iq_col, adjacent_free=free)
    if how == "reuse":
        return col, False
    if how == "insert":
        sheet.Columns(col).Insert(Shift=_XL_SHIFT_RIGHT, CopyOrigin=_XL_FORMAT_FROM_LEFT)
    _com_set_header(sheet, header_row, iq_col, col)
    _com_extend_filter(sheet, col)
    return col, True


def _com_fit_cell(sheet, row: int, col: int, need_w: float, need_h: float) -> tuple[bool, bool]:
    """Widen the column / raise the row (never shrink); (column grew, row grew)."""
    grew_col = grew_row = False
    column = sheet.Columns(col)
    width = float(column.Width)
    if width + 0.5 < need_w:
        chars = float(column.ColumnWidth) or 8.43
        ratio = width / chars if chars > 0 and width > 0 else 5.6
        for _ in range(4):
            target = min(MAX_COLUMN_CHARS, chars + (need_w - width) / ratio + 0.3)
            column.ColumnWidth = target
            chars, width = target, float(column.Width)
            if width + 0.5 >= need_w or target >= MAX_COLUMN_CHARS:
                break
            ratio = width / target if target > 0 else ratio
        grew_col = True
    line = sheet.Rows(row)
    height = float(line.RowHeight)
    if height + 0.5 < need_h:
        line.RowHeight = min(MAX_ROW_HEIGHT_PT, need_h)
        grew_row = True
    return grew_col, grew_row


def _com_place(shape, cell, index: int, count: int, aspect: float) -> None:
    dx, dy, width, height = slot_geometry(index, count, aspect)
    shape.LockAspectRatio = 0
    shape.Left = float(cell.Left) + dx
    shape.Top = float(cell.Top) + dy
    shape.Width = width
    shape.Height = height
    shape.LockAspectRatio = _MSO_TRUE


def _com_relayout(sheet, ours: dict[str, Any], col: int, rows: dict[str, tuple[int, list[str]]], aspect: float, only_row: int = 0) -> None:
    """Put our pictures in a column back at their slots (after the column or a row changed size)."""
    for key, shape in ours.items():
        filename = file_of_shape(str(shape.Name))
        if filename is None:
            continue
        try:
            cell = shape.TopLeftCell
            if int(cell.Column) != col:
                continue
        except Exception:  # noqa: BLE001
            continue
        found = rows.get(filename.casefold())
        if found is None:
            continue
        row, names = found
        if only_row and row != only_row:
            continue
        index, count = part_slot(filename, names)
        _com_place(shape, sheet.Cells(row, col), index, count, aspect)


def write_images_com(path: Path, items: list[dict], *, links: Callable[[Path], Link | None] = link_for) -> BookResult:
    """Place pictures in the workbook as it is open in the running Excel, then save it there."""
    from .cellular_workbook import _book_saved  # noqa: PLC0415
    from .excel_com import _bind_open_workbook, com_busy_reason  # noqa: PLC0415

    try:
        import pythoncom  # type: ignore  # noqa: PLC0415
        import win32com.client  # type: ignore  # noqa: PLC0415
    except ImportError as exc:
        raise ExcelBusyError("Excel COM is not available on this PC.") from exc
    file = Path(path)
    result = BookResult()
    pythoncom.CoInitialize()
    try:
        try:
            app, book = _bind_open_workbook(win32com.client, file)
        except Exception as exc:  # noqa: BLE001
            raise ExcelBusyError(f"{com_busy_reason(exc) or 'Excel could not be reached'}; spectrograms for {file.name} are added when Excel is free.") from exc
        if book is None:
            raise ExcelBusyError(f"{file.name} is not open in Excel any more.")
        try:
            if bool(book.ReadOnly):
                raise ExcelBusyError(f"{file.name} is open read-only in Excel; spectrograms wait until it is opened for editing or closed.")
            alerts = app.DisplayAlerts
            app.DisplayAlerts = False
        except ExcelBusyError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ExcelBusyError(f"{com_busy_reason(exc) or 'Excel refused the call'}; spectrograms for {file.name} are added when Excel is free.") from exc
        changed = False
        try:
            for sheet_name, group in _group_by_sheet(items).items():
                try:
                    sheet = book.Worksheets(sheet_name)
                except Exception as exc:  # noqa: BLE001
                    if com_busy_reason(exc):
                        raise
                    result.outcomes.extend(_outcome(item, "failed", f"worksheet {sheet_name!r} is not in {file.name}") for item in group)
                    continue
                changed |= _com_sheet(sheet, sheet_name, group, result, links)
            if changed or not _book_saved(book):
                book.Save()
                if not _book_saved(book):
                    raise ExcelBusyError(f"{file.name}: Excel did not confirm the save; tried again shortly.")
        except ExcelBusyError:
            raise
        except Exception as exc:  # noqa: BLE001
            reason = com_busy_reason(exc)
            if reason:
                # Pictures placed so far stay in the open workbook; the retry finds them by name and replaces them.
                raise ExcelBusyError(f"{reason}; spectrograms for {file.name} are added when Excel is free.") from exc
            raise
        finally:
            try:
                app.DisplayAlerts = alerts
            except Exception:  # noqa: BLE001
                pass
        return result
    finally:
        import gc  # noqa: PLC0415

        gc.collect()
        try:
            pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            pass


def _com_sheet(sheet, sheet_name: str, group: list[dict], result: BookResult, links) -> bool:
    try:
        if int(sheet.ProtectContents):
            result.outcomes.extend(_outcome(item, "waiting", f"{sheet_name!r} is protected") for item in group)
            return False
    except Exception:  # noqa: BLE001
        pass
    header_row = max(1, int(group[0].get("header_row") or 1))
    headers = _com_headers(sheet, header_row)
    iq_col = iq_column(headers, group[0])
    if not iq_col:
        result.outcomes.extend(_outcome(item, "waiting", f"no {group[0].get('iq_header') or HEADER_IQ!r} column in {sheet_name!r}") for item in group)
        return False
    last_row = _com_last_row(sheet)
    values = _com_column(sheet, header_row + 1, last_row, iq_col)
    targets: list[tuple[dict, int, list[str]]] = []
    for item in group:
        chosen = choose_row(match_rows(values, header_row + 1, str(item.get("file") or "")), int(item.get("row_hint") or 0))
        if chosen is None:
            result.outcomes.append(_outcome(item, "waiting", "its row is not in the sheet yet"))
            continue
        targets.append((item, chosen[0], chosen[1]))
    if not targets:
        return False
    col, created = _com_spectrogram_column(sheet, header_row, iq_col, last_row)
    if created:
        result.columns_created.append((sheet_name, col))
    changed = created
    rows: dict[str, tuple[int, list[str]]] = {}
    for offset, value in enumerate(_com_column(sheet, header_row + 1, last_row, iq_col)):
        names = split_cell_names(value)
        for name in names:
            rows.setdefault(name.casefold(), (header_row + 1 + offset, names))
    ours, others = _com_shapes(sheet)
    for item, _row, _names in targets:
        filename = str(item.get("file") or "")
        found = rows.get(filename.casefold())
        if found is None:
            result.outcomes.append(_outcome(item, "waiting", "its row is not in the sheet yet"))
            continue
        row, names = found
        thumb = Path(str(item.get("thumb") or ""))
        if not thumb.is_file():
            result.outcomes.append(_outcome(item, "failed", "the thumbnail file is missing"))
            continue
        link = links(Path(str(item.get("png") or "")))
        if link is None:
            result.outcomes.append(_outcome(item, "failed", "the full-size spectrogram is missing"))
            continue
        cell = sheet.Cells(row, col)
        text = cell.Value
        if others.get((row, col)) or (text not in (None, "") and str(text).strip()):
            result.outcomes.append(_outcome(item, "skipped", f"cell {row}/{col} already holds something else"))
            continue
        try:
            if bool(sheet.Rows(row).Hidden) or bool(sheet.Columns(col).Hidden):
                result.outcomes.append(_outcome(item, "waiting", "its row or the spectrogram column is hidden"))
                continue
        except Exception:  # noqa: BLE001
            pass
        aspect = png_aspect(thumb)
        index, count = part_slot(filename, names)
        need_w, need_h = cell_size_needed(count, aspect)
        grew_col, grew_row = _com_fit_cell(sheet, row, col, need_w, need_h)
        if grew_col:
            _com_relayout(sheet, ours, col, rows, aspect)
        elif grew_row:
            _com_relayout(sheet, ours, col, rows, aspect, only_row=row)
        name = shape_name(filename)
        old = ours.pop(name.casefold(), None)
        if old is not None:
            old.Delete()
        dx, dy, width, height = slot_geometry(index, count, aspect)
        shape = sheet.Shapes.AddPicture(str(thumb), 0, _MSO_TRUE, float(cell.Left) + dx, float(cell.Top) + dy, width, height)
        shape.Name = name
        shape.AlternativeText = alt_text(filename)
        shape.Placement = _XL_MOVE_AND_SIZE
        shape.LockAspectRatio = _MSO_TRUE
        sheet.Hyperlinks.Add(shape, link.address, "", link.tooltip)
        ours[name.casefold()] = shape
        # Siblings of a multi-part row keep their slots when the part count changed.
        _com_relayout(sheet, ours, col, rows, aspect, only_row=row)
        result.outcomes.append(_outcome(item, "written", link.address if link.shared else "", row, col))
        changed = True
    return changed


# ------------------------------------------------------------------ closed workbook (openpyxl)
def _chars_for_points(points: float) -> float:
    # Calibri 11: 7 px per character plus 5 px padding; 0.75 pt per px.
    return max(0.0, (points / 0.75 - 5.0) / 7.0)


def _points_for_chars(chars: float | None) -> float:
    chars = 8.43 if chars is None else float(chars)
    return (int(chars * 7.0 + 5.0)) * 0.75


def _px_width(ws, col: int) -> float:
    from openpyxl.utils import get_column_letter  # noqa: PLC0415

    dims = ws.column_dimensions.get(get_column_letter(col))
    width = getattr(dims, "width", None) if dims is not None else None
    if dims is not None and getattr(dims, "hidden", False):
        return 0.0
    return _points_for_chars(width if width else None)


def _row_height(ws, row: int) -> float:
    dims = ws.row_dimensions.get(row)
    height = getattr(dims, "ht", None) if dims is not None else None
    return float(height) if height else 15.0


def _anchor(row: int, col: int, index: int, count: int, aspect: float):
    from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor  # noqa: PLC0415

    dx, dy, width, height = slot_geometry(index, count, aspect)
    start = AnchorMarker(col=col - 1, colOff=int(dx * _EMU_PER_PT), row=row - 1, rowOff=int(dy * _EMU_PER_PT))
    end = AnchorMarker(col=col - 1, colOff=int((dx + width) * _EMU_PER_PT), row=row - 1, rowOff=int((dy + height) * _EMU_PER_PT))
    return TwoCellAnchor(editAs="twoCell", _from=start, to=end)


def _image_aspect(image) -> float:
    width, height = getattr(image, "width", 0) or 0, getattr(image, "height", 0) or 0
    return float(height) / float(width) if width and height else DEFAULT_ASPECT


def _anchor_cell(image) -> tuple[int, int] | None:
    anchor = getattr(image, "anchor", None)
    marker = getattr(anchor, "_from", None)
    if marker is None:
        return None
    return int(marker.row) + 1, int(marker.col) + 1


def _ws_table_covering(ws, row: int, col: int) -> bool:
    from openpyxl.utils.cell import range_boundaries  # noqa: PLC0415

    for table in ws.tables.values():
        try:
            min_col, min_row, max_col, max_row = range_boundaries(str(table.ref))
        except (TypeError, ValueError):
            return True
        if min_row <= row <= max_row and min_col <= col <= max_col:
            return True
    return False


def _ws_merged_at(ws, row: int, col: int) -> bool:
    return any(rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col for rng in ws.merged_cells.ranges)


def write_images_openpyxl(path: Path, items: list[dict], *, links: Callable[[Path], Link | None] = link_for) -> BookResult:
    """Place pictures in a closed workbook (atomic save). Raises DeferredError when that is not provably safe."""
    from openpyxl.drawing.image import Image as XLImage  # noqa: PLC0415

    from .openpyxl_links import closed_write_risks, load_workbook_keeping_links, picture_name, set_picture_link  # noqa: PLC0415

    file = Path(path)
    if file.suffix.casefold() not in {".xlsx", ".xlsm"}:
        raise DeferredError(f"{file.name} is not an .xlsx/.xlsm workbook; spectrograms are added when it is open in Excel.")
    risks = closed_write_risks(file)
    if risks:
        raise DeferredError(
            f"Spectrograms for {file.name} are added when it is open in Excel "
            f"(saving it closed could lose its {', '.join(risks[:3])})."
        )
    try:
        workbook = load_workbook_keeping_links(file, keep_vba=file.suffix.casefold() == ".xlsm")
    except PermissionError as exc:
        raise ExcelBusyError(f"Could not open {file.name}: another program has it open.") from exc
    result = BookResult()
    changed = False
    try:
        for sheet_name, group in _group_by_sheet(items).items():
            if sheet_name not in workbook.sheetnames:
                result.outcomes.extend(_outcome(item, "failed", f"worksheet {sheet_name!r} is not in {file.name}") for item in group)
                continue
            ws = workbook[sheet_name]
            if ws.protection.sheet:
                result.outcomes.extend(_outcome(item, "waiting", f"{sheet_name!r} is protected") for item in group)
                continue
            header_row = max(1, int(group[0].get("header_row") or 1))
            max_col = max(ws.max_column or 1, 1)
            headers = [ws.cell(row=header_row, column=c).value for c in range(1, max_col + 1)]
            iq_col = iq_column(headers, group[0])
            if not iq_col:
                result.outcomes.extend(_outcome(item, "waiting", f"no {group[0].get('iq_header') or HEADER_IQ!r} column in {sheet_name!r}") for item in group)
                continue
            last_row = max(ws.max_row or header_row, header_row)
            values = [ws.cell(row=r, column=iq_col).value for r in range(header_row + 1, last_row + 1)]
            rows: dict[str, tuple[int, list[str]]] = {}
            for offset, value in enumerate(values):
                names = split_cell_names(value)
                for name in names:
                    rows.setdefault(name.casefold(), (header_row + 1 + offset, names))
            pending = [item for item in group if str(item.get("file") or "").casefold() in rows]
            result.outcomes.extend(_outcome(item, "waiting", "its row is not in the sheet yet") for item in group if item not in pending)
            if not pending:
                continue
            neighbour_free = all(ws.cell(row=r, column=iq_col + 1).value in (None, "") for r in range(header_row + 1, last_row + 1))
            col, how = resolve_column(headers, iq_col, adjacent_free=neighbour_free)
            if how == "insert":
                raise DeferredError(
                    f"Spectrograms for {file.name} are added when it is open in Excel "
                    f"(a column has to be inserted right of {headers[iq_col - 1]!r})."
                )
            if _ws_table_covering(ws, header_row, iq_col) != _ws_table_covering(ws, header_row, col) or _ws_merged_at(ws, header_row, col):
                raise DeferredError(
                    f"Spectrograms for {file.name} are added when it is open in Excel (its table has to be widened)."
                )
            if how == "adjacent":
                source = ws.cell(row=header_row, column=iq_col)
                target = ws.cell(row=header_row, column=col)
                target.value = HEADER_SPECTROGRAM
                if source.has_style:
                    target._style = source._style
                result.columns_created.append((sheet_name, col))
                changed = True
                _extend_auto_filter(ws, col)
            ours: dict[str, Any] = {}
            others: dict[tuple[int, int], int] = {}
            for image in list(ws._images):
                name = picture_name(image)
                if file_of_shape(name) is not None:
                    ours[name.casefold()] = image
                else:
                    cell = _anchor_cell(image)
                    if cell is not None:
                        others[cell] = others.get(cell, 0) + 1
            for item in pending:
                filename = str(item.get("file") or "")
                row, names = rows[filename.casefold()]
                thumb = Path(str(item.get("thumb") or ""))
                if not thumb.is_file():
                    result.outcomes.append(_outcome(item, "failed", "the thumbnail file is missing"))
                    continue
                link = links(Path(str(item.get("png") or "")))
                if link is None:
                    result.outcomes.append(_outcome(item, "failed", "the full-size spectrogram is missing"))
                    continue
                if others.get((row, col)) or _ws_merged_at(ws, row, col) or ws.cell(row=row, column=col).value not in (None, ""):
                    result.outcomes.append(_outcome(item, "skipped", f"cell {row}/{col} already holds something else"))
                    continue
                dims = ws.row_dimensions.get(row)
                if dims is not None and dims.hidden:
                    result.outcomes.append(_outcome(item, "waiting", "its row is hidden"))
                    continue
                aspect = png_aspect(thumb)
                index, count = part_slot(filename, names)
                need_w, need_h = cell_size_needed(count, aspect)
                if _px_width(ws, col) + 0.5 < need_w:
                    from openpyxl.utils import get_column_letter  # noqa: PLC0415

                    ws.column_dimensions[get_column_letter(col)].width = min(MAX_COLUMN_CHARS, round(_chars_for_points(need_w) + 0.75, 2))
                if _row_height(ws, row) + 0.5 < need_h:
                    ws.row_dimensions[row].height = min(MAX_ROW_HEIGHT_PT, need_h)
                name = shape_name(filename)
                old = ours.pop(name.casefold(), None)
                if old is not None:
                    ws._images.remove(old)
                image = XLImage(str(thumb))
                image.anchor = _anchor(row, col, index, count, aspect)
                set_picture_link(image, link.uri, tooltip=link.tooltip, name=name, descr=alt_text(filename))
                ws._images.append(image)
                ours[name.casefold()] = image
                result.outcomes.append(_outcome(item, "written", link.address if link.shared else "", row, col))
                changed = True
            # Every picture of ours in the column back at its slot (part counts or the cell may have changed).
            for image in ours.values():
                filename = file_of_shape(picture_name(image)) or ""
                found = rows.get(filename.casefold())
                cell = _anchor_cell(image)
                if found is None or cell is None or cell[1] != col:
                    continue
                index, count = part_slot(filename, found[1])
                image.anchor = _anchor(found[0], col, index, count, _image_aspect(image))
        if changed:
            temp = file.with_name(f"{file.stem}.crfs-tmp-{os.getpid()}{file.suffix}")
            try:
                workbook.save(temp)
                os.replace(temp, file)
            except PermissionError as exc:
                raise ExcelBusyError(f"Could not save {file.name}: another program has it open.") from exc
            finally:
                try:
                    if temp.exists():
                        temp.unlink()
                except OSError:
                    pass
        return result
    finally:
        vba = getattr(workbook, "vba_archive", None)
        if vba is not None:
            try:
                vba.close()
            except Exception:  # noqa: BLE001
                pass
        workbook.close()


# ------------------------------------------------------------------ flush
@dataclass
class ImageFlushReport:
    written: list[ImageOutcome] = field(default_factory=list)
    waiting: int = 0
    busy: str = ""
    deferred: list[str] = field(default_factory=list)  # full reasons (diagnostic log)
    deferred_books: list[str] = field(default_factory=list)  # workbook names, for the Activity line
    errors: list[str] = field(default_factory=list)
    given_up: list[dict] = field(default_factory=list)
    columns_created: list[tuple[str, str, int]] = field(default_factory=list)  # (workbook, sheet, column)
    backups: list[str] = field(default_factory=list)
    workbooks: list[str] = field(default_factory=list)
    pending: int = 0


def flush_images(
    queue,
    *,
    excel_state: Callable[[Path], str] | None = None,
    backup: Callable[[Path], Path | None] | None = None,
    links: Callable[[Path], Link | None] = link_for,
    row_queued: Callable[[dict], bool] | None = None,
    com_writer: Callable[..., BookResult] | None = None,
    closed_writer: Callable[..., BookResult] | None = None,
) -> ImageFlushReport:
    """Place every pending picture whose row is in its workbook. Runs on a worker; never raises.

    Same rules as the row writers: through the running Excel when the workbook
    is open there, closed files only when that is safe, and nothing while Excel
    cannot be asked (busy / cell edit) - the pictures stay queued and are retried.
    """
    from .cellular_workbook import BackupError, _norm_text  # noqa: PLC0415
    from .excel_com import BUSY, OPEN, workbook_lock  # noqa: PLC0415
    from .openpyxl_links import closed_write_risks  # noqa: PLC0415

    report = ImageFlushReport()
    items = queue.pending_images()
    if not items:
        return report
    if excel_state is None:
        from . import excel_com  # noqa: PLC0415

        excel_state = excel_com.excel_workbook_state
    com_writer = com_writer or write_images_com
    closed_writer = closed_writer or write_images_openpyxl
    by_book: dict[str, list[dict]] = {}
    for item in items:
        if row_queued is not None and row_queued(item):
            report.waiting += 1
            continue
        by_book.setdefault(_norm_text(str(item.get("workbook") or "")), []).append(item)
    for group in by_book.values():
        file = Path(str(group[0].get("workbook") or ""))
        if not file.is_file():
            note = f"workbook not found: {file.name}"
            report.errors.append(f"Spectrograms: {note}.")
            report.given_up += queue.mark_images([_outcome(item, "failed", note) for item in group])
            continue
        try:
            with workbook_lock(file):
                state = excel_state(file)
                if state == BUSY:
                    raise ExcelBusyError(f"Excel is busy (a cell is being edited or a dialog is open); spectrograms for {file.name} are added when it is free.")
                if state != OPEN:
                    risks = closed_write_risks(file)
                    if risks:
                        raise DeferredError(
                            f"Spectrograms for {file.name} are added when it is open in Excel "
                            f"(saving it closed could lose its {', '.join(risks[:3])})."
                        )
                if backup is not None:
                    made = backup(file)
                    if made is not None:
                        report.backups.append(str(made))
                writer = com_writer if state == OPEN else closed_writer
                result = writer(file, group, links=links)
        except ExcelBusyError as exc:
            report.busy = str(exc)
            continue
        except DeferredError as exc:
            report.deferred.append(str(exc))
            report.deferred_books.append(file.name)
            queue.mark_images([_outcome(item, "waiting", str(exc)) for item in group])
            continue
        except BackupError as exc:
            report.errors.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 - keep the pictures queued; bounded retries
            note = f"{type(exc).__name__}: {exc}"[:300]
            report.errors.append(f"Spectrograms for {file.name}: {note}")
            report.given_up += queue.mark_images([_outcome(item, "failed", note) for item in group])
            continue
        written = [o for o in result.outcomes if o.status == "written"]
        report.written.extend(written)
        report.waiting += sum(1 for o in result.outcomes if o.status == "waiting")
        report.columns_created.extend((str(file), sheet, col) for sheet, col in result.columns_created)
        if written and str(file) not in report.workbooks:
            report.workbooks.append(str(file))
        report.given_up += queue.mark_images(result.outcomes)
    report.pending = queue.image_count()
    return report


def _extend_auto_filter(ws, col: int) -> None:
    from openpyxl.utils import get_column_letter  # noqa: PLC0415
    from openpyxl.utils.cell import range_boundaries  # noqa: PLC0415

    ref = str(getattr(ws.auto_filter, "ref", "") or "")
    if not ref or ws.auto_filter.filterColumn or ws.auto_filter.sortState is not None:
        return
    try:
        min_col, min_row, max_col, max_row = range_boundaries(ref)
    except (TypeError, ValueError):
        return
    if max_col + 1 == col:
        ws.auto_filter.ref = f"{get_column_letter(min_col)}{min_row}:{get_column_letter(col)}{max_row}"
