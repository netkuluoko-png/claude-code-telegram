"""Message orchestrator — single entry point for all Telegram updates.

Routes messages based on agentic vs classic mode. In agentic mode, provides
a minimal conversational interface (3 commands, no inline keyboards). In
classic mode, delegates to existing full-featured handlers.
"""

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..claude.sdk_integration import StreamUpdate
from ..config.settings import Settings
from ..projects import PrivateTopicsUnavailableError
from .utils.draft_streamer import DraftStreamer, generate_draft_id
from .utils.html_format import escape_html
from .utils.file_extractor import FileAttachment, validate_file_path
from .utils.image_extractor import (
    ImageAttachment,
    should_send_as_photo,
    validate_image_path,
)

logger = structlog.get_logger()

_MEDIA_TYPE_MAP = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}

# Patterns that look like secrets/credentials in CLI arguments
_SECRET_PATTERNS: List[re.Pattern[str]] = [
    # API keys / tokens (sk-ant-..., sk-..., ghp_..., gho_..., github_pat_..., xoxb-...)
    re.compile(
        r"(sk-ant-api\d*-[A-Za-z0-9_-]{10})[A-Za-z0-9_-]*"
        r"|(sk-[A-Za-z0-9_-]{20})[A-Za-z0-9_-]*"
        r"|(ghp_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(gho_[A-Za-z0-9]{5})[A-Za-z0-9]*"
        r"|(github_pat_[A-Za-z0-9_]{5})[A-Za-z0-9_]*"
        r"|(xoxb-[A-Za-z0-9]{5})[A-Za-z0-9-]*"
    ),
    # AWS access keys
    re.compile(r"(AKIA[0-9A-Z]{4})[0-9A-Z]{12}"),
    # Generic long hex/base64 tokens after common flags/env patterns
    re.compile(
        r"((?:--token|--secret|--password|--api-key|--apikey|--auth)"
        r"[= ]+)['\"]?[A-Za-z0-9+/_.:-]{8,}['\"]?"
    ),
    # Inline env assignments like KEY=value
    re.compile(
        r"((?:TOKEN|SECRET|PASSWORD|API_KEY|APIKEY|AUTH_TOKEN|PRIVATE_KEY"
        r"|ACCESS_KEY|CLIENT_SECRET|WEBHOOK_SECRET)"
        r"=)['\"]?[^\s'\"]{8,}['\"]?"
    ),
    # Bearer / Basic auth headers
    re.compile(r"(Bearer )[A-Za-z0-9+/_.:-]{8,}" r"|(Basic )[A-Za-z0-9+/=]{8,}"),
    # Connection strings with credentials  user:pass@host
    re.compile(r"://([^:]+:)[^@]{4,}(@)"),
]


