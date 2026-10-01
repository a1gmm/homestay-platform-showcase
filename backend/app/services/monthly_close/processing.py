"""Durable intake and processing hand-off for monthly-close workbooks."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from types import SimpleNamespace
from typing import Annotated, Awaitable, Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import undefer

from app.models.monthly_close import (
    MONTHLY_CLOSE_SOURCE_TYPES,
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseInboxItem,
)
from app.models.monthly_close_control import (
    MonthlyCloseDocumentAnalysis,
    MonthlyCloseEvent,
    MonthlyCloseOutbox,
    MonthlyCloseProcessingJob,
)
from app.services.audit import log_action_tx
from app.services.monthly_close.documents import validate_document
from app.services.monthly_close.inbox import MonthlyCloseInboxError, receive_inbox_item
from app.services.monthly_close.permissions import (
    MonthlyCloseSystemAction,
    MonthlyCloseSystemActor,
    assert_monthly_close_system_action_allowed,
)


DEFAULT_CLASSIFIER_VERSION = "monthly-close-source-v1"
DEFAULT_ANALYZER_VERSION = "monthly-close-analysis-v1"
ANALYSIS_RESULT_SCHEMA_VERSION = "monthly-close-analysis-result-v1"
MAX_SAFE_PROCESSING_ATTEMPTS = 3
SAFE_TRANSIENT_PROCESSING_FAILURE = "SAFE_TRANSIENT_PROCESSING_FAILURE"
_PROCESSOR_SYSTEM_ACTOR = MonthlyCloseSystemActor(
    actor_id="monthly_close_processor",
    verified=True,
)


Confidence = Annotated[float, Field(ge=0, le=1)]


class MonthlyCloseAnalysisResult(BaseModel):
    """Validated, row-free analyzer output safe to persist and project."""

    model_config = ConfigDict(extra="forbid")

    result_schema_version: Literal["monthly-close-analysis-result-v1"]
    source_type: str
    row_count: int = Field(ge=0)
    mapping: dict[str, int] | None = None
    field_confidence: dict[str, Confidence] = Field(default_factory=dict)
    needs_confirmation: bool

    @field_validator("source_type")
    @classmethod
    def validate_source_type(cls, value: str) -> str:
        if value not in MONTHLY_CLOSE_SOURCE_TYPES:
            raise ValueError("unsupported monthly-close source type")
        return value

    @field_validator("mapping")
    @classmethod
    def validate_mapping(cls, value: dict[str, int] | None):
        if value is not None and any(index < 0 for index in value.values()):
            raise ValueError("mapping indexes must be non-negative")
        return value


class SafeTransientProcessingError(RuntimeError):
    """An explicitly classified local failure whose operation has no side effect."""


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class DurableReceipt:
    receipt_id: str
    item_id: str
    cycle_id: str
    billing_month: str
    status: str
    processing_status: str
    processing_job_id: str
    document_id: str | None
    public_receipt_code: str | None = None


def durable_receipt_view(receipt: DurableReceipt) -> dict[str, str | None]:
    return {
        "receipt_id": receipt.receipt_id,
        "item_id": receipt.item_id,
        "cycle_id": receipt.cycle_id,
        "billing_month": receipt.billing_month,
        "status": receipt.status,
        "processing_status": receipt.processing_status,
        "processing_job_id": receipt.processing_job_id,
        "document_id": receipt.document_id,
    }


def _request_dedupe_key(request_id: str) -> str:
    digest = sha256(request_id.encode("utf-8")).hexdigest()
    return f"upload-received:{digest}"


def public_receipt_code(
    *, receipt_id: str, link_id: str, request_id: str
) -> str:
    """Create a stable, non-authorizing code for one capability receipt."""
    digest = sha256(f"{receipt_id}:{link_id}:{request_id}".encode()).hexdigest()
    return "UPL-" + digest[:16].upper()


def _normalized_intake_context(
    *,
    origin: str | None,
    intake_link_id: str | None,
    source_type: str | None,
    submitted_label: str | None,
) -> dict[str, str | None]:
    def normalized(value: str | None, *, lowercase: bool = False) -> str | None:
        if value is None:
            return None
        result = " ".join(unicodedata.normalize("NFKC", value).split())
        if lowercase:
            result = result.lower()
        return result or None

    return {
        "origin": normalized(origin, lowercase=True),
        "intake_link_id": normalized(intake_link_id),
        "asserted_source_type": normalized(source_type, lowercase=True),
        "submitted_label": normalized(submitted_label),
    }


async def _next_event_sequence(db: AsyncSession, cycle_id: str) -> int:
    current = await db.scalar(
        select(func.coalesce(func.max(MonthlyCloseEvent.sequence), 0)).where(
            MonthlyCloseEvent.cycle_id == cycle_id
        )
    )
    return int(current or 0) + 1


async def enqueue_processing_job(
    db: AsyncSession,
    receipt_id: str,
    analyzer_version: str = DEFAULT_CLASSIFIER_VERSION,
) -> MonthlyCloseProcessingJob:
    """Create the canonical first classification job and its outbox row."""
    item = await db.get(MonthlyCloseInboxItem, receipt_id)
    if item is None:
        raise ValueError("monthly-close receipt does not exist")
    dedupe_key = f"classify:{item.sha256}:{analyzer_version}"
    existing = await db.scalar(
        select(MonthlyCloseProcessingJob).where(
            MonthlyCloseProcessingJob.cycle_id == item.cycle_id,
            MonthlyCloseProcessingJob.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return existing
    job = MonthlyCloseProcessingJob(
        job_id="MCJ-" + uuid4().hex[:20].upper(),
        cycle_id=item.cycle_id,
        job_type="classify_document",
        job_version=analyzer_version,
        subject_type="monthly_close_inbox",
        subject_id=item.item_id,
        dedupe_key=dedupe_key,
        status="pending",
        actor_type="system",
        payload={"processing_state": "queued"},
    )
    outbox = MonthlyCloseOutbox(
        outbox_id="MCO-" + uuid4().hex[:20].upper(),
        cycle_id=item.cycle_id,
        topic="monthly_close.process_document",
        payload={"job_id": job.job_id},
        dedupe_key=f"processing-job:{job.job_id}",
    )
    db.add_all([job, outbox])
    await db.flush()
    return job


async def complete_classification_manually(
    db: AsyncSession,
    item: MonthlyCloseInboxItem,
    *,
    actor_id: str,
    classification_source: str = "administrator",
) -> MonthlyCloseProcessingJob | None:
    """Fence an obsolete classifier after a newer classification result wins."""
    await db.execute(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == item.cycle_id)
        .with_for_update()
    )
    job = await db.scalar(
        select(MonthlyCloseProcessingJob)
        .where(
            MonthlyCloseProcessingJob.cycle_id == item.cycle_id,
            MonthlyCloseProcessingJob.job_type == "classify_document",
            MonthlyCloseProcessingJob.subject_id == item.item_id,
        )
        .with_for_update()
    )
    if job is None:
        return None
    target_state = "completed" if item.status == "classified" else "needs_review"
    if (
        job.status == "succeeded"
        and job.payload.get("processing_state") == target_state
        and job.payload.get("classification_generation")
        == item.classification_generation
    ):
        return job
    job.status = "succeeded"
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = None
    job.payload = {
        **job.payload,
        "processing_state": target_state,
        "classification_source": classification_source,
        "classification_generation": item.classification_generation,
    }
    db.add(
        MonthlyCloseEvent(
            event_id="MCE-" + uuid4().hex[:20].upper(),
            cycle_id=item.cycle_id,
            sequence=await _next_event_sequence(db, item.cycle_id),
            event_type=(
                "classification_confirmed"
                if target_state == "completed"
                else "classification_needs_review"
            ),
            schema_version="v1",
            payload_redacted={
                "receipt_id": item.item_id,
                "job_id": job.job_id,
                "source_type": item.source_type,
                "actor_id": actor_id,
                "classification_generation": item.classification_generation,
            },
            visibility_scope="finance",
            dedupe_key=(
                f"classification-confirmed:{job.job_id}:"
                f"{item.classification_generation}"
            ),
            occurred_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()
    return job


async def enqueue_analysis_job(
    db: AsyncSession,
    item: MonthlyCloseInboxItem,
    document: MonthlyCloseDocument,
    *,
    analyzer_version: str = DEFAULT_ANALYZER_VERSION,
    result_schema_version: str = ANALYSIS_RESULT_SCHEMA_VERSION,
    mapping_version: str = "unconfirmed",
) -> MonthlyCloseProcessingJob:
    """Queue analysis only for a document whose business classification is confirmed."""
    if item.status != "confirmed" or item.document_id != document.document_id:
        raise ValueError("document classification must be confirmed before analysis")
    version_fingerprint = sha256(
        f"{analyzer_version}|{result_schema_version}|{mapping_version}".encode()
    ).hexdigest()[:24]
    dedupe_key = (
        f"analyze:{document.document_id}:c{item.classification_generation}:"
        f"{version_fingerprint}"
    )
    existing_analysis = await db.scalar(
        select(MonthlyCloseDocumentAnalysis).where(
            MonthlyCloseDocumentAnalysis.cycle_id == item.cycle_id,
            MonthlyCloseDocumentAnalysis.dedupe_key == dedupe_key,
        )
    )
    if existing_analysis is not None:
        existing = await db.get(
            MonthlyCloseProcessingJob, existing_analysis.job_id
        )
        if existing is None:
            raise RuntimeError("analysis lineage is missing its processing job")
        return existing
    generation = int(
        await db.scalar(
            select(
                func.coalesce(func.max(MonthlyCloseDocumentAnalysis.generation), 0)
            ).where(MonthlyCloseDocumentAnalysis.document_id == document.document_id)
        )
        or 0
    ) + 1
    analysis_id = "MCA-" + uuid4().hex[:20].upper()
    job_id = "MCJ-" + uuid4().hex[:20].upper()
    job = MonthlyCloseProcessingJob(
        job_id=job_id,
        cycle_id=item.cycle_id,
        job_type="analyze_document",
        job_version=analyzer_version,
        subject_type="monthly_close_document_analysis",
        subject_id=analysis_id,
        dedupe_key=dedupe_key,
        status="pending",
        actor_type="system",
        payload={
            "processing_state": "queued",
            "receipt_id": item.item_id,
            "document_id": document.document_id,
            "analysis_generation": generation,
            "classification_generation": item.classification_generation,
            "analyzer_version": analyzer_version,
            "result_schema_version": result_schema_version,
            "mapping_version": mapping_version,
        },
    )
    analysis = MonthlyCloseDocumentAnalysis(
        analysis_id=analysis_id,
        cycle_id=item.cycle_id,
        document_id=document.document_id,
        job_id=job_id,
        generation=generation,
        classification_generation=item.classification_generation,
        source_type=document.source_type,
        analyzer_version=analyzer_version,
        result_schema_version=result_schema_version,
        mapping_version=mapping_version,
        dedupe_key=dedupe_key,
        status="queued",
    )
    # The analysis table has a composite (job_id, cycle_id) foreign key.  The
    # objects deliberately have no ORM relationship, so SQLAlchemy cannot
    # derive their INSERT order from object dependencies.  Persist the lineage
    # root first; PostgreSQL otherwise may insert the analysis before the job.
    db.add(job)
    await db.flush()
    db.add_all(
        [
            analysis,
            MonthlyCloseOutbox(
                outbox_id="MCO-" + uuid4().hex[:20].upper(),
                cycle_id=item.cycle_id,
                topic="monthly_close.process_document",
                payload={"job_id": job.job_id},
                dedupe_key=f"processing-job:{job.job_id}",
            ),
            MonthlyCloseEvent(
                event_id="MCE-" + uuid4().hex[:20].upper(),
                cycle_id=item.cycle_id,
                sequence=await _next_event_sequence(db, item.cycle_id),
                event_type="analysis_queued",
                schema_version="v1",
                payload_redacted={
                    "receipt_id": item.item_id,
                    "document_id": document.document_id,
                    "job_id": job.job_id,
                    "analysis_id": analysis_id,
                    "analysis_generation": generation,
                    "classification_generation": item.classification_generation,
                    "analyzer_version": analyzer_version,
                    "result_schema_version": result_schema_version,
                    "mapping_version": mapping_version,
                },
                visibility_scope="finance",
                dedupe_key=f"analysis-queued:{job.job_id}",
                occurred_at=datetime.now(timezone.utc),
            ),
        ]
    )
    await db.flush()
    return job


async def complete_document_analysis_manually(
    db: AsyncSession,
    document: MonthlyCloseDocument,
    *,
    actor_id: str,
    confirmation: dict[str, object],
) -> MonthlyCloseDocumentAnalysis | None:
    """Fence the current automated analysis after an exact manual confirmation."""
    await db.execute(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == document.cycle_id)
        .with_for_update()
    )
    analysis = (
        await db.execute(
            select(MonthlyCloseDocumentAnalysis)
            .where(
                MonthlyCloseDocumentAnalysis.cycle_id == document.cycle_id,
                MonthlyCloseDocumentAnalysis.document_id == document.document_id,
                MonthlyCloseDocumentAnalysis.source_type == document.source_type,
            )
            .order_by(MonthlyCloseDocumentAnalysis.generation.desc())
            .limit(1)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if analysis is None:
        return None
    job = (
        await db.execute(
            select(MonthlyCloseProcessingJob)
            .where(
                MonthlyCloseProcessingJob.cycle_id == document.cycle_id,
                MonthlyCloseProcessingJob.job_id == analysis.job_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    encoded = json.dumps(
        confirmation,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    confirmation_hash = sha256(encoded).hexdigest()
    current_result = analysis.result if isinstance(analysis.result, dict) else {}
    now = datetime.now(timezone.utc)
    analysis.status = "completed"
    analysis.result = {
        **current_result,
        "manual_confirmation": confirmation,
        "manual_confirmation_hash": confirmation_hash,
    }
    analysis.error_code = None
    analysis.manual_action = None
    analysis.completed_at = now
    job.status = "succeeded"
    job.lease_owner = None
    job.lease_expires_at = None
    job.last_error_code = None
    job.payload = {
        **job.payload,
        "processing_state": "completed",
        "confirmed_by": "administrator",
        "manual_confirmation_hash": confirmation_hash,
    }
    event_dedupe_key = (
        f"analysis-manual-confirmed:{analysis.analysis_id}:{confirmation_hash[:24]}"
    )
    existing_event = await db.scalar(
        select(MonthlyCloseEvent.event_id).where(
            MonthlyCloseEvent.cycle_id == document.cycle_id,
            MonthlyCloseEvent.dedupe_key == event_dedupe_key,
        )
    )
    if existing_event is None:
        db.add(
            MonthlyCloseEvent(
                event_id="MCE-" + uuid4().hex[:20].upper(),
                cycle_id=document.cycle_id,
                sequence=await _next_event_sequence(db, document.cycle_id),
                event_type="analysis_confirmed_manually",
                schema_version="v1",
                payload_redacted={
                    "document_id": document.document_id,
                    "analysis_id": analysis.analysis_id,
                    "job_id": job.job_id,
                    "source_type": document.source_type,
                    "analysis_generation": analysis.generation,
                    "classification_generation": analysis.classification_generation,
                    "actor_id": actor_id,
                    "manual_confirmation_hash": confirmation_hash,
                },
                visibility_scope="finance",
                dedupe_key=event_dedupe_key,
                occurred_at=now,
            )
        )
    await db.flush()
    return analysis


async def claim_processing_job(
    db: AsyncSession,
    job_id: str,
    *,
    worker_id: str,
    now: datetime | None = None,
    lease_duration: timedelta = timedelta(minutes=15),
) -> MonthlyCloseProcessingJob | None:
    """Atomically claim a due first attempt or a proven failed-safe retry."""
    if not worker_id or len(worker_id) > 80:
        raise ValueError("worker_id must be between 1 and 80 characters")
    if lease_duration <= timedelta(0):
        raise ValueError("lease_duration must be positive")
    current = _utc(now or datetime.now(timezone.utc))
    job = (
        await db.execute(
            select(MonthlyCloseProcessingJob)
            .where(MonthlyCloseProcessingJob.job_id == job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if job is None:
        return None
    available = _utc(job.available_at)
    pending = job.status == "pending" and available <= current
    failed_safe = (
        job.status == "failed"
        and job.last_error_code == SAFE_TRANSIENT_PROCESSING_FAILURE
        and available <= current
    )
    if not (pending or failed_safe):
        return None
    assert_monthly_close_system_action_allowed(
        _PROCESSOR_SYSTEM_ACTOR,
        MonthlyCloseSystemAction.classify_safe,
    )
    analysis: MonthlyCloseDocumentAnalysis | None = None
    if job.job_type == "analyze_document":
        analysis = await db.get(
            MonthlyCloseDocumentAnalysis,
            job.subject_id,
            with_for_update=True,
        )
        if analysis is None:
            raise RuntimeError("analysis job is missing its durable analysis record")
    if failed_safe:
        authoritative_failed_safe = analysis is None or (
            analysis.status == "failed_safe"
            and analysis.error_code == SAFE_TRANSIENT_PROCESSING_FAILURE
        )
        rollback_proven = bool(
            authoritative_failed_safe
            and await db.scalar(
                select(MonthlyCloseEvent.event_id).where(
                    MonthlyCloseEvent.cycle_id == job.cycle_id,
                    MonthlyCloseEvent.event_type == "processing_failed_safe",
                    MonthlyCloseEvent.dedupe_key
                    == f"processing-result:{job.job_id}:{job.attempt_count}",
                ).with_for_update()
            )
        )
        assert_monthly_close_system_action_allowed(
            _PROCESSOR_SYSTEM_ACTOR,
            MonthlyCloseSystemAction.retry_failed_safe,
            attempt_state="failed_safe" if authoritative_failed_safe else "unknown",
            rollback_proven=rollback_proven,
        )
    job.status = "leased"
    job.lease_owner = worker_id
    job.lease_expires_at = current + lease_duration
    job.attempt_count += 1
    job.last_error_code = None
    job.payload = {**job.payload, "processing_state": "processing"}
    if analysis is not None:
        analysis.status = "processing"
        analysis.error_code = None
    await db.commit()
    await db.refresh(job)
    return job


async def dispatch_processing_outbox(
    *,
    session_factory: async_sessionmaker | None = None,
    send: Callable[[str], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    batch_size: int = 50,
) -> dict[str, int]:
    """Publish durable job references while retaining failed deliveries."""
    if batch_size < 1 or batch_size > 500:
        raise ValueError("batch_size must be between 1 and 500")
    if session_factory is None:
        from app.core.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    if send is None:
        from app.workers.celery_app import celery_app

        def send(job_id: str) -> None:
            celery_app.send_task(
                "app.workers.monthly_close_tasks.process_monthly_close_job",
                args=[job_id],
                queue="celery",
            )

    current = _utc(now())
    dispatcher_id = "outbox-" + uuid4().hex
    async with session_factory() as claim_db:
        async with claim_db.begin():
            rows = list(
                (
                    await claim_db.scalars(
                        select(MonthlyCloseOutbox)
                        .where(
                            or_(
                                and_(
                                    MonthlyCloseOutbox.status == "pending",
                                    MonthlyCloseOutbox.available_at <= current,
                                ),
                                and_(
                                    MonthlyCloseOutbox.status == "leased",
                                    MonthlyCloseOutbox.lease_expires_at <= current,
                                ),
                            )
                        )
                        .order_by(
                            MonthlyCloseOutbox.available_at,
                            MonthlyCloseOutbox.outbox_id,
                        )
                        .limit(batch_size)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            for row in rows:
                row.status = "leased"
                row.lease_owner = dispatcher_id
                row.lease_expires_at = current + timedelta(minutes=2)
                row.attempt_count += 1
            claimed = [
                (row.outbox_id, str(row.payload["job_id"]), row.attempt_count)
                for row in rows
            ]

    delivered = 0
    failed = 0
    for outbox_id, job_id, attempt_count in claimed:
        try:
            send(job_id)
        except Exception as exc:
            failed += 1
            async with session_factory() as failure_db:
                async with failure_db.begin():
                    row = await failure_db.get(
                        MonthlyCloseOutbox, outbox_id, with_for_update=True
                    )
                    if (
                        row is not None
                        and row.status == "leased"
                        and row.lease_owner == dispatcher_id
                    ):
                        row.status = "pending"
                        row.lease_owner = None
                        row.lease_expires_at = None
                        row.available_at = current + timedelta(
                            seconds=min(2 ** max(0, attempt_count - 1), 300)
                        )
                        row.last_error_code = exc.__class__.__name__
        else:
            delivered += 1
            async with session_factory() as success_db:
                async with success_db.begin():
                    row = await success_db.get(
                        MonthlyCloseOutbox, outbox_id, with_for_update=True
                    )
                    if (
                        row is not None
                        and row.status == "leased"
                        and row.lease_owner == dispatcher_id
                    ):
                        row.status = "delivered"
                        row.lease_owner = None
                        row.lease_expires_at = None
                        row.last_error_code = None
                        row.delivered_at = current
    return {"claimed": len(claimed), "delivered": delivered, "failed": failed}


async def _processing_state(
    session_factory: async_sessionmaker,
    job_id: str,
) -> str:
    async with session_factory() as db:
        job = await db.get(MonthlyCloseProcessingJob, job_id)
        if job is None:
            return "failed_safe"
        return str(job.payload.get("processing_state", "queued"))


async def _finalize_classification(
    session_factory: async_sessionmaker,
    *,
    job_id: str,
    worker_id: str,
    attempt_count: int,
    classification,
    needs_review_error: bool,
    occurred_at: datetime,
) -> str:
    async with session_factory() as db:
        async with db.begin():
            cycle_id = await db.scalar(
                select(MonthlyCloseProcessingJob.cycle_id).where(
                    MonthlyCloseProcessingJob.job_id == job_id
                )
            )
            if cycle_id is None:
                return "failed_safe"
            await db.execute(
                select(MonthlyCloseCycle)
                .where(MonthlyCloseCycle.cycle_id == cycle_id)
                .with_for_update()
            )
            job = (
                await db.execute(
                    select(MonthlyCloseProcessingJob)
                    .where(MonthlyCloseProcessingJob.job_id == job_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                return "failed_safe"
            if (
                job.status != "leased"
                or job.lease_owner != worker_id
                or job.attempt_count != attempt_count
            ):
                return str(job.payload.get("processing_state", "queued"))
            item = (
                await db.execute(
                    select(MonthlyCloseInboxItem)
                    .options(undefer(MonthlyCloseInboxItem.content))
                    .where(MonthlyCloseInboxItem.item_id == job.subject_id)
                    .with_for_update()
                )
            ).scalar_one()
            if needs_review_error:
                processing_state = "needs_review"
                item.status = "needs_review"
                item.last_error = (
                    "自动识别暂不可用，请手动选择资料类型；"
                    "原文件已保存，无需重新上传"
                )
                job.status = "failed"
                job.last_error_code = "MODEL_OR_ANALYZER_UNAVAILABLE"
                event_type = "classification_needs_review"
                event_payload = {
                    "receipt_id": item.item_id,
                    "job_id": job.job_id,
                    "error_code": job.last_error_code,
                    "classification_generation": item.classification_generation,
                }
            else:
                item.source_type = classification.source_type
                item.confidence = Decimal(str(classification.confidence)).quantize(
                    Decimal("0.0001")
                )
                item.suggested_by = classification.suggested_by
                item.classification_reason = classification.reason
                item.status = (
                    "classified" if classification.confidence >= 0.8 else "needs_review"
                )
                item.last_error = None
                processing_state = (
                    "completed" if item.status == "classified" else "needs_review"
                )
                job.status = "succeeded"
                job.last_error_code = None
                event_type = (
                    "classification_completed"
                    if item.status == "classified"
                    else "classification_needs_review"
                )
                event_payload = {
                    "receipt_id": item.item_id,
                    "job_id": job.job_id,
                    "source_type": item.source_type,
                    "confidence": str(item.confidence),
                    "classification_generation": item.classification_generation,
                }
            job.lease_owner = None
            job.lease_expires_at = None
            job.payload = {
                **job.payload,
                "processing_state": processing_state,
                "classification_generation": item.classification_generation,
            }
            db.add(
                MonthlyCloseEvent(
                    event_id="MCE-" + uuid4().hex[:20].upper(),
                    cycle_id=job.cycle_id,
                    sequence=await _next_event_sequence(db, job.cycle_id),
                    event_type=event_type,
                    schema_version="v1",
                    payload_redacted=event_payload,
                    visibility_scope="finance",
                    dedupe_key=f"processing-result:{job.job_id}:{attempt_count}",
                    occurred_at=occurred_at,
                )
            )
        return processing_state


async def _finalize_failed_safe(
    session_factory: async_sessionmaker,
    *,
    job_id: str,
    worker_id: str,
    attempt_count: int,
    occurred_at: datetime,
) -> str:
    async with session_factory() as db:
        async with db.begin():
            cycle_id = await db.scalar(
                select(MonthlyCloseProcessingJob.cycle_id).where(
                    MonthlyCloseProcessingJob.job_id == job_id
                )
            )
            if cycle_id is None:
                return "failed_safe"
            await db.execute(
                select(MonthlyCloseCycle)
                .where(MonthlyCloseCycle.cycle_id == cycle_id)
                .with_for_update()
            )
            job = (
                await db.execute(
                    select(MonthlyCloseProcessingJob)
                    .where(MonthlyCloseProcessingJob.job_id == job_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                return "failed_safe"
            if (
                job.status != "leased"
                or job.lease_owner != worker_id
                or job.attempt_count != attempt_count
            ):
                return str(job.payload.get("processing_state", "queued"))
            job.status = (
                "dead_letter"
                if attempt_count >= MAX_SAFE_PROCESSING_ATTEMPTS
                else "failed"
            )
            job.lease_owner = None
            job.lease_expires_at = None
            job.available_at = occurred_at + timedelta(minutes=1)
            job.last_error_code = SAFE_TRANSIENT_PROCESSING_FAILURE
            job.payload = {**job.payload, "processing_state": "failed_safe"}
            if job.job_type == "analyze_document":
                analysis = await db.get(
                    MonthlyCloseDocumentAnalysis,
                    job.subject_id,
                    with_for_update=True,
                )
                if analysis is None:
                    raise RuntimeError(
                        "analysis job is missing its durable analysis record"
                    )
                analysis.status = "failed_safe"
                analysis.error_code = job.last_error_code
                analysis.manual_action = None
            if job.status != "dead_letter":
                outbox = await db.scalar(
                    select(MonthlyCloseOutbox)
                    .where(
                        MonthlyCloseOutbox.dedupe_key
                        == f"processing-job:{job.job_id}"
                    )
                    .with_for_update()
                )
                if outbox is not None:
                    outbox.status = "pending"
                    outbox.lease_owner = None
                    outbox.lease_expires_at = None
                    outbox.available_at = job.available_at
                    outbox.delivered_at = None
            db.add(
                MonthlyCloseEvent(
                    event_id="MCE-" + uuid4().hex[:20].upper(),
                    cycle_id=job.cycle_id,
                    sequence=await _next_event_sequence(db, job.cycle_id),
                    event_type="processing_failed_safe",
                    schema_version="v1",
                    payload_redacted={
                        "job_id": job.job_id,
                        "attempt_count": attempt_count,
                        "retryable": job.status != "dead_letter",
                        "error_code": job.last_error_code,
                    },
                    visibility_scope="finance",
                    dedupe_key=f"processing-result:{job.job_id}:{attempt_count}",
                    occurred_at=occurred_at,
                )
            )
        return "failed_safe"


async def _finalize_analysis(
    session_factory: async_sessionmaker,
    *,
    job_id: str,
    worker_id: str,
    attempt_count: int,
    needs_review_error: bool,
    analysis_result: MonthlyCloseAnalysisResult | None,
    occurred_at: datetime,
) -> str:
    async with session_factory() as db:
        async with db.begin():
            cycle_id = await db.scalar(
                select(MonthlyCloseProcessingJob.cycle_id).where(
                    MonthlyCloseProcessingJob.job_id == job_id
                )
            )
            if cycle_id is None:
                return "failed_safe"
            await db.execute(
                select(MonthlyCloseCycle)
                .where(MonthlyCloseCycle.cycle_id == cycle_id)
                .with_for_update()
            )
            job = (
                await db.execute(
                    select(MonthlyCloseProcessingJob)
                    .where(MonthlyCloseProcessingJob.job_id == job_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if job is None:
                return "failed_safe"
            if (
                job.status != "leased"
                or job.lease_owner != worker_id
                or job.attempt_count != attempt_count
            ):
                return str(job.payload.get("processing_state", "queued"))
            analysis = (
                await db.execute(
                    select(MonthlyCloseDocumentAnalysis)
                    .where(
                        MonthlyCloseDocumentAnalysis.analysis_id == job.subject_id
                    )
                    .with_for_update()
                )
            ).scalar_one()
            document = (
                await db.execute(
                    select(MonthlyCloseDocument)
                    .where(
                        MonthlyCloseDocument.document_id == analysis.document_id
                    )
                    .with_for_update()
                )
            ).scalar_one()
            latest_generation = await db.scalar(
                select(func.max(MonthlyCloseDocumentAnalysis.generation)).where(
                    MonthlyCloseDocumentAnalysis.document_id == analysis.document_id
                )
            )
            owns_current_document_state = (
                analysis.generation == latest_generation
                and document.source_type == analysis.source_type
            )
            confirmation_required = bool(
                analysis_result is not None and analysis_result.needs_confirmation
            )
            if needs_review_error or confirmation_required:
                processing_state = "needs_review"
                job.status = "failed" if needs_review_error else "succeeded"
                job.last_error_code = (
                    "MODEL_OR_ANALYZER_UNAVAILABLE"
                    if needs_review_error
                    else "ANALYSIS_CONFIRMATION_REQUIRED"
                )
                analysis.status = "needs_review"
                analysis.result = (
                    analysis_result.model_dump(mode="json")
                    if analysis_result is not None
                    else None
                )
                analysis.error_code = job.last_error_code
                analysis.manual_action = (
                    "候选结果已保存，请手动确认字段映射；原文件无需重新上传"
                    if confirmation_required
                    else "请手动确认字段映射；原文件已保存，无需重新上传"
                )
                analysis.completed_at = occurred_at
                if owns_current_document_state:
                    document.processing_error = (
                        analysis.manual_action
                        if confirmation_required
                        else "自动分析暂不可用，请手动确认字段映射；"
                        "原文件已保存，无需重新上传"
                    )
                event_type = "analysis_needs_review"
            else:
                if analysis_result is None:
                    raise RuntimeError("completed analysis requires a validated result")
                processing_state = "completed"
                job.status = "succeeded"
                job.last_error_code = None
                analysis.status = "completed"
                analysis.result = analysis_result.model_dump(mode="json")
                analysis.error_code = None
                analysis.manual_action = None
                analysis.completed_at = occurred_at
                if owns_current_document_state:
                    document.processing_error = None
                event_type = "analysis_completed"
            job.lease_owner = None
            job.lease_expires_at = None
            job.payload = {**job.payload, "processing_state": processing_state}
            db.add(
                MonthlyCloseEvent(
                    event_id="MCE-" + uuid4().hex[:20].upper(),
                    cycle_id=job.cycle_id,
                    sequence=await _next_event_sequence(db, job.cycle_id),
                    event_type=event_type,
                    schema_version="v1",
                    payload_redacted={
                        "receipt_id": job.payload.get("receipt_id"),
                        "document_id": document.document_id,
                        "analysis_id": analysis.analysis_id,
                        "job_id": job.job_id,
                        "source_type": analysis.source_type,
                        "analysis_generation": analysis.generation,
                        "classification_generation": (
                            analysis.classification_generation
                        ),
                        "analyzer_version": analysis.analyzer_version,
                        "result_schema_version": analysis.result_schema_version,
                        "mapping_version": analysis.mapping_version,
                        "error_code": job.last_error_code,
                    },
                    visibility_scope="finance",
                    dedupe_key=f"processing-result:{job.job_id}:{attempt_count}",
                    occurred_at=occurred_at,
                )
            )
        return processing_state


async def process_monthly_close_job_async(
    job_id: str,
    *,
    session_factory: async_sessionmaker | None = None,
    worker_id: str | None = None,
    classifier: Callable[[bytes, str], Awaitable[object]] | None = None,
    analyzer: Callable[[bytes, str, str], Awaitable[object]] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, str]:
    """Run one leased document job; duplicate delivery is a terminal replay."""
    if session_factory is None:
        from app.core.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    if classifier is None:
        from app.services.monthly_close.source_classifier import (
            classify_monthly_close_source,
        )

        classifier = classify_monthly_close_source
    owner = worker_id or "monthly-close-" + uuid4().hex
    current = _utc(now())
    async with session_factory() as claim_db:
        claimed = await claim_processing_job(
            claim_db,
            job_id,
            worker_id=owner,
            now=current,
        )
    if claimed is None:
        return {
            "job_id": job_id,
            "processing_status": await _processing_state(session_factory, job_id),
        }
    attempt_count = claimed.attempt_count
    if claimed.job_type == "analyze_document":
        async with session_factory() as read_db:
            analysis = await read_db.get(
                MonthlyCloseDocumentAnalysis, claimed.subject_id
            )
            if analysis is None:
                raise RuntimeError(
                    "analysis job is missing its durable analysis record"
                )
            document = (
                await read_db.execute(
                    select(MonthlyCloseDocument)
                    .options(undefer(MonthlyCloseDocument.content))
                    .where(
                        MonthlyCloseDocument.document_id == analysis.document_id
                    )
                )
            ).scalar_one()
            data = document.content
            filename = document.filename
            source_type = analysis.source_type
        if analyzer is None:
            async def analyzer(
                _data: bytes, _filename: str, _source_type: str
            ) -> object:
                raise RuntimeError("manual field mapping is required")
        try:
            raw_result = await analyzer(data, filename, source_type)
            analysis_result = MonthlyCloseAnalysisResult.model_validate(raw_result)
            if analysis_result.source_type != source_type:
                raise ValueError(
                    "analysis result source type does not match the document"
                )
        except SafeTransientProcessingError:
            processing_state = await _finalize_failed_safe(
                session_factory,
                job_id=job_id,
                worker_id=owner,
                attempt_count=attempt_count,
                occurred_at=current,
            )
        except Exception:
            processing_state = await _finalize_analysis(
                session_factory,
                job_id=job_id,
                worker_id=owner,
                attempt_count=attempt_count,
                needs_review_error=True,
                analysis_result=None,
                occurred_at=current,
            )
        else:
            processing_state = await _finalize_analysis(
                session_factory,
                job_id=job_id,
                worker_id=owner,
                attempt_count=attempt_count,
                needs_review_error=False,
                analysis_result=analysis_result,
                occurred_at=current,
            )
        return {"job_id": job_id, "processing_status": processing_state}
    if claimed.job_type != "classify_document":
        processing_state = await _finalize_failed_safe(
            session_factory,
            job_id=job_id,
            worker_id=owner,
            attempt_count=attempt_count,
            occurred_at=current,
        )
        return {"job_id": job_id, "processing_status": processing_state}
    async with session_factory() as read_db:
        item = (
            await read_db.execute(
                select(MonthlyCloseInboxItem)
                .options(undefer(MonthlyCloseInboxItem.content))
                .where(MonthlyCloseInboxItem.item_id == claimed.subject_id)
            )
        ).scalar_one()
        data = item.content
        filename = item.filename
        preset = (
            item.status == "classified"
            and item.source_type is not None
            and item.suggested_by in {"external_link", "administrator"}
        )
        if preset:
            preset_classification = SimpleNamespace(
                source_type=item.source_type,
                confidence=float(item.confidence or Decimal("1")),
                suggested_by=item.suggested_by,
                reason=item.classification_reason or "资料类型已由受控入口提供",
            )
        else:
            preset_classification = None
    if preset_classification is not None:
        processing_state = await _finalize_classification(
            session_factory,
            job_id=job_id,
            worker_id=owner,
            attempt_count=attempt_count,
            classification=preset_classification,
            needs_review_error=False,
            occurred_at=current,
        )
        return {"job_id": job_id, "processing_status": processing_state}
    try:
        classification = await classifier(data, filename)
    except SafeTransientProcessingError:
        processing_state = await _finalize_failed_safe(
            session_factory,
            job_id=job_id,
            worker_id=owner,
            attempt_count=attempt_count,
            occurred_at=current,
        )
    except Exception:
        processing_state = await _finalize_classification(
            session_factory,
            job_id=job_id,
            worker_id=owner,
            attempt_count=attempt_count,
            classification=None,
            needs_review_error=True,
            occurred_at=current,
        )
    else:
        processing_state = await _finalize_classification(
            session_factory,
            job_id=job_id,
            worker_id=owner,
            attempt_count=attempt_count,
            classification=classification,
            needs_review_error=False,
            occurred_at=current,
        )
    return {"job_id": job_id, "processing_status": processing_state}


async def receive_monthly_close_file(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    request_id: str,
    filename: str,
    mime_type: str,
    data: bytes,
    origin: str,
    user_id: str | None,
    analyzer_version: str = DEFAULT_CLASSIFIER_VERSION,
    source_type: str | None = None,
    submitted_label: str | None = None,
    intake_link_id: str | None = None,
) -> DurableReceipt:
    """Persist bytes, canonical receipt, first job, outbox, audit, and event."""
    normalized_request_id = request_id.strip()
    if not normalized_request_id or len(normalized_request_id) > 1024:
        raise MonthlyCloseInboxError(
            "invalid_monthly_close_request_id",
            "Idempotency-Key 必须为 1 到 1024 个字符",
            422,
        )
    fingerprint = validate_document(data, filename)
    try:
        locked_cycle = (
            await db.execute(
                select(MonthlyCloseCycle)
                .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
                .with_for_update()
            )
        ).scalar_one()
        event_key = _request_dedupe_key(normalized_request_id)
        replay = await db.scalar(
            select(MonthlyCloseEvent).where(
                MonthlyCloseEvent.cycle_id == locked_cycle.cycle_id,
                MonthlyCloseEvent.dedupe_key == event_key,
            )
        )
        if replay is not None:
            if replay.payload_redacted.get("sha256") != fingerprint:
                raise MonthlyCloseInboxError(
                    "monthly_close_request_conflict",
                    "同一 Idempotency-Key 已用于其他文件",
                    409,
                )
            request_context = _normalized_intake_context(
                origin=origin,
                intake_link_id=intake_link_id,
                source_type=source_type,
                submitted_label=submitted_label,
            )
            original_context = _normalized_intake_context(
                origin=replay.payload_redacted.get("origin"),
                intake_link_id=replay.payload_redacted.get("intake_link_id"),
                source_type=replay.payload_redacted.get("asserted_source_type"),
                submitted_label=replay.payload_redacted.get("submitted_label"),
            )
            if request_context != original_context:
                raise MonthlyCloseInboxError(
                    "monthly_close_request_context_conflict",
                    "同一 Idempotency-Key 已用于不同的受控收件上下文",
                    409,
                )
            item_id = str(replay.payload_redacted["receipt_id"])
            item = await db.get(MonthlyCloseInboxItem, item_id)
            job = await db.get(
                MonthlyCloseProcessingJob,
                str(replay.payload_redacted["processing_job_id"]),
            )
            if item is None or job is None:
                raise RuntimeError("durable receipt lineage is incomplete")
            return DurableReceipt(
                receipt_id=item.item_id,
                item_id=item.item_id,
                cycle_id=locked_cycle.cycle_id,
                billing_month=locked_cycle.billing_month,
                status="stored",
                processing_status=str(job.payload.get("processing_state", "queued")),
                processing_job_id=job.job_id,
                document_id=item.document_id,
                public_receipt_code=(
                    public_receipt_code(
                        receipt_id=item.item_id,
                        link_id=intake_link_id,
                        request_id=normalized_request_id,
                    )
                    if intake_link_id is not None
                    else None
                ),
            )

        canonical_before = await db.scalar(
            select(MonthlyCloseInboxItem).where(
                MonthlyCloseInboxItem.cycle_id == locked_cycle.cycle_id,
                MonthlyCloseInboxItem.sha256 == fingerprint,
            )
        )
        canonical_source_type = (
            canonical_before.source_type if canonical_before is not None else None
        )
        item = await receive_inbox_item(
            db,
            locked_cycle,
            filename=filename,
            mime_type=mime_type,
            data=data,
            origin=origin,
            user_id=user_id,
            source_type=source_type,
            submitted_label=submitted_label,
            commit=False,
        )
        provenance_conflict = bool(
            canonical_before is not None
            and source_type is not None
            and canonical_source_type is not None
            and canonical_source_type != source_type
        )
        if (
            canonical_before is not None
            and source_type is not None
            and canonical_source_type is None
        ):
            item.source_type = source_type
            item.confidence = Decimal("1.0000")
            item.suggested_by = "external_link"
            item.classification_reason = (
                f"由“{submitted_label}”上传链接指定资料类型"
                if submitted_label
                else "由受控上传链接指定资料类型"
            )
            item.status = "classified"
        job = await enqueue_processing_job(db, item.item_id, analyzer_version)
        if provenance_conflict:
            item.status = "needs_review"
            item.last_error = (
                "相同原文件的资料类型冲突："
                f"原分类 {canonical_source_type}，本次链接声明 {source_type}；"
                "请管理员确认，无需重新上传"
            )
            job.status = "failed"
            job.last_error_code = "SOURCE_PROVENANCE_CONFLICT"
            job.payload = {
                **job.payload,
                "processing_state": "needs_review",
                "provenance_conflict": True,
            }
        intake_context = _normalized_intake_context(
            origin=origin,
            intake_link_id=intake_link_id,
            source_type=source_type,
            submitted_label=submitted_label,
        )
        capability_receipt_code = (
            public_receipt_code(
                receipt_id=item.item_id,
                link_id=intake_link_id,
                request_id=normalized_request_id,
            )
            if intake_link_id is not None
            else None
        )
        event = MonthlyCloseEvent(
            event_id="MCE-" + uuid4().hex[:20].upper(),
            cycle_id=locked_cycle.cycle_id,
            sequence=await _next_event_sequence(db, locked_cycle.cycle_id),
            event_type="upload_received",
            schema_version="v1",
            payload_redacted={
                "receipt_id": item.item_id,
                "processing_job_id": job.job_id,
                "sha256": item.sha256,
                **intake_context,
                "canonical_source_type": canonical_source_type or item.source_type,
                "provenance_conflict": provenance_conflict,
                "public_receipt_code": capability_receipt_code,
            },
            visibility_scope="finance",
            dedupe_key=event_key,
            occurred_at=datetime.now(timezone.utc),
        )
        db.add(event)
        await log_action_tx(
            db,
            user_id,
            "monthly_close.inbox.upload_received",
            "monthly_close_inbox",
            item.item_id,
            after_data={
                "cycle_id": locked_cycle.cycle_id,
                "billing_month": locked_cycle.billing_month,
                **intake_context,
                "sha256": item.sha256,
                "processing_job_id": job.job_id,
                "canonical_source_type": canonical_source_type or item.source_type,
                "provenance_conflict": provenance_conflict,
                "public_receipt_code": capability_receipt_code,
            },
        )
        await db.commit()
        return DurableReceipt(
            receipt_id=item.item_id,
            item_id=item.item_id,
            cycle_id=locked_cycle.cycle_id,
            billing_month=locked_cycle.billing_month,
            status="stored",
            processing_status=str(job.payload.get("processing_state", "queued")),
            processing_job_id=job.job_id,
            document_id=item.document_id,
            public_receipt_code=capability_receipt_code,
        )
    except Exception:
        await db.rollback()
        raise
