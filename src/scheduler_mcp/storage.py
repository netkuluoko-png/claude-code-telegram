"""SQLite-backed persistence for scheduled agent tasks.

Used by both the MCP server subprocess (write side: schedule/list/delete)
and the in-process worker (read + update side: claim + mark fired).

The table is created by the main bot's migration #5 — this module only
does an idempotent ``CREATE TABLE IF NOT EXISTS`` safety net so the MCP
subprocess never blows up when it is spawned before the bot finishes its
own migration run.
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

import aiosqlite

from .models import ScheduleType, TaskRecord, TaskStatus

_ENSURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_tasks (
    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL,
    prompt TEXT NOT NULL,
    schedule_type TEXT NOT NULL,
    run_at TIMESTAMP,
    interval_minutes INTEGER,
    cron_expression TEXT,
    max_runs INTEGER,
    runs_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    next_run_at TIMESTAMP NOT NULL,
    last_run_at TIMESTAMP,
    last_error TEXT,
    working_directory TEXT NOT NULL,
    target_chat_id INTEGER,
    created_by INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_scheduler_tasks_due
    ON scheduler_tasks(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_scheduler_tasks_status
    ON scheduler_tasks(status);
"""


class SchedulerTaskStore:
    """Thin async CRUD wrapper around the ``scheduler_tasks`` table."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        conn = await aiosqlite.connect(
            self.database_path, detect_types=sqlite3.PARSE_DECLTYPES
        )
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
        finally:
            await conn.close()

    async def ensure_schema(self) -> None:
        """Create the table if it does not exist yet.

        Safe to call from the MCP subprocess before the bot migration has
        finished on a brand-new volume.
        """
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        async with self._connect() as conn:
            await conn.executescript(_ENSURE_SCHEMA)
            await conn.commit()

    async def create(
        self,
        *,
        task_name: str,
        prompt: str,
        schedule_type: ScheduleType,
        run_at: Optional[datetime],
        interval_minutes: Optional[int],
        cron_expression: Optional[str],
        max_runs: Optional[int],
        next_run_at: datetime,
        working_directory: str,
        target_chat_id: Optional[int],
        created_by: int,
    ) -> TaskRecord:
        now = datetime.now(UTC)
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO scheduler_tasks (
                    task_name, prompt, schedule_type,
                    run_at, interval_minutes, cron_expression, max_runs,
                    runs_count, status, next_run_at,
                    working_directory, target_chat_id, created_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 'active', ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_name,
                    prompt,
                    schedule_type,
                    run_at,
                    interval_minutes,
                    cron_expression,
                    max_runs,
                    next_run_at,
                    working_directory,
                    target_chat_id,
                    created_by,
                    now,
                    now,
                ),
            )
            await conn.commit()
            task_id = cursor.lastrowid
            if task_id is None:
                raise RuntimeError("INSERT produced no task_id")
            return await self._get_by_id(conn, task_id)

    async def list(
        self, status_filter: Optional[str] = None
    ) -> List[TaskRecord]:
        query = (
            "SELECT * FROM scheduler_tasks"
            " WHERE status = ?"
            " ORDER BY next_run_at ASC"
        )
        async with self._connect() as conn:
            if status_filter in (None, "active"):
                cursor = await conn.execute(query, ("active",))
            elif status_filter == "all":
                cursor = await conn.execute(
                    "SELECT * FROM scheduler_tasks ORDER BY next_run_at ASC"
                )
            else:
                cursor = await conn.execute(query, (status_filter,))
            rows = await cursor.fetchall()
            return [TaskRecord.from_row(dict(r)) for r in rows]

    async def get(self, task_id: int) -> Optional[TaskRecord]:
        async with self._connect() as conn:
            cursor = await conn.execute(
                "SELECT * FROM scheduler_tasks WHERE task_id = ?",
                (task_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return None
            return TaskRecord.from_row(dict(row))

    async def delete(self, task_id: int) -> bool:
        """Mark a task as cancelled. Returns False if nothing updated."""
        now = datetime.now(UTC)
        async with self._connect() as conn:
            cursor = await conn.execute(
                """
                UPDATE scheduler_tasks
                SET status = 'cancelled', updated_at = ?
                WHERE task_id = ? AND status = 'active'
                """,
                (now, task_id),
            )
            await conn.commit()
            return (cursor.rowcount or 0) > 0

    async def update_fields(
        self,
        task_id: int,
        *,
        fields: Dict[str, Any],
    ) -> Optional[TaskRecord]:
        """Apply a partial update to a task row.

        ``fields`` maps column names to new values. Unknown columns are
        rejected loudly (caller bug). Returns the updated record, or
        ``None`` if the task does not exist.
        """
        allowed_columns = {
            "task_name",
            "prompt",
            "schedule_type",
            "run_at",
            "interval_minutes",
            "cron_expression",
            "max_runs",
            "next_run_at",
            "status",
            "working_directory",
            "target_chat_id",
        }
        bad = set(fields.keys()) - allowed_columns
        if bad:
            raise ValueError(f"cannot update unknown columns: {sorted(bad)}")

        if not fields:
            return await self.get(task_id)

        now = datetime.now(UTC)
        assignments = ", ".join(f"{col} = ?" for col in fields)
        values = list(fields.values()) + [now, task_id]

        async with self._connect() as conn:
            cursor = await conn.execute(
                f"""
                UPDATE scheduler_tasks
                SET {assignments}, updated_at = ?
                WHERE task_id = ?
                """,
                values,
            )
            await conn.commit()
            if cursor.rowcount == 0:
                return None
            return await self._get_by_id(conn, task_id)

    async def claim_due(self, *, now: datetime) -> List[TaskRecord]:
        """Atomically fetch tasks whose next_run_at is in the past.

        Uses a BEGIN IMMEDIATE transaction so a second worker cannot
        double-fire the same task. The returned rows still have
        ``status='active'`` — the caller marks them completed/rescheduled
        via ``mark_fired``.
        """
        async with self._connect() as conn:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = await conn.execute(
                    """
                    SELECT * FROM scheduler_tasks
                    WHERE status = 'active' AND next_run_at <= ?
                    ORDER BY next_run_at ASC
                    LIMIT 100
                    """,
                    (now,),
                )
                rows = await cursor.fetchall()
                records = [TaskRecord.from_row(dict(r)) for r in rows]
                # Reserve these tasks by pushing next_run_at far into the
                # future. The worker calls mark_fired() with the real new
                # value once it finishes firing. A second worker polling
                # concurrently will simply see no due rows.
                if records:
                    far_future = datetime.max.replace(tzinfo=UTC)
                    await conn.executemany(
                        """
                        UPDATE scheduler_tasks
                        SET next_run_at = ?, updated_at = ?
                        WHERE task_id = ? AND status = 'active'
                        """,
                        [(far_future, now, r.task_id) for r in records],
                    )
                await conn.commit()
                return records
            except Exception:
                await conn.rollback()
                raise

    async def mark_fired(
        self,
        task_id: int,
        *,
        fired_at: datetime,
        next_run_at: Optional[datetime],
        new_status: TaskStatus,
        runs_count: int,
        error: Optional[str] = None,
    ) -> None:
        """Finalise state after firing a claimed task."""
        # When the task is no longer active (completed / cancelled / failed)
        # keep next_run_at = fired_at so the row doesn't linger in the
        # "due" index at datetime.max; that cell is meaningless once
        # status != 'active'.
        effective_next = next_run_at or fired_at
        async with self._connect() as conn:
            await conn.execute(
                """
                UPDATE scheduler_tasks
                SET status = ?,
                    runs_count = ?,
                    last_run_at = ?,
                    last_error = ?,
                    next_run_at = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    new_status,
                    runs_count,
                    fired_at,
                    error,
                    effective_next,
                    fired_at,
                    task_id,
                ),
            )
            await conn.commit()

    async def _get_by_id(
        self, conn: aiosqlite.Connection, task_id: int
    ) -> TaskRecord:
        cursor = await conn.execute(
            "SELECT * FROM scheduler_tasks WHERE task_id = ?",
            (task_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(f"task {task_id} disappeared after insert")
        return TaskRecord.from_row(dict(row))
