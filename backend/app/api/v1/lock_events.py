"""慧享家门锁状态回调入口——lockLogPush 开锁推送（电量/在线自愈 + 低电告警）。

公开 endpoint（无 JWT）：慧享家服务器 POST 到 /lock-events/callback/{token}。
厂商推送体无签名 → 安全靠 URL 路径里的 LOCK_WEBHOOK_TOKEN 比对，对不上直接 401。
处理逻辑在 services/lock/events；本文件只做 HTTP 传输：鉴权 + 快返 200
（未知锁 / 坏体也回 200，避免厂商重推风暴）。
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.config import settings
from app.core.deps import DBSession
from app.services.lock.events import process_lock_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/lock-events", tags=["lock-events"])


@router.post("/callback/{token}")
async def lock_event_callback(token: str, request: Request, db: DBSession):
    if not settings.LOCK_WEBHOOK_TOKEN or token != settings.LOCK_WEBHOOK_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")
    try:
        body = await request.json()
    except Exception:
        body = {}
    await process_lock_event(db, body)
    return {"code": 0, "msg": "ok"}
