"""Parse CRFS Node identity from the webpage JSON APIs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

_TD_PAIR_RE = re.compile(
    r"<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_MISSING = "—"


@dataclass(frozen=True)
class SensorInfo:
    host: str = ""
    model: str = _MISSING
    firmware: str = _MISSING
    serial: str = _MISSING
    status: str = "No sensor IP"
    connected: bool = False
    detail: str = ""
    demo: bool = False

    def display_model(self) -> str:
        return self.model or _MISSING

    def display_firmware(self) -> str:
        return self.firmware or _MISSING

    def display_serial(self) -> str:
        return self.serial or _MISSING


def blank_info(
    *,
    host: str = "",
    status: str = "No sensor IP",
    demo: bool = False,
    detail: str = "",
) -> SensorInfo:
    return SensorInfo(host=host, status=status, demo=demo, detail=detail)


def checking_info(host: str, *, demo: bool = False) -> SensorInfo:
    return SensorInfo(host=host, status="Checking…", demo=demo)


def _clean(text: Any) -> str:
    value = " ".join(str(text or "").split()).strip()
    return value


def _strip_html(text: str) -> str:
    return _clean(_TAG_RE.sub("", text or ""))


def table_pairs(html: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for match in _TD_PAIR_RE.finditer(html or ""):
        label = _strip_html(match.group(1))
        value = _strip_html(match.group(2))
        if label:
            pairs[label] = value
    return pairs


def _walk_strings(obj: Any) -> list[str]:
    found: list[str] = []
    if isinstance(obj, str):
        found.append(obj)
    elif isinstance(obj, dict):
        for value in obj.values():
            found.extend(_walk_strings(value))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_walk_strings(item))
    return found


def labeled_value(pairs: dict[str, str], *labels: str) -> str:
    lowered = {key.casefold(): value for key, value in pairs.items()}
    for label in labels:
        value = lowered.get(label.casefold(), "")
        if value:
            return value
    return ""


def parse_versions_payload(payload: Any) -> tuple[str, str]:
    """Return (model, firmware) from /api/values/versions.json."""
    pairs: dict[str, str] = {}
    for chunk in _walk_strings(payload):
        pairs.update(table_pairs(chunk))
    model = labeled_value(pairs, "Device Type")
    firmware = labeled_value(pairs, "Node Software")
    return model, firmware


def parse_node_payload(payload: Any) -> tuple[str, str]:
    """Return (serial/name, firmware) from /api/node.json."""
    if not isinstance(payload, dict):
        return "", ""
    serial = _clean(payload.get("name"))
    firmware = _clean(payload.get("nodeSoftwareVersion") or payload.get("node_software_version"))
    return serial, firmware


def parse_software_manager_payload(payload: Any) -> tuple[str, str]:
    """Return (serial/label, firmware) from /software-manager/api."""
    if not isinstance(payload, dict):
        return "", ""
    serial = _clean(payload.get("label") or payload.get("name"))
    firmware = _clean(payload.get("node_software_version") or payload.get("nodeSoftwareVersion"))
    return serial, firmware


def as_json_object(body_json: Any, text: str) -> dict[str, Any] | None:
    if isinstance(body_json, dict):
        return body_json
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def build_sensor_info(
    *,
    host: str,
    node_payload: Any = None,
    versions_payload: Any = None,
    manager_payload: Any = None,
    status: str = "Connected",
    detail: str = "",
    demo: bool = False,
) -> SensorInfo:
    serial, firmware = parse_node_payload(node_payload)
    model, version_firmware = parse_versions_payload(versions_payload)
    if not serial or not firmware:
        mgr_serial, mgr_firmware = parse_software_manager_payload(manager_payload)
        serial = serial or mgr_serial
        firmware = firmware or mgr_firmware
    firmware = version_firmware or firmware
    connected = status.casefold() in {"connected", "online"}
    return SensorInfo(
        host=host,
        model=model or _MISSING,
        firmware=firmware or _MISSING,
        serial=serial or _MISSING,
        status=status,
        connected=connected,
        detail=detail,
        demo=demo,
    )
