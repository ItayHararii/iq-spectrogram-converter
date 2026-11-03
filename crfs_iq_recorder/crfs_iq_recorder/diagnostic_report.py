"""The text behind "Copy diagnostic details".

Enough to explain the last recording to someone who was not there: the
application version, which sensor, the request exactly as it was sent (with
local and UTC times), what came back, and the latest diagnostic lines.

Never in it: passwords, tokens, Authorization headers, secrets in URLs, or
the sensor's network address (the sensor is named by model and serial; the
address is replaced with ``ADDRESS_MASK``, any other IPv4 address with
``OTHER_ADDRESS_MASK``). The user's home folder is shown as ``%USERPROFILE%``.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .capture_measure import CaptureMeasurement, coverage_text, received_text
from .diagnostic_log import mask_values, scrub
from .operator_text import requested_text

ADDRESS_MASK = "<sensor-address>"
# Any other network address in the lines (this computer, a gateway, a tunnel).
OTHER_ADDRESS_MASK = "<address>"
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
RECENT_LINES = 30


def mask_secrets(text: str, *, secrets: Iterable[str] = (), hosts: Iterable[str] = ()) -> str:
    """Remove secrets and sensor addresses from free text."""
    out = mask_values(scrub(str(text or "")), secrets)
    for host in sorted({str(h).strip() for h in hosts if h and str(h).strip()}, key=len, reverse=True):
        out = re.sub(rf"(?<![\w.-]){re.escape(host)}(?![\w-]|\.\w)", ADDRESS_MASK, out)
    out = _IPV4.sub(OTHER_ADDRESS_MASK, out)
    home = os.path.expanduser("~")
    if home and home != "~":
        out = out.replace(home, "%USERPROFILE%").replace(home.replace("\\", "/"), "%USERPROFILE%")
    return out


def _stamp(value: Any) -> str:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if not isinstance(value, datetime):
        return "unknown"
    local = value.astimezone()
    utc = local.astimezone(timezone.utc)
    return f"{local:%Y-%m-%d %H:%M:%S} local ({utc:%Y-%m-%d %H:%M:%S} UTC)"


def _sensor_line(sensor: Any) -> str:
    if sensor is None:
        return "Sensor: not checked"
    model = getattr(sensor, "model", "") or "unknown model"
    serial = getattr(sensor, "serial", "") or "unknown"
    firmware = getattr(sensor, "firmware", "") or "unknown"
    demo = " (demo)" if getattr(sensor, "demo", False) else ""
    return f"Sensor: {model}{demo} · serial {serial} · firmware {firmware} · address not included"


def _summary_lines(summary: Any) -> list[str]:
    if summary is None:
        return ["Last recording: none in this session"]
    lines = ["Last recording:"]
    lines.append(f"  Started: {_stamp(getattr(summary, 'started_at', None))}")
    lines.append(f"  Finished: {_stamp(getattr(summary, 'finished_at', None))}")
    payload = dict(getattr(summary, "payload", {}) or {})
    if payload:
        lines.append("  Sent: " + json.dumps(payload, ensure_ascii=False, default=str))
        try:
            lines.append(f"  Requested: {requested_text(int(payload['bandwidth']), float(summary.requested_s))}")
        except (KeyError, TypeError, ValueError):
            pass
    else:
        lines.append("  Sent: nothing (the request did not reach the sensor)")
    result = getattr(summary, "result", "") or getattr(summary, "outcome", "") or "unknown"
    reason = getattr(summary, "result_reason", "")
    lines.append(f"  Result: {result}" + (f" ({reason})" if reason else ""))
    measurement = CaptureMeasurement.from_dict(getattr(summary, "measurement", None) or {})
    lines.append(f"  Received: {received_text(measurement, int(getattr(summary, 'parts', 0) or 0))}")
    lines.append(f"  Recorded range (file metadata): {coverage_text(measurement)}")
    reply = dict(getattr(summary, "sensor_reply", {}) or {})
    if reply:
        lines.append("  Sensor reply: " + json.dumps(reply, ensure_ascii=False, default=str))
    if getattr(summary, "sensor_error", ""):
        lines.append(f"  Sensor error: {summary.sensor_error}")
    detail = getattr(summary, "detail", "")
    if detail:
        lines.append(f"  Detail: {' '.join(str(detail).split())}")
    warnings = list(getattr(summary, "sensor_warnings", []) or [])
    if warnings:
        lines.append(f"  Sensor warnings: {'; '.join(dict.fromkeys(warnings))}")
    files = list(getattr(summary, "finalized_paths", []) or [])
    if files:
        lines.append("  Files: " + ", ".join(path.rsplit("/", 1)[-1] for path in files))
    return lines


def build_report(
    *,
    version: str,
    sensor: Any,
    summary: Any,
    diagnostic_lines: Iterable[str],
    secrets: Iterable[str] = (),
    hosts: Iterable[str] = (),
    now: datetime | None = None,
) -> str:
    lines = [
        f"CRFS IQ Recorder {version} - diagnostic details",
        f"Copied: {_stamp(now or datetime.now())}",
        _sensor_line(sensor),
        *_summary_lines(summary),
        f"Recent diagnostic lines (last {RECENT_LINES}):",
    ]
    recent = list(diagnostic_lines)[-RECENT_LINES:]
    if recent:
        lines.extend(f"  {line}" for line in recent)
    else:
        lines.append("  (none)")
    secret_list, host_list = list(secrets), list(hosts)
    return "\n".join(mask_secrets(line, secrets=secret_list, hosts=host_list) for line in lines) + "\n"
