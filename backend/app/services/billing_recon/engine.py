# backend/app/services/billing_recon/engine.py
"""对账引擎：三级匹配（精确/前缀/客人名兜底）+ 差异分类。全部确定性代码，零 AI。

口径（2026-08-01 人肉对账验证 + 终稿 R1/R3/R6）：
- 候选 = 携程系渠道 × {completed, checked_in, pending_checkout} × 未删 ×
  离店日 ∈ [min(账单月首-7天, date_lo), max(账单月末+7天, date_hi)]
  （checked_in 必须含：月初刚离店的单还没流转成 completed；pending_checkout 必须含：
  已退房未结算的单；date_lo/date_hi 由调用方 Task 6 的 run_recon 从账单行离店日极值
  算出传入——账单里跨月补结行也要能命中候选）
- 同一 platform_order_id 挂多张订单（脏数据）→ 该账单单号判 manual_review
  (detail reason="duplicate_pid")，绝不自动挑一张匹配
- 同名多候选一律 manual_review（detail reason="same_name_multiple"），绝不自动匹配
  （同名夫妻各订一单有先例）
- 系统到手真相源 = Order.expected_revenue（metadata.ota_owner_revenue）
- 匹配上的订单若账单行 row_types 含 compensation → detail 打
  has_compensation=True + compensation_amount=<赔款行金额合计>（终稿 R3，供前端/Task 9 用）
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_helpers import today_cn
from app.models.expense import Expense, ExpenseCategory
from app.models.order import Channel, Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.recon import ReconBatch, ReconDiff, ReconDiffClass, ReconDiffStatus
from app.models.settlement import OwnerSettlement, SettlementStatus
from app.services.audit import log_action_tx
from app.services.billing_recon.parser import (
    BillMapping, BillOrder, aggregate_orders, extract_bill_rows, infer_bill_month,
    load_workbook_rows, validate_bill, _D01, _month_window,
)

logger = logging.getLogger(__name__)

CTRIP_FAMILY = (Channel.ctrip, Channel.qunar, Channel.tongcheng, Channel.zhixing)
_ACTIVE = (OrderStatus.completed, OrderStatus.checked_in, OrderStatus.pending_checkout)
# 前缀匹配的最短公共长度：携程单号 14~16 位，短于这个长度的"前缀命中"基本是巧合
# （8 位单号撞上另一张单的前 8 位就把钱记到别人头上），一律不认，落到客人名兜底。
_MIN_PREFIX_LEN = 12


@dataclass
class MatchResult:
    order: Order | None
    via: str  # exact | prefix | name | ambiguous | duplicate_pid | none
    # ambiguous/duplicate_pid 的候选集合（供 classify 把这些系统单标记为"已在本轮账单单号
    # 里处理过"，避免它们又被系统侧扫描当成"账单没有对应行"误报成 appeal）。
    touched: list[Order] = field(default_factory=list)


@dataclass
class DiffDraft:
    diff_class: ReconDiffClass
    order_id: str | None
    platform_order_id: str | None
    guest_name: str | None
    bill_amount: Decimal | None
    system_amount: Decimal | None
    detail: dict


async def fetch_candidates(
    db: AsyncSession,
    bill_month: str,
    date_lo: date | None = None,
    date_hi: date | None = None,
) -> list[Order]:
    """候选窗口 = [min(月首-7天, date_lo), max(月末+7天, date_hi)]。

    date_lo/date_hi 由调用方（Task 6 的 run_recon）从账单行离店日极值算出传入——
    账单里跨月补结行的离店日可能落在默认 ±7 天窗口外，也要能命中候选，故只做单向扩展
    （传入值比默认窗口更宽才生效，更窄不收缩）。
    """
    lo, hi = _month_window(bill_month)
    if date_lo is not None and date_lo < lo:
        lo = date_lo
    if date_hi is not None and date_hi > hi:
        hi = date_hi
    rows = await db.execute(
        select(Order).where(
            Order.channel.in_(CTRIP_FAMILY),
            Order.order_status.in_(_ACTIVE),
            Order.is_deleted == False,  # noqa: E712
            Order.check_out_date >= lo,
            Order.check_out_date <= hi,
        )
    )
    return list(rows.scalars().all())


def match_orders(bill_orders: dict[str, BillOrder], candidates: list[Order]) -> dict[str, MatchResult]:
    by_pid: dict[str, list[Order]] = {}
    for o in candidates:
        if o.platform_order_id:
            by_pid.setdefault(o.platform_order_id, []).append(o)

    used: set[str] = set()
    out: dict[str, MatchResult] = {}

    for no in bill_orders:  # 1) 精确
        hits = by_pid.get(no)
        if not hits:
            continue
        if len(hits) > 1:  # 同一 pid 挂多张订单：脏数据，绝不自动挑一张（终稿 R1）
            out[no] = MatchResult(None, "duplicate_pid", touched=list(hits))
            continue
        o = hits[0]
        if o.order_id not in used:
            out[no] = MatchResult(o, "exact")
            used.add(o.order_id)

    for no in bill_orders:  # 2) 前缀（互为前缀且候选唯一）
        if no in out:
            continue
        hits = [
            o for pid, lst in by_pid.items() for o in lst
            if o.order_id not in used
            and min(len(no), len(pid)) >= _MIN_PREFIX_LEN
            and (no.startswith(pid) or pid.startswith(no))
        ]
        if len(hits) == 1:
            out[no] = MatchResult(hits[0], "prefix")
            used.add(hits[0].order_id)

    for no, bo in bill_orders.items():  # 3) 客人名兜底（唯一才算；多候选 = ambiguous）
        if no in out:
            continue
        hits = [o for o in candidates if o.order_id not in used and bo.guest and o.guest_name == bo.guest]
        if len(hits) == 1:
            out[no] = MatchResult(hits[0], "name")
            used.add(hits[0].order_id)
        elif len(hits) > 1:
            out[no] = MatchResult(None, "ambiguous", touched=list(hits))
        else:
            out[no] = MatchResult(None, "none")
    return out


def classify(
    bill_orders: dict[str, BillOrder],
    matches: dict[str, MatchResult],
    candidates: list[Order],
    bill_month: str,
) -> list[DiffDraft]:
    drafts: list[DiffDraft] = []
    matched_order_ids: set[str] = set()

    for no, bo in bill_orders.items():
        m = matches[no]
        if m.order is not None:
            matched_order_ids.add(m.order.order_id)
            rev = m.order.expected_revenue
            base = dict(order_id=m.order.order_id, platform_order_id=no, guest_name=bo.guest,
                        bill_amount=bo.net, system_amount=rev)
            comp = (
                {"has_compensation": True, "compensation_amount": str(bo.compensation_amount)}
                if "compensation" in bo.row_types else {}
            )
            if rev is None:
                drafts.append(DiffDraft(ReconDiffClass.manual_review,
                                        detail={"via": m.via, "reason": "no_ota_owner_revenue", **comp}, **base))
            elif abs(rev - bo.net) <= _D01:
                if m.via == "name":  # 断链：钱对上了，人工确认一下链接断了即可
                    drafts.append(DiffDraft(ReconDiffClass.broken_link, detail={"via": m.via, **comp}, **base))
                # exact/prefix 且金额一致 → 无差异
            else:
                drafts.append(DiffDraft(ReconDiffClass.fix_amount,
                                        detail={"via": m.via, "row_types": sorted(bo.row_types), **comp}, **base))
        elif m.via in ("ambiguous", "duplicate_pid"):
            reason = "same_name_multiple" if m.via == "ambiguous" else "duplicate_pid"
            for c in m.touched:  # 这些系统单已经在本条账单行里核过了，别再让它们在系统侧扫描里误报 appeal
                matched_order_ids.add(c.order_id)
            drafts.append(DiffDraft(ReconDiffClass.manual_review, order_id=None, platform_order_id=no,
                                    guest_name=bo.guest, bill_amount=bo.net, system_amount=None,
                                    detail={"via": m.via, "reason": reason}))
        elif bo.net < 0:
            drafts.append(DiffDraft(ReconDiffClass.compensation, order_id=None, platform_order_id=no,
                                    guest_name=bo.guest, bill_amount=bo.net, system_amount=None,
                                    detail={"row_types": sorted(bo.row_types)}))
        elif abs(bo.net) > _D01:
            drafts.append(DiffDraft(ReconDiffClass.manual_review, order_id=None, platform_order_id=no,
                                    guest_name=bo.guest, bill_amount=bo.net, system_amount=None,
                                    detail={"via": "none", "reason": "bill_only"}))
        # net == 0 且没匹配上：0元行，忽略

    # 系统侧：账单月内离店、有到手价、没被任何账单单覆盖 → 需申诉
    for o in candidates:
        if o.order_id in matched_order_ids:
            continue
        rev = o.expected_revenue
        if rev is None or o.check_out_date.strftime("%Y-%m") != bill_month:
            continue
        drafts.append(DiffDraft(ReconDiffClass.appeal, order_id=o.order_id,
                                platform_order_id=o.platform_order_id, guest_name=o.guest_name,
                                bill_amount=None, system_amount=rev,
                                detail={"checkout": o.check_out_date.isoformat()}))
    return drafts


def build_summary(drafts: list[DiffDraft]) -> dict:
    """总览硬数字（确定性，供前端总览卡 + 喂给 diagnose 当 aggregates）。
    appeal_total = 需申诉行的账单/系统金额合计 = 疑似平台漏付总额。"""
    counts: dict[str, int] = {}
    appeal_total = Decimal("0")
    for d in drafts:
        key = d.diff_class.value
        counts[key] = counts.get(key, 0) + 1
        if d.diff_class == ReconDiffClass.appeal and d.system_amount is not None:
            appeal_total += d.system_amount
    return {
        "fix_amount": counts.get("fix_amount", 0),
        "appeal": counts.get("appeal", 0),
        "broken_link": counts.get("broken_link", 0),
        "compensation": counts.get("compensation", 0),
        "manual_review": counts.get("manual_review", 0),
        "appeal_total": str(appeal_total.quantize(_D01)),
    }


async def _enrich_batch_with_ai(
    db: AsyncSession,
    *,
    batch: ReconBatch,
    drafts: list[DiffDraft],
    candidates: list[Order],
    summary: dict,
) -> None:
    """Run optional AI after deterministic data is durably committed."""
    from app.services.billing_recon import ai_claim

    unmatched = [
        {"no": draft.platform_order_id, "guest": draft.guest_name,
         "checkin": None, "checkout": None,
         "amount": str(draft.bill_amount) if draft.bill_amount is not None else None}
        for draft in drafts
        if draft.diff_class == ReconDiffClass.manual_review and draft.platform_order_id
    ]
    pool = [
        {"order_id": order.order_id, "guest": order.guest_name,
         "checkin": order.check_in_date.isoformat() if order.check_in_date else None,
         "checkout": order.check_out_date.isoformat() if order.check_out_date else None,
         "system_amount": str(order.expected_revenue) if order.expected_revenue is not None else None,
         "room_type": None, "pid": order.platform_order_id}
        for order in candidates
    ]
    fix_rows = [{"reason_bucket": "amount_mismatch"}
                for draft in drafts if draft.diff_class == ReconDiffClass.fix_amount]
    claim_result, diagnosis_result = await asyncio.gather(
        ai_claim.propose_claims(unmatched, pool),
        ai_claim.diagnose(summary, fix_rows),
        return_exceptions=True,
    )
    for result in (claim_result, diagnosis_result):
        if isinstance(result, asyncio.CancelledError):
            raise result

    latest = (await db.execute(
        select(ReconBatch).where(
            ReconBatch.platform == batch.platform,
            ReconBatch.bill_month == batch.bill_month,
            ReconBatch.status == "parsed",
        ).order_by(ReconBatch.created_at.desc(), ReconBatch.batch_id.desc()).limit(1)
    )).scalar_one_or_none()
    if latest is None or latest.batch_id != batch.batch_id:
        return

    errors: list[str] = []
    claims: dict = {}
    diagnosis: dict = {"theme_codes": [], "per_row": {}}
    if isinstance(claim_result, BaseException):
        errors.append("claim")
    else:
        claims = claim_result
    if isinstance(diagnosis_result, BaseException):
        errors.append("diagnosis")
    else:
        diagnosis = diagnosis_result.model_dump(mode="json")

    diffs = (await db.execute(select(ReconDiff).where(ReconDiff.batch_id == batch.batch_id))).scalars().all()
    for diff in diffs:
        suggestions = claims.get(diff.platform_order_id, [])
        if suggestions:
            diff.detail = {
                **(diff.detail or {}),
                "ai_candidates": [candidate.model_dump(mode="json") for candidate in suggestions],
            }
    batch.mapping = {
        **(batch.mapping or {}),
        "ai_status": "failed" if errors else "ready",
        "ai_errors": errors,
        "ai_diagnosis": diagnosis,
        "ai_claim_failed": bool(errors),
    }
    await db.commit()


# ─────────────────────────────────────────────────────────────────────────
# run_recon：批次落库入口（终稿 R1/R2 权威口径）
#
# 7 步序（R2）：
#  1. load_workbook_rows → sheets/datemode；mapping 缺省走 ai_column_mapping
#  2. 平台门闸：仅收携程系，AI 认成别的平台整批拒收
#  3. extract_bill_rows + validate_bill；errors 非空 → 拒收落库（batch.error + stats）
#  4. 重传作废：同 (platform, bill_month) 旧批次 pending → dismissed，
#     detail["voided_by_reupload"]=True（appeal_* 等其它状态不受影响）
#  5. 跨月申诉核销：历史 appeal_pending 的单号出现在本期账单 → appeal_settled，
#     detail 记 settled_amount/system_amount/short_paid；settled_nos 本批全类目跳过建 draft
#  6. 建 draft 前三重去重：settled_nos 跳过 / 终态指纹去重（人工处置的不复活，
#     voided_by_reupload 的可以）/ 申诉去重（跨批次跨月，同单号已有 appeal_pending
#     或 appeal_settled 不再新建）
#  7. 结构化日志：入口 / 拒收 / 成功
#
# R1 附则：「已一致」是状态不是分类——重放时若某单历史有 adopted 的 fix_amount/
# compensation 指纹、而这次账单/系统金额已经一致（classify() 因为没有差异不会
# 出 draft），仍然显式落一条 already_consistent 状态的 diff，保留"我们检查过、
# 现在一致了"的审计痕迹，分类沿用历史那条的 diff_class。
# ─────────────────────────────────────────────────────────────────────────

_TERMINAL_STATUSES = {
    ReconDiffStatus.acknowledged,
    ReconDiffStatus.adopted,
    ReconDiffStatus.already_consistent,
    ReconDiffStatus.dismissed,
}


class BillRejected(Exception):
    """校验闸拒收。batch 已以 rejected 状态落库（留痕），errors 给前端展示。"""

    def __init__(self, batch: ReconBatch, errors: list[str]):
        self.batch = batch
        self.errors = errors
        super().__init__("; ".join(errors))


def _new_id(prefix: str) -> str:
    # 12 位 hex：8 位在"每月一批 × 几百条 diff"的量级上撞号概率还不够低，主键撞了整批 500。
    return f"{prefix}-{uuid4().hex[:12].upper()}"


def _fp_pid_key(pid: str | None, order_id: str | None) -> str | None:
    """终态指纹的首元素：pid 缺失（断链单）时退化用 order_id 兜底，避免多个
    null-pid 订单共享同一个 (None, ...) 指纹互相压制彼此的申诉（Fix 2）。
    两者都空时返回 None——调用方须跳过该行的指纹去重，绝不误伤压制。"""
    return pid or (f"oid:{order_id}" if order_id else None)


async def _reject(
    db: AsyncSession, platform: str, user_id: str | None, mapping: BillMapping | None,
    errors: list[str], stats: dict | None = None, batch: ReconBatch | None = None,
) -> ReconBatch:
    mapping_dump = mapping.model_dump() if mapping else {}
    if stats:
        mapping_dump = {**mapping_dump, "stats": stats}
    if batch is None:
        batch = ReconBatch(
            batch_id=_new_id("RB-REJ"), platform=platform, bill_month="0000-00",
            summary_total=Decimal("0"), row_count=0, status="rejected",
            error="; ".join(errors), mapping=mapping_dump, created_by=user_id,
        )
        db.add(batch)
    else:
        batch.status = "rejected"
        batch.error = "; ".join(errors)
        batch.mapping = {**(batch.mapping or {}), **mapping_dump}
    await db.commit()
    logger.info("billing_recon 拒收 platform=%s errors=%s", platform, errors)
    return batch


async def run_recon(
    db: AsyncSession,
    *,
    data: bytes,
    filename: str,
    platform: str = "ctrip",
    user_id: str | None,
    mapping: BillMapping | None = None,
    upload_fingerprint: str | None = None,
    processing_batch: ReconBatch | None = None,
) -> ReconBatch:
    started = time.monotonic()
    logger.info("billing_recon 入口 filename=%s size=%d", filename, len(data))

    # 1. 解析工作簿；mapping 缺省走 AI 认列（engine 内 lazy import，测试可 monkeypatch
    #    app.services.billing_recon.ai_mapping.ai_column_mapping，绝不 patch engine 属性）
    #    load_workbook_rows 是纯 CPU 的同步解析（几万格的表能跑几秒），丢线程池跑，
    #    别把整个 event loop（所有并发请求）堵住。
    sheets, datemode = await asyncio.to_thread(load_workbook_rows, data, filename)
    if mapping is None:
        from app.services.billing_recon.ai_mapping import ai_column_mapping

        mapping = await ai_column_mapping(sheets)

    # 2. 平台门闸：当前只收携程系账单
    if mapping.platform_guess != "ctrip":
        errors = [f"当前仅支持携程系账单（AI 识别为 {mapping.platform_guess}）。其他平台账单等第二技能。"]
        raise BillRejected(await _reject(db, platform, user_id, mapping, errors, batch=processing_batch), errors)

    if mapping.sheet not in sheets:
        errors = [f"AI 认出的 sheet「{mapping.sheet}」不存在"]
        raise BillRejected(await _reject(db, platform, user_id, mapping, errors, batch=processing_batch), errors)

    # 3. 抽取 + 校验闸
    rows = extract_bill_rows(sheets[mapping.sheet], mapping, datemode)
    errors, stats = validate_bill(rows, mapping)
    if errors:
        raise BillRejected(await _reject(db, platform, user_id, mapping, errors, stats, processing_batch), errors)

    bill_month = infer_bill_month(rows)
    bill_orders = aggregate_orders(rows)
    checkouts = [bo.checkout for bo in bill_orders.values() if bo.checkout]
    date_lo = min(checkouts) if checkouts else None
    date_hi = max(checkouts) if checkouts else None

    # 3.5 同月串行闸：两个人同时传同一个月的账单，会各自读到"对方还没落库"的旧状态，
    #     结果两批 live diff 并存（互相都没把对方作废），前台看到重复差异、可能重复采纳。
    #     事务级 advisory lock，commit/rollback 自动释放；非 PG（测试 SQLite）跳过。
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:k))"),
            {"k": f"billrecon:{platform}:{bill_month}"},
        )

    # 4. 重传作废：同 (platform, bill_month) 旧批次的 pending → dismissed
    old_batch_ids = (await db.execute(
        select(ReconBatch.batch_id).where(ReconBatch.platform == platform, ReconBatch.bill_month == bill_month)
    )).scalars().all()

    if old_batch_ids:
        old_pending = (await db.execute(
            select(ReconDiff).where(ReconDiff.batch_id.in_(old_batch_ids),
                                    ReconDiff.status == ReconDiffStatus.pending)
        )).scalars().all()
        for d in old_pending:
            d.status = ReconDiffStatus.dismissed
            d.detail = {**(d.detail or {}), "voided_by_reupload": True}

    # 5. 跨月申诉自动核销：历史 appeal_pending 的单号出现在本期账单 → appeal_settled
    bill_nos = list(bill_orders.keys())
    settled: list[ReconDiff] = []
    if bill_nos:
        # 按平台圈定：别拿携程账单去核销美团批次里的申诉（单号命名空间不同平台可能撞车，
        # 一旦误核销，那笔真欠款就永远没人追了）。
        settled = (await db.execute(
            select(ReconDiff).join(ReconBatch, ReconDiff.batch_id == ReconBatch.batch_id).where(
                ReconBatch.platform == platform,
                ReconDiff.status == ReconDiffStatus.appeal_pending,
                ReconDiff.platform_order_id.in_(bill_nos),
            )
        )).scalars().all()
    for d in settled:
        bo = bill_orders[d.platform_order_id]
        settled_amount = bo.net
        system_amount = d.system_amount
        short_paid = bool(system_amount is not None and (system_amount - settled_amount) > _D01)
        d.status = ReconDiffStatus.appeal_settled
        d.detail = {
            **(d.detail or {}),
            "settled_amount": str(settled_amount),
            "system_amount": str(system_amount) if system_amount is not None else None,
            "settled_month": bill_month,
            "short_paid": short_paid,
        }
    settled_nos = {d.platform_order_id for d in settled}

    candidates = await fetch_candidates(db, bill_month, date_lo=date_lo, date_hi=date_hi)
    matches = match_orders(bill_orders, candidates)
    drafts = classify(bill_orders, matches, candidates, bill_month)

    summary = build_summary(drafts)
    result_mapping = {
        **(processing_batch.mapping if processing_batch else {}),
        **mapping.model_dump(),
        "stats": stats,
        "summary": summary,
        "ai_status": "pending",
        "ai_diagnosis": {"theme_codes": [], "per_row": {}},
        "ai_claim_failed": False,
    }
    if processing_batch is None:
        batch = ReconBatch(
            batch_id=_new_id(f"RB-{bill_month}"), platform=platform, bill_month=bill_month,
            summary_total=Decimal(str(mapping.summary_total)).quantize(_D01),
            row_count=len(rows), status="parsed", mapping=result_mapping, created_by=user_id,
        )
        db.add(batch)
    else:
        batch = processing_batch
        batch.platform = platform
        batch.bill_month = bill_month
        batch.summary_total = Decimal(str(mapping.summary_total)).quantize(_D01)
        batch.row_count = len(rows)
        batch.status = "parsed"
        batch.error = None
        batch.mapping = result_mapping
    if upload_fingerprint:
        batch.mapping = {**(batch.mapping or {}), "upload_fingerprint": upload_fingerprint}

    # 历史 diff（同 platform+bill_month，含刚被作废那批）——terminal 指纹去重 + already_consistent 判定用
    # 注：db.execute(select(...)) 命中会话内已修改但未 flush 的脏对象时，SQLAlchemy 走
    # identity map 返回同一批本轮已改过状态（如 appeal_settled）的实例——无论 autoflush
    # 开关状态都成立，这里不依赖显式 flush。
    hist_diffs: list[ReconDiff] = []
    if old_batch_ids:
        hist_diffs = (await db.execute(
            select(ReconDiff).where(ReconDiff.batch_id.in_(old_batch_ids))
        )).scalars().all()
    terminal_fp: set[tuple] = set()
    for d in hist_diffs:
        if d.status not in _TERMINAL_STATUSES or (d.detail or {}).get("voided_by_reupload"):
            continue
        pid_key = _fp_pid_key(d.platform_order_id, d.order_id)
        if pid_key is None:  # pid 和 order_id 都空：跳过指纹去重，绝不误伤压制
            continue
        terminal_fp.add((pid_key, d.diff_class, d.bill_amount, d.system_amount))

    # 申诉去重（跨批次跨月）：同单号已有 appeal_pending/appeal_settled 的不再新建 appeal draft
    appeal_pids = {dr.platform_order_id for dr in drafts
                   if dr.diff_class == ReconDiffClass.appeal and dr.platform_order_id}
    existing_appeal_pids: set[str] = set()
    if appeal_pids:
        existing_appeal_pids = set((await db.execute(
            select(ReconDiff.platform_order_id).where(
                ReconDiff.platform_order_id.in_(appeal_pids),
                ReconDiff.status.in_([ReconDiffStatus.appeal_pending, ReconDiffStatus.appeal_settled]),
            )
        )).scalars().all())

    diff_count = 0
    for dr in drafts:
        if dr.platform_order_id in settled_nos:  # 5. 刚核销的单号本批全类目跳过
            continue
        pid_key = _fp_pid_key(dr.platform_order_id, dr.order_id)
        fp = (pid_key, dr.diff_class, dr.bill_amount, dr.system_amount) if pid_key is not None else None
        if fp is not None and fp in terminal_fp:  # 6b. 终态指纹去重
            continue
        if dr.diff_class == ReconDiffClass.appeal and dr.platform_order_id in existing_appeal_pids:
            continue  # 6c. 申诉去重
        db.add(ReconDiff(
            diff_id=_new_id("RD"), batch_id=batch.batch_id, order_id=dr.order_id,
            platform_order_id=dr.platform_order_id, guest_name=dr.guest_name,
            diff_class=dr.diff_class, status=ReconDiffStatus.pending,
            bill_amount=dr.bill_amount, system_amount=dr.system_amount,
            detail={**(dr.detail or {})},
        ))
        diff_count += 1

    # R1 附则：重放场景下"账单+系统金额现已一致"，但对应历史 fix_amount/compensation
    # 指纹曾经 adopted 过 → classify() 本身不出 draft，显式补一条 already_consistent 状态的 diff。
    hist_adopted_by_no = {
        d.platform_order_id: d for d in hist_diffs
        if d.status == ReconDiffStatus.adopted and d.diff_class in (ReconDiffClass.fix_amount, ReconDiffClass.compensation)
    }
    for no, bo in bill_orders.items():
        if no in settled_nos or no not in hist_adopted_by_no:
            continue
        m = matches.get(no)
        if m is None or m.order is None or m.via not in ("exact", "prefix"):
            continue
        rev = m.order.expected_revenue
        if rev is None or abs(rev - bo.net) > _D01:
            continue
        hist = hist_adopted_by_no[no]
        fp = (no, hist.diff_class, bo.net, rev)
        if fp in terminal_fp:
            continue
        db.add(ReconDiff(
            diff_id=_new_id("RD"), batch_id=batch.batch_id, order_id=m.order.order_id,
            platform_order_id=no, guest_name=bo.guest,
            diff_class=hist.diff_class, status=ReconDiffStatus.already_consistent,
            bill_amount=bo.net, system_amount=rev,
            detail={"via": m.via, "note": "重放核对：与历史已采纳的差异指纹一致，金额现已一致"},
        ))
        diff_count += 1

    await db.commit()
    await db.refresh(batch)
    await _enrich_batch_with_ai(db, batch=batch, drafts=drafts, candidates=candidates, summary=summary)
    await db.refresh(batch)
    elapsed = time.monotonic() - started
    logger.info(
        "billing_recon 成功 batch_id=%s bill_month=%s row_count=%d diff_count=%d elapsed=%.2fs",
        batch.batch_id, batch.bill_month, batch.row_count, diff_count, elapsed,
    )
    return batch


# ─────────────────────────────────────────────────────────────────────────
# apply_diff_action：处置动作（采纳/忽略/申诉/确认）+ 结算联动 + 审计（Task 7）
#
# 每个分类只认自己的动作/终态（见 recon.py 模型 docstring）：
#  - fix_amount/compensation: pending → adopted / already_consistent / dismissed
#  - appeal: pending → appeal_pending → appeal_settled / dismissed（appeal_settled
#    只由 run_recon 的跨月核销写，本函数不产生）
#  - broken_link: pending → acknowledged / dismissed
#  - manual_review: pending → dismissed
# 非法分类/动作组合 → ValueError（端点层 Task 8 转 400）。
#
# 采纳(adopt)写库语义对照 backend/scripts/fix_bill_recon_202607.py 逐项实现：
# metadata 整体重赋值（不能原地改，JSONB 变更追踪要求）、price_locked=True、
# bill_recon_<YYYYMM> 面包屑（按账单月分 key，见 Fix 6）留痕旧值、倒贴单（到手>房费）
# 按 net=actual×(1−rate)+ota_subsidy 配平补贴、非倒贴单删掉可能存在的假补贴。
# JSONB 落库的金额一律 str()。fix_amount 只认单房单（Fix 1）——多房单订单级
# metadata 结算引擎不接管，写了也是死字段。
# 审计用 log_action_tx（commit 前调用，失败则整个事务回滚，钱不会凭空消失又没审计）。
# ─────────────────────────────────────────────────────────────────────────

_ALLOWED = {
    ReconDiffClass.fix_amount: {"adopt", "dismiss"},
    ReconDiffClass.compensation: {"adopt", "dismiss"},
    ReconDiffClass.appeal: {"appeal", "dismiss"},
    ReconDiffClass.broken_link: {"acknowledge", "dismiss"},
    ReconDiffClass.manual_review: {"dismiss"},
}


_SETTLEMENT_STATUS_LABEL = {
    SettlementStatus.confirmed: "已确认",
    SettlementStatus.paid: "已打款",
    SettlementStatus.disputed: "有争议",
}


def _dec_or_none(raw) -> Decimal | None:
    """metadata 里的脏值（空串/文字/None）→ None，绝不让批处理因为一条脏数据 500。"""
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def _require_bill_amount(diff: ReconDiff) -> Decimal:
    """Fix 7：NULL 账单金额不能进 Decimal()，转成 ValueError（端点层 400）而不是 500。"""
    if diff.bill_amount is None:
        raise ValueError("差异缺少账单金额，无法采纳")
    return Decimal(diff.bill_amount).quantize(_D01)


async def _diff_bill_month(db: AsyncSession, diff: ReconDiff) -> str:
    batch = await db.get(ReconBatch, diff.batch_id)
    return batch.bill_month if batch is not None else "unknown"


def _month_first_day(bill_month: str) -> date | None:
    """"YYYY-MM" → 该月 1 号；解析不出（"unknown"/脏值）返回 None 由调用方兜底。"""
    try:
        return date(int(bill_month[:4]), int(bill_month[5:7]), 1)
    except (ValueError, TypeError):
        return None


async def _settlement_warnings(db: AsyncSession, month: str) -> list[str]:
    """采纳会改结算口径：同月结算单需要人工跟进（粗粒度警告：整月全提示，
    比按业主精确圈定保守但操作上等价——月度结算单本来就要整批重生成）。

    month = 被采纳订单 check_out_date 的 YYYY-MM（R1 铁律，不是批次的 bill_month——
    跨月补结行采纳的是别的月的单，警告要指向那个月的结算单，不是账单批次所属月）。

    pending 行只回 settlement_id（前端包上"结算单需重新生成"的既有文案）；
    已确认/已打款/有争议的行必须也浮出来——重生成救不了它们，钱可能已经按旧数打出去了，
    只能带一句完整警告让人工核对。本函数只读，绝不改任何结算行。
    """
    rows = await db.execute(
        select(OwnerSettlement.settlement_id, OwnerSettlement.status).where(
            OwnerSettlement.billing_month == month,
        )
    )
    out: list[str] = []
    for sid, status in rows.all():
        if status == SettlementStatus.pending:
            out.append(sid)
        else:
            label = _SETTLEMENT_STATUS_LABEL.get(status, "已锁定")
            out.append(f"⚠️ 该月结算单 {sid} {label}，采纳后系统数与{label}数将不一致，请人工核对")
    return out


async def _adopt_fix(db: AsyncSession, diff: ReconDiff, user_id: str | None) -> tuple[str, Order | None]:
    bill = _require_bill_amount(diff)
    if bill <= 0:
        # 净额≤0 的单不该走 fix_amount：写进 ota_owner_revenue 会被结算当"业主到手 0"，
        # 一采纳就把业主收入清零还顺手 price_locked 冻住。
        raise ValueError("账单净额≤0，疑似退款超额或赔款行，请人工核对后处置")

    order = await db.get(Order, diff.order_id)
    if order is None:
        raise ValueError(f"订单 {diff.order_id} 不存在")

    # Fix 1：多房单守卫——本函数写的是订单级 metadata.ota_owner_revenue/ota_subsidy，
    # owner_settlement.py 只对单房单（每单恰一行 OrderRoom）按该字段直接锚定结算；
    # 多房单订单级到手横跨多间，结算引擎不接管，写了也是死字段，人工得去订单页处置。
    rooms = (await db.execute(
        select(OrderRoom).where(OrderRoom.order_id == order.order_id)
    )).scalars().all()
    if len(rooms) != 1:
        raise ValueError("多房单请在订单页人工处置（结算引擎只按单房单消费该字段）")
    room = rooms[0]

    # B2：分支/补贴一律用结算真正消费的那个房费口径。effective_owner_revenue_for_room
    # 拿的是 OrderRoom.actual_price（每房房费），订单级 actual_price 只是兜底；
    # 两者不一致时按订单级判倒贴会把补贴算到别的基数上，钱就错了。
    anchor_raw = room.actual_price if room.actual_price is not None else order.actual_price
    anchor = Decimal(anchor_raw or 0)
    if anchor <= 0 or (order.metadata_ or {}).get("price_pending"):
        raise ValueError("订单价格未回填，先完成价格同步/人工定价再采纳（否则 price_locked 会永久冻结零价）")

    md = dict(order.metadata_ or {})
    old_rev, old_sub = md.get("ota_owner_revenue"), md.get("ota_subsidy")

    # B1（CRITICAL）：order_pricing.effective_owner_revenue_for_room 里**每房手填净房费优先于
    # 订单级** ota_owner_revenue。这行 OrderRoom 上挂着手填值时，只改订单级等于白改——
    # 结算照旧按旧的每房值付钱。必须同步改房行，否则采纳是个静默无效动作。
    room_md = dict(room.metadata_ or {})
    old_room_rev = room_md.get("ota_owner_revenue")
    has_room_manual = old_room_rev not in (None, "")

    rate = Decimal(order.platform_commission_rate or 0)
    if bill > anchor:
        # 倒贴单：结算走 anchor×(1−rate)+ota_subsidy，配平到 net == bill
        md["ota_subsidy"] = str((bill - anchor * (Decimal("1") - rate)).quantize(_D01))
    else:
        md.pop("ota_subsidy", None)  # 删假补贴（改期/改价滞留的旧补贴不再消费）
    # Fix 4：subsidy 的 set/pop 在 already_consistent 短路判断之前算好——重放批次里
    # old_rev 已经等于 bill，但 ota_subsidy 是滞留假值时，仍要把它清掉，不能因为
    # revenue 已经一致就整段跳过写库。只有 revenue、subsidy、每房值都没变化才是真幂等。
    new_sub = md.get("ota_subsidy")
    old_rev_d = _dec_or_none(old_rev)
    old_room_rev_d = _dec_or_none(old_room_rev)
    room_consistent = (not has_room_manual) or (
        old_room_rev_d is not None and abs(old_room_rev_d - bill) <= _D01
    )
    if old_rev_d is not None and abs(old_rev_d - bill) <= _D01 and new_sub == old_sub and room_consistent:
        return "already_consistent", order  # 真幂等：重放已修过的单，不再动库/不再审计

    md["ota_owner_revenue"] = str(bill)
    md["price_locked"] = True
    # Fix 6：面包屑按账单月分 key，不用固定 "bill_recon"——否则跨月重放会互相覆盖
    # old_rev/old_sub（照抄参考脚本 backend/scripts/fix_bill_recon_202607.py 的约定）。
    bill_month = await _diff_bill_month(db, diff)
    breadcrumb_key = f"bill_recon_{bill_month.replace('-', '')}"
    md[breadcrumb_key] = {"batch_id": diff.batch_id, "old_rev": old_rev, "old_sub": old_sub,
                          "old_room_rev": old_room_rev, "fixed_at": today_cn().isoformat()}
    order.metadata_ = md  # JSONB 必须整体重新赋值，原地改字典不会被 SQLAlchemy 追踪
    if has_room_manual:
        room.metadata_ = {**room_md, "ota_owner_revenue": str(bill)}

    await log_action_tx(
        db, user_id, "billing_recon.adopt", "order", order.order_id,
        before_data={"ota_owner_revenue": old_rev, "ota_subsidy": old_sub,
                     "room_ota_owner_revenue": old_room_rev},
        after_data={"ota_owner_revenue": str(bill), "ota_subsidy": md.get("ota_subsidy"),
                    "room_ota_owner_revenue": str(bill) if has_room_manual else old_room_rev},
        notes=f"账单对账采纳 diff={diff.diff_id}",
    )
    return "adopted", order


async def _adopt_compensation(
    db: AsyncSession, diff: ReconDiff, user_id: str | None
) -> tuple[str, str | None]:
    """R3：platform_order_id 能唯一对到订单（含 cancelled 单，不限 _ACTIVE）时，
    Expense 带 order_id 供支出/结算侧溯源；对不到（或同一 platform_order_id 挂多张
    脏数据订单，歧义）就留空，绝不瞎挑一张。room_id 见下方 Fix 2 注释，故意恒为 None。

    返回 (状态, 需要提结算警告的账单月)；判重命中（没写库）时月份回 None。
    """
    amount = abs(_require_bill_amount(diff))
    bill_month = await _diff_bill_month(db, diff)
    # Fix 5：判重按 description（含单号+金额+账单月）不按 category——同一赔款行被重复采纳
    # （批次重传/前端重复点击）不能吃两次扣款；带上账单月，不同月的同额同单赔款各记各的。
    description = f"账单赔款 {diff.platform_order_id} {amount} ({bill_month})"
    dup_id = (await db.execute(
        select(Expense.expense_id).where(
            Expense.description == description,
            Expense.is_deleted == False,  # noqa: E712 — 软删的旧记录不该压制重新入账
        ).limit(1)  # 历史脏数据可能有多条同描述，first() 就够，别让 one() 500 掉
    )).scalars().first()
    if dup_id is not None:
        diff.detail = {**(diff.detail or {}), "expense_deduped": True}
        return "adopted", None

    order_id: str | None = None
    if diff.platform_order_id:
        order_ids = (await db.execute(
            select(Order.order_id).where(Order.platform_order_id == diff.platform_order_id)
        )).scalars().all()
        if len(order_ids) == 1:
            order_id = order_ids[0]

    exp = Expense(
        expense_id=_new_id("EXP-RC")[:20], category=ExpenseCategory.other, amount=amount,
        description=description,
        # D2：罚款记进账单所属月的 1 号，跟收入按账单月归集的口径一致——
        # 记 today_cn() 会让 8 月处理的 7 月赔款掉进 8 月结算，两边永远对不平。
        expense_date=_month_first_day(bill_month) or today_cn(), order_id=order_id,
        # Fix 2：room_id 故意留空——ExpenseCategory.other 在 OWNER_COST_SHARE_CATEGORIES
        # 里，owner_settlement 按 room_id 挑支出分摊业主成本；挂了 room_id 就等于没人拍板
        # 就让业主分摊了这笔平台赔款。若王总拍板赔款由业主分担，改此处一行即可。
        room_id=None, created_by=user_id,
    )
    db.add(exp)
    await log_action_tx(
        db, user_id, "billing_recon.adopt_compensation", "expense", exp.expense_id,
        after_data={"amount": str(amount), "platform_order_id": diff.platform_order_id,
                    "order_id": order_id, "room_id": None, "bill_month": bill_month},
        notes=f"账单对账赔款入账 diff={diff.diff_id}",
    )
    return "adopted", bill_month


async def apply_diff_action(db: AsyncSession, diff: ReconDiff, action: str, user_id: str | None) -> dict:
    """处置一条 ReconDiff。返回 {"status": <新状态>, "settlement_warnings": [...]}。

    settlement_warnings 里 pending 结算单是裸 settlement_id（前端包既有"需重新生成"文案），
    已确认/已打款/有争议的是一整句警告文本（见 _settlement_warnings）。

    允许的 action：
      - fix_amount/compensation: adopt / dismiss
      - appeal: appeal（→appeal_pending）/ dismiss
      - broken_link: acknowledge / dismiss
      - manual_review: dismiss
    其余组合、或 diff 已不在 pending 态 → ValueError。
    """
    # C1：先按主键上行锁再读状态——两个人（或前端重复点击）同时采纳同一条差异时，
    # 无锁的话双方都读到 pending，钱会被写两遍（赔款则建两条 Expense）。
    # 行锁必须在状态判断之前：锁完再读，第二个请求看到的就是第一个已提交的终态。
    # SQLite（测试）不支持 FOR UPDATE，SQLAlchemy 会自动省略，是无害的 no-op。
    # 所有会改变 pending 状态的入口先锁父批次；review 也锁同一行，避免
    # “确认已核对”与最后一条差异处置并发时产生 write skew。
    await db.execute(
        select(ReconBatch).where(ReconBatch.batch_id == diff.batch_id).with_for_update()
    )
    diff = (await db.execute(
        select(ReconDiff).where(ReconDiff.diff_id == diff.diff_id).with_for_update()
    )).scalar_one_or_none()
    if diff is None:
        raise ValueError("差异不存在")
    if diff.status != ReconDiffStatus.pending:
        raise ValueError(f"差异 {diff.diff_id} 当前状态 {diff.status.value}，不可再处置")
    if action not in _ALLOWED[diff.diff_class]:
        raise ValueError(f"{diff.diff_class.value} 不支持动作 {action}")

    warnings: list[str] = []
    if action == "adopt" and diff.diff_class == ReconDiffClass.fix_amount:
        new_status, order = await _adopt_fix(db, diff, user_id)
        if new_status == "adopted" and order is not None:
            warnings = await _settlement_warnings(db, order.check_out_date.strftime("%Y-%m"))
    elif action == "adopt":  # compensation
        new_status, warn_month = await _adopt_compensation(db, diff, user_id)
        if warn_month:
            # 赔款按账单月入账（D2），警告也指向那个月的结算单
            warnings = await _settlement_warnings(db, warn_month)
    elif action == "appeal":
        new_status = "appeal_pending"
    elif action == "acknowledge":
        if not diff.order_id or not diff.platform_order_id:
            raise ValueError("断链差异缺少订单号或平台单号，无法修复关联")
        order = (await db.execute(
            select(Order).where(Order.order_id == diff.order_id).with_for_update()
        )).scalar_one_or_none()
        if order is None or order.is_deleted:
            raise ValueError(f"订单 {diff.order_id} 不存在")
        if order.platform_order_id and order.platform_order_id != diff.platform_order_id:
            raise ValueError("系统订单已关联其他平台单号，请改用人工核对")
        duplicate = (await db.execute(
            select(Order.order_id).where(
                Order.platform_order_id == diff.platform_order_id,
                Order.order_id != order.order_id,
                Order.is_deleted.is_(False),
            )
        )).scalars().first()
        if duplicate:
            raise ValueError("该平台单号已关联其他系统订单，请人工核对")
        old_platform_order_id = order.platform_order_id
        order.platform_order_id = diff.platform_order_id
        await log_action_tx(
            db, user_id, "billing_recon.repair_link", "order", order.order_id,
            before_data={"platform_order_id": old_platform_order_id},
            after_data={"platform_order_id": diff.platform_order_id, "diff_id": diff.diff_id},
        )
        new_status = "acknowledged"
    else:  # dismiss
        new_status = "dismissed"

    diff.status = ReconDiffStatus(new_status)
    diff.resolved_by = user_id
    diff.resolved_at = datetime.now(timezone.utc)
    await db.commit()
    return {"status": new_status, "settlement_warnings": warnings}


# ─────────────────────────────────────────────────────────────────────────
# claim_match：认领确认（Task 5，两步中的 step1）——链接 + 重分类，绝不写钱
#
# manual_review 差异（账单有单号，系统三级匹配都没对上）由用户手动选定应归属的系统单。
# 本函数只做：校验 → 建 platform_order_id 链接 → 按 rev vs bill 重分类 → 审计。
# 金额差（fix_amount）留给用户随后走 apply_diff_action('adopt') 处置——那里才有
# 多房单守卫/结算警告/完整审计。这里绝不碰 order.metadata_["ota_owner_revenue"]。
# ─────────────────────────────────────────────────────────────────────────


async def claim_match(db: AsyncSession, diff: ReconDiff, order_id: str, user_id: str | None) -> dict:
    """认领确认（两步中的 step1）：把账单行链接到用户选定的系统单，重算分类。
    只写 linkage（platform_order_id）+ 分类，绝不写 ota_owner_revenue——金额差由用户
    随后走 apply_diff_action('adopt') 处置（多房单守卫/结算警告/审计都在那）。"""
    await db.execute(
        select(ReconBatch).where(ReconBatch.batch_id == diff.batch_id).with_for_update()
    )
    diff = (await db.execute(
        select(ReconDiff).where(ReconDiff.diff_id == diff.diff_id).with_for_update()
    )).scalar_one_or_none()
    if diff is None:
        raise ValueError("差异不存在")
    if diff.status != ReconDiffStatus.pending:
        raise ValueError(f"差异 {diff.diff_id} 当前状态 {diff.status.value}，不可认领")
    if diff.diff_class != ReconDiffClass.manual_review:
        raise ValueError("只有待人工核对的行可以认领")
    order = await db.get(Order, order_id)
    if order is None or order.is_deleted:
        raise ValueError(f"订单 {order_id} 不存在")
    no = diff.platform_order_id
    if order.platform_order_id and order.platform_order_id != no:
        raise ValueError(f"该订单已有平台单号（{order.platform_order_id}），不能认领到账单单号 {no}")
    # 目标单未被本批另一账单行认领
    dup = (await db.execute(
        select(ReconDiff.diff_id).where(ReconDiff.order_id == order_id,
                                        ReconDiff.batch_id == diff.batch_id)
    )).scalars().first()
    if dup is not None:
        raise ValueError("该订单已被本批另一条账单行认领")

    old_pid = order.platform_order_id
    order.platform_order_id = no  # 建立链接（下次上传即可精确匹配）
    diff.order_id = order_id
    diff.detail = {**(diff.detail or {}), "claimed_by": user_id, "reason": "claimed"}

    rev = order.expected_revenue
    bill = Decimal(diff.bill_amount) if diff.bill_amount is not None else None
    if rev is None:
        diff.diff_class = ReconDiffClass.manual_review
        diff.detail = {**diff.detail, "reason": "no_ota_owner_revenue"}
        new_status, new_class = "pending", "manual_review"
    elif bill is not None and abs(rev - bill) <= _D01:
        diff.diff_class = ReconDiffClass.broken_link
        diff.system_amount = rev
        diff.status = ReconDiffStatus.acknowledged
        new_status, new_class = "acknowledged", "broken_link"
    else:
        diff.diff_class = ReconDiffClass.fix_amount
        diff.system_amount = rev
        new_status, new_class = "pending", "fix_amount"

    await log_action_tx(
        db, user_id, "billing_recon.claim", "recon_diff", diff.diff_id,
        before_data={"order_id": None, "order_platform_order_id": old_pid},
        after_data={"order_id": order_id, "order_platform_order_id": no, "new_class": new_class},
        notes=f"账单认领 diff={diff.diff_id} → order={order_id}",
    )
    await db.commit()
    return {"status": new_status, "diff_class": new_class}
