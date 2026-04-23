"""Bearer-token auth for state-mutating dashboard endpoints.

Single static token from DASHBOARD_API_TOKEN env var. Compared with
hmac.compare_digest to avoid timing leaks. When the token is empty (dev),
auth is disabled and all calls pass; main.py logs a warning at startup if
BOT_ENV=production but the token is unset.
"""
from __future__ import annotations

import hmac
from typing import Optional

import structlog
from fastapi import Header, HTTPException, status

from config import settings

logger = structlog.get_logger(__name__)

_BEARER_PREFIX = "Bearer "


def _expected_token() -> str:
    return settings.bot.dashboard_api_token


def auth_enabled() -> bool:
    return bool(_expected_token())


async def require_api_token(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency. No-op when DASHBOARD_API_TOKEN is unset (dev)."""
    expected = _expected_token()
    if not expected:
        return
    if not authorization or not authorization.startswith(_BEARER_PREFIX):
        logger.warning("api.auth_missing")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )
    presented = authorization[len(_BEARER_PREFIX):].strip()
    if not hmac.compare_digest(presented, expected):
        logger.warning("api.auth_invalid")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API token",
            headers={"WWW-Authenticate": "Bearer"},
        )
