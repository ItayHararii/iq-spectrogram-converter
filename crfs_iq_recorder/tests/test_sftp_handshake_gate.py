"""SSH handshakes are gated process-wide (the Node 40-8 drops handshakes that pile up)."""

from __future__ import annotations

import sys
import threading
import time
import types

import pytest

from crfs_iq_recorder import sftp_client
from crfs_iq_recorder.sftp_client import SSH_HANDSHAKE_SLOTS, SftpError, connect_sftp


class _FakeTransport:
    sock = None


class _Meter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = 0


@pytest.fixture
def fake_paramiko(monkeypatch):
    meter = _Meter()

    class SSHClient:
        def set_missing_host_key_policy(self, _policy) -> None:
            pass

        def connect(self, **_kwargs) -> None:
            with meter.lock:
                meter.active += 1
                meter.calls += 1
                meter.peak = max(meter.peak, meter.active)
            try:
                time.sleep(0.15)  # a Node key exchange takes ~2.4 s; scaled down
            finally:
                with meter.lock:
                    meter.active -= 1

        def get_transport(self):
            return _FakeTransport()

        def open_sftp(self):
            return types.SimpleNamespace(close=lambda: None)

        def close(self) -> None:
            pass

    module = types.SimpleNamespace(
        SSHClient=SSHClient,
        AutoAddPolicy=lambda: None,
        AuthenticationException=type("AuthenticationException", (Exception,), {}),
        SSHException=type("SSHException", (Exception,), {}),
    )
    monkeypatch.setitem(sys.modules, "paramiko", module)
    monkeypatch.setattr(sftp_client, "ParamikoSftpBrowser", lambda client: types.SimpleNamespace(client=client))
    return meter


def test_at_most_two_handshakes_run_at_once(fake_paramiko):
    results: list[object] = []

    def work() -> None:
        try:
            results.append(connect_sftp("sensor", 22, "u", "p", timeout_s=2.0))
        except Exception as exc:  # pragma: no cover - reported through the assertion
            results.append(exc)

    threads = [threading.Thread(target=work) for _ in range(6)]
    started = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    elapsed = time.perf_counter() - started
    assert fake_paramiko.calls == 6
    assert fake_paramiko.peak == SSH_HANDSHAKE_SLOTS == 2
    assert all(not isinstance(item, Exception) for item in results), results
    # Six handshakes of 0.15 s through two slots take about 0.45 s, not 0.9 s and not 0.15 s.
    assert 0.4 < elapsed < 1.5
    # The gate is released afterwards: a new handshake is not blocked.
    assert sftp_client._handshake_gate.acquire(timeout=0.1)
    sftp_client._handshake_gate.release()


def test_cancel_while_waiting_for_a_slot_raises_quickly(fake_paramiko):
    # Occupy both slots.
    assert sftp_client._handshake_gate.acquire(timeout=1)
    assert sftp_client._handshake_gate.acquire(timeout=1)
    cancel = threading.Event()
    cancel.set()
    try:
        started = time.perf_counter()
        with pytest.raises(SftpError, match="cancelled"):
            connect_sftp("sensor", 22, "u", "p", timeout_s=2.0, cancel=cancel)
        assert time.perf_counter() - started < 1.0
        assert fake_paramiko.calls == 0
    finally:
        sftp_client._handshake_gate.release()
        sftp_client._handshake_gate.release()
