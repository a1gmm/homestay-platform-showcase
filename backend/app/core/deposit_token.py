"""押金小票上传页的签名令牌（HMAC-SHA256）。

入住时把这张令牌拼进飞书「上传押金小票」按钮的 URL；前台点开→免登录的拍照页。
令牌只授权「给这一个订单上传押金小票」，不含任何其它权限，且带过期时间
（默认退房后一段时间失效）。密钥复用 JWT_SECRET_KEY（生产必设），故不新增环境变量；
用固定域分隔串 _DOMAIN 与 JWT 等其它用途隔离，避免跨用途令牌互相冒用。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from app.core.config import settings

# 域分隔：签名 key 复用 JWT_SECRET_KEY，但把用途拌进 HMAC 消息，令牌无法跨用途冒用。
_DOMAIN = b"deposit-receipt:v1"
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _sign(payload_b64: str) -> str:
    key = settings.JWT_SECRET_KEY.encode("utf-8")
    mac = hmac.new(key, _DOMAIN + b"." + payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64url_encode(mac)


def make_deposit_token(
    order_id: str, *, ttl_seconds: int = DEFAULT_TTL_SECONDS, now: float | None = None
) -> str:
    """签一张「订单 order_id 的押金小票上传」令牌，ttl_seconds 后过期。"""
    issued = now if now is not None else time.time()
    exp = int(issued + ttl_seconds)
    payload_b64 = _b64url_encode(
        json.dumps({"oid": order_id, "exp": exp}, separators=(",", ":")).encode("utf-8")
    )
    # 分隔符用 ~（URL 未保留字符、非 base64url 字符、非 .）——令牌整段进 URL 路径，
    # 用 . 会让 Next.js/中间件把该段当静态文件（含扩展名）而 404。
    return f"{payload_b64}~{_sign(payload_b64)}"


def verify_deposit_token(token: str, *, now: float | None = None) -> str | None:
    """校验令牌，通过则返回 order_id，否则（签名不符/过期/损坏）返回 None。"""
    if not isinstance(token, str) or "~" not in token:
        return None
    payload_b64, _, sig = token.partition("~")
    if not hmac.compare_digest(sig, _sign(payload_b64)):
        return None
    try:
        data = json.loads(_b64url_decode(payload_b64))
    except Exception:
        return None
    exp = data.get("exp")
    oid = data.get("oid")
    if not isinstance(exp, int) or not isinstance(oid, str) or not oid:
        return None
    current = now if now is not None else time.time()
    if current > exp:
        return None
    return oid
