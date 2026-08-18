"""对账管家接口：比对 + 忽略/关联/补建/作废/一键修复。

GET  /reconciliation?month=YYYY-MM      按客人整体对账（guest_diff 带动作建议+排房建议）
POST /reconciliation/ignore             标忽略（记性，存 ota_raw_orders.reconcile_status）
POST /reconciliation/link               关联手录单（补 platform_order_id）
POST /reconciliation/create-missing     补建漏录单（可带 room_id 直接排房）
POST /reconciliation/void-duplicate     作废重复单（软删，护栏拒绝在住/有收款）
POST /reconciliation/auto-fix           一键修复（dry_run 预览 → 执行，逐项容错+审计）

staging 表 `ota_raw_orders` 由 ota-sync 建（同库）；本仓测试库(SQLite)无此表，故用
SQLAlchemy inspect 检查存在性，缺失时优雅降级。SQL 避免 Postgres 专有语法，JSON
在 Python 侧解析，兼容 SQLite 测试库。reconcile_status 列 bypms 流程不消费（实证
2026-07-03，354 行全 pending 闲置），安全用作对账「记性」。
"""
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import bindparam, inspect, select, text

from app.core.deps import CurrentUser, DBSession
from app.services.audit import log_action_tx
from app.services.reconciliation import (
    build_auto_fix_plan,
    build_missing_order_payload,
    classify_reconciliation,
    detect_anomalies,
)
from app.models.order_source_price_snapshot import SourcePriceSnapshotOrigin
from app.services.sponsorship import record_source_price_snapshot

router = APIRouter(prefix="/reconciliation", tags=["reconciliation"])
logger = logging.getLogger(__name__)

ROLES_VIEW = ("admin", "operator", "finance", "keeper")
ROLES_MONEY = ("admin", "finance")  # 与 dashboard ROLES_WITH_REVENUE 一致
ROLES_WRITE = ("admin", "operator")  # 与 finance.py 写操作一致

# bypms 房型营销名 marker → rooms.room_type（复刻 ota-sync BYPMS_ROOM_TYPE_MAP，
# 两仓改动需同步·landmine）。用于补建时的自动排房建议。
_ROOM_TYPE_MAP = {
    "揽月": "尊享270°海景·两卧度假套房（复式楼王+弧形内阳台+电视投屏）",
    "270": "尊享270°海景·两卧度假套房（复式楼王+弧形内阳台+电视投屏）",
    "栖浪": "私享180°海景·大床套房（复式楼王+电视投屏）",
    "180": "私享180°海景·大床套房（复式楼王+电视投屏）",
    "沐海": "侧面海景·亲子两床套房（复式楼王+电视投屏）",
    "亲子两床": "侧面海景·亲子两床套房（复式楼王+电视投屏）",
    "观澜": "精选市景·大床度假套房（复式楼王+电视投屏）",
    "精选市景": "精选市景·大床度假套房（复式楼王+电视投屏）",
}


def _month_range(month: str) -> tuple[date, date]:
    y, m = int(month[:4]), int(month[5:7])
    lo = date(y, m, 1)
    hi = date(y + (m == 12), (m % 12) + 1, 1)
    return lo, hi


def _as_dict(v):
    if v is None:
        return {}
    return v if isinstance(v, dict) else json.loads(v)


