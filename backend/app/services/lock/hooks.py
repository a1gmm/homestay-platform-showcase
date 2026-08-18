"""订单流门锁钩子（plan §16.4 Lane C）。

订单端点与 OrderLockService 之间的薄胶水。每个钩子 fail-safe：门锁/厂商出问题
绝不阻断办理入住/退房/取消（§16.1——只退化自动重试，不退化主流程）。
OrderLockService 各方法内部已 commit；钩子失败时回滚未提交部分，不污染调用方
已提交的订单流转。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.database import AsyncSessionLocal
from app.models.door_code import DoorCode, DoorCodePurpose, DoorCodeStatus
from app.models.lock_device import LockDevice
from app.models.order import Order
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.services.feishu_lead_alert import (
    send_lock_alert,
    send_password_image,
    send_password_text,
)
from app.services.lock.checkin_card import build_checkin_card
from app.services.lock.checkin_qr import qr_image_key_for_room
from app.services.lock.code_crypto import decrypt_code
from app.services.lock.factory import get_lock_provider
from app.services.lock.orchestrator import (
    OrderLockService,
    code_covers_checkout,
    operative_guest_code,
)

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime | None) -> datetime | None:
    """裸值（SQLite/历史）当 UTC 补 tz，避免 naive/aware 比对报错。"""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _process_checkin_codes(order_id: str, only_room_ids=None) -> None:
    """办理入住后台化的实际活儿：下客人码 + 推飞书卡片 + 失败告警。

    由端点通过 FastAPI BackgroundTasks 调度（响应先 flush→页面秒回，任务在响应后跑）。
    开独立 session（请求 session 响应后即关）。全程 fail-safe，下码/飞书失败会告警
    不静默丢（王总 2026-07-12 明确要求）。
    only_room_ids 给定则整条链路（下码/推卡/告警）只针对这几间房（单间入住）；
    None = 整单全部已排房。"""
    try:
        async with AsyncSessionLocal() as db:
            order = (await db.execute(
                select(Order).where(Order.order_id == order_id).options(selectinload(Order.rooms))
            )).scalar_one_or_none()
            if order is None:
                logger.warning("checkin async: order %s not found, skip 下码", order_id)
                return
            await issue_codes_on_checkin(db, order, only_room_ids=only_room_ids)
            await notify_checkin_codes(db, order, only_room_ids=only_room_ids)
            await _alert_if_codes_missing(db, order, only_room_ids=only_room_ids)
    except Exception:
        logger.exception("checkin async processing failed order=%s", order_id)
        try:
            await send_lock_alert(
                f"⚠️ 办理入住后台下码/通知异常\n订单：{order_id}\n请前台检查门锁或手动处理"
            )
        except Exception:
            logger.exception("send lock alert failed order=%s", order_id)


async def _process_checkout_codes(
    order_id: str, cleaned_room_ids: list[str], task_by_room: dict[str, str],
    revoke_room_ids: list[str] | None = None,
) -> None:
    """办理退房后台化的实际活儿：撤客人码 + 下保洁码 + 发保洁群卡片。

    由 staff_portal.handle_checkout 通过 BackgroundTasks 调度（响应先 flush→页面秒回）。
    开独立 session（请求 session 响应后即关）。全程 fail-safe：门锁/飞书任何问题只 log，
    绝不影响已提交的退房流转。

    revoke_room_ids：单间退房时只撤这几间房的客人码（None = 撤整单全部，兼容整单退房）。
    """
    try:
        async with AsyncSessionLocal() as db:
            order = (await db.execute(
                select(Order).where(Order.order_id == order_id).options(selectinload(Order.rooms))
            )).scalar_one_or_none()
            if order is None:
                logger.warning("checkout async: order %s not found, skip 撤码/下保洁码", order_id)
                return
            # 精确作废客人码（delKey）。单间退房只撤本次退的房。
            await revoke_codes_on_checkout(db, order, room_ids=revoke_room_ids)
            # 撤客人码同时给每间待保洁房下限时保洁码（次日12:00过期），塞进保洁群卡片（方案 B）。
            cleaning_codes = await issue_cleaning_codes_on_checkout(db, order, cleaned_room_ids)
            # #116 保洁通知（best-effort）。_notify_cleaning_best_effort 在 staff_portal，
            # 调用时导入避免模块级循环依赖。
            from app.api.v1.staff_portal import _notify_cleaning_best_effort
            await _notify_cleaning_best_effort(
                db, order, cleaned_room_ids, task_by_room, cleaning_codes
            )
    except Exception:
        logger.exception("checkout async processing failed order=%s", order_id)


async def _window_was_inverted(db, order_id: str, room_id: str) -> bool:
    """最近一次给该房下客人码时，有效期是不是「失效 ≤ 生效」（时窗倒挂）。

    倒挂 = 厂商必拒、跟门锁好坏无关：客人码时窗 =「办理入住此刻」→「退房日 14:00」
    (orchestrator.on_guest_checkin)，退房日已过去就必然倒挂。

    判定读下码当时落库的 start_at/end_at —— 那是「当时到底请求了个什么窗」的不可变事实。
    不拿订单日期现算：「门锁密码延期」只改码有效期、故意不改订单日期
    (orchestrator.extend_guest_codes，王总 2026-07-08 决策)，按订单日期反推会把人还在
    房里的续住客人误判成已离店、把他开不了门这件事静音掉。
    """
    code = (await db.execute(
        select(DoorCode).where(
            DoorCode.order_id == order_id,
            DoorCode.room_id == room_id,
            DoorCode.purpose == DoorCodePurpose.guest,
        ).order_by(DoorCode.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if code is None or code.start_at is None or code.end_at is None:
        return False  # 没下过码/没记时窗 → 不敢认定是倒挂，按真失败报，别赌
    return code.end_at <= code.start_at


async def _alert_if_codes_missing(db, order, only_room_ids=None) -> None:
    """有绑锁的房却没成功下码(FAILED/无码)→ 飞书告警，别静默(王总明确要求，2026-07-12)。
    pending 码算成功：已随卡推给前台、锁上线后 retry_pending 会确认，不算失败。
    only_room_ids 给定则只检查这几间房（单间入住，别把没入住的房误报缺码）。

    判定的是「客人这一段进不进得了门」，不是「本单名下有没有 DoorCode 行」。两步都必须
    跟 orchestrator.on_guest_checkin 用同一份口径，否则真失败会静默：

    1. 哪把码 —— operative_guest_code：本单自己的码优先，否则续住组内沿用的那把。
       续住段沿用锚单码时本单名下没有 DoorCode 行（有意跳过下码，一个密码住到底），
       只认本单的码会把这种正常情况误报成「下码失败」。反过来也不能放宽成「组内随便
       哪把覆盖得够就算数」——组内可能有客人根本没在用的另一把码（补关联历史单），
       拿它顶事会把「客人手里那把延期失败」盖掉。
    2. 这把码行不行 —— code_covers_checkout：光看「有没有码」不够，它的 end_at 可能
       只到锚单退房日、本段中途就失效（见 orchestrator._extend_code_to_segment）。
       DoorCodeStatus.expired 全仓没人写，光看 status 永远发现不了过期，只能看 end_at；
       retry_pending 也只捞 pending/failed，active 但 end_at 陈旧的码没有第二道网，
       这条告警就是唯一的兜底。

    放宽只免掉「客人手里那把码确实能开到本段退房」的情况，真失败一条都不吞（王总 2026-07-12）。

    时窗倒挂的房（补录过去的订单走入住流程）分流到另一条提醒（2026-07-17）：门锁没毛病，
    「请前台检查门锁」是把人往错方向支，生产误报过四条。但不能全静音 —— 前台把真客人的
    日期敲错年份/月份，落到系统里跟补录一模一样，全静音会让在店客人开不了门且无人知晓
    （王总 2026-07-17 拍板：换一条指向日期的提醒）。两条告警都只改措辞，下码本身照常
    尝试、FAILED 照常落库留痕。
    """
    only = set(only_room_ids) if only_room_ids is not None else None
    rooms = (await db.execute(
        select(OrderRoom.room_id, OrderRoom.check_out_date).where(
            OrderRoom.order_id == order.order_id, OrderRoom.room_id.isnot(None)
        )
    )).all()
    if only is not None:
        rooms = [r for r in rooms if r.room_id in only]
    now = datetime.now(timezone.utc)
    missing: list[str] = []
    stale_dates: list[str] = []
    for rid, check_out_date in rooms:
        has_device = (await db.execute(
            select(LockDevice.id).where(LockDevice.room_id == rid))).first() is not None
        if not has_device:
            continue  # 房间没绑门锁本就不下码，不算失败
        code = await operative_guest_code(db, order, rid)
        # 覆盖判定走 code_covers_checkout，与 orchestrator 同一份实现（SQL 侧比 end_at
        # 会掉进各驱动的 tz 语义差异，两处各写一份必然分家）。
        ok = (code is not None and code.password is not None
              and code_covers_checkout(code.end_at, check_out_date))
        if ok:
            continue
        # 逐房判，别按整单一刀切：同一单里可以一间日期是过去、另一间是真故障。
        if await _window_was_inverted(db, order.order_id, rid):
            stale_dates.append(rid)
            continue
        # 客人当下手里那把码还有效（此刻能开门），只是没延到本段退房 → 续住延码未坐实。
        # 交第二道网(extend_stale_group_codes)自动补延，锁离线超 30 分钟它会再告警——
        # 此刻别即时报，躲过锁短暂抖动的虚惊（王总 2026-07-21）。反之「当下就没有效码」
        # （开不了门）必须即时报，真·堵门口一条不吞（王总 2026-07-12）。
        # 抑制只对续住组（stay_group_id 非空）——第二道网只扫组码，非组单没人兜底，
        # 抑制了就成静默漏报，故非组单一律照旧即时报（守住「续住段沿用锚单码」这唯一场景）。
        # end_at/start_at 落 SQLite 会掉 tz（裸值当 UTC），比对前统一补齐避免 naive/aware 撞。
        end_aware = _as_utc(code.end_at) if code is not None else None
        start_aware = _as_utc(code.start_at) if code is not None else None
        from app.services import stay_group as _stay_group
        has_group_retry = False
        if order.stay_group_id is not None:
            if await _stay_group.is_managed_split(db, order.stay_group_id):
                has_group_retry = len(
                    await _stay_group.operational_group_orders(db, order)
                ) > 1
            else:
                has_group_retry = True
        if (has_group_retry
                and code is not None and code.password is not None
                and end_aware is not None and end_aware > now
                and (start_aware is None or start_aware <= now)):
            continue
        missing.append(rid)
    if missing:
        await send_lock_alert(
            f"⚠️ 办理入住没拿到覆盖本段住期的门锁密码\n"
            f"订单：{order.order_id}\n房间：{'、'.join(missing)}\n"
            f"（下码失败，或续住沿用的密码没能延到 {order.check_out_date} 14:00）\n"
            f"请前台检查门锁或手动设置"
        )
    if stale_dates:
        await send_lock_alert(
            f"ℹ️ 订单日期已过去，未下门锁密码\n"
            f"订单：{order.order_id}\n房间：{'、'.join(stale_dates)}\n"
            f"补录历史订单属正常，无需处理；如客人实际在店，请核对订单日期"
        )


async def issue_codes_on_checkin(db, order, only_room_ids=None) -> None:
    """办理入住即同步首试下发客人码（plan §15.7/§16.1）。失败不阻断入住。
    only_room_ids 给定则只给这几间房下码（单间入住）；None = 整单全部已排房。"""
    try:
        await OrderLockService(get_lock_provider(), db).on_guest_checkin(order, only_room_ids=only_room_ids)
    except Exception:
        logger.exception("lock issue on checkin failed order=%s", getattr(order, "order_id", "?"))
        await _safe_rollback(db)


async def revoke_codes_on_checkout(db, order, room_ids=None) -> None:
    try:
        await OrderLockService(get_lock_provider(), db).on_checkout(order, room_ids=room_ids)
    except Exception:
        logger.exception("lock revoke on checkout failed order=%s", getattr(order, "order_id", "?"))
        await _safe_rollback(db)


async def issue_cleaning_codes_on_checkout(db, order, room_ids) -> dict[str, str]:
    """退房后给每间待保洁房下限时保洁码（方案 B）。fail-safe：厂商/门锁问题绝不阻断退房。
    返回 {room_id: 明文保洁码} 供保洁群卡片展示；失败或无房返回空 dict。"""
    if not room_ids:
        return {}
    try:
        return await OrderLockService(get_lock_provider(), db).issue_cleaning_codes(order, room_ids)
    except Exception:
        oid = getattr(order, "order_id", "?")
        logger.exception("lock issue cleaning codes failed order=%s", oid)
        await _safe_rollback(db)
        # 告警：下码失败不再静默（否则保洁卡没码没人知道）。告警本身失败也绝不能
        # 破坏 fail-safe——单独裹 try，最多只 log。
        try:
            await send_lock_alert(
                f"⚠️ 退房下保洁码失败，保洁卡将无门锁密码\n"
                f"订单：{oid}\n房间：{'、'.join(room_ids)}\n请前台检查门锁或手动设置"
            )
        except Exception:
            logger.exception("send lock alert failed order=%s", oid)
        return {}


async def revoke_cleaning_code_on_done(db, room_id, order_id=None) -> None:
    """保洁完工即撤保洁码。fail-safe：门锁问题不影响完工回写。

    撤该房「所有」有效保洁码（不按 order_id 限定）：一房同时只该有一把有效保洁码，
    多次退房轮换攒下的别单老码都是残留，完工时一并清掉——否则老码常挂 active、被房态
    对账的有码守卫跳过，房态卡「过期/在住」（生产 bug 2026-07-14）。order_id 仅留作日志。
    """
    if not room_id:
        return
    try:
        await OrderLockService(get_lock_provider(), db).revoke_cleaning_codes(room_id)
    except Exception:
        logger.exception("lock revoke cleaning code failed room=%s order=%s", room_id, order_id)
        await _safe_rollback(db)


async def issue_inspection_code_on_cleaning_done(db, order, room_ids) -> dict[str, str]:
    """保洁完工、发退押金卡时给每间房下查房码（keeper，王总 2026-07-12）。fail-safe：
    厂商/门锁问题绝不阻断退押金卡发送。返回 {room_id: 明文查房码} 供卡片展示；失败或
    无房返回空 dict，并发一条门锁告警（卡上会回退成「暂无请联系前台」）。"""
    if not room_ids:
        return {}
    try:
        return await OrderLockService(get_lock_provider(), db).issue_inspection_codes(order, room_ids)
    except Exception:
        oid = getattr(order, "order_id", "?")
        logger.exception("lock issue inspection codes failed order=%s", oid)
        await _safe_rollback(db)
        try:
            await send_lock_alert(
                f"⚠️ 下查房码失败，退押金卡将无查房密码\n"
                f"订单：{oid}\n房间：{'、'.join(room_ids)}\n请前台检查门锁或手动设置"
            )
        except Exception:
            logger.exception("send lock alert failed order=%s", oid)
        return {}


async def revoke_inspection_code_on_done(db, room_id, order_id=None) -> None:
    """前台点「查房通过·退押金」即撤查房码（防重复查房）。fail-safe：门锁问题不影响退押金回执。"""
    if not room_id:
        return
    try:
        await OrderLockService(get_lock_provider(), db).revoke_inspection_codes(room_id, order_id=order_id)
    except Exception:
        logger.exception("lock revoke inspection code failed room=%s order=%s", room_id, order_id)
        await _safe_rollback(db)


async def revoke_codes_on_cancel(db, order, group_member_ids=None) -> None:
    """group_member_ids：取消流程先 unlink 再撤码，须传「取消前」的组成员快照，
    on_cancel 才能不误撤仍被剩余活段覆盖的共享码。"""
    try:
        await OrderLockService(get_lock_provider(), db).on_cancel(
            order, group_member_ids=group_member_ids)
    except Exception:
        logger.exception("lock revoke on cancel failed order=%s", getattr(order, "order_id", "?"))
        await _safe_rollback(db)


async def renew_codes_on_reschedule(db, order) -> None:
    try:
        await OrderLockService(get_lock_provider(), db).on_reschedule(order)
    except Exception:
        logger.exception("lock renew failed order=%s", getattr(order, "order_id", "?"))
        await _safe_rollback(db)


async def summarize_guest_codes(db, order) -> list[dict]:
    """Per-room 客人码下发结果，供前台看 GRANTED/QUEUED/FAILED（§16.4 无 webhook）。
    从持久化的 door_codes 重建，不依赖 orchestrator 返回值。"""
    room_ids = [
        rid for rid in (await db.execute(
            select(OrderRoom.room_id).where(
                OrderRoom.order_id == order.order_id, OrderRoom.room_id.isnot(None)
            )
        )).scalars().all()
    ]
    if not room_ids and order.room_id:
        room_ids = [order.room_id]

    codes = (await db.execute(
        select(DoorCode).where(
            DoorCode.order_id == order.order_id,
            DoorCode.purpose == DoorCodePurpose.guest,
        ).order_by(DoorCode.created_at.desc())
    )).scalars().all()
    latest_by_room: dict[str, DoorCode] = {}
    for c in codes:
        latest_by_room.setdefault(c.room_id, c)

    out: list[dict] = []
    for rid in room_ids:
        code = latest_by_room.get(rid)
        if code is not None:
            out.append({"room_id": rid, "status": code.status.value, "skipped_reason": None})
        else:
            # 没真号也会照常下码了，所以没码 = 房间没绑门锁设备（设备入库后才能下）。
            out.append({"room_id": rid, "status": None, "skipped_reason": "房间未绑定门锁，门锁待下码"})
    return out


async def notify_checkin_codes(db, order, only_room_ids=None,
                               include_deposit_card: bool = True) -> bool:
    """办理入住后把客人门锁密码卡片推到运营待办群（飞书），前台手机上即可看到+复制转发。
    fail-safe：飞书/解密任何问题都不抛、不阻断办理入住。返回是否推送了卡片。
    only_room_ids 给定则只推这几间房的卡（单间入住，避免逐间入住时重复推已入住房的卡）。
    include_deposit_card=False 供补发/重发场景：押金上传卡入住时已发过，别再刷一张。"""
    try:
        only = set(only_room_ids) if only_room_ids is not None else None
        conds = [
            DoorCode.order_id == order.order_id,
            DoorCode.purpose == DoorCodePurpose.guest,
            DoorCode.status.in_([DoorCodeStatus.active, DoorCodeStatus.pending]),
            DoorCode.password.isnot(None),
        ]
        if only is not None:
            conds.append(DoorCode.room_id.in_(only))
        rows = (await db.execute(
            select(DoorCode).where(*conds).order_by(DoorCode.created_at.desc())
        )).scalars().all()
        seen: set[str] = set()
        room_names: list[str] = []
        pushed = False
        for c in rows:
            if c.room_id in seen:
                continue
            seen.add(c.room_id)
            room = (await db.execute(
                select(Room).where(Room.room_id == c.room_id))).scalar_one_or_none()
            rname = room.room_name if room else c.room_id
            room_names.append(rname)
            try:
                code = decrypt_code(c.password)
            except Exception:
                continue
            # 整张客人入住卡片（前台复制整条转发给客人）。每间房一张。
            card = build_checkin_card(
                room_name=rname, door_code=code,
                check_in=order.check_in_date, check_out=order.check_out_date,
            )
            await send_password_text(card)
            pushed = True
            # 卡片后紧跟该房间的入住登记二维码（有码才发）。图片是加分项，
            # 查码/发图任何异常都不得影响卡片与办理入住 → 单独兜住。
            try:
                image_key = qr_image_key_for_room(rname)
                if image_key:
                    await send_password_image(image_key, room_label=rname)
            except Exception:
                logger.exception("checkin QR image push failed room=%s", rname)
        # 本单收了押金 → 密码群里紧跟一张「上传押金小票」按钮卡（前台内部用）。
        # best-effort：单独兜住，绝不影响密码卡与办理入住。
        if include_deposit_card:
            try:
                await _push_deposit_upload_card(order, room_names)
            except Exception:
                logger.exception("deposit upload card push failed order=%s",
                                 getattr(order, "order_id", "?"))
        return pushed
    except Exception:
        logger.exception("feishu checkin notify failed order=%s", getattr(order, "order_id", "?"))
        return False


async def gather_active_guest_codes(db, order) -> list[dict]:
    """取本单当前可用的客人门锁密码（解密后明文），供前台在详情里查看/复制（兜底）。

    可见口径与 notify_checkin_codes 一致：active/pending 的客人码、有密文、每房去重
    （取最新一条）。解密失败的码跳过（不把乱码/密文当密码给前台）。只读、不碰门锁。
    返回 [{room_id, room_name, password}]，无可用码时返回空列表。"""
    conds = [
        DoorCode.order_id == order.order_id,
        DoorCode.purpose == DoorCodePurpose.guest,
        DoorCode.status.in_([DoorCodeStatus.active, DoorCodeStatus.pending]),
        DoorCode.password.isnot(None),
    ]
    rows = (await db.execute(
        select(DoorCode).where(*conds).order_by(DoorCode.created_at.desc())
    )).scalars().all()
    seen: set[str] = set()
    out: list[dict] = []
    for c in rows:
        if c.room_id in seen:
            continue
        seen.add(c.room_id)
        try:
            code = decrypt_code(c.password)
        except Exception:
            continue
        room = (await db.execute(
            select(Room).where(Room.room_id == c.room_id))).scalar_one_or_none()
        out.append({
            "room_id": c.room_id,
            "room_name": room.room_name if room else c.room_id,
            "password": code,
        })
    return out


async def has_inflight_guest_code(db, order) -> bool:
    """本单是否有「正在下发/重试中」的客人码（pending/failed），供空态区分文案。

    入住瞬间下码可能 FAILED（锁一时离线）或 QUEUED 尚未写密码 → gather 取不到码，
    但 retry_pending 每 5 分钟会自动重推、会自愈。这种情况该告诉前台「稍候，会自动到」，
    而不是误导的「未办理入住/房间未绑锁/码已失效」。只读，不碰门锁。"""
    row = (await db.execute(
        select(DoorCode.id).where(
            DoorCode.order_id == order.order_id,
            DoorCode.purpose == DoorCodePurpose.guest,
            DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.failed]),
        ).limit(1)
    )).first()
    return row is not None


async def notify_recovered_guest_codes(db, recovered: list[tuple[str, str]]) -> None:
    """retry 把首发 FAILED 的客人码翻活后补发密码卡（2026-07-18）。

    首发 FAILED 时 notify_checkin_codes 不推卡（只推 active/pending），retry 成功后
    没人补 → 前台永远等不到密码。pending 码首发已带密码推过卡，不在此列（防重复刷群）。
    fail-safe：飞书任何问题只 log，绝不影响 retry 本身的落库。
    """
    by_order: dict[str, list[str]] = {}
    for order_id, room_id in recovered:
        by_order.setdefault(order_id, []).append(room_id)
    for order_id, room_ids in by_order.items():
        try:
            order = (await db.execute(
                select(Order).where(Order.order_id == order_id)
            )).scalar_one_or_none()
            if order is None:
                continue
            await notify_checkin_codes(db, order, only_room_ids=room_ids,
                                       include_deposit_card=False)
        except Exception:
            logger.exception("recovered code notify failed order=%s", order_id)


async def _push_deposit_upload_card(order, room_names: list[str]) -> None:
    """每单入住都发「上传押金小票」按钮卡到密码群。令牌绑本订单、免登录拍照页。

    不按 order.deposit>0 判断——押金是 POS 机刷的，系统里 deposit 字段前台经常不填，
    用它当闸会大量误伤（收了押金却不出按钮）。宁可每单都给前台一个入口，没收押金的
    单前台忽略即可。"""
    deposit = getattr(order, "deposit", None)
    from app.core.config import settings
    from app.core.datetime_helpers import today_cn
    from app.core.deposit_token import make_deposit_token
    from app.services.feishu_lead_alert import send_password_card
    from app.services.lock.checkin_card import build_deposit_upload_card

    # 令牌有效期覆盖整段住期 + 3 天缓冲（下限 7 天）：入住即发，但长住单也不能中途失效。
    try:
        days = max((order.check_out_date - today_cn()).days + 3, 7)
    except Exception:
        days = 7
    token = make_deposit_token(order.order_id, ttl_seconds=days * 86400)
    upload_url = f"{settings.FRONTEND_BASE_URL}/deposit-receipt/{token}"
    card = build_deposit_upload_card(
        room_label="、".join(room_names) or "本单",
        deposit=deposit,
        upload_url=upload_url,
    )
    await send_password_card(card)


async def _safe_rollback(db) -> None:
    try:
        await db.rollback()
    except Exception:
        logger.exception("lock hook rollback failed")


async def notify_transfer_code(db, order, new_room_id: str) -> bool:
    """换房后把新房门锁密码卡推到密码群（飞书），前台转发给客人。fail-safe。"""
    try:
        c = (await db.execute(
            select(DoorCode).where(
                DoorCode.order_id == order.order_id,
                DoorCode.room_id == new_room_id,
                DoorCode.purpose == DoorCodePurpose.guest,
                DoorCode.status.in_([DoorCodeStatus.active, DoorCodeStatus.pending]),
                DoorCode.password.isnot(None),
            ).order_by(DoorCode.created_at.desc())
        )).scalars().first()
        if c is None:
            return False
        room = (await db.execute(
            select(Room).where(Room.room_id == new_room_id))).scalar_one_or_none()
        rname = room.room_name if room else new_room_id
        try:
            code = decrypt_code(c.password)
        except Exception:
            return False
        card = build_checkin_card(
            room_name=rname, door_code=code,
            check_in=order.check_in_date, check_out=order.check_out_date)
        prefix = "🔁 客人已换房，门锁密码已重置。请把下面这张新卡转发给客人：\n\n"
        await send_password_text(prefix + card)
        try:
            image_key = qr_image_key_for_room(rname)
            if image_key:
                await send_password_image(image_key, room_label=rname)
        except Exception:
            logger.exception("transfer QR image push failed room=%s", rname)
        return True
    except Exception:
        logger.exception("feishu transfer notify failed order=%s", getattr(order, "order_id", "?"))
        return False


async def _process_transfer_relink(order_id: str, old_room_id: str, new_room_id: str) -> None:
    """换房后台化：新房下码 + 撤旧房码 + 推飞书新卡。开独立 session，全程 fail-safe。"""
    try:
        async with AsyncSessionLocal() as db:
            order = (await db.execute(
                select(Order).where(Order.order_id == order_id).options(selectinload(Order.rooms))
            )).scalar_one_or_none()
            if order is None:
                logger.warning("transfer relink: order %s not found, skip", order_id)
                return
            await OrderLockService(get_lock_provider(), db).on_room_transfer(
                order, old_room_id=old_room_id, new_room_id=new_room_id)
            await notify_transfer_code(db, order, new_room_id)
    except Exception:
        logger.exception("transfer relink failed order=%s", order_id)
        try:
            await send_lock_alert(
                f"⚠️ 换房门锁换码/通知异常\n订单：{order_id}\n"
                f"{old_room_id} → {new_room_id}\n请前台检查门锁或手动换码")
        except Exception:
            logger.exception("send lock alert failed order=%s", order_id)
