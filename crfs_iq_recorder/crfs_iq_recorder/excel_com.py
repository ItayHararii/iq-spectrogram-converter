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
XL_FORMULAS = -4123
XL_PART = 2
XL_BY_ROWS = 1
XL_PREVIOUS = 2


def _norm(path: Path) -> str:
    return str(path.resolve()).casefold()


def _excel_window_exists() -> bool:
    """True when a live Excel process owns a main window (class XLMAIN).

    GetActiveObject on a running-object-table entry whose process has just
    exited blocks for about a minute before failing. A dead process has no
    windows, so this check skips that hang.
    """
    try:
        import ctypes

        return bool(ctypes.windll.user32.FindWindowW("XLMAIN", None))
    except Exception:
        return True


def excel_has_workbook(path: Path) -> bool:
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
    except ImportError:
        return False
    if not _excel_window_exists():
        return False
    # Callers may run on a worker thread that never initialised COM.
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass
    try:
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
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _range_values(sheet, first_row: int, first_col: int, last_row: int, last_col: int) -> list[list]:
    """Read a block with one COM call. Cell-by-cell access costs ~50 µs per cell."""
    if last_row < first_row or last_col < first_col:
        return []
    value = sheet.Range(sheet.Cells(first_row, first_col), sheet.Cells(last_row, last_col)).Value
    if first_row == last_row and first_col == last_col:
        return [[value]]
    if first_row == last_row:
        return [list(value[0]) if isinstance(value, tuple) and value and isinstance(value[0], tuple) else list(value)]
    rows: list[list] = []
    for item in value:
        rows.append(list(item) if isinstance(item, tuple) else [item])
    return rows


def _header_map(sheet) -> dict[str, int]:
    found: dict[str, int] = {}
    last_col = int(sheet.UsedRange.Columns.Count)
    header = _range_values(sheet, 1, 1, 1, last_col)
    values = header[0] if header else []
    for index, value in enumerate(values, start=1):
        text = str(value).strip() if value is not None else ""
        if text and text not in found:
            found[text] = index
    return found


def _last_used_row(sheet, columns: int) -> int:
    """Last row with a value. UsedRange can reach row 1,048,576 after stray formatting.

    Walking UsedRange cell by cell over COM takes minutes on such a sheet and
    blocks the UI. Excel's own Find (searching backwards from A1) returns the
    last non-empty cell in one call.
    """
    try:
        found = sheet.Cells.Find(
            What="*",
            After=sheet.Cells(1, 1),
            LookIn=XL_FORMULAS,
            LookAt=XL_PART,
            SearchOrder=XL_BY_ROWS,
            SearchDirection=XL_PREVIOUS,
            MatchCase=False,
        )
    except Exception:
        found = None
    if found is not None:
        try:
            return max(1, int(found.Row))
        except Exception:
            pass
    # Fallback for a sheet without values or a failed Find: bounded walk.
    last = 1
    used = min(int(sheet.UsedRange.Rows.Count), 5000)
    block = _range_values(sheet, 1, 1, used, max(1, columns))
    for index, row in enumerate(block, start=1):
        if any(value not in (None, "") for value in row):
            last = index
    return last


def _find_row_with_names(sheet, headers: dict[str, int], names: list[str]) -> int | None:
    iq_col = headers.get(HEADER_IQ)
    if not iq_col:
        return None
    wanted = {name.casefold() for name in names if name}
    if not wanted:
        return None
    last = _last_used_row(sheet, max(headers.values()))
    if last < 2:
        return None
    column = _range_values(sheet, 2, iq_col, last, iq_col)
    for offset, row in enumerate(column, start=2):
        existing = split_iq_filenames(row[0] if row else None)
        if any(name.casefold() in wanted for name in existing):
            return offset
    return None


def append_rows_com(path: Path, rows: list[ExcelRow]) -> list[int]:
    if not rows:
        return []
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
    except ImportError as exc:
        raise ExcelBusyError("Excel COM is not available on this PC.") from exc
    pythoncom.CoInitialize()
    quit_pid = 0
    try:
        written, quit_pid = _append_rows_com_impl(win32com.client, path, rows)
        return written
    finally:
        # _append_rows_com_impl has returned, so every COM proxy it held is out
        # of scope. Uninitialising with live proxies raises RPC_E_DISCONNECTED
        # (0x80010108) inside the COM runtime.
        import gc

        gc.collect()
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass
        if quit_pid:
            _wait_for_process_exit(quit_pid, HEADLESS_EXIT_WAIT_S)


HEADLESS_EXIT_WAIT_S = 15.0


def _process_id_of_app(app) -> int:
    """PID of an Excel.Application via its window handle (0 when unknown)."""
    try:
        import ctypes

        hwnd = int(app.Hwnd)
        if not hwnd:
            return 0
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def _wait_for_process_exit(pid: int, timeout_s: float) -> bool:
    """Block until a process we told to quit has really exited.

    Excel stays registered in the running-object table until it exits. If the
    next flush attaches to that dying instance, its calls hang for a minute or
    the instance is revived and left running invisibly with the workbook open.
    """
    try:
        import ctypes

        SYNCHRONIZE = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
        if not handle:
            return True
        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(handle, int(max(timeout_s, 0) * 1000))
            return result == 0
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return False


def _append_rows_com_impl(win32com_client, path: Path, rows: list[ExcelRow]) -> tuple[list[int], int]:
    """Write rows; return (row numbers, pid of an instance that was told to quit or 0)."""
    started = False
    headless = False
    app = None
    book = None
    sheet = None
    quit_pid = 0
    want = _norm(path)
    try:
        app = None
        if _excel_window_exists():
            try:
                app = win32com_client.GetActiveObject("Excel.Application")
            except Exception:
                app = None
        if app is not None:
            try:
                headless = not bool(app.Visible)
            except Exception:
                headless = False
            if headless:
                # Invisible instances are ones this app started. Use one only
                # when it still holds our workbook (it would keep the file
                # locked otherwise); never attach to one that is shutting down.
                holds_book = False
                try:
                    for item in app.Workbooks:
                        try:
                            if _norm(Path(str(item.FullName))) == want:
                                holds_book = True
                                break
                        except Exception:
                            continue
                except Exception:
                    holds_book = False
                if not holds_book:
                    app = None
        if app is None:
            app = win32com_client.Dispatch("Excel.Application")
            started = True
            headless = False
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
        if (started or headless) and book is not None:
            book.Close(SaveChanges=True)
            book = None
            try:
                remaining = int(app.Workbooks.Count)
            except Exception:
                remaining = 0
            if remaining == 0:
                quit_pid = _process_id_of_app(app)
                app.Quit()
        return written, quit_pid
    except ExcelBusyError:
        raise
    except Exception as exc:
        message = str(exc).lower()
        busy = any(token in message for token in ("locked", "access", "deny", "busy", "in use", "permission"))
        if busy:
            raise ExcelBusyError(f"Excel could not save {path.name} right now.") from exc
        raise
    finally:
        sheet = None
        book = None
        app = None


def _col_letter(index: int) -> str:
    out = ""
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"
