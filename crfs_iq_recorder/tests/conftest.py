"""Pytest config: keep tests off the real network."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    def _blocked_socket(*_args, **_kwargs):
        raise RuntimeError("Tests must not open TCP sockets to real hosts.")

    def _blocked_request(*_args, **_kwargs):
        raise RuntimeError("Tests must not call requests.Session.request.")

    monkeypatch.setattr("socket.create_connection", _blocked_socket)
    monkeypatch.setattr("crfs_iq_recorder.api_client.socket.create_connection", _blocked_socket)
    monkeypatch.setattr("requests.sessions.Session.request", _blocked_request)

    def _blocked_sftp(*_args, **_kwargs):
        raise RuntimeError("Tests must not open real SFTP connections.")

    monkeypatch.setattr("crfs_iq_recorder.sftp_client.connect_sftp", _blocked_sftp)


@pytest.fixture(autouse=True, scope="session")
def stop_worker_threads_at_exit():
    """GUI tests leave sensor checks and previews running; finish them before
    the interpreter deletes their QThreads (Qt would abort with 0xC0000409)."""
    yield
    from crfs_iq_recorder.worker_threads import stop_worker_threads

    stop_worker_threads(timeout_s=15.0)


@pytest.fixture(autouse=True)
def reset_demo_sftp():
    from crfs_iq_recorder.demo_sftp import reset_shared_demo_browser

    reset_shared_demo_browser()
    yield
    reset_shared_demo_browser()


@pytest.fixture(autouse=True)
def isolated_config_dir(tmp_path, monkeypatch):
    """Keep every test out of the developer's own config folder (panel settings,
    cellular sessions, merge history)."""
    config = tmp_path / "config"
    monkeypatch.setattr("crfs_iq_recorder.paths.user_config_dir", lambda: config)
    monkeypatch.setattr("crfs_iq_recorder.gui.user_config_dir", lambda: config)
    monkeypatch.setattr("crfs_iq_recorder.merge_controller.merge_history_path", lambda: config / "merge_history.json")


@pytest.fixture(autouse=True)
def isolated_excel_log(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "crfs_iq_recorder.paths.excel_log_path",
        lambda: tmp_path / "excel_log.json",
    )
    monkeypatch.setattr(
        "crfs_iq_recorder.excel_log.excel_log_path",
        lambda: tmp_path / "excel_log.json",
    )
    monkeypatch.setattr("crfs_iq_recorder.excel_log.excel_has_workbook", lambda _path: False)
    monkeypatch.setattr("crfs_iq_recorder.excel_com.excel_has_workbook", lambda _path: False)
