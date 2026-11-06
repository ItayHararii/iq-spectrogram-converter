"""Downloads of files over 2 GiB: a 125 MS/s 5 s capture on a Node 100-18 is one 2.3-2.5 GB part.

1.17.0 declared the progress signal as (str, int, int). Above 2,147,483,647 bytes
PySide6 raised OverflowError on every 32 KB chunk: with no receiver (Cellular
runner, Recorder tab) the download failed at once with "Download failed: "; with
Sensor Files connected the transfer ran, the slot got negative values and each
chunk sent an OverflowError to sys.excepthook (~400 a second), which flooded the
diagnostic log and froze the application (Windows closed it as not responding).
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from crfs_iq_recorder import sftp_window
from crfs_iq_recorder.sftp_client import SftpError

BIG = 2_434_511_424  # iq_20261106_101303_0001.wav: 125 MS/s, 4.869 s


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class _FakeBrowser:
    """Calls `progress` like paramiko does (every chunk), past 2 GiB, then writes the file."""

    def __init__(self, total: int, steps: int = 400) -> None:
        self.total = total
        self.steps = steps

    def download(self, remote, local, progress=None, *, cancel=None, expected_size=0):
        try:
            for i in range(1, self.steps + 1):
                if progress:
                    progress(self.total * i // self.steps, self.total)
        except SftpError:
            raise
        except Exception as exc:  # mirrors SftpBrowser.download
            raise SftpError(f"Download failed: {exc}") from exc
        Path(local).write_bytes(b"done")

    def close(self):
        pass

    def abort(self):
        pass


def _state():
    return type("State", (), {"demo": False, "host": "sensor"})()


def _worker(monkeypatch, tmp_path, total):
    monkeypatch.setattr(sftp_window, "_open_browser", lambda state, cancel=None: _FakeBrowser(total))
    return sftp_window.FileDownloadWorker(_state(), "/mnt/1/remdata/x.bin", str(tmp_path / "x.bin"), "x.bin")


def _hooks(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(sys, "excepthook", lambda t, v, tb: calls.append(t.__name__))
    return calls


def test_runner_download_of_a_file_over_2_gib_finishes(qapp, monkeypatch, tmp_path):
    """Cellular runner and Recorder tab: nothing is connected to `progress`."""
    hooks = _hooks(monkeypatch)
    worker = _worker(monkeypatch, tmp_path, BIG)
    done: list[str] = []
    failed: list[str] = []
    worker.finished.connect(lambda name, path: done.append(path))
    worker.failed.connect(lambda name, text: failed.append(text))
    worker.run()
    assert failed == []
    assert done == [str(tmp_path / "x.bin")]
    assert hooks == []


def test_sensor_files_progress_over_2_gib_is_exact_and_throttled(qapp, monkeypatch, tmp_path):
    """Sensor Files: queued receiver on the GUI thread, worker in its own QThread."""
    from PySide6.QtCore import QObject, QThread, Qt, Slot

    hooks = _hooks(monkeypatch)
    worker = _worker(monkeypatch, tmp_path, BIG)

    class Sink(QObject):
        def __init__(self):
            super().__init__()
            self.got: list[tuple[int, int]] = []
            self.done = threading.Event()

        @Slot(str, object, object)
        def progress(self, name, done, total):
            self.got.append((done, total))

        @Slot(str, str)
        def finished(self, name, path):
            self.done.set()

    sink = Sink()
    queued = Qt.ConnectionType.QueuedConnection
    worker.progress.connect(sink.progress, queued)
    worker.finished.connect(sink.finished, queued)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not sink.done.is_set():
        qapp.processEvents()
        time.sleep(0.01)
    thread.quit()
    thread.wait(3000)
    qapp.processEvents()
    assert sink.done.is_set()
    assert hooks == []
    assert sink.got, "progress must reach the window"
    assert all(0 <= done <= total == BIG for done, total in sink.got)
    assert sink.got[-1] == (BIG, BIG)
    assert len(sink.got) < 50, "400 chunk callbacks within milliseconds must not become 400 GUI updates"


def test_a_failing_progress_display_never_ends_the_transfer(tmp_path):
    from crfs_iq_recorder.sftp_client import ParamikoSftpBrowser

    class Sftp:
        def get(self, remote, local, callback=None):
            for done in (32768, BIG // 2, BIG):
                callback(done, BIG)
            Path(local).write_bytes(b"x" * 10)

        def stat(self, path):
            return type("St", (), {"st_size": 10})()

    browser = ParamikoSftpBrowser.__new__(ParamikoSftpBrowser)
    browser._sftp = Sftp()
    calls: list[int] = []

    def progress(done, total):
        calls.append(done)
        raise OverflowError

    dest = tmp_path / "iq.wav"
    browser.download("/r/iq.wav", dest, progress=progress)
    assert dest.read_bytes() == b"x" * 10
    assert calls == [32768], "after the first failure the display is not called again"


def test_download_error_without_a_message_names_its_type(tmp_path):
    from crfs_iq_recorder.sftp_client import ParamikoSftpBrowser, exception_text

    class Sftp:
        def get(self, remote, local, callback=None):
            raise OverflowError

        def stat(self, path):
            return type("St", (), {"st_size": 10})()

    browser = ParamikoSftpBrowser.__new__(ParamikoSftpBrowser)
    browser._sftp = Sftp()
    with pytest.raises(SftpError) as info:
        browser.download("/r/iq.wav", tmp_path / "iq.wav")
    assert str(info.value) == "Download failed: OverflowError"
    assert not (tmp_path / "iq.wav.part").exists()
    assert exception_text(ValueError("bad value")) == "bad value"
    assert exception_text(OverflowError()) == "OverflowError"
