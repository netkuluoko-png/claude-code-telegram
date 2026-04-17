"""MCP server: agent-facing scheduler tools.

Runs as a stdio subprocess spawned by the Claude CLI. Writes tasks to
the bot's SQLite DB; the in-process ``SchedulerTaskWorker`` picks them
up and fires them via the event bus.

Tools:

* ``schedule_task`` — create a new one-shot / interval / cron task.
* ``list_tasks`` — inspect the queue.
* ``delete_task`` — cancel a task.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

from fastmcp import FastMCP
from pydantic import ValidationError

from .models import compute_initial_next_run, parse_iso_utc
from .schemas import DeleteTaskInput, ListTasksInput, ScheduleTaskInput
from .storage import SchedulerTaskStore

_DEFAULT_DB_PATH = "/app/data/bot.db"


def _database_path() -> Path:
    return Path(os.environ.get("SCHEDULER_DB_PATH", _DEFAULT_DB_PATH))


def _default_working_directory() -> str:
    return os.environ.get("APPROVED_DIRECTORY", "/app/data/project")


mcp = FastMCP("mcp-scheduler")
_store = SchedulerTaskStore(_database_path())


def _format_task_line(record: object) -> str:
    # record is TaskRecord; imported lazily to avoid circular type hints
    from .models import TaskRecord

    assert isinstance(record, TaskRecord)
    schedule_descr = _describe_schedule(record)
    next_run = record.next_run_at.isoformat() if record.next_run_at else "-"
    runs = f"{record.runs_count}" + (
        f"/{record.max_runs}" if record.max_runs is not None else ""
    )
    chat = (
        f" chat={record.target_chat_id}" if record.target_chat_id is not None else ""
    )
    return (
        f"#{record.task_id} [{record.status}] '{record.task_name}'"
        f" — {schedule_descr}"
        f" | next_run={next_run}"
        f" | runs={runs}"
        f" | cwd={record.working_directory}{chat}"
    )


def _describe_schedule(record: object) -> str:
    from .models import TaskRecord

    assert isinstance(record, TaskRecord)
    if record.schedule_type == "once":
        ts = record.run_at.isoformat() if record.run_at else "-"
        return f"once at {ts}"
    if record.schedule_type == "interval":
        return f"every {record.interval_minutes} min"
    if record.schedule_type == "cron":
        return f"cron '{record.cron_expression}'"
    return record.schedule_type


@mcp.tool()
async def schedule_task(
    task_name: str,
    prompt: str,
    schedule_type: str,
    run_at: Optional[str] = None,
    interval_minutes: Optional[int] = None,
    cron_expression: Optional[str] = None,
    max_runs: Optional[int] = None,
    working_directory: Optional[str] = None,
    target_chat_id: Optional[int] = None,
) -> str:
    """Schedule an agent task to run later, once or on a recurring schedule.

    When the task fires, the bot's worker hands `prompt` to the same
    agent entry point that Telegram messages use — the agent runs in
    `working_directory`, produces a reply, and the bot delivers that
    reply to `target_chat_id` (or to the configured notification chats
    if omitted).

    Pick exactly one schedule mode:

    - schedule_type="once"     + run_at="2026-04-18T09:30:00Z"
    - schedule_type="interval" + interval_minutes=30
    - schedule_type="cron"     + cron_expression="0 9 * * 1-5"

    All times are UTC. `prompt` must be completely self-contained — the
    agent will have no memory of this conversation when the task fires.
    """
    try:
        payload = ScheduleTaskInput(
            task_name=task_name,
            prompt=prompt,
            schedule_type=schedule_type,  # type: ignore[arg-type]
            run_at=run_at,
            interval_minutes=interval_minutes,
            cron_expression=cron_expression,
            max_runs=max_runs,
            working_directory=working_directory,
            target_chat_id=target_chat_id,
        )
    except ValidationError as exc:
        return f"Error: {exc.errors()[0]['msg']}" if exc.errors() else f"Error: {exc}"

    run_at_dt = parse_iso_utc(payload.run_at) if payload.run_at else None
    now = datetime.now(UTC)

    try:
        next_run = compute_initial_next_run(
            payload.schedule_type,
            run_at=run_at_dt,
            interval_minutes=payload.interval_minutes,
            cron_expression=payload.cron_expression,
            now=now,
        )
    except ValueError as exc:
        return f"Error: {exc}"

    work_dir = payload.working_directory or _default_working_directory()
    created_by = int(os.environ.get("SCHEDULER_DEFAULT_USER_ID", "0") or "0")

    await _store.ensure_schema()
    record = await _store.create(
        task_name=payload.task_name,
        prompt=payload.prompt,
        schedule_type=payload.schedule_type,
        run_at=run_at_dt,
        interval_minutes=payload.interval_minutes,
        cron_expression=payload.cron_expression,
        max_runs=payload.max_runs,
        next_run_at=next_run,
        working_directory=work_dir,
        target_chat_id=payload.target_chat_id,
        created_by=created_by,
    )

    return (
        f"Task #{record.task_id} '{record.task_name}' scheduled.\n"
        f"{_describe_schedule(record)}\n"
        f"Next run: {record.next_run_at.isoformat()}\n"
        f"Working dir: {record.working_directory}"
    )


@mcp.tool()
async def list_tasks(status_filter: Optional[str] = None) -> str:
    """List scheduled agent tasks.

    status_filter:
      - omit or "active"  — still-running schedules (default)
      - "completed"       — one-shot tasks that have already fired or
                            recurring tasks that hit max_runs
      - "cancelled"       — tasks deleted via delete_task
      - "failed"          — tasks whose last dispatch raised
      - "all"             — every task regardless of status

    Returns one line per task with id, name, status, schedule summary
    and next fire time.
    """
    try:
        payload = ListTasksInput(status_filter=status_filter)  # type: ignore[arg-type]
    except ValidationError as exc:
        return f"Error: {exc.errors()[0]['msg']}" if exc.errors() else f"Error: {exc}"

    await _store.ensure_schema()
    records = await _store.list(payload.status_filter)
    if not records:
        scope = payload.status_filter or "active"
        return f"No {scope} tasks."

    lines: List[str] = [_format_task_line(r) for r in records]
    return "\n".join(lines)


@mcp.tool()
async def delete_task(task_id: int) -> str:
    """Cancel a scheduled task. Future firings are dropped; history is kept.

    Use list_tasks to discover the numeric task_id.
    """
    try:
        payload = DeleteTaskInput(task_id=task_id)
    except ValidationError as exc:
        return f"Error: {exc.errors()[0]['msg']}" if exc.errors() else f"Error: {exc}"

    await _store.ensure_schema()
    existing = await _store.get(payload.task_id)
    if existing is None:
        return f"Error: task #{payload.task_id} not found."
    if existing.status != "active":
        return (
            f"Task #{payload.task_id} '{existing.task_name}' is already "
            f"{existing.status}; nothing to cancel."
        )

    cancelled = await _store.delete(payload.task_id)
    if not cancelled:
        return f"Error: task #{payload.task_id} could not be cancelled."
    return f"Task #{payload.task_id} '{existing.task_name}' cancelled."


if __name__ == "__main__":
    mcp.run(transport="stdio")
