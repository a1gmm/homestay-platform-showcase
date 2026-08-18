"""业主托管合作留资（showcase.example.invalid 落地页）。

Public (no auth):
- POST /hosting-leads — 提交留资线索

防护设计（设计文档 + pre-landing review 修订）：
- 24h 同号 DB 去重是主防线（幂等返回，不重发飞书；放在 flood 护栏之前，
  保证流控期间幂等语义不变）。
- 限流 key 用 phone 维度：Vercel proxy 链路下 request.client.host 是代理
  出口 IP，不可用；且生产 Redis 长期不可用，enforce_rate_limit 自带降级放行。
- 全局 flood 护栏只「静音通知」不丢数据：营销群发等真实高峰场景（王总把
  落地页发进业主群）线索照常落库，仅跳过逐条飞书通知，并在首次越界时发
  一条运维告警保证可观测——绝不静默丢真实业主线索。
"""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, Request
from sqlalchemy import func, select

from app.core.deps import DBSession, RedisClient
from app.core.rate_limit import enforce_rate_limit
from app.models.hosting_lead import HostingLead
from app.schemas.hosting_lead import HostingLeadCreate, HostingLeadOut
from app.services.feishu_lead_alert import send_lead_alert, send_ops_alert

router = APIRouter(prefix="/hosting-leads", tags=["hosting-leads"])

_DEDUP_WINDOW_HOURS = 24  # 同号去重窗口
_FLOOD_WINDOW_HOURS = 1  # flood 统计窗口
_FLOOD_LIMIT = 100  # 窗口内全局新增达到该值 → 静音逐条通知（数据仍落库）
_FLOOD_ALERT_WINDOW = 5  # 越界后前 N 条仍发运维告警（防并发下 == 精确匹配漏告警）
_MAX_UA_LEN = 500  # UA 截断：防异常长 header 撑爆日志，非 DB 约束


def _new_lead_id() -> str:
    return "HL" + uuid.uuid4().hex[:12].upper()


@router.post("", response_model=HostingLeadOut)
async def create_hosting_lead(
    payload: HostingLeadCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: DBSession,
    redis: RedisClient,
):
    await enforce_rate_limit(
        redis, f"hosting_lead:{payload.phone}", limit=5, window_seconds=3600
    )

    # 24h 同号去重：判断是否「重复」。命中时仍落库（不静默丢信息——真业主
    # 修改/重填、或填错纠正都不能丢；且只按 phone 去重，冒名抢注不应让真业主
    # 数据消失），只是不重发逐条飞书通知。
    cutoff = datetime.now(timezone.utc) - timedelta(hours=_DEDUP_WINDOW_HOURS)
    is_duplicate = (
        await db.execute(
            select(func.count())
            .select_from(HostingLead)
            .where(HostingLead.phone == payload.phone, HostingLead.created_at >= cutoff)
        )
    ).scalar_one() > 0

    # 全局 flood 护栏：轮换号码可绕过 phone 维度限流/去重，且生产 Redis 长期
    # 不可用。超阈值只静音逐条通知，线索仍落库（见模块 docstring）。
    flood_cutoff = datetime.now(timezone.utc) - timedelta(hours=_FLOOD_WINDOW_HOURS)
    recent_count = (
        await db.execute(
            select(func.count())
            .select_from(HostingLead)
            .where(HostingLead.created_at >= flood_cutoff)
        )
    ).scalar_one()

    lead = HostingLead(
        lead_id=_new_lead_id(),
        name=payload.name,  # schema validator 已 strip 并拒绝纯空白
        phone=payload.phone,
        property_location=payload.property_location,
        source_ua=(request.headers.get("user-agent") or "")[:_MAX_UA_LEN],
    )
    db.add(lead)
    await db.commit()

    if is_duplicate:
        # 重复提交：已落库，最小暴露（不回显 lead_id），不重发飞书。
        return HostingLeadOut(lead_id="", status="already_registered")

    if recent_count < _FLOOD_LIMIT:
        background_tasks.add_task(
            send_lead_alert, lead.name, lead.phone, lead.property_location
        )
    elif recent_count < _FLOOD_LIMIT + _FLOOD_ALERT_WINDOW:
        # 进入静音态后的前几条各发一次运维告警。用区间而非 == 精确匹配：
        # 并发下可能没有任何请求恰好读到 count==limit，== 会让告警永不触发。
        # 区间会重复告警最多 _FLOOD_ALERT_WINDOW 次，可接受（飞书不会被刷爆）。
        background_tasks.add_task(
            send_ops_alert,
            f"⚠️ 托管留资 flood 护栏触发：过去 {_FLOOD_WINDOW_HOURS} 小时新增 "
            f"{recent_count} 条线索，已暂停逐条通知（数据仍正常落库）。"
            "请核查是营销高峰还是刷量。",
        )
    return HostingLeadOut(lead_id=lead.lead_id, status="ok")
