"""Application paths and small persistence (no passwords)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from .constants import RECORDINGS_FILENAME, SETTINGS_FILENAME
from .sanitizer import redact_obj


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def recordings_path() -> Path:
    return app_dir() / RECORDINGS_FILENAME


def settings_path() -> Path:
    return app_dir() / SETTINGS_FILENAME


def load_settings() -> dict[str, Any]:
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    data.pop("password", None)
    data.pop("http_password", None)
    data.pop("sftp_password", None)
    return data


def save_settings(data: dict[str, Any]) -> None:
    payload = redact_obj(dict(data))
    if isinstance(payload, dict):
        for secret in ("password", "passwd", "pwd", "http_password", "sftp_password"):
            payload.pop(secret, None)
        settings_path().write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
