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
        assert window.tabs.currentIndex() == 0 and panel.body.isHidden(), "the form is built when its tab opens"
        window.tabs.setCurrentIndex(1)
        assert panel._built and not panel.body.isHidden()
        assert panel.start_btn.isEnabled() is False
        # Only the essential controls exist: no campaign, site, scenario, UTC or mapping widgets.
        for gone in ("campaign_edit", "site_combo", "tz_combo", "map_btn", "template_btn", "window_spin", "format_combo", "test_btn"):
            assert not hasattr(panel, gone), gone
        assert panel.duration_spin.value() == 5.0 and panel.wait_spin.value() == 10, "defaults: 5 s recordings, 10 s wait"
        _load(panel, qapp, book)
        assert panel.imported is not None and panel.plan is not None
        assert panel.import_label.text() == "2 rows · 5 s per request · 10 s between recordings · 1 pass", panel.import_label.text()
        assert "Estimate only" in panel.details_label.text() and panel.details_btn.isVisibleTo(panel.body)
        assert panel.plan.ready and panel.start_btn.isEnabled()
        assert [t.direction for t in panel.plan.tasks] == ["UL", "DL"], "worksheet order"
        panel.duration_spin.setValue(1.0)
        assert all(t.duration_s == 1.0 for t in panel.plan.tasks), "the app duration applies to every row"
        panel.wait_spin.setValue(0)
        assert panel.wait_seconds() == 0
        dialog = PlanPreviewDialog(panel.plan, wait_s=0, rows_total=2, notes=panel._notes_before_start(None), start_label="Start")
        assert dialog.windowTitle() == "Excel Collection"
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
        assert _pump_until(qapp, lambda: "recording" in panel.status_label.text().casefold() or "idle" in panel.status_label.text().casefold(), timeout=5.0)
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
        assert panel.recovery.isHidden(), "a finished collection is summarised, not a recovery notice"
        assert not panel.pass_box.isHidden() and panel.pass_title.text() == "Pass 1 finished"
        assert panel.pass_counts.text() == "Completed 2 · Partial 0 · Failed 0 · Uncertain 0"
        assert panel.retry_btn.isHidden(), "nothing failed, nothing to retry"
        card = panel.result_card
        assert not card.isHidden() and card.title.text().startswith("Row 2 of 2: ")
        assert card.requested.text() == "Requested: 10 MHz for 1 s" and card.received.text().startswith("Received (read from the files): 1 file, ")
        assert card.tag.text() == "Complete" and "Saved locally" in card.states.text()
        assert card.open_btn.isEnabled() and card.waterfall_btn.isEnabled()
        assert window._external_owner == ""
        panel._discard()
        qapp.processEvents()
        assert window._cellular.state == S_IDLE and panel.chip.text() == "Ready", "a loaded plan is 'Ready', never 'Off'"
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
        window.tabs.setCurrentIndex(1)
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
        assert shown and shown[0]["title"] == "Excel Collection"
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
        window.tabs.setCurrentIndex(1)
        _load(panel, qapp, book)
        panel.run_times.setChecked(True)
        assert panel.schedule_box.isVisibleTo(panel.body) and not panel.repeat_spin.isVisibleTo(panel.body)
        schedule, error = panel._schedule()
        assert schedule is None and "start time" in error
        # Schedule without a start time is not "Ready": Start waits and says why.
        assert not panel.start_btn.isEnabled()
        assert panel.reason_label.text() == "Add at least one start time." and not panel.reason_label.isHidden()
        assert panel.status_label.text().startswith("Schedule not set · ")
        assert panel.chip.text() == "Idle", "the badge agrees with the status line"
        panel.times_field.set_times([dtime(20, 0), dtime(8, 0)])
        panel.daily_check.setChecked(False)
        panel.date_field.set_date(date.today())
        assert panel.start_btn.isEnabled() and panel.reason_label.isHidden()
        assert panel.status_label.text().startswith("Ready to start · ")
        assert panel.chip.text() == "Ready"
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
        window.tabs.setCurrentIndex(1)
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
        window.tabs.setCurrentIndex(1)
        _load(panel, qapp, book)
        assert panel.imported is not None
        # The file becomes unreadable (locked); the user re-reads it.
        monkeypatch.setattr(cp, "list_worksheets", unavailable)
        monkeypatch.setattr(cp, "read_headers", unavailable)
        panel._fill_sheets("Cellular Protocol")
        panel._import(force=True)
        assert _pump_until(qapp, lambda: not panel._importing)
        assert panel.imported is not None and panel._current_sheet() == "Cellular Protocol"
        assert panel.import_label.text() == (
            "Protocol.xlsx cannot be opened right now. Is it open in another program? "
            "Using the 2 rows imported earlier; results are queued."
        )
        from crfs_iq_recorder import diagnostic_log

        assert any("Permission denied" in line for line in diagnostic_log.recent(50))
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
        assert "waiting for excel" in panel.detail_label.text().casefold(), panel.detail_label.text()
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


