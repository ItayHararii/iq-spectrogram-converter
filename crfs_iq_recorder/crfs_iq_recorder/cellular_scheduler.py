"""When the collection rounds run.

A schedule is either "run now" or a list of times of day on selected dates
(or every day). Each occurrence is a *round*: one complete pass of the
plan. Times are expressed in an explicit time zone that defaults to the
computer's local zone; every stored timestamp also carries UTC.

Rounds never overlap and are never dropped silently: a round that is due
while another is still running is marked *delayed* and starts when the
sensor is free, keeping its original scheduled time. A round whose time
passed while the application was not running (or the PC slept) is marked
*missed* and waits for the operator to run or skip it; it is never
performed at a different hour on its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any

MODE_NOW = "now"
MODE_TIMES = "times"

TZ_LOCAL = "local"
TZ_UTC = "utc"
TZ_FIXED = "fixed"

ROUND_PENDING = "pending"
ROUND_DUE = "due"
ROUND_DELAYED = "delayed"
ROUND_RUNNING = "running"
ROUND_PAUSED = "paused"
ROUND_DONE = "done"
ROUND_PARTIAL = "partial"
ROUND_FAILED = "failed"
ROUND_MISSED = "missed"
ROUND_SKIPPED = "skipped"
ROUND_STOPPED = "stopped"

FINISHED_STATES = {ROUND_DONE, ROUND_PARTIAL, ROUND_FAILED, ROUND_SKIPPED, ROUND_STOPPED}

# A round is "missed" when its time passed by more than this while nothing
# could start it (application closed, PC asleep). Shorter lateness is
# ordinary scheduler latency and the round starts at once.
MISSED_AFTER_S = 15 * 60

_TIME_RE = re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*$")


@dataclass(frozen=True)
class ScheduleTimeZone:
    kind: str = TZ_LOCAL
    offset_minutes: int = 0

    def tzinfo(self, at: datetime | None = None) -> tzinfo:
        if self.kind == TZ_UTC:
            return timezone.utc
        if self.kind == TZ_FIXED:
            return timezone(timedelta(minutes=self.offset_minutes))
        probe = (at or datetime.now()).astimezone()
        return probe.tzinfo or timezone.utc

    def localize(self, naive: datetime) -> datetime:
        """Attach this zone to a naive wall-clock time (DST-aware for the local zone)."""
        if self.kind == TZ_LOCAL:
            # The C library resolves the UTC offset for that wall-clock time.
            return naive.replace(tzinfo=None).astimezone()
        return naive.replace(tzinfo=self.tzinfo())

    def describe(self, at: datetime | None = None) -> str:
        if self.kind == TZ_UTC:
            return "UTC"
        if self.kind == TZ_FIXED:
            sign = "+" if self.offset_minutes >= 0 else "-"
            hours, minutes = divmod(abs(self.offset_minutes), 60)
            return f"UTC{sign}{hours:02d}:{minutes:02d}"
        probe = (at or datetime.now()).astimezone()
        name = probe.tzname() or "local time"
        offset = probe.utcoffset() or timedelta(0)
        total = int(offset.total_seconds() // 60)
        sign = "+" if total >= 0 else "-"
        hours, minutes = divmod(abs(total), 60)
        return f"Computer local time ({name}, UTC{sign}{hours:02d}:{minutes:02d})"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "offset_minutes": self.offset_minutes}

    @classmethod
    def from_dict(cls, data: dict | None) -> "ScheduleTimeZone":
        data = data or {}
        kind = str(data.get("kind") or TZ_LOCAL)
        if kind not in {TZ_LOCAL, TZ_UTC, TZ_FIXED}:
            kind = TZ_LOCAL
        try:
            offset = int(data.get("offset_minutes") or 0)
        except (TypeError, ValueError):
            offset = 0
        return cls(kind=kind, offset_minutes=max(-14 * 60, min(14 * 60, offset)))


def parse_time_of_day(text: str) -> time | None:
    match = _TIME_RE.match(text or "")
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return time(hour, minute)


def parse_times(text: str) -> tuple[list[time], list[str]]:
    """'08:00, 13:00 18:00' -> sorted unique times and the tokens that were not times."""
    good: set[time] = set()
    bad: list[str] = []
    for token in re.split(r"[,;\s]+", text or ""):
        if not token:
            continue
        parsed = parse_time_of_day(token)
        if parsed is None:
            bad.append(token)
        else:
            good.add(parsed)
    return sorted(good), bad


@dataclass
class Schedule:
    mode: str = MODE_NOW
    times: list[time] = field(default_factory=list)
    dates: list[date] = field(default_factory=list)
    daily: bool = False
    tz: ScheduleTimeZone = field(default_factory=ScheduleTimeZone)
    # Daily repetition runs until this date (inclusive) when set.
    daily_until: date | None = None
    # Passes over the whole protocol. Run now: this many passes back to back
    # (after the wait interval), 0 = until stopped. Timed schedule: a count
    # above one caps the number of passes; 0 or 1 = every listed time.
    repeat: int = 1

    def describe(self) -> str:
        if self.mode == MODE_NOW:
            passes = "until stopped" if self.repeat == 0 else (f"{self.repeat} passes" if self.repeat > 1 else "one pass")
            return f"Run now ({passes})"
        cap = f" ({self.repeat} passes)" if self.repeat > 1 else ""
        times = ", ".join(t.strftime("%H:%M") for t in self.times) or "no times"
        if self.daily:
            until = f" until {self.daily_until.isoformat()}" if self.daily_until else ""
            return f"Every day at {times}{until}{cap}"
        dates = ", ".join(d.isoformat() for d in self.dates) or "no dates"
        return f"At {times} on {dates}{cap}"

    def occurrences(self, *, after: datetime, horizon_days: int = 14) -> list[datetime]:
        """Aware scheduled starts after `after`, nearest first."""
        if self.mode != MODE_TIMES or not self.times:
            return []
        out: list[datetime] = []
        days: list[date]
        anchor = after.astimezone(self.tz.tzinfo(after)).date()
        if self.daily:
            days = [anchor + timedelta(days=n) for n in range(horizon_days + 1)]
            if self.daily_until is not None:
                days = [d for d in days if d <= self.daily_until]
        else:
            days = sorted(set(self.dates))
        for day in days:
            for t in self.times:
                when = self.tz.localize(datetime.combine(day, t))
                if when > after:
                    out.append(when)
        return sorted(out)

    def all_planned(self, *, now: datetime, horizon_days: int = 14) -> list[datetime]:
        """Every scheduled start of the selected dates (past ones included for missed detection)."""
        if self.mode != MODE_TIMES or not self.times:
            return []
        if self.daily:
            anchor = now.astimezone(self.tz.tzinfo(now)).date()
            days = [anchor + timedelta(days=n) for n in range(horizon_days + 1)]
            if self.daily_until is not None:
                days = [d for d in days if d <= self.daily_until]
        else:
            days = sorted(set(self.dates))
        return sorted(self.tz.localize(datetime.combine(day, t)) for day in days for t in self.times)

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "times": [t.strftime("%H:%M") for t in self.times],
            "dates": [d.isoformat() for d in self.dates],
            "daily": self.daily,
            "daily_until": self.daily_until.isoformat() if self.daily_until else None,
            "tz": self.tz.to_dict(),
            "repeat": self.repeat,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "Schedule":
        data = data or {}
        mode = str(data.get("mode") or MODE_NOW)
        times = [t for t in (parse_time_of_day(str(item)) for item in data.get("times") or []) if t is not None]
        dates: list[date] = []
        for item in data.get("dates") or []:
            try:
                dates.append(date.fromisoformat(str(item)))
            except ValueError:
                continue
        until = data.get("daily_until")
        try:
            daily_until = date.fromisoformat(str(until)) if until else None
        except ValueError:
            daily_until = None
        try:
            repeat = max(int(data.get("repeat", 1) or 0), 0)
        except (TypeError, ValueError):
            repeat = 1
        return cls(
            mode=mode if mode in {MODE_NOW, MODE_TIMES} else MODE_NOW,
            times=sorted(set(times)),
            dates=sorted(set(dates)),
            daily=bool(data.get("daily", False)),
            tz=ScheduleTimeZone.from_dict(data.get("tz")),
            daily_until=daily_until,
            repeat=repeat,
        )


def round_id(scheduled: datetime, campaign: str) -> str:
    """Unique, readable round identifier: campaign slug, local date and time, UTC marker."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", campaign or "campaign").strip("-")[:24] or "campaign"
    local = scheduled.strftime("%Y%m%d-%H%M")
    utc = scheduled.astimezone(timezone.utc).strftime("%H%MZ")
    return f"{slug}_{local}_{utc}"


