"""Authorized read snapshots and durable formatting turns; never financial writes."""
from datetime import datetime, timezone
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select

from app.models.monthly_close import MonthlyCloseDocument
from app.models.monthly_close_control import MonthlyCloseRun, MonthlyCloseMessage
from app.services.financial_case.chat import previous_reply
from app.services.financial_case.service import require_admin, validate_attachments, actor_id
from app.services.monthly_close.workflow import MonthlyCloseConflict
from app.services.monthly_close.result_output import (
    FormattedResultFacts, ResultOutput, build_document, output_request, present_document, unsupported_output_request, scope_counted_document,
)


def _references(value, key):
    if isinstance(value, dict):
        return ({value[key]} if isinstance(value.get(key), str) else set()) | set().union(*(_references(v, key) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_references(v, key) for v in value))
    return set()


async def source_reply(db, cycle, actor, run_id=None):
    await require_admin(db, actor)
    reply = await previous_reply(db, cycle, actor, run_id)
    if reply is None or reply.intent not in {'read_query', 'action_plan', 'action_result'}:
        raise LookupError('没有找到可导出的当前月份查询。请先查账，再指定输出格式。')
    # Formatting of formatting refers directly to the original, never an unbounded chain.
    if reply.tool == 'formatted_result':
        reply = await previous_reply(db, cycle, actor, reply.facts['source_run_id'])
    if reply is None or reply.facts.get('billing_month') != cycle.billing_month or reply.tool == 'formatted_result':
        raise LookupError('原查询已不可用，请重新查询后输出。')
    docs = _references(reply.facts, 'document_id')
    if docs:
        active = set(await db.scalars(select(MonthlyCloseDocument.document_id).where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id,
            MonthlyCloseDocument.document_id.in_(docs), MonthlyCloseDocument.is_active.is_(True))))
        if active != docs:
            raise LookupError('原查询引用的文件已撤回或不可用，请重新查询后输出。')
    if reply.tool == 'financial_case':
        try:
            await validate_attachments(db, cycle.cycle_id, [s['source_id'] for s in reply.facts.get('sources', [])])
        except ValueError as exc:
            raise LookupError('原查询引用的财务原件已不可用，请重新查询。') from exc
    return reply


async def export_document(db, cycle, actor, run_id):
    reply = await source_reply(db, cycle, actor, run_id)
    requested = await previous_reply(db, cycle, actor, run_id)
    run = await db.get(MonthlyCloseRun, reply.run_id)
    facts = dict(reply.facts)
    if reply.tool == 'financial_case' and facts.get('report_snapshot'):
        from app.services.financial_case.service import load_sources, report_snapshot
        report, snapshot = await report_snapshot(db, cycle, await load_sources(db, cycle.cycle_id), facts['report_months'])
        if snapshot != facts['report_snapshot']:
            raise MonthlyCloseConflict('report_evidence_changed', '报告依据已经变化，请重新生成报告后导出。')
        if len(report['issues']) > 5000:
            raise ValueError('报告超过 5000 项，请缩小月份范围后导出。')
        facts['issues'] = report['issues']
        reply = reply.model_copy(update={'facts': facts})
    if reply.tool == 'review_month' and facts.get('detail_evidence_hash') and not facts.get('selected_ordinal'):
        from app.services.monthly_close.progress_details import read_progress_snapshot
        if facts.get('detail_total', 0) > 5000:
            raise ValueError('查询超过 5000 项，请缩小范围后导出。')
        documents = {r['document_id'] for r in facts.get('detail_items', []) if r.get('document_id')}
        _, complete = await read_progress_snapshot(db, cycle, actor_role='admin', actor=actor,
            step_key=facts['focus'], document_id=next(iter(documents)) if len(documents) == 1 else None,
            expected_evidence_hash=facts['detail_evidence_hash'])
        facts['detail_items'] = complete
        reply = reply.model_copy(update={'facts': facts})
    document = build_document(reply, run.started_at.isoformat())
    if reply.tool == 'financial_case' and facts.get('report_snapshot'):
        from app.services.financial_case.reporting import _KIND_LABELS, _CATEGORY_LABELS
        from app.services.monthly_close.result_output import ResultSection
        if len(report['details']) > 5000:
            raise ValueError('来源明细超过 5000 项，请缩小范围后导出。')
        names = {s['source_id']: s['filename'] for s in report['source_inventory']}
        document.sections.append(ResultSection(title='完整来源明细', columns=['日期', '业务月份', '资料类型', '分类', '金额（元）', '确认状态', '来源'], rows=[
            [r.get('date') or '待核实', r.get('business_month') or '待核实', _KIND_LABELS.get(r.get('kind'), '来源资料'),
             _CATEGORY_LABELS.get(r.get('category'), '待分类'), r.get('amount'), '已确认解释' if r.get('confirmed') else '待核实',
             '；'.join(f"{names.get(ref.get('source_id'), '原件')} · {ref.get('sheet') or '原表'} 第 {ref.get('row1based') or '待核实'} 行" for ref in r.get('lineage', []))[:2000]]
            for r in report['details']], note='来源证据逐项保留；银行、平台与供应方金额可能重叠，不可直接相加。打扫次数不推导费用。'))
        document.caveats.append(f"已保留全部 {len(report['issues'])} 项报告核对提示、{len(report['details'])} 条来源明细。")
    if requested and requested.output:
        document.summary = requested.output.document.summary
        if requested.tool == 'formatted_result':
            # The signed formatting snapshot may select just one of several
            # original groups. Preserve that selection through every reformat.
            titles = {section.title for section in requested.output.document.sections}
            document.sections = [section for section in document.sections if section.title in titles]
    document.caveats.append(f'查询编号：{reply.run_id}')
    return document


