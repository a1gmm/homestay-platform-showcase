"""Structured stay and travel guide content-management API."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from starlette.convertors import Convertor, register_url_convertor

from app.core.deps import DBSession, require_role
from app.models.miniapp_content import MiniappContentRelease, MiniappMedia
from app.schemas.miniapp_content_guides_api import (
    GuideChannel,
    StructuredGuidePreview,
    StructuredDraftRevision,
    StructuredDraftSaveInput,
)
from app.services.audit import log_action_tx
from app.services.miniapp_content.errors import (
    ContentValidationError,
    DraftConflictError,
)
from app.services.miniapp_content.guides import (
    load_structured_draft,
    save_structured_draft,
)
from app.services.miniapp_content.guide_preview import build_guide_preview_manifest
from app.services.miniapp_content.media import (
    ContentMediaService,
    InMemoryVideoUploadStore,
)
from app.services.miniapp_content.publication_policy import validate_guide_publication
from app.services.miniapp_content.publisher import (
    PublishJobOut,
    enqueue_publish_result,
    enqueue_rollback_result,
    resolve_publish_idempotency,
    resolve_rollback_idempotency,
)
from app.services.miniapp_content.storage import AliyunContentStorage
from app.workers.miniapp_content_publish import load_guide_snapshot


class _GuideChannelConvertor(Convertor):
    """Keep unsupported channels out of route dependencies and preserve owner routes."""

    regex = "stay_guide|travel"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        if value not in {"stay_guide", "travel"}:
            raise ValueError("unsupported guide channel")
        return value


register_url_convertor("guide_channel", _GuideChannelConvertor())

router = APIRouter(prefix="/miniapp-content", tags=["miniapp-content"])
_media_service: ContentMediaService | None = None
_ROLLBACK_ELIGIBLE_RELEASE_STATUSES = frozenset(
    {"published", "superseded", "rolled_back"}
)


class _GuideApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, strict=True)


class RevisionInput(_GuideApiModel):
    revision: int = Field(ge=0)


class MediaPublicApprovalInput(_GuideApiModel):
    approved: bool


class MediaPublicApprovalOut(_GuideApiModel):
    media_id: str = Field(alias="mediaId")
    approved: bool
    approved_by: str | None = Field(alias="approvedBy")
    approved_at: datetime | None = Field(alias="approvedAt")


class PublishAccepted(_GuideApiModel):
    job_id: str = Field(alias="jobId")
    channel: GuideChannel
    status: str
    expected_revision: int = Field(alias="expectedRevision", ge=0)


class RollbackAccepted(_GuideApiModel):
    job_id: str = Field(alias="jobId")
    channel: GuideChannel
    status: str
    target_version: str = Field(alias="targetVersion")


class ReleaseSummary(_GuideApiModel):
    version: str
    status: str
    published_at: datetime | None = Field(alias="publishedAt")


class ReleaseHistory(_GuideApiModel):
    releases: list[ReleaseSummary]


def _publish_accepted(job: PublishJobOut) -> PublishAccepted:
    assert job.expected_revision is not None
    return PublishAccepted(
        jobId=job.job_id,
        channel=job.channel,
        status=job.status,
        expectedRevision=job.expected_revision,
    )


def _rollback_accepted(job: PublishJobOut) -> RollbackAccepted:
    assert job.target_version is not None
    return RollbackAccepted(
        jobId=job.job_id,
        channel=job.channel,
        status=job.status,
        targetVersion=job.target_version,
    )


def _content_media_service() -> ContentMediaService:
    global _media_service
    if _media_service is None:
        from app.core.config import settings

        _media_service = ContentMediaService(
            AliyunContentStorage.from_settings(settings),
            upload_store=InMemoryVideoUploadStore(),
        )
    return _media_service


class _LazyGuideStorage:
    """Keep text-only previews independent from object-storage configuration."""

    def __getattr__(self, name: str):
        return getattr(_content_media_service()._storage, name)


def _sign_guide_preview(object_key: str, expires_seconds: int) -> str:
    return _content_media_service().sign_draft_preview(object_key, expires_seconds)


@router.get(
    "/{channel:guide_channel}/draft",
    response_model=StructuredDraftRevision,
)
async def get_guide_draft(
    channel: GuideChannel,
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> StructuredDraftRevision:
    return await load_structured_draft(db, channel=channel)


@router.put(
    "/{channel:guide_channel}/draft",
    response_model=StructuredDraftRevision,
)
async def save_guide_draft(
    channel: GuideChannel,
    body: StructuredDraftSaveInput,
    db: DBSession,
    current_user: dict = Depends(require_role("admin", "operator")),
) -> StructuredDraftRevision:
    return await save_structured_draft(
        db,
        channel=channel,
        expected_revision=body.expected_revision,
        payload=body.draft,
        actor_id=current_user["user_id"],
    )


@router.patch(
    "/media/{media_id}/public-approval",
    response_model=MediaPublicApprovalOut,
)
async def set_guide_media_public_approval(
    media_id: str,
    body: MediaPublicApprovalInput,
    db: DBSession,
    current_user: dict = Depends(require_role("admin", "operator")),
) -> MediaPublicApprovalOut:
    media = await db.scalar(
        select(MiniappMedia)
        .where(MiniappMedia.media_id == media_id)
        .with_for_update()
    )
    if (
        media is None
        or media.media_type != "image"
        or media.reference_status not in {"draft", "published"}
    ):
        raise ContentValidationError("guide public approval requires available image media")
    if body.approved:
        if media.public_approved_by is None or media.public_approved_at is None:
            media.public_approved_by = current_user["user_id"]
            media.public_approved_at = datetime.now(timezone.utc)
    else:
        media.public_approved_by = None
        media.public_approved_at = None
    await log_action_tx(
        db,
        current_user["user_id"],
        "content.media.public_approval",
        "miniapp_media",
        media.media_id,
        after_data={"approved": body.approved},
    )
    await db.commit()
    return MediaPublicApprovalOut(
        mediaId=media.media_id,
        approved=media.public_approved_by is not None,
        approvedBy=media.public_approved_by,
        approvedAt=media.public_approved_at,
    )


@router.post(
    "/{channel:guide_channel}/preview",
    response_model=StructuredGuidePreview,
)
async def preview_guide_draft(
    channel: GuideChannel,
    body: RevisionInput,
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> StructuredGuidePreview:
    saved = await load_structured_draft(db, channel=channel)
    if saved.saved_by is None:
        raise ContentValidationError("guide draft has not been saved")
    if saved.revision != body.revision:
        raise DraftConflictError("preview revision is not the saved draft revision")
    snapshot = await load_guide_snapshot(
        db,
        SimpleNamespace(channel=channel, expected_revision=body.revision),
        storage=_LazyGuideStorage(),
        enforce_publication_policy=False,
        include_hidden_media=True,
    )
    manifest = build_guide_preview_manifest(
        snapshot,
        sign_url=_sign_guide_preview,
        created_at=datetime.now(timezone.utc),
    )
    return StructuredGuidePreview(
        channel=channel,
        revision=saved.revision,
        manifest=manifest,
    )


@router.post(
    "/{channel:guide_channel}/publish",
    response_model=PublishAccepted,
)
async def publish_guide_draft(
    channel: GuideChannel,
    body: RevisionInput,
    db: DBSession,
    current_user: dict = Depends(require_role("admin")),
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=120
    ),
) -> PublishAccepted:
    replay = await resolve_publish_idempotency(
        db,
        channel,
        current_user["user_id"],
        body.revision,
        idempotency_key,
    )
    if replay is not None:
        return _publish_accepted(replay)

    saved = await load_structured_draft(
        db,
        channel=channel,
        lock_workspace=True,
    )
    if saved.saved_by is None:
        raise ContentValidationError("guide draft has not been saved")
    if saved.revision != body.revision:
        raise DraftConflictError("publish revision is not the saved draft revision")

    await validate_guide_publication(db, saved.draft)

    enqueue_result = await enqueue_publish_result(
        db,
        channel,
        current_user["user_id"],
        body.revision,
        idempotency_key,
    )
    job = enqueue_result.job
    if enqueue_result.created:
        await log_action_tx(
            db,
            current_user["user_id"],
            f"content.{channel}.publish",
            "miniapp_publish_job",
            job.job_id,
        )
    await db.commit()
    return _publish_accepted(job)


@router.get(
    "/{channel:guide_channel}/releases",
    response_model=ReleaseHistory,
)
async def list_guide_releases(
    channel: GuideChannel,
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> ReleaseHistory:
    releases = (
        await db.scalars(
            select(MiniappContentRelease)
            .where(MiniappContentRelease.channel == channel)
            .order_by(MiniappContentRelease.created_at.desc())
        )
    ).all()
    return ReleaseHistory(
        releases=[
            ReleaseSummary(
                version=row.version,
                status=row.status,
                publishedAt=row.published_at,
            )
            for row in releases
        ]
    )


@router.post(
    "/{channel:guide_channel}/releases/{version}/rollback",
    response_model=RollbackAccepted,
)
async def rollback_guide_release(
    channel: GuideChannel,
    version: str,
    db: DBSession,
    current_user: dict = Depends(require_role("admin")),
    idempotency_key: str = Header(
        ..., alias="Idempotency-Key", min_length=1, max_length=120
    ),
) -> RollbackAccepted:
    replay = await resolve_rollback_idempotency(
        db,
        channel,
        current_user["user_id"],
        version,
        idempotency_key,
    )
    if replay is not None:
        return _rollback_accepted(replay)
    if len(version) > 40:
        raise ContentValidationError("rollback target version is too long")

    release = await db.scalar(
        select(MiniappContentRelease)
        .where(
            MiniappContentRelease.channel == channel,
            MiniappContentRelease.version == version,
        )
        .with_for_update()
    )
    if release is None:
        raise ContentValidationError("rollback target release does not exist")
    if release.status not in _ROLLBACK_ELIGIBLE_RELEASE_STATUSES:
        raise ContentValidationError("rollback target release is not eligible")

    enqueue_result = await enqueue_rollback_result(
        db,
        channel,
        version,
        current_user["user_id"],
        idempotency_key,
    )
    job = enqueue_result.job
    if enqueue_result.created:
        await log_action_tx(
            db,
            current_user["user_id"],
            f"content.{channel}.rollback",
            "miniapp_publish_job",
            job.job_id,
        )
    await db.commit()
    return _rollback_accepted(job)
