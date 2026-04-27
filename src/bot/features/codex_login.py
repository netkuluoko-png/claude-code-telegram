"""Codex CLI device authentication helpers."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class CodexLoginProcess:
    """Tracks a running `codex login --device-auth` process."""

    process: asyncio.subprocess.Process
    output_lines: List[str] = field(default_factory=list)
    stderr_lines: List[str] = field(default_factory=list)
    done: bool = False
    returncode: Optional[int] = None

    def output_text(self) -> str:
        """Return captured output suitable for showing to a user."""
        lines = self.output_lines or self.stderr_lines
        return "\n".join(lines).strip()


def build_codex_login_env() -> Dict[str, str]:
    """Build environment for Codex CLI login."""
    env = os.environ.copy()
    codex_home = env.get("CODEX_HOME")
    if codex_home:
        Path(codex_home).mkdir(parents=True, exist_ok=True)
    return env


async def start_device_login(codex_cli_path: Optional[str] = None) -> CodexLoginProcess:
    """Start `codex login --device-auth` and return its tracker."""
    process = await asyncio.create_subprocess_exec(
        codex_cli_path or "codex",
        "login",
        "--device-auth",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=build_codex_login_env(),
    )
    return CodexLoginProcess(process=process)


async def collect_initial_output(
    login: CodexLoginProcess,
    *,
    timeout: float = 10.0,
) -> str:
    """Collect early CLI output until instructions appear or timeout expires."""

    async def _read_stream(stream, target: List[str]) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            target.append(line.decode(errors="replace").rstrip())

    if login.process.stdout is None or login.process.stderr is None:
        return ""

    stdout_task = asyncio.create_task(
        _read_stream(login.process.stdout, login.output_lines)
    )
    stderr_task = asyncio.create_task(
        _read_stream(login.process.stderr, login.stderr_lines)
    )

    try:
        await asyncio.wait_for(login.process.wait(), timeout=0.05)
    except asyncio.TimeoutError:
        pass

    try:
        await asyncio.wait_for(_wait_for_visible_output(login), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    if login.process.returncode is not None:
        login.done = True
        login.returncode = login.process.returncode

    # Keep the stream-reader tasks alive; finish_device_login awaits process
    # completion and then cancels any still-running readers.
    login._stdout_task = stdout_task  # type: ignore[attr-defined]
    login._stderr_task = stderr_task  # type: ignore[attr-defined]
    return login.output_text()


async def _wait_for_visible_output(login: CodexLoginProcess) -> None:
    """Wait until the CLI emits something user-facing."""
    while not login.output_text() and login.process.returncode is None:
        await asyncio.sleep(0.1)


async def finish_device_login(login: CodexLoginProcess) -> int:
    """Wait for device auth completion and clean up reader tasks."""
    returncode = await login.process.wait()
    login.done = True
    login.returncode = returncode

    for attr in ("_stdout_task", "_stderr_task"):
        task = getattr(login, attr, None)
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    return returncode
