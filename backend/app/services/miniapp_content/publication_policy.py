"""Conservative server-owned publication policy for public guide content."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.miniapp_content import MiniappMedia
from app.schemas.miniapp_content_guides import StayGuideDraft, TravelGuideDraft
from app.services.miniapp_content.errors import (
    PublicationPolicyError,
    PublicationPolicyIssue,
)
from app.services.miniapp_content.guides import find_public_guide_privacy_issues

# The approved stay template has six operational sections plus FAQ.  One
# operational section is the smallest useful public stay guide; FAQ alone is
# supplemental and does not satisfy this publication threshold.
STAY_CORE_PUBLIC_SECTIONS = (
    "arrivalDeparture",
    "parking",
    "wifiAndDevices",
    "houseRules",
    "checkOut",
    "support",
)


def visible_guide_media(
    draft: StayGuideDraft | TravelGuideDraft,
) -> tuple[tuple[str, str], ...]:
    refs: list[tuple[str, str]] = []
    if isinstance(draft, StayGuideDraft):
        for name in (
            "arrivalDeparture",
            "parking",
            "wifiAndDevices",
            "houseRules",
            "checkOut",
            "support",
            "faq",
        ):
            section = getattr(draft.sections, name)
            if section.visible and section.image is not None:
                refs.append((f"/sections/{name}/image", section.image.mediaId))
    else:
        for index, item in enumerate(draft.recommendations):
            if item.visible and item.image is not None:
                refs.append((f"/recommendations/{index}/image", item.image.mediaId))
    return tuple(refs)


public_text_issues = find_public_guide_privacy_issues


def publication_completeness_issues(
    draft: StayGuideDraft | TravelGuideDraft,
) -> list[PublicationPolicyIssue]:
    if isinstance(draft, StayGuideDraft):
        if not any(
            getattr(draft.sections, name).visible
            for name in STAY_CORE_PUBLIC_SECTIONS
        ):
            return [
                PublicationPolicyIssue(
                    path="/sections",
                    reason="入住指南至少需要一个可见的核心公开章节",
                )
            ]
        return []
    if not any(item.visible for item in draft.recommendations):
        return [
            PublicationPolicyIssue(
                path="/recommendations",
                reason="旅行指南至少需要一个可见推荐",
            )
        ]
    return []


async def validate_guide_publication(
    db: AsyncSession,
    draft: StayGuideDraft | TravelGuideDraft,
) -> None:
    issues = publication_completeness_issues(draft) + public_text_issues(draft)
    visible_media = visible_guide_media(draft)
    media_ids = {media_id for _path, media_id in visible_media}
    media_by_id: dict[str, MiniappMedia] = {}
    if media_ids:
        rows = (
            await db.scalars(
                select(MiniappMedia).where(MiniappMedia.media_id.in_(media_ids))
            )
        ).all()
        media_by_id = {row.media_id: row for row in rows}
    for path, media_id in visible_media:
        media = media_by_id.get(media_id)
        if (
            media is None
            or media.public_approved_by is None
            or media.public_approved_at is None
        ):
            issues.append(
                PublicationPolicyIssue(
                    path=path,
                    reason="图片尚未确认可以公开，请先完成公开授权确认",
                )
            )
    if issues:
        ordered = sorted(issues, key=lambda issue: (issue.path, issue.reason))
        raise PublicationPolicyError(ordered)


__all__ = [
    "STAY_CORE_PUBLIC_SECTIONS",
    "publication_completeness_issues",
    "public_text_issues",
    "validate_guide_publication",
    "visible_guide_media",
]
