"""Background process manager — processes persist across Claude sessions."""

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

MAX_LOG_LINES = 200


@dataclass
class ManagedProcess:
    """A background process managed by the bot."""

    id: int
    command: str
    cwd: str
    pid: Optional[int] = None
    started_at: float = 0.0
    process: Optional[asyncio.subprocess.Process] = None
    stdout_lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    stderr_lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LOG_LINES))
    _reader_task: Optional[asyncio.Task] = field(default=None, repr=False)

    @property
    def is_alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def uptime(self) -> str:
        if not self.started_at:
            return "0s"
        delta = int(time.time() - self.started_at)
        if delta < 60:
            return f"{delta}s"
        if delta < 3600:
            return f"{delta // 60}m {delta % 60}s"
        h = delta // 3600
        m = (delta % 3600) // 60
        return f"{h}h {m}m"

    @property
    def status(self) -> str:
        if self.is_alive:
            return "running"
        rc = self.process.returncode if self.process else None
        return f"exited({rc})"

    @property
    def last_logs(self) -> str:
        lines = list(self.stdout_lines) + list(self.stderr_lines)
        lines.sort()
        return "\n".join(line for _, line in lines[-30:])


class ProcessManager:
    """Manages background processes that outlive Claude sessions."""

    def __init__(self) -> None:
        self._processes: dict[int, ManagedProcess] = {}
        self._next_id = 1

    async def start(self, command: str, cwd: str) -> ManagedProcess:
        """Start a background process."""
        proc_id = self._next_id
        self._next_id += 1

        mp = ManagedProcess(id=proc_id, command=command, cwd=cwd)

        process = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        mp.process = process
        mp.pid = process.pid
        mp.started_at = time.time()
        mp._reader_task = asyncio.create_task(self._read_output(mp))

        self._processes[proc_id] = mp
        logger.info("Process #%d started: pid=%d cmd=%s", proc_id, process.pid, command)
        return mp

    async def _read_output(self, mp: ManagedProcess) -> None:
        """Read stdout and stderr in background."""
        proc = mp.process
        if not proc:
            return

        async def _read_stream(
            stream: Optional[asyncio.StreamReader],
            buf: deque,
            tag: str,
        ) -> None:
            if not stream:
                return
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                buf.append((time.time(), text))

        await asyncio.gather(
            _read_stream(proc.stdout, mp.stdout_lines, "out"),
            _read_stream(proc.stderr, mp.stderr_lines, "err"),
        )

    def list(self) -> list[ManagedProcess]:
        """List all processes (alive and dead)."""
        return list(self._processes.values())

    def list_alive(self) -> list[ManagedProcess]:
        """List only alive processes."""
        return [p for p in self._processes.values() if p.is_alive]

    def get(self, proc_id: int) -> Optional[ManagedProcess]:
        """Get process by ID."""
        return self._processes.get(proc_id)

    async def kill(self, proc_id: int) -> Optional[ManagedProcess]:
        """Kill a process by ID."""
        mp = self._processes.get(proc_id)
        if not mp or not mp.process:
            return None

        if mp.is_alive:
            try:
                mp.process.terminate()
                try:
                    await asyncio.wait_for(mp.process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    mp.process.kill()
                    await mp.process.wait()
            except ProcessLookupError:
                pass

        if mp._reader_task:
            mp._reader_task.cancel()

        logger.info("Process #%d killed", proc_id)
        return mp

    async def kill_all(self) -> int:
        """Kill all alive processes. Returns count killed."""
        count = 0
        for mp in self.list_alive():
            await self.kill(mp.id)
            count += 1
        return count

    def cleanup_dead(self) -> int:
        """Remove dead processes from the list."""
        dead = [pid for pid, p in self._processes.items() if not p.is_alive]
        for pid in dead:
            del self._processes[pid]
        return len(dead)
