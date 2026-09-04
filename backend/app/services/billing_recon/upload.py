"""Upload preflight and lifecycle helpers for billing reconciliation."""

from __future__ import annotations

import io
import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import PurePosixPath

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recon import ReconBatch
from app.services.billing_recon.analysis import MappingCoordinates
from app.services.billing_recon.parser import BillParseError

_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_MAX_ZIP_MEMBERS = 256
_MAX_MEMBER_SIZE = 32 * 1024 * 1024
_MAX_TOTAL_SIZE = 64 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_REQUIRED_OOXML_MEMBERS = {"[Content_Types].xml", "xl/workbook.xml"}
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_PROCESSING_LEASE_SECONDS = 120


class ProcessingBatchState(str, Enum):
    """Outcome of claiming one durable confirmation identity."""

    acquired = "acquired"
    parsed = "parsed"
    in_flight = "in_flight"


@dataclass(frozen=True)
class ProcessingBatchResult:
    """Keep an active lease distinct from a completed reusable batch."""

    batch: ReconBatch
    state: ProcessingBatchState


def _existing_batch_result(batch: ReconBatch) -> ProcessingBatchResult:
    state = ProcessingBatchState.parsed if batch.status == "parsed" else ProcessingBatchState.in_flight
    return ProcessingBatchResult(batch=batch, state=state)


def _safe_member_name(name: str) -> bool:
    if not name or "\x00" in name or name.startswith(("/", "\\")) or _DRIVE_PREFIX.match(name):
        return False
    normalized = name.replace("\\", "/")
    return ".." not in PurePosixPath(normalized).parts


def _validate_xlsx(data: bytes) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]
            if len(members) > _MAX_ZIP_MEMBERS:
                raise BillParseError("工作簿结构超出安全限制")

            names: set[str] = set()
            total_size = 0
            total_compressed = 0
            for item in members:
                if item.flag_bits & 0x1 or not _safe_member_name(item.filename):
                    raise BillParseError("工作簿结构不安全")
                if item.file_size > _MAX_MEMBER_SIZE:
                    raise BillParseError("工作簿解压尺寸超出安全限制")
                compressed = max(item.compress_size, 1)
                if item.file_size > 1024 and item.file_size / compressed > _MAX_COMPRESSION_RATIO:
                    raise BillParseError("工作簿压缩比超出安全限制")
                total_size += item.file_size
                total_compressed += item.compress_size
                names.add(item.filename.replace("\\", "/"))

            if total_size > _MAX_TOTAL_SIZE:
                raise BillParseError("工作簿解压尺寸超出安全限制")
            if total_size > 1024 and total_size / max(total_compressed, 1) > _MAX_COMPRESSION_RATIO:
                raise BillParseError("工作簿压缩比超出安全限制")
            if not _REQUIRED_OOXML_MEMBERS.issubset(names):
                raise BillParseError("工作簿结构不完整")
    except BillParseError:
        raise
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        raise BillParseError("无法解析 xlsx 文件") from exc


def validate_bill_container(data: bytes, filename: str) -> None:
    """Reject malformed or resource-amplifying Excel containers before parsing."""
    name = (filename or "").lower()
    if name.endswith(".xlsx"):
        _validate_xlsx(data)
        return
    if name.endswith(".xls") and data.startswith(_OLE_SIGNATURE):
        return
    raise BillParseError("请上传有效的 xls/xlsx 账单文件")


