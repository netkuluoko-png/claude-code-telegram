"""Claude Code Python SDK integration."""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ClaudeSDKError,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    Message,
    PermissionResultAllow,
    PermissionResultDeny,
    ProcessError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk._errors import MessageParseError
from claude_agent_sdk._internal.message_parser import parse_message
from claude_agent_sdk.types import StreamEvent

from ..config.settings import Settings
from ..security.validators import SecurityValidator
from .exceptions import (
    ClaudeMCPError,
    ClaudeParsingError,
    ClaudeProcessError,
    ClaudeTimeoutError,
)
from .monitor import _is_claude_internal_path, check_bash_directory_boundary

logger = structlog.get_logger()

# Fallback message when Claude produces no text but did use tools.
TASK_COMPLETED_MSG = "✅ Task completed. Tools used: {tools_summary}"

_USER_TIMEZONE = os.environ.get("USER_TIMEZONE", "Europe/Kyiv")


def _allowed_repos_prompt_fragment(approved_directory: Path) -> str:
    """List APPROVED_DIRECTORY + immediate subdirs for the agent.

    Mirrors the Telegram ``/repo`` command output so the agent always
    sees the exact same set of choices the user does. Used to inform
    mcp-scheduler's ``working_directory`` choice (among other things).
    """
    base = approved_directory.resolve()
    lines = [f"base: {base}"]
    try:
        for d in sorted(base.iterdir(), key=lambda p: p.name):
            if d.is_dir() and not d.name.startswith("."):
                is_git = (d / ".git").is_dir()
                marker = " (git)" if is_git else ""
                lines.append(f"- {d.name} -> {d.resolve()}{marker}")
    except (OSError, FileNotFoundError):
        pass
    if len(lines) == 1:
        lines.append("(no subdirectories yet)")
    body = "\n".join(lines)
    return (
        "<available-repos>\n"
        "Working directories allowed for scheduled tasks (same set as the "
        "Telegram /repo command). Pass one of the absolute paths below as "
        "`working_directory` to mcp-scheduler.schedule_task / update_task.\n"
        f"{body}\n"
        "</available-repos>"
    )


def _current_time_prompt_fragment() -> str:
    """Build a fresh 'current time' snippet for the system prompt.

    Rebuilt on every Claude invocation so the agent always sees the
    actual current time (useful for scheduling decisions via mcp-scheduler).
    Reports both UTC and the user's local timezone (default Europe/Kyiv).
    """
    now_utc = datetime.now(UTC)
    try:
        local_tz = ZoneInfo(_USER_TIMEZONE)
        now_local = now_utc.astimezone(local_tz)
        local_line = (
            f"Local time ({_USER_TIMEZONE}): "
            f"{now_local.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"
        )
    except ZoneInfoNotFoundError:
        local_line = (
            f"Local time: unavailable (timezone '{_USER_TIMEZONE}' not installed)"
        )
    return (
        "<current-time>\n"
        f"UTC: {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
        f"{local_line}\n"
        f"UTC ISO 8601: {now_utc.isoformat()}\n"
        "</current-time>"
    )


