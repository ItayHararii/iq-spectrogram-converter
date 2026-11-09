"""Sensor clock against a trustworthy computer clock (sensor_clock). No network."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from crfs_iq_recorder import sensor_clock as sc

PC = datetime(2026, 11, 9, 10, 7, 12, tzinfo=timezone.utc)


def _status(system_time: str, *, time_source: str = "NTP", date_header: str = "") -> SimpleNamespace:
    body = {
        "pageErrors": [{"message": "Release not covered by Software Support and Update Service", "type": "warning"}],
        "values": {"02-system-time": system_time, "03-time-source": time_source},
    }
    headers = {"Date": date_header} if date_header else {}
    return SimpleNamespace(status=200, headers=headers, text=json.dumps(body), error="")


def _clock(*stamps: float):
    """now() that returns the given instants in turn (request start, request end)."""
    values = list(stamps)
    return lambda: values.pop(0) if len(values) > 1 else values[0]


def test_a_correct_clock_reads_correct_with_the_delay_halved():
    t0 = PC.timestamp()
    reading = sc.read_sensor_clock(lambda _p: _status("2026-11-09 10:07:12+0000"), now=_clock(t0 - 0.2, t0 + 0.2))
    assert reading.available and reading.source.startswith("status page")
    assert reading.offset_s == pytest.approx(0.0, abs=0.01)
    assert reading.rtt_s == pytest.approx(0.4) and reading.uncertainty_s == pytest.approx(1.2)
    assert sc.clock_verdict(reading) == sc.CORRECT
    assert reading.time_source == "NTP" and reading.warnings


def test_a_display_time_zone_is_not_drift():
    t0 = PC.timestamp()
    plus_two = sc.read_sensor_clock(lambda _p: _status("2026-11-09 12:07:12+0200"), now=_clock(t0, t0))
    zulu = sc.read_sensor_clock(lambda _p: _status("2026-11-09T10:07:12Z"), now=_clock(t0, t0))
    assert plus_two.offset_s == pytest.approx(0.0) and zulu.offset_s == pytest.approx(0.0)
    assert sc.clock_verdict(plus_two) == sc.CORRECT and sc.clock_verdict(zulu) == sc.CORRECT


def test_a_slow_link_widens_the_uncertainty_instead_of_reading_as_drift():
    # The page was answered 4 s into an 8 s round trip: a naive "after - sensor" says 4 s behind.
    t0 = PC.timestamp() - 4.0
    reading = sc.read_sensor_clock(lambda _p: _status("2026-11-09 10:07:12+0000"), now=_clock(t0, t0 + 8.0))
    assert reading.offset_s == pytest.approx(0.0, abs=0.01)
    assert reading.uncertainty_s == pytest.approx(5.0)
    assert sc.clock_verdict(reading) == sc.CORRECT
    # 125 s off but measured over a 20 s round trip: not provably beyond 120 s.
    slow = sc.read_sensor_clock(lambda _p: _status("2026-11-09 10:09:17+0000"), now=_clock(PC.timestamp() - 10, PC.timestamp() + 10))
    assert slow.offset_s == pytest.approx(125.0, abs=0.01)
    assert sc.clock_verdict(slow) == sc.DRIFTING


def test_the_2012_clock_of_9_november_is_incorrect_and_says_its_date():
    t0 = PC.timestamp()
    reading = sc.read_sensor_clock(lambda _p: _status("2012-01-02 11:41:53+0000"), now=_clock(t0, t0 + 0.1))
    assert sc.clock_verdict(reading) == sc.INCORRECT
    assert reading.offset_s < -14 * 365 * 86400
    assert sc.reads_text(reading) == "reads 2012-01-02"
    ten_min = sc.read_sensor_clock(lambda _p: _status("2026-11-09 09:57:12+0000"), now=_clock(t0, t0))
    assert sc.clock_verdict(ten_min) == sc.INCORRECT
    assert sc.reads_text(ten_min) == "is 10 min behind"
    assert sc.offset_text(400) == "6 min 40 s" and sc.offset_text(0.44) == "0.4 s" and sc.offset_text(-7200) == "2.0 h"


def test_drift_between_5_s_and_120_s_is_reported_not_corrected():
    t0 = PC.timestamp()
    reading = sc.read_sensor_clock(lambda _p: _status("2026-11-09 10:07:42+0000"), now=_clock(t0, t0))
    assert reading.offset_s == pytest.approx(30.0) and sc.clock_verdict(reading) == sc.DRIFTING


def test_unreadable_status_is_unknown_never_a_guess():
    def refused(_path):
        raise ConnectionError("refused")

    assert sc.clock_verdict(sc.read_sensor_clock(refused)) == sc.UNKNOWN
    assert not sc.read_sensor_clock(lambda _p: SimpleNamespace(status=401, headers={}, text="", error="")).available
    no_time = SimpleNamespace(status=200, headers={}, text=json.dumps({"values": {}}), error="")
    assert sc.clock_verdict(sc.read_sensor_clock(lambda _p: no_time)) == sc.UNKNOWN
    # The Date header is the whole-second fallback.
    t0 = PC.timestamp()
    header = SimpleNamespace(status=200, headers={"Date": "Mon, 09 Nov 2026 10:07:12 GMT"}, text="{}", error="")
    reading = sc.read_sensor_clock(lambda _p: header, now=_clock(t0, t0))
    assert reading.available and reading.source.startswith("HTTP Date") and reading.offset_s == pytest.approx(0.0)


def test_dotnet_fractions_and_naive_text_parse_as_utc():
    assert sc.parse_sensor_time("2012-01-02T11:24:50.0010006") == datetime(2012, 1, 2, 11, 24, 50, 1000, tzinfo=timezone.utc)
    assert sc.parse_sensor_time("2026-11-09 10:07:12") == PC
    assert sc.parse_sensor_time("") is None and sc.parse_sensor_time("garbage") is None


W32TM_SYNCED = """Leap Indicator: 0(no warning)
Stratum: 4 (secondary reference - syncd by (S)NTP)
Precision: -23 (119.209ns per tick)
Last Successful Sync Time: 11/9/2026 9:51:03 AM
Source: time.windows.com,0x9
Poll Interval: 10 (1024s)

