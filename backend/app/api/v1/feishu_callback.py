"""飞书卡片「回传交互」回调入口——保洁点「打扫完了」回写房态。

公开 endpoint（无 JWT）：飞书服务器直接 POST 到这里。安全靠
verify_callback_token（header.token == 应用 Verification Token）；未配置 token 时
一律拒绝。纯解析/校验逻辑在 services/feishu_card_callback，动作编排在
services/feishu_card_actions（与 ws 长连接入口共用），本文件只做
HTTP 传输层：验签 / challenge / 新老管道应答格式。
"""
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from app.core.config import settings
from app.core.deps import DBSession
from app.services.feishu_card_actions import process_card_action
from app.services.feishu_card_callback import (
    build_toast_response,
    parse_card_callback,
    verify_callback_token,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feishu", tags=["feishu"])


@router.post("/card-callback")
async def card_callback(request: Request, db: DBSession, background_tasks: BackgroundTasks):
    try:
        body = await request.json()
    except ValueError:  # 公开端点：垃圾 body 给 400，不能 500
        raise HTTPException(status_code=400, detail="无效的 JSON body")
    parsed = parse_card_callback(body)

    # 配置回调地址时的握手：原样回 challenge（此请求不带业务 token，不做校验）。
    if parsed["kind"] == "challenge":
        return {"challenge": parsed["challenge"]}

    # 其余回调一律验签：header.token 必须等于应用 Verification Token。
    if not verify_callback_token(body, settings.FEISHU_CARD_VERIFICATION_TOKEN):
        raise HTTPException(status_code=401, detail="飞书回调验签失败")

    if parsed["kind"] == "action":
        toast = await process_card_action(
            db,
            value=parsed.get("value") or {},
            open_id=parsed.get("open_id", ""),
            background_tasks=background_tasks,
            message_id=parsed.get("message_id", ""),
        )
        if parsed.get("legacy"):
            # 老管道支持自定义 toast；成功仍沿用空对象确认，错误必须让点击人可见。
            if (toast.get("toast") or {}).get("type") == "error":
                return toast
            return {}
        return toast

    # 未知事件：仍 200 应答，避免飞书重试刷屏；留痕便于诊断（trigger_v1 曾被
    # 当 unknown 静默打发一整天，没有任何日志线索）。
    header = body.get("header") if isinstance(body, dict) else {}
    logger.warning(
        "飞书回调未识别 kind=%s event_type=%s",
        parsed["kind"], (header or {}).get("event_type") if isinstance(header, dict) else None,
    )
    return build_toast_response("已收到")
