"""SFTP listing and download for sensor recording folders."""

from __future__ import annotations

import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from .download_util import DownloadError, cleanup_part, finalize_download, part_path
from .iq_wav import stereo_iq_tone_wav_bytes
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


def sort_remote_entries(entries: list[RemoteEntry]) -> list[RemoteEntry]:
    """Newest first for files and folders. Date folders use the YYYYMMDD name."""
    from .listing_sort import sort_entries_newest_first

    return sort_entries_newest_first(entries)


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
        *,
        cancel: threading.Event | None = None,
        expected_size: int = 0,
    ) -> None:
        raise NotImplementedError

    def read_prefix(self, remote_path: str, local_path: Path, max_bytes: int) -> int:
        raise NotImplementedError


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SftpError("Download cancelled.")


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
        return sort_remote_entries(entries)

    def read_prefix(self, remote_path: str, local_path: Path, max_bytes: int) -> int:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._sftp.open(remote_path, "rb") as remote:
                data = remote.read(int(max(0, max_bytes)))
        except OSError as exc:
            raise SftpError(f"Could not read {remote_path}: {exc}") from exc
        local_path.write_bytes(data)
        return len(data)

    def download(
        self,
        remote_path: str,
        local_path: Path,
        progress: Callable[[int, int], None] | None = None,
        *,
        cancel: threading.Event | None = None,
        expected_size: int = 0,
    ) -> None:
        local_path = Path(local_path)
        tmp = part_path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        _raise_if_cancelled(cancel)

        def _cb(transferred: int, total: int) -> None:
            _raise_if_cancelled(cancel)
            if progress:
                progress(int(transferred), int(total or 0))

        try:
            self._sftp.get(remote_path, str(tmp), callback=_cb)
            _raise_if_cancelled(cancel)
            finalize_download(tmp, local_path, expected_size=expected_size)
        except SftpError:
            cleanup_part(tmp)
            raise
        except DownloadError as exc:
            cleanup_part(tmp)
            raise SftpError(str(exc)) from exc
        except OSError as exc:
            cleanup_part(tmp)
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
                "Open Settings and check the SFTP username and password."
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


def _demo_dir_key(path: str) -> str:
    text = (path or "/").replace("\\", "/")
    if text != "/" and not text.endswith("/"):
        text += "/"
    return text


class DemoSftpBrowser(SftpBrowser):
    """In-memory folders. Tests and demo mode must not open sockets."""

    def __init__(self, *, include_today: bool = True) -> None:
        today = today_remdata_directory()
        now = datetime.now()
        wav = stereo_iq_tone_wav_bytes()
        self._files: dict[str, bytes] = {}
        self._dirs: dict[str, list[RemoteEntry]] = {
            "/": [RemoteEntry("mnt", "/mnt", True, 0, now)],
            "/mnt/": [RemoteEntry("1", "/mnt/1", True, 0, now)],
            "/mnt/1/": [RemoteEntry("remdata", "/mnt/1/remdata", True, 0, now)],
            "/mnt/1/remdata/": [],
        }
        date_name = today.strip("/").rsplit("/", 1)[-1]
        if include_today:
            older_dates = ("20260115", "20260801")
            for older in older_dates:
                older_path = join_remote("/mnt/1/remdata", older)
                older_when = datetime.strptime(older, "%Y%m%d")
                self._dirs["/mnt/1/remdata/"].append(
                    RemoteEntry(older, older_path, True, 0, older_when)
                )
                self._dirs[_demo_dir_key(older_path)] = []
            self._dirs["/mnt/1/remdata/"].append(
                RemoteEntry(date_name, today.rstrip("/"), True, 0, now)
            )
            demo = join_remote(today, "iq-demo.wav")
            demo2 = join_remote(today, "iq-demo-2.wav")
            part1 = join_remote(today, "iq_20260910_161629_0001.wav")
            part2 = join_remote(today, "iq_20260910_161629_0002.wav")
            self._files = {demo: wav, demo2: wav, part1: wav, part2: wav}
            self._dirs[today] = sort_remote_entries(
                [
                    RemoteEntry("iq-demo.wav", demo, False, len(wav), now - timedelta(minutes=8)),
                    RemoteEntry("iq-demo-2.wav", demo2, False, len(wav), now - timedelta(minutes=5)),
                    RemoteEntry(
                        "iq_20260910_161629_0001.wav",
                        part1,
                        False,
                        len(wav),
                        now - timedelta(minutes=1),
                    ),
                    RemoteEntry(
                        "iq_20260910_161629_0002.wav",
                        part2,
                        False,
                        len(wav),
                        now,
                    ),
                ]
            )

    def directory_exists(self, path: str) -> bool:
        key = _demo_dir_key(path)
        return key in self._dirs

    def listdir(self, path: str) -> list[RemoteEntry]:
        key = _demo_dir_key(path)
        if key not in self._dirs:
            raise SftpError(f"Folder not found: {path}")
        return sort_remote_entries(list(self._dirs[key]))

    def read_prefix(self, remote_path: str, local_path: Path, max_bytes: int) -> int:
        payload = self._files.get(remote_path, stereo_iq_tone_wav_bytes())
        data = payload[: int(max(0, max_bytes))]
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(data)
        return len(data)

    def download(
        self,
        remote_path: str,
        local_path: Path,
        progress: Callable[[int, int], None] | None = None,
        *,
        cancel: threading.Event | None = None,
        expected_size: int = 0,
    ) -> None:
        _raise_if_cancelled(cancel)
        payload = self._files.get(remote_path, stereo_iq_tone_wav_bytes())
        total = len(payload)
        if progress:
            progress(0, total)
        tmp = part_path(Path(local_path))
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(payload)
        if progress:
            progress(total, total)
        _raise_if_cancelled(cancel)
        try:
            finalize_download(tmp, Path(local_path), expected_size=expected_size or total)
        except DownloadError as exc:
            cleanup_part(tmp)
            raise SftpError(str(exc)) from exc
