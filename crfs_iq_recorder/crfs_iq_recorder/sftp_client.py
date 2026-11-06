"""SFTP listing and download for sensor recording folders."""

from __future__ import annotations

import logging
import socket
import stat
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .download_util import DownloadError, cleanup_part, finalize_download, part_path
from .iq_wav import stereo_iq_tone_wav_bytes
from .sensor_storage import DEMO_FREE_BYTES, DEMO_TOTAL_BYTES, StorageSnapshot
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


class SftpNotFoundError(SftpError):
    """The remote path does not exist (as opposed to the session failing)."""


class SftpConnectError(SftpError):
    """No SSH session could be opened, so nothing was transferred.

    `retry_after_s` is set while new connections are held back after a timeout.
    """

    def __init__(self, message: str, *, retry_after_s: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after_s = max(float(retry_after_s or 0.0), 0.0)


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

    def abort(self) -> None:
        self.close()

    def is_alive(self) -> bool:
        """False once the underlying connection is known to be gone."""
        return True

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

    def open_read(self, remote_path: str):
        """(binary file object supporting seek/read, size in bytes) for a remote file."""
        raise NotImplementedError

    def inspect_wave(self, remote_path: str):
        """The WAVE header and CRFS metadata of a file on the sensor (reads only its header and trailer)."""
        from .iq_merge import MergeError, inspect_stream

        try:
            handle, size = self.open_read(remote_path)
        except SftpError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported as an SFTP failure
            raise SftpError(f"Could not open {remote_path}: {exc}") from exc
        try:
            return inspect_stream(handle, size=size, path=remote_path)
        except MergeError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise SftpError(f"Could not read {remote_path}: {exc}") from exc
        finally:
            try:
                handle.close()
            except Exception:  # noqa: BLE001
                pass

    def remove_file(self, remote_path: str) -> None:
        raise NotImplementedError

    def storage_usage(self, path: str):
        """Free/total bytes for the filesystem that contains `path`."""
        from .sensor_storage import StorageSnapshot

        return StorageSnapshot.unavailable("Storage query is not implemented.")

    def remote_sha256(self, remote_path: str, *, timeout_s: float = 0.0) -> tuple[str, str]:
        """(hex digest, note) computed on the sensor; ('' , why) when the sensor offers none."""
        return "", "no checksum available from this connection"

    def pending_writeback_bytes(self) -> int | None:
        """Bytes the sensor's Linux still has to write to its disks (Dirty + Writeback), None when unknown."""
        return None


def parse_writeback_bytes(meminfo_text: str) -> int | None:
    """Dirty + Writeback from /proc/meminfo text (kB lines), None when neither line is there."""
    total = 0
    found = False
    for line in (meminfo_text or "").splitlines():
        key, _, rest = line.partition(":")
        if key.strip() not in {"Dirty", "Writeback"}:
            continue
        fields = rest.split()
        if not fields or not fields[0].isdigit():
            continue
        found = True
        unit = fields[1].casefold() if len(fields) > 1 else "kb"
        total += int(fields[0]) * (1024 if unit == "kb" else 1)
    return total if found else None


REMOTE_CHECKSUM_TIMEOUT_S = 180.0  # a 500 MB part takes the Node a few seconds; a slow disk much longer


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise SftpError("Download cancelled.")


def exception_text(exc: BaseException) -> str:
    """The message, or the exception's type when it has none ("OverflowError", not "")."""
    text = str(exc).strip()
    return text or type(exc).__name__


SFTP_CONNECT_TIMEOUT_S = 8.0
SFTP_READ_TIMEOUT_S = 20.0
# The Node's SSH server finishes one key exchange in ~2.4 s and starves when
# several run at once: on an RFeye Node 40-8, 4 simultaneous handshakes with
# other sessions open left 2 of them timing out at 8 s (measured). Handshakes
# are gated process-wide; open sessions are not limited.
SSH_HANDSHAKE_SLOTS = 2
_handshake_gate = threading.BoundedSemaphore(SSH_HANDSHAKE_SLOTS)
# Measured on an RFeye Node 100-18 over OpenVPN: after about six new SSH
# connections within ten seconds, further ones were dropped (the TCP connect
# timed out while HTTP on the same sensor answered at once) until nothing had
# been tried for ~15 s. A timed-out connect sends several SYNs, so retrying at
# once kept the block in place: in one collection 11 of 15 downloads gave up.
# New connections are paced process-wide; after a connect timeout none is
# tried for a cool-down, then one at a time until one gets through.
SSH_CONNECT_WINDOW_S = 10.0
SSH_CONNECTS_PER_WINDOW = 3
SSH_COOLDOWN_S = 20.0
SSH_COOLDOWN_MAX_S = 60.0


class ConnectPacer:
    """When a new SSH connection may be opened (shared by every SFTP user in the process)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._cond = threading.Condition()
        self._clock = clock
        self._starts: deque[float] = deque()
        self._blocked_until = 0.0
        self._timeouts = 0
        self._probing = False

    def cooling_down_s(self) -> float:
        """Seconds until new connections are tried again (0 when they may be opened)."""
        with self._cond:
            return max(self._blocked_until - self._clock(), 0.0)

    def acquire(self, cancel: threading.Event | None = None) -> None:
        """Wait for a turn; raises SftpConnectError while connections are held back."""
        with self._cond:
            while True:
                if cancel is not None and cancel.is_set():
                    raise SftpError("SFTP cancelled.")
                now = self._clock()
                if now < self._blocked_until:
                    wait = self._blocked_until - now
                    raise SftpConnectError(
                        f"The sensor is not accepting new SFTP connections; trying again in {int(wait + 0.999)} s.",
                        retry_after_s=wait,
                    )
                if self._timeouts and self._probing:
                    # Another connection is finding out whether the sensor accepts them again.
                    self._cond.wait(0.2)
                    continue
                while self._starts and now - self._starts[0] >= SSH_CONNECT_WINDOW_S:
                    self._starts.popleft()
                if len(self._starts) >= SSH_CONNECTS_PER_WINDOW:
                    self._cond.wait(min(0.2, max(self._starts[0] + SSH_CONNECT_WINDOW_S - now, 0.01)))
                    continue
                self._starts.append(now)
                if self._timeouts:
                    self._probing = True
                return

    def reached(self) -> None:
        """The sensor answered (logged in, or refused the login): connections are accepted."""
        with self._cond:
            self._timeouts = 0
            self._probing = False
            self._blocked_until = 0.0
            self._cond.notify_all()

    def timed_out(self) -> float:
        """A connect timed out: hold new connections back. Returns the cool-down in seconds."""
        with self._cond:
            self._timeouts += 1
            self._probing = False
            cool = min(SSH_COOLDOWN_S * (2 ** (self._timeouts - 1)), SSH_COOLDOWN_MAX_S)
            self._blocked_until = self._clock() + cool
            self._cond.notify_all()
            return cool

    def failed(self) -> None:
        """Another connection failure (refused, unreachable): nothing learnt about the block."""
        with self._cond:
            self._probing = False
            self._cond.notify_all()


connect_pacer = ConnectPacer()


def _is_timeout(exc: BaseException) -> bool:
    """A TCP connect or SSH banner that timed out (paramiko wraps the latter in SSHException)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (TimeoutError, socket.timeout)) or "timed out" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def _failure_text(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    return "timed out" if _is_timeout(exc) else type(exc).__name__


# paramiko's transport thread logs a full traceback when a socket is closed
# under it (our cancel path). Without a handler Python prints that to the
# console; the app reports the outcome itself. A configured logger still gets it.
logging.getLogger("paramiko").addHandler(logging.NullHandler())


def _close_ssh_client(client) -> None:
    if client is None:
        return
    try:
        transport = client.get_transport()
    except Exception:
        transport = None
    if transport is not None:
        try:
            sock = getattr(transport, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(2)
                except Exception:
                    pass
            transport.close()
        except Exception:
            pass
    try:
        client.close()
    except Exception:
        pass


class ParamikoSftpBrowser(SftpBrowser):
    def __init__(self, client) -> None:
        self._client = client
        self._sftp = client.open_sftp()
        try:
            channel = self._sftp.get_channel()
            channel.settimeout(SFTP_READ_TIMEOUT_S)
        except Exception:
            pass

    def abort(self) -> None:
        _close_ssh_client(self._client)

    def close(self) -> None:
        try:
            self._sftp.close()
        except Exception:
            pass
        _close_ssh_client(self._client)

    def is_alive(self) -> bool:
        try:
            transport = self._client.get_transport()
        except Exception:
            return False
        if transport is None:
            return False
        try:
            return bool(transport.is_active())
        except Exception:
            return False

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
            raise SftpNotFoundError(f"Folder not found: {path}") from exc
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

    def open_read(self, remote_path: str):
        try:
            size = int(self._sftp.stat(remote_path).st_size or 0)
            return self._sftp.open(remote_path, "rb"), size
        except FileNotFoundError as exc:
            raise SftpNotFoundError(f"File not found: {remote_path}") from exc
        except OSError as exc:
            raise SftpError(f"Could not open {remote_path}: {exc}") from exc

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

        progress_failed: list[str] = []

        def _cb(transferred: int, total: int) -> None:
            _raise_if_cancelled(cancel)
            if progress and not progress_failed:
                try:
                    progress(int(transferred), int(total or 0))
                except Exception as exc:  # noqa: BLE001 - a progress display never ends a transfer
                    progress_failed.append(exception_text(exc))
                    logging.getLogger(__name__).warning("download progress disabled: %s", progress_failed[0])

        try:
            self._sftp.get(remote_path, str(tmp), callback=_cb)
            _raise_if_cancelled(cancel)
            # The sensor's size now, not the one listed earlier: a file it was
            # still writing must not count as downloaded.
            final = int(self._sftp.stat(remote_path).st_size or 0)
            got = tmp.stat().st_size
            if final and got != final:
                raise SftpError(f"Download incomplete: {got:,} of {final:,} bytes arrived.")
            finalize_download(tmp, local_path, expected_size=max(int(expected_size or 0), final))
        except SftpError:
            cleanup_part(tmp)
            raise
        except DownloadError as exc:
            cleanup_part(tmp)
            raise SftpError(str(exc)) from exc
        except OSError as exc:
            cleanup_part(tmp)
            if cancel is not None and cancel.is_set():
                raise SftpError("Download cancelled.") from exc
            raise SftpError(f"Download failed: {exception_text(exc)}") from exc
        except Exception as exc:  # noqa: BLE001 - paramiko raises its own classes
            # Cancel closes the socket under paramiko, which then raises
            # `paramiko.sftp.SFTPError: Garbage packet received` (not an OSError):
            # on the Node 100-18 that left "<name>.wav.part" behind and counted
            # the cancel as a failure with a pop-up.
            cleanup_part(tmp)
            if cancel is not None and cancel.is_set():
                raise SftpError("Download cancelled.") from exc
            raise SftpError(f"Download failed: {exception_text(exc)}") from exc

    def remove_file(self, remote_path: str) -> None:
        try:
            self._sftp.remove(remote_path)
        except FileNotFoundError as exc:
            raise SftpError(f"File not found: {remote_path}") from exc
        except OSError as exc:
            raise SftpError(f"Could not delete {remote_path}: {exc}") from exc
        except Exception as exc:
            raise SftpError(f"Could not delete {remote_path}: {exc}") from exc

    def remote_sha256(self, remote_path: str, *, timeout_s: float = REMOTE_CHECKSUM_TIMEOUT_S) -> tuple[str, str]:
        """(hex digest, note) of the sensor's file, computed on the sensor over SSH.

        The Node exposes no checksum in its HTTP API; its Linux has GNU
        ``sha256sum`` (seen on the R40-8, coreutils 8.30), so the digest is
        read over the same SSH session the files come from. Read-only. The
        digest is '' with a note when the command is unavailable or slow.
        """
        import shlex

        quoted = shlex.quote(remote_path)
        try:
            _stdin, stdout, stderr = self._client.exec_command(f"sha256sum {quoted}", timeout=float(timeout_s))
            text = stdout.read().decode("utf-8", errors="replace").strip()
            err = stderr.read().decode("utf-8", errors="replace").strip()
        except Exception as exc:  # noqa: BLE001 - the checksum is one of three verifications, not the download
            return "", f"sha256sum over SSH failed: {exc}"
        digest = text.split()[0].casefold() if text else ""
        if len(digest) == 64 and all(c in "0123456789abcdef" for c in digest):
            return digest, "sha256sum computed on the sensor over SSH"
        return "", f"sha256sum over SSH gave no digest ({err or text or 'no output'})"

    def pending_writeback_bytes(self) -> int | None:
        """Read-only: `/proc/meminfo` over this SSH session (no extra handshake)."""
        try:
            _stdin, stdout, _stderr = self._client.exec_command("grep -E '^(Dirty|Writeback):' /proc/meminfo", timeout=10)
            return parse_writeback_bytes(stdout.read().decode("utf-8", errors="replace"))
        except Exception:  # noqa: BLE001 - unknown, never a failed recording
            return None

    def storage_usage(self, path: str):
        from .sensor_storage import snapshot_from_df_text, snapshot_from_statvfs

        try:
            attr = self._sftp.statvfs(path)
            snap = snapshot_from_statvfs(path, attr)
            if snap is not None:
                return snap
        except Exception:
            pass
        try:
            import shlex

            quoted = shlex.quote(path or "/")
            _stdin, stdout, _stderr = self._client.exec_command(f"df -kP {quoted}", timeout=12)
            text = stdout.read().decode("utf-8", errors="replace")
            snap = snapshot_from_df_text(text, path)
            if snap is not None:
                return snap
        except Exception as exc:
            raise SftpError(f"Could not read storage for {path}: {exc}") from exc
        raise SftpError(f"Could not read storage for {path}.")


def connect_sftp(
    host: str,
    port: int,
    username: str,
    password: str,
    *,
    timeout_s: float = SFTP_CONNECT_TIMEOUT_S,
    cancel: threading.Event | None = None,
    on_client: Callable | None = None,
) -> ParamikoSftpBrowser:
    try:
        import paramiko
    except ImportError as exc:
        raise SftpError("paramiko is required for SFTP. Install requirements.txt.") from exc

    timeout_s = float(timeout_s or SFTP_CONNECT_TIMEOUT_S)
    holder: dict[str, object] = {"client": None}

    def _cancelled() -> bool:
        return cancel is not None and cancel.is_set()

    def _raise_if_cancelled() -> None:
        if _cancelled():
            _close_ssh_client(holder.get("client"))
            raise SftpError("SFTP cancelled.")

    done = threading.Event()

    def _watch_cancel() -> None:
        if cancel is None:
            return
        while not done.is_set():
            if cancel.wait(0.2):
                _close_ssh_client(holder.get("client"))
                return

    def _new_client():
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        holder["client"] = client
        if on_client is not None:
            on_client(client)
        return client

    def _connect(client, extra: dict | None = None) -> None:
        _raise_if_cancelled()
        connect_pacer.acquire(cancel)
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
        try:
            client.connect(**kwargs)
        except paramiko.AuthenticationException:
            connect_pacer.reached()
            raise
        except Exception as exc:
            if _is_timeout(exc):
                connect_pacer.timed_out()
            else:
                connect_pacer.failed()
            raise
        connect_pacer.reached()
        _raise_if_cancelled()
        try:
            transport = client.get_transport()
            if transport is not None and getattr(transport, "sock", None) is not None:
                transport.sock.settimeout(SFTP_READ_TIMEOUT_S)
        except Exception:
            pass

    if cancel is not None:
        threading.Thread(target=_watch_cancel, name="sftp-cancel", daemon=True).start()
    client = _new_client()
    gate_held = False
    try:
        _raise_if_cancelled()
        while not _handshake_gate.acquire(timeout=0.2):
            _raise_if_cancelled()
        gate_held = True
        try:
            _connect(client)
        except paramiko.AuthenticationException as exc:
            _close_ssh_client(client)
            raise SftpError(
                "SFTP login failed. CRFS SSH uses a different account from HTTP. "
                "Open Settings and check the SFTP username and password."
            ) from exc
        except paramiko.SSHException as exc:
            message = str(exc).lower()
            retryable = any(token in message for token in ("algorithm", "kex", "no matching", "banner"))
            if not retryable:
                raise
            _close_ssh_client(client)
            client = _new_client()
            _connect(client, {"disabled_algorithms": {"pubkeys": ["rsa-sha2-256", "rsa-sha2-512"]}})
        holder["connected"] = True
        _raise_if_cancelled()
        return ParamikoSftpBrowser(client)
    except SftpError:
        _close_ssh_client(holder.get("client"))
        raise
    except Exception as exc:
        _close_ssh_client(holder.get("client"))
        if _cancelled():
            raise SftpError("SFTP cancelled.") from exc
        if not holder.get("connected"):
            raise SftpConnectError(
                f"SFTP connection failed: {_failure_text(exc)}",
                retry_after_s=connect_pacer.cooling_down_s(),
            ) from exc
        raise SftpError(f"SFTP connection failed: {exc}") from exc
    finally:
        if gate_held:
            _handshake_gate.release()
        done.set()


def _demo_dir_key(path: str) -> str:
    text = (path or "/").replace("\\", "/")
    if text != "/" and not text.endswith("/"):
        text += "/"
    return text


DEMO_SAMPLE_RATE = 20_000


class DemoSftpBrowser(SftpBrowser):
    """In-memory folders. Tests and demo mode must not open sockets."""

    def __init__(self, *, include_today: bool = True) -> None:
        today = today_remdata_directory()
        now = datetime.now()
        wav = stereo_iq_tone_wav_bytes()
        self._lock = threading.RLock()
        self._files: dict[str, bytes] = {}
        self._storage_total = DEMO_TOTAL_BYTES
        self._storage_free = DEMO_FREE_BYTES
        self._storage_fail = False
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

    def add_file(
        self,
        name: str,
        directory: str,
        *,
        payload: bytes | None = None,
        size: int | None = None,
        modified: datetime | None = None,
    ) -> RemoteEntry:
        with self._lock:
            return self._add_file_locked(
                name,
                directory,
                payload=payload,
                size=size,
                modified=modified,
            )

    def _add_file_locked(
        self,
        name: str,
        directory: str,
        *,
        payload: bytes | None = None,
        size: int | None = None,
        modified: datetime | None = None,
    ) -> RemoteEntry:
        folder = _demo_dir_key(directory)
        if folder not in self._dirs:
            parent = folder.rsplit("/", 2)[0] + "/" if folder.count("/") > 2 else "/mnt/1/remdata/"
            date_name = folder.strip("/").rsplit("/", 1)[-1]
            remote_dir = folder.rstrip("/")
            if parent in self._dirs and not any(item.path.rstrip("/") == remote_dir for item in self._dirs[parent]):
                self._dirs[parent].append(
                    RemoteEntry(date_name, remote_dir, True, 0, modified or datetime.now())
                )
            self._dirs[folder] = []
        data = payload if payload is not None else stereo_iq_tone_wav_bytes()
        path = join_remote(directory, name)
        nbytes = int(size if size is not None else len(data))
        if size is not None and size != len(data):
            data = (data + b"\x00" * max(0, size - len(data)))[:size] if size else data
            data = data[:nbytes] if nbytes <= len(data) else data + bytes(nbytes - len(data))
        previous = len(self._files.get(path, b""))
        self._files[path] = data[:nbytes] if nbytes <= len(data) else data + bytes(nbytes - len(data))
        delta = nbytes - previous
        self._storage_free = max(0, min(self._storage_total, self._storage_free - delta))
        entry = RemoteEntry(name, path, False, nbytes, modified or datetime.now())
        current = [item for item in self._dirs[folder] if item.path != path]
        current.append(entry)
        self._dirs[folder] = sort_remote_entries(current)
        return entry

    def set_file_size(self, remote_path: str, size: int, *, modified: datetime | None = None) -> None:
        with self._lock:
            self._set_file_size_locked(remote_path, size, modified=modified)

    def _set_file_size_locked(self, remote_path: str, size: int, *, modified: datetime | None = None) -> None:
        payload = self._files.get(remote_path, stereo_iq_tone_wav_bytes())
        nbytes = max(0, int(size))
        previous = len(self._files.get(remote_path, b""))
        self._files[remote_path] = payload[:nbytes] if nbytes <= len(payload) else payload + bytes(nbytes - len(payload))
        self._storage_free = max(0, min(self._storage_total, self._storage_free - (nbytes - previous)))
        when = modified or datetime.now()
        for folder, entries in list(self._dirs.items()):
            updated: list[RemoteEntry] = []
            changed = False
            for item in entries:
                if item.path == remote_path:
                    updated.append(RemoteEntry(item.name, item.path, False, nbytes, when))
                    changed = True
                else:
                    updated.append(item)
            if changed:
                self._dirs[folder] = sort_remote_entries(updated)

    def publish_capture(
        self,
        stem: str,
        *,
        parts: int = 1,
        directory: str | None = None,
        when: datetime | None = None,
        size: int | None = None,
        seconds: float | None = None,
        center_hz: float = 0.0,
        bandwidth_hz: float = 0.0,
        sample_rate: int = DEMO_SAMPLE_RATE,
    ) -> list[RemoteEntry]:
        """Files of one simulated capture.

        With `seconds`, every part is a CRFS-style WAVE whose header holds its
        share of that many seconds at `sample_rate` (a small rate keeps the
        files tiny) and whose metadata names the centre and bandwidth; without
        it, the tone file padded to `size` bytes.
        """
        with self._lock:
            return self._publish_capture_locked(
                stem, parts=parts, directory=directory, when=when, size=size,
                seconds=seconds, center_hz=center_hz, bandwidth_hz=bandwidth_hz, sample_rate=sample_rate,
            )

    def _publish_capture_locked(
        self,
        stem: str,
        *,
        parts: int = 1,
        directory: str | None = None,
        when: datetime | None = None,
        size: int | None = None,
        seconds: float | None = None,
        center_hz: float = 0.0,
        bandwidth_hz: float = 0.0,
        sample_rate: int = DEMO_SAMPLE_RATE,
    ) -> list[RemoteEntry]:
        from .iq_wav import crfs_style_wav_bytes

        folder = directory or today_remdata_directory()
        stamp = when or datetime.now()
        out: list[RemoteEntry] = []
        count = max(1, int(parts))
        total_frames = int(round(max(float(seconds or 0.0), 0.0) * int(sample_rate)))
        for index in range(1, count + 1):
            name = f"{stem}_{index:04d}.wav"
            if seconds is not None:
                share = total_frames // count + (1 if index <= total_frames % count else 0)
                start = stamp.astimezone() + timedelta(seconds=(total_frames // count) * (index - 1) / float(sample_rate))
                payload = crfs_style_wav_bytes(
                    frames=share,
                    sample_rate=int(sample_rate),
                    center_hz=center_hz,
                    bandwidth_hz=bandwidth_hz,
                    signal_time=start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                )
                nbytes = len(payload)
            else:
                payload = stereo_iq_tone_wav_bytes()
                nbytes = int(size if size is not None else len(payload))
            out.append(
                self._add_file_locked(
                    name,
                    folder,
                    payload=payload,
                    size=nbytes,
                    modified=stamp + timedelta(seconds=index - 1),
                )
            )
        return out

    def open_read(self, remote_path: str):
        import io

        with self._lock:
            payload = self._files.get((remote_path or "").replace("\\", "/"))
        if payload is None:
            raise SftpNotFoundError(f"File not found: {remote_path}")
        return io.BytesIO(payload), len(payload)

    def directory_exists(self, path: str) -> bool:
        with self._lock:
            key = _demo_dir_key(path)
            return key in self._dirs

    def listdir(self, path: str) -> list[RemoteEntry]:
        with self._lock:
            key = _demo_dir_key(path)
            if key not in self._dirs:
                raise SftpNotFoundError(f"Folder not found: {path}")
            return sort_remote_entries(list(self._dirs[key]))

    def read_prefix(self, remote_path: str, local_path: Path, max_bytes: int) -> int:
        with self._lock:
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
        with self._lock:
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

    def remote_sha256(self, remote_path: str, *, timeout_s: float = 0.0) -> tuple[str, str]:
        import hashlib

        with self._lock:
            payload = self._files.get((remote_path or "").replace("\\", "/"))
            fail = getattr(self, "_checksum_fail", False)
        if payload is None:
            return "", f"File not found: {remote_path}"
        if fail:
            return "", "sha256sum unavailable (demo)"
        return hashlib.sha256(payload).hexdigest(), "sha256 of the demo file"

    def set_checksum_available(self, available: bool) -> None:
        """Simulate a sensor without a usable sha256sum."""
        with self._lock:
            self._checksum_fail = not available

    def set_storage(self, *, total: int | None = None, free: int | None = None, fail: bool | None = None) -> None:
        with self._lock:
            if total is not None:
                self._storage_total = max(int(total), 0)
            if free is not None:
                self._storage_free = max(0, min(int(free), self._storage_total))
            if fail is not None:
                self._storage_fail = bool(fail)

    def storage_usage(self, path: str) -> StorageSnapshot:
        with self._lock:
            if self._storage_fail:
                raise SftpError("Could not read storage.")
            return StorageSnapshot(
                path=path,
                total_bytes=int(self._storage_total),
                free_bytes=min(int(self._storage_free), int(self._storage_total)),
                ok=True,
            )

    def pending_writeback_bytes(self) -> int | None:
        return 0

    def remove_file(self, remote_path: str) -> None:
        with self._lock:
            self._remove_file_locked(remote_path)

    def _remove_file_locked(self, remote_path: str) -> None:
        path = (remote_path or "").replace("\\", "/")
        if path not in self._files:
            raise SftpError(f"File not found: {path}")
        size = len(self._files[path])
        del self._files[path]
        self._storage_free = min(self._storage_total, self._storage_free + size)
        for folder, entries in list(self._dirs.items()):
            self._dirs[folder] = [item for item in entries if item.path != path]
