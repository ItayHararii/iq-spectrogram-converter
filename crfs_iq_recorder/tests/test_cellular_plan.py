"""Sequence generation, capability windows and scheduling (no sensor, no real workbook)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from openpyxl import Workbook

from crfs_iq_recorder.cellular_capabilities import MHZ, capability_for, normalise_window_hz, readable_sensor_name
from crfs_iq_recorder.cellular_plan import (
    DURATION_FROM_CAMPAIGN,
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
    assert cap.verified_window_hz == 20 * MHZ and cap.max_sample_rate_hz == 62_500_000
    assert "verified" in cap.window_note(20 * MHZ).casefold()
    assert "62.5" in cap.window_note(30 * MHZ)
    # The 40-8 sustains 31.25 MS/s: the verified window applies whatever the duration.
    assert cap.window_for(1.0) == 20 * MHZ and cap.window_for(120.0) == 20 * MHZ
    assert cap.max_duration_for(20 * MHZ) is None
    unverified = capability_for("Node 100-8")
    assert unverified.ibw_hz == 100 * MHZ and unverified.max_sample_rate_hz == 125_000_000
    assert unverified.verified_window_hz == 0 and unverified.capture_budget_bytes is None
    assert unverified.default_window_hz == 20 * MHZ and unverified.window_for(2.0) == 20 * MHZ
    assert "not a verified capability" in unverified.window_note(20 * MHZ)
    unknown = capability_for("—")
    assert unknown.max_hz is None and unknown.coverage_issue(1, 2) is None
    assert cap.coverage_issue(8_500 * MHZ, 8_600 * MHZ) is not None
    assert cap.coverage_issue(703 * MHZ, 713 * MHZ) is None


def test_node_100_18_capability_follows_the_verified_capture_budget():
    """Measured on rfeye100400: complete captures up to 625 MB of IQ; 100 MHz is sampled at 125 MS/s."""
    cap = capability_for("R100-18")
    assert cap.ibw_hz == 100 * MHZ and cap.max_hz == 18_000 * MHZ
    assert cap.max_sample_rate_hz == 125_000_000 and cap.capture_budget_bytes == 625_000_000
    assert cap.sample_rate_for(100 * MHZ) == 125_000_000 and cap.sample_rate_for(25 * MHZ) == 31_250_000
    assert cap.bandwidth_tiers_hz()[:4] == [100 * MHZ, 50 * MHZ, 25 * MHZ, 12_500_000]
    # The widest tier whose IQ fits the budget for the chosen duration.
    assert cap.window_for(1.0) == 100 * MHZ
    assert cap.window_for(2.0) == 50 * MHZ
    assert cap.window_for(2.5) == 50 * MHZ
    assert cap.window_for(3.0) == 25 * MHZ
    assert cap.window_for(5.0) == 25 * MHZ
    assert cap.window_for(10.0) == 12_500_000
    assert cap.window_for(20.0) == 6_250_000
    assert cap.max_duration_for(100 * MHZ) == 1.25 and cap.max_duration_for(25 * MHZ) == 5.0
    assert cap.capture_bytes(25 * MHZ, 5.0) == 625_000_000
    assert "within the 625 MB" in cap.window_note(25 * MHZ, 5.0)
    note = cap.window_note(50 * MHZ, 5.0)
    assert "above the 625 MB" in note and "2.5 s" in note
    assert readable_sensor_name("R100-18") == "CRFS 100-18"
    assert readable_sensor_name("Node 40-8") == "CRFS 40-8"
    assert readable_sensor_name("", "fallback") == "fallback"


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


def test_plan_follows_the_worksheet_order_uses_the_app_duration_and_expands_wide_tdd(tmp_path):
    path = _plan_book(tmp_path / "chart.xlsx", CHART_ROWS)
    imported = import_protocol(path, "Plan")
    assert imported.errors == []
    cap = capability_for("R40-8")
    plan = build_plan(imported, cap, fallback_duration_s=10)
    assert plan.errors == []
    # Bands in the order they first appear in the worksheet; nothing is re-sorted.
    assert plan.band_order == ["1800 MHz", "700 MHz", "3500 MHz", "800 MHz", "900 MHz"]
    order = [(t.band_label, t.direction, t.window_start_hz // MHZ, t.window_stop_hz // MHZ) for t in plan.tasks]
    assert order == [
        ("1800 MHz", DIR_UL, 1760, 1780),
        ("1800 MHz", DIR_DL, 1855, 1875),
        ("1800 MHz", DIR_DL, 1805, 1825),
        ("1800 MHz", DIR_UL, 1710, 1730),
        ("700 MHz", DIR_UL, 703, 713),
        ("700 MHz", DIR_DL, 758, 768),
        ("3500 MHz", DIR_SHARED, 3600, 3620),
        ("3500 MHz", DIR_SHARED, 3620, 3640),
        ("3500 MHz", DIR_SHARED, 3640, 3660),
        ("3500 MHz", DIR_SHARED, 3660, 3680),
        ("3500 MHz", DIR_SHARED, 3680, 3700),
        ("800 MHz", DIR_DL, 791, 801),
        ("800 MHz", DIR_UL, 832, 842),
        ("900 MHz", DIR_DL, 947, 955),  # 947.2-955.2 shown truncated by // here
        ("900 MHz", DIR_UL, 902, 910),
    ]
    assert [t.row for t in plan.tasks[:6]] == [2, 3, 4, 5, 6, 7]
    # Exact allocation edges, no widening to a standard band.
    b900 = plan.tasks[13]
    assert b900.window_start_hz == 947_200_000 and b900.window_stop_hz == 955_200_000
    assert b900.bandwidth_hz == 8_000_000 and b900.sample_rate_hz == 15_625_000
    # The app's duration applies to every row; the workbook's duration column (5 s on
    # the TDD row) is a result of an earlier pass, never an instruction.
    assert all(t.duration_s == 10 and t.duration_source == DURATION_FROM_CAMPAIGN for t in plan.tasks)
    tdd = [t for t in plan.tasks if t.direction == DIR_SHARED]
    assert all(t.partial and t.window_count == 5 for t in tdd)
    assert [t.window_index for t in tdd] == [1, 2, 3, 4, 5]
    assert all(t.requested_start_hz == 3600 * MHZ and t.requested_stop_hz == 3700 * MHZ for t in tdd)
    assert "sequential coverage" in tdd[0].coverage_label
    expanded = [i for i in plan.issues if i.code == "expanded"]
    assert len(expanded) == 1 and "not a simultaneous full-band capture" in expanded[0].message
    assert "5 sequential windows of 10 s each" in expanded[0].message
    assert tdd[0].label() == "3500 MHz TDD / shared UL-DL 3600–3620 MHz (1/5)"
    # Estimates: per task from the sample-rate model; the round adds waits and overhead for every task.
    assert plan.tasks[0].estimated_bytes > 0 and plan.tasks[0].sample_rate_hz == 31_250_000
    assert plan.estimated_bytes == sum(t.estimated_bytes for t in plan.tasks)
    assert plan.estimated_round_seconds(wait_s=10, overhead_s=0) == plan.capture_seconds + 14 * 10
    assert plan.ready
    # Round trip through JSON keeps every task.
    again = CellularPlan.from_dict(plan.to_dict())
    assert [t.to_dict() for t in again.tasks] == [t.to_dict() for t in plan.tasks]


def test_plan_window_follows_the_sensor_and_the_chosen_duration(tmp_path):
    """On the Node 100-18 the window is the widest tier verified complete for the duration."""
    rows = [
        ["Band", "Direction", "Duplex", "Freq Start (MHz)", "Freq Stop (MHz)"],
        ["700 MHz (B68 / B28)", "DL", "FDD", 753, 803],
        ["1800 MHz (B3)", "DL", "FDD", 1805, 1880],
        ["3500 MHz (n78)", "UL/DL", "TDD", 3300, 3800],
    ]
    path = _plan_book(tmp_path / "proto.xlsx", rows)
    imported = import_protocol(path, "Plan")
    cap = capability_for("R100-18")
    # 2 s: 50 MHz captures (500 MB each) -> the 50 MHz row is one task.
    plan2 = build_plan(imported, cap, fallback_duration_s=2)
    assert plan2.window_hz == 50 * MHZ
    assert [t.window_count for t in plan2.tasks if t.row == 2] == [1]
    assert [t.window_count for t in plan2.tasks if t.row == 3] == [2, 2]
    assert len([t for t in plan2.tasks if t.row == 4]) == 10
    assert all(t.sample_rate_hz == 62_500_000 for t in plan2.tasks)
    assert "within the 625 MB" in plan2.window_note
    # 5 s: 25 MHz captures (625 MB each) -> the same rows need more windows, each 5 s long.
    plan5 = build_plan(imported, cap, fallback_duration_s=5)
    assert plan5.window_hz == 25 * MHZ
    assert [t.window_count for t in plan5.tasks if t.row == 2] == [2, 2]
    assert [t.window_count for t in plan5.tasks if t.row == 3] == [3, 3, 3]
    assert len([t for t in plan5.tasks if t.row == 4]) == 20
    assert all(t.duration_s == 5 for t in plan5.tasks)
    # 625 MB of IQ plus the WAVE header/trailer and the Node's ~0.6 % extra samples.
    assert all(620_000_000 <= t.estimated_bytes <= 630_000_000 for t in plan5.tasks)
    # 1 s: 100 MHz windows at 125 MS/s -> every FDD row is one capture at the rate its width needs.
    plan1 = build_plan(imported, cap, fallback_duration_s=1)
    assert plan1.window_hz == 100 * MHZ
    assert [t.window_count for t in plan1.tasks if t.row in (2, 3)] == [1, 1]
    assert [t.sample_rate_hz for t in plan1.tasks if t.row in (2, 3)] == [62_500_000, 125_000_000]
    assert len([t for t in plan1.tasks if t.row == 4]) == 5
    assert all(t.sample_rate_hz == 125_000_000 for t in plan1.tasks if t.row == 4)
    # An explicit window still wins (operator override).
    forced = build_plan(imported, cap, window_hz=20 * MHZ, fallback_duration_s=2)
    assert forced.window_hz == 20 * MHZ
    # The Node 40-8 keeps its verified 20 MHz window whatever the duration.
    plan40 = build_plan(imported, capability_for("R40-8"), fallback_duration_s=2)
    assert plan40.window_hz == 20 * MHZ and all(t.sample_rate_hz == 31_250_000 for t in plan40.tasks)


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
