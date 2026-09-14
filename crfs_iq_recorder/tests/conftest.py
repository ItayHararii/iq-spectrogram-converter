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
