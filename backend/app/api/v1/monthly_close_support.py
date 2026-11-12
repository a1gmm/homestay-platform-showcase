"""Private tutorial and support inbox; never mutates financial records or starts AI jobs."""
from __future__ import annotations

import asyncio
import base64
import binascii
from datetime import datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4
import warnings

from fastapi import APIRouter, Depends, HTTPException, Query, Response, Path as PathParam
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.core.deps import DBSession, RedisClient, require_role
from app.core.rate_limit import enforce_rate_limit
from app.models.monthly_close_feedback import MonthlyCloseFeedback
from app.services.audit import log_action_tx

router = APIRouter(prefix="/monthly-close-support", tags=["monthly-close-support"])
Admin = Annotated[dict, Depends(require_role("admin"))]
MONTH_PATTERN = r"^20\d{2}-(0[1-9]|1[0-2])$"
PRIVATE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"}
GUIDE = Path(__file__).resolve().parents[2] / "resources/monthly_close/trial-guide-v1.pdf"


class FeedbackCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    request_id: UUID
    billing_month: str = Field(pattern=MONTH_PATTERN)
    step: Literal["start", "upload", "receipts", "cleaning", "expenses", "statements", "recovery", "other"]
    description: str = Field(min_length=5, max_length=3000)
    expected: str = Field(default="", max_length=1500)
    execution_clicked: Literal["yes", "no", "unknown"] = "unknown"
    screenshot_base64: str | None = Field(default=None, max_length=4 * 1024 * 1024)


class FeedbackUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    revision: int = Field(ge=1)
    status: Literal["received", "processing", "resolved"]
    reply: str = Field(min_length=2, max_length=2000)


class FeedbackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    feedback_id: str
    billing_month: str
    step: str
    description: str
    expected: str
    execution_clicked: str
    screenshot_mime: str | None
    status: str
    reply: str
    revision: int
    created_at: datetime
    updated_at: datetime


