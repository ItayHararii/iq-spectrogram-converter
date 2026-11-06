"""The protocol workbook is found in whichever Excel instance has it open, by its full path.

GetActiveObject("Excel.Application") returns one instance only. With a second
Excel running, the workbook open in the other one looked closed, so the open
window was never written and openpyxl would have been asked to rewrite the file
behind it. Every instance registers each workbook it opens in the running-object
table under its full path; binding goes through that.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from crfs_iq_recorder import excel_com
from crfs_iq_recorder.excel_com import excel_workbook_state as real_workbook_state  # before conftest replaces it
from crfs_iq_recorder.excel_writer import ExcelBusyError


class _Book:
    def __init__(self, full_name: str, app=None, read_only: bool = False):
        self.FullName = full_name
        self.Application = app
        self.ReadOnly = read_only


class _App:
    def __init__(self, name: str, books: list[str]):
        self.name = name
        self.Workbooks = [_Book(b, self) for b in books]


class _Moniker:
    def __init__(self, name: str, obj=None):
        self.name = name
        self.obj = obj

    def GetDisplayName(self, _ctx, _other):  # noqa: N802 - COM name
        return self.name


class _Unknown:
    def __init__(self, obj):
        self.obj = obj

    def QueryInterface(self, _iid):  # noqa: N802
        return self.obj


class _Rot:
    def __init__(self, monikers):
        self.monikers = monikers

    def __iter__(self):
        return iter(self.monikers)

    def GetObject(self, moniker):  # noqa: N802
        return _Unknown(moniker.obj)


def _fake_pythoncom(monkeypatch, monikers):
    module = types.SimpleNamespace(
        GetRunningObjectTable=lambda: _Rot(monikers),
        CreateBindCtx=lambda _n: object(),
        IID_IDispatch=object(),
        CoInitialize=lambda: None,
        CoUninitialize=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "pythoncom", module)
    return module


class _Client:
    def __init__(self, active: _App | None, error: str = "no such instance"):
        self.active = active
        self.asked_active = 0
        self.error = error

    def GetActiveObject(self, _progid):  # noqa: N802
        self.asked_active += 1
        if self.active is None:
            raise OSError(self.error)
        return self.active

    def Dispatch(self, obj):  # noqa: N802
        return obj


def test_binds_the_workbook_in_the_second_excel_instance(monkeypatch, tmp_path):
    ours = tmp_path / "Real Cellular protocol.xlsx"
    first = _App("first", [str(tmp_path / "other.xlsx")])
    second = _App("second", [str(ours)])
    _fake_pythoncom(monkeypatch, [
        _Moniker("!{00024500-0000-0000-C000-000000000046}", first),
        _Moniker(str(tmp_path / "other.xlsx"), first.Workbooks[0]),
        _Moniker(str(ours).upper(), second.Workbooks[0]),
    ])
    client = _Client(first)
    app, book = excel_com._bind_open_workbook(client, ours)  # noqa: SLF001
    assert app is second and book is second.Workbooks[0]
    assert client.asked_active == 0


def test_urls_and_class_monikers_are_never_taken_for_the_file(monkeypatch, tmp_path):
    ours = tmp_path / "book.xlsx"
    active = _App("active", [str(ours)])
    _fake_pythoncom(monkeypatch, [
        _Moniker("https://example.invalid/sites/x/book.xlsx", object()),
        _Moniker("!{00024500-0000-0000-C000-000000000046}", object()),
    ])
    app, book = excel_com._bind_open_workbook(_Client(active), ours)  # noqa: SLF001
    assert app is active and book is active.Workbooks[0], "falls back to the active instance"


def test_not_open_anywhere_binds_nothing(monkeypatch, tmp_path):
    _fake_pythoncom(monkeypatch, [])
    assert excel_com._bind_open_workbook(_Client(_App("a", [])), tmp_path / "x.xlsx") == (None, None)  # noqa: SLF001
    assert excel_com._bind_open_workbook(_Client(None), tmp_path / "x.xlsx") == (None, None)  # noqa: SLF001
    # MK_E_UNAVAILABLE while an Excel window exists: not registered yet (until it loses focus) -> retried.
    with pytest.raises(OSError):
        excel_com._bind_open_workbook(_Client(None, "Operation unavailable"), tmp_path / "x.xlsx")  # noqa: SLF001


@pytest.fixture
def state_env(monkeypatch):
    monkeypatch.setattr(excel_com, "_excel_window_exists", lambda: True)
    locked: dict[str, bool] = {"value": False}
    monkeypatch.setattr(excel_com, "file_locked", lambda _p: locked["value"])
    fake_client = _Client(_App("active", []))
    win32com = types.ModuleType("win32com")
    win32com.client = fake_client  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", fake_client)
    return locked, fake_client


def test_state_is_open_when_any_instance_registered_the_file(monkeypatch, tmp_path, state_env):
    ours = tmp_path / "book.xlsx"
    _fake_pythoncom(monkeypatch, [_Moniker(str(ours), object())])
    _locked, client = state_env
    assert real_workbook_state(ours) == "open"
    assert client.asked_active == 0, "reading the table's names never calls into an Excel instance"


def test_state_is_busy_when_the_file_is_locked_but_no_excel_shows_it(monkeypatch, tmp_path, state_env):
    ours = tmp_path / "book.xlsx"
    ours.write_bytes(b"x")
    _fake_pythoncom(monkeypatch, [])
    locked, _client = state_env
    locked["value"] = True
    assert real_workbook_state(ours) == "busy", "a locked file is never rewritten with openpyxl"
    locked["value"] = False
    assert real_workbook_state(ours) == "closed"


def test_file_locked_sees_a_handle_that_denies_writing(tmp_path):
    if sys.platform != "win32":
        pytest.skip("Windows share modes")
    import ctypes
    from ctypes import wintypes

    target = tmp_path / "book.xlsx"
    target.write_bytes(b"PK")
    assert excel_com.file_locked(target) is False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    # Excel keeps an open workbook like this: others may read, nobody may write.
    handle = kernel32.CreateFileW(str(target), 0x80000000, 0x00000001, None, 3, 0, None)
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        assert excel_com.file_locked(target) is True
        assert target.read_bytes() == b"PK"
    finally:
        kernel32.CloseHandle(handle)
    assert excel_com.file_locked(target) is False
    assert excel_com.file_locked(tmp_path / "missing.xlsx") is False


class _EditingBook:
    """A workbook whose Excel is in cell-edit mode: pywin32 raises AttributeError('<unknown>.FullName')."""

    def __getattr__(self, name):
        raise AttributeError(f"<unknown>.{name}")


def test_an_excel_in_cell_edit_mode_is_busy_and_the_operator_line_is_plain(monkeypatch, tmp_path):
    """Hardware, 6 Nov 2026: the write during cell-edit mode stayed queued, but the Activity line read
    "Excel could not be reached for C118_protocol.xlsx: <unknown>.FullName"."""
    from operator_rules import problems

    from crfs_iq_recorder import cellular_workbook

    ours = tmp_path / "book.xlsx"
    _fake_pythoncom(monkeypatch, [_Moniker(str(ours), _EditingBook())])
    with pytest.raises(excel_com.ExcelRefused) as caught:
        excel_com._bind_open_workbook(_Client(None), ours)  # noqa: SLF001
    assert excel_com.com_busy_reason(caught.value) == "Excel is busy (a cell is being edited or a dialog is open)"
    win32com = types.ModuleType("win32com")
    win32com.client = _Client(None)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", win32com.client)
    with pytest.raises(ExcelBusyError) as busy:
        cellular_workbook.write_results_com(ours, layout=None, plan=[], results=[], blocks={})
    text = str(busy.value)
    assert text == "Excel is busy (a cell is being edited or a dialog is open); book.xlsx is written when Excel is free."
    from crfs_iq_recorder.cellular_runner import _excel_wait_line

    # Hardware, 6 Nov 2026 (build B): the line was cut mid-word, "... is written when Exc…. Results stay queued."
    for reason in (text, text.replace("book.xlsx", "Real Cellular protocol.xlsx")):
        line = _excel_wait_line(reason, "Results stay queued.")
        assert line == reason, line
        assert problems(line) == [], problems(line)
    assert _excel_wait_line("Excel is busy", "Results stay queued.") == "Waiting for Excel: Excel is busy. Results stay queued."


def test_an_unexpected_binding_failure_keeps_its_detail_out_of_the_operator_line(monkeypatch, tmp_path):
    from crfs_iq_recorder import cellular_workbook

    def broken(_client, _path):
        raise ValueError("internal detail 0x1234")

    monkeypatch.setattr(cellular_workbook, "_bind_open_workbook", broken)
    win32com = types.ModuleType("win32com")
    win32com.client = _Client(None)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "win32com", win32com)
    monkeypatch.setitem(sys.modules, "win32com.client", win32com.client)
    _fake_pythoncom(monkeypatch, [])
    with pytest.raises(ExcelBusyError) as busy:
        cellular_workbook.write_results_com(tmp_path / "book.xlsx", layout=None, plan=[], results=[], blocks={})
    assert str(busy.value) == "Excel could not be reached for book.xlsx"


def test_read_only_workbook_keeps_the_results_queued(monkeypatch, tmp_path):
    from crfs_iq_recorder import cellular_workbook

    book = _Book(str(tmp_path / "book.xlsx"), read_only=True)
    monkeypatch.setattr(cellular_workbook, "_bind_open_workbook", lambda _client, _path: (object(), book))
    with pytest.raises(ExcelBusyError, match="read-only"):
        cellular_workbook.write_results_com(tmp_path / "book.xlsx", layout=None, plan=[], results=[], blocks={})
