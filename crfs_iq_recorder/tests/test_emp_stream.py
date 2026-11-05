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
    NODE_SLOWEST_WRITE_BYTES_PER_S,
    STREAM_BUDGET,
    STREAM_CANCELLED,
    STREAM_FINISHED,
    EmpClient,
    RawResponse,
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
    assert recording_stream_budget_s(2, 1_000_000_000) == 62 + 1_000_000_000 / NODE_SLOWEST_WRITE_BYTES_PER_S


def test_default_budget_leaves_the_node_time_to_write_a_full_rate_capture():
    """2 s of 2110-2170 MHz is 1.0 GB at 125 MS/s on a Node 100-18; with a download
    running the Node needed over a minute to write it, and a client that leaves
    earlier gets the file closed short."""
    budgets = []

    class Transport:
        def request(self, method, url, **kwargs):
            budgets.append(kwargs["stream"].budget_s)
            return RawResponse(
                status_code=200, text='{"status":"ok"}', headers={"Content-Type": "application/json"},
                elapsed_s=0.0, url=url,
            )

        def close(self):
            pass

    payload = build_payload_for_recording_time(from_center_bandwidth(2_140_000_000, 60_000_000), 2.0, task_id="iq-test")
    target = ConnectionTarget(host="127.0.0.1", port=80, use_https=False, username="admin", timeout_s=5.0)
    outcome = EmpClient(transport=Transport(), demo=False).start_recording(target, "pw", payload)
    assert outcome.accepted
    assert budgets and budgets[0] >= 2 + 60 + 1_000_000_000 / NODE_SLOWEST_WRITE_BYTES_PER_S


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


CLOCK = (
    'event: message.MessageData\ndata: [{"level":"WARNING","message":"Using internal clock because of '
    'ncpd.conf settings","timestamp":"2026-11-02T16:44:10+00:00"}]\n\n'
)
SCHEDULE_ERROR = (
    'event: message.MessageData\ndata: [{"level":"ERROR","message":"Unable to schedule the command",'
    '"timestamp":"2026-11-02T16:45:03+00:00"}]\n\n'
)


def test_repeated_warning_is_counted_once_and_never_hides_the_real_error(sensor):
    """Root cause (a), Node 100-18 at 18:44: the clock WARNING arrived several times a second,
    filled the message window and the ERROR that ended the stream was invisible."""
    script = _Script([(0.02, CLOCK * 60), (0.02, remote_time("file_164410_0001.wav")), (0.02, SCHEDULE_ERROR)])
    outcome = EmpClient(demo=False).start_recording(sensor(script), "pw", _payload(), stream_budget_s=10)
    assert outcome.kind == "sensor_error", "a WARNING is not fatal; the ERROR is what ended this recording"
    assert outcome.sensor_messages == [
        "WARNING: Using internal clock because of ncpd.conf settings (×60)",
        "ERROR: Unable to schedule the command",
    ]
    assert outcome.sensor_error == "ERROR: Unable to schedule the command"
    assert "Unable to schedule the command" in outcome.detail
    assert outcome.sensor_parts == ["file_164410_0001.wav"]


def test_internal_clock_warning_carries_its_explanation():
    """Read on rfeye100400 (3 Nov 2026, read-only): ncpd.conf sets real_time_clock and
    reference_clock to "internal". The log line says what the message means."""
    from crfs_iq_recorder.api_client import sensor_message_note

    note = sensor_message_note("Using internal clock because of ncpd.conf settings")
    assert "real_time_clock" in note and "not affected" in note
    assert sensor_message_note("No suitable antenna found") == ""
    assert sensor_message_note("") == ""


def test_warning_only_stream_finishes_normally(sensor):
    """A WARNING alone (internal clock) must never be treated as a failed recording."""
    script = _Script([(0.02, CLOCK), (0.02, remote_time("file_164410_0001.wav")), (0.05, CLOCK), (0.05, FINISHED)])
    outcome = EmpClient(demo=False).start_recording(sensor(script), "pw", _payload(), stream_budget_s=10)
    assert outcome.kind == "accepted" and outcome.accepted and outcome.sensor_finished
    assert outcome.sensor_error == ""
    assert outcome.sensor_messages == [
        "WARNING: Using internal clock because of ncpd.conf settings (×2)",
        "INFO: Remote recording is now finished",
    ]


