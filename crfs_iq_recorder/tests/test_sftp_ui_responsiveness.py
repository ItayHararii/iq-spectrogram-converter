"""Sensor Files stays interactive while SFTP and waterfall work run elsewhere."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from crfs_iq_recorder.sftp_client import SFTP_CONNECT_TIMEOUT_S, SFTP_READ_TIMEOUT_S, SftpError


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
    monkeypatch.setattr(
        "crfs_iq_recorder.recording_history.recordings_path",
        lambda: tmp_path / "recordings.json",
    )
    monkeypatch.setattr(
        "crfs_iq_recorder.paths.settings_path",
        lambda: tmp_path / "settings.json",
    )
    monkeypatch.setattr(
        "crfs_iq_recorder.sftp_window.QDesktopServices.openUrl",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "crfs_iq_recorder.sftp_window.QMessageBox.warning",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "crfs_iq_recorder.sftp_window.QMessageBox.information",
        lambda *_args, **_kwargs: None,
    )


def _pump(qapp, seconds: float = 0.25) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)


def _wait_listing(window, qapp, count=1, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if window.table.topLevelItemCount() >= count and not window._busy:
            return window.table.topLevelItemCount()
        time.sleep(0.05)
    return window.table.topLevelItemCount()


def _hanging_open(_state, *, cancel=None, on_client=None):
    if on_client is not None:
        try:
            on_client(None)
        except Exception:
            pass
    if cancel is not None:
        cancel.wait(30)
    raise SftpError("SFTP cancelled.")


def test_connect_timeouts_are_bounded():
    assert SFTP_CONNECT_TIMEOUT_S <= 8
    assert SFTP_READ_TIMEOUT_S <= 20


def test_close_during_hanging_connect_returns_immediately(qapp, monkeypatch):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    started = threading.Event()

    def hanging(state, *, cancel=None, on_client=None):
        started.set()
        return _hanging_open(state, cancel=cancel, on_client=on_client)

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", hanging)
    window = SftpWindow(ConnectionState(demo=False, host="192.0.2.1"))
    try:
        deadline = time.time() + 2
        while time.time() < deadline and not started.is_set():
            qapp.processEvents()
            time.sleep(0.02)
        assert started.is_set()
        assert window.open_folder_btn.isEnabled()
        t0 = time.perf_counter()
        window.close()
        qapp.processEvents()
        assert time.perf_counter() - t0 < 1.0
        assert window._closing
    finally:
        window.close()
        qapp.processEvents()


def test_open_recordings_folder_while_connecting(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    opened: list[str] = []

    def _open(url):
        opened.append(url.toLocalFile() if hasattr(url, "toLocalFile") else str(url))
        return True

    monkeypatch.setattr("crfs_iq_recorder.sftp_window.QDesktopServices.openUrl", _open)
    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", _hanging_open)
    dest = tmp_path / "Recordings"
    dest.mkdir()
    window = SftpWindow(
        ConnectionState(demo=False, host="192.0.2.1", download_dir=str(dest))
    )
    try:
        _pump(qapp, 0.2)
        assert window.open_folder_btn.isEnabled()
        window._open_recordings_folder()
        assert opened
        assert Path(opened[0]).resolve() == dest.resolve()
        t0 = time.perf_counter()
        window.close()
        assert time.perf_counter() - t0 < 1.0
    finally:
        window.close()
        qapp.processEvents()


def test_demo_listing_independent_of_hanging_connect(qapp, monkeypatch):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    original = __import__("crfs_iq_recorder.sftp_window", fromlist=["_open_browser"])._open_browser

    def maybe_hang(state, *, cancel=None, on_client=None):
        if state.demo:
            return original(state, cancel=cancel, on_client=on_client)
        return _hanging_open(state, cancel=cancel, on_client=on_client)

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", maybe_hang)
    hanging = SftpWindow(ConnectionState(demo=False, host="192.0.2.1"))
    demo = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        assert _wait_listing(demo, qapp, count=1) >= 1
        t0 = time.perf_counter()
        hanging.close()
        qapp.processEvents()
        assert time.perf_counter() - t0 < 1.0
        assert demo.table.topLevelItemCount() >= 1
    finally:
        hanging.close()
        demo.close()
        qapp.processEvents()


def test_late_preview_result_ignored_after_selection_change(qapp):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow
    from crfs_iq_recorder.spectrogram_preview import PreviewResult

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        _wait_listing(window, qapp, count=1)
        before = window.preview_caption.text()
        window._preview_token = 9
        window._on_preview_ready(
            1,
            PreviewResult(
                png_bytes=b"",
                caption="stale waterfall",
                sampled_seconds=0.1,
                sample_rate=48000.0,
                peak_dbfs=None,
                layout="stereo",
                whole_file_read=False,
                cache_key=("stale", 1, 0),
            ),
        )
        assert window.preview_caption.text() == before
        window._on_preview_failed(1, "Preview cancelled.")
        assert window.preview_caption.text() == before
    finally:
        window.close()
        qapp.processEvents()


def test_closing_sftp_does_not_abort_active_downloads(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_client import DemoSftpBrowser
    from crfs_iq_recorder.sftp_window import SftpWindow

    started = threading.Event()

    def slow_download(
        self,
        remote_path,
        local_path,
        progress=None,
        *,
        cancel=None,
        expected_size=0,
    ):
        started.set()
        for _ in range(40):
            if cancel is not None and cancel.is_set():
                raise SftpError("Download cancelled.")
            time.sleep(0.05)
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(b"iq-data")
        if progress:
            progress(7, 7)

    monkeypatch.setattr(DemoSftpBrowser, "download", slow_download)
    dest = tmp_path / "Recordings"
    dest.mkdir()
    window = SftpWindow(
        ConnectionState(demo=True, host="192.0.2.10", download_dir=str(dest))
    )
    try:
        assert _wait_listing(window, qapp, count=1) >= 1
        files = [entry for entry in window._entries if not entry.is_dir]
        assert files
        window._begin_downloads(files[:1])
        deadline = time.time() + 2
        while time.time() < deadline and not (started.is_set() and window._dl_active):
            qapp.processEvents()
            time.sleep(0.02)
        assert window._dl_active
        local = Path(window._dl_active[0][1]._local)
        t0 = time.perf_counter()
        window.close()
        qapp.processEvents()
        assert time.perf_counter() - t0 < 1.0
        deadline = time.time() + 4
        while time.time() < deadline and not local.exists():
            qapp.processEvents()
            time.sleep(0.05)
        assert local.exists()
        assert local.read_bytes() == b"iq-data"
    finally:
        window.close()
        qapp.processEvents()


def test_main_window_close_does_not_wait_on_sftp(qapp, monkeypatch):
    from crfs_iq_recorder.gui import MainWindow

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", _hanging_open)
    monkeypatch.setattr("crfs_iq_recorder.gui._open_browser", _hanging_open)
    window = MainWindow(demo=True)
    try:
        window._conn.demo = False
        window._conn.host = "192.0.2.1"
        window._open_sftp()
        _pump(qapp, 0.2)
        assert window._sftp_win is not None
        t0 = time.perf_counter()
        window.close()
        qapp.processEvents()
        assert time.perf_counter() - t0 < 1.0
    finally:
        window.close()
        qapp.processEvents()
