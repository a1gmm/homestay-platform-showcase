"""房态对账 "stuck" 告警去重闸。

reconcile_hxjiot_roomstate 每 15 分钟一轮，对「系统显示在住、慧享家 roomState 却
不是在住(2)」的房只能告警、一期不自动拉回（spec 2026-07-14 §七）。没有去重时，
同一批房每轮都原样再发一条飞书 —— 只要没人处理就每 15 分钟刷屏一次。

本闸把「已告警过的房号集合（基线）」记到 notification_logs（复用 daily_guard 手法，
无需新迁移）。**仅当本批里出现「基线里没有的新房」时才再次告警**——只是某间老房
恢复又复现、或名单里其他房进进出出，都不再刷屏（2026-07-19：常年卡住的房里夹着
一两间来回抖动，旧的"集合一变就重发"逻辑被打穿，每 15 分钟刷屏）。基线只增不减，
房号集合清空（全部恢复一致）时才删标记，使同批房日后复现能重新告警。celery beat
单实例，check-then-write 无竞态。

调用方须「先判定、发送成功后再记标记」：send 失败则不 record，下一轮会重试告警，
不会因标记已写而静默丢失一条真实告警。
"""
import logging
import uuid
from collections.abc import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationLog

logger = logging.getLogger(__name__)

_TEMPLATE = "roomstate-stuck-guard"
# 在住房门锁离线告警共用同一套「集合变化才再报」机制，只是各用各的标记行。
OFFLINE_TEMPLATE = "lock-offline-guard"


def _fingerprint(stuck: Iterable[str]) -> str:
    return "、".join(sorted(stuck))


async def _baseline(db: AsyncSession, template: str) -> set[str]:
    """已告警过的房号基线（最近一行标记）。无标记 → 空集。"""
    last = await db.scalar(
        select(NotificationLog.content)
        .where(NotificationLog.template_name == template)
        .order_by(NotificationLog.created_at.desc())
        .limit(1)
    )
    if not last:
        return set()
    return set(last.split("、"))


async def set_needs_alert(
    db: AsyncSession, items: Iterable[str], template: str = _TEMPLATE
) -> bool:
    """这批房号里是否有「基线中没有的新房」——有新房才需要告警。

    只读，不改标记。items 为空时返回 False。老房恢复又复现、或名单里其他房进出，
    只要没冒出没报过的新房，就不再刷屏。
    """
    current = set(items)
    if not current:
        return False
    baseline = await _baseline(db, template)
    fresh = current - baseline
    if not fresh:
        logger.info("%s 告警去重：无新增房（本批「%s」均已报过），跳过",
                    template, _fingerprint(current))
        return False
    return True


async def record_set_alert(
    db: AsyncSession, items: Iterable[str], template: str = _TEMPLATE
) -> None:
    """把本批并入「已告警」基线（只增不减）。

    始终保持至多一行标记：先删旧、非空再插新。items 为空 → 只删（全部恢复一致时清
    基线，使同批房日后复现可重新告警）。基线不因某间房本轮恢复而收缩——否则该房再复
    现会被当成"新房"重新告警，又刷屏。
    """
    current = set(items)
    if current:
        current |= await _baseline(db, template)  # 并入旧基线，只增不减
    await db.execute(delete(NotificationLog).where(NotificationLog.template_name == template))
    fp = _fingerprint(current)
    if fp:
        db.add(
            NotificationLog(
                log_id="SG-" + uuid.uuid4().hex[:12].upper(),
                template_name=template,
                content=fp,
                status="marker",
            )
        )
    await db.commit()


async def stuck_needs_alert(db: AsyncSession, stuck: Iterable[str]) -> bool:
    return await set_needs_alert(db, stuck, _TEMPLATE)


async def record_stuck_alert(db: AsyncSession, stuck: Iterable[str]) -> None:
    await record_set_alert(db, stuck, _TEMPLATE)
