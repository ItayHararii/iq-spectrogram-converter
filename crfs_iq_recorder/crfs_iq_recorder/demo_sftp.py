"""Shared in-memory SFTP for demo mode so listings see new captures."""

from __future__ import annotations

from .sftp_client import DemoSftpBrowser

_shared: DemoSftpBrowser | None = None


def shared_demo_browser() -> DemoSftpBrowser:
    global _shared
    if _shared is None:
        _shared = DemoSftpBrowser()
    return _shared


def reset_shared_demo_browser() -> None:
    global _shared
    _shared = None
