"""一次性跨子域登录交接码 (#46)。

跨子域登录(admin.* ↔ www.*)不能把 access/refresh token 放进跳转 URL —— 会进
浏览器历史、Referer、边缘/CDN 访问日志,7 天 refresh token 泄漏即账号接管。

改为:登录侧把 token 暂存到 Redis 并换取一个短时效、一次性的 code,跳转只带 code;
目标子域的 /auth/accept 用 code 向后端换回真正的 token。token 永不进 URL。

- TTL 90s:足够一次跳转,过期即失效。
- 一次性:exchange 时 delete,重放无效。
- code 用 secrets 生成,足够熵。
"""
from __future__ import annotations

import json
import logging
import secrets
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_PREFIX = "auth:handoff:"
_TTL_SECONDS = 90


def _key(code: str) -> str:
    return f"{_PREFIX}{code}"


async def store(redis: aioredis.Redis, payload: dict) -> str:
    """暂存交接 payload,返回一次性 code。Redis 不可用时抛异常 → 端点返回 503,
    由前端降级回 URL 老方式(见 login/page.tsx handoffRedirect)。"""
    code = secrets.token_urlsafe(24)
    await redis.set(_key(code), json.dumps(payload), ex=_TTL_SECONDS)
    return code


async def consume(redis: aioredis.Redis, code: str) -> Optional[dict]:
    """用 code 换回 payload 并立即删除(一次性)。无效/过期/已用返回 None。"""
    if not code:
        return None
    key = _key(code)
    raw = await redis.get(key)
    if raw is None:
        return None
    # 一次性:无论后续如何,先删掉
    try:
        await redis.delete(key)
    except Exception as exc:  # pragma: no cover - 删除失败不应阻塞换取
        logger.warning("Failed to delete handoff code: %s", exc)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None