def test_error_after_finished_keeps_the_recording_confirmed(sensor):
    """Root cause (b): 'Unable to schedule the command' arrived ~45 s after 'finished' while the
    GUI still held the stream. The recording is done; the late error is reported, not fatal."""
    script = _Script([(0.02, remote_time("file_164410_0001.wav")), (0.05, FINISHED), (0.2, SCHEDULE_ERROR)])
    cancel = threading.Event()
    client = EmpClient(demo=False)
    outcome = client.start_recording(sensor(script), "pw", _payload(), cancel=cancel, stream_budget_s=10, stop_on_finished=False)
    assert outcome.kind == "accepted" and outcome.accepted is True
    assert outcome.sensor_finished is True and outcome.outcome_unknown is False
    assert outcome.sensor_error == "ERROR: Unable to schedule the command"
    assert "confirmed from the files" in outcome.detail and "Unable to schedule the command" in outcome.detail
    assert outcome.sensor_parts == ["file_164410_0001.wav"]


def test_refused_request_names_no_file_and_is_not_finished(sensor):
    """Root cause (d): a POST refused by the sensor because the previous mission was still open.
    Nothing was recorded: no file, no 'finished'; the caller must not count it."""
    outcome = EmpClient(demo=False).start_recording(sensor(_Script([(0.02, SCHEDULE_ERROR)])), "pw", _payload(), stream_budget_s=10)
    assert outcome.kind == "sensor_error" and outcome.accepted is False and outcome.request_sent is True
    assert outcome.sensor_parts == [] and outcome.sensor_finished is False
    # A refusal names no file, so the runner records the row as failed and never
    # re-sends it by itself. An error after a part was named is different: the
    # sensor did start, so the files decide.
    finished = EmpClient(demo=False).start_recording(
        sensor(_Script([(0.02, remote_time("f.wav")), (0.02, FINISHED)])), "pw", _payload(), stream_budget_s=10
    )
    assert finished.kind == "accepted" and finished.sensor_finished is True and finished.sensor_parts == ["f.wav"]
    partial = EmpClient(demo=False).start_recording(
        sensor(_Script([(0.02, remote_time("f.wav")), (0.02, SCHEDULE_ERROR)])), "pw", _payload(), stream_budget_s=10
    )
    assert partial.kind == "sensor_error" and partial.sensor_parts == ["f.wav"]


MISSIONS_HTML = (
    '<div class="item">Periodic Sweep [1 MHz-6 GHz]</div>\n'
    '<div class="item active">EMP Remote Recording [Center:1.0 GHz,Span:20 MHz]</div>\n'
    '<div class="item"><span>Standby</span></div>'
)


def test_mission_list_parsing_and_recording_detection():
    from crfs_iq_recorder.api_client import MissionList, parse_emp_missions, remote_recording_missions

    missions = parse_emp_missions(MISSIONS_HTML)
    assert missions == ["Periodic Sweep [1 MHz-6 GHz]", "EMP Remote Recording [Center:1.0 GHz,Span:20 MHz]", "Standby"]
    assert remote_recording_missions(missions) == ["EMP Remote Recording [Center:1.0 GHz,Span:20 MHz]"]
    assert parse_emp_missions("") == [] and parse_emp_missions("<div></div>") == []
    assert parse_emp_missions("Standby\nEMP Remote Recording [x]") == ["Standby", "EMP Remote Recording [x]"]
    assert MissionList(available=True, missions=missions, recording=remote_recording_missions(missions)).recording_open
    assert not MissionList(available=True).recording_open
    assert not MissionList(available=False, error="HTTP 401").recording_open


def test_list_missions_is_a_read_only_get(monkeypatch):
    from crfs_iq_recorder.api_client import EMP_MISSIONS_PATH, EmpClient, RawResponse

    calls = []

    class Transport:
        def request(self, method, url, **kwargs):
            calls.append((method, url))
            return RawResponse(status_code=200, headers={}, text=MISSIONS_HTML, url=url, elapsed_s=0.01)

    target = ConnectionTarget(host="sensor.local", port=80, use_https=False, username="admin", timeout_s=5.0)
    listing = EmpClient(demo=False, transport=Transport()).list_missions(target, "pw")
    assert calls == [("GET", f"{target.origin}{EMP_MISSIONS_PATH}")]
    assert listing.available and listing.recording_open
    assert listing.recording == ["EMP Remote Recording [Center:1.0 GHz,Span:20 MHz]"]

    class Refusing:
        def request(self, method, url, **kwargs):
            return RawResponse(status_code=401, headers={}, text="", url=url, elapsed_s=0.01)

    denied = EmpClient(demo=False, transport=Refusing()).list_missions(target, "pw")
    assert denied.available is False and denied.error == "HTTP 401" and not denied.recording_open
    assert EmpClient(demo=True).list_missions(target, "pw").available is True