async def answer_formatted(db, cycle, actor, text, context_run_id):
    from app.services.monthly_close.assistant import (
        AssistantReply, _ensure_conversation, _complete_run, _stable_id, redact_text,
    )
    await require_admin(db, actor)
    # Resolve before storing this turn. An invalid explicit reference never falls back.
    try:
        if unsupported_output_request(text):
            raise LookupError('当前支持 Excel、Word、PDF、CSV、JSON、Markdown、HTML 和 TXT；你指定的其他文件格式暂未生成。你可以重新查询，并指定这些格式之一。')
        source = await source_reply(db, cycle, actor, context_run_id)
        requested = await previous_reply(db, cycle, actor, context_run_id)
        document = await export_document(db, cycle, actor, requested.run_id if requested else source.run_id)
        document = scope_counted_document(document, text)
        request = output_request(text)
        document = present_document(document, request, text)
        facts = FormattedResultFacts(billing_month=cycle.billing_month, request_text=redact_text(text),
            source_run_id=source.run_id, document=document, presentation=request).model_dump(mode='json')
        message = '已按你的要求整理本次查询结果。可以在下方查看，并选择文件格式下载；下载不会修改账目。'
        intent, tool = 'read_query', 'formatted_result'
        output = ResultOutput(document=document, presentation=request)
    except (LookupError, MonthlyCloseConflict) as exc:
        facts, message, intent, tool, output = {}, str(exc), 'clarification', None, None
    except ValueError:
        facts, message, intent, tool, output = {}, '这份结果超出当前整理范围，请缩小查询范围后重新输出。', 'clarification', None, None
    await require_admin(db, actor)
    conversation_id = _stable_id('MCCV-', f'{cycle.cycle_id}:finance')
    await _ensure_conversation(db, conversation_id=conversation_id, cycle_id=cycle.cycle_id,
                               scope='finance', actor_id=actor_id(actor))
    run_id = 'MCR-' + uuid4().hex[:20].upper()
    run = MonthlyCloseRun(run_id=run_id, cycle_id=cycle.cycle_id, conversation_id=conversation_id,
        trigger_type='user_message', actor_type='user', actor_id=actor_id(actor),
        permission_snapshot={'role': 'admin'}, status='running',
        prompt_version='result-output/v1', tool_manifest_version='result-output/v1',
        input_hash=sha256(redact_text(text).encode()).hexdigest(), started_at=datetime.now(timezone.utc))
    db.add(run)
    await db.flush()
    db.add(MonthlyCloseMessage(message_id='MCM-'+uuid4().hex[:20].upper(), cycle_id=cycle.cycle_id,
        conversation_id=conversation_id, run_id=run_id, role='user', content_redacted=redact_text(text),
        attachments=[], visibility_scope='finance', created_by=actor_id(actor)))
    reply = AssistantReply(message=message, intent=intent, tool=tool, facts=facts, output=output,
        recommended_action={}, narration_degraded=False, conversation_id=conversation_id, run_id=run_id)
    result, _ = await _complete_run(db, run_id, reply=reply,
        status='succeeded' if tool else 'waiting_user', error_code=None, error_detail_redacted=None)
    return result
