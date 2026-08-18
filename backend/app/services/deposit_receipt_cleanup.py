"""订单走完整流程（completed）后，删掉 OSS 上的押金小票图，省服务器空间。

备注文本保留（很小、留档用）；被删的图在 metadata 里清掉 image 字段、打 image_deleted 标记。
由 orders.transition_status 在订单→completed 时通过 BackgroundTask 调度。best-effort：
开独立 session，任何异常只 log，绝不影响已提交的订单完成。
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


async def cleanup_deposit_receipt_images(order_id: str) -> None:
    if not order_id:
        return
    try:
        from sqlalchemy import select

        from app.core.database import AsyncSessionLocal
        from app.models.order import Order
        from app.services import oss_service

        async with AsyncSessionLocal() as db:
            # 加行锁 + 取最新 metadata，避免与并发写覆盖（同上传路径的考量）。
            order = (await db.execute(
                select(Order).where(Order.order_id == order_id)
                .with_for_update().execution_options(populate_existing=True)
            )).scalar_one_or_none()
            if order is None:
                return
            receipts = (order.metadata_ or {}).get("deposit_receipts")
            if not isinstance(receipts, list) or not receipts:
                return
            # 构造全新对象图（新 list + 新 dict），别原地改——JSONB 原地改 SQLAlchemy 检测不到
            # 变更、不会写库。
            new_receipts: list = []
            changed = False
            for r in receipts:
                key = r.get("object_key") if isinstance(r, dict) else None
                if not key or r.get("image_deleted"):
                    new_receipts.append(r)
                    continue
                try:
                    await asyncio.to_thread(oss_service.delete_object, key)
                except Exception:  # noqa: BLE001 — 单张删失败不挡其它
                    logger.warning("删押金小票 OSS 对象失败 key=%s", key, exc_info=True)
                    new_receipts.append(r)
                    continue
                nr = {k: v for k, v in r.items()
                      if k not in ("oss_url", "object_key", "image_key")}
                nr["image_deleted"] = True
                new_receipts.append(nr)
                changed = True
            if changed:
                meta = dict(order.metadata_ or {})
                meta["deposit_receipts"] = new_receipts
                order.metadata_ = meta
                await db.commit()
    except Exception:  # noqa: BLE001 — best-effort，绝不影响订单完成
        logger.warning("清理押金小票图失败 order=%s", order_id, exc_info=True)
