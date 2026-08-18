from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, and_
from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, field_validator
import uuid

from app.core.deps import DBSession, CurrentUser
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.models.room_block import RoomBlock, BlockType
from app.services.audit import log_action_tx

router = APIRouter(prefix="/room-blocks", tags=["room-blocks"])


class RoomBlockCreate(BaseModel):
    room_id: str
    block_type: BlockType
    start_date: date
    end_date: date
    reason: Optional[str] = None

    @field_validator("end_date")
    @classmethod
    def end_after_start(cls, v, info):
        if "start_date" in info.data and v <= info.data["start_date"]:
            raise ValueError("结束日期必须晚于开始日期")
        return v


class RoomBlockOut(BaseModel):
    block_id: str
    room_id: str
    block_type: BlockType
    start_date: date
    end_date: date
    reason: Optional[str] = None
    created_by: Optional[str] = None
    # 之前 created_at 标 str → DB 实际是 datetime，from_attributes 模式下
    # pydantic v2 不会自动 str()，导致 GET/POST /room-blocks 返回真实数据时 500。
    # 改成 datetime，pydantic 序列化为 ISO 8601 字符串。
    created_at: datetime

    model_config = {"from_attributes": True}


BLOCK_TYPE_LABELS = {
    "owner_use": "房东自住",
    "maintenance": "维修",
    "reserved": "预留",
    "other": "其他",
}


@router.get("", response_model=list[RoomBlockOut])
async def list_blocks(
    db: DBSession,
    current_user: CurrentUser,
    room_id: Optional[str] = Query(default=None),
    year: Optional[int] = Query(default=None),
    month: Optional[int] = Query(default=None),
):
    q = select(RoomBlock)
    if room_id:
        q = q.where(RoomBlock.room_id == room_id)
    if year and month:
        from calendar import monthrange
        _, days = monthrange(year, month)
        month_start = date(year, month, 1)
        month_end = date(year, month, days)
        q = q.where(
            RoomBlock.start_date <= month_end,
            RoomBlock.end_date >= month_start,
        )
    q = q.order_by(RoomBlock.start_date)
    result = await db.execute(q)
    return result.scalars().all()


@router.post("", response_model=RoomBlockOut, status_code=201)
async def create_block(body: RoomBlockCreate, db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权创建锁房记录")

    # 与下单侧（room_availability.check_room_conflict）用同一把 Room 行锁串行化：
    # 否则并发「建单 + 锁房」各自都看不到对方未提交的行，双双通过校验，
    # 落库后就是本守卫要防的「锁房覆盖订单」状态。
    await db.execute(
        select(Room.room_id).where(Room.room_id == body.room_id).with_for_update()
    )

    # Check for overlapping blocks
    overlap_result = await db.execute(
        select(RoomBlock).where(
            RoomBlock.room_id == body.room_id,
            RoomBlock.start_date < body.end_date,
            RoomBlock.end_date > body.start_date,
        )
    )
    if overlap_result.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="该日期段已有锁房记录")

    # 锁房也要让路给已有订单：此前只查锁房重叠，可以对「已排房/在住」的
    # 订单日期直接锁房，甘特图上锁房条会覆盖并隐藏订单，前台误判空房。
    # 区间语义与订单一致（半开 [start, end)），谓词与 check_room_conflict 对齐。
    # 只查 order_rooms 不查老字段 Order.room_id：迁移保证每张订单至少有一行
    # order_room（见 tests 里的 invariant 注释），无 order_rooms 的订单在生产不存在。
    order_conflict = await db.execute(
        select(OrderRoom.order_room_id)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(
            OrderRoom.room_id == body.room_id,
            Order.is_deleted == False,
            Order.order_status != OrderStatus.cancelled,
            OrderRoom.check_in_date < body.end_date,
            OrderRoom.check_out_date > body.start_date,
        )
        .limit(1)
    )
    if order_conflict.scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="该日期段已有未取消的订单，无法锁房。请先处理订单（换房/改期/取消）再锁。",
        )

    block = RoomBlock(
        block_id="BLK-" + uuid.uuid4().hex[:12].upper(),
        created_by=current_user["user_id"],
        **body.model_dump(),
    )
    db.add(block)
    await log_action_tx(db, current_user["user_id"], "room_block.create", "room_block", block.block_id)
    await db.commit()
    await db.refresh(block)
    return block


@router.delete("/{block_id}")
async def delete_block(block_id: str, db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权删除锁房记录")

    result = await db.execute(select(RoomBlock).where(RoomBlock.block_id == block_id))
    block = result.scalar_one_or_none()
    if not block:
        raise HTTPException(status_code=404, detail="锁房记录不存在")

    await db.delete(block)
    await log_action_tx(db, current_user["user_id"], "room_block.delete", "room_block", block_id)
    await db.commit()
    return {"message": "锁房记录已删除"}
