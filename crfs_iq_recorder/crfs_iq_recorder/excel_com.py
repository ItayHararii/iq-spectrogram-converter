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

# COM HRESULTs that mean "Excel cannot take the call right now" (not "the write is wrong"):
# a modal dialog or a cell being edited rejects every automation call until the user is done.
RPC_E_CALL_REJECTED = 0x80010001
RPC_E_SERVERCALL_RETRYLATER = 0x8001010A
RPC_E_SERVERCALL_REJECTED = 0x80010100
RPC_E_DISCONNECTED = 0x80010108
RPC_S_SERVER_UNAVAILABLE = 0x800706BA
VBA_E_IGNORE = 0x800AC472  # Excel is in cell edit mode (or shows a dialog)
COM_BUSY_HRESULTS = {
    RPC_E_CALL_REJECTED,
    RPC_E_SERVERCALL_RETRYLATER,
    RPC_E_SERVERCALL_REJECTED,
    RPC_E_DISCONNECTED,
    RPC_S_SERVER_UNAVAILABLE,
    VBA_E_IGNORE,
}
_BUSY_TEXT = ("locked", "access", "deny", "busy", "in use", "permission", "rejected", "retry", "edit mode", "unavailable")

OPEN = "open"
CLOSED = "closed"
BUSY = "busy"


def _norm(path: Path) -> str:
    return str(path.resolve()).casefold()


def _hresults_of(exc: BaseException) -> set[int]:
    """Every HRESULT a pywin32 com_error carries (its own and the one in EXCEPINFO)."""
    codes: set[int] = set()
    for value in (getattr(exc, "hresult", None), *(exc.args[:1] if exc.args else ())):
        if isinstance(value, int):
            codes.add(value & 0xFFFFFFFF)
    if len(exc.args) >= 3 and isinstance(exc.args[2], (tuple, list)) and len(exc.args[2]) >= 6:
        scode = exc.args[2][5]
        if isinstance(scode, int):
            codes.add(scode & 0xFFFFFFFF)
    return codes


def com_busy_reason(exc: BaseException) -> str | None:
    """Why the COM call must be retried later, or None when the error is not a busy state.

    Excel refuses automation calls while a cell is being edited or a dialog is
    open (RPC_E_CALL_REJECTED 0x80010001, RPC_E_SERVERCALL_RETRYLATER
    0x8001010A, 0x800AC472), and a closing instance answers with
    RPC_E_DISCONNECTED / RPC_S_SERVER_UNAVAILABLE. These are "wait and try
    again" states: the results stay queued.
    """
    codes = _hresults_of(exc)
    if codes & {VBA_E_IGNORE}:
        return "Excel is busy (a cell is being edited or a dialog is open)"
    if codes & {RPC_E_CALL_REJECTED, RPC_E_SERVERCALL_RETRYLATER, RPC_E_SERVERCALL_REJECTED}:
        return "Excel rejected the call (busy)"
    if codes & {RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE}:
        return "the Excel instance went away (closing or restarting)"
    text = str(exc).casefold()
    if any(token in text for token in _BUSY_TEXT):
        return "Excel is busy or the file is locked"
    return None


def excel_workbook_state(path: Path) -> str:
    """'open' when the running Excel holds the workbook, 'closed' when it does not, 'busy' when Excel could not be asked.

    'busy' is distinct from 'closed' on purpose: a workbook may well be open
    in an Excel that is in cell edit mode, and writing the file with openpyxl
    meanwhile would race the open instance. Callers treat 'busy' as "wait".
    """
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
    except ImportError:
        return CLOSED
    if not _excel_window_exists():
        return CLOSED
    # Callers may run on a worker thread that never initialised COM.
    try:
        pythoncom.CoInitialize()
    except Exception:
        pass
    try:
        try:
            app = win32com.client.GetActiveObject("Excel.Application")
        except Exception as exc:
            return BUSY if com_busy_reason(exc) else CLOSED
        want = _norm(path)
        try:
            for book in app.Workbooks:
                try:
                    if _norm(Path(str(book.FullName))) == want:
                        return OPEN
                except Exception as exc:
                    if com_busy_reason(exc):
                        return BUSY
                    continue
        except Exception as exc:
            return BUSY if com_busy_reason(exc) else CLOSED
        return CLOSED
    finally:
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


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
    """True when the running Excel instance holds this workbook (see excel_workbook_state)."""
    return excel_workbook_state(path) == OPEN


