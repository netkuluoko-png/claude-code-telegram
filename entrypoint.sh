#!/bin/bash
# Restore Claude Code OAuth credentials on startup
#
# Credentials live on the persistent volume at /app/data/.claude/.credentials.json
# so that OAuth refresh tokens (which rotate on every refresh) survive container
# restarts. CLAUDE_CREDENTIALS_B64 is only used as a one-time seed when the volume
# is empty (first deploy / fresh volume).

# Fix volume permissions (Railway volumes mount as root)
mkdir -p /app/data /app/data/proc_logs /app/data/.claude /app/data/.claude/projects /app/data/project
chown -R claude:claude /app/data

# Seed /app/data/project from the image's tarball the first time the volume
# is empty, then symlink /project to the volume. This way user edits under
# /project persist across deploys (previously /project was rebuilt from the
# tarball every deploy, wiping local changes).
PROJECT_VOLUME=/app/data/project
PROJECT_SEED=/app/project-seed.tar.gz
if [ -z "$(ls -A "$PROJECT_VOLUME" 2>/dev/null)" ] && [ -f "$PROJECT_SEED" ]; then
    echo "Seeding $PROJECT_VOLUME from $PROJECT_SEED (first deploy on fresh volume)"
    tar xzf "$PROJECT_SEED" -C "$PROJECT_VOLUME/"
    chown -R claude:claude "$PROJECT_VOLUME"
fi

# Replace /project with a symlink to the volume.
# If /project already exists as a real directory (from the old image), remove it.
if [ -e /project ] && [ ! -L /project ]; then
    rm -rf /project
fi
ln -sfn "$PROJECT_VOLUME" /project
chown -h claude:claude /project

CREDS_PERSISTENT=/app/data/.claude/.credentials.json
CREDS_LIVE=/home/claude/.claude/.credentials.json
SEED_HASH_FILE=/app/data/.claude/.seed_hash

mkdir -p /home/claude/.claude

# Persist Claude CLI session transcripts across deploys.
# Claude CLI writes ~/.claude/projects/<sanitized-cwd>/<session-id>.jsonl;
# without this symlink the transcripts live on the ephemeral container fs,
# so every redeploy wipes them and /resume finds session_id in bot.db but
# Claude itself can't restore the conversation → silent fresh session.
# Migrate any transcripts that the previous (non-symlinked) container left
# behind, then replace the directory with a symlink to the volume.
if [ -d /home/claude/.claude/projects ] && [ ! -L /home/claude/.claude/projects ]; then
    if [ -n "$(ls -A /home/claude/.claude/projects 2>/dev/null)" ]; then
        echo "Migrating existing ~/.claude/projects transcripts to volume"
        cp -an /home/claude/.claude/projects/. /app/data/.claude/projects/ 2>/dev/null || true
    fi
    rm -rf /home/claude/.claude/projects
fi
ln -sfn /app/data/.claude/projects /home/claude/.claude/projects
chown -h claude:claude /home/claude/.claude/projects
chown -R claude:claude /app/data/.claude/projects

# Reseed when CLAUDE_CREDENTIALS_B64 is set AND its hash differs from last seed.
# This lets the operator force-replace bad credentials by rotating the env var,
# while normal restarts keep refreshed tokens that SDK wrote to the volume.
if [ -n "$CLAUDE_CREDENTIALS_B64" ]; then
    NEW_HASH=$(printf '%s' "$CLAUDE_CREDENTIALS_B64" | sha256sum | awk '{print $1}')
    OLD_HASH=$(cat "$SEED_HASH_FILE" 2>/dev/null || echo "")
    if [ "$NEW_HASH" != "$OLD_HASH" ] || [ ! -s "$CREDS_PERSISTENT" ]; then
        echo "Seeding Claude credentials from CLAUDE_CREDENTIALS_B64 (hash changed or missing)"
        echo "$CLAUDE_CREDENTIALS_B64" | base64 -d > "$CREDS_PERSISTENT"
        printf '%s' "$NEW_HASH" > "$SEED_HASH_FILE"
    else
        echo "Using existing Claude credentials from volume (env hash unchanged)"
    fi
