"""Row model and next-run arithmetic for scheduled tasks."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Any, Dict, Literal, Optional
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

ScheduleType = Literal["once", "interval", "cron", "random_daily"]
TaskStatus = Literal["active", "completed", "cancelled", "failed"]

_RANDOM_DAILY_MAX_SKIP_STREAK = 10
_DEFAULT_TIMEZONE = "Europe/Kyiv"


@dataclass
class TaskRecord:
    """In-memory view of a row from ``scheduler_tasks``."""

    task_id: int
    task_name: str
    prompt: str
    schedule_type: ScheduleType
    run_at: Optional[datetime]
    interval_minutes: Optional[int]
    cron_expression: Optional[str]
    max_runs: Optional[int]
    runs_count: int
    status: TaskStatus
    next_run_at: datetime
    last_run_at: Optional[datetime]
    last_error: Optional[str]
    working_directory: str
    target_chat_id: Optional[int]
    created_by: int
    created_at: datetime
    updated_at: datetime
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    skip_probability: float = 0.0
    timezone: Optional[str] = None
    agent_backend: str = "claude"

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "TaskRecord":
        return cls(
            task_id=int(row["task_id"]),
            task_name=row["task_name"],
            prompt=row["prompt"],
            schedule_type=row["schedule_type"],
            run_at=_parse_dt(row.get("run_at")),
            interval_minutes=row.get("interval_minutes"),
            cron_expression=row.get("cron_expression"),
            max_runs=row.get("max_runs"),
            runs_count=int(row.get("runs_count") or 0),
            status=row.get("status") or "active",
            next_run_at=_parse_dt(row["next_run_at"]) or datetime.now(UTC),
            last_run_at=_parse_dt(row.get("last_run_at")),
            last_error=row.get("last_error"),
            working_directory=row["working_directory"],
            target_chat_id=row.get("target_chat_id"),
            created_by=int(row.get("created_by") or 0),
            agent_backend=row.get("agent_backend") or "claude",
            created_at=_parse_dt(row.get("created_at")) or datetime.now(UTC),
            updated_at=_parse_dt(row.get("updated_at")) or datetime.now(UTC),
            window_start=row.get("window_start"),
            window_end=row.get("window_end"),
            skip_probability=float(row.get("skip_probability") or 0.0),
            timezone=row.get("timezone"),
        )


def parse_time_of_day(value: str) -> float:
    """Parse a time-of-day string into seconds from midnight (float).

    Accepted formats: ``HH:MM``, ``HH:MM:SS``, ``HH:MM:SS.fff`` (any
    fractional digits). ``24:00`` / ``24:00:00`` / ``24:00:00.000`` are
    accepted as the end-of-day sentinel (86400s). Raises ``ValueError``
    on malformed input or out-of-range values.
    """
    text = value.strip()
    parts = text.split(":")
    if len(parts) not in (2, 3):
        raise ValueError(
            f"expected 'HH:MM', 'HH:MM:SS' or 'HH:MM:SS.fff', got {value!r}"
        )

    try:
        hh = int(parts[0])
        mm = int(parts[1])
        ss: float = float(parts[2]) if len(parts) == 3 else 0.0
    except ValueError as exc:
        raise ValueError(
            f"expected 'HH:MM', 'HH:MM:SS' or 'HH:MM:SS.fff', got {value!r}"
        ) from exc

    # End-of-day sentinel: 24:00 / 24:00:00 / 24:00:00.000
    if hh == 24 and mm == 0 and ss == 0.0:
        return 86400.0

    if not (0 <= hh <= 23 and 0 <= mm <= 59 and 0.0 <= ss < 60.0):
        raise ValueError(f"time-of-day out of range: {value!r}")

    return hh * 3600.0 + mm * 60.0 + ss


# Back-compat alias for any caller importing the old name.
parse_hhmm = parse_time_of_day


def compute_random_daily_next_run(
    *,
    window_start: str,
    window_end: str,
    skip_probability: float,
    timezone: str,
    now: datetime,
    last_fired: Optional[datetime] = None,
) -> datetime:
    """Pick a random fire time within the given daily window.

    The selected date is today (if the window has not yet closed today
    and we haven't already fired today) or the earliest future day that
    survives the skip-roll. Within that date a uniform random minute in
    ``[window_start, window_end]`` is picked. Returns an aware UTC
    datetime.
    """
    if not (0.0 <= skip_probability < 1.0):
        raise ValueError("skip_probability must be in [0.0, 1.0)")

    try:
        tz = ZoneInfo(timezone)
    except Exception as exc:
        raise ValueError(f"unknown timezone {timezone!r}") from exc

    start_sec = parse_time_of_day(window_start)
    end_sec = parse_time_of_day(window_end)
    if end_sec <= start_sec:
        raise ValueError("window_end must be strictly after window_start")

    now_local = now.astimezone(tz)
    candidate_date = now_local.date()

    # If we already fired today, or the window has already closed, skip to
    # tomorrow.
    today_close_local = datetime.combine(candidate_date, time(0), tz) + timedelta(
        seconds=end_sec
    )
    already_fired_today = False
    if last_fired is not None:
        already_fired_today = last_fired.astimezone(tz).date() == candidate_date
    if already_fired_today or now_local >= today_close_local:
        candidate_date = candidate_date + timedelta(days=1)

    # Roll the skip dice; bounded streak to keep this finite even if the
    # caller passes a pathological probability.
    for _ in range(_RANDOM_DAILY_MAX_SKIP_STREAK):
        if random.random() >= skip_probability:
            break
        candidate_date = candidate_date + timedelta(days=1)

    # Pick a uniform random offset within the window. On the first
    # eligible day we avoid picking a time already in the past.
    effective_start = start_sec
    day_start_local = datetime.combine(candidate_date, time(0), tz)
    if candidate_date == now_local.date():
        secs_into_day = (now_local - day_start_local).total_seconds()
        # +0.001 so we never pick the exact current instant.
        effective_start = max(start_sec, secs_into_day + 0.001)
        if effective_start >= end_sec:
            # Window already closed on this date — fall through to tomorrow.
            candidate_date = candidate_date + timedelta(days=1)
            day_start_local = datetime.combine(candidate_date, time(0), tz)
            effective_start = start_sec

    chosen_sec = random.uniform(effective_start, end_sec)
    candidate_local = day_start_local + timedelta(seconds=chosen_sec)
    return candidate_local.astimezone(UTC)


def parse_iso_utc(value: str) -> datetime:
    """Parse an ISO 8601 string into an aware UTC datetime.

    Accepts trailing 'Z' as UTC. Naive inputs are assumed to be UTC.
    """
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def compute_initial_next_run(
    schedule_type: ScheduleType,
    *,
    run_at: Optional[datetime],
    interval_minutes: Optional[int],
    cron_expression: Optional[str],
    window_start: Optional[str] = None,
    window_end: Optional[str] = None,
    skip_probability: Optional[float] = None,
    timezone: Optional[str] = None,
    now: Optional[datetime] = None,
) -> datetime:
    """Work out the first ``next_run_at`` for a brand-new task."""
    current = now or datetime.now(UTC)

    if schedule_type == "once":
        if run_at is None:
            raise ValueError("run_at required for schedule_type='once'")
        if run_at <= current:
            raise ValueError("run_at must be in the future")
        return run_at

    if schedule_type == "interval":
        if not interval_minutes or interval_minutes < 1:
            raise ValueError("interval_minutes must be >= 1")
        return current + timedelta(minutes=interval_minutes)

    if schedule_type == "cron":
        if not cron_expression:
            raise ValueError("cron_expression required for schedule_type='cron'")
        trigger = CronTrigger.from_crontab(cron_expression, timezone=UTC)
        nxt = trigger.get_next_fire_time(None, current)
        if nxt is None:
            raise ValueError(f"cron_expression '{cron_expression}' never fires")
        return nxt.astimezone(UTC)

    if schedule_type == "random_daily":
        if not window_start or not window_end:
            raise ValueError(
                "window_start and window_end required for "
                "schedule_type='random_daily'"
            )
        return compute_random_daily_next_run(
            window_start=window_start,
            window_end=window_end,
            skip_probability=skip_probability or 0.0,
            timezone=timezone or _DEFAULT_TIMEZONE,
            now=current,
        )

    raise ValueError(f"unknown schedule_type: {schedule_type!r}")


def compute_next_run(
    record: TaskRecord,
    *,
    fired_at: datetime,
) -> Optional[datetime]:
    """Given that ``record`` just fired at ``fired_at``, return the next
    ``next_run_at`` or ``None`` if the task is finished.
    """
    if record.schedule_type == "once":
        return None

    if record.schedule_type == "interval":
        if record.interval_minutes is None:
            return None
        return fired_at + timedelta(minutes=record.interval_minutes)

    if record.schedule_type == "cron":
        if not record.cron_expression:
            return None
        trigger = CronTrigger.from_crontab(record.cron_expression, timezone=UTC)
        nxt = trigger.get_next_fire_time(fired_at, fired_at)
        if nxt is None:
            return None
        return nxt.astimezone(UTC)

    if record.schedule_type == "random_daily":
        if not record.window_start or not record.window_end:
            return None
        return compute_random_daily_next_run(
            window_start=record.window_start,
            window_end=record.window_end,
            skip_probability=record.skip_probability,
            timezone=record.timezone or _DEFAULT_TIMEZONE,
            now=fired_at,
            last_fired=fired_at,
        )

    return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            return parse_iso_utc(value)
        except ValueError:
            return None
    return None