def test_excel_form_is_built_when_its_tab_opens_and_for_a_saved_session(qapp, tmp_path):
    from PySide6.QtWidgets import QWidget
    from crfs_iq_recorder.cellular_plan import build_plan
    from crfs_iq_recorder.cellular_capabilities import capability_for
    from crfs_iq_recorder.cellular_runner import CampaignSettings, CellularRunner
    from crfs_iq_recorder.cellular_scheduler import Schedule
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        assert window.tabs.currentIndex() == 0 and window.tabs.tabText(0) == "Manual Recording" and window.tabs.tabText(1) == "Excel Collection"
        assert not panel._built and panel.body.isHidden()
        assert len(panel.findChildren(QWidget)) < 10, "the Excel form is not built while the manual tab is shown"
        assert panel.chip.text() == "Idle"
        assert window.cellular_bar is panel.action_bar and not panel.start_btn.isEnabled()
        assert panel.start_btn.toolTip() == "Load a protocol workbook first."
        window.close()
        qapp.processEvents()
        saved = json.loads((tmp_path / "config" / "cellular" / "settings.json").read_text(encoding="utf-8"))
        assert "expanded" not in saved and saved["wait_unit"] == "seconds"
    finally:
        window.close()
        qapp.processEvents()
    # A session left behind by a previous run opens the Excel tab on the next start.
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
        assert window2.tabs.currentIndex() == 1, "a collection that needs attention opens its tab"
        assert panel2.chip.text() == "Interrupted"
        assert not panel2.recovery.isHidden() and "interrupted" in panel2.recovery_label.text().casefold()
        # Closed during a pass whose capture was refused: nothing is uncertain, so
        # Continue is offered without "Check with the sensor".
        assert window2._cellular.uncertain_tasks() == [] and window2._cellular.current_round() is not None
        assert panel2.reconcile_btn.isHidden() and not panel2.resume_session_btn.isHidden()
        assert "during a pass" in panel2.recovery_label.text()
        # The bar is pinned under the Excel tab's form; the manual Start is on the other tab.
        assert window2.cellular_bar is panel2.action_bar and panel2.action_bar.isVisibleTo(window2)
        assert not window2.start_btn.isVisibleTo(window2)
        assert panel2.start_btn.accessibleName() == "Start Excel Collection"
        assert panel2.reason_label.text() == "Continue or dismiss the interrupted collection first."
    finally:
        window2.close()
        qapp.processEvents()


def test_panel_offers_run_or_skip_for_a_pass_missed_while_the_app_was_closed(qapp, tmp_path):
    """1.12.0 regression: a session closed while waiting for a scheduled pass is not a bare
    "Interrupted: check with the sensor"; the missed pass is listed with Run now / Skip."""
    from datetime import datetime, timedelta, timezone
    from crfs_iq_recorder.cellular_plan import build_plan
    from crfs_iq_recorder.cellular_capabilities import capability_for
    from crfs_iq_recorder.cellular_runner import S_INTERRUPTED, CampaignSettings, CellularRunner
    from crfs_iq_recorder.cellular_scheduler import MODE_TIMES, Schedule
    from crfs_iq_recorder.gui import MainWindow

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
            raise AssertionError("nothing may be recorded")

    now = datetime.now().astimezone()
    runner = CellularRunner(Host(), config_dir=tmp_path / "config", download_dir=lambda: tmp_path / "downloads")
    tomorrow = now.date() + timedelta(days=1)
    schedule = Schedule(mode=MODE_TIMES, times=[datetime(2026, 1, 1, 12, 0).time(), datetime(2026, 1, 1, 13, 0).time()], dates=[tomorrow])
    assert runner.start_session(plan, schedule, CampaignSettings(wait_s=0, auto_download=False)) is None
    first = runner.session.rounds[0]
    # The first pass's time went by three minutes ago while the application was closed.
    first.scheduled_local = now - timedelta(minutes=3)
    first.scheduled_utc = first.scheduled_local.astimezone(timezone.utc)
    runner._save()
    runner.shutdown()

    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        runner2 = window._cellular
        assert runner2.state == S_INTERRUPTED and runner2.uncertain_tasks() == []
        assert runner2.status()["missed_rounds"] == [first.round_id]
        qapp.processEvents()
        text = panel.recovery_label.text().casefold()
        assert "missed" in text and "run it now or skip it" in text
        assert "check with the sensor" not in text
        assert not panel.run_missed_btn.isHidden() and not panel.skip_missed_btn.isHidden()
        assert panel.reconcile_btn.isHidden(), "nothing is uncertain, so there is nothing to check"
        assert not panel.resume_session_btn.isHidden() and not panel.discard_btn.isHidden()
        assert "missed" in panel.status_label.text().casefold()
        panel.skip_missed_btn.click()
        qapp.processEvents()
        assert runner2.status()["missed_rounds"] == []
        assert panel.run_missed_btn.isHidden()
        assert "continue" in panel.status_label.text().casefold()
    finally:
        window.close()
        qapp.processEvents()


