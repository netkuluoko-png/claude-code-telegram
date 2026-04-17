"""Pydantic input schemas for mcp-scheduler tools.

These models are rendered into the JSON-Schema that Claude sees when it
decides which tool to call. Descriptions are written for the LLM: explain
not just *what* each field means, but *when* it is required and how the
three schedule types interact.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

ScheduleTypeLiteral = Literal["once", "interval", "cron", "random_daily"]
StatusFilterLiteral = Literal["active", "completed", "cancelled", "failed", "all"]


class ScheduleTaskInput(BaseModel):
    """Parameters for ``schedule_task``.

    Four schedule types are mutually exclusive — pick exactly one and
    fill in the matching fields:

    * ``once``         — fill ``run_at`` with an ISO 8601 timestamp.
    * ``interval``     — fill ``interval_minutes`` with a positive integer.
    * ``cron``         — fill ``cron_expression`` with a 5-field cron
                         string (UTC).
    * ``random_daily`` — fire once per day at a uniformly random moment
                         inside ``[window_start, window_end]`` in the
                         given ``timezone``; optionally skip whole days
                         with probability ``skip_probability``. Used to
                         break up predictable patterns.
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
    window_start: Optional[str] = Field(
        default=None,
        description=(
            "Daily window start as 'HH:MM', 'HH:MM:SS' or "
            "'HH:MM:SS.fff' in `timezone`. Example: '12:00'. REQUIRED "
            "when schedule_type='random_daily', ignored otherwise."
        ),
    )
    window_end: Optional[str] = Field(
        default=None,
        description=(
            "Daily window end — same format as window_start. Must be "
            "strictly greater than window_start. '24:00' is accepted as "
            "end-of-day. REQUIRED when schedule_type='random_daily', "
            "ignored otherwise."
        ),
    )
    skip_probability: Optional[float] = Field(
        default=None,
        ge=0.0,
        lt=1.0,
        description=(
            "For schedule_type='random_daily': probability in [0, 1) of "
            "silently skipping an entire day. 0.0 = fire every day, "
            "0.2 ≈ skip ~1 day per 5 (~1-2 days per week), 0.5 = skip "
            "half the days. Default 0.0. Used to break up predictable "
            "patterns so heuristics don't flag the activity."
        ),
    )
    timezone: Optional[str] = Field(
        default=None,
        description=(
            "IANA timezone in which window_start / window_end are "
            "interpreted (e.g. 'Europe/Kyiv', 'UTC'). Default "
            "'Europe/Kyiv'. Ignored unless schedule_type='random_daily'."
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
            "Must be the APPROVED_DIRECTORY base itself or one of its "
            "immediate subdirectories — the same set of repos visible "
            "via the Telegram /repo command. Call list_repos to discover "
            "valid choices. Defaults to APPROVED_DIRECTORY (base) when "
            "omitted."
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
        elif self.schedule_type == "random_daily":
            if not self.window_start or not self.window_end:
                raise ValueError(
                    "window_start and window_end are required when "
                    "schedule_type='random_daily'"
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


class UpdateTaskInput(BaseModel):
    """Parameters for ``update_task``.

    Only the fields you pass are changed; everything else stays as it
    was. If you touch any schedule-shape field (``schedule_type``,
    ``run_at``, ``interval_minutes``, ``cron_expression``) the next-run
    time is recomputed.

    When changing ``schedule_type`` you must also supply the matching
    field for the new type (same rule as ``schedule_task``).
    """

    task_id: int = Field(
        ...,
        ge=1,
        description="Numeric id of the task to edit (see list_tasks).",
    )
    task_name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=200,
        description="New human-readable name. Omit to keep current.",
    )
    prompt: Optional[str] = Field(
        default=None,
        min_length=1,
        description=(
            "Replacement prompt delivered to the agent when the task "
            "fires. Remember it must be fully self-contained. Omit to "
            "keep the current prompt."
        ),
    )
    schedule_type: Optional[ScheduleTypeLiteral] = Field(
        default=None,
        description=(
            "Switch schedule mode. When set, supply the matching "
            "field (run_at / interval_minutes / cron_expression)."
        ),
    )
    run_at: Optional[str] = Field(
        default=None,
        description=(
            "New ISO 8601 UTC timestamp. Used when schedule_type='once' "
            "(either already or being switched to). Must be in the future."
        ),
    )
    interval_minutes: Optional[int] = Field(
        default=None,
        ge=1,
        description="New interval in minutes. Used when schedule_type='interval'.",
    )
    cron_expression: Optional[str] = Field(
        default=None,
        description="New cron expression (UTC). Used when schedule_type='cron'.",
    )
    window_start: Optional[str] = Field(
        default=None,
        description=(
            "New daily window start ('HH:MM[:SS[.fff]]'). Used when "
            "schedule_type='random_daily'."
        ),
    )
    window_end: Optional[str] = Field(
        default=None,
        description=(
            "New daily window end ('HH:MM[:SS[.fff]]'). Used when "
            "schedule_type='random_daily'."
        ),
    )
    skip_probability: Optional[float] = Field(
        default=None,
        ge=0.0,
        lt=1.0,
        description=(
            "New daily-skip probability in [0, 1). Used when "
            "schedule_type='random_daily'. Pass 0.0 to fire every day."
        ),
    )
    timezone: Optional[str] = Field(
        default=None,
        description=(
            "New IANA timezone for the random-daily window (e.g. "
            "'Europe/Kyiv'). Pass an empty string to restore the default."
        ),
    )
    max_runs: Optional[int] = Field(
        default=None,
        ge=0,
        description=(
            "New firing cap. Pass 0 to clear the cap (unlimited). Omit "
            "to keep current."
        ),
    )
    working_directory: Optional[str] = Field(
        default=None,
        description=(
            "New working directory. Must be the APPROVED_DIRECTORY base "
            "itself or one of its immediate subdirectories — the same "
            "set visible via the Telegram /repo command. Omit to keep "
            "current."
        ),
    )
    target_chat_id: Optional[int] = Field(
        default=None,
        description=(
            "New Telegram chat id for delivering the agent's reply. "
            "Pass 0 to clear (fall back to NOTIFICATION_CHAT_IDS). Omit "
            "to keep current."
        ),
    )
    reactivate: Optional[bool] = Field(
        default=None,
        description=(
            "If True, revive a cancelled/completed/failed task back to "
            "'active' status. Ignored if the task is already active."
        ),
    )

    @model_validator(mode="after")
    def _validate_schedule_fields(self) -> "UpdateTaskInput":
        if self.schedule_type is None:
            return self
        if self.schedule_type == "once" and not self.run_at:
            raise ValueError(
                "run_at is required when changing schedule_type to 'once'"
            )
        if self.schedule_type == "interval" and self.interval_minutes is None:
            raise ValueError(
                "interval_minutes is required when changing schedule_type to 'interval'"
            )
        if self.schedule_type == "cron" and not self.cron_expression:
            raise ValueError(
                "cron_expression is required when changing schedule_type to 'cron'"
            )
        if self.schedule_type == "random_daily" and (
            not self.window_start or not self.window_end
        ):
            raise ValueError(
                "window_start and window_end are required when changing "
                "schedule_type to 'random_daily'"
            )
        return self
