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
# The sensor said "finished" but never named a file: how long to keep looking
# before giving up. The first part is normally announced ~1 s after the POST.
NO_FILE_GRACE_S = 10.0


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
    sensor_finished: bool = False,
    stream_pending: bool = False,
    expected_bytes: int | None = None,
) -> bool:
    """True when the capture can be treated as complete.

    Rules, in order:
    - Nothing before the recording time has elapsed.
    - While the sensor's event stream is still open and has not said
      "finished", wait (the stream has its own time budget).
    - Files that are still growing are never complete. A listing that has not
      changed for `max_watch_s` ends the wait anyway, so a stalled writer cannot
      hold the application forever.
    - No file at all after `max_watch_s`: give up (caller reports UNKNOWN).
    - All known parts stable: done once the sensor said finished and the parts
      hold at least 98 % of the expected bytes, otherwise after `settle_s` of
      quiet so a late part can still be picked up.

    HTTP success, the duration timer, or the first file is not enough on its own.
    """
    if not elapsed:
        return False
    since_elapsed = (now - elapsed_at).total_seconds() if elapsed_at is not None else 0.0
    quiet_s = (now - state.last_change_at).total_seconds() if state.last_change_at is not None else None
    if stream_pending and not sensor_finished:
        return False
    pending = [
        path
        for path, size in state.sizes.items()
        if size > 0 and path not in state.finalized
    ]
    if pending:
        # Growing files: only a long stall ends the wait.
        return quiet_s is not None and quiet_s >= max_watch_s
    if not state.finalized:
        return elapsed_at is not None and since_elapsed >= max_watch_s
    if quiet_s is None:
        return False
    if sensor_finished and expected_bytes and expected_bytes > 0:
        total = sum(size for path, size in state.sizes.items() if path in state.finalized)
        if total >= 0.98 * expected_bytes:
            return True
    return quiet_s >= max(settle_s, 0)