Phase Offset: 0.0012741s
Time since Last Good Sync Time: 1032.4513217s
"""


def test_windows_time_service_counts_only_a_recent_network_sync():
    status = sc.parse_w32tm(W32TM_SYNCED)
    assert status["Source"] == "time.windows.com,0x9"
    assert sc.w32tm_trust(status).trusted
    assert not sc.w32tm_trust(sc.parse_w32tm(W32TM_SYNCED.replace("time.windows.com,0x9", "Local CMOS Clock"))).trusted
    assert not sc.w32tm_trust(sc.parse_w32tm(W32TM_SYNCED.replace("time.windows.com,0x9", "Free-running System Clock"))).trusted
    assert not sc.w32tm_trust(sc.parse_w32tm(W32TM_SYNCED.replace("Leap Indicator: 0(no warning)", "Leap Indicator: 3(not synchronized)"))).trusted
    stale = W32TM_SYNCED.replace("1032.4513217s", f"{9 * 86400}.0s")
    assert not sc.w32tm_trust(sc.parse_w32tm(stale)).trusted
    assert not sc.w32tm_trust({}).trusted  # another language or no access: not trusted


def test_the_computer_clock_is_checked_by_sntp_first_with_the_sensor_servers():
    asked: list[str] = []

    def sntp(server):
        asked.append(server)
        if server == "1.il.pool.ntp.org":
            raise TimeoutError
        return 0.08, 0.03, 2

    pc = sc.check_pc_clock(["1.il.pool.ntp.org", "1.il.pool.ntp.org"], sntp=sntp, w32tm=lambda: {})
    assert pc.trusted and pc.source == "SNTP time.windows.com" and pc.offset_s == pytest.approx(0.08)
    assert asked == ["1.il.pool.ntp.org", "time.windows.com"]
    wrong = sc.check_pc_clock([], sntp=lambda _s: (45.0, 0.02, 2), w32tm=lambda: sc.parse_w32tm(W32TM_SYNCED))
    assert not wrong.trusted and "45" in wrong.detail  # SNTP disagrees: Windows' own word does not override it
    none = sc.check_pc_clock(["a", "b", "c", "d"], sntp=lambda _s: (_ for _ in ()).throw(OSError("blocked")), w32tm=lambda: {})
    assert not none.trusted and "no SNTP answer" in none.detail
    fallback = sc.check_pc_clock([], sntp=lambda _s: (_ for _ in ()).throw(OSError("blocked")), w32tm=lambda: sc.parse_w32tm(W32TM_SYNCED))
    assert fallback.trusted and fallback.source.startswith("Windows time service")
    assert len(sc.sntp_servers(["a", "b", "c", "d"])) == sc.SNTP_MAX_QUERIES


def test_the_pre_collection_clock_line_uses_the_offset_and_its_uncertainty(tmp_path):
    from crfs_iq_recorder import cellular_quality as cq
    from crfs_iq_recorder.cellular_preflight import _clock_reading, gather_preflight
    from crfs_iq_recorder.api_client import SensorClock

    common = dict(sensor_info=SimpleNamespace(connected=True, model="R100-18", serial="rfeye100400", firmware="2.25"),
                  sensor_storage=None, local_dir=tmp_path, plan=None, local_reserve_bytes=0, check_writable=False, now=PC)
    wrong = {c.name: c for c in cq.pre_run_checks(**common, sensor_time=datetime(2012, 1, 2, 11, 41, 53, tzinfo=timezone.utc),
                                                    clock_note="An NTP refresh did not correct it.")}
    message = wrong["sensor_clock"].message
    assert wrong["sensor_clock"].level == cq.LEVEL_ERROR and "(reads 2012-01-02 11:41 UTC)" in message and "days behind" in message
    assert message.endswith("An NTP refresh did not correct it.") and "before 2024" not in message
    # 6 s measured over a slow link (2 s uncertainty) is within tolerance; a naive time (no offset) is UTC.
    slow = {c.name: c for c in cq.pre_run_checks(**common, clock_offset_s=6.0, clock_uncertainty_s=2.0)}
    assert slow["sensor_clock"].level == cq.LEVEL_OK and "within 2.0 s" in slow["sensor_clock"].message
    naive = {c.name: c for c in cq.pre_run_checks(**common, sensor_time=datetime(2026, 11, 9, 10, 7, 13))}
    assert naive["sensor_clock"].level == cq.LEVEL_OK
    # The pre-flight carries the uncertainty and the recovery note through.
    api = SensorClock(available=True, sensor_utc=PC, pc_utc=PC, offset_s=6.0, source="status page system time (UTC)", time_source="NTP")
    reading = _clock_reading(api, 2.0, "Corrected by an NTP refresh.")
    assert reading.uncertainty_s == pytest.approx(2.0) and reading.note == "Corrected by an NTP refresh."
    result = gather_preflight(plan=None, local_dir=tmp_path, reserve_bytes=0, expected_identity=None, workbook_path=None,
                              workbook_sheet="", protocol_loaded=False, fetch_info=lambda: common["sensor_info"],
                              fetch_clock=lambda: reading, fetch_storage=None, workbook_state=None, check_writable=False)
    line = next(c for c in result.checks if c.name == "sensor_clock")
    assert line.level == cq.LEVEL_OK and line.message.endswith("Corrected by an NTP refresh.")
    assert result.clock["uncertainty_s"] == 2.0


def test_a_recording_reply_from_a_wrong_clock_is_evidence():
    now = datetime(2026, 11, 9, 9, 29, 0, tzinfo=timezone.utc)
    wrong = {"timestamp": "2012-01-02T11:24:50.0010006", "file_dir": "/mnt/1/remdata/20120102/"}
    assert "2012-01-02T11:24:50" in sc.reply_clock_evidence(wrong, now=now)
    folder_only = {"file_dir": "/mnt/1/remdata/20120102/"}
    assert "20120102" in sc.reply_clock_evidence(folder_only, now=now)
    # The right date in UTC or local time, a minute-old timestamp: nothing to check.
    right = {"timestamp": (now - timedelta(seconds=40)).strftime("%Y-%m-%dT%H:%M:%S.0010006"), "file_dir": "/mnt/1/remdata/20261109/"}
    assert sc.reply_clock_evidence(right, now=now) == ""
    assert sc.reply_clock_evidence({"file_dir": "/mnt/1/remdata/20261108/"}, now=now) == ""
    assert sc.reply_clock_evidence({}, now=now) == ""
