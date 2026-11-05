"""HTTP client for CRFS Node identity and EMP remote-recording POST.

Identity is read from the Node webpage JSON used by the sensor UI:
GET /api/node.json and GET /api/values/versions.json.
Recording still uses only POST /emp/. Connection tests use TCP plus GET /.
"""

from __future__ import annotations

import socket
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import requests
    from requests.auth import HTTPBasicAuth

from .constants import (
    CONNECTION_TEST_PATH,
    EMP_PATH,
    NODE_JSON_PATH,
    SOFTWARE_MANAGER_API_PATH,
    VERSIONS_JSON_PATH,
)
from .sanitizer import headers_without_auth, redact_text, redact_url
from .sensor_info import SensorInfo, as_json_object, blank_info, build_sensor_info
from .validation import ConnectionTarget

# Bound how much of a non-JSON / error body we keep in the GUI.
_MAX_BODY_CHARS = 16_384


def _response_socket(response: Any) -> Any:
    """The TCP socket under a streaming `requests` response, or None."""
    try:
        raw = response.raw
        conn = getattr(raw, "_connection", None) or getattr(raw, "connection", None)
        sock = getattr(conn, "sock", None)
        if sock is None:
            fp = getattr(raw, "_fp", None)
            inner = getattr(fp, "fp", None)
            sock = getattr(getattr(inner, "raw", None), "_sock", None)
        return sock
    except Exception:  # noqa: BLE001
        return None


def _close_quietly(closable: Any) -> None:
    try:
        closable.close()
    except Exception:  # noqa: BLE001
        pass


def connection_failure_reason(exc: BaseException) -> str:
    """Short, plain-language cause for a `requests` ConnectionError.

    urllib3 wraps the socket error in "HTTPConnectionPool(...): Max retries
    exceeded with url ..." even though this client never retries; the user
    needs the cause, not the wrapper.
    """
    text = str(exc)
    lowered = text.casefold()
    if "10061" in text or "connection refused" in lowered or "errno 111" in lowered:
        return "the sensor refused the connection (nothing is listening on that port)"
    if "11001" in text or "getaddrinfo" in lowered or "name or service not known" in lowered or "nodename" in lowered:
        return "the host name could not be resolved"
    if "10060" in text or "timed out" in lowered:
        return "the connection attempt timed out"
    if "10065" in text or "10051" in text or "unreachable" in lowered:
        return "the network reports the host as unreachable"
    if "10054" in text or "reset by peer" in lowered:
        return "the sensor closed the connection"
    if "remotedisconnected" in lowered or "connection aborted" in lowered:
        return "the sensor closed the connection without a reply"
    # Fall back to the innermost message without the pool/retry wrapper.
    inner = text.rsplit("Caused by", 1)[-1].strip(" :()") if "Caused by" in text else text
    return redact_text(inner)[:200] or "connection error"

# `requests` costs ~0.3 s to import and is only needed once the first HTTP call
# runs on a worker thread. Importing it here at module load would delay the
# main window. _ensure_requests() fills these names on first use.
requests: Any = None
HTTPBasicAuth: Any = None
RequestsConnectionError: Any = None
ConnectTimeout: Any = None
ReadTimeout: Any = None
SSLError: Any = None
Timeout: Any = None


def _ensure_requests() -> None:
    global requests, HTTPBasicAuth, RequestsConnectionError, ConnectTimeout, ReadTimeout, SSLError, Timeout
    if requests is not None:
        return
    import requests as _requests
    from requests.auth import HTTPBasicAuth as _HTTPBasicAuth
    from requests.exceptions import ConnectionError as _ConnectionError
    from requests.exceptions import ConnectTimeout as _ConnectTimeout
    from requests.exceptions import ReadTimeout as _ReadTimeout
    from requests.exceptions import SSLError as _SSLError
    from requests.exceptions import Timeout as _Timeout

    HTTPBasicAuth = _HTTPBasicAuth
    RequestsConnectionError = _ConnectionError
    ConnectTimeout = _ConnectTimeout
    ReadTimeout = _ReadTimeout
    SSLError = _SSLError
    Timeout = _Timeout
    requests = _requests


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
        auth: HTTPBasicAuth | None = None,
        timeout: float = 30.0,
    ) -> "RawResponse":
        ...


@dataclass
class SseEvent:
    """One Server-Sent Events block (`event:` name plus parsed `data:`)."""

    event: str
    data: Any
    received_s: float


# Why the stream ended. "finished" = the sensor said the recording is done.
STREAM_FINISHED = "finished"
STREAM_ERROR = "error"
STREAM_BUDGET = "budget"
STREAM_CANCELLED = "cancelled"
STREAM_CLOSED = "closed"
STREAM_LOST = "lost"

# EMP event names seen on RFeye Node firmware 2.25 (CRFS.MissionProcessor 1.11).
SSE_EVENT_MESSAGE = "message.MessageData"
SSE_EVENT_REMOTE_TIME = "time.RemoteTimeData"
# Synthetic event this client raises once the response headers have arrived.
SSE_EVENT_HTTP_ACCEPTED = "crfs_iq_recorder.http_accepted"
SSE_FINISHED_TEXT = "remote recording is now finished"
_SSE_ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL"}
# Noise the Node repeats several times a second; not worth a log line each.
_SSE_IGNORED_MESSAGE_FRAGMENTS = ("gps reference",)
_MAX_STREAM_EVENTS = 2000
# Distinct sensor messages kept per recording; repeats are counted, not stored.
_MAX_DISTINCT_MESSAGES = 50


def _with_repeat_count(line: str, count: int) -> str:
    return f"{line} (×{count})" if count > 1 else line


