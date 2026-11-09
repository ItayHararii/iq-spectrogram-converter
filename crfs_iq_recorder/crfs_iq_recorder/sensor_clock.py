"""The sensor's clock against a trustworthy computer clock.

The sensor's time comes from its status page: ``values["02-system-time"]`` of
``GET /api/values/status.json`` is "2026-11-09 10:07:12+0000" (whole seconds,
with an explicit UTC offset). The request is bracketed with this computer's
clock; the offset is sensor minus the middle of the request and its
uncertainty is half the round trip plus the page's 1 s resolution. The HTTP
``Date`` header is the fallback and a second opinion.

Instants are compared, never displayed local times: "12:07:12+0200" is the
same moment as "10:07:12+0000" and is no drift at all.

Before anything is changed on the sensor this computer's own clock must be
trustworthy: one SNTP answer (the sensor's own NTP servers first) within
``PC_TRUST_MAX_OFFSET_S``, else the Windows time service saying it synchronised
recently. When neither can be shown the sensor is left alone.
"""

from __future__ import annotations

import re
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

STATUS_PAGE_PATH = "/api/values/status.json"
STATUS_RESOLUTION_S = 1.0
# A wrong clock: more than this beyond the measurement's uncertainty. Far above
# link noise and RTC drift between NTP updates, below the 10 min a sensor clock
# must be off before its files land at visibly wrong times, and above chrony's
# "initstepslew 10" threshold, so a refresh really steps the clock.
CLOCK_INCORRECT_S = 120.0
# Synchronised: within this of the computer (plus the measurement's uncertainty).
CLOCK_SYNCED_S = 5.0

CORRECT = "correct"
DRIFTING = "drifting"  # off by more than CLOCK_SYNCED_S, not by CLOCK_INCORRECT_S
INCORRECT = "incorrect"
UNKNOWN = "unknown"

PC_TRUST_MAX_OFFSET_S = 10.0
# Windows syncs a workgroup computer once a week by default.
PC_SYNC_MAX_AGE_S = 8 * 24 * 3600.0
SNTP_TIMEOUT_S = 2.0
SNTP_MAX_QUERIES = 3
SNTP_FALLBACK_SERVERS = ("time.windows.com", "pool.ntp.org")
_NTP_EPOCH = 2208988800


@dataclass
class ClockReading:
    """One reading of the sensor's clock next to this computer's at the same moment."""

    available: bool
    sensor_utc: datetime | None = None
    pc_utc: datetime | None = None  # middle of the request
    offset_s: float | None = None  # sensor minus computer
    uncertainty_s: float = 0.0
    rtt_s: float = 0.0
    source: str = ""
    raw: str = ""  # the sensor's own text, e.g. "2012-01-02 11:41:53+0000"
    time_source: str = ""  # what the sensor disciplines its clock with (NTP, GPS, ...)
    gnss_status: str = ""
    header_offset_s: float | None = None
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "sensor_utc": self.sensor_utc.isoformat() if self.sensor_utc is not None else None,
            "pc_utc": self.pc_utc.isoformat() if self.pc_utc is not None else None,
            "offset_s": self.offset_s,
            "uncertainty_s": round(self.uncertainty_s, 3),
            "rtt_s": round(self.rtt_s, 3),
            "source": self.source,
            "raw": self.raw,
            "time_source": self.time_source,
            "gnss_status": self.gnss_status,
            "header_offset_s": self.header_offset_s,
            "warnings": list(self.warnings),
            "error": self.error,
            "note": self.note,
        }


