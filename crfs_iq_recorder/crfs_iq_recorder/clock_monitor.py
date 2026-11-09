"""When the sensor's clock is checked and when NTP may be refreshed: the main window's side of ntp_recovery.

Checks run when the sensor connects, at most every ``RECHECK_S`` on the
connection checks, before a collection pass, and when a recording reply carries
a date far from this computer's (``sensor_clock.reply_clock_evidence``).

A check only starts while the sensor is idle: no recording, no confirmation of
one, no collection capture. Otherwise it waits and starts at the next capture
boundary, before the next capture. While a check runs (bounded by
``HOLD_MAX_S``) a new capture is held: ``claim_for_capture`` returns
``HOLD_TEXT``, which the collection runner treats as "busy, try again" and
which a manual Start waits out and then resumes.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Qt, Signal, Slot

from .connection_monitor import LINK_CHECK_TIMEOUT_S
from .diagnostic_log import diag
from .ntp_recovery import (
    COOLDOWN,
    DRIFT,
    NTP_IS_OFF,
    OK,
    PC_UNTRUSTED,
    SYNCED,
    RecoveryResult,
    RecoveryStore,
    SensorWeb,
    recover_clock,
    restore_message,
    result_message,
)
from .recording_status import FINALIZING, RECORDING, SUBMITTING
from .sensor_clock import CORRECT, DRIFTING, INCORRECT, PcClock, check_pc_clock, clock_verdict
from .sftp_paths import sensor_clock_offset, set_sensor_clock_offset

HOLD_TEXT = "Sensor clock check in progress."
RECHECK_S = 600.0
EVIDENCE_GAP_S = 60.0
PENDING_POLL_MS = 3000
HOLD_MAX_S = 240.0
WAIT_MAX_S = HOLD_MAX_S + 10.0
PC_CACHE_S = 600.0
RESTORE_ON_SHUTDOWN_S = 5.0
SAVE_QUIET_S = LINK_CHECK_TIMEOUT_S + 1.0
# Shown once until the clock is right again; the other outcomes each time.
_SHOWN_ONCE = {NTP_IS_OFF, PC_UNTRUSTED, COOLDOWN, DRIFT}


class _PairIdle(threading.Event):
    """Set while no change-and-restore pair runs; ``ended_at`` is when the last one ended (monotonic)."""

    def __init__(self) -> None:
        super().__init__()
        self.ended_at = float("-inf")
        super().set()

    def set(self) -> None:
        if not self.is_set():
            self.ended_at = time.monotonic()
        super().set()


class SensorClockMonitor(QObject):
    notice = Signal(str, str)
    _requested = Signal(str, bool)

    def __init__(
        self,
        host: Any,
        *,
        web_factory: Callable[[], Any] | None = None,
        store: RecoveryStore | None = None,
        pc_check: Callable[..., PcClock] | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent if parent is not None else host)
        self._host = host
        self._web_factory = web_factory
        self._store = store or RecoveryStore()
        self._pc_check = pc_check or check_pc_clock
        self._pc_cache: tuple[float, PcClock, tuple[str, ...]] | None = None
        self._running = False
        self._run_key = ""
        self._token = 0
        self._cancel = threading.Event()
        self._pair_idle = _PairIdle()
        self._pending = ""
        self._last_started: float | None = None
        self._resume: Callable[[], None] | None = None
        self._waiters: list[threading.Event] = []
        self._lock = threading.Lock()
        self._shown: set[str] = set()
        self.last_result: RecoveryResult | None = None
        self.last_finished: float | None = None
        self._pending_timer = QTimer(self)
        self._pending_timer.setInterval(PENDING_POLL_MS)
        self._pending_timer.timeout.connect(self._try_pending)
        self._watchdog = QTimer(self)
        self._watchdog.setSingleShot(True)
        self._watchdog.timeout.connect(self._on_watchdog)
        queued = Qt.ConnectionType.QueuedConnection
        self._requested.connect(self._on_requested, queued)
        self.notice.connect(self._show_notice, queued)

    # ------------------------------------------------------------------ public
    @property
    def running(self) -> bool:
        return self._running

    def request_check(self, reason: str, *, force: bool = False) -> None:
        """Ask for a check (any thread). Without ``force`` at most every RECHECK_S."""
        self._requested.emit(reason, force)

    def note_sensor_reply(self, reply: dict) -> None:
        from .sensor_clock import reply_clock_evidence

        why = reply_clock_evidence(reply)
        if not why:
            return
        recent = self._last_started is not None and time.monotonic() - self._last_started < EVIDENCE_GAP_S
        diag("sensor_clock_evidence", why, checked_recently=recent)
        if not recent:
            self.request_check(why, force=True)

    def claim_for_capture(self, resume: Callable[[], None] | None = None) -> str | None:
        """HOLD_TEXT while a check runs (or one that was waiting starts now); None to go ahead."""
        if not self._running and self._pending and self._enabled() and not self.busy_reason():
            self._start(self._pending)
        if not self._running:
            return None
        if resume is not None:
            self._resume = resume
        return HOLD_TEXT

    def wait_for_check(self, reason: str, timeout_s: float = WAIT_MAX_S) -> None:
        """Run a check now and wait for it (a worker thread only; never the UI thread)."""
        if QThread.currentThread() is self.thread():
            self.request_check(reason, force=True)
            return
        done = threading.Event()
        with self._lock:
            self._waiters.append(done)
        self._requested.emit(reason, True)
        done.wait(timeout_s)

    def reset(self) -> None:
        """The connection settings were saved: forget this sensor's state (the saved retry policy stays).
        A check of the same sensor keeps running."""
        if self._running and self._enabled() and self._key() == self._run_key:
            return
        self._token += 1
        self._cancel.set()
        self._running = False
        self._pending = ""
        self._pending_timer.stop()
        self._watchdog.stop()
        self._last_started = None
        self._shown.clear()
        self._pc_cache = None
        self.last_result = None
        self._resume = None
        self._set_folder_hint(None)
        self._release_waiters()

    def shutdown(self) -> None:
        """Stop; a refresh whose server list is not yet restored gets RESTORE_ON_SHUTDOWN_S to restore it
        (otherwise its pending-restore record finishes it at the next start)."""
        self._token += 1
        self._cancel.set()
        if not self._pair_idle.wait(RESTORE_ON_SHUTDOWN_S):
            diag("sensor_clock_shutdown", "NTP server list restore not finished; it is finished at the next start")
        self._running = False
        self._pending = ""
        self._pending_timer.stop()
        self._watchdog.stop()
        self._resume = None
        self._release_waiters()

    def saving_ntp_page(self) -> bool:
        """True during a change-and-restore pair and SAVE_QUIET_S after it. The Node answers no other
        request while it saves its NTP page (6-10 s per Save on firmware 2.25), so a link check that
        overlapped a Save times out although the sensor is there."""
        return not self._pair_idle.is_set() or time.monotonic() - self._pair_idle.ended_at < SAVE_QUIET_S

    def busy_reason(self) -> str:
        """Why NTP must not be touched now ('' when the sensor is idle)."""
        if not self._pair_idle.is_set():
            return "NTP server list restore"
        host = self._host
        if getattr(host, "_recording_active", False) or getattr(host, "_submit_lock", False) or getattr(host, "_http_in_flight", False):
            return "recording"
        if getattr(host, "_record_phase", "") in {SUBMITTING, RECORDING}:
            return "recording"
        if getattr(host, "_watching", False) or getattr(host, "_measuring", False) or getattr(host, "_record_phase", "") == FINALIZING:
            return "finalizing"
        if getattr(host, "_external_capture", None) is not None:
            return "collection capture"
        runner = getattr(host, "_cellular", None)
        if runner is not None and getattr(runner, "_capture_in_flight", False):
            return "collection capture"
        return ""

    # ------------------------------------------------------------------ internals
    def _enabled(self) -> bool:
        if self._web_factory is not None:
            return True
        conn = getattr(self._host, "_conn", None)
        return conn is not None and not conn.demo and bool(conn.host.strip())

    def _key(self) -> str:
        conn = getattr(self._host, "_conn", None)
        return (conn.host.strip().casefold() if conn is not None else "") or "sensor"

    def _make_web(self) -> Any:
        if self._web_factory is not None:
            return self._web_factory()
        target = self._host._target()  # noqa: SLF001
        return SensorWeb(target.origin, target.username, self._host._conn.http_password, timeout_s=min(float(target.timeout_s or 10.0), 10.0))  # noqa: SLF001

    def _pc_reference(self, servers: list[str]) -> PcClock:
        cached = self._pc_cache
        if cached is not None and time.monotonic() - cached[0] < PC_CACHE_S and cached[1].trusted:
            return cached[1]
        result = self._pc_check(servers)
        self._pc_cache = (time.monotonic(), result, tuple(servers))
        return result

    @Slot(str, bool)
    def _on_requested(self, reason: str, force: bool) -> None:
        if self._running:
            return
        if not self._enabled() or not getattr(self._host, "_alive", True):
            self._release_waiters()
            return
        if not force and self._last_started is not None and time.monotonic() - self._last_started < RECHECK_S:
            self._release_waiters()
            return
        busy = self.busy_reason()
        if busy:
            if not self._pending:
                diag("sensor_clock_deferred", reason, busy=busy)
            self._pending = reason
            self._pending_timer.start()
            self._release_waiters()
            return
        self._start(reason)

    @Slot()
    def _try_pending(self) -> None:
        if not self._pending or self._running:
            self._pending_timer.stop()
            return
        if not self._enabled() or not getattr(self._host, "_alive", True):
            self._pending = ""
            self._pending_timer.stop()
            return
        if not self.busy_reason():
            self._start(self._pending)

    def _start(self, reason: str) -> None:
        try:
            web = self._make_web()
        except Exception as exc:  # noqa: BLE001 - no target: nothing to check
            diag("sensor_clock_skipped", reason, error=f"{type(exc).__name__}: {exc}")
            self._pending = ""
            self._pending_timer.stop()
            self._release_waiters()
            return
        self._token += 1
        token = self._token
        self._running = True
        self._pending = ""
        self._pending_timer.stop()
        self._last_started = time.monotonic()
        self._cancel = threading.Event()
        cancel = self._cancel
        store = self._store
        key = self._run_key = self._key()
        notify = self.notice.emit
        pair_idle = self._pair_idle

        def work() -> tuple[int, RecoveryResult]:
            try:
                return token, recover_clock(
                    web,
                    store,
                    key,
                    reason=reason,
                    pc_check=self._pc_reference,
                    wait=cancel.wait,
                    notify=lambda text: notify(text, "error"),
                    pair_idle=pair_idle,
                )
            finally:
                close = getattr(web, "close", None)
                if callable(close):
                    close()

        diag("sensor_clock_check", reason)
        self._watchdog.start(int(HOLD_MAX_S * 1000))
        self._host._start_job(work, self._finished)  # noqa: SLF001

    def _finished(self, payload: Any) -> None:
        token, result = payload
        if token != self._token:
            return
        self._watchdog.stop()
        self._running = False
        self.last_result = result
        self.last_finished = time.monotonic()
        diag("sensor_clock", result.outcome, **result.to_dict())
        if getattr(self._host, "_alive", True):
            self._report(result)
            self._apply_folder_hint(result)
            if result.outcome == SYNCED:
                self._refresh_discovery()
        self._release_waiters()
        self._resume_start()

    @Slot()
    def _on_watchdog(self) -> None:
        if not self._running:
            return
        self._token += 1
        self._cancel.set()
        self._running = False
        diag("sensor_clock_timeout", f"no result after {HOLD_MAX_S:.0f} s; captures released")
        if getattr(self._host, "_alive", True):
            self._host.log("Sensor clock check did not finish. See details.", level="error")
        self._release_waiters()
        self._resume_start()

    def _release_waiters(self) -> None:
        with self._lock:
            waiters, self._waiters = self._waiters, []
        for event in waiters:
            event.set()

    def _resume_start(self) -> None:
        callback, self._resume = self._resume, None
        if callback is not None and getattr(self._host, "_alive", True):
            QTimer.singleShot(0, callback)

    @Slot(str, str)
    def _show_notice(self, text: str, level: str) -> None:
        if getattr(self._host, "_alive", True):
            self._host.log(text, level=level)

    def _report(self, result: RecoveryResult) -> None:
        if result.outcome in {OK, SYNCED}:
            self._shown.clear()
        restored = restore_message(result)
        if restored is not None:
            self._host.log(restored[0], level=restored[1])
        message = result_message(result)
        if message is not None:
            text, level = message
            if result.outcome not in _SHOWN_ONCE or result.outcome not in self._shown:
                self._shown.add(result.outcome)
                self._host.log(text, level=level)
        if result.outcome == PC_UNTRUSTED:
            self._pc_cache = None

    def _apply_folder_hint(self, result: RecoveryResult) -> None:
        reading = result.final_reading()
        verdict = clock_verdict(reading)
        if verdict == INCORRECT and reading is not None:
            self._set_folder_hint(reading.offset_s)
        elif verdict in {CORRECT, DRIFTING}:
            self._set_folder_hint(None)

    def _set_folder_hint(self, offset_s: float | None) -> None:
        before = sensor_clock_offset()
        set_sensor_clock_offset(offset_s)
        if (before is None) != (offset_s is None):
            diag("sensor_clock_folders", "sensor date folder first" if offset_s is not None else "computer date folders", offset_s=offset_s)
            if offset_s is not None:
                self._refresh_discovery()

    def _refresh_discovery(self) -> None:
        """The sensor's 'today' folder changed: Sensor Files and the storage view look again."""
        if not getattr(self._host, "_alive", True):
            return
        window = getattr(self._host, "_sftp_win", None)
        if window is not None:
            try:
                window.retry_today()
            except Exception as exc:  # noqa: BLE001 - a refresh must not break the recorder
                diag("sensor_clock_refresh_failed", "Sensor Files", error=f"{type(exc).__name__}: {exc}")
        refresh = getattr(self._host, "_refresh_storage", None)
        if callable(refresh):
            refresh()
        diag("sensor_clock_discovery", "recording folders looked up again")


__all__ = ["HOLD_TEXT", "SensorClockMonitor"]