def activate_workbook(path: Path) -> bool:
    """Bring this workbook to the front when the running Excel holds it; False when it is not open.

    Used by "Open written Excel file": a workbook that Excel already has must
    not be opened a second time (Excel would refuse or open it read-only).
    """
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
    except ImportError:
        return False
    if not _excel_window_exists():
        return False
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
                    if _norm(Path(str(book.FullName))) != want:
                        continue
                    book.Activate()
                    try:
                        app.Visible = True
                        window = app.ActiveWindow
                        if window is not None and int(window.WindowState) == -4140:  # xlMinimized
                            window.WindowState = -4143  # xlNormal
                    except Exception:
                        pass
                    try:
                        import ctypes

                        ctypes.windll.user32.SetForegroundWindow(int(app.Hwnd))
                    except Exception:
                        pass
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
                template_height = 0.0
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
                        try:
                            # PasteSpecial(formats) does not carry the row height.
                            template_height = float(sheet.Rows(last).RowHeight or 0)
                        except Exception:
                            template_height = 0.0
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
                    # One line per part, never shorter than the sheet's own
                    # rows (the row it was copied from may be taller).
                    current = max(template_height, float(sheet.Rows(dest).RowHeight or 0) if found else 0.0)
                    sheet.Rows(dest).RowHeight = max(18, 16 * max(1, merged.count("\n") + 1) + 4, current)
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
            if not _saved(book):
                raise ExcelBusyError(f"Excel did not confirm that {path.name} was saved; the rows are written again when it is.")
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
        reason = com_busy_reason(exc)
        if reason:
            raise ExcelBusyError(f"Excel could not take the update for {path.name} right now: {reason}.") from exc
        raise
    finally:
        sheet = None
        book = None
        app = None


def add_worksheet_com(path: Path, sheet: str, headers: list[str]) -> bool:
    """Create `sheet` with `headers` in row 1 through the Excel that has the workbook open, then save.

    Returns True when the sheet exists afterwards (already there or created
    and saved), False when the running Excel does not hold the workbook.
    Raises ExcelBusyError when Excel cannot take the call right now or did
    not confirm the save; the caller keeps the work queued.
    """
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore
    except ImportError as exc:
        raise ExcelBusyError("Excel COM is not available on this PC.") from exc
    if not _excel_window_exists():
        return False
    pythoncom.CoInitialize()
    try:
        return _add_worksheet_com_impl(win32com.client, path, sheet, headers)
    finally:
        import gc

        gc.collect()
        try:
            pythoncom.CoUninitialize()
        except Exception:
            pass


def _add_worksheet_com_impl(win32com_client, path: Path, sheet: str, headers: list[str]) -> bool:
    want = _norm(path)
    app = None
    book = None
    try:
        try:
            app = win32com_client.GetActiveObject("Excel.Application")
        except Exception as exc:
            if com_busy_reason(exc):
                raise ExcelBusyError(f"Excel could not be asked about {path.name} right now: {com_busy_reason(exc)}.") from exc
            return False
        for item in app.Workbooks:
            try:
                if _norm(Path(str(item.FullName))) == want:
                    book = item
                    break
            except Exception:
                continue
        if book is None:
            return False
        for existing in book.Worksheets:
            try:
                if str(existing.Name).strip().casefold() == sheet.strip().casefold():
                    return True
            except Exception:
                continue
        alerts = app.DisplayAlerts
        app.DisplayAlerts = False
        try:
            count = int(book.Worksheets.Count)
            new_sheet = book.Worksheets.Add(After=book.Worksheets(count))
            new_sheet.Name = sheet
            if headers:
                new_sheet.Range(new_sheet.Cells(1, 1), new_sheet.Cells(1, len(headers))).Value = [list(headers)]
                try:
                    new_sheet.Rows(1).Font.Bold = True
                except Exception:
                    pass
            book.Save()
            if not _saved(book):
                raise ExcelBusyError(f"Excel did not confirm that {path.name} was saved; the results sheet is created again when it is.")
        finally:
            app.DisplayAlerts = alerts
        return True
    except ExcelBusyError:
        raise
    except Exception as exc:
        reason = com_busy_reason(exc)
        if reason:
            raise ExcelBusyError(f"Excel could not add the results sheet to {path.name} right now: {reason}.") from exc
        raise
    finally:
        book = None
        app = None


def _saved(book) -> bool:
    """Excel's own word that the workbook has no unsaved changes (Workbook.Saved)."""
    try:
        return bool(book.Saved)
    except Exception:
        return False


def _col_letter(index: int) -> str:
    out = ""
    n = index
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out or "A"