class _Browser:
    """An SFTP session fake: `folders` maps path -> entries, None = raises SftpError."""

    def __init__(self, folders, *, alive=True):
        self.folders = folders
        self.alive = alive
        self.calls: list[str] = []

    def is_alive(self):
        return self.alive

    def listdir(self, path):
        from crfs_iq_recorder.sftp_client import SftpError, SftpNotFoundError

        self.calls.append(path)
        if path not in self.folders:
            raise SftpNotFoundError(f"Folder not found: {path}")
        if self.folders[path] is None:
            raise SftpError("Socket is closed")
        return list(self.folders[path])


def test_dead_watch_session_raises_instead_of_looking_like_an_empty_folder():
    """Root cause (c), Node 100-18: the watch session went silent during a download, every
    listing came back empty and a recording that was on the disk was never confirmed."""
    from crfs_iq_recorder.file_watch import list_watch_entries
    from crfs_iq_recorder.sftp_client import SftpError

    when = datetime(2026, 11, 2, 18, 44, 0)
    folders = ["/mnt/1/remdata/20261102/", "/mnt/1/remdata/20261103/"]
    # Missing date folders are skipped (the day's first file has not been written yet).
    assert list_watch_entries(_Browser({}), folders) == []
    # A present folder lists; a missing sibling does not matter.
    live = _Browser({folders[0]: [_entry("file_184400_0001.wav", 10, when)]})
    assert [e.name for e in list_watch_entries(live, folders)] == ["file_184400_0001.wav"]
    # Every folder failing (not "missing") is a failing session, not an empty folder.
    with pytest.raises(SftpError, match="Could not list"):
        list_watch_entries(_Browser({folders[0]: None, folders[1]: None}), folders)
    # One failing folder with another listed fine is tolerated.
    partial = _Browser({folders[0]: None, folders[1]: [_entry("file_000100_0001.wav", 10, when)]})
    assert [e.name for e in list_watch_entries(partial, folders)] == ["file_000100_0001.wav"]
    # A closed transport is rejected before any listing is attempted.
    dead = _Browser({folders[0]: [_entry("x.wav", 10, when)]}, alive=False)
    with pytest.raises(SftpError, match="closed"):
        list_watch_entries(dead, folders)
    assert dead.calls == []


def test_paramiko_browser_reports_a_dead_transport_and_distinguishes_missing_folders():
    from crfs_iq_recorder.sftp_client import ParamikoSftpBrowser, SftpError, SftpNotFoundError

    class Transport:
        def __init__(self, active):
            self.active = active

        def is_active(self):
            return self.active

    class Client:
        def __init__(self, transport):
            self.transport = transport

        def get_transport(self):
            return self.transport

    class Sftp:
        def listdir_attr(self, path):
            if path.endswith("missing/"):
                raise FileNotFoundError(path)
            raise OSError("Socket is closed")

        def close(self):
            return None

    browser = ParamikoSftpBrowser.__new__(ParamikoSftpBrowser)
    browser._client = Client(Transport(True))
    browser._sftp = Sftp()
    assert browser.is_alive() is True
    browser._client = Client(Transport(False))
    assert browser.is_alive() is False
    browser._client = Client(None)
    assert browser.is_alive() is False
    with pytest.raises(SftpNotFoundError):
        browser.listdir("/mnt/1/remdata/missing/")
    with pytest.raises(SftpError) as info:
        browser.listdir("/mnt/1/remdata/20261102/")
    assert not isinstance(info.value, SftpNotFoundError)


