"""Authenticated release announcement API."""

from __future__ import annotations

from datetime import UTC, datetime
import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict

from app.core.deps import CurrentUser, DBSession
from app.release_announcements import ReleaseAnnouncement
from app.services.release_announcements import (
    AnnouncementInvalidError,
    acknowledge_announcements,
    list_unread_announcements,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/release-announcements", tags=["release-announcements"])


class ReleaseAnnouncementOut(BaseModel):
    announcement_id: str
    title: str
    summary: str
    items: list[str]
    published_at: datetime
    cta_label: str | None
    cta_path: str | None


class UnreadAnnouncementsResponse(BaseModel):
    items: list[ReleaseAnnouncementOut]


class AcknowledgeAnnouncementsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    announcement_ids: list[str]


class AcknowledgeAnnouncementsResponse(BaseModel):
    acknowledged_ids: list[str]


def _safe_announcement(item: ReleaseAnnouncement) -> ReleaseAnnouncementOut:
    return ReleaseAnnouncementOut(
        announcement_id=item.announcement_id,
        title=item.title,
        summary=item.summary,
        items=list(item.items),
        published_at=item.published_at,
        cta_label=item.cta_label,
        cta_path=item.cta_path,
    )


@router.get("/unread", response_model=UnreadAnnouncementsResponse)
async def unread_announcements(
    db: DBSession,
    current_user: CurrentUser,
) -> UnreadAnnouncementsResponse:
    try:
        items = await list_unread_announcements(
            db,
            current_user["user_id"],
            current_user["role"],
            datetime.now(UTC),
        )
    except Exception:
        logger.error(
            "release announcement unread announcement_count=0 "
            "result=failure error_code=ANNOUNCEMENT_UNREAD_FAILED"
        )
        raise

    logger.info(
        "release announcement unread announcement_count=%d result=success",
        len(items),
    )
    return UnreadAnnouncementsResponse(
        items=[_safe_announcement(item) for item in items]
    )


@router.post("/acknowledge", response_model=AcknowledgeAnnouncementsResponse)
async def acknowledge_release_announcements(
    body: AcknowledgeAnnouncementsRequest,
    db: DBSession,
    current_user: CurrentUser,
) -> AcknowledgeAnnouncementsResponse:
    announcement_count = len(body.announcement_ids)
    try:
        acknowledged_ids = await acknowledge_announcements(
            db,
            current_user["user_id"],
            current_user["role"],
            body.announcement_ids,
            datetime.now(UTC),
        )
    except AnnouncementInvalidError:
        logger.info(
            "release announcement acknowledge announcement_count=%d "
            "result=failure error_code=ANNOUNCEMENT_INVALID",
            announcement_count,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "ANNOUNCEMENT_INVALID",
                "message": "更新公告不可确认",
                "field": "announcement_ids",
            },
        ) from None
    except Exception:
        await db.rollback()
        logger.error(
            "release announcement acknowledge announcement_count=%d "
            "result=failure error_code=ANNOUNCEMENT_ACKNOWLEDGE_FAILED",
            announcement_count,
        )
        raise

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        logger.error(
            "release announcement acknowledge announcement_count=%d "
            "result=failure error_code=ANNOUNCEMENT_ACKNOWLEDGE_FAILED",
            announcement_count,
        )
        raise

    logger.info(
        "release announcement acknowledge announcement_count=%d result=success",
        announcement_count,
    )
    return AcknowledgeAnnouncementsResponse(
        acknowledged_ids=list(acknowledged_ids)
    )