def _as_datetime(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    try:
        return datetime.fromisoformat(str(v))
    except (ValueError, TypeError):
        return None


async def _staging_price_source(db, platform_order_id: str) -> tuple[dict, datetime] | None:
    row = (
        await db.execute(
            text(
                "SELECT raw_payload, fetched_at FROM ota_raw_orders "
                "WHERE platform = 'bypms' AND platform_order_id = :pid"
            ),
            {"pid": platform_order_id},
        )
    ).mappings().one_or_none()
    if row is None:
        return None
    return _as_dict(row["raw_payload"]), _as_datetime(row["fetched_at"]) or datetime.now(
        timezone.utc
    )


def _as_date(v) -> date | None:
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _redact(body: dict) -> dict:
    """非 finance 角色：服务端置 null 所有金额，绝不下发。"""
    for row in body["guest_diff"]:
        for f in ("our_total", "bypms_total", "diff"):
            row[f] = None
        for o in row.get("our_orders", []):
            o["actual_price"] = None
        for b in row.get("bypms_orders", []):
            b["bypms_fang"] = None
    for a in body.get("anomalies", []):
        a["amount"] = None
    return body


async def _has_staging(db) -> bool:
    return await db.run_sync(
        lambda s: inspect(s.get_bind()).has_table("ota_raw_orders"))


async def _suggest_room(db, bypms_room_type: str | None, ci, co) -> str | None:
    """按 bypms 房型 marker 找同房型、日期无冲突（活跃单+锁房）的第一间房。找不到 None。"""
    ci, co = _as_date(ci), _as_date(co)
    if not bypms_room_type or not ci or not co:
        return None
    target = next((t for m, t in _ROOM_TYPE_MAP.items() if m in bypms_room_type), None)
    if not target:
        return None
    row = (await db.execute(text("""
        SELECT r.room_id FROM rooms r
        WHERE r.room_type = :rt
          AND NOT EXISTS (
            SELECT 1 FROM order_rooms orr JOIN orders o ON o.order_id = orr.order_id
            WHERE orr.room_id = r.room_id AND o.order_status != 'cancelled'
              AND NOT o.is_deleted
              AND orr.check_in_date < :co AND orr.check_out_date > :ci)
          AND NOT EXISTS (
            SELECT 1 FROM room_blocks b
            WHERE b.room_id = r.room_id AND b.start_date < :co AND b.end_date > :ci)
        ORDER BY r.room_id LIMIT 1
    """), {"rt": target, "ci": ci, "co": co})).scalar_one_or_none()
    return row


async def _fetch_and_classify(db, month: str) -> dict:
    """取数 + 比对 + 给可补建行算排房建议。GET 与 auto-fix 共用。"""
    lo, hi = _month_range(month)
    # 「入住区间与该月有交集」而非「入住日在该月」——两侧同规则。否则跨月单不对称：
    # 孙晶案：我们 6/29 入住的单被排除，bypms 7/01 的续住段却被算进，误报 0 vs 766。
    orows = (await db.execute(text("""
        SELECT o.order_id, o.channel, o.actual_price, o.platform_commission_rate,
               o.guest_name, o.check_in_date AS check_in, o.check_out_date AS check_out,
               o.room_id, o.platform_order_id, o.order_status, o.metadata AS meta
        FROM orders o
        WHERE o.check_in_date < :hi AND o.check_out_date > :lo AND NOT o.is_deleted
    """), {"lo": lo, "hi": hi})).mappings().all()

    srows = []
    if await _has_staging(db):
        srows = (await db.execute(text("""
            SELECT platform_order_id, room_type, check_in, guest_name, raw_payload,
                   reconcile_status, fetched_at
            FROM ota_raw_orders
            WHERE platform = 'bypms' AND check_in < :hi
              AND (check_out IS NULL OR check_out > :lo)
        """), {"lo": lo, "hi": hi})).mappings().all()

    # 各订单的房间段（内部金额不平/同房重叠检测用）；按 order_id 归组
    rooms_by_oid: dict = {}
    oids = [r["order_id"] for r in orows]
    if oids:
        rr = (await db.execute(text(
            "SELECT order_id, room_id, check_in_date AS check_in, "
            "check_out_date AS check_out, actual_price, daily_prices "
            "FROM order_rooms WHERE order_id IN :ids"
        ).bindparams(bindparam("ids", expanding=True)), {"ids": oids})).mappings().all()
        for x in rr:
            rooms_by_oid.setdefault(x["order_id"], []).append({
                "room_id": x["room_id"], "check_in": x["check_in"],
                "check_out": x["check_out"], "actual_price": x["actual_price"],
                "daily_prices": _as_dict(x["daily_prices"]),
            })

    orders = []
    for r in orows:
        meta = _as_dict(r["meta"])
        orders.append({
            "order_id": r["order_id"], "channel": str(r["channel"]) if r["channel"] is not None else None,
            "actual_price": r["actual_price"], "guest_name": r["guest_name"],
            "check_in": r["check_in"], "check_out": r["check_out"], "room_id": r["room_id"],
            "platform_order_id": r["platform_order_id"],
            "order_status": str(r["order_status"]) if r["order_status"] is not None else None,
            "price_locked": str(meta.get("price_locked")).lower() == "true",
            "commission_rate": r["platform_commission_rate"], "metadata": meta,
            "rooms": rooms_by_oid.get(r["order_id"], []),
        })

    # 幽灵判定：同步每 180s 全量刷新 fetched_at；比水位（全表最新抓取时间）落后
    # 超过 1 小时 = bypms 已删除该单（改单/重新下单后的旧单），不参与比对。
    def _ts(v):
        if isinstance(v, datetime):
            return v
        try:
            return datetime.fromisoformat(str(v))
        except (ValueError, TypeError):
            return None
    fetched = [t for t in (_ts(r["fetched_at"]) for r in srows) if t is not None]
    watermark = max(fetched) if fetched else None

    staging = []
    for r in srows:
        raw = _as_dict(r["raw_payload"])
        ft = _ts(r["fetched_at"])
        ghost = bool(watermark and ft and (watermark - ft).total_seconds() > 3600)
        staging.append({
            "platform_order_id": r["platform_order_id"], "room_type": r["room_type"],
            "check_in": r["check_in"], "guest_name": r["guest_name"],
            "state": raw.get("state"), "raw": raw,
            "reconcile_status": r["reconcile_status"],
            "ghost": ghost,
        })

    res = classify_reconciliation(orders, staging)
    # 可补建行 → 自动排房建议（bypms 单默认 1 晚起，日期取原始 checkIn/checkOut）
    raw_by_pid = {s["platform_order_id"]: s["raw"] for s in staging}
    for row in res["guest_diff"]:
        for b in row["bypms_orders"]:
            if b.get("suggested_action") == "create":
                raw = raw_by_pid.get(b["platform_order_id"]) or {}
                b["suggested_room"] = await _suggest_room(
                    db, b.get("room_type"), raw.get("checkIn"), raw.get("checkOut"))
            else:
                b["suggested_room"] = None
    res["anomalies"] = detect_anomalies(orders)
    return res


def _summarize(res: dict) -> dict:
    plan = build_auto_fix_plan(res["guest_diff"])
    auto_pids = ({c["platform_order_id"] for c in plan["creates"]}
                 | {l["platform_order_id"] for l in plan["links"]}
                 | {i["platform_order_id"] for i in plan["ignores"]})
    manual = 0
    for row in res["guest_diff"]:
        fully_auto = row["reason"] != "over" and all(
            b["matched"] or b["platform_order_id"] in auto_pids
            for b in row["bypms_orders"])
        if not fully_auto:
            manual += 1
    anomalies = res.get("anomalies", [])
    return {
        "guest_diff_count": len(res["guest_diff"]),
        "diff_total": sum((abs(r["diff"]) for r in res["guest_diff"]), Decimal("0")),
        "dup_count": len(res["duplicates"]),
        "ignored_count": res.get("ignored_count", 0),
        "ghost_count": res.get("ghost_count", 0),
        "auto_fixable": len(auto_pids),
        "manual_count": manual,
        "anomaly_count": len(anomalies),
        "anomaly_high": sum(1 for a in anomalies if a["severity"] == "high"),
    }


@router.get("")
async def get_reconciliation(
    db: DBSession, current_user: CurrentUser,
    month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
):
    if current_user["role"] not in ROLES_VIEW:
        raise HTTPException(403, "无权限")
    res = await _fetch_and_classify(db, month)
    res["summary"] = _summarize(res)
    if current_user["role"] not in ROLES_MONEY:
        res = _redact(res)
        res["summary"]["diff_total"] = None
    return res


# ── 写操作 ────────────────────────────────────────────────────────


class IgnoreBody(BaseModel):
    platform_order_ids: list[str] = Field(min_length=1)


@router.post("/ignore")
async def ignore_rows(
    body: IgnoreBody, db: DBSession, current_user: CurrentUser, request: Request,
):
    """标忽略（记性）：写 ota_raw_orders.reconcile_status='ignored'，之后不再出现在差异里。"""
    if current_user["role"] not in ROLES_WRITE:
        raise HTTPException(403, "无权限")
    if not await _has_staging(db):
        raise HTTPException(404, "无 bypms 原始数据")
    stmt = text(
        "UPDATE ota_raw_orders SET reconcile_status = 'ignored' "
        "WHERE platform = 'bypms' AND platform_order_id IN :pids"
    ).bindparams(bindparam("pids", expanding=True))
    result = await db.execute(stmt, {"pids": body.platform_order_ids})
    await log_action_tx(db, current_user.get("user_id"), "reconciliation.ignore",
                        "ota_raw_orders", ",".join(body.platform_order_ids[:10]),
                        None, {"count": result.rowcount},
                        ip_address=request.client.host if request.client else None)
    await db.commit()
    return {"ignored": result.rowcount}


class LinkBody(BaseModel):
    platform_order_id: str
    order_id: str


@router.post("/link")
async def link_order(
    body: LinkBody, db: DBSession, current_user: CurrentUser, request: Request,
):
    """关联：把 bypms 平台单号补到我们的手录单上（防重复建单的正确动作）。"""
    if current_user["role"] not in ROLES_WRITE:
        raise HTTPException(403, "无权限")
    from app.models.order import Order

    o = (
        await db.execute(
            select(Order).where(Order.order_id == body.order_id).with_for_update()
        )
    ).scalar_one_or_none()
    if o is None or o.is_deleted:
        raise HTTPException(404, "订单不存在")
    if o.platform_order_id:
        raise HTTPException(409, f"该单已有平台单号（{o.platform_order_id}），不能重复关联")
    dup = (await db.execute(text(
        "SELECT order_id FROM orders WHERE platform_order_id = :pid LIMIT 1"),
        {"pid": body.platform_order_id})).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"平台单号已挂在 {dup} 上")
    if not await _has_staging(db):
        raise HTTPException(409, "bypms 原始价格源不可用，不能关联")
    source = await _staging_price_source(db, body.platform_order_id)
    if source is None:
        raise HTTPException(409, "bypms 原始价格源不存在，不能关联")
    raw, fetched_at = source
    o.platform_order_id = body.platform_order_id
    meta = dict(o.metadata_ or {})
    meta["reconciliation_linked"] = {
        "pid": body.platform_order_id,
        "at": datetime.now(timezone.utc).isoformat(),
        "by": current_user.get("user_id"),
    }
    try:
        await record_source_price_snapshot(
            db,
            source_order_id=o.order_id,
            raw_payload=raw,
            origin=SourcePriceSnapshotOrigin.bypms_adoption,
            fetched_at=fetched_at,
            created_by=current_user.get("user_id"),
        )
    except ValueError as exc:
        raise HTTPException(422, f"bypms 原始价格无法留档：{exc}") from exc
    try:
        from app.core.free_room import is_free_room_type
        room_type = raw.get("unitName") or raw.get("room_type")
        meta["ota_room_type"] = room_type
        meta["ota_free_room"] = is_free_room_type(room_type)
    except Exception:
        # Classification is auxiliary; never break the established rescue path.
        logger.warning(
            "reconciliation link succeeded without OTA free-room classification: pid=%s",
            body.platform_order_id,
            exc_info=True,
        )
    o.metadata_ = meta
    await log_action_tx(db, current_user.get("user_id"), "reconciliation.link",
                        "order", body.order_id, None,
                        {"platform_order_id": body.platform_order_id},
                        ip_address=request.client.host if request.client else None)
    await db.commit()
    return {"order_id": body.order_id, "platform_order_id": body.platform_order_id}