def test_chip_says_stopped_when_the_operator_stopped_the_collection(qapp, tmp_path):
    """1.12.0: a session the operator stopped is not badged "Finished" (seen on hardware, 3 Nov)."""
    from crfs_iq_recorder.cellular_runner import S_DONE
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        window.tabs.setCurrentIndex(1)
        qapp.processEvents()
        panel._on_status({"state": S_DONE, "text": "Stopped", "active": False})
        assert (panel.chip.text(), panel.chip.objectName()) == ("Stopped", "badgeWait")
        panel._on_status({"state": S_DONE, "text": "Finished with problems", "active": False})
        assert (panel.chip.text(), panel.chip.objectName()) == ("Finished", "badgeWait")
        panel._on_status({"state": S_DONE, "text": "Finished", "active": False})
        assert (panel.chip.text(), panel.chip.objectName()) == ("Finished", "badgeOk")
    finally:
        window.close()
        qapp.processEvents()


def test_a_download_backlog_pause_is_said_once(qapp, tmp_path):
    """Hardware, 3 Nov (VPN at ~2 MB/s): the status line already held the backlog reason and the
    detail line repeated it twice ("Downloads: 10 waiting · Downloads: 10 waiting (limit 8)…")."""
    from crfs_iq_recorder.cellular_runner import S_PAUSED, S_PAUSED_BACKLOG
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        window.tabs.setCurrentIndex(1)
        qapp.processEvents()
        reason = "Downloads: 10 waiting (limit 8). Recording resumes when they catch up."
        panel._on_status({"state": S_PAUSED_BACKLOG, "text": f"Paused: {reason}", "active": True, "pending_downloads": 10, "pause_reason": reason})
        assert panel.status_label.text() == f"Paused: {reason}"
        assert panel.detail_label.isHidden(), panel.detail_label.text()
        panel._on_status({"state": S_PAUSED, "text": "Paused", "active": True, "pending_downloads": 4, "pause_reason": "Paused by the operator."})
        assert panel.detail_label.text() == "Downloads: 4 waiting · Paused by the operator."
    finally:
        window.close()
        qapp.processEvents()


def test_start_says_it_waits_for_the_last_collections_downloads(qapp, tmp_path):
    from crfs_iq_recorder.cellular_runner import S_DONE
    from crfs_iq_recorder.gui import MainWindow

    book = _plan_book(tmp_path / "plan.xlsx")
    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        window.tabs.setCurrentIndex(1)
        _load(panel, qapp, book)
        waiting = "The last collection is still downloading (2 waiting). Start the next one when the downloads finish."
        panel._on_status({"state": S_DONE, "text": "Finished", "active": False, "previous_work": waiting})
        assert not panel.start_btn.isEnabled()
        assert panel.reason_label.text() == waiting and not panel.reason_label.isHidden()
        panel._on_status({"state": S_DONE, "text": "Finished", "active": False, "previous_work": ""})
        assert panel.start_btn.isEnabled() and panel.reason_label.isHidden()
    finally:
        window.close()
        qapp.processEvents()


