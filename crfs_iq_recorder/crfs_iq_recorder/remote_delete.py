"""Delete one folder of recordings from the sensor, and nothing else.

What is deleted is exactly what was counted for the confirmation:

- the folder lies strictly inside the recording root (``/mnt/1/remdata/``):
  never the root itself, never a path with ``.`` or ``..`` parts, and its real
  path on the sensor is the same path below the root's real path, so a folder
  reached through a link is refused;
- every entry is examined with lstat and nothing is followed: a link is
  removed as a link (what it points to is not touched) and never descended;
- right before each removal the entry is checked again: the real path of its
  folder is unchanged and it still has the kind, size and time that were
  counted. Anything new or changed stays on the sensor and is reported, and
  its folder is then kept;
- files and links first, then the sub-folders deepest first, then the folder.

Local copies are never touched here.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .constants import SFTP_REMDATA_ROOT
from .operator_text import size_text
from .recording_history import iq_group_key
from .sftp_client import SftpBrowser, SftpError, SftpNotFoundError

# A file the sensor wrote this recently may still be growing (a 125 MS/s part grows ~500 MB/s).
RECENT_WRITE_S = 120.0
MAX_ENTRIES = 50_000
MAX_DEPTH = 32

KIND_FILE = "file"
KIND_LINK = "link"
KIND_DIR = "dir"

REASON_RECORDING = "Recording in progress"
REASON_COLLECTION = "Collection running"
REASON_UNCHECKED = "Could not check recordings"
REASON_READING = "Download or preview running"
REASON_LIVE = "Recording being written"
REASON_RECENT = "Written in the last 2 min"


class DeleteRefused(Exception):
    """The folder may not be deleted; the message says why in a few words."""


def _parts(path: str) -> list[str]:
    return [part for part in str(path).split("/") if part]


def normalise_root(root: str = SFTP_REMDATA_ROOT) -> str:
    """'/mnt/1/remdata' (no trailing slash)."""
    parts = _parts(root)
    return "/" + "/".join(parts) if parts else "/"


def confined_folder(path: str, root: str = SFTP_REMDATA_ROOT) -> str:
    """The folder as '/a/b/c' when it lies strictly inside `root`; DeleteRefused otherwise."""
    text = str(path or "")
    if not text.strip():
        raise DeleteRefused("No folder selected")
    if any(ord(ch) < 32 for ch in text):
        raise DeleteRefused("Unusual characters in the path")
    if not text.startswith("/"):
        raise DeleteRefused("Not a full sensor path")
    parts = _parts(text)
    if any(part in {".", ".."} for part in parts):
        raise DeleteRefused("The path contains . or ..")
    base = normalise_root(root)
    if base == "/":
        raise DeleteRefused("No recording root set")
    folder = "/" + "/".join(parts)
    if folder == base:
        raise DeleteRefused("Recording root")
    if not folder.startswith(base + "/"):
        raise DeleteRefused("Outside recording storage")
    return folder


def folder_refusal(path: str, root: str = SFTP_REMDATA_ROOT) -> str:
    """'' when `path` may be offered for deletion, else the short reason."""
    try:
        confined_folder(path, root)
    except DeleteRefused as exc:
        return str(exc)
    return ""


@dataclass(frozen=True)
class PlannedEntry:
    path: str
    kind: str
    size: int
    mtime: float | None
    depth: int


@dataclass(frozen=True)
class DeletePlan:
    """Everything inside one folder, as counted for the confirmation."""

    folder: str  # '/mnt/1/remdata/x', no trailing slash
    root: str
    real_root: str
    entries: tuple[PlannedEntry, ...]
    folder_mtime: float | None
    sensor_now: float | None  # None: the sensor's clock could not be read

    @property
    def files(self) -> int:
        return sum(1 for entry in self.entries if entry.kind == KIND_FILE)

    @property
    def links(self) -> int:
        return sum(1 for entry in self.entries if entry.kind == KIND_LINK)

    @property
    def subfolders(self) -> int:
        return sum(1 for entry in self.entries if entry.kind == KIND_DIR)

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.entries if entry.kind == KIND_FILE)

    def file_paths(self) -> list[str]:
        return [entry.path for entry in self.entries if entry.kind != KIND_DIR]

    def real_path(self, path: str) -> str:
        """Where `path` must resolve on the sensor when no link is on the way."""
        return self.real_root.rstrip("/") + path[len(self.root):]

    def written_recently(self, within_s: float = RECENT_WRITE_S, *, now: float | None = None) -> bool:
        """True when an entry (or the folder) changed within `within_s` of the sensor's clock, or in its future."""
        clock = now if now is not None else (self.sensor_now if self.sensor_now is not None else time.time())
        times = [entry.mtime for entry in self.entries] + [self.folder_mtime]
        return any(t is not None and t >= clock - within_s for t in times)

    def contents_text(self) -> str:
        """'14 files (12.3 GB), 1 subfolder, 1 link' or 'empty'."""
        bits: list[str] = []
        if self.files:
            bits.append(f"{self.files} file{'s' if self.files != 1 else ''} ({size_text(self.total_bytes)})")
        if self.subfolders:
            bits.append(f"{self.subfolders} subfolder{'s' if self.subfolders != 1 else ''}")
        if self.links:
            bits.append(f"{self.links} link{'s' if self.links != 1 else ''}")
        return ", ".join(bits) if bits else "empty"

    def removal_order(self) -> list[PlannedEntry]:
        others = sorted((entry for entry in self.entries if entry.kind != KIND_DIR), key=lambda e: e.path)
        dirs = sorted((entry for entry in self.entries if entry.kind == KIND_DIR), key=lambda e: (-e.depth, e.path))
        return [*others, *dirs, PlannedEntry(self.folder, KIND_DIR, 0, self.folder_mtime, 0)]


