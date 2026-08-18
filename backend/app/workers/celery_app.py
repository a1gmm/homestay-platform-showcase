from celery import Celery
from celery.schedules import crontab
from app.core.config import settings

celery_app = Celery(
    "homestay",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=[
        "app.workers.reminder_tasks",
        "app.workers.settlement_tasks",
        "app.workers.order_maintenance",
        "app.workers.housekeeping_tasks",
        "app.workers.lock_tasks",
        "app.workers.sync_watchdog",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="Asia/Shanghai",
    enable_utc=False,  # Interpret crontab in Asia/Shanghai timezone
    task_soft_time_limit=300,  # 5 min soft limit per task
    task_time_limit=600,  # 10 min hard limit
    beat_schedule={
        "daily-overdue-check": {
            "task": "app.workers.reminder_tasks.check_overdue_tasks",
            "schedule": crontab(hour=8, minute=0),  # 每天 8AM Shanghai 一次（闲置房定不出去会反复报，降频）
        },
        "daily-checkin-reminder": {
            "task": "app.workers.reminder_tasks.send_checkin_reminder",
            "schedule": crontab(hour=8, minute=30),  # 8:30 AM Shanghai
        },
        # 每天 11:30 Shanghai 推「哪些房还没退房」到退房提醒群（王总 2026-07-08）。
        # 退房时间通常 12:00，11:30 提前半小时让前台催客人。daily_guard 防 beat 重启刷屏。
        "daily-checkout-reminder": {
            "task": "app.workers.reminder_tasks.send_checkout_reminder",
            "schedule": crontab(hour=11, minute=30),  # 11:30 AM Shanghai
        },
        "daily-pricing-job": {
            "task": "app.workers.reminder_tasks.run_daily_pricing",
            "schedule": crontab(hour=6, minute=0),  # 6:00 AM Shanghai
        },
        # 每天 8:00 Shanghai 发「今日续住房打扫申请」卡到打扫申请群，保洁自助拿门锁码
        # （PRD 打扫申请群-飞书自助）。daily_guard 防 beat 重启刷屏。
        "daily-cleaning-request-card": {
            "task": "app.workers.reminder_tasks.send_cleaning_request_card",
            "schedule": crontab(hour=8, minute=0),
        },
        # 每月 1 号凌晨 3 点自动生成上月业主结算
        "monthly-owner-settlements": {
            "task": "app.workers.settlement_tasks.generate_monthly_settlements",
            "schedule": crontab(day_of_month=1, hour=3, minute=0),
        },
        # 2026-06-17 重新启用：判定条件已重写以适配 2026-06-05「确认收款挪到退房后」改版。
        # 旧逻辑「pending_confirm + 未付款 > 24h = 弃单」会误删真实未来订单（实测 29 个）；
        # 新逻辑只取消「整段入住窗口已过期 + 从未排房 + 从未付款 + normal」的真正弃单。
        # 判定细节见 order_maintenance._cancel_stale_pending_orders_async 文档串 + 单测
        # tests/test_cancel_stale_pending.py。详见 memory: celery-not-deployed-20260617。
        "cancel-stale-pending-orders": {
            "task": "app.workers.order_maintenance.cancel_stale_pending_orders",
            "schedule": crontab(minute="*/10"),
        },
        # 每天 9:00 Shanghai 扫描空房闲置 >= IDLE_MAINTENANCE_DAYS 天的房间，自动派发管家维护提醒任务
        "daily-idle-room-detection": {
            "task": "app.workers.housekeeping_tasks.detect_idle_rooms",
            "schedule": crontab(hour=9, minute=0),
        },
        # 每天 15:00 Shanghai 自动纠错：把卡在「待清扫/清扫中」超 STALE_PENDING_CLEAN_HOURS
        # 的残留房清回 available（正常保洁当天已点完成）。放午后避开上午退房+保洁高峰，
        # 不误清当天真在扫的房。受 STALE_PENDING_CLEAN_RECON_ENABLED 控制。
        "daily-clear-stale-pending-clean": {
            "task": "app.workers.housekeeping_tasks.reconcile_stale_pending_clean",
            "schedule": crontab(hour=15, minute=0),
        },
        # 每 5 分钟重推锁离线时未确认下发的客人码（plan §16.1 同步首试失败的兜底）
        "retry-pending-door-codes": {
            "task": "app.workers.lock_tasks.retry_pending_door_codes",
            "schedule": crontab(minute="*/5"),
        },
        # 每 15 分钟对账慧享家房态(roomState)与系统真实占用，自愈假在住/漏在住（spec 2026-07-14）。
        # 真正 checkOut/告警受 gate HXJIOT_ROOMSTATE_SYNC_LIVE 控制；关时只 dry-run 记日志。
        # 每 15 分钟自愈续住关联组：摘掉组内已取消/删除成员（bypms 退订兜底，eng-review #3）。
        # gate STAY_GROUP_SELFHEAL_LIVE=0 可关；unlink 幂等低风险。
        "selfheal-stay-groups": {
            "task": "app.workers.housekeeping_tasks.selfheal_stay_groups",
            "schedule": crontab(minute="*/15"),
        },
        "reconcile-hxjiot-roomstate": {
            "task": "app.workers.lock_tasks.reconcile_hxjiot_roomstate",
            "schedule": crontab(minute="*/15"),
        },
        # 回调心跳告警已停（2026-07-28 业务决策）：32 间民宿不需要门锁「开门审计/流水」，
        # 日常运转（发码/开门/电量/在线）不依赖回调——电量/在线由每 15 分钟 roomState 轮询兜底，
        # 下码由每 5 分钟 retry-pending 兜底。回调断的唯一代价是缺开门记录，王总不看，故不再告警扰民。
        # 下方 reregister-lock-webhook（每天 4:20 静默重登记）保留：厂商侧若恢复，记录自动回来、零打扰。
        # 想恢复告警：取消本段注释即可。
        # "lock-callback-heartbeat": {
        #     "task": "app.workers.lock_tasks.lock_callback_heartbeat",
        #     "schedule": crontab(minute=5),
        # },
        # 每 10 分钟查宝禹拉取水位：staging 超 30 分钟无新拉取 → 同步八成停了（401/崩溃/
        # 进程没跑都逮得住——检测活在 ota-sync 之外），发同步告警群并按周期重报（7-30 停摆
        # 19h 事故，详见 services/bypms_pull_health.py）。
        "bypms-pull-watchdog": {
            "task": "app.workers.sync_watchdog.bypms_pull_watchdog",
            "schedule": crontab(minute="*/10"),
        },
        # 每天 4:20 Shanghai 幂等重登记回调地址：自愈 token/域名漂移致的回调静默失效（7-12 根因）。
        "reregister-lock-webhook": {
            "task": "app.workers.lock_tasks.reregister_lock_webhook",
            "schedule": crontab(hour=4, minute=20),
        },
        # 每天 3:30 Shanghai 把单房单的每房价拉回订单级真值（对账裸 SQL 改 orders.actual_price
        # 没同步 order_rooms 的兜底自愈）。放凌晨避开白天改单；幂等只碰单房单。
        # gate SINGLE_ROOM_PRICE_SELFHEAL_LIVE=0 可关。
        "selfheal-single-room-prices": {
            "task": "app.workers.housekeeping_tasks.selfheal_single_room_prices",
            "schedule": crontab(hour=3, minute=30),
        },
    },
)
