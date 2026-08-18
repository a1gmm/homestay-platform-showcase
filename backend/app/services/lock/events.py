"""慧享家 lockLogPush 开锁推送处理（PLAN_lock_auto_sync 方案1）。

每次锁被操作，厂商 push 一条日志到我们的回调，内含 electricNum（电量）。这里：
1. 按 logId 幂等去重（重复推送忽略），落 lock_events 一行（审计）。
2. 按 lockMac（兜底 roomId）匹配 lock_devices → 刷新 last_battery / last_online_at（电量自愈）。
3. 低电滞回告警：电量 ≤ 阈值告警一次，回充到重置线才复位（防刷屏）。

best-effort：任何异常不外抛——回调必须快返 200，避免厂商重推风暴。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.datetime_helpers import now_cn
from app.models.door_code import DoorCode, DoorCodePurpose, DoorCodeStatus
from app.models.lock_device import LockDevice
from app.models.lock_event import LockEvent
from app.services.feishu_lead_alert import send_lock_battery_alert

logger = logging.getLogger(__name__)

# 慧享家 lockLogPush 的 logType：8=添加钥匙（码真正写入锁后产生）。这条回调是
# 「apartmentAddPasswordKey 客户端超时但码其实已进锁」最可靠的异步确认（doc §推送事件）。
_LOG_TYPE_ADD_KEY = 8


@dataclass
class ProcessResult:
    accepted: bool = False        # 合法 push（有 logId）
    deduped: bool = False         # 同 logId 已处理过，忽略
    device_updated: bool = False  # 匹配到设备并刷新了电量/在线
    device_unknown: bool = False  # 落了 event 但匹配不到系统里的锁
    alert_sent: bool = False      # 本次触发了低电告警
    code_confirmed: bool = False  # logType=8 确认了一把待下发的客人码


def _epoch_to_dt(epoch) -> datetime | None:
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc)
    except (ValueError, TypeError, OSError, OverflowError):
        return None


async def process_lock_event(db: AsyncSession, payload: dict) -> ProcessResult:
    result = ProcessResult()
    data = (payload or {}).get("data") or {}
    log_id = data.get("logId")
    if not log_id:
        logger.warning("lockLogPush missing logId, ignored: %s", str(payload)[:200])
        return result
    result.accepted = True

    # 幂等去重
    if (await db.execute(select(LockEvent.id).where(LockEvent.log_id == log_id))).first():
        result.deduped = True
        return result

    electric = data.get("electricNum")
    electric = electric if isinstance(electric, int) else None
    lock_mac = data.get("lockMac")
    vendor_room_id = data.get("roomId")
    log_type = data.get("logType") if isinstance(data.get("logType"), int) else None

    db.add(
        LockEvent(
            log_id=log_id,
            vendor_room_id=vendor_room_id,
            lock_mac=lock_mac,
            log_type=log_type,
            log_level=data.get("logLevel"),
            log_alert=data.get("logAlert"),
            electric_num=electric,
            photo_url=data.get("photoUrl"),
            event_time=_epoch_to_dt(data.get("updateTime")),
        )
    )

    device = await _match_device(db, lock_mac=lock_mac, vendor_room_id=vendor_room_id)
    if device is None:
        result.device_unknown = True
        logger.warning("lockLogPush unknown lock mac=%s roomId=%s", lock_mac, vendor_room_id)
    else:
        device.last_online_at = now_cn()
        if electric is not None:
            device.last_battery = electric
            result.alert_sent = await _handle_low_battery(device, electric)
        result.device_updated = True
        # logType=8「添加钥匙」→ 该房待下发的客人码已真正写入锁，异步坐实为 active。
        # 补 apartmentAddPasswordKey 客户端超时（码已进锁但没收到同步成功）的确认缺口，
        # 断掉「以为没成→每轮重推→连环响」的病根。
        if log_type == _LOG_TYPE_ADD_KEY and device.room_id:
            result.code_confirmed = await _confirm_added_key(db, device.room_id)

    try:
        await db.commit()
    except IntegrityError:
        # 并发同 logId 撞唯一约束 → 当作去重
        await db.rollback()
        result.deduped = True
        result.device_updated = False
        result.alert_sent = False
    return result


async def _confirm_added_key(db: AsyncSession, room_id: str) -> bool:
    """收到该房 logType=8「添加钥匙」→ 把它 pending/manual 的客人码坐实为 active。

    apartmentAddPasswordKey 客户端超时时码其实已进锁，这条回调是最可靠的下码确认，
    故 pending（待确认）与 manual（重推超时转人工、其实可能已进锁）都据此翻活并清退避。
    只认客人码；同房多把取最新。返回是否确认了一把。keyId 精确匹配需 logKeys 附录字段，
    本期按房号+用途粗匹配（同房至多一把在途客人码）。
    """
    code = (await db.execute(
        select(DoorCode).where(
            DoorCode.room_id == room_id,
            DoorCode.purpose == DoorCodePurpose.guest,
            DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.manual]),
        ).order_by(DoorCode.created_at.desc())
    )).scalars().first()
    if code is None:
        return False
    code.status = DoorCodeStatus.active
    code.retry_count = 0
    code.next_retry_at = None
    code.last_error = None
    return True


async def _match_device(
    db: AsyncSession, *, lock_mac: str | None, vendor_room_id: str | None
) -> LockDevice | None:
    if lock_mac:
        d = (
            await db.execute(select(LockDevice).where(LockDevice.vendor_lock_mac == lock_mac))
        ).scalar_one_or_none()
        if d is not None:
            return d
    if vendor_room_id:
        d = (
            await db.execute(
                select(LockDevice).where(LockDevice.vendor_room_id == vendor_room_id)
            )
        ).scalar_one_or_none()
        if d is not None:
            return d
    return None


async def _handle_low_battery(device: LockDevice, electric: int) -> bool:
    """滞回闸。返回本次是否发了告警。"""
    low = settings.LOCK_LOW_BATTERY_THRESHOLD
    rearm = settings.LOCK_BATTERY_REARM_THRESHOLD
    if electric <= low and not device.battery_alerted:
        device.battery_alerted = True
        await _safe_alert(device, electric)
        return True
    if electric >= rearm and device.battery_alerted:
        device.battery_alerted = False
    return False


async def _safe_alert(device: LockDevice, electric: int) -> None:
    text = (
        "🔋 门锁低电告警\n"
        f"房间：{device.room_id}\n"
        f"当前电量：{electric}%（低于 {settings.LOCK_LOW_BATTERY_THRESHOLD}%）\n"
        "请尽快现场更换电池（5 号 AA）。耗尽会掉线，导致该房客人码/门锁下发失败。"
    )
    try:
        await send_lock_battery_alert(text)
    except Exception:
        logger.exception("send lock battery alert failed room=%s", device.room_id)
