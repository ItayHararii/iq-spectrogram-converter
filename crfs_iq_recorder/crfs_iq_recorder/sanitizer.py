"""Strip secrets from logs, presets, and exported history."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_PASSWORD_KEYS = {
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "authorization",
    "proxy-authorization",
    "auth",
}
_AUTH_HEADER_RE = re.compile(r"(?i)^(authorization|proxy-authorization)$")
_BASIC_IN_TEXT = re.compile(r"(?i)(authorization\s*[:=]\s*basic\s+)\S+")
_USERINFO_RE = re.compile(r"(?i)^([^:/?#]+)://([^/?#]*?)@")


def redact_url(url: str) -> str:
    text = url or ""
    text = _USERINFO_RE.sub(r"\1://***@", text)
    try:
        parts = urlsplit(text)
        if parts.username or parts.password:
            host = parts.hostname or ""
            if parts.port:
                netloc = f"{host}:{parts.port}"
            else:
                netloc = host
            text = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass
    return text


def _is_secret_key(key: str) -> bool:
    lowered = key.lower()
    return bool(_AUTH_HEADER_RE.match(key)) or lowered in _PASSWORD_KEYS or "password" in lowered


def _redact_value(key: str, value: Any) -> Any:
    if _is_secret_key(key):
        return "***"
    return redact_obj(value)


def redact_obj(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(item) for item in value]
    if isinstance(value, tuple):
        return [redact_obj(item) for item in value]
    if isinstance(value, str):
        return _BASIC_IN_TEXT.sub(r"\1***", redact_url(value))
    return value


def redact_text(text: str) -> str:
    if not text:
        return text
    redacted = _BASIC_IN_TEXT.sub(r"\1***", redact_url(text))
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return redacted
    return json.dumps(redact_obj(parsed), indent=2)


def headers_without_auth(headers: dict | None) -> dict[str, str]:
    safe: dict[str, str] = {}
    if not headers:
        return safe
    for key, value in headers.items():
        if _AUTH_HEADER_RE.match(str(key)):
            continue
        safe[str(key)] = str(value)
    return safe
