"""Optional GUI smoke test. Uses offscreen Qt; never talks to a sensor."""

from __future__ import annotations

import os

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
        assert "finished" in window.msg.text().lower()
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
        assert window.table.columnCount() == 8
        shown = [
            (
                window.table.item(row, 3).text() if window.table.item(row, 3) else "",
                window.table.item(row, 7).text() if window.table.item(row, 7) else "",
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
        assert window.download_btn.text() == "Download Selected Files"
    finally:
        window.close()
        qapp.processEvents()
