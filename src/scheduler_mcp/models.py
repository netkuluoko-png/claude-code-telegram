"""Row model and next-run arithmetic for scheduled tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Dict, Literal, Optional

from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

ScheduleType = Literal["once", "interval", "cron"]
TaskStatus = Literal["active", "completed", "cancelled", "failed"]


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
            created_at=_parse_dt(row.get("created_at")) or datetime.now(UTC),
            updated_at=_parse_dt(row.get("updated_at")) or datetime.now(UTC),
        )


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
            raise ValueError(
                f"cron_expression '{cron_expression}' never fires"
            )
        return nxt.astimezone(UTC)

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
