"""
OTP lifecycle: generate, store (Redis), verify, rate limit.

Keys:
- otp:{phone}          → the 6-digit code, TTL = OTP_CODE_TTL_SECONDS
- otp:cooldown:{phone} → cooldown flag, TTL = OTP_PHONE_COOLDOWN_SECONDS
- otp:count:{phone}:{YYYYMMDD} → daily send count (北京日历日), TTL ~25h
- otp:ip:{ip}:{YYYYMMDDHH}     → hourly send count per IP, TTL ~1h
"""
import random
from datetime import datetime, timezone
from typing import Optional

import redis.asyncio as aioredis
from fastapi import HTTPException, status

from app.core.config import settings
from app.core.datetime_helpers import today_cn


def _gen_code() -> str:
    return f"{random.randint(0, 999999):06d}"


async def _safe_redis_get(redis, key):
    try:
        return await redis.get(key)
    except Exception:
        return None


async def _safe_redis_set(redis, key, value, ex=None):
    try:
        await redis.set(key, value, ex=ex)
    except Exception:
        pass


async def _safe_redis_delete(redis, key):
    try:
        await redis.delete(key)
    except Exception:
        pass


async def _safe_redis_incr(redis, key, expire_sec):
    try:
        await redis.incr(key)
        await redis.expire(key, expire_sec)
    except Exception:
        pass


async def check_send_limits(redis: aioredis.Redis, phone: str, ip: Optional[str]) -> None:
    """Raise HTTPException(429) if any rate limit is violated. Graceful when Redis is unavailable."""
    # Cooldown
    cooldown_key = f"otp:cooldown:{phone}"
    if await _safe_redis_get(redis, cooldown_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"请在 {settings.OTP_PHONE_COOLDOWN_SECONDS} 秒后再试",
        )

    # Daily limit per phone
    today = today_cn().strftime("%Y%m%d")  # 日额度北京零点重置；IP 小时桶保持 UTC
    count_key = f"otp:count:{phone}:{today}"
    count = int(await _safe_redis_get(redis, count_key) or 0)
    if count >= settings.OTP_PHONE_DAILY_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="今日验证码发送次数已达上限",
        )

    # Hourly limit per IP (if provided)
    if ip:
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        ip_key = f"otp:ip:{ip}:{hour}"
        ip_count = int(await _safe_redis_get(redis, ip_key) or 0)
        if ip_count >= settings.OTP_IP_HOURLY_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="IP 请求频繁，请稍后再试",
            )


async def generate_and_store(redis: aioredis.Redis, phone: str, ip: Optional[str]) -> str:
    """Generate code, best-effort store in Redis. Graceful when Redis is unavailable
    (send-otp endpoint still returns OK so DEMO_OTP_CODE can be used to verify)."""
    await check_send_limits(redis, phone, ip)
    code = _gen_code()
    await _safe_redis_set(redis, f"otp:{phone}", code, ex=settings.OTP_CODE_TTL_SECONDS)
    await _safe_redis_set(redis, f"otp:cooldown:{phone}", "1", ex=settings.OTP_PHONE_COOLDOWN_SECONDS)

    today = today_cn().strftime("%Y%m%d")  # 日额度北京零点重置；IP 小时桶保持 UTC
    await _safe_redis_incr(redis, f"otp:count:{phone}:{today}", 90_000)

    if ip:
        hour = datetime.now(timezone.utc).strftime("%Y%m%d%H")
        await _safe_redis_incr(redis, f"otp:ip:{ip}:{hour}", 3700)

    return code


# 每个手机号在一个验证码生命周期内允许的最大错误尝试次数。超过即锁定并作废该码,
# 防止 6 位码被在线爆破(发送侧已限速,验证侧此前完全不限速)(#51)。
MAX_VERIFY_ATTEMPTS = 5


async def verify(redis: aioredis.Redis, phone: str, code: str) -> bool:
    """Returns True if code matches. Graceful when Redis is unavailable
    (DEMO_OTP_CODE always works; real OTP requires Redis).

    每手机号的错误尝试次数受限:连续 MAX_VERIFY_ATTEMPTS 次错误后锁定并作废当前码,
    必须重新发送 (#51)。Redis 不可用时计数失效,回退为不限速(与既有 fail-open 一致)。
    """
    # DEMO 万能验证码 —— 仅演示期使用,不依赖 Redis
    if settings.DEMO_OTP_CODE and code == settings.DEMO_OTP_CODE:
        await _safe_redis_delete(redis, f"otp:{phone}")
        return True

    fail_key = f"otp:verify_fail:{phone}"
    fail_count = int(await _safe_redis_get(redis, fail_key) or 0)
    if fail_count >= MAX_VERIFY_ATTEMPTS:
        # 锁定:作废当前码,要求重新发送
        await _safe_redis_delete(redis, f"otp:{phone}")
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="验证码错误次数过多,请重新获取验证码",
        )

    stored = await _safe_redis_get(redis, f"otp:{phone}")
    if not stored or stored != code:
        # 记一次失败(TTL 与验证码一致,失效后自动清零)
        await _safe_redis_incr(redis, fail_key, settings.OTP_CODE_TTL_SECONDS)
        return False

    await _safe_redis_delete(redis, f"otp:{phone}")
    await _safe_redis_delete(redis, fail_key)
    return True
