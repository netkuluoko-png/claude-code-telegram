# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Process Manager

Background processes persist across Claude sessions. Use MCP tools to manage them:

- `process_run(command, cwd, name)` — start a background process
- `process_ps()` — list all managed processes
- `process_logs(process_id, lines)` — view process output
- `process_kill(process_id)` — stop a process
- `process_cleanup()` — remove dead processes

Always use the process manager instead of running servers directly — direct processes die when the session ends.

## Telegram Tools

Send files and images directly to the user's Telegram chat:

- `send_file_to_user(file_path, caption)` — send any file as a document attachment. Use when the user asks to receive, download, or get a file from the server. The file_path must be absolute and within the approved working directory. Max 50 MB.
- `send_image_to_user(file_path, caption)` — send an image with inline preview. Supported formats: png, jpg, jpeg, gif, webp, bmp, svg.

Both tools validate the file and queue it for delivery — the actual Telegram message is sent automatically after your response.

## Project Overview

Telegram bot providing remote access to Claude Code. Python 3.10+, built with Poetry, using `python-telegram-bot` for Telegram and `claude-agent-sdk` for Claude Code integration.

## Commands

```bash
make dev              # Install all deps (including dev)
make install          # Production deps only
make run              # Run the bot
make run-debug        # Run with debug logging
make test             # Run tests with coverage
make lint             # Black + isort + flake8 + mypy
make format           # Auto-format with black + isort

# Run a single test
poetry run pytest tests/unit/test_config.py -k test_name -v

# Type checking only
poetry run mypy src
```

## Architecture

### Claude SDK Integration

`ClaudeIntegration` (facade in `src/claude/facade.py`) wraps `ClaudeSDKManager` (`src/claude/sdk_integration.py`), which uses `claude-agent-sdk` with `ClaudeSDKClient` for async streaming. Session IDs come from Claude's `ResultMessage`, not generated locally.

Sessions auto-resume: per user+directory, persisted in SQLite.

### Request Flow

**Agentic mode** (default, `AGENTIC_MODE=true`):

```
Telegram message -> Security middleware (group -3) -> Auth middleware (group -2)
-> Rate limit (group -1) -> MessageOrchestrator.agentic_text() (group 10)
-> ClaudeIntegration.run_command() -> SDK
-> Response parsed -> Stored in SQLite -> Sent back to Telegram
```

**External triggers** (webhooks, scheduler):

```
Webhook POST /webhooks/{provider} -> Signature verification -> Deduplication
-> Publish WebhookEvent to EventBus -> AgentHandler.handle_webhook()
-> ClaudeIntegration.run_command() -> Publish AgentResponseEvent
-> NotificationService -> Rate-limited Telegram delivery
```

**Classic mode** (`AGENTIC_MODE=false`): Same middleware chain, but routes through full command/message handlers in `src/bot/handlers/` with 13 commands and inline keyboards.

### Dependency Injection

Bot handlers access dependencies via `context.bot_data`:
```python
context.bot_data["auth_manager"]
context.bot_data["claude_integration"]
context.bot_data["storage"]
context.bot_data["security_validator"]
```

### Key Directories

- `src/config/` -- Pydantic Settings v2 config with env detection, feature flags (`features.py`), YAML project loader (`loader.py`)
- `src/bot/handlers/` -- Telegram command, message, and callback handlers (classic mode + project thread commands)
- `src/bot/middleware/` -- Auth, rate limit, security input validation
- `src/bot/features/` -- Git integration, file handling, quick actions, session export
- `src/bot/orchestrator.py` -- MessageOrchestrator: routes to agentic or classic handlers, project-topic routing
- `src/claude/` -- Claude integration facade, SDK/CLI managers, session management, tool monitoring
- `src/projects/` -- Multi-project support: `registry.py` (YAML project config), `thread_manager.py` (Telegram topic sync/routing)
- `src/storage/` -- SQLite via aiosqlite, repository pattern (users, sessions, messages, tool_usage, audit_log, cost_tracking, project_threads)
- `src/security/` -- Multi-provider auth (whitelist + token), input validators (with optional `disable_security_patterns`), rate limiter, audit logging
- `src/events/` -- EventBus (async pub/sub), event types, AgentHandler, EventSecurityMiddleware
- `src/api/` -- FastAPI webhook server, GitHub HMAC-SHA256 + Bearer token auth
- `src/scheduler/` -- APScheduler cron jobs, persistent storage in SQLite
- `src/notifications/` -- NotificationService, rate-limited Telegram delivery

