"""Captures and downloads share the sensor's storage (Node 100-18: a USB 2.0 stick).

Hardware, 6 Nov 2026, 2110-2170 MHz at 125 MS/s, 5 s: idle 4.869 s; requested 11 s after the previous
file was final, with 247 MB of it still unwritten, 4.634 s; with a download reading the stick 4.835 s.
So no download starts while a capture is in flight, the next request waits while a download is
transferring, and then until the sensor has written its data out (bounded).
"""

from __future__ import annotations

import pytest

import crfs_iq_recorder.cellular_runner as cr
from crfs_iq_recorder.cellular_runner import S_RUNNING, S_WAITING, T_SUBMITTING
from crfs_iq_recorder.cellular_scheduler import MODE_NOW, Schedule
from crfs_iq_recorder.sftp_client import DemoSftpBrowser, SftpBrowser, parse_writeback_bytes
from operator_rules import problems
from test_cellular_runner import FakeHost, _plan, _pump, _runner, _settings, qapp  # noqa: F401 - fixture

MB = 1024 * 1024


class _Thread:
    def quit(self):
        pass

    def deleteLater(self):
        pass


class _Worker:
    def __init__(self, name):
        self._name = name

    def abort(self):
        pass

    def deleteLater(self):
        pass


class _Browser:
    def __init__(self, readings):
        self.readings = list(readings)
        self.closed = False

    def pending_writeback_bytes(self):
        return self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]

    def close(self):
        self.closed = True


def _waiting_runner(app, tmp_path, **settings):
    """A runner whose first capture is confirmed and whose wait between recordings is over."""
    host = FakeHost()
    runner = _runner(tmp_path, host)
    logs: list[str] = []
    runner.log.connect(lambda message, _level: logs.append(message))
    assert runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(wait_s=60.0, **settings)) is None
    host.finish(host.requests[0])
    _pump(app)
    assert runner.state == S_WAITING
    runner._wait_deadline = 0.0
    return host, runner, logs


def test_meminfo_dirty_and_writeback_are_added_in_bytes():
    text = "MemTotal:        3932160 kB\nDirty:            247000 kB\nWriteback:          1024 kB\n"
    assert parse_writeback_bytes(text) == (247000 + 1024) * 1024
    assert parse_writeback_bytes("MemTotal: 1 kB\n") is None
    assert parse_writeback_bytes("") is None
    assert SftpBrowser().pending_writeback_bytes() is None, "a connection that cannot tell says so"
    assert DemoSftpBrowser().pending_writeback_bytes() == 0


def test_no_download_starts_while_a_capture_is_in_flight(qapp, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cr, "FINALIZE_SETTLE_S", 0.01)

    def unlisted(*_a, **_k):
        raise OSError("not listed in this test")

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", unlisted)
    host = FakeHost()
    runner = _runner(tmp_path, host)
    started: list[tuple[str, bool]] = []
    runner._start_download = lambda task_key, round_id, item: started.append((item["name"], runner._capture_in_flight))
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, wait_s=0.0))
    host.finish(host.requests[0])
    _pump(qapp, 0.3)
    runner._tick()
    assert len(host.requests) == 2 and runner.current_tasks()[1].status == T_SUBMITTING
    assert started == [("file_000001_0001.wav", False)], "the first file's download started before the second capture"
    for index in (1, 2):
        host.finish(host.requests[index])
        _pump(qapp, 0.3)
        runner._tick()
    runner._drain_downloads()
    assert sorted(name for name, _ in started) == ["file_000001_0001.wav", "file_000002_0001.wav", "file_000003_0001.wav"]
    assert not any(in_flight for _, in_flight in started), "every download started between captures"


def test_the_last_captures_download_goes_before_the_next_recording(qapp, tmp_path, monkeypatch):  # noqa: F811
    # Hardware, 6 Nov: the files were listed again 0.3 s after the 10 s wait had ended; the storage
    # check had begun and that download waited until after the next capture.
    monkeypatch.setattr(cr, "FINALIZE_SETTLE_S", 0.2)

    def unlisted(*_a, **_k):
        raise OSError("not listed in this test")

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", unlisted)
    host = FakeHost()
    runner = _runner(tmp_path, host)
    started: list[tuple[str, int]] = []

    def start(task_key, round_id, item):
        started.append((item["name"], len(host.requests)))
        runner._downloads_active.append((_Thread(), _Worker(item["name"]), task_key, round_id))

    runner._start_download = start
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, wait_s=0.0))
    host.finish(host.requests[0])
    _pump(qapp, 0.05)
    runner._tick()
    assert len(host.requests) == 1 and started == []
    assert runner.status()["text"].startswith("Next recording after the downloads that are running"), runner.status()["text"]
    _pump(qapp, 0.5)
    assert started == [("file_000001_0001.wav", 1)], "listed, then downloaded, before the next request"
    assert len(host.requests) == 1, "the next request waits for that download"
    runner._downloads_active.clear()
    runner._tick()
    assert len(host.requests) == 2


