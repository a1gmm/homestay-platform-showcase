import asyncio

from fastapi import APIRouter, HTTPException, Query, UploadFile, File
from pydantic import BaseModel
from sqlalchemy import select, and_, delete, update
from datetime import date, datetime, timedelta, timezone

from app.core.datetime_helpers import today_cn
from app.core.trial_tags import trial_tag_for_notes
from typing import Optional
import enum
import uuid

from app.core.deps import DBSession, CurrentUser
from app.models.room import Room, RoomStatus
from app.models.order import Order, OrderStatus, BookingType
from app.models.pricing import PricingRecord
from app.models.task import Task
from app.models.expense import Expense, ExpenseCategory
from app.models.room_image import RoomImage
from app.schemas.room import (
    RoomCreate,
    RoomUpdate,
    RoomOut,
    AvailabilityCheckRequest,
    RoomPricingPoint,
    BulkShareRatiosRequest,
)
from app.schemas.room_cost_share import (
    CostShareRuleOut,
    CostShareRulesUpsertRequest,
    CostShareRulesBulkRequest,
)
from app.models.room_cost_share import RoomCostShareRule
from app.services.room_availability import check_room_conflict, get_available_rooms
from app.services.audit import log_action, log_action_tx
from app.services.pricing_service import calculate_room_pricing_for_range
from app.services import oss_service
from app.models.room_block import RoomBlock

MAX_IMAGES_PER_ROOM = 10

router = APIRouter(prefix="/rooms", tags=["rooms"])


@router.get("", response_model=list[RoomOut])
async def list_rooms(db: DBSession, current_user: CurrentUser):
    from app.services.room_availability import compute_effective_status
    rooms = (await db.execute(select(Room).where(Room.is_deleted == False).order_by(Room.room_id))).scalars().all()
    # 用北京时间当天,而非服务器 UTC 的 date.today();Railway 跑 UTC,凌晨 0-8 点会算错一天 (#47)
    eff = await compute_effective_status(db, today_cn())
    out = []
    for r in rooms:
        data = RoomOut.model_validate(r)
        data.effective_status = eff.get(r.room_id)
        out.append(data)
    return out


@router.post("", response_model=RoomOut, status_code=201)
async def create_room(body: RoomCreate, db: DBSession, current_user: CurrentUser):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可添加房间")
    data = body.model_dump()
    beds = data.pop("beds", None)  # beds 是 metadata 派生属性，单独 set 避开 __init__ 注入顺序
    room = Room(**data)
    if beds is not None:
        room.beds = beds
    db.add(room)
    await log_action_tx(db, current_user["user_id"], "room.create", "room", room.room_id)
    await db.commit()
    await db.refresh(room)
    return room


