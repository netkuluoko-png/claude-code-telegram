"""Codex CLI integration.

The Codex CLI does not expose the same Python SDK surface as Claude Code in this
project, so this manager drives `codex exec --json` as a subprocess and adapts
its JSONL events to the bot's existing response/stream contract.
"""

import asyncio
import base64
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..claude.exceptions import ClaudeProcessError, ClaudeTimeoutError
from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings

logger = structlog.get_logger()

TASK_COMPLETED_MSG = "Task completed."


@dataclass
class CodexResponse:
    """Response from Codex CLI."""

    content: str
    session_id: str
    cost: float
    duration_ms: int
    num_turns: int
    is_error: bool = False
    error_type: Optional[str] = None
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    interrupted: bool = False


class CodexCLIManager:
    """Manage Codex CLI subprocess execution."""

    def __init__(self, config: Settings):
        self.config = config

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
    ) -> CodexResponse:
        """Execute a Codex command via `codex exec`."""
        del user_id
        start_time = asyncio.get_event_loop().time()
        working_directory = Path(working_directory).resolve()
        stderr_lines: List[str] = []
        stdout_lines: List[str] = []
        events: List[Dict[str, Any]] = []
        output_last_message = None
        interrupted = False

        with tempfile.TemporaryDirectory(prefix="codex-telegram-") as tmpdir:
            tmp_path = Path(tmpdir)
            output_last_message = tmp_path / "last-message.txt"
            image_paths = self._write_image_files(tmp_path, images or [])
            args = self._build_args(
                working_directory=working_directory,
                session_id=session_id,
                continue_session=continue_session,
                output_last_message=output_last_message,
                image_paths=image_paths,
                model_override=model_override,
                effort_override=effort_override,
            )

            logger.info(
                "Starting Codex CLI command",
                working_directory=str(working_directory),
                session_id=session_id,
                continue_session=continue_session,
            )

            proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(working_directory),
                env=self._build_env(),
            )

            async def _feed_prompt() -> None:
                assert proc.stdin is not None
                proc.stdin.write(prompt.encode("utf-8"))
                await proc.stdin.drain()
                proc.stdin.close()

            async def _read_stdout() -> None:
                assert proc.stdout is not None
                async for raw_line in proc.stdout:
                    line = raw_line.decode(errors="replace").strip()
                    if not line:
                        continue
                    event = self._parse_json_line(line)
                    if event is None:
                        stdout_lines.append(line)
                        continue
                    events.append(event)
                    if stream_callback:
                        update = self._event_to_stream_update(event)
                        if update is not None:
                            await stream_callback(update)

            async def _read_stderr() -> None:
                assert proc.stderr is not None
                async for raw_line in proc.stderr:
                    line = raw_line.decode(errors="replace").rstrip()
                    if line:
                        stderr_lines.append(line)
                        logger.debug("Codex CLI stderr", line=line)

            feed_task = asyncio.create_task(_feed_prompt())
            stdout_task = asyncio.create_task(_read_stdout())
            stderr_task = asyncio.create_task(_read_stderr())
            wait_task = asyncio.create_task(proc.wait())

            interrupt_task: Optional["asyncio.Task[None]"] = None
            if interrupt_event is not None:

                async def _cancel_on_interrupt() -> None:
                    nonlocal interrupted
                    await interrupt_event.wait()
                    interrupted = True
                    proc.terminate()

                interrupt_task = asyncio.create_task(_cancel_on_interrupt())

            try:
                await asyncio.wait_for(
                    wait_task,
                    timeout=self.config.codex_timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise ClaudeTimeoutError(
                    f"Codex CLI timed out after {self.config.codex_timeout_seconds}s"
                )
            finally:
                for task in (feed_task, stdout_task, stderr_task):
                    if not task.done():
                        task.cancel()
                if interrupt_task is not None:
                    interrupt_task.cancel()
                await asyncio.gather(
                    feed_task, stdout_task, stderr_task, return_exceptions=True
                )

            if proc.returncode != 0 and not interrupted:
                message = (
                    self._extract_error(events)
                    or "\n".join(stderr_lines[-20:])
                    or "\n".join(stdout_lines[-20:])
                    or "no error output"
                )
                logger.error(
                    "Codex CLI process failed",
                    exit_code=proc.returncode,
                    error=message[-2000:],
                    stderr_tail=stderr_lines[-5:],
                    stdout_tail=stdout_lines[-5:],
                    event_types=[e.get("type") for e in events[-10:]],
                )
                raise ClaudeProcessError(
                    f"Codex process error (exit {proc.returncode}): {message}"
                )

            content = self._extract_final_content(events)
            if output_last_message and output_last_message.exists():
                file_content = output_last_message.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                if file_content:
                    content = file_content
            if not content and self._extract_tools(events):
                content = TASK_COMPLETED_MSG

            duration_ms = int((asyncio.get_event_loop().time() - start_time) * 1000)
            final_session_id = self._extract_session_id(events) or session_id or ""

            return CodexResponse(
                content=content,
                session_id=final_session_id,
                cost=0.0,
                duration_ms=duration_ms,
                num_turns=max(
                    1, len([e for e in events if e.get("type") == "turn.started"])
                ),
                tools_used=self._extract_tools(events),
                interrupted=interrupted,
            )

    def _build_args(
        self,
        working_directory: Path,
        session_id: Optional[str],
        continue_session: bool,
        output_last_message: Path,
        image_paths: List[Path],
        model_override: Optional[str],
        effort_override: Optional[str],
    ) -> List[str]:
        binary = self.config.codex_cli_path or "codex"
        sandbox = (
            "workspace-write" if self.config.sandbox_enabled else "danger-full-access"
        )
        args = [
            binary,
            "--ask-for-approval",
            self.config.codex_approval_policy,
            "--sandbox",
            sandbox,
            "--cd",
            str(working_directory),
        ]

        model = model_override or self.config.codex_model
        if model:
            args.extend(["--model", model])

        effort = effort_override or self.config.codex_effort
        if effort:
            args.extend(["-c", f'model_reasoning_effort="{effort}"'])

        args.append("exec")
        if continue_session and session_id:
            args.extend(["resume", "--json", "--skip-git-repo-check"])
            args.extend(["-o", str(output_last_message)])
            for image_path in image_paths:
                args.extend(["--image", str(image_path)])
            args.extend([session_id, "-"])
        else:
            args.extend(["--json", "--skip-git-repo-check"])
            args.extend(["-o", str(output_last_message)])
            for image_path in image_paths:
                args.extend(["--image", str(image_path)])
            args.append("-")
        return args

    def _build_env(self) -> Dict[str, str]:
        env = os.environ.copy()
        if not env.get("CODEX_HOME") and Path("/app/data/.codex").exists():
            env["CODEX_HOME"] = "/app/data/.codex"
        if self.config.openai_api_key_str:
            env.setdefault("OPENAI_API_KEY", self.config.openai_api_key_str)
        return env

    @staticmethod
    def _parse_json_line(line: str) -> Optional[Dict[str, Any]]:
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _event_to_stream_update(event: Dict[str, Any]) -> Optional[StreamUpdate]:
        event_type = str(event.get("type") or "")
        if event_type == "error":
            return StreamUpdate(
                type="error",
                content=str(event.get("message") or "Codex error"),
                metadata={"is_error": True},
            )
        if event_type in {"turn.started", "thread.started"}:
            return StreamUpdate(type="system", metadata=event)

        text = CodexCLIManager._extract_text_from_event(event)
        if text:
            return StreamUpdate(type="stream_delta", content=text)

        tool = CodexCLIManager._extract_tool_from_event(event)
        if tool:
            return StreamUpdate(type="assistant", tool_calls=[tool])
        return None

    @staticmethod
    def _extract_session_id(events: List[Dict[str, Any]]) -> Optional[str]:
        for event in events:
            thread_id = event.get("thread_id") or event.get("session_id")
            if isinstance(thread_id, str) and thread_id:
                return thread_id
        return None

    @staticmethod
    def _extract_final_content(events: List[Dict[str, Any]]) -> str:
        for event in reversed(events):
            text = CodexCLIManager._extract_text_from_event(event)
            if text:
                return text.strip()
        return ""

    @staticmethod
    def _extract_error(events: List[Dict[str, Any]]) -> str:
        for event in reversed(events):
            if event.get("type") in {"turn.failed", "error"}:
                error = event.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str):
                        return message
                message = event.get("message")
                if isinstance(message, str):
                    return message
        return ""

    @staticmethod
    def _extract_text_from_event(event: Dict[str, Any]) -> str:
        for key in ("message", "text", "content", "delta"):
            value = event.get(key)
            if isinstance(value, str) and value:
                return value

        item = event.get("item")
        if isinstance(item, dict):
            for key in ("text", "content"):
                value = item.get(key)
                if isinstance(value, str) and value:
                    return value
            content = item.get("content")
            if isinstance(content, list):
                parts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and isinstance(block.get("text"), str)
                ]
                return "".join(parts)
        return ""

    @staticmethod
    def _extract_tool_from_event(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for key in ("tool_call", "tool"):
            value = event.get(key)
            if isinstance(value, dict):
                name = value.get("name") or value.get("tool_name")
                if isinstance(name, str) and name:
                    return {
                        "name": name,
                        "input": value.get("input") or value.get("arguments") or {},
                    }
        return None

    @staticmethod
    def _extract_tools(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        for event in events:
            tool = CodexCLIManager._extract_tool_from_event(event)
            if tool:
                tools.append(tool)
        return tools

    @staticmethod
    def _write_image_files(tmp_path: Path, images: List[Dict[str, str]]) -> List[Path]:
        paths: List[Path] = []
        for idx, image in enumerate(images):
            data = image.get("data")
            if not data:
                continue
            media_type = image.get("media_type", "image/png")
            ext = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(media_type, ".png")
            path = tmp_path / f"image-{idx}{ext}"
            path.write_bytes(base64.b64decode(data))
            paths.append(path)
        return paths

    async def inspect_mcp_servers(
        self, working_directory: Path
    ) -> List[Dict[str, Any]]:
        """MCP inspection placeholder for API parity with ClaudeIntegration."""
        del working_directory
        return []