@dataclass
class RoundRecord:
    """One scheduled (or immediate) pass with its lifecycle timestamps."""

    round_id: str
    scheduled_local: datetime
    scheduled_utc: datetime
    status: str = ROUND_PENDING
    actual_start_utc: datetime | None = None
    finished_utc: datetime | None = None
    tasks_total: int = 0
    tasks_confirmed: int = 0
    tasks_failed: int = 0
    tasks_uncertain: int = 0
    tasks_skipped: int = 0
    note: str = ""
    # Results sheet of this round in the protocol workbook, and its last write state.
    protocol_sheet: str = ""
    protocol_status: str = ""

    @property
    def finished(self) -> bool:
        return self.status in FINISHED_STATES

    def to_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id,
            "scheduled_local": self.scheduled_local.isoformat(),
            "scheduled_utc": self.scheduled_utc.isoformat(),
            "status": self.status,
            "actual_start_utc": self.actual_start_utc.isoformat() if self.actual_start_utc else None,
            "finished_utc": self.finished_utc.isoformat() if self.finished_utc else None,
            "tasks_total": self.tasks_total,
            "tasks_confirmed": self.tasks_confirmed,
            "tasks_failed": self.tasks_failed,
            "tasks_uncertain": self.tasks_uncertain,
            "tasks_skipped": self.tasks_skipped,
            "note": self.note,
            "protocol_sheet": self.protocol_sheet,
            "protocol_status": self.protocol_status,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "RoundRecord":
        def _dt(value) -> datetime | None:
            if not value:
                return None
            try:
                parsed = datetime.fromisoformat(str(value))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed

        scheduled_utc = _dt(data.get("scheduled_utc")) or datetime.now(timezone.utc)
        scheduled_local = _dt(data.get("scheduled_local")) or scheduled_utc
        return cls(
            round_id=str(data.get("round_id") or ""),
            scheduled_local=scheduled_local,
            scheduled_utc=scheduled_utc,
            status=str(data.get("status") or ROUND_PENDING),
            actual_start_utc=_dt(data.get("actual_start_utc")),
            finished_utc=_dt(data.get("finished_utc")),
            tasks_total=int(data.get("tasks_total") or 0),
            tasks_confirmed=int(data.get("tasks_confirmed") or 0),
            tasks_failed=int(data.get("tasks_failed") or 0),
            tasks_uncertain=int(data.get("tasks_uncertain") or 0),
            tasks_skipped=int(data.get("tasks_skipped") or 0),
            note=str(data.get("note") or ""),
            protocol_sheet=str(data.get("protocol_sheet") or ""),
            protocol_status=str(data.get("protocol_status") or ""),
        )


