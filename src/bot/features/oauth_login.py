"""Claude Code OAuth login flow.

Implements the manual PKCE flow used by `claude auth login --claudeai`:
user opens the authorize URL, authorizes in browser, copies the callback
code from the redirect page, and pastes it back to the bot. Bot exchanges
the code for tokens and writes `~/.claude/.credentials.json`.

Endpoints and client_id are extracted from the Claude Code CLI bundle.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
SCOPES = [
    "org:create_api_key",
    "user:profile",
    "user:inference",
    "user:sessions:claude_code",
    "user:mcp_servers",
    "user:file_upload",
]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class PendingLogin:
    verifier: str
    state: str
    authorize_url: str


def start_login() -> PendingLogin:
    """Generate PKCE + state and build the authorize URL."""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    state = _b64url(secrets.token_bytes(24))

    params = {
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": " ".join(SCOPES),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    from urllib.parse import urlencode

    url = f"{AUTHORIZE_URL}?{urlencode(params)}"
    return PendingLogin(verifier=verifier, state=state, authorize_url=url)


def parse_code_input(raw: str) -> tuple[str, Optional[str]]:
    """Accept raw code, `code#state`, or a full callback URL. Returns (code, state)."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty input")

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        q = parse_qs(parsed.query)
        code = (q.get("code") or [""])[0]
        state = (q.get("state") or [None])[0]
        if not code:
            raise ValueError("no 'code' parameter in URL")
        return code, state

    if "#" in raw:
        code, _, state = raw.partition("#")
        return code.strip(), state.strip() or None

    if not re.match(r"^[A-Za-z0-9_.\-]{4,}$", raw):
        raise ValueError("input does not look like an OAuth code")
    return raw, None


async def exchange_code(
    code: str, verifier: str, state: str, *, timeout: float = 15.0
) -> dict:
    """Exchange authorization code for tokens. Returns parsed JSON from token endpoint."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
        "state": state,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            TOKEN_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
    if resp.status_code == 401:
        raise RuntimeError("Invalid authorization code (expired or wrong)")
    if resp.status_code != 200:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json()


def write_credentials(token_resp: dict, path: Optional[Path] = None) -> Path:
    """Write `.credentials.json` in the format the Claude CLI expects.

    Preserves any existing subscriptionType/rateLimitTier so a manual
    re-login doesn't wipe the cached profile. Uses atomic rename.
    """
    if path is None:
        path = Path.home() / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    # If the live path is a symlink (to volume-backed storage), write to the
    # target file directly. Otherwise os.replace() would swap the symlink for
    # a regular file and new tokens would not persist across restarts.
    if path.is_symlink():
        try:
            target = path.resolve(strict=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            path = target
        except OSError:
            pass

    access_token = token_resp["access_token"]
    refresh_token = token_resp["refresh_token"]
    expires_in = int(token_resp.get("expires_in") or 0)
    scope_str = token_resp.get("scope") or " ".join(SCOPES)

    import time as _time

    expires_at_ms = int(_time.time() * 1000) + expires_in * 1000
    scopes = [s for s in re.split(r"[\s,]+", scope_str) if s]

    existing_sub: Optional[str] = None
    existing_tier: Optional[str] = None
    if path.exists():
        try:
            old = json.loads(path.read_text())
            prev = old.get("claudeAiOauth") or {}
            existing_sub = prev.get("subscriptionType")
            existing_tier = prev.get("rateLimitTier")
        except (json.JSONDecodeError, OSError):
            pass

    creds = {
        "claudeAiOauth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": expires_at_ms,
            "scopes": scopes,
            "subscriptionType": existing_sub,
            "rateLimitTier": existing_tier,
        }
    }

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(creds, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path
