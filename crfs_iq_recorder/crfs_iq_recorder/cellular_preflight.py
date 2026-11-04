"""Pre-collection check, run on a worker before every pass of an Excel Collection.

What is checked (and what stops the pass):
- the sensor answers and is the one the collection was planned for (stops);
- its clock against the computer's, from the sensor's own status page (reported);
- sensor storage and local storage with the stated reserve, temp space included
  (stops when a complete pass cannot fit; estimates are labelled as such);
- the local destination is writable (stops);
- the protocol workbook and worksheet are reachable (reported only: a loaded
  plan records on, results wait in the queue - "Excel updates waiting");
- the plan's ranges and durations are valid as entered (stops; nothing is
  adjusted).

Everything that cannot be read says "unknown". Nothing here records, deletes
or changes a setting on the sensor.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .cellular_quality import (
    LEVEL_ERROR,
    LEVEL_INFO,
    LEVEL_OK,
    LEVEL_WARNING,
    Check,
    blocking_checks,
    pre_run_checks,
    worst_level,
)

WORKBOOK_WAITING = "Excel updates waiting"


@dataclass
class PreflightResult:
    checks: list[Check] = field(default_factory=list)
    identity: dict[str, Any] = field(default_factory=dict)
    clock: dict[str, Any] = field(default_factory=dict)
    storage: dict[str, Any] = field(default_factory=dict)
    workbook_state: str = ""
    started_utc: str = ""
    finished_utc: str = ""
    duration_s: float = 0.0

    @property
    def level(self) -> str:
        return worst_level(self.checks)

    def blocking(self) -> list[Check]:
        return blocking_checks(self.checks)

    @property
    def ok(self) -> bool:
        return not self.blocking()

    def problems(self) -> list[Check]:
        return [c for c in self.checks if c.level in {LEVEL_ERROR, LEVEL_WARNING}]

    def summary_line(self) -> str:
        """One line for the activity log."""
        blocking = self.blocking()
        if blocking:
            return "Pre-collection check failed: " + " ".join(c.message for c in blocking)
        warnings = [c for c in self.checks if c.level == LEVEL_WARNING]
        errors = [c for c in self.checks if c.level == LEVEL_ERROR]
        if errors or warnings:
            return f"Pre-collection check passed with {len(errors) + len(warnings)} note(s). See details."
        return f"Pre-collection check passed ({len(self.checks)} checks)."

    def excel_waiting(self) -> bool:
        return any(c.name == "workbook" and c.level in {LEVEL_WARNING, LEVEL_ERROR} for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "identity": dict(self.identity),
            "clock": dict(self.clock),
            "storage": dict(self.storage),
            "workbook_state": self.workbook_state,
            "started_utc": self.started_utc,
            "finished_utc": self.finished_utc,
            "duration_s": round(self.duration_s, 3),
        }


def _identity_dict(info: Any) -> dict[str, Any]:
    def clean(value: Any) -> str:
        text = str(value or "").strip()
        return "" if text in {"—", "-", "?"} else text

    return {
        "connected": bool(getattr(info, "connected", False)),
        "model": clean(getattr(info, "model", "")),
        "serial": clean(getattr(info, "serial", "")),
        "firmware": clean(getattr(info, "firmware", "")),
        "status": str(getattr(info, "status", "") or ""),
    }


def gather_preflight(
    *,
    plan: Any,
    local_dir: Path | str,
    reserve_bytes: int,
    expected_identity: dict[str, str] | None,
    workbook_path: str | Path | None,
    workbook_sheet: str,
    protocol_loaded: bool,
    fetch_info: Callable[[], Any],
    fetch_clock: Callable[[], Any] | None,
    fetch_storage: Callable[[], Any] | None,
    workbook_state: Callable[[Path], str] | None,
    previous_identity: dict[str, str] | None = None,
    now: datetime | None = None,
    check_writable: bool = True,
) -> PreflightResult:
    """Collect every reading (each one guarded) and turn them into checks. Never raises."""
    started = time.monotonic()
    result = PreflightResult(started_utc=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    notes: list[Check] = []

    info: Any = None
    try:
        info = fetch_info()
    except Exception as exc:  # noqa: BLE001 - reported as "not connected"
        notes.append(Check("connection", LEVEL_ERROR, f"Sensor identity could not be read ({type(exc).__name__}: {exc})."))
    result.identity = _identity_dict(info)

    clock: Any = None
    if fetch_clock is not None and result.identity.get("connected"):
        try:
            clock = fetch_clock()
        except Exception as exc:  # noqa: BLE001
            notes.append(Check("sensor_clock", LEVEL_INFO, f"Sensor clock: unknown ({type(exc).__name__}: {exc})."))
    clock_offset = None
    clock_source = ""
    sensor_time = None
    gnss = None
    if clock is not None:
        result.clock = clock.to_dict() if hasattr(clock, "to_dict") else dict(clock)
        if getattr(clock, "available", False):
            clock_offset = getattr(clock, "offset_s", None)
            sensor_time = getattr(clock, "sensor_utc", None)
            clock_source = getattr(clock, "source", "") or ""
            if getattr(clock, "time_source", ""):
                clock_source = f"{clock_source}; sensor time source {clock.time_source}"
            gnss = getattr(clock, "gnss_status", "") or None
        else:
            result.clock["error"] = getattr(clock, "error", "") or "unavailable"

    storage: Any = None
    if fetch_storage is not None and result.identity.get("connected"):
        try:
            storage = fetch_storage()
        except Exception as exc:  # noqa: BLE001
            notes.append(Check("sensor_storage", LEVEL_WARNING, f"Sensor storage: unknown ({type(exc).__name__}: {exc})."))
    if storage is not None:
        result.storage = {
            "ok": bool(getattr(storage, "ok", False)),
            "total_bytes": int(getattr(storage, "total_bytes", 0) or 0),
            "free_bytes": int(getattr(storage, "free_bytes", 0) or 0),
            "detail": str(getattr(storage, "detail", "") or ""),
        }

    state = None
    if workbook_path:
        file = Path(str(workbook_path))
        if not file.is_file():
            state = "missing"
        elif workbook_state is not None:
            try:
                state = workbook_state(file)
            except Exception:  # noqa: BLE001 - treat as busy: never write behind an unknown Excel state
                state = "busy"
        else:
            state = "closed"
    result.workbook_state = state or ""

    checks = pre_run_checks(
        sensor_info=info if info is not None else _Blank(),
        sensor_storage=storage,
        local_dir=local_dir,
        plan=plan,
        local_reserve_bytes=int(reserve_bytes),
        sensor_time=sensor_time,
        previous_identity=previous_identity,
        gnss_status=gnss,
        now=now,
        expected_identity=expected_identity,
        clock_offset_s=clock_offset,
        clock_source=clock_source,
        workbook_path=workbook_path,
        workbook_sheet=workbook_sheet,
        workbook_state=state,
        protocol_loaded=protocol_loaded,
        check_writable=check_writable,
    )
    # A reading that failed replaces the check made from its absence.
    names = {c.name for c in notes}
    result.checks = notes + [c for c in checks if c.name not in names]
    result.finished_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    result.duration_s = time.monotonic() - started
    return result


class _Blank:
    connected = False
    model = ""
    serial = ""
    firmware = ""
    status = "no answer"


@dataclass
class _DemoInfo:
    model: str
    serial: str
    firmware: str
    connected: bool = True
    demo: bool = True
    status: str = "Demo"


def default_probes(host: Any, conn: Any) -> dict[str, Callable | None]:
    """The real readers for a runner host (MainWindow): identity and clock over HTTP, storage over SFTP."""
    demo = bool(getattr(conn, "demo", False))

    def fetch_info():
        if demo:
            # The demo sensor is "connected" by definition; its identity is what the window shows.
            info = getattr(host, "_sensor_info", None)
            return _DemoInfo(
                model=str(getattr(info, "model", "") or "Demo"),
                serial=str(getattr(info, "serial", "") or "demo"),
                firmware=str(getattr(info, "firmware", "") or "demo"),
            )
        make_target = getattr(host, "_target", None)
        make_client = getattr(host, "_client", None)
        if make_target is None or make_client is None:
            return getattr(host, "_sensor_info", None)
        return make_client().fetch_sensor_info(make_target(), getattr(conn, "http_password", ""))

    def fetch_clock():
        make_target = getattr(host, "_target", None)
        make_client = getattr(host, "_client", None)
        if make_target is None or make_client is None:
            if demo:
                from .api_client import SensorClock

                now = datetime.now(timezone.utc)
                return SensorClock(available=True, sensor_utc=now, pc_utc=now, offset_s=0.0, source="demo", time_source="demo")
            return None
        client = make_client()
        return client.fetch_sensor_clock(make_target(), getattr(conn, "http_password", ""))

    def fetch_storage():
        from .sensor_storage import query_iq_storage

        if demo:
            from .demo_sftp import shared_demo_browser

            return query_iq_storage(shared_demo_browser())
        from .sftp_window import _open_browser

        browser = _open_browser(conn)
        try:
            return query_iq_storage(browser)
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    def workbook_state(path: Path) -> str:
        from .excel_com import excel_workbook_state

        return excel_workbook_state(path)

    return {"fetch_info": fetch_info, "fetch_clock": fetch_clock, "fetch_storage": fetch_storage, "workbook_state": workbook_state}


__all__ = ["PreflightResult", "WORKBOOK_WAITING", "default_probes", "gather_preflight"]