@router.get("/{room_id}", response_model=RoomOut)
async def get_room(room_id: str, db: DBSession, current_user: CurrentUser):
    result = await db.execute(select(Room).where(Room.room_id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    return room


@router.patch("/{room_id}", response_model=RoomOut)
async def update_room(room_id: str, body: RoomUpdate, db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权修改房间")
    result = await db.execute(select(Room).where(Room.room_id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    data = body.model_dump(exclude_none=True)
    old_status = room.room_status
    status_changed = "room_status" in data and data["room_status"] != old_status
    # 状态变化时记录上一个状态，供"结束维修/解除锁房一键恢复"使用。
    if status_changed:
        room.previous_status = old_status
    for field, value in data.items():
        setattr(room, field, value)
    await log_action_tx(
        db,
        current_user["user_id"],
        "room.update",
        "room",
        room_id,
        before_data={"room_status": old_status.value} if status_changed else None,
        after_data={"room_status": room.room_status.value} if status_changed else None,
    )
    await db.commit()
    await db.refresh(room)
    return room


@router.delete("/{room_id}")
async def delete_room(room_id: str, db: DBSession, current_user: CurrentUser):
    """下线（软删）房间，仅管理员可操作。

    删除 = 归档：房间打 is_deleted 标记，一行数据都不物理删。房态列表/甘特/
    可订房/统计分母 会过滤掉它，但历史订单/收款/退款/分账仍能 join 出房名，
    完整可溯源。业主收回、不再托管的房间用这个「下线」。
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可下线房间")

    result = await db.execute(select(Room).where(Room.room_id == room_id))
    room = result.scalar_one_or_none()
    if not room or room.is_deleted:
        raise HTTPException(status_code=404, detail="房间不存在")

    # 在住/预留的房间不能直接下线——先让客人退房，避免甘特与账目错乱。
    if room.room_status in (RoomStatus.occupied, RoomStatus.reserved):
        raise HTTPException(
            status_code=409,
            detail="该房间当前有客人在住或已预留，不能下线。请先完成退房再下线。",
        )

    # 更可靠的拦截：按「实际订单日期」而非静态房态枚举查未来未取消订单。
    # 静态房态可能与按日期的订单不同步；有已确认的未来订单（今天还没退房）
    # 就下线，前台看不到排房、客人到店会发现房被撤了。历史订单不拦（软删溯源）。
    from app.models.order_room import OrderRoom
    from app.core.datetime_helpers import today_cn
    future_q = (
        select(OrderRoom.order_room_id)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(
            OrderRoom.room_id == room_id,
            Order.is_deleted == False,
            Order.order_status != OrderStatus.cancelled,
            OrderRoom.check_out_date > today_cn(),
        )
        .limit(1)
    )
    if (await db.execute(future_q)).scalar_one_or_none():
        raise HTTPException(
            status_code=409,
            detail="该房间还有未完成的订单（在住或未来入住），不能下线。请先处理这些订单再下线。",
        )

    room.is_deleted = True
    room.deleted_at = datetime.now(timezone.utc)
    await log_action_tx(db, current_user["user_id"], "room.delete", "room", room_id)
    await db.commit()

    return {"message": "房间已下线（历史订单与账目已保留，可随时溯源）"}


@router.post("/availability/check")
async def check_availability(body: AvailabilityCheckRequest, db: DBSession, current_user: CurrentUser):
    """Check if a room is available for given dates."""
    from datetime import date as date_type
    checkin = date_type.fromisoformat(body.check_in_date)
    checkout = date_type.fromisoformat(body.check_out_date)
    has_conflict = await check_room_conflict(
        db, body.room_id, checkin, checkout, exclude_order_id=body.exclude_order_id
    )
    return {"room_id": body.room_id, "available": not has_conflict}


@router.get("/availability/list")
async def availability_list(
    db: DBSession,
    current_user: CurrentUser,
    check_in: date = Query(...),
    check_out: date = Query(...),
    exclude_order_id: str | None = Query(None),
):
    """批量：返回 [check_in, check_out) 窗口内可订的 room_id 列表（下单选房器用）。

    exclude_order_id：编辑订单场景传本单 id，本单自己的占用不算冲突。"""
    if check_out <= check_in:
        raise HTTPException(status_code=422, detail="退房日期必须晚于入住日期")
    ids = await get_available_rooms(db, check_in, check_out, exclude_order_id=exclude_order_id)
    return {"available_room_ids": ids}


def _even_split_price(actual_price, nights: int):
    """无 daily_prices 时按 actual_price/nights 均摊单晚价（保留 2 位小数）。"""
    if actual_price is None or nights <= 0:
        return None
    try:
        return round(float(actual_price) / nights, 2)
    except (TypeError, ValueError):
        return None


def _cell_price(orm, day: date):
    """取某一晚的房价：优先 daily_prices[day]，否则按 actual_price 均摊。"""
    dp = getattr(orm, "daily_prices", None) or {}
    key = day.isoformat()
    if key in dp and dp[key] not in (None, ""):
        try:
            return round(float(dp[key]), 2)
        except (TypeError, ValueError):
            pass
    nights = (orm.check_out_date - orm.check_in_date).days
    return _even_split_price(getattr(orm, "actual_price", None), nights)


@router.get("/availability/calendar")
async def availability_calendar(
    db: DBSession,
    current_user: CurrentUser,
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None),
    start: Optional[str] = Query(None, description="滚动窗口起始日 YYYY-MM-DD（与 days 配合，跨月）"),
    days: Optional[int] = Query(None, ge=1, le=90, description="滚动窗口天数（默认配合 start 使用）"),
):
    """房态占用矩阵（甘特图用）。

    两种模式：
    - 滚动窗口（D1，推荐）：传 ``start`` + ``days``，返回从起始日往后 days 天的连续窗口，
      天然跨月、跨年，前台可自由选起止段。
    - 自然月（向后兼容）：传 ``year`` + ``month``，返回该自然月。
    """
    if start is not None:
        try:
            month_start = date.fromisoformat(start)
        except ValueError:
            raise HTTPException(status_code=422, detail="start 必须是 YYYY-MM-DD 格式")
        span = days if days is not None else 30
        # month_end 为窗口最后一天（含），与自然月模式语义一致
        month_end = month_start + timedelta(days=span - 1)
    elif year is not None and month is not None:
        from calendar import monthrange
        _, days_in_month = monthrange(year, month)
        month_start = date(year, month, 1)
        month_end = date(year, month, days_in_month)
    else:
        raise HTTPException(status_code=422, detail="请提供 start+days（滚动窗口）或 year+month（自然月）")

    # Fetch all rooms
    rooms_result = await db.execute(select(Room).where(Room.is_deleted == False).order_by(Room.room_id))
    rooms = rooms_result.scalars().all()

    # Fetch active orders overlapping this month, joined to OrderRoom rows.
    # Multi-room: 多房订单的每行 OrderRoom 单独绘到对应房间行，使用 OrderRoom 自己的
    # check_in/out（每房可不同），不再用 Order 顶层日期。
    from app.models.order_room import OrderRoom
    rows_result = await db.execute(
        select(Order, OrderRoom)
        .join(OrderRoom, OrderRoom.order_id == Order.order_id)
        .where(
            Order.is_deleted == False,
            Order.order_status.not_in([OrderStatus.cancelled]),
            OrderRoom.room_id.isnot(None),
            OrderRoom.check_in_date <= month_end,
            OrderRoom.check_out_date >= month_start,
        )
    )
    order_room_pairs = rows_result.all()

    # Compute group classification from complete groups, not the visible viewport.
    from app.core.free_room import free_room_kind
    visible_orders = {order.order_id: order for order, _ in order_room_pairs}
    group_ids = {o.stay_group_id for o in visible_orders.values() if o.stay_group_id}
    group_kind: dict[str, str] = {}
    if group_ids:
        members = (await db.execute(
            select(Order).where(
                Order.stay_group_id.in_(group_ids),
                Order.is_deleted == False,
                Order.order_status != OrderStatus.cancelled,
            )
        )).scalars().all()
        by_group: dict[str, list[Order]] = {gid: [] for gid in group_ids}
        for member in members:
            by_group[member.stay_group_id].append(member)
        group_kind = {gid: free_room_kind(items) for gid, items in by_group.items()}

    # Build calendar
    calendar = {}
    for room in rooms:
        calendar[room.room_id] = {
            "room_id": room.room_id,
            "room_name": room.room_name,
            "room_status": room.room_status.value,
            "days": {}
        }

    for order, orm in order_room_pairs:
        if orm.room_id not in calendar:
            continue
        current = max(orm.check_in_date, month_start)
        end = min(orm.check_out_date, month_end + timedelta(days=1))
        channel_value = order.channel.value if hasattr(order.channel, "value") else order.channel
        payment_status_value = (
            order.payment_status.value if hasattr(order.payment_status, "value") else order.payment_status
        )
        trial_tag = trial_tag_for_notes(order.notes)
        while current < end:
            if month_start <= current <= month_end:
                calendar[orm.room_id]["days"][current.isoformat()] = {
                    "order_id": order.order_id,
                    "order_room_id": orm.order_room_id,
                    "stay_group_id": order.stay_group_id,  # 续住关联：同组相邻同房格连成一条横条
                    "guest_name": order.guest_name,
                    "status": order.order_status.value,
                    "channel": channel_value,
                    "payment_status": payment_status_value,
                    # D2: 甘特条直接显示 ￥单晚价（daily_prices 优先，否则均摊）
                    "price": _cell_price(orm, current),
                    # 试运营房型角标：备注命中关键词（当前=极海）时打标，前台一眼识别
                    "trial_tag": trial_tag,
                    "free_room_kind": (
                        group_kind.get(order.stay_group_id, "none")
                        if order.stay_group_id else
                        ("all" if order.is_ota_free_room else "none")
                    ),
                    "stay_settlement_kind": (
                        order.stay_settlement_kind.value
                        if order.stay_settlement_kind is not None else None
                    ),
                    "is_manually_managed": order.is_manually_managed,
                }
            current += timedelta(days=1)

    # Overlay room blocks (owner_use, maintenance, etc.)
    blocks_result = await db.execute(
        select(RoomBlock).where(
            RoomBlock.start_date <= month_end,
            RoomBlock.end_date >= month_start,
        )
    )
    for block in blocks_result.scalars().all():
        if block.room_id not in calendar:
            continue
        current = max(block.start_date, month_start)
        end = min(block.end_date, month_end + timedelta(days=1))
        block_type_value = block.block_type.value if isinstance(block.block_type, enum.Enum) else block.block_type
        while current < end:
            if month_start <= current <= month_end:
                calendar[block.room_id]["days"][current.isoformat()] = {
                    "order_id": None,
                    "guest_name": block_type_value,
                    "status": f"block:{block_type_value}",
                    # block_id 让前端能在甘特图上直接「解除锁房」(此前缺失，导致锁房只能创建无法删除)
                    "block_id": block.block_id,
                    "block_reason": block.reason,
                    "block_start": block.start_date.isoformat(),
                    "block_end": block.end_date.isoformat(),
                }
            current += timedelta(days=1)

    return list(calendar.values())


@router.get("/{room_id}/pricing", response_model=list[RoomPricingPoint])
async def get_room_pricing(
    room_id: str,
    days: int = Query(7, ge=1, le=30),
    db: DBSession = ...,
    current_user: CurrentUser = ...,
):
    """返回房间未来 N 天的定价记录，含来源 URL。"""
    today = today_cn()
    end_date = today + timedelta(days=days - 1)

    result = await db.execute(
        select(PricingRecord)
        .where(
            PricingRecord.room_id == room_id,
            PricingRecord.effective_date >= today,
            PricingRecord.effective_date <= end_date,
        )
        .order_by(PricingRecord.effective_date)
    )
    records = result.scalars().all()
    return [
        RoomPricingPoint(
            date=r.effective_date,
            recommended_price=r.recommended_price or r.price,
            base_price=r.base_price,
            competitor_avg_price=r.competitor_avg_price,
            source=r.source,
            source_url=r.source_url,
        )
        for r in records
    ]


@router.get("/{room_id}/pricing/detail")
async def get_room_pricing_detail(
    room_id: str,
    date_str: str = Query(..., alias="date"),
    db: DBSession = ...,
    current_user: CurrentUser = ...,
):
    """返回单日定价的详细信息，包括算法因子和竞品样本。"""
    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的日期格式，应为 YYYY-MM-DD")

    result = await db.execute(
        select(PricingRecord).where(
            PricingRecord.room_id == room_id,
            PricingRecord.effective_date == target_date,
        )
    )
    record = result.scalar_one_or_none()
    if not record:
        raise HTTPException(status_code=404, detail="未找到该日期的定价记录")

    factors = record.algorithm_factors or {}
    competitors = factors.get("competitors_sample") or []

    def _to_float(v):
        return float(v) if v is not None else None

    return {
        "date": record.effective_date.isoformat(),
        "recommended_price": _to_float(record.recommended_price or record.price),
        "base_price": _to_float(record.base_price),
        "competitor_avg_price": _to_float(record.competitor_avg_price),
        "factors": factors,
        "competitors": competitors,
        "source_url": record.source_url,
    }


@router.post("/{room_id}/pricing/trigger")
async def trigger_room_pricing(
    room_id: str,
    days: int = Query(30, ge=1, le=90),
    db: DBSession = ...,
    current_user: CurrentUser = ...,
):
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权触发定价任务")

    result = await db.execute(select(Room).where(Room.room_id == room_id))
    room = result.scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")

    today = today_cn()
    await calculate_room_pricing_for_range(db, room, today, days)
    await db.commit()
    return {"message": "定价已触发", "room_id": room_id, "days": days}


# ─── Room images (public GET + admin/operator write) ─────────────────────────


class RoomImageOut(BaseModel):
    image_id: str
    room_id: str
    url: str
    sort_order: int
    is_cover: bool
    content_type: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class RoomImagePatch(BaseModel):
    is_cover: Optional[bool] = None
    sort_order: Optional[int] = None


class RoomImageReorder(BaseModel):
    ordered_ids: list[str]


async def _fetch_room_or_404(db, room_id: str) -> Room:
    room = (await db.execute(select(Room).where(Room.room_id == room_id))).scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房间不存在")
    return room


def _ensure_admin_or_operator(current_user: dict) -> None:
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权操作房间图片")


async def _list_images(db, room_id: str) -> list[RoomImage]:
    rows = await db.execute(
        select(RoomImage)
        .where(RoomImage.room_id == room_id)
        .order_by(RoomImage.sort_order.asc(), RoomImage.created_at.asc())
    )
    return list(rows.scalars().all())


async def upload_image_for_room(
    db,
    room_id: str,
    content: bytes,
    content_type: str,
    size: int,
    uploaded_by: Optional[str],
) -> RoomImage:
    """Shared path: validate caps, push to OSS, insert row. Raises HTTPException."""
    if content_type not in oss_service.ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=400, detail="仅支持 JPEG / PNG / WEBP")
    if size > oss_service.MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="单张图片不能超过 5 MB")

    existing_count = await db.scalar(
        select(__import__("sqlalchemy").func.count())
        .select_from(RoomImage)
        .where(RoomImage.room_id == room_id)
    )
    if existing_count and existing_count >= MAX_IMAGES_PER_ROOM:
        raise HTTPException(
            status_code=409,
            detail=f"每间房最多 {MAX_IMAGES_PER_ROOM} 张图片，已达上限",
        )

    try:
        # OSS put_object 是同步阻塞调用：放线程池 + 超时，避免卡住整个事件循环（批5）。
        url, object_key = await asyncio.wait_for(
            asyncio.to_thread(oss_service.upload_image, content, content_type, room_id),
            timeout=20,
        )
    except oss_service.OSSNotConfigured as e:
        raise HTTPException(status_code=503, detail=f"OSS 未配置：{e}")
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="OSS 上传超时，请重试")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"OSS 上传失败：{e}")

    is_first = not existing_count
    image = RoomImage(
        image_id="IMG-" + uuid.uuid4().hex[:12].upper(),
        room_id=room_id,
        url=url,
        object_key=object_key,
        sort_order=(existing_count or 0),
        is_cover=is_first,  # 首张自动设为封面
        content_type=content_type,
        size_bytes=size,
        uploaded_by=uploaded_by,
    )
    db.add(image)
    await db.commit()
    await db.refresh(image)
    return image


async def patch_image(
    db, room_id: str, image_id: str, body: RoomImagePatch
) -> RoomImage:
    image = (
        await db.execute(
            select(RoomImage).where(
                RoomImage.image_id == image_id,
                RoomImage.room_id == room_id,
            )
        )
    ).scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="图片不存在")

    if body.is_cover is True:
        # Unset sibling covers atomically
        await db.execute(
            update(RoomImage)
            .where(RoomImage.room_id == room_id, RoomImage.image_id != image_id)
            .values(is_cover=False)
        )
        image.is_cover = True
    elif body.is_cover is False:
        image.is_cover = False

    if body.sort_order is not None:
        image.sort_order = body.sort_order

    await db.commit()
    await db.refresh(image)
    return image


async def delete_image_for_room(db, room_id: str, image_id: str) -> None:
    image = (
        await db.execute(
            select(RoomImage).where(
                RoomImage.image_id == image_id,
                RoomImage.room_id == room_id,
            )
        )
    ).scalar_one_or_none()
    if not image:
        raise HTTPException(status_code=404, detail="图片不存在")

    was_cover = image.is_cover
    object_key = image.object_key

    await db.delete(image)
    await db.commit()

    # Remove from OSS after DB commit — if OSS delete fails, we don't want
    # to be left with a dangling row.
    oss_service.delete_object(object_key)

    # Promote a replacement cover if we just deleted the cover
    if was_cover:
        next_image = (
            await db.execute(
                select(RoomImage)
                .where(RoomImage.room_id == room_id)
                .order_by(RoomImage.sort_order.asc(), RoomImage.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if next_image:
            next_image.is_cover = True
            await db.commit()


async def reorder_images_for_room(
    db, room_id: str, ordered_ids: list[str]
) -> list[RoomImage]:
    existing = await _list_images(db, room_id)
    existing_by_id = {img.image_id: img for img in existing}
    unknown = [iid for iid in ordered_ids if iid not in existing_by_id]
    if unknown:
        raise HTTPException(
            status_code=400, detail=f"图片不属于该房间：{unknown}"
        )
    missing = [img.image_id for img in existing if img.image_id not in ordered_ids]
    if missing:
        raise HTTPException(
            status_code=400, detail=f"reorder 列表缺少图片：{missing}"
        )

    for index, iid in enumerate(ordered_ids):
        existing_by_id[iid].sort_order = index
    await db.commit()
    return await _list_images(db, room_id)


# Public (no auth) — C-end booking detail uses this; B-end also reads it.
@router.get("/{room_id}/images", response_model=list[RoomImageOut])
async def list_room_images(room_id: str, db: DBSession):
    await _fetch_room_or_404(db, room_id)
    return await _list_images(db, room_id)


@router.post("/{room_id}/images", response_model=RoomImageOut, status_code=201)
async def upload_room_image(
    room_id: str,
    db: DBSession,
    current_user: CurrentUser,
    file: UploadFile = File(...),
):
    _ensure_admin_or_operator(current_user)
    await _fetch_room_or_404(db, room_id)
    content = await file.read()
    image = await upload_image_for_room(
        db,
        room_id=room_id,
        content=content,
        content_type=file.content_type or "",
        size=len(content),
        uploaded_by=current_user["user_id"],
    )
    # 图片 helper 内部已 commit，此处无调用方事务可加入，且图片非「单据消失」排查对象，保留 fire-and-forget（landmine）。
    await log_action(
        db, current_user["user_id"], "room_image.upload", "room", room_id
    )
    return image


@router.patch("/{room_id}/images/{image_id}", response_model=RoomImageOut)
async def update_room_image(
    room_id: str,
    image_id: str,
    body: RoomImagePatch,
    db: DBSession,
    current_user: CurrentUser,
):
    _ensure_admin_or_operator(current_user)
    await _fetch_room_or_404(db, room_id)
    image = await patch_image(db, room_id, image_id, body)
    # 图片 helper 内部已 commit，此处无调用方事务可加入，且图片非「单据消失」排查对象，保留 fire-and-forget（landmine）。
    await log_action(
        db, current_user["user_id"], "room_image.patch", "room_image", image_id
    )
    return image


@router.delete("/{room_id}/images/{image_id}")
async def delete_room_image(
    room_id: str,
    image_id: str,
    db: DBSession,
    current_user: CurrentUser,
):
    _ensure_admin_or_operator(current_user)
    await _fetch_room_or_404(db, room_id)
    await delete_image_for_room(db, room_id, image_id)
    # 图片 helper 内部已 commit，此处无调用方事务可加入，且图片非「单据消失」排查对象，保留 fire-and-forget（landmine）。
    await log_action(
        db, current_user["user_id"], "room_image.delete", "room_image", image_id
    )
    return {"ok": True}


@router.post("/{room_id}/images/reorder", response_model=list[RoomImageOut])
async def reorder_room_images(
    room_id: str,
    body: RoomImageReorder,
    db: DBSession,
    current_user: CurrentUser,
):
    _ensure_admin_or_operator(current_user)
    await _fetch_room_or_404(db, room_id)
    images = await reorder_images_for_room(db, room_id, body.ordered_ids)
    # 图片 helper 内部已 commit，此处无调用方事务可加入，且图片非「单据消失」排查对象，保留 fire-and-forget（landmine）。
    await log_action(
        db, current_user["user_id"], "room_image.reorder", "room", room_id
    )
    return images


# ─── issue#7 — 批量设置分成比例 ────────────────────────────────────────────────
@router.post("/share-ratios/bulk")
async def bulk_update_share_ratios(
    body: BulkShareRatiosRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """批量更新选中房间的三类分成比例。仅 admin 可用。

    任一 ratio 字段为 None → 跳过（仅覆盖请求中明确传的字段，避免误改）。
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可批量设置分成比例")

    if not body.room_ids:
        raise HTTPException(status_code=400, detail="未选择任何房间")

    result = await db.execute(
        select(Room).where(Room.room_id.in_(body.room_ids))
    )
    rooms = result.scalars().all()
    if not rooms:
        raise HTTPException(status_code=404, detail="选中的房间不存在")

    before_data: dict = {
        r.room_id: {
            "owner_share_ratio": str(r.owner_share_ratio),
            "share_ratio_trial": str(r.share_ratio_trial),
            "share_ratio_owner_self": str(r.share_ratio_owner_self),
        }
        for r in rooms
    }

    for r in rooms:
        if body.ratios.normal is not None:
            r.owner_share_ratio = body.ratios.normal
        if body.ratios.trial is not None:
            r.share_ratio_trial = body.ratios.trial
        if body.ratios.owner_self is not None:
            r.share_ratio_owner_self = body.ratios.owner_self

    await db.flush()
    for r in rooms:
        await db.refresh(r)

    after_data: dict = {
        r.room_id: {
            "owner_share_ratio": str(r.owner_share_ratio),
            "share_ratio_trial": str(r.share_ratio_trial),
            "share_ratio_owner_self": str(r.share_ratio_owner_self),
        }
        for r in rooms
    }
    await log_action_tx(
        db,
        current_user["user_id"],
        "rooms.bulk_update_share_ratios",
        "rooms",
        ",".join(body.room_ids[:5]) + ("..." if len(body.room_ids) > 5 else ""),
        before_data=before_data,
        after_data=after_data,
    )
    await db.commit()
    return {"updated": len(rooms), "room_ids": [r.room_id for r in rooms]}


# ─── v2-issue#2 — 业主费用支出占比规则 (RoomCostShareRule) ─────────────────────
@router.get("/{room_id}/cost-share-rules", response_model=list[CostShareRuleOut])
async def list_cost_share_rules(
    room_id: str,
    db: DBSession,
    current_user: CurrentUser,
):
    """列出某房间已配置的所有费用占比规则。

    缺失的 (booking_type, expense_category) 组合前端可补默认值（share_percent=1.0）。
    返回的列表顺序无严格保证，前端按 OWNER_COST_SHARE_CATEGORIES + booking_type 排序。
    """
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权查看费用占比规则")

    result = await db.execute(
        select(RoomCostShareRule).where(RoomCostShareRule.room_id == room_id)
    )
    return result.scalars().all()


@router.put("/{room_id}/cost-share-rules", response_model=list[CostShareRuleOut])
async def upsert_cost_share_rules(
    room_id: str,
    body: CostShareRulesUpsertRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """为单房间 upsert 一组规则。

    规则匹配 (room_id, booking_type, expense_category) 三元组：存在则更新 share_percent
    + unit_price，不存在则创建。请求体里没传的组合保持不动（不会删除）。
    """
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权修改费用占比规则")

    # 检查 room 存在
    room_check = await db.execute(select(Room.room_id).where(Room.room_id == room_id))
    if not room_check.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="房间不存在")

    # 拉现有规则（用于 upsert）
    existing_result = await db.execute(
        select(RoomCostShareRule).where(RoomCostShareRule.room_id == room_id)
    )
    existing = {
        (r.booking_type, r.expense_category): r for r in existing_result.scalars().all()
    }

    affected: list[RoomCostShareRule] = []
    for item in body.rules:
        key = (item.booking_type, item.expense_category)
        if key in existing:
            rule = existing[key]
            rule.share_percent = item.share_percent
            rule.unit_price = item.unit_price
        else:
            rule = RoomCostShareRule(
                rule_id="CSR-" + uuid.uuid4().hex[:12].upper(),
                room_id=room_id,
                booking_type=item.booking_type,
                expense_category=item.expense_category,
                share_percent=item.share_percent,
                unit_price=item.unit_price,
            )
            db.add(rule)
        affected.append(rule)

    await db.flush()
    for r in affected:
        await db.refresh(r)

    await log_action_tx(
        db,
        current_user["user_id"],
        "room.cost_share.upsert",
        "room",
        room_id,
        after_data={"rules_count": len(affected)},
    )
    await db.commit()
    return affected


@router.post("/cost-share-rules/bulk")
async def bulk_upsert_cost_share_rules(
    body: CostShareRulesBulkRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """批量给多个房间设同一组规则。

    王总参考 PMS 系统的"批量更改"按钮用此端点：选 N 套房 → 一次写入相同的占比配置。
    仅 admin 可调用。
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可批量更新费用占比规则")

    if not body.room_ids:
        raise HTTPException(status_code=400, detail="未选择任何房间")
    if not body.rules:
        raise HTTPException(status_code=400, detail="未传入任何规则")

    # 拉所有目标房间现存的规则
    existing_result = await db.execute(
        select(RoomCostShareRule).where(RoomCostShareRule.room_id.in_(body.room_ids))
    )
    existing: dict[tuple[str, BookingType, ExpenseCategory], RoomCostShareRule] = {}
    for r in existing_result.scalars().all():
        existing[(r.room_id, r.booking_type, r.expense_category)] = r

    upserted = 0
    for room_id in body.room_ids:
        for item in body.rules:
            key = (room_id, item.booking_type, item.expense_category)
            if key in existing:
                r = existing[key]
                r.share_percent = item.share_percent
                r.unit_price = item.unit_price
            else:
                db.add(
                    RoomCostShareRule(
                        rule_id="CSR-" + uuid.uuid4().hex[:12].upper(),
                        room_id=room_id,
                        booking_type=item.booking_type,
                        expense_category=item.expense_category,
                        share_percent=item.share_percent,
                        unit_price=item.unit_price,
                    )
                )
            upserted += 1

    await log_action_tx(
        db,
        current_user["user_id"],
        "rooms.cost_share.bulk_upsert",
        "rooms",
        ",".join(body.room_ids[:5]) + ("..." if len(body.room_ids) > 5 else ""),
        after_data={
            "room_count": len(body.room_ids),
            "rules_per_room": len(body.rules),
            "total_upserted": upserted,
        },
    )
    await db.commit()
    return {"upserted": upserted, "room_ids": body.room_ids}
