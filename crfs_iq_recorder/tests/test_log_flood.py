"""The same unhandled exception over and over must not flood the diagnostic log.

6 Nov 2026: three Sensor Files downloads of 2.3 GB parts raised an OverflowError
on every 32 KB chunk. Each one was written in full (error log rewritten, log line
flushed); the 10 MB of diagnostic log (all backups) were filled in minutes with
`{"traceback": "OverflowError\\n"}`, the history of the night was lost, and the
application stopped responding.
"""

from __future__ import annotations

import json
import sys

from crfs_iq_recorder import diagnostic_log
from crfs_iq_recorder.diagnostic_log import RepeatLimiter, exception_signature


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_repeat_limiter_writes_the_first_then_one_per_window_with_the_count():
    clock = _Clock()
    limiter = RepeatLimiter(10.0, clock=clock)
    assert limiter.admit("a") == (True, 0)
    for _ in range(399):
        clock.now += 0.0025
        assert limiter.admit("a") == (False, 0)
    assert limiter.admit("b") == (True, 0), "a different exception is never held back by another"
    clock.now += 10.0
    assert limiter.admit("a") == (True, 399)
    assert limiter.admit("a") == (False, 0)


def test_repeat_limiter_forgets_the_oldest_key_beyond_its_size():
    clock = _Clock()
    limiter = RepeatLimiter(10.0, clock=clock, max_keys=2)
    limiter.admit("a")
    clock.now += 1
    limiter.admit("b")
    clock.now += 1
    limiter.admit("c")
    assert limiter.admit("a") == (True, 0)


def _raise(exc):
    try:
        raise exc
    except BaseException:
        return sys.exc_info()


def test_signature_tells_apart_type_place_and_message():
    one = exception_signature(*_raise(OverflowError()))
    assert one.startswith("OverflowError|") and one == exception_signature(*_raise(OverflowError()))
    assert one != exception_signature(*_raise(ValueError()))
    assert exception_signature(*_raise(ValueError("x"))) != exception_signature(*_raise(ValueError("y")))
    # Raised inside a Qt signal emission there is no Python frame at all.
    assert exception_signature(OverflowError, OverflowError(), None) == "OverflowError||"


def test_installed_hook_logs_one_readable_line_for_a_burst(monkeypatch):
    from crfs_iq_recorder import gui

    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(sys, "__excepthook__", lambda *args: None)
    gui._install_exception_hook()  # noqa: SLF001
    for _ in range(400):
        sys.excepthook(OverflowError, OverflowError(), None)
    lines = [line for line in diagnostic_log.recent(diagnostic_log.RECENT_LINES) if "[unhandled_exception]" in line]
    assert len(lines) == 1
    text, _, payload = lines[0].split("[unhandled_exception] ", 1)[1].partition(" {")
    assert text == "OverflowError", "an exception without a message is named by its type"
    fields = json.loads("{" + payload)
    assert fields["type"] == "OverflowError"
    assert "repeats_not_logged" not in fields