### Security Model

5-layer defense: authentication (whitelist/token) -> directory isolation (APPROVED_DIRECTORY + path traversal prevention) -> input validation (blocks `..`, `;`, `&&`, `$()`, etc.) -> rate limiting (token bucket) -> audit logging.

`SecurityValidator` blocks access to secrets (`.env`, `.ssh`, `id_rsa`, `.pem`) and dangerous shell patterns. Can be relaxed with `DISABLE_SECURITY_PATTERNS=true` (trusted environments only).

`ToolMonitor` validates Claude's tool calls against allowlist/disallowlist, file path boundaries, and dangerous bash patterns. Tool name validation can be bypassed with `DISABLE_TOOL_VALIDATION=true`.

Webhook authentication: GitHub HMAC-SHA256 signature verification, generic Bearer token for other providers, atomic deduplication via `webhook_events` table.

### MCP Configuration

MCP tools auto-register in every Claude session. The merge pipeline (implemented in `ClaudeSDKManager._build_mcp_servers` + `execute_command`):

1. **Project-local** — `<cwd>/.mcp.json` and `<cwd>/.claude/settings.json` (`mcpServers` block). Entries with a missing `cwd`, a Windows drive-letter path, or a `Path(arg)` that doesn't exist on Linux are dropped via `_mcp_config_skip_reason` with a `Skipping invalid project MCP entry` warning.
2. **Bot-owned** — `/app/mcp-process.json` auto-discovered from the image root. Keys declared here always win over project-local entries with the same name (so a project can't shadow `process-manager`/`telegram`).
3. **User-configured** — `MCP_CONFIG_PATH` JSON merged on top when `ENABLE_MCP=true`.

The merged dict is passed to `ClaudeAgentOptions.mcp_servers` **and** `extra_args={"strict-mcp-config": None, "debug-to-stderr": None}` — `--strict-mcp-config` forces the CLI to use only our merged list and ignore any native `.mcp.json` discovery, which eliminates the "launched but not connected" approval-limbo state. `--debug-to-stderr` surfaces server spawn failures through our `_stderr_callback`.

Per working directory the bot also:
- Rewrites `.claude/settings.json` with the merged `mcpServers`, `mcp__<name>__*` permissions, `enabledMcpjsonServers`, and `enableAllProjectMcpServers: false` (`_ensure_mcp_settings`).
- Stamps `~/.claude.json` → `projects.<cwd>` with `hasTrustDialogAccepted: true` (`_approve_project_mcps`).
- Regenerates `.claude/rules/mcp-guide.md` (`_ensure_mcp_rules`).

**Project MCP dependency auto-install** (`_ensure_mcp_deps`): for every project-local MCP whose `cwd` contains a `requirements.txt`, the bot runs `pip install --target /app/data/.mcp_deps/<name>-<sha256-12>` once per requirements-hash, then prepends the target to the subprocess's `PYTHONPATH`. The cache lives on the `/app/data` volume, so installs survive deploys. Same algorithm fires before `/mcp` inspection.

#### Adding a new **bot-owned** MCP server

1. Create a FastMCP server in `src/mcp/` (see `src/process/mcp_server.py`). **Import: `from fastmcp import FastMCP`** (NOT `from mcp.server.fastmcp` — that package is not installed). **Use `mcp.run(transport="stdio")`** — the CLI speaks stdio, not HTTP.
2. Register it in `mcp-process.json` under `mcpServers`. **Must include `env.PYTHONPATH=/app`** — the CLI ignores `cwd` for module resolution:
   ```json
   {
     "mcpServers": {
       "my-server": {
         "command": "python",
         "args": ["-m", "src.mcp.my_server"],
         "cwd": "/app",
         "env": { "PYTHONPATH": "/app" }
       }
     }
   }
   ```
3. Mirror the same entry in `entrypoint.sh` (both `/project/*/` and `/home/claude/.claude/settings.json` blocks) so existing project folders on the persistent volume pick up the new server on restart.
4. Deploy — new sessions see the tools automatically.

#### Adding a **project-local** MCP server (no bot code change)

1. Put `server.py` anywhere in the project folder. Use `from fastmcp import FastMCP` + `mcp.run(transport="stdio")`.
2. Create `.mcp.json` at the project root:
   ```json
   {
     "mcpServers": {
       "my-proj-mcp": {
         "command": "python",
         "args": ["server.py"],
         "cwd": "/app/data/project/<project-name>/<server-dir>"
       }
     }
   }
   ```
3. Drop a `requirements.txt` next to `server.py` listing every third-party import. The bot auto-installs it on the next `/mcp` inspection or Claude session.
4. Use Linux paths only — Windows drive letters are filtered out.

**Do NOT** edit `CLAUDE.md` with bash fallback commands as a workaround. If MCP tools are missing, fix `mcp-process.json` (bot-owned) or the project's `.mcp.json` + `requirements.txt` (project-local).

### Configuration

Settings loaded from environment variables via Pydantic Settings. Required: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_BOT_USERNAME`, `APPROVED_DIRECTORY`. Key optional: `ALLOWED_USERS` (comma-separated Telegram IDs), `ANTHROPIC_API_KEY`, `ENABLE_MCP`, `MCP_CONFIG_PATH`.

Agentic platform settings: `AGENTIC_MODE` (default true), `ENABLE_API_SERVER`, `API_SERVER_PORT` (default 8080), `GITHUB_WEBHOOK_SECRET`, `WEBHOOK_API_SECRET`, `ENABLE_SCHEDULER`, `NOTIFICATION_CHAT_IDS`.

Security relaxation (trusted environments only): `DISABLE_SECURITY_PATTERNS` (default false), `DISABLE_TOOL_VALIDATION` (default false).

Multi-project topics: `ENABLE_PROJECT_THREADS` (default false), `PROJECT_THREADS_MODE` (`private`|`group`), `PROJECT_THREADS_CHAT_ID` (required for group mode), `PROJECTS_CONFIG_PATH` (path to YAML project registry), `PROJECT_THREADS_SYNC_ACTION_INTERVAL_SECONDS` (default `1.1`, set `0` to disable pacing). See `config/projects.example.yaml`.

Output verbosity: `VERBOSE_LEVEL` (default 1, range 0-2). Controls how much of Claude's background activity is shown to the user in real-time. 0 = quiet (only final response, typing indicator still active), 1 = normal (tool names + reasoning snippets shown during execution), 2 = detailed (tool names with input summaries + longer reasoning text). Users can override per-session via `/verbose 0|1|2`. A persistent typing indicator is refreshed every ~2 seconds at all levels.

Voice transcription: `ENABLE_VOICE_MESSAGES` (default true), `VOICE_PROVIDER` (`mistral`|`openai`|`local`, default `mistral`), `MISTRAL_API_KEY`, `OPENAI_API_KEY`, `VOICE_TRANSCRIPTION_MODEL`. For local provider: `WHISPER_CPP_BINARY_PATH`, `WHISPER_CPP_MODEL_PATH` (requires ffmpeg + whisper.cpp installed). Provider implementation is in `src/bot/features/voice_handler.py`.

Feature flags in `src/config/features.py` control: MCP, git integration, file uploads, quick actions, session export, image uploads, voice messages, conversation mode, agentic mode, API server, scheduler.

### DateTime Convention

All datetimes use timezone-aware UTC: `datetime.now(UTC)` (not `datetime.utcnow()`). SQLite adapters auto-convert TIMESTAMP/DATETIME columns to `datetime` objects via `detect_types=PARSE_DECLTYPES`. Model `from_row()` methods must guard `fromisoformat()` calls with `isinstance(val, str)` checks.

## Code Style

- Black (88 char line length), isort (black profile), flake8, mypy strict, autoflake for unused imports
- pytest-asyncio with `asyncio_mode = "auto"`
- structlog for all logging (JSON in prod, console in dev)
- Type hints required on all functions (`disallow_untyped_defs = true`)
- Use `datetime.now(UTC)` not `datetime.utcnow()` (deprecated)

## Adding a New Bot Command

### Agentic mode

Agentic mode commands: `/start`, `/new`, `/status`, `/verbose`, `/repo`. If `ENABLE_PROJECT_THREADS=true`: `/sync_threads`. To add a new command:

1. Add handler function in `src/bot/orchestrator.py`
2. Register in `MessageOrchestrator._register_agentic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()` for Telegram's command menu
4. Add audit logging for the command

### Classic mode

1. Add handler function in `src/bot/handlers/command.py`
2. Register in `MessageOrchestrator._register_classic_handlers()`
3. Add to `MessageOrchestrator.get_bot_commands()` for Telegram's command menu
4. Add audit logging for the command