@dataclass
class ClaudeResponse:
    """Response from Claude Code SDK."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False


@dataclass
class StreamUpdate:
    """Streaming update from Claude SDK."""

    type: str  # 'assistant', 'user', 'system', 'result', 'stream_delta'
    content: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    metadata: Optional[Dict[str, Any]] = None
    progress: Optional[Dict[str, Any]] = None

    def get_tool_names(self) -> List[str]:
        """Return tool names from the stream payload."""
        names: List[str] = []

        if self.tool_calls:
            for tool_call in self.tool_calls:
                name = tool_call.get("name") if isinstance(tool_call, dict) else None
                if isinstance(name, str) and name:
                    names.append(name)

        if self.metadata:
            tool_name = self.metadata.get("tool_name")
            if isinstance(tool_name, str) and tool_name:
                names.append(tool_name)

            metadata_tools = self.metadata.get("tools")
            if isinstance(metadata_tools, list):
                for tool in metadata_tools:
                    if isinstance(tool, dict):
                        name = tool.get("name")
                    elif isinstance(tool, str):
                        name = tool
                    else:
                        name = None

                    if isinstance(name, str) and name:
                        names.append(name)

        # Preserve insertion order while de-duplicating.
        return list(dict.fromkeys(names))

    def is_error(self) -> bool:
        """Check whether this stream update represents an error."""
        if self.type == "error":
            return True

        if self.metadata:
            if self.metadata.get("is_error") is True:
                return True
            status = self.metadata.get("status")
            if isinstance(status, str) and status.lower() == "error":
                return True
            error_val = self.metadata.get("error")
            if isinstance(error_val, str) and error_val:
                return True
            error_msg_val = self.metadata.get("error_message")
            if isinstance(error_msg_val, str) and error_msg_val:
                return True

        if self.progress:
            status = self.progress.get("status")
            if isinstance(status, str) and status.lower() == "error":
                return True

        return False

    def get_error_message(self) -> str:
        """Get the best available error message from the stream payload."""
        if self.metadata:
            for key in ("error_message", "error", "message"):
                value = self.metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value

        if isinstance(self.content, str) and self.content.strip():
            return self.content

        if self.progress:
            value = self.progress.get("error")
            if isinstance(value, str) and value.strip():
                return value

        return "Unknown error"

    def get_progress_percentage(self) -> Optional[int]:
        """Extract progress percentage if present."""

        def _to_int(value: Any) -> Optional[int]:
            if isinstance(value, (int, float)):
                return int(value)
            if isinstance(value, str) and value.strip():
                try:
                    return int(float(value))
                except ValueError:
                    return None
            return None

        if self.progress:
            for key in ("percentage", "percent", "progress"):
                percentage = _to_int(self.progress.get(key))
                if percentage is not None:
                    return max(0, min(100, percentage))

            step = _to_int(self.progress.get("step"))
            total_steps = _to_int(self.progress.get("total_steps"))
            if step is not None and total_steps and total_steps > 0:
                return max(0, min(100, int((step / total_steps) * 100)))

        if self.metadata:
            percentage = _to_int(self.metadata.get("progress_percentage"))
            if percentage is not None:
                return max(0, min(100, percentage))

        return None


def _make_can_use_tool_callback(
    security_validator: SecurityValidator,
    working_directory: Path,
    approved_directory: Path,
) -> Any:
    """Create a can_use_tool callback for SDK-level tool permission validation.

    The callback validates file path boundaries and bash directory boundaries
    *before* the SDK executes the tool, providing preventive security enforcement.
    """
    _FILE_TOOLS = {"Write", "Edit", "Read", "create_file", "edit_file", "read_file"}
    _BASH_TOOLS = {"Bash", "bash", "shell"}

    async def can_use_tool(
        tool_name: str,
        tool_input: Dict[str, Any],
        context: ToolPermissionContext,
    ) -> Any:
        # File path validation
        if tool_name in _FILE_TOOLS:
            file_path = tool_input.get("file_path") or tool_input.get("path")
            if file_path:
                # Allow Claude Code internal paths (~/.claude/plans/, etc.)
                if _is_claude_internal_path(file_path):
                    return PermissionResultAllow()

                valid, _resolved, error = security_validator.validate_path(
                    file_path, working_directory
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied file operation",
                        tool_name=tool_name,
                        file_path=file_path,
                        error=error,
                    )
                    return PermissionResultDeny(message=error or "Invalid file path")

        # Bash directory boundary validation
        if tool_name in _BASH_TOOLS:
            command = tool_input.get("command", "")
            if command:
                valid, error = check_bash_directory_boundary(
                    command, working_directory, approved_directory
                )
                if not valid:
                    logger.warning(
                        "can_use_tool denied bash command",
                        tool_name=tool_name,
                        command=command,
                        error=error,
                    )
                    return PermissionResultDeny(
                        message=error or "Bash directory boundary violation"
                    )

        return PermissionResultAllow()

    return can_use_tool


class ClaudeSDKManager:
    """Manage Claude Code SDK integration."""

    def __init__(
        self,
        config: Settings,
        security_validator: Optional[SecurityValidator] = None,
    ):
        """Initialize SDK manager with configuration."""
        self.config = config
        self.security_validator = security_validator

        # Set up environment for Claude Code SDK if API key is provided
        # If no API key is provided, the SDK will use existing CLI authentication
        if config.anthropic_api_key_str:
            os.environ["ANTHROPIC_API_KEY"] = config.anthropic_api_key_str
            logger.info("Using provided API key for Claude SDK authentication")
        else:
            logger.info("No API key provided, using existing Claude CLI authentication")

    def _is_retryable_error(self, exc: BaseException) -> bool:
        """Return True for transient errors that warrant a retry.
        asyncio.TimeoutError is intentional (user-configured timeout) — not retried.
        Only non-MCP CLIConnectionError is considered transient.
        """
        if isinstance(exc, CLIConnectionError):
            msg = str(exc).lower()
            return "mcp" not in msg  # "server" alone is too broad
        return False

    async def execute_command(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        interrupt_event: Optional[asyncio.Event] = None,
        images: Optional[List[Dict[str, str]]] = None,
        model_override: Optional[str] = None,
        user_id: int = 0,
        effort_override: Optional[str] = None,
    ) -> ClaudeResponse:
        """Execute Claude Code command via SDK."""
        start_time = asyncio.get_event_loop().time()

        logger.info(
            "Starting Claude SDK command",
            working_directory=str(working_directory),
            session_id=session_id,
            continue_session=continue_session,
        )

        try:
            approved_directory = self.config.approved_directory_for_user(user_id)
            sandbox_excluded_commands = (
                []
                if self.config.is_isolated_user(user_id)
                else self.config.sandbox_excluded_commands or []
            )

            # Capture stderr from Claude CLI for better error diagnostics
            stderr_lines: List[str] = []

            def _stderr_callback(line: str) -> None:
                stderr_lines.append(line)
                logger.debug("Claude CLI stderr", line=line)

            # Build system prompt, loading CLAUDE.md from working directory if present
            base_prompt = (
                f"All file operations must stay within {working_directory}. "
                "Use relative paths."
            )
            base_prompt += "\n\n" + _current_time_prompt_fragment()
            base_prompt += "\n\n" + _allowed_repos_prompt_fragment(approved_directory)
            claude_md_path = Path(working_directory) / "CLAUDE.md"
            if claude_md_path.exists():
                base_prompt += "\n\n" + claude_md_path.read_text(encoding="utf-8")
                logger.info(
                    "Loaded CLAUDE.md into system prompt",
                    path=str(claude_md_path),
                )

            # When DISABLE_TOOL_VALIDATION=true, pass None for allowed/disallowed
            # tools so the SDK does not restrict tool usage (e.g. MCP tools).
            if self.config.disable_tool_validation:
                sdk_allowed_tools = None
                sdk_disallowed_tools = None
            else:
                sdk_allowed_tools = self.config.claude_allowed_tools
                sdk_disallowed_tools = self.config.claude_disallowed_tools

            # Build Claude Agent options
            options = ClaudeAgentOptions(
                max_turns=self.config.claude_max_turns,
                model=model_override or self.config.claude_model or None,
                max_budget_usd=self.config.claude_max_cost_per_request,
                effort=effort_override or self.config.claude_effort,
                cwd=str(working_directory),
                allowed_tools=sdk_allowed_tools,
                disallowed_tools=sdk_disallowed_tools,
                cli_path=self.config.claude_cli_path or None,
                include_partial_messages=stream_callback is not None,
                sandbox={
                    "enabled": self.config.sandbox_enabled,
                    "autoAllowBashIfSandboxed": True,
                    "excludedCommands": sandbox_excluded_commands,
                },
                system_prompt=base_prompt,
                setting_sources=["project", "user"],
                # Force the CLI to honour only the MCP servers we pass via
                # --mcp-config, ignoring the project's .mcp.json entirely.
                # Without this flag the CLI still tries to load project MCPs
                # (e.g. broken Windows paths) and enters a "launched but not
                # connected" state that also shadows our bot MCPs.
                extra_args={"strict-mcp-config": None, "debug-to-stderr": None},
                stderr=_stderr_callback,
            )

            bot_mcp_servers, project_mcp_servers = self._build_mcp_servers(
                working_directory
            )
            # Install each project MCP's Python deps (if requirements.txt is
            # present next to its script) so the stdio subprocess can import
            # them. Keyed by hash of requirements.txt → persistent cache on
            # the data volume, re-installed only when the file changes.
            for name, cfg in project_mcp_servers.items():
                await self._ensure_mcp_deps(name, cfg)

            mcp_servers: Dict[str, Any] = {}
            mcp_servers.update(project_mcp_servers)
            mcp_servers.update(bot_mcp_servers)

            # Inject the active user's id into mcp-scheduler so it can default
            # target_chat_id to the caller's private chat (chat_id == user_id
            # for private Telegram chats). Without this, tasks scheduled by
            # the agent arrive with target_chat_id=NULL and — if
            # NOTIFICATION_CHAT_IDS is unset — the reply gets silently dropped.
            if user_id and "mcp-scheduler" in mcp_servers:
                sched_cfg = mcp_servers["mcp-scheduler"]
                sched_env = sched_cfg.setdefault("env", {})
                sched_env.setdefault("SCHEDULER_DEFAULT_USER_ID", str(user_id))
                sched_env.setdefault("SCHEDULER_DEFAULT_CHAT_ID", str(user_id))
                sched_env.setdefault("APPROVED_DIRECTORY", str(approved_directory))

            if user_id and self.config.is_isolated_user(user_id):
                if "process-manager" in mcp_servers:
                    proc_cfg = mcp_servers["process-manager"]
                    proc_env = proc_cfg.setdefault("env", {})
                    proc_env["PROCESS_NAMESPACE"] = f"user-{user_id}"
                    proc_env["PROCESS_APPROVED_DIRECTORY"] = str(approved_directory)

            if mcp_servers:
                options.mcp_servers = mcp_servers
                logger.info(
                    "MCP servers configured",
                    project_servers=list(project_mcp_servers.keys()),
                    bot_servers=list(bot_mcp_servers.keys()),
                )

            # Ensure .claude/settings.json and .claude/rules/ exist in working_directory
            # so the CLI picks up MCP tools regardless of which project is active
            self._ensure_mcp_settings(working_directory, mcp_servers)
            self._ensure_mcp_rules(working_directory)
            self._ensure_language_rules(working_directory)
            # Pre-approve project MCP servers in the user-scope state file so the
            # CLI attaches them automatically (avoids "launched but not connected"
            # limbo when a project .mcp.json exists).
            self._approve_project_mcps(working_directory, mcp_servers)

            # Wire can_use_tool callback for preventive tool validation
            if self.security_validator:
                options.can_use_tool = _make_can_use_tool_callback(
                    security_validator=self.security_validator,
                    working_directory=working_directory,
                    approved_directory=approved_directory,
                )

            # Resume previous session if we have a session_id
            if session_id and continue_session:
                options.resume = session_id
                logger.info(
                    "Resuming previous session",
                    session_id=session_id,
                )

            # Collect messages via ClaudeSDKClient
            messages: List[Message] = []
            interrupted = False

            async def _run_client() -> None:
                client = ClaudeSDKClient(options)
                try:
                    await client.connect()

                    if images:
                        content_blocks: List[Dict[str, Any]] = []
                        for img in images:
                            media_type = img.get("media_type", "image/png")
                            content_blocks.append(
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": img["data"],
                                    },
                                }
                            )
                        content_blocks.append({"type": "text", "text": prompt})

                        multimodal_msg = {
                            "type": "user",
                            "message": {
                                "role": "user",
                                "content": content_blocks,
                            },
                        }

                        async def _multimodal_prompt() -> AsyncIterator[Dict[str, Any]]:
                            yield multimodal_msg

                        await client.query(_multimodal_prompt())
                    else:
                        await client.query(prompt)

                    async for raw_data in client._query.receive_messages():
                        try:
                            message = parse_message(raw_data)
                        except MessageParseError as e:
                            logger.debug(
                                "Skipping unparseable message",
                                error=str(e),
                            )
                            continue

                        messages.append(message)

                        if isinstance(message, ResultMessage):
                            break

                        # Handle streaming callback
                        if stream_callback:
                            try:
                                await self._handle_stream_message(
                                    message, stream_callback
                                )
                            except Exception as callback_error:
                                logger.warning(
                                    "Stream callback failed",
                                    error=str(callback_error),
                                    error_type=type(callback_error).__name__,
                                )
                finally:
                    await client.disconnect()

            # Execute with timeout and retry, racing against optional interrupt
            max_attempts = max(1, self.config.claude_retry_max_attempts)
            last_exc: Optional[BaseException] = None

            for attempt in range(max_attempts):
                # Reset message accumulator each attempt so that a failed attempt
                # does not pollute the next one with partial/duplicate messages.
                # _run_client() closes over `messages` by reference (late-binding
                # closure), so clearing it here is seen by every new call.
                messages.clear()

                if attempt > 0:
                    delay = min(
                        self.config.claude_retry_base_delay
                        * (self.config.claude_retry_backoff_factor ** (attempt - 1)),
                        self.config.claude_retry_max_delay,
                    )
                    logger.warning(
                        "Retrying Claude SDK command",
                        attempt=attempt + 1,
                        max_attempts=max_attempts,
                        delay_seconds=delay,
                    )
                    await asyncio.sleep(delay)

                run_task = asyncio.create_task(_run_client())

                interrupt_watcher: Optional["asyncio.Task[None]"] = None
                if interrupt_event is not None:

                    async def _cancel_on_interrupt() -> None:
                        nonlocal interrupted
                        await interrupt_event.wait()
                        interrupted = True
                        run_task.cancel()

                    interrupt_watcher = asyncio.create_task(_cancel_on_interrupt())

                # Note: asyncio.TimeoutError is intentionally NOT retried —
                # it reflects a user-configured hard limit.
                try:
                    await asyncio.wait_for(
                        asyncio.shield(run_task),
                        timeout=self.config.claude_timeout_seconds,
                    )
                    break  # success — exit retry loop
                except asyncio.CancelledError:
                    if not interrupted:
                        raise
                    # Interrupt cancelled the task — wait for cleanup
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    break  # user interrupted — don't retry
                except asyncio.TimeoutError:
                    run_task.cancel()
                    try:
                        await run_task
                    except asyncio.CancelledError:
                        pass
                    raise  # timeout — don't retry
                except CLIConnectionError as exc:
                    if self._is_retryable_error(exc) and attempt < max_attempts - 1:
                        last_exc = exc
                        logger.warning(
                            "Transient connection error, will retry",
                            attempt=attempt + 1,
                            error=str(exc),
                        )
                        continue
                    raise  # non-retryable or attempts exhausted
                finally:
                    if interrupt_watcher is not None:
                        interrupt_watcher.cancel()
            else:
                if last_exc is not None:
                    raise last_exc

            # Extract cost, tools, and session_id from result message
            cost = 0.0
            tools_used: List[Dict[str, Any]] = []
            claude_session_id = None
            result_content = None
            for message in messages:
                if isinstance(message, ResultMessage):
                    cost = getattr(message, "total_cost_usd", 0.0) or 0.0
                    claude_session_id = getattr(message, "session_id", None)
                    result_content = getattr(message, "result", None)
                    current_time = asyncio.get_event_loop().time()
                    for msg in messages:
                        if isinstance(msg, AssistantMessage):
                            msg_content = getattr(msg, "content", [])
                            if msg_content and isinstance(msg_content, list):
                                for block in msg_content:
                                    if isinstance(block, ToolUseBlock):
                                        tools_used.append(
                                            {
                                                "name": getattr(
                                                    block, "name", "unknown"
                                                ),
                                                "timestamp": current_time,
                                                "input": getattr(block, "input", {}),
                                            }
                                        )
                    break

            # Fallback: extract session_id from StreamEvent messages if
            # ResultMessage didn't provide one (can happen with some CLI versions)
            if not claude_session_id:
                for message in messages:
                    msg_session_id = getattr(message, "session_id", None)
                    if msg_session_id and not isinstance(message, ResultMessage):
                        claude_session_id = msg_session_id
                        logger.info(
                            "Got session ID from stream event (fallback)",
                            session_id=claude_session_id,
                        )
                        break

            # Calculate duration
            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)

            # Use Claude's session_id if available, otherwise fall back
            final_session_id = claude_session_id or session_id or ""

            if claude_session_id and claude_session_id != session_id:
                logger.info(
                    "Got session ID from Claude",
                    claude_session_id=claude_session_id,
                    previous_session_id=session_id,
                )

            # Use ResultMessage.result if available, fall back to the LAST
            # assistant message's text. Concatenating ALL assistant messages
            # mixes intermediate preamble ("I'll start by reading…") with the
            # final answer and produces confusing replies on long tasks.
            if result_content is not None:
                content = str(result_content).strip()
            else:
                content = ""
                for msg in reversed(messages):
                    if not isinstance(msg, AssistantMessage):
                        continue
                    msg_content = getattr(msg, "content", [])
                    text_parts: List[str] = []
                    if msg_content and isinstance(msg_content, list):
                        for block in msg_content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                    elif msg_content:
                        text_parts.append(str(msg_content))
                    candidate = "\n".join(text_parts).strip()
                    if candidate:
                        content = candidate
                        break

            if not content and tools_used:
                tool_names = [
                    tool.get("name", "")
                    for tool in tools_used
                    if isinstance(tool.get("name"), str) and tool.get("name")
                ]
                unique_tool_names = list(dict.fromkeys(tool_names))
                tools_summary = ", ".join(unique_tool_names) or "unknown"
                content = TASK_COMPLETED_MSG.format(tools_summary=tools_summary)

            return ClaudeResponse(
                content=content,
                session_id=final_session_id,
                cost=cost,
                duration_ms=duration_ms,
                num_turns=len(
                    [
                        m
                        for m in messages
                        if isinstance(m, (UserMessage, AssistantMessage))
                    ]
                ),
                tools_used=tools_used,
                interrupted=interrupted,
            )

        except asyncio.TimeoutError:
            logger.error(
                "Claude SDK command timed out",
                timeout_seconds=self.config.claude_timeout_seconds,
            )
            raise ClaudeTimeoutError(
                f"Claude SDK timed out after {self.config.claude_timeout_seconds}s"
            )

        except CLINotFoundError as e:
            logger.error("Claude CLI not found", error=str(e))
            error_msg = (
                "Claude Code not found. Please ensure Claude is installed:\n"
                "  npm install -g @anthropic-ai/claude-code\n\n"
                "If already installed, try one of these:\n"
                "  1. Add Claude to your PATH\n"
                "  2. Create a symlink: ln -s $(which claude) /usr/local/bin/claude\n"
                "  3. Set CLAUDE_CLI_PATH environment variable"
            )
            raise ClaudeProcessError(error_msg)

        except ProcessError as e:
            error_str = str(e)
            # Include captured stderr for better diagnostics
            captured_stderr = "\n".join(stderr_lines[-20:]) if stderr_lines else ""
            if captured_stderr:
                error_str = f"{error_str}\nStderr: {captured_stderr}"
            logger.error(
                "Claude process failed",
                error=error_str,
                exit_code=getattr(e, "exit_code", None),
                stderr=captured_stderr or None,
            )
            # Check if the process error is MCP-related
            if "mcp" in error_str.lower():
                raise ClaudeMCPError(f"MCP server error: {error_str}")
            raise ClaudeProcessError(f"Claude process error: {error_str}")

        except CLIConnectionError as e:
            error_str = str(e)
            logger.error("Claude connection error", error=error_str)
            # Check if the connection error is MCP-related
            if "mcp" in error_str.lower() or "server" in error_str.lower():
                raise ClaudeMCPError(f"MCP server connection failed: {error_str}")
            raise ClaudeProcessError(f"Failed to connect to Claude: {error_str}")

        except CLIJSONDecodeError as e:
            logger.error("Claude SDK JSON decode error", error=str(e))
            raise ClaudeParsingError(f"Failed to decode Claude response: {str(e)}")

        except ClaudeSDKError as e:
            logger.error("Claude SDK error", error=str(e))
            raise ClaudeProcessError(f"Claude SDK error: {str(e)}")

        except Exception as e:
            exceptions = getattr(e, "exceptions", None)
            if exceptions is not None:
                # ExceptionGroup from TaskGroup operations (Python 3.11+)
                logger.error(
                    "Task group error in Claude SDK",
                    error=str(e),
                    error_type=type(e).__name__,
                    exception_count=len(exceptions),
                    exceptions=[str(ex) for ex in exceptions[:3]],
                )
                raise ClaudeProcessError(
                    f"Claude SDK task error: {exceptions[0] if exceptions else e}"
                )

            logger.error(
                "Unexpected error in Claude SDK",
                error=str(e),
                error_type=type(e).__name__,
            )
            raise ClaudeProcessError(f"Unexpected error: {str(e)}")

    async def _handle_stream_message(
        self, message: Message, stream_callback: Callable[[StreamUpdate], None]
    ) -> None:
        """Handle streaming message from claude-agent-sdk."""
        try:
            if isinstance(message, AssistantMessage):
                # Extract content from assistant message
                content = getattr(message, "content", [])
                text_parts = []
                tool_calls = []

                if content and isinstance(content, list):
                    for block in content:
                        if isinstance(block, ToolUseBlock):
                            tool_calls.append(
                                {
                                    "name": block.name,
                                    "input": block.input,
                                    "id": block.id,
                                }
                            )
                        elif isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ThinkingBlock):
                            text_parts.append(block.thinking)

                if text_parts or tool_calls:
                    update = StreamUpdate(
                        type="assistant",
                        content=("\n".join(text_parts) if text_parts else None),
                        tool_calls=tool_calls if tool_calls else None,
                    )
                    await stream_callback(update)
                elif content:
                    # Fallback for non-list content
                    update = StreamUpdate(
                        type="assistant",
                        content=str(content),
                    )
                    await stream_callback(update)

            elif isinstance(message, StreamEvent):
                event = message.event or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        if text:
                            update = StreamUpdate(
                                type="stream_delta",
                                content=text,
                            )
                            await stream_callback(update)

            elif isinstance(message, UserMessage):
                content = getattr(message, "content", "")
                if content:
                    update = StreamUpdate(
                        type="user",
                        content=content,
                    )
                    await stream_callback(update)

        except Exception as e:
            logger.warning("Stream callback failed", error=str(e))

    @staticmethod
    def _ensure_mcp_settings(
        working_directory: Path, mcp_servers: Dict[str, Any]
    ) -> None:
        """Ensure .claude/settings.json with MCP config exists in working_directory.

        The CLI reads MCP servers from project-level .claude/settings.json.
        This creates or updates the file so MCP tools are available in any
        project directory, not just those pre-configured by the entrypoint.
        """
        import json

        if not mcp_servers:
            return

        settings_dir = Path(working_directory) / ".claude"
        settings_path = settings_dir / "settings.json"

        # Build desired MCP config
        desired: Dict[str, Any] = {
            "permissions": {"allow": [f"mcp__{name}__*" for name in mcp_servers]},
            "mcpServers": mcp_servers,
        }

        # Merge with existing settings if present
        existing: Dict[str, Any] = {}
        if settings_path.exists():
            try:
                existing = json.loads(settings_path.read_text())
            except (json.JSONDecodeError, OSError):
                existing = {}

        existing.setdefault("permissions", {}).setdefault("allow", [])
        for perm in desired["permissions"]["allow"]:
            if perm not in existing["permissions"]["allow"]:
                existing["permissions"]["allow"].append(perm)
        existing["mcpServers"] = mcp_servers

        # Auto-approve only the MCP names we validated and kept. This prevents
        # a broken entry left in the project's .mcp.json from blocking the
        # non-interactive approval flow and cascading onto healthy servers.
        enabled_servers = sorted(mcp_servers.keys())
        existing["enabledMcpjsonServers"] = enabled_servers
        existing["enableAllProjectMcpServers"] = False

        # Skip the write when nothing needs to change (avoids spurious fsync
        # and log spam on repeat invocations).
        if settings_path.exists():
            try:
                on_disk = json.loads(settings_path.read_text())
            except (json.JSONDecodeError, OSError):
                on_disk = None
            if on_disk == existing:
                return

        try:
            settings_dir.mkdir(parents=True, exist_ok=True)
            settings_path.write_text(json.dumps(existing, indent=2))
            logger.info(
                "MCP settings.json written",
                path=str(settings_path),
                servers=list(mcp_servers.keys()),
            )
        except OSError as e:
            logger.warning(
                "Failed to write MCP settings.json",
                path=str(settings_path),
                error=str(e),
            )

    @staticmethod
    def _approve_project_mcps(
        working_directory: Path, mcp_servers: Dict[str, Any]
    ) -> None:
        """Auto-approve project-level MCP servers in ~/.claude.json.

        Claude CLI stores per-project MCP approval under
        `.projects.<abs_path>.enabledMcpjsonServers`. Until each server is
        approved, the CLI reports them as "launched but not connected" and
        does not surface their tools. Writing the approval list preemptively
        matches the state the CLI would record after an interactive "trust"
        prompt — which the bot cannot answer in its non-interactive flow.

        Also pre-approves every server listed in the project's own `.mcp.json`
        (including names we filtered out of options.mcp_servers). Leaving an
        entry unapproved keeps the CLI in the "needs approval" state for the
        whole session, which suppresses otherwise-healthy servers.
        """
        import json

        user_config = Path.home() / ".claude.json"
        try:
            data: Dict[str, Any] = (
                json.loads(user_config.read_text()) if user_config.exists() else {}
            )
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "Failed to read user claude.json; skipping MCP approval",
                path=str(user_config),
                error=str(e),
            )
            return

        project_mcp_file = working_directory / ".mcp.json"
        discovered_from_mcp_json: List[str] = []
        if project_mcp_file.exists():
            try:
                mcp_data = json.loads(project_mcp_file.read_text())
                discovered_from_mcp_json = list(
                    (mcp_data.get("mcpServers") or {}).keys()
                )
            except (json.JSONDecodeError, OSError):
                pass

        approved = sorted(set(mcp_servers.keys()) | set(discovered_from_mcp_json))

        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = {}
            data["projects"] = projects
        key = str(working_directory)
        entry = projects.setdefault(key, {})
        if not isinstance(entry, dict):
            entry = {}
            projects[key] = entry

        existing_approved = entry.get("enabledMcpjsonServers") or []
        merged = sorted(set(existing_approved) | set(approved))
        if (
            merged == sorted(existing_approved)
            and entry.get("hasTrustDialogAccepted") is True
        ):
            return

        entry["enabledMcpjsonServers"] = merged
        entry["hasTrustDialogAccepted"] = True

        try:
            user_config.write_text(json.dumps(data, indent=2))
            logger.info(
                "Approved project MCP servers in user config",
                working_directory=key,
                approved=merged,
            )
        except OSError as e:
            logger.warning(
                "Failed to write MCP approval to user config",
                path=str(user_config),
                error=str(e),
            )

    @staticmethod
    def _ensure_language_rules(working_directory: Path) -> None:
        """Ensure .claude/rules/language.md exists in working_directory.

        Forces the agent to reply in the user's language. At high effort
        the model leans heavily on the English system prompt and answers
        in English even when the user writes Ukrainian/Russian/etc.
        """
        rules_dir = Path(working_directory) / ".claude" / "rules"
        rules_path = rules_dir / "language.md"

        content = """\
