"""MCP server exposing process management tools to Claude Code."""

import os

from fastmcp import FastMCP

from src.process.manager import ProcessManager

mcp = FastMCP("process-manager")
pm = ProcessManager()


@mcp.tool()
def process_run(command: str, cwd: str = "", name: str = "") -> str:
    """Start a background process that persists across sessions.

    Args:
        command: Shell command to run (e.g. "python bot.py", "npm start")
        cwd: Working directory (default: /project)
        name: Human-readable name for the process
    """
    if not cwd:
        cwd = os.environ.get("PROCESS_APPROVED_DIRECTORY") or "/project"
    entry = pm.start(command, cwd, name)
    return (
        f"Process #{entry.id} '{entry.name}' started\n"
        f"PID: {entry.pid}\n"
        f"Dir: {entry.cwd}\n"
        f"Cmd: {entry.command}\n"
        f"Logs: /logs {entry.id}"
    )


@mcp.tool()
def process_ps() -> str:
    """List all managed background processes (across all projects)."""
    procs = pm.list_all()
    if not procs:
        return "No processes."

    lines = []
    for p in procs:
        icon = "🟢" if p.is_alive else "🔴"
        lines.append(
            f"{icon} #{p.id} '{p.name}' [{p.status}] {p.uptime}\n"
            f"   cmd: {p.command}\n"
            f"   dir: {p.cwd}\n"
            f"   pid: {p.pid}"
        )
    return "\n\n".join(lines)


@mcp.tool()
def process_kill(process_id: int) -> str:
    """Kill a background process by its ID.

    Args:
        process_id: Process ID (from process_ps output)
    """
    entry = pm.kill(process_id)
    if entry:
        return f"Process #{process_id} '{entry.name}' killed."
    return f"Process #{process_id} not found."


@mcp.tool()
def process_logs(process_id: int, lines: int = 50) -> str:
    """View recent output of a background process.

    Args:
        process_id: Process ID (from process_ps output)
        lines: Number of lines to show (default 50)
    """
    entry = pm.get(process_id)
    if not entry:
        return f"Process #{process_id} not found."

    logs = entry.last_logs(lines)
    return f"=== Logs #{process_id} '{entry.name}' [{entry.status}] ===\n{logs}"


@mcp.tool()
def process_cleanup() -> str:
    """Remove all dead/stopped processes from the list."""
    count = pm.cleanup_dead()
    return f"Cleaned up {count} dead process(es)."


@mcp.tool()
def process_restore() -> str:
    """Restart processes that died from server restart (not manually killed).

    Processes stopped via process_kill are NOT restored.
    Only processes that died unexpectedly (e.g. deploy, crash) are restarted.
    """
    restored = pm.restore()
    if not restored:
        return "No processes to restore (all alive or manually stopped)."

    lines = []
    for p in restored:
        lines.append(f"🔄 #{p.id} '{p.name}' restored (new pid: {p.pid})")
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