@dataclass
class StreamOptions:
    """How long to follow an event-stream reply and how to stop early."""

    budget_s: float
    cancel: Any = None  # threading.Event-like; set() ends the stream
    on_event: Any = None  # callable(SseEvent) -> bool (True = stop reading)
    read_timeout_s: float = 30.0


@dataclass
class RawResponse:
    status_code: int
    text: str
    headers: dict[str, str]
    elapsed_s: float
    url: str
    stream_events: list[SseEvent] | None = None
    stream_ended: str = ""


@dataclass
class HttpOutcome:
    kind: str
    title: str
    detail: str
    status_code: int | None = None
    url: str = ""
    elapsed_s: float = 0.0
    body_text: str = ""
    body_json: Any = None
    request_sent: bool = False
    outcome_unknown: bool = False
    demo: bool = False
    accepted: bool = False
    headers: dict[str, str] = field(default_factory=dict)
    # Filled from the EMP event stream when the sensor replies with text/event-stream.
    stream_ended: str = ""
    sensor_finished: bool = False
    sensor_parts: list[str] = field(default_factory=list)
    sensor_file_dir: str = ""
    sensor_task_id: str = ""
    sensor_messages: list[str] = field(default_factory=list)
    # First ERROR-level message of the stream, if any. Set even when the sensor
    # had already reported the recording finished (then `kind` stays "accepted").
    sensor_error: str = ""

    def summary(self) -> str:
        parts = [self.title]
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.demo:
            parts.append("demo")
        return " — ".join(parts)


def parse_sse_blocks(buffer: str) -> tuple[list[tuple[str, str]], str]:
    """Split complete SSE blocks out of `buffer`; return (event, data) pairs and the remainder."""
    blocks: list[tuple[str, str]] = []
    text = buffer.replace("\r\n", "\n").replace("\r", "\n")
    while "\n\n" in text:
        block, text = text.split("\n\n", 1)
        name = "message"
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":"):
                continue  # comment / keepalive
            if line.startswith("event:"):
                name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip(" "))
        if data_lines:
            blocks.append((name, "\n".join(data_lines)))
    return blocks, text


def sse_event_from_block(name: str, data: str, received_s: float) -> SseEvent:
    import json

    parsed: Any = data
    try:
        parsed = json.loads(data)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return SseEvent(event=name, data=parsed, received_s=received_s)


def _message_items(event: SseEvent) -> list[dict]:
    if event.event != SSE_EVENT_MESSAGE:
        return []
    items = event.data if isinstance(event.data, list) else [event.data]
    return [item for item in items if isinstance(item, dict)]


_INTERNAL_CLOCK_NOTE = (
    " (the Node's ncpd.conf sets real_time_clock and reference_clock to \"internal\", so time stamps "
    "come from its own clock, not GPS; the recording is not affected)"
)


def sensor_message_note(text: str) -> str:
    """A short explanation appended to a known sensor message, or ''.

    "Using internal clock because of ncpd.conf settings" is repeated several
    times a second by the Node 100-18 for the whole capture. Read on the sensor
    (read-only): `/etc/node/ncpd/ncpd.conf` has `"real_time_clock":"internal"`
    and `"reference_clock":"internal"`; the files are complete and the capture
    finishes normally, so the message is a configuration notice, not a fault.
    """
    lowered = (text or "").casefold()
    if "internal clock" in lowered and "ncpd.conf" in lowered:
        return _INTERNAL_CLOCK_NOTE
    return ""


def sse_message_is_noise(item: dict) -> bool:
    text = str(item.get("message", "")).casefold()
    return any(fragment in text for fragment in _SSE_IGNORED_MESSAGE_FRAGMENTS)


def sse_message_is_finished(item: dict) -> bool:
    return SSE_FINISHED_TEXT in str(item.get("message", "")).casefold()


def sse_message_is_error(item: dict) -> bool:
    return str(item.get("level", "")).upper() in _SSE_ERROR_LEVELS


def sse_part_filename(event: SseEvent) -> tuple[str, str]:
    """(file_dir, filename) from a time.RemoteTimeData event, or ('', '')."""
    if event.event != SSE_EVENT_REMOTE_TIME:
        return "", ""
    items = event.data if isinstance(event.data, list) else [event.data]
    for item in items:
        if isinstance(item, dict) and item.get("first_filename"):
            return str(item.get("file_dir") or ""), str(item.get("first_filename") or "")
    return "", ""


def sse_task_id(event: SseEvent) -> str:
    if event.event != SSE_EVENT_REMOTE_TIME:
        return ""
    items = event.data if isinstance(event.data, list) else [event.data]
    for item in items:
        if isinstance(item, dict) and item.get("task_id"):
            return str(item["task_id"])
    return ""


# Slowest rate at which a Node is assumed to write a capture to its disk when
# sizing the stream budget. Measured on a Node 100-18 (2.25-325): the 1.0 GB of
# a 2 s capture at 125 MS/s reached the disk at 13-20 MB/s on an idle Node and
# at about 10 MB/s while another file was read from it over SFTP.
NODE_SLOWEST_WRITE_BYTES_PER_S = 4_000_000


def recording_stream_budget_s(duration_s: float, capture_bytes: int = 0) -> float:
    """How long to keep the sensor's event stream open after the POST.

    Hardware measurements (RFeye Node 40-8 and 100-18, firmware 2.25): the
    completion message arrives about 1.5-2.5 s after the nominal end of the
    capture, but that only ends the radio capture. The Node then keeps writing
    the samples to its disk, and when the client disconnects first it ends the
    mission and closes the WAVE file at whatever is on the disk: a well-formed
    file that is simply shorter. A 2 s capture at 125 MS/s (1.0 GB) needs over
    a minute to reach the disk while other files are read from the Node, so
    the budget allows for writing `capture_bytes` at
    `NODE_SLOWEST_WRITE_BYTES_PER_S`. The GUI holds
    the stream until the files are final and then closes it; this budget is
    the safety net when that confirmation never comes.
    """
    seconds = max(float(duration_s), 0.0)
    writing = max(int(capture_bytes or 0), 0) / NODE_SLOWEST_WRITE_BYTES_PER_S
    return seconds + max(60.0, seconds) + writing


