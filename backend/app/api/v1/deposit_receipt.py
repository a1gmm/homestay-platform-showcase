"""押金小票存档（公开，免登录，靠签名令牌授权）。

前台从飞书入住卡片上的「上传押金小票」按钮打开本页 → 拍照上传 **或** 手打备注 →
挂到订单 metadata_['deposit_receipts']。退房时退押金卡内嵌显示小票图；没图有备注则显示备注。

图片与备注**二选一**即可（图片跨海上传偶发超时，备注是可靠兜底）。

Public (no auth)：
- GET  /deposit-receipt/{token}/meta  — 校验令牌，回订单信息给页面确认
- POST /deposit-receipt/{token}        — 收图/备注存档

令牌只授权「给这一个订单存档」，见 app/core/deposit_token。
"""
import asyncio

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from sqlalchemy import select

from app.core.datetime_helpers import now_cn
from app.core.deps import DBSession
from app.core.deposit_token import verify_deposit_token
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.services import oss_service
from app.services.audit import log_action_tx
from app.services.feishu_lead_alert import upload_image_to_feishu

router = APIRouter(prefix="/deposit-receipt", tags=["deposit-receipt"])

_INVALID = HTTPException(status_code=404, detail="链接无效或已过期")
# 一单存档条数上限：防止令牌泄露/误点被反复上传，把订单 metadata JSONB 撑爆。
_MAX_RECEIPTS = 20
_MAX_NOTE_LEN = 200


async def _resolve_order(token: str, db) -> Order:
    order_id = verify_deposit_token(token)
    if not order_id:
        raise _INVALID
    order = await db.get(Order, order_id)
    if order is None:
        raise _INVALID
    return order


async def _room_label(db, order_id: str) -> str:
    """订单房间名（多房拼「、」）。best-effort，取不到返回空串。"""
    room_ids = (await db.execute(
        select(OrderRoom.room_id).where(OrderRoom.order_id == order_id)
    )).scalars().all()
    if not room_ids:
        return ""
    names = (await db.execute(
        select(Room.room_name).where(Room.room_id.in_(room_ids))
    )).scalars().all()
    return "、".join(n for n in names if n)


def _receipts_of(order: Order) -> list:
    return (order.metadata_ or {}).get("deposit_receipts", [])


@router.get("/{token}/meta")
async def get_receipt_meta(token: str, db: DBSession):
    """页面进页先调：确认令牌有效 + 展示订单/客人/房间，避免存错单。押金金额不展示。"""
    order = await _resolve_order(token, db)
    return {
        "order_id": order.order_id,
        "guest_name": order.guest_name,
        "room_label": await _room_label(db, order.order_id),
        "existing_count": len(_receipts_of(order)),
    }


@router.post("/{token}")
async def upload_receipt(
    token: str, db: DBSession,
    file: UploadFile | None = File(None),
    note: str = Form(""),
):
    """存一条押金存档：图片和备注**二选一**（可都给）。图→OSS+飞书；备注→存文本。"""
    order = await _resolve_order(token, db)
    if order.order_status == OrderStatus.cancelled:
        raise HTTPException(status_code=409, detail="订单已取消，无法存档")

    note = (note or "").strip()[:_MAX_NOTE_LEN]
    has_file = file is not None and file.filename

    # 读图片（若给了）。限量读防 OOM。
    content = b""
    content_type = ""
    if has_file:
        content = await file.read(oss_service.MAX_IMAGE_BYTES + 1)
        if len(content) > oss_service.MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="图片过大（上限 5MB）")
        content_type = (file.content_type or "").lower()
        if content and content_type not in oss_service.ALLOWED_CONTENT_TYPES:
            raise HTTPException(status_code=415, detail="仅支持图片（JPG/PNG）")

    has_image = bool(content)
    # 图片和备注至少给一个。
    if not has_image and not note:
        raise HTTPException(status_code=400, detail="请拍照上传小票，或手打一条备注")

    # 条数上限在写 OSS 前先挡，避免超限时仍上传出孤儿 OSS 对象。
    if len(_receipts_of(order)) >= _MAX_RECEIPTS:
        raise HTTPException(
            status_code=409, detail=f"本单存档已达上限（{_MAX_RECEIPTS} 条）"
        )

    entry: dict = {"note": note or None, "uploaded_at": now_cn().isoformat()}
    if has_image:
        # OSS 存档（内部已重试跨海超时）+ 传飞书拿 image_key，两件事互不依赖 → 并发。
        # OSS 失败会抛 → 500 不写脏数据；飞书 best-effort（失败 None，卡降级不显示图）。
        (oss_url, object_key), image_key = await asyncio.gather(
            asyncio.to_thread(
                oss_service.upload_image, content, content_type, f"deposit-{order.order_id}"
            ),
            upload_image_to_feishu(content, content_type),
        )
        entry.update({
            "oss_url": oss_url, "object_key": object_key,
            "image_key": image_key, "content_type": content_type,
        })

    # 重取订单行加行锁 + 最新 metadata_ 再追加：metadata_ 是共享 JSONB 热字段
    # （price_locked/ota_subsidy 等），上面几个 await 期间可能有 bypms 同步写同一行，
    # 不加锁按内存旧值整体重赋值会把它们覆盖掉。
    locked = (await db.execute(
        select(Order).where(Order.order_id == order.order_id)
        .with_for_update().execution_options(populate_existing=True)
    )).scalar_one()
    meta = dict(locked.metadata_ or {})
    receipts = list(meta.get("deposit_receipts", []))
    if len(receipts) >= _MAX_RECEIPTS:  # 锁内二次确认（并发）
        raise HTTPException(
            status_code=409, detail=f"本单存档已达上限（{_MAX_RECEIPTS} 条）"
        )
    receipts.append(entry)
    meta["deposit_receipts"] = receipts
    locked.metadata_ = meta  # 整体重赋值，SQLAlchemy 才认 JSONB 变更

    await log_action_tx(
        db, None, "order.deposit_receipt_upload", "order", order.order_id,
        after_data={"has_image": has_image, "has_note": bool(note), "count": len(receipts)},
        notes="押金存档（飞书存档页：图片/备注）",
    )
    await db.commit()
    return {"ok": True, "count": len(receipts)}
