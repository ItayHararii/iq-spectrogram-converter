"""Protect connection secrets on this computer. Never write them to logs."""

from __future__ import annotations

import base64
import sys


_DESC = "CRFS IQ Recorder"


def protect_secret(text: str) -> str:
    payload = (text or "").encode("utf-8")
    if sys.platform == "win32":
        import win32crypt  # type: ignore

        blob = win32crypt.CryptProtectData(payload, _DESC, None, None, None, 0)
        return base64.b64encode(blob).decode("ascii")
    return base64.b64encode(payload).decode("ascii")


def unprotect_secret(token: str | None) -> str | None:
    if not token:
        return None
    try:
        blob = base64.b64decode(str(token).encode("ascii"), validate=True)
    except (ValueError, TypeError):
        return None
    if sys.platform == "win32":
        try:
            import win32crypt  # type: ignore

            _desc, data = win32crypt.CryptUnprotectData(blob, None, None, None, 0)
        except Exception:
            return None
        try:
            return bytes(data).decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None