def upload_fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def confirmed_mapping_fingerprint(coordinates: MappingCoordinates | None) -> str | None:
    """Hash the canonical confirmed coordinates without retaining workbook contents."""
    if coordinates is None:
        return None
    payload = json.dumps(coordinates.model_dump(mode="json"), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def confirmation_identity(
    fingerprint: str, platform: str, layout_signature: str | None, mapping_fingerprint: str | None,
) -> str:
    """Stable, PII-free identity for one confirmed file/mapping/source combination."""
    payload = "\x1f".join((fingerprint, platform, layout_signature or "", mapping_fingerprint or ""))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _processing_batch_id(identity: str, attempt: int = 0) -> str:
    """Return a bounded, deterministic ID for one confirmation attempt."""
    if attempt == 0:
        # Preserve the original canonical ID so existing failed rows remain
        # reclaimable after this attempt-identity field was introduced.
        return f"RB-CFM-{identity[:24].upper()}"
    suffix = f"-A{attempt}"
    prefix_length = 40 - len("RB-CFM-") - len(suffix)
    return f"RB-CFM-{identity[:prefix_length].upper()}{suffix}"


def _confirmation_attempt_number(batch: ReconBatch, identity: str) -> int:
    """Read a persisted attempt number while remaining compatible with old rows."""
    value = (batch.mapping or {}).get("confirmation_attempt")
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return 0 if batch.batch_id == _processing_batch_id(identity) else -1


async def _confirmation_attempts(
    db: AsyncSession, identity: str,
) -> list[ReconBatch]:
    """Find every durable attempt for an identity, including pre-field rows."""
    rows = await db.execute(
        select(ReconBatch).where(
            or_(
                ReconBatch.batch_id == _processing_batch_id(identity),
                ReconBatch.mapping["confirmation_identity"].as_string() == identity,
            )
        )
    )
    return list(rows.scalars())


def _processing_mapping(
    *,
    fingerprint: str,
    filename: str,
    layout_signature: str | None,
    mapping_fingerprint: str | None,
    identity: str,
    confirmation_attempt: int,
    now: datetime,
    processing_attempt: int = 1,
    existing: dict | None = None,
) -> dict:
    return {
        **(existing or {}),
        "upload_fingerprint": fingerprint,
        "filename": filename,
        "layout_signature": layout_signature,
        "confirmed_mapping_fingerprint": mapping_fingerprint,
        "confirmation_identity": identity,
        "confirmation_attempt": confirmation_attempt,
        "ai_status": "pending",
        "processing_started_at": now.isoformat(),
        "processing_attempt": processing_attempt,
    }


async def find_reusable_batch(
    db: AsyncSession,
    fingerprint: str,
    *,
    platform: str | None = None,
    layout_signature: str | None = None,
    mapping_fingerprint: str | None = None,
) -> ReconBatch | None:
    """Return only a compatible, active confirmation for the same input identity."""
    statement = select(ReconBatch).where(
        ReconBatch.status.in_(("processing", "parsed")),
        ReconBatch.mapping["upload_fingerprint"].as_string() == fingerprint,
    )
    if platform is not None:
        statement = statement.where(ReconBatch.platform == platform)
    if layout_signature is not None:
        statement = statement.where(ReconBatch.mapping["layout_signature"].as_string() == layout_signature)
    if mapping_fingerprint is not None:
        statement = statement.where(
            ReconBatch.mapping["confirmed_mapping_fingerprint"].as_string() == mapping_fingerprint
        )
    rows = await db.execute(statement.order_by(ReconBatch.created_at.desc()))
    for batch in rows.scalars():
        mapping = batch.mapping or {}
        if not mapping.get("archived_at"):
            return batch
    return None


async def mark_processing_batch_failed(db: AsyncSession, batch_id: str) -> None:
    """Retire a committed processing lease after an unexpected reconciliation crash."""
    await db.rollback()
    batch = await db.get(ReconBatch, batch_id)
    if batch is None or batch.status != "processing":
        return
    batch.status = "failed"
    batch.error = "账单处理失败，请重试"
    batch.mapping = {
        **(batch.mapping or {}),
        "processing_failed_at": datetime.now(timezone.utc).isoformat(),
    }
    await db.commit()


async def get_or_create_processing_batch(
    db: AsyncSession,
    *,
    fingerprint: str,
    filename: str,
    platform: str,
    user_id: str | None,
    layout_signature: str | None = None,
    confirmed_mapping: MappingCoordinates | None = None,
    now: datetime | None = None,
) -> ProcessingBatchResult:
    """Serialize identical uploads and persist a lightweight processing attempt."""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"billrecon:file:{fingerprint}"},
        )
    now = now or datetime.now(timezone.utc)
    mapping_fingerprint = confirmed_mapping_fingerprint(confirmed_mapping)
    identity = confirmation_identity(fingerprint, platform, layout_signature, mapping_fingerprint)
    existing = await find_reusable_batch(
        db, fingerprint, platform=platform, layout_signature=layout_signature,
        mapping_fingerprint=mapping_fingerprint,
    )
    if existing is not None:
        if existing.status == "processing":
            raw_started = (existing.mapping or {}).get("processing_started_at")
            try:
                started = datetime.fromisoformat(raw_started)
            except (TypeError, ValueError):
                started = datetime.min.replace(tzinfo=timezone.utc)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if (now - started).total_seconds() >= _PROCESSING_LEASE_SECONDS:
                prior_mapping = existing.mapping or {}
                replacement_mapping = _processing_mapping(
                    fingerprint=fingerprint,
                    filename=filename,
                    layout_signature=layout_signature,
                    mapping_fingerprint=mapping_fingerprint,
                    identity=identity,
                    confirmation_attempt=_confirmation_attempt_number(existing, identity),
                    now=now,
                    processing_attempt=int(prior_mapping.get("processing_attempt", 1)) + 1,
                    existing=prior_mapping,
                )
                # A stale lease is a compare-and-swap transition. Without the
                # observed timestamp in the predicate, two SQLite sessions can
                # both decide that they acquired the same processing work.
                reclaimed = await db.execute(
                    update(ReconBatch).where(
                        ReconBatch.batch_id == existing.batch_id,
                        ReconBatch.status == "processing",
                        ReconBatch.mapping["processing_started_at"].as_string() == raw_started,
                    ).values(mapping=replacement_mapping)
                )
                await db.commit()
                if reclaimed.rowcount:
                    await db.refresh(existing)
                    return ProcessingBatchResult(batch=existing, state=ProcessingBatchState.acquired)
                existing = await find_reusable_batch(
                    db, fingerprint, platform=platform, layout_signature=layout_signature,
                    mapping_fingerprint=mapping_fingerprint,
                )
                if existing is not None:
                    # Sessions deliberately keep objects alive after commit.
                    # Reload the CAS winner instead of returning the stale
                    # processing lease this session read before the race.
                    await db.refresh(existing)
                    return _existing_batch_result(existing)
        if existing is not None:
            return _existing_batch_result(existing)

    # Failed attempts intentionally remain auditable, but their deterministic
    # confirmation identity must be reclaimable.  The conditional update is the
    # cross-dialect retry lease: exactly one concurrent retrier wins it.
    attempts = await _confirmation_attempts(db, identity)
    latest = max(attempts, key=lambda row: _confirmation_attempt_number(row, identity), default=None)
    if latest is not None and latest.status == "failed":
        prior_mapping = latest.mapping or {}
        retry_mapping = _processing_mapping(
            fingerprint=fingerprint,
            filename=filename,
            layout_signature=layout_signature,
            mapping_fingerprint=mapping_fingerprint,
            identity=identity,
            confirmation_attempt=_confirmation_attempt_number(latest, identity),
            now=now,
            processing_attempt=int(prior_mapping.get("processing_attempt", 1)) + 1,
            existing=prior_mapping,
        )
        reclaimed = await db.execute(
            update(ReconBatch).where(
                ReconBatch.batch_id == latest.batch_id, ReconBatch.status == "failed",
            ).values(status="processing", error=None, mapping=retry_mapping)
        )
        await db.commit()
        if reclaimed.rowcount:
            await db.refresh(latest)
            return ProcessingBatchResult(batch=latest, state=ProcessingBatchState.acquired)
        existing = await find_reusable_batch(
            db, fingerprint, platform=platform, layout_signature=layout_signature,
            mapping_fingerprint=mapping_fingerprint,
        )
        if existing is not None:
            return _existing_batch_result(existing)

    # Terminal (archived/rejected) rows must remain auditable and never be
    # reused. Allocate a deterministic next attempt; an insert collision is
    # recovered by looking up the concurrent winner and, if it already became
    # terminal, by selecting the next durable attempt identity.
    while True:
        attempts = await _confirmation_attempts(db, identity)
        next_attempt = max(
            (_confirmation_attempt_number(row, identity) for row in attempts), default=-1,
        ) + 1
        batch = ReconBatch(
            batch_id=_processing_batch_id(identity, next_attempt),
            platform=platform,
            bill_month="0000-00",
            summary_total=Decimal("0"),
            row_count=0,
            status="processing",
            mapping=_processing_mapping(
                fingerprint=fingerprint,
                filename=filename,
                layout_signature=layout_signature,
                mapping_fingerprint=mapping_fingerprint,
                identity=identity,
                confirmation_attempt=next_attempt,
                now=now,
            ),
            created_by=user_id,
        )
        db.add(batch)
        try:
            await db.commit()
        except IntegrityError:
            # PostgreSQL's advisory lock protects production. This unique,
            # deterministic attempt identity additionally serializes SQLite.
            await db.rollback()
            existing = await find_reusable_batch(
                db, fingerprint, platform=platform, layout_signature=layout_signature,
                mapping_fingerprint=mapping_fingerprint,
            )
            if existing is not None:
                return _existing_batch_result(existing)
            # Only a collision on the attempt we just selected is recoverable
            # here. Re-raise unrelated integrity errors instead of looping.
            if await db.get(ReconBatch, batch.batch_id) is None:
                raise
            continue
        await db.refresh(batch)
        return ProcessingBatchResult(batch=batch, state=ProcessingBatchState.acquired)
