"""Track worker QThreads so the app can stop them before Python tears down.

Deleting a QThread that is still running is a Qt fatal error. On Windows Qt
ends the process with fail-fast code 0xC0000409 ("QThread: Destroyed while
thread '' is still running") and Windows files an Application Error event.
Python deletes the QThread wrappers during interpreter shutdown, so a sensor
check or SFTP connect that is still in flight when the user closes the window
used to crash every exit.

run() calls stop_worker_threads() after the event loop ends: it asks every
tracked worker to abort, waits a bounded time for the threads to finish, and
deliberately leaks any thread that is still blocked (a few KB, once, at exit)
instead of letting Python delete it.
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QThread

_log = logging.getLogger(__name__)
_TRACKED: list[tuple[QThread, QObject | None]] = []


def track(thread: QThread, worker: QObject | None = None) -> None:
    """Remember a worker thread until it finishes."""
    entry = (thread, worker)
    _TRACKED.append(entry)

    def _forget() -> None:
        try:
            _TRACKED.remove(entry)
        except ValueError:
            pass

    thread.finished.connect(_forget)


def tracked_count() -> int:
    return len(_TRACKED)


def tracked_workers() -> list[QObject]:
    """The workers whose threads are still running."""
    return [worker for _thread, worker in list(_TRACKED) if worker is not None]


def stop_worker_threads(timeout_s: float = 8.0) -> int:
    """Stop every tracked thread. Returns how many had to be leaked."""
    entries = list(_TRACKED)
    for thread, worker in entries:
        abort = getattr(worker, "abort", None)
        if callable(abort):
            try:
                abort()
            except Exception:  # noqa: BLE001 - best effort during shutdown
                pass
        try:
            thread.requestInterruption()
            thread.quit()
        except RuntimeError:
            pass  # Qt object already deleted
    deadline = time.monotonic() + max(0.0, float(timeout_s))
    leaked = 0
    for thread, worker in entries:
        try:
            if thread.isFinished():
                continue
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            if thread.wait(remaining_ms):
                continue
        except RuntimeError:
            continue
        leaked += 1
        _leak(thread, worker)
    _TRACKED.clear()
    if leaked:
        _log.warning("%d worker thread(s) still running at exit; left to the OS.", leaked)
    return leaked


def _leak(thread: QThread, worker: QObject | None) -> None:
    """Give up Python ownership so the running thread is never deleted."""
    try:
        import shiboken6

        for obj in (worker, thread):
            if obj is not None:
                shiboken6.invalidate(obj)
    except Exception:  # noqa: BLE001
        pass
