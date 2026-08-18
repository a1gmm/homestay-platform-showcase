"""智能门锁 Celery 任务（plan §16.1 / §16.6 Lane D）。

retry_pending 重推 door_codes 里 (pending, failed) 的客人码——办理入住时锁离线
未确认下发的那些。32 把锁单 Celery Beat 足够，无需分布式协调（§16.3）。
"""
import logging

from app.workers.celery_app import celery_app
from app.workers.async_helper import run_async
from app.core.database import AsyncSessionLocal
from app.services.lock.factory import get_lock_provider
from app.services.lock.orchestrator import OrderLockService

logger = logging.getLogger(__name__)


async def _retry_pending_async(db=None, provider=None) -> dict:
    provider = provider or get_lock_provider()

    async def _run(session) -> dict:
        svc = OrderLockService(provider=provider, db=session)
        # ① 先重推 pending/failed 码——可能刚把离线锁救回在线，紧接着 ② 就能延成。
        n = await svc.retry_pending()
        # ② 第二道网：在住续住组里 end_at 陈旧的活码补延到组末（关联时锁离线延失败、
        #    锁恢复后无人重试的缺口）。
        stale = await svc.extend_stale_group_codes()
        # ②b 单住版第二道网（D11）：单住陈旧码补延到本单退房日（改期延码离线失败后无人重延）。
        single = await svc.extend_stale_single_codes()
        # ③ 告警盲区：非续住组的在住客人码持续卡 manual、下发不成功→客人进不了门，
        #    retry_pending 静默转人工后无人管（recover 路径故意静默、续住第二道网只管组码）。
        manual_stuck = await svc.alert_stuck_manual_codes()
        return {"processed": n, "group_codes_extended": stale["renewed"],
                "single_codes_extended": single["renewed"],
                "stale_alerted": stale.get("alerted", 0),
                "manual_stuck_alerted": manual_stuck["alerted"]}

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as owned:
        summary = await _run(owned)
        if (summary["processed"] or summary["group_codes_extended"]
                or summary["single_codes_extended"]
                or summary["stale_alerted"] or summary["manual_stuck_alerted"]):
            logger.info(
                "retry_pending re-pushed %d door code(s); 续住组补延 %d 把码; 单住补延 %d 把码; "
                "离线超时告警 %d 条; 在住卡 manual 告警 %d 条",
                summary["processed"], summary["group_codes_extended"],
                summary["single_codes_extended"],
                summary["stale_alerted"], summary["manual_stuck_alerted"])
        return summary


@celery_app.task(name="app.workers.lock_tasks.retry_pending_door_codes")
def retry_pending_door_codes():
    """重推 pending/failed 客人码（锁曾离线）+ 续住组陈旧码补延到组末。"""
    return run_async(_retry_pending_async())


async def _reconcile_roomstate_async(db=None, provider=None, live=None) -> dict:
    """慧享家房态对账（spec 2026-07-14）。live=None → 读 gate HXJIOT_ROOMSTATE_SYNC_LIVE。"""
    from app.core.config import settings

    provider = provider or get_lock_provider()
    if live is None:
        live = settings.HXJIOT_ROOMSTATE_SYNC_LIVE
    if db is not None:
        return await OrderLockService(provider=provider, db=db).reconcile_room_states(live=live)
    async with AsyncSessionLocal() as owned:
        res = await OrderLockService(provider=provider, db=owned).reconcile_room_states(live=live)
        logger.info("reconcile roomState result: %s", res)
        return res


@celery_app.task(name="app.workers.lock_tasks.reconcile_hxjiot_roomstate")
def reconcile_hxjiot_roomstate():
    """定时对账慧享家房态(roomState)与系统真实占用，自愈假在住/漏在住。
    gate=HXJIOT_ROOMSTATE_SYNC_LIVE：关时只 dry-run（不发指令）。"""
    return run_async(_reconcile_roomstate_async())


async def _callback_heartbeat_async(db=None) -> bool:
    """回调心跳 + 自愈闭环：lock_events 超阈值无新行 → 当场尝试重登记回调地址、按周期告警。

    与老版差别（根治 7-21 静默 5 天没人管）：检测到静默不再只报一条就闭嘴，而是
    ① 每轮当场调 _reregister_webhook_async 尝试自愈（不等每天 4:20）；② 按
    LOCK_CALLBACK_REALERT_HOURS 周期重报，断着一直喊；③ 把自愈成败写进告警。
    """
    from datetime import datetime, timedelta, timezone
    from app.core.config import settings
    from app.services.feishu_lead_alert import send_lock_alert
    from app.services.lock.callback_health import alert_if_callback_silent

    async def _run(session) -> bool:
        return await alert_if_callback_silent(
            session,
            now=datetime.now(timezone.utc),
            threshold=timedelta(hours=settings.LOCK_CALLBACK_SILENCE_ALERT_HOURS),
            send=send_lock_alert,
            recover=_reregister_webhook_async,   # 检测到静默即当场自愈重登记（闭环）
            re_alert_interval=timedelta(hours=settings.LOCK_CALLBACK_REALERT_HOURS),
        )

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as owned:
        return await _run(owned)


@celery_app.task(name="app.workers.lock_tasks.lock_callback_heartbeat")
def lock_callback_heartbeat():
    """回调链路健康心跳（每小时）。lock_events 长时间无新行→回调八成断了，告警前台。"""
    return run_async(_callback_heartbeat_async())


async def _reregister_webhook_async(provider=None) -> bool:
    """每日幂等重登记回调地址，防 token/域名漂移致回调静默失效（7-12 根因）。

    厂商侧查不到我方登记的地址、地址错了也不报错——只能我方定期把「当前正确 URL」重推一遍。
    未接厂商(manual)或没配 token 时跳过（别登记一个必然 401 的地址）。
    """
    from app.core.config import settings

    if settings.LOCK_PROVIDER != "hxjiot" or not settings.LOCK_WEBHOOK_TOKEN:
        return False
    provider = provider or get_lock_provider()
    url = (
        f"{settings.BACKEND_PUBLIC_BASE.rstrip('/')}"
        f"/api/v1/lock-events/callback/{settings.LOCK_WEBHOOK_TOKEN}"
    )
    ok = await provider.set_webhook(url)
    logger.info("门锁回调地址每日重登记 ok=%s", ok)
    return ok


@celery_app.task(name="app.workers.lock_tasks.reregister_lock_webhook")
def reregister_lock_webhook():
    """每日幂等重登记回调地址（自愈 token/域名漂移致的回调静默失效，7-12 根因）。"""
    return run_async(_reregister_webhook_async())
