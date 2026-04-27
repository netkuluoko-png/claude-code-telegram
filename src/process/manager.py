"""File-based process manager — shared between bot and MCP server."""

import json
import logging
import os
import signal
import shlex
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
    log_dir: str = str(LOGS_DIR)

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
        return Path(self.log_dir) / f"{self.id}.log"

    def last_logs(self, lines: int = 30) -> str:
        if not self.log_path.exists():
            return "(no output)"
        try:
            text = self.log_path.read_text(errors="replace")
            all_lines = text.splitlines()
            return "\n".join(all_lines[-lines:])
        except Exception:
            return "(error reading logs)"


def _safe_namespace(value: str) -> str:
    return "".join(ch for ch in value if ch.isalnum() or ch in ("-", "_")) or "default"


class ProcessManager:
    """Manages background processes with file-based state (shared across bot & MCP)."""

    def __init__(
        self,
        namespace: Optional[str] = None,
        approved_directory: Optional[str | Path] = None,
    ) -> None:
        namespace = namespace or os.environ.get("PROCESS_NAMESPACE") or "default"
        self.namespace = _safe_namespace(namespace)
        approved = approved_directory or os.environ.get("PROCESS_APPROVED_DIRECTORY")
        self.approved_directory = Path(approved).resolve() if approved else None
        self.state_file = (
            STATE_FILE
            if self.namespace == "default"
            else STATE_FILE.with_name(f"processes-{self.namespace}.json")
        )
        self.logs_dir = (
            LOGS_DIR if self.namespace == "default" else LOGS_DIR / self.namespace
        )
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def _load_state(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except (json.JSONDecodeError, OSError):
                return {"next_id": 1, "processes": {}}
        return {"next_id": 1, "processes": {}}

    def _save_state(self, state: dict) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, indent=2))

    def _entry_from_dict(self, d: dict) -> ProcessEntry:
        return ProcessEntry(
            id=d["id"],
            name=d.get("name", ""),
            command=d["command"],
            cwd=d["cwd"],
            pid=d["pid"],
            started_at=d["started_at"],
            log_dir=d.get("log_dir", str(self.logs_dir)),
        )

    def _validate_cwd(self, cwd: str) -> Path:
        resolved = Path(cwd).resolve()
        if not resolved.is_dir():
            raise ValueError(f"cwd does not exist: {cwd}")
        if self.approved_directory:
            try:
                resolved.relative_to(self.approved_directory)
            except ValueError as exc:
                raise ValueError(
                    f"cwd is outside approved directory: {self.approved_directory}"
                ) from exc
        return resolved

    def _validate_command_paths(self, command: str, cwd: Path) -> None:
        if not self.approved_directory:
            return
        try:
            tokens = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"command could not be parsed safely: {exc}") from exc

        for token in tokens:
            if (
                token.startswith("-")
                or "=" in token
                and not token.startswith(("/", "."))
            ):
                continue
            looks_like_path = token.startswith(("/", "./", "../")) or "/" in token
            if not looks_like_path:
                continue
            path = Path(token)
            resolved = path.resolve() if path.is_absolute() else (cwd / path).resolve()
            try:
                resolved.relative_to(self.approved_directory)
            except ValueError as exc:
                raise ValueError(
                    f"command path is outside approved directory: {token}"
                ) from exc

    def start(self, command: str, cwd: str, name: str = "") -> ProcessEntry:
        """Start a background process."""
        resolved_cwd = self._validate_cwd(cwd)
        self._validate_command_paths(command, resolved_cwd)

        state = self._load_state()
        proc_id = state["next_id"]
        state["next_id"] = proc_id + 1

        log_path = self.logs_dir / f"{proc_id}.log"

        with open(log_path, "w") as log_file:
            proc = subprocess.Popen(
                command,
                shell=True,
                cwd=str(resolved_cwd),
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
            cwd=str(resolved_cwd),
            pid=proc.pid,
            started_at=time.time(),
            log_dir=str(self.logs_dir),
        )

        entry_dict = asdict(entry)
        entry_dict["manually_stopped"] = False
        state["processes"][str(proc_id)] = entry_dict
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

        # Позначити як зупинений вручну — не відновлювати при рестарті
        state = self._load_state()
        key = str(proc_id)
        if key in state["processes"]:
            state["processes"][key]["manually_stopped"] = True
            self._save_state(state)

        logger.info("Process #%d killed (pid=%d)", proc_id, entry.pid)
        return entry

    def remove(self, proc_id: int) -> bool:
        state = self._load_state()
        key = str(proc_id)
        if key in state["processes"]:
            del state["processes"][key]
            self._save_state(state)
            log = self.logs_dir / f"{proc_id}.log"
            if log.exists():
                log.unlink()
            return True
        return False

    def cleanup_dead(self) -> int:
        state = self._load_state()
        dead = [
            k
            for k, d in state["processes"].items()
            if not self._entry_from_dict(d).is_alive
        ]
        for k in dead:
            log = self.logs_dir / f"{k}.log"
            if log.exists():
                log.unlink()
            del state["processes"][k]
        if dead:
            self._save_state(state)
        return len(dead)

    def restore(self) -> list[ProcessEntry]:
        """Restart processes that died from server restart (not manually killed).

        Processes killed via kill() are marked manually_stopped=True and skipped.
        All other dead processes are re-launched with the same command/cwd/name.
        """
        state = self._load_state()
        restored: list[ProcessEntry] = []

        for key, d in list(state["processes"].items()):
            entry = self._entry_from_dict(d)

            if entry.is_alive:
                continue

            if d.get("manually_stopped", False):
                continue

            logger.info(
                "Restoring process #%d '%s': %s", entry.id, entry.name, entry.command
            )

            log_path = self.logs_dir / f"{entry.id}.log"
            try:
                with open(log_path, "a") as log_file:
                    log_file.write(
                        f"\n--- Restored at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n"
                    )
                    proc = subprocess.Popen(
                        entry.command,
                        shell=True,
                        cwd=entry.cwd,
                        stdout=log_file,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )

                state["processes"][key]["pid"] = proc.pid
                state["processes"][key]["started_at"] = time.time()
                restored.append(self._entry_from_dict(state["processes"][key]))

                logger.info("Process #%d restored: new pid=%d", entry.id, proc.pid)
            except Exception as exc:
                logger.error("Failed to restore process #%d: %s", entry.id, exc)

        if restored:
            self._save_state(state)

        return restored
