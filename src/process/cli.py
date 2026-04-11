#!/usr/bin/env python3
"""CLI process manager — used by Claude Code via Bash tool.

Usage:
    python -m src.process.cli run "python bot.py"
    python -m src.process.cli run --name my-bot "python bot.py"
    python -m src.process.cli run --cwd /project/MyApp "python main.py"
    python -m src.process.cli ps
    python -m src.process.cli kill 1
    python -m src.process.cli logs 1
    python -m src.process.cli cleanup
"""

import sys

from src.process.manager import ProcessManager


def main() -> None:
    pm = ProcessManager()
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        return

    cmd = args[0]

    if cmd == "run":
        name = ""
        cwd = "/project"
        rest = args[1:]

        while rest:
            if rest[0] == "--name" and len(rest) > 1:
                name = rest[1]
                rest = rest[2:]
            elif rest[0] == "--cwd" and len(rest) > 1:
                cwd = rest[1]
                rest = rest[2:]
            else:
                break

        command = " ".join(rest)
        if not command:
            print("Error: no command specified")
            print("Usage: procmgr run [--name NAME] [--cwd DIR] <command>")
            sys.exit(1)

        entry = pm.start(command, cwd, name)
        print(f"Started process #{entry.id} '{entry.name}'")
        print(f"  PID: {entry.pid}")
        print(f"  Dir: {entry.cwd}")
        print(f"  Cmd: {entry.command}")

    elif cmd == "ps":
        procs = pm.list_all()
        if not procs:
            print("No processes.")
            return
        for p in procs:
            icon = "🟢" if p.is_alive else "🔴"
            print(f"{icon} #{p.id} '{p.name}' [{p.status}] {p.uptime}")
            print(f"   cmd: {p.command}")
            print(f"   dir: {p.cwd}")
            print(f"   pid: {p.pid}")
            print()

    elif cmd == "kill":
        if len(args) < 2:
            print("Usage: procmgr kill <id>")
            sys.exit(1)
        proc_id = int(args[1])
        entry = pm.kill(proc_id)
        if entry:
            print(f"Killed #{proc_id} '{entry.name}'")
        else:
            print(f"Process #{proc_id} not found")
            sys.exit(1)

    elif cmd == "logs":
        if len(args) < 2:
            print("Usage: procmgr logs <id> [lines]")
            sys.exit(1)
        proc_id = int(args[1])
        lines = int(args[2]) if len(args) > 2 else 50
        entry = pm.get(proc_id)
        if not entry:
            print(f"Process #{proc_id} not found")
            sys.exit(1)
        print(f"=== #{proc_id} '{entry.name}' [{entry.status}] ===")
        print(entry.last_logs(lines))

    elif cmd == "cleanup":
        count = pm.cleanup_dead()
        print(f"Removed {count} dead process(es)")

    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
