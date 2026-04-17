"""MCP server: agent-facing scheduler tools.

Runs as a stdio subprocess spawned by the Claude CLI. Writes tasks to
the bot's SQLite DB; the in-process ``SchedulerTaskWorker`` picks them
up and fires them via the event bus.

Tools:

* ``schedule_task`` — create a new one-shot / interval / cron task.
* ``list_tasks`` — inspect the queue.
* ``update_task`` — edit an existing task.
* ``delete_task`` — cancel a task.
* ``list_repos`` — list the working directories a task is allowed to use
  (same set as the Telegram ``/repo`` command: APPROVED_DIRECTORY +
  immediate subdirectories).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastmcp import FastMCP
from pydantic import ValidationError

from .models import TaskRecord, compute_initial_next_run, parse_iso_utc
from .schemas import (
    DeleteTaskInput,
    ListTasksInput,
    ScheduleTaskInput,
    UpdateTaskInput,
)
from .storage import SchedulerTaskStore

_DEFAULT_DB_PATH = "/app/data/bot.db"
_DEFAULT_APPROVED_DIR = "/app/data/project"


def _database_path() -> Path:
    return Path(os.environ.get("SCHEDULER_DB_PATH", _DEFAULT_DB_PATH))


def _approved_directory() -> Path:
    return Path(
        os.environ.get("APPROVED_DIRECTORY", _DEFAULT_APPROVED_DIR)
    ).resolve()


def _list_allowed_repos() -> Tuple[Path, List[Path]]:
    """Return (base, subdirs). ``subdirs`` excludes hidden entries.

    Mirrors what the Telegram ``/repo`` command surfaces so the agent's
    options here match the user's mental model.
    """
    base = _approved_directory()
    subdirs: List[Path] = []
    try:
        for d in sorted(base.iterdir(), key=lambda p: p.name):
            if d.is_dir() and not d.name.startswith("."):
                subdirs.append(d.resolve())
    except (OSError, FileNotFoundError):
        pass
    return base, subdirs


def _resolve_working_directory(
    value: Optional[str],
) -> Tuple[Optional[str], Optional[str]]:
    """Validate a user-supplied working directory.

    Returns ``(resolved_path_or_None, error_or_None)``. ``value=None``
    resolves to the APPROVED_DIRECTORY base. Non-absolute paths are
    interpreted as names relative to the base so the agent can just say
    ``"my-project"`` without knowing the absolute prefix.
    """
    base, subdirs = _list_allowed_repos()
    allowed = {base, *subdirs}

    if value is None or value == "":
        return str(base), None

    candidate: Path
    if value in ("/", ".", "..", "~", "base"):
        return str(base), None
    trimmed = value.strip()
    as_path = Path(trimmed)
    if as_path.is_absolute():
        candidate = as_path.resolve()
    else:
        candidate = (base / trimmed).resolve()

    if candidate not in allowed:
        names = [base.name or str(base)] + [p.name for p in subdirs]
        return None, (
            f"working_directory '{value}' is not an allowed repo. "
            f"Allowed (same as /repo): {', '.join(names)}. "
            f"Call list_repos for absolute paths."
        )
    return str(candidate), None


mcp = FastMCP("mcp-scheduler")
_store = SchedulerTaskStore(_database_path())


def _describe_schedule(record: TaskRecord) -> str:
    if record.schedule_type == "once":
        ts = record.run_at.isoformat() if record.run_at else "-"
        return f"once at {ts}"
    if record.schedule_type == "interval":
        return f"every {record.interval_minutes} min"
    if record.schedule_type == "cron":
        return f"cron '{record.cron_expression}'"
    return record.schedule_type


def _format_task_line(record: TaskRecord) -> str:
    schedule_descr = _describe_schedule(record)
    next_run = record.next_run_at.isoformat() if record.next_run_at else "-"
    runs = f"{record.runs_count}" + (
        f"/{record.max_runs}" if record.max_runs is not None else ""
    )
    chat = (
        f" chat={record.target_chat_id}"
        if record.target_chat_id is not None
        else ""
    )
    return (
        f"#{record.task_id} [{record.status}] '{record.task_name}'"
        f" — {schedule_descr}"
        f" | next_run={next_run}"
        f" | runs={runs}"
        f" | cwd={record.working_directory}{chat}"
    )


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

    All times are UTC. `working_directory` must be one of the repos
    visible via the Telegram /repo command (the APPROVED_DIRECTORY base
    or one of its immediate subdirectories) — call list_repos to see
    the current set. `prompt` must be completely self-contained — the
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

    work_dir, err = _resolve_working_directory(payload.working_directory)
    if err is not None:
        return f"Error: {err}"
    assert work_dir is not None

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

    created_by = int(os.environ.get("SCHEDULER_DEFAULT_USER_ID", "0") or "0")

    # Default the delivery target to the caller's private chat (chat_id ==
    # user_id for Telegram private chats). The bot injects both env vars at
    # MCP-spawn time; without them the worker falls back further to
    # created_by / NOTIFICATION_CHAT_IDS.
    effective_chat_id: Optional[int] = payload.target_chat_id
    if effective_chat_id is None:
        env_chat = os.environ.get("SCHEDULER_DEFAULT_CHAT_ID")
        if env_chat:
            try:
                effective_chat_id = int(env_chat)
            except ValueError:
                effective_chat_id = None

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
        target_chat_id=effective_chat_id,
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
async def update_task(
    task_id: int,
    task_name: Optional[str] = None,
    prompt: Optional[str] = None,
    schedule_type: Optional[str] = None,
    run_at: Optional[str] = None,
    interval_minutes: Optional[int] = None,
    cron_expression: Optional[str] = None,
    max_runs: Optional[int] = None,
    working_directory: Optional[str] = None,
    target_chat_id: Optional[int] = None,
    reactivate: Optional[bool] = None,
) -> str:
    """Edit an existing scheduled task.

    Only fields you pass are changed. If you change any schedule-shape
    field (`schedule_type`, `run_at`, `interval_minutes`,
    `cron_expression`) the next_run_at is recomputed from now.

    Rules:
    - Changing `schedule_type` requires the matching field for the new
      mode (same validation as schedule_task).
    - `max_runs=0` clears the cap (unlimited).
    - `target_chat_id=0` clears the override (fall back to
      NOTIFICATION_CHAT_IDS).
    - `working_directory` must be one of the repos in /repo (call
      list_repos for the full set).
    - `reactivate=True` revives a cancelled / completed / failed task
      back to status='active'; the task then also needs a valid
      future schedule (pass schedule_type + matching field, or the
      task must still have one from before).
    """
    try:
        payload = UpdateTaskInput(
            task_id=task_id,
            task_name=task_name,
            prompt=prompt,
            schedule_type=schedule_type,  # type: ignore[arg-type]
            run_at=run_at,
            interval_minutes=interval_minutes,
            cron_expression=cron_expression,
            max_runs=max_runs,
            working_directory=working_directory,
            target_chat_id=target_chat_id,
            reactivate=reactivate,
        )
    except ValidationError as exc:
        return (
            f"Error: {exc.errors()[0]['msg']}" if exc.errors() else f"Error: {exc}"
        )

    await _store.ensure_schema()
    existing = await _store.get(payload.task_id)
    if existing is None:
        return f"Error: task #{payload.task_id} not found."

    updates: Dict[str, Any] = {}

    if payload.task_name is not None:
        updates["task_name"] = payload.task_name
    if payload.prompt is not None:
        updates["prompt"] = payload.prompt

    # Resolve new schedule shape
    new_schedule_type = payload.schedule_type or existing.schedule_type
    schedule_touched = (
        payload.schedule_type is not None
        or payload.run_at is not None
        or payload.interval_minutes is not None
        or payload.cron_expression is not None
    )

    if schedule_touched:
        if new_schedule_type == "once":
            run_at_dt = (
                parse_iso_utc(payload.run_at)
                if payload.run_at
                else existing.run_at
            )
            if run_at_dt is None:
                return "Error: run_at is required for schedule_type='once'"
            next_interval = None
            next_cron = None
        elif new_schedule_type == "interval":
            run_at_dt = None
            next_interval = (
                payload.interval_minutes
                if payload.interval_minutes is not None
                else existing.interval_minutes
            )
            if next_interval is None:
                return (
                    "Error: interval_minutes is required for "
                    "schedule_type='interval'"
                )
            next_cron = None
        elif new_schedule_type == "cron":
            run_at_dt = None
            next_interval = None
            next_cron = payload.cron_expression or existing.cron_expression
            if not next_cron:
                return (
                    "Error: cron_expression is required for "
                    "schedule_type='cron'"
                )
        else:
            return f"Error: unknown schedule_type '{new_schedule_type}'"

        try:
            next_run = compute_initial_next_run(
                new_schedule_type,  # type: ignore[arg-type]
                run_at=run_at_dt,
                interval_minutes=next_interval,
                cron_expression=next_cron,
                now=datetime.now(UTC),
            )
        except ValueError as exc:
            return f"Error: {exc}"

        updates["schedule_type"] = new_schedule_type
        updates["run_at"] = run_at_dt
        updates["interval_minutes"] = next_interval
        updates["cron_expression"] = next_cron
        updates["next_run_at"] = next_run

    if payload.max_runs is not None:
        updates["max_runs"] = None if payload.max_runs == 0 else payload.max_runs

    if payload.working_directory is not None:
        work_dir, err = _resolve_working_directory(payload.working_directory)
        if err is not None:
            return f"Error: {err}"
        assert work_dir is not None
        updates["working_directory"] = work_dir

    if payload.target_chat_id is not None:
        updates["target_chat_id"] = (
            None if payload.target_chat_id == 0 else payload.target_chat_id
        )

    if payload.reactivate is True and existing.status != "active":
        updates["status"] = "active"
        # If the existing next_run_at is in the past (or bogus sentinel from
        # a fired task) we need a fresh one. If the user also touched the
        # schedule this is already handled above; otherwise recompute from
        # the task's existing shape.
        if "next_run_at" not in updates:
            try:
                fresh_next = compute_initial_next_run(
                    existing.schedule_type,
                    run_at=existing.run_at,
                    interval_minutes=existing.interval_minutes,
                    cron_expression=existing.cron_expression,
                    now=datetime.now(UTC),
                )
                updates["next_run_at"] = fresh_next
            except ValueError as exc:
                return (
                    f"Error: cannot reactivate task #{existing.task_id}: "
                    f"{exc}. Provide a new schedule (schedule_type + its "
                    "matching field)."
                )

    if not updates:
        return f"Task #{existing.task_id} '{existing.task_name}': nothing to update."

    updated = await _store.update_fields(payload.task_id, fields=updates)
    if updated is None:
        return f"Error: task #{payload.task_id} could not be updated."

    changed = sorted(updates.keys())
    return (
        f"Task #{updated.task_id} '{updated.task_name}' updated "
        f"({', '.join(changed)}).\n"
        f"{_format_task_line(updated)}"
    )


@mcp.tool()
async def delete_task(task_id: int) -> str:
    """Cancel a scheduled task. Future firings are dropped; history is kept.

    Use list_tasks to discover the numeric task_id.
    """
    try:
        payload = DeleteTaskInput(task_id=task_id)
    except ValidationError as exc:
        return (
            f"Error: {exc.errors()[0]['msg']}" if exc.errors() else f"Error: {exc}"
        )

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


@mcp.tool()
async def list_repos() -> str:
    """List the working directories a task may use.

    Returns the APPROVED_DIRECTORY base plus every immediate
    subdirectory (non-hidden) — the same set the Telegram /repo command
    shows. Use one of these absolute paths as `working_directory` in
    schedule_task / update_task. A short name relative to the base is
    also accepted.
    """
    base, subdirs = _list_allowed_repos()
    lines = [f"base: {base}"]
    for d in subdirs:
        is_git = (d / ".git").is_dir()
        marker = " (git)" if is_git else ""
        lines.append(f"- {d.name} -> {d}{marker}")
    if not subdirs:
        lines.append("(no subdirectories yet — base is the only option)")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