class VoidBody(BaseModel):
    order_id: str


@router.post("/void-duplicate")
async def void_duplicate(
    body: VoidBody, db: DBSession, current_user: CurrentUser, request: Request,
):
    """作废重复单（软删可逆）。护栏：有收款记录或已在住/退房流程 → 拒绝。"""
    if current_user["role"] not in ROLES_WRITE:
        raise HTTPException(403, "无权限")
    from app.models.order import Order

    o = await db.get(Order, body.order_id)
    if o is None or o.is_deleted:
        raise HTTPException(404, "订单不存在或已作废")
    status = o.order_status.value if hasattr(o.order_status, "value") else str(o.order_status)
    if status in ("checked_in", "pending_checkout", "completed"):
        raise HTTPException(409, f"该单状态为 {status}，不能作废（请人工核实）")
    paid = (await db.execute(text(
        "SELECT count(*) FROM payments WHERE order_id = :oid AND deleted_at IS NULL"),
        {"oid": body.order_id})).scalar() or 0
    if paid:
        raise HTTPException(409, "该单已有收款记录，不能作废（请先处理收款）")
    o.is_deleted = True
    await log_action_tx(db, current_user.get("user_id"), "reconciliation.void_duplicate",
                        "order", body.order_id,
                        {"order_status": status, "is_deleted": False}, {"is_deleted": True},
                        ip_address=request.client.host if request.client else None)
    await db.commit()
    return {"order_id": body.order_id, "voided": True}


