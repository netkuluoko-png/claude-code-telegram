#!/bin/bash
# Restore Claude Code OAuth credentials on startup

if [ -n "$CLAUDE_CREDENTIALS_B64" ]; then
    mkdir -p ~/.claude
    echo "$CLAUDE_CREDENTIALS_B64" | base64 -d > ~/.claude/.credentials.json
    echo "Claude credentials restored"
else
    echo "WARNING: CLAUDE_CREDENTIALS_B64 not set"
fi

# Create data dir if needed
mkdir -p /app/data

exec python -c "from src.main import run; run()"
