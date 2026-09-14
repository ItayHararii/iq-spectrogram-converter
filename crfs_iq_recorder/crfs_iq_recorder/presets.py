"""Save/load local recording presets. Passwords are never stored."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .sanitizer import redact_obj

PRESET_VERSION = 1
FORBIDDEN_KEYS = {"password", "passwd", "pwd", "authorization", "auth"}


class PresetError(ValueError):
    """Preset file could not be read or written."""


def _strip_secrets(data: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if str(key).lower() in FORBIDDEN_KEYS:
            continue
        cleaned[str(key)] = redact_obj(value) if isinstance(value, (dict, list)) else value
    return cleaned


def save_preset(path: Path, data: dict[str, Any]) -> None:
    payload = {"preset_version": PRESET_VERSION, **_strip_secrets(data)}
    path = Path(path)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_preset(path: Path) -> dict[str, Any]:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PresetError(f"Could not read preset: {exc}") from exc
    if not isinstance(raw, dict):
        raise PresetError("Preset file must contain a JSON object.")
    return _strip_secrets(raw)