@dataclass(frozen=True)
class DeleteProgress:
    phase: str  # "counting" or "deleting"
    done: int
    total: int
    bytes_done: int = 0
    bytes_total: int = 0


@dataclass(frozen=True)
class DeleteResult:
    folder: str
    items_total: int  # files and links that were counted
    items_deleted: int
    bytes_deleted: int
    folders_total: int  # subfolders and the folder itself
    folders_deleted: int
    already_gone: int  # files and links someone else removed meanwhile
    failed: tuple[tuple[str, str], ...]  # (path, reason)
    folder_removed: bool
    cancelled: bool
    links: int = 0
    connection_lost: bool = False
    failed_folders: tuple[str, ...] = ()  # the failed paths that are folders

    @property
    def complete(self) -> bool:
        return self.folder_removed and not self.failed

    def _noun(self, count: int | None = None) -> str:
        noun = "item" if self.links else "file"
        return noun if (self.items_total if count is None else count) == 1 else noun + "s"

    def failed_items(self) -> list[tuple[str, str]]:
        """Failed files and links (folders kept because something stayed inside are not repeated)."""
        return [item for item in self.failed if item[0] not in self.failed_folders and item[0] != self.folder]

    @staticmethod
    def _names(failed: list[tuple[str, str]], limit: int = 3) -> str:
        shown = [f"{path.rsplit('/', 1)[-1]} ({reason})" for path, reason in failed[:limit]]
        more = len(failed) - len(shown)
        return ", ".join(shown) + (f" and {more} more" if more > 0 else "")

    def failed_text(self, limit: int = 3) -> str:
        return self._names(list(self.failed), limit)

    def summary(self) -> str:
        folder = self.folder.rstrip("/") + "/"
        if self.complete:
            if not self.items_total:
                return f"Deleted the folder {folder} from the sensor."
            gone = f" ({self.already_gone} already gone)" if self.already_gone else ""
            return f"Deleted {folder} from the sensor: {self.items_deleted} {self._noun(self.items_deleted)}, {size_text(self.bytes_deleted)}{gone}."
        done = f"Deleted {self.items_deleted} of {self.items_total} {self._noun()}"
        if self.cancelled:
            return f"Stopped. {done}. The folder was kept."
        if self.connection_lost:
            return f"The connection to the sensor was lost. {done}. The folder was kept."
        items = self.failed_items()
        if items:
            return f"{done}. {len(items)} could not be deleted: {self._names(items)}. The folder was kept."
        subfolders = [item for item in self.failed if item[0] in self.failed_folders and item[0] != self.folder]
        if subfolders:
            noun = "subfolder" if len(subfolders) == 1 else "subfolders"
            return f"{done}. {len(subfolders)} {noun} could not be deleted: {self._names(subfolders)}. The folder was kept."
        reason = next((why for path, why in self.failed if path == self.folder), "it is not empty")
        return f"{done}. The folder was kept: {reason}."


# ---------------------------------------------------------------------------- counting


