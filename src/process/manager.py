"""File-based process manager — shared between bot and MCP server."""

import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

STATE_FILE = Path("/app/data/processes.json")
LOGS_DIR = Path("/app/data/proc_logs")


@dataclass
class ProcessEntry:
    id: int
    name: str
    command: str
    cwd: str
    pid: int
    started_at: float

    @property
    def is_alive(self) -> bool:
        try:
            os.kill(self.pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    @property
    def uptime(self) -> str:
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
        return "running" if self.is_alive else "stopped"

    @property
    def log_path(self) -> Path:
        return LOGS_DIR / f"{self.id}.log"

    def last_logs(self, lines: int = 30) -> str:
        if not self.log_path.exists():
            return "(no output)"
        try:
            text = self.log_path.read_text(errors="replace")
            all_lines = text.splitlines()
            return "\n".join(all_lines[-lines:])
        except Exception:
            return "(error reading logs)"


class ProcessManager:
    """Manages background processes with file-based state (shared across bot & MCP)."""

    def __init__(self) -> None:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> dict:
        if STATE_FILE.exists():
            try:
                return json.loads(STATE_FILE.read_text())
            except (json.JSONDecodeError, OSError):
                return {"next_id": 1, "processes": {}}
        return {"next_id": 1, "processes": {}}

    def _save_state(self, state: dict) -> None:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state, indent=2))

    def _entry_from_dict(self, d: dict) -> ProcessEntry:
        return ProcessEntry(
            id=d["id"],
            name=d.get("name", ""),
            command=d["command"],
            cwd=d["cwd"],
            pid=d["pid"],
            started_at=d["started_at"],
        )

    def start(self, command: str, cwd: str, name: str = "") -> ProcessEntry:
        """Start a background process."""
        state = self._load_state()
        proc_id = state["next_id"]
        state["next_id"] = proc_id + 1

        log_path = LOGS_DIR / f"{proc_id}.log"

        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=cwd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )

        if not name:
            name = command.split()[0] if command.split() else command[:20]

        entry = ProcessEntry(
            id=proc_id,
            name=name,
            command=command,
            cwd=cwd,
            pid=proc.pid,
            started_at=time.time(),
        )

        state["processes"][str(proc_id)] = asdict(entry)
        self._save_state(state)

        logger.info("Process #%d '%s' started: pid=%d", proc_id, name, proc.pid)
        return entry

    def list_all(self) -> list[ProcessEntry]:
        state = self._load_state()
        return [self._entry_from_dict(d) for d in state["processes"].values()]

    def list_alive(self) -> list[ProcessEntry]:
        return [p for p in self.list_all() if p.is_alive]

    def get(self, proc_id: int) -> Optional[ProcessEntry]:
        state = self._load_state()
        d = state["processes"].get(str(proc_id))
        return self._entry_from_dict(d) if d else None

    def kill(self, proc_id: int) -> Optional[ProcessEntry]:
        entry = self.get(proc_id)
        if not entry:
            return None

        if entry.is_alive:
            try:
                os.killpg(os.getpgid(entry.pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                try:
                    os.kill(entry.pid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass

        logger.info("Process #%d killed (pid=%d)", proc_id, entry.pid)
        return entry

    def remove(self, proc_id: int) -> bool:
        state = self._load_state()
        key = str(proc_id)
        if key in state["processes"]:
            del state["processes"][key]
            self._save_state(state)
            log = LOGS_DIR / f"{proc_id}.log"
            if log.exists():
                log.unlink()
            return True
        return False

    def cleanup_dead(self) -> int:
        state = self._load_state()
        dead = [k for k, d in state["processes"].items()
                if not self._entry_from_dict(d).is_alive]
        for k in dead:
            log = LOGS_DIR / f"{k}.log"
            if log.exists():
                log.unlink()
            del state["processes"][k]
        if dead:
            self._save_state(state)
        return len(dead)