elif [ -s "$CREDS_PERSISTENT" ]; then
    echo "Using existing Claude credentials from volume (no env var set)"
else
    echo "WARNING: no credentials on volume and CLAUDE_CREDENTIALS_B64 not set"
fi

chown claude:claude "$CREDS_PERSISTENT" 2>/dev/null || true
chmod 600 "$CREDS_PERSISTENT" 2>/dev/null || true

# Symlink the live path to the volume so SDK refreshes write to persistent storage
rm -f "$CREDS_LIVE"
ln -s "$CREDS_PERSISTENT" "$CREDS_LIVE"
chown -h claude:claude "$CREDS_LIVE"
chown -R claude:claude /home/claude/.claude

# Merge MCP servers into .claude/settings.json for all project directories
# Uses Python to MERGE into existing settings (preserving permissions, hooks, etc.)
# instead of overwriting them
for dir in /project /project/*/; do
    if [ -d "$dir" ]; then
        mkdir -p "$dir/.claude"
        python3 -c "
import json, pathlib, sys
settings_path = pathlib.Path('$dir/.claude/settings.json')
existing = {}
if settings_path.exists():
    try:
        existing = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError):
        existing = {}

mcp_perms = ['mcp__process-manager__*', 'mcp__telegram__*', 'mcp__mcp-scheduler__*']
allow = existing.setdefault('permissions', {}).setdefault('allow', [])
for p in mcp_perms:
    if p not in allow:
        allow.append(p)

existing['mcpServers'] = {
    'process-manager': {
        'command': 'python', 'args': ['-m', 'src.process.mcp_server'],
        'cwd': '/app', 'env': {'PYTHONPATH': '/app'}
    },
    'telegram': {
        'command': 'python', 'args': ['-m', 'src.mcp.telegram_server'],
        'cwd': '/app', 'env': {'PYTHONPATH': '/app'}
    },
    'mcp-scheduler': {
        'command': 'python', 'args': ['-m', 'src.scheduler_mcp.mcp_server'],
        'cwd': '/app',
        'env': {'PYTHONPATH': '/app', 'SCHEDULER_DB_PATH': '/app/data/bot.db'}
    }
}
settings_path.write_text(json.dumps(existing, indent=2))
print(f'MCP merged into {settings_path}')
"
    fi
done

chown -R claude:claude /project

# Also set user-level MCP config for claude user as fallback
mkdir -p /home/claude/.claude
python3 -c "
import json, pathlib
settings_path = pathlib.Path('/home/claude/.claude/settings.json')
existing = {}
if settings_path.exists():
    try:
        existing = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, OSError):
        existing = {}

mcp_perms = ['mcp__process-manager__*', 'mcp__telegram__*', 'mcp__mcp-scheduler__*']
allow = existing.setdefault('permissions', {}).setdefault('allow', [])
for p in mcp_perms:
    if p not in allow:
        allow.append(p)

existing['mcpServers'] = {
    'process-manager': {
        'command': 'python', 'args': ['-m', 'src.process.mcp_server'],
        'cwd': '/app', 'env': {'PYTHONPATH': '/app'}
    },
    'telegram': {
        'command': 'python', 'args': ['-m', 'src.mcp.telegram_server'],
        'cwd': '/app', 'env': {'PYTHONPATH': '/app'}
    },
    'mcp-scheduler': {
        'command': 'python', 'args': ['-m', 'src.scheduler_mcp.mcp_server'],
        'cwd': '/app',
        'env': {'PYTHONPATH': '/app', 'SCHEDULER_DB_PATH': '/app/data/bot.db'}
    }
}
settings_path.write_text(json.dumps(existing, indent=2))
print(f'MCP merged into {settings_path}')
"
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
