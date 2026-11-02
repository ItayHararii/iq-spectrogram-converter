"""Cellular Collection panel inside the demo main window (offscreen Qt)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from openpyxl import Workbook

from crfs_iq_recorder.cellular_panel import (
    ColumnMappingDialog,
    CoverageDialog,
    PanelSettings,
    PlanPreviewDialog,
    load_panel_settings,
    parse_dates,
    save_panel_settings,
)
from crfs_iq_recorder.cellular_protocol import ColumnMapping, import_protocol, suggest_mapping
from crfs_iq_recorder.cellular_reference import REFERENCE_ALLOCATIONS, VERIFY_NOTE, write_template_workbook
from crfs_iq_recorder.cellular_runner import OWNER, S_DONE, S_IDLE, S_RUNNING, T_CONFIRMED

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


def test_panel_settings_round_trip_and_date_parsing(tmp_path):
    settings = PanelSettings(campaign="Camp", wait_s=25.0, wait_unit="seconds", sites={"A": {"site_id": "A", "scenario": {"network": "Cellcom"}}})
    save_panel_settings(tmp_path, settings)
    loaded = load_panel_settings(tmp_path)
    assert loaded.campaign == "Camp" and loaded.wait_s == 25.0 and loaded.sites["A"]["scenario"]["network"] == "Cellcom"
    assert loaded.fallback_duration_s is None
    dates, bad = parse_dates("2026-11-03, 2026-11-04; nope")
    assert [d.isoformat() for d in dates] == ["2026-11-03", "2026-11-04"] and bad == ["nope"]


def test_mapping_dialog_requires_allocation_edges(qapp):
    headers = ["Band", "Frequency", "Duplex", "Direction", "Col E", "Col F"]
    mapping = suggest_mapping(headers)
    assert mapping.missing(), "unnamed columns are not guessed as allocation edges"
    dialog = ColumnMappingDialog(headers, mapping)
    dialog._accept_if_complete()
    assert dialog.result() != 1 and "missing" in dialog.problem.text().casefold()
    dialog._combos["start"].setCurrentIndex(dialog._combos["start"].findData("Col E"))
    dialog._combos["stop"].setCurrentIndex(dialog._combos["stop"].findData("Col F"))
    dialog.unit_combo.setCurrentIndex(dialog.unit_combo.findData("MHz"))
    dialog._accept_if_complete()
    assert dialog.result() == 1
    chosen = dialog.mapping()
    assert chosen.start == "Col E" and chosen.stop == "Col F" and chosen.default_unit == "MHz" and chosen.layout() == "rows"


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
        # Load the workbook as the Browse dialog would.
        panel.workbook_edit.setText(str(book))
        panel._fill_sheets("")
        panel._import(force=True)
        assert panel.imported is not None and panel.plan is not None
        assert "2 allocation(s)" in panel.import_label.text()
        assert "required" in panel.duration_label.text().casefold(), "no duration column -> the campaign duration is required"
        assert panel.start_btn.isEnabled() is False
        panel.duration_edit.setText("0.05")
        assert panel.plan.ready and panel.start_btn.isEnabled()
        assert [t.direction for t in panel.plan.tasks] == ["DL", "UL"]
        panel.wait_unit.setCurrentText("Seconds")
        panel.wait_spin.setValue(0)
        assert panel.wait_seconds() == 0
        # Preview dialog builds with the sample plan and pre-run checks.
        checks = panel._pre_run_checks()
        dialog = PlanPreviewDialog(panel.plan, panel.imported, wait_s=panel.wait_seconds(), checks=checks, start_label="Start", format_text=panel.format_text())
        assert dialog.windowTitle() == "Cellular collection plan"
        dialog.close()
        # Recording format: follows the recorder's Format box unless chosen here; shown before Start.
        assert panel.format_combo.currentData() == "" and panel._effective_format() == ("WAVE", "recorder setting")
        assert panel.format_text().startswith("Recording format WAVE (recorder setting).")
        assert any(c.name == "recording_format" and c.level == "ok" for c in checks)
        window.format_combo.setCurrentIndex(window.format_combo.findData("XDAT"))
        assert panel._effective_format() == ("XDAT", "recorder setting")
        assert "not inspected" in panel.format_text()
        panel.format_combo.setCurrentIndex(panel.format_combo.findData("WAVE"))
        assert panel._effective_format() == ("WAVE", "chosen here")
        assert panel._campaign_settings().recording_format == "WAVE"
        window.format_combo.setCurrentIndex(window.format_combo.findData("WAVE"))
        # Settings persisted separately from the ordinary recorder settings.
        panel.campaign_edit.setText("Demo campaign")
        panel.site_combo.setCurrentText("Lab")
        panel._save_site()
        saved = json.loads((tmp_path / "config" / "cellular" / "settings.json").read_text(encoding="utf-8"))
        assert saved["campaign"] == "Demo campaign" and "Lab" in saved["sites"] and saved["wait_s"] == 0
        assert saved["workbook"] == str(book)
        # Start (bypassing the modal preview) and run the round on the demo transport.
        panel.run_now.setChecked(True)
        schedule, error = panel._schedule()
        assert error is None
        panel._start(schedule)
        assert window._external_owner == OWNER
        assert panel.chip.text() == "Recording"
        assert not panel.start_btn.isEnabled() and panel.stop_btn.isEnabled() and panel.pause_btn.isEnabled()
        for _ in range(2):
            assert _pump_until(qapp, lambda: window._http_in_flight is False)
            _drive_capture(window, qapp)
            window._cellular._tick()
        assert _pump_until(qapp, lambda: window._cellular.state == S_DONE, timeout=8.0), window._cellular.status()
        assert _pump_until(qapp, lambda: window._cellular.background_jobs() == 0, timeout=20.0)
        tasks = window._cellular.current_tasks() or list(window._cellular.session.tasks.values())[0]
        assert [t.status for t in tasks] == [T_CONFIRMED, T_CONFIRMED]
        assert all(t.summary.get("recording_format") == "WAVE" for t in tasks), "the format that was sent travels with the summary"
        manifest = json.loads(Path(tasks[0].manifest_path).read_text(encoding="utf-8"))
        assert manifest["recording"]["recording_format"] == "WAVE" and manifest["recording"]["encoding"].startswith("WAVE")
        assert tasks[0].quality.get("level") in {"ok", "warning"}
        qapp.processEvents()
        assert panel.chip.text() == "Finished"
        assert not panel.recovery.isHidden() and "finished" in panel.recovery_label.text().casefold()
        assert window._external_owner == ""
        # Coverage dialog over the finished session.
        coverage = CoverageDialog(__import__("crfs_iq_recorder.cellular_quality", fromlist=["coverage_rows"]).coverage_rows(window._cellular.session))
        assert coverage.tree.topLevelItemCount() == 2
        coverage.close()
        # Discard returns the panel to idle.
        window._cellular.discard_session()
        qapp.processEvents()
        assert window._cellular.state == S_IDLE and panel.chip.text() == "Off"
    finally:
        window._watch_timer.stop()
        window._record_timer.stop()
        window.close()
        qapp.processEvents()


def test_collapsed_panel_builds_nothing_until_opened_and_opens_for_a_saved_session(qapp, tmp_path):
    from PySide6.QtWidgets import QWidget
    from crfs_iq_recorder.cellular_plan import build_plan
    from crfs_iq_recorder.cellular_capabilities import capability_for
    from crfs_iq_recorder.cellular_runner import CampaignSettings, CellularRunner
    from crfs_iq_recorder.cellular_scheduler import MODE_NOW, Schedule
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
    assert runner.start_session(plan, Schedule(mode=MODE_NOW), CampaignSettings(campaign="C", site="S", wait_s=0, auto_download=False)) is None
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


def test_panel_test_capture_reserves_and_releases_the_sensor(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    book = _plan_book(tmp_path / "plan.xlsx")
    window = MainWindow(demo=True)
    try:
        window._settle_s = 0
        window._stable_needed = 2
        window._demo_parts = 1
        panel = window.cellular_panel
        assert not panel._built, "the body is built on first use only"
        panel._set_expanded(True)
        panel.workbook_edit.setText(str(book))
        panel._fill_sheets("")
        panel._import(force=True)
        panel.duration_edit.setText("0.05")
        import crfs_iq_recorder.cellular_panel as cp

        old = cp.HISTORICAL_PRESET_S
        cp.HISTORICAL_PRESET_S = 0.05
        try:
            panel._test_capture()
        finally:
            cp.HISTORICAL_PRESET_S = old
        assert panel._test_running and window._external_owner == OWNER
        assert not panel.start_btn.isEnabled()
        assert _pump_until(qapp, lambda: window._http_in_flight is False)
        _drive_capture(window, qapp)
        assert _pump_until(qapp, lambda: not panel._test_running, timeout=6.0)
        assert window._external_owner == ""
        assert "Test capture confirmed" in panel.status_label.text()
        assert panel.start_btn.isEnabled()
    finally:
        window._watch_timer.stop()
        window._record_timer.stop()
        window.close()
        qapp.processEvents()
