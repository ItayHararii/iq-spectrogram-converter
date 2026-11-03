"""What an operator message (Activity line or status text) may not contain.

Used by the autouse fixture in conftest.py on every message any test
produces, and by test_operator_messages.py directly.
"""

from __future__ import annotations

import re

MAX_LENGTH = 140

# snake_case program names (window_for, sensor_error); file names such as
# iq_20261103_171321_0001.wav or file_063929_merged.wav contain digits and pass.
_IDENTIFIER = re.compile(r"\b[a-z]{2,}(?:_[a-z]{2,})+\b")
_EXCEPTION = re.compile(r"\b(?:Traceback|WinError|Errno|[A-Z][A-Za-z]*(?:Error|Exception))\b")
_TRACE = re.compile(r'File "|, line \d+|\bin <module>')
_TEMP_PATH = re.compile(r"(?i)\\Temp\\|/tmp/|%TEMP%|pytest-of-|\\AppData\\")
_SECRET = re.compile(
    r"(?i)\b(?:password|passwd|pwd|token|authorization)\s*[:=]\s*\S|\bbearer\s+\S{6,}|\bbasic\s+[A-Za-z0-9+/]{8,}=*|://[^/\s@]+@"
)
_INTERNAL = re.compile(r"\b(?:payload|SSE|RemoteTimeData|MessageData|task_id|http_accepted|budget|window_for)\b")


def problems(text: str, *, secrets: tuple[str, ...] = ()) -> list[str]:
    found: list[str] = []
    if len(text) > MAX_LENGTH:
        found.append(f"longer than {MAX_LENGTH} characters ({len(text)})")
    if "\n" in text or "\r" in text:
        found.append("more than one line")
    if _IDENTIFIER.search(text):
        found.append(f"program name {_IDENTIFIER.search(text).group(0)!r}")
    if _EXCEPTION.search(text) or _TRACE.search(text):
        found.append("exception or traceback text")
    if _TEMP_PATH.search(text):
        found.append("temporary or profile path")
    if _SECRET.search(text):
        found.append("credential text")
    if _INTERNAL.search(text):
        found.append(f"internal term {_INTERNAL.search(text).group(0)!r}")
    for secret in secrets:
        if secret and len(secret) >= 3 and re.search(rf"(?<![A-Za-z0-9]){re.escape(secret)}(?![A-Za-z0-9])", text):
            found.append("a stored password")
    return found