def _redact_secrets(text: str) -> str:
    """Replace likely secrets/credentials with redacted placeholders."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub(
            lambda m: next((g + "***" for g in m.groups() if g is not None), "***"),
            result,
        )
    return result


# Tool name -> friendly emoji mapping for verbose output
_TOOL_ICONS: Dict[str, str] = {
    "Read": "\U0001f4d6",
    "Write": "\u270f\ufe0f",
    "Edit": "\u270f\ufe0f",
    "MultiEdit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb",
    "Glob": "\U0001f50d",
    "Grep": "\U0001f50d",
    "LS": "\U0001f4c2",
    "Task": "\U0001f9e0",
    "TaskOutput": "\U0001f9e0",
    "WebFetch": "\U0001f310",
    "WebSearch": "\U0001f310",
    "NotebookRead": "\U0001f4d3",
    "NotebookEdit": "\U0001f4d3",
    "TodoRead": "\u2611\ufe0f",
    "TodoWrite": "\u2611\ufe0f",
}


def _tool_icon(name: str) -> str:
    """Return emoji for a tool, with a default wrench."""
    return _TOOL_ICONS.get(name, "\U0001f527")


# Available Claude models for /model command
# 1M context models first (primary), then standard context
_AVAILABLE_MODELS: List[Dict[str, str]] = [
    {
        "id": "claude-opus-4-7[1m]",
        "label": "Opus 4.7 (1M)",
        "desc": "Newest & smartest, 1M context",
    },
    {"id": "claude-opus-4-7", "label": "Opus 4.7", "desc": "Newest & smartest"},
    {
        "id": "claude-sonnet-4-6[1m]",
        "label": "Sonnet 4.6 (1M)",
        "desc": "Fast & capable, 1M context",
    },
    {
        "id": "claude-opus-4-6[1m]",
        "label": "Opus 4.6 (1M)",
        "desc": "Intelligent, 1M context",
    },
    {"id": "claude-sonnet-4-6", "label": "Sonnet 4.6", "desc": "Fast & capable"},
    {"id": "claude-opus-4-6", "label": "Opus 4.6", "desc": "Intelligent"},
    {
        "id": "claude-haiku-4-5-20251001",
        "label": "Haiku 4.5",
        "desc": "Fastest & cheapest",
    },
]

_AGENT_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex",
}


@dataclass
class ActiveRequest:
    """Tracks an in-flight Claude request so it can be interrupted."""

    user_id: int
    interrupt_event: asyncio.Event = field(default_factory=asyncio.Event)
    interrupted: bool = False
    progress_msg: Any = None  # telegram Message object


class MessageOrchestrator:
    """Routes messages based on mode. Single entry point for all Telegram updates."""

    def __init__(self, settings: Settings, deps: Dict[str, Any]):
        self.settings = settings
        self.deps = deps
        self._active_requests: Dict[int, ActiveRequest] = {}
        self._known_commands: frozenset[str] = frozenset()

    def _inject_deps(self, handler: Callable) -> Callable:  # type: ignore[type-arg]
        """Wrap handler to inject dependencies into context.bot_data."""

        async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
            for key, value in self.deps.items():
                context.bot_data[key] = value
            context.bot_data["settings"] = self.settings
            context.user_data.pop("_thread_context", None)
            self._apply_user_workspace(update, context)

            is_sync_bypass = handler.__name__ == "sync_threads"
            is_start_bypass = handler.__name__ in {"start_command", "agentic_start"}
            message_thread_id = self._extract_message_thread_id(update)
            should_enforce = self.settings.enable_project_threads

            if should_enforce:
                if self.settings.project_threads_mode == "private":
                    should_enforce = not is_sync_bypass and not (
                        is_start_bypass and message_thread_id is None
                    )
                else:
                    should_enforce = not is_sync_bypass

            if should_enforce:
                allowed = await self._apply_thread_routing_context(update, context)
                if not allowed:
                    return
                self._apply_user_workspace(update, context)

            if update.effective_user and "agent_backend" not in context.user_data:
                persisted = self._load_persisted_agent_backend(update.effective_user.id)
                if persisted:
                    context.user_data["agent_backend"] = persisted

            self._activate_agent_backend(context)

            try:
                await handler(update, context)
            finally:
                self._persist_active_agent_session(context)
                if should_enforce:
                    self._persist_thread_state(context)

        return wrapped

    def _approved_directory_for_user(self, user_id: Optional[int]) -> Path:
        """Return and create the filesystem root for this Telegram user."""
        if user_id is None:
            return self.settings.approved_directory
        root = self.settings.approved_directory_for_user(user_id)
        if self.settings.is_isolated_user(user_id):
            root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def _approved_directory_for_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> Path:
        root = context.user_data.get("approved_directory")
        if isinstance(root, Path):
            return root
        user_id = update.effective_user.id if update.effective_user else None
        return self._approved_directory_for_user(user_id)

    def _apply_user_workspace(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Keep current_directory inside the user's allowed workspace root."""
        user_id = update.effective_user.id if update.effective_user else None
        root = self._approved_directory_for_user(user_id)
        context.user_data["approved_directory"] = root
        self._clamp_current_directory(context, root)

    def _clamp_current_directory(
        self, context: ContextTypes.DEFAULT_TYPE, root: Path
    ) -> None:
        """Reset current_directory if it escapes the supplied root."""

        current = context.user_data.get("current_directory")
        if not isinstance(current, Path):
            current = Path(str(current)) if current else root
        current = current.resolve()
        if not self._is_within(current, root) or not current.is_dir():
            current = root
        context.user_data["current_directory"] = current

    def _get_agent_backend(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Return the user's active agent backend."""
        backend = str(
            context.user_data.get("agent_backend") or self.settings.agent_backend
        ).lower()
        return backend if backend in _AGENT_LABELS else self.settings.agent_backend

    def _agent_backend_state_path(self) -> Path:
        """Return persistent path for per-user backend selection."""
        db_path = self.settings.database_path
        if db_path is not None:
            return db_path.parent / "agent_backends.json"
        return Path("data") / "agent_backends.json"

    def _load_persisted_agent_backend(self, user_id: int) -> Optional[str]:
        path = self._agent_backend_state_path()
        try:
            data = json.loads(path.read_text()) if path.exists() else {}
        except (json.JSONDecodeError, OSError):
            return None
        backend = str(data.get(str(user_id)) or "").lower()
        return backend if backend in _AGENT_LABELS else None

    def _save_persisted_agent_backend(self, user_id: int, backend: str) -> None:
        if backend not in _AGENT_LABELS:
            return
        path = self._agent_backend_state_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(data, dict):
                data = {}
            data[str(user_id)] = backend
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
            tmp.replace(path)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(
                "Failed to persist agent backend",
                path=str(path),
                user_id=user_id,
                backend=backend,
                error=str(e),
            )

    def _activate_agent_backend(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Expose the selected integration via compatibility bot_data keys."""
        backend = self._get_agent_backend(context)
        previous = context.user_data.get("_active_agent_backend")
        current_session_id = context.user_data.get("claude_session_id")
        agent_sessions = context.user_data.setdefault("agent_session_ids", {})

        if previous and previous != backend:
            agent_sessions[previous] = current_session_id
        elif previous == backend and current_session_id is not None:
            agent_sessions[backend] = current_session_id

        context.user_data["_active_agent_backend"] = backend
        context.user_data["claude_session_id"] = agent_sessions.get(backend)

        integrations = context.bot_data.get("agent_integrations") or {}
        integration = integrations.get(backend) or context.bot_data.get(
            "claude_integration"
        )
        context.bot_data["claude_integration"] = integration
        context.bot_data["agent_integration"] = integration
        context.bot_data["agent_backend"] = backend

    def _persist_active_agent_session(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist the compatibility session key under the active backend."""
        backend = self._get_agent_backend(context)
        agent_sessions = context.user_data.setdefault("agent_session_ids", {})
        agent_sessions[backend] = context.user_data.get("claude_session_id")

    async def _apply_thread_routing_context(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> bool:
        """Enforce strict project-thread routing and load thread-local state."""
        manager = context.bot_data.get("project_threads_manager")
        if manager is None:
            await self._reject_for_thread_mode(
                update,
                "❌ <b>Project Thread Mode Misconfigured</b>\n\n"
                "Thread manager is not initialized.",
            )
            return False

        chat = update.effective_chat
        message = update.effective_message
        if not chat or not message:
            return False

        if self.settings.project_threads_mode == "group":
            if chat.id != self.settings.project_threads_chat_id:
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False
        else:
            if getattr(chat, "type", "") != "private":
                await self._reject_for_thread_mode(
                    update,
                    manager.guidance_message(mode=self.settings.project_threads_mode),
                )
                return False

        message_thread_id = self._extract_message_thread_id(update)
        if not message_thread_id:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        project = await manager.resolve_project(chat.id, message_thread_id)
        if not project:
            await self._reject_for_thread_mode(
                update,
                manager.guidance_message(mode=self.settings.project_threads_mode),
            )
            return False

        state_key = f"{chat.id}:{message_thread_id}"
        thread_states = context.user_data.setdefault("thread_state", {})
        state = thread_states.get(state_key, {})

        project_root = project.absolute_path
        current_dir_raw = state.get("current_directory")
        current_dir = (
            Path(current_dir_raw).resolve() if current_dir_raw else project_root
        )
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        context.user_data["current_directory"] = current_dir
        context.user_data["agent_backend"] = state.get(
            "agent_backend", self.settings.agent_backend
        )
        context.user_data["agent_session_ids"] = state.get("agent_session_ids", {})
        restored_session_id = context.user_data["agent_session_ids"].get(
            context.user_data["agent_backend"]
        )
        if restored_session_id is None:
            restored_session_id = state.get("claude_session_id")
            if restored_session_id:
                context.user_data["agent_session_ids"][
                    context.user_data["agent_backend"]
                ] = restored_session_id
        context.user_data["claude_session_id"] = restored_session_id
        context.user_data["_thread_context"] = {
            "chat_id": chat.id,
            "message_thread_id": message_thread_id,
            "state_key": state_key,
            "project_slug": project.slug,
            "project_root": str(project_root),
            "project_name": project.name,
        }
        return True

    def _persist_thread_state(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Persist compatibility keys back into per-thread state."""
        thread_context = context.user_data.get("_thread_context")
        if not thread_context:
            return

        project_root = Path(thread_context["project_root"])
        current_dir = context.user_data.get("current_directory", project_root)
        if not isinstance(current_dir, Path):
            current_dir = Path(str(current_dir))
        current_dir = current_dir.resolve()
        if not self._is_within(current_dir, project_root) or not current_dir.is_dir():
            current_dir = project_root

        thread_states = context.user_data.setdefault("thread_state", {})
        thread_states[thread_context["state_key"]] = {
            "current_directory": str(current_dir),
            "claude_session_id": context.user_data.get("claude_session_id"),
            "agent_backend": self._get_agent_backend(context),
            "agent_session_ids": context.user_data.get("agent_session_ids", {}),
            "project_slug": thread_context["project_slug"],
        }

    @staticmethod
    def _is_within(path: Path, root: Path) -> bool:
        """Return True if path is within root."""
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _extract_message_thread_id(update: Update) -> Optional[int]:
        """Extract topic/thread id from update message for forum/direct topics."""
        message = update.effective_message
        if not message:
            return None
        message_thread_id = getattr(message, "message_thread_id", None)
        if isinstance(message_thread_id, int) and message_thread_id > 0:
            return message_thread_id
        dm_topic = getattr(message, "direct_messages_topic", None)
        topic_id = getattr(dm_topic, "topic_id", None) if dm_topic else None
        if isinstance(topic_id, int) and topic_id > 0:
            return topic_id
        # Telegram omits message_thread_id for the General topic in forum
        # supergroups; its canonical thread ID is 1.
        chat = update.effective_chat
        if chat and getattr(chat, "is_forum", False):
            return 1
        return None

    async def _reject_for_thread_mode(self, update: Update, message: str) -> None:
        """Send a guidance response when strict thread routing rejects an update."""
        query = update.callback_query
        if query:
            try:
                await query.answer()
            except Exception:
                pass
            if query.message:
                await query.message.reply_text(message, parse_mode="HTML")
            return

        if update.effective_message:
            await update.effective_message.reply_text(message, parse_mode="HTML")

    def register_handlers(self, app: Application) -> None:
        """Register handlers based on mode."""
        if self.settings.agentic_mode:
            self._register_agentic_handlers(app)
        else:
            self._register_classic_handlers(app)

    def _register_agentic_handlers(self, app: Application) -> None:
        """Register agentic handlers: commands + text/file/photo."""
        from .handlers import command

        # Commands
        handlers = [
            ("start", self.agentic_start),
            ("new", self.agentic_new),
            ("status", self.agentic_status),
            ("verbose", self.agentic_verbose),
            ("effort", self.agentic_effort),
            ("backend", self.agentic_backend),
            ("claude", self.agentic_claude),
            ("codex", self.agentic_codex),
            ("repo", self.agentic_repo),
            ("resume", self.agentic_resume),
            ("model", self.agentic_model),
            ("login", self.agentic_login),
            ("update", self.agentic_update),
            ("process", self.process_dispatch),
            ("mcp", self.agentic_mcp),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        # Derive known commands dynamically — avoids drift when new commands are added
        self._known_commands: frozenset[str] = frozenset(cmd for cmd, _ in handlers)

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        # Text messages -> Claude
        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(self.agentic_text),
            ),
            group=10,
        )

        # Unknown slash commands -> Claude (passthrough in agentic mode).
        # Registered commands are handled by CommandHandlers in group 0
        # (higher priority). This catches any /command not matched there
        # and forwards it to Claude, while skipping known commands to
        # avoid double-firing.
        app.add_handler(
            MessageHandler(
                filters.COMMAND,
                self._inject_deps(self._handle_unknown_command),
            ),
            group=10,
        )

        # File uploads -> Claude
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(self.agentic_document)
            ),
            group=10,
        )

        # Photo uploads -> Claude
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(self.agentic_photo)),
            group=10,
        )

        # Voice messages -> transcribe -> Claude
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(self.agentic_voice)),
            group=10,
        )

        # Stop button callback (must be before cd: handler)
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_stop_callback),
                pattern=r"^stop:",
            )
        )

        # Only cd: callbacks (for project selection), scoped by pattern
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._agentic_callback),
                pattern=r"^cd:",
            )
        )

        # Resume session callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_resume_callback),
                pattern=r"^resume:",
            )
        )

        # Model selection callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_model_callback),
                pattern=r"^model:",
            )
        )

        # Resume pagination callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_resume_page_callback),
                pattern=r"^rpage:",
            )
        )

        # Process management callbacks
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_plogs_callback),
                pattern=r"^plogs:",
            )
        )
        app.add_handler(
            CallbackQueryHandler(
                self._inject_deps(self._handle_pkill_callback),
                pattern=r"^pkill:",
            )
        )

        logger.info("Agentic handlers registered")

    def _register_classic_handlers(self, app: Application) -> None:
        """Register full classic handler set (moved from core.py)."""
        from .handlers import callback, command, message

        handlers = [
            ("start", command.start_command),
            ("help", command.help_command),
            ("new", command.new_session),
            ("continue", command.continue_session),
            ("end", command.end_session),
            ("ls", command.list_files),
            ("cd", command.change_directory),
            ("pwd", command.print_working_directory),
            ("projects", command.show_projects),
            ("status", command.session_status),
            ("export", command.export_session),
            ("actions", command.quick_actions),
            ("git", command.git_command),
            ("restart", command.restart_command),
        ]
        if self.settings.enable_project_threads:
            handlers.append(("sync_threads", command.sync_threads))

        for cmd, handler in handlers:
            app.add_handler(CommandHandler(cmd, self._inject_deps(handler)))

        app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._inject_deps(message.handle_text_message),
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(
                filters.Document.ALL, self._inject_deps(message.handle_document)
            ),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.PHOTO, self._inject_deps(message.handle_photo)),
            group=10,
        )
        app.add_handler(
            MessageHandler(filters.VOICE, self._inject_deps(message.handle_voice)),
            group=10,
        )
        app.add_handler(
            CallbackQueryHandler(self._inject_deps(callback.handle_callback_query))
        )

        logger.info("Classic handlers registered (13 commands + full handler set)")

    async def get_bot_commands(self) -> list:  # type: ignore[type-arg]
        """Return bot commands appropriate for current mode."""
        if self.settings.agentic_mode:
            commands = [
                BotCommand("start", "Start the bot"),
                BotCommand("new", "Start a fresh session"),
                BotCommand("status", "Show session status"),
                BotCommand("verbose", "Set output verbosity (0/1/2)"),
                BotCommand("effort", "Set reasoning effort (low/medium/high/max)"),
                BotCommand("backend", "Show or switch agent backend"),
                BotCommand("claude", "Switch to Claude Code"),
                BotCommand("codex", "Switch to Codex"),
                BotCommand("repo", "List repos / switch workspace"),
                BotCommand("resume", "Browse & resume previous sessions"),
                BotCommand("model", "Select agent model"),
                BotCommand("login", "Re-authorize Claude (OAuth)"),
                BotCommand("update", "Update Claude Code CLI"),
                BotCommand("process", "Manage processes: run/ps/kill/logs"),
                BotCommand("mcp", "List MCP servers and their tools"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands
        else:
            commands = [
                BotCommand("start", "Start bot and show help"),
                BotCommand("help", "Show available commands"),
                BotCommand("new", "Clear context and start fresh session"),
                BotCommand("continue", "Explicitly continue last session"),
                BotCommand("end", "End current session and clear context"),
                BotCommand("ls", "List files in current directory"),
                BotCommand("cd", "Change directory (resumes project session)"),
                BotCommand("pwd", "Show current directory"),
                BotCommand("projects", "Show all projects"),
                BotCommand("status", "Show session status"),
                BotCommand("export", "Export current session"),
                BotCommand("actions", "Show quick actions"),
                BotCommand("git", "Git repository commands"),
                BotCommand("restart", "Restart the bot"),
            ]
            if self.settings.enable_project_threads:
                commands.append(BotCommand("sync_threads", "Sync project topics"))
            return commands

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        approved_directory = self._approved_directory_for_context(update, context)
        current_dir = context.user_data.get("current_directory", approved_directory)
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        self._persist_active_agent_session(context)
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        await update.message.reply_text("Session reset. What's next?")

    async def _switch_agent_backend(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        backend: str,
    ) -> None:
        """Switch the current user/thread to a different agent backend."""
        if backend not in _AGENT_LABELS:
            await update.message.reply_text(
                "Use <code>/backend claude</code> or <code>/backend codex</code>.",
                parse_mode="HTML",
            )
            return

        self._persist_active_agent_session(context)
        context.user_data["agent_backend"] = backend
        if update.effective_user:
            self._save_persisted_agent_backend(update.effective_user.id, backend)
        self._activate_agent_backend(context)
        context.user_data["force_new_session"] = False

        label = _AGENT_LABELS[backend]
        session_id = context.user_data.get("claude_session_id")
        session_line = "active session restored" if session_id else "no active session"
        await update.message.reply_text(
            f"Backend switched to <b>{escape_html(label)}</b> ({session_line}).",
            parse_mode="HTML",
        )

    async def agentic_backend(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show or switch active agent backend."""
        args = update.message.text.split()[1:] if update.message.text else []
        if args:
            await self._switch_agent_backend(update, context, args[0].strip().lower())
            return

        backend = self._get_agent_backend(context)
        session_ids = context.user_data.get("agent_session_ids", {})
        lines = [
            f"Current backend: <b>{escape_html(_AGENT_LABELS[backend])}</b>",
            "",
            "Commands:",
            "<code>/claude</code> - switch to Claude Code",
            "<code>/codex</code> - switch to Codex",
            "<code>/backend claude|codex</code>",
            "",
            "Sessions:",
        ]
        for key in ("claude", "codex"):
            sid = session_ids.get(key)
            status = "active" if sid else "none"
            marker = " *" if key == backend else ""
            lines.append(f"{_AGENT_LABELS[key]}: {status}{marker}")

        await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    async def agentic_claude(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Switch to Claude Code."""
        await self._switch_agent_backend(update, context, "claude")

    async def agentic_codex(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Switch to Codex."""
        await self._switch_agent_backend(update, context, "codex")

    async def _get_agent_cli_version(self, backend: str) -> str:
        """Return active agent CLI version output, or 'unknown' on error."""
        binary = (
            (self.settings.codex_cli_path or "codex")
            if backend == "codex"
            else (self.settings.claude_cli_path or "claude")
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                binary,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
            return stdout.decode(errors="replace").strip() or "unknown"
        except (FileNotFoundError, asyncio.TimeoutError, OSError):
            return "unknown"

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        approved_directory = self._approved_directory_for_context(update, context)
        current_dir = context.user_data.get("current_directory", approved_directory)
        dir_display = str(current_dir)

        session_id = context.user_data.get("claude_session_id")
        session_status = "active" if session_id else "none"

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                user_status = rate_limiter.get_user_status(update.effective_user.id)
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
            except Exception:
                pass

        backend = self._get_agent_backend(context)
        cli_version = await self._get_agent_cli_version(backend)
        agent_label = _AGENT_LABELS[backend]
        await update.message.reply_text(
            f"📂 {dir_display} · Session: {session_status}{cost_str}\n"
            f"🧩 {agent_label}: <code>{escape_html(cli_version)}</code>",
            parse_mode="HTML",
        )

    async def agentic_update(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Update the active agent CLI via npm."""
        backend = self._get_agent_backend(context)
        package = (
            "@openai/codex@latest"
            if backend == "codex"
            else "@anthropic-ai/claude-code@latest"
        )
        label = _AGENT_LABELS[backend]

        old_version = await self._get_agent_cli_version(backend)
        progress = await update.message.reply_text(
            f"⬆️ Updating {label}...\nCurrent: <code>{escape_html(old_version)}</code>",
            parse_mode="HTML",
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                "sudo",
                "-n",
                "npm",
                "install",
                "-g",
                package,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=180.0)
            output = stdout.decode(errors="replace")
            rc = proc.returncode
        except asyncio.TimeoutError:
            await progress.edit_text(
                "❌ npm install timed out after 3 minutes. Try again later."
            )
            return
        except FileNotFoundError:
            await progress.edit_text(
                "❌ `sudo` or `npm` not available in container. "
                "Rebuild needed to enable /update."
            )
            return

        if rc != 0:
            tail = output[-500:] if len(output) > 500 else output
            await progress.edit_text(
                f"❌ Update failed (exit {rc}):\n<pre>{escape_html(tail)}</pre>",
                parse_mode="HTML",
            )
            return

        new_version = await self._get_agent_cli_version(backend)
        changed = new_version != old_version
        arrow = " → " if changed else " = "
        status_emoji = "✅" if changed else "ℹ️"
        note = (
            "New requests will use the updated CLI automatically."
            if changed
            else "Already on the latest version."
        )
        await progress.edit_text(
            f"{status_emoji} {label} updated.\n"
            f"<code>{escape_html(old_version)}</code>{arrow}"
            f"<code>{escape_html(new_version)}</code>\n\n{note}",
            parse_mode="HTML",
        )
        logger.info(
            "Agent CLI updated via /update",
            backend=backend,
            user_id=update.effective_user.id,
            old=old_version,
            new=new_version,
        )

    def _get_verbose_level(self, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Return effective verbose level: per-user override or global default."""
        user_override = context.user_data.get("verbose_level")
        if user_override is not None:
            return int(user_override)
        return self.settings.verbose_level

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    def _get_preferred_model(self, context: ContextTypes.DEFAULT_TYPE) -> Optional[str]:
        """Get user's preferred model for the active backend."""
        backend = self._get_agent_backend(context)
        models = context.user_data.get("preferred_models")
        if isinstance(models, dict):
            selected = models.get(backend)
            if isinstance(selected, str) and selected:
                return selected

        legacy_model = context.user_data.get("preferred_model")
        if backend == "claude" and isinstance(legacy_model, str) and legacy_model:
            return legacy_model
        return None

    def _set_preferred_model(
        self,
        context: ContextTypes.DEFAULT_TYPE,
        backend: str,
        model_id: str,
    ) -> None:
        """Store a model override without crossing Claude/Codex backends."""
        models = context.user_data.get("preferred_models")
        if not isinstance(models, dict):
            models = {}
            context.user_data["preferred_models"] = models
        models[backend] = model_id
        if backend == "claude":
            context.user_data["preferred_model"] = model_id

    _EFFORT_LEVELS = ("low", "medium", "high", "max", "xhigh")

    def _get_preferred_effort(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        """Return effective effort: per-user override or config default."""
        backend = self._get_agent_backend(context)
        user_override = context.user_data.get("effort")
        if user_override in self._EFFORT_LEVELS:
            if backend == "codex" and user_override == "max":
                return "xhigh"
            return str(user_override)
        if backend == "codex":
            return str(self.settings.codex_effort)
        return str(self.settings.claude_effort)

    async def agentic_effort(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set reasoning effort: /effort [low|medium|high|max]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_preferred_effort(context)
            await update.message.reply_text(
                f"Effort: <b>{escape_html(current)}</b>\n\n"
                "Usage: <code>/effort low|medium|high|max|xhigh</code>\n"
                "  low    — fastest, shallow reasoning\n"
                "  medium — balanced\n"
                "  high   — deeper reasoning\n"
                "  max    — maximum Claude reasoning\n"
                "  xhigh  — maximum Codex reasoning",
                parse_mode="HTML",
            )
            return

        level = args[0].strip().lower()
        if level not in self._EFFORT_LEVELS:
            await update.message.reply_text(
                "Please use: /effort low, /effort medium, /effort high, /effort max, or /effort xhigh"
            )
            return

        context.user_data["effort"] = level
        await update.message.reply_text(
            f"Effort set to <b>{escape_html(level)}</b>",
            parse_mode="HTML",
        )

    async def agentic_model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show model selector: /model."""
        if self._get_agent_backend(context) == "codex":
            current = self._get_preferred_model(context) or self.settings.codex_model
            current_display = current or "CLI default"
            integration = context.bot_data.get("claude_integration")
            if not integration or not hasattr(integration, "list_models"):
                await update.message.reply_text(
                    "Codex model catalog is not available for this backend."
                )
                return

            try:
                models = await integration.list_models()
            except Exception as e:
                logger.warning("Failed to load Codex model catalog", error=str(e))
                await update.message.reply_text(
                    "Could not load Codex models from the current CLI version:\n"
                    f"<code>{escape_html(str(e))}</code>",
                    parse_mode="HTML",
                )
                return

            if not models:
                await update.message.reply_text(
                    "Codex CLI did not return any selectable models."
                )
                return

            context.user_data["codex_model_catalog"] = models
            keyboard = []
            for idx, model in enumerate(models):
                check = " \u2705" if model["id"] == current else ""
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{model['label']}{check}",
                            callback_data=f"model:codex:{idx}",
                        )
                    ]
                )

            current_label = current_display
            for model in models:
                if model["id"] == current:
                    current_label = str(model["label"])
                    break

            await update.message.reply_text(
                f"Current Codex model: <b>{escape_html(current_label)}</b>\n\n"
                "Select a Codex model from the current CLI catalog:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        current = (
            self._get_preferred_model(context)
            or self.settings.claude_model
            or "default"
        )

        keyboard = []
        for m in _AVAILABLE_MODELS:
            check = " \u2705" if m["id"] == current else ""
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{m['label']} — {m['desc']}{check}",
                        callback_data=f"model:{m['id']}",
                    )
                ]
            )

        current_label = current
        for m in _AVAILABLE_MODELS:
            if m["id"] == current:
                current_label = m["label"]
                break

        await update.message.reply_text(
            f"Current model: <b>{escape_html(current_label)}</b>\n\n"
            "Select a model for all future sessions:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    async def _handle_model_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle model selection from inline keyboard."""
        query = update.callback_query
        data = query.data or ""

        if data.startswith("model:codex:"):
            raw_index = data.rsplit(":", 1)[1]
            try:
                index = int(raw_index)
            except ValueError:
                await query.answer("Unknown model", show_alert=True)
                return

            catalog = context.user_data.get("codex_model_catalog")
            if not isinstance(catalog, list) or index < 0 or index >= len(catalog):
                await query.answer(
                    "Model list expired. Run /model again.", show_alert=True
                )
                return

            selected = catalog[index]
            if not isinstance(selected, dict) or not isinstance(
                selected.get("id"), str
            ):
                await query.answer("Unknown model", show_alert=True)
                return

            model_id = selected["id"]
            label = str(selected.get("label") or model_id)
            self._set_preferred_model(context, "codex", model_id)
            await query.answer()

            keyboard = []
            for idx, model in enumerate(catalog):
                if not isinstance(model, dict):
                    continue
                check = " \u2705" if model.get("id") == model_id else ""
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"{model.get('label') or model.get('id')}{check}",
                            callback_data=f"model:codex:{idx}",
                        )
                    ]
                )

            await query.edit_message_text(
                f"Codex model set to <b>{escape_html(label)}</b>\n\n"
                "All new Codex requests will use this model.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        model_id = data.split(":", 1)[1]

        # Validate model_id against known models
        valid_ids = {m["id"] for m in _AVAILABLE_MODELS}
        if model_id not in valid_ids:
            await query.answer("Unknown model", show_alert=True)
            return

        self._set_preferred_model(context, "claude", model_id)
        await query.answer()

        # Find label for the selected model
        label = model_id
        for m in _AVAILABLE_MODELS:
            if m["id"] == model_id:
                label = m["label"]
                break

        # Rebuild keyboard with updated checkmark
        keyboard = []
        for m in _AVAILABLE_MODELS:
            check = " \u2705" if m["id"] == model_id else ""
            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{m['label']} — {m['desc']}{check}",
                        callback_data=f"model:{m['id']}",
                    )
                ]
            )

        await query.edit_message_text(
            f"Model set to <b>{escape_html(label)}</b>\n\n"
            "All new requests will use this model.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    def _format_verbose_progress(
        self,
        activity_log: List[Dict[str, Any]],
        verbose_level: int,
        start_time: float,
    ) -> str:
        """Build the progress message text based on activity so far."""
        if not activity_log:
            return "Working..."

        elapsed = time.time() - start_time
        lines: List[str] = [f"Working... ({elapsed:.0f}s)\n"]

        for entry in activity_log[-15:]:  # Show last 15 entries max
            kind = entry.get("kind", "tool")
            if kind == "text":
                # Claude's intermediate reasoning/commentary
                snippet = entry.get("detail", "")
                if verbose_level >= 2:
                    lines.append(f"\U0001f4ac {snippet}")
                else:
                    # Level 1: one short line
                    lines.append(f"\U0001f4ac {snippet[:80]}")
            else:
                # Tool call
                icon = _tool_icon(entry["name"])
                if verbose_level >= 2 and entry.get("detail"):
                    lines.append(f"{icon} {entry['name']}: {entry['detail']}")
                else:
                    lines.append(f"{icon} {entry['name']}")

        if len(activity_log) > 15:
            lines.insert(1, f"... ({len(activity_log) - 15} earlier entries)\n")

        return "\n".join(lines)

    @staticmethod
    def _summarize_tool_input(tool_name: str, tool_input: Dict[str, Any]) -> str:
        """Return a short summary of tool input for verbose level 2."""
        if not tool_input:
            return ""
        if tool_name in ("Read", "Write", "Edit", "MultiEdit"):
            path = tool_input.get("file_path") or tool_input.get("path", "")
            if path:
                # Show just the filename, not the full path
                return path.rsplit("/", 1)[-1]
        if tool_name in ("Glob", "Grep"):
            pattern = tool_input.get("pattern", "")
            if pattern:
                return pattern[:60]
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if cmd:
                return _redact_secrets(cmd[:100])[:80]
        if tool_name in ("WebFetch", "WebSearch"):
            return (tool_input.get("url", "") or tool_input.get("query", ""))[:60]
        if tool_name == "Task":
            desc = tool_input.get("description", "")
            if desc:
                return desc[:60]
        # Generic: show first key's value
        for v in tool_input.values():
            if isinstance(v, str) and v:
                return v[:60]
        return ""

    @staticmethod
    def _start_typing_heartbeat(
        chat: Any,
        interval: float = 2.0,
    ) -> "asyncio.Task[None]":
        """Start a background typing indicator task.

        Sends typing every *interval* seconds, independently of
        stream events. Cancel the returned task in a ``finally``
        block.
        """

        async def _heartbeat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        await chat.send_action("typing")
                    except Exception:
                        pass
            except asyncio.CancelledError:
                pass

        return asyncio.create_task(_heartbeat())

    def _make_stream_callback(
        self,
        verbose_level: int,
        progress_msg: Any,
        tool_log: List[Dict[str, Any]],
        start_time: float,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
        mcp_images: Optional[List[ImageAttachment]] = None,
        mcp_files: Optional[List[FileAttachment]] = None,
        approved_directory: Optional[Path] = None,
        draft_streamer: Optional[DraftStreamer] = None,
        interrupt_event: Optional[asyncio.Event] = None,
    ) -> Optional[Callable[[StreamUpdate], Any]]:
        """Create a stream callback for verbose progress updates.

        When *mcp_images* is provided, the callback also intercepts
        ``send_image_to_user`` tool calls and collects validated
        :class:`ImageAttachment` objects for later Telegram delivery.

        When *mcp_files* is provided, the callback also intercepts
        ``send_file_to_user`` tool calls and collects validated
        :class:`FileAttachment` objects for later Telegram delivery.

        When *draft_streamer* is provided, tool activity and assistant
        text are streamed to the user in real time via
        ``sendMessageDraft``.

        Returns None when verbose_level is 0 **and** no MCP image/file
        collection or draft streaming is requested.
        Typing indicators are handled by a separate heartbeat task.
        """
        need_mcp_intercept = (
            mcp_images is not None or mcp_files is not None
        ) and approved_directory is not None

        if verbose_level == 0 and not need_mcp_intercept and draft_streamer is None:
            return None

        last_edit_time = [0.0]  # mutable container for closure

        async def _on_stream(update_obj: StreamUpdate) -> None:
            # Stop all streaming activity after interrupt
            if interrupt_event is not None and interrupt_event.is_set():
                return

            # Intercept send_image_to_user and send_file_to_user MCP tool calls.
            # The SDK namespaces MCP tools as "mcp__<server>__<tool>",
            # so match both the bare name and the namespaced variant.
            if update_obj.tool_calls and need_mcp_intercept:
                for tc in update_obj.tool_calls:
                    tc_name = tc.get("name", "")
                    if mcp_images is not None and (
                        tc_name == "send_image_to_user"
                        or tc_name.endswith("__send_image_to_user")
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        img = validate_image_path(
                            file_path, approved_directory, caption
                        )
                        if img:
                            mcp_images.append(img)
                    if mcp_files is not None and (
                        tc_name == "send_file_to_user"
                        or tc_name.endswith("__send_file_to_user")
                    ):
                        tc_input = tc.get("input", {})
                        file_path = tc_input.get("file_path", "")
                        caption = tc_input.get("caption", "")
                        attachment = validate_file_path(
                            file_path, approved_directory, caption
                        )
                        if attachment:
                            mcp_files.append(attachment)

            # Capture tool calls
            if update_obj.tool_calls:
                for tc in update_obj.tool_calls:
                    name = tc.get("name", "unknown")
                    detail = self._summarize_tool_input(name, tc.get("input", {}))
                    if verbose_level >= 1:
                        tool_log.append(
                            {"kind": "tool", "name": name, "detail": detail}
                        )
                    if draft_streamer:
                        icon = _tool_icon(name)
                        line = (
                            f"{icon} {name}: {detail}" if detail else f"{icon} {name}"
                        )
                        await draft_streamer.append_tool(line)

            # Capture assistant text (reasoning / commentary)
            if update_obj.type == "assistant" and update_obj.content:
                text = update_obj.content.strip()
                if text:
                    first_line = text.split("\n", 1)[0].strip()
                    if first_line:
                        if verbose_level >= 1:
                            tool_log.append(
                                {"kind": "text", "detail": first_line[:120]}
                            )
                        if draft_streamer:
                            await draft_streamer.append_tool(
                                f"\U0001f4ac {first_line[:120]}"
                            )

            # Stream text to user via draft (prefer token deltas;
            # skip full assistant messages to avoid double-appending)
            if draft_streamer and update_obj.content:
                if update_obj.type == "stream_delta":
                    await draft_streamer.append_text(update_obj.content)

            # Throttle progress message edits to avoid Telegram rate limits
            if not draft_streamer and verbose_level >= 1:
                now = time.time()
                if (now - last_edit_time[0]) >= 2.0 and tool_log:
                    last_edit_time[0] = now
                    new_text = self._format_verbose_progress(
                        tool_log, verbose_level, start_time
                    )
                    try:
                        await progress_msg.edit_text(
                            new_text, reply_markup=reply_markup
                        )
                    except Exception:
                        pass

        return _on_stream

    async def _send_images(
        self,
        update: Update,
        images: List[ImageAttachment],
        reply_to_message_id: Optional[int] = None,
        caption: Optional[str] = None,
        caption_parse_mode: Optional[str] = None,
    ) -> bool:
        """Send extracted images as a media group (album) or documents.

        If *caption* is provided and fits (≤1024 chars), it is attached to the
        photo / first album item so text + images appear as one message.

        Returns True if the caption was successfully embedded in the photo message.
        """
        photos: List[ImageAttachment] = []
        documents: List[ImageAttachment] = []
        for img in images:
            if should_send_as_photo(img.path):
                photos.append(img)
            else:
                documents.append(img)

        # Telegram caption limit
        use_caption = bool(
            caption and len(caption) <= 1024 and photos and not documents
        )
        caption_sent = False

        # Send raster photos as a single album (Telegram groups 2-10 items)
        if photos:
            try:
                if len(photos) == 1:
                    with open(photos[0].path, "rb") as f:
                        await update.message.reply_photo(
                            photo=f,
                            reply_to_message_id=reply_to_message_id,
                            caption=caption if use_caption else None,
                            parse_mode=caption_parse_mode if use_caption else None,
                        )
                    caption_sent = use_caption
                else:
                    media = []
                    file_handles = []
                    for idx, img in enumerate(photos[:10]):
                        fh = open(img.path, "rb")  # noqa: SIM115
                        file_handles.append(fh)
                        media.append(
                            InputMediaPhoto(
                                media=fh,
                                caption=caption if use_caption and idx == 0 else None,
                                parse_mode=(
                                    caption_parse_mode
                                    if use_caption and idx == 0
                                    else None
                                ),
                            )
                        )
                    try:
                        await update.message.chat.send_media_group(
                            media=media,
                            reply_to_message_id=reply_to_message_id,
                        )
                        caption_sent = use_caption
                    finally:
                        for fh in file_handles:
                            fh.close()
            except Exception as e:
                logger.warning("Failed to send photo album", error=str(e))

        # Send SVGs / large files as documents (one by one — can't mix in album)
        for img in documents:
            try:
                with open(img.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=img.path.name,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send document image",
                    path=str(img.path),
                    error=str(e),
                )

        return caption_sent

    async def _send_files(
        self,
        update: Update,
        files: List[FileAttachment],
        reply_to_message_id: Optional[int] = None,
    ) -> None:
        """Send collected file attachments as Telegram documents."""
        for attachment in files:
            try:
                with open(attachment.path, "rb") as f:
                    await update.message.reply_document(
                        document=f,
                        filename=attachment.path.name,
                        caption=attachment.caption or None,
                        reply_to_message_id=reply_to_message_id,
                    )
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(
                    "Failed to send file",
                    path=str(attachment.path),
                    error=str(e),
                )

    async def agentic_login(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Start OAuth re-auth flow. Sends authorize URL; next text msg is the code."""
        if self._get_agent_backend(context) == "codex":
            await self._start_codex_login(update, context)
            return

        from .features.oauth_login import start_login

        pending = start_login()
        context.user_data["oauth_pending"] = {
            "verifier": pending.verifier,
            "state": pending.state,
            "started_at": time.time(),
        }
        await update.message.reply_text(
            "🔐 <b>Claude re-authorization</b>\n\n"
            "1. Open this URL in a browser and sign in:\n"
            f"<code>{escape_html(pending.authorize_url)}</code>\n\n"
            "2. After approving, the page will show a code (format: "
            "<code>abc...#state...</code>). Copy it and send it back here "
            "as a plain message.\n\n"
            "You can also paste the full callback URL. "
            "Send /new to cancel.",
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    async def _handle_oauth_code_message(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, raw: str
    ) -> None:
        """Exchange the pasted OAuth code for tokens and write credentials."""
        from .features.oauth_login import (
            exchange_code,
            parse_code_input,
            write_credentials,
        )

        pending = context.user_data.get("oauth_pending") or {}
        verifier = pending.get("verifier")
        expected_state = pending.get("state")
        if not verifier or not expected_state:
            context.user_data.pop("oauth_pending", None)
            await update.message.reply_text("No pending login. Run /login to start.")
            return

        # 10 min timeout for the OAuth code
        if time.time() - float(pending.get("started_at") or 0) > 600:
            context.user_data.pop("oauth_pending", None)
            await update.message.reply_text(
                "⌛ Login timed out. Run /login to start again."
            )
            return

        try:
            code, got_state = parse_code_input(raw)
        except ValueError as e:
            await update.message.reply_text(
                f"❌ Can't parse code: {e}. Paste the code from the callback page "
                "(or the full URL), or /new to cancel."
            )
            return

        if got_state and got_state != expected_state:
            context.user_data.pop("oauth_pending", None)
            await update.message.reply_text("❌ State mismatch. Run /login again.")
            return

        try:
            token_resp = await exchange_code(code, verifier, expected_state)
            path = write_credentials(token_resp)
        except Exception as e:
            logger.error(
                "OAuth exchange failed", user_id=update.effective_user.id, error=str(e)
            )
            await update.message.reply_text(
                f"❌ Token exchange failed: {escape_html(str(e))}\n\n"
                "Run /login to try again.",
                parse_mode="HTML",
            )
            return
        finally:
            context.user_data.pop("oauth_pending", None)

        logger.info(
            "OAuth credentials refreshed via /login",
            user_id=update.effective_user.id,
            path=str(path),
        )
        await update.message.reply_text("✅ Authorized. You can use the bot now.")

    async def _start_codex_login(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Start Codex device auth and report completion back to Telegram."""
        from .features.codex_login import (
            collect_initial_output,
            finish_device_login,
            start_device_login,
        )

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id
        login_key = f"codex_login:{user_id}"
        logins = context.bot_data.setdefault("codex_login_processes", {})

        existing = logins.get(login_key)
        if existing and not existing.done:
            output = existing.output_text() or "Login is already in progress."
            await update.message.reply_text(
                "Codex login is already running:\n\n"
                f"<pre>{escape_html(output[-3000:])}</pre>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return

        progress = await update.message.reply_text("Starting Codex login...")
        try:
            login = await start_device_login(self.settings.codex_cli_path)
            logins[login_key] = login
            output = await collect_initial_output(login, timeout=12.0)
        except FileNotFoundError:
            await progress.edit_text(
                "Codex CLI not found. Set CODEX_CLI_PATH or install codex in the runtime."
            )
            return
        except Exception as e:
            await progress.edit_text(
                f"Failed to start Codex login: <code>{escape_html(str(e))}</code>",
                parse_mode="HTML",
            )
            return

        if login.done:
            text = output or "Codex login exited before producing instructions."
            if login.returncode == 0:
                await progress.edit_text("✅ Codex authorized.")
            else:
                await progress.edit_text(
                    "❌ Codex login failed:\n\n"
                    f"<pre>{escape_html(text[-3000:])}</pre>",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            return

        if output:
            await progress.edit_text(
                "🔐 <b>Codex authorization</b>\n\n"
                "Open the URL below, enter the code if prompted, and finish sign-in. "
                "I'll notify you when the CLI confirms authorization.\n\n"
                f"<pre>{escape_html(output[-3000:])}</pre>",
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        else:
            await progress.edit_text(
                "Codex login started, but the CLI has not printed instructions yet. "
                "Run <code>/login</code> again to see captured output.",
                parse_mode="HTML",
            )

        async def _notify_when_done() -> None:
            try:
                rc = await finish_device_login(login)
                text = login.output_text()
                if rc == 0:
                    await context.bot.send_message(
                        chat_id=chat_id, text="✅ Codex authorized."
                    )
                else:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            "❌ Codex login failed:\n\n"
                            f"<pre>{escape_html(text[-3000:] or f'exit {rc}')}</pre>"
                        ),
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
            finally:
                if logins.get(login_key) is login:
                    logins.pop(login_key, None)

        asyncio.create_task(_notify_when_done())

    async def agentic_text(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Direct Claude passthrough. Simple progress. No suggestions."""
        user_id = update.effective_user.id
        message_text = update.message.text

        # If user is in /login flow, treat this message as the OAuth code.
        if context.user_data.get("oauth_pending"):
            await self._handle_oauth_code_message(update, context, message_text)
            return

        logger.info(
            "Agentic text message",
            user_id=user_id,
            message_length=len(message_text),
        )

        # Rate limit check
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            allowed, limit_message = await rate_limiter.check_rate_limit(user_id, 0.001)
            if not allowed:
                await update.message.reply_text(f"⏱️ {limit_message}")
                return

        chat = update.message.chat
        await chat.send_action("typing")

        verbose_level = self._get_verbose_level(context)

        # Create Stop button and interrupt event
        interrupt_event = asyncio.Event()
        stop_kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Stop", callback_data=f"stop:{user_id}")]]
        )
        progress_msg = await update.message.reply_text(
            "Working...", reply_markup=stop_kb
        )

        # Register active request for stop callback
        active_request = ActiveRequest(
            user_id=user_id,
            interrupt_event=interrupt_event,
            progress_msg=progress_msg,
        )
        self._active_requests[user_id] = active_request

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            self._active_requests.pop(user_id, None)
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration.",
                reply_markup=None,
            )
            return

        approved_directory = self._approved_directory_for_context(update, context)
        current_dir = context.user_data.get("current_directory", approved_directory)
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        # --- Verbose progress tracking via stream callback ---
        tool_log: List[Dict[str, Any]] = []
        start_time = time.time()
        mcp_images: List[ImageAttachment] = []
        mcp_files: List[FileAttachment] = []

        # Stream drafts (private chats only)
        draft_streamer: Optional[DraftStreamer] = None
        if self.settings.enable_stream_drafts and chat.type == "private":
            draft_streamer = DraftStreamer(
                bot=context.bot,
                chat_id=chat.id,
                draft_id=generate_draft_id(),
                message_thread_id=update.message.message_thread_id,
                throttle_interval=self.settings.stream_draft_interval,
            )

        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            start_time,
            reply_markup=stop_kb,
            mcp_images=mcp_images,
            mcp_files=mcp_files,
            approved_directory=approved_directory,
            draft_streamer=draft_streamer,
            interrupt_event=interrupt_event,
        )

        # Independent typing heartbeat — stays alive even with no stream events
        heartbeat = self._start_typing_heartbeat(chat)

        # Callback fired by the facade when a resume fails and a fresh retry
        # is about to start. Notifies the user and clears stale tool output so
        # the verbose stream doesn't mix aborted + fresh runs.
        async def _on_fresh_retry(_error: str) -> None:
            tool_log.clear()
            try:
                await progress_msg.edit_text(
                    "🔄 Previous session expired — starting fresh...",
                    reply_markup=stop_kb,
                )
            except Exception:
                logger.debug("Failed to edit progress on fresh-retry notice")

        success = True
        try:
            claude_response = await claude_integration.run_command(
                prompt=message_text,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                interrupt_event=interrupt_event,
                model_override=self._get_preferred_model(context),
                effort_override=self._get_preferred_effort(context),
                on_retry=_on_fresh_retry,
            )

            # New session created successfully — clear the one-shot flag
            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            # Track directory changes
            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )
            self._clamp_current_directory(context, approved_directory)

            # Store interaction
            storage = context.bot_data.get("storage")
            if storage:
                try:
                    await storage.save_claude_interaction(
                        user_id=user_id,
                        session_id=claude_response.session_id,
                        prompt=message_text,
                        response=claude_response,
                        ip_address=None,
                    )
                except Exception as e:
                    logger.warning("Failed to log interaction", error=str(e))

            # Format response (no reply_markup — strip keyboards)
            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)

            response_content = claude_response.content
            if claude_response.interrupted:
                response_content = (
                    response_content or ""
                ) + "\n\n_(Interrupted by user)_"

            formatted_messages = formatter.format_claude_response(response_content)

        except Exception as e:
            success = False
            logger.error("Claude integration failed", error=str(e), user_id=user_id)
            # The stored session_id may now point at an abandoned session
            # (facade drops it on timeout-during-resume). Clear it so the
            # NEXT message starts fresh instead of attempting another resume
            # against the same dead session.
            context.user_data["claude_session_id"] = None
            from .handlers.message import _format_error_message
            from .utils.formatting import FormattedMessage

            formatted_messages = [
                FormattedMessage(_format_error_message(e), parse_mode="HTML")
            ]
        finally:
            heartbeat.cancel()
            self._active_requests.pop(user_id, None)
            if draft_streamer:
                try:
                    await draft_streamer.flush()
                except Exception:
                    logger.debug("Draft flush failed in finally block", user_id=user_id)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls)
        images: List[ImageAttachment] = mcp_images

        # Try to combine text + images in one message when possible
        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        # Send text messages (skip if caption was already embedded in photos)
        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                try:
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,  # No keyboards in agentic mode
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)
                except Exception as send_err:
                    logger.warning(
                        "Failed to send HTML response, retrying as plain text",
                        error=str(send_err),
                        message_index=i,
                    )
                    try:
                        await update.message.reply_text(
                            message.text,
                            reply_markup=None,
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )
                    except Exception as plain_err:
                        await update.message.reply_text(
                            f"Failed to deliver response "
                            f"(Telegram error: {str(plain_err)[:150]}). "
                            f"Please try again.",
                            reply_to_message_id=(
                                update.message.message_id if i == 0 else None
                            ),
                        )

            # Send images separately if caption wasn't used
            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

        # Send MCP-collected files (from send_file_to_user tool calls)
        if mcp_files:
            try:
                await self._send_files(
                    update,
                    mcp_files,
                    reply_to_message_id=update.message.message_id,
                )
            except Exception as file_err:
                logger.warning("File send failed", error=str(file_err))

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=user_id,
                command="text_message",
                args=[message_text[:100]],
                success=success,
            )

    async def agentic_document(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process file upload -> Claude, minimal chrome."""
        user_id = update.effective_user.id
        document = update.message.document

        logger.info(
            "Agentic document upload",
            user_id=user_id,
            filename=document.file_name,
        )

        # Security validation
        security_validator = context.bot_data.get("security_validator")
        if security_validator:
            valid, error = security_validator.validate_filename(document.file_name)
            if not valid:
                await update.message.reply_text(f"File rejected: {error}")
                return

        # Size check
        max_size = 10 * 1024 * 1024
        if document.file_size > max_size:
            await update.message.reply_text(
                f"File too large ({document.file_size / 1024 / 1024:.1f}MB). Max: 10MB."
            )
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        # Try enhanced file handler, fall back to basic
        features = context.bot_data.get("features")
        file_handler = features.get_file_handler() if features else None
        prompt: Optional[str] = None

        if file_handler:
            try:
                processed_file = await file_handler.handle_document_upload(
                    document,
                    user_id,
                    update.message.caption or "Please review this file:",
                )
                prompt = processed_file.prompt
            except Exception:
                file_handler = None

        if not file_handler:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            try:
                content = file_bytes.decode("utf-8")
                if len(content) > 50000:
                    content = content[:50000] + "\n... (truncated)"
                caption = update.message.caption or "Please review this file:"
                prompt = (
                    f"{caption}\n\n**File:** `{document.file_name}`\n\n"
                    f"```\n{content}\n```"
                )
            except UnicodeDecodeError:
                await progress_msg.edit_text(
                    "Unsupported file format. Must be text-based (UTF-8)."
                )
                return

        # Process with Claude
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        approved_directory = self._approved_directory_for_context(update, context)
        current_dir = context.user_data.get("current_directory", approved_directory)
        session_id = context.user_data.get("claude_session_id")

        # Check if /new was used — skip auto-resume for this first message.
        # Flag is only cleared after a successful run so retries keep the intent.
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_doc: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_doc,
            approved_directory=approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                model_override=self._get_preferred_model(context),
                effort_override=self._get_preferred_effort(context),
            )

            if force_new:
                context.user_data["force_new_session"] = False

            context.user_data["claude_session_id"] = claude_response.session_id

            from .handlers.message import _update_working_directory_from_claude_response

            _update_working_directory_from_claude_response(
                claude_response, context, self.settings, user_id
            )
            self._clamp_current_directory(context, approved_directory)

            from .utils.formatting import ResponseFormatter

            formatter = ResponseFormatter(self.settings)
            formatted_messages = formatter.format_claude_response(
                claude_response.content
            )

            try:
                await progress_msg.delete()
            except Exception:
                logger.debug("Failed to delete progress message, ignoring")

            # Use MCP-collected images (from send_image_to_user tool calls)
            images: List[ImageAttachment] = mcp_images_doc

            caption_sent = False
            if images and len(formatted_messages) == 1:
                msg = formatted_messages[0]
                if msg.text and len(msg.text) <= 1024:
                    try:
                        caption_sent = await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                            caption=msg.text,
                            caption_parse_mode=msg.parse_mode,
                        )
                    except Exception as img_err:
                        logger.warning("Image+caption send failed", error=str(img_err))

            if not caption_sent:
                for i, message in enumerate(formatted_messages):
                    await update.message.reply_text(
                        message.text,
                        parse_mode=message.parse_mode,
                        reply_markup=None,
                        reply_to_message_id=(
                            update.message.message_id if i == 0 else None
                        ),
                    )
                    if i < len(formatted_messages) - 1:
                        await asyncio.sleep(0.5)

                if images:
                    try:
                        await self._send_images(
                            update,
                            images,
                            reply_to_message_id=update.message.message_id,
                        )
                    except Exception as img_err:
                        logger.warning("Image send failed", error=str(img_err))

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error("Claude file processing failed", error=str(e), user_id=user_id)
        finally:
            heartbeat.cancel()

    async def agentic_photo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Process photo -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        image_handler = features.get_image_handler() if features else None

        if not image_handler:
            await update.message.reply_text("Photo processing is not available.")
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Working...")

        try:
            photo = update.message.photo[-1]
            processed_image = await image_handler.process_image(
                photo, update.message.caption
            )
            fmt = processed_image.metadata.get("format", "png")
            images = [
                {
                    "data": processed_image.base64_data,
                    "media_type": _MEDIA_TYPE_MAP.get(fmt, "image/png"),
                }
            ]

            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_image.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
                images=images,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude photo processing failed", error=str(e), user_id=user_id
            )

    async def agentic_voice(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transcribe voice message -> Claude, minimal chrome."""
        user_id = update.effective_user.id

        features = context.bot_data.get("features")
        voice_handler = features.get_voice_handler() if features else None

        if not voice_handler:
            await update.message.reply_text(self._voice_unavailable_message())
            return

        chat = update.message.chat
        await chat.send_action("typing")
        progress_msg = await update.message.reply_text("Transcribing...")

        try:
            voice = update.message.voice
            processed_voice = await voice_handler.process_voice_message(
                voice, update.message.caption
            )

            await progress_msg.edit_text("Working...")
            await self._handle_agentic_media_message(
                update=update,
                context=context,
                prompt=processed_voice.prompt,
                progress_msg=progress_msg,
                user_id=user_id,
                chat=chat,
            )

        except Exception as e:
            from .handlers.message import _format_error_message

            await progress_msg.edit_text(_format_error_message(e), parse_mode="HTML")
            logger.error(
                "Claude voice processing failed", error=str(e), user_id=user_id
            )

    async def _handle_agentic_media_message(
        self,
        *,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        prompt: str,
        progress_msg: Any,
        user_id: int,
        chat: Any,
        images: Optional[List[Dict[str, str]]] = None,
    ) -> None:
        """Run a media-derived prompt through Claude and send responses."""
        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await progress_msg.edit_text(
                "Claude integration not available. Check configuration."
            )
            return

        approved_directory = self._approved_directory_for_context(update, context)
        current_dir = context.user_data.get("current_directory", approved_directory)
        session_id = context.user_data.get("claude_session_id")
        force_new = bool(context.user_data.get("force_new_session"))

        verbose_level = self._get_verbose_level(context)
        tool_log: List[Dict[str, Any]] = []
        mcp_images_media: List[ImageAttachment] = []
        on_stream = self._make_stream_callback(
            verbose_level,
            progress_msg,
            tool_log,
            time.time(),
            mcp_images=mcp_images_media,
            approved_directory=approved_directory,
        )

        heartbeat = self._start_typing_heartbeat(chat)
        try:
            claude_response = await claude_integration.run_command(
                prompt=prompt,
                working_directory=current_dir,
                user_id=user_id,
                session_id=session_id,
                on_stream=on_stream,
                force_new=force_new,
                images=images,
                model_override=self._get_preferred_model(context),
                effort_override=self._get_preferred_effort(context),
            )
        finally:
            heartbeat.cancel()

        if force_new:
            context.user_data["force_new_session"] = False

        context.user_data["claude_session_id"] = claude_response.session_id

        from .handlers.message import _update_working_directory_from_claude_response

        _update_working_directory_from_claude_response(
            claude_response, context, self.settings, user_id
        )
        self._clamp_current_directory(context, approved_directory)

        from .utils.formatting import ResponseFormatter

        formatter = ResponseFormatter(self.settings)
        formatted_messages = formatter.format_claude_response(claude_response.content)

        try:
            await progress_msg.delete()
        except Exception:
            logger.debug("Failed to delete progress message, ignoring")

        # Use MCP-collected images (from send_image_to_user tool calls).
        images: List[ImageAttachment] = mcp_images_media

        caption_sent = False
        if images and len(formatted_messages) == 1:
            msg = formatted_messages[0]
            if msg.text and len(msg.text) <= 1024:
                try:
                    caption_sent = await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                        caption=msg.text,
                        caption_parse_mode=msg.parse_mode,
                    )
                except Exception as img_err:
                    logger.warning("Image+caption send failed", error=str(img_err))

        if not caption_sent:
            for i, message in enumerate(formatted_messages):
                if not message.text or not message.text.strip():
                    continue
                await update.message.reply_text(
                    message.text,
                    parse_mode=message.parse_mode,
                    reply_markup=None,
                    reply_to_message_id=(update.message.message_id if i == 0 else None),
                )
                if i < len(formatted_messages) - 1:
                    await asyncio.sleep(0.5)

            if images:
                try:
                    await self._send_images(
                        update,
                        images,
                        reply_to_message_id=update.message.message_id,
                    )
                except Exception as img_err:
                    logger.warning("Image send failed", error=str(img_err))

    async def _handle_unknown_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Forward unknown slash commands to Claude in agentic mode.

        Known commands are handled by their own CommandHandlers (group 0);
        this handler fires for *every* COMMAND message in group 10 but
        returns immediately when the command is registered, preventing
        double execution.
        """
        msg = update.effective_message
        if not msg or not msg.text:
            return
        cmd = msg.text.split()[0].lstrip("/").split("@")[0].lower()
        if cmd in self._known_commands:
            return  # let the registered CommandHandler take care of it
        # Forward unrecognised /commands to Claude as natural language
        await self.agentic_text(update, context)

    def _voice_unavailable_message(self) -> str:
        """Return provider-aware guidance when voice feature is unavailable."""
        if self.settings.voice_provider == "local":
            return (
                "Voice processing is not available. "
                "Ensure whisper.cpp is installed and the model file exists. "
                "Check WHISPER_CPP_BINARY_PATH and WHISPER_CPP_MODEL_PATH settings."
            )
        return (
            "Voice processing is not available. "
            f"Set {self.settings.voice_provider_api_key_env} "
            f"for {self.settings.voice_provider_display_name} and install "
            'voice extras with: pip install "claude-code-telegram[voice]"'
        )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self._approved_directory_for_context(update, context)
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo, or back to base with /, ., .., ~
            target_name = args[0]
            if target_name in ("/", ".", "..", "~", "base"):
                target_path = base
                display_name = "base"
            else:
                target_path = (base / target_name).resolve()
                display_name = target_name
            if not self._is_within(target_path, base) or not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session
            claude_integration = context.bot_data.get("claude_integration")
            session_id = None
            if claude_integration:
                existing = await claude_integration._find_resumable_session(
                    update.effective_user.id, target_path
                )
                if existing:
                    session_id = existing.session_id
            context.user_data["claude_session_id"] = session_id

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(display_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        at_base = current_dir == base
        current_name = current_dir.name if not at_base else None

        base_marker = " \u25c0" if at_base else ""
        lines.append(f"\U0001f3e0 <code>base/</code>{base_marker}")
        keyboard_rows.append(
            [InlineKeyboardButton("\U0001f3e0 base", callback_data="cd:/")]
        )

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )

    # ── /mcp: list configured MCP servers and their tools ──────────────

    async def agentic_mcp(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show MCP servers visible to the current session, with tool lists."""
        base = self._approved_directory_for_context(update, context)
        current_dir: Path = context.user_data.get("current_directory", base)

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration:
            await update.message.reply_text("Claude integration not available.")
            return

        sent = await update.message.reply_text(
            "\U0001f50e Inspecting MCP servers in "
            f"<code>{escape_html(current_dir.name or str(current_dir))}/</code>…",
            parse_mode="HTML",
        )

        try:
            servers = await claude_integration.inspect_mcp_servers(current_dir)
        except Exception as e:
            await sent.edit_text(
                f"Failed to inspect MCP servers: <code>{escape_html(str(e))}</code>",
                parse_mode="HTML",
            )
            return

        if not servers:
            await sent.edit_text("No MCP servers configured.")
            return

        lines: List[str] = [
            f"<b>MCP servers</b> in "
            f"<code>{escape_html(current_dir.name or str(current_dir))}/</code>"
        ]
        total_tools = 0
        for s in servers:
            name = escape_html(str(s["name"]))
            origin = s["origin"]
            badge = "\U0001f916" if origin == "bot" else "\U0001f4e6"
            if "error" in s:
                lines.append(
                    f"\n{badge} <b>{name}</b> \u2014 "
                    f"<i>{escape_html(s['error'])}</i>"
                )
                continue
            tools = s.get("tools") or []
            total_tools += len(tools)
            lines.append(f"\n{badge} <b>{name}</b> \u2014 {len(tools)} tool(s)")
            for tool in tools:
                lines.append(f"  • <code>{escape_html(tool)}</code>")

        lines.append(f"\n<i>Total tools: {total_tools}</i>")

        # Audit
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=update.effective_user.id,
                command="mcp",
                args=[str(current_dir)],
                success=True,
            )

        text = "\n".join(lines)
        # Telegram message cap is 4096; chunk if large
        if len(text) <= 4000:
            await sent.edit_text(text, parse_mode="HTML")
        else:
            await sent.delete()
            chunk = ""
            for line in lines:
                if len(chunk) + len(line) + 1 > 3800:
                    await update.message.reply_text(chunk, parse_mode="HTML")
                    chunk = ""
                chunk += line + "\n"
            if chunk:
                await update.message.reply_text(chunk, parse_mode="HTML")

    # ── /process: background process management ────────────────────────

    def _get_pm(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> "ProcessManager":  # type: ignore[name-defined]
        from src.process.manager import ProcessManager

        user_id = update.effective_user.id if update.effective_user else 0
        approved_directory = self._approved_directory_for_context(update, context)
        namespace = (
            f"user-{user_id}" if self.settings.is_isolated_user(user_id) else None
        )
        approved = str(approved_directory) if namespace else None
        return ProcessManager(namespace=namespace, approved_directory=approved)

    async def process_dispatch(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """/process <sub> — dispatch to run/ps/kill/logs/cleanup."""
        text = update.message.text or ""
        parts = text.split(None, 2)
        sub = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""

        if sub == "run":
            await self._proc_run(update, context, rest)
        elif sub == "ps":
            await self._proc_ps(update, context)
        elif sub == "kill":
            await self._proc_kill(update, context, rest)
        elif sub == "logs":
            await self._proc_logs(update, context, rest)
        elif sub == "cleanup":
            pm = self._get_pm(update, context)
            c = pm.cleanup_dead()
            await update.message.reply_text(f"Removed {c} dead process(es).")
        else:
            await update.message.reply_text(
                "<b>/process</b> \u2014 manage background processes\n\n"
                "<code>/process run &lt;cmd&gt;</code> \u2014 start\n"
                "<code>/process run -n name &lt;cmd&gt;</code> \u2014 start with name\n"
                "<code>/process ps</code> \u2014 list all\n"
                "<code>/process kill &lt;id&gt;</code> \u2014 stop\n"
                "<code>/process logs &lt;id&gt;</code> \u2014 output\n"
                "<code>/process cleanup</code> \u2014 remove dead",
                parse_mode="HTML",
            )

    async def _proc_run(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        if not args:
            await update.message.reply_text("Usage: /process run <command>")
            return

        name = ""
        command = args
        if args.startswith("-n "):
            parts = args[3:].split(None, 1)
            if len(parts) == 2:
                name, command = parts
            else:
                await update.message.reply_text(
                    "Usage: /process run -n <name> <command>"
                )
                return

        base = self._approved_directory_for_context(update, context)
        cwd = str(context.user_data.get("current_directory", base))
        pm = self._get_pm(update, context)
        try:
            entry = pm.start(command, cwd, name)
            await update.message.reply_text(
                f"\U0001f7e2 <b>#{entry.id} '{escape_html(entry.name)}'</b>\n"
                f"PID: {entry.pid}\n"
                f"Dir: <code>{escape_html(entry.cwd)}</code>\n"
                f"Cmd: <code>{escape_html(command)}</code>",
                parse_mode="HTML",
            )
        except Exception as e:
            await update.message.reply_text(f"Failed: {e}")

    async def _proc_ps(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        pm = self._get_pm(update, context)
        procs = pm.list_all()
        if not procs:
            await update.message.reply_text("No processes.")
            return

        lines = ["<b>Processes</b>\n"]
        for p in procs:
            icon = "\U0001f7e2" if p.is_alive else "\U0001f534"
            lines.append(
                f"{icon} <b>#{p.id}</b> {escape_html(p.name)}\n"
                f"    <code>{escape_html(p.command[:50])}</code>\n"
                f"    {p.status} \u00b7 {p.uptime} \u00b7 pid {p.pid}\n"
                f"    {escape_html(p.cwd)}"
            )

        keyboard = []
        for p in procs:
            if p.is_alive:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            f"Logs #{p.id} {p.name}", callback_data=f"plogs:{p.id}"
                        ),
                        InlineKeyboardButton(
                            f"Kill #{p.id}", callback_data=f"pkill:{p.id}"
                        ),
                    ]
                )
        markup = InlineKeyboardMarkup(keyboard) if keyboard else None
        await update.message.reply_text(
            "\n".join(lines), parse_mode="HTML", reply_markup=markup
        )

    async def _proc_kill(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        if not args or not args.strip().isdigit():
            await update.message.reply_text("Usage: /process kill <id>")
            return
        pm = self._get_pm(update, context)
        entry = pm.kill(int(args.strip()))
        if entry:
            await update.message.reply_text(
                f"\U0001f534 #{entry.id} '{escape_html(entry.name)}' killed",
                parse_mode="HTML",
            )
        else:
            await update.message.reply_text(f"Process #{args.strip()} not found.")

    async def _proc_logs(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, args: str
    ) -> None:
        if not args or not args.strip().isdigit():
            await update.message.reply_text("Usage: /process logs <id>")
            return
        pm = self._get_pm(update, context)
        entry = pm.get(int(args.strip()))
        if not entry:
            await update.message.reply_text(f"Process #{args.strip()} not found.")
            return
        logs = entry.last_logs(50)
        header = f"<b>#{entry.id} '{escape_html(entry.name)}'</b> [{entry.status}]\n\n"
        await update.message.reply_text(
            header + f"<pre>{escape_html(logs[-3500:])}</pre>", parse_mode="HTML"
        )

    # ── process callback handlers ────────────────────────────────────

    async def _handle_plogs_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        await query.answer()
        proc_id = int(query.data.split(":", 1)[1])
        pm = self._get_pm(update, context)
        entry = pm.get(proc_id)
        if not entry:
            await query.message.reply_text(f"Process #{proc_id} not found.")
            return
        logs = entry.last_logs(50)
        header = f"<b>#{proc_id} '{escape_html(entry.name)}'</b> [{entry.status}]\n\n"
        await query.message.reply_text(
            header + f"<pre>{escape_html(logs[-3500:])}</pre>", parse_mode="HTML"
        )

    async def _handle_pkill_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        proc_id = int(query.data.split(":", 1)[1])
        pm = self._get_pm(update, context)
        entry = pm.kill(proc_id)
        if entry:
            await query.answer(f"#{proc_id} killed")
            await query.edit_message_text(
                f"\U0001f534 #{proc_id} '{escape_html(entry.name)}' killed",
                parse_mode="HTML",
            )
        else:
            await query.answer("Not found", show_alert=True)

    # ── /resume: browse & resume previous sessions ────────────────────

    _RESUME_PAGE_SIZE = 5

    async def agentic_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List previous sessions for the current project directory."""
        await self._send_resume_page(update.message, context, page=0)

    async def _send_resume_page(
        self,
        target,  # type: ignore[no-untyped-def]
        context: ContextTypes.DEFAULT_TYPE,
        page: int,
        edit: bool = False,
    ) -> None:
        """Build and send/edit a paginated session list."""
        user_id = (
            target.from_user.id if hasattr(target, "from_user") else target.chat.id
        )
        base = context.user_data.get(
            "approved_directory", self.settings.approved_directory
        )
        current_dir = context.user_data.get("current_directory", base)
        current_session_id = context.user_data.get("claude_session_id")

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration or not claude_integration.session_manager:
            text = "Session manager not available."
            if edit:
                await target.edit_message_text(text)
            else:
                await target.reply_text(text)
            return

        session_mgr = claude_integration.session_manager

        # Get all active sessions for this user
        all_sessions = await session_mgr._get_user_sessions(user_id)

        # Filter by project directory only — show all sessions regardless of age
        # so the user can resume any previous chat after restarts/deploys.
        sessions = [s for s in all_sessions if str(s.project_path) == str(current_dir)]

        if not sessions:
            rel = current_dir.name if current_dir != base else str(current_dir)
            text = f"No sessions for <code>{escape_html(rel)}/</code>"
            if edit:
                await target.edit_message_text(text, parse_mode="HTML")
            else:
                await target.reply_text(text, parse_mode="HTML")
            return

        # Fetch first prompt for each session as label
        first_prompts: dict[str, str] = {}
        storage = context.bot_data.get("storage")
        db = getattr(storage, "db_manager", None) if storage else None
        if db:
            try:
                async with db.get_connection() as conn:
                    for s in sessions:
                        if not s.session_id:
                            continue
                        cursor = await conn.execute(
                            "SELECT prompt FROM messages WHERE session_id = ? "
                            "ORDER BY timestamp ASC LIMIT 1",
                            [s.session_id],
                        )
                        row = await cursor.fetchone()
                        if row:
                            first_prompts[s.session_id] = row[0]
            except Exception:
                pass  # fallback: no labels

        total = len(sessions)
        ps = self._RESUME_PAGE_SIZE
        max_page = (total - 1) // ps
        page = max(0, min(page, max_page))
        page_sessions = sessions[page * ps : (page + 1) * ps]

        rel_dir = current_dir.name if current_dir != base else str(current_dir)
        lines = [
            f"<b>Sessions for</b> <code>{escape_html(rel_dir)}/</code>"
            f" ({total} total \u00b7 page {page + 1}/{max_page + 1})\n"
        ]

        keyboard = []
        for i, s in enumerate(page_sessions, start=page * ps + 1):
            is_current = s.session_id == current_session_id
            marker = " \u2705" if is_current else ""
            last = s.last_used.strftime("%m-%d %H:%M")

            # Session label from first user message
            raw_label = first_prompts.get(s.session_id, "")
            short_label = (raw_label[:40] + "...") if len(raw_label) > 40 else raw_label
            label_line = f" <i>{escape_html(short_label)}</i>" if short_label else ""

            lines.append(
                f"<b>{i}.</b>{marker} {last}"
                f" \u00b7 {s.message_count} msgs"
                f" \u00b7 ${s.total_cost:.3f}"
                f"\n    {label_line}"
            )
            btn_label = short_label or last
            if is_current:
                btn_label = f"\u2705 {btn_label}"
            keyboard.append(
                [
                    InlineKeyboardButton(
                        btn_label, callback_data=f"resume:{s.session_id}"
                    )
                ]
            )

        # Pagination row
        nav_row = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("\u25c0 Back", callback_data=f"rpage:{page - 1}")
            )
        if page < max_page:
            nav_row.append(
                InlineKeyboardButton("Next \u25b6", callback_data=f"rpage:{page + 1}")
            )
        if nav_row:
            keyboard.append(nav_row)

        markup = InlineKeyboardMarkup(keyboard)
        text = "\n".join(lines)

        if edit:
            await target.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        else:
            await target.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def _handle_resume_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Resume a selected session."""
        query = update.callback_query
        session_id = query.data.split(":", 1)[1]
        user_id = query.from_user.id

        claude_integration = context.bot_data.get("claude_integration")
        if not claude_integration or not claude_integration.session_manager:
            await query.answer("Session manager not available", show_alert=True)
            return

        session = await claude_integration.session_manager.storage.load_session(
            session_id, user_id
        )
        if not session:
            await query.answer("Session not found or expired", show_alert=True)
            return

        context.user_data["claude_session_id"] = session.session_id
        context.user_data["current_directory"] = session.project_path

        await query.answer()

        # Fetch last Claude response for context
        last_response = ""
        storage = context.bot_data.get("storage")
        db = getattr(storage, "db_manager", None) if storage else None
        if db:
            try:
                async with db.get_connection() as conn:
                    cursor = await conn.execute(
                        "SELECT response FROM messages "
                        "WHERE session_id = ? AND response IS NOT NULL "
                        "ORDER BY timestamp DESC LIMIT 1",
                        [session_id],
                    )
                    row = await cursor.fetchone()
                    if row and row[0]:
                        last_response = row[0]
            except Exception:
                pass

        rel = session.project_path.name
        header = (
            f"\u2705 Resumed session in <code>{escape_html(rel)}/</code>"
            f"\n{session.message_count} msgs \u00b7 ${session.total_cost:.3f}"
        )

        await query.edit_message_text(header, parse_mode="HTML")

        # Send last Claude response as a separate message for context
        if last_response:
            preview = last_response[:4000]
            if len(last_response) > 4000:
                preview += "\n\n..."
            await query.message.reply_text(
                f"<b>Last response:</b>\n\n{escape_html(preview)}",
                parse_mode="HTML",
            )

    async def _handle_resume_page_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle pagination for /resume."""
        query = update.callback_query
        await query.answer()
        page = int(query.data.split(":", 1)[1])
        await self._send_resume_page(query, context, page=page, edit=True)

    # ── stop callback ────────────────────────────────────────────────

    async def _handle_stop_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle stop: callbacks — interrupt a running Claude request."""
        query = update.callback_query
        target_user_id = int(query.data.split(":", 1)[1])

        # Only the requesting user can stop their own request
        if query.from_user.id != target_user_id:
            await query.answer(
                "Only the requesting user can stop this.", show_alert=True
            )
            return

        active = self._active_requests.get(target_user_id)
        if not active:
            await query.answer("Already completed.", show_alert=False)
            return
        if active.interrupted:
            await query.answer("Already stopping...", show_alert=False)
            return

        active.interrupt_event.set()
        active.interrupted = True
        await query.answer("Stopping...", show_alert=False)

        try:
            await active.progress_msg.edit_text("Stopping...", reply_markup=None)
        except Exception:
            pass

    async def _agentic_callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle cd: callbacks — switch directory and resume session if available."""
        query = update.callback_query
        await query.answer()

        data = query.data
        _, project_name = data.split(":", 1)

        base = self._approved_directory_for_context(update, context)
        if project_name in ("/", ".", "..", "~", "base", ""):
            new_path = base
            display_name = "base"
        else:
            new_path = (base / project_name).resolve()
            display_name = project_name

        if not self._is_within(new_path, base) or not new_path.is_dir():
            await query.edit_message_text(
                f"Directory not found: <code>{escape_html(display_name)}</code>",
                parse_mode="HTML",
            )
            return

        context.user_data["current_directory"] = new_path

        # Look for a resumable session instead of always clearing
        claude_integration = context.bot_data.get("claude_integration")
        session_id = None
        if claude_integration:
            existing = await claude_integration._find_resumable_session(
                query.from_user.id, new_path
            )
            if existing:
                session_id = existing.session_id
        context.user_data["claude_session_id"] = session_id

        is_git = (new_path / ".git").is_dir()
        git_badge = " (git)" if is_git else ""
        session_badge = " · session resumed" if session_id else ""

        await query.edit_message_text(
            f"Switched to <code>{escape_html(display_name)}/</code>"
            f"{git_badge}{session_badge}",
            parse_mode="HTML",
        )

        # Audit log
        audit_logger = context.bot_data.get("audit_logger")
        if audit_logger:
            await audit_logger.log_command(
                user_id=query.from_user.id,
                command="cd",
                args=[display_name],
                success=True,
            )