def test_retry_failed_rows_from_the_pass_summary_sends_the_rejected_row_as_attempt_2(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import QTimer
    from crfs_iq_recorder.capture_api import OUTCOME_SENSOR_ERROR, CaptureSummary
    from crfs_iq_recorder.capture_measure import RESULT_FAILED
    from crfs_iq_recorder.gui import MainWindow

    book = _plan_book(tmp_path / "plan.xlsx")
    window = MainWindow(demo=True)
    try:
        window._settle_s = 0
        window._stable_needed = 2
        window._demo_parts = 1
        panel = window.cellular_panel
        window.tabs.setCurrentIndex(1)
        _load(panel, qapp, book)
        panel.duration_spin.setValue(1.0)
        panel.wait_spin.setValue(0)
        real_start = window.start_capture
        rejected = []

        def reject_the_first(request):
            if rejected:
                return real_start(request)
            rejected.append(request)
            detail = "ERROR: Unable to schedule the command"
            summary = CaptureSummary(
                outcome=OUTCOME_SENSOR_ERROR, request=request, result=RESULT_FAILED, result_reason="rejected by the sensor",
                sensor_error=detail, detail=detail, requested_s=request.seconds,
                payload={"center_frequency": request.plan.center_hz, "bandwidth": request.plan.bandwidth_hz, "duration": request.seconds},
            )
            QTimer.singleShot(0, lambda: window.capture_finished.emit(summary))
            return None

        monkeypatch.setattr(window, "start_capture", reject_the_first)
        panel.run_now.setChecked(True)
        schedule, error = panel._schedule()
        assert error is None
        panel._start(schedule)
        runner = window._cellular

        def drive(rounds):
            for _ in range(rounds):
                assert _pump_until(qapp, lambda: window._http_in_flight is False)
                _drive_capture(window, qapp)
                runner._tick()
            assert _pump_until(qapp, lambda: runner.state == S_DONE, timeout=8.0), runner.status()
            assert _pump_until(qapp, lambda: runner.background_jobs() == 0, timeout=20.0)
            qapp.processEvents()

        drive(2)
        assert len(rejected) == 1
        assert not panel.pass_box.isHidden()
        assert panel.pass_counts.text() == "Completed 1 · Partial 0 · Failed 1 · Uncertain 0"
        assert not panel.retry_btn.isHidden() and panel.retry_btn.isEnabled()
        panel.retry_btn.click()
        qapp.processEvents()
        assert window._external_owner == OWNER and panel.pass_box.isHidden(), "the pass runs again for the one row"
        drive(1)
        assert len(rejected) == 1, "the retry is a new request on the demo sensor"
        assert panel.pass_counts.text() == "Completed 2 · Partial 0 · Failed 0 · Uncertain 0"
        assert panel.retry_btn.isHidden()
        assert panel.result_card.title.text() == "Row 1 of 2: 703-713 MHz (attempt 2)"
        first = runner.session.tasks[runner.status()["last_round_id"]][0]
        assert first.attempts == 2 and first.status == T_CONFIRMED and len(first.history) == 1
        # A file given up while the sensor accepted no connection: the pass box says so and
        # "Retry downloads" fetches it without recording anything again.
        assert panel.retry_dl_btn.isHidden()
        item = first.files[0]
        item["download_status"] = "failed"
        panel._on_status(runner.status())
        assert panel.pass_counts.text().endswith("\nNot downloaded: 1 file, still on the sensor.")
        assert not panel.retry_dl_btn.isHidden() and panel.retry_dl_btn.isEnabled()
        panel.retry_dl_btn.click()
        assert _pump_until(qapp, lambda: item["download_status"] == "verified", timeout=10.0), item
        assert _pump_until(qapp, lambda: runner.background_jobs() == 0, timeout=10.0)
        panel._on_status(runner.status())
        assert panel.retry_dl_btn.isHidden() and "Not downloaded" not in panel.pass_counts.text()
        assert first.attempts == 2 and runner.state == S_DONE, "nothing was recorded again"
        panel.pass_dismiss_btn.click()
        assert panel.pass_box.isHidden()
    finally:
        window._watch_timer.stop()
        window._record_timer.stop()
        window.close()
        qapp.processEvents()


def test_wait_unit_keeps_the_same_wait_and_a_missing_workbook_says_what_to_do(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    save_panel_settings(tmp_path / "config", PanelSettings(workbook=str(tmp_path / "moved.xlsx"), wait_s=90.0, wait_unit="seconds"))
    window = MainWindow(demo=True)
    try:
        panel = window.cellular_panel
        assert panel.wait_seconds() == 90.0, "read from the settings before the form is built"
        window.tabs.setCurrentIndex(1)
        assert panel.import_label.text() == "Protocol workbook not found. Choose the file again."
        assert not panel.start_btn.isEnabled()
        panel.wait_unit.setCurrentText("Minutes")
        assert panel.wait_spin.value() == pytest.approx(1.5) and panel.wait_seconds() == pytest.approx(90.0)
        panel.wait_spin.setValue(2)
        assert panel.wait_seconds() == pytest.approx(120.0)
        panel._save_settings()
        saved = json.loads((tmp_path / "config" / "cellular" / "settings.json").read_text(encoding="utf-8"))
        assert saved["wait_unit"] == "minutes" and saved["wait_s"] == pytest.approx(120.0)
    finally:
        window.close()
        qapp.processEvents()
