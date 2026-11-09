"""The sensor clock check in the main window: when it runs, what it holds, what it tells. Offscreen Qt, no network."""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from crfs_iq_recorder import ntp_recovery as nr
from crfs_iq_recorder import sftp_paths
from crfs_iq_recorder.sensor_clock import PcClock

from test_ntp_recovery import EXACT_BODY, NTP_PAGE, ORIGINAL, SERVERS, TEMP_BODY, TEMPORARY

TRUSTED = PcClock(True, "SNTP test", 0.02, "test")


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    from crfs_iq_recorder.gui import configure_appearance

    app = QApplication.instance() or QApplication([])
    configure_appearance(app)
    yield app


@pytest.fixture(autouse=True)
def isolated_files(tmp_path, monkeypatch):
    monkeypatch.setattr("crfs_iq_recorder.recording_history.recordings_path", lambda: tmp_path / "recordings.json")
    monkeypatch.setattr("crfs_iq_recorder.paths.settings_path", lambda: tmp_path / "settings.json")


@pytest.fixture(autouse=True)
def fast_clock_checks(monkeypatch):
    from crfs_iq_recorder import clock_monitor

    monkeypatch.setattr(nr, "SYNC_POLL_S", 0.02)
    monkeypatch.setattr(nr, "SYNC_TIMEOUT_S", 1.0)
    monkeypatch.setattr(nr, "RESTORE_RETRY_DELAYS_S", (0.01, 0.01, 0.01))
    monkeypatch.setattr(clock_monitor, "PENDING_POLL_MS", 30)
    sftp_paths.set_sensor_clock_offset(None)
    yield
    sftp_paths.set_sensor_clock_offset(None)


class RealTime:
    @property
    def t(self) -> float:
        return time.time()


class Node:
    """A Node whose clock is off until a Save changes its server list (then right after ``fixes_after_s``).
    ``gate`` holds every reply, ``put_gate`` the first Save; ``puts`` are (status, error, applied) per Save."""

    origin = "http://192.0.2.33"

    def __init__(self, offset_s: float, *, fixes_after_s: float | None = 0.03, page=None, gate: threading.Event | None = None,
                 put_gate: threading.Event | None = None, puts=()) -> None:
        self.offset_s = offset_s
        self.fixes_after_s = fixes_after_s
        self.page = json.loads(json.dumps(page or NTP_PAGE))
        self.gate = gate
        self.put_gate = put_gate
        self.puts = list(puts)
        self.fixed_at: float | None = None
        self.calls: list[tuple] = []
        self.closed = 0

    def servers(self):
        return self.page["values"].get(SERVERS)

    def get(self, path: str) -> nr.WebReply:
        if self.gate is not None:
            self.gate.wait(10)
        self.calls.append(("GET", path))
        if path == nr.NTP_PAGE_PATH:
            return nr.WebReply(200, {}, json.dumps(self.page))
        offset = 0.2 if self.fixed_at is not None and time.time() >= self.fixed_at else self.offset_s
        stamp = datetime.fromtimestamp(time.time() + offset, tz=timezone.utc)
        source = "NTP" if abs(offset) < 5 else "No accurate source"
        return nr.WebReply(200, {}, json.dumps({"values": {"02-system-time": stamp.strftime("%Y-%m-%d %H:%M:%S.%f+0000"), "03-time-source": source}}))

    def put_json(self, path: str, body: bytes, *, page: str) -> nr.WebReply:
        self.calls.append(("PUT", path, body))
        if self.put_gate is not None and len(self.saves()) == 1:
            self.put_gate.wait(10)
        status, error, applied = self.puts.pop(0) if self.puts else (200, "", True)
        sent = json.loads(body)[SERVERS]
        if applied and sent != self.servers():
            self.page["values"][SERVERS] = sent
            stepped = self.fixed_at is not None and time.time() >= self.fixed_at
            if self.fixes_after_s is not None and not stepped:
                self.fixed_at = time.time() + self.fixes_after_s
        return nr.WebReply(error=error) if error else nr.WebReply(status, {}, "")

    def close(self) -> None:
        self.closed += 1

    def saves(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "PUT"]


