"""一次性跨子域登录交接码 (#46)。

跨子域登录(admin.* ↔ www.*)不能把 access/refresh token 放进跳转 URL —— 会进
浏览器历史、Referer、边缘/CDN 访问日志,7 天 refresh token 泄漏即账号接管。

改为:登录侧把 token 暂存到 Redis 并换取一个短时效、一次性的 code,跳转只带 code;
目标子域的 /auth/accept 用 code 向后端换回真正的 token。token 永不进 URL。

- TTL 90s:足够一次跳转,过期即失效。
- 一次性:exchange 时用原子 Lua 取走值,并发兑换和重放均无效。
- code 用 secrets 生成,足够熵。
"""
from __future__ import annotations

import json
import secrets
from typing import Optional

import redis.asyncio as aioredis

_PREFIX = "auth:handoff:"
_TTL_SECONDS = 90
_CONSUME_SCRIPT = """
local value = redis.call("GET", KEYS[1])
if value then
    redis.call("DEL", KEYS[1])
end
return value
"""


def _key(code: str) -> str:
    return f"{_PREFIX}{code}"


async def store(redis: aioredis.Redis, payload: dict) -> str:
    """暂存交接 payload,返回一次性 code。Redis 不可用时抛异常 → 端点返回 503,
    前端留在登录页重试,不得将 token 放入 URL。"""
    code = secrets.token_urlsafe(24)
    await redis.set(_key(code), json.dumps(payload), ex=_TTL_SECONDS)
    return code


async def consume(redis: aioredis.Redis, code: str) -> Optional[dict]:
    """用 code 换回 payload 并立即删除(一次性)。无效/过期/已用返回 None。"""
    if not code:
        return None
    # GET + DELETE 会让并发请求同时读到 payload；删除失败也不能返回凭证。
    # 单个 EVAL 在 Redis 内原子取走值，兼容尚不支持 GETDEL 的 Redis。
    # 任何 Redis 错误都交给端点返回 503，绝不拆成独立读写进行重试。
    raw = await redis.eval(_CONSUME_SCRIPT, 1, _key(code))
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else None
    except (ValueError, TypeError):
        return None
