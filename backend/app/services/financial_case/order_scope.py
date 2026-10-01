"""Exact, durable order selections from a read query to a correction preview.

The scope contains server-selected IDs and a snapshot, never a model selector.
Reading evidence does not create a checklist source or modify business data.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .operations import digest, label, load_context, select_orders, snapshot


class OrderQueryScope(BaseModel):
    model_config = ConfigDict(extra='forbid')
    version: Literal['order-query-scope-v1']
    run_id: str
    actor_id: str
    cycle_id: str
    billing_month: str
    control_version: int
    cycle_status: str
    order_ids: list[str] = Field(max_length=500)
    snapshot_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    issue_hash: str = Field(pattern=r'^[a-f0-9]{64}$')


def is_identity_diagnostic(text):
    return bool(re.search(r'身份|身份证|入住人|平台订单号|平台单号', text)
                and re.search(r'查看|看看|查询|列出|哪些|什么|缺|问题|待核实|待确认', text)
                and not re.search(r'改为|改成|修正|重新关联|公司承担|公司全担', text))


def wants_order_query(text):
    # Identity diagnostics carry their own follow-up target and controlled
    # correction route. Do not replace that history with a financial scope.
    if re.search(r'身份|身份证|入住人|平台订单号|平台单号', text):
        return False
    if re.search(r'改为|改成|修正|确认执行|重新关联|公司承担|公司全担|不用补房费', text):
        return False
    # A general checklist-topic question must keep its review_month history.
    # An explicit list of pending orders or a named guest/room read creates
    # an actionable object scope; "查看订单有哪些问题" is a workflow overview.
    if re.search(r'这一步|步骤|卡在哪|怎么处理|如何处理|核对进度|缺哪些资料|继续之前|继续刚才', text):
        return False
    if re.fullmatch(r'[\s，。？?]*(?:(?:请|帮我|本月|这个月|当前|现在|还有|查看|看看|查询|的|订单|有|哪些|什么|问题|吗|和|及|与|、)|(?:20\d{2}[-年])?(?:\d{1,2}|十一|十二|十|[一二三四五六七八九])月|20\d{2}-\d{2})+[\s，。？?]*', text):
        return False
    return bool(re.search(r'订单|ORD-[A-Za-z0-9-]+|哪些客人', text, re.I)
                and re.search(r'查看|看看|查询|列出|列一下|哪些|什么问题|有问题|待处理|未处理|待核实|未核实|待确认|未确认|剩余|剩下', text))


def refers_to_scope(text):
    return bool(re.search(r'这些|这几|这批|这笔|上述|剩下|剩余|全部|其余', text))


async def order_issues(db, cycle):
    from app.services.monthly_close.evidence import build_step_evidences
    result = {}
    for report in await build_step_evidences(db, cycle):
        if report.step_key == 'exception_clearance':
            continue
        for issue in report.snapshot.get('issues', []):
            ids = set(issue.get('order_ids') or [])
            if issue.get('order_id'):
                ids.add(issue['order_id'])
            for key in ids:
                result.setdefault(key, {})[digest(issue)] = issue
    return {key: [items[h] for h in sorted(items)] for key, items in result.items()}


def issues_hash(issues, ids):
    return digest({key: issues.get(key, []) for key in sorted(ids)})


async def query(db, cycle, actor, text, run_id):
    from .service import actor_id
    # Never reinterpret an explicit foreign month as the active workspace.
    from app.services.monthly_close.semantic_agent import month_scope_question
    question = month_scope_question(cycle.billing_month, text, strict=True)
    if question:
        raise ValueError(question)
    ctx = await load_context(db, cycle)
    issues = await order_issues(db, cycle)
    selected = select_orders(ctx, text)
    explicit = set(re.findall(r'ORD-[A-Za-z0-9-]+', text, re.I))
    if explicit and len(explicit) != len(selected):
        raise ValueError('部分订单不在当前月份，请核实订单范围后重新查询。')
    if not selected:
        # Only an explicitly broad question may select across the month. An
        # unmatched guest or room must never fall back to every problem order.
        broad = re.fullmatch(r'[\s，。？?]*(?:(?:请|帮我|本月|这个月|当前|现在|还有|还剩|剩下|剩余|全部|所有|查看|看看|查询|列出|列一下|的|和|、|哪些|什么|有|问题|订单|客人|待处理|未处理|待核实|未核实|待确认|未确认|需要处理|需要核实|需要确认)|(?:20\d{2}[-年])?\d{1,2}月|20\d{2}-\d{2})+[\s，。？?]*', text)
        if not broad:
            raise ValueError('请提供当前月份的客人姓名、房号或订单编号，才能列出对应的订单。')
        selected = ctx['orders']
    if re.search(r'问题|待处理|未处理|待核实|未核实|待确认|未确认|需要处理|需要核实|需要确认|剩下|剩余', text):
        selected = [order for order in selected if issues.get(order.order_id)]
    if len(selected) > 500:
        raise ValueError('这次查询超过 500 笔订单，请按客人或房号缩小范围后再处理。')
    ids = sorted(order.order_id for order in selected)
    scope = OrderQueryScope(version='order-query-scope-v1', run_id=run_id,
        actor_id=actor_id(actor), cycle_id=cycle.cycle_id, billing_month=cycle.billing_month,
        control_version=cycle.control_version, cycle_status=cycle.status, order_ids=ids,
        snapshot_hash=snapshot(ctx, ids), issue_hash=issues_hash(issues, ids))
    details = []
    for order in selected:
        rows = issues.get(order.order_id, [])
        descriptions = list(dict.fromkeys(item['message'] for item in rows))
        amount = f'当前订单房费 {order.actual_price:.2f} 元。' if order.actual_price is not None else '当前订单房费未登记。'
        details.append(dict(label=label(ctx, order), value=(amount + ('；'.join(descriptions) or '当前检查未发现关联的待处理事项。'))[:1900]))
    return dict(order_scope=scope.model_dump(), details=details, export_ready=False,
                message=f'本次列出 {len(ids)} 笔订单。下方是这次查询的具体范围；继续说“这些按当前系统为准”或明确费用承担规则时，会先生成对应订单的方案。' if ids else '本次查询没有找到符合条件的订单，请核对月份、姓名、房号和问题范围。')


async def validate(db, cycle, actor, scope, context_run_id, ctx):
    from .service import actor_id
    try:
        bound = OrderQueryScope.model_validate(scope)
    except ValueError as exc:
        raise ValueError('上次查询范围无法验证，请重新列出需要处理的订单。') from exc
    if (bound.run_id != context_run_id or bound.actor_id != actor_id(actor)
            or bound.cycle_id != cycle.cycle_id or bound.billing_month != cycle.billing_month):
        raise ValueError('这次操作没有对应的本人本月查询，请重新列出需要处理的订单。')
    ids = bound.order_ids
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('上次查询没有可处理的订单，请重新查询。')
    if (bound.control_version != cycle.control_version or bound.cycle_status != cycle.status
            or set(ids) - {order.order_id for order in ctx['orders']}
            or bound.snapshot_hash != snapshot(ctx, ids)
            or bound.issue_hash != issues_hash(await order_issues(db, cycle), ids)):
        raise ValueError('上次查询后的订单、账目或核实状态已变化，请重新查询后再生成方案。')
    return ids
