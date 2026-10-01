"""Admin-only financial-case routing and bounded, server-owned reply schemas.

Routing selects a service, never a write operation. Proposal execution belongs to
service.respond and must use an explicit, actor-bound prior run.
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from app.models.monthly_close_control import MonthlyCloseMessage, MonthlyCloseRun
from app.models.user import User
from .order_scope import OrderQueryScope

VERSION = 'financial-case-v1'


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra='forbid')


class FinancialCaseSource(StrictDTO):
    source_id: str = Field(min_length=1, max_length=80)
    filename: str = Field(min_length=1, max_length=255)
    kind: str = Field(min_length=1, max_length=80)
    fact_count: int = Field(ge=0)


class FinancialCaseMetric(StrictDTO):
    label: str = Field(max_length=160)
    value: str = Field(max_length=160)
    detail: str | None = Field(default=None, max_length=1000)


class FinancialCaseIssue(StrictDTO):
    code: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
    message: str = Field(max_length=1000)
    source_id: str | None = Field(default=None, max_length=80)
    fact_key: str | None = Field(default=None, max_length=160)


class FinancialCaseDetail(StrictDTO):
    label: str = Field(max_length=160)
    value: str = Field(max_length=2000)


class FinancialCaseFacts(StrictDTO):
    projection_version: Literal['financial-case-v1']
    billing_month: str = Field(pattern=r'^\d{4}-(?:0[1-9]|1[0-2])$')
    state: Literal['result', 'proposal', 'completed', 'needs_information']
    message: str = Field(min_length=1, max_length=500)
    sources: list[FinancialCaseSource] = Field(max_length=100)
    metrics: list[FinancialCaseMetric] = Field(max_length=100)
    issues: list[FinancialCaseIssue] = Field(max_length=500)
    details: list[FinancialCaseDetail] = Field(max_length=500)
    proposal_id: str | None = Field(default=None, max_length=80)
    proposal_kind: Literal['interpretation', 'posting', 'business_correction'] | None = None
    report_months: list[str] = Field(max_length=240)
    export_ready: bool
    ledger_scope: dict | None = None
    order_scope: OrderQueryScope | None = None

    @field_validator('report_months')
    @classmethod
    def valid_report_months(cls, values):
        if any(not re.fullmatch(r'\d{4}-(?:0[1-9]|1[0-2])', value) for value in values):
            raise ValueError('invalid report month')
        return values


def _service():
    from app.services.financial_case import service
    return service


async def previous_reply(db, cycle, actor, context_run_id=None):
    """Load exactly the explicit run, or the latest actor turn regardless of tool.

    Filtering to financial runs first would let an old financial plan steal a
    confirmation intended for a newer cleaning/order plan.
    """
    from app.services.monthly_close.assistant import _actor_identity, _validated_durable_reply
    actor_id, role = _actor_identity(actor)
    if role != 'admin':
        return None
    query = select(MonthlyCloseRun, MonthlyCloseMessage).join(
        MonthlyCloseMessage, MonthlyCloseMessage.run_id == MonthlyCloseRun.run_id
    ).where(
        MonthlyCloseRun.cycle_id == cycle.cycle_id,
        MonthlyCloseRun.actor_id == actor_id,
        MonthlyCloseRun.status.in_(['succeeded', 'waiting_user', 'degraded']),
        MonthlyCloseMessage.role == 'assistant',
    )
    if context_run_id:
        query = query.where(MonthlyCloseRun.run_id == context_run_id)
    row = (await db.execute(query.order_by(MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc()).limit(1))).first()
    if row is None or row[0].permission_snapshot.get('role') != 'admin':
        return None
    return _validated_durable_reply(row[0], row[1], expected_scope='finance')


async def resolve_context_run_id(db, cycle, actor, context_run_id=None):
    # Preserve invalid explicit references so a later scope lookup rejects
    # them instead of silently switching to the latest conversation result.
    if context_run_id:
        return context_run_id
    reply = await previous_reply(db, cycle, actor, context_run_id)
    return reply.run_id if reply is not None and reply.tool == 'financial_case' else None


async def resolve_operation_attachment_ids(db, cycle, actor, text, attachment_ids, context_run_id=None):
    """Bridge explicitly selected cleaning originals to their controlled work tool.

    Never drop a mixed attachment or widen an explicit selection to every file.
    Confirmation remains bound to the prior work-chat run and executor.
    """
    operation = bool(re.search(r'保洁记录|打扫记录|保洁差异|续住.{0,8}(?:费用|补账)|(?:补齐|补录|对齐|删除|恢复).{0,12}(?:保洁|打扫)', text))
    control = re.sub(r'[\s，。！!]+', '', text) in {
        '确认执行', '确认补齐', '确认删除', '确认恢复', '确认', '取消方案', '取消',
    }
    if not operation and not (control and attachment_ids):
        return attachment_ids
    from app.services.monthly_close.assistant import _actor_identity, AssistantAttachmentNotFound
    if _actor_identity(actor)[1] != 'admin':
        return attachment_ids
    prior_work_document = None
    if control and not operation:
        previous = await previous_reply(db, cycle, actor, context_run_id)
        if previous is None or previous.tool != 'cleaning_work_chat':
            return attachment_ids
        prior_work_document = previous.facts.get('document_id')
    ids = list(attachment_ids)
    if not ids:
        previous = await previous_reply(db, cycle, actor, context_run_id)
        if previous is None or previous.tool != 'financial_case':
            return attachment_ids
        ids = [s['source_id'] for s in previous.facts.get('sources', [])]
        # A financial overview may contain other files. An explicit cleaning
        # operation can select its cleaning originals without passing bank data.
        scoped = {s.source_id: s for s in await _service().load_sources(db, cycle.cycle_id)}
        ids = [i for i in ids if i in scoped and scoped[i].kind == 'cleaning']
    if not ids or any(not isinstance(i, str) or not i.startswith('FCS-') for i in ids):
        return attachment_ids
    if len(ids) > 20 or len(ids) != len(set(ids)):
        raise AssistantAttachmentNotFound
    await _service().require_admin(db, actor)
    await _service().validate_attachments(db, cycle.cycle_id, ids)
    sources = {s.source_id: s for s in await _service().load_sources(db, cycle.cycle_id)}
    selected = [sources[i] for i in ids]
    if any(s.kind != 'cleaning' or not s.document_id for s in selected):
        return attachment_ids
    if prior_work_document and any(s.document_id != prior_work_document for s in selected):
        return attachment_ids
    return [s.document_id for s in selected]


async def can_handle(db, cycle, actor, text, attachment_ids, context_run_id=None):
    from app.services.monthly_close.assistant import _actor_identity, AssistantAttachmentNotFound
    actor_id, role = _actor_identity(actor)
    financial_attachments = any(isinstance(value, str) and value.startswith('FCS-') for value in attachment_ids)
    if role != 'admin':
        if financial_attachments:
            raise PermissionError('financial sources require an administrator')
        return False
    if attachment_ids and not financial_attachments:
        return False
    if financial_attachments:
        if len(attachment_ids) > 20 or len(attachment_ids) != len(set(attachment_ids)) or any(
            not isinstance(value, str) or not re.fullmatch(r'FCS-[A-Za-z0-9._:-]{1,76}', value)
            for value in attachment_ids
        ):
            raise AssistantAttachmentNotFound
    # An actor dictionary is not proof of live authorization.
    user = await db.scalar(select(User).where(User.user_id == actor_id).execution_options(populate_existing=True))
    if user is None or not user.is_active or str(getattr(user.role, 'value', user.role)) != 'admin':
        if financial_attachments:
            raise PermissionError('financial sources require an active administrator')
        return False
    if financial_attachments:
        if context_run_id and re.search(r'确认|执行|取消', text):
            selected_reply = await previous_reply(db, cycle, actor, context_run_id)
            if selected_reply is None or selected_reply.tool != 'financial_case':
                return False
        await _service().validate_attachments(db, cycle.cycle_id, attachment_ids)
        return True
    from .operations import wants_operation
    from .order_scope import wants_order_query, is_identity_diagnostic
    if is_identity_diagnostic(text):
        return False
    if wants_order_query(text):
        return True
    if wants_operation(text):
        return True
    if re.search(r'待核实清单|核实清单|待确认清单',text):
        return True
    if re.search(r'打扫明细|保洁明细',text) and re.search(r'补充|补齐|补录|录入|导入',text) and not re.search(r'多少钱|费用多少|合计',text):
        return False
    if re.search(r'保洁|打扫|洗涤|水电|物业|费用|支出',text) and re.search(r'多少|合计|明细|总共|一共',text):
        return True
    if re.search(r'核对保洁记录|查保洁差异|核对打扫记录',text) and not re.search(r'金额|多少钱|费用',text):
        return False
    # Operational edits keep their existing controlled executor even if a prior
    # financial discussion happens to mention cleaning supplier expenditure.
    if re.search(r'(?:删除|补齐|恢复|撤销|重建).{0,16}(?:打扫|保洁|工单|订单|记录)|(?:打扫|保洁|工单|订单).{0,16}(?:删除|补齐|恢复|撤销)|身份证|入住人|订单身份', text):
        return False
    previous = await previous_reply(db, cycle, actor, context_run_id)
    if context_run_id and (previous is None or previous.tool != 'financial_case'):
        return False
    is_case_context = previous is not None and previous.tool == 'financial_case'
    financial_topic = re.search(r'银行|流水|记账|利润|会计|资金|现金流|收支|资本|股东|借款|开业|核对这些资料|生成.*报告', text)
    followup = re.search(r'这些|这个|那些|刚才|一共|合计|总共|多少|保洁|洗涤|工资|水电|水费|电费|物业|承担|支付|收款|收入|支出|费用|确认|执行|取消|分类|归类|更正|改成|月份|月底|月初|导出|报告|凭证|证据|明细|原始|准确|为什么|怎么算|怎么算的|继续|再看|核对|层|排除|只看', text)
    if not financial_topic and not (is_case_context and followup):
        return False
    if is_case_context and previous.facts.get('proposal_kind') == 'business_correction':
        return True
    if is_case_context and previous.facts.get('ledger_scope'):
        return True
    return await _service().has_sources(db, cycle.cycle_id)