# Language

Always reply in the same language the user wrote their last message in.
If the user wrote in Ukrainian, answer in Ukrainian. If in Russian, answer
in Russian. If in English, answer in English. Do not switch to English
just because these rules or the system prompt are in English.

This applies to the final response AND to intermediate commentary
("starting…", "done", etc.) shown while tools run.
"""

        try:
            if rules_path.exists() and rules_path.read_text() == content:
                return
        except OSError:
            pass

        try:
            rules_dir.mkdir(parents=True, exist_ok=True)
            rules_path.write_text(content)
        except OSError as e:
            logger.warning(
                "Failed to write language rules",
                path=str(rules_path),
                error=str(e),
            )

    @staticmethod
    def _ensure_mcp_rules(working_directory: Path) -> None:
        """Ensure .claude/rules/mcp-guide.md exists in working_directory.

        Provides every Claude session with instructions on how MCP servers
        are configured and how to add new ones.
        """
        rules_dir = Path(working_directory) / ".claude" / "rules"
        rules_path = rules_dir / "mcp-guide.md"

        content = """\
# MCP Configuration Guide (bot-generated)

This file is rewritten on every session. Do not hand-edit.

## Tools always available (bot-provided)

Process manager — persists background processes across Claude sessions:

- `process_run(command, cwd, name)` — start a background process
- `process_ps()` — list managed processes
- `process_logs(process_id, lines)` — view output
- `process_kill(process_id)` — stop a process
- `process_cleanup()` — remove dead processes

