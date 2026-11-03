"""Cellular runner: sequencing, waits after confirmed completion, pause/stop, persistence, recovery.

Uses a scripted host (no sensor) and, for the integration test, the demo
MainWindow with offscreen Qt.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from openpyxl import Workbook
from PySide6.QtCore import QObject, Signal

from crfs_iq_recorder.capture_api import (
    OUTCOME_CONFIRMED,
    OUTCOME_NOT_SENT,
    OUTCOME_SENSOR_ERROR,
    OUTCOME_UNKNOWN,
    CaptureRequest,
    CaptureSummary,
)
from crfs_iq_recorder.cellular_capabilities import capability_for
from crfs_iq_recorder.cellular_plan import build_plan
from crfs_iq_recorder.cellular_protocol import import_protocol
from crfs_iq_recorder.cellular_runner import (
    OWNER,
    S_DONE,
    S_INTERRUPTED,
    S_PAUSED,
    S_PAUSED_BACKLOG,
    S_RUNNING,
    S_WAITING,
    S_WAITING_SCHEDULE,
    T_CONFIRMED,
    T_NOT_SENT,
    T_QUEUED,
    T_SENSOR_ERROR,
    T_SKIPPED,
    T_SUBMITTING,
    T_UNKNOWN,
    CampaignSettings,
    CellularRunner,
    load_session,
    session_path,
)
from crfs_iq_recorder.cellular_scheduler import (
    MODE_NOW,
    MODE_TIMES,
    ROUND_DELAYED,
    ROUND_DONE,
    ROUND_MISSED,
    ROUND_PARTIAL,
    ROUND_RUNNING,
    ROUND_STOPPED,
    TZ_UTC,
    Schedule,
    ScheduleTimeZone,
)

MHZ = 1_000_000


@pytest.fixture
def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


class FakeStore:
    def __init__(self):
        self.logged = set()

    def pending_count(self):
        return 0

    def is_logged(self, sensor_id, remote_path):
        return (sensor_id, remote_path) in self.logged


class FakeHost(QObject):
    """Stands in for MainWindow: records requests and lets the test finish captures."""

    capture_finished = Signal(object)

    def __init__(self):
        super().__init__()
        self.requests: list[CaptureRequest] = []
        self.owner = ""
        self.refuse: str | None = None
        self._excel_store = FakeStore()
        self._conn = type("Conn", (), {"host": "sensor", "excel_enabled": False, "excel_workbook": "", "demo": True})()
        self._sensor_info = type("Info", (), {"model": "R40-8", "serial": "SN1", "firmware": "2.25"})()
        self.log_lines = []

    def set_external_owner(self, name):
        if self.owner and self.owner != name:
            return f"{self.owner} is using the sensor."
        self.owner = name
        return None

    def release_external_owner(self, name=None):
        self.owner = ""

    def start_capture(self, request):
        if self.refuse:
            return self.refuse
        self.requests.append(request)
        return None

    def finish(self, request, outcome=OUTCOME_CONFIRMED, *, parts=1, held=None, detail="", names=None):
        names = names or [f"file_{len(self.requests):06d}_{i + 1:04d}.wav" for i in range(parts)]
        paths = [f"/mnt/1/remdata/20261103/{name}" for name in names]
        summary = CaptureSummary(
            outcome=outcome,
            request=request,
            record_id=f"rec-{len(self.requests)}",
            requested_s=request.seconds,
            held_s=held if held is not None else (request.seconds if outcome == OUTCOME_CONFIRMED else None),
            parts=parts if outcome == OUTCOME_CONFIRMED else 0,
            total_bytes=1000 * parts,
            sensor_finished=outcome == OUTCOME_CONFIRMED,
            request_sent=outcome not in {OUTCOME_NOT_SENT},
            sensor_parts=list(names) if outcome == OUTCOME_CONFIRMED else [],
            finalized_paths=paths if outcome == OUTCOME_CONFIRMED else [],
            sizes={p: 1000 for p in paths} if outcome == OUTCOME_CONFIRMED else {},
            started_at=datetime.now(),
            finished_at=datetime.now(),
            stem=names[0].rsplit("_", 1)[0] if outcome == OUTCOME_CONFIRMED else "",
            detail=detail,
        )
        self.capture_finished.emit(summary)


def _plan(tmp_path, *, fallback=1.0, rows=None):
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    for row in rows or [
        ["Band", "Duplex", "Direction", "Start (MHz)", "Stop (MHz)"],
        ["700", "FDD", "UL", 703, 713],
        ["700", "FDD", "DL", 758, 768],
        ["1800", "FDD", "DL", 1805, 1825],
    ]:
        ws.append(row)
    path = tmp_path / "plan.xlsx"
    wb.save(path)
    imported = import_protocol(path, "Plan")
    plan = build_plan(imported, capability_for("R40-8"), fallback_duration_s=fallback)
    assert plan.ready, [i.message for i in plan.issues]
    return plan


def _runner(tmp_path, host, clock=None, **settings):
    cfg = tmp_path / "config"
    cfg.mkdir(exist_ok=True)
    downloads = tmp_path / "downloads"
    downloads.mkdir(exist_ok=True)
    return CellularRunner(host, config_dir=cfg, download_dir=lambda: downloads, clock=clock)


def _settings(**kwargs):
    base = dict(campaign="Camp", site="Site A", wait_s=0.0, auto_download=False, verify_checksums=False)
    base.update(kwargs)
    return CampaignSettings(**base)


def _pump(qapp, seconds=0.05):
    deadline = time.time() + seconds
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def test_run_now_sequence_follows_the_worksheet_waits_after_confirmation_and_finishes(qapp, tmp_path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    plan = _plan(tmp_path)
    assert runner.start_session(plan, Schedule(mode=MODE_NOW), _settings(wait_s=0.2)) is None
    assert host.owner == OWNER, "the runner reserves the sensor for the whole session"
    assert runner.state == S_RUNNING
    assert len(host.requests) == 1
    first = host.requests[0]
    assert first.owner == OWNER and first.excel is False
    # Worksheet order: the UL row comes first in this sheet.
    assert first.label.startswith("700 MHz UL 703–713 MHz"), first.label
    assert first.plan.start_hz == 703 * MHZ and first.plan.end_hz == 713 * MHZ
    assert first.collection_event.startswith("Camp / Site A / Camp_")
    state = runner.current_tasks()[0]
    assert state.status == T_SUBMITTING and state.attempts == 1
    assert runner.status()["text"] == "700 MHz UL - row 1 of 3 - recording"
    # Nothing else starts while the capture runs.
    runner._tick()
    assert len(host.requests) == 1
    host.finish(first, parts=2)
    _pump(qapp)
    assert runner.state == S_WAITING, "the wait starts only after the capture is confirmed"
    assert len(host.requests) == 1
    assert runner.current_tasks()[0].status == T_CONFIRMED
    assert [f["name"] for f in runner.current_tasks()[0].files] == ["file_000001_0001.wav", "file_000001_0002.wav"]
    manifest = Path(runner.current_tasks()[0].manifest_path)
    assert manifest.name == "file_000001.cellular.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["allocation"]["window_start_hz"] == 703 * MHZ
    assert data["target"]["direction"] == "UL"
    assert data["recording"]["outcome"] == "confirmed"
    assert data["identifiers"]["protocol_version"] == plan.protocol_version
    assert data["sensor"]["gnss_status"] == "unknown"
    status = runner.status()
    assert status["countdown_s"] is not None and status["task_done"] == 1 and status["tasks_total"] == 3
    assert status["text"].startswith("Next recording in ") and status["text"].endswith(" seconds")
    time.sleep(0.25)
    runner._tick()
    assert len(host.requests) == 2
    assert host.requests[1].label.startswith("700 MHz DL 758–768 MHz")
    host.finish(host.requests[1])
    _pump(qapp)
    time.sleep(0.25)
    runner._tick()
    assert host.requests[2].label.startswith("1800 MHz DL")
    host.finish(host.requests[2])
    _pump(qapp)
    assert runner.state in {S_WAITING_SCHEDULE, S_DONE}, "the round is over; the timer may already have closed the session"
    runner._tick()
    assert runner.state == S_DONE
    assert host.owner == "", "the sensor is released when the session ends"
    record = runner.session.rounds[0]
    assert record.status == ROUND_DONE and record.tasks_confirmed == 3 and record.finished_utc is not None
    saved = load_session(tmp_path / "config")
    assert saved is not None and saved.runner_state == S_DONE
    handover = list((tmp_path / "downloads" / "cellular_manifests").glob("handover_*.json"))
    assert len(handover) == 1
    summary = json.loads(handover[0].read_text(encoding="utf-8"))
    assert summary["counts"]["logical_captures_confirmed"] == 3 and summary["counts"]["files"] == 4
    assert (tmp_path / "downloads" / "cellular_manifests" / handover[0].name.replace(".json", ".md")).is_file()


def test_short_unknown_sensor_error_and_not_sent_outcomes(qapp, tmp_path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    plan = _plan(tmp_path)
    runner.start_session(plan, Schedule(mode=MODE_NOW), _settings())
    # 1: short capture is confirmed but marked short and logged.
    host.finish(host.requests[0], held=0.3)
    _pump(qapp)
    tasks = runner.current_tasks()
    assert tasks[0].status == T_CONFIRMED and tasks[0].short is True
    assert any("Short capture" in n for n in tasks[0].notes)
    # 2: uncertain outcome is never counted as collected.
    assert len(host.requests) == 2
    host.finish(host.requests[1], OUTCOME_UNKNOWN, detail="no file settled")
    _pump(qapp)
    assert tasks[1].status == T_UNKNOWN
    # 3: nothing reached the sensor -> one automatic retry, then not_sent.
    assert len(host.requests) == 3
    host.finish(host.requests[2], OUTCOME_NOT_SENT, detail="refused")
    _pump(qapp)
    assert len(host.requests) == 4, "retry sent at once (wait 0) because nothing reached the sensor"
    assert tasks[2].status == T_SUBMITTING and tasks[2].attempts == 2
    assert any("retried once" in n for n in tasks[2].notes)
    host.finish(host.requests[3], OUTCOME_NOT_SENT, detail="refused again")
    _pump(qapp)
    assert tasks[2].status == T_NOT_SENT and tasks[2].attempts == 2
    runner._tick()
    record = runner.session.rounds[0]
    assert record.status == ROUND_PARTIAL
    assert (record.tasks_confirmed, record.tasks_uncertain, record.tasks_failed) == (1, 1, 1)
    assert runner.state == S_DONE
    # A sensor error on an explicit retry ends as sensor_error.
    assert runner.retry_task(record.round_id, tasks[2].task_key) is None
    assert runner.state == S_RUNNING and len(host.requests) == 5
    host.finish(host.requests[4], OUTCOME_SENSOR_ERROR, detail="No suitable antenna")
    _pump(qapp)
    assert tasks[2].status == T_SENSOR_ERROR
    runner._tick()
    assert runner.state == S_DONE


def test_pause_after_current_resume_and_stop_let_the_capture_finish(qapp, tmp_path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(wait_s=0.0))
    runner.pause()
    assert runner.status()["text"].startswith("Pausing")
    host.finish(host.requests[0])
    _pump(qapp)
    assert runner.state == S_PAUSED and len(host.requests) == 1
    runner._tick()
    assert len(host.requests) == 1, "nothing starts while paused"
    runner.resume()
    assert len(host.requests) == 2
    runner.stop()
    assert runner.status()["text"].startswith("Stopping")
    runner._tick()
    assert len(host.requests) == 2
    host.finish(host.requests[1])
    _pump(qapp)
    assert runner.state == S_DONE
    record = runner.session.rounds[0]
    assert record.status == ROUND_STOPPED and record.tasks_confirmed == 2 and record.tasks_skipped == 1
    assert host.owner == ""


def test_stop_during_countdown_cancels_the_pending_capture(qapp, tmp_path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(wait_s=30.0))
    host.finish(host.requests[0])
    _pump(qapp)
    assert runner.state == S_WAITING
    runner.stop()
    assert runner.state == S_DONE and len(host.requests) == 1
    assert runner.session.rounds[0].status == ROUND_STOPPED


def test_pause_during_countdown_then_resume_restarts_the_wait(qapp, tmp_path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(wait_s=0.2))
    host.finish(host.requests[0])
    _pump(qapp)
    runner.pause()
    assert runner.state == S_PAUSED
    time.sleep(0.25)
    runner._tick()
    assert len(host.requests) == 1
    runner.resume()
    assert runner.state == S_WAITING and len(host.requests) == 1
    time.sleep(0.25)
    runner._tick()
    assert len(host.requests) == 2


def test_recorder_busy_is_retried_and_storage_refusal_pauses_with_reason(qapp, tmp_path):
    host = FakeHost()
    host.refuse = "The previous recording is still being confirmed from the sensor's files."
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings())
    assert host.requests == [] and runner.state == S_RUNNING
    runner._tick()
    assert host.requests == []
    host.refuse = None
    runner._tick()
    assert len(host.requests) == 1
    host.refuse = "Not enough IQ storage for this recording. Need about 1 GB free."
    host.finish(host.requests[0])
    _pump(qapp)
    assert runner.state == S_PAUSED_BACKLOG
    assert "storage" in runner.status()["pause_reason"].casefold()
    host.refuse = "Set the sensor IP in Settings."
    runner.resume()
    assert runner.state == S_PAUSED and "Settings" in runner.session.pause_reason
    host.refuse = None
    runner.resume()
    assert len(host.requests) == 2


def test_scheduled_rounds_delayed_and_missed_handling(qapp, tmp_path):
    host = FakeHost()
    base = datetime(2026, 11, 3, 7, 59, tzinfo=timezone.utc)
    clock = {"now": base}
    runner = _runner(tmp_path, host, clock=lambda: clock["now"])
    plan = _plan(tmp_path)
    schedule = Schedule(
        mode=MODE_TIMES,
        times=[datetime(2026, 1, 1, 8, 0).time(), datetime(2026, 1, 1, 8, 5).time(), datetime(2026, 1, 1, 12, 0).time()],
        dates=[base.date()],
        tz=ScheduleTimeZone(kind=TZ_UTC),
    )
    assert runner.start_session(plan, schedule, _settings()) is None
    assert runner.state == S_WAITING_SCHEDULE and host.requests == []
    assert [r.scheduled_utc.strftime("%H:%M") for r in runner.session.rounds] == ["08:00", "08:05", "12:00"]
    assert runner.status()["next_round_text"].endswith("08:00")
    assert runner.session.rounds[0].round_id.endswith("_0800Z")
    # 08:00 -> first round starts.
    clock["now"] = base + timedelta(minutes=1)
    runner._tick()
    assert runner.state == S_RUNNING and len(host.requests) == 1
    assert runner.session.rounds[0].status == ROUND_RUNNING
    # 08:06 -> second round is due while the first still runs: delayed, time kept.
    clock["now"] = base + timedelta(minutes=7)
    runner._tick()
    assert runner.session.rounds[1].status == ROUND_DELAYED
    assert runner.session.rounds[1].scheduled_utc == datetime(2026, 11, 3, 8, 5, tzinfo=timezone.utc)
    for request in list(host.requests):
        host.finish(request)
        _pump(qapp)
        runner._tick()
        if len(host.requests) > len(runner.current_tasks()) and runner.current_round() is not None:
            pass
    # Finish the remaining tasks of round 1.
    while runner.current_round() is not None and runner.current_round().round_id == runner.session.rounds[0].round_id:
        host.finish(host.requests[-1])
        _pump(qapp)
        runner._tick()
    assert runner.session.rounds[0].status == ROUND_DONE
    # The delayed round started right after, with its own tasks.
    assert runner.current_round() is not None and runner.current_round().round_id == runner.session.rounds[1].round_id
    assert runner.session.rounds[1].actual_start_utc > runner.session.rounds[1].scheduled_utc
    while runner.current_round() is not None:
        host.finish(host.requests[-1])
        _pump(qapp)
        runner._tick()
    assert runner.session.rounds[1].status == ROUND_DONE
    assert runner.state == S_WAITING_SCHEDULE
    # The PC "slept" through 12:00: the round is missed and never started on its own.
    clock["now"] = base + timedelta(hours=5)
    requests_before = len(host.requests)
    runner._tick()
    assert runner.session.rounds[2].status == ROUND_MISSED
    assert len(host.requests) == requests_before
    assert runner.status()["missed_rounds"] == [runner.session.rounds[2].round_id]
    assert runner.state == S_WAITING_SCHEDULE
    # The operator decides: skip it -> session complete.
    runner.skip_missed_round(runner.session.rounds[2].round_id)
    runner._tick()
    assert runner.state == S_DONE
    summary_rounds = {r.round_id: r.status for r in runner.session.rounds}
    assert list(summary_rounds.values()) == [ROUND_DONE, ROUND_DONE, "skipped"]


def test_run_missed_round_on_request(qapp, tmp_path):
    host = FakeHost()
    base = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
    clock = {"now": base}
    runner = _runner(tmp_path, host, clock=lambda: clock["now"])
    schedule = Schedule(mode=MODE_TIMES, times=[datetime(2026, 1, 1, 8, 0).time()], dates=[base.date()], tz=ScheduleTimeZone(kind=TZ_UTC))
    runner.start_session(_plan(tmp_path), schedule, _settings())
    clock["now"] = base + timedelta(hours=3)
    runner._tick()
    rid = runner.session.rounds[0].round_id
    assert runner.session.rounds[0].status == ROUND_MISSED and host.requests == []
    assert runner.run_missed_round(rid) is None
    assert len(host.requests) == 1 and runner.session.rounds[0].status == ROUND_RUNNING
    assert "operator's request" in runner.session.rounds[0].note


def _timed_session(tmp_path, host, clock, times):
    runner = _runner(tmp_path, host, clock=lambda: clock["now"])
    schedule = Schedule(
        mode=MODE_TIMES,
        times=[datetime(2026, 1, 1, h, m).time() for h, m in times],
        dates=[clock["now"].date()],
        tz=ScheduleTimeZone(kind=TZ_UTC),
    )
    assert runner.start_session(_plan(tmp_path), schedule, _settings()) is None
    assert runner.state == S_WAITING_SCHEDULE and host.requests == []
    return runner


def test_restart_after_a_scheduled_time_passed_lists_the_pass_as_missed_and_continue_never_runs_it(qapp, tmp_path):
    """1.12.0 regression (hardware, 3 Nov): the app was closed while waiting for 10:50 and reopened at
    10:53; the session came back as a bare "Interrupted" and Continue started the pass late on its own."""
    host = FakeHost()
    clock = {"now": datetime(2026, 11, 3, 8, 40, tzinfo=timezone.utc)}
    runner = _timed_session(tmp_path, host, clock, [(8, 50), (12, 0)])
    runner.shutdown()  # closed while waiting, nothing in flight
    clock["now"] = datetime(2026, 11, 3, 8, 53, tzinfo=timezone.utc)  # inside the 15-minute grace of a running app
    host2 = FakeHost()
    runner2 = _runner(tmp_path, host2, clock=lambda: clock["now"])
    first, second = runner2.session.rounds
    assert runner2.state == S_INTERRUPTED and host2.requests == []
    status = runner2.status()
    assert status["missed_rounds"] == [first.round_id] and second.status == "pending"
    assert "missed" in status["text"] and "check with the sensor" not in status["text"]
    assert runner2.uncertain_tasks() == []
    logged: list[str] = []
    runner2.log.connect(lambda text, level: logged.append(text))
    _pump(qapp)
    assert any("Missed pass" in line for line in logged), "the missed pass is logged once the panel listens"
    # Continue keeps the 12:00 pass and does not start the 08:50 one.
    assert runner2.resume_after_restart() is None
    assert host2.requests == [] and runner2.state == S_WAITING_SCHEDULE
    assert first.status == ROUND_MISSED and host2.owner == OWNER
    assert runner2.status()["next_round_text"].endswith("12:00")
    # The operator decides to run it now.
    assert runner2.run_missed_round(first.round_id) is None
    assert len(host2.requests) == 1 and first.status == ROUND_RUNNING


def test_missed_pass_can_be_run_or_skipped_straight_from_the_interrupted_session(qapp, tmp_path):
    host = FakeHost()
    clock = {"now": datetime(2026, 11, 3, 8, 40, tzinfo=timezone.utc)}
    runner = _timed_session(tmp_path, host, clock, [(8, 50), (9, 30)])
    runner.shutdown()
    clock["now"] = datetime(2026, 11, 3, 11, 0, tzinfo=timezone.utc)  # both times went by
    # Skip both: the session finishes without a recording and releases the sensor.
    host2 = FakeHost()
    runner2 = _runner(tmp_path, host2, clock=lambda: clock["now"])
    missed = runner2.status()["missed_rounds"]
    assert len(missed) == 2 and runner2.state == S_INTERRUPTED
    for rid in missed:
        runner2.skip_missed_round(rid)
    assert runner2.status()["missed_rounds"] == [] and "Continue" in runner2.status()["text"]
    assert runner2.resume_after_restart() is None
    assert runner2.state == S_DONE and host2.requests == [] and host2.owner == ""
    # Run the first one straight away (no Continue first): the sensor is reserved on the way.
    runner2.discard_session()
    runner3 = _timed_session(tmp_path, FakeHost(), {"now": datetime(2026, 11, 3, 8, 40, tzinfo=timezone.utc)}, [(8, 50)])
    runner3.shutdown()
    host4 = FakeHost()
    runner4 = _runner(tmp_path, host4, clock=lambda: clock["now"])
    rid = runner4.status()["missed_rounds"][0]
    assert runner4.run_missed_round(rid) is None
    assert host4.owner == OWNER and len(host4.requests) == 1 and runner4.state == S_RUNNING
    host4.finish(host4.requests[0])
    _pump(qapp)
    for _ in range(3):
        if host4.requests and runner4.current_round() is not None:
            host4.finish(host4.requests[-1])
            _pump(qapp)
            runner4._tick()
    assert runner4.state == S_DONE and runner4.session.rounds[0].status == ROUND_DONE


def test_a_missed_pass_is_not_run_while_an_interrupted_pass_still_needs_reconciliation(qapp, tmp_path):
    host = FakeHost()
    clock = {"now": datetime(2026, 11, 3, 7, 59, tzinfo=timezone.utc)}
    runner = _timed_session(tmp_path, host, clock, [(8, 0), (8, 5)])
    clock["now"] = datetime(2026, 11, 3, 8, 1, tzinfo=timezone.utc)
    runner._tick()
    assert runner.state == S_RUNNING and len(host.requests) == 1
    runner.shutdown()  # closed during the first capture
    clock["now"] = datetime(2026, 11, 3, 8, 30, tzinfo=timezone.utc)
    host2 = FakeHost()
    runner2 = _runner(tmp_path, host2, clock=lambda: clock["now"])
    assert runner2.state == S_INTERRUPTED and len(runner2.uncertain_tasks()) == 1
    assert runner2.status()["missed_rounds"] == [runner2.session.rounds[1].round_id]
    error = runner2.run_missed_round(runner2.session.rounds[1].round_id)
    assert error and "Check with the sensor" in error
    assert host2.requests == [] and runner2.state == S_INTERRUPTED


def test_restart_recovers_an_interrupted_session_without_resending(qapp, tmp_path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True))
    runner._drain_downloads = lambda: None  # the first capture's download never starts before the "crash"
    host.finish(host.requests[0])
    _pump(qapp)
    assert len(host.requests) == 2 and runner.current_tasks()[1].status == T_SUBMITTING
    runner.shutdown()  # application closed during the second capture
    saved = json.loads(session_path(tmp_path / "config").read_text(encoding="utf-8"))
    assert saved["runner_state"] == S_RUNNING
    assert "reconcile" in saved["pause_reason"].casefold()
    assert saved["tasks"][saved["current_round_id"]][0]["files"][0]["download_status"] == "pending"

    host2 = FakeHost()
    runner2 = _runner(tmp_path, host2)
    assert runner2.state == S_INTERRUPTED and runner2.interrupted()
    assert host2.requests == [], "nothing is resent blindly after a restart"
    uncertain = runner2.uncertain_tasks()
    assert len(uncertain) == 1 and uncertain[0][0].status == T_SUBMITTING
    assert runner2.resume_after_restart() is not None, "the uncertain capture blocks resumption"
    rid = runner2.session.current_round_id
    runner2.skip_task(rid, uncertain[0][0].task_key)
    assert runner2.uncertain_tasks() == []
    started: list[dict] = []
    runner2._start_download = lambda task_key, round_id, item: started.append(item)
    assert runner2.resume_after_restart() is None
    assert [item["name"] for item in started] == ["file_000001_0001.wav"], "the download left pending before the restart is queued again"
    assert len(host2.requests) == 1 and host2.requests[0].label.startswith("1800 MHz DL")
    host2.finish(host2.requests[0])
    _pump(qapp)
    runner2._tick()
    assert runner2.state == S_DONE
    statuses = [t.status for t in runner2.session.tasks[rid]]
    assert statuses == [T_CONFIRMED, T_SKIPPED, T_CONFIRMED]
    assert runner2.session.rounds[0].status == ROUND_PARTIAL


def test_reconcile_confirms_from_the_sensor_files_or_marks_unknown(qapp, tmp_path, monkeypatch):
    from crfs_iq_recorder import recording_history
    from crfs_iq_recorder.sftp_client import RemoteEntry

    monkeypatch.setattr("crfs_iq_recorder.recording_history.recordings_path", lambda: tmp_path / "rec.json")
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings())
    state = runner.current_tasks()[0]
    planned = runner.session.planned(state.task_key)
    record = recording_history.new_recording(
        host="sensor", start_hz=planned.window_start_hz, end_hz=planned.window_stop_hz,
        center_hz=planned.center_hz, bandwidth_hz=planned.bandwidth_hz, duration_s=planned.duration_s, demo=True,
    )
    recording_history.add_recording(record)
    state.record_id = record.id
    state.summary = {"sensor_parts": ["file_120000_0001.wav"]}
    runner.shutdown()
    runner2 = _runner(tmp_path, FakeHost())
    assert runner2.state == S_INTERRUPTED

    class Browser:
        def listdir(self, path):
            if path.rstrip("/").endswith(record.started_at.strftime("%Y%m%d")):
                return [RemoteEntry("file_120000_0001.wav", path.rstrip("/") + "/file_120000_0001.wav", False, 62_544_000 + 692, datetime.now())]
            return []

        def directory_exists(self, path):
            return True

        def close(self):
            pass

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", lambda *_a, **_k: Browser())
    messages = []
    runner2.reconcile(messages.append)
    assert messages and "1 capture(s) confirmed" in messages[0]
    task = runner2.session.tasks[runner2.session.current_round_id][0]
    assert task.status == T_CONFIRMED and task.files[0]["name"] == "file_120000_0001.wav"
    assert task.summary["held_s"] == pytest.approx(1.0, rel=0.01), "duration measured from the file size at 15.625 MS/s"
    assert any("after restart" in n for n in task.notes)
    assert runner2.resume_after_restart() is None


def test_backlog_pauses_new_captures_until_downloads_drain(qapp, tmp_path):
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, backlog_limit=1))
    # Pretend the download queue is deep and nothing drains it for now.
    runner._drain_downloads = lambda: None
    runner._downloads_pending = [("k", "r", {"name": f"f{i}.wav", "remote_path": f"/x/f{i}.wav", "size": 1}) for i in range(3)]
    host.finish(host.requests[0])
    _pump(qapp)
    assert runner.state == S_PAUSED_BACKLOG
    assert "backlog" in runner.status()["pause_reason"].casefold()
    assert len(host.requests) == 1
    runner._downloads_pending = []
    runner._tick()
    assert runner.state == S_RUNNING and len(host.requests) == 2


def test_downloads_start_from_a_fresh_listing_that_picks_up_late_files_and_final_sizes(qapp, tmp_path, monkeypatch):
    """Seen on the R40-8: the sensor finishes a file after reporting the recording done
    (WAVE +648 B; XDAT writes .xhdr late and grows .xmrk). The runner lists the folder again
    before downloading, records both sizes, adds the late file, and falls back to the
    listed files when the sensor cannot be reached."""
    import crfs_iq_recorder.cellular_runner as cr

    # Long enough that the "nothing downloaded yet" check below is not a race under load.
    monkeypatch.setattr(cr, "FINALIZE_SETTLE_S", 0.4)

    class Entry:
        def __init__(self, path, size):
            self.path, self.size, self.is_dir = path, size, False
            self.name = path.rsplit("/", 1)[-1]

    class Browser:
        def listdir(self, folder):
            assert folder == "/mnt/1/remdata/20261103"
            return [
                Entry("/mnt/1/remdata/20261103/file_000001_0001.xdat", 1648),  # grew after confirmation
                Entry("/mnt/1/remdata/20261103/file_000001_0001.xhdr", 795),  # written late
                Entry("/mnt/1/remdata/20261103/file_000002_0001.xdat", 5),  # another capture: ignored
            ]

        def close(self):
            pass

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", lambda *_a, **_k: Browser())
    host = FakeHost()
    runner = _runner(tmp_path, host)
    started: list[dict] = []
    runner._start_download = lambda task_key, round_id, item: started.append(item)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True))
    host.finish(host.requests[0], names=["file_000001_0001.xdat"])
    _pump(qapp)
    state = runner.current_tasks()[0]
    assert state.status == T_CONFIRMED and started == [], "downloads wait for the fresh listing"
    assert runner.background_jobs() >= 1, "the pending listing counts as background work"
    assert _pump_until(qapp, lambda: len(started) == 2, timeout=3.0)
    names = sorted(item["name"] for item in state.files)
    assert names == ["file_000001_0001.xdat", "file_000001_0001.xhdr"]
    xdat = next(item for item in state.files if item["name"].endswith(".xdat"))
    assert xdat["size"] == 1648 and xdat["listed_size"] == 1000
    assert any("appeared on the sensor after the capture was confirmed" in n for n in state.notes)
    manifest = json.loads(Path(state.manifest_path).read_text(encoding="utf-8")) if state.manifest_path else {}
    # The manifest is rewritten after the download; before that the listed size stands.
    assert manifest["files"][0]["size_listed_at_confirmation"] == 1000

    # The sensor cannot be listed: the files are downloaded as listed, with a note.
    def broken(*_a, **_k):
        raise OSError("sensor unreachable")

    monkeypatch.setattr("crfs_iq_recorder.sftp_window._open_browser", broken)
    started.clear()
    time.sleep(0.05)
    runner._tick()
    host.finish(host.requests[1])
    _pump(qapp)
    assert _pump_until(qapp, lambda: len(started) == 1, timeout=3.0)
    second = runner.current_tasks()[1]
    assert any("could not be listed again" in n for n in second.notes)
    assert second.files[0]["size"] == 1000 and "listed_size" not in second.files[0]


def test_low_local_storage_pauses_before_the_next_capture_and_never_deletes(qapp, tmp_path, monkeypatch):
    """Simulated fault: the download disk fills up. The runner pauses new captures with a
    reason, keeps the finished capture, deletes nothing, and continues once space is back."""
    import shutil as _shutil

    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(auto_download=True, local_reserve_bytes=10_000_000))
    runner._drain_downloads = lambda: None
    usage = type("Usage", (), {"free": 1_000, "total": 10 ** 12, "used": 10 ** 12})()
    monkeypatch.setattr(_shutil, "disk_usage", lambda _p: usage)
    marker = tmp_path / "downloads" / "existing_recording.wav"
    marker.write_bytes(b"keep me")
    host.finish(host.requests[0])
    _pump(qapp)
    assert runner.state == S_PAUSED_BACKLOG
    reason = runner.status()["pause_reason"]
    assert "free" in reason and "No recording is deleted automatically" in reason
    assert len(host.requests) == 1
    assert marker.read_bytes() == b"keep me"
    assert runner.current_tasks()[0].status == T_CONFIRMED
    usage.free = 10 ** 12
    runner._tick()
    assert runner.state == S_RUNNING and len(host.requests) == 2


def test_locked_workbook_defers_excel_rows_and_retries_later(qapp, tmp_path, monkeypatch):
    """Simulated fault: the workbook is open in Excel. Rows are deferred, the capture stays
    confirmed, and the row is queued on a later tick once the workbook is free."""
    import crfs_iq_recorder.cellular_runner as cr
    from crfs_iq_recorder.excel_writer import ExcelBusyError

    host = FakeHost()
    workbook = tmp_path / "log.xlsx"
    Workbook().save(workbook)
    host.excel_logging_target = lambda: (workbook, type("Catalog", (), {"sensor_names": []})())
    attempts = {"n": 0}

    def flaky_ensure(store, path, sheet):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ExcelBusyError("log.xlsx is open in Excel")
        return True

    queued = []
    monkeypatch.setattr(cr, "ensure_results_sheet", flaky_ensure)
    monkeypatch.setattr(cr, "queue_capture_rows", lambda store, **kw: queued.append(kw) or 1)
    monkeypatch.setattr(cr, "worksheet_for_task", lambda task, catalog: cr.RESULTS_SHEET)
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings())
    runner._timer.stop()  # ticks only when the test says so (the 500 ms tick would retry on its own)
    host.finish(host.requests[0])
    _pump(qapp)
    state = runner.current_tasks()[0]
    assert state.status == T_CONFIRMED, "a locked workbook never changes the capture result"
    assert state.excel_status == "deferred" and any("Excel deferred" in n for n in state.notes)
    assert queued == []
    runner._tick()
    assert state.excel_status == "queued" and len(queued) == 1
    assert queued[0]["collection_event"].startswith("Camp / Site A")
    assert attempts["n"] == 2


def test_connection_loss_during_a_capture_is_uncertain_and_never_resent(qapp, tmp_path):
    """Simulated fault: the link drops while the sensor records. The capture ends 'unknown'
    (the sensor may still have written it), it is never resent on its own, the session goes
    on, and the round is reported as partial, not successful."""
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings())
    request = host.requests[0]
    host.finish(request, OUTCOME_UNKNOWN, detail="HTTP connection failed while recording; the sensor may still be writing")
    _pump(qapp)
    tasks = runner.current_tasks()
    assert tasks[0].status == T_UNKNOWN and tasks[0].attempts == 1
    assert len(host.requests) == 2, "the next task proceeds; the uncertain one is not resent"
    assert host.requests[1].label.startswith("700 MHz DL")
    manifest = json.loads(Path(tasks[0].manifest_path).read_text(encoding="utf-8"))
    assert manifest["recording"]["outcome"] == "unknown"
    runner.stop()
    host.finish(host.requests[1], OUTCOME_SENSOR_ERROR, detail="ERROR: Unable to schedule the command")
    _pump(qapp)
    assert runner.state == S_DONE
    record = runner.session.rounds[0]
    assert record.status == ROUND_STOPPED
    # The refusal named no file: nothing was recorded. It would be sent once more after the
    # idle check, but Stop was requested, so it ends as not recorded (skipped), not failed.
    assert (record.tasks_confirmed, record.tasks_uncertain, record.tasks_failed, record.tasks_skipped) == (0, 1, 0, 2)
    assert any("nothing was recorded" in n for n in tasks[1].notes)
    assert len(runner.uncertain_tasks()) == 1, "the uncertain capture stays listed for Reconcile"


def test_round_spanning_midnight_keeps_each_file_in_its_own_sensor_folder(qapp, tmp_path):
    """The sensor's dated folder changes at midnight: files of one round may live in two
    folders. Each capture keeps its own remote directory; nothing is guessed from the round."""
    host = FakeHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings())
    host.finish(host.requests[0], names=["file_235958_0001.wav"])
    _pump(qapp)
    first = runner.current_tasks()[0]
    assert first.files[0]["remote_path"] == "/mnt/1/remdata/20261103/file_235958_0001.wav"
    second_request = host.requests[1]
    summary = CaptureSummary(
        outcome=OUTCOME_CONFIRMED,
        request=second_request,
        record_id="rec-2",
        requested_s=second_request.seconds,
        held_s=second_request.seconds,
        parts=1,
        total_bytes=1000,
        sensor_finished=True,
        request_sent=True,
        sensor_parts=["file_000003_0001.wav"],
        finalized_paths=["/mnt/1/remdata/20261104/file_000003_0001.wav"],
        sizes={"/mnt/1/remdata/20261104/file_000003_0001.wav": 1000},
        started_at=datetime.now(),
        finished_at=datetime.now(),
        stem="file_000003",
    )
    host.capture_finished.emit(summary)
    _pump(qapp)
    second = runner.current_tasks()[1]
    assert second.files[0]["remote_path"] == "/mnt/1/remdata/20261104/file_000003_0001.wav"
    assert first.files[0]["remote_path"].startswith("/mnt/1/remdata/20261103/"), "the earlier capture keeps its own folder"
    manifest = json.loads(Path(second.manifest_path).read_text(encoding="utf-8"))
    assert manifest["files"][0]["remote_path"].startswith("/mnt/1/remdata/20261104/")


@pytest.fixture
def isolated_app_paths(tmp_path, monkeypatch):
    monkeypatch.setattr("crfs_iq_recorder.recording_history.recordings_path", lambda: tmp_path / "recordings.json")
    monkeypatch.setattr("crfs_iq_recorder.paths.settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.setattr("crfs_iq_recorder.sftp_window.QDesktopServices.openUrl", lambda *_a, **_k: True)


def _pump_until(qapp, predicate, timeout=6.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _drive_capture(window, qapp):
    qapp.processEvents()
    window._record_timer.stop()
    if window._recording_active:
        window._on_recording_time_elapsed()
    qapp.processEvents()
    for _ in range(4):
        window._poll_collection_files()
    qapp.processEvents()


def test_demo_main_window_runs_a_round_with_downloads_quality_excel_and_blocks_manual_start(qapp, tmp_path, isolated_app_paths):
    """End to end on the demo transport: captures confirmed from the demo sensor's files,
    parts downloaded and checksummed, quality report + preview written, Excel rows queued,
    manual Start refused while the runner owns the sensor."""
    from crfs_iq_recorder.gui import MainWindow
    from crfs_iq_recorder.recording_history import split_iq_filenames
    from openpyxl import load_workbook
    from test_collection_excel import build_sample_workbook

    workbook = build_sample_workbook(tmp_path / "collection.xlsm")
    window = MainWindow(demo=True)
    runner = None
    try:
        window._settle_s = 0
        window._stable_needed = 2
        window._demo_parts = 2
        window.excel_check.setChecked(True)
        assert window._load_workbook(str(workbook), quiet=True)
        downloads = tmp_path / "dl"
        runner = CellularRunner(window, config_dir=tmp_path / "cfg", download_dir=lambda: downloads)
        logs = []
        runner.log.connect(lambda m, lvl: logs.append((lvl, m)))
        plan = _plan(tmp_path, fallback=0.05)
        assert runner.start_session(plan, Schedule(mode=MODE_NOW), _settings(wait_s=0.0, auto_download=True, verify_checksums=True)) is None
        assert window._external_owner == OWNER
        # Manual Start is held while the session owns the sensor: the button says who has it
        # and a programmatic trigger changes nothing.
        window.time_edit.setText("0.05")
        assert not window.start_btn.isEnabled()
        assert window.start_btn.text().startswith("Recording") or "Cellular Collection" in window.start_btn.text()
        window._on_start()
        assert window._external_owner == OWNER
        assert runner.current_tasks()[0].status == T_SUBMITTING
        for _ in range(3):
            assert _pump_until(qapp, lambda: window._http_in_flight is False)
            _drive_capture(window, qapp)
            runner._tick()
        assert _pump_until(qapp, lambda: runner.state == S_DONE, timeout=8.0), runner.status()
        tasks = runner.session.tasks[runner.session.rounds[0].round_id]
        assert [t.status for t in tasks] == [T_CONFIRMED] * 3
        assert all(len(t.files) == 2 for t in tasks)
        assert _pump_until(qapp, lambda: runner.background_jobs() == 0, timeout=20.0), runner.status()
        for t in tasks:
            assert all(f["download_status"] == "verified" and f["sha256"] for f in t.files), t.files
            assert all(Path(f["local_path"]).is_file() for f in t.files)
            assert t.quality.get("level") in {"ok", "warning"}, t.quality
            assert Path(t.quality["preview_path"]).is_file()
            manifest = json.loads(Path(t.manifest_path).read_text(encoding="utf-8"))
            assert manifest["files"][0]["sha256"] == t.files[0]["sha256"]
            assert manifest["derived_images"] == [t.quality["preview_path"]]
            assert t.excel_status in {"queued", "saved"}
        assert window._external_owner == ""
        # Excel: one row per capture (two parts joined), on the results sheet created for it.
        from crfs_iq_recorder.excel_log import flush_excel_queue

        flush_excel_queue(window._excel_store)
        wb = load_workbook(workbook, keep_vba=True)
        assert "Cellular Results" in wb.sheetnames
        ws = wb["Cellular Results"]
        rows = [[c.value for c in r] for r in ws.iter_rows(min_row=2) if any(c.value for c in r)]
        assert len(rows) == 3
        assert all(len(split_iq_filenames(r[8])) == 2 for r in rows)
        assert rows[0][1] == "Cellular 700 MHz UL" and rows[0][2] == "703 MHz" and rows[0][3] == "713 MHz"
        wb.close()
        handovers = list((downloads / "cellular_manifests").glob("handover_*.json"))
        assert handovers, "handover written once downloads and checks finished"
        summary = json.loads(handovers[-1].read_text(encoding="utf-8"))
        assert summary["counts"]["logical_captures_confirmed"] == 3
        assert summary["counts"]["files"] == 6 and summary["counts"]["files_verified_locally"] == 6
        assert summary["counts"]["derived_images"] == 3
        # Manual Start works again after the session.
        window._on_start()
        assert _pump_until(qapp, lambda: window._http_in_flight is False)
        assert window._recording_active or window._record_phase != "idle"
        _drive_capture(window, qapp)
    finally:
        if runner is not None:
            runner.shutdown()
        window._watch_timer.stop()
        window._record_timer.stop()
        window.close()
        qapp.processEvents()


def test_capture_summary_dict_and_properties():
    summary = CaptureSummary(outcome=OUTCOME_CONFIRMED, requested_s=20, held_s=14.1, parts=7)
    assert summary.short and summary.confirmed and not summary.retry_is_safe
    assert summary.as_dict()["held_s"] == 14.1
    assert CaptureSummary(outcome=OUTCOME_NOT_SENT).retry_is_safe


# --------------------------------------------------------------------------- 1.10: idle check, refusals, identity
class IdleHost(FakeHost):
    """A host whose mission-list check the test answers by hand."""

    def __init__(self):
        super().__init__()
        self.idle_callbacks: list = []

    def verify_sensor_idle(self, on_result, **_kwargs):
        self.idle_callbacks.append(on_result)

    def answer_idle(self, ok, detail=""):
        callback = self.idle_callbacks.pop(0)
        callback(ok, detail)


def test_every_request_waits_for_the_mission_list_and_a_refusal_is_sent_once_more(qapp, tmp_path):
    """Node 100-18, 18:44 run: a POST sent 2 s after the previous stream closed was refused
    ("ERROR: Unable to schedule the command") with no file and no 'finished'. The runner reads
    the mission list before every request, counts the refusal as not recorded, and re-sends
    once after the list shows the sensor idle — never on a timer alone."""
    host = IdleHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings())
    assert host.requests == [] and len(host.idle_callbacks) == 1, "the first request also waits for the list"
    assert runner.status()["text"] == "checking the sensor is idle"
    runner._tick()
    assert len(host.idle_callbacks) == 1, "one check at a time"
    host.answer_idle(True)
    assert len(host.requests) == 1
    first = host.requests[0]
    host.finish(first, OUTCOME_SENSOR_ERROR, detail="ERROR: Unable to schedule the command")
    _pump(qapp)
    task = runner.current_tasks()[0]
    assert task.status == T_QUEUED and task.attempts == 1
    assert any("nothing was recorded" in n for n in task.notes)
    assert len(host.requests) == 1 and len(host.idle_callbacks) == 1, "re-sent only after the list is read again"
    host.answer_idle(True, "waited 3 s for the previous mission to close")
    assert len(host.requests) == 2 and task.status == T_SUBMITTING and task.attempts == 2
    host.finish(host.requests[1], OUTCOME_SENSOR_ERROR, detail="ERROR: Unable to schedule the command")
    _pump(qapp)
    assert task.status == T_SENSOR_ERROR, "at most one re-submission"
    assert any("Not recorded" in n for n in task.notes)
    # Seen on the Node 100-18 (22:26): the sensor named a file, then reported "ERROR: Collection
    # was modified; enumeration operation may not execute" 0.2 s in; 58 MB of 251 MB stayed on
    # the disk. Nothing finished and no file was confirmed: the partial file is named in the
    # note, not counted, and the request is sent once more after the idle check.
    host.answer_idle(True)
    second = host.requests[2]
    summary = CaptureSummary(
        outcome=OUTCOME_SENSOR_ERROR, request=second, requested_s=1.0, sensor_parts=["iq_x_0001.wav"],
        detail="ERROR: Collection was modified; enumeration operation may not execute.",
    )
    host.capture_finished.emit(summary)
    _pump(qapp)
    broken = runner.current_tasks()[1]
    assert broken.status == T_QUEUED and broken.attempts == 1
    assert any("partial file (iq_x_0001.wav)" in n and "not counted" in n for n in broken.notes)
    assert len(host.requests) == 3, "re-sent only after the mission list is read again"
    host.answer_idle(True)
    assert len(host.requests) == 4 and broken.attempts == 2
    host.finish(host.requests[3], held=1.0, names=["iq_y_0001.wav"])
    _pump(qapp)
    assert broken.status == T_CONFIRMED
    # An error after a file was CONFIRMED is a failed capture with files; never re-sent.
    host.answer_idle(True)
    third = host.requests[4]
    last = runner.current_tasks()[2]
    host.capture_finished.emit(CaptureSummary(
        outcome=OUTCOME_SENSOR_ERROR, request=third, requested_s=1.0, parts=1, sensor_parts=["iq_z_0001.wav"],
        finalized_paths=["/mnt/1/remdata/20261103/iq_z_0001.wav"], detail="FATAL: disk",
    ))
    _pump(qapp)
    assert last.status == T_SENSOR_ERROR and last.attempts == 1
    assert len(host.requests) == 5, "never re-sent"


def test_a_sensor_that_still_lists_a_mission_delays_the_request_then_pauses(qapp, tmp_path):
    import crfs_iq_recorder.cellular_runner as cr

    host = IdleHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings())
    for attempt in range(cr.IDLE_RETRIES):
        host.answer_idle(False, "EMP Remote Recording [Center:0.708 GHz,Span:10 MHz]")
        assert host.requests == [] and runner.state == S_WAITING, attempt
        runner._wait_deadline = 0.0  # the retry wait elapses
        runner._tick()
        assert len(host.idle_callbacks) == 1
    host.answer_idle(False, "EMP Remote Recording [Center:0.708 GHz,Span:10 MHz]")
    assert runner.state == S_PAUSED and "still lists a remote recording" in runner.session.pause_reason
    assert host.requests == []
    runner.resume()
    host.answer_idle(True)
    assert len(host.requests) == 1


def test_pause_requested_during_the_idle_check_is_honoured(qapp, tmp_path):
    host = IdleHost()
    runner = _runner(tmp_path, host)
    runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings())
    runner.pause()
    host.answer_idle(True)
    assert host.requests == [] and runner.state == S_PAUSED
    runner.resume()
    host.answer_idle(True)
    assert len(host.requests) == 1


def test_start_is_refused_when_the_connected_sensor_is_not_the_one_the_plan_was_made_for(qapp, tmp_path):
    host = FakeHost()  # R40-8, serial SN1
    runner = _runner(tmp_path, host)
    settings = _settings(sensor_model="R100-18", sensor_serial="rfeye100400")
    error = runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), settings)
    assert error and "CRFS 40-8" in error and "CRFS 100-18" in error and "rfeye100400" in error
    assert host.owner == "" and runner.session is None
    assert runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW), _settings(sensor_model="R40-8", sensor_serial="SN1")) is None
    assert runner.session.sensor["name"] == "CRFS 40-8"


def test_run_now_repeats_passes_after_the_wait_and_until_stopped(qapp, tmp_path):
    host = FakeHost()
    base = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
    clock = {"now": base}
    runner = _runner(tmp_path, host, clock=lambda: clock["now"])
    assert runner.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW, repeat=2), _settings(wait_s=5.0)) is None
    for _ in range(3):
        host.finish(host.requests[-1])
        _pump(qapp)
        runner._wait_deadline = 0.0
        runner._tick()
    assert runner.session.rounds[0].status == ROUND_DONE
    assert len(runner.session.rounds) == 2, "a second pass was queued after the first"
    assert runner.state == S_WAITING_SCHEDULE and runner.status()["text"].startswith("Next pass in ")
    assert len(host.requests) == 3, "the second pass waits for the interval"
    clock["now"] = base + timedelta(seconds=6)
    runner._tick()
    assert len(host.requests) == 4 and runner.current_round() is runner.session.rounds[1]
    for _ in range(3):
        host.finish(host.requests[-1])
        _pump(qapp)
        runner._wait_deadline = 0.0
        runner._tick()
    runner._tick()
    assert runner.state == S_DONE and len(runner.session.rounds) == 2, "repeat count reached"
    # Until stopped: passes keep coming until Stop.
    host2 = FakeHost()
    (tmp_path / "b").mkdir(exist_ok=True)
    runner2 = _runner(tmp_path / "b", host2, clock=lambda: clock["now"])
    assert runner2.start_session(_plan(tmp_path), Schedule(mode=MODE_NOW, repeat=0), _settings(wait_s=0.0)) is None
    for _ in range(3):
        host2.finish(host2.requests[-1])
        _pump(qapp)
        runner2._tick()
    assert len(runner2.session.rounds) == 2 and runner2.state in {S_WAITING_SCHEDULE, S_RUNNING}
    runner2.stop()
    assert runner2.state == S_DONE


def test_timed_schedule_is_capped_by_the_repeat_count(qapp, tmp_path):
    host = FakeHost()
    base = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
    runner = _runner(tmp_path, host, clock=lambda: base)
    schedule = Schedule(
        mode=MODE_TIMES, daily=True, repeat=3,
        times=[datetime(2026, 1, 1, 8, 0).time(), datetime(2026, 1, 1, 20, 0).time()],
        tz=ScheduleTimeZone(kind=TZ_UTC),
    )
    assert runner.start_session(_plan(tmp_path), schedule, _settings()) is None
    assert len(runner.session.rounds) == 3
    assert "3 passes" in schedule.describe()


def test_sleep_gap_and_missed_pass_are_reported_simply(qapp, tmp_path):
    host = FakeHost()
    base = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
    clock = {"now": base}
    runner = _runner(tmp_path, host, clock=lambda: clock["now"])
    logs = []
    runner.log.connect(lambda m, lvl: logs.append(m))
    schedule = Schedule(mode=MODE_TIMES, times=[datetime(2026, 1, 1, 8, 0).time()], dates=[base.date()], tz=ScheduleTimeZone(kind=TZ_UTC))
    runner.start_session(_plan(tmp_path), schedule, _settings())
    runner._last_tick_wall = time.time() - 600
    clock["now"] = base + timedelta(hours=3)
    runner._tick()
    assert any("asleep" in m for m in logs)
    assert any("Missed pass" in m and "08:00" in m for m in logs)
    assert sum(1 for m in logs if "Missed pass" in m) == 1
    runner._tick()
    assert sum(1 for m in logs if "Missed pass" in m) == 1, "said once"


def _protocol_settings(workbook, **kwargs):
    from crfs_iq_recorder.cellular_workbook import layout_from_import, plan_rows_from_import

    imported = import_protocol(workbook, "Cellular Protocol")
    assert not imported.errors
    layout = layout_from_import(imported)
    rows = plan_rows_from_import(imported)
    base = dict(
        wait_s=0.0, auto_download=False, verify_checksums=False,
        protocol_path=str(workbook), protocol_sheet="Cellular Protocol", protocol_results=True,
        protocol_layout=layout.to_dict(), plan_rows=[r.to_dict() for r in rows],
    )
    base.update(kwargs)
    return imported, CampaignSettings(**base)


def test_results_land_in_the_protocol_rows_then_in_an_appended_block(qapp, tmp_path, monkeypatch):
    import crfs_iq_recorder.cellular_runner as cr
    from openpyxl import load_workbook
    from test_cellular_workbook import make_workbook

    monkeypatch.setattr(cr, "WORKBOOK_COALESCE_S", 0.02)
    workbook = make_workbook(
        tmp_path / "protocol.xlsx",
        rows=[
            ("700 MHz (B28)", "DL", "FDD", 753, 773),
            ("700 MHz (B28)", "UL", "FDD", 698, 718),
            ("1800 MHz (B3)", "DL", "FDD", 1805, 1825),
            ("3500 MHz (n78)", "TDD / shared UL-DL", "TDD", 3300, 3450),
        ],
    )
    imported, settings = _protocol_settings(workbook)
    plan = build_plan(imported, capability_for("R100-18"), fallback_duration_s=2.0)
    # 2 s on the 100-18 -> 50 MHz windows: three single-window rows and n78 in three windows.
    assert plan.ready and len(plan.tasks) == 6, [t.label() for t in plan.tasks]
    host = FakeHost()
    host._sensor_info = type("Info", (), {"model": "R100-18", "serial": "rfeye100400", "firmware": "2.25"})()
    runner = _runner(tmp_path, host)
    logs = []
    runner.log.connect(lambda m, lvl: logs.append(m))
    assert runner.start_session(plan, Schedule(mode=MODE_NOW), settings) is None
    assert runner.protocol_results_enabled()
    # Row 1 (700 DL): one window, two files, 2.011 s measured.
    host.finish(host.requests[0], parts=2, held=2.011, names=["iq_20261103_070001_0000.wav", "iq_20261103_070001_0001.wav"])
    _pump(qapp)
    assert runner.workbook_pending() == 0, runner.status()
    wb = load_workbook(workbook)
    ws = wb["Cellular Protocol"]
    assert ws["G2"].value == "iq_20261103_070001_0000.wav\niq_20261103_070001_0001.wav"
    assert ws["H2"].value == pytest.approx(2.011) and ws["H2"].number_format == "0.000"
    assert isinstance(ws["I2"].value, datetime) and ws["I2"].number_format == "dd/mm/yyyy hh:mm:ss"
    assert ws["F2"].value == "CRFS 100-18"
    assert ws["G3"].value is None
    wb.close()
    assert any("row 1 of 4): complete" in m for m in logs), logs
    # Row 2 (700 UL): refused twice -> nothing written, reported as incomplete coverage.
    host.finish(host.requests[1], OUTCOME_SENSOR_ERROR, detail="ERROR: Unable to schedule the command")
    _pump(qapp)
    host.finish(host.requests[2], OUTCOME_SENSOR_ERROR, detail="ERROR: Unable to schedule the command")
    _pump(qapp)
    assert any("row 2 of 4): incomplete" in m for m in logs)
    # Row 3 (1800 DL) one window; row 4 (n78) two of three windows recorded.
    host.finish(host.requests[3], held=2.0, names=["iq_20261103_070100_0000.wav"])
    _pump(qapp)
    n78 = [r for r in host.requests[4:]]
    host.finish(n78[0], held=2.0, names=["iq_20261103_070200_0000.wav"])
    _pump(qapp)
    host.finish(host.requests[5], OUTCOME_UNKNOWN, detail="no file settled")
    _pump(qapp)
    host.finish(host.requests[6], held=2.0, names=["iq_20261103_070300_0000.wav"])
    _pump(qapp)
    runner._tick()
    assert runner.state == S_DONE
    assert _pump_until(qapp, lambda: runner.workbook_pending() == 0 and not runner._workbook_flushing, timeout=5.0), runner.status()
    wb = load_workbook(workbook)
    ws = wb["Cellular Protocol"]
    assert ws["G3"].value is None and ws["H3"].value is None, "a row with nothing recorded stays empty"
    assert ws["G4"].value == "iq_20261103_070100_0000.wav"
    assert ws["G5"].value == "iq_20261103_070200_0000.wav\niq_20261103_070300_0000.wav", "windows of one row grouped in its cell"
    assert ws["H5"].value == pytest.approx(4.0), "seconds of IQ held by the files of both windows"
    assert ws.max_row == 5 and dict(ws.tables.items()) == {"CellularProtocol": "A1:I5"}
    wb.close()
    assert any("Incomplete coverage this pass" in m and "700 MHz" in m and "2 of 3 windows" in m for m in logs), logs
    assert any("Pass 1 partial: 2 of 4 protocol row(s) complete" in m for m in logs), logs
    # Second pass (new session, same workbook): appended block, first block untouched.
    host2 = FakeHost()
    host2._sensor_info = host._sensor_info
    (tmp_path / "second").mkdir(exist_ok=True)
    runner2 = _runner(tmp_path / "second", host2)
    assert runner2.start_session(plan, Schedule(mode=MODE_NOW), settings) is None
    host2.finish(host2.requests[0], held=2.5, names=["iq_20261103_120000_0000.wav"])
    _pump(qapp)
    assert _pump_until(qapp, lambda: runner2.workbook_pending() == 0 and not runner2._workbook_flushing, timeout=5.0), runner2.status()
    wb = load_workbook(workbook)
    ws = wb["Cellular Protocol"]
    assert ws["G2"].value.startswith("iq_20261103_070001_0000.wav")
    assert ws["G6"].value == "iq_20261103_120000_0000.wav" and ws["A6"].value == "700 MHz (B28)" and ws["B6"].value == "DL"
    assert ws["H6"].value == pytest.approx(2.5)
    assert dict(ws.tables.items()) == {"CellularProtocol": "A1:I9"}
    wb.close()
    assert len(list(tmp_path.glob("*.crfs-backup-*"))) == 2, "every session backs the workbook up before its first change"
    runner2.stop()
    if runner2.state != S_DONE and host2.requests:
        host2.finish(host2.requests[-1])
    _pump(qapp)
    assert runner2.state == S_DONE


def test_pending_workbook_results_survive_a_restart_and_a_locked_file(qapp, tmp_path, monkeypatch):
    from openpyxl import load_workbook
    from test_cellular_workbook import make_workbook

    import crfs_iq_recorder.cellular_workbook as cw
    from crfs_iq_recorder.excel_writer import ExcelBusyError

    workbook = make_workbook(tmp_path / "protocol.xlsx")
    imported, settings = _protocol_settings(workbook)
    plan = build_plan(imported, capability_for("R40-8"), fallback_duration_s=1.0)
    host = FakeHost()
    runner = _runner(tmp_path, host)
    logs = []
    runner.log.connect(lambda m, lvl: logs.append(m))

    def locked(*_a, **_k):
        raise ExcelBusyError("protocol.xlsx is open in another program")

    monkeypatch.setattr(cw, "write_results_openpyxl", locked)
    runner.start_session(plan, Schedule(mode=MODE_NOW), settings)
    host.finish(host.requests[0], held=1.0, names=["iq_20261103_080000_0000.wav"])
    _pump(qapp)
    assert runner.workbook_pending() == 1
    status = runner.status()
    assert status["workbook_pending"] == 1 and status["workbook_status"] == "busy"
    assert any("Excel update pending" in m for m in logs)
    assert load_workbook(workbook)["Cellular Protocol"]["G2"].value is None
    runner.shutdown()
    # Restart with the workbook free: the queued result is written, once.
    monkeypatch.undo()
    host2 = FakeHost()
    runner2 = _runner(tmp_path, host2)
    assert runner2.workbook_pending() == 1
    assert runner2.status()["workbook_pending"] == 1, "the pending indicator shows before anything is started"
    # The runner flushes on its own shortly after start-up (no user action needed).
    assert _pump_until(qapp, lambda: runner2.workbook_pending() == 0 and not runner2._workbook_flushing, timeout=6.0)
    wb = load_workbook(workbook)
    assert wb["Cellular Protocol"]["G2"].value == "iq_20261103_080000_0000.wav"
    assert wb["Cellular Protocol"]["H2"].value == 1.0
    wb.close()
    runner2._flush_workbook()
    assert runner2.workbook_pending() == 0
    assert len(list(tmp_path.glob("*.crfs-backup-*"))) == 1, "one backup before the first change"
