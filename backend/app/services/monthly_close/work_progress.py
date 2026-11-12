"""Live, read-only progress for daily cleaning records and their stayover fees."""
from hashlib import sha256
from sqlalchemy import select
from sqlalchemy.orm import undefer
from app.models.monthly_close import MonthlyCloseDocument
from app.services.monthly_close.cleaning_work_log import parse_work_document, compare_cleaning_work_log
from app.services.monthly_close.work_record_fees import plan_work_fees
from app.services.monthly_close.service_statement import ServiceStatementError
from app.services.billing_recon.parser import BillParseError


async def current_work_progress(db, cycle, document_ids):
    if not document_ids:
        return {}
    documents = await db.scalars(select(MonthlyCloseDocument).where(
        MonthlyCloseDocument.cycle_id == cycle.cycle_id,
        MonthlyCloseDocument.document_id.in_(document_ids),
        MonthlyCloseDocument.is_active.is_(True),
        MonthlyCloseDocument.source_type == "cleaning_statement",
    ).options(undefer(MonthlyCloseDocument.content)))
    result = {}
    for document in documents:
        if sha256(document.content).hexdigest() != document.sha256:
            continue
        try:
            entries = parse_work_document(document, cycle.billing_month)
            if entries is None:
                continue
            comparison = await compare_cleaning_work_log(db, entries, cycle.billing_month,
                                                         document.document_id, source_scope=True)
            if comparison.differences:
                result[document.document_id] = "records_pending"
            else:
                fees = await plan_work_fees(db, document, cycle.billing_month)
                result[document.document_id] = "fees_pending" if fees["actions"] or fees["unresolved"] else "checked"
        except (ServiceStatementError, BillParseError, ValueError):
            # Unreadable or oversized evidence stays unresolved without breaking
            # access to the rest of the month's files and saved bills.
            continue
    return result
