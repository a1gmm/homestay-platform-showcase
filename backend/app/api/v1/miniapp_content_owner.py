"""Owner-channel content-management API, with no storage implementation leaks."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import DBSession, require_role
from app.models.miniapp_content import (
    MiniappContentRelease,
    MiniappContentWorkspace,
    MiniappImagePairGeneration,
    MiniappImagePairUpload,
    MiniappMedia,
    MiniappPublishJob,
    MiniappVideoUpload,
    OwnerCase,
    OwnerDraftVideo,
)
from app.services.audit import log_action_tx
from app.services.miniapp_content.errors import ContentValidationError, DraftConflictError
from app.services.miniapp_content.media import (
    ContentMediaService,
    InMemoryVideoUploadStore,
    MAX_IMAGE_BYTES,
    MediaStateError,
    StoredMedia,
    VideoUploadRecord,
    public_media_object_key,
)
from app.services.miniapp_content.publisher import (
    PublishedMedia,
    build_owner_manifest,
    enqueue_publish_result,
    enqueue_rollback_result,
    resolve_publish_idempotency,
    resolve_rollback_idempotency,
)
from app.schemas.miniapp_content_owner import OwnerManifestDraft
from app.services.miniapp_content.storage import AliyunContentStorage
from app.workers.miniapp_content_publish import (
    IMAGE_PAIR_LATE_WRITE_WINDOW,
    PRIVATE_MEDIA_UPLOAD_LEASE,
    load_owner_snapshot,
)


router = APIRouter(prefix="/miniapp-content", tags=["miniapp-content"])
_media_service: ContentMediaService | None = None
_UPLOAD_READ_CHUNK_BYTES = 64 * 1024


class DatabaseVideoUploadStore:
    """Synchronous Task 3 store backed by the request transaction's database session."""

    def __init__(self, db: Session) -> None:
        self._db = db

    @staticmethod
    def _record(row: MiniappVideoUpload) -> VideoUploadRecord:
        stored = None
        if row.status == "finalized":
            stored = StoredMedia(
                row.draft_key, row.expected_sha256, row.declared_size,
                row.mime_type or "video/mp4",
            )
        return VideoUploadRecord(
            media_id=row.media_id, quarantine_key=row.quarantine_key, draft_key=row.draft_key,
            original_name=row.original_name, declared_size=row.declared_size,
            expected_sha256=row.expected_sha256, status=row.status, stored_media=stored,
            error=row.error, pending_draft_cleanup=row.pending_draft_cleanup,
            width=row.width, height=row.height,
        )

    def create(self, record: VideoUploadRecord) -> None:
        self._db.add(MiniappVideoUpload(
            media_id=record.media_id, quarantine_key=record.quarantine_key,
            draft_key=record.draft_key, original_name=record.original_name,
            declared_size=record.declared_size, expected_sha256=record.expected_sha256,
            status=record.status,
        ))
        self._db.flush()

    def get(self, media_id: str) -> VideoUploadRecord:
        row = self._db.get(MiniappVideoUpload, media_id)
        if row is None:
            raise MediaStateError("unknown video upload")
        return self._record(row)

    def claim(self, media_id: str) -> VideoUploadRecord:
        row = self._db.scalar(
            select(MiniappVideoUpload)
            .where(MiniappVideoUpload.media_id == media_id)
            .with_for_update()
        )
        if row is None:
            raise MediaStateError("unknown video upload")
        if row.status == "finalized":
            return self._record(row)
        if row.status != "uploading":
            raise MediaStateError("video upload cannot finalize from its current state")
        row.status = "finalizing"
        self._db.flush()
        return self._record(row)

    def finalize(self, media_id: str, stored_media: StoredMedia) -> None:
        row = self._db.scalar(
            select(MiniappVideoUpload)
            .where(MiniappVideoUpload.media_id == media_id)
            .with_for_update()
        )
        if row is None or row.status != "finalizing":
            raise MediaStateError("video upload lost its finalizing claim")
        row.status = "finalized"
        row.mime_type = stored_media.mime_type
        row.error = None
        row.pending_draft_cleanup = False
        self._db.flush()

    def set_dimensions(self, media_id: str, width: int, height: int) -> None:
        row = self._db.scalar(
            select(MiniappVideoUpload)
            .where(MiniappVideoUpload.media_id == media_id)
            .with_for_update()
        )
        if row is None or row.status != "finalizing":
            raise MediaStateError("video upload lost its finalizing claim")
        row.width = width
        row.height = height
        self._db.flush()

    def reconcile_after_finalize_error(self, media_id: str) -> VideoUploadRecord:
        self._db.rollback()
        return self.get(media_id)

    def prepare_retry(self, media_id: str, error: str, *, pending_draft_cleanup: bool) -> None:
        row = self._db.get(MiniappVideoUpload, media_id)
        if row is None or row.status != "finalizing":
            raise MediaStateError("video upload lost its finalizing claim")
        row.status = "uploading"
        row.error = error[:240]
        row.pending_draft_cleanup = pending_draft_cleanup
        self._db.flush()

    def fail(self, media_id: str, error: str) -> None:
        row = self._db.get(MiniappVideoUpload, media_id)
        if row is not None and row.status != "finalized":
            row.status = "failed"
            row.error = error[:240]
            self._db.flush()