def test_the_next_recording_waits_while_a_download_is_transferring(qapp, tmp_path):  # noqa: F811
    host, runner, logs = _waiting_runner(qapp, tmp_path)
    # Hardware, 6 Nov: a download started 0.1 s before the request was due and connected 0.5 s later;
    # a started download counts from its start, not from its connection.
    runner._downloads_active.append((_Thread(), _Worker("a.wav"), "k", "r"))
    runner._downloads_active.append((_Thread(), _Worker("b.wav"), "k", "r"))
    runner._on_download_waiting("b.wav", 5.0, "refused")  # the sensor refuses b's connection for now
    runner._tick()
    runner._tick()
    assert len(host.requests) == 1 and runner.state == S_RUNNING
    wait_lines = [m for m in logs if m.startswith("The next recording waits")]
    assert wait_lines == ["The next recording waits for 1 file to finish downloading (they share the sensor's storage)."]
    assert runner.status()["text"].startswith("Next recording after the downloads that are running"), runner.status()["text"]
    for line in wait_lines + [runner.status()["text"]]:
        assert problems(line) == [], (line, problems(line))
    # A download still waiting for a connection reads nothing: it does not hold the recording back.
    runner._downloads_active.pop(0)
    runner._tick()
    assert len(host.requests) == 2, "the request goes once no download is reading"
    # Once it connects it reads, and the next request would wait for it.
    runner._on_download_connected("b.wav")
    assert runner._downloads_reading() == 1
    runner._downloads_active.clear()


def test_downloads_in_the_sensor_files_window_hold_the_next_recording_back(qapp, tmp_path):  # noqa: F811
    host, runner, logs = _waiting_runner(qapp, tmp_path)

    class Window:
        running = 2

        def downloads_running(self):
            return self.running

    host._sftp_win = Window()
    runner._tick()
    assert len(host.requests) == 1
    assert "The next recording waits for 2 files to finish downloading (they share the sensor's storage)." in logs
    host._sftp_win.running = 0
    runner._tick()
    assert len(host.requests) == 2

    class Closed:
        def downloads_running(self):
            raise RuntimeError("Internal C++ object already deleted.")

    host._sftp_win = Closed()
    assert runner._downloads_reading() == 0


def test_the_next_recording_waits_until_the_sensor_has_written_its_data_out(qapp, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cr, "STORAGE_SETTLE_POLL_S", 0.01)
    browser = _Browser([370 * MB, 120 * MB, 8 * MB])
    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", lambda *_a, **_k: browser)
    host, runner, logs = _waiting_runner(qapp, tmp_path)
    host._conn.demo = False
    runner._tick()
    assert browser.readings == [8 * MB] and browser.closed, "polled over one connection, then closed"
    assert len(host.requests) == 2
    line = next(m for m in logs if m.startswith("Waited "))
    assert line == "Waited 1 s for the sensor to finish writing the previous recording to its storage."
    assert problems(line) == []
    # Each request needs its own check: the flag is spent when the capture is sent.
    assert runner._storage_settled is False


def test_the_storage_wait_is_bounded_and_says_the_recording_starts_anyway(qapp, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cr, "STORAGE_SETTLE_POLL_S", 0.01)
    monkeypatch.setattr(cr, "STORAGE_SETTLE_MAX_S", 0.05)
    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", lambda *_a, **_k: _Browser([400 * MB]))
    host, runner, logs = _waiting_runner(qapp, tmp_path)
    host._conn.demo = False
    runner._tick()
    assert len(host.requests) == 2, "a slow storage never stops the collection"
    line = next(m for m in logs if m.startswith("The sensor was still writing"))
    assert line == "The sensor was still writing to its storage after 1 s. The next recording starts anyway."
    assert problems(line) == []


@pytest.mark.parametrize("failure", ["unreadable", "no connection"])
def test_when_the_storage_cannot_be_read_the_request_goes_ahead_quietly(qapp, tmp_path, monkeypatch, failure):  # noqa: F811
    if failure == "unreadable":
        monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", lambda *_a, **_k: _Browser([None]))
    else:
        def refused(*_a, **_k):
            raise OSError("refused")

        monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", refused)
    host, runner, logs = _waiting_runner(qapp, tmp_path)
    host._conn.demo = False
    runner._tick()
    assert len(host.requests) == 2
    assert not [m for m in logs if "storage" in m], logs


def test_a_storage_wait_cut_short_by_closing_sends_nothing(qapp, tmp_path, monkeypatch):  # noqa: F811
    jobs = []
    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", lambda *_a, **_k: _Browser([8 * MB]))
    host, runner, _logs = _waiting_runner(qapp, tmp_path)
    host._conn.demo = False
    host._start_job = lambda work, apply: jobs.append((work, apply))
    runner._tick()
    assert len(jobs) == 1 and runner._storage_settle_pending
    assert runner.status()["text"].startswith("Waiting for the sensor to finish writing to its storage")
    assert runner._drain_downloads() is None and not runner._downloads_active, "downloads wait for the check too"
    runner.shutdown()
    work, apply = jobs[0]
    apply(work())
    assert len(host.requests) == 1
