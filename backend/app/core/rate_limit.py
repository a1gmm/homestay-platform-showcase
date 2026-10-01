"""轻量级 Redis 计数限流。Redis 不可用时静默放行（degrade gracefully）。

用法：
    @router.post(...)
    async def handler(request: Request, redis: RedisClient, ...):
        await enforce_rate_limit(redis, f"book:{request.client.host}", limit=10, window_seconds=60)
        ...
"""
import logging
from hashlib import sha256

from fastapi import HTTPException, status
from redis.asyncio import Redis

logger = logging.getLogger(__name__)

_ATOMIC_WINDOW_COUNTER = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""


async def enforce_rate_limit(
    redis: Redis,
    key: str,
    *,
    limit: int,
    window_seconds: int,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    try:
        evaluator = getattr(redis, "eval", None)
        if callable(evaluator):
            attempts = int(
                await evaluator(
                    _ATOMIC_WINDOW_COUNTER, 1, key, int(window_seconds)
                )
            )
        else:
            # Compatibility for narrow test doubles/older adapters. Redis INCR
            # itself is atomic; production clients take the Lua path so first
            # increment and expiry cannot be separated by a worker crash.
            attempts = int(await redis.incr(key))
            if attempts == 1:
                await redis.expire(key, int(window_seconds))
        if attempts > limit:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail
            )
    except HTTPException:
        raise
    except Exception as e:
        # Redis 不可用时不阻断业务（fail-open：保可用性）。但**不再静默**——登录爆破等
        # 安全限流在 Redis 挂时会失效，必须留痕告警，让降级可见、可排查（批5）。
        category = key.rsplit(":", 1)[0]
        category_hash = sha256(category.encode("utf-8")).hexdigest()[:12]
        logger.warning(
            "rate limit degraded (fail-open) category_hash=%s error_type=%s",
            category_hash,
            type(e).__name__,
        )
        return
