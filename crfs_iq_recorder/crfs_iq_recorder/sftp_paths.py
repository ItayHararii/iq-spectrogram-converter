"""SFTP recording-folder paths on the CRFS sensor."""

from __future__ import annotations

from datetime import datetime

from .constants import SFTP_REMDATA_ROOT


def today_remdata_directory(now: datetime | None = None) -> str:
    """Return /mnt/1/remdata/YYYYMMDD/ using the computer's local date."""
    when = now if now is not None else datetime.now()
    return f"/mnt/1/remdata/{when:%Y%m%d}/"


def parent_directory(path: str) -> str:
    text = (path or "").replace("\\", "/").rstrip("/")
    if text in ("", "/"):
        return "/"
    parent = text.rsplit("/", 1)[0]
    if parent == "":
        return "/"
    return parent if parent.endswith("/") else parent + "/"


def join_remote(directory: str, name: str) -> str:
    base = (directory or "/").replace("\\", "/")
    if not base.endswith("/"):
        base += "/"
    child = name.lstrip("/")
    return base + child


def resolve_start_directory(today_dir: str, exists: bool) -> tuple[str, str | None]:
    """If today's dated folder is missing, fall back to /mnt/1/remdata/."""
    if exists:
        return today_dir, None
    fallback = SFTP_REMDATA_ROOT
    message = (
        f"Today's folder {today_dir} does not exist yet. "
        "The sensor creates it after a recording is started. "
        f"Opened {fallback} so you can pick another date, or start a recording and click Refresh."
    )
    return fallback, message


def is_remdata_root(path: str) -> bool:
    text = (path or "").replace("\\", "/").rstrip("/") + "/"
    return text == SFTP_REMDATA_ROOT