def plan_folder_delete(
    browser: SftpBrowser,
    path: str,
    *,
    root: str = SFTP_REMDATA_ROOT,
    cancel: threading.Event | None = None,
    progress: Callable[[DeleteProgress], None] | None = None,
    max_entries: int = MAX_ENTRIES,
    max_depth: int = MAX_DEPTH,
) -> DeletePlan:
    """Count everything inside the folder (lstat, nothing followed). DeleteRefused says why it may not go."""
    folder = confined_folder(path, root)
    base = normalise_root(root)
    try:
        real_root = browser.realpath(base)
        top = browser.lstat(folder)
    except SftpNotFoundError as exc:
        raise DeleteRefused("The folder is no longer there") from exc
    if top.is_link:
        raise DeleteRefused("This is a link, not a folder")
    if not top.is_dir:
        raise DeleteRefused("Not a folder")
    expected_real = real_root.rstrip("/") + folder[len(base):]
    if browser.realpath(folder) != expected_real:
        raise DeleteRefused("Reached through a link")
    entries: list[PlannedEntry] = []
    stack: list[tuple[str, int]] = [(folder, 0)]
    while stack:
        if cancel is not None and cancel.is_set():
            raise DeleteRefused("Cancelled")
        current, depth = stack.pop()
        for name, st in browser.scan(current):
            if not name or name in {".", ".."} or "/" in name:
                raise DeleteRefused("Unexpected entry name")
            child = f"{current}/{name}"
            if st.is_dir:
                # Confirm with lstat before descending: a server that followed links in its listing is not trusted.
                st = browser.lstat(child)
            if st.is_link:
                entries.append(PlannedEntry(child, KIND_LINK, 0, st.mtime, depth + 1))
            elif st.is_dir:
                if depth + 1 > max_depth:
                    raise DeleteRefused("Folders nested too deeply")
                entries.append(PlannedEntry(child, KIND_DIR, 0, st.mtime, depth + 1))
                stack.append((child, depth + 1))
            else:
                entries.append(PlannedEntry(child, KIND_FILE, int(st.size or 0), st.mtime, depth + 1))
            if len(entries) > max_entries:
                raise DeleteRefused(f"More than {max_entries:,} items")
        if progress is not None:
            progress(DeleteProgress("counting", len(entries), 0))
    return DeletePlan(
        folder=folder,
        root=base,
        real_root=real_root,
        entries=tuple(entries),
        folder_mtime=top.mtime,
        sensor_now=browser.remote_time(),
    )


# ---------------------------------------------------------------------------- deleting