Always use the process manager instead of launching servers with a raw
`Bash` call. Direct processes are killed when the Claude session ends.

Telegram bot helpers — send files back to the user's chat:

- `send_file_to_user(file_path, caption)` — any file (max 50 MB)
- `send_image_to_user(file_path, caption)` — image with inline preview
  (png, jpg, jpeg, gif, webp, bmp, svg)

Both accept absolute paths inside the approved working directory only,
and deliver the file automatically after your response.

## How MCP is wired in this bot

For every session the bot builds a merged MCP list and hands it to Claude
CLI via `--mcp-config` plus `--strict-mcp-config`. Sources, in precedence
(bot wins on name collisions):

1. **Project-local**: `<project>/.mcp.json` and `<project>/.claude/settings.json`
   (entries whose `cwd` is missing or uses a Windows drive letter are
   dropped with a warning).
2. **Bot-owned**: `/app/mcp-process.json` — `process-manager`, `telegram`.
3. **User-configured**: `MCP_CONFIG_PATH` env var if `ENABLE_MCP=true`.

Because of `--strict-mcp-config`, the CLI ignores any native MCP approval
dialog. There is nothing for you to "trust" — if a server is in the
merged dict, the CLI attaches it.

## Auto-install of project MCP dependencies

If a project MCP config points to `cwd: /path/to/server_dir` and that
directory contains `requirements.txt`, the bot automatically runs:

    pip install --target /app/data/.mcp_deps/<server>-<hash> -r requirements.txt

