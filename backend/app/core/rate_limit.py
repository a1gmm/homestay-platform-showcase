"""轻量级 Redis 计数限流。Redis 不可用时静默放行（degrade gracefully）。

用法：
    @router.post(...)
    async def handler(request: Request, redis: RedisClient, ...):
        await enforce_rate_limit(redis, f"book:{request.client.host}", limit=10, window_seconds=60)
        ...
"""
import logging

from fastapi import HTTPException, status
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


async def enforce_rate_limit(
    redis: Redis,
    key: str,
    *,
    limit: int,
    window_seconds: int,
    detail: str = "请求过于频繁，请稍后再试",
) -> None:
    try:
        attempts = await redis.get(key)
        if attempts and int(attempts) >= limit:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=detail)
        pipe = redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds)
        await pipe.execute()
    except HTTPException:
        raise
    except Exception as e:
        # Redis 不可用时不阻断业务（fail-open：保可用性）。但**不再静默**——登录爆破等
        # 安全限流在 Redis 挂时会失效，必须留痕告警，让降级可见、可排查（批5）。
        logger.warning("rate limit degraded (fail-open) key=%s: %s", key, e)
        return
