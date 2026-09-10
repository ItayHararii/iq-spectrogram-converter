"""GUI and connection-field validation. Hardware limits are not applied."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

from .frequency import FrequencyError, parse_decimal

_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ValidationError(ValueError):
    """User-facing validation failure."""


@dataclass(frozen=True)
class ConnectionTarget:
    host: str
    port: int
    use_https: bool
    username: str
    timeout_s: float

    @property
    def origin(self) -> str:
        scheme = "https" if self.use_https else "http"
        if (self.use_https and self.port == 443) or (not self.use_https and self.port == 80):
            netloc = _host_for_url(self.host)
        else:
            netloc = f"{_host_for_url(self.host)}:{self.port}"
        return f"{scheme}://{netloc}"


def _host_for_url(host: str) -> str:
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return host
    if addr.version == 6:
        return f"[{host}]"
    return host


def parse_timeout(text: str) -> float:
    try:
        value = Decimal(str(text).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValidationError("Timeout must be a positive number of seconds.") from exc
    if value <= 0 or not value.is_finite():
        raise ValidationError("Timeout must be a positive number of seconds.")
    return float(value)


def parse_optional_port(text: str) -> int | None:
    raw = (text or "").strip()
    if raw == "":
        return None
    if not raw.isdigit():
        raise ValidationError("Port must be an integer 1–65535, or left empty for the default.")
    port = int(raw)
    if port < 1 or port > 65535:
        raise ValidationError("Port must be in the range 1–65535.")
    return port


def _decimal_field(text: str, *, field: str) -> Decimal:
    try:
        return parse_decimal(text, field=field)
    except FrequencyError as exc:
        raise ValidationError(str(exc)) from exc


def parse_positive_number(text: str, *, field: str) -> float:
    value = _decimal_field(text, field=field)
    if value <= 0:
        raise ValidationError(f"{field} must be greater than zero.")
    as_float = float(value)
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        raise ValidationError(f"{field} must be a finite number.")
    return as_float


def parse_finite_number(text: str, *, field: str) -> float:
    value = _decimal_field(text, field=field)
    as_float = float(value)
    if as_float != as_float or as_float in (float("inf"), float("-inf")):
        raise ValidationError(f"{field} must be a finite number.")
    return as_float


def parse_recorded_time(text: str) -> Decimal:
    value = _decimal_field(text, field="Recording time (seconds)")
    if value <= 0:
        raise ValidationError("Recording time (seconds) must be greater than zero.")
    return value


def parse_connection(
    host_text: str,
    port_text: str,
    *,
    use_https: bool,
    username: str,
    timeout_text: str,
) -> ConnectionTarget:
    raw = (host_text or "").strip()
    if not raw:
        raise ValidationError("Sensor IP or hostname is required.")

    pasted_https: bool | None = None
    parsed_host = raw
    parsed_port: int | None = None

    if "://" in raw or raw.lower().startswith("http"):
        url = urlparse(raw if "://" in raw else f"http://{raw}")
        if url.scheme in ("http", "https"):
            pasted_https = url.scheme == "https"
        if not url.hostname:
            raise ValidationError("Could not parse a hostname from the sensor address.")
        parsed_host = url.hostname
        parsed_port = url.port
    elif raw.startswith("[") and "]" in raw:
        # [IPv6] or [IPv6]:port
        end = raw.find("]")
        parsed_host = raw[1:end]
        rest = raw[end + 1 :]
        if rest.startswith(":"):
            parsed_port = parse_optional_port(rest[1:])
            if parsed_port is None:
                raise ValidationError("IPv6 port after ] is empty.")
    else:
        try:
            ipaddress.ip_address(raw)
            parsed_host = raw
        except ValueError:
            if raw.count(":") == 1:
                host_part, port_part = raw.rsplit(":", 1)
                parsed_host = host_part
                parsed_port = parse_optional_port(port_part)
                if parsed_port is None:
                    raise ValidationError("Port after hostname is empty.")
            else:
                parsed_host = raw

    parsed_host = parsed_host.strip().strip("[]")
    if not parsed_host:
        raise ValidationError("Sensor IP or hostname is required.")
    try:
        ipaddress.ip_address(parsed_host)
    except ValueError:
        if not _HOST_RE.fullmatch(parsed_host):
            raise ValidationError(f"Invalid hostname: {parsed_host!r}.")

    field_port = parse_optional_port(port_text)
    if parsed_port is not None and field_port is not None and parsed_port != field_port:
        raise ValidationError(
            f"Host includes port {parsed_port} but the port field is {field_port}. "
            "Use one or the other."
        )
    https = pasted_https if pasted_https is not None else use_https
    port = field_port or parsed_port
    if port is None:
        port = 443 if https else 80

    user = (username or "").strip()
    if not user:
        raise ValidationError("Username is required for HTTP Basic Authentication.")

    return ConnectionTarget(
        host=parsed_host,
        port=port,
        use_https=https,
        username=user,
        timeout_s=parse_timeout(timeout_text),
    )


def pretty_json(payload: dict) -> str:
    return json.dumps(payload, indent=2) + "\n"