before spawning the subprocess, and prepends the target to the server's
`PYTHONPATH`. The cache is keyed by sha256 of `requirements.txt`, so the
install happens once per requirements snapshot and is re-used on next
sessions. Change `requirements.txt` → new hash → re-install.

Installs live on the `/app/data` volume, so they survive deploys and
container restarts.

**What you should do when a project MCP is missing dependencies:**

1. Put a `requirements.txt` next to the MCP's server script (in the same
   directory that the `.mcp.json` entry uses as `cwd`).
2. List every third-party module the server imports (e.g. `telethon`,
   `aiosqlite`).
3. Run `/mcp` in the bot — the next inspection (or any Claude session)
   will install them and attach the server.

Do **not** try to `pip install --user …` from inside the Claude session:
`~/.local` is not on the persistent volume and the install is wiped on
the next deploy. Put the deps in `requirements.txt` and let the bot
cache them.

## Adding a new bot-owned MCP server

1. Create a FastMCP server in `src/mcp/` or `src/process/`. Import from
   `fastmcp` (`from fastmcp import FastMCP`) and launch with
   `mcp.run(transport="stdio")`. Stdio is mandatory — the CLI does not
   speak HTTP to local MCPs.
2. Register it in `/app/mcp-process.json` under `mcpServers` with an
   explicit `env.PYTHONPATH=/app` (the CLI ignores `cwd` for module
   resolution, so without PYTHONPATH your module will not import).