def test_paramiko_browser_cancel_reports_cancelled_and_leaves_no_part_file(tmp_path):
    """Hardware (Node 100-18, 750 MB file cancelled after 5 MB): Cancel closes the socket under
    paramiko, which raises its own `SFTPError("Garbage packet received")` (not an OSError).
    That was counted as a failure, shown in a pop-up, and left `<name>.wav.part` behind."""
    import threading
    from pathlib import Path

    from crfs_iq_recorder.sftp_client import ParamikoSftpBrowser, SftpError

    class ParamikoSFTPError(Exception):
        pass

    class Sftp:
        def __init__(self, cancel):
            self.cancel = cancel

        def get(self, remote, local, callback=None):
            Path(local).write_bytes(b"x" * 1024)  # a partial file is on disk already
            self.cancel.set()  # the operator pressed Cancel; the socket is closed under us
            raise ParamikoSFTPError("Garbage packet received")

    cancel = threading.Event()
    browser = ParamikoSftpBrowser.__new__(ParamikoSftpBrowser)
    browser._client = None
    browser._sftp = Sftp(cancel)
    dest = tmp_path / "iq_20261103_081248_0001.wav"
    with pytest.raises(SftpError, match="cancelled"):
        browser.download("/mnt/1/remdata/20261103/iq_20261103_081248_0001.wav", dest, cancel=cancel, expected_size=750_047_812)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists(), "no leftover part file after a cancel"

    # The same transport error without a cancel is a failure, still without a leftover.
    quiet = threading.Event()
    browser._sftp = Sftp(threading.Event())
    with pytest.raises(SftpError, match="Download failed: Garbage packet received"):
        browser.download("/mnt/1/remdata/20261103/iq_x_0001.wav", dest, cancel=quiet)
    assert not dest.with_name(dest.name + ".part").exists()


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


def test_pruning_forgets_a_neighbours_file_adopted_before_the_sensor_named_its_own():
    from crfs_iq_recorder.file_watch import prune_file_watch

    now = datetime(2026, 11, 1, 8, 36, 0)
    state = FileWatchState()
    for offset in (0, 2):
        state, _ = update_file_watch(state, [_entry("file_083500_0001.wav", 500, now)], now=now + timedelta(seconds=offset))
    assert state.finalized == {"/mnt/1/remdata/20261031/file_083500_0001.wav"}
    # A growing neighbour would otherwise hold the watch open for a minute.
    state, _ = update_file_watch(state, [_entry("file_083500_0001.wav", 500, now), _entry("file_083500_0002.wav", 10, now)],
                                 now=now + timedelta(seconds=4))
    ours = {"/mnt/1/remdata/20261031/file_083625_0001.wav"}
    pruned, dropped = prune_file_watch(state, ours)
    assert sorted(dropped) == ["/mnt/1/remdata/20261031/file_083500_0001.wav", "/mnt/1/remdata/20261031/file_083500_0002.wav"]
    assert pruned.sizes == {} and pruned.finalized == set() and pruned.stable == {}
    assert state.finalized, "the original state is left untouched"
    same, none = prune_file_watch(pruned, ours)
    assert none == [] and same is pruned


def test_parts_named_by_the_sensor_are_waited_for_before_completion():
    """The stream named three parts; two have settled and hold "enough" bytes.
    The third is still flushing on the Node, so the capture is not complete yet."""
    now = datetime(2026, 11, 1, 8, 36, 0)
    state = FileWatchState()
    two = [_entry("file_083600_0001.wav", 1000, now), _entry("file_083600_0002.wav", 1000, now)]
    for offset in (0, 2):
        state, _ = update_file_watch(state, two, now=now + timedelta(seconds=offset))
    when = now + timedelta(seconds=3)
    common = dict(now=when, elapsed=True, elapsed_at=now, sensor_finished=True, expected_bytes=2000)
    assert capture_is_settled(state, announced_parts=2, **common)
    assert not capture_is_settled(state, announced_parts=3, **common)
    # The third part lands and settles: complete.
    three = two + [_entry("file_083600_0003.wav", 500, when)]
    for offset in (4, 6):
        state, _ = update_file_watch(state, three, now=now + timedelta(seconds=offset))
    assert capture_is_settled(state, announced_parts=3, now=now + timedelta(seconds=7), elapsed=True, elapsed_at=now,
                              sensor_finished=True, expected_bytes=2000)
    # A named part that never shows up cannot hold the application past the watch limit.
    stuck = FileWatchState()
    for offset in (0, 2):
        stuck, _ = update_file_watch(stuck, two, now=now + timedelta(seconds=offset))
    assert not capture_is_settled(stuck, announced_parts=3, now=now + timedelta(seconds=59), elapsed=True, elapsed_at=now,
                                  sensor_finished=True, expected_bytes=2000)
    assert capture_is_settled(stuck, announced_parts=3, now=now + timedelta(seconds=61), elapsed=True, elapsed_at=now,
                              sensor_finished=True, expected_bytes=2000)
