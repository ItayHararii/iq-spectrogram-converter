"""Optional GUI smoke test. Uses offscreen Qt; never talks to a sensor."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crfs_iq_recorder.request_builder import default_payload


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from crfs_iq_recorder.gui import configure_appearance

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)
    yield app


@pytest.fixture(autouse=True)
def isolated_recordings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crfs_iq_recorder.recording_history.recordings_path",
        lambda: tmp_path / "recordings.json",
    )


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crfs_iq_recorder.paths.settings_path",
        lambda: tmp_path / "settings.json",
    )


@pytest.fixture(autouse=True)
def no_explorer(monkeypatch):
    monkeypatch.setattr(
        "crfs_iq_recorder.sftp_window.QDesktopServices.openUrl",
        lambda *_args, **_kwargs: True,
    )


def test_gui_default_recording_request(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        payload, error = window._try_payload()
        assert error is None
        assert payload == default_payload()
        scan = payload["remote_recording_scans"][0]
        assert scan["duration"] == scan["rate"] == scan["capture_length"] == 0.1
        assert scan["recording_format"] == "WAVE"
        assert "7.631375" in window.size_label.text()
        assert "Estimate" in window.size_label.text()
        assert window._conn.demo is True
        assert window.model_value.text() == "R100-18"
        assert window.firmware_value.text() == "2.25-325"
        assert window.serial_value.text() == "rfeyeDEMO"
        assert window.status_value.text() == "Demo"
    finally:
        window.close()


def test_gui_recording_time_fills_all_timing_fields(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.time_edit.setText("2.5")
        payload, error = window._try_payload()
        assert error is None
        scan = payload["remote_recording_scans"][0]
        assert scan["duration"] == 2.5
        assert scan["rate"] == 2.5
        assert scan["capture_length"] == 2.5
    finally:
        window.close()


def test_gui_start_end_mhz_converts_to_hz(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.mode_start.setChecked(True)
        window.field_a.setText("793.75")
        window.field_b.setText("806.25")
        payload, error = window._try_payload()
        assert error is None
        scan = payload["remote_recording_scans"][0]
        assert scan["center_frequency"] == 800_000_000
        assert scan["bandwidth"] == 12_500_000
        assert scan["recording_format"] == "WAVE"
    finally:
        window.close()


def test_sftp_window_opens_today_local_date_folder(qapp):
    from datetime import datetime

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_paths import today_remdata_directory
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        expected = f"/mnt/1/remdata/{datetime.now():%Y%m%d}/"
        assert window.path_label.text() == expected
        assert window.path_label.text() == today_remdata_directory()
    finally:
        window.close()


def test_sftp_window_fills_demo_listing_without_crashing(qapp):
    import time

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        deadline = time.time() + 3
        while time.time() < deadline and window.table.rowCount() == 0:
            qapp.processEvents()
            time.sleep(0.05)
        assert window.table.rowCount() >= 1
        assert window.table.item(0, 0) is not None
        window.refresh()
        deadline = time.time() + 3
        while time.time() < deadline and window._busy:
            qapp.processEvents()
            time.sleep(0.05)
        qapp.processEvents()
        assert window.table.rowCount() >= 1
    finally:
        window.close()
        qapp.processEvents()


def test_gui_marks_recording_finished_after_recording_time(qapp):
    import time

    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.time_edit.setText("0.1")
        window._on_start()
        assert window.start_btn.text().startswith("Recording")
        deadline = time.time() + 3
        while time.time() < deadline and window.start_btn.text() != "Start Recording":
            qapp.processEvents()
            time.sleep(0.05)
        assert window.start_btn.text() == "Start Recording"
        assert window.start_btn.isEnabled()
        text = window.msg.text().lower()
        assert "elapsed" in text
        assert "finished" not in text
        assert window._http_in_flight is False
        assert window._record_phase in {"finalizing", "unknown", "confirmed"}
        window._on_start()
        assert window.start_btn.text().startswith("Recording")
    finally:
        window.close()
        qapp.processEvents()


def test_hung_http_does_not_block_the_next_recording(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window._http_in_flight = True
        window._record_client = window._client()
        window._on_recording_time_elapsed()
        assert window._http_in_flight is False
        assert window.start_btn.isEnabled()
        window._on_start()
        assert window.start_btn.text().startswith("Recording")
    finally:
        window.close()
        qapp.processEvents()


def test_gui_saves_recording_parameters(qapp, tmp_path):
    from crfs_iq_recorder.gui import MainWindow
    from crfs_iq_recorder.recording_history import load_recordings

    window = MainWindow(demo=True)
    try:
        window.mode_start.setChecked(True)
        window.field_a.setText("790")
        window.field_b.setText("810")
        window.time_edit.setText("4")
        window._on_start()
        records = load_recordings(tmp_path / "recordings.json")
        assert len(records) == 1
        rec = records[0]
        assert rec.start_hz == 790_000_000
        assert rec.end_hz == 810_000_000
        assert rec.center_hz == 800_000_000
        assert rec.bandwidth_hz == 20_000_000
        assert rec.duration_s == 4
        assert rec.host == window._conn.host
    finally:
        window.close()
        qapp.processEvents()


def test_sftp_table_shows_saved_recording_parameters(qapp, tmp_path):
    import time
    from datetime import datetime

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.recording_history import add_recording, new_recording
    from crfs_iq_recorder.sftp_window import SftpWindow

    add_recording(
        new_recording(
            host="192.0.2.10",
            start_hz=790_000_000,
            end_hz=810_000_000,
            center_hz=800_000_000,
            bandwidth_hz=20_000_000,
            duration_s=4,
            started_at=datetime.now(),
            demo=True,
        )
    )
    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        deadline = time.time() + 3
        while time.time() < deadline and window.table.rowCount() == 0:
            qapp.processEvents()
            time.sleep(0.05)
        assert window.table.columnCount() == 9
        shown = [
            (
                window.table.item(row, 4).text() if window.table.item(row, 4) else "",
                window.table.item(row, 8).text() if window.table.item(row, 8) else "",
            )
            for row in range(window.table.rowCount())
        ]
        assert any(start == "790" and duration == "4" for start, duration in shown)
    finally:
        window.close()
        qapp.processEvents()


def test_sftp_downloads_multiple_files(qapp, tmp_path):
    import time

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        deadline = time.time() + 3
        while time.time() < deadline and window.table.rowCount() < 2:
            qapp.processEvents()
            time.sleep(0.05)
        files = [entry for entry in window._entries if not entry.is_dir]
        assert len(files) >= 2
        jobs = [(entry, str(tmp_path / entry.name)) for entry in files[:2]]
        window._queue_downloads(jobs)
        deadline = time.time() + 4
        while time.time() < deadline:
            qapp.processEvents()
            if (
                (tmp_path / files[0].name).is_file()
                and (tmp_path / files[1].name).is_file()
                and not window._dl_active
            ):
                break
            time.sleep(0.05)
        assert (tmp_path / files[0].name).is_file()
        assert (tmp_path / files[1].name).is_file()
        assert window.download_btn.text() == "Download"
    finally:
        window.close()
        qapp.processEvents()


def test_sftp_begin_downloads_uses_fixed_folder_without_dialogs(qapp, tmp_path, monkeypatch):
    import time

    from PySide6.QtWidgets import QFileDialog

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    def _no_dialog(*_args, **_kwargs):
        raise AssertionError("Downloads must not ask for a destination.")

    monkeypatch.setattr(QFileDialog, "getSaveFileName", _no_dialog)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", _no_dialog)

    dest = tmp_path / "CRFS IQ Recorder" / "Recordings"
    window = SftpWindow(
        ConnectionState(demo=True, host="192.0.2.10", download_dir=str(dest))
    )
    try:
        deadline = time.time() + 3
        while time.time() < deadline and window.table.rowCount() < 2:
            qapp.processEvents()
            time.sleep(0.05)
        files = [entry for entry in window._entries if not entry.is_dir]
        assert len(files) >= 2
        window._begin_downloads(files[:2])
        deadline = time.time() + 4
        while time.time() < deadline:
            qapp.processEvents()
            if (
                (dest / files[0].name).is_file()
                and (dest / files[1].name).is_file()
                and not window._dl_active
                and not window._busy
            ):
                break
            time.sleep(0.05)
        assert dest.is_dir()
        assert (dest / files[0].name).is_file()
        assert (dest / files[1].name).is_file()
        existing = dest / files[0].name
        window._begin_downloads(files[:1])
        deadline = time.time() + 4
        renamed = dest / f"{Path(files[0].name).stem} (2){Path(files[0].name).suffix}"
        while time.time() < deadline:
            qapp.processEvents()
            if renamed.is_file() and not window._dl_active and not window._busy:
                break
            time.sleep(0.05)
        assert existing.is_file()
        assert renamed.is_file()
    finally:
        window.close()
        qapp.processEvents()


def test_settings_dialog_shows_and_returns_download_folder(qapp, tmp_path):
    from crfs_iq_recorder.connection_dialog import ConnectionDialog
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.paths import default_download_dir

    dialog = ConnectionDialog(ConnectionState())
    try:
        assert dialog.download_edit.text() == str(default_download_dir())
        chosen = tmp_path / "My Recordings"
        dialog.download_edit.setText(str(chosen))
        result = dialog.result_state()
        assert result.download_dir == str(chosen)
        assert not hasattr(dialog, "demo_check")
        assert not hasattr(dialog, "sensor_combo")
        assert not hasattr(dialog, "sftp_same")
        assert dialog.sftp_user.isEnabled()
        assert dialog.sftp_pass.isEnabled()
        assert result.sftp_same_as_http is False
        assert dialog.scroll is not None
        from PySide6.QtWidgets import QDialogButtonBox

        save = dialog.buttons.button(QDialogButtonBox.StandardButton.Ok)
        assert save is not None
        assert save.text() == "Save"
        dialog.show()
        qapp.processEvents()
        dialog.resize(480, 320)
        qapp.processEvents()
        assert dialog.buttons.isVisible()
        assert dialog.scroll.isVisible()
    finally:
        dialog.close()


def test_gui_mode_switch_converts_fields(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.mode_center.setChecked(True)
        window.field_a.setText("800")
        window.field_b.setText("12.5")
        window.mode_start.setChecked(True)
        assert window.field_a.text() == "793.75"
        assert window.field_b.text() == "806.25"
        payload, error = window._try_payload()
        assert error is None
        scan = payload["remote_recording_scans"][0]
        assert scan["center_frequency"] == 800_000_000
        assert scan["bandwidth"] == 12_500_000
        window.mode_center.setChecked(True)
        assert window.field_a.text() == "800"
        assert window.field_b.text() == "12.5"
    finally:
        window.close()


def test_gui_rejects_invalid_frequency(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.field_a.setText("abc")
        payload, error = window._try_payload()
        assert payload is None
        assert error
        window.field_a.setText("800")
        window.field_b.setText("12.5")
        window.time_edit.setText("0")
        payload, error = window._try_payload()
        assert payload is None
    finally:
        window.close()


def test_gui_start_uses_unique_task_id_and_blocks_duplicates(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.time_edit.setText("2")
        window._on_start()
        assert window._recording_active
        first_gen = window._record_gen
        window._on_start()
        assert window._record_gen == first_gen
        assert window.start_btn.isEnabled() is False
    finally:
        window.close()
        qapp.processEvents()


def test_stale_sensor_info_does_not_overwrite(qapp):
    from crfs_iq_recorder.gui import MainWindow
    from crfs_iq_recorder.sensor_info import SensorInfo

    window = MainWindow(demo=True)
    try:
        window._conn.host = "192.0.2.10"
        window._info_token = 3
        window._apply_sensor_info(
            SensorInfo(
                host="192.0.2.10",
                model="R40-8",
                firmware="2.25-325",
                serial="rfeyeTEST",
                status="Connected",
                connected=True,
            )
        )
        window._show_sensor_info_result(
            (
                2,
                "192.0.2.12",
                SensorInfo(
                    host="192.0.2.12",
                    model="StaleModel",
                    firmware="old",
                    serial="stale",
                    status="Connected",
                    connected=True,
                ),
            )
        )
        assert window.model_value.text() != "StaleModel"
        assert window.serial_value.text() == "rfeyeTEST"
        window._show_sensor_info(
            SensorInfo(
                host="192.0.2.12",
                model="WrongHost",
                firmware="x",
                serial="y",
                status="Connected",
                connected=True,
            )
        )
        assert window.model_value.text() != "WrongHost"
    finally:
        window.close()


def test_sftp_lists_newest_files_first_without_new_on_first_listing(qapp):
    import time
    from datetime import timedelta

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_client import RemoteEntry
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        deadline = time.time() + 3
        while time.time() < deadline and window.table.rowCount() == 0:
            qapp.processEvents()
            time.sleep(0.05)
        files = [entry for entry in window._entries if not entry.is_dir]
        assert files
        from crfs_iq_recorder.listing_sort import entry_newest_time

        newest_times = [entry_newest_time(entry) for entry in files]
        assert newest_times == sorted(newest_times, reverse=True)
        new_flags = [
            window.table.item(row, 0).text()
            for row in range(window.table.rowCount())
            if window.table.item(row, 0)
        ]
        assert all(flag == "" for flag in new_flags)
        header = window.table.horizontalHeaderItem(3)
        assert header is not None
        assert "▼" in header.text()
        keep_name = files[0].name
        window.table.selectRow(0)
        extra = RemoteEntry(
            "iq_brand_new.wav",
            files[0].path.rsplit("/", 1)[0] + "/iq_brand_new.wav",
            False,
            100,
            files[0].modified + timedelta(seconds=30) if files[0].modified else None,
        )
        window._fill(window._entries + [extra])
        assert window.table.item(0, 1).text() == "iq_brand_new.wav"
        assert window.table.item(0, 0).text() == "NEW"
        selected = [
            window._entry_at(index.row()).name
            for index in window.table.selectionModel().selectedRows()
            if window._entry_at(index.row()) is not None
        ]
        assert keep_name in selected
        assert "iq_brand_new.wav" not in selected
    finally:
        window.close()
        qapp.processEvents()


def test_gui_ghz_unit_converts_to_hz(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.mode_center.setChecked(True)
        window.unit_combo.setCurrentText("GHz")
        window.field_a.setText("0.8")
        window.field_b.setText("0.0125")
        payload, error = window._try_payload()
        assert error is None
        scan = payload["remote_recording_scans"][0]
        assert scan["center_frequency"] == 800_000_000
        assert scan["bandwidth"] == 12_500_000
        assert window.unit_a.text() == "GHz"
        assert "Center frequency" in window.label_a.text()
    finally:
        window.close()


def test_gui_verified_format_is_sent(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.format_combo.setCurrentText("NCP")
        payload, error = window._try_payload()
        assert error is None
        assert payload["remote_recording_scans"][0]["recording_format"] == "NCP"
        labels = [window.format_combo.itemText(i) for i in range(window.format_combo.count())]
        assert labels == ["WAVE", "XDAT", "NCP", "HDF5"]
    finally:
        window.close()


def test_gui_remembers_band_time_unit_format(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        window.mode_start.setChecked(True)
        window.unit_combo.setCurrentText("GHz")
        window.field_a.setText("0.79")
        window.field_b.setText("0.81")
        window.time_edit.setText("4")
        window.format_combo.setCurrentText("XDAT")
    finally:
        window.close()
        qapp.processEvents()

    window2 = MainWindow(demo=True)
    try:
        assert window2.mode_start.isChecked()
        assert window2.unit_combo.currentText() == "GHz"
        assert window2.time_edit.text() == "4"
        assert window2.format_combo.currentText() == "XDAT"
        payload, error = window2._try_payload()
        assert error is None
        scan = payload["remote_recording_scans"][0]
        assert scan["center_frequency"] == 800_000_000
        assert scan["bandwidth"] == 20_000_000
        assert scan["recording_format"] == "XDAT"
        assert scan["duration"] == 4
    finally:
        window2.close()


def test_download_this_recording_expands_split_parts(qapp):
    import time

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        deadline = time.time() + 3
        while time.time() < deadline and window.table.rowCount() == 0:
            qapp.processEvents()
            time.sleep(0.05)
        row = next(i for i, entry in enumerate(window._entries) if entry.name.endswith("_0001.wav"))
        window.table.selectRow(row)
        window._on_selection_changed()
        assert window.download_btn.text() == "Download this recording"
        captured = []
        window._begin_downloads = lambda files: captured.extend(files)
        window._download_selected()
        names = {entry.name for entry in captured}
        assert "iq_20260910_161629_0001.wav" in names
        assert "iq_20260910_161629_0002.wav" in names
        assert window.open_folder_btn.text() == "Open recordings folder"
    finally:
        window.close()
        qapp.processEvents()


def test_open_recordings_folder_uses_saved_download_dir(qapp, tmp_path, monkeypatch):
    opened = []

    def _open(url):
        opened.append(url.toLocalFile() if hasattr(url, "toLocalFile") else str(url))
        return True

    monkeypatch.setattr("crfs_iq_recorder.sftp_window.QDesktopServices.openUrl", _open)
    dest = tmp_path / "CRFS IQ Recorder" / "Recordings"
    dest.mkdir(parents=True)
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10", download_dir=str(dest)))
    try:
        window._open_recordings_folder()
        assert opened
        assert Path(opened[0]).resolve() == dest.resolve()
    finally:
        window.close()
        qapp.processEvents()


def _luminance(color) -> float:
    return 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()


def test_combo_popups_have_readable_contrast(qapp):
    from PySide6.QtGui import QPalette

    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        form = window.format_combo.view().palette()
        form_bg = form.color(QPalette.ColorRole.Base)
        form_fg = form.color(QPalette.ColorRole.Text)
        assert abs(_luminance(form_bg) - _luminance(form_fg)) > 80
        assert _luminance(form_bg) > 180
        assert _luminance(form_fg) < 80
        units = [window.unit_combo.itemText(i) for i in range(window.unit_combo.count())]
        assert units == ["kHz", "MHz", "GHz"]
        assert not hasattr(window, "sensor_combo")
    finally:
        window.close()


def test_recording_fields_do_not_overlap(qapp):
    from PySide6.QtCore import QRect

    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        for size in ((1100, 720), (880, 560)):
            window.resize(*size)
            window.show()
            qapp.processEvents()
            widgets = [window.field_a, window.field_b, window.format_combo, window.time_edit]
            rects = []
            for widget in widgets:
                top = widget.mapTo(window, widget.rect().topLeft())
                rects.append(QRect(top.x(), top.y(), widget.width(), widget.height()))
            for index, first in enumerate(rects):
                for second in rects[index + 1 :]:
                    assert not first.intersects(second), (size, first, second)
            assert window.field_a.height() >= 28
            assert "800" in window.field_a.text() or window.field_a.text()
            assert window.format_combo.currentText() == "WAVE"
    finally:
        window.close()


def test_sftp_up_opens_parent_folder(qapp):
    import time

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_paths import parent_directory, today_remdata_directory
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        deadline = time.time() + 3
        while time.time() < deadline and window.table.rowCount() == 0:
            qapp.processEvents()
            time.sleep(0.05)
        today = today_remdata_directory()
        assert window.path_label.text().rstrip("/") == today.rstrip("/")

        def wait_path(expected: str) -> bool:
            want = expected.rstrip("/") or "/"
            deadline = time.time() + 3
            while time.time() < deadline:
                qapp.processEvents()
                current = window.path_label.text().rstrip("/") or "/"
                if current == want and not window._busy:
                    return True
                time.sleep(0.05)
            return False

        window._go_parent()
        parent = parent_directory(today)
        assert wait_path(parent)
        names = [
            window.table.item(row, 1).text()
            for row in range(window.table.rowCount())
            if window.table.item(row, 1)
        ]
        assert any(name.rstrip("/").isdigit() and len(name.rstrip("/")) == 8 for name in names)
        today_name = today.rstrip("/").rsplit("/", 1)[-1]
        assert names[0].rstrip("/") == today_name
        folder_dates = [name.rstrip("/") for name in names if name.rstrip("/").isdigit() and len(name.rstrip("/")) == 8]
        assert folder_dates == sorted(folder_dates, reverse=True)
        for expected in ("/mnt/1/", "/mnt/", "/"):
            window._go_parent()
            assert wait_path(expected)
        window._go_parent()
        qapp.processEvents()
        assert (window.path_label.text().rstrip("/") or "/") == "/"
    finally:
        window.close()
        qapp.processEvents()


def test_theme_toggle_persists(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder.gui import MainWindow
    from crfs_iq_recorder.paths import settings_path
    from crfs_iq_recorder.theme import DARK_MODE, LIGHT_MODE, current_mode

    window = MainWindow(demo=True)
    try:
        assert current_mode() == LIGHT_MODE
        window._toggle_theme()
        assert current_mode() == DARK_MODE
        assert window.theme_btn.text() == ""
        assert window.theme_btn.toolTip() == "Switch to Light Mode"
        assert not window.theme_btn.icon().isNull()
        saved = settings_path().read_text(encoding="utf-8")
        assert '"ui_theme": "dark"' in saved
        window._toggle_theme()
        assert current_mode() == LIGHT_MODE
        assert window.theme_btn.toolTip() == "Switch to Dark Mode"
    finally:
        from crfs_iq_recorder.gui import configure_appearance

        configure_appearance(qapp, LIGHT_MODE)
        window.close()


def test_sftp_column_sort_persists_until_directory_change(qapp):
    import time

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.listing_sort import COL_NAME, COL_SIZE
    from crfs_iq_recorder.sftp_paths import parent_directory, today_remdata_directory
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        deadline = time.time() + 3
        while time.time() < deadline and (window.table.rowCount() == 0 or window._busy):
            qapp.processEvents()
            time.sleep(0.05)
        assert window.table.rowCount() > 0
        assert window._user_sort is False
        window._on_header_clicked(COL_NAME)
        qapp.processEvents()
        names = [
            window.table.item(row, 1).text().rstrip("/")
            for row in range(window.table.rowCount())
            if window.table.item(row, 1)
        ]
        assert names == sorted(names, key=str.casefold)
        assert "▲" in window.table.horizontalHeaderItem(COL_NAME).text()
        window._on_header_clicked(COL_NAME)
        qapp.processEvents()
        names_desc = [
            window.table.item(row, 1).text().rstrip("/")
            for row in range(window.table.rowCount())
            if window.table.item(row, 1)
        ]
        assert names_desc == sorted(names, key=str.casefold, reverse=True)
        window._on_header_clicked(COL_SIZE)
        qapp.processEvents()
        assert window._user_sort is True
        assert window._sort_column == COL_SIZE
        snapshot = list(window._entries)
        window._fill(snapshot)
        assert window._user_sort is True
        assert window._sort_column == COL_SIZE
        window._go_parent()
        expected = parent_directory(today_remdata_directory())
        deadline = time.time() + 3
        while time.time() < deadline:
            qapp.processEvents()
            current = window.path_label.text().rstrip("/") or "/"
            if current == expected.rstrip("/") and not window._busy:
                break
            time.sleep(0.05)
        assert window._user_sort is False
        assert "▼" in window.table.horizontalHeaderItem(3).text()
    finally:
        window.close()
        qapp.processEvents()