class _LazyContentStorage:
    """Defers storage setup until a non-empty draft actually reads media."""

    def __getattr__(self, name: str):
        return getattr(_content_media_service()._storage, name)


def _content_media_service() -> ContentMediaService:
    global _media_service
    if _media_service is None:
        from app.core.config import settings

        _media_service = ContentMediaService(
            AliyunContentStorage.from_settings(settings),
            upload_store=InMemoryVideoUploadStore(),
        )
    return _media_service


def _database_video_service(db: Session) -> ContentMediaService:
    return ContentMediaService(
        _content_media_service()._storage,
        upload_store=DatabaseVideoUploadStore(db),
    )


async def _read_image_upload_limited(file: UploadFile) -> bytes:
    """Read at most the media service's image limit plus one byte in fixed chunks."""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > MAX_IMAGE_BYTES:
            raise ContentValidationError("image upload exceeds the size limit")
        chunks.append(chunk)


class OwnerImageInput(BaseModel):
    """Feature-local draft IDs for one logical public image."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    alt: str = Field(min_length=1, max_length=200)
    thumbnail_media_id: str = Field(alias="thumbnailMediaId", min_length=1, max_length=24)
    display_media_id: str = Field(alias="displayMediaId", min_length=1, max_length=24)


class OwnerCaseInput(BaseModel):
    """Persistence input, deliberately distinct from the generated public manifest."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    case_id: str | None = Field(default=None, alias="caseId", min_length=1, max_length=24)
    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    is_visible: bool = Field(default=False, alias="isVisible")
    primary_media_id: str | None = Field(default=None, alias="primaryMediaId", max_length=24)
    detail_images: list[OwnerImageInput] = Field(
        default_factory=list, alias="detailImages", max_length=20
    )
    privacy_confirmed: bool = Field(default=False, alias="privacyConfirmed")


class OwnerDraftVideoInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    media_id: str = Field(alias="mediaId", min_length=1, max_length=24)
    poster_media_id: str = Field(
        alias="posterMediaId", min_length=1, max_length=24
    )
    alt: str = Field(min_length=1, max_length=200)


class OwnerDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)
    cases: list[OwnerCaseInput] = Field(default_factory=list)
    video: OwnerDraftVideoInput | None = None


class RevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=0)