def make_round(scheduled: datetime, campaign: str, tasks_total: int) -> RoundRecord:
    if scheduled.tzinfo is None:
        scheduled = scheduled.astimezone()
    return RoundRecord(
        round_id=round_id(scheduled, campaign),
        scheduled_local=scheduled,
        scheduled_utc=scheduled.astimezone(timezone.utc),
        tasks_total=tasks_total,
    )


def materialise_rounds(
    schedule: Schedule,
    existing: list[RoundRecord],
    *,
    campaign: str,
    tasks_total: int,
    now: datetime,
    horizon_days: int = 14,
) -> list[RoundRecord]:
    """Add rounds for every scheduled start not yet known. Existing rounds keep their state."""
    known = {record.scheduled_utc for record in existing}
    out = list(existing)
    for when in schedule.all_planned(now=now, horizon_days=horizon_days):
        key = when.astimezone(timezone.utc)
        if key in known:
            continue
        out.append(make_round(when, campaign, tasks_total))
        known.add(key)
    out.sort(key=lambda record: record.scheduled_utc)
    return out


def classify_pending(rounds: list[RoundRecord], *, now: datetime, busy: bool, missed_after_s: float = MISSED_AFTER_S) -> list[RoundRecord]:
    """Update pending rounds: due now, delayed behind a running round, or missed."""
    now_utc = now.astimezone(timezone.utc)
    for record in rounds:
        if record.status not in {ROUND_PENDING, ROUND_DUE, ROUND_DELAYED}:
            continue
        if record.scheduled_utc > now_utc:
            record.status = ROUND_PENDING
            continue
        late = (now_utc - record.scheduled_utc).total_seconds()
        if record.status == ROUND_PENDING and late > missed_after_s:
            record.status = ROUND_MISSED
            record.note = f"Scheduled time passed {int(late // 60)} min ago while no round could start."
            continue
        if busy:
            record.status = ROUND_DELAYED
            if not record.note:
                record.note = "Previous round still running at the scheduled time; starts when the sensor is free."
        elif record.status != ROUND_DELAYED:
            # A delayed round keeps that label (it starts late and the report says so).
            record.status = ROUND_DUE
    return rounds


def next_due(rounds: list[RoundRecord]) -> RoundRecord | None:
    due = [record for record in rounds if record.status in {ROUND_DUE, ROUND_DELAYED}]
    return min(due, key=lambda record: record.scheduled_utc) if due else None


def next_pending(rounds: list[RoundRecord], *, now: datetime) -> RoundRecord | None:
    now_utc = now.astimezone(timezone.utc)
    pending = [record for record in rounds if record.status == ROUND_PENDING and record.scheduled_utc > now_utc]
    return min(pending, key=lambda record: record.scheduled_utc) if pending else None


def missed_rounds(rounds: list[RoundRecord]) -> list[RoundRecord]:
    return [record for record in rounds if record.status == ROUND_MISSED]


def tight_schedule_warning(schedule: Schedule, estimated_round_s: float, *, now: datetime) -> str | None:
    """Warn when consecutive scheduled rounds are closer together than one round takes."""
    starts = schedule.all_planned(now=now)
    if len(starts) < 2 or estimated_round_s <= 0:
        return None
    gaps = [(b - a).total_seconds() for a, b in zip(starts, starts[1:])]
    shortest = min(gaps)
    if shortest < estimated_round_s:
        return (
            f"Rounds are scheduled {shortest / 60:.0f} min apart but one round is estimated to take "
            f"{estimated_round_s / 60:.0f} min. Later rounds will start late (delayed), not overlap."
        )
    return None
