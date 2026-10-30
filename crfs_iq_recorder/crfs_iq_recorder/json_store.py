"""Crash-safe JSON state files.

``write_json`` never truncates the live file: the document goes to a sibling
temp file first and is then moved over the target (atomic on NTFS and POSIX),
so a kill or power loss mid-write leaves either the old or the new document,
never an empty one. The previous good document is kept as ``<name>.bak`` and
``read_json`` falls back to it when the live file is missing or corrupt.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

BACKUP_SUFFIX = ".bak"
_REPLACE_ATTEMPTS = 8
_REPLACE_RETRY_S = 0.05


def backup_path(path: Path) -> Path:
    return Path(path).with_name(Path(path).name + BACKUP_SUFFIX)


def _parse(path: Path) -> tuple[Any, bool]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, False
    try:
        return json.loads(text), True
    except ValueError:  # json.JSONDecodeError is a ValueError
        return None, False


def read_json(path: Path, *, backup: bool = True) -> Any:
    """Parsed document in ``path``; ``None`` when it is missing or unreadable.

    A corrupt or missing live file falls back to the ``.bak`` copy written by
    :func:`write_json` when ``backup`` is true.
    """
    path = Path(path)
    data, ok = _parse(path)
    if ok:
        return data
    if backup:
        data, ok = _parse(backup_path(path))
        if ok:
            return data
    return None


def _replace(tmp: Path, path: Path, text: str) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Another handle is open on the target (Windows sharing violation).
            if attempt + 1 < _REPLACE_ATTEMPTS:
                time.sleep(_REPLACE_RETRY_S)
        except OSError:
            break
    # Last resort: the plain write the app used before. Still clean up the temp file.
    try:
        path.write_text(text, encoding="utf-8")
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def write_json(path: Path, payload: Any, *, backup: bool = True) -> None:
    """Write ``payload`` as indented JSON so that ``path`` always holds a complete document."""
    path = Path(path)
    text = json.dumps(payload, indent=2) + "\n"
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    _replace(tmp, path, text)
    if backup:
        try:
            backup_path(path).write_text(text, encoding="utf-8")
        except OSError:
            pass
