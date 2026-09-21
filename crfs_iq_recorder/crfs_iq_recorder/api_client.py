"""HTTP client for CRFS Node identity and EMP remote-recording POST.

Identity is read from the Node webpage JSON used by the sensor UI:
GET /api/node.json and GET /api/values/versions.json.
Recording still uses only POST /emp/. Connection tests use TCP plus GET /.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from typing import Any, Protocol

import requests
from requests.auth import HTTPBasicAuth
from requests.exceptions import (
    ConnectionError as RequestsConnectionError,
    ConnectTimeout,
    ReadTimeout,
    SSLError,
    Timeout,
)

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
class RawResponse:
    status_code: int
    text: str
    headers: dict[str, str]
    elapsed_s: float
    url: str


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

    def summary(self) -> str:
        parts = [self.title]
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.demo:
            parts.append("demo")
        return " — ".join(parts)


class RequestsTransport:
    """One-shot requests calls. No retries (a timed-out POST must not be replayed)."""

    def __init__(self, session: requests.Session | None = None) -> None:
        self._session = session or requests.Session()

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict | None = None,
        auth: HTTPBasicAuth | None = None,
        timeout: float = 30.0,
    ) -> RawResponse:
        response = self._session.request(
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

    def test_connection(self, target: ConnectionTarget, password: str) -> HttpOutcome:
        """Reachability + GET /. Never POSTs a recording body."""
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
    ) -> HttpOutcome:
        url = emp_url(target)
        auth = HTTPBasicAuth(target.username, password)
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/plain;q=0.8"}
        try:
            raw = self.transport.request(
                "POST",
                url,
                headers=headers,
                json_body=payload,
                auth=auth,
                timeout=target.timeout_s,
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
                detail=f"POST {redact_url(url)} failed: {exc}. Recording was not retried.",
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
