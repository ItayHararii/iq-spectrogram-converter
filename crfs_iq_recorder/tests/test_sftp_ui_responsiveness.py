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


def test_cancelled_download_is_counted_as_cancelled_not_failed(qapp, tmp_path, monkeypatch):
    """Hardware (Node 100-18): after Cancel the transport raised paramiko's own
    SFTPError; the window said "1 failed." and opened a pop-up."""
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_client import DemoSftpBrowser
    from crfs_iq_recorder.sftp_window import SftpWindow

    class ParamikoSFTPError(Exception):
        pass

    started = threading.Event()

    def download_then_die(self, remote_path, local_path, progress=None, *, cancel=None, expected_size=0):
        started.set()
        for _ in range(100):
            if cancel is not None and cancel.is_set():
                raise ParamikoSFTPError("Garbage packet received")
            time.sleep(0.02)
        raise AssertionError("cancel never arrived")

    popups: list[object] = []
    monkeypatch.setattr(DemoSftpBrowser, "download", download_then_die)
    monkeypatch.setattr("crfs_iq_recorder.sftp_window.QMessageBox.warning", lambda *a, **k: popups.append(a))
    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10", download_dir=str(tmp_path)))
    try:
        assert _wait_listing(window, qapp, count=1) >= 1
        files = [entry for entry in window._entries if not entry.is_dir]
        window._begin_downloads(files[:1])
        deadline = time.time() + 3
        while time.time() < deadline and not (started.is_set() and window._dl_active):
            qapp.processEvents()
            time.sleep(0.02)
        window.cancel_btn.click()
        deadline = time.time() + 5
        while time.time() < deadline and (window._dl_active or window._dl_pending):
            qapp.processEvents()
            time.sleep(0.02)
        _pump(qapp, 0.1)
        assert window._dl_cancel_count == 1 and window._dl_fail == 0
        assert window.status.text() == "1 cancelled."
        assert popups == [], "a cancel is not an error"
        assert not window._busy and window.refresh_btn.isEnabled()
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


def test_list_failure_does_not_open_a_popup(qapp, monkeypatch):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow

    shown: list[object] = []
    monkeypatch.setattr(
        "crfs_iq_recorder.sftp_window.QMessageBox.warning",
        lambda *args, **kwargs: shown.append(args),
    )
    window = SftpWindow(ConnectionState(demo=False, host="192.0.2.1"))
    try:
        window._on_failed("SFTP connection failed: timed out")
        qapp.processEvents()
        assert shown == []
        text = window.empty_page.text()
        assert "Could not list files" in text
        assert "timed out" in text
        assert window.status.text() == "SFTP error."
    finally:
        window.close()
        qapp.processEvents()


def _waterfall_data(requested=(703.0, 713.0)):
    from datetime import datetime, timezone

    import numpy as np

    from crfs_iq_recorder.spectrogram_preview import WaterfallData

    n_freq, n_time = 32, 64
    return WaterfallData(
        power_db=np.full((n_freq, n_time), -60.0, dtype=np.float32),
        rgb=np.zeros((n_time, n_freq, 3), dtype=np.uint8),
        colorbar_rgb=np.zeros((256, 3), dtype=np.uint8),
        vmin=-100.0,
        vmax=-20.0,
        duration_s=0.5,
        sample_rate_hz=16e6,
        absolute=True,
        freq_lo=700.0,
        freq_hi=716.0,
        freq_unit="MHz",
        x_label="Frequency (MHz)",
        capture_start=datetime(2026, 11, 4, 22, 48, 50, tzinfo=timezone.utc),
        covered_mhz=(700.0, 716.0),
        requested_mhz=requested,
        markers=(),
        legend_lines=(),
        peak_dbfs=-20.0,
        whole_file_read=False,
    )


