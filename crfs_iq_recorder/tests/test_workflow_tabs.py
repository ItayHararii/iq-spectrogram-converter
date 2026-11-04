"""Manual Recording / Excel Collection tabs: one controller at a time, restore, pause, result card."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from openpyxl import Workbook


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
    monkeypatch.setattr("crfs_iq_recorder.sftp_window.QDesktopServices.openUrl", lambda *_a, **_k: True)


def _pump_until(qapp, predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _drive_capture(window, qapp) -> None:
    qapp.processEvents()
    window._record_timer.stop()
    if window._recording_active:
        window._on_recording_time_elapsed()
    qapp.processEvents()
    for _ in range(4):
        window._poll_collection_files()
    qapp.processEvents()


def _window(tmp_path, *, parts=1):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    window._settle_s = 0
    window._stable_needed = 2
    window._demo_parts = parts
    window._conn.download_dir = str(tmp_path / "downloads")
    window.mode_start.setChecked(True)
    window.field_a.setText("156")
    window.field_b.setText("162")
    window.time_edit.setText("0.05")
    return window


def _close(window, qapp):
    window._wait_timer.stop()
    window._watch_timer.stop()
    window._record_timer.stop()
    window.close()
    qapp.processEvents()


def _plan_book(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    for row in [["Band", "Duplex", "Direction", "Start (MHz)", "Stop (MHz)"], ["700", "FDD", "UL", 703, 713]]:
        ws.append(row)
    wb.save(path)
    return path


def test_one_controller_at_a_time_and_a_tab_switch_never_interrupts(qapp, tmp_path):
    window = _window(tmp_path)
    try:
        panel = window.cellular_panel
        window.tabs.setCurrentIndex(1)
        panel.workbook_edit.setText(str(_plan_book(tmp_path / "plan.xlsx")))
        panel._fill_sheets("")
        panel._import(force=True)
        assert _pump_until(qapp, lambda: panel.plan is not None)
        assert panel.start_btn.isEnabled() and panel.reason_label.isHidden()
        window.tabs.setCurrentIndex(0)
        window._on_start()
        assert window._recording_active
        assert window.manual_busy_reason() == "Manual recording is running."
        assert not panel.start_btn.isEnabled()
        assert panel.reason_label.text() == "Manual recording is running." and not panel.reason_label.isHidden()
        # Looking at the other tab changes nothing about the recording.
        window.tabs.setCurrentIndex(1)
        qapp.processEvents()
        assert window._recording_active and window._conn.ui_tab == "excel"
        window.tabs.setCurrentIndex(0)
        _drive_capture(window, qapp)
        assert _pump_until(qapp, lambda: window._recordings_completed == 1 and not window._recording_active)
        assert _pump_until(qapp, lambda: window.manual_busy_reason() is None)
        qapp.processEvents()
        assert panel.start_btn.isEnabled() and panel.reason_label.isHidden(), "free again once the manual recording ended"
        assert window.manual_reason.isHidden()
    finally:
        _close(window, qapp)


def test_the_manual_tab_says_whether_the_collection_is_running_or_paused(qapp, tmp_path):
    from crfs_iq_recorder.cellular_runner import OWNER, S_PAUSED, S_RUNNING

    window = _window(tmp_path)
    try:
        panel = window.cellular_panel
        assert window.set_external_owner(OWNER) is None
        panel.runner.state = S_RUNNING
        panel._on_status({"state": S_RUNNING, "text": "Recording row 1 of 2"})
        assert window.manual_reason.text() == "Excel Collection is running." and not window.start_btn.isEnabled()
        panel.runner.state = S_PAUSED
        panel._on_status({"state": S_PAUSED, "text": "Paused"})
        assert window.manual_reason.text() == "Excel Collection is paused. Stop it first."
        assert window.start_btn.toolTip() == "Excel Collection is paused. Stop it first."
        panel.runner.state = S_RUNNING
        panel._on_status({"state": S_RUNNING, "text": "Recording row 2 of 2"})
        assert window.manual_reason.text() == "Excel Collection is running."
        window.release_external_owner(OWNER)
        assert window.manual_reason.isHidden() and window.start_btn.isEnabled()
    finally:
        _close(window, qapp)


def test_the_selected_tab_is_restored_and_nothing_starts_by_itself(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    window = _window(tmp_path)
    try:
        window.tabs.setCurrentIndex(1)
        window.wait_unit.setCurrentText("Minutes")
    finally:
        _close(window, qapp)
    restored = MainWindow(demo=True)
    try:
        assert restored.tabs.currentIndex() == 1 and restored.cellular_panel._built
        assert restored.wait_unit.currentText() == "Minutes"
        assert not restored._recording_active and not restored._series_active
        assert restored._cellular.session is None and not restored._external_owner
        restored.tabs.setCurrentIndex(0)
    finally:
        _close(restored, qapp)
    again = MainWindow(demo=True)
    try:
        assert again.tabs.currentIndex() == 0 and not again.cellular_panel._built
    finally:
        _close(again, qapp)


def test_manual_series_pauses_after_the_current_recording_and_paused_time_counts_toward_the_wait(qapp, tmp_path):
    window = _window(tmp_path)
    try:
        window.repeat_n.setChecked(True)
        window.repeat_spin.setValue(3)
        window.wait_spin.setValue(30)
        assert not window.pause_btn.isEnabled() and not window.stop_btn.isEnabled()
        assert window.stop_btn.toolTip() == "Stops after the current recording"
        window._on_start()
        assert window._series_active and window.pause_btn.isEnabled()
        window.pause_btn.click()
        assert window._pause_requested and not window.pause_btn.isEnabled()
        _drive_capture(window, qapp)
        assert window._recordings_completed == 1
        assert window._series_paused and not window._waiting and window.phase_label.text() == "Paused"
        assert window.resume_btn.isVisibleTo(window) and window.resume_btn.isEnabled()
        assert not window.pause_btn.isVisibleTo(window)
        assert window.start_btn.text() == "Series paused" and not window.start_btn.isEnabled()
        assert window.stop_btn.isEnabled()
        assert window.manual_busy_reason() == "Manual recording is paused. Stop it first."
        # Resume: the remaining wait (30 s minus the pause) runs, then a pause during the wait freezes it.
        window.resume_btn.click()
        assert window._waiting and not window._series_paused
        window.pause_btn.click()
        assert window._series_paused and not window._waiting and not window._wait_timer.isActive()
        assert 25 <= window._paused_wait_s <= 30
        # A pause longer than the wait: the next recording starts right after Resume.
        window._paused_at -= 60
        window.resume_btn.click()
        assert _pump_until(qapp, lambda: window._recording_active)
        _drive_capture(window, qapp)
        assert window._recordings_completed == 2 and window._waiting
        # Stop while paused ends the series.
        window.pause_btn.click()
        assert window._series_paused
        window.stop_btn.click()
        qapp.processEvents()
        assert not window._series_active and not window._series_paused and window._recordings_completed == 2
        assert window.start_btn.isEnabled() and window.start_btn.text() == "Start Recording"
        assert window.pause_btn.isVisibleTo(window) and not window.resume_btn.isVisibleTo(window)
    finally:
        _close(window, qapp)


def test_manual_result_card_downloads_then_opens_the_folder_and_the_waterfall(qapp, tmp_path, no_desktop_open):
    window = _window(tmp_path)
    try:
        card = window.manual_result
        assert card.isHidden(), "no recording yet"
        window._on_start()
        _drive_capture(window, qapp)
        assert _pump_until(qapp, lambda: card.view is not None and not window._recording_active)
        assert not card.isHidden()
        assert card.title.text() == "156-162 MHz"
        assert card.requested.text() == "Requested: 6 MHz for 0.05 s"
        assert card.received.text().startswith("Received (read from the files): 1 file, ")
        assert card.states.text() == "Files ready · On the sensor"
        assert not card.open_btn.isEnabled() and card.open_btn.toolTip() == "Download required."
        assert not card.waterfall_btn.isEnabled() and card.download_btn.isEnabled()
        card.download_btn.click()
        assert _pump_until(qapp, lambda: window._manual_download is None, timeout=15.0)
        assert "Saved locally" in card.states.text(), card.states.text()
        local = Path(card.view.local_paths[0])
        assert local.is_file() and local.parent == tmp_path / "downloads"
        assert card.open_btn.isEnabled() and card.waterfall_btn.isEnabled() and not card.download_btn.isEnabled()
        card.open_btn.click()
        assert no_desktop_open == [("reveal", str(local))]
        card.waterfall_btn.click()
        dialog = window._waterfall
        assert dialog is not None
        assert _pump_until(qapp, lambda: not dialog.running(), timeout=30.0)
        assert not dialog.error and dialog.result is not None, dialog.error
        assert dialog.image.pixmap() is not None and not dialog.image.pixmap().isNull()
        dialog.close()
    finally:
        _close(window, qapp)


def test_missing_locations_get_one_actionable_line_each(qapp, tmp_path):
    window = _window(tmp_path)
    try:
        lines = []
        window.log = lambda message, *, level="info": lines.append((level, message))
        window._conn.demo = False
        window._conn.download_dir = str(tmp_path / "gone" / "downloads")
        window._conn.excel_workbook = str(tmp_path / "gone.xlsm")
        window.excel_check.setChecked(True)
        window._report_missing_locations()
        assert lines == [
            ("info", "Download folder not found. It is created again at the first download."),
            ("error", "Excel log workbook not found. Choose the file again."),
        ]
        assert window.collection_status.text() == "Excel log workbook not found. Choose the file again."
        lines.clear()
        window.excel_check.setChecked(False)
        window._conn.download_dir = "Q:\\nowhere\\downloads" if not Path("Q:\\").exists() else str(tmp_path / "x")
        window._report_missing_locations()
        if not Path("Q:\\").exists():
            assert lines == [("error", "Download folder not found. Choose it again in Settings.")]
    finally:
        window._conn.demo = True
        _close(window, qapp)
