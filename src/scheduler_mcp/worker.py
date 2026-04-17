"""In-process worker that fires scheduled tasks.

The worker polls ``scheduler_tasks`` every ``poll_interval_seconds`` for
rows whose ``next_run_at`` is in the past, claims them atomically, and
publishes a ``ScheduledEvent`` to the event bus. The existing
``AgentHandler.handle_scheduled`` turns each event into a
``ClaudeIntegration.run_command()`` call — exactly the same entry point
Telegram messages use.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import List, Optional

import structlog

from ..events.bus import EventBus
from ..events.types import ScheduledEvent
from ..storage.database import DatabaseManager
from .models import TaskRecord, compute_next_run
from .storage import SchedulerTaskStore

logger = structlog.get_logger()


class SchedulerTaskWorker:
    """Async polling worker firing due tasks through the event bus."""

    def __init__(
        self,
        *,
        db_manager: DatabaseManager,
        event_bus: EventBus,
        default_working_directory: Path,
        default_chat_ids: Optional[List[int]] = None,
        poll_interval_seconds: float = 10.0,
    ) -> None:
        self.event_bus = event_bus
        self.default_working_directory = default_working_directory
        self.default_chat_ids = list(default_chat_ids or [])
        self.poll_interval_seconds = poll_interval_seconds
        self.store = SchedulerTaskStore(db_manager.database_path)
        self._task: Optional[asyncio.Task[None]] = None
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        if self._task is not None:
            return
        await self.store.ensure_schema()
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._loop(), name="scheduler-task-worker"
        )
        logger.info(
            "Scheduler task worker started",
            poll_interval_seconds=self.poll_interval_seconds,
        )

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop_event.set()
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
        logger.info("Scheduler task worker stopped")

    async def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Scheduler task worker tick failed")
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        due = await self.store.claim_due(now=now)
        if not due:
            return
        logger.info("Firing scheduled tasks", count=len(due))
        for record in due:
            await self._fire(record, fired_at=now)

    async def _fire(self, record: TaskRecord, *, fired_at: datetime) -> None:
        chat_ids: List[int]
        if record.target_chat_id is not None:
            chat_ids = [record.target_chat_id]
        else:
            chat_ids = list(self.default_chat_ids)

        working_dir = Path(record.working_directory or self.default_working_directory)

        event = ScheduledEvent(
            job_id=str(record.task_id),
            job_name=record.task_name,
            prompt=record.prompt,
            working_directory=working_dir,
            target_chat_ids=chat_ids,
        )

        publish_error: Optional[str] = None
        try:
            await self.event_bus.publish(event)
            logger.info(
                "Scheduled task dispatched",
                task_id=record.task_id,
                task_name=record.task_name,
                schedule_type=record.schedule_type,
                event_id=event.id,
            )
        except Exception as exc:
            publish_error = f"{type(exc).__name__}: {exc}"
            logger.exception(
                "Failed to publish ScheduledEvent",
                task_id=record.task_id,
            )

        runs_count = record.runs_count + 1
        next_run = compute_next_run(record, fired_at=fired_at)

        new_status = record.status
        if publish_error is not None:
            new_status = "failed"
        elif record.schedule_type == "once":
            new_status = "completed"
        elif record.max_runs is not None and runs_count >= record.max_runs:
            new_status = "completed"
        elif next_run is None:
            new_status = "completed"
        else:
            new_status = "active"

        await self.store.mark_fired(
            record.task_id,
            fired_at=fired_at,
            next_run_at=next_run if new_status == "active" else None,
            new_status=new_status,
            runs_count=runs_count,
            error=publish_error,
        )
