"""Sequence generation, capability windows and scheduling (no sensor, no real workbook)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from openpyxl import Workbook

from crfs_iq_recorder.cellular_capabilities import MHZ, capability_for, normalise_window_hz
from crfs_iq_recorder.cellular_plan import (
    DURATION_FROM_CAMPAIGN,
    DURATION_FROM_WORKBOOK,
    CellularPlan,
    build_plan,
    split_windows,
)
from crfs_iq_recorder.cellular_protocol import DIR_DL, DIR_SHARED, DIR_UL, import_protocol
from crfs_iq_recorder.cellular_scheduler import (
    MISSED_AFTER_S,
    MODE_TIMES,
    ROUND_DELAYED,
    ROUND_DUE,
    ROUND_MISSED,
    ROUND_PENDING,
    TZ_UTC,
    Schedule,
    ScheduleTimeZone,
    classify_pending,
    make_round,
    materialise_rounds,
    next_due,
    parse_times,
    round_id,
    tight_schedule_warning,
)


def _plan_book(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Plan"
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


# Synthetic plan in the layout of the reference chart: bands listed out of
# order on purpose, UL rows before DL rows, one wide TDD allocation.
CHART_ROWS = [
    ["Band", "Duplex", "Direction", "Start (MHz)", "Stop (MHz)", "Network", "Duration (s)"],
    ["1800", "FDD", "UL", 1760, 1780, "Op B", None],
    ["1800", "FDD", "DL", 1855, 1875, "Op B", None],
    ["1800", "FDD", "DL", 1805, 1825, "Op A", None],
    ["1800", "FDD", "UL", 1710, 1730, "Op A", None],
    ["700", "FDD", "UL", 703, 713, "Op B", None],
    ["700", "FDD", "DL", 758, 768, "Op B", None],
    ["3500", "TDD", "TDD", 3600, 3700, "Op A", 5],
    ["800", "FDD", "DL", 791, 801, "Op B", None],
    ["800", "FDD", "UL", 832, 842, "Op B", None],
    ["900", "FDD", "DL", 947.2, 955.2, "Op C", None],
    ["900", "FDD", "UL", 902.2, 910.2, "Op C", None],
]


def test_capability_from_model_string():
    cap = capability_for("R40-8")
    assert cap.family == "RFeye Node 40-8"
    assert cap.ibw_hz == 40 * MHZ and cap.max_hz == 8_000 * MHZ
    assert cap.verified_window_hz == 20 * MHZ
    assert "verified" in cap.window_note(20 * MHZ).casefold()
    assert "62.5" in cap.window_note(30 * MHZ)
    other = capability_for("Node 100-18")
    assert other.ibw_hz == 100 * MHZ and other.max_hz == 18_000 * MHZ
    assert other.verified_window_hz == 0
    assert other.default_window_hz == 20 * MHZ
    assert "not a verified capability" in other.window_note(20 * MHZ)
    unknown = capability_for("—")
    assert unknown.max_hz is None and unknown.coverage_issue(1, 2) is None
    assert cap.coverage_issue(8_500 * MHZ, 8_600 * MHZ) is not None
    assert cap.coverage_issue(703 * MHZ, 713 * MHZ) is None


def test_window_normalisation_never_exceeds_ibw_and_keeps_even_width():
    cap = capability_for("R40-8")
    assert normalise_window_hz(0, cap)[0] == 20 * MHZ
    assert normalise_window_hz(20 * MHZ + 1, cap)[0] == 20 * MHZ
    window, notes = normalise_window_hz(60 * MHZ, cap)
    assert window == 40 * MHZ and any("instantaneous" in n for n in notes)


def test_split_windows_cover_the_allocation_without_gaps():
    assert split_windows(758 * MHZ, 768 * MHZ, 20 * MHZ) == [(758 * MHZ, 768 * MHZ, 0)]
    assert split_windows(3600 * MHZ, 3700 * MHZ, 20 * MHZ) == [
        (3600 * MHZ, 3620 * MHZ, 0),
        (3620 * MHZ, 3640 * MHZ, 0),
        (3640 * MHZ, 3660 * MHZ, 0),
        (3660 * MHZ, 3680 * MHZ, 0),
        (3680 * MHZ, 3700 * MHZ, 0),
    ]
    # Windows step by the full width from the start and the last one is moved back to the
    # stop edge: edges stay whole MHz and the extra overlap is reported on the last window.
    windows = split_windows(3500 * MHZ, 3550 * MHZ, 20 * MHZ)
    assert [(a // MHZ, b // MHZ, o // MHZ) for a, b, o in windows] == [(3500, 3520, 0), (3520, 3540, 0), (3530, 3550, 10)]
    # Union covers exactly the allocation, no gap between neighbours.
    for (a, b, _o), (c, d, _p) in zip(windows, windows[1:]):
        assert c <= b
    assert windows[0][0] == 3500 * MHZ and windows[-1][1] == 3550 * MHZ
    # A 75 MHz band (B3) keeps whole-MHz edges instead of thirds of a MHz.
    b3 = split_windows(1805 * MHZ, 1880 * MHZ, 20 * MHZ)
    assert [(a // MHZ, b // MHZ, o // MHZ) for a, b, o in b3] == [(1805, 1825, 0), (1825, 1845, 0), (1845, 1865, 0), (1860, 1880, 5)]
    assert all(a % MHZ == 0 and b % MHZ == 0 for a, b, _o in b3)
    # An edge margin only increases the overlap; the window width stays the same.
    margin = split_windows(3600 * MHZ, 3700 * MHZ, 20 * MHZ, edge_margin_hz=2 * MHZ)
    assert len(margin) == 6 and all(b - a == 20 * MHZ for a, b, _o in margin)
    assert margin[0][0] == 3600 * MHZ and margin[-1][1] == 3700 * MHZ
    assert all(o >= 4 * MHZ - 1 for _a, _b, o in margin[1:])


def test_plan_orders_bands_by_nominal_frequency_dl_before_ul_and_expands_wide_tdd(tmp_path):
    path = _plan_book(tmp_path / "chart.xlsx", CHART_ROWS)
    imported = import_protocol(path, "Plan")
    assert imported.errors == []
    cap = capability_for("R40-8")
    plan = build_plan(imported, cap, fallback_duration_s=10)
    assert plan.errors == []
    assert plan.band_order == ["700 MHz", "800 MHz", "900 MHz", "1800 MHz", "3500 MHz"]
    order = [(t.band_label, t.direction, t.window_start_hz // MHZ, t.window_stop_hz // MHZ) for t in plan.tasks]
    assert order == [
        ("700 MHz", DIR_DL, 758, 768),
        ("700 MHz", DIR_UL, 703, 713),
        ("800 MHz", DIR_DL, 791, 801),
        ("800 MHz", DIR_UL, 832, 842),
        ("900 MHz", DIR_DL, 947, 955),  # 947.2-955.2 shown truncated by // here
        ("900 MHz", DIR_UL, 902, 910),
        ("1800 MHz", DIR_DL, 1805, 1825),
        ("1800 MHz", DIR_DL, 1855, 1875),
        ("1800 MHz", DIR_UL, 1710, 1730),
        ("1800 MHz", DIR_UL, 1760, 1780),
        ("3500 MHz", DIR_SHARED, 3600, 3620),
        ("3500 MHz", DIR_SHARED, 3620, 3640),
        ("3500 MHz", DIR_SHARED, 3640, 3660),
        ("3500 MHz", DIR_SHARED, 3660, 3680),
        ("3500 MHz", DIR_SHARED, 3680, 3700),
    ]
    # Band 20 style (DL below UL) is taken from the workbook, DL still first.
    assert plan.tasks[2].direction == DIR_DL and plan.tasks[2].window_start_hz < plan.tasks[3].window_start_hz
    # Exact allocation edges, no widening to a standard band.
    assert plan.tasks[4].window_start_hz == 947_200_000 and plan.tasks[4].window_stop_hz == 955_200_000
    assert plan.tasks[4].bandwidth_hz == 8_000_000 and plan.tasks[4].sample_rate_hz == 15_625_000
    # Durations: workbook value wins, otherwise the campaign fallback, and the source is kept.
    assert plan.tasks[0].duration_s == 10 and plan.tasks[0].duration_source == DURATION_FROM_CAMPAIGN
    tdd = [t for t in plan.tasks if t.direction == DIR_SHARED]
    assert all(t.duration_s == 5 and t.duration_source == DURATION_FROM_WORKBOOK for t in tdd)
    assert all(t.partial and t.window_count == 5 for t in tdd)
    assert [t.window_index for t in tdd] == [1, 2, 3, 4, 5]
    assert all(t.requested_start_hz == 3600 * MHZ and t.requested_stop_hz == 3700 * MHZ for t in tdd)
    assert "sequential coverage" in tdd[0].coverage_label
    assert any(i.code == "expanded" and "not a simultaneous full-band capture" in i.message for i in plan.issues)
    assert tdd[0].label() == "3500 MHz TDD / shared UL-DL 3600–3620 MHz (1/5)"
    # Estimates: per task from the sample-rate model; the round adds waits and overhead for every task.
    assert plan.tasks[0].estimated_bytes > 0 and plan.tasks[0].sample_rate_hz == 15_625_000
    assert plan.estimated_bytes == sum(t.estimated_bytes for t in plan.tasks)
    assert plan.estimated_round_seconds(wait_s=10, overhead_s=0) == plan.capture_seconds + 14 * 10
    assert plan.ready
    # Round trip through JSON keeps every task.
    again = CellularPlan.from_dict(plan.to_dict())
    assert [t.to_dict() for t in again.tasks] == [t.to_dict() for t in plan.tasks]


def test_plan_reports_missing_duration_and_out_of_range_instead_of_guessing(tmp_path):
    rows = [
        ["Band", "Direction", "Start (MHz)", "Stop (MHz)"],
        ["700", "DL", 758, 768],
        ["9000", "DL", 9000, 9010],
    ]
    path = _plan_book(tmp_path / "plan.xlsx", rows)
    imported = import_protocol(path, "Plan")
    cap = capability_for("R40-8")
    plan = build_plan(imported, cap)
    codes = {i.code for i in plan.errors}
    assert "duration_missing" in codes and "outside_coverage" in codes
    assert plan.tasks == [] and not plan.ready
    plan2 = build_plan(imported, cap, fallback_duration_s=2)
    assert [i.code for i in plan2.errors] == ["outside_coverage"]
    assert len(plan2.tasks) == 1 and not plan2.ready, "an unrecordable row blocks the plan until removed"


def test_odd_window_edges_are_widened_by_one_hz_and_said_so(tmp_path):
    rows = [["Band", "Direction", "Start", "Stop"], ["700", "DL", "758 MHz", "768.000001 MHz"]]
    path = _plan_book(tmp_path / "odd.xlsx", rows)
    plan = build_plan(import_protocol(path, "Plan"), capability_for("R40-8"), fallback_duration_s=1)
    task = plan.tasks[0]
    assert task.window_stop_hz == 768_000_002 and task.adjustments
    assert task.requested_stop_hz == 768_000_001


def test_repeat_column_repeats_every_window(tmp_path):
    rows = [["Band", "Direction", "Start (MHz)", "Stop (MHz)", "Repeat"], ["3500", "TDD", 3500, 3550, 2]]
    path = _plan_book(tmp_path / "rep.xlsx", rows)
    plan = build_plan(import_protocol(path, "Plan"), capability_for("R40-8"), fallback_duration_s=1)
    assert [(t.repetition, t.window_index) for t in plan.tasks] == [(1, 1), (1, 2), (1, 3), (2, 1), (2, 2), (2, 3)]


# ----------------------------------------------------------------------------- scheduling
def test_parse_times_and_round_ids():
    times, bad = parse_times("08:00, 13:00 18.00 25:00 noon")
    assert [t.strftime("%H:%M") for t in times] == ["08:00", "13:00", "18:00"]
    assert bad == ["25:00", "noon"]
    when = datetime(2026, 11, 3, 8, 0, tzinfo=timezone(timedelta(hours=2)))
    rid = round_id(when, "Site A / morning")
    assert rid == "Site-A-morning_20261103-0800_0600Z"


def test_schedule_occurrences_in_utc_and_local_and_round_materialisation():
    tz = ScheduleTimeZone(kind=TZ_UTC)
    schedule = Schedule(mode=MODE_TIMES, times=[time(8, 0), time(13, 0)], dates=[date(2026, 11, 3)], tz=tz)
    now = datetime(2026, 11, 3, 7, 0, tzinfo=timezone.utc)
    starts = schedule.occurrences(after=now)
    assert [s.isoformat() for s in starts] == ["2026-11-03T08:00:00+00:00", "2026-11-03T13:00:00+00:00"]
    rounds = materialise_rounds(schedule, [], campaign="C", tasks_total=4, now=now)
    assert [r.scheduled_utc.hour for r in rounds] == [8, 13]
    assert all(r.status == ROUND_PENDING and r.tasks_total == 4 for r in rounds)
    # Re-materialising never duplicates rounds and keeps their state.
    rounds[0].status = ROUND_DUE
    again = materialise_rounds(schedule, rounds, campaign="C", tasks_total=4, now=now)
    assert len(again) == 2 and again[0].status == ROUND_DUE
    local = Schedule.from_dict(schedule.to_dict())
    assert local.to_dict() == schedule.to_dict()
    daily = Schedule(mode=MODE_TIMES, times=[time(18, 0)], daily=True, tz=tz, daily_until=date(2026, 11, 5))
    assert [s.date().isoformat() for s in daily.occurrences(after=now)] == ["2026-11-03", "2026-11-04", "2026-11-05"]
    assert "Every day at 18:00 until 2026-11-05" == daily.describe()
    assert "UTC" == tz.describe()
    local_tz = ScheduleTimeZone()
    assert local_tz.describe().startswith("Computer local time (")
    aware = local_tz.localize(datetime(2026, 7, 1, 12, 0))
    assert aware.tzinfo is not None and aware.utcoffset() is not None


def test_due_delayed_and_missed_classification():
    tz = ScheduleTimeZone(kind=TZ_UTC)
    base = datetime(2026, 11, 3, 8, 0, tzinfo=timezone.utc)
    rounds = [make_round(base, "C", 3), make_round(base + timedelta(hours=1), "C", 3), make_round(base + timedelta(hours=5), "C", 3)]
    # 2 minutes after the first time, sensor free: the first round is due, others pending.
    classify_pending(rounds, now=base + timedelta(minutes=2), busy=False)
    assert [r.status for r in rounds] == [ROUND_DUE, ROUND_PENDING, ROUND_PENDING]
    assert next_due(rounds) is rounds[0]
    # The first round is running when the second becomes due: delayed, original time kept.
    rounds[0].status = "running"
    classify_pending(rounds, now=base + timedelta(hours=1, minutes=1), busy=True)
    assert rounds[1].status == ROUND_DELAYED and rounds[1].scheduled_utc == base + timedelta(hours=1)
    assert "starts when the sensor is free" in rounds[1].note
    # Once free, the delayed round is next; it does not become "missed".
    classify_pending(rounds, now=base + timedelta(hours=2), busy=False)
    assert rounds[1].status == ROUND_DELAYED and next_due(rounds) is rounds[1]
    # A pending round whose time passed long ago (application closed) is missed, never auto-run.
    classify_pending(rounds, now=base + timedelta(hours=5) + timedelta(seconds=MISSED_AFTER_S + 1), busy=False)
    assert rounds[2].status == ROUND_MISSED and "Scheduled time passed" in rounds[2].note
    assert next_due(rounds) is rounds[1]
    assert tz.kind == TZ_UTC


def test_tight_schedule_warning():
    tz = ScheduleTimeZone(kind=TZ_UTC)
    schedule = Schedule(mode=MODE_TIMES, times=[time(8, 0), time(8, 30)], dates=[date(2026, 11, 3)], tz=tz)
    now = datetime(2026, 11, 2, 7, 0, tzinfo=timezone.utc)
    assert tight_schedule_warning(schedule, 45 * 60, now=now) is not None
    assert tight_schedule_warning(schedule, 20 * 60, now=now) is None
