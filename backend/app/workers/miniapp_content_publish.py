"""Celery ownership for mini-program content publish/outbox recovery."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import os
import re
from uuid import uuid4

from PIL import Image, UnidentifiedImageError
from redis.asyncio import Redis
from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.miniapp_content import (
    MiniappContentRelease,
    MiniappContentWorkspace,
    MiniappImagePairGeneration,
    MiniappImagePairUpload,
    MiniappMedia,
    MiniappPublishJob,
    MiniappPublishOutbox,
    MiniappStructuredDraft,
    MiniappVideoUpload,
    OwnerCase,
    OwnerDraftVideo,
    PUBLISH_RECOVERY_METADATA_VERSION,
)
from app.schemas.miniapp_content_guides import StayGuideDraft, TravelGuideDraft
from app.services.feishu_lead_alert import send_content_publish_alert
from app.services.miniapp_content.errors import (
    ContentValidationError,
    DraftConflictError,
    MiniappContentError,
    WorkerLostError,
    error_code,
    retry_delay_for,
)
from app.services.miniapp_content.media import (
    ContentMediaService,
    InMemoryVideoUploadStore,
    MediaError,
    MediaVerificationError,
    StoredMedia,
)
from app.services.miniapp_content.publisher import (
    DraftMediaRef,
    DraftImageRef,
    HttpPublicVerifier,
    OwnerDraftCase,
    OwnerDraftSnapshot,
    OwnerDraftVideoRef,
    PublishDependencies,
    RedisPublishLease,
    StructuredGuideDraftSnapshot,
    dispatch_publish_outbox,
    execute_publish_job,
    reconcile_publish_terminal_failure,
)
from app.services.miniapp_content.publication_policy import (
    validate_guide_publication,
    visible_guide_media,
)
from app.services.miniapp_content.storage import (
    AliyunContentStorage,
    StorageError,
    StorageNotFoundError,
)
from app.workers.async_helper import run_async
from app.workers.celery_app import celery_app


PUBLISH_HARD_LIMIT_SECONDS = 900
PUBLISH_SOFT_LIMIT_SECONDS = 840
WATCHDOG_STALE_AFTER = timedelta(minutes=5)
WATCHDOG_DELIVERED_STALE_AFTER = timedelta(seconds=3600)
WATCHDOG_MAX_RECOVERIES = 3
VIDEO_CLEANUP_LEASE = timedelta(minutes=5)
VIDEO_CLEANUP_MAX_BACKOFF = timedelta(minutes=15)
IMAGE_PAIR_CLEANUP_STALE_AFTER = timedelta(minutes=15)
IMAGE_PAIR_CLEANUP_LEASE = timedelta(minutes=5)
IMAGE_PAIR_LATE_WRITE_WINDOW = timedelta(minutes=30)
IMAGE_PAIR_CLEANUP_PROBE_INTERVAL = timedelta(minutes=5)
PRIVATE_MEDIA_UPLOAD_LEASE = timedelta(minutes=15)
PUBLIC_BASE_URL = "https://media.example.invalid"
ADMIN_CONTENT_URL = "https://admin.example.invalid/content"
_SAFE_CASE_ID = re.compile(r"[^a-z0-9-]+")


@dataclass(frozen=True)
class VideoCleanupSummary:
    claimed: int = 0
    cleaned: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ImagePairCleanupSummary:
    claimed: int = 0
    cleaned: int = 0
    failed: int = 0


@dataclass(frozen=True)
class PrivateMediaCleanupSummary:
    claimed: int = 0
    cleaned: int = 0
    failed: int = 0


def _cleanup_backoff(attempt: int) -> timedelta:
    return min(timedelta(seconds=2 ** min(attempt, 10)), VIDEO_CLEANUP_MAX_BACKOFF)


def _video_cleanup_storage():
    process_test_root = os.environ.get("MINIAPP_CONTENT_PROCESS_TEST_ROOT", "")
    if process_test_root and settings.APP_ENV == "test":
        from tests.support.miniapp_content_process import ProcessLocalStorage

        return ProcessLocalStorage(process_test_root)
    return AliyunContentStorage.from_settings(settings)


async def reconcile_image_pair_cleanup(
    *, batch_size: int = 50, storage=None, session_factory=None,
    now: datetime | None = None,
) -> ImagePairCleanupSummary:
    """Reconcile every exact generation key until its late-write window closes."""
    current = now or datetime.now(timezone.utc)
    session_factory = session_factory or AsyncSessionLocal
    storage = storage or _video_cleanup_storage()
    async with session_factory() as db:
        async with db.begin():
            rows = (await db.scalars(
                select(MiniappImagePairGeneration).where(
                    or_(
                        and_(
                            MiniappImagePairGeneration.status == "cleanup_pending",
                            or_(
                                MiniappImagePairGeneration.next_probe_at.is_(None),
                                MiniappImagePairGeneration.next_probe_at <= current,
                            ),
                        ),
                        and_(
                            MiniappImagePairGeneration.status == "active",
                            MiniappImagePairGeneration.updated_at
                            <= current - IMAGE_PAIR_CLEANUP_STALE_AFTER,
                        ),
                        and_(
                            MiniappImagePairGeneration.status == "cleaning",
                            MiniappImagePairGeneration.next_probe_at <= current,
                        ),
                    )
                )
                .order_by(
                    MiniappImagePairGeneration.next_probe_at.asc().nullslast(),
                    MiniappImagePairGeneration.updated_at,
                    MiniappImagePairGeneration.derivative_set_id,
                    MiniappImagePairGeneration.key_generation,
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )).all()
            claimed = []
            for row in rows:
                claim_token = uuid4().hex
                row.status = "cleaning"
                row.cleanup_attempt_count += 1
                row.next_probe_at = current + IMAGE_PAIR_CLEANUP_LEASE
                row.retire_at = row.retire_at or (
                    current + IMAGE_PAIR_LATE_WRITE_WINDOW
                )
                row.cleanup_claim_token = claim_token
                claimed.append((
                    row.derivative_set_id,
                    row.key_generation,
                    row.thumbnail_key,
                    row.display_key,
                    row.cleanup_attempt_count,
                    row.retire_at,
                    claim_token,
                ))
        cleaned = failed = 0
        for (
            set_id,
            key_generation,
            thumbnail_key,
            display_key,
            attempt,
            retire_at,
            claim_token,
        ) in claimed:
            normalized_retire_at = retire_at
            if normalized_retire_at.tzinfo is None:
                normalized_retire_at = normalized_retire_at.replace(
                    tzinfo=timezone.utc
                )
            try:
                storage.delete(thumbnail_key)
                storage.delete(display_key)
                absent = False
                if current >= normalized_retire_at:
                    absent = True
                    for object_key in (thumbnail_key, display_key):
                        try:
                            storage.head(object_key)
                        except StorageNotFoundError:
                            continue
                        else:
                            absent = False
            except Exception as exc:
                async with session_factory() as update_db:
                    async with update_db.begin():
                        result = await update_db.execute(
                            update(MiniappImagePairGeneration)
                            .where(
                                MiniappImagePairGeneration.derivative_set_id == set_id,
                                MiniappImagePairGeneration.key_generation == key_generation,
                                MiniappImagePairGeneration.status == "cleaning",
                                MiniappImagePairGeneration.cleanup_claim_token == claim_token,
                            )
                            .values(
                                status="cleanup_pending",
                                error=f"image_pair_cleanup:{exc.__class__.__name__}"[:240],
                                next_probe_at=current + _cleanup_backoff(attempt),
                                cleanup_claim_token=None,
                                updated_at=current,
                            )
                        )
                        failed += result.rowcount
                terminal_status = "cleanup_pending"
                next_probe_at = current + _cleanup_backoff(attempt)
                error = f"image_pair_cleanup:{exc.__class__.__name__}"[:240]
            else:
                terminal = absent and current >= normalized_retire_at
                terminal_status = "retired" if terminal else "cleanup_pending"
                next_probe_at = None if terminal else min(
                    current + IMAGE_PAIR_CLEANUP_PROBE_INTERVAL,
                    normalized_retire_at,
                )
                error = None
                async with session_factory() as update_db:
                    async with update_db.begin():
                        result = await update_db.execute(
                            update(MiniappImagePairGeneration)
                            .where(
                                MiniappImagePairGeneration.derivative_set_id == set_id,
                                MiniappImagePairGeneration.key_generation == key_generation,
                                MiniappImagePairGeneration.status == "cleaning",
                                MiniappImagePairGeneration.cleanup_claim_token == claim_token,
                            )
                            .values(
                                status=terminal_status,
                                next_probe_at=next_probe_at,
                                cleanup_claim_token=None,
                                error=None,
                                updated_at=current,
                            )
                        )
                        cleaned += result.rowcount
            if result.rowcount:
                async with session_factory() as parent_db:
                    async with parent_db.begin():
                        parent = await parent_db.scalar(
                            select(MiniappImagePairUpload)
                            .where(
                                MiniappImagePairUpload.derivative_set_id == set_id
                            )
                            .with_for_update()
                        )
                        generation = await parent_db.get(
                            MiniappImagePairGeneration,
                            (set_id, key_generation),
                        )
                        if (
                            parent is not None
                            and generation is not None
                            and parent.key_generation == key_generation
                            and parent.status != "finalized"
                            and generation.status == terminal_status
                        ):
                            parent.status = (
                                "retired"
                                if terminal_status == "retired"
                                else "cleanup_pending"
                            )
                            parent.cleanup_attempt_count = generation.cleanup_attempt_count
                            parent.cleanup_next_attempt_at = next_probe_at
                            parent.cleanup_claim_token = None
                            parent.error = error
                            parent.updated_at = current
        return ImagePairCleanupSummary(
            claimed=len(claimed), cleaned=cleaned, failed=failed
        )


async def reconcile_video_quarantine_cleanup(
    *, batch_size: int = 50, storage=None, session_factory=None, now: datetime | None = None,
) -> VideoCleanupSummary:
    """Autonomously delete post-commit quarantine objects with durable retries.

    The initial claim is committed before touching OSS.  Thus worker loss only
    delays the row until its lease expires; it can never roll back finalized
    media/audit state.
    """
    current = now or datetime.now(timezone.utc)
    session_factory = session_factory or AsyncSessionLocal
    storage = storage or _video_cleanup_storage()
    async with session_factory() as db:
        async with db.begin():
            rows = (await db.scalars(
                select(MiniappVideoUpload).where(
                    or_(
                        MiniappVideoUpload.pending_quarantine_cleanup.is_(True),
                        and_(
                            MiniappVideoUpload.pending_draft_cleanup.is_(True),
                            MiniappVideoUpload.status != "finalized",
                        ),
                    ),
                    or_(MiniappVideoUpload.cleanup_next_attempt_at.is_(None),
                        MiniappVideoUpload.cleanup_next_attempt_at <= current),
                ).order_by(MiniappVideoUpload.cleanup_next_attempt_at, MiniappVideoUpload.media_id)
                .limit(batch_size).with_for_update(skip_locked=True)
            )).all()
            claimed = []
            for row in rows:
                claim_token = uuid4().hex
                row.cleanup_attempt_count += 1
                row.cleanup_next_attempt_at = current + VIDEO_CLEANUP_LEASE
                row.cleanup_claim_token = claim_token
                claimed.append(
                    (
                        row.media_id,
                        row.quarantine_key
                        if row.pending_quarantine_cleanup else None,
                        row.draft_key
                        if row.pending_draft_cleanup and row.status != "finalized" else None,
                        row.cleanup_attempt_count,
                        claim_token,
                    )
                )
        cleaned = failed = 0
        for media_id, quarantine_key, draft_key, attempt, claim_token in claimed:
            try:
                if quarantine_key is not None:
                    storage.delete(quarantine_key)
                if draft_key is not None:
                    storage.delete(draft_key)
            except Exception as exc:
                async with session_factory() as update_db:
                    async with update_db.begin():
                        result = await update_db.execute(
                            update(MiniappVideoUpload)
                            .where(
                                MiniappVideoUpload.media_id == media_id,
                                MiniappVideoUpload.cleanup_claim_token == claim_token,
                            )
                            .values(
                                error=f"quarantine_cleanup:{exc.__class__.__name__}"[:240],
                                cleanup_next_attempt_at=current + _cleanup_backoff(attempt),
                                cleanup_claim_token=None,
                            )
                        )
                        failed += result.rowcount
            else:
                async with session_factory() as update_db:
                    async with update_db.begin():
                        result = await update_db.execute(
                            update(MiniappVideoUpload)
                            .where(
                                MiniappVideoUpload.media_id == media_id,
                                MiniappVideoUpload.cleanup_claim_token == claim_token,
                            )
                            .values(
                                pending_quarantine_cleanup=False,
                                pending_draft_cleanup=False,
                                cleanup_next_attempt_at=None,
                                cleanup_claim_token=None,
                                error=None,
                            )
                        )
                        cleaned += result.rowcount
        return VideoCleanupSummary(claimed=len(claimed), cleaned=cleaned, failed=failed)


async def reconcile_private_media_cleanup(
    *, batch_size: int = 50, storage=None, session_factory=None,
    now: datetime | None = None,
) -> PrivateMediaCleanupSummary:
    """Delete only exact, private draft keys whose durable upload lease expired."""
    current = now or datetime.now(timezone.utc)
    session_factory = session_factory or AsyncSessionLocal
    storage = storage or _video_cleanup_storage()
    async with session_factory() as db:
        async with db.begin():
            rows = (await db.scalars(
                select(MiniappMedia)
                .where(
                    MiniappMedia.pending_private_cleanup.is_(True),
                    MiniappMedia.reference_status == "unreferenced",
                    MiniappMedia.object_key.startswith("miniapp/drafts/"),
                    or_(
                        MiniappMedia.cleanup_next_attempt_at.is_(None),
                        MiniappMedia.cleanup_next_attempt_at <= current,
                    ),
                )
                .order_by(
                    MiniappMedia.cleanup_next_attempt_at,
                    MiniappMedia.media_id,
                )
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )).all()
            claimed = []
            for row in rows:
                claim_token = uuid4().hex
                row.cleanup_attempt_count += 1
                row.cleanup_next_attempt_at = current + VIDEO_CLEANUP_LEASE
                row.cleanup_claim_token = claim_token
                claimed.append((
                    row.media_id,
                    row.object_key,
                    row.cleanup_attempt_count,
                    claim_token,
                ))

        cleaned = failed = 0
        for media_id, object_key, attempt, claim_token in claimed:
            try:
                storage.delete(object_key)
            except Exception as exc:
                async with session_factory() as update_db:
                    async with update_db.begin():
                        result = await update_db.execute(
                            update(MiniappMedia)
                            .where(
                                MiniappMedia.media_id == media_id,
                                MiniappMedia.pending_private_cleanup.is_(True),
                                MiniappMedia.reference_status == "unreferenced",
                                MiniappMedia.cleanup_claim_token == claim_token,
                            )
                            .values(
                                cleanup_error=(
                                    f"private_media_cleanup:{exc.__class__.__name__}"
                                )[:240],
                                cleanup_next_attempt_at=current + _cleanup_backoff(attempt),
                                cleanup_claim_token=None,
                            )
                        )
                        failed += result.rowcount
            else:
                async with session_factory() as update_db:
                    async with update_db.begin():
                        result = await update_db.execute(
                            update(MiniappMedia)
                            .where(
                                MiniappMedia.media_id == media_id,
                                MiniappMedia.pending_private_cleanup.is_(True),
                                MiniappMedia.reference_status == "unreferenced",
                                MiniappMedia.cleanup_claim_token == claim_token,
                            )
                            .values(
                                pending_private_cleanup=False,
                                cleanup_next_attempt_at=None,
                                cleanup_claim_token=None,
                                cleanup_error=None,
                            )
                        )
                        cleaned += result.rowcount
        return PrivateMediaCleanupSummary(
            claimed=len(claimed), cleaned=cleaned, failed=failed
        )


def _normalized_case_id(case_id: str) -> str:
    normalized = _SAFE_CASE_ID.sub("-", case_id.lower()).strip("-")
    return normalized or "case"


def _read_media_bytes(storage, media: MiniappMedia) -> bytes:
    total = 0
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    for chunk in storage.read(media.object_key, chunk_size=1024 * 1024):
        total += len(chunk)
        if total > media.size_bytes:
            raise MediaVerificationError("draft media read exceeds persisted size")
        digest.update(chunk)
        chunks.append(chunk)
    if total != media.size_bytes or digest.hexdigest() != media.sha256:
        raise MediaVerificationError("draft media bytes differ from persisted metadata")
    return b"".join(chunks)


def _image_dimensions(storage, media: MiniappMedia) -> tuple[int, int]:
    if not media.mime_type.startswith("image/"):
        raise ContentValidationError("owner case media must be an image")
    try:
        with Image.open(BytesIO(_read_media_bytes(storage, media))) as image:
            image.verify()
            return image.size
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise MediaVerificationError("owner image dimensions could not be verified") from exc


async def _media_ref(
    db: AsyncSession,
    storage,
    media_id: str,
    *,
    alt: str,
) -> DraftMediaRef:
    media = await db.get(MiniappMedia, media_id)
    if media is None or media.reference_status not in {"draft", "published"}:
        raise ContentValidationError("owner case references unavailable media")
    width, height = await asyncio.to_thread(_image_dimensions, storage, media)
    return DraftMediaRef(
        media_id=media.media_id,
        stored=StoredMedia(
            object_key=media.object_key,
            sha256=media.sha256,
            byte_size=media.size_bytes,
            mime_type=media.mime_type,
        ),
        alt=alt,
        width=width,
        height=height,
        derivative_set_id=media.derivative_set_id,
        derivative_role=media.derivative_role,
    )


async def _video_ref(
    db: AsyncSession,
    media_id: str,
    *,
    alt: str,
) -> DraftMediaRef:
    media = await db.get(MiniappMedia, media_id)
    upload = await db.get(MiniappVideoUpload, media_id)
    if (
        media is None
        or media.media_type != "video"
        or media.mime_type != "video/mp4"
        or media.reference_status not in {"draft", "published"}
        or upload is None
        or upload.status != "finalized"
        or upload.width is None
        or upload.height is None
        or upload.draft_key != media.object_key
        or upload.expected_sha256 != media.sha256
        or upload.declared_size != media.size_bytes
    ):
        raise ContentValidationError("owner video is not durably publishable")
    return DraftMediaRef(
        media_id=media.media_id,
        stored=StoredMedia(
            object_key=media.object_key,
            sha256=media.sha256,
            byte_size=media.size_bytes,
            mime_type=media.mime_type,
        ),
        alt=alt,
        width=upload.width,
        height=upload.height,
    )


async def _image_pair_refs(
    db: AsyncSession,
    storage,
    thumbnail_id: str,
    display_id: str,
    *,
    alt: str,
) -> tuple[DraftMediaRef, DraftMediaRef]:
    if thumbnail_id == display_id:
        raise ContentValidationError(
            "owner image derivatives must be one server-generated source pair"
        )
    thumbnail = await db.get(MiniappMedia, thumbnail_id)
    display = await db.get(MiniappMedia, display_id)
    if (
        thumbnail is None
        or display is None
        or thumbnail.media_type != "image"
        or display.media_type != "image"
        or thumbnail.reference_status not in {"draft", "published"}
        or display.reference_status not in {"draft", "published"}
        or thumbnail.derivative_role != "thumbnail"
        or display.derivative_role != "display"
        or thumbnail.derivative_set_id is None
        or thumbnail.derivative_set_id != display.derivative_set_id
        or thumbnail.source_sha256 is None
        or thumbnail.source_sha256 != display.source_sha256
    ):
        raise ContentValidationError(
            "owner image derivatives must be one server-generated source pair"
        )
    return (
        await _media_ref(db, storage, thumbnail_id, alt=alt),
        await _media_ref(db, storage, display_id, alt=alt),
    )


async def load_owner_snapshot(
    db: AsyncSession,
    job: MiniappPublishJob,
    *,
    storage,
    allow_empty: bool = False,
    lock_workspace: bool = False,
) -> OwnerDraftSnapshot:
    workspace_query = select(MiniappContentWorkspace).where(
        MiniappContentWorkspace.channel == job.channel
    )
    if lock_workspace:
        workspace_query = workspace_query.with_for_update()
    workspace = await db.scalar(workspace_query)
    if workspace is None:
        raise ContentValidationError("owner workspace does not exist")
    video_row = await db.get(OwnerDraftVideo, "owner")
    video = None
    if video_row is not None:
        if video_row.revision != workspace.revision:
            raise ContentValidationError("owner video revision is stale")
        video = OwnerDraftVideoRef(
            video=await _video_ref(db, video_row.video_media_id, alt=video_row.alt),
            poster=await _media_ref(
                db, storage, video_row.poster_media_id, alt=f"{video_row.alt}封面"
            ),
            alt=video_row.alt,
        )
    rows = (
        await db.scalars(
            select(OwnerCase)
            .where(
                OwnerCase.is_visible.is_(True),
                OwnerCase.deleted_at.is_(None),
                OwnerCase.revision == workspace.revision,
            )
            .order_by(OwnerCase.sort_order, OwnerCase.case_id)
        )
    ).all()
    cases: list[OwnerDraftCase] = []
    for row in rows:
        if row.privacy_confirmed_by is None or row.primary_media_id is None:
            raise ContentValidationError("visible owner case lacks privacy confirmation or hero")
        hero = await _media_ref(db, storage, row.primary_media_id, alt=f"{row.title} 主图")
        images: list[DraftImageRef] = []
        for index, image in enumerate(row.detail_media_ids, start=1):
            if not isinstance(image, dict):
                raise ContentValidationError("owner case detail image must contain paired derivatives")
            alt = image.get("alt") or f"{row.title} 详情图 {index}"
            thumbnail_id = image.get("thumbnailMediaId")
            display_id = image.get("displayMediaId")
            if not isinstance(thumbnail_id, str) or not isinstance(display_id, str):
                raise ContentValidationError("owner case detail image must contain paired derivatives")
            thumbnail, display = await _image_pair_refs(
                db, storage, thumbnail_id, display_id, alt=alt
            )
            images.append(DraftImageRef(alt=alt, thumbnail=thumbnail, display=display))
        cases.append(
            OwnerDraftCase(
                case_id=_normalized_case_id(row.case_id),
                title=row.title,
                summary=row.description or "",
                hero=hero,
                images=tuple(images),
            )
    )
    if not cases:
        if allow_empty:
            return OwnerDraftSnapshot(
                revision=workspace.revision,
                hero=None,
                cases=(),
                video=video,
            )
        raise ContentValidationError("owner draft has no publishable cases")
    return OwnerDraftSnapshot(
        revision=workspace.revision,
        hero=cases[0].hero,
        cases=tuple(cases),
        video=video,
    )


def _guide_media_ids(draft: StayGuideDraft | TravelGuideDraft) -> set[str]:
    return {media_id for _path, media_id in visible_guide_media(draft)}


def _all_guide_media_ids(draft: StayGuideDraft | TravelGuideDraft) -> set[str]:
    if isinstance(draft, StayGuideDraft):
        sections = draft.sections
        images = (
            sections.arrivalDeparture.image,
            sections.parking.image,
            sections.wifiAndDevices.image,
            sections.houseRules.image,
            sections.checkOut.image,
            sections.support.image,
            sections.faq.image,
        )
    else:
        images = tuple(item.image for item in draft.recommendations)
    return {image.mediaId for image in images if image is not None}


async def load_guide_snapshot(
    db: AsyncSession,
    job: MiniappPublishJob,
    *,
    storage,
    enforce_publication_policy: bool = True,
    include_hidden_media: bool = False,
) -> StructuredGuideDraftSnapshot:
    model = {
        "stay_guide": StayGuideDraft,
        "travel": TravelGuideDraft,
    }.get(job.channel)
    if model is None:
        raise ContentValidationError("guide snapshot channel is unsupported")
    saved = (
        await db.execute(
            select(MiniappContentWorkspace, MiniappStructuredDraft)
            .join(
                MiniappStructuredDraft,
                MiniappStructuredDraft.channel == MiniappContentWorkspace.channel,
            )
            .where(MiniappContentWorkspace.channel == job.channel)
        )
    ).one_or_none()
    if saved is None:
        raise ContentValidationError("saved structured draft does not exist")
    workspace, row = saved
    if workspace.revision != job.expected_revision:
        raise DraftConflictError(
            "saved draft revision no longer matches the publish request"
        )
    try:
        draft = model.model_validate(row.payload)
    except (TypeError, ValueError) as exc:
        raise ContentValidationError("stored structured draft is invalid") from exc

    if enforce_publication_policy:
        await validate_guide_publication(db, draft)

    media_ids = (
        _all_guide_media_ids(draft) if include_hidden_media else _guide_media_ids(draft)
    )
    rows = []
    if media_ids:
        rows = (
            await db.scalars(
                select(MiniappMedia).where(MiniappMedia.media_id.in_(media_ids))
            )
        ).all()
    available = {
        media.media_id: media
        for media in rows
        if media.media_type == "image"
        and media.reference_status in {"draft", "published"}
    }
    if set(available) != media_ids:
        raise ContentValidationError(
            "structured draft references unavailable image media"
        )

    media_refs: dict[str, DraftMediaRef] = {}
    for media_id, media in available.items():
        width, height = await asyncio.to_thread(_image_dimensions, storage, media)
        media_refs[media_id] = DraftMediaRef(
            media_id=media.media_id,
            stored=StoredMedia(
                object_key=media.object_key,
                sha256=media.sha256,
                byte_size=media.size_bytes,
                mime_type=media.mime_type,
            ),
            alt="guide image",
            width=width,
            height=height,
        )
    return StructuredGuideDraftSnapshot(
        revision=workspace.revision,
        channel=job.channel,
        draft=draft,
        media=media_refs,
    )


async def load_publish_snapshot(
    db: AsyncSession,
    job: MiniappPublishJob,
    *,
    storage,
) -> OwnerDraftSnapshot | StructuredGuideDraftSnapshot:
    if job.channel == "owner":
        return await load_owner_snapshot(db, job, storage=storage)
    if job.channel in {"stay_guide", "travel"}:
        return await load_guide_snapshot(db, job, storage=storage)
    raise ContentValidationError("publish snapshot channel is unsupported")


async def _default_dependencies() -> tuple[PublishDependencies, Redis]:
    process_test_root = os.environ.get("MINIAPP_CONTENT_PROCESS_TEST_ROOT", "")
    if process_test_root and settings.APP_ENV == "test":
        from tests.support.miniapp_content_process import build_process_dependencies

        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        lease = RedisPublishLease(redis, ttl_seconds=120)
        return (
            build_process_dependencies(
                root=process_test_root,
                lease=lease,
                public_base_url=PUBLIC_BASE_URL,
            ),
            redis,
        )
    storage = AliyunContentStorage.from_settings(settings)
    media_service = ContentMediaService(
        storage, upload_store=InMemoryVideoUploadStore()
    )
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    lease = RedisPublishLease(redis, ttl_seconds=120)

    async def snapshot_loader(db: AsyncSession, job: MiniappPublishJob):
        return await load_publish_snapshot(db, job, storage=storage)

    async def persist_heartbeat(
        job_id: str,
        attempt_generation: int,
        attempt_token: str,
        heartbeat_at: datetime,
    ) -> None:
        async with AsyncSessionLocal() as heartbeat_db:
            await heartbeat_db.execute(
                update(MiniappPublishJob)
                .where(
                    MiniappPublishJob.job_id == job_id,
                    MiniappPublishJob.status == "running",
                    MiniappPublishJob.attempt_generation == attempt_generation,
                    MiniappPublishJob.attempt_token == attempt_token,
                )
                .values(heartbeat_at=heartbeat_at)
            )
            await heartbeat_db.commit()

    return (
        PublishDependencies(
            storage=storage,
            media_service=media_service,
            verifier=HttpPublicVerifier(allowed_host="media.example.invalid"),
            lease=lease,
            load_snapshot=snapshot_loader,
            public_base_url=PUBLIC_BASE_URL,
            persist_heartbeat=persist_heartbeat,
        ),
        redis,
    )


async def prepare_publish_job_retry(
    db: AsyncSession,
    job_id: str,
    error: BaseException,
    *,
    now=lambda: datetime.now(timezone.utc),
    expected_generation: int,
    expected_token: str,
) -> bool:
    job = await db.scalar(
        select(MiniappPublishJob)
        .where(MiniappPublishJob.job_id == job_id)
        .with_for_update()
    )
    if job is None or job.status in {"succeeded", "failed", "rolled_back"}:
        return False
    if (
        job.attempt_generation != expected_generation
        or job.attempt_token != expected_token
    ):
        return False
    delay_seconds = retry_delay_for(error, job.transient_publish_retry_count)
    if delay_seconds is None:
        return False
    next_generation = job.attempt_generation + 1
    next_token = uuid4().hex
    transitioned = await db.execute(
        update(MiniappPublishJob)
        .where(
            MiniappPublishJob.job_id == job_id,
            MiniappPublishJob.status.in_(("queued", "running")),
            MiniappPublishJob.attempt_generation == expected_generation,
            MiniappPublishJob.attempt_token == expected_token,
        )
        .values(
            attempt_generation=next_generation,
            attempt_token=next_token,
            transient_publish_retry_count=(
                MiniappPublishJob.transient_publish_retry_count + 1
            ),
            status="queued",
            progress_stage="retrying",
            error_code=error_code(error),
            heartbeat_at=None,
        )
    )
    if transitioned.rowcount != 1:
        await db.rollback()
        return False
    db.add(
        MiniappPublishOutbox(
            outbox_id=f"MCO-{uuid4().hex[:20]}",
            job_id=job_id,
            attempt_generation=next_generation,
            attempt_token=next_token,
            available_at=now() + timedelta(seconds=delay_seconds),
        )
    )
    await db.commit()
    return True


async def fail_publish_job(
    db: AsyncSession,
    job_id: str,
    error: BaseException,
    *,
    expected_generation: int,
    expected_token: str,
    storage=None,
) -> bool:
    outcome = await reconcile_publish_terminal_failure(
        db,
        job_id,
        error,
        expected_generation=expected_generation,
        expected_token=expected_token,
        storage=storage,
    )
    return outcome is not None


async def _send_terminal_alert(job_id: str) -> None:
    async with AsyncSessionLocal() as db:
        job = await db.get(MiniappPublishJob, job_id)
        if job is None:
            return
        await send_content_publish_alert(
            job.channel,
            job.job_id,
            job.progress_stage or "failed",
            job.error_code or "unknown",
            f"{ADMIN_CONTENT_URL}?job={job.job_id}",
        )


async def _execute_owned_job(
    job_id: str, attempt_generation: int, attempt_token: str
) -> None:
    dependencies, redis = await _default_dependencies()
    try:
        async with AsyncSessionLocal() as db:
            await execute_publish_job(
                db,
                job_id,
                dependencies=dependencies,
                attempt_generation=attempt_generation,
                attempt_token=attempt_token,
            )
    finally:
        await redis.aclose()


@celery_app.task(
    bind=True,
    name="app.workers.miniapp_content_publish.run_miniapp_publish",
    acks_late=True,
    reject_on_worker_lost=True,
    soft_time_limit=PUBLISH_SOFT_LIMIT_SECONDS,
    time_limit=PUBLISH_HARD_LIMIT_SECONDS,
    max_retries=0,
)
def run_miniapp_publish(
    self, job_id: str, attempt_generation: int, attempt_token: str
) -> None:
    try:
        run_async(_execute_owned_job(job_id, attempt_generation, attempt_token))
    except (MiniappContentError, StorageError, MediaError) as exc:
        async def retry_or_fail_and_alert() -> None:
            async with AsyncSessionLocal() as db:
                queued = await prepare_publish_job_retry(
                    db,
                    job_id,
                    exc,
                    expected_generation=attempt_generation,
                    expected_token=attempt_token,
                )
                if queued:
                    return
                transitioned = await fail_publish_job(
                    db,
                    job_id,
                    exc,
                    expected_generation=attempt_generation,
                    expected_token=attempt_token,
                    storage=_video_cleanup_storage(),
                )
            if transitioned:
                await _send_terminal_alert(job_id)

        run_async(retry_or_fail_and_alert())


@celery_app.task(
    name="app.workers.miniapp_content_publish.dispatch_miniapp_publish_outbox"
)
def dispatch_miniapp_publish_outbox(batch_size: int = 50) -> dict[str, int]:
    summary = run_async(dispatch_publish_outbox(batch_size=batch_size))
    return {
        "claimed": summary.claimed,
        "dispatched": summary.dispatched,
        "failed": summary.failed,
    }


@celery_app.task(name="app.workers.miniapp_content_publish.reconcile_video_quarantine_cleanup")
def reconcile_video_quarantine_cleanup_task(batch_size: int = 50) -> dict[str, int]:
    summary = run_async(reconcile_video_quarantine_cleanup(batch_size=batch_size))
    return {"claimed": summary.claimed, "cleaned": summary.cleaned, "failed": summary.failed}


@celery_app.task(name="app.workers.miniapp_content_publish.reconcile_image_pair_cleanup")
def reconcile_image_pair_cleanup_task(batch_size: int = 50) -> dict[str, int]:
    summary = run_async(reconcile_image_pair_cleanup(batch_size=batch_size))
    return {"claimed": summary.claimed, "cleaned": summary.cleaned, "failed": summary.failed}


@celery_app.task(name="app.workers.miniapp_content_publish.reconcile_private_media_cleanup")
def reconcile_private_media_cleanup_task(batch_size: int = 50) -> dict[str, int]:
    summary = run_async(reconcile_private_media_cleanup(batch_size=batch_size))
    return {"claimed": summary.claimed, "cleaned": summary.cleaned, "failed": summary.failed}


async def _channel_lease_allows_watchdog_reconciliation(lease, channel: str) -> bool:
    owner, ttl = await lease.owner_ttl(channel)
    return owner is None and ttl == -2


async def recover_stale_publish_jobs(
    *,
    now: datetime | None = None,
    max_recoveries: int = WATCHDOG_MAX_RECOVERIES,
    storage=None,
    lease=None,
    session_factory=None,
) -> tuple[int, int]:
    current = now or datetime.now(timezone.utc)
    redis = None
    if lease is None:
        redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        lease = RedisPublishLease(redis)
    session_factory = session_factory or AsyncSessionLocal
    recovered = 0
    failed = 0
    alert_ids: list[str] = []
    try:
        async with session_factory() as db:
            current_delivery_exists = exists(
                select(MiniappPublishOutbox.outbox_id).where(
                    MiniappPublishOutbox.job_id == MiniappPublishJob.job_id,
                    MiniappPublishOutbox.attempt_generation
                    == MiniappPublishJob.attempt_generation,
                    MiniappPublishOutbox.attempt_token == MiniappPublishJob.attempt_token,
                    or_(
                        MiniappPublishOutbox.status.in_(("pending", "claiming")),
                        and_(
                            MiniappPublishOutbox.status == "delivered",
                            MiniappPublishOutbox.delivered_at.is_not(None),
                            MiniappPublishOutbox.delivered_at
                            >= current - WATCHDOG_DELIVERED_STALE_AFTER,
                        ),
                    ),
                )
            )
            jobs = (
                await db.scalars(
                    select(MiniappPublishJob)
                    .where(
                        or_(
                            and_(
                                MiniappPublishJob.status == "running",
                                MiniappPublishJob.heartbeat_at
                                < current - WATCHDOG_STALE_AFTER,
                            ),
                            and_(
                                MiniappPublishJob.status == "queued",
                                MiniappPublishJob.updated_at
                                < current - WATCHDOG_STALE_AFTER,
                                ~current_delivery_exists,
                            ),
                        )
                    )
                    .order_by(MiniappPublishJob.created_at)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for job in jobs:
                if not await _channel_lease_allows_watchdog_reconciliation(
                    lease, job.channel
                ):
                    continue
                if (
                    job.recovery_metadata_version
                    != PUBLISH_RECOVERY_METADATA_VERSION
                ):
                    outcome = await reconcile_publish_terminal_failure(
                        db,
                        job.job_id,
                        WorkerLostError("legacy publish recovery metadata missing"),
                        expected_generation=job.attempt_generation,
                        expected_token=job.attempt_token,
                        storage=None,
                        now=lambda: current,
                    )
                    if outcome is not None:
                        failed += 1
                        alert_ids.append(job.job_id)
                    continue
                recoveries = job.recovery_count
                if recoveries < max_recoveries:
                    missing_delivery = job.status == "queued"
                    previous_generation = job.attempt_generation
                    previous_token = job.attempt_token
                    next_generation = previous_generation + 1
                    next_token = uuid4().hex
                    progress_stage = (
                        "recovering_missing_delivery"
                        if missing_delivery
                        else "recovering_worker_lost"
                    )
                    transitioned = await db.execute(
                        update(MiniappPublishJob)
                        .where(
                            MiniappPublishJob.job_id == job.job_id,
                            MiniappPublishJob.status == job.status,
                            MiniappPublishJob.attempt_generation
                            == previous_generation,
                            MiniappPublishJob.attempt_token == previous_token,
                        )
                        .values(
                            attempt_generation=next_generation,
                            attempt_token=next_token,
                            recovery_count=MiniappPublishJob.recovery_count + 1,
                            status="queued",
                            progress_stage=progress_stage,
                            heartbeat_at=None,
                        )
                    )
                    if transitioned.rowcount != 1:
                        continue
                    db.add(
                        MiniappPublishOutbox(
                            outbox_id=f"MCO-{uuid4().hex[:20]}",
                            job_id=job.job_id,
                            attempt_generation=next_generation,
                            attempt_token=next_token,
                            available_at=current,
                        )
                    )
                    recovered += 1
                else:
                    reconciliation_storage = storage
                    if reconciliation_storage is None and job.result_release_id:
                        reconciliation_storage = _video_cleanup_storage()
                    outcome = await reconcile_publish_terminal_failure(
                        db,
                        job.job_id,
                        WorkerLostError("publish worker heartbeat expired"),
                        expected_generation=job.attempt_generation,
                        expected_token=job.attempt_token,
                        storage=reconciliation_storage,
                        now=lambda: current,
                    )
                    if outcome is None:
                        continue
                    if outcome == "live":
                        recovered += 1
                    else:
                        failed += 1
                    alert_ids.append(job.job_id)
            await db.commit()
        for job_id in alert_ids:
            await _send_terminal_alert(job_id)
    finally:
        if redis is not None:
            await redis.aclose()
    return recovered, failed


@celery_app.task(
    name="app.workers.miniapp_content_publish.watchdog_miniapp_publish_jobs"
)
def watchdog_miniapp_publish_jobs() -> dict[str, int]:
    recovered, failed = run_async(recover_stale_publish_jobs())
    return {"recovered": recovered, "failed": failed}


@celery_app.task(name="app.workers.miniapp_content_publish.probe")
def probe_miniapp_content_publish() -> dict[str, bool]:
    return {"ok": True}


__all__ = [
    "dispatch_miniapp_publish_outbox",
    "fail_publish_job",
    "load_guide_snapshot",
    "load_owner_snapshot",
    "load_publish_snapshot",
    "prepare_publish_job_retry",
    "probe_miniapp_content_publish",
    "recover_stale_publish_jobs",
    "reconcile_video_quarantine_cleanup",
    "reconcile_video_quarantine_cleanup_task",
    "reconcile_image_pair_cleanup",
    "reconcile_image_pair_cleanup_task",
    "reconcile_private_media_cleanup",
    "reconcile_private_media_cleanup_task",
    "run_miniapp_publish",
    "watchdog_miniapp_publish_jobs",
]
