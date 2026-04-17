"""Pydantic input schemas for mcp-scheduler tools.

These models are rendered into the JSON-Schema that Claude sees when it
decides which tool to call. Descriptions are written for the LLM: explain
not just *what* each field means, but *when* it is required and how the
three schedule types interact.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

ScheduleTypeLiteral = Literal["once", "interval", "cron"]
StatusFilterLiteral = Literal["active", "completed", "cancelled", "failed", "all"]


class ScheduleTaskInput(BaseModel):
    """Parameters for ``schedule_task``.

    The three schedule types are mutually exclusive — pick exactly one
    and fill in the matching field:

    * ``once``     — fill ``run_at`` with an ISO 8601 timestamp.
    * ``interval`` — fill ``interval_minutes`` with a positive integer.
    * ``cron``     — fill ``cron_expression`` with a 5-field cron string.
    """

    task_name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "Short human-readable name shown back to the user in listings "
            "and log output. Example: 'Morning inbox summary'."
        ),
    )
    prompt: str = Field(
        ...,
        min_length=1,
        description=(
            "The exact message that will be delivered to the agent when the "
            "task fires, as if the user had typed it in Telegram. Write it "
            "in the second person / imperative, include every bit of "
            "context the agent will need (files to read, criteria, desired "
            "output format), since the agent has no memory of this "
            "conversation when the task later fires."
        ),
    )
    schedule_type: ScheduleTypeLiteral = Field(
        ...,
        description=(
            "How the task repeats. 'once' fires exactly one time at "
            "`run_at`. 'interval' fires every `interval_minutes` minutes "
            "starting `interval_minutes` from now. 'cron' fires on a cron "
            "schedule given by `cron_expression` (UTC)."
        ),
    )
    run_at: Optional[str] = Field(
        default=None,
        description=(
            "ISO 8601 UTC timestamp (e.g. '2026-04-18T09:30:00Z' or "
            "'2026-04-18T09:30:00+00:00'). REQUIRED when "
            "schedule_type='once', ignored otherwise. Must be in the "
            "future."
        ),
    )
    interval_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Repeat period in minutes. REQUIRED when "
            "schedule_type='interval', ignored otherwise. Minimum 1."
        ),
    )
    cron_expression: Optional[str] = Field(
        default=None,
        description=(
            "Standard 5-field cron expression interpreted in UTC: "
            "'minute hour day-of-month month day-of-week'. Example: "
            "'0 9 * * 1-5' = every weekday at 09:00 UTC. REQUIRED when "
            "schedule_type='cron', ignored otherwise."
        ),
    )
    max_runs: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Optional cap on total firings. When `runs_count` reaches "
            "`max_runs` the task is marked completed. Leave unset for an "
            "unlimited schedule. Ignored for schedule_type='once' (which "
            "always runs exactly once)."
        ),
    )
    working_directory: Optional[str] = Field(
        default=None,
        description=(
            "Absolute path the agent should run in when the task fires. "
            "Defaults to the bot's APPROVED_DIRECTORY if omitted. Pick the "
            "same directory you are currently working in if the task "
            "operates on this project."
        ),
    )
    target_chat_id: Optional[int] = Field(
        default=None,
        description=(
            "Telegram chat id to deliver the agent's response to when the "
            "task fires. Defaults to the bot's configured "
            "NOTIFICATION_CHAT_IDS (all of them) if omitted. Pass the "
            "current user's chat id to send the reply only to them."
        ),
    )

    @model_validator(mode="after")
    def _validate_schedule_fields(self) -> "ScheduleTaskInput":
        if self.schedule_type == "once":
            if not self.run_at:
                raise ValueError(
                    "run_at is required when schedule_type='once'"
                )
        elif self.schedule_type == "interval":
            if self.interval_minutes is None:
                raise ValueError(
                    "interval_minutes is required when "
                    "schedule_type='interval'"
                )
        elif self.schedule_type == "cron":
            if not self.cron_expression:
                raise ValueError(
                    "cron_expression is required when schedule_type='cron'"
                )
        return self


class ListTasksInput(BaseModel):
    """Parameters for ``list_tasks``."""

    status_filter: Optional[StatusFilterLiteral] = Field(
        default=None,
        description=(
            "Filter by lifecycle status. Omit or pass 'active' to see "
            "still-running schedules. Pass 'completed', 'cancelled' or "
            "'failed' to inspect history. Pass 'all' to see everything."
        ),
    )


class DeleteTaskInput(BaseModel):
    """Parameters for ``delete_task``."""

    task_id: int = Field(
        ...,
        ge=1,
        description=(
            "Numeric id of the task to cancel, as returned by "
            "schedule_task or list_tasks."
        ),
    )