def test_interactive_preview_shows_view_note_readout_and_reset(qapp):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow
    from crfs_iq_recorder.spectrogram_preview import PreviewResult
    from crfs_iq_recorder.waterfall_view import IDLE_READOUT

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        _wait_listing(window, qapp, count=1)
        window.resize(1100, 700)
        data = _waterfall_data(requested=(691.0, 721.0))
        result = PreviewResult(
            png_bytes=b"",
            caption="First 0.500 s · centre 708 MHz",
            sampled_seconds=0.5,
            sample_rate=16e6,
            peak_dbfs=-20.0,
            layout="xdat",
            whole_file_read=False,
            cache_key=("k", 1, 0, 691_000_000, 721_000_000),
            data=data,
        )
        window._preview_token = 3
        window._on_preview_ready(3, result)
        qapp.processEvents()
        assert window.preview_stack.currentIndex() == 1
        assert window.preview_view.data is data
        assert window.preview_note.isVisibleTo(window)
        assert "hatched 691-700 MHz and 716-721 MHz not recorded" in window.preview_note.text()
        assert "start 05 Nov 2026" in window.preview_caption.text() or "start 04 Nov 2026" in window.preview_caption.text()
        assert window.preview_readout.text() == IDLE_READOUT
        assert not window.preview_reset.isEnabled()
        # Zooming enables Reset; the readout follows the view's signal; Reset restores the full view.
        window.preview_view.zoom_by(2.0, 0.5)
        qapp.processEvents()
        assert window.preview_reset.isEnabled()
        window.preview_view.readout_changed.emit("00:48:50.100000 local (+100.000 ms)  ·  708.000 MHz  ·  -60.0 dBFS/bin")
        assert window.preview_readout.text().endswith("-60.0 dBFS/bin")
        window.preview_view.readout_changed.emit("")
        assert window.preview_readout.text() == IDLE_READOUT
        window.preview_reset.click()
        qapp.processEvents()
        assert not window.preview_view.zoomed
        assert not window.preview_reset.isEnabled()
        # A cancelled preview keeps the picture; a real failure returns to the plain label and hides the extra lines.
        window._on_preview_failed(3, "Preview cancelled.")
        assert window.preview_stack.currentIndex() == 1
        window._on_preview_failed(3, "Could not read the file: connection lost.")
        assert window.preview_caption.text() == "Could not read the file: connection lost."
        assert window.preview_stack.currentIndex() == 0
        assert window.preview_view.data is None
        assert not window.preview_note.isVisibleTo(window)
        assert not window.preview_readout.isVisibleTo(window)
        assert not window.preview_reset.isEnabled()
    finally:
        window.close()
        qapp.processEvents()


def test_sidecar_selection_explains_without_starting_a_preview(qapp):
    from datetime import datetime

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_client import RemoteEntry
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        _wait_listing(window, qapp, count=1)
        before = window._preview_token
        entry = RemoteEntry(name="file_221054_0001.xhdr", path="/x/file_221054_0001.xhdr", is_dir=False, size=1200, modified=datetime(2026, 11, 4, 22, 10, 54))
        window._request_preview(entry)
        qapp.processEvents()
        assert window._preview_token == before + 1
        assert not window._preview_active
        assert window._preview_pending is None
        assert window.preview_caption.text() == "Header and marker files have no samples."
        assert "Select the .xdat file beside it" in window.preview_image.text()
        assert window.preview_stack.currentIndex() == 0
    finally:
        window.close()
        qapp.processEvents()


def test_preview_cache_key_includes_the_requested_range(qapp):
    from datetime import datetime
    from types import SimpleNamespace

    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_client import RemoteEntry
    from crfs_iq_recorder.sftp_window import SftpWindow

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        _wait_listing(window, qapp, count=1)
        entry = RemoteEntry(name="a.xdat", path="/x/a.xdat", is_dir=False, size=10, modified=datetime(2026, 11, 4, 22, 10, 54))
        assert window._requested_range(entry) == (None, None)
        window._matched[entry.path] = SimpleNamespace(start_hz=703_000_000, end_hz=713_000_000, center_hz=708_000_000, bandwidth_hz=10_000_000)
        assert window._requested_range(entry) == (703_000_000, 713_000_000)
        window._matched[entry.path] = SimpleNamespace(start_hz=None, end_hz=713_000_000)
        assert window._requested_range(entry) == (None, None)
    finally:
        window.close()
        qapp.processEvents()


def test_demo_selection_produces_an_interactive_waterfall(qapp):
    """End to end on the demo transport: select a file -> worker -> array -> interactive view."""
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import SftpWindow
    from crfs_iq_recorder.waterfall_view import IDLE_READOUT

    window = SftpWindow(ConnectionState(demo=True, host="192.0.2.10"))
    try:
        _wait_listing(window, qapp, count=1)
        window.resize(1100, 700)
        window.table.setCurrentItem(window.table.topLevelItem(0))
        window.table.topLevelItem(0).setSelected(True)
        deadline = time.time() + 20
        while time.time() < deadline and window.preview_stack.currentIndex() != 1:
            qapp.processEvents()
            time.sleep(0.02)
        assert window.preview_stack.currentIndex() == 1, window.preview_caption.text()
        data = window.preview_view.data
        assert data is not None and data.n_time > 0 and data.n_freq > 0
        assert window.preview_caption.text().startswith("First")
        assert window.preview_readout.text() == IDLE_READOUT
        assert window.preview_view.window is not None and not window.preview_view.zoomed
        # The result is cached under the file key plus the requested range.
        assert any(len(k) == 5 for k in window._preview_cache)
    finally:
        window.close()
        qapp.processEvents()
