"""POST /emp/ answers with a Server-Sent Events stream on real Nodes.

A local HTTP server replays what an RFeye Node 40-8 (firmware 2.25-325) sent
on 31 Oct 2026, so the client code is exercised end to end through `requests`.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests.sessions

# Captured at import time, before the conftest fixture blocks the network.
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_SESSION_REQUEST = requests.sessions.Session.request


def _loopback_only_create_connection(address, *args, **kwargs):
    host = address[0]
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(f"Tests must not open TCP sockets to real hosts ({host}).")
    return _REAL_CREATE_CONNECTION(address, *args, **kwargs)

from crfs_iq_recorder.api_client import (
    STREAM_BUDGET,
    STREAM_CANCELLED,
    STREAM_FINISHED,
    EmpClient,
    SseEvent,
    parse_sse_blocks,
    recording_stream_budget_s,
    sse_part_filename,
)
from crfs_iq_recorder.file_watch import FileWatchState, capture_is_settled, update_file_watch
from crfs_iq_recorder.request_builder import build_payload_for_recording_time
from crfs_iq_recorder.frequency import from_center_bandwidth
from crfs_iq_recorder.sftp_client import RemoteEntry
from crfs_iq_recorder.validation import ConnectionTarget

GPS = (
    'event: message.MessageData\ndata: [{"level":"WARNING","message":"No GPS reference '
    '(0 satellites, jamming indicator 21 %), using internal clock","timestamp":"2026-10-31T18:15:47+00:00"}]\n\n'
)


def remote_time(name: str, task: str = "iq-20261031-201546") -> str:
    item = {
        "GPS": {"latitude": 0.0, "longitude": 0.0, "altitude": 0.0},
        "center_frequency": 800000000.0,
        "bandwidth": 20000000.0,
        "event_uuid": "f5a5fa09-5ddb-44f2-8f03-b3a6db2ad7c0",
        "duration": 10.0,
        "file_dir": "/mnt/1/remdata/20261031/",
        "first_filename": name,
        "file_count": 1,
        "task_id": task,
        "timestamp": "2026-10-31T18:15:47.0010012",
    }
    return f"event: time.RemoteTimeData\ndata: {json.dumps([item])}\n\n"


FINISHED = (
    'event: message.MessageData\ndata: [{"level":"INFO","message":"Remote recording is now finished",'
    '"timestamp":"2026-10-31T18:15:58.6705665+00:00"}]\n\n'
)
ERROR = (
    'event: message.MessageData\ndata: [{"level":"ERROR","message":"Centre frequency out of range",'
    '"timestamp":"2026-10-31T18:15:47+00:00"}]\n\n'
)
KEEPALIVE = ": keepalive\n\n"


class _Script:
    """What the fake sensor streams: list of (delay_s, text). None text = close."""

    def __init__(self, steps, *, status=200, content_type="text/event-stream"):
        self.steps = steps
        self.status = status
        self.content_type = content_type
        self.requests: list[dict] = []
        self.disconnected = threading.Event()


def _make_server(script: _Script):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            return None

        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            script.requests.append({"path": self.path, "body": json.loads(body or b"{}")})
            self.send_response(script.status)
            self.send_header("Content-Type", script.content_type)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            try:
                for delay, text in script.steps:
                    if delay:
                        time.sleep(delay)
                    if text is None:
                        break
                    data = text.encode("utf-8")
                    self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                    self.wfile.flush()
                else:
                    # Keep the connection open like the Node does until the client leaves.
                    deadline = time.time() + 20
                    while time.time() < deadline:
                        time.sleep(0.2)
                        self.wfile.write(b"d\r\n: keepalive\n\n\r\n")
                        self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass
            finally:
                script.disconnected.set()
                try:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True

    class QuietServer(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):  # noqa: D401 - silence client disconnects
            return None

    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


@pytest.fixture
def sensor(monkeypatch):
    monkeypatch.setattr("socket.create_connection", _loopback_only_create_connection)
    monkeypatch.setattr("requests.sessions.Session.request", _REAL_SESSION_REQUEST)
    servers = []

    def start(script: _Script):
        server = _make_server(script)
        servers.append(server)
        port = server.server_address[1]
        target = ConnectionTarget(host="127.0.0.1", port=port, use_https=False, username="admin", timeout_s=5.0)
        return target

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def _payload(seconds=10.0):
    return build_payload_for_recording_time(from_center_bandwidth(800_000_000, 20_000_000), seconds, task_id="iq-test")


def test_parse_sse_blocks_handles_split_chunks_and_comments():
    text = GPS[:40]
    blocks, rest = parse_sse_blocks(text)
    assert blocks == [] and rest == text
    blocks, rest = parse_sse_blocks(text + GPS[40:] + KEEPALIVE + remote_time("file_181547_0001.wav")[:30])
    assert len(blocks) == 1
    assert blocks[0][0] == "message.MessageData"
    assert rest == remote_time("file_181547_0001.wav")[:30]
    event = SseEvent("time.RemoteTimeData", json.loads(remote_time("x.wav").split("data: ")[1]), 0.0)
    assert sse_part_filename(event) == ("/mnt/1/remdata/20261031/", "x.wav")


def test_stream_returns_when_the_sensor_reports_finished(sensor):
    script = _Script([
        (0.05, GPS),
        (0.0, remote_time("file_181547_0001.wav")),
        (0.1, GPS + GPS),
        (0.1, remote_time("file_181547_0002.wav")),
        (0.1, FINISHED),
    ])
    target = sensor(script)
    seen = []
    client = EmpClient(demo=False)
    started = time.monotonic()
    outcome = client.start_recording(target, "pw", _payload(), on_event=seen.append, stream_budget_s=10)
    elapsed = time.monotonic() - started
    assert outcome.kind == "accepted" and outcome.accepted
    assert outcome.sensor_finished is True
    assert outcome.stream_ended == STREAM_FINISHED
    assert outcome.sensor_parts == ["file_181547_0001.wav", "file_181547_0002.wav"]
    assert outcome.sensor_file_dir == "/mnt/1/remdata/20261031/"
    assert outcome.sensor_task_id == "iq-20261031-201546"
    assert outcome.sensor_messages == ["INFO: Remote recording is now finished"]
    assert elapsed < 5, "must not wait for the endless keepalives"
    assert script.requests[0]["path"] == "/emp/"
    assert script.requests[0]["body"] == _payload()
    assert seen[0].event == "crfs_iq_recorder.http_accepted"
    assert [e.event for e in seen if e.event == "time.RemoteTimeData"] == ["time.RemoteTimeData"] * 2
    assert script.disconnected.wait(3), "client must release the connection so the Node drops the mission"


def test_stream_error_message_is_reported_as_sensor_error(sensor):
    target = sensor(_Script([(0.05, ERROR)]))
    outcome = EmpClient(demo=False).start_recording(target, "pw", _payload(), stream_budget_s=10)
    assert outcome.kind == "sensor_error"
    assert outcome.accepted is False
    assert outcome.request_sent is True
    assert "Centre frequency out of range" in outcome.detail
    assert outcome.sensor_finished is False


def test_stream_budget_ends_the_wait_without_completion(sensor):
    target = sensor(_Script([(0.05, remote_time("file_181547_0001.wav"))]))  # then keepalives forever
    started = time.monotonic()
    outcome = EmpClient(demo=False).start_recording(target, "pw", _payload(), stream_budget_s=1.0)
    assert time.monotonic() - started < 4
    assert outcome.kind == "accepted" and outcome.accepted
    assert outcome.sensor_finished is False
    assert outcome.stream_ended == STREAM_BUDGET
    assert outcome.outcome_unknown is True
    assert outcome.sensor_parts == ["file_181547_0001.wav"]
    assert "Completion is confirmed from the files" in outcome.detail


def test_cancel_event_and_abort_unblock_the_stream(sensor):
    target = sensor(_Script([(0.05, GPS)]))  # keepalives forever
    cancel = threading.Event()
    client = EmpClient(demo=False)
    box = {}

    def work():
        box["outcome"] = client.start_recording(target, "pw", _payload(), cancel=cancel, stream_budget_s=30)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    time.sleep(0.4)
    cancel.set()
    started = time.monotonic()
    client.abort()
    client.close()
    abort_took = time.monotonic() - started
    thread.join(5)
    unblocked_after = time.monotonic() - started
    assert not thread.is_alive(), "abort() must unblock the worker"
    assert abort_took < 0.2, f"abort()/close() must not stall the caller ({abort_took:.3f} s)"
    assert unblocked_after < 1.0, f"the blocked reader must return promptly ({unblocked_after:.3f} s)"
    outcome = box["outcome"]
    assert outcome.stream_ended == STREAM_CANCELLED
    assert outcome.accepted is True
    assert outcome.outcome_unknown is False


def test_server_dropping_the_stream_is_reported_as_lost(sensor):
    target = sensor(_Script([(0.05, GPS), (0.05, None)]))
    outcome = EmpClient(demo=False).start_recording(target, "pw", _payload(), stream_budget_s=10)
    assert outcome.accepted is True
    assert outcome.sensor_finished is False
    assert outcome.stream_ended in ("closed",) or outcome.stream_ended.startswith("lost")
    assert outcome.outcome_unknown is True


def test_plain_json_reply_still_works(sensor):
    script = _Script([(0.0, '{"status":"ok"}'), (0.0, None)], content_type="application/json")
    target = sensor(script)
    outcome = EmpClient(demo=False).start_recording(target, "pw", _payload(), stream_budget_s=5)
    assert outcome.kind == "accepted"
    assert outcome.stream_ended == ""
    assert outcome.body_json == {"status": "ok"}


def test_budget_scales_with_duration():
    assert recording_stream_budget_s(2) == 62
    assert recording_stream_budget_s(40) == 100
    assert recording_stream_budget_s(300) == 600


def test_gui_mode_holds_the_stream_after_finished_until_cancelled(sensor):
    script = _Script([(0.05, remote_time("file_181547_0001.wav")), (0.1, FINISHED)])  # then keepalives
    target = sensor(script)
    cancel = threading.Event()
    client = EmpClient(demo=False)
    seen = []
    box = {}

    def work():
        box["outcome"] = client.start_recording(
            target, "pw", _payload(), cancel=cancel, on_event=seen.append, stream_budget_s=30, stop_on_finished=False
        )

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not any(e.event == "message.MessageData" for e in seen):
        time.sleep(0.05)
    assert any(e.event == "message.MessageData" for e in seen), "finished event must be delivered live"
    time.sleep(0.5)
    assert thread.is_alive(), "stream must stay open after the sensor's completion message"
    assert not script.disconnected.is_set()
    cancel.set()
    client.abort()
    thread.join(5)
    assert not thread.is_alive()
    outcome = box["outcome"]
    assert outcome.sensor_finished is True
    assert outcome.kind == "accepted"
    assert outcome.stream_ended == STREAM_CANCELLED
    assert outcome.sensor_parts == ["file_181547_0001.wav"]
    assert script.disconnected.wait(3)


def _entry(name, size, when):
    return RemoteEntry(name, f"/mnt/1/remdata/20261031/{name}", False, size, when)


def test_growing_files_are_never_complete_even_after_the_watch_limit():
    # 40 MHz x 40 s: the sensor writes ~100 MB/s while capturing 250 MB/s, so the
    # last part can still grow well after the 60 s watch limit.
    now = datetime(2026, 10, 31, 20, 0, 0)
    elapsed_at = now
    state = FileWatchState()
    sizes = [100, 200, 300, 400]
    for index, size in enumerate(sizes):
        when = now + timedelta(seconds=2 * index + 70)  # already past max_watch_s
        state, _ = update_file_watch(state, [_entry("file_a_0001.wav", 1000, when), _entry("file_a_0002.wav", size, when)], now=when)
        assert not capture_is_settled(state, now=when, elapsed=True, elapsed_at=elapsed_at)
    # Once stable (two listings) and quiet long enough it completes.
    when = now + timedelta(seconds=80)
    state, newly = update_file_watch(state, [_entry("file_a_0001.wav", 1000, when), _entry("file_a_0002.wav", 400, when)], now=when)
    assert [e.name for e in newly] == ["file_a_0002.wav"]
    state, newly = update_file_watch(state, [_entry("file_a_0001.wav", 1000, when), _entry("file_a_0002.wav", 400, when)], now=when + timedelta(seconds=2))
    assert newly == []
    assert not capture_is_settled(state, now=when + timedelta(seconds=3), elapsed=True, elapsed_at=elapsed_at, settle_s=8)
    assert capture_is_settled(state, now=when + timedelta(seconds=12), elapsed=True, elapsed_at=elapsed_at, settle_s=8)


def test_open_stream_without_finished_holds_completion_and_sensor_finished_plus_size_releases_it():
    now = datetime(2026, 10, 31, 20, 0, 0)
    state = FileWatchState()
    for offset in (0, 2):
        when = now + timedelta(seconds=offset)
        state, _ = update_file_watch(state, [_entry("file_a_0001.wav", 1_000_000, when)], now=when)
    when = now + timedelta(seconds=3)
    assert not capture_is_settled(state, now=when, elapsed=True, elapsed_at=now, stream_pending=True, sensor_finished=False)
    # Sensor said finished and the file holds the expected bytes: no 8 s settle needed.
    assert capture_is_settled(
        state, now=when, elapsed=True, elapsed_at=now, stream_pending=False, sensor_finished=True, expected_bytes=1_000_000
    )
    # Sensor finished but only half the expected bytes are there: wait for the quiet period.
    assert not capture_is_settled(
        state, now=when, elapsed=True, elapsed_at=now, sensor_finished=True, expected_bytes=2_000_000, settle_s=8
    )
    assert capture_is_settled(
        state, now=now + timedelta(seconds=11), elapsed=True, elapsed_at=now, sensor_finished=True, expected_bytes=2_000_000, settle_s=8
    )


def test_no_files_gives_up_after_the_watch_limit_only():
    now = datetime(2026, 10, 31, 20, 0, 0)
    state = FileWatchState()
    assert not capture_is_settled(state, now=now + timedelta(seconds=59), elapsed=True, elapsed_at=now)
    assert capture_is_settled(state, now=now + timedelta(seconds=60), elapsed=True, elapsed_at=now)
