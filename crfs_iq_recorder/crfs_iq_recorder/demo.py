"""Simulated EMP/HTTP responses. Never opens a network socket."""

from __future__ import annotations

import json
from urllib.parse import urlparse

from requests.auth import HTTPBasicAuth

from .api_client import RawResponse

_DEMO_NODE = {
    "name": "rfeyeDEMO",
    "nodeSoftwareVersion": "2.25-325",
    "hostVersion": "2.25-4823",
    "version": "2.25-3000",
    "client": {"name": "crfs-rfeye-node", "version": "5.1.6"},
}

_DEMO_VERSIONS = {
    "values": {
        "0-software-version": (
            "# Software Version\n<table><tbody><tr><td>Node Software</td>"
            "<td>2.25-325</td></tr></tbody></table>"
        ),
        "1-hardware-info": (
            "# Radio Hardware and Firmware\n<table><tbody><tr><td>Device Type</td>"
            "<td>R100-18</td></tr></tbody></table>"
        ),
    }
}


class DemoTransport:
    """In-process fake sensor. Tests and GUI demo mode must use this, not a live Node."""

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
        parsed = urlparse(url)
        path = parsed.path or "/"
        method_u = method.upper()
        if method_u == "GET" and path == "/api/node.json":
            return RawResponse(
                status_code=200,
                text=json.dumps(_DEMO_NODE),
                headers={"Content-Type": "application/json", "Server": "CRFS-IQ-Recorder-Demo"},
                elapsed_s=0.01,
                url=url,
            )
        if method_u == "GET" and path == "/api/values/versions.json":
            return RawResponse(
                status_code=200,
                text=json.dumps(_DEMO_VERSIONS),
                headers={"Content-Type": "application/json", "Server": "CRFS-IQ-Recorder-Demo"},
                elapsed_s=0.01,
                url=url,
            )
        if method_u == "GET":
            body = (
                "<!DOCTYPE html><html><head><title>RFeye Node (demo)</title></head>"
                "<body><p>Simulated Node webpage. No sensor was contacted.</p></body></html>"
            )
            return RawResponse(
                status_code=200,
                text=body,
                headers={"Content-Type": "text/html; charset=utf-8", "Server": "CRFS-IQ-Recorder-Demo"},
                elapsed_s=0.012,
                url=url,
            )
        if method_u == "POST" and path.rstrip("/") == "/emp":
            simulated = {
                "demo": True,
                "message": "Simulated EMP response. No recording command was sent to a sensor.",
                "accepted": True,
                "note": (
                    "A real Node may return a different JSON schema. "
                    "This object is not a CRFS specification."
                ),
                "echo_task": None,
            }
            if isinstance(json_body, dict):
                scans = json_body.get("remote_recording_scans")
                if isinstance(scans, list) and scans and isinstance(scans[0], dict):
                    simulated["echo_task"] = scans[0].get("task_id")
            return RawResponse(
                status_code=200,
                text=json.dumps(simulated, indent=2),
                headers={"Content-Type": "application/json", "Server": "CRFS-IQ-Recorder-Demo"},
                elapsed_s=0.025,
                url=url,
            )
        return RawResponse(
            status_code=404,
            text=json.dumps({"demo": True, "error": f"Demo transport has no handler for {method_u} {path}"}),
            headers={"Content-Type": "application/json"},
            elapsed_s=0.001,
            url=url,
        )


def demo_unreachable_tcp_host() -> str:
    """Documentation helper — demo mode does not use this host."""
    return "203.0.113.1"