def parse_sensor_time(value: Any) -> datetime | None:
    """'2026-11-04 17:24:51+0000' (or ISO 8601) as an aware UTC datetime; naive text is UTC."""
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("T", " ")
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+0000"
    # .NET writes seven fraction digits ("11:24:50.0010006"); Python reads six.
    candidate = re.sub(r"(\.\d{6})\d+", r"\1", candidate)
    for fmt in ("%Y-%m-%d %H:%M:%S%z", "%Y-%m-%d %H:%M:%S.%f%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_http_date(value: Any) -> datetime | None:
    from email.utils import parsedate_to_datetime

    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _gnss_text(values: dict) -> str:
    info = values.get("04-time-info") if isinstance(values.get("04-time-info"), dict) else {}
    if not info:
        return ""
    fix = str(info.get("02-time-fix") or "").strip()
    satellites = str(info.get("03-time-satellites") or "").strip()
    has_fix = fix not in {"", "\u2718", "x", "X", "no", "No"}
    source = str(info.get("01-time-source") or "").strip() or "GNSS"
    return f"{source}: {'time fix' if has_fix else 'no time fix'}" + (f", {satellites} satellites" if satellites else "")


def read_sensor_clock(
    get: Callable[[str], Any],
    *,
    now: Callable[[], float] = time.time,
    path: str = STATUS_PAGE_PATH,
) -> ClockReading:
    """GET the status page once and measure the offset. Read-only; never raises.

    ``get(path)`` returns an object with ``status``, ``headers`` and ``text``
    (``ntp_recovery.WebReply``) or raises.
    """
    import json

    before = now()
    try:
        reply = get(path)
    except Exception as exc:  # noqa: BLE001 - reported as "unknown"
        return ClockReading(available=False, error=f"{type(exc).__name__}: {exc}")
    after = now()
    status = int(getattr(reply, "status", 0) or 0)
    if getattr(reply, "error", ""):
        return ClockReading(available=False, error=str(reply.error))
    if status >= 400 or status == 0:
        return ClockReading(available=False, error=f"HTTP {status}")
    rtt = max(after - before, 0.0)
    middle = datetime.fromtimestamp(before + rtt / 2, tz=timezone.utc)
    try:
        body = json.loads(getattr(reply, "text", "") or "")
    except (TypeError, ValueError):
        body = None
    values = body.get("values") if isinstance(body, dict) and isinstance(body.get("values"), dict) else {}
    warnings = [
        str(item["message"]) for item in (body.get("pageErrors") or []) if isinstance(item, dict) and item.get("message")
    ] if isinstance(body, dict) else []
    headers = {str(k).casefold(): str(v) for k, v in (getattr(reply, "headers", None) or {}).items()}
    header_time = parse_http_date(headers.get("date"))
    header_offset = round((header_time - middle).total_seconds(), 3) if header_time is not None else None
    raw = str(values.get("02-system-time") or "").strip()
    system_time = parse_sensor_time(raw)
    common = dict(
        pc_utc=middle,
        rtt_s=rtt,
        time_source=str(values.get("03-time-source") or "").strip(),
        gnss_status=_gnss_text(values),
        header_offset_s=header_offset,
        warnings=warnings,
    )
    if system_time is not None:
        return ClockReading(
            available=True,
            sensor_utc=system_time,
            offset_s=round((system_time - middle).total_seconds(), 3),
            uncertainty_s=rtt / 2 + STATUS_RESOLUTION_S,
            source="status page system time (UTC)",
            raw=raw,
            **common,
        )
    if header_time is not None:
        return ClockReading(
            available=True,
            sensor_utc=header_time,
            offset_s=header_offset,
            uncertainty_s=rtt / 2 + STATUS_RESOLUTION_S,
            source="HTTP Date header (whole seconds)",
            raw=headers.get("date", ""),
            **common,
        )
    return ClockReading(available=False, error="the status page has no system time", **common)


def clock_verdict(reading: ClockReading | None) -> str:
    if reading is None or not reading.available or reading.offset_s is None:
        return UNKNOWN
    size = abs(float(reading.offset_s))
    margin = max(float(reading.uncertainty_s or 0.0), 0.0)
    if size - margin > CLOCK_INCORRECT_S:
        return INCORRECT
    if size <= CLOCK_SYNCED_S + margin:
        return CORRECT
    return DRIFTING


def offset_text(seconds: float) -> str:
    """'0.4 s', '6 min 40 s', '3.2 h', '4695 days'."""
    size = abs(float(seconds))
    if size < 120:
        return f"{size:.1f} s"
    if size < 3600:
        minutes, rest = divmod(int(round(size)), 60)
        return f"{minutes} min {rest} s" if rest else f"{minutes} min"
    if size < 2 * 86400:
        return f"{size / 3600:.1f} h"
    return f"{size / 86400:.0f} days"


def direction_text(seconds: float) -> str:
    return "ahead of" if seconds > 0 else "behind"


def reads_text(reading: ClockReading | None) -> str:
    """'reads 2012-01-02' when the sensor's date is wrong, else 'is 10 min behind'."""
    if reading is None or reading.sensor_utc is None or reading.offset_s is None:
        return "time unknown"
    pc = reading.pc_utc or datetime.now(timezone.utc)
    if reading.sensor_utc.date() != pc.astimezone(timezone.utc).date():
        return f"reads {reading.sensor_utc:%Y-%m-%d}"
    word = "ahead" if reading.offset_s > 0 else "behind"
    return f"is {offset_text(reading.offset_s)} {word}"


# ----------------------------------------------------------------------------- the computer's clock


@dataclass
class PcClock:
    trusted: bool
    source: str = ""
    offset_s: float | None = None  # computer minus the reference (SNTP)
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"trusted": self.trusted, "source": self.source, "offset_s": self.offset_s, "detail": self.detail}


