"""One request per protocol row, model facts and scheduling (no sensor, no real workbook)."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone

from openpyxl import Workbook

from crfs_iq_recorder.cellular_capabilities import MHZ, capability_for, readable_sensor_name
from crfs_iq_recorder.cellular_plan import (
    DURATION_FROM_CAMPAIGN,
    REQUEST_RULE,
    REQUEST_SENTENCE,
    CellularPlan,
    build_plan,
    request_range,
    summary_line,
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

# The rows of the field protocol that once were split into sub-band captures.
WIDE_ROWS = [
    ["Band", "Direction", "Duplex", "Freq Start (MHz)", "Freq Stop (MHz)"],
    ["700 MHz (B68 / B28)", "DL", "FDD", 753, 803],
    ["1800 MHz (B3)", "DL", "FDD", 1805, 1880],
    ["3500 MHz (n78)", "UL/DL", "TDD", 3300, 3800],
    ["3500 MHz (n78)", "UL/DL", "TDD", 3300, 3400],
]


def test_capability_from_model_string_is_information_only():
    cap = capability_for("R40-8")
    assert cap.family == "RFeye Node 40-8"
    assert cap.ibw_hz == 40 * MHZ and cap.max_hz == 8_000 * MHZ
    assert cap.max_sample_rate_hz == 62_500_000
    assert cap.tuning_note(8_500 * MHZ, 8_600 * MHZ) is not None
    assert cap.tuning_note(703 * MHZ, 713 * MHZ) is None
    assert cap.wider_than_receiver(100 * MHZ) and not cap.wider_than_receiver(40 * MHZ)
    node100 = capability_for("R100-18")
    assert node100.ibw_hz == 100 * MHZ and node100.max_hz == 18_000 * MHZ
    assert node100.max_sample_rate_hz == 125_000_000
    assert node100.sample_rate_for(100 * MHZ) == 125_000_000 and node100.sample_rate_for(25 * MHZ) == 31_250_000
    assert node100.capture_bytes(100 * MHZ, 2.0) == 1_000_000_000
    unknown = capability_for("—")
    assert unknown.max_hz is None and unknown.tuning_note(1, 2) is None and not unknown.wider_than_receiver(10**12)
    assert readable_sensor_name("R100-18") == "CRFS 100-18"
    assert readable_sensor_name("Node 40-8") == "CRFS 40-8"
    assert readable_sensor_name("", "fallback") == "fallback"


def test_every_row_is_one_request_in_worksheet_order_with_its_own_range(tmp_path):
    path = _plan_book(tmp_path / "chart.xlsx", CHART_ROWS)
    imported = import_protocol(path, "Plan")
    assert imported.errors == []
    plan = build_plan(imported, capability_for("R40-8"), fallback_duration_s=10)
    assert plan.errors == [] and plan.ready and not plan.legacy
    assert plan.request_rule == REQUEST_RULE
    # Bands in the order they first appear in the worksheet; nothing is re-sorted.
    assert plan.band_order == ["1800 MHz", "700 MHz", "3500 MHz", "800 MHz", "900 MHz"]
    order = [(t.row, t.band_label, t.direction, t.window_start_hz, t.window_stop_hz) for t in plan.tasks]
    assert order == [
        (2, "1800 MHz", DIR_UL, 1760 * MHZ, 1780 * MHZ),
        (3, "1800 MHz", DIR_DL, 1855 * MHZ, 1875 * MHZ),
        (4, "1800 MHz", DIR_DL, 1805 * MHZ, 1825 * MHZ),
        (5, "1800 MHz", DIR_UL, 1710 * MHZ, 1730 * MHZ),
        (6, "700 MHz", DIR_UL, 703 * MHZ, 713 * MHZ),
        (7, "700 MHz", DIR_DL, 758 * MHZ, 768 * MHZ),
        (8, "3500 MHz", DIR_SHARED, 3600 * MHZ, 3700 * MHZ),
        (9, "800 MHz", DIR_DL, 791 * MHZ, 801 * MHZ),
        (10, "800 MHz", DIR_UL, 832 * MHZ, 842 * MHZ),
        (11, "900 MHz", DIR_DL, 947_200_000, 955_200_000),
        (12, "900 MHz", DIR_UL, 902_200_000, 910_200_000),
    ]
    assert len(plan.tasks) == len(imported.valid_entries()) == 11
    for task in plan.tasks:
        # Only unit -> Hz and start/stop -> centre/span; the request is the row.
        assert task.requested_start_hz == task.window_start_hz and task.requested_stop_hz == task.window_stop_hz
        assert task.center_hz * 2 == task.window_start_hz + task.window_stop_hz
        assert task.bandwidth_hz == task.window_stop_hz - task.window_start_hz
        assert task.window_count == 1 and task.window_index == 1 and task.adjustments == ()
        # The app's duration for every row; the workbook's duration column is a result, never an instruction.
        assert task.duration_s == 10 and task.duration_source == DURATION_FROM_CAMPAIGN
    tdd = plan.tasks[6]
    assert tdd.center_hz == 3650 * MHZ and tdd.bandwidth_hz == 100 * MHZ
    assert tdd.range_text == "3600-3700 MHz" and tdd.request_text == "100 MHz for 10 s"
    assert plan.tasks[9].range_text == "947.2-955.2 MHz"
    # The 100 MHz row is wider than the 40-8's receiver: said, not changed.
    wider = [i for i in plan.issues if i.code == "wider_than_receiver"]
    assert [i.row for i in wider] == [8] and "sent as one request" in wider[0].message
    # Estimates are information only.
    assert plan.estimated_bytes == sum(t.estimated_bytes for t in plan.tasks) > 0
    assert plan.estimated_round_seconds(wait_s=10, overhead_s=0) == plan.capture_seconds + 10 * 10
    # Round trip through JSON keeps every request.
    again = CellularPlan.from_dict(plan.to_dict())
    assert [t.to_dict() for t in again.tasks] == [t.to_dict() for t in plan.tasks]
    assert not again.legacy


def test_request_count_and_ranges_do_not_depend_on_the_duration(tmp_path):
    imported = import_protocol(_plan_book(tmp_path / "wide.xlsx", WIDE_ROWS), "Plan")
    cap = capability_for("R100-18")
    expected = [(753 * MHZ, 803 * MHZ), (1805 * MHZ, 1880 * MHZ), (3300 * MHZ, 3800 * MHZ), (3300 * MHZ, 3400 * MHZ)]
    for seconds in (0.5, 1, 2, 2.5, 5, 30, 600):
        plan = build_plan(imported, cap, fallback_duration_s=seconds)
        assert plan.ready, seconds
        assert [(t.window_start_hz, t.window_stop_hz) for t in plan.tasks] == expected, seconds
        assert all(t.duration_s == seconds for t in plan.tasks)
        assert [t.window_count for t in plan.tasks] == [1, 1, 1, 1]


def test_3300_3800_mhz_stays_one_500_mhz_request(tmp_path):
    imported = import_protocol(_plan_book(tmp_path / "wide.xlsx", WIDE_ROWS), "Plan")
    plan = build_plan(imported, capability_for("R100-18"), fallback_duration_s=2)
    wide = [t for t in plan.tasks if t.row == 4]
    assert len(wide) == 1
    assert wide[0].center_hz == 3_550_000_000 and wide[0].bandwidth_hz == 500_000_000
    assert wide[0].request_text == "500 MHz for 2 s"
    notes = [i for i in plan.issues if i.row == 4]
    assert [i.level for i in notes] == ["info"]
    # Never presented as simultaneous coverage of the whole range.
    assert "the files show what CRFS recorded" in notes[0].message
    assert "simultaneous" not in notes[0].message.casefold()


def test_missing_duration_and_out_of_range_rows_are_reported_not_changed(tmp_path):
    rows = [
        ["Band", "Direction", "Start (MHz)", "Stop (MHz)"],
        ["700", "DL", 758, 768],
        ["9000", "DL", 9000, 9010],
    ]
    imported = import_protocol(_plan_book(tmp_path / "plan.xlsx", rows), "Plan")
    cap = capability_for("R40-8")
    plan = build_plan(imported, cap)
    assert [i.code for i in plan.errors] == ["duration_missing"]
    assert plan.tasks == [] and not plan.ready
    plan2 = build_plan(imported, cap, fallback_duration_s=2)
    assert plan2.errors == [] and plan2.ready
    outside = [i for i in plan2.issues if i.code == "outside_tuning_range"]
    assert len(outside) == 1 and "sent as listed" in outside[0].message
    assert plan2.tasks[1].window_start_hz == 9_000 * MHZ and plan2.tasks[1].window_stop_hz == 9_010 * MHZ
    bad = build_plan(imported, cap, fallback_duration_s=0)
    assert "fallback_invalid" in {i.code for i in bad.errors} and not bad.ready


def test_a_row_that_cannot_be_sent_exactly_is_reported_and_left_out(tmp_path):
    rows = [["Band", "Direction", "Start", "Stop"], ["700", "DL", "758 MHz", "768.000001 MHz"], ["800", "DL", "791 MHz", "801 MHz"]]
    plan = build_plan(import_protocol(_plan_book(tmp_path / "odd.xlsx", rows), "Plan"), capability_for("R40-8"), fallback_duration_s=1)
    errors = [i for i in plan.errors if i.code == "unrepresentable"]
    assert len(errors) == 1 and errors[0].row == 2 and "whole Hz" in errors[0].message
    # Nothing rounded or widened: the odd row has no request and the plan cannot start.
    assert [t.row for t in plan.tasks] == [3] and not plan.ready
    assert request_range(758 * MHZ, 768_000_001)[0] is None
    exact, problem = request_range(3300 * MHZ, 3800 * MHZ)
    assert problem == "" and exact.center_hz == 3550 * MHZ and exact.bandwidth_hz == 500 * MHZ
    assert request_range(800 * MHZ, 800 * MHZ)[0] is None


def test_repeat_column_sends_the_same_unchanged_request_again(tmp_path):
    rows = [["Band", "Direction", "Start (MHz)", "Stop (MHz)", "Repeat"], ["3500", "TDD", 3500, 3550, 2]]
    plan = build_plan(import_protocol(_plan_book(tmp_path / "rep.xlsx", rows), "Plan"), capability_for("R40-8"), fallback_duration_s=1)
    assert [(t.repetition, t.repetitions) for t in plan.tasks] == [(1, 2), (2, 2)]
    first, second = plan.tasks
    assert (first.center_hz, first.bandwidth_hz, first.duration_s) == (second.center_hz, second.bandwidth_hz, second.duration_s)
    assert first.task_key != second.task_key


def test_plans_saved_by_older_versions_are_recognised(tmp_path):
    imported = import_protocol(_plan_book(tmp_path / "chart.xlsx", CHART_ROWS), "Plan")
    data = build_plan(imported, capability_for("R40-8"), fallback_duration_s=2).to_dict()
    old = dict(data)
    old.pop("request_rule")
    assert CellularPlan.from_dict(old).legacy
    split = dict(data, tasks=[dict(task, window_count=2) for task in data["tasks"]])
    assert CellularPlan.from_dict(split).legacy
    assert not CellularPlan.from_dict(data).legacy


def test_summary_line_and_request_sentence(tmp_path):
    imported = import_protocol(_plan_book(tmp_path / "chart.xlsx", CHART_ROWS), "Plan")
    plan = build_plan(imported, capability_for("R100-18"), fallback_duration_s=2)
    assert summary_line(plan, wait_s=10, passes=1) == "11 rows · 2 s per request · 10 s between recordings · 1 pass"
    assert summary_line(plan, wait_s=90, passes=0).endswith("1 min 30 s between recordings · until stopped")
    assert summary_line(plan, wait_s=0, passes=3).endswith("· 3 passes")
    rows = [["Band", "Direction", "Start (MHz)", "Stop (MHz)", "Repeat"], ["3500", "TDD", 3500, 3550, 2]]
    repeated = build_plan(import_protocol(_plan_book(tmp_path / "rep.xlsx", rows), "Plan"), capability_for("R40-8"), fallback_duration_s=1)
    assert summary_line(repeated, wait_s=5, passes=1) == "1 row · 2 requests · 1 s per request · 5 s between recordings · 1 pass"
    assert REQUEST_SENTENCE == (
        "Each Excel row is sent to CRFS as one recording request using the listed frequency range and selected duration."
    )
    for word in ("capture", "window", "budget"):
        assert word not in summary_line(plan, wait_s=10, passes=1)


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
