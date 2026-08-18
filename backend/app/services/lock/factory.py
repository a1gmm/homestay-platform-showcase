"""get_lock_provider —— 按 settings.LOCK_PROVIDER 选 provider 实现（见 plan §15.2）。

manual（默认）= 兜底人工；hxjiot = 慧享家真厂商。凭证全走 env（HXJIOT_*），不硬编码。
hxjiot 复用一个长生命周期 httpx.AsyncClient；msgId 用秒级单调自增（见 util.make_msgid_factory，
现场实测慧享家按 int32 解析 msgId，13 位毫秒戳会溢出报「协议解释异常」）。
"""
from __future__ import annotations

import httpx

from app.core.config import settings
from app.services.lock.hxjiot import HxjiotProvider
from app.services.lock.manual import ManualProvider
from app.services.lock.transport import make_httpx_transport
from app.services.lock.util import make_msgid_factory

_shared_client: httpx.AsyncClient | None = None
# 复用同一 HxjiotProvider 实例，让 tokenId(7天有效) 的进程内缓存真正生效。
# 否则每次办理入住都新建实例、缓存永远空 → 白跑一次 apartmentLogin 往返拖慢入住。
# 按凭证 key 缓存：凭证轮换 → 自动重建实例用新账号登录。
_hxjiot_provider: HxjiotProvider | None = None
_hxjiot_key: tuple | None = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient()
    return _shared_client


def get_lock_provider():
    provider = (settings.LOCK_PROVIDER or "manual").lower()
    if provider == "manual":
        return ManualProvider()
    if provider == "hxjiot":
        global _hxjiot_provider, _hxjiot_key
        key = (settings.HXJIOT_BASE_URL, settings.HXJIOT_ACCOUNT_NAME, settings.HXJIOT_PASSWORD)
        if _hxjiot_provider is None or _hxjiot_key != key:
            _hxjiot_provider = HxjiotProvider(
                base_url=settings.HXJIOT_BASE_URL,
                account_name=settings.HXJIOT_ACCOUNT_NAME,
                password=settings.HXJIOT_PASSWORD,
                transport=make_httpx_transport(_get_client()),
                msgid_factory=make_msgid_factory(),
            )
            _hxjiot_key = key
        return _hxjiot_provider
    raise ValueError(f"Unknown LOCK_PROVIDER: {provider!r}")
