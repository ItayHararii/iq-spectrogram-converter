"""SFTP listing and download for sensor recording folders."""

from __future__ import annotations

import stat
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .sftp_paths import join_remote, today_remdata_directory


@dataclass(frozen=True)
class RemoteEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    modified: datetime | None


class SftpError(Exception):
    """SFTP operation failed."""


def _mtime(value: float | int | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value))
    except (OSError, OverflowError, ValueError):
        return None


class SftpBrowser:
    def close(self) -> None:
        return None

    def directory_exists(self, path: str) -> bool:
        raise NotImplementedError

    def listdir(self, path: str) -> list[RemoteEntry]:
        raise NotImplementedError

    def download(
        self,
        remote_path: str,
        local_path: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        raise NotImplementedError


class ParamikoSftpBrowser(SftpBrowser):
    def __init__(self, client) -> None:
        self._client = client
        self._sftp = client.open_sftp()

    def close(self) -> None:
        try:
            self._sftp.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass

    def directory_exists(self, path: str) -> bool:
        try:
            mode = self._sftp.stat(path).st_mode
        except Exception:
            return False
        return bool(mode and stat.S_ISDIR(mode))

    def listdir(self, path: str) -> list[RemoteEntry]:
        try:
            attrs = self._sftp.listdir_attr(path)
        except FileNotFoundError as exc:
            raise SftpError(f"Folder not found: {path}") from exc
        except OSError as exc:
            raise SftpError(str(exc)) from exc
        except Exception as exc:
            raise SftpError(f"Could not list {path}: {exc}") from exc
        entries: list[RemoteEntry] = []
        for item in attrs:
            name = item.filename
            mode = item.st_mode or 0
            is_dir = bool(stat.S_ISDIR(mode))
            entries.append(
                RemoteEntry(
                    name=name,
                    path=join_remote(path, name),
                    is_dir=is_dir,
                    size=0 if is_dir else int(item.st_size or 0),
                    modified=_mtime(item.st_mtime),
                )
            )
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def download(
        self,
        remote_path: str,
        local_path: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)

        def _cb(transferred: int, total: int) -> None:
            if progress:
                progress(int(transferred), int(total or 0))

        try:
            self._sftp.get(remote_path, str(local_path), callback=_cb)
        except OSError as exc:
            raise SftpError(f"Download failed: {exc}") from exc


def connect_sftp(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    timeout_s: float = 20.0,
) -> ParamikoSftpBrowser:
    try:
        import paramiko
    except ImportError as exc:
        raise SftpError("paramiko is required for SFTP. Install requirements.txt.") from exc

    def _connect(client, extra: dict | None = None) -> None:
        kwargs = {
            "hostname": host,
            "port": int(port),
            "username": username,
            "password": password,
            "timeout": timeout_s,
            "banner_timeout": timeout_s,
            "auth_timeout": timeout_s,
            "allow_agent": False,
            "look_for_keys": False,
        }
        if extra:
            kwargs.update(extra)
        client.connect(**kwargs)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        try:
            _connect(client)
        except paramiko.AuthenticationException as exc:
            try:
                client.close()
            except Exception:
                pass
            raise SftpError(
                "SFTP login failed. CRFS SSH uses a different account from HTTP. "
                "Open Connection Settings and check the SFTP username and password."
            ) from exc
        except paramiko.SSHException as exc:
            message = str(exc).lower()
            retryable = any(token in message for token in ("algorithm", "kex", "no matching", "banner"))
            if not retryable:
                raise
            try:
                client.close()
            except Exception:
                pass
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            _connect(client, {"disabled_algorithms": {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]}})
    except SftpError:
        raise
    except Exception as exc:
        try:
            client.close()
        except Exception:
            pass
        raise SftpError(f"SFTP connection failed: {exc}") from exc
    try:
        return ParamikoSftpBrowser(client)
    except Exception as exc:
        try:
            client.close()
        except Exception:
            pass
        raise SftpError(f"SFTP session failed: {exc}") from exc


class DemoSftpBrowser(SftpBrowser):
    """In-memory folders. Tests and demo mode must not open sockets."""

    def __init__(self, *, include_today: bool = True) -> None:
        today = today_remdata_directory()
        self._dirs: dict[str, list[RemoteEntry]] = {
            "/mnt/1/remdata/": [],
            "/mnt/1/": [
                RemoteEntry("remdata", "/mnt/1/remdata", True, 0, datetime.now()),
            ],
        }
        date_name = today.strip("/").rsplit("/", 1)[-1]
        if include_today:
            self._dirs["/mnt/1/remdata/"].append(
                RemoteEntry(date_name, today.rstrip("/"), True, 0, datetime.now())
            )
            self._dirs[today] = [
                RemoteEntry(
                    "iq-demo.wav",
                    join_remote(today, "iq-demo.wav"),
                    False,
                    7_631_375,
                    datetime.now(),
                ),
                RemoteEntry(
                    "iq-demo-2.wav",
                    join_remote(today, "iq-demo-2.wav"),
                    False,
                    7_631_375,
                    datetime.now(),
                ),
            ]

    def directory_exists(self, path: str) -> bool:
        key = path if path.endswith("/") else path + "/"
        return key in self._dirs or path in self._dirs

    def listdir(self, path: str) -> list[RemoteEntry]:
        key = path if path.endswith("/") else path + "/"
        if key not in self._dirs:
            raise SftpError(f"Folder not found: {path}")
        return list(self._dirs[key])

    def download(
        self,
        remote_path: str,
        local_path: Path,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        payload = b"CRFS IQ Recorder demo file. Not a real recording.\n"
        total = len(payload)
        if progress:
            progress(0, total)
        Path(local_path).write_bytes(payload)
        if progress:
            progress(total, total)