def sntp_offset(server: str, *, timeout_s: float = SNTP_TIMEOUT_S, now: Callable[[], float] = time.time) -> tuple[float, float, int]:
    """(computer minus server in seconds, round trip, stratum) from one SNTP query. Raises on no answer."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(timeout_s)
        sent = now()
        sock.sendto(b"\x1b" + 47 * b"\0", (server, 123))
        data = sock.recv(48)
        received = now()
    if len(data) < 48:
        raise ValueError("short SNTP reply")
    leap, mode, stratum = data[0] >> 6, data[0] & 7, data[1]
    words = struct.unpack("!12I", data[:48])
    if leap == 3 or mode not in (4, 5) or not 1 <= stratum <= 15 or not words[10]:
        raise ValueError(f"server not synchronised (leap {leap}, mode {mode}, stratum {stratum})")
    server_rx = words[8] - _NTP_EPOCH + words[9] / 2**32
    server_tx = words[10] - _NTP_EPOCH + words[11] / 2**32
    server_minus_pc = ((server_rx - sent) + (server_tx - received)) / 2
    return -server_minus_pc, received - sent, stratum


def w32tm_status(*, timeout_s: float = 5.0) -> dict[str, str]:
    """`w32tm /query /status /verbose` as {label: value}; {} when it cannot be read (other OS, denied, other language)."""
    if sys.platform != "win32":
        return {}
    try:
        done = subprocess.run(
            ["w32tm", "/query", "/status", "/verbose"],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if done.returncode != 0:
        return {}
    return parse_w32tm(done.stdout)


def parse_w32tm(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in (text or "").splitlines():
        label, sep, value = line.partition(":")
        if sep and label.strip():
            out[label.strip()] = value.strip()
    return out


def w32tm_trust(status: dict[str, str]) -> PcClock:
    """Trusted when the Windows time service names a network source and synchronised within PC_SYNC_MAX_AGE_S."""
    if not status:
        return PcClock(False, "Windows time service", detail="status not readable")
    source = status.get("Source", "")
    lowered = source.casefold()
    if not source or "local cmos" in lowered or "free-running" in lowered:
        return PcClock(False, "Windows time service", detail=f"no network time source ({source or 'none'})")
    if status.get("Leap Indicator", "").startswith("3"):
        return PcClock(False, f"Windows time service ({source})", detail="not synchronised")
    match = re.match(r"\s*(\d+)", status.get("Stratum", ""))
    if match is None or not 1 <= int(match.group(1)) <= 15:
        return PcClock(False, f"Windows time service ({source})", detail=f"stratum {status.get('Stratum') or 'unknown'}")
    since = re.match(r"\s*([\d.]+)\s*s", status.get("Time since Last Good Sync Time", ""))
    if since is None:
        return PcClock(False, f"Windows time service ({source})", detail="last sync time not readable")
    age = float(since.group(1))
    if age > PC_SYNC_MAX_AGE_S:
        return PcClock(False, f"Windows time service ({source})", detail=f"last synchronised {age / 86400:.1f} days ago")
    return PcClock(True, f"Windows time service ({source})", detail=f"synchronised {age / 60:.0f} min ago")


def sntp_servers(preferred: Iterable[str]) -> list[str]:
    out: list[str] = []
    for name in [*preferred, *SNTP_FALLBACK_SERVERS]:
        text = str(name or "").strip()
        if text and text not in out:
            out.append(text)
    return out[:SNTP_MAX_QUERIES]


def check_pc_clock(
    servers: Iterable[str] = (),
    *,
    sntp: Callable[..., tuple[float, float, int]] = sntp_offset,
    w32tm: Callable[[], dict[str, str]] = w32tm_status,
) -> PcClock:
    """Is this computer's clock a reference good enough to judge (and fix) the sensor's? Never raises."""
    tried: list[str] = []
    for server in sntp_servers(servers):
        try:
            offset, rtt, stratum = sntp(server)
        except Exception as exc:  # noqa: BLE001 - next server, then the Windows time service
            tried.append(f"{server}: {type(exc).__name__}")
            continue
        detail = f"SNTP {server}: computer {offset:+.3f} s, round trip {rtt:.3f} s, stratum {stratum}"
        if abs(offset) > PC_TRUST_MAX_OFFSET_S:
            return PcClock(False, f"SNTP {server}", offset_s=round(offset, 3), detail=detail)
        return PcClock(True, f"SNTP {server}", offset_s=round(offset, 3), detail=detail)
    trust = w32tm_trust(w32tm())
    if tried:
        trust.detail = f"{trust.detail}; no SNTP answer ({'; '.join(tried)})"
    return trust


# ----------------------------------------------------------------------------- evidence from a recording reply


def reply_clock_evidence(reply: dict, *, now: datetime | None = None) -> str:
    """Why an EMP reply suggests a wrong sensor clock, or ''.

    The reply's ``timestamp`` ("2012-01-02T11:24:50.0010006", the sensor's clock,
    no offset: read as UTC) and its ``file_dir`` date folder are only triggers
    for a real check of the status page; a time zone misread costs one read.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = parse_sensor_time(reply.get("timestamp")) if isinstance(reply, dict) else None
    if stamp is not None and abs((stamp - current).total_seconds()) > CLOCK_INCORRECT_S:
        return f"recording reply timestamp {reply.get('timestamp')}"
    folder = str(reply.get("file_dir") or "") if isinstance(reply, dict) else ""
    match = re.search(r"/(\d{8})/?$", folder)
    if match:
        try:
            day = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            return ""
        allowed = {(current + timedelta(days=d)).date() for d in (-1, 0, 1)}
        allowed |= {(current.astimezone() + timedelta(days=d)).date() for d in (-1, 0, 1)}
        if day not in allowed:
            return f"recording folder {folder}"
    return ""


__all__ = [
    "CLOCK_INCORRECT_S",
    "CLOCK_SYNCED_S",
    "CORRECT",
    "ClockReading",
    "DRIFTING",
    "INCORRECT",
    "PcClock",
    "UNKNOWN",
    "check_pc_clock",
    "clock_verdict",
    "offset_text",
    "parse_sensor_time",
    "read_sensor_clock",
    "reads_text",
    "reply_clock_evidence",
]
