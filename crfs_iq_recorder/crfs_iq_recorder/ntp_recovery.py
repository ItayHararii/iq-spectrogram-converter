"""Refresh the sensor's NTP service when its clock is wrong, with its web page's own Save, then verify.

What the Node's web page does (main.js "CRFS Node Webpage v5.1.6", host software
2.25-4823, read from the Node 100-18 on 9 Nov 2026): Configuration > Network Time
Service (``/network/ntp``) loads ``GET /api/values/network/ntp.json``; its Submit
button sends ``PUT /api/values/network/ntp.json`` with
``Content-Type: application/json; charset=utf-8`` and ``JSON.stringify(values)``,
where ``values`` holds every widget's value present in the GET reply, in widget
order, read-only ones included. 2xx is "Done", 400 is "Invalid data".

The sensor rewrites chrony's configuration and restarts chrony (whose
``initstepslew`` then steps a wrong clock) only when the saved server list differs
from the current one; a Save of the same values changes nothing (measured on the
Node, 9 Nov 2026). So one refresh is a pair of Saves: the preferred servers with one
more copy of the first server (the same servers, a different list), then the list
exactly as read. A pending-restore record is kept from before the first Save until
the page reads back the original list, so a restart finishes an interrupted pair.

Rules kept here:
- nothing is enabled: a page without servers, or a sensor whose time source is
  another reference, is reported and left alone; no server is removed, reordered
  or invented;
- this computer's clock must be verified first (``sensor_clock.check_pc_clock``);
- the clock is measured again right before the pair, so a correct clock is never saved;
- success is the verified restore and then a verified clock (``SYNC_CONFIRMATIONS``
  readings in a row within ``CLOCK_SYNCED_S``, bounded by ``SYNC_TIMEOUT_S``),
  never a Save's HTTP answer;
- at most ``MAX_SAVES_PER_EPISODE`` refreshes (pairs) until the clock is right
  again, ``RETRY_GAP_S`` apart, then ``COOLDOWN_S``; kept in a small JSON file so a
  restart or a connection check cannot cause repeated refreshes.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .sensor_clock import (
    CORRECT,
    DRIFTING,
    INCORRECT,
    ClockReading,
    PcClock,
    check_pc_clock,
    clock_verdict,
    direction_text,
    offset_text,
    read_sensor_clock,
    reads_text,
)

NTP_PAGE = "/network/ntp"
NTP_PAGE_PATH = "/api/values/network/ntp.json"
SAVE_METHOD = "PUT"
SAVE_CONTENT_TYPE = "application/json; charset=utf-8"
SAVE_TIMEOUT_S = 30.0  # the page's own request timeout
READ_TIMEOUT_S = 10.0

MAX_SAVES_PER_EPISODE = 2
RETRY_GAP_S = 180.0
COOLDOWN_S = 1800.0
SYNC_TIMEOUT_S = 90.0
SYNC_POLL_S = 3.0
SYNC_CONFIRMATIONS = 2
RESTORE_RETRY_DELAYS_S = (2.0, 5.0, 10.0)
STORE_FILENAME = "sensor_clock_recovery.json"

NTP_ON = "on"
NTP_OFF = "off"
NTP_UNKNOWN = "unknown"
_OFF_WORDS = {"disabled", "disable", "off", "none", "false", "no"}
_OTHER_SOURCES = ("gps", "gnss", "ptp", "irig", "pps")

# Outcomes of recover_clock().
OK = "ok"  # the clock is right; nothing was done
SYNCED = "synced"
STILL_WRONG = "still_wrong"
SAVE_FAILED = "save_failed"
RESTORE_FAILED = "restore_failed"
PAGE_UNREADABLE = "page_unreadable"
NTP_IS_OFF = "ntp_off"
PC_UNTRUSTED = "pc_untrusted"
COOLDOWN = "cooldown"
DRIFT = "drifting"
UNREADABLE = "unreadable"
CANCELLED = "cancelled"

REFRESHING_TEXT = "Sensor clock incorrect - refreshing NTP"


# ----------------------------------------------------------------------------- web access


@dataclass
class WebReply:
    status: int = 0
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    error: str = ""
    elapsed_s: float = 0.0


class SensorWeb:
    """GET and PUT on the Node's web API with the saved web login (HTTP Basic). close() after use."""

    def __init__(self, origin: str, username: str, password: str, *, timeout_s: float = READ_TIMEOUT_S) -> None:
        self.origin = origin.rstrip("/")
        self._username = username
        self._password = password
        self._timeout_s = float(timeout_s or READ_TIMEOUT_S)
        self._session: Any = None

    def _send(self, method: str, path: str, *, headers: dict[str, str], data: bytes | None, timeout: float) -> WebReply:
        import requests
        from requests.auth import HTTPBasicAuth

        from .api_client import connection_failure_reason
        from .sanitizer import redact_text

        if self._session is None:
            self._session = requests.Session()
        started = time.monotonic()
        try:
            response = self._session.request(
                method,
                self.origin + path,
                headers=headers,
                data=data,
                auth=HTTPBasicAuth(self._username, self._password),
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.exceptions.ConnectionError as exc:
            return WebReply(error=connection_failure_reason(exc), elapsed_s=time.monotonic() - started)
        except requests.exceptions.RequestException as exc:
            return WebReply(error=redact_text(f"{type(exc).__name__}: {exc}")[:300], elapsed_s=time.monotonic() - started)
        return WebReply(
            status=int(response.status_code),
            headers={str(k): str(v) for k, v in response.headers.items()},
            text=response.text or "",
            elapsed_s=time.monotonic() - started,
        )

    def get(self, path: str) -> WebReply:
        return self._send("GET", path, headers={"Accept": "*/*"}, data=None, timeout=self._timeout_s)

    def put_json(self, path: str, body: bytes, *, page: str) -> WebReply:
        return self._send(SAVE_METHOD, path, headers=save_headers(self.origin, page), data=body, timeout=SAVE_TIMEOUT_S)

    def close(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception:  # noqa: BLE001
                pass


def save_headers(origin: str, page: str) -> dict[str, str]:
    """The headers a browser's XMLHttpRequest sends with the page's Submit (the login is added separately)."""
    return {
        "Content-Type": SAVE_CONTENT_TYPE,
        "Accept": "*/*",
        "Origin": origin,
        "Referer": origin + page,
    }


# ----------------------------------------------------------------------------- the NTP page


@dataclass
class NtpPage:
    values: dict[str, Any]
    widgets: list[dict[str, Any]]
    page_errors: list[str] = field(default_factory=list)

    def widget(self, wid: str) -> dict[str, Any]:
        return next((w for w in self.widgets if w.get("id") == wid), {})

    def editable_ids(self) -> list[str]:
        return [w["id"] for w in self.widgets if w.get("editable", True) is not False and w["id"] in self.values]

    def configured_servers(self) -> list[str]:
        """Servers the operator set (editable server fields), in their order, duplicates kept."""
        out: list[str] = []
        for wid in self.editable_ids():
            if "server" not in wid.casefold():
                continue
            value = self.values[wid]
            items = value if isinstance(value, list) else [value]
            out.extend(str(item).strip() for item in items if isinstance(item, str) and item.strip())
        return out

    def all_servers(self) -> list[str]:
        """Configured servers, then the one the sensor selected; no duplicates."""
        out: list[str] = []
        for wid, value in self.values.items():
            if "server" not in wid.casefold():
                continue
            for item in value if isinstance(value, list) else [value]:
                text = str(item).strip() if isinstance(item, str) else ""
                if text and text not in out:
                    out.append(text)
        configured = self.configured_servers()
        return [s for s in out if s in configured] + [s for s in out if s not in configured]

    def server_list_id(self) -> str:
        """The editable list of preferred servers ('' when the page has none)."""
        return next((wid for wid in self.editable_ids() if "server" in wid.casefold() and isinstance(self.values[wid], list)), "")

    def save_body(self) -> bytes:
        """``JSON.stringify(values)``: compact, keys in widget order, non-ASCII as UTF-8."""
        return _json_body(self.values)

    def body_with(self, wid: str, servers: list[Any]) -> bytes:
        """save_body() with one value replaced; keys, their order and every other value as read."""
        return _json_body({**self.values, wid: list(servers)})


def _json_body(values: dict[str, Any]) -> bytes:
    return json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def temporary_servers(original: list[Any]) -> list[Any] | None:
    """The list saved first: the original plus one more copy of its first server (None without a server)."""
    first = next((item for item in original if isinstance(item, str) and item.strip()), None)
    return None if first is None else [*original, first]


def parse_ntp_page(text: str) -> NtpPage | None:
    """The page's state from its GET reply: only values that have a widget, in widget order (as main.js keeps them)."""
    try:
        data = json.loads(text or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("values"), dict) or not isinstance(data.get("widgets"), list):
        return None
    raw = data["values"]
    widgets = [w for w in data["widgets"] if isinstance(w, dict) and isinstance(w.get("id"), str)]
    values = {w["id"]: raw[w["id"]] for w in widgets if w["id"] in raw}
    errors = [str(e.get("message")) for e in data.get("pageErrors") or [] if isinstance(e, dict) and e.get("message")]
    return NtpPage(values=values, widgets=widgets, page_errors=errors)


def ntp_state(page: NtpPage | None, time_source: str = "") -> tuple[str, str]:
    """(NTP_ON / NTP_OFF / NTP_UNKNOWN, detail). The page has no enable switch on firmware 2.25:
    NTP counts as off when an enable flag says so, the source says off, no server is set,
    or the status page names another reference. While the clock is unsynchronised the status
    page says "No accurate source", which says nothing about the configuration."""
    if page is None:
        return NTP_UNKNOWN, "the NTP page could not be read"
    for widget in page.widgets:
        wid = widget["id"]
        if widget.get("type") == "boolean" and "enable" in wid.casefold() and page.values.get(wid) is False:
            return NTP_OFF, f"{wid} is false"
    for wid, value in page.values.items():
        if "source" in wid.casefold() and isinstance(value, str) and value.strip().casefold() in _OFF_WORDS:
            return NTP_OFF, f"{wid} is {value!r}"
    if not any("server" in wid.casefold() for wid in page.editable_ids()):
        return NTP_UNKNOWN, "the NTP page has no server field"
    if not page.configured_servers():
        return NTP_OFF, "no NTP server is set"
    source = (time_source or "").strip()
    folded = source.casefold()
    if "ntp" not in folded and any(name in folded for name in _OTHER_SOURCES):
        return NTP_OFF, f"the sensor's time source is {source}"
    return NTP_ON, ", ".join(page.configured_servers())


# ----------------------------------------------------------------------------- retry policy


_STORE_LOCK = threading.Lock()


class RecoveryStore:
    """Refreshes per sensor until its clock is right again, the cooldown after a failed episode,
    and a server list that still has to be put back (``pending_restore``)."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path

    def file(self) -> Path:
        if self._path is not None:
            return self._path
        from .paths import user_config_dir

        return user_config_dir() / STORE_FILENAME

    def _load(self) -> dict[str, Any]:
        from .json_store import read_json

        data = read_json(self.file())
        data = data if isinstance(data, dict) else {}
        for name in ("sensors", "pending_restore"):
            if not isinstance(data.get(name), dict):
                data[name] = {}
        return data

    def _write(self, data: dict[str, Any]) -> bool:
        from .json_store import write_json

        try:
            write_json(self.file(), data)
        except OSError:
            return False
        return True

    def pending_restore(self, key: str) -> dict[str, Any]:
        """{"field", "original", "temporary", ...} of a pair that has not been verified restored, or {}."""
        with _STORE_LOCK:
            item = self._load()["pending_restore"].get(key)
        if not isinstance(item, dict) or not item.get("field"):
            return {}
        if not isinstance(item.get("original"), list) or not isinstance(item.get("temporary"), list):
            return {}
        return dict(item)

    def note_pending_restore(self, key: str, *, field_id: str, original: list[Any], temporary: list[Any], now: float) -> bool:
        """Written before the first Save of a pair; False when it could not be written (then nothing is sent)."""
        with _STORE_LOCK:
            data = self._load()
            data["pending_restore"][key] = {
                "field": field_id,
                "original": list(original),
                "temporary": list(temporary),
                "since": now,
                "since_local": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            }
            return self._write(data)

    def clear_pending_restore(self, key: str) -> None:
        with _STORE_LOCK:
            data = self._load()
            if data["pending_restore"].pop(key, None) is not None:
                self._write(data)

    def entry(self, key: str) -> dict[str, Any]:
        with _STORE_LOCK:
            item = self._load()["sensors"].get(key)
        return dict(item) if isinstance(item, dict) else {}

    def may_save(self, key: str, now: float) -> tuple[bool, float | None]:
        """(allowed, earliest next refresh as epoch seconds)."""
        item = self.entry(key)
        saves = int(item.get("saves") or 0)
        last = float(item.get("last_save") or 0.0)
        until = float(item.get("cooldown_until") or 0.0)
        if saves >= MAX_SAVES_PER_EPISODE and not until:
            until = last + COOLDOWN_S  # the result of the last save was never written
        if until and now < until:
            return False, until
        if until and now >= until:
            return True, None
        if last and now - last < RETRY_GAP_S:
            return False, last + RETRY_GAP_S
        return True, None

    def note_save(self, key: str, now: float) -> None:
        """One refresh started (a change-and-restore pair counts once, its restore retries not at all)."""
        with _STORE_LOCK:
            data = self._load()
            item = data["sensors"].get(key) if isinstance(data["sensors"].get(key), dict) else {}
            if float(item.get("cooldown_until") or 0.0) and now >= float(item["cooldown_until"]):
                item = {}  # the cooldown is over: a new episode
            item["saves"] = int(item.get("saves") or 0) + 1
            item["last_save"] = now
            item["last_save_local"] = datetime.fromtimestamp(now).isoformat(timespec="seconds")
            item.setdefault("episode_started", now)
            item.pop("cooldown_until", None)
            data["sensors"][key] = item
            self._write(data)

    def note_result(self, key: str, outcome: str, now: float) -> None:
        with _STORE_LOCK:
            data = self._load()
            if outcome == SYNCED:
                data["sensors"].pop(key, None)
                self._write(data)
                return
            item = data["sensors"].get(key)
            if not isinstance(item, dict):
                return
            item["last_outcome"] = outcome
            if int(item.get("saves") or 0) >= MAX_SAVES_PER_EPISODE:
                item["cooldown_until"] = now + COOLDOWN_S
                item["cooldown_until_local"] = datetime.fromtimestamp(now + COOLDOWN_S).isoformat(timespec="seconds")
                item["saves"] = 0
            self._write(data)

    def note_correct(self, key: str) -> None:
        with _STORE_LOCK:
            data = self._load()
            if key in data["sensors"]:
                data["sensors"].pop(key)
                self._write(data)


# ----------------------------------------------------------------------------- the procedure


@dataclass
class RecoveryResult:
    outcome: str
    reason: str = ""
    detail: str = ""
    before: ClockReading | None = None
    after: ClockReading | None = None
    pc: PcClock | None = None
    ntp: str = ""
    saved: bool = False  # a refresh was started: its first Save was sent
    original_servers: list[Any] | None = None
    temporary_servers: list[Any] | None = None
    restored: bool | None = None  # the page read back the original list (None: the list was never changed)
    saves: list[dict[str, Any]] = field(default_factory=list)
    read_backs: list[dict[str, Any]] = field(default_factory=list)
    pending: dict[str, Any] | None = None  # what happened to an earlier pair's pending restore
    page_before: dict[str, Any] | None = None
    next_save_at: float | None = None
    polls: list[dict[str, Any]] = field(default_factory=list)
    timing: dict[str, Any] = field(default_factory=dict)
    finished_at: float = 0.0

    def final_reading(self) -> ClockReading | None:
        return self.after or self.before

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason,
            "detail": self.detail,
            "before": self.before.to_dict() if self.before is not None else None,
            "after": self.after.to_dict() if self.after is not None else None,
            "pc_clock": self.pc.to_dict() if self.pc is not None else None,
            "ntp": self.ntp,
            "saved": self.saved,
            "original_servers": self.original_servers,
            "temporary_servers": self.temporary_servers,
            "restored": self.restored,
            "saves": list(self.saves),
            "read_backs": list(self.read_backs),
            "pending_restore": self.pending,
            "page_before": self.page_before,
            "next_save_at": datetime.fromtimestamp(self.next_save_at).isoformat(timespec="seconds") if self.next_save_at else None,
            "polls": list(self.polls),
            "timing": dict(self.timing),
        }


def result_message(result: RecoveryResult) -> tuple[str, str] | None:
    """(Activity line, level) for an outcome, or None when there is nothing to tell the operator."""
    reading = result.final_reading()
    if result.outcome == RESTORE_FAILED:
        return "Sensor NTP server list not restored. See details.", "error"
    if result.outcome == SYNCED:
        return ("Sensor clock synchronized.", "info") if result.restored else None
    if result.outcome == STILL_WRONG:
        return f"Sensor clock still wrong after NTP refresh ({reads_text(reading)}). New recordings will be misdated.", "error"
    if result.outcome in {SAVE_FAILED, PAGE_UNREADABLE}:
        return "Could not refresh NTP on the sensor. See details.", "error"
    if result.outcome == NTP_IS_OFF:
        return "Sensor clock incorrect - NTP is off on the sensor.", "error"
    if result.outcome == PC_UNTRUSTED:
        return "Sensor clock looks wrong, but this computer's clock is not verified. NTP not refreshed.", "error"
    if result.outcome == COOLDOWN:
        when = f" Next NTP refresh after {datetime.fromtimestamp(result.next_save_at):%H:%M}." if result.next_save_at else ""
        return f"Sensor clock still wrong ({reads_text(reading)}).{when}", "error"
    if result.outcome == DRIFT and reading is not None and reading.offset_s is not None:
        return f"Sensor clock is {offset_text(reading.offset_s)} {direction_text(reading.offset_s)} the computer.", "info"
    return None


def restore_message(result: RecoveryResult) -> tuple[str, str] | None:
    """The activity line when this check put back the list an earlier, unfinished refresh left changed."""
    if result.pending is not None and result.pending.get("action") == "restored":
        return "Sensor NTP server list restored.", "info"
    return None


def _put_back_detail(original: list[Any]) -> str:
    return f"Put back the preferred servers {json.dumps(original, ensure_ascii=False)} on the sensor's NTP page."


def _reading_brief(reading: ClockReading, t: float) -> dict[str, Any]:
    return {
        "t": round(t, 1),
        "verdict": clock_verdict(reading),
        "offset_s": reading.offset_s,
        "uncertainty_s": round(reading.uncertainty_s, 3),
        "raw": reading.raw,
        "error": reading.error,
    }


def recover_clock(
    web: Any,
    store: RecoveryStore,
    key: str,
    *,
    reason: str = "",
    pc_check: Callable[..., PcClock] = check_pc_clock,
    now: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    wait: Callable[[float], bool] | None = None,
    notify: Callable[[str], None] | None = None,
    pair_idle: threading.Event | None = None,
) -> RecoveryResult:
    """Check the clock; when it is wrong and every rule allows it, refresh NTP with the page's Submit and verify.

    ``web`` has ``get(path)``, ``put_json(path, body, page=...)`` and ``origin``.
    ``wait(seconds)`` returns True when the caller cancelled; a cancel never skips
    the first restore of a pair that was started. ``pair_idle`` is cleared while
    the server list may differ from the original. Never raises.
    """
    wait = wait or threading.Event().wait
    pair_idle = pair_idle if pair_idle is not None else threading.Event()
    try:
        return _recover(web, store, key, reason=reason, pc_check=pc_check, now=now, monotonic=monotonic, wait=wait, notify=notify, pair_idle=pair_idle)
    except Exception as exc:  # noqa: BLE001 - a programming error must not leave the caller's hold set
        import traceback

        return RecoveryResult(UNREADABLE, reason=reason, detail=f"internal error: {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}", finished_at=now())
    finally:
        pair_idle.set()


class _Saves:
    """The Saves of one refresh and the page read-backs after them, as the diagnostic log shows them."""

    def __init__(self, web: Any, result: RecoveryResult, *, now: Callable[[], float], monotonic: Callable[[], float], wait: Callable[[float], bool]) -> None:
        self._web = web
        self._result = result
        self._now = now
        self._monotonic = monotonic
        self._wait = wait
        self.t0 = monotonic()
        self.restore_t: float | None = None

    def _stamp(self) -> dict[str, Any]:
        return {"t": round(self._monotonic() - self.t0, 3), "at": datetime.fromtimestamp(self._now()).isoformat(timespec="milliseconds")}

    def save(self, kind: str, body: bytes) -> WebReply:
        stamp = self._stamp()
        reply = self._web.put_json(NTP_PAGE_PATH, body, page=NTP_PAGE)
        self._result.saves.append({
            "kind": kind,
            **stamp,
            "method": SAVE_METHOD,
            "path": NTP_PAGE_PATH,
            "headers": save_headers(getattr(self._web, "origin", ""), NTP_PAGE),
            "body": body.decode("utf-8"),
            "status": reply.status,
            "error": reply.error,
            "elapsed_s": round(reply.elapsed_s, 3),
            "response": (reply.text or "")[:2000],
        })
        return reply

    def servers(self, wid: str, after: str, original: list[Any]) -> list[Any] | None:
        """The page's server list now (None when the page cannot be read)."""
        stamp = self._stamp()
        reply = self._web.get(NTP_PAGE_PATH)
        page = parse_ntp_page(reply.text) if not reply.error and 200 <= reply.status < 300 else None
        servers = page.values.get(wid) if page is not None else None
        self._result.read_backs.append({
            "after": after,
            **stamp,
            "status": reply.status,
            "error": reply.error,
            "servers": servers,
            "matches_original": servers == original,
        })
        return servers

    def put_back(self, kind: str, body: bytes, wid: str, original: list[Any]) -> tuple[bool, bool]:
        """(restored, cancelled): Save the original values until the page reads back the original list,
        at once and then after each of RESTORE_RETRY_DELAYS_S. A cancel stops the retries, never the first try."""
        for attempt, delay in enumerate((0.0, *RESTORE_RETRY_DELAYS_S)):
            if delay and self._wait(delay):
                return False, True
            label = kind if attempt == 0 else f"{kind} retry {attempt}"
            self.save(label, body)
            if self.servers(wid, label, original) == original:
                self.restore_t = self._result.saves[-1]["t"]
                return True, False
        return False, False


def _finish_pending_restore(web, store, key, record, result, *, now, monotonic, wait) -> bool:
    """An earlier refresh did not end with a verified restore (the app closed, the restore failed).
    Put the original list back first, but only if the page still shows exactly the temporary list.
    False: stop this check here (``result`` says why)."""
    wid, original, temporary = record["field"], record["original"], record["temporary"]
    result.pending = {"since": record.get("since_local"), "original": original, "temporary": temporary}
    saves = _Saves(web, result, now=now, monotonic=monotonic, wait=wait)
    reply = web.get(NTP_PAGE_PATH)
    page = parse_ntp_page(reply.text) if not reply.error and 200 <= reply.status < 300 else None
    if page is None:
        result.pending["action"] = "page unreadable; kept for the next check"
        result.outcome = PAGE_UNREADABLE
        result.detail = reply.error or f"HTTP {reply.status}"
        return False
    current = page.values.get(wid)
    result.pending["found"] = current
    if current != temporary:
        store.clear_pending_restore(key)
        result.pending["action"] = "already the original list" if current == original else "the page shows another list; nothing written"
        return True
    restored, cancelled = saves.put_back("pending restore", page.body_with(wid, original), wid, original)
    if restored:
        store.clear_pending_restore(key)
        result.pending["action"] = "restored"
        return True
    result.pending["action"] = "restore not verified; kept for the next check"
    result.restored = False
    result.original_servers, result.temporary_servers = original, temporary
    result.outcome = CANCELLED if cancelled else RESTORE_FAILED
    result.detail = _put_back_detail(original)
    return False


def _recover(web, store, key, *, reason, pc_check, now, monotonic, wait, notify, pair_idle) -> RecoveryResult:
    def read() -> ClockReading:
        return read_sensor_clock(web.get, now=now)

    result = RecoveryResult(UNREADABLE, reason=reason)
    record = store.pending_restore(key)
    if record:
        pair_idle.clear()
        try:
            if not _finish_pending_restore(web, store, key, record, result, now=now, monotonic=monotonic, wait=wait):
                result.finished_at = now()
                return result
        finally:
            pair_idle.set()

    not_wrong = {CORRECT: OK, DRIFTING: DRIFT}
    before = read()
    result.before = before
    verdict = clock_verdict(before)
    if verdict != INCORRECT:
        result.outcome = not_wrong.get(verdict, UNREADABLE)
        result.detail = before.error
        if verdict == CORRECT:
            store.note_correct(key)
        result.finished_at = now()
        return result

    allowed, next_at = store.may_save(key, now())
    if not allowed:
        result.outcome, result.next_save_at = COOLDOWN, next_at
        result.detail = "save limit or cooldown"
        result.finished_at = now()
        return result

    page_reply = web.get(NTP_PAGE_PATH)
    page = parse_ntp_page(page_reply.text) if not page_reply.error and 200 <= page_reply.status < 300 else None
    if page is None:
        result.outcome = PAGE_UNREADABLE
        result.detail = page_reply.error or f"HTTP {page_reply.status}"
        result.finished_at = now()
        return result
    result.page_before = dict(page.values)
    state, detail = ntp_state(page, before.time_source)
    result.ntp = f"{state}: {detail}"
    if state != NTP_ON:
        result.outcome = NTP_IS_OFF if state == NTP_OFF else PAGE_UNREADABLE
        result.detail = detail
        result.finished_at = now()
        return result

    result.pc = pc_check(page.all_servers())
    if not result.pc.trusted:
        result.outcome = PC_UNTRUSTED
        result.detail = result.pc.detail
        result.finished_at = now()
        return result

    wid = page.server_list_id()
    original = list(page.values[wid]) if wid else []
    temporary = temporary_servers(original)
    if temporary is None:
        result.outcome = PAGE_UNREADABLE
        result.detail = "the NTP page has no list of preferred servers"
        result.finished_at = now()
        return result

    # Never save a clock that is right: measure again just before the pair.
    again = read()
    result.polls.append(_reading_brief(again, 0.0))
    if clock_verdict(again) != INCORRECT:
        result.after = again
        result.outcome = not_wrong.get(clock_verdict(again), UNREADABLE)
        if result.outcome == OK:
            store.note_correct(key)
        result.detail = "the clock was right when measured again"
        result.finished_at = now()
        return result

    if notify is not None:
        notify(REFRESHING_TEXT)
    result.original_servers, result.temporary_servers = original, temporary
    pair_idle.clear()
    try:
        if wait(0):
            result.outcome = CANCELLED
            result.detail = "stopped before the refresh; nothing was sent"
            result.finished_at = now()
            return result
        if not store.note_pending_restore(key, field_id=wid, original=original, temporary=temporary, now=now()):
            result.outcome = SAVE_FAILED
            result.detail = f"the restore record could not be written ({store.file()}); nothing was sent"
            result.finished_at = now()
            return result
        store.note_save(key, now())
        result.saved = True
        saves = _Saves(web, result, now=now, monotonic=monotonic, wait=wait)
        result.timing["first_save_at"] = datetime.fromtimestamp(now()).isoformat(timespec="milliseconds")
        first = saves.save("temporary", page.body_with(wid, temporary))
        if first.error or not 200 <= first.status < 300:
            return _first_save_failed(saves, store, key, first, page, wid, original, temporary, result, now=now)
        restored, cancelled = saves.put_back("restore", page.save_body(), wid, original)
        result.restored = restored
        if not restored:
            result.outcome = CANCELLED if cancelled else RESTORE_FAILED
            result.detail = _put_back_detail(original)
            store.note_result(key, result.outcome, now())
            result.finished_at = now()
            return result
        store.clear_pending_restore(key)
        result.timing["restore_save_s"] = saves.restore_t
        result.timing["restore_verified_s"] = round(monotonic() - saves.t0, 3)
    finally:
        pair_idle.set()

    restored_at = monotonic()
    deadline = restored_at + SYNC_TIMEOUT_S
    good = 0
    while monotonic() < deadline:
        if wait(SYNC_POLL_S):
            result.outcome = CANCELLED
            result.detail = "stopped while waiting for the clock"
            store.note_result(key, CANCELLED, now())
            result.finished_at = now()
            return result
        reading = read()
        result.after = reading
        t = monotonic() - saves.t0
        result.polls.append(_reading_brief(reading, t))
        if clock_verdict(reading) == CORRECT:
            if "first_correct_s" not in result.timing:
                result.timing["first_correct_s"] = round(t, 1)
                result.timing["first_correct_after_restore_save_s"] = round(t - (saves.restore_t or 0.0), 1)
            good += 1
            if good >= SYNC_CONFIRMATIONS:
                result.outcome = SYNCED
                store.note_result(key, SYNCED, now())
                result.finished_at = now()
                return result
        else:
            good = 0
    result.outcome = STILL_WRONG
    result.detail = f"not synchronised {SYNC_TIMEOUT_S:.0f} s after the restored server list"
    store.note_result(key, STILL_WRONG, now())
    _, result.next_save_at = store.may_save(key, now())
    result.finished_at = now()
    return result


def _first_save_failed(saves: _Saves, store, key, reply: WebReply, page: NtpPage, wid: str, original, temporary, result: RecoveryResult, *, now) -> RecoveryResult:
    """The temporary list was refused: normally nothing changed; put the list back only if the page shows it changed."""
    result.outcome = SAVE_FAILED
    result.detail = reply.error or f"HTTP {reply.status}"
    servers = saves.servers(wid, "temporary refused", original)
    if servers == original:
        store.clear_pending_restore(key)
    elif servers == temporary:
        restored, cancelled = saves.put_back("restore", page.save_body(), wid, original)
        result.restored = restored
        if restored:
            store.clear_pending_restore(key)
        else:
            result.outcome = CANCELLED if cancelled else RESTORE_FAILED
            result.detail = _put_back_detail(original)
    elif servers is None:
        result.detail += "; the page could not be read back, so the next check looks again"
    else:
        store.clear_pending_restore(key)
        result.detail += f"; the server list now reads {json.dumps(servers, ensure_ascii=False)}, left as it is"
    store.note_result(key, result.outcome, now())
    result.finished_at = now()
    return result


__all__ = [
    "COOLDOWN",
    "NTP_PAGE_PATH",
    "NtpPage",
    "RESTORE_FAILED",
    "RecoveryResult",
    "RecoveryStore",
    "SYNCED",
    "SensorWeb",
    "WebReply",
    "ntp_state",
    "parse_ntp_page",
    "recover_clock",
    "restore_message",
    "result_message",
    "temporary_servers",
]
