"""The diagnostic log: full technical detail kept out of the Activity panel.

API replies, exact request payloads, the sensor's event-stream messages
(repeats counted), SFTP problems and stack traces go to a rotating file under
the application's settings folder (``logs/diagnostic.log``). Secrets never do:
every line passes through the sanitizer, and password/token fields are
masked. The Activity panel shows short operator messages and points here
("See details").
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import deque
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import paths
from .sanitizer import redact_obj, redact_text

LOG_FOLDER = "logs"
LOG_NAME = "diagnostic.log"
MAX_BYTES = 2 * 1024 * 1024
BACKUPS = 4
RECENT_LINES = 400

_lock = threading.RLock()
_handler: RotatingFileHandler | None = None
_handler_path: Path | None = None
_recent: deque[str] = deque(maxlen=RECENT_LINES)
_secrets: set[str] = set()
_SECRET_FIELD = re.compile(
    r'(?i)("?(?:password|passwd|pwd|secret|token|authorization)"?\s*[:=]\s*)((?:(?:basic|bearer|digest)\s+)?(?:"[^"]*"|\S+))'
)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
# "Bearer <token>", or "Basic <base64>" (a digit, + / or = in it, so "Basic settings" is left alone).
_AUTH_SCHEME = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{6,}|\b(basic)\s+(?=[A-Za-z]*[0-9+/=])[A-Za-z0-9+/]{8,}=*")
_URL_SECRET = re.compile(
    r"(?i)([?&;](?:password|passwd|pwd|pass|token|access_token|api_key|apikey|secret|auth|sig|signature)=)[^&\s\"'#]+"
)
# The sensor's position: CRFS puts its GPS fix into every recording reply. Quotes may be JSON-escaped
# (a reply body inside a JSON field).
_GPS_FIELD = re.compile(r'(?i)((?:\\?")?(?:latitude|longitude|altitude)(?:\\?")?\s*[:=]\s*)-?\d+(?:\.\d+)?')
LOCATION_MASK = "<location>"


def log_path() -> Path:
    return Path(paths.user_config_dir()) / LOG_FOLDER / LOG_NAME


def register_secret(value: str | None) -> None:
    """A stored password or token that must never appear in either log (masked if it does)."""
    text = str(value or "")
    if len(text) >= 3:
        with _lock:
            _secrets.add(text)


def _ordinary_word(secret: str) -> bool:
    # The factory default "pass" is also a word of the operator's own messages.
    return len(secret) < 6 and secret.isalpha()


def mask_values(text: str, secrets, *, spare_words: bool = False) -> str:
    """Replace each secret where it stands as a whole token (not inside a longer word).

    Every secret is masked, an ordinary word such as "pass" too ("next ***" in
    the diagnostic log when that is the password). `spare_words` keeps such a
    word in the Activity panel, whose sentences are the application's own.
    """
    out = text
    for secret in sorted({str(s) for s in secrets if s and len(str(s)) >= 3}, key=len, reverse=True):
        if spare_words and _ordinary_word(secret):
            continue
        if secret in out:
            out = re.sub(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", "***", out)
    return out


def _scrub_patterns(text: str) -> str:
    out = _SECRET_FIELD.sub(r"\1***", redact_text(str(text or "")))
    out = _URL_USERINFO.sub(r"\1***@", out)
    out = _AUTH_SCHEME.sub(lambda m: f"{m.group(1) or m.group(2)} ***", out)
    out = _GPS_FIELD.sub(lambda m: m.group(1) + LOCATION_MASK, out)
    return _URL_SECRET.sub(r"\1***", out)


def _registered() -> list[str]:
    with _lock:
        return list(_secrets)


def scrub(text: str) -> str:
    """Diagnostic log and copied details: every stored secret masked."""
    return mask_values(_scrub_patterns(text), _registered())


def scrub_operator(text: str) -> str:
    """An Activity line: as `scrub`, but an ordinary-word password does not blank that word."""
    return mask_values(_scrub_patterns(text), _registered(), spare_words=True)


def _logger_handler() -> RotatingFileHandler | None:
    global _handler, _handler_path
    path = log_path()
    if _handler is not None and _handler_path == path:
        return _handler
    if _handler is not None:
        try:
            _handler.close()
        except Exception:  # noqa: BLE001
            pass
        _handler = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _handler = RotatingFileHandler(path, maxBytes=MAX_BYTES, backupCount=BACKUPS, encoding="utf-8", delay=True)
        _handler.setFormatter(logging.Formatter("%(message)s"))
        _handler_path = path
    except OSError:
        _handler = None
        _handler_path = None
    return _handler


def _format_fields(fields: dict[str, Any]) -> str:
    if not fields:
        return ""
    try:
        return " " + json.dumps(redact_obj(fields), ensure_ascii=False, default=str, sort_keys=False)
    except (TypeError, ValueError):
        return " " + str(fields)


def diag(event: str, message: str = "", **fields: Any) -> None:
    """One diagnostic line: time, event name, free text and structured fields (JSON)."""
    stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    line = scrub(f"{stamp} [{event}] {message}".rstrip() + _format_fields(fields))
    with _lock:
        _recent.append(line)
        handler = _logger_handler()
        if handler is None:
            return
        record = logging.LogRecord("crfs.diagnostic", logging.INFO, __file__, 0, line, None, None)
        try:
            handler.emit(record)
            handler.flush()
        except Exception:  # noqa: BLE001 - diagnostics must never break the application
            pass


REPEAT_WINDOW_S = 10.0


class RepeatLimiter:
    """The same event over and over: the first in full, then at most one per window with the count left out.

    Without it an exception raised for every 32 KB of a download filled the
    log's 10 MB (all backups) in minutes and kept the GUI thread busy writing.
    """

    def __init__(self, window_s: float = REPEAT_WINDOW_S, *, clock=time.monotonic, max_keys: int = 64) -> None:
        self._window_s = float(window_s)
        self._clock = clock
        self._max_keys = int(max_keys)
        self._last: dict[str, float] = {}
        self._suppressed: dict[str, int] = {}
        self._lock = threading.Lock()

    def admit(self, key: str) -> tuple[bool, int]:
        """(write this one?, occurrences left out since the last one written)."""
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self._window_s:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return False, 0
            if last is None and len(self._last) >= self._max_keys:
                oldest = min(self._last, key=self._last.__getitem__)
                self._last.pop(oldest, None)
                self._suppressed.pop(oldest, None)
            self._last[key] = now
            return True, self._suppressed.pop(key, 0)


def exception_signature(exc_type, exc, tb) -> str:
    """Type, innermost frame and message: what makes two unhandled exceptions "the same"."""
    where = ""
    frame = tb
    while frame is not None and frame.tb_next is not None:
        frame = frame.tb_next
    if frame is not None:
        code = frame.tb_frame.f_code
        where = f"{code.co_filename}:{frame.tb_lineno}"
    name = getattr(exc_type, "__name__", str(exc_type))
    return f"{name}|{where}|{str(exc)[:200]}"


def recent(count: int = 30) -> list[str]:
    with _lock:
        items = list(_recent)
    return items[-max(int(count), 0):] if count else []


def close() -> None:
    global _handler, _handler_path
    with _lock:
        if _handler is not None:
            try:
                _handler.close()
            except Exception:  # noqa: BLE001
                pass
        _handler = None
        _handler_path = None


def reset_for_tests() -> None:
    with _lock:
        _recent.clear()
        _secrets.clear()
    close()
