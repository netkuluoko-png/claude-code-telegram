"""High-level Codex integration facade."""

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import structlog

from ..claude.exceptions import ClaudeTimeoutError
from ..claude.sdk_integration import StreamUpdate
from ..claude.session import SessionManager
from ..config.settings import Settings
from .sdk_integration import CodexCLIManager, CodexResponse

logger = structlog.get_logger()


class CodexIntegration:
    """Main integration point for Codex CLI."""

    def __init__(
        self,
        config: Settings,
        sdk_manager: Optional[CodexCLIManager] = None,
        session_manager: Optional[SessionManager] = None,
    ):
        self.config = config
        self.sdk_manager = sdk_manager or CodexCLIManager(config)
        self.session_manager = session_manager

    async def run_command(
        self,
        prompt: str,
        working_directory: Path,
        user_id: int,
        session_id: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
        force_new: bool = False,
        interrupt_event: Optional["Any"] = None,
        images: Optional[List[Dict[str, str]]] = None,
        model_override: Optional[str] = None,
        effort_override: Optional[str] = None,
        on_retry: Optional[Callable[[str], Any]] = None,
    ) -> CodexResponse:
        del on_retry
        logger.info(
            "Running Codex command",
            user_id=user_id,
            working_directory=str(working_directory),
            session_id=session_id,
            force_new=force_new,
        )

        if not session_id and not force_new:
            existing_session = await self._find_resumable_session(
                user_id, working_directory
            )
            if existing_session:
                session_id = existing_session.session_id

        session = await self.session_manager.get_or_create_session(
            user_id, working_directory, session_id
        )
        is_new = getattr(session, "is_new_session", False)
        should_continue = not is_new and bool(session.session_id)
        codex_session_id = session.session_id if should_continue else None

        try:
            try:
                response = await self._execute(
                    prompt=prompt,
                    working_directory=working_directory,
                    session_id=codex_session_id,
                    continue_session=should_continue,
                    stream_callback=on_stream,
                    interrupt_event=interrupt_event,
                    images=images,
                    model_override=model_override,
                    effort_override=effort_override,
                    user_id=user_id,
                )
            except ClaudeTimeoutError:
                if should_continue:
                    await self.session_manager.remove_session(session.session_id)
                raise

            await self.session_manager.update_session(session, response)
            response.session_id = session.session_id
            return response
        except Exception:
            logger.exception(
                "Codex command failed",
                user_id=user_id,
                session_id=session.session_id,
            )
            raise

    async def _execute(
        self,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str] = None,
        continue_session: bool = False,
        stream_callback: Optional[Callable[[StreamUpdate], None]] = None,
        interrupt_event: Optional[Any] = None,
        images: Optional[List[Dict[str, str]]] = None,
        model_override: Optional[str] = None,
        effort_override: Optional[str] = None,
        user_id: int = 0,
    ) -> CodexResponse:
        return await self.sdk_manager.execute_command(
            prompt=prompt,
            working_directory=working_directory,
            session_id=session_id,
            continue_session=continue_session,
            stream_callback=stream_callback,
            interrupt_event=interrupt_event,
            images=images,
            model_override=model_override,
            effort_override=effort_override,
            user_id=user_id,
        )

    async def _find_resumable_session(self, user_id: int, working_directory: Path):
        sessions = await self.session_manager._get_user_sessions(user_id)
        matching_sessions = [
            s
            for s in sessions
            if s.project_path == working_directory
            and bool(s.session_id)
            and not s.is_expired(self.config.session_timeout_hours)
        ]
        if not matching_sessions:
            return None
        return max(matching_sessions, key=lambda s: s.last_used)

    async def continue_session(
        self,
        user_id: int,
        working_directory: Path,
        prompt: Optional[str] = None,
        on_stream: Optional[Callable[[StreamUpdate], None]] = None,
    ) -> Optional[CodexResponse]:
        latest_session = await self._find_resumable_session(user_id, working_directory)
        if not latest_session:
            return None
        return await self.run_command(
            prompt=prompt or "Please continue where we left off",
            working_directory=working_directory,
            user_id=user_id,
            session_id=latest_session.session_id,
            on_stream=on_stream,
        )

    async def get_session_info(
        self, session_id: str, user_id: int
    ) -> Optional[Dict[str, Any]]:
        return await self.session_manager.get_session_info(session_id, user_id)

    async def get_user_sessions(self, user_id: int) -> List[Dict[str, Any]]:
        sessions = await self.session_manager._get_user_sessions(user_id)
        return [
            {
                "session_id": s.session_id,
                "project_path": str(s.project_path),
                "created_at": s.created_at.isoformat(),
                "last_used": s.last_used.isoformat(),
                "total_cost": s.total_cost,
                "message_count": s.message_count,
                "tools_used": s.tools_used,
                "expired": s.is_expired(self.config.session_timeout_hours),
            }
            for s in sessions
        ]

    async def cleanup_expired_sessions(self) -> int:
        return await self.session_manager.cleanup_expired_sessions()

    async def get_user_summary(self, user_id: int) -> Dict[str, Any]:
        session_summary = await self.session_manager.get_user_session_summary(user_id)
        return {"user_id": user_id, **session_summary}

    async def inspect_mcp_servers(
        self, working_directory: Path
    ) -> List[Dict[str, Any]]:
        return await self.sdk_manager.inspect_mcp_servers(working_directory)

    async def shutdown(self) -> None:
        logger.info("Codex integration shutdown complete")
