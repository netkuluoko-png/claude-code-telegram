"""Validate file paths and prepare them for Telegram delivery.

Used by the MCP ``send_file_to_user`` tool intercept — the stream callback
validates each path via :func:`validate_file_path` and collects
:class:`FileAttachment` objects for later Telegram delivery.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()

# Safety caps
MAX_FILES_PER_RESPONSE = 10
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB — Telegram Bot API limit


@dataclass
class FileAttachment:
    """A file to attach to a Telegram response."""

    path: Path
    caption: str


def validate_file_path(
    file_path: str,
    approved_directory: Path,
    caption: str = "",
) -> Optional[FileAttachment]:
    """Validate a file path from an MCP ``send_file_to_user`` call.

    Returns a :class:`FileAttachment` if the path is a valid, existing file
    inside *approved_directory*, or ``None`` otherwise.
    """
    try:
        path = Path(file_path)
        if not path.is_absolute():
            return None

        resolved = path.resolve()

        # Security: must be within approved directory
        try:
            resolved.relative_to(approved_directory.resolve())
        except ValueError:
            logger.debug(
                "MCP file path outside approved directory",
                path=str(resolved),
                approved=str(approved_directory),
            )
            return None

        if not resolved.is_file():
            return None

        file_size = resolved.stat().st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            logger.debug("MCP file too large", path=str(resolved), size=file_size)
            return None

        return FileAttachment(
            path=resolved,
            caption=caption,
        )
    except (OSError, ValueError) as e:
        logger.debug("MCP file path validation failed", path=file_path, error=str(e))
        return None