3. Mirror the same entry in `entrypoint.sh` so existing project folders
   on the volume pick it up on next restart.
4. Deploy. Every session will see the new tools automatically.

## Common mistakes — don't

- Do not add bash fallbacks to `CLAUDE.md` when an MCP is missing. Fix
  the MCP config or its `requirements.txt` instead.
- Do not launch FastMCP with the default HTTP transport.
- Do not omit `env.PYTHONPATH`.
- Do not hard-code Windows paths (`D:\\…`) in `.mcp.json` — this is a
  Linux container. Such entries are filtered out on load.
- Do not install MCP deps into `~/.local` — they will be lost on deploy.
"""

        try:
            if rules_path.exists() and rules_path.read_text() == content:
                return
        except OSError:
            pass

        try:
            rules_dir.mkdir(parents=True, exist_ok=True)
            rules_path.write_text(content)
        except OSError as e:
            logger.warning(
                "Failed to write MCP rules",
                path=str(rules_path),
                error=str(e),
            )

    async def _ensure_mcp_deps(self, server_name: str, cfg: Dict[str, Any]) -> None:
        """Install `requirements.txt` next to a project MCP into a persistent
        cache, and prepend it to the server's PYTHONPATH.

        Rationale: project-local stdio MCP servers (e.g. TG_mcp_clon/server.py)
        import third-party libs (telethon, aiosqlite, …) that are not in the
        bot's image. User-site pip installs don't persist across deploys, so
        we cache installs on the `/app/data` volume keyed by a hash of the
        requirements file. Re-install only when the file changes.
        """
        import hashlib

        cwd_str = cfg.get("cwd")
        if not isinstance(cwd_str, str) or not cwd_str:
            return
        cwd = Path(cwd_str)
        if not cwd.is_dir():
            return
        req_file = cwd / "requirements.txt"
        if not req_file.is_file():
            return

        try:
            req_bytes = req_file.read_bytes()
        except OSError:
            return
        req_hash = hashlib.sha256(req_bytes).hexdigest()[:12]

        deps_root = Path("/app/data/.mcp_deps")
        target = deps_root / f"{server_name}-{req_hash}"
        marker = target / ".installed"

        if not marker.exists():
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                logger.warning(
                    "Cannot create MCP deps dir",
                    server=server_name,
                    target=str(target),
                    error=str(e),
                )
                return
            logger.info(
                "Installing MCP server deps",
                server=server_name,
                requirements=str(req_file),
                target=str(target),
            )
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pip",
                    "install",
                    "--quiet",
                    "--disable-pip-version-check",
                    "--no-warn-script-location",
                    "--target",
                    str(target),
                    "-r",
                    str(req_file),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                logger.warning(
                    "pip not available, skipping MCP deps install",
                    server=server_name,
                )
                return
            try:
                stdout_b, stderr_b = await asyncio.wait_for(
                    proc.communicate(), timeout=600
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                logger.warning("MCP deps install timed out", server=server_name)
                return
            if proc.returncode != 0:
                logger.warning(
                    "MCP deps install failed",
                    server=server_name,
                    returncode=proc.returncode,
                    stderr=(stderr_b or b"").decode(errors="replace")[-500:],
                )
                return
            try:
                marker.write_text(req_hash)
            except OSError:
                pass
            logger.info("MCP deps ready", server=server_name, target=str(target))

        env = cfg.get("env")
        if not isinstance(env, dict):
            env = {}
            cfg["env"] = env
        target_str = str(target)
        existing = env.get("PYTHONPATH", "")
        parts = existing.split(":") if existing else []
        if target_str not in parts:
            parts.insert(0, target_str)
            env["PYTHONPATH"] = ":".join(parts)

    def _build_mcp_servers(
        self, working_directory: Path
    ) -> tuple[Dict[str, Any], Dict[str, Any]]:
        """Build the bot + project MCP server dicts for a working directory.

        Returns a (bot_servers, project_servers) tuple where bot-owned names
        are authoritative and project-local entries sharing those names are
        filtered out (see `_load_project_mcps`).
        """
        bot_servers: Dict[str, Any] = {}

        app_root = Path(__file__).resolve().parent.parent.parent
        process_mcp_path = app_root / "mcp-process.json"
        if process_mcp_path.exists():
            auto_mcp = self._load_mcp_config(process_mcp_path)
            for server_name in auto_mcp:
                auto_mcp[server_name]["cwd"] = str(app_root)
                auto_mcp[server_name].setdefault("env", {})
                auto_mcp[server_name]["env"]["PYTHONPATH"] = str(app_root)
            bot_servers.update(auto_mcp)

        if self.config.enable_mcp and self.config.mcp_config_path:
            bot_servers.update(self._load_mcp_config(self.config.mcp_config_path))

        project_servers = self._load_project_mcps(
            working_directory, exclude_names=set(bot_servers.keys())
        )
        return bot_servers, project_servers

    async def inspect_mcp_servers(
        self, working_directory: Path
    ) -> List[Dict[str, Any]]:
        """Enumerate MCP servers and their tools for `working_directory`.

        Each returned entry has keys: name, origin ("bot" or "project"),
        command, args, cwd, tools (list[str] on success) or error (str).
        """
        # Refresh the on-disk rules doc so the project folder stays in sync
        # with the bot's current MCP contract.
        self._ensure_mcp_rules(working_directory)
        self._ensure_language_rules(working_directory)

        bot_servers, project_servers = self._build_mcp_servers(working_directory)
        for name, cfg in project_servers.items():
            await self._ensure_mcp_deps(name, cfg)

        combined: List[tuple[str, str, Dict[str, Any]]] = []
        for name, cfg in project_servers.items():
            combined.append((name, "project", cfg))
        for name, cfg in bot_servers.items():
            combined.append((name, "bot", cfg))

        results: List[Dict[str, Any]] = []
        for name, origin, cfg in combined:
            entry: Dict[str, Any] = {
                "name": name,
                "origin": origin,
                "command": cfg.get("command"),
                "args": cfg.get("args") or [],
                "cwd": cfg.get("cwd"),
            }
            try:
                tools = await asyncio.wait_for(
                    self._list_tools_for_server(cfg), timeout=10
                )
                entry["tools"] = tools
                logger.info(
                    "MCP inspect ok",
                    server=name,
                    origin=origin,
                    tool_count=len(tools),
                )
            except asyncio.TimeoutError:
                entry["error"] = "timed out after 10s"
                logger.warning("MCP inspect timeout", server=name, origin=origin)
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}"
                # Truncate long errors so they fit Telegram HTML rendering
                if len(err_msg) > 300:
                    err_msg = err_msg[:297] + "..."
                entry["error"] = err_msg
                logger.warning(
                    "MCP inspect failed",
                    server=name,
                    origin=origin,
                    error=err_msg,
                )
            results.append(entry)
        return results

    @staticmethod
    async def _list_tools_for_server(cfg: Dict[str, Any]) -> List[str]:
        """Spawn a stdio MCP server subprocess and query tools/list.

        Captures subprocess stderr so import-time crashes surface as a
        readable reason instead of a generic ExceptionGroup.
        """
        import tempfile

        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = cfg.get("command")
        if not isinstance(command, str) or not command:
            raise ValueError("missing command")

        server_env = dict(os.environ)
        cfg_env = cfg.get("env") or {}
        if isinstance(cfg_env, dict):
            server_env.update({str(k): str(v) for k, v in cfg_env.items()})

        cfg_cwd = cfg.get("cwd")
        cwd_value: Optional[str] = None
        if isinstance(cfg_cwd, str) and cfg_cwd:
            if not Path(cfg_cwd).is_dir():
                raise ValueError(f"cwd does not exist: {cfg_cwd}")
            cwd_value = cfg_cwd

        params = StdioServerParameters(
            command=command,
            args=[str(a) for a in (cfg.get("args") or [])],
            env=server_env,
            cwd=cwd_value,
        )

        # stdio_client forwards `errlog` straight to asyncio.create_subprocess_exec
        # as its `stderr=` argument, which needs a real OS file descriptor.
        # Use a rewindable temp file so we can read captured stderr after the
        # subprocess exits.
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            try:
                async with stdio_client(params, errlog=stderr_file) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        resp = await session.list_tools()
                        return [t.name for t in resp.tools]
            except BaseException as exc:
                inner = exc
                while True:
                    sub_excs = getattr(inner, "exceptions", None)
                    if sub_excs:
                        inner = sub_excs[0]
                        continue
                    cause = getattr(inner, "__cause__", None) or getattr(
                        inner, "__context__", None
                    )
                    if cause is not None and cause is not inner:
                        inner = cause
                        continue
                    break
                try:
                    stderr_file.seek(0)
                    stderr_text = stderr_file.read().strip()
                except OSError:
                    stderr_text = ""
                if stderr_text:
                    lines = [ln for ln in stderr_text.splitlines() if ln.strip()]
                    picked = next(
                        (
                            ln
                            for ln in reversed(lines)
                            if "Error" in ln or "error" in ln
                        ),
                        lines[-1] if lines else "",
                    )
                    raise RuntimeError(f"{type(inner).__name__}: {picked}") from inner
                raise RuntimeError(f"{type(inner).__name__}: {inner}") from inner

    def _load_mcp_config(self, config_path: Path) -> Dict[str, Any]:
        """Load MCP server configuration from a JSON file.

        The new claude-agent-sdk expects mcp_servers as a dict, not a file path.
        """
        import json

        try:
            with open(config_path) as f:
                config_data = json.load(f)
            return config_data.get("mcpServers", {})
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "Failed to load MCP config", path=str(config_path), error=str(e)
            )
            return {}

    def _load_project_mcps(
        self, working_directory: Path, exclude_names: set
    ) -> Dict[str, Any]:
        """Discover MCP servers declared inside the user's selected folder.

        Reads Claude Code's native project MCP locations:
        - {working_directory}/.mcp.json
        - {working_directory}/.claude/settings.json (mcpServers block)

        Keys present in `exclude_names` are skipped so bot-owned names remain
        authoritative and stale copies in the project's settings.json cannot
        shadow the current bot configuration.

        Entries with an unreachable `cwd` or a script path that clearly cannot
        be launched on this host (e.g. Windows drive letters copied into a
        Linux container) are dropped — a single broken subprocess otherwise
        cascades and prevents the remaining MCP servers from being usable.
        """
        import json

        discovered: Dict[str, Any] = {}
        candidates = [
            working_directory / ".mcp.json",
            working_directory / ".claude" / "settings.json",
        ]

        for path in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "Failed to read project MCP file",
                    path=str(path),
                    error=str(e),
                )
                continue
            servers = data.get("mcpServers") or {}
            for name, cfg in servers.items():
                if name in exclude_names or name in discovered:
                    continue
                skip_reason = self._mcp_config_skip_reason(cfg)
                if skip_reason:
                    logger.warning(
                        "Skipping invalid project MCP entry",
                        server=name,
                        source=str(path),
                        reason=skip_reason,
                    )
                    continue
                discovered[name] = cfg

        if discovered:
            logger.info(
                "Loaded project-local MCPs",
                working_directory=str(working_directory),
                servers=list(discovered.keys()),
            )

        return discovered

    @staticmethod
    def _mcp_config_skip_reason(cfg: Any) -> Optional[str]:
        """Return a human-readable reason to skip an MCP entry, or None.

        Only validates stdio-transport configs that launch a local process.
        Anything with an explicit non-stdio `type`/`transport` is assumed
        valid — we can't check remote endpoints from here.
        """
        if not isinstance(cfg, dict):
            return "entry is not a JSON object"

        transport = cfg.get("type") or cfg.get("transport")
        if transport and transport not in ("stdio", None):
            return None

        command = cfg.get("command")
        if not isinstance(command, str) or not command:
            return "missing or invalid command"

        # Windows drive letters in a Linux container are a common footgun
        def _looks_like_windows_path(val: str) -> bool:
            return len(val) >= 3 and val[1:3] == ":\\" and val[0].isalpha()

        cwd = cfg.get("cwd")
        if isinstance(cwd, str) and cwd:
            if _looks_like_windows_path(cwd):
                return f"cwd uses a Windows path ({cwd!r}) unavailable here"
            if not Path(cwd).is_dir():
                return f"cwd does not exist: {cwd!r}"

        args = cfg.get("args") or []
        if isinstance(args, list):
            for arg in args:
                if isinstance(arg, str) and _looks_like_windows_path(arg):
                    return f"arg uses a Windows path ({arg!r}) unavailable here"

        return None