def _short(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text.rsplit(": ", 1)[-1] if ": " in text else text


def _recheck(browser: SftpBrowser, plan: DeletePlan, entry: PlannedEntry) -> str:
    """'' when `entry` is still exactly what was counted, '-' when it is gone, else why it is left."""
    if entry.path != plan.folder and not entry.path.startswith(plan.folder + "/"):
        return "outside the folder"
    try:
        confined_folder(entry.path, plan.root)
    except DeleteRefused as exc:
        return str(exc).lower()
    parent = entry.path.rsplit("/", 1)[0]
    try:
        if browser.realpath(parent) != plan.real_path(parent):
            return "its folder is now reached through a link"
        st = browser.lstat(entry.path)
    except SftpNotFoundError:
        return "-"
    except SftpError as exc:
        return _short(exc)
    if entry.kind == KIND_LINK:
        return "" if st.is_link else "changed since it was counted"
    if entry.kind == KIND_DIR:
        return "" if st.is_dir else "changed since it was counted"
    if st.is_link or st.is_dir:
        return "changed since it was counted"
    if int(st.size or 0) != entry.size or st.mtime != entry.mtime:
        return "changed since it was counted"
    return ""


def delete_planned(
    browser: SftpBrowser,
    plan: DeletePlan,
    *,
    cancel: threading.Event | None = None,
    progress: Callable[[DeleteProgress], None] | None = None,
) -> DeleteResult:
    """Remove what the plan counted, checking each entry again first. Never raises for one entry."""
    order = plan.removal_order()
    items_total = plan.files + plan.links
    bytes_total = plan.total_bytes
    items_deleted = folders_deleted = bytes_deleted = gone = 0
    failed: list[tuple[str, str]] = []
    folder_removed = cancelled = lost = False
    failed_folders: list[str] = []

    def remove(entry: PlannedEntry) -> str:
        """'' removed, '-' already gone, else why it stays."""
        problem = _recheck(browser, plan, entry)
        if problem:
            return problem
        try:
            if entry.kind == KIND_DIR:
                browser.remove_dir(entry.path)
            else:
                # SFTP REMOVE is unlink(2): a link goes, what it points to stays.
                browser.remove_file(entry.path)
        except SftpNotFoundError:
            return "-"
        except SftpError as exc:
            return _short(exc)
        return ""

    for index, entry in enumerate(order, start=1):
        if cancel is not None and cancel.is_set():
            cancelled = True
            break
        if failed and not browser.is_alive():
            lost = True
            break
        outcome = remove(entry)
        if outcome in {"", "-"}:
            if entry.kind == KIND_DIR:
                folders_deleted += 1
                folder_removed = folder_removed or entry.path == plan.folder
            elif outcome == "-":
                gone += 1
            else:
                items_deleted += 1
                bytes_deleted += entry.size
        else:
            failed.append((entry.path, outcome))
            if entry.kind == KIND_DIR:
                failed_folders.append(entry.path)
        if progress is not None:
            progress(DeleteProgress("deleting", index, len(order), bytes_deleted, bytes_total))
    return DeleteResult(
        folder=plan.folder,
        items_total=items_total,
        items_deleted=items_deleted,
        bytes_deleted=bytes_deleted,
        folders_total=plan.subfolders + 1,
        folders_deleted=folders_deleted,
        already_gone=gone,
        failed=tuple(failed),
        folder_removed=folder_removed,
        cancelled=cancelled,
        links=plan.links,
        connection_lost=lost,
        failed_folders=tuple(failed_folders),
    )


# ---------------------------------------------------------------------------- what is busy


@dataclass(frozen=True)
class SensorActivity:
    """What may be writing to or reading from the sensor's storage right now."""

    recording: str = ""  # why no folder may be deleted now ('' when nothing records)
    live_stems: frozenset[str] = frozenset()  # recordings still being written (casefolded stems)
    reading: frozenset[str] = frozenset()  # remote files downloads and previews read or are queued to read


def _inside(path: str, folder: str) -> bool:
    return str(path).startswith(folder.rstrip("/") + "/")


def _recording_key(name: str) -> str:
    """'tx_0002.WAV', '/mnt/1/remdata/d/tx.wav' and 'tx' all give 'tx'."""
    key = iq_group_key(str(name).rsplit("/", 1)[-1])
    return key[:-4] if key.endswith(".wav") else key


def _is_live(paths: Iterable[str], activity: SensorActivity) -> bool:
    stems = {_recording_key(stem) for stem in activity.live_stems if stem}
    return bool(stems) and any(_recording_key(path) in stems for path in paths)


def folder_busy_reason(folder: str, activity: SensorActivity) -> str:
    """Before counting: '' or why the folder cannot be deleted now."""
    if activity.recording:
        return activity.recording
    if any(_inside(path, folder) for path in activity.reading):
        return REASON_READING
    return ""


def plan_busy_reason(plan: DeletePlan, activity: SensorActivity) -> str:
    """After counting: also a recording file inside, or anything written in the last minutes."""
    reason = folder_busy_reason(plan.folder, activity)
    if reason:
        return reason
    if _is_live(plan.file_paths(), activity):
        return REASON_LIVE
    if plan.written_recently():
        return REASON_RECENT
    return ""


def files_busy_reason(paths: Iterable[str], activity: SensorActivity) -> str:
    """'' or why these files cannot be deleted now."""
    wanted = list(paths)
    if _is_live(wanted, activity):
        return REASON_LIVE
    if any(path in activity.reading for path in wanted):
        return REASON_READING
    return ""


_IDLE_RUNNER_STATES = {"idle", "done", "interrupted", "paused", "paused_backlog"}


def host_activity(host, *, also_reading: Iterable[str] = (), skip: Iterable[object] = ()) -> SensorActivity:
    """Ask the main window (duck-typed, like the collection runner does) and every running transfer worker
    except those in `skip`.

    A main window that cannot be asked counts as recording: deleting is refused rather than guessed.
    """
    from .worker_threads import tracked_workers

    skipped = {id(worker) for worker in skip}
    reading: set[str] = {str(path) for path in also_reading if path}
    for worker in tracked_workers():
        if id(worker) in skipped:
            continue
        paths = getattr(worker, "remote_paths", None)
        if callable(paths):
            try:
                reading.update(str(path) for path in paths() if path)
            except RuntimeError:  # deleted C++ object
                continue
    if host is None:
        return SensorActivity(reading=frozenset(reading))
    try:
        recording = REASON_RECORDING if host.capture_busy_reason() else ""
        runner = getattr(getattr(host, "cellular_panel", None), "runner", None)
        if not recording and runner is not None and str(getattr(runner, "state", "idle")) not in _IDLE_RUNNER_STATES:
            recording = REASON_COLLECTION
        stem_of = getattr(host, "_live_recording_stem", None)
        stem = stem_of() if callable(stem_of) else None
        for _task, _round, item in list(getattr(runner, "_downloads_pending", None) or []):
            if isinstance(item, dict) and item.get("remote_path"):
                reading.add(str(item["remote_path"]))
        job = getattr(host, "_manual_download", None)
        if job is not None and job.busy():
            reading.update(str(path) for path in getattr(job, "_remote", []) or [])
    except Exception:  # noqa: BLE001 - unknown means busy
        return SensorActivity(recording=REASON_UNCHECKED, reading=frozenset(reading))
    return SensorActivity(
        recording=recording,
        live_stems=frozenset({str(stem).casefold()} if stem else set()),
        reading=frozenset(reading),
    )


__all__ = [
    "DeletePlan",
    "DeleteProgress",
    "DeleteRefused",
    "DeleteResult",
    "PlannedEntry",
    "SensorActivity",
    "confined_folder",
    "delete_planned",
    "files_busy_reason",
    "folder_busy_reason",
    "folder_refusal",
    "host_activity",
    "plan_busy_reason",
    "plan_folder_delete",
]
