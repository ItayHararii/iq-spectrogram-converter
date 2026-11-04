"""Nothing records unless the operator presses Start (or set a schedule): Enter in a
field, a focus change, a restore or an unlock/standby-like event never starts."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
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


@pytest.fixture
def starts(monkeypatch):
    """Every way a recording or a collection can begin, recorded instead of run.

    Patched on the classes before any window exists, so the signal connections
    made in the constructors reach the recorders."""
    from crfs_iq_recorder.cellular_panel import CellularPanel
    from crfs_iq_recorder.cellular_runner import CellularRunner
    from crfs_iq_recorder.gui import MainWindow

    calls: list[tuple[str, str]] = []

    def recorder(name):
        def record(self, *_args, **_kwargs):
            calls.append((name, str(getattr(self, "_start_trigger", "") or "")))
            return None

        return record

    for cls, names in (
        (MainWindow, ("_on_start", "_launch_capture", "_on_resume_series", "_start_next_now", "_open_sftp")),
        (CellularPanel, ("_start_clicked", "_start", "_resume_after_restart", "_run_missed", "_retry_failed")),
        (CellularRunner, ("start_session", "resume", "resume_after_restart", "run_missed_round", "retry_failed", "_start_round")),
    ):
        for name in names:
            monkeypatch.setattr(cls, name, recorder(f"{cls.__name__}.{name}"))
    return calls


def _pump(qapp, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.02)


def _pump_until(qapp, predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _plan_book(path: Path) -> Path:
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    for row in [["Band", "Duplex", "Direction", "Start (MHz)", "Stop (MHz)"], ["700", "FDD", "UL", 703, 713]]:
        ws.append(row)
    wb.save(path)
    return path


def _window(tmp_path):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    window._settle_s = 0
    window._stable_needed = 2
    window._conn.download_dir = str(tmp_path / "downloads")
    window.mode_start.setChecked(True)
    window.field_a.setText("156")
    window.field_b.setText("162")
    window.time_edit.setText("0.05")
    return window


def _show_active(window, qapp):
    from PySide6.QtTest import QTest

    window.show()
    window.raise_()
    window.activateWindow()
    QTest.qWaitForWindowActive(window, 3000)
    qapp.processEvents()
    assert window.isActiveWindow(), "the test needs an active window to deliver keys like a real session"


def _close(window, qapp):
    for name in ("_wait_timer", "_watch_timer", "_record_timer"):
        timer = getattr(window, name, None)
        if timer is not None:
            timer.stop()
    window.close()
    qapp.processEvents()


def _press(window, key, qapp) -> None:
    """A key press and release through the window, as Windows delivers it."""
    from PySide6.QtTest import QTest

    QTest.keyClick(window.windowHandle(), key)
    qapp.processEvents()


def _inputs(root, *, exclude=None):
    from PySide6.QtWidgets import QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QPlainTextEdit, QPushButton

    from crfs_iq_recorder.cellular_pickers import DateField, TimesField

    kinds = (QLineEdit, QAbstractSpinBox, QComboBox, QPlainTextEdit, QAbstractButton, DateField, TimesField)
    found = []
    for widget in root.findChildren(object):
        if not isinstance(widget, kinds) or isinstance(widget, QPushButton):
            continue  # push buttons are checked one by one below; Browse would open a file dialog
        if exclude is not None and exclude.isAncestorOf(widget):
            continue
        if widget.isVisibleTo(root) and widget.isEnabled():
            found.append(widget)
    return found


def _click_spy(window):
    """Every push button of the window (Reload, Sensor Files, Start…) reports a click here."""
    from PySide6.QtWidgets import QPushButton

    clicked: list[str] = []
    for button in window.findChildren(QPushButton):
        button.clicked.connect(lambda _checked=False, b=button: clicked.append(b.text() or b.objectName()))
    return clicked


def _ready_excel_panel(window, tmp_path, qapp):
    panel = window.cellular_panel
    window.tabs.setCurrentIndex(1)
    panel.workbook_edit.setText(str(_plan_book(tmp_path / "plan.xlsx")))
    panel._fill_sheets("")
    panel._import(force=True)
    assert _pump_until(qapp, lambda: panel.plan is not None)
    qapp.processEvents()
    assert panel.start_btn.isEnabled(), "Excel Start must be available, or the test proves nothing"
    return panel


def test_enter_in_any_manual_field_does_not_start(qapp, tmp_path, starts):
    from PySide6.QtCore import Qt

    window = _window(tmp_path)
    try:
        _show_active(window, qapp)
        window.tabs.setCurrentIndex(0)
        window.repeat_n.setChecked(True)  # the worst case: Start would begin a series
        qapp.processEvents()
        assert window.start_btn.isEnabled() and not window.start_btn.isDefault() and not window.start_btn.autoDefault()
        page = window.tabs.widget(0)
        clicked = _click_spy(window)
        tried = 0
        for widget in _inputs(page):
            widget.setFocus(Qt.FocusReason.TabFocusReason)
            qapp.processEvents()
            if not widget.hasFocus() and not widget.isAncestorOf(qapp.focusWidget()):
                continue
            tried += 1
            for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                _press(window, key, qapp)
                where = f"{type(widget).__name__} {widget.objectName() or widget.toolTip()!r}"
                assert starts == [], f"Enter in {where} started"
                assert clicked == [], f"Enter in {where} pressed {clicked}"
        assert tried >= 10, f"only {tried} Manual fields took focus"
        for widget in (window.field_a, window.field_b, window.time_edit, window.repeat_spin, window.wait_spin, window.unit_combo, window.format_combo):
            assert widget in _inputs(page) or not widget.isVisibleTo(page)
        assert not window._recording_active and not window._series_active
    finally:
        _close(window, qapp)


def test_enter_in_any_excel_field_does_not_start(qapp, tmp_path, starts):
    from PySide6.QtCore import Qt

    window = _window(tmp_path)
    try:
        _show_active(window, qapp)
        panel = _ready_excel_panel(window, tmp_path, qapp)
        panel.run_times.setChecked(True)
        qapp.processEvents()
        page = window.tabs.widget(1)
        clicked = _click_spy(window)
        tried = 0
        for widget in _inputs(page):
            widget.setFocus(Qt.FocusReason.TabFocusReason)
            qapp.processEvents()
            if not widget.hasFocus() and not widget.isAncestorOf(qapp.focusWidget()):
                continue
            tried += 1
            for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                _press(window, key, qapp)
                for popup in [w for w in qapp.topLevelWidgets() if w.isVisible() and w is not window]:
                    popup.close()  # Enter on the date field opens its calendar; that is not a start
                qapp.processEvents()
                where = f"{type(widget).__name__} {widget.objectName() or widget.toolTip()!r}"
                assert starts == [], f"Enter in {where} started"
                assert clicked == [], f"Enter in {where} pressed {clicked}"
        assert tried >= 8, f"only {tried} Excel fields took focus"
        assert panel.runner.session is None
    finally:
        _close(window, qapp)


def test_start_buttons_are_never_default_and_enter_on_them_does_nothing(qapp, tmp_path, starts):
    from PySide6.QtCore import Qt

    from crfs_iq_recorder.start_button import StartButton

    window = _window(tmp_path)
    try:
        _show_active(window, qapp)
        panel = _ready_excel_panel(window, tmp_path, qapp)
        buttons = window.findChildren(StartButton)
        names = {button.text() for button in buttons}
        assert {"Start Recording", "Resume", "Start"} <= names
        for button in buttons:
            assert not button.isDefault() and not button.autoDefault()
            assert button.focusPolicy() == Qt.FocusPolicy.TabFocus, f"{button.text()} takes focus on a click"
        for tab, button in ((0, window.start_btn), (1, panel.start_btn)):
            window.tabs.setCurrentIndex(tab)
            qapp.processEvents()
            button.setFocus(Qt.FocusReason.OtherFocusReason)
            qapp.processEvents()
            assert button.hasFocus()
            for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                _press(window, key, qapp)
            assert starts == [], f"Enter on {button.text()} started"
    finally:
        _close(window, qapp)


def test_a_click_leaves_no_focus_so_a_later_key_cannot_start_again(qapp, tmp_path, starts):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    window = _window(tmp_path)
    try:
        _show_active(window, qapp)
        window.time_edit.setFocus()
        QTest.mouseClick(window.start_btn, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert starts == [("MainWindow._on_start", "Start button (mouse)")]
        assert not window.start_btn.hasFocus()
        starts.clear()
        for key in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            _press(window, key, qapp)
        assert starts == []
        # A programmatic click (UI Automation Invoke, a test harness) says so in the log.
        window.start_btn.click()
        assert starts == [("MainWindow._on_start", "Start button (program)")]
    finally:
        _close(window, qapp)


def test_space_starts_only_after_a_deliberate_tab_from_an_enabled_control(qapp, tmp_path, starts):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QWidget

    window = _window(tmp_path)
    try:
        _show_active(window, qapp)
        button = window.start_btn
        # Tab from an enabled field: Space presses the button, like any button.
        window.time_edit.setFocus(Qt.FocusReason.TabFocusReason)
        button.setFocus(Qt.FocusReason.TabFocusReason)
        qapp.processEvents()
        assert button.keyboard_armed
        _press(window, Qt.Key.Key_Space, qapp)
        assert starts == [("MainWindow._on_start", "Start button (keyboard)")]
        starts.clear()
        # Focus arriving by window activation (unlock, Alt+Tab, a closed dialog) is not a Tab.
        button.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        window.time_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        button.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        qapp.processEvents()
        assert button.hasFocus() and not button.keyboard_armed
        _press(window, Qt.Key.Key_Space, qapp)
        assert starts == []
        # Qt moves focus on with a Tab reason when the focused control is disabled or hidden.
        for take_away in (lambda w: w.setEnabled(False), lambda w: w.setVisible(False)):
            window.time_edit.setEnabled(True)
            window.time_edit.setVisible(True)
            QWidget.setTabOrder(window.time_edit, button)
            window.time_edit.setFocus(Qt.FocusReason.TabFocusReason)
            qapp.processEvents()
            assert window.time_edit.hasFocus()
            take_away(window.time_edit)
            qapp.processEvents()
            assert button.hasFocus(), "focus did not move on to Start; the check below would prove nothing"
            assert not button.keyboard_armed
            _press(window, Qt.Key.Key_Space, qapp)
            assert starts == []
        window.time_edit.setEnabled(True)
        window.time_edit.setVisible(True)
    finally:
        _close(window, qapp)


def test_unlock_and_standby_like_event_sequences_do_not_start(qapp, tmp_path, starts):
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtWidgets import QApplication, QWidget

    from crfs_iq_recorder.sensor_info import SensorInfo

    window = _window(tmp_path)
    other = QWidget()
    other.setWindowTitle("Another application")
    try:
        _show_active(window, qapp)
        panel = _ready_excel_panel(window, tmp_path, qapp)
        for tab, button in ((0, window.start_btn), (1, panel.start_btn)):
            window.tabs.setCurrentIndex(tab)
            qapp.processEvents()
            # The worst case: Start held keyboard focus (and was armed) when the PC locked.
            button.setFocus(Qt.FocusReason.TabFocusReason)
            qapp.processEvents()
            # Lock / standby: another window becomes active, ours is minimised, the link drops.
            other.show()
            other.activateWindow()
            _pump(qapp, 0.1)
            for kind in (QEvent.Type.WindowDeactivate, QEvent.Type.ActivationChange):
                QApplication.sendEvent(window, QEvent(kind))
            window.showMinimized()
            _pump(qapp, 0.1)
            window._apply_sensor_info(SensorInfo(status="Disconnected", connected=False))
            # Unlock / resume: the window comes back, the link recovers, keys from the
            # sign-in screen (Enter, Space) arrive late.
            window.showNormal()
            other.hide()
            _show_active(window, qapp)
            for kind in (QEvent.Type.WindowActivate, QEvent.Type.ActivationChange, QEvent.Type.ScreenChangeInternal):
                QApplication.sendEvent(window, QEvent(kind))
            window._apply_sensor_info(SensorInfo(status="Demo", connected=True, demo=True, model="RFeye Node 100-18"))
            qapp.processEvents()
            assert not button.keyboard_armed
            for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
                _press(window, key, qapp)
            _pump(qapp, 0.5)
            assert starts == [], f"{starts} on the {'Manual' if tab == 0 else 'Excel'} tab"
        assert not window._recording_active and not window._series_active and panel.runner.session is None
    finally:
        other.close()
        _close(window, qapp)


def test_restore_never_arms_a_series_or_a_schedule(qapp, tmp_path, starts):
    from crfs_iq_recorder.cellular_scheduler import MODE_TIMES
    from crfs_iq_recorder.gui import MainWindow

    window = _window(tmp_path)
    try:
        window.repeat_n.setChecked(True)
        window.repeat_spin.setValue(3)
        window.wait_spin.setValue(5)
        window.tabs.setCurrentIndex(1)
        panel = window.cellular_panel
        panel.run_times.setChecked(True)
        soon = (datetime.now() + timedelta(minutes=1)).time().replace(second=0, microsecond=0)
        panel.times_field.set_times([soon])
        panel.daily_check.setChecked(True)
        panel._save_settings()
        window.tabs.setCurrentIndex(0)
    finally:
        _close(window, qapp)
    restored = MainWindow(demo=True)
    try:
        assert restored.repeat_n.isChecked() and restored.repeat_spin.value() == 3
        _pump(qapp, 1.0)
        assert starts == []
        assert not restored._series_active and not restored._waiting and not restored._recording_active
        assert not restored._wait_timer.isActive()
        restored.tabs.setCurrentIndex(1)
        qapp.processEvents()
        panel = restored.cellular_panel
        assert panel.settings.schedule_mode == MODE_TIMES and panel.run_times.isChecked()
        assert panel.times_field.times() == [soon]
        _pump(qapp, 0.5)
        assert starts == [] and panel.runner.session is None
    finally:
        _close(restored, qapp)


def test_preview_start_is_not_default_and_stop_defaults_to_no(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialogButtonBox, QMessageBox

    from crfs_iq_recorder.cellular_panel import PlanPreviewDialog
    from crfs_iq_recorder.cellular_runner import S_RUNNING

    window = _window(tmp_path)
    try:
        panel = _ready_excel_panel(window, tmp_path, qapp)
        dialog = PlanPreviewDialog(panel.plan, wait_s=0, rows_total=1, notes=[], start_label="Start")
        assert dialog.start_button is not None
        assert not dialog.start_button.isDefault() and not dialog.start_button.autoDefault()
        box = dialog.findChild(QDialogButtonBox)
        close = box.button(QDialogButtonBox.StandardButton.Close)
        assert close is not None and close.isDefault()
        dialog.close()

        asked = []

        def question(_parent, _title, _text, _buttons, default):
            asked.append(default)
            return QMessageBox.StandardButton.No

        stopped = []
        monkeypatch.setattr("crfs_iq_recorder.cellular_panel.QMessageBox.question", question)
        monkeypatch.setattr(panel.runner, "status", lambda: {"state": S_RUNNING})
        monkeypatch.setattr(panel.runner, "stop", lambda: stopped.append(True))
        panel._stop()
        assert asked == [QMessageBox.StandardButton.No] and stopped == []
    finally:
        _close(window, qapp)


def test_each_request_names_its_trigger_and_series_starts_are_logged(qapp, tmp_path):
    from crfs_iq_recorder.diagnostic_log import recent

    window = _window(tmp_path)
    try:
        window.repeat_n.setChecked(True)
        window.repeat_spin.setValue(2)
        window.wait_spin.setValue(0)
        window.start_btn.click()
        assert window._recording_active and window._series_active
        requests = [line for line in recent(80) if "[request]" in line]
        assert requests and '"trigger": "Start button (program)"' in requests[-1]
        qapp.processEvents()
        window._record_timer.stop()
        window._on_recording_time_elapsed()
        for _ in range(4):
            window._poll_collection_files()
        assert _pump_until(qapp, lambda: window._recordings_completed >= 1 and window._recording_active)
        assert "Repeat: recording 2 of 2." in window.log_edit.toPlainText()
        requests = [line for line in recent(120) if "[request]" in line]
        assert '"trigger": "repeat series (2 of 2)"' in requests[-1]
    finally:
        window._end_series()
        _close(window, qapp)


def test_an_excel_collection_logs_which_buttons_started_it(qapp, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QDialog

    from crfs_iq_recorder.cellular_panel import PlanPreviewDialog
    from crfs_iq_recorder.diagnostic_log import recent

    window = _window(tmp_path)
    try:
        panel = _ready_excel_panel(window, tmp_path, qapp)
        monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_has_workbook", lambda _path: False)
        sessions = []
        monkeypatch.setattr(panel.runner, "start_session", lambda *_a, **kwargs: sessions.append(kwargs.get("trigger")))

        def confirm(dialog):
            dialog.start_button.click()
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(PlanPreviewDialog, "exec", confirm)
        panel.start_btn.click()
        assert _pump_until(qapp, lambda: sessions)
        assert sessions == ["Start button (program), then preview Start (program)"]
        monkeypatch.setattr(panel.runner, "resume", lambda: None)
        panel.resume_btn.setEnabled(True)
        panel.resume_btn.click()
        lines = [line for line in recent(40) if "[excel_request]" in line]
        assert lines and '"trigger": "Resume button (program)"' in lines[-1]
    finally:
        _close(window, qapp)


def test_manual_status_line_is_neutral_while_the_excel_collection_records(qapp, tmp_path):
    from crfs_iq_recorder.cellular_runner import OWNER, S_RUNNING
    from crfs_iq_recorder.recording_status import FINALIZING, IDLE, PHASE_LABELS, RECORDING, SUBMITTING

    window = _window(tmp_path)
    try:
        panel = window.cellular_panel
        idle = PHASE_LABELS[IDLE]
        assert window.set_external_owner(OWNER) is None
        panel.runner.state = S_RUNNING
        panel._on_status({"state": S_RUNNING, "text": "Recording row 1 of 2"})
        for phase, message in ((SUBMITTING, None), (RECORDING, "Recording 2 s…"), (FINALIZING, "Waiting for files…")):
            window._set_phase(phase, message)
            window._set_msg("Waiting for the sensor's files…")
            assert window.phase_label.text() == idle, f"{phase} leaked into the Manual tab"
            assert window.phase_label.objectName() == "muted"
            assert window.msg.isHidden()
            assert window.manual_reason.text() == "Excel Collection is running." and not window.manual_reason.isHidden()
        window.release_external_owner(OWNER)
        qapp.processEvents()
        assert window.phase_label.text() == idle and window.manual_reason.isHidden()
        window._set_phase(RECORDING, "Recording 2 s…")
        assert window.phase_label.text() == "Recording 2 s…", "the Manual tab's own states still show"
    finally:
        _close(window, qapp)
