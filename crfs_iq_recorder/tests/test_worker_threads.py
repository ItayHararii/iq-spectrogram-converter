"""Worker threads are stopped or leaked before exit, never deleted while running."""

from __future__ import annotations

import os
import threading
import time

import pytest
from PySide6.QtCore import QObject, QThread, Slot

from crfs_iq_recorder import worker_threads


class _SleepWorker(QObject):
    def __init__(self, seconds: float) -> None:
        super().__init__()
        self._seconds = seconds
        self.aborted = threading.Event()
        self.done = threading.Event()

    def abort(self) -> None:
        self.aborted.set()

    @Slot()
    def run(self) -> None:
        deadline = time.monotonic() + self._seconds
        while time.monotonic() < deadline and not self.aborted.is_set():
            time.sleep(0.02)
        self.done.set()
        try:
            thread = self.thread()
            if thread is not None:
                thread.quit()
        except RuntimeError:
            pass  # wrapper leaked by stop_worker_threads(); the thread ends anyway


class _StuckWorker(_SleepWorker):
    def abort(self) -> None:  # ignores the request, like a hung socket
        pass


@pytest.fixture
def qt_app():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _start(worker: _SleepWorker) -> QThread:
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker_threads.track(thread, worker)
    thread.start()
    return thread


def test_stop_aborts_and_waits_for_cooperative_workers(qt_app):
    workers = [_SleepWorker(30.0) for _ in range(3)]
    threads = [_start(w) for w in workers]
    assert worker_threads.tracked_count() >= 3
    started = time.monotonic()
    leaked = worker_threads.stop_worker_threads(timeout_s=5.0)
    assert leaked == 0
    assert time.monotonic() - started < 3.0
    assert all(t.isFinished() for t in threads)
    assert all(w.done.is_set() for w in workers)
    assert worker_threads.tracked_count() == 0


def test_stuck_worker_is_leaked_not_deleted(qt_app):
    worker = _StuckWorker(1.5)
    _start(worker)
    leaked = worker_threads.stop_worker_threads(timeout_s=0.3)
    assert leaked == 1
    # The QThread object must survive the leak so Qt never sees a running
    # thread being destroyed; the Python wrapper is simply detached.
    assert worker.done.wait(5.0)
    time.sleep(0.2)


def test_finished_threads_are_forgotten(qt_app):
    worker = _SleepWorker(0.05)
    thread = _start(worker)
    assert thread.wait(5000)
    deadline = time.monotonic() + 2.0
    while worker_threads.tracked_count() and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.01)
    assert worker_threads.tracked_count() == 0
