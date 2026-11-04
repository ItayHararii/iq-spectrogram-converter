"""New SSH connections are paced, and held back after a connect timeout.

Hardware, 4 Nov 2026 (RFeye Node 100-18 over OpenVPN): after about six new SSH
connections in ten seconds the sensor dropped further ones (TCP connect timed
out, HTTP still answered) until nothing had been tried for ~15 s. Downloads that
retried at once kept the block in place, and 11 of 15 files were given up.
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from crfs_iq_recorder import sftp_client
from crfs_iq_recorder.download_util import already_saved
from crfs_iq_recorder.sftp_client import (
    SSH_CONNECT_WINDOW_S,
    SSH_COOLDOWN_MAX_S,
    SSH_COOLDOWN_S,
    ConnectPacer,
    ParamikoSftpBrowser,
    SftpConnectError,
    SftpError,
    connect_sftp,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _in_thread(target):
    """Run `target` in a thread; its result or SftpConnectError lands in the returned list."""
    results: list[object] = []

    def run() -> None:
        try:
            results.append(target())
        except SftpConnectError as exc:
            results.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, results


def test_at_most_three_new_connections_start_in_ten_seconds():
    clock = _Clock()
    pacer = ConnectPacer(clock=clock)
    for _ in range(3):
        pacer.acquire()
        pacer.reached()
    thread, results = _in_thread(lambda: pacer.acquire() or "started")
    time.sleep(0.5)
    assert results == [], "a fourth new connection in the same ten seconds waits"
    clock.now += SSH_CONNECT_WINDOW_S
    thread.join(2.0)
    assert results == ["started"]


def test_after_a_timeout_nothing_is_tried_for_a_cool_down_then_one_probe_at_a_time():
    clock = _Clock()
    pacer = ConnectPacer(clock=clock)
    pacer.acquire()
    assert pacer.timed_out() == SSH_COOLDOWN_S == 20.0
    with pytest.raises(SftpConnectError) as info:
        pacer.acquire()
    assert info.value.retry_after_s == pytest.approx(20.0)
    assert str(info.value) == "The sensor is not accepting new SFTP connections; trying again in 20 s."
    clock.now += SSH_COOLDOWN_S + SSH_CONNECT_WINDOW_S
    pacer.acquire()  # the probe
    thread, results = _in_thread(lambda: pacer.acquire() or "started")
    time.sleep(0.5)
    assert results == [], "while the probe is out, other connections wait"
    assert pacer.timed_out() == 40.0, "the probe timed out as well: a longer cool-down"
    thread.join(2.0)
    assert isinstance(results[0], SftpConnectError) and results[0].retry_after_s == pytest.approx(40.0)
    clock.now += 40.0 + SSH_CONNECT_WINDOW_S
    pacer.acquire()
    pacer.reached()
    assert pacer.cooling_down_s() == 0.0
    pacer.acquire()  # accepted again: no probe, only the window
    for _ in range(5):
        pacer.timed_out()
    assert pacer.timed_out() == SSH_COOLDOWN_MAX_S == 60.0


def test_cancel_ends_a_wait_for_the_window():
    pacer = ConnectPacer(clock=_Clock())
    for _ in range(3):
        pacer.acquire()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(SftpError, match="cancelled"):
        pacer.acquire(cancel)


@pytest.fixture
def paramiko_failing(monkeypatch):
    """paramiko whose connect raises `state['error']` (None: connects)."""
    state: dict[str, object] = {"error": None, "calls": 0}
    auth_error = type("AuthenticationException", (Exception,), {})

    class SSHClient:
        def set_missing_host_key_policy(self, _policy) -> None:
            pass

        def connect(self, **_kwargs) -> None:
            state["calls"] = int(state["calls"]) + 1
            if state["error"] is not None:
                raise state["error"]

        def get_transport(self):
            return None

        def close(self) -> None:
            pass

    module = types.SimpleNamespace(
        SSHClient=SSHClient,
        AutoAddPolicy=lambda: None,
        AuthenticationException=auth_error,
        SSHException=type("SSHException", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "paramiko", module)
    monkeypatch.setattr(sftp_client, "ParamikoSftpBrowser", lambda client: types.SimpleNamespace(client=client))
    state["auth_error"] = auth_error
    return state


def test_a_connect_timeout_starts_the_cool_down_and_the_next_connect_is_not_even_tried(paramiko_failing):
    paramiko_failing["error"] = socket.timeout("timed out")
    with pytest.raises(SftpConnectError) as info:
        connect_sftp("sensor", 22, "u", "p", timeout_s=1.0)
    assert str(info.value) == "SFTP connection failed: timed out"
    assert info.value.retry_after_s == pytest.approx(SSH_COOLDOWN_S, abs=1.0)
    with pytest.raises(SftpConnectError, match="not accepting new SFTP connections"):
        connect_sftp("sensor", 22, "u", "p", timeout_s=1.0)
    assert paramiko_failing["calls"] == 1, "no SYN is sent during the cool-down"


def test_a_refused_connection_can_be_retried_and_a_wrong_password_is_not_a_connection_problem(paramiko_failing):
    paramiko_failing["error"] = ConnectionRefusedError(10061, "No connection could be made")
    with pytest.raises(SftpConnectError) as info:
        connect_sftp("sensor", 22, "u", "p", timeout_s=1.0)
    assert info.value.retry_after_s == 0.0 and sftp_client.connect_pacer.cooling_down_s() == 0.0
    paramiko_failing["error"] = paramiko_failing["auth_error"]("bad password")
    with pytest.raises(SftpError) as login:
        connect_sftp("sensor", 22, "u", "p", timeout_s=1.0)
    assert not isinstance(login.value, SftpConnectError) and "login failed" in str(login.value)
    paramiko_failing["error"] = None
    assert connect_sftp("sensor", 22, "u", "p", timeout_s=1.0) is not None


# ---------------------------------------------------------------- download worker


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    yield QApplication.instance() or QApplication([])


class _Browser:
    def __init__(self, data: bytes = b"IQ" * 50) -> None:
        self.data = data

    def download(self, remote, local, progress=None, *, cancel=None, expected_size=0):
        Path(local).write_bytes(self.data)

    def close(self):
        pass


def _run_worker(qapp, worker, timeout=10.0):
    from PySide6.QtCore import QThread

    events: list[tuple] = []
    worker.waiting.connect(lambda _name, delay, _detail: events.append(("waiting", round(delay, 2))))
    worker.connected.connect(lambda _name: events.append(("connected",)))
    worker.unreachable.connect(lambda _name, _detail: events.append(("unreachable",)))
    worker.finished.connect(lambda _name, _path: events.append(("finished",)))
    worker.failed.connect(lambda _name, detail: events.append(("failed", detail)))
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.start()
    deadline = time.time() + timeout
    while thread.isRunning() and time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.01)
    assert thread.wait(2000)
    return events


def _sensor_refusing(monkeypatch, refusals: int):
    calls = {"n": 0}

    def open_browser(_state, cancel=None):
        calls["n"] += 1
        if calls["n"] <= refusals:
            raise SftpConnectError("The sensor is not accepting new SFTP connections; trying again in 1 s.", retry_after_s=0.05)
        return _Browser()

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", open_browser)
    monkeypatch.setattr("crfs_iq_recorder.sftp_window.random.uniform", lambda _a, _b: 0.0)
    return calls


def test_the_download_worker_waits_for_a_connection_and_then_downloads(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import FileDownloadWorker

    calls = _sensor_refusing(monkeypatch, 2)
    worker = FileDownloadWorker(ConnectionState(), "/mnt/1/remdata/x.wav", str(tmp_path / "x.wav"), "x.wav")
    events = _run_worker(qapp, worker)
    assert events == [("waiting", 0.05), ("waiting", 0.05), ("connected",), ("finished",)]
    assert calls["n"] == 3 and (tmp_path / "x.wav").is_file()


def test_the_download_worker_gives_up_after_its_patience_without_reporting_a_transfer_failure(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import FileDownloadWorker

    _sensor_refusing(monkeypatch, 10**6)
    worker = FileDownloadWorker(ConnectionState(), "/r/x.wav", str(tmp_path / "x.wav"), "x.wav", patience_s=0.3)
    events = _run_worker(qapp, worker)
    assert events[-1] == ("unreachable",) and all(kind == "waiting" for kind, *_ in events[:-1])
    assert not (tmp_path / "x.wav").exists()


def test_a_downloaded_wave_is_read_for_sensor_files(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.file_measurements import load_measurements, lookup
    from crfs_iq_recorder.iq_wav import crfs_style_wav_bytes
    from crfs_iq_recorder.sftp_window import FileDownloadWorker

    data = crfs_style_wav_bytes(frames=500, sample_rate=1000, center_hz=847e6, bandwidth_hz=30e6,
                                signal_time="2026-11-04T08:51:01.000Z")
    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", lambda _state, cancel=None: _Browser(data))
    remote = "/mnt/1/remdata/20261104/iq_20261104_085101_0001.wav"
    worker = FileDownloadWorker(ConnectionState(host="192.0.2.7"), remote, str(tmp_path / "iq.wav"), "iq.wav")
    assert _run_worker(qapp, worker)[-1] == ("finished",)
    reading = lookup(load_measurements(), remote, len(data))
    assert reading is not None and reading["duration_s"] == 0.5 and reading["bandwidth_hz"] == 30e6


def test_stop_waiting_ends_a_long_wait_quickly(qapp, tmp_path, monkeypatch):
    """Closing Sensor Files must not leave workers retrying for ten minutes."""
    from crfs_iq_recorder.connection_state import ConnectionState
    from crfs_iq_recorder.sftp_window import FileDownloadWorker

    _sensor_refusing(monkeypatch, 10**6)
    worker = FileDownloadWorker(ConnectionState(), "/r/x.wav", str(tmp_path / "x.wav"), "x.wav")
    threading.Timer(0.3, worker.stop_waiting).start()
    started = time.monotonic()
    events = _run_worker(qapp, worker, timeout=5.0)
    assert time.monotonic() - started < 2.0
    assert events[-1] == ("failed", "Download cancelled.")


# ---------------------------------------------------------------- the transfer itself


class _FakeSftp:
    def __init__(self, data: bytes, final_size: int) -> None:
        self.data, self.final_size = data, final_size

    def get_channel(self):
        return types.SimpleNamespace(settimeout=lambda _t: None)

    def get(self, _remote, local, callback=None):
        Path(local).write_bytes(self.data)
        if callback:
            callback(len(self.data), len(self.data))

    def stat(self, _remote):
        return types.SimpleNamespace(st_size=self.final_size)

    def close(self):
        pass


def _browser(data: bytes, final_size: int) -> ParamikoSftpBrowser:
    sftp = _FakeSftp(data, final_size)
    client = types.SimpleNamespace(open_sftp=lambda: sftp, get_transport=lambda: None, close=lambda: None)
    return ParamikoSftpBrowser(client)


def test_a_download_shorter_than_the_file_on_the_sensor_is_not_saved(tmp_path):
    dest = tmp_path / "iq.wav"
    with pytest.raises(SftpError, match=r"^Download incomplete: 100 of 110 bytes arrived\.$"):
        _browser(b"x" * 100, 110).download("/r/iq.wav", dest)
    assert not dest.exists() and not (tmp_path / "iq.wav.part").exists()
    _browser(b"x" * 110, 110).download("/r/iq.wav", dest)
    assert dest.stat().st_size == 110


def test_already_saved_means_the_same_name_and_the_sensor_s_size(tmp_path):
    dest = tmp_path / "iq.wav"
    assert not already_saved(dest, 10)
    dest.write_bytes(b"x" * 10)
    assert already_saved(dest, 10)
    assert not already_saved(dest, 11) and not already_saved(dest, 0)
