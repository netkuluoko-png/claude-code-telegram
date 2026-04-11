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

# Create .mcp.json ONLY if it doesn't exist yet (don't overwrite user changes)
for dir in /project /project/*/; do
    if [ -d "$dir" ] && [ ! -f "$dir/.mcp.json" ]; then
        cat > "$dir/.mcp.json" << 'MCPEOF'
{
  "mcpServers": {
    "process-manager": {
      "command": "python",
      "args": ["-m", "src.process.mcp_server"],
      "cwd": "/app"
    }
  }
}
MCPEOF
    fi
done

chown -R claude:claude /project

# Run as claude user
exec su claude -c "python -c 'from src.main import run; run()'"
