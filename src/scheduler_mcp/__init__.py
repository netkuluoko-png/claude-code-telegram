"""Agent task scheduler exposed to Claude via MCP.

Provides three capabilities:

* ``schedule_task`` — agent schedules its own future invocations.
* ``list_tasks`` — agent inspects scheduled work.
* ``delete_task`` — agent cancels a schedule.

The MCP server is stateless; it persists tasks to SQLite. A companion
``SchedulerTaskWorker`` runs inside the bot process, polls the table and
fires due tasks by publishing a ``ScheduledEvent`` to the event bus —
which the existing ``AgentHandler`` turns into a ``ClaudeIntegration``
invocation (same entry point used by Telegram messages).
"""

from .models import ScheduleType, TaskRecord, TaskStatus, compute_next_run
from .storage import SchedulerTaskStore
from .worker import SchedulerTaskWorker

__all__ = [
    "ScheduleType",
    "TaskRecord",
    "TaskStatus",
    "compute_next_run",
    "SchedulerTaskStore",
    "SchedulerTaskWorker",
]
