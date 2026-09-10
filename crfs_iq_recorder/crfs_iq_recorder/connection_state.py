"""In-memory connection settings. Passwords are never written to disk."""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    DEFAULT_PASSWORD,
    DEFAULT_SFTP_PASSWORD,
    DEFAULT_SFTP_PORT,
    DEFAULT_SFTP_USERNAME,
    DEFAULT_USERNAME,
)


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

    def sftp_user(self) -> str:
        if self.sftp_same_as_http:
            return (self.username or DEFAULT_SFTP_USERNAME).strip()
        return (self.sftp_username or DEFAULT_SFTP_USERNAME).strip()

    def sftp_pass(self) -> str:
        if self.sftp_same_as_http:
            return self.http_password
        return self.sftp_password

    def persistable(self) -> dict:
        return {
            "host": self.host,
            "http_port": self.http_port,
            "use_https": self.use_https,
            "username": self.username,
            "timeout": self.timeout,
            "sftp_port": self.sftp_port,
            "sftp_username": self.sftp_username,
            "sftp_same_as_http": self.sftp_same_as_http,
            "demo": self.demo,
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
        saved_same = data.get("sftp_same_as_http")
        used_http_login_for_sftp = saved_same is True or saved_user in ("", DEFAULT_USERNAME)
        if used_http_login_for_sftp:
            # Older builds reused HTTP admin/pass. CRFS SSH is a different account.
            state.sftp_same_as_http = False
            state.sftp_username = DEFAULT_SFTP_USERNAME
        else:
            state.sftp_same_as_http = bool(saved_same)
            state.sftp_username = saved_user
        state.sftp_password = DEFAULT_SFTP_PASSWORD
        if demo or bool(data.get("demo")):
            state.demo = True
        if not state.host and state.demo:
            state.host = "192.0.2.10"
        return state