class VideoUploadInput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    original_name: str = Field(alias="originalName", min_length=1, max_length=255)
    byte_size: int = Field(alias="byteSize", ge=1, le=80 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def _case_output(row: OwnerCase) -> dict:
    return {
        "caseId": row.case_id,
        "title": row.title,
        "description": row.description,
        "isVisible": row.is_visible,
        "primaryMediaId": row.primary_media_id,
        "detailImages": row.detail_media_ids,
        "privacyConfirmed": row.privacy_confirmed_by is not None,
    }


async def _validate_case_media(db, case: OwnerCaseInput) -> None:
    media_ids = [case.primary_media_id] if case.primary_media_id else []
    for image in case.detail_images:
        if image.thumbnail_media_id == image.display_media_id:
            raise ContentValidationError("owner image derivatives must use distinct media items")
        media_ids.extend((image.thumbnail_media_id, image.display_media_id))
    if len(set(media_ids)) != len(media_ids):
        raise ContentValidationError("owner case cannot repeat a media item")
    loaded: dict[str, MiniappMedia] = {}
    for media_id in media_ids:
        media = await db.get(MiniappMedia, media_id)
        if (
            media is None
            or media.media_type != "image"
            or media.reference_status not in {"draft", "published"}
        ):
            raise ContentValidationError("owner case references unavailable image media")
        loaded[media_id] = media
    for image in case.detail_images:
        thumbnail = loaded[image.thumbnail_media_id]
        display = loaded[image.display_media_id]
        if (
            thumbnail.derivative_role != "thumbnail"
            or display.derivative_role != "display"
            or thumbnail.derivative_set_id is None
            or thumbnail.derivative_set_id != display.derivative_set_id
            or thumbnail.source_sha256 is None
            or thumbnail.source_sha256 != display.source_sha256
        ):
            raise ContentValidationError(
                "owner image derivatives must be one server-generated source pair"
            )
    if case.is_visible and (not case.privacy_confirmed or not case.primary_media_id):
        raise ContentValidationError("visible owner case requires privacy confirmation and hero media")


async def _validate_draft_video(db, video: OwnerDraftVideoInput | None) -> None:
    if video is None:
        return
    if video.media_id == video.poster_media_id:
        raise ContentValidationError("owner video and poster must use distinct media items")
    media = await db.get(MiniappMedia, video.media_id)
    upload = await db.get(MiniappVideoUpload, video.media_id)
    poster = await db.get(MiniappMedia, video.poster_media_id)
    if (
        media is None
        or media.media_type != "video"
        or media.mime_type != "video/mp4"
        or media.reference_status not in {"draft", "published"}
        or upload is None
        or upload.status != "finalized"
        or upload.mime_type != "video/mp4"
        or upload.width is None
        or upload.height is None
        or upload.draft_key != media.object_key
        or upload.expected_sha256 != media.sha256
        or upload.declared_size != media.size_bytes
    ):
        raise ContentValidationError("owner video is not durably finalized")
    if (
        poster is None
        or poster.media_type != "image"
        or poster.reference_status not in {"draft", "published"}
    ):
        raise ContentValidationError("owner video poster is unavailable")


def _video_output(row: OwnerDraftVideo | None) -> dict | None:
    if row is None:
        return None
    return {
        "mediaId": row.video_media_id,
        "posterMediaId": row.poster_media_id,
        "alt": row.alt,
    }


@router.get("/owner/draft")
async def get_owner_draft(
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    workspace = await db.get(MiniappContentWorkspace, "owner")
    rows = (
        await db.scalars(
            select(OwnerCase)
            .where(OwnerCase.deleted_at.is_(None))
            .order_by(OwnerCase.sort_order, OwnerCase.case_id)
        )
    ).all()
    video = await db.get(OwnerDraftVideo, "owner")
    result = {
        "revision": workspace.revision if workspace is not None else 0,
        "cases": [_case_output(row) for row in rows],
    }
    if video is not None:
        result["video"] = _video_output(video)
    return result


@router.put("/owner/draft")
async def save_owner_draft(
    body: OwnerDraftInput,
    db: DBSession,
    current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    provided_case_ids = [case.case_id for case in body.cases if case.case_id]
    if len(provided_case_ids) != len(set(provided_case_ids)):
        raise ContentValidationError("owner case identifiers must be unique")
    for case in body.cases:
        await _validate_case_media(db, case)
    await _validate_draft_video(db, body.video)

    workspace = await db.get(MiniappContentWorkspace, "owner")
    try:
        async with db.begin_nested():
            if workspace is None:
                if body.revision != 0:
                    raise DraftConflictError("saved draft revision no longer matches the request")
                db.add(MiniappContentWorkspace(channel="owner", revision=1, is_dirty=True))
                await db.flush()
                next_revision = 1
            else:
                result = await db.execute(
                    update(MiniappContentWorkspace)
                    .where(
                        MiniappContentWorkspace.channel == "owner",
                        MiniappContentWorkspace.revision == body.revision,
                    )
                    .values(revision=body.revision + 1, is_dirty=True)
                )
                if result.rowcount != 1:
                    raise DraftConflictError("saved draft revision no longer matches the request")
                next_revision = body.revision + 1

            existing_rows = {
                row.case_id: row
                for row in (await db.scalars(select(OwnerCase))).all()
            }
            active_ids: set[str] = set()
            changed_at = datetime.now(timezone.utc)
            for index, case in enumerate(body.cases):
                case_id = case.case_id or f"MOC-{uuid4().hex[:20]}"
                row = existing_rows.get(case_id)
                if row is None:
                    row = OwnerCase(case_id=case_id)
                    db.add(row)
                active_ids.add(case_id)
                row.title = case.title
                row.description = case.description
                row.sort_order = index
                row.revision = next_revision
                row.is_visible = case.is_visible
                row.primary_media_id = case.primary_media_id
                row.detail_media_ids = [
                    image.model_dump(by_alias=True) for image in case.detail_images
                ]
                row.deleted_at = None
                if case.privacy_confirmed:
                    if row.privacy_confirmed_by is None:
                        row.privacy_confirmed_by = current_user["user_id"]
                        row.privacy_confirmed_at = changed_at
                else:
                    row.privacy_confirmed_by = None
                    row.privacy_confirmed_at = None
            for case_id, row in existing_rows.items():
                if case_id not in active_ids and row.deleted_at is None:
                    row.deleted_at = changed_at
                    row.revision = next_revision

            video_row = await db.get(OwnerDraftVideo, "owner")
            if body.video is None:
                if video_row is not None:
                    await db.delete(video_row)
            elif video_row is None:
                db.add(OwnerDraftVideo(
                    channel="owner",
                    video_media_id=body.video.media_id,
                    poster_media_id=body.video.poster_media_id,
                    alt=body.video.alt,
                    revision=next_revision,
                ))
            else:
                video_row.video_media_id = body.video.media_id
                video_row.poster_media_id = body.video.poster_media_id
                video_row.alt = body.video.alt
                video_row.revision = next_revision
            await log_action_tx(
                db, current_user["user_id"], "content.owner.save", "miniapp_content_workspace", "owner",
                after_data={"revision": next_revision, "case_count": len(body.cases)},
            )
    except IntegrityError as exc:
        raise DraftConflictError("saved draft revision no longer matches the request") from exc
    await db.commit()
    saved_rows = (
        await db.scalars(
            select(OwnerCase)
            .where(OwnerCase.deleted_at.is_(None))
            .order_by(OwnerCase.sort_order)
        )
    ).all()
    result = {
        "revision": next_revision,
        "cases": [_case_output(row) for row in saved_rows],
    }
    saved_video = await db.get(OwnerDraftVideo, "owner")
    if saved_video is not None:
        result["video"] = _video_output(saved_video)
    return result


def _preview_manifest(snapshot, service: ContentMediaService) -> dict:
    if not snapshot.cases:
        return OwnerManifestDraft(cases=[]).model_dump(by_alias=True, exclude_none=True)
    assert snapshot.hero is not None
    refs = [snapshot.hero]
    for case in snapshot.cases:
        refs.append(case.hero)
        for image in case.images:
            refs.extend((image.thumbnail, image.display))
    if snapshot.video is not None:
        refs.extend((snapshot.video.video, snapshot.video.poster))
    promoted: dict[str, PublishedMedia] = {}
    for ref in refs:
        suffix = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "video/mp4": "mp4",
        }.get(ref.stored.mime_type)
        if suffix is None:
            raise ContentValidationError("owner draft media type is unsupported")
        promoted[ref.stored.object_key] = PublishedMedia(
            public_media_object_key(
                ref.stored,
                derivative_set_id=ref.derivative_set_id,
                derivative_role=ref.derivative_role,
            ), ref.stored.sha256,
            ref.stored.byte_size, ref.stored.mime_type,
        )
    manifest = build_owner_manifest(
        snapshot, version=f"preview-{snapshot.revision}", published_at=datetime.now(timezone.utc),
        promoted_media=promoted,
    )
    urls = {
        ref.media_id: service.sign_draft_preview(ref.stored.object_key, 900)
        for ref in refs
    }
    manifest_refs = [(manifest["hero"], snapshot.hero)]
    for manifest_case, snapshot_case in zip(manifest["cases"], snapshot.cases):
        manifest_refs.append((manifest_case["hero"], snapshot_case.hero))
        for manifest_image, snapshot_image in zip(
            manifest_case["images"], snapshot_case.images
        ):
            manifest_refs.extend((
                (manifest_image["thumbnail"], snapshot_image.thumbnail),
                (manifest_image["display"], snapshot_image.display),
            ))
    if snapshot.video is not None:
        manifest_refs.extend((
            (manifest["video"], snapshot.video.video),
            (manifest["video"]["poster"], snapshot.video.poster),
        ))
    for item, ref in manifest_refs:
        item["url"] = urls[ref.media_id]
    return manifest


@router.post("/owner/preview")
async def preview_owner_draft(
    body: RevisionInput,
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    async with db.begin():
        snapshot = await load_owner_snapshot(
            db, SimpleNamespace(channel="owner"), storage=_LazyContentStorage(),
            allow_empty=True, lock_workspace=True,
        )
        if snapshot.revision != body.revision:
            raise DraftConflictError("preview revision is not the saved draft revision")
    if not snapshot.cases:
        return _preview_manifest(snapshot, None)
    service = _content_media_service()
    return _preview_manifest(snapshot, service)


@router.post("/media")
async def upload_media(
    db: DBSession,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    content = await _read_image_upload_limited(file)
    service = _content_media_service()
    prepared = service.prepare_draft_media(
        content, file.content_type or "", file.filename or ""
    )
    stored = prepared.stored
    media = MiniappMedia(
        media_id=f"MCM-{uuid4().hex[:20]}", object_key=stored.object_key, media_type="image",
        mime_type=stored.mime_type, size_bytes=stored.byte_size, sha256=stored.sha256,
        reference_status="unreferenced", pending_private_cleanup=True,
        cleanup_next_attempt_at=datetime.now(timezone.utc) + PRIVATE_MEDIA_UPLOAD_LEASE,
    )
    db.add(media)
    await db.commit()
    service.write_draft_media(prepared)
    media.reference_status = "draft"
    await log_action_tx(db, current_user["user_id"], "content.media.upload", "miniapp_media", media.media_id)
    media.pending_private_cleanup = False
    await db.commit()
    return {"mediaId": media.media_id, "mediaType": media.media_type, "mimeType": media.mime_type, "sizeBytes": media.size_bytes, "sha256": media.sha256}


def _derivative_media_output(media: MiniappMedia) -> dict:
    return {
        "mediaId": media.media_id,
        "mediaType": media.media_type,
        "mimeType": media.mime_type,
        "sizeBytes": media.size_bytes,
        "sha256": media.sha256,
        "derivativeRole": media.derivative_role,
    }


def _persisted_pair_output(rows: list[MiniappMedia]) -> dict:
    by_role = {row.derivative_role: row for row in rows}
    if set(by_role) != {"thumbnail", "display"} or len(rows) != 2:
        raise ContentValidationError("stored image derivative set is incomplete")
    thumbnail = by_role["thumbnail"]
    display = by_role["display"]
    if (
        thumbnail.derivative_set_id is None
        or thumbnail.derivative_set_id != display.derivative_set_id
        or thumbnail.source_sha256 is None
        or thumbnail.source_sha256 != display.source_sha256
    ):
        raise ContentValidationError("stored image derivative provenance is inconsistent")
    return {
        "derivativeSetId": thumbnail.derivative_set_id,
        "sourceSha256": thumbnail.source_sha256,
        "thumbnail": _derivative_media_output(thumbnail),
        "display": _derivative_media_output(display),
    }


def _reservation_keys(pair, generation: int) -> tuple[str, str]:
    prefix = f"miniapp/drafts/image-sets/{pair.derivative_set_id}/g{generation}"
    return (
        f"{prefix}/thumbnail-{pair.thumbnail.stored.sha256}.jpg",
        f"{prefix}/display-{pair.display.stored.sha256}.jpg",
    )


def _reservation_matches_pair(
    reservation: MiniappImagePairUpload, pair
) -> bool:
    return (
        reservation.source_sha256 == pair.source_sha256
        and reservation.thumbnail_media_id == pair.thumbnail.media_id
        and reservation.display_media_id == pair.display.media_id
        and reservation.thumbnail_sha256 == pair.thumbnail.stored.sha256
        and reservation.display_sha256 == pair.display.stored.sha256
        and reservation.thumbnail_size == pair.thumbnail.stored.byte_size
        and reservation.display_size == pair.display.stored.byte_size
        and reservation.thumbnail_width == pair.thumbnail.width
        and reservation.thumbnail_height == pair.thumbnail.height
        and reservation.display_width == pair.display.width
        and reservation.display_height == pair.display.height
    )


async def _ensure_image_pair_reservation(db, pair) -> MiniappImagePairUpload:
    reservation = await db.get(MiniappImagePairUpload, pair.derivative_set_id)
    if reservation is None:
        thumbnail_key, display_key = _reservation_keys(pair, 1)
        reservation = MiniappImagePairUpload(
            derivative_set_id=pair.derivative_set_id,
            source_sha256=pair.source_sha256,
            status="reserved",
            key_generation=1,
            thumbnail_media_id=pair.thumbnail.media_id,
            display_media_id=pair.display.media_id,
            thumbnail_key=thumbnail_key,
            display_key=display_key,
            thumbnail_sha256=pair.thumbnail.stored.sha256,
            display_sha256=pair.display.stored.sha256,
            thumbnail_size=pair.thumbnail.stored.byte_size,
            display_size=pair.display.stored.byte_size,
            thumbnail_width=pair.thumbnail.width,
            thumbnail_height=pair.thumbnail.height,
            display_width=pair.display.width,
            display_height=pair.display.height,
        )
        db.add(reservation)
        reservation.generations.append(MiniappImagePairGeneration(
            derivative_set_id=pair.derivative_set_id,
            key_generation=1,
            thumbnail_key=thumbnail_key,
            display_key=display_key,
            status="active",
        ))
        try:
            await db.commit()
        except Exception:
            await db.rollback()
            reservation = await db.get(
                MiniappImagePairUpload, pair.derivative_set_id
            )
            if reservation is None:
                raise
    else:
        await db.commit()
    if not _reservation_matches_pair(reservation, pair):
        raise ContentValidationError("image pair reservation does not match source")
    return reservation


async def _locked_image_pair_generation(
    db, reservation: MiniappImagePairUpload
) -> MiniappImagePairGeneration:
    generation = await db.scalar(
        select(MiniappImagePairGeneration)
        .where(
            MiniappImagePairGeneration.derivative_set_id
            == reservation.derivative_set_id,
            MiniappImagePairGeneration.key_generation
            == reservation.key_generation,
        )
        .with_for_update()
    )
    if generation is None:
        raise ContentValidationError("image pair reservation generation is missing")
    if (
        generation.thumbnail_key != reservation.thumbnail_key
        or generation.display_key != reservation.display_key
    ):
        raise ContentValidationError("image pair reservation generation is inconsistent")
    return generation


async def _reservation_media_rows(db, reservation) -> list[MiniappMedia]:
    return list((await db.scalars(
        select(MiniappMedia).where(
            MiniappMedia.media_id.in_((
                reservation.thumbnail_media_id,
                reservation.display_media_id,
            ))
        )
    )).all())


@router.post("/media/image-pair", status_code=201)
async def upload_image_pair(
    db: DBSession,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    content = await _read_image_upload_limited(file)
    service = _content_media_service()
    pair = service.prepare_image_derivative_pair(
        content, file.content_type or "", file.filename or ""
    )
    reservation = await _ensure_image_pair_reservation(db, pair)
    if reservation.status == "finalized":
        return _persisted_pair_output(await _reservation_media_rows(db, reservation))

    reservation = await db.scalar(
        select(MiniappImagePairUpload)
        .where(MiniappImagePairUpload.derivative_set_id == pair.derivative_set_id)
        .with_for_update()
    )
    assert reservation is not None
    current = datetime.now(timezone.utc)
    if reservation.status == "finalized":
        rows = await _reservation_media_rows(db, reservation)
        await db.commit()
        return _persisted_pair_output(rows)
    generation = await _locked_image_pair_generation(db, reservation)
    if generation.status == "cleaning":
        lease_until = generation.next_probe_at
        if lease_until is not None and lease_until.tzinfo is None:
            lease_until = lease_until.replace(tzinfo=timezone.utc)
        if lease_until is not None and lease_until > current:
            await db.rollback()
            raise MediaStateError("image pair cleanup is in progress; retry later")
    if generation.status == "cleaning" or generation.status == "retired":
        generation.status = "cleanup_pending"
        generation.next_probe_at = current
        generation.retire_at = generation.retire_at or (
            current + IMAGE_PAIR_LATE_WRITE_WINDOW
        )
        generation.cleanup_claim_token = None
        reservation.key_generation += 1
        reservation.thumbnail_key, reservation.display_key = _reservation_keys(
            pair, reservation.key_generation
        )
        db.add(MiniappImagePairGeneration(
            derivative_set_id=reservation.derivative_set_id,
            key_generation=reservation.key_generation,
            thumbnail_key=reservation.thumbnail_key,
            display_key=reservation.display_key,
            status="active",
            upload=reservation,
        ))
        reservation.status = "reserved"
        reservation.cleanup_claim_token = None
        reservation.cleanup_next_attempt_at = None
        reservation.error = None
        await db.commit()
        reservation = await db.scalar(
            select(MiniappImagePairUpload)
            .where(MiniappImagePairUpload.derivative_set_id == pair.derivative_set_id)
            .with_for_update()
        )
        assert reservation is not None
        generation = await _locked_image_pair_generation(db, reservation)
    elif generation.status == "cleanup_pending":
        generation.status = "active"
        generation.next_probe_at = None
        generation.retire_at = None
        generation.cleanup_claim_token = None
        generation.error = None
        reservation.status = "reserved"
        reservation.cleanup_next_attempt_at = None
        reservation.cleanup_claim_token = None
        reservation.error = None
        await db.commit()
        reservation = await db.scalar(
            select(MiniappImagePairUpload)
            .where(MiniappImagePairUpload.derivative_set_id == pair.derivative_set_id)
            .with_for_update()
        )
        assert reservation is not None
        generation = await _locked_image_pair_generation(db, reservation)

    pair = service.image_pair_with_keys(
        pair,
        thumbnail_key=reservation.thumbnail_key,
        display_key=reservation.display_key,
    )

    rows = [
        MiniappMedia(
            media_id=derivative.media_id,
            object_key=derivative.stored.object_key,
            media_type="image",
            mime_type=derivative.stored.mime_type,
            size_bytes=derivative.stored.byte_size,
            sha256=derivative.stored.sha256,
            derivative_set_id=pair.derivative_set_id,
            derivative_role=derivative.role,
            source_sha256=pair.source_sha256,
        )
        for derivative in (pair.thumbnail, pair.display)
    ]
    try:
        service.write_image_derivative_pair(pair)
        db.add_all(rows)
        await log_action_tx(
            db,
            current_user["user_id"],
            "content.media.image_pair.upload",
            "miniapp_media",
            pair.derivative_set_id,
            after_data={"source_sha256": pair.source_sha256},
        )
        reservation.status = "finalized"
        generation.status = "finalized"
        generation.next_probe_at = None
        generation.retire_at = None
        generation.cleanup_claim_token = None
        generation.error = None
        reservation.cleanup_next_attempt_at = None
        reservation.cleanup_claim_token = None
        reservation.error = None
        await db.commit()
    except Exception as error:
        await db.rollback()
        reconciled = await db.get(MiniappImagePairUpload, pair.derivative_set_id)
        if reconciled is not None and reconciled.status == "finalized":
            return _persisted_pair_output(
                await _reservation_media_rows(db, reconciled)
            )
        if reconciled is not None:
            failed_at = datetime.now(timezone.utc)
            reconciled.status = "cleanup_pending"
            reconciled.cleanup_next_attempt_at = failed_at
            reconciled.cleanup_claim_token = None
            reconciled.error = f"image_pair_finalize:{error.__class__.__name__}"[:240]
            failed_generation = await db.get(
                MiniappImagePairGeneration,
                (reconciled.derivative_set_id, reconciled.key_generation),
            )
            if failed_generation is None:
                raise ContentValidationError(
                    "image pair reservation generation is missing"
                )
            failed_generation.status = "cleanup_pending"
            failed_generation.next_probe_at = failed_at
            failed_generation.retire_at = failed_at + IMAGE_PAIR_LATE_WRITE_WINDOW
            failed_generation.cleanup_claim_token = None
            failed_generation.error = reconciled.error
            await db.commit()
        raise
    return _persisted_pair_output(rows)


@router.post("/media/video-upload")
async def create_video_upload(
    body: VideoUploadInput,
    db: DBSession,
    current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    upload = await db.run_sync(
        lambda sync_db: _database_video_service(sync_db).create_video_upload(
            body.original_name, body.byte_size, body.sha256
        )
    )
    await log_action_tx(db, current_user["user_id"], "content.media.upload", "miniapp_media", upload.media_id)
    await db.commit()
    return {"mediaId": upload.media_id, "uploadUrl": upload.upload_url, "expiresSeconds": upload.expires_seconds, "method": "PUT", "headers": upload.headers}


@router.post("/media/{media_id}/finalize")
async def finalize_video_upload(
    media_id: str,
    db: DBSession,
    current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    # Promotion is validated/copied first, but the quarantine object remains
    # until the finalized state, media row and audit record are committed.
    try:
        stored = await db.run_sync(
            lambda sync_db: _database_video_service(sync_db).finalize_video_upload(
                media_id, cleanup_quarantine=False
            )
        )
    except Exception as exc:
        await db.rollback()
        upload = await db.get(MiniappVideoUpload, media_id)
        if upload is not None and upload.status != "finalized":
            upload.status = "failed"
            upload.error = exc.__class__.__name__[:240]
            upload.pending_quarantine_cleanup = True
            upload.pending_draft_cleanup = True
            upload.cleanup_next_attempt_at = datetime.now(timezone.utc)
            upload.cleanup_claim_token = None
            await db.commit()
        raise
    media = await db.get(MiniappMedia, media_id)
    if media is None:
        media = MiniappMedia(
            media_id=media_id, object_key=stored.object_key, media_type="video",
            mime_type=stored.mime_type, size_bytes=stored.byte_size, sha256=stored.sha256,
        )
        db.add(media)
        await log_action_tx(db, current_user["user_id"], "content.media.upload", "miniapp_media", media_id)
    upload = await db.get(MiniappVideoUpload, media_id)
    if upload is None:
        raise ContentValidationError("video upload does not exist")
    upload.pending_quarantine_cleanup = True
    await db.commit()
    # This is intentionally post-commit and idempotent.  A process crash or a
    # storage outage leaves the durable flag set; any successful finalize retry
    # observes finalized media and finishes deletion without duplicating it.
    try:
        _content_media_service()._storage.delete(upload.quarantine_key)
    except Exception:
        pass
    else:
        upload.pending_quarantine_cleanup = False
        await db.commit()
    return {"mediaId": media.media_id, "mediaType": media.media_type, "mimeType": media.mime_type, "sizeBytes": media.size_bytes, "sha256": media.sha256}


@router.get("/media/{media_id}/preview-url")
async def media_preview_url(
    media_id: str,
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    media = await db.get(MiniappMedia, media_id)
    if media is None:
        raise ContentValidationError("media does not exist")
    return {"url": _content_media_service().sign_draft_preview(media.object_key, 900), "expiresSeconds": 900}


@router.post("/owner/publish")
async def publish_owner_draft(
    body: RevisionInput,
    db: DBSession,
    current_user: dict = Depends(require_role("admin")),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=120),
) -> dict:
    replay = await resolve_publish_idempotency(
        db, "owner", current_user["user_id"], body.revision, idempotency_key
    )
    if replay is not None:
        return {"jobId": replay.job_id, "channel": replay.channel, "status": replay.status, "expectedRevision": replay.expected_revision}
    workspace = await db.get(MiniappContentWorkspace, "owner")
    if workspace is None or workspace.revision != body.revision:
        raise DraftConflictError("publish revision is not the saved draft revision")
    has_visible_case = await db.scalar(
        select(OwnerCase.case_id).where(
            OwnerCase.revision == body.revision,
            OwnerCase.deleted_at.is_(None),
            OwnerCase.is_visible.is_(True),
        ).limit(1)
    )
    if has_visible_case is None:
        raise ContentValidationError("owner draft has no publishable cases")
    service = _content_media_service()
    snapshot = await load_owner_snapshot(
        db, SimpleNamespace(channel="owner"), storage=service._storage,
    )
    if snapshot.revision != body.revision:
        raise DraftConflictError("publish revision is not the saved draft revision")
    # The same manifest builder used by Task 4 owns publication validation. Build
    # against deterministic private-media placeholders before we create a job.
    _preview_manifest(snapshot, service)
    enqueue_result = await enqueue_publish_result(
        db, "owner", current_user["user_id"], body.revision, idempotency_key
    )
    job = enqueue_result.job
    if enqueue_result.created:
        await log_action_tx(
            db,
            current_user["user_id"],
            "content.owner.publish",
            "miniapp_publish_job",
            job.job_id,
        )
    await db.commit()
    return {"jobId": job.job_id, "channel": job.channel, "status": job.status, "expectedRevision": job.expected_revision}


@router.get("/publish-jobs/{job_id}")
async def get_publish_job(
    job_id: str,
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    job = await db.get(MiniappPublishJob, job_id)
    if job is None:
        raise ContentValidationError("publish job does not exist")
    return {"jobId": job.job_id, "channel": job.channel, "operation": job.operation, "status": job.status, "expectedRevision": job.expected_revision, "targetVersion": job.target_version, "progressStage": job.progress_stage, "progressPercent": job.progress_percent, "errorCode": job.error_code}


@router.get("/owner/releases")
async def list_owner_releases(
    db: DBSession,
    _current_user: dict = Depends(require_role("admin", "operator")),
) -> dict:
    releases = (await db.scalars(select(MiniappContentRelease).where(MiniappContentRelease.channel == "owner").order_by(MiniappContentRelease.created_at.desc()))).all()
    return {"releases": [{"version": row.version, "status": row.status, "publishedAt": row.published_at} for row in releases]}


@router.post("/owner/releases/{version}/rollback")
async def rollback_owner_release(
    version: str,
    db: DBSession,
    current_user: dict = Depends(require_role("admin")),
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=1, max_length=120),
) -> dict:
    replay = await resolve_rollback_idempotency(
        db, "owner", current_user["user_id"], version, idempotency_key
    )
    if replay is not None:
        return {
            "jobId": replay.job_id,
            "channel": replay.channel,
            "status": replay.status,
            "targetVersion": replay.target_version,
        }
    enqueue_result = await enqueue_rollback_result(
        db, "owner", version, current_user["user_id"], idempotency_key
    )
    job = enqueue_result.job
    if enqueue_result.created:
        await log_action_tx(
            db,
            current_user["user_id"],
            "content.owner.rollback",
            "miniapp_publish_job",
            job.job_id,
        )
    await db.commit()
    return {"jobId": job.job_id, "channel": job.channel, "status": job.status, "targetVersion": job.target_version}
