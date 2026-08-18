"""OrderLockService —— 门锁业务编排层（见 plan §15.2 / §16）。

懂订单/保洁，把订单生命周期翻译成 LockProvider 调用，拥有 door_codes。
provider 注入（HxjiotProvider / ManualProvider，由 get_lock_provider 按 LOCK_PROVIDER 选）。

时窗（§15.7）：客人码 beginTime = 办理入住此刻，endTime = 退房日 14:00 北京（宽限上界）；
提前手动发起退房则立即撤码。
状态映射：GRANTED→active、QUEUED→pending、FAILED→failed。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, time, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_helpers import CN_TZ, to_cn
from app.models.door_code import DoorCode, DoorCodePurpose, DoorCodeStatus
from app.models.lock_device import LockDevice
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.services.lock.code_crypto import decrypt_code, encrypt_code
from app.services.lock.types import KEY_STATE_NORMAL, IssueOutcome

_CHECKOUT_HOUR = 14  # 退房日密码失效上界 14:00 北京（宽限：前台没及时发起退房也不至于12点就把客人锁门外；
                     # 提前手动发起退房则 on_checkout 立即撤码。2026-07-08 王总定）
_CLEANING_EXPIRE_HOUR = 12  # 保洁码兜底过期：退房次日 12:00 北京（方案 B，2026-07-08 王总定）
_INSPECT_VALID_DAYS = 30    # 查房码「不自动过期」：给锁一个足够长的窗口（30 天，远超任何
                            # 现实查房延迟）；真正失效靠前台点「查房通过·退押金」时 delKey
                            # 撤（王总 2026-07-12 拍板「不过期一直等人点」）

# 重推年龄上限：一条码建了多久还没确认 active 就停止无限重推（beat 每 5min 一轮）。
# 锁短暂离线几分钟会在窗口内自愈；真·持续离线超此时长 = 需人工介入，别再每 5 分钟
# 把码推给物理锁让它反复播报「设置成功」扰民（见 1401 事故 2026-07-09）。
_RETRY_MAX_AGE = timedelta(hours=2)

# 重推指数退避（安全网，见连环响事故 2026-07-21）：Celery beat 每 5 分钟一趟，但同一把
# 未确认码不该每趟都重推——否则「客户端超时但码已进锁」的在线锁会每 5 分钟反复播报「设置
# 成功」。据 retry_count 退避到 5→10→20→40→60min（封顶），下成功即清零。
_RETRY_BACKOFF_BASE = timedelta(minutes=5)
_RETRY_BACKOFF_CAP = timedelta(minutes=60)


def _backoff_delay(retry_count: int) -> timedelta:
    """第 retry_count 次未确认后到下次重推的间隔：5·2^(n-1)，封顶 60min。"""
    secs = _RETRY_BACKOFF_BASE.total_seconds() * (2 ** max(0, retry_count - 1))
    return timedelta(seconds=min(secs, _RETRY_BACKOFF_CAP.total_seconds()))

# 续住延码「宽限期」：关联/入住时锁离线致延码未坐实，交第二道网每 5 分钟静默重试；
# 锁一恢复即坐实、不打扰前台。只有锁「持续离线超此时长」仍没延成，才告警一次让前台
# 手动设——躲过锁短暂抖动的虚惊（王总 2026-07-21）。
_STALE_ALERT_GRACE = timedelta(minutes=30)
# 已就某把陈旧码告警过的去重标记（落 last_error）：仍离线的后续轮次不再刷屏。
_STALE_ALERTED = "锁离线超时，已提醒前台手动延期"
# 已就某把「在住客人持续卡 manual」的码告警过的去重标记（落 last_error）：下一轮不再刷屏。
# 与 retry_pending 翻 manual 时写的文案不同，故码被重推后再次翻 manual 会覆盖本标记→自然重报。
_MANUAL_ALERTED = "在住客人码持续转人工，已提醒前台手动设置"

logger = logging.getLogger(__name__)

_OUTCOME_TO_STATUS = {
    IssueOutcome.GRANTED: DoorCodeStatus.active,
    IssueOutcome.QUEUED: DoorCodeStatus.pending,
    IssueOutcome.FAILED: DoorCodeStatus.failed,
}


def _checkout_at(check_out_date) -> datetime:
    """退房日 14:00 北京时间（aware，宽限上界）。provider 会 int(.timestamp()) 转秒级 epoch。"""
    return datetime.combine(check_out_date, time(_CHECKOUT_HOUR, 0), tzinfo=CN_TZ)


def code_covers_checkout(end_at: datetime | None, check_out_date) -> bool:
    """这把码的有效期够不够开到该退房日 14:00。

    「续住段沿用组内那把码」是否安全、以及要不要告警，两处都问这一个问题，所以只留
    这一份实现——口径分家就会出现「入住时认为够用所以不延、告警时认为不够所以报警」。
    end_at 为空 = 有效期不明，按不覆盖算（宁可误报，不可漏报）。

    naive 值按北京时间补：本列的值都是 _checkout_at 写进去的北京 aware 值，SQLite/部分
    驱动会把 tzinfo 丢掉。这里**不**套 datetime_helpers.to_cn 的「naive 按 UTC」约定——
    那条约定是给 created_at 这类 func.now() 服务器 UTC 列用的，套到本列会整整宽 8 小时：
    退房日早上 9 点就失效的码会被判成「够用」，客人中午被锁在门外且没有任何告警。
    生产 Postgres 是 timestamptz，永远 aware，这条分支只在 SQLite 测试里走到。
    """
    if end_at is None:
        return False
    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=CN_TZ)
    return end_at >= _checkout_at(check_out_date)


async def _own_guest_code(db, order_id: str, room_id: str):
    """本单该房那把有效客人码（没有则 None）。"""
    return (await db.execute(
        select(DoorCode).where(
            DoorCode.order_id == order_id,
            DoorCode.room_id == room_id,
            DoorCode.purpose == DoorCodePurpose.guest,
            DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
        ).order_by(DoorCode.created_at.desc())
    )).scalars().first()


async def group_code_for_room(db, order, room_id: str):
    """续住关联组内、除本单外，该房那把有效客人码（没有则 None）。

    组内可能不止一把（补关联历史单时两张单各自入住过、各持一把），这里定死取「入住最早
    那张单」的那把 = 续住段实际沿用的那把（get_group_orders 按 check_in_date 升序）。
    """
    if not order.stay_group_id:
        return None
    from app.services import stay_group as _sg
    for go in await _sg.operational_group_orders(db, order):
        if go.order_id == order.order_id:
            continue
        code = await _own_guest_code(db, go.order_id, room_id)
        if code is not None:
            return code
    return None


async def operative_guest_code(db, order, room_id: str):
    """本段这间房，客人实际会拿去开门的那把码（没有则 None）。

    选码顺序必须和 on_guest_checkin 一模一样：本单自己有码就是自己的（幂等跳过下码），
    否则是续住组内沿用的那把。告警只有问「这把码行不行」才有意义——组内另一把碰巧覆盖
    得够长、但客人根本没在用的码，不能拿来证明客人进得了门（否则真的延期失败会静默）。
    """
    own = await _own_guest_code(db, order.order_id, room_id)
    if own is not None:
        return own
    return await group_code_for_room(db, order, room_id)


def _cleaning_expiry(now_utc: datetime) -> datetime:
    """保洁码兜底过期上界：退房次日 12:00 北京（aware）。以退房那一刻的北京日历日 +1 计，
    晚退房/次日早上打扫都够用。provider 会 int(.timestamp()) 转秒级 epoch。"""
    nextday = (to_cn(now_utc) + timedelta(days=1)).date()
    return datetime.combine(nextday, time(_CLEANING_EXPIRE_HOUR, 0), tzinfo=CN_TZ)


def _cleaning_phone(order_id: str, room_id: str, start_at: datetime) -> str:
    """保洁码占位号：按订单+房号+**下码时刻**生成。两层不撞厂商 500007（同号同锁只允
    一个授权窗）：①与客人码占位号（仅按订单号）必然不同；②续住同房**逐天**下码得**不同号**。

    历史上按订单+房号恒定，续住 day2 与 day1 用同号；day1 授权窗在锁离线那阵没被真正清掉
    （DB 标 revoked 但厂商侧残留）→ day2 同号+新开始时间撞 500007（生产 1611/1613
    2026-07-29）。占位号只作厂商内部标识、不影响 6 位密码开锁；撤码认存的 vendor_key、
    续期/重试认存的 phone_no，均不重构此号，故按下码时刻取唯一安全，残留短窗自行过期。"""
    return _placeholder_phone(f"{order_id}#clean#{room_id}#{int(start_at.timestamp())}")


def _inspect_expiry(now_utc: datetime) -> datetime:
    """查房码有效窗上界：下码此刻 + 30 天（北京 aware）。查房码语义上不自动过期
    （靠前台点「查房通过」撤），此窗仅作锁侧兜底、远超任何现实查房延迟。
    provider 会 int(.timestamp()) 转秒级 epoch。"""
    return to_cn(now_utc) + timedelta(days=_INSPECT_VALID_DAYS)


def _inspect_phone(order_id: str, room_id: str) -> str:
    """查房码占位号：按订单+房号生成，与客人码/保洁码占位号都不同 →
    同锁上三把码各自独立授权窗，不撞厂商 500007（同号同锁只允一个窗）。"""
    return _placeholder_phone(f"{order_id}#inspect#{room_id}")


def _placeholder_phone(order_id: str) -> str:
    """没真实手机号时，按订单号生成确定性的 11 位占位号（看似手机号，喂给厂商当内部标识）。
    同订单恒定 → 续期/撤码都认它；不同订单不同 → 不会撞 500007/500008。
    客人靠 6 位密码开门，占位号不影响开锁。"""
    h = int(hashlib.sha1(order_id.encode()).hexdigest(), 16)
    second = 3 + (h % 7)          # 第 2 位 3~9，整体形似手机号，提升厂商兼容
    rest = (h // 7) % (10 ** 9)   # 后 9 位
    return f"1{second}{rest:09d}"


class OrderLockService:
    def __init__(self, provider, db: AsyncSession, clock=None):
        self._provider = provider
        self._db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def _assigned_rooms(self, order_id: str) -> list[OrderRoom]:
        rows = await self._db.execute(
            select(OrderRoom).where(
                OrderRoom.order_id == order_id, OrderRoom.room_id.isnot(None)
            )
        )
        return list(rows.scalars().all())

    async def _vendor_room_id(self, room_id: str) -> str | None:
        row = await self._db.execute(
            select(LockDevice).where(LockDevice.room_id == room_id)
        )
        dev = row.scalar_one_or_none()
        return dev.vendor_room_id if dev else None

    async def _has_active_code(self, order_id: str, room_id: str, purpose) -> bool:
        """该订单该房该用途已有 active/pending 码？幂等防重复下码（§16 决策2）。"""
        row = await self._db.execute(
            select(DoorCode.id).where(
                DoorCode.order_id == order_id,
                DoorCode.room_id == room_id,
                DoorCode.purpose == purpose,
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
            )
        )
        return row.first() is not None

    async def _has_active_guest_code(self, order_id: str, room_id: str) -> bool:
        return await self._has_active_code(order_id, room_id, DoorCodePurpose.guest)

    async def _group_room_has_guest_code(self, order, room_id: str) -> bool:
        """续住关联组内、除本单外，是否已有别的单在该房下过有效客人码。
        有则续住段沿用锚单码、不重复下码（见 on_guest_checkin）。"""
        return await group_code_for_room(self._db, order, room_id) is not None

    async def _group_code_for_room(self, order, room_id: str):
        return await group_code_for_room(self._db, order, room_id)

    async def _extend_code_to_segment(self, code, room, target_date) -> bool:
        """把续住段沿用的那把码延到 target_date 14:00。返回是否改了 end_at（调用方据此 commit）。

        必要性：关联发生在锚单入住前时，link_continuation 那一刻锚单还没码可延（
        extend_guest_codes 拿到空结果直接放过），锚单入住时 end_at 只按它自己的退房日下，
        续住段又沿用不下码 → 密码在本段中途失效，客人住着却被锁在门外。

        target_date 用【组末退房日】而非本段退房日（D10）：入住、改期、自愈三条路径统一
        延到组末，并发办入住时各段都指向同一日期→幂等、不会各延各的让 DB 与物理锁分叉；
        组末 ≥ 任何段退房日，绝不缩短、绝不把还在住的客人锁门外。无组时退回本段退房日。
        厂商未确认（锁离线）时保持旧 end_at 不写，由 _alert_if_codes_missing 告警——
        宁可让前台去手动延，也绝不静默（王总 2026-07-12）。
        """
        if code_covers_checkout(code.end_at, target_date):
            return False  # 已覆盖组末（别的段已延过），不必再打厂商——并发第二个入住到此幂等收敛
        new_end = _checkout_at(target_date)
        vendor_room_id = await self._vendor_room_id(room.room_id)
        if vendor_room_id is None:
            return False
        if not await self._provider.renew(vendor_room_id, code.phone_no, new_end):
            logger.warning("续住段沿用码延期未确认 room=%s code_order=%s，end_at 保持旧值",
                           room.room_id, code.order_id)
            return False
        code.end_at = new_end
        return True

    async def on_guest_checkin(self, order, only_room_ids=None) -> None:
        """前台办理入住：给订单每间已排房下客人码并落库。

        三段式，避免多房串行等厂商拖慢入住：① 串行做完 DB 判定选出待下码房间
        （AsyncSession 非并发安全）→ ② 并发打厂商 issue_code（纯 HTTP，不碰 session；
        多房不再叠加、单锁离线只吃一次超时）→ ③ 串行落库 + 一次 commit。

        only_room_ids 给定则只给这几间房下码（单间入住，别给订单里尚未入住的房提前下码）；
        None = 整单入住给全部已排房下码（兼容原行为）。
        """
        only = set(only_room_ids) if only_room_ids is not None else None

        def _in_scope(room) -> bool:
            return only is None or room.room_id in only

        # 没真实手机号也照常下码（OTA 单常拿不到客人号）。厂商只把 phoneNo 当内部标识，
        # 客人靠 6 位密码开门、不靠手机号；故用订单号生成的占位号顶上（续期/撤码都认它）。
        phone = order.guest_phone or _placeholder_phone(order.order_id)
        start_at = self._clock()

        # ⓪ 清掉该房残留的查房码（keeper）：上一段住的查房码若前台一直没点「查房通过·退押金」
        #    则不会自动过期（end_at 兜底 +30 天），绝不能带进新客人这一住——否则退押金群里
        #    见过码的人能在新客人住期内开门。入住是硬边界：新客人一来就撤（不带 order_id →
        #    撤该物理房全部有效查房码，跨订单）。常态无残留码时只是一次空 SELECT，不加下码延迟。
        #    best-effort：撤码失败绝不阻断入住下码（review 三评审共识，王总 2026-07-12）。
        for room in await self._assigned_rooms(order.order_id):
            if room.room_id is None or not _in_scope(room):
                continue
            try:
                await self.revoke_inspection_codes(room.room_id)
            except Exception:  # noqa: BLE001 — best-effort，绝不阻断入住
                logger.warning("入住前撤残留查房码失败 room=%s（不阻断入住）",
                               room.room_id, exc_info=True)

        # ① 串行选房（DB 读）
        pending: list[tuple] = []  # (room, vendor_room_id, end_at, key_name)
        group_extended = False
        # 续住组沿用码统一延到【组末退房日】（D10）：入住/改期/自愈同一目标，并发办入住
        # 不会各延各的让 DB 与锁分叉。组末 ≥ 各段退房日，绝不缩短。无组则各段退房日。
        group_final = None
        if order.stay_group_id:
            from app.services import stay_group as _sg
            group_final = await _sg.operational_group_final_checkout_date(
                self._db, order, alive_only=True)
        for room in await self._assigned_rooms(order.order_id):
            if not _in_scope(room):
                continue  # 单间入住：只给本次入住的房下码
            if await self._has_active_guest_code(order.order_id, room.room_id):
                continue  # 幂等：已有有效客人码，跳过
            # 续住关联组：本房间组内（别的单）已有有效客人码则沿用，不重复下码。
            # eng-review #6：按"已有码"判定而非"是不是锚单"——续住段先入住时也能正确接管。
            # 沿用不等于原样不动：得把那把码延到本段退房日，否则它在本段中途就失效
            # （见 _extend_code_to_segment）。
            group_code = (await self._group_code_for_room(order, room.room_id)
                          if order.stay_group_id else None)
            if group_code is not None:
                target = group_final or room.check_out_date
                if await self._extend_code_to_segment(group_code, room, target):
                    group_extended = True
                continue
            vendor_room_id = await self._vendor_room_id(room.room_id)
            if vendor_room_id is None:
                continue  # 未映射门锁设备，跳过（设备入库后才能下码）
            end_at = _checkout_at(room.check_out_date)
            key_name = f"客人-{order.guest_name}-{room.check_in_date:%Y%m%d}"
            pending.append((room, vendor_room_id, end_at, key_name))

        if not pending:
            # 续住段常态：没新码可下，但沿用码的 end_at 刚被延过，得落库。
            if group_extended:
                await self._db.commit()
            return

        # ② 并发下码（不触碰 db session）
        results = await asyncio.gather(*(
            self._provider.issue_code(
                vendor_room_id=vendor_room_id,
                phone=phone,
                start_at=start_at,
                end_at=end_at,
                key_name=key_name,
            )
            for (_room, vendor_room_id, end_at, key_name) in pending
        ))

        # ③ 串行落库
        for (room, _vendor_room_id, end_at, _key_name), result in zip(pending, results):
            self._db.add(
                DoorCode(
                    order_id=order.order_id,
                    room_id=room.room_id,
                    purpose=DoorCodePurpose.guest,
                    phone_no=phone,
                    # at-rest 加密（§8.2）：列里存 Fernet 密文，DB 泄露不泄露有效码
                    password=encrypt_code(result.password) if result.password else None,
                    status=_OUTCOME_TO_STATUS[result.outcome],
                    vendor_key_id=result.vendor_key_id,
                    vendor_lock_key_id=result.vendor_lock_key_id,
                    vendor_key_group_id=result.vendor_key_group_id,
                    start_at=start_at,
                    end_at=end_at,
                    last_error=result.error,
                )
            )
        await self._db.commit()

    async def _active_guest_codes(self, order_id: str) -> list[DoorCode]:
        rows = await self._db.execute(
            select(DoorCode).where(
                DoorCode.order_id == order_id,
                DoorCode.purpose == DoorCodePurpose.guest,
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
            )
        )
        return list(rows.scalars().all())

    async def _group_guest_codes(self, order, room_id=None,
                                 extra_order_ids=None) -> list[DoorCode]:
        """组感知查有效客人码：续住组的共享码物理上只有一行、挂锚单 order_id 下
        （续段入住被 _group_room_has_guest_code 抑制），按操作单自己的 order_id 查
        会扑空。故取组内全部段的 order_id 一起查（无组 = 只有自己）；room_id 给定
        则只看该房；extra_order_ids 供取消流程补传「取消前」的组成员（unlink 之后
        组号已清，按当前组号查不到旧成员）。"""
        order_ids = {order.order_id} | set(extra_order_ids or ())
        if getattr(order, "stay_group_id", None):
            from app.services import stay_group as _sg
            order_ids |= {
                o.order_id for o in await _sg.operational_group_orders(self._db, order)
            }
        conds = [
            DoorCode.order_id.in_(order_ids),
            DoorCode.purpose == DoorCodePurpose.guest,
            DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
        ]
        if room_id is not None:
            conds.append(DoorCode.room_id == room_id)
        rows = await self._db.execute(select(DoorCode).where(*conds))
        return list(rows.scalars().all())

    async def _revoke_guest_codes(self, order, room_ids=None, extra_order_ids=None) -> None:
        now = self._clock()
        # room_ids 给定则只撤这几间房的客人码（单间退房）；None = 撤整单全部。
        # 组感知：共享码挂锚单名下，末段退房也必须撤得到（退房闸已保证只有末段能走到撤码）。
        room_filter = set(room_ids) if room_ids is not None else None
        for code in await self._group_guest_codes(order, extra_order_ids=extra_order_ids):
            if room_filter is not None and code.room_id not in room_filter:
                continue
            if code.vendor_key_id:
                await self._provider.revoke_code(code.vendor_key_id)
            code.status = DoorCodeStatus.revoked
            code.revoked_at = now
        await self._db.commit()

    async def _active_cleaning_code_plain(self, order_id: str, room_id: str) -> str | None:
        """取该订单该房现有有效保洁码的明文（幂等回显用）。解不开则 None。"""
        row = await self._db.execute(
            select(DoorCode).where(
                DoorCode.order_id == order_id,
                DoorCode.room_id == room_id,
                DoorCode.purpose == DoorCodePurpose.cleaning,
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
                DoorCode.password.isnot(None),
            ).order_by(DoorCode.created_at.desc())
        )
        code = row.scalars().first()
        if code and code.password:
            try:
                return decrypt_code(code.password)
            except Exception:
                return None
        return None

    async def issue_cleaning_codes(self, order, room_ids) -> dict[str, str]:
        """退房后给每间待保洁房下限时保洁码（§16 方案 B）。

        时窗：退房此刻（clock）→ 退房次日 12:00 北京兜底过期。与客人码相互独立
        （purpose=cleaning + 独立占位号），on_checkout 的精确 delKey 只删客人码、
        不动保洁码。幂等：已有有效保洁码则回显不重下。返回 {room_id: 明文码}
        供保洁群卡片展示（明文只此一处出栈，落库仍 Fernet 加密）。
        """
        codes: dict[str, str] = {}
        start_at = self._clock()
        end_at = _cleaning_expiry(start_at)
        for room_id in room_ids:
            if await self._has_active_code(order.order_id, room_id, DoorCodePurpose.cleaning):
                existing = await self._active_cleaning_code_plain(order.order_id, room_id)
                if existing:
                    codes[room_id] = existing
                continue
            vendor_room_id = await self._vendor_room_id(room_id)
            if vendor_room_id is None:
                continue  # 未映射门锁设备，跳过
            phone = _cleaning_phone(order.order_id, room_id, start_at)
            result = await self._provider.issue_code(
                vendor_room_id=vendor_room_id,
                phone=phone,
                start_at=start_at,
                end_at=end_at,
                key_name=f"保洁-{room_id}-{to_cn(start_at):%m%d}",
            )
            self._db.add(
                DoorCode(
                    order_id=order.order_id,
                    room_id=room_id,
                    purpose=DoorCodePurpose.cleaning,
                    phone_no=phone,
                    password=encrypt_code(result.password) if result.password else None,
                    status=_OUTCOME_TO_STATUS[result.outcome],
                    vendor_key_id=result.vendor_key_id,
                    vendor_lock_key_id=result.vendor_lock_key_id,
                    vendor_key_group_id=result.vendor_key_group_id,
                    start_at=start_at,
                    end_at=end_at,
                    last_error=result.error,
                )
            )
            if result.password:
                codes[room_id] = result.password
        await self._db.commit()
        return codes

    async def revoke_cleaning_codes(self, room_id: str, order_id: str | None = None) -> None:
        """撤该房有效保洁码（保洁完工/兜底）：精确 delKey + 标记 revoked。
        order_id 给定则限本单，否则撤该房全部有效保洁码（回调只带 room_id 时的兜底）。
        只动 purpose=cleaning，客人码不受影响。"""
        conds = [
            DoorCode.room_id == room_id,
            DoorCode.purpose == DoorCodePurpose.cleaning,
            DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
        ]
        if order_id:
            conds.append(DoorCode.order_id == order_id)
        rows = await self._db.execute(select(DoorCode).where(*conds))
        now = self._clock()
        changed = False
        for code in rows.scalars().all():
            if code.vendor_key_id:
                await self._provider.revoke_code(code.vendor_key_id)
            code.status = DoorCodeStatus.revoked
            code.revoked_at = now
            changed = True
        if changed:
            await self._db.commit()

    async def issue_inspection_codes(self, order, room_ids) -> dict[str, str]:
        """保洁完工、发退押金卡时给每间房下一把查房码（purpose=keeper，王总 2026-07-12）。

        给前台/查房人的进门码，塞进「查房+退押金」卡展示。语义上不自动过期——失效
        唯一途径是前台点「查房通过·退押金」时 delKey 撤（防重复查房）；此处 end_at
        仅锁侧兜底（+30 天）。与客人码/保洁码相互独立（keeper purpose + 独立占位号），
        三把码在同锁上互不干扰。幂等：已有有效查房码则回显不重下。返回 {room_id: 明文码}
        供卡片展示（明文只此一处出栈，落库仍 Fernet 加密）。
        """
        codes: dict[str, str] = {}
        start_at = self._clock()
        end_at = _inspect_expiry(start_at)
        for room_id in room_ids:
            if await self._has_active_code(order.order_id, room_id, DoorCodePurpose.keeper):
                existing = await self._active_inspection_code_plain(order.order_id, room_id)
                if existing:
                    codes[room_id] = existing
                continue
            vendor_room_id = await self._vendor_room_id(room_id)
            if vendor_room_id is None:
                continue  # 未映射门锁设备，跳过
            phone = _inspect_phone(order.order_id, room_id)
            result = await self._provider.issue_code(
                vendor_room_id=vendor_room_id,
                phone=phone,
                start_at=start_at,
                end_at=end_at,
                key_name=f"查房-{room_id}-{to_cn(start_at):%m%d}",
            )
            self._db.add(
                DoorCode(
                    order_id=order.order_id,
                    room_id=room_id,
                    purpose=DoorCodePurpose.keeper,
                    phone_no=phone,
                    password=encrypt_code(result.password) if result.password else None,
                    status=_OUTCOME_TO_STATUS[result.outcome],
                    vendor_key_id=result.vendor_key_id,
                    vendor_lock_key_id=result.vendor_lock_key_id,
                    vendor_key_group_id=result.vendor_key_group_id,
                    start_at=start_at,
                    end_at=end_at,
                    last_error=result.error,
                )
            )
            if result.password:
                codes[room_id] = result.password
        await self._db.commit()
        return codes

    async def _active_inspection_code_plain(self, order_id: str, room_id: str) -> str | None:
        """取该订单该房现有有效查房码的明文（幂等回显）。解不开则 None。"""
        row = await self._db.execute(
            select(DoorCode).where(
                DoorCode.order_id == order_id,
                DoorCode.room_id == room_id,
                DoorCode.purpose == DoorCodePurpose.keeper,
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
                DoorCode.password.isnot(None),
            ).order_by(DoorCode.created_at.desc())
        )
        code = row.scalars().first()
        if code and code.password:
            try:
                return decrypt_code(code.password)
            except Exception:
                return None
        return None

    async def revoke_inspection_codes(self, room_id: str, order_id: str | None = None) -> None:
        """撤该房有效查房码（前台点「查房通过·退押金」触发，防重复查房）：精确 delKey +
        标记 revoked。order_id 给定则限本单，否则撤该房全部有效查房码（回调只带 room_id
        时的兜底）。只动 purpose=keeper，客人码/保洁码不受影响。"""
        conds = [
            DoorCode.room_id == room_id,
            DoorCode.purpose == DoorCodePurpose.keeper,
            DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
        ]
        if order_id:
            conds.append(DoorCode.order_id == order_id)
        rows = await self._db.execute(select(DoorCode).where(*conds))
        now = self._clock()
        changed = False
        for code in rows.scalars().all():
            if code.vendor_key_id:
                await self._provider.revoke_code(code.vendor_key_id)
            code.status = DoorCodeStatus.revoked
            code.revoked_at = now
            changed = True
        if changed:
            await self._db.commit()

    async def on_cancel(self, order, group_member_ids=None) -> None:
        """订单取消：作废客人码。

        续住组里取消一段时，组内还有别的活段（未取消未删除）的话，客人还住着——
        仍被活段覆盖的房间的客人码（含挂在本单名下的共享码）不撤；组里没有活段了
        才把整组该撤的码（含挂在早已取消段名下的）一并撤干净。

        group_member_ids：取消流程会先 unlink 再撤码，组号那时已清；调用方传
        「取消前」抓的组成员快照，判断才不会失真。
        """
        member_ids = set(group_member_ids or ())
        if getattr(order, "stay_group_id", None):
            from app.services import stay_group as _sg
            member_ids |= {
                o.order_id
                for o in await _sg.operational_group_orders(self._db, order)
            }
        member_ids.discard(order.order_id)
        if not member_ids:
            await self._revoke_guest_codes(order)
            return

        siblings = (await self._db.execute(
            select(Order).where(Order.order_id.in_(member_ids))
        )).scalars().all()
        alive = [o for o in siblings
                 if not o.is_deleted and o.order_status != OrderStatus.cancelled]
        if not alive:
            # 本单是组里最后一个活段：整组共享码一并撤（含挂在旧成员名下的）。
            await self._revoke_guest_codes(order, extra_order_ids=member_ids)
            return

        covered = set((await self._db.execute(
            select(OrderRoom.room_id).where(
                OrderRoom.order_id.in_([o.order_id for o in alive]),
                OrderRoom.room_id.isnot(None),
            )
        )).scalars().all())
        now = self._clock()
        for code in await self._active_guest_codes(order.order_id):
            if code.room_id in covered:
                continue  # 剩余活段还在住这间房，撤共享码等于把客人锁在门外
            if code.vendor_key_id:
                await self._provider.revoke_code(code.vendor_key_id)
            code.status = DoorCodeStatus.revoked
            code.revoked_at = now
        await self._db.commit()

    async def on_checkout(self, order, room_ids=None) -> None:
        """退房：精确 delKey 删客人码（§16决策3，不用 revoke_all 以免误删总码/保洁码）。

        room_ids 给定则只撤这几间房的客人码（单间退房，别动订单里仍在住房的码）；
        None = 撤整单全部（整单退房，兼容原行为）。
        """
        await self._revoke_guest_codes(order, room_ids=room_ids)

    async def reconcile_room_states(self, *, live: bool) -> dict:
        """慧享家房态(roomState)对账自愈（spec 2026-07-14）。

        拿我们系统真实占用校准慧享家 roomState，只做两色（有人=2/没人=1）：
        - 没人却非闲置 且 房内无任何在用码 → checkOut 清成闲置(1)；
        - 有人却非在住 → 收集告警（一期不自动拉回，见 spec §七）；
        - **房内还有 active/pending 码的一律不碰**（安全守卫：防把正在打扫的保洁员/
          系统误判下仍有效的客人锁在门外）；
        - 未绑锁 / 慧享家没拉到该房 roomState → 不动。

        live=False 即 dry-run：只算不发指令、不告警，返回将要执行的清单供人工过目。
        逐房 fail-safe，单房厂商错误不中断整轮。
        """
        from app.services.feishu_lead_alert import send_lock_alert
        from app.services.lock.roomstate_sync import (
            STATE_VACANT,
            ReconcileAction,
            decide_action,
        )

        empty = {"checked_out": [], "stuck": [], "skipped_in_use": [], "noop": 0, "live": live}
        devices = (await self._db.execute(select(LockDevice))).scalars().all()
        if not devices:
            return empty

        try:
            vendor_rooms = await self._provider.list_rooms()
        except Exception:
            logger.exception("reconcile: list_rooms 失败，本轮跳过")
            return {**empty, "error": "list_rooms_failed"}
        state_by_vrid = {vr.vendor_room_id: vr.room_state for vr in vendor_rooms}

        today = to_cn(self._clock()).date()
        occupied_rooms = set((await self._db.execute(
            select(OrderRoom.room_id)
            .join(Order, Order.order_id == OrderRoom.order_id)
            .where(
                OrderRoom.room_id.isnot(None),
                Order.is_deleted.is_(False),
                Order.order_status.in_([OrderStatus.checked_in, OrderStatus.pending_checkout]),
                OrderRoom.check_in_date <= today,
                OrderRoom.check_out_date >= today,
            )
        )).scalars().all())
        coded_rooms = set((await self._db.execute(
            select(DoorCode.room_id).where(
                DoorCode.status.in_([DoorCodeStatus.active, DoorCodeStatus.pending]),
            )
        )).scalars().all())

        # 顺手把 list_rooms 带回的在线/电量写回 lock_devices（2026-07-18：lockLogPush
        # 回调 7/12 起断供，事件驱动的在线/电量自愈全盲——本轮询是唯一兜底）。
        # dry-run 也持久化：这是只读厂商数据的本地记账，不是对锁下指令。
        # 低电告警复用 events 的 battery_alerted 挂闸（≤20 报一次、回充 ≥40 解闸）。
        from app.services.lock.events import _handle_low_battery

        vr_by_vrid = {vr.vendor_room_id: vr for vr in vendor_rooms}
        offline_occupied: list[str] = []
        for dev in devices:
            vr = vr_by_vrid.get(dev.vendor_room_id)
            if vr is None:
                continue
            if vr.battery is not None:
                dev.last_battery = vr.battery
                await _handle_low_battery(dev, vr.battery)
            if vr.online:
                dev.last_online_at = self._clock()
            elif dev.room_id in occupied_rooms:
                # 在住房锁离线才告警：直接影响下码/撤码；空房离线不扰民
                offline_occupied.append(dev.room_id)
        await self._db.commit()

        checked_out: list[str] = []
        stuck: list[str] = []
        skipped: list[str] = []
        noop = 0
        for dev in devices:
            rid = dev.room_id
            occupied = rid in occupied_rooms
            state = state_by_vrid.get(dev.vendor_room_id)
            has_code = rid in coded_rooms
            action = decide_action(occupied, state, has_code)
            if action == ReconcileAction.checkout:
                checked_out.append(rid)
                if live:
                    try:
                        await self._provider.revoke_all(dev.vendor_room_id)
                    except Exception:
                        logger.exception("reconcile: checkOut 失败 room=%s", rid)
            elif action == ReconcileAction.alert_stuck:
                stuck.append(rid)
            elif (not occupied) and has_code and state not in (None, STATE_VACANT):
                skipped.append(rid)  # 想清成闲置但守卫命中（房内有码，正在用）
            else:
                noop += 1

        if live:
            # stuck 告警去重：同一批房只报一次，集合变化才再报，避免每 15 分钟刷屏。
            from app.services.lock.stuck_alert_guard import (
                OFFLINE_TEMPLATE,
                record_set_alert,
                record_stuck_alert,
                set_needs_alert,
                stuck_needs_alert,
            )

            if stuck:
                if await stuck_needs_alert(self._db, stuck):
                    try:
                        await send_lock_alert(
                            "⚠️ 慧享家房态对账：以下房系统显示有人住、但慧享家未显示「在住」，请核查：\n"
                            + "、".join(sorted(stuck))
                        )
                        await record_stuck_alert(self._db, stuck)  # 发送成功才记标记
                    except Exception:
                        logger.exception("reconcile: 发送 stuck 告警失败")
            else:
                # 恢复一致：清标记，使同批房日后复现能重新告警。
                await record_stuck_alert(self._db, [])

            # 在住房锁离线告警：同款集合去重（锁恢复/换批才再报），不每 15 分钟刷屏。
            if offline_occupied:
                if await set_needs_alert(self._db, offline_occupied, OFFLINE_TEMPLATE):
                    try:
                        await send_lock_alert(
                            "⚠️ 在住房门锁离线：\n"
                            + "、".join(sorted(offline_occupied))
                            + "\n离线期间新密码推不到锁上（下码/撤码会延迟生效），"
                            "请检查门锁网络或路由器"
                        )
                        await record_set_alert(self._db, offline_occupied, OFFLINE_TEMPLATE)
                    except Exception:
                        logger.exception("reconcile: 发送门锁离线告警失败")
            else:
                await record_set_alert(self._db, [], OFFLINE_TEMPLATE)

        logger.info(
            "reconcile roomState live=%s checkout=%d stuck=%d skipped=%d noop=%d",
            live, len(checked_out), len(stuck), len(skipped), noop,
        )
        return {
            "checked_out": sorted(checked_out),
            "stuck": sorted(stuck),
            "skipped_in_use": sorted(skipped),
            "noop": noop,
            "live": live,
        }

    async def on_reschedule(self, order) -> None:
        """改期：按各房新退房时间 renew 客人码，并同步 door_code.end_at。

        续住组：共享码挂锚单名下、覆盖整段，组内任一段改日期都要把码对齐到
        **组末退房日**（活段口径），不能按本段退房日——按本段缩码会把还在住的
        客人锁在门外，按本单 order_id 查码则根本找不到。无组行为不变。
        """
        group_final = None
        if getattr(order, "stay_group_id", None):
            from app.services import stay_group as _sg
            group_final = await _sg.operational_group_final_checkout_date(
                self._db, order, alive_only=True)
        if group_final is not None:
            codes = await self._group_guest_codes(order)
        else:
            codes = await self._active_guest_codes(order.order_id)

        rooms = {r.room_id: r for r in await self._assigned_rooms(order.order_id)}
        for code in codes:
            if group_final is None:
                room = rooms.get(code.room_id)
                if room is None:
                    continue
                new_end = _checkout_at(room.check_out_date)
            else:
                new_end = _checkout_at(group_final)
            vendor_room_id = await self._vendor_room_id(code.room_id)
            if vendor_room_id is None:
                continue
            ok = await self._provider.renew(vendor_room_id, code.phone_no, new_end)
            if ok:
                code.end_at = new_end
            else:
                # 厂商未确认延期（锁离线/报错）：绝不乐观改库。否则库里显示已延期、
                # 物理锁上仍是旧退房时间 → 客人到原退房日被锁门外（1613 类隐患）。
                code.last_error = "改期门锁延期未确认（锁可能离线），退房时间未同步到锁"
                logger.warning(
                    "on_reschedule renew 未确认 room=%s order=%s，end_at 保持旧值",
                    code.room_id, getattr(order, "order_id", "?"),
                )
        await self._db.commit()

    async def on_room_transfer(self, order, old_room_id: str, new_room_id: str) -> None:
        """已入住换房：先给新房下码、再定向撤旧房码（先下新再撤旧，任一失败客人不至两头进不去）。

        只处理传入的 old→new 这一对房（多房单其余房不动）。撤旧码用定向删，
        **绝不用 _revoke_guest_codes**（撤全部会连刚下的新码一起撤）。

        续住组：旧房共享码可能挂在组内任一段（通常是锚单）名下，撤码按「房间+组」
        查；新码挂锚单（活段口径）名下、有效期到组末退房日，维持「组码挂锚单、
        覆盖整段」不变式。无组行为不变。
        """
        anchor_order_id = order.order_id
        group_final = None
        if getattr(order, "stay_group_id", None):
            from app.services import stay_group as _sg
            anchor = await _sg.operational_group_anchor(self._db, order, alive_only=True)
            if anchor is not None:
                anchor_order_id = anchor.order_id
            group_final = await _sg.operational_group_final_checkout_date(
                self._db, order, alive_only=True)

        # ① 新房下码（幂等：新房已有有效客人码——组内任一段名下的都算——则跳过；
        #    未映射门锁设备则跳过）
        if not await self._group_guest_codes(order, room_id=new_room_id):
            vendor_room_id = await self._vendor_room_id(new_room_id)
            if vendor_room_id is not None:
                room = next((r for r in await self._assigned_rooms(order.order_id)
                             if r.room_id == new_room_id), None)
                check_out = group_final or (room.check_out_date if room else order.check_out_date)
                start_at = self._clock()
                end_at = _checkout_at(check_out)
                phone = order.guest_phone or _placeholder_phone(order.order_id)
                key_name = f"客人-{order.guest_name}-换房{to_cn(start_at):%Y%m%d}"
                result = await self._provider.issue_code(
                    vendor_room_id=vendor_room_id, phone=phone,
                    start_at=start_at, end_at=end_at, key_name=key_name)
                self._db.add(DoorCode(
                    order_id=anchor_order_id, room_id=new_room_id,
                    purpose=DoorCodePurpose.guest, phone_no=phone,
                    password=encrypt_code(result.password) if result.password else None,
                    status=_OUTCOME_TO_STATUS[result.outcome],
                    vendor_key_id=result.vendor_key_id,
                    vendor_lock_key_id=result.vendor_lock_key_id,
                    vendor_key_group_id=result.vendor_key_group_id,
                    start_at=start_at, end_at=end_at, last_error=result.error))

        # ② 定向撤旧房码（只撤 old_room_id 的 active/pending 客人码；组感知——
        #    共享码不管挂在组内哪段名下都要撤，否则旧房还能用旧码开门）
        now = self._clock()
        for code in await self._group_guest_codes(order, room_id=old_room_id):
            if code.vendor_key_id:
                await self._provider.revoke_code(code.vendor_key_id)
            code.status = DoorCodeStatus.revoked
            code.revoked_at = now
        await self._db.commit()

    async def extend_guest_codes(self, order, new_checkout_date) -> list[dict]:
        """把订单现有客人码的有效期延到 new_checkout_date 14:00（续住手动延期用）。
        **只动门锁密码、不改订单日期/金额**——续住来两个订单时，前台点一下沿用原密码，
        避免合并订单动到按夜价格/月底对账（王总 2026-07-08 决策）。前台主动确认是续住
        才点，系统不自动判定（规避重名/换客误配）。返回每间房结果供前台看成功/失败。"""
        new_end = _checkout_at(new_checkout_date)
        results: list[dict] = []
        codes = await self._active_guest_codes(order.order_id)
        for code in codes:
            vendor_room_id = await self._vendor_room_id(code.room_id)
            if vendor_room_id is None:
                results.append({"room_id": code.room_id, "ok": False,
                                "reason": "房间未绑定门锁设备"})
                continue
            ok = await self._provider.renew(vendor_room_id, code.phone_no, new_end)
            if ok:
                code.end_at = new_end
            results.append({"room_id": code.room_id, "ok": bool(ok),
                            "reason": None if ok else "厂商延期未确认（锁可能离线），可稍后重试"})
        await self._db.commit()
        return results

    async def renew_group_codes(self, stay_group_id: str, new_checkout_date,
                                *, commit: bool = True, only_stale: bool = False,
                                alive_only: bool = False) -> list[dict]:
        """把续住组内全部有效客人码的有效期对齐到 new_checkout_date 14:00。

        unlink 缩短组后收码用（组末退房日提前，共享码不缩会让码在客人走后仍能开门）。
        commit=False 供 unlink 等「调用方自己管事务」的流程：只 flush，end_at 随
        调用方 commit 落库——中途 commit 会把调用方未写完的审计拆成两半。
        only_stale=True：只推「end_at 还没覆盖到目标退房日」的码，已覆盖的跳过——
        延码自愈（extend_stale_group_codes）每 5 分钟扫，不能把健康码也重推一遍骚扰厂商。
        alive_only=True：跳过已取消段名下的码。取消段退房前撤码若因锁离线失败，码仍
        active；自动延码若把它也延到组末=让退订/走掉的客人还能开门。**只有自动自愈传
        True**；unlink 缩组保持默认 False（那条是人工缩短、语义不同，不改其行为）。
        返回每把码结果，口径同 extend_guest_codes。
        """
        from app.services import stay_group as _sg

        if await _sg.is_managed_split(self._db, stay_group_id):
            # Managed splits renew at the physical-leg entry points above.  A
            # single group-wide target is undefined for cross-room/A→B→A stays.
            return []

        orders = await _sg.get_group_orders(self._db, stay_group_id)
        if alive_only:
            orders = [o for o in orders if o.order_status != OrderStatus.cancelled]
        member_ids = [o.order_id for o in orders]
        if not member_ids:
            return []
        rows = await self._db.execute(
            select(DoorCode).where(
                DoorCode.order_id.in_(member_ids),
                DoorCode.purpose == DoorCodePurpose.guest,
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
            )
        )
        new_end = _checkout_at(new_checkout_date)
        results: list[dict] = []
        for code in rows.scalars().all():
            if only_stale and code_covers_checkout(code.end_at, new_checkout_date):
                continue  # 已覆盖到组末，无需重推
            vendor_room_id = await self._vendor_room_id(code.room_id)
            if vendor_room_id is None:
                results.append({"room_id": code.room_id, "ok": False,
                                "reason": "房间未绑定门锁设备"})
                continue
            ok = await self._provider.renew(vendor_room_id, code.phone_no, new_end)
            if ok:
                code.end_at = new_end
                code.last_error = None  # 坐实即清残留「未确认」，别留旧文案吓人/污染去重
            elif code.last_error != _STALE_ALERTED:
                # 保留「已告警」去重标记：仍离线的后续轮次不覆盖它、不再刷屏
                code.last_error = "组变更后门锁有效期调整未确认（锁可能离线）"
            results.append({"room_id": code.room_id, "ok": bool(ok),
                            "reason": None if ok else "厂商延期未确认（锁可能离线），可稍后重试"})
        if commit:
            await self._db.commit()
        else:
            await self._db.flush()
        return results

    async def extend_stale_group_codes(self) -> dict:
        """第二道网（Celery 每 5 分钟）：把在住续住组里 end_at 还没覆盖到组末退房日的
        活客人码，自动补延到组末 14:00。锁一恢复即坐实，前台永不用手动点「门锁密码延期」。

        补 hooks.py 承认的缺口：关联时锁离线→延码失败只发过一次告警，锁恢复后
        retry_pending 只捞 pending/failed，active 但 end_at 陈旧的码没有自动重试。
        「先各自入住发码、后关联」产出的多把码也一并对齐（renew_group_codes 覆盖全组）。

        离线安全：renew_group_codes 只在厂商确认后写 end_at，仍离线则留旧值等下一轮，
        绝不静默复活。only_stale=True 保证健康码不被每 5 分钟重推骚扰厂商；
        alive_only=True 保证取消段撤码失败留下的活码不被误延（不给退订客人续开门权）。
        组末退房日已过（客人早走）的组跳过——延旧码等于让走掉的客人还能开门。

        重试边界（有意不套 retry_pending 的 _RETRY_MAX_AGE 龄封顶）：锚单码在锚单入住时
        建、续住可能隔几天才关联，码本就"老"，按码龄封顶会把正常晚关联的续住误停掉。
        锁长期离线的重推由「组末退房日已过即跳过」自然收敛（到点退房就不再扫），且本路
        全程静默不发飞书（不同于 1401 事故的刷屏），代价只是离线期间每 5 分钟一次厂商调用。
        """
        from app.services import stay_group as _sg

        today = to_cn(self._clock()).date()
        rows = await self._db.execute(
            select(DoorCode.order_id, Order.stay_group_id)
            .join(Order, Order.order_id == DoorCode.order_id)
            .where(
                DoorCode.purpose == DoorCodePurpose.guest,
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
                Order.stay_group_id.isnot(None),
                Order.is_deleted.is_(False),
            )
        )
        gids = {gid for _oid, gid in rows.all() if gid}
        now = self._clock()
        groups = renewed = alerted = 0
        for gid in gids:
            if await _sg.is_managed_split(self._db, gid):
                continue  # managed legs are retried by extend_stale_single_codes
            final = await _sg.group_final_checkout_date(self._db, gid, alive_only=True)
            if final is None or final < today:
                continue  # 组已散/客人已离店：交撤码流程，不延旧码
            results = await self.renew_group_codes(
                gid, final, commit=False, only_stale=True, alive_only=True)
            if results:  # 有码真被处理（有陈旧码需要延）才算动过这个组
                groups += 1
                renewed += sum(1 for r in results if r.get("ok"))
            alerted += await self._alert_offline_stale_codes(gid, final, now)
        await self._db.commit()
        return {"groups": groups, "renewed": renewed, "alerted": alerted}

    async def extend_stale_single_codes(self) -> dict:
        """第二道网（单住版，Celery 每 5 分钟）：单住（非续住组）在住客人码 end_at 还没
        覆盖到本单退房日的，补延到本单退房日 14:00。

        补 D11 缺口：续住组有 extend_stale_group_codes 兜底，单住此前没有第二道网——
        改期延码时锁离线 renew 失败（on_reschedule 只告警一次、不写旧 end_at），锁恢复后
        无人重延，客人到旧退房日被锁门外。本网每 5 分钟补上。离线安全：renew 确认后才写
        end_at，仍离线留旧值等下一轮，绝不静默复活；退房日已过（客人早走）跳过。
        """
        today = to_cn(self._clock()).date()
        rows = await self._db.execute(
            select(DoorCode, Order)
            .join(Order, Order.order_id == DoorCode.order_id)
            .where(
                DoorCode.purpose == DoorCodePurpose.guest,
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
                Order.is_deleted.is_(False),
                Order.order_status.in_([OrderStatus.checked_in, OrderStatus.pending_checkout]),
            )
        )
        renewed = 0
        for code, order in rows.all():
            from app.services import stay_group as _sg
            if order.stay_group_id and not await _sg.is_managed_split(
                self._db, order.stay_group_id
            ):
                continue
            co = (
                await _sg.operational_group_final_checkout_date(
                    self._db, order, alive_only=True
                )
                if order.stay_group_id else order.check_out_date
            )
            if co is None or co < today:
                continue  # 客人早走：交撤码流程，不延旧码
            if code_covers_checkout(code.end_at, co):
                continue  # 已覆盖本单退房日，无需重推
            vendor_room_id = await self._vendor_room_id(code.room_id)
            if vendor_room_id is None:
                continue
            new_end = _checkout_at(co)
            if await self._provider.renew(vendor_room_id, code.phone_no, new_end):
                code.end_at = new_end
                code.last_error = None
                renewed += 1
        await self._db.commit()
        return {"renewed": renewed}

    async def _alert_offline_stale_codes(self, gid: str, final, now: datetime) -> int:
        """续住延码「宽限期」告警：组内某房锁「持续离线超 _STALE_ALERT_GRACE」仍没把
        码延到组末退房日 → 告警前台一次（去重）。锁在线时第二道网几分钟就坐实、不进这里，
        故躲过锁短暂抖动的虚惊（王总 2026-07-21）。

        判据用 last_online_at 的「离线时长」实证，不用码龄（锚单码本就老、会误报）。
        last_online_at 为空 = 从没上报过在线、拿不到离线时长实证 → 宁可不报（避免误报）。
        坐实/取消由 renew_group_codes 清 last_error、alive_only 过滤兜住，此处只管告警。
        """
        from app.services import stay_group as _sg
        from app.services.feishu_lead_alert import send_lock_alert

        member_ids = [o.order_id for o in await _sg.get_group_orders(self._db, gid)
                      if o.order_status != OrderStatus.cancelled]
        if not member_ids:
            return 0
        rows = await self._db.execute(
            select(DoorCode).where(
                DoorCode.order_id.in_(member_ids),
                DoorCode.purpose == DoorCodePurpose.guest,
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.active]),
            )
        )
        alerted = 0
        for code in rows.scalars().all():
            if code_covers_checkout(code.end_at, final):
                continue  # 已坐实到组末，无需告警
            if code.last_error == _STALE_ALERTED:
                continue  # 已就该码告警过，别刷屏
            dev = (await self._db.execute(
                select(LockDevice).where(LockDevice.room_id == code.room_id)
            )).scalar_one_or_none()
            last_online = dev.last_online_at if dev else None
            if last_online is None:
                continue  # 无离线时长实证，宁可不报
            if last_online.tzinfo is None:  # 历史裸值当 UTC
                last_online = last_online.replace(tzinfo=timezone.utc)
            if now - last_online <= _STALE_ALERT_GRACE:
                continue  # 锁最近还在线（未超宽限期）→ 交下一轮自动重试，先不打扰前台
            await send_lock_alert(
                f"⚠️ 门锁离线超 30 分钟，续住密码没能自动延期\n"
                f"房间：{code.room_id}\n"
                f"（组 {gid} 需延到 {final} 14:00，系统自动重试 30 分钟仍未坐实）\n"
                f"请前台检查门锁网络，或到慧享家 App 手动把该房密码延到 {final} 14:00"
            )
            code.last_error = _STALE_ALERTED
            alerted += 1
        return alerted

    async def alert_stuck_manual_codes(self) -> dict:
        """在住客人码「持续卡 manual、下发不成功」告警（Celery 每 5 分钟，挂在 retry_pending 后）。

        补 retry_pending 的告警盲区：持续离线 >_RETRY_MAX_AGE 的客人码被翻成 manual
        「交前台」后，若无人处理就静静挂到 end_at 过期、客人进不了门——而前台没有任何
        提示（recover 路径刻意静默；续住组的 _alert_offline_stale_codes 只覆盖 stay_group）。
        2026-07-21 生产 1412/1413/1414 史上 0 把码成功上锁正卡在此，多是非续住组的在住客人。

        判据（口径见 [[lock-alert-must-ask-can-guest-open-door-20260717]]「问客人进不进得了门」）：
        - 只报**在住**订单（checked_in/pending_checkout）的客人码——退房/取消/删除的不是
          「客人进不了门」，交撤码流程，绝不喊前台。
        - 只报**非续住组**（stay_group_id 为空）：续住组另有 _alert_offline_stale_codes，别重复。
        - 持续时长用 **code.created_at** 判（naive 裸值当 UTC，与 retry_pending 同源），超
          _STALE_ALERT_GRACE 才报。**有意不**用锁 last_online_at 门控：生产那几把恰恰是
          「锁其实在线、码却卡 manual」，用离线时长门控会永远漏报（正是被回退的 #330 要救的场景）。
          manual 本就 >_RETRY_MAX_AGE(2h) 才会产生，宽限期只是防「刚翻 manual 就喊」的抖动。

        去重：标记落 last_error=_MANUAL_ALERTED，下一轮仍卡也不刷屏；码被重推后再次翻 manual
        时 retry_pending 会用自己的文案覆盖本标记→视为新一次卡死、重报。
        清除：已打过标记但订单已不在住（退房/取消/删除）→ 清 last_error，好让同房日后再卡能重报。
        """
        from app.services.feishu_lead_alert import send_lock_alert

        rows = await self._db.execute(
            select(DoorCode).where(
                DoorCode.purpose == DoorCodePurpose.guest,
                DoorCode.status == DoorCodeStatus.manual,
            )
        )
        now = self._clock()
        alerted = 0
        for code in rows.scalars().all():
            order = await self._db.get(Order, code.order_id) if code.order_id else None
            in_house = (
                order is not None
                and not order.is_deleted
                and order.order_status in (OrderStatus.checked_in, OrderStatus.pending_checkout)
            )
            if not in_house:
                # 客人已不在（退房/取消/删除/无单）→ 不是「进不了门」，且清掉残留去重标记
                if code.last_error == _MANUAL_ALERTED:
                    code.last_error = None
                continue
            if order.stay_group_id is not None:
                from app.services import stay_group as _sg
                if not await _sg.is_managed_split(self._db, order.stay_group_id):
                    continue  # ordinary continuation groups have their group-level alert
            if code.last_error == _MANUAL_ALERTED:
                continue  # 已告警过，别刷屏
            created = code.created_at
            if created is None:
                continue  # 无建码时间实证，宁可不报
            if created.tzinfo is None:  # SQLite/历史裸值当 UTC（与 retry_pending 同源）
                created = created.replace(tzinfo=timezone.utc)
            if now - created <= _STALE_ALERT_GRACE:
                continue  # 刚转 manual、未超宽限期 → 先不打扰前台
            await send_lock_alert(
                f"⚠️ 在住客人门锁密码持续下发失败，客人可能进不了门\n"
                f"房间：{code.room_id}\n"
                f"订单：{code.order_id}（{order.guest_name}，住到 {order.check_out_date} 14:00）\n"
                f"（系统自动重推超时后已转人工，至今仍未设置成功）\n"
                f"请前台到慧享家 App 手动为该房设置客人密码，或检查门锁网络"
            )
            code.last_error = _MANUAL_ALERTED
            alerted += 1
        await self._db.commit()
        return {"alerted": alerted}

    async def _confirmed_on_cloud(self, vendor_room_id: str, code) -> bool:
        """查云端钥匙列表旁证「码已下发生效」：同手机号 + keyState==3 + 有效期覆盖本码退房日。

        云端视角（非物理锁真值），仅作重推前兜底确认，避免回调断时对已下好码的锁瞎重推。
        provider 无 list_keys 能力（如测试替身）或查询失败 → 返回 False，照常重推（宁可多推不漏）。
        """
        list_keys = getattr(self._provider, "list_keys", None)
        if list_keys is None:
            return False
        try:
            keys = await list_keys(vendor_room_id)
        except Exception:
            logger.exception("list_keys 查询失败 room=%s，跳过云端确认照常重推", code.room_id)
            return False
        for k in keys:
            if k.phone_no != code.phone_no or k.key_state != KEY_STATE_NORMAL:
                continue
            if code.end_at is not None:
                if k.end_at is None:
                    continue
                ke = k.end_at if k.end_at.tzinfo else k.end_at.replace(tzinfo=timezone.utc)
                ce = code.end_at if code.end_at.tzinfo else code.end_at.replace(tzinfo=timezone.utc)
                if ke < ce:
                    continue  # 云端钥匙有效期不覆盖本码退房日 → 可能过期/待续期，别当已确认
            return True
        return False

    async def retry_pending(self) -> int:
        """Celery 兜底：重推锁离线时未确认下发的客人码（§16.1）。

        解密后用同一密码重推（不重新生成，客人手里的码不变）；锁恢复→active，
        仍离线→留 pending 等下一轮。返回处理条数。
        """
        rows = await self._db.execute(
            select(DoorCode).where(
                DoorCode.purpose.in_([
                    DoorCodePurpose.guest, DoorCodePurpose.cleaning, DoorCodePurpose.keeper,
                ]),
                DoorCode.status.in_([DoorCodeStatus.pending, DoorCodeStatus.failed]),
            )
        )
        processed = 0
        # 首发 FAILED 的客人码当时没推过密码卡（notify 只认 active/pending），翻活后要补
        recovered_unnotified: list[tuple[str, str]] = []
        for code in rows.scalars().all():
            if not (code.room_id and code.phone_no and code.password):
                continue
            # 订单已删/已终态 → 绝不把码推到锁上，反而撤掉该 pending 码自愈。
            # （landmine：delete_order 曾漏撤码，若此处不设防，已删单的 pending 码会被
            # 重试复活到物理锁上——删了的单还能开门。见 test_retry_pending_*deleted/cancelled）
            # 终态判定按用途区分：客人码在订单取消/完成即作废；保洁码常在「收齐房费→
            # completed」之后才打扫，故 completed/cancelled 不撤保洁码，仅订单删除才撤。
            order = await self._db.get(Order, code.order_id)
            gone = order is None or order.is_deleted
            if not gone and code.purpose == DoorCodePurpose.guest:
                gone = order.order_status in (OrderStatus.cancelled, OrderStatus.completed)
            if gone:
                code.status = DoorCodeStatus.revoked
                code.last_error = "订单已删除/取消/完成，撤码不再重试"
                continue
            # 重推年龄封顶：锁持续离线，码建了超过 _RETRY_MAX_AGE 还没翻 active →
            # 停止无限重推，交前台人工（manual 态不再被本查询捞到），并告警。
            # 防「每 5 分钟把码推给离线锁、锁一联网就播报设置成功」扰民（1401 事故）。
            created = code.created_at
            if created is not None:
                if created.tzinfo is None:  # SQLite/历史裸值当 UTC
                    created = created.replace(tzinfo=timezone.utc)
                if created < self._clock() - _RETRY_MAX_AGE:
                    code.status = DoorCodeStatus.manual
                    code.last_error = "锁持续离线超时，停止自动重推，请前台检查网络或手动设置"
                    logger.warning(
                        "retry_pending 放弃重推：room=%s order=%s 建码已超 %s 仍未确认，转人工",
                        code.room_id, code.order_id, _RETRY_MAX_AGE,
                    )
                    continue
            # 退避闸：未到下次重推时刻就跳过——避免每 5 分钟骚扰「码已进锁但客户端超时」的
            # 在线锁反复播报「设置成功」（连环响事故 2026-07-21）。next_retry_at 由上一轮未
            # 确认时按 _backoff_delay 写入；从没退避过（None）的照常本轮重推。
            if code.next_retry_at is not None:
                nra = code.next_retry_at
                if nra.tzinfo is None:  # SQLite/历史裸值当 UTC
                    nra = nra.replace(tzinfo=timezone.utc)
                if nra > self._clock():
                    continue
            vendor_room_id = await self._vendor_room_id(code.room_id)
            if vendor_room_id is None:
                continue
            # 双保险（兜底 PULL）：重推前先查云端钥匙列表，同手机号已有 keyState=3 且有效期
            # 覆盖本码退房日 → 码其实已下发，直接确认、不再重推（省一次会让在线锁播报的下码）。
            # 回调 logType=8 是首选确认；此路防回调断时也能收敛。查询失败/无此能力→照常重推。
            if await self._confirmed_on_cloud(vendor_room_id, code):
                code.status = DoorCodeStatus.active
                code.retry_count = 0
                code.next_retry_at = None
                code.last_error = None
                processed += 1
                continue
            label = {
                DoorCodePurpose.cleaning: "保洁",
                DoorCodePurpose.keeper: "查房",
            }.get(code.purpose, "客人")
            was_failed = code.status == DoorCodeStatus.failed
            result = await self._provider.issue_code(
                vendor_room_id=vendor_room_id,
                phone=code.phone_no,
                start_at=code.start_at or self._clock(),
                end_at=code.end_at or self._clock(),
                key_name=f"{label}-retry-{code.room_id}",
                password=decrypt_code(code.password),  # provider 要明文
            )
            code.status = _OUTCOME_TO_STATUS[result.outcome]
            code.vendor_key_id = result.vendor_key_id or code.vendor_key_id
            code.vendor_lock_key_id = result.vendor_lock_key_id or code.vendor_lock_key_id
            code.vendor_key_group_id = result.vendor_key_group_id or code.vendor_key_group_id
            code.last_error = result.error
            # 退避记账：下成功即清零；仍未确认（pending/failed）→ 计数+1 并指数退避到下次。
            if code.status == DoorCodeStatus.active:
                code.retry_count = 0
                code.next_retry_at = None
            elif code.status in (DoorCodeStatus.pending, DoorCodeStatus.failed):
                code.retry_count = (code.retry_count or 0) + 1
                code.next_retry_at = self._clock() + _backoff_delay(code.retry_count)
            if (was_failed and code.purpose == DoorCodePurpose.guest
                    and code.status in (DoorCodeStatus.active, DoorCodeStatus.pending)):
                recovered_unnotified.append((code.order_id, code.room_id))
            processed += 1
        await self._db.commit()
        if recovered_unnotified:
            # 延迟导入：hooks 模块级依赖本模块，反向只能函数内引
            from app.services.lock.hooks import notify_recovered_guest_codes

            await notify_recovered_guest_codes(self._db, recovered_unnotified)
        return processed