def normalize_screenshot(encoded: str | None) -> tuple[bytes | None, str | None]:
    if encoded is None:
        return None, None
    try:
        raw = base64.b64decode(encoded, validate=True)
        if not raw or len(raw) > 3 * 1024 * 1024:
            raise ValueError()
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 16_000_000:
                    raise ValueError()
                image.load()
                image.thumbnail((2400, 2400))
                # A fresh image strips metadata and any embedded ancillary payload.
                clean = Image.new("RGB", image.size, "white")
                if image.mode == "RGBA":
                    clean.paste(image, mask=image.getchannel("A"))
                else:
                    clean.paste(image.convert("RGB"))
                output = BytesIO()
                clean.save(output, format="JPEG", quality=88)
                return output.getvalue(), "image/jpeg"
    except (ValueError, binascii.Error, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise HTTPException(422, "截图无法读取，请选择 3MB 以内的 PNG、JPG 或 WebP 图片（不超过 1600 万像素）") from None


@router.get("/guide")
async def guide(_admin: Admin):
    return FileResponse(GUIDE, media_type="application/pdf", filename="monthly-close-trial-guide.pdf", content_disposition_type="inline", headers=PRIVATE_HEADERS)


@router.get("/guide/pages/{page}")
async def guide_page(_admin: Admin, page: int = PathParam(ge=1, le=7)):
    return FileResponse(GUIDE.with_name(f"guide-page-{page}.webp"), media_type="image/webp", headers=PRIVATE_HEADERS)


@router.get("/feedback")
async def list_feedback(db: DBSession, _admin: Admin, response: Response,
                        billing_month: str = Query(pattern=MONTH_PATTERN), offset: int = Query(default=0, ge=0)):
    response.headers.update(PRIVATE_HEADERS)
    query = select(MonthlyCloseFeedback).where(MonthlyCloseFeedback.billing_month == billing_month)
    total = await db.scalar(select(func.count()).select_from(MonthlyCloseFeedback).where(MonthlyCloseFeedback.billing_month == billing_month))
    rows = (await db.scalars(query.order_by(MonthlyCloseFeedback.created_at.desc(), MonthlyCloseFeedback.feedback_id.desc()).offset(offset).limit(30))).all()
    return {"items": [FeedbackOut.model_validate(row) for row in rows], "total": total}


@router.post("/feedback", response_model=FeedbackOut)
async def create_feedback(body: FeedbackCreate, db: DBSession, redis: RedisClient, admin: Admin, response: Response):
    response.headers.update(PRIVATE_HEADERS)
    await enforce_rate_limit(redis, f"monthly-close-support:{admin['user_id']}", limit=20, window_seconds=60)
    fingerprint = sha256(body.model_dump_json(exclude={"request_id"}).encode()).hexdigest()
    identity = (MonthlyCloseFeedback.user_id == admin["user_id"], MonthlyCloseFeedback.request_id == str(body.request_id))

    def replay(row):
        if row.payload_hash != fingerprint:
            raise HTTPException(409, "这次提交内容已改变，请重新提交")
        return FeedbackOut.model_validate(row)

    existing = await db.scalar(select(MonthlyCloseFeedback).where(*identity))
    if existing:
        return replay(existing)
    screenshot, mime = await asyncio.to_thread(normalize_screenshot, body.screenshot_base64)
    row = MonthlyCloseFeedback(
        feedback_id=f"FB-{uuid4().hex[:24]}", user_id=admin["user_id"], request_id=str(body.request_id),
        payload_hash=fingerprint, **body.model_dump(exclude={"request_id", "screenshot_base64"}),
        screenshot=screenshot, screenshot_mime=mime,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        existing = await db.scalar(select(MonthlyCloseFeedback).where(*identity))
        if existing is None:
            raise
        return replay(existing)
    await log_action_tx(db, admin["user_id"], "monthly_close_feedback_create", resource_type="monthly_close_feedback", resource_id=row.feedback_id,
                        after_data={"billing_month": row.billing_month, "step": row.step, "has_screenshot": mime is not None})
    await db.commit()
    await db.refresh(row)
    return FeedbackOut.model_validate(row)


@router.get("/feedback/{feedback_id}/screenshot")
async def feedback_screenshot(feedback_id: str, db: DBSession, _admin: Admin):
    row = (await db.execute(select(MonthlyCloseFeedback.screenshot, MonthlyCloseFeedback.screenshot_mime).where(MonthlyCloseFeedback.feedback_id == feedback_id))).first()
    if not row or row.screenshot is None:
        raise HTTPException(404, "未找到截图")
    return Response(row.screenshot, media_type=row.screenshot_mime, headers=PRIVATE_HEADERS)


@router.patch("/feedback/{feedback_id}", response_model=FeedbackOut)
async def update_feedback(feedback_id: str, body: FeedbackUpdate, db: DBSession, redis: RedisClient, admin: Admin, response: Response):
    response.headers.update(PRIVATE_HEADERS)
    await enforce_rate_limit(redis, f"monthly-close-support-reply:{admin['user_id']}", limit=30, window_seconds=60)
    result = await db.execute(update(MonthlyCloseFeedback).where(
        MonthlyCloseFeedback.feedback_id == feedback_id, MonthlyCloseFeedback.revision == body.revision,
    ).values(status=body.status, reply=body.reply, revision=body.revision + 1, updated_at=func.now()))
    if result.rowcount != 1:
        exists = await db.scalar(select(MonthlyCloseFeedback.feedback_id).where(MonthlyCloseFeedback.feedback_id == feedback_id))
        raise HTTPException(409 if exists else 404, "记录已更新，请刷新后再处理" if exists else "未找到反馈")
    await log_action_tx(db, admin["user_id"], "monthly_close_feedback_update", resource_type="monthly_close_feedback", resource_id=feedback_id,
                        after_data={"status": body.status, "revision": body.revision + 1})
    await db.commit()
    row = await db.get(MonthlyCloseFeedback, feedback_id, populate_existing=True)
    return FeedbackOut.model_validate(row)
