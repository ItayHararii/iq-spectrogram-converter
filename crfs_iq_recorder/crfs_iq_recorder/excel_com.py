"""Write collection rows through a running Excel instance when the workbook is open."""

from __future__ import annotations

from pathlib import Path

from .excel_writer import ExcelBusyError, ExcelRow, format_playback_duration
from .frequency import format_freq_text
from .recording_history import join_iq_filenames, split_iq_filenames
from .collection_catalog import (
    HEADER_CLASS,
    HEADER_COLLECTION,
    HEADER_IQ,
    HEADER_PLAY_END,
    HEADER_PLAY_START,
    HEADER_PLAY_TIME,
    HEADER_SENSOR,
    HEADER_START,
    HEADER_STOP,
    REQUIRED_HEADERS,
)

XL_PASTE_FORMATS = -4122


def _norm(path: Path) -> str:
    return str(path.resolve()).casefold()


def excel_has_workbook(path: Path) -> bool:
    try:
        import win32com.client  # type: ignore
    except ImportError:
        return False
    try:
        app = win32com.client.GetActiveObject("Excel.Application")
    except Exception:
        return False
    want = _norm(path)
    try:
        for book in app.Workbooks:
            try:
                if _norm(Path(str(book.FullName))) == want:
                    return True
            except Exception:
                continue
    except Exception:
        return False
    return False


def _header_map(sheet) -> dict[str, int]:
    found: dict[str, int] = {}
    last_col = int(sheet.UsedRange.Columns.Count)
    for col in range(1, last_col + 1):
        value = sheet.Cells(1, col).Value
        text = str(value).strip() if value is not None else ""
        if text and text not in found:
            found[text] = col
    return found


def _find_row_with_names(sheet, headers: dict[str, int], names: list[str]) -> int | None:
    iq_col = headers.get(HEADER_IQ)
    if not iq_col:
        return None
    wanted = {name.casefold() for name in names if name}
    if not wanted:
        return None
    last = int(sheet.UsedRange.Rows.Count)
    for row in range(2, last + 1):
        existing = split_iq_filenames(sheet.Cells(row, iq_col).Value)
        if any(name.casefold() in wanted for name in existing):
            return row
    return None


def _last_used_row(sheet, columns: int) -> int:
    last = 1
    used = int(sheet.UsedRange.Rows.Count)
    for row in range(1, used + 1):
        for col in range(1, columns + 1):
            if sheet.Cells(row, col).Value not in (None, ""):
                last = row
                break
    return last


def append_rows_com(path: Path, rows: list[ExcelRow]) -> list[int]:
    if not rows:
        return []
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
    except ImportError as exc:
        raise ExcelBusyError("Excel COM is not available on this PC.") from exc
    pythoncom.CoInitialize()
    started = False
    app = None
    try:
        try:
            app = win32com.client.GetActiveObject("Excel.Application")
        except Exception:
            app = win32com.client.Dispatch("Excel.Application")
            started = True
        want = _norm(path)
        book = None
        for item in app.Workbooks:
            try:
                if _norm(Path(str(item.FullName))) == want:
                    book = item
                    break
            except Exception:
                continue
        if book is None:
            if not started:
                # Excel is open with other files. Do not load a second copy.
                raise ExcelBusyError(f"{path.name} is not open in Excel and Excel is busy.")
            app.Visible = False
            book = app.Workbooks.Open(str(path.resolve()))
        written: list[int] = []
        alerts = app.DisplayAlerts
        app.DisplayAlerts = False
        try:
            for row in rows:
                try:
                    sheet = book.Worksheets(row.worksheet)
                except Exception as exc:
                    raise ValueError(f"Workbook has no worksheet named {row.worksheet!r}.") from exc
                headers = _header_map(sheet)
                missing = [name for name in REQUIRED_HEADERS if name not in headers]
                if missing:
                    raise ValueError(f"Worksheet {row.worksheet!r} is missing columns: {', '.join(missing)}")
                columns = max(headers.values())
                incoming = split_iq_filenames(row.filename) or [row.filename]
                found = _find_row_with_names(sheet, headers, incoming)
                if found:
                    dest = found
                    existing = split_iq_filenames(sheet.Cells(dest, headers[HEADER_IQ]).Value)
                    merged = join_iq_filenames(existing + incoming)
                else:
                    last = _last_used_row(sheet, columns)
                    dest = last + 1
                    merged = join_iq_filenames(incoming)
                    if last >= 1:
                        sheet.Rows(last).Copy()
                        sheet.Rows(dest).PasteSpecial(Paste=XL_PASTE_FORMATS)
                        try:
                            app.CutCopyMode = False
                        except Exception:
                            pass
                    def write(header: str, value) -> None:
                        sheet.Cells(dest, headers[header]).Value = value

                    write(HEADER_COLLECTION, row.collection_event)
                    write(HEADER_CLASS, row.target_class)
                    write(HEADER_START, format_freq_text(row.start_hz, row.start_style))
                    write(HEADER_STOP, format_freq_text(row.end_hz, row.stop_style))
                    write(HEADER_SENSOR, row.sensor)
                    if HEADER_PLAY_END in headers:
                        sheet.Cells(dest, headers[HEADER_PLAY_END]).Value = None
                    if HEADER_PLAY_TIME in headers:
                        play_cell = sheet.Cells(dest, headers[HEADER_PLAY_TIME])
                        play_cell.Value = None
                        try:
                            play_cell.Formula = ""
                        except Exception:
                            pass
                iq_cell = sheet.Cells(dest, headers[HEADER_IQ])
                iq_cell.Value = merged
                try:
                    iq_cell.WrapText = True
                except Exception:
                    pass
                try:
                    sheet.Rows(dest).RowHeight = max(18, 16 * max(1, merged.count("\n") + 1) + 4)
                except Exception:
                    pass
                local = Path(row.local_path) if row.local_path else None
                if "\n" not in merged and local is not None and local.is_file():
                    try:
                        sheet.Hyperlinks.Add(
                            Anchor=iq_cell,
                            Address=str(local.resolve()),
                            TextToDisplay=merged,
                        )
                    except Exception:
                        pass
                if HEADER_PLAY_START in headers and row.duration_s is not None:
                    start_cell = sheet.Cells(dest, headers[HEADER_PLAY_START])
                    start_cell.NumberFormat = "General"
                    start_cell.Value = format_playback_duration(row.duration_s)
                    try:
                        start_cell.ClearComments()
                    except Exception:
                        pass
                try:
                    if bool(sheet.AutoFilterMode):
                        sheet.AutoFilterMode = False
                    sheet.Range(f"A1:{_col_letter(columns)}{dest}").AutoFilter()
                except Exception:
                    pass
                written.append(dest)
            book.Save()
        finally:
            app.DisplayAlerts = alerts
        if started and book is not None:
            book.Close(SaveChanges=True)
            app.Quit()
        return written
    except ExcelBusyError:
        raise
    except Exception as exc:
        message = str(exc).lower()
        busy = any(token in message for token in ("locked", "access", "deny", "busy", "in use", "permission"))
        if busy:
            raise ExcelBusyError(f"Excel could not save {path.name} right now.") from exc
        raise
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _col_letter(index: int) -> str:
    out = ""
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"