class RequestsTransport:
    """One-shot requests calls. No retries (a timed-out POST must not be replayed)."""

    def __init__(self, session: requests.Session | None = None) -> None:
        # The session is created on first request so that `requests` is
        # imported on the worker thread, not while the window is opening.
        self._session = session
        self._active_response: Any = None
        self._aborted = False

    def _get_session(self) -> requests.Session:
        if self._session is None:
            _ensure_requests()
            self._session = requests.Session()
        return self._session

    def abort(self) -> None:
        """Stop an event-stream read that another thread is blocked on.

        Shutting the socket down makes the blocked `recv()` return at once; the
        reading thread then closes the response itself. Closing the response
        from here instead stalled the caller for over a second on Windows.
        """
        self._aborted = True
        response = self._active_response
        if response is None:
            return
        sock = _response_socket(response)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        else:
            threading.Thread(target=_close_quietly, args=(response,), daemon=True, name="emp-stream-close").start()

    def close(self) -> None:
        self.abort()
        session = self._session
        self._session = None
        if session is None:
            return
        if self._active_response is not None:
            # A reader is still unwinding; let it finish before the pool is torn down.
            threading.Thread(target=_close_quietly, args=(session,), daemon=True, name="emp-session-close").start()
            return
        _close_quietly(session)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
        auth: HTTPBasicAuth | None = None,
        timeout: float = 30.0,
        stream: StreamOptions | None = None,
    ) -> RawResponse:
        if stream is None:
            response = self._get_session().request(
                method=method,
                url=url,
                headers=headers,
                json=json_body,
                auth=auth,
                timeout=timeout,
                allow_redirects=True,
            )
            return RawResponse(
                status_code=int(response.status_code),
                text=response.text or "",
                headers={str(k): str(v) for k, v in response.headers.items()},
                elapsed_s=float(response.elapsed.total_seconds()) if response.elapsed else 0.0,
                url=str(response.url),
            )
        return self._request_streaming(method, url, headers=headers, json_body=json_body, auth=auth,
                                       timeout=timeout, options=stream)

    def _request_streaming(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None,
        json_body: dict | None,
        auth: HTTPBasicAuth | None,
        timeout: float,
        options: StreamOptions,
    ) -> RawResponse:
        """Fetch headers, then follow a text/event-stream body until it is done.

        The EMP endpoint on RFeye Nodes answers the recording POST with an
        endless Server-Sent Events stream (keepalive every 10 s). A plain
        `response.text` read never returns, so the body is consumed block by
        block until the caller's `on_event` says stop, the time budget runs
        out, the stream is cancelled, or the sensor closes it.
        """
        import time

        self._aborted = False
        read_timeout = max(1.0, float(options.read_timeout_s or timeout))
        started = time.monotonic()
        response = self._get_session().request(
            method=method,
            url=url,
            headers=headers,
            json=json_body,
            auth=auth,
            timeout=(timeout, read_timeout),
            allow_redirects=True,
            stream=True,
        )
        content_type = str(response.headers.get("Content-Type", "")).casefold()
        if not content_type.startswith("text/event-stream"):
            try:
                text = response.text or ""
            finally:
                response.close()
            return RawResponse(
                status_code=int(response.status_code),
                text=text,
                headers={str(k): str(v) for k, v in response.headers.items()},
                elapsed_s=float(response.elapsed.total_seconds()) if response.elapsed else 0.0,
                url=str(response.url),
            )
        self._active_response = response
        events: list[SseEvent] = []
        lines: list[str] = []
        ended = STREAM_CLOSED
        buffer = ""
        if options.on_event is not None:
            try:
                options.on_event(
                    SseEvent(
                        event=SSE_EVENT_HTTP_ACCEPTED,
                        data={"status": int(response.status_code)},
                        received_s=time.monotonic() - started,
                    )
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            for chunk in response.iter_content(chunk_size=None, decode_unicode=False):
                if self._aborted or (options.cancel is not None and options.cancel.is_set()):
                    ended = STREAM_CANCELLED
                    break
                if chunk:
                    buffer += chunk.decode("utf-8", errors="replace")
                    blocks, buffer = parse_sse_blocks(buffer)
                    stop = False
                    for name, data in blocks:
                        event = sse_event_from_block(name, data, time.monotonic() - started)
                        if len(events) < _MAX_STREAM_EVENTS:
                            events.append(event)
                            lines.append(f"{name}: {data}")
                        if options.on_event is not None and options.on_event(event):
                            stop = True
                            break
                    if stop:
                        ended = STREAM_FINISHED
                        break
                if time.monotonic() - started >= options.budget_s:
                    ended = STREAM_BUDGET
                    break
        except Exception as exc:  # noqa: BLE001 - classified below
            if self._aborted or (options.cancel is not None and options.cancel.is_set()):
                ended = STREAM_CANCELLED
            else:
                ended = f"{STREAM_LOST}: {type(exc).__name__}"
        finally:
            self._active_response = None
            try:
                response.close()
            except Exception:
                pass
        return RawResponse(
            status_code=int(response.status_code),
            text="\n".join(lines),
            headers={str(k): str(v) for k, v in response.headers.items()},
            elapsed_s=float(response.elapsed.total_seconds()) if response.elapsed else 0.0,
            url=str(response.url),
            stream_events=events,
            stream_ended=ended,
        )


def emp_url(target: ConnectionTarget) -> str:
    return f"{target.origin}{EMP_PATH}"


def webpage_url(target: ConnectionTarget) -> str:
    return f"{target.origin}{CONNECTION_TEST_PATH}"


def _clip(text: str) -> str:
    if len(text) <= _MAX_BODY_CHARS:
        return text
    return text[:_MAX_BODY_CHARS] + f"\n… [truncated, {len(text)} characters total]"


def _parse_body(text: str) -> tuple[str, Any]:
    clipped = _clip(text)
    raw = (text or "").strip()
    if not raw:
        return clipped, None
    try:
        import json

        return clipped, json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return clipped, None


def classify_http_error(status_code: int) -> tuple[str, str]:
    if status_code == 401:
        return (
            "authentication_error",
            "Authentication failed (HTTP 401). Check username and password. "
            "The Node webpage default on some Core firmware is different from this app's EMP defaults.",
        )
    if status_code == 403:
        return ("authentication_error", "Access forbidden (HTTP 403). The account may lack EMP permission.")
    if status_code == 400:
        return ("validation_error", "The sensor rejected the request as invalid (HTTP 400).")
    if status_code == 404:
        return (
            "http_error",
            "HTTP 404 — path not found. Confirm the sensor serves EMP at /emp/ on this host and port.",
        )
    if status_code == 409:
        return ("http_error", "HTTP 409 conflict. The sensor may already be busy (IQ vs sweep exclusivity).")
    if status_code == 422:
        return ("validation_error", "The sensor reported a validation error (HTTP 422).")
    if 500 <= status_code <= 599:
        return ("server_error", f"Sensor HTTP {status_code}.")
    return ("http_error", f"HTTP {status_code}.")


def tcp_probe(host: str, port: int, timeout_s: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, f"TCP connected to {host}:{port}."
    except TimeoutError:
        return False, f"TCP timeout connecting to {host}:{port}."
    except OSError as exc:
        return False, f"TCP connection to {host}:{port} failed: {exc}."


class EmpClient:
    def __init__(
        self,
        transport: Transport | None = None,
        *,
        demo: bool = False,
        tcp_probe_fn=tcp_probe,
    ) -> None:
        self.transport = transport or RequestsTransport()
        self.demo = demo
        self._tcp_probe = tcp_probe_fn

    def close(self) -> None:
        closer = getattr(self.transport, "close", None)
        if closer is not None:
            closer()

    def abort(self) -> None:
        """Interrupt a recording POST whose event stream is still open."""
        aborter = getattr(self.transport, "abort", None)
        if aborter is not None:
            aborter()

    def test_connection(self, target: ConnectionTarget, password: str) -> HttpOutcome:
        """Reachability + GET /. Never POSTs a recording body."""
        _ensure_requests()
        if self.demo:
            tcp_ok, tcp_msg = True, "Demo mode: TCP probe skipped; no sensor was contacted."
        else:
            tcp_ok, tcp_msg = self._tcp_probe(target.host, target.port, min(target.timeout_s, 10.0))
        if not tcp_ok:
            return HttpOutcome(
                kind="connection_failure",
                title="Sensor not reachable",
                detail=(
                    f"{tcp_msg} This check did not verify HTTP Basic Auth or EMP recording capability."
                ),
                url=target.origin,
                request_sent=False,
                demo=self.demo,
            )

        url = webpage_url(target)
        auth = HTTPBasicAuth(target.username, password)
        try:
            raw = self.transport.request(
                "GET",
                url,
                headers={"Accept": "text/html, application/json;q=0.9, */*;q=0.8"},
                auth=auth,
                timeout=target.timeout_s,
            )
        except ConnectTimeout:
            return HttpOutcome(
                kind="timeout",
                title="HTTP connect timeout",
                detail=(
                    f"TCP to {target.host}:{target.port} succeeded, but the HTTP GET {redact_url(url)} "
                    "timed out before a connection completed. Auth and EMP recording were not verified."
                ),
                url=redact_url(url),
                request_sent=False,
                demo=self.demo,
            )
        except (ReadTimeout, Timeout):
            return HttpOutcome(
                kind="timeout",
                title="HTTP read timeout",
                detail=(
                    f"GET {redact_url(url)} timed out after the request was sent. "
                    "The Node webpage did not finish responding. EMP recording was not verified."
                ),
                url=redact_url(url),
                request_sent=True,
                demo=self.demo,
            )
        except SSLError as exc:
            return HttpOutcome(
                kind="connection_failure",
                title="TLS error",
                detail=f"{exc}. Try HTTP if this Node is not serving HTTPS, or the reverse. EMP was not verified.",
                url=redact_url(url),
                request_sent=False,
                demo=self.demo,
            )
        except RequestsConnectionError as exc:
            return HttpOutcome(
                kind="connection_failure",
                title="HTTP connection failed",
                detail=f"TCP succeeded, HTTP GET failed: {exc}. EMP recording was not verified.",
                url=redact_url(url),
                request_sent=False,
                demo=self.demo,
            )

        body_text, body_json = _parse_body(raw.text)
        kind, extra = classify_http_error(raw.status_code) if raw.status_code >= 400 else ("ok", "")
        if raw.status_code < 400:
            title = "Host reachable over HTTP"
            detail = (
                f"{tcp_msg} GET {redact_url(raw.url or url)} returned HTTP {raw.status_code}. "
                "This is the Node webpage path (/), not an EMP recording call. "
                "It does not prove that POST /emp/ will accept credentials or start a recording."
            )
            accepted = True
        else:
            title = "HTTP reachable, request not authorized or not accepted"
            detail = (
                f"{tcp_msg} {extra} "
                "Recording was not started. Webpage auth may differ from EMP auth."
            )
            accepted = False
        return HttpOutcome(
            kind=kind if raw.status_code >= 400 else "reachable",
            title=title,
            detail=detail,
            status_code=raw.status_code,
            url=redact_url(raw.url or url),
            elapsed_s=raw.elapsed_s,
            body_text=redact_text(body_text),
            body_json=body_json,
            request_sent=True,
            demo=self.demo,
            accepted=accepted,
            headers=headers_without_auth(raw.headers),
        )

    def _get(
        self,
        target: ConnectionTarget,
        password: str,
        path: str,
        *,
        timeout: float | None = None,
    ) -> RawResponse:
        _ensure_requests()
        url = f"{target.origin}{path}"
        auth = HTTPBasicAuth(target.username, password)
        return self.transport.request(
            "GET",
            url,
            headers={"Accept": "application/json, text/html;q=0.8"},
            auth=auth,
            timeout=timeout if timeout is not None else target.timeout_s,
        )

    def _json_body(self, raw: RawResponse) -> dict | None:
        _text, parsed = _parse_body(raw.text)
        return as_json_object(parsed, raw.text)

    def _identity_http_error(self, target: ConnectionTarget, raw: RawResponse) -> SensorInfo:
        host = target.host
        if raw.status_code == 401:
            status = "Authentication failed"
        elif raw.status_code == 403:
            status = "Access forbidden"
        elif raw.status_code == 404:
            status = "Identity API not found"
        else:
            status = f"HTTP {raw.status_code}"
        return blank_info(host=host, status=status, demo=self.demo)

    def probe_sensor(
        self,
        target: ConnectionTarget,
        password: str,
        *,
        timeout_s: float = 4.0,
    ) -> tuple[bool, str]:
        """Authenticated GET /api/node.json. Read-only; never POSTs."""
        _ensure_requests()
        timeout_s = float(timeout_s or 4.0)
        try:
            raw = self._get(target, password, NODE_JSON_PATH, timeout=timeout_s)
        except ConnectTimeout:
            return False, "HTTP connect timeout"
        except (ReadTimeout, Timeout):
            return False, "HTTP read timeout"
        except SSLError:
            return False, "TLS error"
        except RequestsConnectionError:
            return False, "HTTP connection failed"
        except Exception as exc:
            return False, str(exc) or "Sensor check failed"
        if raw.status_code < 400:
            return True, "ok"
        if raw.status_code in (401, 403):
            return False, "Authentication failed"
        return False, f"HTTP {raw.status_code}"

    def fetch_sensor_info(self, target: ConnectionTarget, password: str) -> SensorInfo:
        """Read model, firmware, serial, and reachability from the Node webpage APIs."""
        _ensure_requests()
        host = target.host
        if self.demo:
            tcp_ok = True
        else:
            tcp_ok, _tcp_msg = self._tcp_probe(target.host, target.port, min(target.timeout_s, 10.0))
        if not tcp_ok:
            return blank_info(host=host, status="Unreachable", demo=self.demo)

        try:
            node_raw = self._get(target, password, NODE_JSON_PATH)
        except ConnectTimeout:
            return blank_info(host=host, status="Unreachable", demo=self.demo)
        except (ReadTimeout, Timeout):
            return blank_info(host=host, status="Timed out", demo=self.demo)
        except SSLError:
            return blank_info(host=host, status="TLS error", demo=self.demo)
        except RequestsConnectionError:
            return blank_info(host=host, status="Unreachable", demo=self.demo)

        node_payload = None
        manager_payload = None
        if node_raw.status_code < 400:
            node_payload = self._json_body(node_raw)
        elif node_raw.status_code in (401, 403):
            return self._identity_http_error(target, node_raw)
        else:
            try:
                mgr_raw = self._get(target, password, SOFTWARE_MANAGER_API_PATH)
            except Exception:
                return self._identity_http_error(target, node_raw)
            if mgr_raw.status_code >= 400:
                return self._identity_http_error(target, node_raw)
            manager_payload = self._json_body(mgr_raw)

        versions_payload = None
        try:
            versions_raw = self._get(target, password, VERSIONS_JSON_PATH)
            if versions_raw.status_code < 400:
                versions_payload = self._json_body(versions_raw)
        except Exception:
            versions_payload = None

        status = "Demo" if self.demo else "Connected"
        return build_sensor_info(
            host=host,
            node_payload=node_payload,
            versions_payload=versions_payload,
            manager_payload=manager_payload,
            status=status,
            demo=self.demo,
        )

    def start_recording(
        self,
        target: ConnectionTarget,
        password: str,
        payload: dict[str, Any],
        *,
        cancel: Any = None,
        on_event: Any = None,
        stream_budget_s: float | None = None,
        stop_on_finished: bool = True,
    ) -> HttpOutcome:
        """POST the recording and, when the sensor answers with an event stream,
        follow it until the sensor reports that the recording finished.

        `on_event(SseEvent)` is called on the worker thread for every event.
        `cancel` (threading.Event) ends the wait early. Nothing is ever retried.
        With `stop_on_finished=False` the stream stays open after the sensor's
        completion message until `cancel` is set or the budget runs out: the
        Node drops the mission the moment its client disconnects, so the GUI
        keeps the connection until the WAVE parts are confirmed on disk.
        """
        _ensure_requests()
        url = emp_url(target)
        auth = HTTPBasicAuth(target.username, password)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/plain;q=0.8"}
        duration_s = _payload_duration(payload)
        budget = (
            stream_budget_s
            if stream_budget_s is not None
            else recording_stream_budget_s(duration_s, _payload_largest_capture_bytes(payload))
        )
        parts: list[str] = []
        file_dir = ""
        task_echo = ""
        # Distinct messages in order of first appearance, with a repeat count:
        # the Node 100-18 repeats "Using internal clock because of ncpd.conf
        # settings" several times a second, which used to fill the 50-message
        # window and hide the error that actually ended the stream.
        messages: list[str] = []
        repeats: dict[str, int] = {}
        state = {"finished": False, "error": False, "error_text": ""}

        def _watch(event: SseEvent) -> bool:
            nonlocal file_dir, task_echo
            directory, name = sse_part_filename(event)
            if name:
                file_dir = directory or file_dir
                if name not in parts:
                    parts.append(name)
            echo = sse_task_id(event)
            if echo:
                task_echo = echo
            stop = False
            for item in _message_items(event):
                if sse_message_is_noise(item):
                    continue
                text = redact_text(str(item.get("message", "")))
                level = str(item.get("level", "")).upper()
                line = f"{level}: {text}" if level else text
                if line in repeats:
                    repeats[line] += 1
                elif len(messages) < _MAX_DISTINCT_MESSAGES:
                    messages.append(line)
                    repeats[line] = 1
                if sse_message_is_finished(item):
                    state["finished"] = True
                    stop = stop or stop_on_finished
                elif sse_message_is_error(item):
                    state["error"] = True
                    if not state["error_text"]:
                        state["error_text"] = line
                    stop = True
            if on_event is not None:
                try:
                    on_event(event)
                except Exception:  # noqa: BLE001 - a UI callback must not kill the stream
                    pass
            return stop

        stream = StreamOptions(
            budget_s=budget,
            cancel=cancel,
            on_event=_watch,
            read_timeout_s=max(target.timeout_s, 15.0),
        )
        try:
            raw = self._request_with_optional_stream(
                url,
                headers=headers,
                payload=payload,
                auth=auth,
                timeout=target.timeout_s,
                stream=stream,
            )
        except ConnectTimeout:
            return HttpOutcome(
                kind="timeout",
                title="Connect timeout — recording POST not sent",
                detail=(
                    f"Connecting to {redact_url(url)} timed out. The recording command was likely not "
                    "transmitted. It was not retried."
                ),
                url=redact_url(url),
                request_sent=False,
                outcome_unknown=False,
                demo=self.demo,
            )
        except (ReadTimeout, Timeout):
            return HttpOutcome(
                kind="timeout_after_send",
                title="Timeout after transmission — recording outcome unknown",
                detail=(
                    f"POST {redact_url(url)} timed out waiting for a response. "
                    "The request may already have been accepted by the sensor. "
                    "This client will not retry automatically."
                ),
                url=redact_url(url),
                request_sent=True,
                outcome_unknown=True,
                demo=self.demo,
            )
        except SSLError as exc:
            return HttpOutcome(
                kind="connection_failure",
                title="TLS error",
                detail=str(exc),
                url=redact_url(url),
                request_sent=False,
                demo=self.demo,
            )
        except RequestsConnectionError as exc:
            return HttpOutcome(
                kind="connection_failure",
                title="Connection failed",
                detail=(
                    f"Could not connect to {redact_url(url)}: {connection_failure_reason(exc)}. "
                    "The recording request was not sent and was not retried."
                ),
                url=redact_url(url),
                request_sent=False,
                demo=self.demo,
            )

        body_text, body_json = _parse_body(raw.text)
        if raw.status_code >= 400:
            kind, extra = classify_http_error(raw.status_code)
            non_json_note = ""
            if (raw.text or "").strip() and body_json is None:
                non_json_note = " Response body is not JSON."
            return HttpOutcome(
                kind=kind,
                title="Recording request not accepted",
                detail=extra + non_json_note,
                status_code=raw.status_code,
                url=redact_url(raw.url or url),
                elapsed_s=raw.elapsed_s,
                body_text=redact_text(body_text),
                body_json=body_json,
                request_sent=True,
                demo=self.demo,
                accepted=False,
                headers=headers_without_auth(raw.headers),
            )

        if raw.stream_events is not None:
            return self._stream_outcome(
                raw,
                url=url,
                finished=state["finished"],
                error=state["error"],
                error_text=str(state["error_text"]),
                parts=parts,
                file_dir=file_dir,
                task_echo=task_echo,
                messages=[_with_repeat_count(line, repeats.get(line, 1)) for line in messages],
            )

        non_json_note = ""
        if (raw.text or "").strip() and body_json is None:
            non_json_note = (
                " The response is not JSON, so this client cannot interpret sensor-specific fields."
            )
        return HttpOutcome(
            kind="accepted",
            title="HTTP request accepted — recording completion not confirmed",
            detail=(
                f"POST {redact_url(raw.url or url)} returned HTTP {raw.status_code}. "
                "That is request acceptance at the HTTP layer, not proof that the IQ file "
                "finished writing on the sensor or external storage."
                + non_json_note
            ),
            status_code=raw.status_code,
            url=redact_url(raw.url or url),
            elapsed_s=raw.elapsed_s,
            body_text=redact_text(body_text),
            body_json=body_json,
            request_sent=True,
            demo=self.demo,
            accepted=True,
            headers=headers_without_auth(raw.headers),
        )

    def _request_with_optional_stream(
        self,
        url: str,
        *,
        headers: dict[str, str],
        payload: dict[str, Any],
        auth: Any,
        timeout: float,
        stream: StreamOptions,
    ) -> RawResponse:
        """Pass `stream=` only to transports that understand it (fakes in tests may not)."""
        try:
            return self.transport.request(
                "POST",
                url,
                headers=headers,
                json_body=payload,
                auth=auth,
                timeout=timeout,
                stream=stream,
            )
        except TypeError as exc:
            if "stream" not in str(exc):
                raise
            return self.transport.request(
                "POST",
                url,
                headers=headers,
                json_body=payload,
                auth=auth,
                timeout=timeout,
            )

    def _stream_outcome(
        self,
        raw: RawResponse,
        *,
        url: str,
        finished: bool,
        error: bool,
        error_text: str = "",
        parts: list[str],
        file_dir: str,
        task_echo: str,
        messages: list[str],
    ) -> HttpOutcome:
        shown_url = redact_url(raw.url or url)
        error_line = error_text or (messages[-1] if messages else "The sensor reported an error.")
        common = dict(
            status_code=raw.status_code,
            url=shown_url,
            elapsed_s=raw.elapsed_s,
            body_text=redact_text(_clip(raw.text)),
            request_sent=True,
            demo=self.demo,
            headers=headers_without_auth(raw.headers),
            stream_ended=raw.stream_ended,
            sensor_finished=finished,
            sensor_parts=list(parts),
            sensor_file_dir=file_dir,
            sensor_task_id=task_echo,
            sensor_messages=list(messages),
            sensor_error=error_line if error else "",
        )
        if error and not finished:
            return HttpOutcome(
                kind="sensor_error",
                title="Sensor reported an error for this recording",
                detail=f"POST {shown_url} was accepted (HTTP {raw.status_code}) but the sensor reported: {error_line}",
                accepted=False,
                outcome_unknown=False,
                **common,
            )
        if finished:
            # An ERROR that arrives after "Remote recording is now finished" is
            # about the mission the sensor keeps open while this client holds
            # the stream (seen on the Node 100-18: "Unable to schedule the
            # command" about 45 s after the end of the capture), not about the
            # recording, whose file is already on the disk. It is reported, and
            # the capture is still confirmed from the files.
            count = len(parts)
            files = f"{count} file(s): {', '.join(parts)}" if parts else "no file names were reported"
            late = (
                f" After that the sensor also reported: {error_line}. The recording itself finished before "
                "this message; it is confirmed from the files." if error else ""
            )
            return HttpOutcome(
                kind="accepted",
                title="Sensor reported the recording finished",
                detail=(
                    f"POST {shown_url} returned HTTP {raw.status_code} and the sensor's event stream "
                    f"reported 'Remote recording is now finished' ({files}). Files may still be "
                    "flushing to the sensor disk." + late
                ),
                accepted=True,
                **common,
            )
        if raw.stream_ended.startswith(STREAM_LOST):
            reason = "the connection to the sensor dropped"
        elif raw.stream_ended == STREAM_CANCELLED:
            reason = "this application stopped waiting"
        elif raw.stream_ended == STREAM_BUDGET:
            reason = "no completion message arrived within the time budget"
        else:
            reason = "the sensor closed the stream"
        return HttpOutcome(
            kind="accepted",
            title="HTTP request accepted — recording completion not confirmed",
            detail=(
                f"POST {shown_url} returned HTTP {raw.status_code}; {reason} before the sensor "
                "reported completion. Completion is confirmed from the files instead."
            ),
            accepted=True,
            outcome_unknown=not raw.stream_ended == STREAM_CANCELLED,
            **common,
        )


    # ------------------------------------------------------------------ mission list
    def list_missions(self, target: ConnectionTarget, password: str, *, timeout_s: float = 6.0) -> MissionList:
        """Read-only GET of the EMP mission list (the page behind 'EMP Status').

        On RFeye Node firmware 2.25 `/emp/missions` lists every open EMP
        mission; a remote recording appears as "EMP Remote Recording
        [Center:… MHz,Span:… MHz]" from the POST until the client that
        started it disconnects (measured on the Node 40-8: gone within 2 s
        of the disconnect). Never POSTs.
        """
        _ensure_requests()
        if self.demo:
            return MissionList(available=True, missions=[], recording=[])
        try:
            raw = self._get(target, password, EMP_MISSIONS_PATH, timeout=float(timeout_s or 6.0))
        except Exception as exc:  # noqa: BLE001 - the caller decides what an unanswered probe means
            if RequestsConnectionError is not None and isinstance(exc, RequestsConnectionError):
                reason = connection_failure_reason(exc)
            else:
                reason = redact_text(str(exc)) or type(exc).__name__
            return MissionList(available=False, error=reason)
        if raw.status_code >= 400:
            return MissionList(available=False, error=f"HTTP {raw.status_code}")
        missions = parse_emp_missions(raw.text)
        return MissionList(available=True, missions=missions, recording=remote_recording_missions(missions))

    # ------------------------------------------------------------------ sensor clock
    def fetch_sensor_clock(self, target: ConnectionTarget, password: str, *, timeout_s: float = 6.0) -> "SensorClock":
        """Read-only GET of the Node's status page values: its system time and time source.

        Firmware 2.25 answers `/api/values/status.json` with
        ``values["02-system-time"] = "2026-11-04 17:24:51+0000"`` (UTC, with
        offset) and ``values["03-time-source"]`` ("NTP", "GPS", ...). The HTTP
        Date header is the fallback. The offset is sensor minus computer,
        measured against the middle of the request, so a slow link does not
        read as clock drift. Never POSTs.
        """
        _ensure_requests()
        from datetime import datetime, timezone

        if self.demo:
            now = datetime.now(timezone.utc)
            return SensorClock(available=True, sensor_utc=now, pc_utc=now, offset_s=0.0, source="demo", time_source="demo")
        sent = datetime.now(timezone.utc)
        try:
            raw = self._get(target, password, STATUS_VALUES_PATH, timeout=float(timeout_s or 6.0))
        except Exception as exc:  # noqa: BLE001 - the caller reports "unknown"
            if RequestsConnectionError is not None and isinstance(exc, RequestsConnectionError):
                reason = connection_failure_reason(exc)
            else:
                reason = redact_text(str(exc)) or type(exc).__name__
            return SensorClock(available=False, error=reason)
        received = datetime.now(timezone.utc)
        middle = sent + (received - sent) / 2
        if raw.status_code >= 400:
            return SensorClock(available=False, error=f"HTTP {raw.status_code}")
        values = {}
        body = self._json_body(raw)
        if isinstance(body, dict) and isinstance(body.get("values"), dict):
            values = body["values"]
        warnings = []
        if isinstance(body, dict):
            for item in body.get("pageErrors") or []:
                if isinstance(item, dict) and item.get("message"):
                    warnings.append(str(item["message"]))
        system_time = parse_sensor_time(values.get("02-system-time"))
        time_source = str(values.get("03-time-source") or "").strip()
        info = values.get("04-time-info") if isinstance(values.get("04-time-info"), dict) else {}
        fix = str(info.get("02-time-fix") or "").strip()
        satellites = str(info.get("03-time-satellites") or "").strip()
        gnss = ""
        if info:
            gnss_source = str(info.get("01-time-source") or "").strip()
            has_fix = fix not in {"", "\u2718", "x", "X", "no", "No"}
            gnss = f"{gnss_source or 'GNSS'}: {'time fix' if has_fix else 'no time fix'}" + (f", {satellites} satellites" if satellites else "")
        if system_time is not None:
            return SensorClock(
                available=True,
                sensor_utc=system_time,
                pc_utc=middle,
                offset_s=round((system_time - middle).total_seconds(), 3),
                source="status page system time (UTC)",
                time_source=time_source,
                gnss_status=gnss,
                warnings=warnings,
            )
        header_time = parse_http_date((raw.headers or {}).get("Date") or (raw.headers or {}).get("date"))
        if header_time is not None:
            return SensorClock(
                available=True,
                sensor_utc=header_time,
                pc_utc=middle,
                offset_s=round((header_time - middle).total_seconds(), 3),
                source="HTTP Date header (whole seconds)",
                time_source=time_source,
                gnss_status=gnss,
                warnings=warnings,
            )
        return SensorClock(available=False, error="the status page has no system time", time_source=time_source, gnss_status=gnss, warnings=warnings)


EMP_MISSIONS_PATH = "/emp/missions"
STATUS_VALUES_PATH = "/api/values/status.json"


@dataclass
class SensorClock:
    """The sensor's clock as read from its API, next to the computer's at the same moment."""

    available: bool
    sensor_utc: Any = None  # datetime (UTC) or None
    pc_utc: Any = None
    offset_s: float | None = None  # sensor minus computer, seconds
    source: str = ""
    time_source: str = ""  # what the sensor says it disciplines its clock with (NTP, GPS, ...)
    gnss_status: str = ""
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "sensor_utc": self.sensor_utc.isoformat() if self.sensor_utc is not None else None,
            "pc_utc": self.pc_utc.isoformat() if self.pc_utc is not None else None,
            "offset_s": self.offset_s,
            "source": self.source,
            "time_source": self.time_source,
            "gnss_status": self.gnss_status,
            "warnings": list(self.warnings),
            "error": self.error,
        }


def parse_sensor_time(value: Any):
    """'2026-11-04 17:24:51+0000' (or ISO 8601) as an aware UTC datetime; None when unreadable."""
    from datetime import datetime, timezone

    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("T", " ")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+0000"
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_http_date(value: Any):
    from datetime import timezone
    from email.utils import parsedate_to_datetime

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class MissionList:
    """What the sensor's EMP mission page showed, or why it could not be read."""

    available: bool
    missions: list[str] = field(default_factory=list)
    recording: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def recording_open(self) -> bool:
        return bool(self.recording)


def parse_emp_missions(html: str) -> list[str]:
    """Mission texts from the EMP missions fragment ('<div class="item">…</div>' per mission)."""
    import re

    text = str(html or "")
    items = re.findall(r"<div[^>]*class=\"[^\"]*\bitem\b[^\"]*\"[^>]*>(.*?)</div>", text, flags=re.IGNORECASE | re.DOTALL)
    if not items and text.strip():
        stripped = re.sub(r"<[^>]+>", "\n", text)
        items = [line for line in stripped.splitlines()]
    out: list[str] = []
    for item in items:
        clean = re.sub(r"<[^>]+>", " ", item)
        clean = re.sub(r"\s+", " ", clean).strip()
        if clean:
            out.append(clean)
    return out


def remote_recording_missions(missions: list[str]) -> list[str]:
    """The missions that are remote recordings (ours or another client's)."""
    return [text for text in missions if "remote recording" in text.casefold()]


def _payload_duration(payload: dict[str, Any]) -> float:
    try:
        scans = payload.get("remote_recording_scans") or []
        return float(scans[0].get("duration", 0.0)) if scans else 0.0
    except (AttributeError, IndexError, TypeError, ValueError):
        return 0.0


def _payload_largest_capture_bytes(payload: dict[str, Any]) -> int:
    """Bytes the request can produce on the fastest Node (125 MS/s full rate); 0 when unknown.

    The model of the sensor is not known here, so the larger Node 100 file size
    is assumed: a longer budget only matters when the files are never confirmed.
    """
    from .constants import DATASHEET_FULLRATE_COMPLEX_SPS
    from .size_estimate import SizeEstimateError, wave_size_model

    try:
        scans = payload.get("remote_recording_scans") or []
        bandwidth = int(scans[0].get("bandwidth", 0)) if scans else 0
        model = wave_size_model(bandwidth, _payload_duration(payload), max_rate_hz=DATASHEET_FULLRATE_COMPLEX_SPS)
    except (AttributeError, IndexError, TypeError, ValueError, SizeEstimateError, ArithmeticError):
        return 0
    return int(model.total_bytes)
