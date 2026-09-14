"""In-memory connection and last-used recording settings. Passwords are never written to disk."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    DEFAULT_BANDWIDTH_HZ,
    DEFAULT_CENTER_HZ,
    DEFAULT_END_HZ,
    DEFAULT_FREQ_UNIT,
    DEFAULT_PASSWORD,
    DEFAULT_RECORDED_TIME_S,
    DEFAULT_RECORDING_FORMAT,
    DEFAULT_SFTP_PASSWORD,
    DEFAULT_SFTP_PORT,
    DEFAULT_SFTP_USERNAME,
    DEFAULT_START_HZ,
    DEFAULT_USERNAME,
    FORMAT_BY_ID,
    FREQ_UNITS,
)
from .frequency import FrequencyError, FrequencyPlan, from_center_bandwidth, from_start_end
from .paths import default_download_dir, is_cloud_path


def _persisted_download_dir(stored: str) -> str:
    folder = (stored or "").strip() or str(default_download_dir())
    if is_cloud_path(folder):
        return str(default_download_dir())
    return folder


def _as_int(data: dict, key: str, default: int) -> int:
    try:
        return int(data.get(key, default))
    except (TypeError, ValueError):
        return default


def _as_float(data: dict, key: str, default: float) -> float:
    try:
        value = float(data.get(key, default))
    except (TypeError, ValueError):
        return default
    if value != value or value in (float("inf"), float("-inf")) or value <= 0:
        return default
    return value


@dataclass
class ConnectionState:
    host: str = ""
    http_port: str = ""
    use_https: bool = False
    username: str = DEFAULT_USERNAME
    http_password: str = DEFAULT_PASSWORD
    timeout: str = "30"
    sftp_port: str = str(DEFAULT_SFTP_PORT)
    sftp_username: str = DEFAULT_SFTP_USERNAME
    sftp_password: str = DEFAULT_SFTP_PASSWORD
    sftp_same_as_http: bool = False
    demo: bool = False
    download_dir: str = ""
    freq_unit: str = DEFAULT_FREQ_UNIT
    freq_mode: str = "center"
    center_hz: int = DEFAULT_CENTER_HZ
    bandwidth_hz: int = DEFAULT_BANDWIDTH_HZ
    start_hz: int = DEFAULT_START_HZ
    end_hz: int = DEFAULT_END_HZ
    recorded_time_s: float = DEFAULT_RECORDED_TIME_S
    recording_format: str = DEFAULT_RECORDING_FORMAT
    ui_theme: str = "light"

    def sftp_user(self) -> str:
        return (self.sftp_username or DEFAULT_SFTP_USERNAME).strip()

    def sftp_pass(self) -> str:
        return self.sftp_password

    def frequency_plan(self) -> FrequencyPlan:
        try:
            if self.freq_mode == "start_end":
                return from_start_end(int(self.start_hz), int(self.end_hz))
            return from_center_bandwidth(int(self.center_hz), int(self.bandwidth_hz))
        except (FrequencyError, TypeError, ValueError):
            return from_center_bandwidth(DEFAULT_CENTER_HZ, DEFAULT_BANDWIDTH_HZ)

    def persistable(self) -> dict:
        return {
            "host": self.host,
            "http_port": self.http_port,
            "use_https": self.use_https,
            "username": self.username,
            "timeout": self.timeout,
            "sftp_port": self.sftp_port,
            "sftp_username": self.sftp_username,
            "sftp_same_as_http": False,
            "download_dir": _persisted_download_dir(self.download_dir),
            "freq_unit": self.freq_unit if self.freq_unit in FREQ_UNITS else DEFAULT_FREQ_UNIT,
            "freq_mode": self.freq_mode if self.freq_mode in {"center", "start_end"} else "center",
            "center_hz": int(self.center_hz),
            "bandwidth_hz": int(self.bandwidth_hz),
            "start_hz": int(self.start_hz),
            "end_hz": int(self.end_hz),
            "recorded_time_s": float(self.recorded_time_s),
            "recording_format": self.recording_format,
            "ui_theme": "dark" if self.ui_theme == "dark" else "light",
        }

    @classmethod
    def from_settings(cls, data: dict, *, demo: bool = False) -> ConnectionState:
        state = cls(demo=demo)
        state.host = str(data.get("host") or "")
        port = data.get("http_port", data.get("port", ""))
        state.http_port = "" if port in (None, "") else str(port)
        state.use_https = bool(data.get("use_https", False))
        state.username = str(data.get("username") or DEFAULT_USERNAME)
        state.timeout = str(data.get("timeout") or "30")
        state.sftp_port = str(data.get("sftp_port") or DEFAULT_SFTP_PORT)
        saved_user = str(data.get("sftp_username") or "")
        used_http_login_for_sftp = bool(data.get("sftp_same_as_http")) or saved_user in ("", DEFAULT_USERNAME)
        state.sftp_same_as_http = False
        if used_http_login_for_sftp:
            # Older builds reused the HTTP username for SFTP. SSH is a different account.
            state.sftp_username = DEFAULT_SFTP_USERNAME
        else:
            state.sftp_username = saved_user
        state.sftp_password = DEFAULT_SFTP_PASSWORD
        if demo and not state.host:
            state.host = "192.0.2.10"
        stored = str(data.get("download_dir") or "").strip()
        if stored and is_cloud_path(stored):
            stored = ""
        state.download_dir = stored
        unit = str(data.get("freq_unit") or DEFAULT_FREQ_UNIT)
        state.freq_unit = unit if unit in FREQ_UNITS else DEFAULT_FREQ_UNIT
        mode = str(data.get("freq_mode") or "center")
        state.freq_mode = mode if mode in {"center", "start_end"} else "center"
        state.center_hz = _as_int(data, "center_hz", DEFAULT_CENTER_HZ)
        state.bandwidth_hz = _as_int(data, "bandwidth_hz", DEFAULT_BANDWIDTH_HZ)
        state.start_hz = _as_int(data, "start_hz", DEFAULT_START_HZ)
        state.end_hz = _as_int(data, "end_hz", DEFAULT_END_HZ)
        state.recorded_time_s = _as_float(data, "recorded_time_s", DEFAULT_RECORDED_TIME_S)
        fmt = str(data.get("recording_format") or DEFAULT_RECORDING_FORMAT).strip()
        info = FORMAT_BY_ID.get(fmt)
        state.recording_format = fmt if info is not None and info.emp_verified else DEFAULT_RECORDING_FORMAT
        theme = str(data.get("ui_theme") or "light").strip().casefold()
        state.ui_theme = "dark" if theme == "dark" else "light"
        return state