def _pump_until(qapp, predicate, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _window(qapp, tmp_path, node, *, pc=TRUSTED):
    from crfs_iq_recorder.clock_monitor import SensorClockMonitor
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    _pump_until(qapp, lambda: False, timeout=0.3)  # startup reads finish first
    window._link_timer.stop()
    window._link_token += 1
    window._clock_monitor.shutdown()
    monitor = SensorClockMonitor(window, web_factory=lambda: node, store=nr.RecoveryStore(tmp_path / "recovery.json"), pc_check=lambda _s: pc)
    window._clock_monitor = monitor
    discovery = {"retry_today": 0, "storage": 0}
    window._sftp_win = SimpleNamespace(retry_today=lambda: discovery.__setitem__("retry_today", discovery["retry_today"] + 1), close=lambda: None,
                                       set_connection_state=lambda _c: None)
    window._refresh_storage = lambda: discovery.__setitem__("storage", discovery["storage"] + 1)  # type: ignore[method-assign]
    return window, monitor, discovery


def _close(window, qapp) -> None:
    window._sftp_win = None
    window.close()
    qapp.processEvents()


def test_a_wrong_clock_is_corrected_and_the_recording_folders_are_looked_up_again(qapp, tmp_path):
    node = Node(-600.0)
    window, monitor, discovery = _window(qapp, tmp_path, node)
    try:
        monitor.request_check("sensor connected", force=True)
        assert _pump_until(qapp, lambda: monitor.last_result is not None)
        assert monitor.last_result.outcome == nr.SYNCED, monitor.last_result.to_dict()
        assert [c[2] for c in node.saves()] == [TEMP_BODY, EXACT_BODY] and node.servers() == ORIGINAL
        log = window.log_edit.toPlainText()
        assert "Sensor clock incorrect - refreshing NTP" in log and "Sensor clock synchronized." in log
        assert log.index("refreshing NTP") < log.index("synchronized")
        assert discovery["retry_today"] >= 1 and discovery["storage"] >= 1
        assert sftp_paths.sensor_clock_offset() is None
        assert node.closed == 1 and not monitor.running
    finally:
        _close(window, qapp)


def test_no_check_while_recording_or_finalizing_then_one_before_the_next_capture(qapp, tmp_path):
    node = Node(-600.0)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        window._recording_active = True
        monitor.request_check("recording reply", force=True)
        _pump_until(qapp, lambda: False, timeout=0.2)
        assert node.calls == [] and not monitor.running
        window._recording_active = False
        window._watching = True  # finalizing: the files are being confirmed
        _pump_until(qapp, lambda: False, timeout=0.2)
        assert node.calls == []
        window._watching = False
        assert _pump_until(qapp, lambda: monitor.last_result is not None)
        assert monitor.last_result.outcome == nr.SYNCED and len(node.saves()) == 2
    finally:
        _close(window, qapp)


def test_a_capture_waits_for_a_running_check_and_a_manual_start_resumes(qapp, tmp_path):
    from crfs_iq_recorder.clock_monitor import HOLD_TEXT

    gate = threading.Event()
    node = Node(-600.0, gate=gate)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        monitor.request_check("sensor connected", force=True)
        assert _pump_until(qapp, lambda: monitor.running)
        # The collection runner reads "in progress" as busy and tries again on its next tick.
        assert window.start_capture(SimpleNamespace(owner="Cellular Collection")) == HOLD_TEXT
        assert "in progress" in HOLD_TEXT.casefold()
        window._on_start()
        assert not window._submit_lock and not window._recording_active
        started: list[int] = []
        window._on_start = lambda: started.append(1)  # type: ignore[method-assign]
        gate.set()
        assert _pump_until(qapp, lambda: started == [1])
        assert monitor.last_result.outcome == nr.SYNCED
        assert window.start_capture(SimpleNamespace(owner="x", seconds="bad")) != HOLD_TEXT
    finally:
        gate.set()
        _close(window, qapp)


def test_a_stopped_series_is_not_resumed(qapp, tmp_path):
    gate = threading.Event()
    node = Node(0.1, gate=gate)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        monitor.request_check("sensor connected", force=True)
        assert _pump_until(qapp, lambda: monitor.running)
        window._series_active = True
        window._on_start()
        window._series_active = False
        started: list[int] = []
        window._on_start = lambda: started.append(1)  # type: ignore[method-assign]
        gate.set()
        assert _pump_until(qapp, lambda: monitor.last_result is not None)
        _pump_until(qapp, lambda: False, timeout=0.1)
        assert started == []
    finally:
        gate.set()
        _close(window, qapp)


def test_a_clock_that_stays_wrong_points_sensor_files_at_the_sensor_date(qapp, tmp_path):
    node = Node(-14 * 365 * 86400.0, fixes_after_s=None)
    window, monitor, discovery = _window(qapp, tmp_path, node)
    try:
        monitor.request_check("sensor connected", force=True)
        assert _pump_until(qapp, lambda: monitor.last_result is not None, timeout=8)
        assert monitor.last_result.outcome == nr.STILL_WRONG
        log = window.log_edit.toPlainText()
        assert "Sensor clock still wrong after NTP refresh (reads 20" in log and "synchronized" not in log
        offset = sftp_paths.sensor_clock_offset()
        assert offset is not None and offset < 0
        first = sftp_paths.candidate_today_directories()[0]
        assert first == f"/mnt/1/remdata/{datetime.fromtimestamp(time.time() + offset, tz=timezone.utc):%Y%m%d}/"
        assert discovery["retry_today"] >= 1
        # Within the retry gap a second check saves nothing and says when the next refresh may run.
        monitor.request_check("again", force=True)
        assert _pump_until(qapp, lambda: monitor.last_result.outcome == nr.COOLDOWN)
        assert len(node.saves()) == 2 and "Next NTP refresh after" in window.log_edit.toPlainText()
        assert node.servers() == ORIGINAL
    finally:
        _close(window, qapp)


def test_a_server_list_that_is_not_restored_is_reported_and_never_called_synchronized(qapp, tmp_path):
    node = Node(-600.0, puts=[(200, "", True)] + [(200, "", False)] * 4)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        monitor.request_check("sensor connected", force=True)
        assert _pump_until(qapp, lambda: monitor.last_result is not None)
        assert monitor.last_result.outcome == nr.RESTORE_FAILED and len(node.saves()) == 5
        log = window.log_edit.toPlainText()
        assert "Sensor NTP server list not restored." in log and "synchronized" not in log  # "See details." is the window's
        assert monitor.last_result.detail == f"Put back the preferred servers {json.dumps(ORIGINAL)} on the sensor's NTP page."
    finally:
        _close(window, qapp)


def test_closing_during_a_refresh_tries_the_restore_once_and_the_next_start_finishes_it(qapp, tmp_path):
    from crfs_iq_recorder import clock_monitor
    from crfs_iq_recorder.clock_monitor import SensorClockMonitor

    put_gate = threading.Event()
    node = Node(-600.0, put_gate=put_gate, puts=[(200, "", True), (500, "", False)])
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        monitor.request_check("sensor connected", force=True)
        assert _pump_until(qapp, lambda: len(node.saves()) == 1)  # the temporary list is on its way
        threading.Timer(0.2, put_gate.set).start()
        started = time.monotonic()
        monitor.shutdown()
        assert time.monotonic() - started < clock_monitor.RESTORE_ON_SHUTDOWN_S
        assert [c[2] for c in node.saves()] == [TEMP_BODY, EXACT_BODY]  # one restore, no retries after the stop
        store = nr.RecoveryStore(tmp_path / "recovery.json")
        assert node.servers() == TEMPORARY and store.pending_restore(monitor._key())["original"] == ORIGINAL
        # The next start: the list goes back first, then the clock check carries on.
        again = SensorClockMonitor(window, web_factory=lambda: node, store=store, pc_check=lambda _s: TRUSTED)
        window._clock_monitor = again
        again.request_check("sensor connected", force=True)
        assert _pump_until(qapp, lambda: again.last_result is not None)
        assert node.servers() == ORIGINAL and store.pending_restore(again._key()) == {}
        assert "Sensor NTP server list restored." in window.log_edit.toPlainText()
    finally:
        put_gate.set()
        _close(window, qapp)


def test_link_checks_that_time_out_while_the_sensor_saves_its_ntp_page_are_not_a_disconnect(qapp, tmp_path, monkeypatch):
    """9 Nov 2026 hardware: each Save of the NTP page took 6.6-9.4 s and the Node answered nothing else
    meanwhile, longer than the 4 s link check."""
    from crfs_iq_recorder import clock_monitor
    from crfs_iq_recorder.connection_monitor import CONNECTED, RECONNECTING

    monkeypatch.setattr(clock_monitor, "SAVE_QUIET_S", 1.0)
    put_gate = threading.Event()
    node = Node(-600.0, put_gate=put_gate)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        window._link.state = CONNECTED
        monitor.request_check("sensor connected", force=True)
        assert _pump_until(qapp, lambda: len(node.saves()) == 1)
        for _ in range(3):
            window._apply_link_result(window._link_token, False, "HTTP read timeout")
        assert window._link.state == CONNECTED and window._link.failures == 0
        put_gate.set()
        assert _pump_until(qapp, lambda: monitor.last_result is not None)
        assert monitor.last_result.outcome == nr.SYNCED
        window._apply_link_result(window._link_token, False, "HTTP read timeout")  # sent during the last Save
        assert window._link.state == CONNECTED
        assert _pump_until(qapp, lambda: not monitor.saving_ntp_page(), timeout=3)
        window._apply_link_result(window._link_token, False, "HTTP read timeout")
        assert window._link.state == RECONNECTING
    finally:
        put_gate.set()
        _close(window, qapp)


def test_ntp_off_is_told_once_and_nothing_is_saved(qapp, tmp_path):
    page = {**NTP_PAGE, "values": {**NTP_PAGE["values"], "02-preferred-servers": []}}
    node = Node(-600.0, page=page)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        for _ in range(2):
            monitor.last_result = None
            monitor.request_check("check", force=True)
            assert _pump_until(qapp, lambda: monitor.last_result is not None)
        assert window.log_edit.toPlainText().count("Sensor clock incorrect - NTP is off on the sensor.") == 1
        assert node.saves() == []
    finally:
        _close(window, qapp)


def test_an_unverified_computer_clock_changes_nothing(qapp, tmp_path):
    node = Node(-600.0)
    window, monitor, _discovery = _window(qapp, tmp_path, node, pc=PcClock(False, "Windows time service", detail="Local CMOS Clock"))
    try:
        monitor.request_check("check", force=True)
        assert _pump_until(qapp, lambda: monitor.last_result is not None)
        assert monitor.last_result.outcome == nr.PC_UNTRUSTED and node.saves() == []
        assert "this computer's clock is not verified" in window.log_edit.toPlainText()
    finally:
        _close(window, qapp)


def test_connection_checks_trigger_at_most_one_routine_check_per_interval(qapp, tmp_path):
    node = Node(0.1)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        monitor.request_check("connection check")
        assert _pump_until(qapp, lambda: monitor.last_result is not None)
        reads = len(node.calls)
        monitor.request_check("connection check")
        window._apply_link_result(window._link_token, True, "ok")
        _pump_until(qapp, lambda: False, timeout=0.2)
        assert len(node.calls) == reads == 1 and monitor.last_result.outcome == nr.OK
        assert "Sensor clock" not in window.log_edit.toPlainText()  # a right clock is not news
    finally:
        _close(window, qapp)


def test_a_check_that_hangs_releases_the_captures(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder import clock_monitor

    monkeypatch.setattr(clock_monitor, "HOLD_MAX_S", 0.2)
    gate = threading.Event()
    node = Node(-600.0, gate=gate)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        monitor.request_check("check", force=True)
        assert _pump_until(qapp, lambda: monitor.running)
        assert _pump_until(qapp, lambda: not monitor.running, timeout=3)
        assert window.start_capture(SimpleNamespace(owner="x", seconds="bad")) != clock_monitor.HOLD_TEXT
        assert "Sensor clock check did not finish." in window.log_edit.toPlainText()
    finally:
        gate.set()
        _close(window, qapp)


def test_the_demo_sensor_is_never_checked(qapp):
    from crfs_iq_recorder.gui import MainWindow

    window = MainWindow(demo=True)
    try:
        jobs = len(window._jobs)
        window._clock_monitor.request_check("sensor connected", force=True)
        window._clock_monitor.note_sensor_reply({"file_dir": "/mnt/1/remdata/20120102/"})
        _pump_until(qapp, lambda: False, timeout=0.2)
        assert not window._clock_monitor.running and window._clock_monitor.last_result is None
        assert len(window._jobs) <= jobs
    finally:
        window.close()
        qapp.processEvents()


def test_the_pre_collection_check_waits_for_the_clock_check(qapp, tmp_path):
    from crfs_iq_recorder.cellular_preflight import recovery_note

    node = Node(-600.0)
    window, monitor, _discovery = _window(qapp, tmp_path, node)
    try:
        done = threading.Event()
        threading.Thread(target=lambda: (monitor.wait_for_check("collection pre-flight"), done.set()), daemon=True).start()
        assert _pump_until(qapp, done.is_set)
        assert monitor.last_result is not None and monitor.last_result.outcome == nr.SYNCED
        assert recovery_note(monitor) == "Corrected by an NTP refresh."
    finally:
        _close(window, qapp)


def test_files_in_the_folder_named_by_a_wrong_sensor_clock_are_found(qapp, tmp_path, monkeypatch):
    """9 Nov 2026 11:29: the sensor's clock read 2012-01-02, its reply said file_dir /mnt/1/remdata/20120102/,
    and the watch only looked in 20261109: the recording ended "not confirmed" while its file was on the disk."""
    from crfs_iq_recorder import gui as gui_module
    from crfs_iq_recorder.api_client import HttpOutcome, SseEvent
    from crfs_iq_recorder.demo_sftp import shared_demo_browser
    from crfs_iq_recorder.file_watch import folders_for_recording
    from crfs_iq_recorder.gui import MainWindow
    from crfs_iq_recorder.recording_history import load_recordings
    from crfs_iq_recorder.recording_status import CONFIRMED

    wrong_dir = "/mnt/1/remdata/20120102/"
    name = "iq_20120102_112117_0001.wav"

    class Client:
        def __init__(self):
            self.cancel = None

        def start_recording(self, _target, _password, _payload, *, cancel=None, on_event=None, stop_on_finished=True):
            self.cancel = cancel
            for event in (
                SseEvent("crfs_iq_recorder.http_accepted", {"status": 200}, 0.1),
                SseEvent("time.RemoteTimeData", [{"first_filename": name, "file_dir": wrong_dir, "task_id": "iq-test",
                                                  "timestamp": "2012-01-02T11:21:17.0010006"}], 0.1),
                SseEvent("message.MessageData", [{"level": "INFO", "message": "Remote recording is now finished"}], 0.1),
            ):
                on_event(event)
                time.sleep(0.05)
            if cancel is not None:
                cancel.wait(10)
            return HttpOutcome(kind="accepted", title="Sensor reported the recording finished", detail="scripted", request_sent=True,
                               accepted=True, stream_ended="cancelled", sensor_finished=True, sensor_parts=[name], sensor_file_dir=wrong_dir)

        def abort(self):
            if self.cancel is not None:
                self.cancel.set()

        def close(self):
            self.abort()

    monkeypatch.setattr(gui_module, "_open_browser", lambda *_a, **_k: shared_demo_browser())
    window = MainWindow(demo=True)
    window._settle_s = 0
    window._stable_needed = 2
    client = Client()
    monkeypatch.setattr(window, "_client", lambda: client)
    try:
        window.mode_start.setChecked(True)
        window.field_a.setText("790")
        window.field_b.setText("810")
        window.time_edit.setText("0.05")
        window.wait_spin.setValue(0)
        window.repeat_n.setChecked(True)
        window.repeat_spin.setValue(1)
        window._on_start()
        assert _pump_until(qapp, lambda: window._sensor_finished, timeout=5)
        record = load_recordings(tmp_path / "recordings.json")[0]
        assert wrong_dir not in folders_for_recording(record.started_at)  # what the watch looked at before
        shared_demo_browser().publish_capture(
            "iq_20120102_112117", directory=wrong_dir, when=datetime(2012, 1, 2, 11, 21, 17), seconds=float(record.duration_s),
            center_hz=float(record.center_hz), bandwidth_hz=float(record.bandwidth_hz),
        )
        window._record_timer.stop()
        window._conn.demo = False  # no demo files of its own: only the sensor's file exists
        window._on_recording_time_elapsed()
        window._conn.demo = True
        assert _pump_until(qapp, lambda: not window._watch_in_flight, timeout=5)
        for _ in range(4):
            window._poll_collection_files()
            qapp.processEvents()
        assert _pump_until(qapp, lambda: window._record_phase == CONFIRMED, timeout=5), (window._record_phase, window.log_edit.toPlainText())
        assert window._sensor_dirs[0] == wrong_dir
        assert "Recording confirmed" in window.log_edit.toPlainText()
    finally:
        window._watch_timer.stop()
        window._record_timer.stop()
        window.close()
        qapp.processEvents()