class CreateMissingBody(BaseModel):
    platform_order_id: str
    room_id: str | None = None


async def _create_from_staging(
    db, current_user, request, platform_order_id: str, room_id: str | None = None,
) -> dict:
    """补建核心（create-missing 与 auto-fix 共用）。守卫全部以 HTTPException 抛出。"""
    if not await _has_staging(db):
        raise HTTPException(404, "无 bypms 原始数据")
    source = await _staging_price_source(db, platform_order_id)
    if source is None:
        raise HTTPException(404, "bypms 原始单不存在")
    raw, fetched_at = source
    if (raw.get("state") or "").upper() == "D":
        raise HTTPException(409, "该单在 bypms 已取消，不补建")
    dup = (await db.execute(text(
        "SELECT order_id FROM orders WHERE platform_order_id = :pid LIMIT 1"),
        {"pid": platform_order_id})).scalar_one_or_none()
    if dup:
        raise HTTPException(409, f"我们系统已有该平台单号的订单（{dup}），无需补建")

    payload = build_missing_order_payload(raw)
    if not payload["check_in"] or not payload["check_out"]:
        raise HTTPException(422, "bypms 原始缺入住/退房日期，无法补建")

    from app.core.ids import gen_order_room_id, unique_order_id
    from app.models.order import Channel, Order, OrderStatus
    from app.models.order_room import OrderRoom
    from app.models.room import Room

    # 排房：只对单间单生效（多间需人工拆分排房）；房间必须存在
    assign_room = None
    if room_id and len(payload["rooms"]) == 1:
        if await db.get(Room, room_id) is None:
            raise HTTPException(404, f"房间 {room_id} 不存在")
        assign_room = room_id

    order_id = await unique_order_id(db)
    meta = dict(payload["metadata"])
    meta["imported_at"] = datetime.now(timezone.utc).isoformat()
    order = Order(
        order_id=order_id,
        channel=Channel(payload["channel"]),
        platform_order_id=payload["platform_order_id"],
        guest_name=payload["guest_name"],
        guest_phone=payload["guest_phone"],
        room_id=assign_room,
        check_in_date=payload["check_in"],
        check_out_date=payload["check_out"],
        actual_price=payload["actual_price"],
        platform_commission_rate=payload["commission_rate"],
        order_status=OrderStatus(payload["order_status"]),
        notes=payload["notes"],
        metadata_=meta,
    )
    db.add(order)
    for i, rm in enumerate(payload["rooms"]):
        db.add(OrderRoom(
            order_room_id=gen_order_room_id(),
            order_id=order_id,
            room_id=assign_room,
            check_in_date=payload["check_in"],
            check_out_date=payload["check_out"],
            actual_price=rm["actual_price"],
            daily_prices=rm["daily_prices"],
            position=i,
        ))
    try:
        await record_source_price_snapshot(
            db,
            source_order_id=order.order_id,
            raw_payload=raw,
            origin=SourcePriceSnapshotOrigin.bypms_import,
            fetched_at=fetched_at,
            created_by=current_user.get("user_id"),
        )
    except ValueError as exc:
        raise HTTPException(422, f"bypms 原始价格无法留档：{exc}") from exc
    await log_action_tx(
        db, current_user.get("user_id"), "reconciliation.create_missing",
        "order", order_id, None,
        {"platform_order_id": payload["platform_order_id"],
         "actual_price": str(payload["actual_price"]),
         "channel": payload["channel"], "room_id": assign_room},
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return {
        "order_id": order_id,
        "guest_name": payload["guest_name"],
        "actual_price": str(payload["actual_price"]),
        "channel": payload["channel"],
        "room_id": assign_room,
    }


@router.post("/create-missing", status_code=201)
async def create_missing(
    body: CreateMissingBody, db: DBSession, current_user: CurrentUser, request: Request,
):
    """补建漏录单：按 bypms staging 原始建进我们系统；可带 room_id 直接排房（单间单）。"""
    if current_user["role"] not in ROLES_WRITE:
        raise HTTPException(403, "无权限")
    return await _create_from_staging(
        db, current_user, request, body.platform_order_id, body.room_id)


class AutoFixBody(BaseModel):
    month: str = Field(pattern=r"^\d{4}-\d{2}$")
    dry_run: bool = True


@router.post("/auto-fix")
async def auto_fix(
    body: AutoFixBody, db: DBSession, current_user: CurrentUser, request: Request,
):
    """一键修复：dry_run=True 返回预览计划；False 逐项执行（补建带排房建议、关联、忽略），
    单项失败不连累其余，逐项审计，返回逐项结果。"""
    if current_user["role"] not in ROLES_WRITE:
        raise HTTPException(403, "无权限")
    res = await _fetch_and_classify(db, body.month)
    plan = build_auto_fix_plan(res["guest_diff"])
    # 补建项带上排房建议，预览/执行同源
    room_by_pid = {
        b["platform_order_id"]: b.get("suggested_room")
        for row in res["guest_diff"] for b in row["bypms_orders"]
        if b.get("suggested_action") == "create"
    }
    for c in plan["creates"]:
        c["suggested_room"] = room_by_pid.get(c["platform_order_id"])
    if body.dry_run:
        return {"plan": plan, "counts": {
            "creates": len(plan["creates"]), "links": len(plan["links"]),
            "ignores": len(plan["ignores"])}}

    results = {"created": [], "linked": [], "ignored": 0, "errors": []}
    for c in plan["creates"]:
        try:
            r = await _create_from_staging(
                db, current_user, request, c["platform_order_id"], c.get("suggested_room"))
            results["created"].append(r)
        except HTTPException as e:
            await db.rollback()
            results["errors"].append({"platform_order_id": c["platform_order_id"],
                                      "action": "create", "detail": e.detail})
    for l in plan["links"]:
        try:
            from app.models.order import Order
            o = (
                await db.execute(
                    select(Order)
                    .where(Order.order_id == l["order_id"])
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if o is None or o.is_deleted or o.platform_order_id:
                raise HTTPException(409, "候选单状态已变化，跳过")
            o.platform_order_id = l["platform_order_id"]
            source = await _staging_price_source(db, l["platform_order_id"])
            if source is None:
                raise HTTPException(409, "bypms 原始价格已不存在，跳过")
            raw, fetched_at = source
            try:
                await record_source_price_snapshot(
                    db,
                    source_order_id=o.order_id,
                    raw_payload=raw,
                    origin=SourcePriceSnapshotOrigin.bypms_adoption,
                    fetched_at=fetched_at,
                    created_by=current_user.get("user_id"),
                )
            except ValueError as exc:
                raise HTTPException(422, f"bypms 原始价格无法留档：{exc}") from exc
            await log_action_tx(db, current_user.get("user_id"), "reconciliation.link",
                                "order", l["order_id"], None,
                                {"platform_order_id": l["platform_order_id"], "auto": True},
                                ip_address=request.client.host if request.client else None)
            await db.commit()
            results["linked"].append(l)
        except HTTPException as e:
            await db.rollback()
            results["errors"].append({"platform_order_id": l["platform_order_id"],
                                      "action": "link", "detail": e.detail})
    if plan["ignores"]:
        stmt = text(
            "UPDATE ota_raw_orders SET reconcile_status = 'ignored' "
            "WHERE platform = 'bypms' AND platform_order_id IN :pids"
        ).bindparams(bindparam("pids", expanding=True))
        result = await db.execute(
            stmt, {"pids": [i["platform_order_id"] for i in plan["ignores"]]})
        results["ignored"] = result.rowcount
        await log_action_tx(db, current_user.get("user_id"), "reconciliation.auto_ignore",
                            "ota_raw_orders", body.month, None,
                            {"count": result.rowcount},
                            ip_address=request.client.host if request.client else None)
        await db.commit()
    return {"results": results}
