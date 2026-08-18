"""JWT revocation via Redis-backed jti blacklist.

Fail-open on Redis errors: revocation is best-effort. A Redis outage must not block login
or user traffic, but operators should monitor logs for revocation gaps.

When REDIS_URL points at localhost (the default placeholder), we treat Redis as
unconfigured and skip the lookup entirely — every authenticated request would
otherwise log a connection-refused WARNING. The fail-open behaviour is the same
either way; this just stops the log spam and saves the per-request connect timeout.
"""
from __future__ import annotations

import logging
from typing import Optional

import redis.asyncio as aioredis

from app.core.config import settings

logger = logging.getLogger(__name__)

_REVOKED_PREFIX = "jwt:revoked:"


def _redis_disabled() -> bool:
    """True when REDIS_URL points at localhost — treated as "no Redis configured"."""
    url = settings.REDIS_URL or ""
    return "localhost" in url or "127.0.0.1" in url


def _key(jti: str) -> str:
    return f"{_REVOKED_PREFIX}{jti}"


async def revoke(redis: aioredis.Redis, jti: Optional[str], ttl_seconds: int) -> None:
    """Mark a jti as revoked with a TTL at least as long as the token's remaining lifetime."""
    if not jti or ttl_seconds <= 0 or _redis_disabled():
        return
    try:
        await redis.set(_key(jti), "1", ex=max(ttl_seconds, 60))
    except Exception as exc:
        logger.warning("Failed to revoke jti %s: %s", jti, exc)


async def is_revoked(redis: aioredis.Redis, jti: Optional[str]) -> bool:
    """Return True if the jti is on the blacklist. Returns False on Redis errors (fail-open)."""
    if not jti or _redis_disabled():
        return False
    try:
        return bool(await redis.exists(_key(jti)))
    except Exception as exc:
        logger.warning("Failed to check revocation for jti %s: %s", jti, exc)
        return False


def remaining_ttl(exp_timestamp: Optional[float], now_timestamp: float) -> int:
    """Return seconds remaining between now and exp, clamped at 0."""
    if exp_timestamp is None:
        return 0
    return max(0, int(exp_timestamp - now_timestamp))
