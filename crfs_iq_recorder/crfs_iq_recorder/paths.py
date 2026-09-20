"""Application paths and small persistence. Connection secrets are stored protected."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .constants import (
    DOWNLOAD_APP_FOLDER,
    DOWNLOAD_SUBFOLDER,
    EXCEL_LOG_FILENAME,
    RECORDINGS_FILENAME,
    SETTINGS_FILENAME,
)
from .sanitizer import redact_obj


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def resource_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def user_config_dir() -> Path:
    """Writable folder for settings. Frozen builds cannot rely on the exe directory."""
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        path = base / DOWNLOAD_APP_FOLDER
        path.mkdir(parents=True, exist_ok=True)
        return path
    return app_dir()


def recordings_path() -> Path:
    return user_config_dir() / RECORDINGS_FILENAME


def settings_path() -> Path:
    return user_config_dir() / SETTINGS_FILENAME


def excel_log_path() -> Path:
    return user_config_dir() / EXCEL_LOG_FILENAME


def _local_app_folder() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / DOWNLOAD_APP_FOLDER


def _dir_is_writable(path: Path) -> bool:
    try:
        if not path.is_dir():
            return False
        probe = path / f".crfs_write_probe_{os.getpid()}"
        probe.mkdir(exist_ok=True)
        probe.rmdir()
        return True
    except OSError:
        return False


_CLOUD_MARKERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "icloud",
    "sharepoint",
)


def _local_profile_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("USERPROFILE") or Path.home())
    return Path.home()


def is_cloud_path(path: Path | str) -> bool:
    text = str(path).replace("/", "\\").casefold()
    return any(marker in text for marker in _CLOUD_MARKERS)


def preferred_data_root() -> Path | None:
    """D: when that drive is present and writable. Recordings live there instead of C:."""
    if sys.platform != "win32":
        return None
    root = Path("D:/")
    try:
        if not root.exists() or is_cloud_path(root):
            return None
        app_folder = root / DOWNLOAD_APP_FOLDER
        app_folder.mkdir(parents=True, exist_ok=True)
        if _dir_is_writable(app_folder):
            return root
    except OSError:
        return None
    return None


def user_documents_dir() -> Path:
    """Parent of CRFS IQ Recorder\\Recordings. Prefers D: on Windows.

    Cloud locations (OneDrive and similar) are never used.
    """
    preferred = preferred_data_root()
    if preferred is not None:
        return preferred
    profile = _local_profile_dir()
    if _dir_is_writable(profile) and not is_cloud_path(profile):
        return profile
    fallback = _local_app_folder()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def remap_legacy_download_dir(stored: str | None) -> str:
    """Move old C: default recordings folders onto D: when that drive is in use."""
    text = str(stored or "").strip()
    if not text:
        return text
    preferred = preferred_data_root()
    if preferred is None:
        return text
    path = Path(text)
    parts = path.parts
    if len(parts) < 2:
        return text
    drive = parts[0]
    if not (len(drive) >= 2 and drive[1] == ":" and drive[0].upper() == "C"):
        return text
    names = [part.casefold() for part in parts]
    app = DOWNLOAD_APP_FOLDER.casefold()
    if app not in names:
        return text
    idx = names.index(app)
    before = names[:idx]
    under_c_root = len(before) == 1
    under_profile = len(before) == 3 and before[1] == "users"
    if not (under_c_root or under_profile):
        return text
    return str(preferred.joinpath(*parts[idx:]))


def brand_icon_path(*, prefer_png: bool = False) -> Path | None:
    """Shared Sensorz mark used by Converter and Recorder (window, taskbar, header)."""
    names = ("sensorz_icon.png", "sensorz_icon.ico") if prefer_png else ("sensorz_icon.ico", "sensorz_icon.png")
    here = Path(__file__).resolve()
    roots = (
        resource_dir() / "assets",
        here.parents[1] / "assets",
        here.parents[2] / "assets",
    )
    for root in roots:
        for name in names:
            path = root / name
            if path.is_file():
                return path
    return None


def default_download_dir() -> Path:
    return user_documents_dir() / DOWNLOAD_APP_FOLDER / DOWNLOAD_SUBFOLDER


def resolved_download_dir(stored: str | None = None) -> Path:
    text = str(stored or "").strip()
    if text and is_cloud_path(text):
        return default_download_dir()
    text = remap_legacy_download_dir(text)
    if not text:
        return default_download_dir()
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = user_documents_dir() / path
    if is_cloud_path(path):
        return default_download_dir()
    return path


def ensure_download_dir(stored: str | None = None) -> Path:
    folder = resolved_download_dir(stored)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if not _dir_is_writable(folder):
            raise OSError(f"Download folder is not writable: {folder}")
        try:
            return folder.resolve()
        except OSError:
            return folder
    except OSError:
        requested = str(stored or "").strip()
        using_custom = bool(requested) and not is_cloud_path(requested)
        if using_custom:
            raise
        fallback = _local_app_folder() / DOWNLOAD_SUBFOLDER
        fallback.mkdir(parents=True, exist_ok=True)
        try:
            return fallback.resolve()
        except OSError:
            return fallback


def prepare_runtime() -> None:
    """Writable matplotlib/Qt cache when running as a frozen exe."""
    if not getattr(sys, "frozen", False):
        return
    os.environ.setdefault("QT_API", "pyside6")
    os.environ.setdefault("MPLBACKEND", "Agg")
    cache = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / DOWNLOAD_APP_FOLDER / "cache"
    mpl = cache / "matplotlib"
    try:
        mpl.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("MPLCONFIGDIR", str(mpl))
    except OSError:
        pass


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
    raw = dict(data)
    http_secret = raw.get("http_secret")
    sftp_secret = raw.get("sftp_secret")
    payload = redact_obj(raw)
    if isinstance(payload, dict):
        for secret in ("password", "passwd", "pwd", "http_password", "sftp_password"):
            payload.pop(secret, None)
        if isinstance(http_secret, str) and http_secret:
            payload["http_secret"] = http_secret
        if isinstance(sftp_secret, str) and sftp_secret:
            payload["sftp_secret"] = sftp_secret
        path = settings_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
