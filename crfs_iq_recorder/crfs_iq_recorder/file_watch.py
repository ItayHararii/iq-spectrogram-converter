"""Decide when split IQ parts are finished writing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .sftp_client import RemoteEntry, SftpError
from .sftp_paths import candidate_today_directories

STABLE_LISTINGS = 2
SETTLE_S = 8.0
POLL_S = 2.0
MAX_WATCH_AFTER_ELAPSED_S = 60.0


def folders_for_recording(started_at: datetime, now: datetime | None = None) -> list[str]:
    """Local and UTC date folders for the start time and now (midnight crossings)."""
    current = now or datetime.now()
    paths: list[str] = []
    for when in (started_at, current):
        for folder in candidate_today_directories(when):
            if folder not in paths:
                paths.append(folder)
    return paths


def list_watch_entries(browser, folders: list[str]) -> list[RemoteEntry]:
    files: list[RemoteEntry] = []
    seen: set[str] = set()
    for folder in folders:
        try:
            if not browser.directory_exists(folder):
                continue
            entries = browser.listdir(folder)
        except (SftpError, OSError, AttributeError):
            continue
        for entry in entries:
            if entry.is_dir or entry.path in seen:
                continue
            seen.add(entry.path)
            files.append(entry)
    return files


@dataclass
class FileWatchState:
    sizes: dict[str, int] = field(default_factory=dict)
    stable: dict[str, int] = field(default_factory=dict)
    finalized: set[str] = field(default_factory=set)
    last_change_at: datetime | None = None
    listings: int = 0

    def copy(self) -> FileWatchState:
        return FileWatchState(
            sizes=dict(self.sizes),
            stable=dict(self.stable),
            finalized=set(self.finalized),
            last_change_at=self.last_change_at,
            listings=self.listings,
        )


def update_file_watch(
    state: FileWatchState,
    entries: list[RemoteEntry],
    *,
    now: datetime,
    stable_needed: int = STABLE_LISTINGS,
) -> tuple[FileWatchState, list[RemoteEntry]]:
    """Mark files whose size stayed the same for `stable_needed` listings."""
    next_state = state.copy()
    next_state.listings += 1
    by_path = {entry.path: entry for entry in entries if not entry.is_dir}
    newly: list[RemoteEntry] = []
    for path, entry in by_path.items():
        size = int(entry.size or 0)
        previous = next_state.sizes.get(path)
        if previous is None:
            next_state.sizes[path] = size
            next_state.stable[path] = 1 if size > 0 else 0
            next_state.last_change_at = now
            continue
        if size != previous:
            next_state.sizes[path] = size
            next_state.stable[path] = 1 if size > 0 else 0
            next_state.last_change_at = now
            next_state.finalized.discard(path)
            continue
        if size <= 0:
            next_state.stable[path] = 0
            continue
        next_state.stable[path] = next_state.stable.get(path, 0) + 1
        if next_state.stable[path] >= stable_needed and path not in next_state.finalized:
            next_state.finalized.add(path)
            newly.append(entry)
    return next_state, newly


def capture_is_settled(
    state: FileWatchState,
    *,
    now: datetime,
    elapsed: bool,
    settle_s: float = SETTLE_S,
    max_watch_s: float = MAX_WATCH_AFTER_ELAPSED_S,
    elapsed_at: datetime | None = None,
) -> bool:
    """True when elapsed, known files are stable, and no new parts arrived recently.

    HTTP success, the duration timer, or the first file is not enough on its own.
    """
    if not elapsed:
        return False
    if elapsed_at is not None and (now - elapsed_at).total_seconds() >= max_watch_s:
        return True
    if not state.finalized:
        return False
    pending = [
        path
        for path, size in state.sizes.items()
        if size > 0 and path not in state.finalized
    ]
    if pending:
        return False
    if state.last_change_at is None:
        return False
    return (now - state.last_change_at) >= timedelta(seconds=max(settle_s, 0))
