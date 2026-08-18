"""Upload preflight and lifecycle helpers for billing reconciliation."""

from __future__ import annotations

import io
import hashlib
import re
import zipfile
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import PurePosixPath
from uuid import uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recon import ReconBatch
from app.services.billing_recon.parser import BillParseError

_OLE_SIGNATURE = bytes.fromhex("D0CF11E0A1B11AE1")
_MAX_ZIP_MEMBERS = 256
_MAX_MEMBER_SIZE = 32 * 1024 * 1024
_MAX_TOTAL_SIZE = 64 * 1024 * 1024
_MAX_COMPRESSION_RATIO = 100
_REQUIRED_OOXML_MEMBERS = {"[Content_Types].xml", "xl/workbook.xml"}
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_PROCESSING_LEASE_SECONDS = 120


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


async def find_reusable_batch(db: AsyncSession, fingerprint: str) -> ReconBatch | None:
    """Return the newest completed/in-flight batch for identical bytes."""
    rows = await db.execute(select(ReconBatch).order_by(ReconBatch.created_at.desc()).limit(100))
    for batch in rows.scalars():
        mapping = batch.mapping or {}
        if (not mapping.get("archived_at")
                and mapping.get("upload_fingerprint") == fingerprint
                and batch.status in {"processing", "parsed"}):
            return batch
    return None


async def get_or_create_processing_batch(
    db: AsyncSession,
    *,
    fingerprint: str,
    filename: str,
    platform: str,
    user_id: str | None,
    now: datetime | None = None,
) -> tuple[ReconBatch, bool]:
    """Serialize identical uploads and persist a lightweight processing attempt."""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"billrecon:file:{fingerprint}"},
        )
    now = now or datetime.now(timezone.utc)
    existing = await find_reusable_batch(db, fingerprint)
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
                existing.mapping = {
                    **(existing.mapping or {}),
                    "filename": filename,
                    "processing_started_at": now.isoformat(),
                    "processing_attempt": int((existing.mapping or {}).get("processing_attempt", 1)) + 1,
                }
                await db.commit()
                await db.refresh(existing)
                return existing, True
        return existing, False

    batch = ReconBatch(
        batch_id=f"RB-UPLOAD-{uuid4().hex[:12].upper()}",
        platform=platform,
        bill_month="0000-00",
        summary_total=Decimal("0"),
        row_count=0,
        status="processing",
        mapping={
            "upload_fingerprint": fingerprint,
            "filename": filename,
            "ai_status": "pending",
            "processing_started_at": now.isoformat(),
            "processing_attempt": 1,
        },
        created_by=user_id,
    )
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    return batch, True
