"""HxjiotProvider 的真实 HTTP transport（httpx）。

慧享家要求 Content-Type: text/json;charset=utf-8，与 httpx 默认 application/json 不同，
故自行 json.dumps（ensure_ascii=False 容中文 keyName）后用 content= 发送，保留厂商头。
"""
from __future__ import annotations

import json

import httpx

# 生产服务端实测：成功 ~3s 返回，锁卡顿时它自己 ~15s 才返 406 超时。
# 6s 对正常成功(~3s)留了 2x 余量，又把「锁离线时前台干等」从 12s 砍半；
# 更慢的落 pending 交 retry_pending(Celery) 兜底——pending 码本身已带可用密码，前台照样能给客人。
_DEFAULT_TIMEOUT = 6.0


def make_httpx_transport(client: httpx.AsyncClient):
    """返回一个 async(url, headers, payload)->dict 的 transport，注入共享 AsyncClient。"""

    async def _transport(url: str, headers: dict, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = await client.post(url, headers=headers, content=body, timeout=_DEFAULT_TIMEOUT)
        return resp.json()

    return _transport
