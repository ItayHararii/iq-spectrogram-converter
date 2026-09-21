from unittest.mock import MagicMock
from urllib.parse import urlparse

from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import ConnectTimeout, ReadTimeout

from crfs_iq_recorder.api_client import EmpClient, RawResponse, RequestsTransport
from crfs_iq_recorder.demo import DemoTransport
from crfs_iq_recorder.request_builder import default_payload
from crfs_iq_recorder.validation import ConnectionTarget


def _target() -> ConnectionTarget:
    return ConnectionTarget(
        host="192.0.2.10",
        port=80,
        use_https=False,
        username="admin",
        timeout_s=5.0,
    )


class FakeTransport:
    def __init__(self, response: RawResponse | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.error:
            raise self.error
        assert self.response is not None
        return self.response


def test_demo_connection_and_recording_never_post_to_hardware():
    transport = DemoTransport()
    client = EmpClient(transport=transport, demo=True)
    target = _target()
    probe = client.test_connection(target, "pass")
    rec = client.start_recording(target, "pass", default_payload())
    assert probe.demo
    assert rec.demo
    assert probe.accepted
    assert rec.accepted
    assert rec.kind == "accepted"
    assert "No recording command was sent" in rec.body_text or (
        isinstance(rec.body_json, dict) and rec.body_json.get("demo") is True
    )


def test_connection_test_uses_get_never_post():
    transport = FakeTransport(
        RawResponse(200, "<html>node</html>", {"Content-Type": "text/html"}, 0.01, "http://192.0.2.10/")
    )
    client = EmpClient(transport=transport, demo=True)
    client.test_connection(_target(), "secret-password")
    assert [c[0] for c in transport.calls] == ["GET"]
    assert all("/emp" not in c[1] for c in transport.calls)


def test_start_recording_posts_json_to_emp():
    transport = FakeTransport(
        RawResponse(200, '{"status":"ok"}', {"Content-Type": "application/json"}, 0.2, "http://192.0.2.10/emp/")
    )
    client = EmpClient(transport=transport, demo=False)
    payload = default_payload()
    outcome = client.start_recording(_target(), "pass", payload)
    assert outcome.accepted
    assert "not proof that the IQ file" in outcome.detail
    assert "completion not confirmed" in outcome.title
    method, url, kwargs = transport.calls[0]
    assert method == "POST"
    assert url.endswith("/emp/")
    assert kwargs["json_body"] == payload
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_http_401():
    transport = FakeTransport(RawResponse(401, "unauthorized", {}, 0.01, "http://192.0.2.10/emp/"))
    outcome = EmpClient(transport=transport).start_recording(_target(), "bad", default_payload())
    assert outcome.kind == "authentication_error"
    assert outcome.status_code == 401
    assert not outcome.accepted


def test_http_500():
    transport = FakeTransport(RawResponse(500, "boom", {}, 0.01, "http://192.0.2.10/emp/"))
    outcome = EmpClient(transport=transport).start_recording(_target(), "pass", default_payload())
    assert outcome.kind == "server_error"
    assert outcome.status_code == 500


def test_connection_error():
    transport = FakeTransport(error=RequestsConnectionError("refused"))
    outcome = EmpClient(transport=transport).start_recording(_target(), "pass", default_payload())
    assert outcome.kind == "connection_failure"
    assert not outcome.request_sent
    assert not outcome.outcome_unknown


def test_connect_timeout_not_sent():
    transport = FakeTransport(error=ConnectTimeout("connect"))
    outcome = EmpClient(transport=transport).start_recording(_target(), "pass", default_payload())
    assert outcome.kind == "timeout"
    assert not outcome.request_sent
    assert not outcome.outcome_unknown


def test_read_timeout_after_send_is_unknown_and_not_retried():
    transport = FakeTransport(error=ReadTimeout("read"))
    client = EmpClient(transport=transport)
    outcome = client.start_recording(_target(), "pass", default_payload())
    assert outcome.kind == "timeout_after_send"
    assert outcome.request_sent
    assert outcome.outcome_unknown
    assert "will not retry" in outcome.detail.lower() or "not retry" in outcome.detail.lower()
    assert len(transport.calls) == 1


def test_non_json_success_body():
    transport = FakeTransport(RawResponse(200, "not-json", {}, 0.01, "http://192.0.2.10/emp/"))
    outcome = EmpClient(transport=transport).start_recording(_target(), "pass", default_payload())
    assert outcome.accepted
    assert outcome.body_json is None
    assert "not JSON" in outcome.detail


def test_validation_http_400():
    transport = FakeTransport(RawResponse(400, '{"error":"bad"}', {}, 0.01, "http://192.0.2.10/emp/"))
    outcome = EmpClient(transport=transport).start_recording(_target(), "pass", default_payload())
    assert outcome.kind == "validation_error"
    assert outcome.body_json == {"error": "bad"}


def test_password_not_copied_into_outcome(monkeypatch):
    transport = FakeTransport(RawResponse(401, "Authorization: Basic dXNlcjpwYXNz", {}, 0.01, "http://192.0.2.10/emp/"))
    outcome = EmpClient(transport=transport).start_recording(_target(), "super-secret", default_payload())
    blob = outcome.detail + outcome.body_text + (outcome.url or "")
    assert "super-secret" not in blob
    assert "Basic ***" in outcome.body_text or "dXNlcjpwYXNz" not in outcome.body_text or "***" in outcome.body_text


def test_requests_transport_uses_injected_session():
    session = MagicMock()
    response = MagicMock()
    response.status_code = 204
    response.text = ""
    response.headers = {}
    response.elapsed.total_seconds.return_value = 0.0
    response.url = "http://192.0.2.10/emp/"
    session.request.return_value = response
    raw = RequestsTransport(session=session).request("POST", "http://192.0.2.10/emp/", json_body={"a": 1}, timeout=1)
    assert raw.status_code == 204
    session.request.assert_called_once()
    assert session.request.call_args.kwargs.get("json") == {"a": 1}


class PathTransport:
    def __init__(self, by_path: dict[str, RawResponse]) -> None:
        self.by_path = by_path
        self.calls: list[tuple] = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        path = urlparse(url).path
        return self.by_path[path]


def test_fetch_sensor_info_uses_node_json_and_versions():
    transport = PathTransport(
        {
            "/api/node.json": RawResponse(
                200,
                '{"name":"rfeye100400","nodeSoftwareVersion":"2.25-325"}',
                {"Content-Type": "application/json"},
                0.01,
                "http://192.0.2.10/api/node.json",
            ),
            "/api/values/versions.json": RawResponse(
                200,
                (
                    '{"values":{"1-hardware-info":"<table><tr><td>Device Type</td>'
                    '<td>R100-18</td></tr></table>","0-software-version":'
                    '"<table><tr><td>Node Software</td><td>2.25-325</td></tr></table>"}}'
                ),
                {"Content-Type": "application/json"},
                0.01,
                "http://192.0.2.10/api/values/versions.json",
            ),
        }
    )
    client = EmpClient(transport=transport, demo=False, tcp_probe_fn=lambda *a, **k: (True, "ok"))
    info = client.fetch_sensor_info(_target(), "pass")
    assert info.connected
    assert info.status == "Connected"
    assert info.serial == "rfeye100400"
    assert info.model == "R100-18"
    assert info.firmware == "2.25-325"
    assert [call[0] for call in transport.calls] == ["GET", "GET"]
    assert all("/emp" not in call[1] for call in transport.calls)


def test_fetch_sensor_info_demo_never_posts():
    transport = DemoTransport()
    client = EmpClient(transport=transport, demo=True)
    info = client.fetch_sensor_info(_target(), "pass")
    assert info.demo
    assert info.serial == "rfeyeDEMO"
    assert info.model == "R100-18"
    assert info.firmware == "2.25-325"
    assert info.status == "Demo"


def test_fetch_sensor_info_401():
    transport = PathTransport(
        {
            "/api/node.json": RawResponse(401, "unauthorized", {}, 0.01, "http://192.0.2.10/api/node.json"),
        }
    )
    client = EmpClient(transport=transport, demo=False, tcp_probe_fn=lambda *a, **k: (True, "ok"))
    info = client.fetch_sensor_info(_target(), "bad")
    assert info.status == "Authentication failed"
    assert not info.connected
    assert len(transport.calls) == 1


def test_probe_sensor_uses_authenticated_node_json_and_short_timeout():
    transport = FakeTransport(
        RawResponse(200, '{"name":"rfeyeTEST"}', {"Content-Type": "application/json"}, 0.01, "http://192.0.2.10/api/node.json")
    )
    client = EmpClient(transport=transport, demo=False, tcp_probe_fn=lambda *a, **k: (True, "ok"))
    ok, detail = client.probe_sensor(_target(), "pass", timeout_s=4.0)
    assert ok is True
    assert detail == "ok"
    assert len(transport.calls) == 1
    method, url, kwargs = transport.calls[0]
    assert method == "GET"
    assert url.endswith("/api/node.json")
    assert kwargs["timeout"] == 4.0
    assert "/emp" not in url


def test_probe_sensor_timeout_and_auth_failure():
    timeout_transport = FakeTransport(error=ReadTimeout("late"))
    client = EmpClient(transport=timeout_transport, demo=False, tcp_probe_fn=lambda *a, **k: (True, "ok"))
    ok, detail = client.probe_sensor(_target(), "pass", timeout_s=4.0)
    assert ok is False
    assert "timeout" in detail.casefold()
    auth_transport = FakeTransport(
        RawResponse(401, "unauthorized", {}, 0.01, "http://192.0.2.10/api/node.json")
    )
    client = EmpClient(transport=auth_transport, demo=False, tcp_probe_fn=lambda *a, **k: (True, "ok"))
    ok, detail = client.probe_sensor(_target(), "bad", timeout_s=4.0)
    assert ok is False
    assert "authentication" in detail.casefold()
    assert all(call[0] == "GET" for call in auth_transport.calls)

