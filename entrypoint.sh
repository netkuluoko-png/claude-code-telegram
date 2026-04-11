#!/bin/bash
# Restore Claude Code OAuth credentials on startup

if [ -n "$CLAUDE_CREDENTIALS_B64" ]; then
    mkdir -p /home/claude/.claude
    echo "$CLAUDE_CREDENTIALS_B64" | base64 -d > /home/claude/.claude/.credentials.json
    chown -R claude:claude /home/claude/.claude
    echo "Claude credentials restored"
else
    echo "WARNING: CLAUDE_CREDENTIALS_B64 not set"
fi

# Fix volume permissions (Railway volumes mount as root)
mkdir -p /app/data /app/data/proc_logs
chown -R claude:claude /app/data

# Configure MCP servers via .claude/settings.json (project-level)
# Claude CLI reads this when setting_sources=["project"]
for dir in /project /project/*/; do
    if [ -d "$dir" ]; then
        mkdir -p "$dir/.claude"
        cat > "$dir/.claude/settings.json" << 'MCPEOF'
{
  "permissions": {
    "allow": ["mcp__process-manager__*"]
  },
  "mcpServers": {
    "process-manager": {
      "command": "python",
      "args": ["-m", "src.process.mcp_server"],
      "cwd": "/app",
      "env": {
        "PYTHONPATH": "/app"
      }
    }
  }
}
MCPEOF
    fi
done

chown -R claude:claude /project

# Also set user-level MCP config for claude user as fallback
mkdir -p /home/claude/.claude
cat > /home/claude/.claude/settings.json << 'MCPEOF'
{
  "permissions": {
    "allow": ["mcp__process-manager__*"]
  },
  "mcpServers": {
    "process-manager": {
      "command": "python",
      "args": ["-m", "src.process.mcp_server"],
      "cwd": "/app",
      "env": {
        "PYTHONPATH": "/app"
      }
    }
  }
}
MCPEOF
chown -R claude:claude /home/claude/.claude

# Enable process manager MCP by default (Railway/Docker env vars take precedence)
export ENABLE_MCP="${ENABLE_MCP:-true}"
export MCP_CONFIG_PATH="${MCP_CONFIG_PATH:-/app/mcp-process.json}"

# Restore background processes from previous session (if any survived deploy)
echo "Checking for processes to restore..."
su claude -c "cd /app && PYTHONPATH=/app python -c '
from src.process.manager import ProcessManager
pm = ProcessManager()
restored = pm.restore()
if restored:
    print(f\"Restored {len(restored)} process(es):\")
    for p in restored:
        print(f\"  #{p.id} {p.name!r} -> pid {p.pid}\")
else:
    print(\"No processes to restore.\")
'"

# Run as claude user
exec su claude -c "python -c 'from src.main import run; run()'"
