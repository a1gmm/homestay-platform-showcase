"""Refresh only derived recognition after an explicit administrator retry."""
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.models.financial_case import FinancialCaseSource
from app.models.monthly_close import MonthlyCloseDocument
from app.services.audit import log_action_tx
from app.services.monthly_close.financial_lock import acquire_month_financial_lock
from app.services.monthly_close.workflow import MonthlyCloseConflict, require_cycle_writable


async def refresh_cleaning_recognition(db, cycle, document, actor):
    from app.services.financial_case.service import require_admin, actor_id
    from app.services.financial_case.source_parsers import parse_financial_workbook
    from app.services.monthly_close.service_reconciliation import process_service_document

    await require_admin(db, actor)
    # A completed month stays read-only, including its derived evidence.
    if cycle.status == 'completed':
        return
    await acquire_month_financial_lock(db, cycle.billing_month)
    await require_cycle_writable(db, cycle)
    await require_admin(db, actor, lock=True)
    document = await db.scalar(select(MonthlyCloseDocument).options(undefer(MonthlyCloseDocument.content)).where(
        MonthlyCloseDocument.document_id == document.document_id,
        MonthlyCloseDocument.cycle_id == cycle.cycle_id,
        MonthlyCloseDocument.is_active.is_(True),
    ).with_for_update().execution_options(populate_existing=True))
    if document is None or document.source_type != 'cleaning_statement':
        return
    if sha256(document.content).hexdigest() != document.sha256:
        raise MonthlyCloseConflict('document_hash_changed', '原文件校验失败，请重新核实文件')
    source = await db.scalar(select(FinancialCaseSource).where(
        FinancialCaseSource.document_id == document.document_id,
        FinancialCaseSource.cycle_id == cycle.cycle_id,
    ).with_for_update().execution_options(populate_existing=True))
    source_changed = False
    # Never reinterpret confirmed facts or decisions during a recognition retry.
    if (source and source.kind == 'cleaning' and source.sha256 == document.sha256
            and not source.parsed.get('facts') and not source.decisions
            and any(issue.get('code') == 'cleaning_parse_error' for issue in source.parsed.get('issues', []))):
        parsed = parse_financial_workbook(document.content, document.filename)
        if parsed and parsed['kind'] == 'cleaning' and parsed['facts']:
            source.parsed = parsed
            source.version += 1
            source_changed = True
    before_status = document.processing_status
    recognition = (document.metadata_ or {}).get('work_log_recognition', {})
    document_changed = before_status == 'rejected' or recognition.get('source_sha256') != document.sha256
    if document_changed:
        await process_service_document(db, document, billing_month=cycle.billing_month)
    if source_changed or document_changed:
        await log_action_tx(db, actor_id(actor), 'monthly_close.cleaning_recognition.refreshed',
                            'monthly_close_document', document.document_id,
                            before_data={'processing_status': before_status},
                            after_data={'processing_status': document.processing_status,
                                        'source_refreshed': source_changed,
                                        'record_count': (document.metadata_ or {}).get('work_log_recognition', {}).get('record_count', 0)})
        await db.flush()
