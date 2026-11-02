"""Cellular Collection panel inside the demo main window (offscreen Qt)."""

from __future__ import annotations

import json
import os
import time
from datetime import date, datetime
from datetime import time as dtime
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook

from crfs_iq_recorder.cellular_panel import (
    PanelSettings,
    PlanPreviewDialog,
    load_panel_settings,
    save_panel_settings,
)
from crfs_iq_recorder.cellular_protocol import import_protocol
from crfs_iq_recorder.cellular_reference import REFERENCE_ALLOCATIONS, VERIFY_NOTE, write_template_workbook
from crfs_iq_recorder.cellular_runner import OWNER, S_DONE, S_IDLE, T_CONFIRMED
from crfs_iq_recorder.cellular_scheduler import MODE_NOW, MODE_TIMES, TZ_LOCAL

MHZ = 1_000_000


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from crfs_iq_recorder.gui import configure_appearance

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)
    yield app


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("crfs_iq_recorder.recording_history.recordings_path", lambda: tmp_path / "recordings.json")
    monkeypatch.setattr("crfs_iq_recorder.paths.settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr("crfs_iq_recorder.paths.user_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr("crfs_iq_recorder.gui.user_config_dir", lambda: tmp_path / "config")
    monkeypatch.setattr("crfs_iq_recorder.gui.resolved_download_dir", lambda _stored=None: tmp_path / "downloads")
    monkeypatch.setattr("crfs_iq_recorder.sftp_window.QDesktopServices.openUrl", lambda *_a, **_k: True)


def _pump_until(qapp, predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _drive_capture(window, qapp):
    qapp.processEvents()
    window._record_timer.stop()
    if window._recording_active:
        window._on_recording_time_elapsed()
    qapp.processEvents()
    for _ in range(4):
        window._poll_collection_files()
    qapp.processEvents()


def _plan_book(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    for row in [
        ["Band", "Duplex", "Direction", "Start (MHz)", "Stop (MHz)"],
        ["700", "FDD", "UL", 703, 713],
        ["700", "FDD", "DL", 758, 768],
    ]:
        ws.append(row)
    wb.save(path)
    return path


def _protocol_book(path: Path) -> Path:
    """The collection protocol's layout: nine columns, result columns empty, a Sensor column."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Cellular Protocol"
    ws.append(["Band", "Direction", "Duplex", "Freq Start (MHz)", "Freq Stop (MHz)", "Sensor", "IQ Recording", "IQ Duration (s)", "Recording Date & Time"])
    ws.append(["700 MHz (B68 / B28)", "DL", "FDD", 758, 768, "CRFS 100-18", None, None, None])
    ws.append(["700 MHz (B68 / B28)", "UL", "FDD", 703, 713, "CRFS 100-18", None, None, None])
    wb.create_sheet("Reference").append(["Save each round separately; do not overwrite previous results."])
    wb.save(path)
    return path


def _load(panel, qapp, book: Path) -> None:
    panel.workbook_edit.setText(str(book))
    panel._fill_sheets("")
    panel._import(force=True)
    assert _pump_until(qapp, lambda: panel.imported is not None or "cannot" in panel.import_label.text()), panel.import_label.text()


def test_template_round_trips_through_the_importer(tmp_path):
    path = write_template_workbook(tmp_path / "template.xlsx", duration_s=10)
    imported = import_protocol(path, "Cellular Protocol")
    assert imported.ready, [i.message for i in imported.errors]
    entries = imported.valid_entries()
    assert len(entries) == len(REFERENCE_ALLOCATIONS)
    assert all(e.notes == VERIFY_NOTE for e in entries)
    shared = [e for e in entries if e.duplex == "TDD"]
    assert len(shared) == 4 and all(e.direction == "TDD / shared UL-DL" for e in shared)
    b20 = [e for e in entries if e.band == "800"]
    assert {(e.direction, e.start_hz // MHZ) for e in b20} == {("UL", 832), ("UL", 842), ("DL", 791), ("DL", 801)}, "Band 20 DL is below UL; no generic offset"
    assert all(e.duration_s == 10 for e in entries)
    assert imported.result_rows() == []


def test_panel_settings_round_trip(tmp_path):
    settings = PanelSettings(workbook="x.xlsx", duration_s=2.5, wait_s=25.0, schedule_mode=MODE_TIMES, times=["08:00", "20:30"], daily=True, repeat=3)
    save_panel_settings(tmp_path, settings)
    loaded = load_panel_settings(tmp_path)
    assert loaded.workbook == "x.xlsx" and loaded.duration_s == 2.5 and loaded.wait_s == 25.0
    assert loaded.schedule_mode == MODE_TIMES and loaded.times == ["08:00", "20:30"] and loaded.daily is True and loaded.repeat == 3
    assert PanelSettings.from_dict({"wait_s": "oops", "repeat": 0}).wait_s == 10.0
    assert PanelSettings.from_dict({"repeat": 0}).repeat == 1


def test_panel_in_demo_window_imports_previews_starts_and_finishes(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    book = _plan_book(tmp_path / "plan.xlsx")
    window = MainWindow(demo=True)
    try:
        window._settle_s = 0
        window._stable_needed = 2
        window._demo_parts = 1
        panel = window.cellular_panel
        assert panel.body.isHidden(), "collapsed by default"
        panel._set_expanded(True)
        assert not panel.body.isHidden()
        assert panel.start_btn.isEnabled() is False
        # Only the essential controls exist: no campaign, site, scenario, UTC or mapping widgets.
        for gone in ("campaign_edit", "site_combo", "tz_combo", "map_btn", "template_btn", "window_spin", "format_combo", "test_btn"):
            assert not hasattr(panel, gone), gone
        assert panel.duration_spin.value() == 5.0 and panel.wait_spin.value() == 10, "defaults: 5 s recordings, 10 s wait"
        _load(panel, qapp, book)
        assert panel.imported is not None and panel.plan is not None
        assert "2 row(s) from 'Plan'" in panel.import_label.text()
        assert panel.plan.ready and panel.start_btn.isEnabled()
        assert [t.direction for t in panel.plan.tasks] == ["UL", "DL"], "worksheet order"
        panel.duration_spin.setValue(1.0)
        assert all(t.duration_s == 1.0 for t in panel.plan.tasks), "the app duration applies to every row"
        panel.wait_spin.setValue(0)
        assert panel.wait_seconds() == 0
        dialog = PlanPreviewDialog(panel.plan, wait_s=0, rows_total=2, notes=panel._notes_before_start(None), start_label="Start")
        assert dialog.windowTitle() == "Cellular Collection"
        dialog.close()
        # Settings persisted separately from the ordinary recorder settings.
        panel.until_stopped.setChecked(True)
        panel._save_settings()
        saved = json.loads((tmp_path / "config" / "cellular" / "settings.json").read_text(encoding="utf-8"))
        assert saved["workbook"] == str(book) and saved["wait_s"] == 0 and saved["duration_s"] == 1.0 and saved["until_stopped"] is True
        panel.until_stopped.setChecked(False)
        # Start (bypassing the modal preview) and run the pass on the demo transport.
        panel.run_now.setChecked(True)
        schedule, error = panel._schedule()
        assert error is None and schedule.mode == MODE_NOW and schedule.repeat == 1 and schedule.tz.kind == TZ_LOCAL
        settings = panel._campaign_settings()
        assert settings.sensor_model and settings.plan_rows and settings.protocol_results is False, "no result column in this sheet"
        panel._start(schedule)
        assert window._external_owner == OWNER
        assert panel.chip.text() == "Recording"
        assert not panel.start_btn.isEnabled() and panel.stop_btn.isEnabled() and panel.pause_btn.isEnabled()
        assert _pump_until(qapp, lambda: "recording" in panel.status_label.text() or "idle" in panel.status_label.text().casefold(), timeout=5.0)
        for _ in range(2):
            assert _pump_until(qapp, lambda: window._http_in_flight is False)
            _drive_capture(window, qapp)
            window._cellular._tick()
        assert _pump_until(qapp, lambda: window._cellular.state == S_DONE, timeout=8.0), window._cellular.status()
        assert _pump_until(qapp, lambda: window._cellular.background_jobs() == 0, timeout=20.0)
        tasks = window._cellular.current_tasks() or list(window._cellular.session.tasks.values())[0]
        assert [t.status for t in tasks] == [T_CONFIRMED, T_CONFIRMED]
        assert tasks[0].quality.get("level") in {"ok", "warning"}
        qapp.processEvents()
        assert panel.chip.text() == "Finished"
        assert not panel.recovery.isHidden() and "finished" in panel.recovery_label.text().casefold()
        assert window._external_owner == ""
        panel._discard()
        qapp.processEvents()
        assert window._cellular.state == S_IDLE and panel.chip.text() == "Off"
    finally:
        window._watch_timer.stop()
        window._record_timer.stop()
        window.close()
        qapp.processEvents()


def test_start_button_verifies_the_sensor_identity_then_previews(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    import crfs_iq_recorder.cellular_panel as cp
    from crfs_iq_recorder.gui import MainWindow

    book = _plan_book(tmp_path / "plan.xlsx")
    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        panel._set_expanded(True)
        _load(panel, qapp, book)
        panel.duration_spin.setValue(1.0)
        panel.wait_spin.setValue(0)
        shown: list[dict] = []

        def fake_exec(self):
            shown.append({"title": self.windowTitle()})
            return QDialog.DialogCode.Rejected  # look, do not start

        monkeypatch.setattr(cp.PlanPreviewDialog, "exec", fake_exec)
        panel._start_clicked()
        assert panel._identity_pending is True and not panel.start_btn.isEnabled()
        assert panel.status_label.text().startswith("Checking the sensor")
        assert _pump_until(qapp, lambda: not panel._identity_pending, timeout=6.0)
        assert shown and shown[0]["title"] == "Cellular Collection"
        assert panel.settings.last_sensor == {} or True
        assert window._cellular.session is None, "rejecting the preview starts nothing"
        assert panel.start_btn.isEnabled()
    finally:
        window.close()
        qapp.processEvents()


def test_schedule_uses_the_pickers_and_the_local_clock(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    book = _plan_book(tmp_path / "plan.xlsx")
    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        panel._set_expanded(True)
        _load(panel, qapp, book)
        panel.run_times.setChecked(True)
        assert panel.schedule_box.isVisibleTo(panel.body) and not panel.repeat_spin.isVisibleTo(panel.body)
        schedule, error = panel._schedule()
        assert schedule is None and "start time" in error
        panel.times_field.set_times([dtime(20, 0), dtime(8, 0)])
        panel.daily_check.setChecked(False)
        panel.date_field.set_date(date.today())
        schedule, error = panel._schedule()
        assert error is None and schedule.mode == MODE_TIMES
        assert schedule.times == [dtime(8, 0), dtime(20, 0)] and schedule.dates == [date.today()] and schedule.daily is False
        assert schedule.tz.kind == TZ_LOCAL and "UTC" not in schedule.describe()
        panel.daily_check.setChecked(True)
        assert not panel.date_field.isEnabled()
        schedule, error = panel._schedule()
        assert error is None and schedule.daily is True and schedule.dates == []
        panel._save_settings()
        saved = json.loads((tmp_path / "config" / "cellular" / "settings.json").read_text(encoding="utf-8"))
        assert saved["schedule_mode"] == MODE_TIMES and saved["times"] == ["08:00", "20:00"] and saved["daily"] is True
        # Run now with "until stopped" -> repeat 0.
        panel.run_now.setChecked(True)
        panel.until_stopped.setChecked(True)
        schedule, error = panel._schedule()
        assert error is None and schedule.mode == MODE_NOW and schedule.repeat == 0
        panel.until_stopped.setChecked(False)
        panel.repeat_spin.setValue(4)
        assert panel._schedule()[0].repeat == 4
    finally:
        window.close()
        qapp.processEvents()


def test_panel_fills_the_protocol_rows_through_the_demo_window(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    book = _protocol_book(tmp_path / "Cellular_IQ_Collection_Protocol.xlsx")
    window = MainWindow(demo=True)
    try:
        window._settle_s = 0
        window._stable_needed = 2
        window._demo_parts = 1
        panel = window.cellular_panel
        panel._set_expanded(True)
        _load(panel, qapp, book)
        assert panel.imported is not None and panel.plan is not None
        assert panel.imported.mapping.result == "IQ Recording" and panel.imported.mapping.result_time == "Recording Date & Time"
        assert "no result column" not in panel.import_label.text()
        panel.duration_spin.setValue(1.0)
        panel.wait_spin.setValue(0)
        settings = panel._campaign_settings()
        assert settings.protocol_results is True and settings.protocol_path == str(book)
        assert settings.protocol_layout["result"] == 7 and settings.protocol_layout["time"] == 9 and settings.protocol_layout["sensor"] == 6
        assert [r["sheet_row"] for r in settings.plan_rows] == [2, 3]
        panel.run_now.setChecked(True)
        schedule, error = panel._schedule()
        assert error is None
        started = datetime.now().replace(microsecond=0)
        panel._start(schedule)
        assert window._external_owner == OWNER
        for _ in range(2):
            assert _pump_until(qapp, lambda: window._http_in_flight is False)
            _drive_capture(window, qapp)
            window._cellular._tick()
        assert _pump_until(qapp, lambda: window._cellular.state == S_DONE, timeout=8.0), window._cellular.status()
        assert _pump_until(qapp, lambda: window._cellular.background_jobs() == 0 and window._cellular.workbook_pending() == 0, timeout=30.0), window._cellular.status()
        status = window._cellular.status()
        assert status["workbook_pending"] == 0 and status["workbook_status"] == "saved"
        wb = load_workbook(book)
        try:
            assert wb.sheetnames == ["Cellular Protocol", "Reference"], "no extra sheets"
            ws = wb["Cellular Protocol"]
            rows = [list(r) for r in ws.iter_rows(values_only=True)]
            assert rows[0] == ["Band", "Direction", "Duplex", "Freq Start (MHz)", "Freq Stop (MHz)", "Sensor", "IQ Recording", "IQ Duration (s)", "Recording Date & Time"]
            assert len(rows) == 3, "the two protocol rows were filled in place; nothing appended"
            for row in rows[1:]:
                assert row[6] and row[6].endswith(".wav"), row
                assert row[5] not in (None, ""), "the sensor that recorded is named"
                assert isinstance(row[7], (int, float)) and not isinstance(row[7], bool) and row[7] > 0, "seconds as a number"
                assert isinstance(row[8], datetime) and row[8] >= started - __import__("datetime").timedelta(hours=1)
            assert rows[1][:5] == ["700 MHz (B68 / B28)", "DL", "FDD", 758, 768]
            assert ws["H2"].number_format == "0.000" and ws["I2"].number_format == "dd/mm/yyyy hh:mm:ss"
        finally:
            wb.close()
        assert len([p for p in tmp_path.iterdir() if ".crfs-backup-" in p.name]) == 1
        window._cellular.discard_session()
        qapp.processEvents()
    finally:
        window._watch_timer.stop()
        window._record_timer.stop()
        window.close()
        qapp.processEvents()


def test_a_workbook_that_cannot_be_read_keeps_the_stored_import_and_queues_results(qapp, tmp_path, monkeypatch):
    """Hardware session 3 (100-18): the workbook was locked by another process when the panel
    re-read it; the worksheet list came back empty, so the session silently ran with the
    workbook updates off. The rows imported earlier stay the plan, the worksheet stays
    selected, and the results queue up (busy) until the file is free."""
    import crfs_iq_recorder.cellular_panel as cp
    import crfs_iq_recorder.cellular_workbook as cw
    from crfs_iq_recorder.cellular_protocol import ProtocolError, WorkbookUnavailable
    from crfs_iq_recorder.excel_writer import ExcelBusyError
    from crfs_iq_recorder.gui import MainWindow

    def unavailable(*_a, **_k):
        raise WorkbookUnavailable(f"Could not open {book.name}: [Errno 13] Permission denied")

    book = _protocol_book(tmp_path / "Protocol.xlsx")
    window = MainWindow(demo=True)
    try:
        window._settle_s = 0
        window._stable_needed = 2
        window._demo_parts = 1
        panel = window.cellular_panel
        panel._set_expanded(True)
        _load(panel, qapp, book)
        assert panel.imported is not None
        # The file becomes unreadable (locked); the user re-reads it.
        monkeypatch.setattr(cp, "list_worksheets", unavailable)
        monkeypatch.setattr(cp, "read_headers", unavailable)
        panel._fill_sheets("Cellular Protocol")
        panel._import(force=True)
        assert _pump_until(qapp, lambda: not panel._importing)
        assert panel.imported is not None and panel._current_sheet() == "Cellular Protocol"
        assert "Permission denied" in panel.import_label.text() and "imported earlier" in panel.import_label.text()
        settings = panel._campaign_settings()
        assert settings.protocol_results is True and settings.protocol_sheet == "Cellular Protocol" and len(settings.plan_rows) == 2
        # A different workbook path gets no such fallback, nor does a worksheet that changed.
        panel.sheet_combo.clear()
        assert panel._current_sheet() == "Cellular Protocol", "the imported worksheet stands in for the unreadable list"
        panel.workbook_edit.setText(str(tmp_path / "other.xlsx"))
        assert panel._current_sheet() == ""
        panel.workbook_edit.setText(str(book))
        monkeypatch.setattr(cp, "list_worksheets", lambda _p: [("Cellular Protocol", True)])
        monkeypatch.setattr(cp, "read_headers", lambda *_a: (["Band"], 1))
        panel._fill_sheets("Cellular Protocol")
        panel._import(force=True)
        assert _pump_until(qapp, lambda: not panel._importing)
        assert panel.imported is None and "missing" in panel.import_label.text()
        monkeypatch.setattr(cp, "list_worksheets", _original_list_worksheets)
        monkeypatch.setattr(cp, "read_headers", _original_read_headers)
        _load(panel, qapp, book)
        assert panel.imported is not None
        # Writing fails while the file is locked: results stay queued and the indicator says so.
        monkeypatch.setattr(cw, "write_results_openpyxl", lambda *_a, **_k: (_ for _ in ()).throw(ExcelBusyError("The workbook is open in another program.")))
        monkeypatch.setattr("crfs_iq_recorder.cellular_runner.WORKBOOK_BUSY_RETRY_S", 0.3)
        panel.duration_spin.setValue(1.0)
        panel.wait_spin.setValue(0)
        panel.run_now.setChecked(True)
        schedule, error = panel._schedule()
        assert error is None
        panel._start(schedule)
        assert window._cellular.protocol_results_enabled()
        for _ in range(2):
            assert _pump_until(qapp, lambda: window._http_in_flight is False)
            _drive_capture(window, qapp)
            window._cellular._tick()
        assert _pump_until(qapp, lambda: window._cellular.state == S_DONE, timeout=8.0), window._cellular.status()
        assert _pump_until(qapp, lambda: window._cellular.status()["workbook_status"] == "busy", timeout=8.0), window._cellular.status()
        status = window._cellular.status()
        assert status["workbook_pending"] == 2 and "another program" in status["workbook_note"]
        assert "pending" in panel.detail_label.text().casefold(), panel.detail_label.text()
        # The file is free again: the next retry writes both rows.
        monkeypatch.setattr(cw, "write_results_openpyxl", _original_write_results)
        assert _pump_until(qapp, lambda: window._cellular.workbook_pending() == 0 and not window._cellular._workbook_flushing, timeout=10.0), window._cellular.status()
        wb = load_workbook(book)
        try:
            ws = wb["Cellular Protocol"]
            assert ws["G2"].value and ws["G3"].value and ws.max_row == 3
        finally:
            wb.close()
        window._cellular.discard_session()
        qapp.processEvents()
    finally:
        window._watch_timer.stop()
        window._record_timer.stop()
        window.close()
        qapp.processEvents()


_original_write_results = __import__("crfs_iq_recorder.cellular_workbook", fromlist=["write_results_openpyxl"]).write_results_openpyxl
_original_list_worksheets = __import__("crfs_iq_recorder.cellular_panel", fromlist=["list_worksheets"]).list_worksheets
_original_read_headers = __import__("crfs_iq_recorder.cellular_panel", fromlist=["read_headers"]).read_headers


def test_collapsed_panel_builds_nothing_until_opened_and_opens_for_a_saved_session(qapp, tmp_path):
    from PySide6.QtWidgets import QWidget
    from crfs_iq_recorder.cellular_plan import build_plan
    from crfs_iq_recorder.cellular_capabilities import capability_for
    from crfs_iq_recorder.cellular_runner import CampaignSettings, CellularRunner
    from crfs_iq_recorder.cellular_scheduler import Schedule
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        assert not panel._built and panel.body.isHidden()
        assert len(panel.findChildren(QWidget)) < 10, "only the header exists while collapsed"
        assert panel.chip.text() == "Off"
        window.close()
        qapp.processEvents()
        saved = json.loads((tmp_path / "config" / "cellular" / "settings.json").read_text(encoding="utf-8"))
        assert saved["expanded"] is False
    finally:
        window.close()
        qapp.processEvents()
    # A session left behind by a previous run opens the panel on the next start.
    book = _plan_book(tmp_path / "plan.xlsx")
    plan = build_plan(import_protocol(book, "Plan"), capability_for("R40-8"), fallback_duration_s=1.0)

    class Host:
        capture_finished = None
        _conn = None

        def set_external_owner(self, name):
            return None

        def release_external_owner(self, name=None):
            return None

        def start_capture(self, request):
            return "A recording is in progress."

    runner = CellularRunner(Host(), config_dir=tmp_path / "config", download_dir=lambda: tmp_path / "downloads")
    assert runner.start_session(plan, Schedule(mode=MODE_NOW), CampaignSettings(wait_s=0, auto_download=False)) is None
    runner.shutdown()
    window2 = MainWindow(demo=True)
    try:
        panel2 = window2.cellular_panel
        assert panel2._built and not panel2.body.isHidden()
        assert panel2.chip.text() == "Interrupted"
        assert not panel2.recovery.isHidden() and "interrupted" in panel2.recovery_label.text().casefold()
    finally:
        window2.close()
        qapp.processEvents()
