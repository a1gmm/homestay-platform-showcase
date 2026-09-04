"""Durable mini-program content publishing primitives.

Pipeline::

    enqueue(job + outbox, one transaction)
      -> dispatcher claim/commit -> Celery delivery
      -> worker DB claim -> immutable media + manifest
      -> candidate pointer -> public verification
      -> production current pointer -> terminal DB state

Duplicate delivery is expected. Database job state and immutable object read-back
make replay safe; no release object referenced by a manifest is ever deleted.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Protocol
from uuid import uuid4
from urllib.parse import urlparse

import httpx
from kombu.exceptions import OperationalError as BrokerOperationalError

from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.miniapp_content import (
    MiniappChannelConfig,
    MiniappContentRelease,
    MiniappContentWorkspace,
    MiniappMedia,
    MiniappPublishJob,
    MiniappPublishOutbox,
    PUBLISH_RECOVERY_METADATA_VERSION,
)
from app.schemas.miniapp_content_guides import (
    StayGuideDraft,
    StayGuideManifest,
    TravelGuideDraft,
    TravelGuideManifest,
)
from app.services.miniapp_content.contracts import OwnerManifestValidationError, validate_owner_manifest
from app.services.miniapp_content.errors import (
    ContentValidationError,
    DraftConflictError,
    IdempotencyConflictError,
    PointerSwapError,
    PublishBusyError,
    PublicSmokeTimeout,
    PublicVerificationError,
    error_code,
)
from app.services.miniapp_content.media import PublishedMedia, StoredMedia
from app.services.miniapp_content.storage import (
    IMMUTABLE_CACHE_CONTROL,
    ContentStorage,
    StorageConflictError,
    StorageNotFoundError,
)


CURRENT_POINTER_CACHE_CONTROL = "public, max-age=30, must-revalidate"
JSON_CONTENT_TYPE = "application/json"
OUTBOX_CLAIM_TIMEOUT = timedelta(minutes=2)
OUTBOX_MAX_BACKOFF_SECONDS = 300
TERMINAL_JOB_STATUSES = frozenset({"succeeded", "failed", "rolled_back"})
LEGACY_RECOVERY_ERROR_CODE = "legacy_recovery_metadata_missing"
LEGACY_RECOVERY_STAGE = "manual_recovery_required"


@dataclass(frozen=True)
class PublishJobOut:
    job_id: str
    channel: str
    operation: str
    status: str
    expected_revision: int | None
    target_version: str | None
    progress_stage: str | None
    progress_percent: int
    error_code: str | None


@dataclass(frozen=True)
class EnqueueResult:
    job: PublishJobOut
    created: bool


@dataclass(frozen=True)
class DispatchSummary:
    claimed: int = 0
    dispatched: int = 0
    failed: int = 0


@dataclass(frozen=True)
class ReleaseResult:
    release_id: str
    channel: str
    version: str
    status: str


@dataclass(frozen=True)
class DraftMediaRef:
    media_id: str
    stored: StoredMedia
    alt: str
    width: int
    height: int
    derivative_set_id: str | None = None
    derivative_role: str | None = None


@dataclass(frozen=True)
class DraftImageRef:
    alt: str
    thumbnail: DraftMediaRef
    display: DraftMediaRef


@dataclass(frozen=True)
class OwnerDraftCase:
    case_id: str
    title: str
    summary: str
    hero: DraftMediaRef
    images: tuple[DraftImageRef, ...] = ()


@dataclass(frozen=True)
class OwnerDraftVideoRef:
    video: DraftMediaRef
    poster: DraftMediaRef
    alt: str


@dataclass(frozen=True)
class OwnerDraftSnapshot:
    revision: int
    hero: DraftMediaRef | None
    cases: tuple[OwnerDraftCase, ...]
    video: OwnerDraftVideoRef | None = None


@dataclass(frozen=True)
class StructuredGuideDraftSnapshot:
    revision: int
    channel: str
    draft: StayGuideDraft | TravelGuideDraft
    media: Mapping[str, DraftMediaRef]


ManifestSnapshot = OwnerDraftSnapshot | StructuredGuideDraftSnapshot
ManifestBuilder = Callable[..., dict]
ManifestValidator = Callable[[dict], dict]
ManifestMediaRefs = Callable[[ManifestSnapshot], tuple[DraftMediaRef, ...]]


@dataclass(frozen=True)
class ChannelManifestDefinition:
    builder: ManifestBuilder
    validator: ManifestValidator
    media_refs: ManifestMediaRefs
    publish_media_reference_state: bool = False


class MediaPublisher(Protocol):
    def promote_media(
        self,
        media: StoredMedia,
        *,
        derivative_set_id: str | None = None,
        derivative_role: str | None = None,
    ) -> PublishedMedia: ...

    def verify_public_object(
        self,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
        expected_mime: str,
    ) -> None: ...


class PublicVerifier(Protocol):
    async def verify(
        self,
        pointer_url: str,
        *,
        expected_version: str,
        expected_manifest_sha256: str,
        manifest: dict,
    ) -> None: ...


class HttpPublicVerifier:
    """Cache-bypassing public verification against the configured CDN host."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        allowed_host: str = "media.example.invalid",
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._allowed_host = allowed_host
        self._timeout_seconds = timeout_seconds

    def _safe_url(self, url: str, *, expected_prefix: str) -> str:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname != self._allowed_host
            or not parsed.path.startswith(expected_prefix)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
        ):
            raise PublicVerificationError("public verification URL is outside policy")
        return url

    async def _get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        try:
            response = await client.get(
                url,
                params={"_publish_probe": uuid4().hex},
                headers={
                    "Cache-Control": "no-cache, no-store",
                    "Pragma": "no-cache",
                },
                follow_redirects=False,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise PublicSmokeTimeout("public verification timed out") from exc
        except httpx.HTTPError as exc:
            raise PublicVerificationError("public verification transport failed") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise PublicVerificationError("public verification returned a non-success status")
        return response

    async def verify(
        self,
        pointer_url: str,
        *,
        expected_version: str,
        expected_manifest_sha256: str,
        manifest: dict,
    ) -> None:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        try:
            safe_pointer_url = self._safe_url(
                pointer_url, expected_prefix="/miniapp/content/"
            )
            pointer_path = urlparse(safe_pointer_url).path.split("/")
            if len(pointer_path) < 5 or not pointer_path[3]:
                raise PublicVerificationError("public pointer URL has no channel identity")
            expected_channel = pointer_path[3]
            pointer_response = await self._get(client, safe_pointer_url)
            try:
                pointer = json.loads(pointer_response.content)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise PublicVerificationError("public pointer is not valid JSON") from exc
            required_pointer = {
                "schema",
                "channel",
                "version",
                "manifestUrl",
                "sha256",
            }
            if (
                not isinstance(pointer, dict)
                or set(pointer) != required_pointer
                or pointer.get("schema") != "guanhaiju.pointer.v1"
                or pointer.get("channel") != expected_channel
                or pointer.get("version") != expected_version
                or pointer.get("sha256") != expected_manifest_sha256
            ):
                raise PublicVerificationError("public pointer identity does not match release")

            manifest_url = self._safe_url(
                str(pointer["manifestUrl"]), expected_prefix="/miniapp/releases/"
            )
            expected_manifest_path = (
                f"/miniapp/releases/{expected_channel}/{expected_version}/manifest.json"
            )
            if urlparse(manifest_url).path != expected_manifest_path:
                raise PublicVerificationError("public pointer identity does not match release")
            manifest_response = await self._get(client, manifest_url)
            if hashlib.sha256(manifest_response.content).hexdigest() != expected_manifest_sha256:
                raise PublicVerificationError("public manifest SHA-256 does not match pointer")
            try:
                public_manifest = json.loads(manifest_response.content)
                public_manifest = _validate_channel_manifest(
                    expected_channel, public_manifest
                )
            except (
                json.JSONDecodeError,
                UnicodeDecodeError,
                ContentValidationError,
            ) as exc:
                raise PublicVerificationError("public manifest failed contract validation") from exc
            if public_manifest != manifest:
                raise PublicVerificationError("public manifest differs from candidate bytes")

            media: dict[str, tuple[str, str]] = {}

            def collect(value: object) -> None:
                if isinstance(value, dict):
                    url = value.get("url")
                    digest = value.get("sha256")
                    mime_type = value.get("mimeType")
                    if isinstance(url, str) and isinstance(digest, str) and isinstance(mime_type, str):
                        media[url] = (digest, mime_type)
                    for nested in value.values():
                        collect(nested)
                elif isinstance(value, list):
                    for nested in value:
                        collect(nested)

            collect(public_manifest)
            for media_url, (digest, mime_type) in media.items():
                safe_media_url = self._safe_url(media_url, expected_prefix="/miniapp/media/")
                response = await self._get(client, safe_media_url)
                actual_type = response.headers.get("content-type", "").split(";", 1)[0].strip()
                if actual_type != mime_type:
                    raise PublicVerificationError("public media MIME does not match manifest")
                if hashlib.sha256(response.content).hexdigest() != digest:
                    raise PublicVerificationError("public media SHA-256 does not match manifest")
        finally:
            if owns_client:
                await client.aclose()


class PublishLease(Protocol):
    async def acquire(self, channel: str, job_id: str) -> bool: ...

    async def renew(self, channel: str, job_id: str) -> bool: ...

    async def release(self, channel: str, job_id: str) -> bool: ...


class RedisPublishLease:
    """Redis channel lease with compare-and-renew/release Lua operations."""

    _RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
end
return 0
"""
    _RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
end
return 0
"""

    def __init__(self, client: object, *, ttl_seconds: int = 120) -> None:
        if ttl_seconds < 1:
            raise ValueError("publish lease TTL must be positive")
        self._client = client
        self.ttl_seconds = ttl_seconds

    @staticmethod
    def key(channel: str) -> str:
        return f"miniapp:publish:{channel}"

    async def acquire(self, channel: str, job_id: str) -> bool:
        created = await self._client.set(
            self.key(channel), job_id, ex=self.ttl_seconds, nx=True
        )
        if created:
            return True
        owner = await self._client.get(self.key(channel))
        if isinstance(owner, bytes):
            owner = owner.decode()
        # Re-entry is only for a crash after lease acquisition but before the
        # queued -> running database claim commits. Running duplicates are
        # rejected under the job row lock before they can touch this lease.
        if owner == job_id:
            return await self.renew(channel, job_id)
        return False

    async def renew(self, channel: str, job_id: str) -> bool:
        result = await self._client.eval(
            self._RENEW_SCRIPT,
            1,
            self.key(channel),
            job_id,
            self.ttl_seconds,
        )
        return bool(result)

    async def release(self, channel: str, job_id: str) -> bool:
        result = await self._client.eval(
            self._RELEASE_SCRIPT, 1, self.key(channel), job_id
        )
        return bool(result)

    async def owner_ttl(self, channel: str) -> tuple[str | None, int]:
        key = self.key(channel)
        owner = await self._client.get(key)
        if isinstance(owner, bytes):
            owner = owner.decode()
        return owner, int(await self._client.ttl(key))


SnapshotLoader = Callable[[AsyncSession, MiniappPublishJob], Awaitable[ManifestSnapshot]]
HeartbeatPersister = Callable[[str, int, str, datetime], Awaitable[None]]


@dataclass(frozen=True)
class PublishDependencies:
    storage: ContentStorage
    media_service: MediaPublisher
    verifier: PublicVerifier
    lease: PublishLease
    load_snapshot: SnapshotLoader
    public_base_url: str = "https://media.example.invalid"
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    persist_heartbeat: HeartbeatPersister | None = None


def _utc_iso(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _public_media_ref(
    draft: DraftMediaRef,
    published: PublishedMedia,
    public_base_url: str,
) -> dict:
    return {
        "url": f"{public_base_url.rstrip('/')}/{published.object_key}",
        "alt": draft.alt,
        "width": draft.width,
        "height": draft.height,
        "mimeType": published.mime_type,
        "sha256": published.sha256,
    }


def _public_image_derivative_ref(
    draft: DraftMediaRef,
    published: PublishedMedia,
    public_base_url: str,
) -> dict:
    if draft.derivative_set_id is None or draft.derivative_role not in {
        "thumbnail", "display"
    }:
        raise ContentValidationError("image derivative provenance is missing")
    return {
        "url": f"{public_base_url.rstrip('/')}/{published.object_key}",
        "derivativeSetId": draft.derivative_set_id,
        "role": draft.derivative_role,
        "width": draft.width,
        "height": draft.height,
        "bytes": published.byte_size,
        "mimeType": published.mime_type,
        "sha256": published.sha256,
    }


def build_owner_manifest(
    snapshot: OwnerDraftSnapshot,
    *,
    version: str,
    published_at: datetime,
    promoted_media: Mapping[str, PublishedMedia],
    public_base_url: str = "https://media.example.invalid",
) -> dict:
    """Build and validate the one server-owned owner manifest representation."""

    if snapshot.hero is None or len(snapshot.cases) < 4:
        raise ContentValidationError("owner draft has no publishable cases")

    def media(ref: DraftMediaRef) -> dict:
        try:
            promoted = promoted_media[ref.stored.object_key]
        except KeyError as exc:
            raise ContentValidationError("manifest references unpromoted media") from exc
        return _public_media_ref(ref, promoted, public_base_url)

    def image(ref: DraftImageRef) -> dict:
        if (
            ref.thumbnail.derivative_set_id is None
            or ref.thumbnail.derivative_set_id != ref.display.derivative_set_id
            or ref.thumbnail.derivative_role != "thumbnail"
            or ref.display.derivative_role != "display"
        ):
            raise ContentValidationError("image derivative pair provenance is inconsistent")
        derivatives: dict[str, dict] = {}
        for role, derivative in (
            ("thumbnail", ref.thumbnail),
            ("display", ref.display),
        ):
            try:
                promoted = promoted_media[derivative.stored.object_key]
            except KeyError as exc:
                raise ContentValidationError("manifest references unpromoted media") from exc
            derivatives[role] = _public_image_derivative_ref(
                derivative, promoted, public_base_url
            )
        return {"alt": ref.alt, **derivatives}

    payload = {
        "schema": "guanhaiju.owner.v1",
        "version": version,
        "publishedAt": _utc_iso(published_at),
        "hero": media(snapshot.hero),
        "cases": [
            {
                "id": case.case_id,
                "title": case.title,
                "summary": case.summary,
                "hero": media(case.hero),
                "images": [image(item) for item in case.images],
            }
            for case in snapshot.cases
        ],
    }
    if snapshot.video is not None:
        payload["video"] = {
            **media(snapshot.video.video),
            "poster": media(snapshot.video.poster),
            "alt": snapshot.video.alt,
            "mutedByDefault": True,
        }
    try:
        return validate_owner_manifest(payload)
    except OwnerManifestValidationError as exc:
        raise ContentValidationError("owner manifest failed publication validation") from exc


def _guide_public_media_ref(
    ref: DraftMediaRef,
    published: PublishedMedia,
    *,
    alt: str,
    public_base_url: str,
) -> dict:
    return {
        "url": f"{public_base_url.rstrip('/')}/{published.object_key}",
        "alt": alt,
        "width": ref.width,
        "height": ref.height,
        "mimeType": published.mime_type,
        "sha256": published.sha256,
    }


def _guide_image(
    snapshot: StructuredGuideDraftSnapshot,
    media_id: str,
    *,
    alt: str,
    promoted_media: Mapping[str, PublishedMedia],
    public_base_url: str,
) -> dict:
    try:
        ref = snapshot.media[media_id]
        promoted = promoted_media[ref.stored.object_key]
    except KeyError as exc:
        raise ContentValidationError(
            "guide manifest references unpromoted media"
        ) from exc
    return _guide_public_media_ref(
        ref,
        promoted,
        alt=alt,
        public_base_url=public_base_url,
    )


def _validate_owner_public_manifest(payload: dict) -> dict:
    try:
        return validate_owner_manifest(payload)
    except OwnerManifestValidationError as exc:
        raise ContentValidationError(
            "owner manifest failed publication validation"
        ) from exc


def _validate_stay_guide_manifest(payload: dict) -> dict:
    try:
        return StayGuideManifest.model_validate(payload).model_dump(
            by_alias=True, exclude_none=True
        )
    except (TypeError, ValueError) as exc:
        raise ContentValidationError(
            "stay guide manifest failed publication validation"
        ) from exc


def _validate_travel_manifest(payload: dict) -> dict:
    try:
        return TravelGuideManifest.model_validate(payload).model_dump(
            by_alias=True, exclude_none=True
        )
    except (TypeError, ValueError) as exc:
        raise ContentValidationError(
            "travel manifest failed publication validation"
        ) from exc


def build_stay_guide_manifest(
    snapshot: StructuredGuideDraftSnapshot,
    *,
    version: str,
    published_at: datetime,
    promoted_media: Mapping[str, PublishedMedia],
    public_base_url: str = "https://media.example.invalid",
) -> dict:
    if snapshot.channel != "stay_guide" or not isinstance(
        snapshot.draft, StayGuideDraft
    ):
        raise ContentValidationError("stay guide snapshot has the wrong channel")
    draft = snapshot.draft.model_dump(by_alias=True, exclude_none=True)
    for section in draft["sections"].values():
        if not section["visible"]:
            is_faq = "items" in section
            section.clear()
            section.update(
                {
                    "summary": "该章节暂未公开。",
                    "visible": False,
                }
            )
            if is_faq:
                section["items"] = [
                    {
                        "question": "该章节是否公开？",
                        "answer": "该章节暂未公开。",
                    }
                ]
            continue
        image = section.get("image")
        if image is None:
            continue
        section["image"] = _guide_image(
            snapshot,
            image["mediaId"],
            alt=section["summary"],
            promoted_media=promoted_media,
            public_base_url=public_base_url,
        )
    return _validate_stay_guide_manifest(
        {
            "schema": "guanhaiju.stay_guide.v1",
            "version": version,
            "publishedAt": _utc_iso(published_at),
            **draft,
        }
    )


def build_travel_manifest(
    snapshot: StructuredGuideDraftSnapshot,
    *,
    version: str,
    published_at: datetime,
    promoted_media: Mapping[str, PublishedMedia],
    public_base_url: str = "https://media.example.invalid",
) -> dict:
    if snapshot.channel != "travel" or not isinstance(
        snapshot.draft, TravelGuideDraft
    ):
        raise ContentValidationError("travel snapshot has the wrong channel")
    draft = snapshot.draft.model_dump(by_alias=True, exclude_none=True)
    draft["recommendations"] = [
        recommendation
        for recommendation in draft["recommendations"]
        if recommendation["visible"]
    ]
    for recommendation in draft["recommendations"]:
        image = recommendation.get("image")
        if image is None:
            continue
        recommendation["image"] = _guide_image(
            snapshot,
            image["mediaId"],
            alt=recommendation["name"],
            promoted_media=promoted_media,
            public_base_url=public_base_url,
        )
    return _validate_travel_manifest(
        {
            "schema": "guanhaiju.travel.v1",
            "version": version,
            "publishedAt": _utc_iso(published_at),
            **draft,
        }
    )


def _owner_manifest_media_refs(
    snapshot: ManifestSnapshot,
) -> tuple[DraftMediaRef, ...]:
    if not isinstance(snapshot, OwnerDraftSnapshot):
        raise ContentValidationError("owner manifest requires an owner draft snapshot")
    refs: list[DraftMediaRef] = []
    if snapshot.hero is not None:
        refs.append(snapshot.hero)
    for case in snapshot.cases:
        refs.append(case.hero)
        for image_ref in case.images:
            refs.extend((image_ref.thumbnail, image_ref.display))
    if snapshot.video is not None:
        refs.extend((snapshot.video.video, snapshot.video.poster))
    return tuple(refs)


def _guide_manifest_media_refs(
    snapshot: ManifestSnapshot,
) -> tuple[DraftMediaRef, ...]:
    if not isinstance(snapshot, StructuredGuideDraftSnapshot):
        raise ContentValidationError("guide manifest requires a structured snapshot")
    return tuple(snapshot.media.values())


CHANNEL_MANIFEST_REGISTRY: dict[str, ChannelManifestDefinition] = {
    "owner": ChannelManifestDefinition(
        builder=build_owner_manifest,
        validator=_validate_owner_public_manifest,
        media_refs=_owner_manifest_media_refs,
    ),
    "stay_guide": ChannelManifestDefinition(
        builder=build_stay_guide_manifest,
        validator=_validate_stay_guide_manifest,
        media_refs=_guide_manifest_media_refs,
        publish_media_reference_state=True,
    ),
    "travel": ChannelManifestDefinition(
        builder=build_travel_manifest,
        validator=_validate_travel_manifest,
        media_refs=_guide_manifest_media_refs,
        publish_media_reference_state=True,
    ),
}


def _channel_manifest_definition(channel: str) -> ChannelManifestDefinition:
    try:
        return CHANNEL_MANIFEST_REGISTRY[channel]
    except KeyError as exc:
        raise ContentValidationError("content channel has no manifest publisher") from exc


def _validate_channel_manifest(channel: str, payload: dict) -> dict:
    return _channel_manifest_definition(channel).validator(payload)


def _job_out(job: MiniappPublishJob) -> PublishJobOut:
    return PublishJobOut(
        job_id=job.job_id,
        channel=job.channel,
        operation=job.operation,
        status=job.status,
        expected_revision=job.expected_revision,
        target_version=job.target_version,
        progress_stage=job.progress_stage,
        progress_percent=job.progress_percent,
        error_code=job.error_code,
    )


def _fingerprint(operation: str, **payload: object) -> str:
    canonical = json.dumps(
        {"operation": operation, **payload},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


async def resolve_publish_idempotency(
    db: AsyncSession, channel: str, actor_id: str, expected_revision: int, idempotency_key: str,
) -> PublishJobOut | None:
    """Resolve a replay before validating mutable workspace state.

    A replay represents the original request, not a request against today's
    workspace revision.  `_enqueue` remains the race-safe authority for a new
    job after this inexpensive early resolution.
    """
    scope = f"{actor_id}:{channel}:publish"
    fingerprint = _fingerprint("publish", channel=channel, expected_revision=expected_revision, target_version=None)
    existing = await db.scalar(select(MiniappPublishJob).where(
        MiniappPublishJob.idempotency_scope == scope,
        MiniappPublishJob.idempotency_key == idempotency_key,
    ))
    if existing is None:
        return None
    if existing.request_fingerprint != fingerprint:
        raise IdempotencyConflictError("idempotency key was used for a different request")
    return _job_out(existing)


async def resolve_rollback_idempotency(
    db: AsyncSession,
    channel: str,
    actor_id: str,
    target_version: str,
    idempotency_key: str,
) -> PublishJobOut | None:
    scope = f"{actor_id}:{channel}:rollback"
    fingerprint = _fingerprint(
        "rollback",
        channel=channel,
        expected_revision=None,
        target_version=target_version,
    )
    existing = await db.scalar(
        select(MiniappPublishJob).where(
            MiniappPublishJob.idempotency_scope == scope,
            MiniappPublishJob.idempotency_key == idempotency_key,
        )
    )
    if existing is None:
        return None
    if existing.request_fingerprint != fingerprint:
        raise IdempotencyConflictError(
            "idempotency key was used for a different request"
        )
    return _job_out(existing)


async def _enqueue(
    db: AsyncSession,
    *,
    channel: str,
    operation: str,
    actor_id: str,
    idempotency_key: str,
    expected_revision: int | None,
    target_version: str | None,
) -> EnqueueResult:
    scope = f"{actor_id}:{channel}:{operation}"
    fingerprint = _fingerprint(
        operation,
        channel=channel,
        expected_revision=expected_revision,
        target_version=target_version,
    )
    existing = await db.scalar(
        select(MiniappPublishJob).where(
            MiniappPublishJob.idempotency_scope == scope,
            MiniappPublishJob.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflictError("idempotency key was used for a different request")
        return EnqueueResult(_job_out(existing), created=False)

    active = await db.scalar(
        select(MiniappPublishJob.job_id).where(
            MiniappPublishJob.channel == channel,
            MiniappPublishJob.status.in_(("queued", "running")),
        )
    )
    if active is not None:
        raise PublishBusyError("another publish operation is active for this channel")

    attempt_token = uuid4().hex
    job = MiniappPublishJob(
        job_id=f"MCJ-{uuid4().hex[:20]}",
        channel=channel,
        operation=operation,
        expected_revision=expected_revision,
        target_version=target_version,
        actor_id=actor_id,
        idempotency_scope=scope,
        idempotency_key=idempotency_key,
        request_fingerprint=fingerprint,
        attempt_generation=1,
        attempt_token=attempt_token,
        transient_publish_retry_count=0,
        recovery_count=0,
        recovery_metadata_version=PUBLISH_RECOVERY_METADATA_VERSION,
        progress_stage="queued",
    )
    outbox = MiniappPublishOutbox(
        outbox_id=f"MCO-{uuid4().hex[:20]}",
        job_id=job.job_id,
        attempt_generation=1,
        attempt_token=attempt_token,
    )
    try:
        async with db.begin_nested():
            db.add_all([job, outbox])
            await db.flush()
    except IntegrityError as exc:
        raced = await db.scalar(
            select(MiniappPublishJob).where(
                MiniappPublishJob.idempotency_scope == scope,
                MiniappPublishJob.idempotency_key == idempotency_key,
            )
        )
        if raced is not None:
            if raced.request_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    "idempotency key was concurrently used for a different request"
                ) from exc
            return EnqueueResult(_job_out(raced), created=False)
        active = await db.scalar(
            select(MiniappPublishJob.job_id).where(
                MiniappPublishJob.channel == channel,
                MiniappPublishJob.status.in_(("queued", "running")),
            )
        )
        if active is not None:
            raise PublishBusyError("another publish operation became active") from exc
        raise
    return EnqueueResult(_job_out(job), created=True)


async def enqueue_publish_result(
    db: AsyncSession,
    channel: str,
    actor_id: str,
    expected_revision: int,
    idempotency_key: str,
) -> EnqueueResult:
    return await _enqueue(
        db,
        channel=channel,
        operation="publish",
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        expected_revision=expected_revision,
        target_version=None,
    )


async def enqueue_publish(
    db: AsyncSession,
    channel: str,
    actor_id: str,
    expected_revision: int,
    idempotency_key: str,
) -> PublishJobOut:
    return (
        await enqueue_publish_result(
            db,
            channel,
            actor_id,
            expected_revision,
            idempotency_key,
        )
    ).job


async def enqueue_rollback_result(
    db: AsyncSession,
    channel: str,
    target_version: str,
    actor_id: str,
    idempotency_key: str,
) -> EnqueueResult:
    return await _enqueue(
        db,
        channel=channel,
        operation="rollback",
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        expected_revision=None,
        target_version=target_version,
    )


async def enqueue_rollback(
    db: AsyncSession,
    channel: str,
    target_version: str,
    actor_id: str,
    idempotency_key: str,
) -> PublishJobOut:
    return (
        await enqueue_rollback_result(
            db,
            channel,
            target_version,
            actor_id,
            idempotency_key,
        )
    ).job


def _outbox_backoff(attempt_count: int) -> int:
    return min(2 ** max(0, attempt_count - 1), OUTBOX_MAX_BACKOFF_SECONDS)


async def dispatch_publish_outbox(
    batch_size: int = 50,
    *,
    session_factory: async_sessionmaker | None = None,
    send: Callable[[str, int, str], None] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> DispatchSummary:
    """Durably claim eligible rows before broker delivery.

    A crash after broker acceptance and before ``delivered`` intentionally causes
    a duplicate delivery; the worker's terminal-state check absorbs it.
    """

    if batch_size < 1 or batch_size > 500:
        raise ValueError("batch_size must be between 1 and 500")
    if session_factory is None:
        from app.core.database import AsyncSessionLocal

        session_factory = AsyncSessionLocal
    if send is None:
        from app.workers.celery_app import celery_app

        def send(job_id: str, attempt_generation: int, attempt_token: str) -> None:
            celery_app.send_task(
                "app.workers.miniapp_content_publish.run_miniapp_publish",
                args=[job_id, attempt_generation, attempt_token],
                queue="content_publish",
            )

    current = now()
    stale_before = current - OUTBOX_CLAIM_TIMEOUT
    async with session_factory() as claim_session:
        async with claim_session.begin():
            rows = (
                await claim_session.scalars(
                    select(MiniappPublishOutbox)
                    .where(
                        or_(
                            (
                                (MiniappPublishOutbox.status == "pending")
                                & (MiniappPublishOutbox.available_at <= current)
                            ),
                            (
                                (MiniappPublishOutbox.status == "claiming")
                                & (MiniappPublishOutbox.claimed_at <= stale_before)
                            ),
                        )
                    )
                    .order_by(MiniappPublishOutbox.available_at, MiniappPublishOutbox.outbox_id)
                    .limit(batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for row in rows:
                row.status = "claiming"
                row.claimed_at = current
                row.attempt_count += 1
            claimed = [
                (
                    row.outbox_id,
                    row.job_id,
                    row.attempt_generation,
                    row.attempt_token,
                    row.attempt_count,
                )
                for row in rows
            ]

    dispatched = 0
    failed = 0
    for outbox_id, job_id, attempt_generation, attempt_token, attempt_count in claimed:
        try:
            send(job_id, attempt_generation, attempt_token)
        except (BrokerOperationalError, ConnectionError, TimeoutError, OSError) as exc:
            failed += 1
            async with session_factory() as failure_session:
                async with failure_session.begin():
                    row = await failure_session.get(MiniappPublishOutbox, outbox_id, with_for_update=True)
                    if row is not None and row.status == "claiming":
                        row.status = "pending"
                        row.claimed_at = None
                        row.available_at = current + timedelta(seconds=_outbox_backoff(attempt_count))
                        row.last_error = exc.__class__.__name__
        else:
            dispatched += 1
            async with session_factory() as success_session:
                async with success_session.begin():
                    row = await success_session.get(MiniappPublishOutbox, outbox_id, with_for_update=True)
                    if row is not None and row.status == "claiming":
                        row.status = "delivered"
                        row.delivered_at = current
                        row.last_error = None
    return DispatchSummary(len(claimed), dispatched, failed)


def _manifest_bytes(manifest: dict) -> bytes:
    return json.dumps(manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def _pointer_bytes(channel: str, version: str, manifest_url: str, sha256: str) -> bytes:
    return json.dumps(
        {
            "schema": "guanhaiju.pointer.v1",
            "channel": channel,
            "version": version,
            "manifestUrl": manifest_url,
            "sha256": sha256,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()


def _read_all(storage: ContentStorage, object_key: str) -> bytes:
    return b"".join(storage.read(object_key, chunk_size=1024 * 1024))


def _put_immutable_json(
    storage: ContentStorage,
    object_key: str,
    content: bytes,
    sha256: str,
) -> None:
    try:
        head = storage.head(object_key)
    except StorageNotFoundError:
        try:
            storage.put(
                object_key,
                content,
                content_type=JSON_CONTENT_TYPE,
                acl="public-read",
                cache_control=IMMUTABLE_CACHE_CONTROL,
                metadata={"sha256": sha256},
            )
        except StorageConflictError:
            pass
    head = storage.head(object_key)
    if (
        head.acl != "public-read"
        or head.content_type != JSON_CONTENT_TYPE
        or head.cache_control != IMMUTABLE_CACHE_CONTROL
        or head.byte_size != len(content)
        or head.metadata.get("sha256") != sha256
        or hashlib.sha256(_read_all(storage, object_key)).hexdigest() != sha256
    ):
        raise ContentValidationError("immutable manifest read-back verification failed")


def _put_immutable_candidate(
    storage: ContentStorage,
    object_key: str,
    content: bytes,
) -> None:
    digest = hashlib.sha256(content).hexdigest()
    try:
        head = storage.head(object_key)
    except StorageNotFoundError:
        try:
            storage.put(
                object_key,
                content,
                content_type=JSON_CONTENT_TYPE,
                acl="public-read",
                cache_control="no-store",
                metadata={"sha256": digest},
            )
        except StorageConflictError:
            pass
        head = storage.head(object_key)
    if (
        head.acl != "public-read"
        or head.content_type != JSON_CONTENT_TYPE
        or head.cache_control != "no-store"
        or head.byte_size != len(content)
        or head.metadata.get("sha256") != digest
        or hashlib.sha256(_read_all(storage, object_key)).hexdigest() != digest
    ):
        raise ContentValidationError("immutable candidate read-back verification failed")


async def _stage(
    db: AsyncSession,
    job: MiniappPublishJob,
    dependencies: PublishDependencies,
    attempt_generation: int,
    attempt_token: str,
    lease_owner: str,
    stage: str,
    percent: int,
) -> None:
    channel = job.channel
    if not await dependencies.lease.renew(channel, lease_owner):
        raise PublishBusyError("publish lease fence was lost")
    transitioned = await db.execute(
        update(MiniappPublishJob)
        .where(
            MiniappPublishJob.job_id == job.job_id,
            MiniappPublishJob.status == "running",
            MiniappPublishJob.attempt_generation == attempt_generation,
            MiniappPublishJob.attempt_token == attempt_token,
        )
        .values(
            progress_stage=stage,
            progress_percent=percent,
            heartbeat_at=dependencies.now(),
        )
    )
    if transitioned.rowcount != 1:
        await db.rollback()
        raise PublishBusyError("publish attempt fence is stale")
    await db.commit()
    await db.refresh(job)


async def _cas_running_job(
    db: AsyncSession,
    job: MiniappPublishJob,
    attempt_generation: int,
    attempt_token: str,
    *,
    commit: bool = True,
    **values: object,
) -> None:
    transitioned = await db.execute(
        update(MiniappPublishJob)
        .where(
            MiniappPublishJob.job_id == job.job_id,
            MiniappPublishJob.status == "running",
            MiniappPublishJob.attempt_generation == attempt_generation,
            MiniappPublishJob.attempt_token == attempt_token,
        )
        .values(**values)
    )
    if transitioned.rowcount != 1:
        await db.rollback()
        raise PublishBusyError("publish attempt fence is stale")
    if commit:
        await db.commit()
        await db.refresh(job)


def _release_version(job: MiniappPublishJob, now: datetime) -> str:
    return f"{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{job.job_id[-20:]}"


async def _load_job_for_execution(db: AsyncSession, job_id: str) -> MiniappPublishJob:
    job = await db.scalar(
        select(MiniappPublishJob).where(MiniappPublishJob.job_id == job_id).with_for_update()
    )
    if job is None:
        raise ContentValidationError("publish job does not exist")
    return job


async def execute_publish_job(
    db: AsyncSession,
    job_id: str,
    *,
    dependencies: PublishDependencies,
    attempt_generation: int | None = None,
    attempt_token: str | None = None,
) -> ReleaseResult | None:
    job = await _load_job_for_execution(db, job_id)
    if job.status in TERMINAL_JOB_STATUSES:
        if job.result_release_id is None or job.target_version is None:
            raise ContentValidationError("terminal publish job has no release result")
        return ReleaseResult(job.result_release_id, job.channel, job.target_version, job.status)
    generation = attempt_generation or job.attempt_generation
    token = attempt_token or job.attempt_token
    if job.attempt_generation != generation or job.attempt_token != token:
        raise PublishBusyError("publish attempt fence is stale")
    # Watchdog recovery always advances generation/token. The same fence in
    # running state is therefore a concurrent/redelivered duplicate, not a
    # recovery attempt, and must not acquire or release the active lease.
    if job.status == "running":
        await db.rollback()
        return None
    channel = job.channel
    operation = job.operation
    lease_owner = f"{job.job_id}:{generation}:{token}"
    if not await dependencies.lease.acquire(channel, lease_owner):
        await db.rollback()
        raise PublishBusyError("channel publish lease is held by another job")
    claimed = await db.execute(
        update(MiniappPublishJob)
        .where(
            MiniappPublishJob.job_id == job_id,
            MiniappPublishJob.status == "queued",
            MiniappPublishJob.attempt_generation == generation,
            MiniappPublishJob.attempt_token == token,
        )
        .values(
            status="running",
            progress_stage="claiming",
            progress_percent=1,
            heartbeat_at=dependencies.now(),
        )
    )
    if claimed.rowcount != 1:
        await db.rollback()
        await dependencies.lease.release(channel, lease_owner)
        raise PublishBusyError("publish attempt fence is stale")
    await db.commit()
    await db.refresh(job)

    async def renew_lease() -> None:
        ttl = float(getattr(dependencies.lease, "ttl_seconds", 120))
        interval = max(0.01, min(40.0, ttl / 3.0))
        while True:
            await asyncio.sleep(interval)
            if not await dependencies.lease.renew(channel, lease_owner):
                raise PublishBusyError("publish lease fence was lost")
            if dependencies.persist_heartbeat is not None:
                await dependencies.persist_heartbeat(
                    job_id, generation, token, dependencies.now()
                )

    heartbeat_task = asyncio.create_task(renew_lease())
    pipeline_task = asyncio.create_task(
        _execute_rollback(db, job, dependencies, generation, token, lease_owner)
        if operation == "rollback"
        else _execute_publish(db, job, dependencies, generation, token, lease_owner)
    )

    try:
        done, _ = await asyncio.wait(
            {pipeline_task, heartbeat_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if heartbeat_task in done:
            heartbeat_error = heartbeat_task.exception()
            if heartbeat_error is not None:
                pipeline_result = (
                    await asyncio.gather(pipeline_task, return_exceptions=True)
                )[0]
                if isinstance(pipeline_result, BaseException) and not isinstance(
                    pipeline_result, PublishBusyError
                ):
                    raise pipeline_result
                raise heartbeat_error
        return await pipeline_task
    finally:
        if not pipeline_task.done():
            pipeline_task.cancel()
            try:
                await pipeline_task
            except asyncio.CancelledError:
                pass
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass
        await dependencies.lease.release(channel, lease_owner)


def _candidate_key(job: MiniappPublishJob) -> str:
    return (
        f"miniapp/content/{job.channel}/candidates/{job.target_version}/"
        f"{job.attempt_generation}-{job.attempt_token}.json"
    )


def _pointer_matches(storage: ContentStorage, object_key: str, expected: bytes) -> bool:
    try:
        return _read_all(storage, object_key) == expected
    except StorageNotFoundError:
        return False


def _pointer_is_absent(storage: ContentStorage, object_key: str) -> bool:
    try:
        storage.head(object_key)
    except StorageNotFoundError:
        return True
    return False


async def _compensate_current_pointer(
    db: AsyncSession,
    job: MiniappPublishJob,
    dependencies: PublishDependencies,
    current_key: str,
    intended_pointer: bytes,
) -> None:
    if not await asyncio.to_thread(
        _pointer_matches, dependencies.storage, current_key, intended_pointer
    ):
        return
    previous = (
        await db.get(MiniappContentRelease, job.previous_release_id)
        if job.previous_release_id
        else None
    )
    if previous is None:
        await asyncio.to_thread(
            dependencies.storage.delete_current_pointer, current_key
        )
        return
    previous_pointer = _pointer_bytes(
        job.channel,
        previous.version,
        previous.manifest_url,
        previous.manifest_sha256,
    )
    await asyncio.to_thread(
        dependencies.storage.put,
        current_key,
        previous_pointer,
        content_type=JSON_CONTENT_TYPE,
        acl="public-read",
        cache_control=CURRENT_POINTER_CACHE_CONTROL,
        metadata={"sha256": hashlib.sha256(previous_pointer).hexdigest()},
    )


_POST_SWAP_STAGES = frozenset(
    {"swapping_pointer", "observing_pointer", "finalizing"}
)


async def reconcile_publish_terminal_failure(
    db: AsyncSession,
    job_id: str,
    error: BaseException,
    *,
    expected_generation: int,
    expected_token: str,
    storage: ContentStorage | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> str | None:
    """Fence a terminal attempt and reconcile any public pointer side effect.

    Returns ``live``, ``compensated``, or ``failed`` when this exact attempt
    transitions. A stale or already-terminal attempt returns ``None``.
    """

    job = await db.scalar(
        select(MiniappPublishJob)
        .where(MiniappPublishJob.job_id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        job is None
        or job.status not in {"queued", "running"}
        or job.attempt_generation != expected_generation
        or job.attempt_token != expected_token
    ):
        await db.rollback()
        return None

    if job.recovery_metadata_version != PUBLISH_RECOVERY_METADATA_VERSION:
        transitioned = await db.execute(
            update(MiniappPublishJob)
            .where(
                MiniappPublishJob.job_id == job.job_id,
                MiniappPublishJob.status.in_(("queued", "running")),
                MiniappPublishJob.attempt_generation == expected_generation,
                MiniappPublishJob.attempt_token == expected_token,
                MiniappPublishJob.recovery_metadata_version.is_(None),
            )
            .values(
                status="failed",
                progress_stage=LEGACY_RECOVERY_STAGE,
                progress_percent=100,
                error_code=LEGACY_RECOVERY_ERROR_CODE,
                heartbeat_at=now(),
            )
        )
        if transitioned.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        return "failed"

    release = (
        await db.get(MiniappContentRelease, job.result_release_id)
        if job.result_release_id is not None
        else None
    )
    workspace = await db.scalar(
        select(MiniappContentWorkspace)
        .where(MiniappContentWorkspace.channel == job.channel)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    db_points_to_target = bool(
        release is not None
        and workspace is not None
        and workspace.current_release_id == release.release_id
        and release.status == "published"
        and release.pointer_swapped_at is not None
        and (job.operation != "publish" or workspace.is_dirty is False)
    )
    needs_pointer_reconciliation = bool(
        release is not None
        and (job.progress_stage in _POST_SWAP_STAGES or db_points_to_target)
    )

    pointer_points_to_target = False
    current_key = f"miniapp/content/{job.channel}/current.json"
    if release is not None and storage is not None:
        target_pointer = _pointer_bytes(
            job.channel,
            release.version,
            release.manifest_url,
            release.manifest_sha256,
        )
        try:
            pointer_points_to_target = await asyncio.to_thread(
                _pointer_matches, storage, current_key, target_pointer
            )
        except BaseException:
            await db.rollback()
            raise
        needs_pointer_reconciliation = bool(
            needs_pointer_reconciliation or pointer_points_to_target
        )

    if needs_pointer_reconciliation and storage is None:
        await db.rollback()
        raise ContentValidationError(
            "post-swap terminal reconciliation requires content storage"
        )

    terminal_error_code = error_code(error)
    if db_points_to_target and pointer_points_to_target:
        terminal_status = "succeeded" if job.operation == "publish" else "rolled_back"
        terminal_stage = "published" if job.operation == "publish" else "rolled_back"
        transitioned = await db.execute(
            update(MiniappPublishJob)
            .where(
                MiniappPublishJob.job_id == job.job_id,
                MiniappPublishJob.status.in_(("queued", "running")),
                MiniappPublishJob.attempt_generation == expected_generation,
                MiniappPublishJob.attempt_token == expected_token,
            )
            .values(
                status=terminal_status,
                progress_stage=terminal_stage,
                progress_percent=100,
                error_code=terminal_error_code,
                heartbeat_at=now(),
            )
        )
        if transitioned.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        return "live"

    if needs_pointer_reconciliation:
        assert storage is not None
        if workspace is None or release is None:
            await db.rollback()
            raise ContentValidationError(
                "post-swap state lacks workspace or target release"
            )
        previous = (
            await db.get(MiniappContentRelease, job.previous_release_id)
            if job.previous_release_id is not None
            else None
        )
        if job.previous_release_id is not None and (
            previous is None or previous.channel != job.channel
        ):
            await db.rollback()
            raise ContentValidationError(
                "post-swap compensation lacks the previous release"
            )
        if workspace.current_release_id not in {
            release.release_id,
            job.previous_release_id,
        }:
            await db.rollback()
            raise ContentValidationError(
                "post-swap compensation found a newer workspace pointer"
            )
        if job.operation == "rollback" and job.result_release_previous_status is None:
            await db.rollback()
            raise ContentValidationError(
                "rollback compensation lacks the target previous status"
            )

        try:
            if previous is None:
                await asyncio.to_thread(storage.delete_current_pointer, current_key)
                if not await asyncio.to_thread(
                    _pointer_is_absent, storage, current_key
                ):
                    raise PointerSwapError(
                        "first-publish pointer compensation did not become durable"
                    )
            else:
                previous_pointer = _pointer_bytes(
                    job.channel,
                    previous.version,
                    previous.manifest_url,
                    previous.manifest_sha256,
                )
                if not await asyncio.to_thread(
                    _pointer_matches, storage, current_key, previous_pointer
                ):
                    await asyncio.to_thread(
                        storage.put,
                        current_key,
                        previous_pointer,
                        content_type=JSON_CONTENT_TYPE,
                        acl="public-read",
                        cache_control=CURRENT_POINTER_CACHE_CONTROL,
                        metadata={
                            "sha256": hashlib.sha256(previous_pointer).hexdigest()
                        },
                    )
                if not await asyncio.to_thread(
                    _pointer_matches, storage, current_key, previous_pointer
                ):
                    raise PointerSwapError(
                        "previous pointer compensation did not become durable"
                    )
        except BaseException:
            await db.rollback()
            raise

        workspace.current_release_id = job.previous_release_id
        if job.operation == "publish":
            workspace.is_dirty = True
            release.status = "failed"
        else:
            release.status = job.result_release_previous_status
        if previous is not None:
            previous.status = "published"
        config_was_preserved = False
        if job.auto_enabled_channel:
            if (
                job.auto_enabled_config_token is None
                or job.auto_enabled_config_updated_at is None
            ):
                config_was_preserved = True
            else:
                deleted_config = await db.execute(
                    delete(MiniappChannelConfig).where(
                        MiniappChannelConfig.channel == job.channel,
                        MiniappChannelConfig.enabled.is_(True),
                        MiniappChannelConfig.auto_creation_token
                        == job.auto_enabled_config_token,
                        MiniappChannelConfig.updated_at
                        == job.auto_enabled_config_updated_at,
                    )
                )
                config_was_preserved = deleted_config.rowcount != 1
        transitioned = await db.execute(
            update(MiniappPublishJob)
            .where(
                MiniappPublishJob.job_id == job.job_id,
                MiniappPublishJob.status.in_(("queued", "running")),
                MiniappPublishJob.attempt_generation == expected_generation,
                MiniappPublishJob.attempt_token == expected_token,
            )
            .values(
                status="failed",
                progress_stage=(
                    "failed_compensated_config_preserved"
                    if config_was_preserved
                    else "failed_compensated"
                ),
                progress_percent=100,
                error_code=terminal_error_code,
                heartbeat_at=now(),
            )
        )
        if transitioned.rowcount != 1:
            await db.rollback()
            return None
        await db.commit()
        return "compensated"

    transitioned = await db.execute(
        update(MiniappPublishJob)
        .where(
            MiniappPublishJob.job_id == job.job_id,
            MiniappPublishJob.status.in_(("queued", "running")),
            MiniappPublishJob.attempt_generation == expected_generation,
            MiniappPublishJob.attempt_token == expected_token,
        )
        .values(
            status="failed",
            progress_stage="failed",
            error_code=terminal_error_code,
            heartbeat_at=now(),
        )
    )
    if transitioned.rowcount != 1:
        await db.rollback()
        return None
    if release is not None and release.status in {"pending", "promoting"}:
        release.status = "failed"
    await db.commit()
    return "failed"


async def _lock_workspace_for_pointer(
    db: AsyncSession,
    job: MiniappPublishJob,
) -> MiniappContentWorkspace:
    workspace = await db.scalar(
        select(MiniappContentWorkspace)
        .where(MiniappContentWorkspace.channel == job.channel)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if workspace is None:
        raise ContentValidationError("content workspace does not exist")
    if job.operation == "publish" and workspace.revision != job.expected_revision:
        raise DraftConflictError("saved draft revision changed before pointer swap")
    return workspace


async def _read_durable_pointer_commit(
    db: AsyncSession,
    *,
    channel: str,
    job_id: str,
    operation: str,
    release_id: str,
    attempt_generation: int,
    attempt_token: str,
) -> tuple[bool | None, MiniappContentWorkspace | None]:
    """Return committed, not committed, or unknown after an ambiguous commit."""
    try:
        durable_workspace = await db.scalar(
            select(MiniappContentWorkspace)
            .where(MiniappContentWorkspace.channel == channel)
            .execution_options(populate_existing=True)
        )
        durable_job = await db.scalar(
            select(MiniappPublishJob)
            .where(MiniappPublishJob.job_id == job_id)
            .execution_options(populate_existing=True)
        )
        durable_release = await db.scalar(
            select(MiniappContentRelease)
            .where(MiniappContentRelease.release_id == release_id)
            .execution_options(populate_existing=True)
        )
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        return None, None

    job_confirms_commit = bool(
        durable_job is not None
        and (
            (
                durable_job.status == "running"
                and durable_job.progress_stage in {"observing_pointer", "finalizing"}
            )
            or (
                operation == "publish"
                and durable_job.status == "succeeded"
                and durable_job.progress_stage == "published"
            )
            or (
                operation == "rollback"
                and durable_job.status == "rolled_back"
                and durable_job.progress_stage == "rolled_back"
            )
        )
    )
    committed = bool(
        durable_workspace is not None
        and durable_workspace.current_release_id == release_id
        and durable_job is not None
        and durable_job.result_release_id == release_id
        and job_confirms_commit
        and durable_release is not None
        and durable_release.status == "published"
        and durable_release.pointer_swapped_at is not None
        and (operation != "publish" or durable_workspace.is_dirty is False)
    )
    if committed:
        return True, durable_workspace

    not_committed = bool(
        operation == "publish"
        and durable_workspace is not None
        and durable_workspace.current_release_id != release_id
        and durable_workspace.is_dirty is True
        and durable_job is not None
        and durable_job.result_release_id == release_id
        and durable_job.status == "running"
        and durable_job.progress_stage == "swapping_pointer"
        and durable_job.attempt_generation == attempt_generation
        and durable_job.attempt_token == attempt_token
        and durable_release is not None
        and durable_release.status == "promoting"
        and durable_release.pointer_swapped_at is None
    )
    if not_committed:
        return False, None
    return None, None


async def _finalize_pointer_swap(
    db: AsyncSession,
    job: MiniappPublishJob,
    release: MiniappContentRelease,
    dependencies: PublishDependencies,
    pointer: bytes,
    attempt_generation: int,
    attempt_token: str,
    lease_owner: str,
) -> MiniappContentWorkspace:
    current_key = f"miniapp/content/{job.channel}/current.json"
    try:
        workspace = await _lock_workspace_for_pointer(db, job)
    except (ContentValidationError, DraftConflictError):
        await _compensate_current_pointer(
            db, job, dependencies, current_key, pointer
        )
        await db.rollback()
        raise

    await db.refresh(job)
    if (
        job.status != "running"
        or job.attempt_generation != attempt_generation
        or job.attempt_token != attempt_token
        or not await dependencies.lease.renew(job.channel, lease_owner)
    ):
        await db.rollback()
        raise PublishBusyError("publish attempt fence is stale before pointer swap")

    if job.operation == "publish":
        cas = await db.execute(
            update(MiniappContentWorkspace)
            .where(
                MiniappContentWorkspace.channel == job.channel,
                MiniappContentWorkspace.revision == job.expected_revision,
            )
            .values(current_release_id=release.release_id, is_dirty=False)
        )
        if cas.rowcount != 1:
            await _compensate_current_pointer(db, job, dependencies, current_key, pointer)
            await db.rollback()
            raise DraftConflictError("workspace revision CAS failed before pointer swap")
    else:
        workspace.current_release_id = release.release_id

    already_swapped = await asyncio.to_thread(
        _pointer_matches, dependencies.storage, current_key, pointer
    )
    if not already_swapped:
        try:
            await asyncio.to_thread(
                dependencies.storage.put,
                current_key,
                pointer,
                content_type=JSON_CONTENT_TYPE,
                acl="public-read",
                cache_control=CURRENT_POINTER_CACHE_CONTROL,
                metadata={"sha256": hashlib.sha256(pointer).hexdigest()},
            )
        except StorageConflictError as exc:
            await db.rollback()
            raise PointerSwapError("production pointer swap was rejected") from exc
        except BaseException:
            await db.rollback()
            raise

    if not await dependencies.lease.renew(job.channel, lease_owner):
        await _compensate_current_pointer(db, job, dependencies, current_key, pointer)
        await db.rollback()
        raise PublishBusyError("publish lease fence was lost during pointer swap")

    if workspace.current_release_id and workspace.current_release_id != release.release_id:
        previous = await db.get(MiniappContentRelease, workspace.current_release_id)
        if previous is not None and previous.status == "published":
            previous.status = "rolled_back" if job.operation == "rollback" else "superseded"
    previous = (
        await db.get(MiniappContentRelease, job.previous_release_id)
        if job.previous_release_id and job.previous_release_id != release.release_id
        else None
    )
    if previous is not None and previous.status == "published":
        previous.status = "rolled_back" if job.operation == "rollback" else "superseded"
    release.status = "published"
    release.pointer_swapped_at = dependencies.now()
    first_publish = job.previous_release_id is None
    auto_enabled_config_token = None
    auto_enabled_config_updated_at = None
    if job.operation == "publish":
        config = await db.get(MiniappChannelConfig, job.channel)
        if config is None and first_publish:
            auto_enabled_config_token = uuid4().hex
            config = MiniappChannelConfig(
                channel=job.channel,
                enabled=True,
                auto_creation_token=auto_enabled_config_token,
                updated_at=dependencies.now(),
            )
            db.add(config)
            await db.flush([config])
            auto_enabled_config_updated_at = config.updated_at
            job.auto_enabled_channel = True
            job.auto_enabled_config_token = auto_enabled_config_token
            job.auto_enabled_config_updated_at = auto_enabled_config_updated_at
    durable_channel = job.channel
    durable_job_id = job.job_id
    durable_operation = job.operation
    durable_release_id = release.release_id
    transitioned = await db.execute(
        update(MiniappPublishJob)
        .where(
            MiniappPublishJob.job_id == job.job_id,
            MiniappPublishJob.status == "running",
            MiniappPublishJob.attempt_generation == attempt_generation,
            MiniappPublishJob.attempt_token == attempt_token,
        )
        .values(
            result_release_id=release.release_id,
            auto_enabled_channel=(
                True if auto_enabled_config_token is not None else job.auto_enabled_channel
            ),
            auto_enabled_config_token=(
                auto_enabled_config_token or job.auto_enabled_config_token
            ),
            auto_enabled_config_updated_at=(
                auto_enabled_config_updated_at or job.auto_enabled_config_updated_at
            ),
            progress_stage="observing_pointer",
            progress_percent=90,
            heartbeat_at=dependencies.now(),
        )
    )
    if transitioned.rowcount != 1:
        await _compensate_current_pointer(db, job, dependencies, current_key, pointer)
        await db.rollback()
        raise PublishBusyError("publish attempt fence is stale during pointer finalization")
    try:
        await db.commit()
    except BaseException as commit_error:
        # The promoting intent was committed before any object mutation.  Leave
        # an acknowledged pointer in place for the next fenced attempt to
        # reconcile, but never leak this transaction or its workspace lock.
        try:
            await db.rollback()
        except Exception:
            pass
        commit_state, durable_workspace = await _read_durable_pointer_commit(
            db,
            channel=durable_channel,
            job_id=durable_job_id,
            operation=durable_operation,
            release_id=durable_release_id,
            attempt_generation=attempt_generation,
            attempt_token=attempt_token,
        )
        if commit_state is True and durable_workspace is not None:
            return durable_workspace
        await db.rollback()
        if (
            commit_state is False
            and first_publish
            and await dependencies.lease.renew(durable_channel, lease_owner)
            and await asyncio.to_thread(
                _pointer_matches, dependencies.storage, current_key, pointer
            )
        ):
            await asyncio.to_thread(
                dependencies.storage.delete_current_pointer, current_key
            )
        raise commit_error
    await db.refresh(workspace)
    return workspace


async def _execute_publish(
    db: AsyncSession,
    job: MiniappPublishJob,
    dependencies: PublishDependencies,
    attempt_generation: int,
    attempt_token: str,
    lease_owner: str,
) -> ReleaseResult:
    manifest_definition = _channel_manifest_definition(job.channel)
    await _stage(
        db, job, dependencies, attempt_generation, attempt_token, lease_owner,
        "validating", 10,
    )
    release = (
        await db.get(MiniappContentRelease, job.result_release_id)
        if job.result_release_id
        else None
    )
    workspace = await db.get(MiniappContentWorkspace, job.channel)
    if workspace is None or workspace.revision != job.expected_revision:
        if release is not None:
            intended = _pointer_bytes(
                job.channel,
                release.version,
                release.manifest_url,
                release.manifest_sha256,
            )
            await _compensate_current_pointer(
                db,
                job,
                dependencies,
                f"miniapp/content/{job.channel}/current.json",
                intended,
            )
        await db.rollback()
        raise DraftConflictError("saved draft revision no longer matches the publish request")
    if release is not None and release.manifest_payload is not None:
        manifest = manifest_definition.validator(release.manifest_payload)
        content = _manifest_bytes(manifest)
        digest = hashlib.sha256(content).hexdigest()
        if digest != release.manifest_sha256:
            raise ContentValidationError("persisted manifest intent hash does not match")
    else:
        snapshot = await dependencies.load_snapshot(db, job)
        if snapshot.revision != job.expected_revision:
            raise DraftConflictError("loaded draft revision no longer matches the publish request")

        if job.target_version is None:
            target_version = _release_version(job, dependencies.now())
        else:
            target_version = job.target_version
        if job.manifest_published_at is None:
            manifest_published_at = dependencies.now()
        else:
            manifest_published_at = job.manifest_published_at
        await _cas_running_job(
            db,
            job,
            attempt_generation,
            attempt_token,
            target_version=target_version,
            manifest_published_at=manifest_published_at,
            previous_release_id=workspace.current_release_id,
        )

        published: dict[str, PublishedMedia] = {}
        media_refs = manifest_definition.media_refs(snapshot)
        await _stage(
            db, job, dependencies, attempt_generation, attempt_token, lease_owner,
            "promoting_media", 30,
        )
        for ref in media_refs:
            if ref.stored.object_key in published:
                continue
            promoted = await asyncio.to_thread(
                dependencies.media_service.promote_media,
                ref.stored,
                derivative_set_id=ref.derivative_set_id,
                derivative_role=ref.derivative_role,
            )
            await asyncio.to_thread(
                dependencies.media_service.verify_public_object,
                promoted.object_key,
                promoted.sha256,
                promoted.byte_size,
                promoted.mime_type,
            )
            if not await dependencies.lease.renew(job.channel, lease_owner):
                raise PublishBusyError("publish lease fence was lost during media promotion")
            published[ref.stored.object_key] = promoted

        manifest = manifest_definition.builder(
            snapshot,
            version=job.target_version,
            published_at=job.manifest_published_at,
            promoted_media=published,
            public_base_url=dependencies.public_base_url,
        )
        if manifest_definition.publish_media_reference_state and media_refs:
            media_ids = {ref.media_id for ref in media_refs}
            transitioned_media = await db.execute(
                update(MiniappMedia)
                .where(
                    MiniappMedia.media_id.in_(media_ids),
                    MiniappMedia.reference_status.in_(("draft", "published")),
                )
                .values(
                    reference_status="published",
                    last_referenced_at=job.manifest_published_at,
                )
            )
            if transitioned_media.rowcount != len(media_ids):
                await db.rollback()
                raise ContentValidationError(
                    "guide media reference state changed during publication"
                )
        content = _manifest_bytes(manifest)
        digest = hashlib.sha256(content).hexdigest()
        version = job.target_version
        manifest_key = f"miniapp/releases/{job.channel}/{version}/manifest.json"
        manifest_url = f"{dependencies.public_base_url.rstrip('/')}/{manifest_key}"
        release_id = f"MCR-{job.job_id[-20:]}"
        release = MiniappContentRelease(
            release_id=release_id,
            channel=job.channel,
            version=version,
            manifest_url=manifest_url,
            manifest_sha256=digest,
            manifest_payload=manifest,
            published_at=job.manifest_published_at,
            status="promoting",
            created_by=job.actor_id,
        )
        db.add(release)
        await db.flush()
        await _cas_running_job(
            db,
            job,
            attempt_generation,
            attempt_token,
            result_release_id=release.release_id,
            progress_stage="manifest_intent",
            progress_percent=45,
        )

    version = release.version
    manifest = manifest_definition.validator(release.manifest_payload)
    content = _manifest_bytes(manifest)
    digest = release.manifest_sha256
    manifest_key = f"miniapp/releases/{job.channel}/{version}/manifest.json"
    await _stage(
        db, job, dependencies, attempt_generation, attempt_token, lease_owner,
        "uploading_manifest", 50,
    )
    await asyncio.to_thread(
        _put_immutable_json, dependencies.storage, manifest_key, content, digest
    )

    await _stage(
        db, job, dependencies, attempt_generation, attempt_token, lease_owner,
        "verifying_candidate", 70,
    )
    pointer = _pointer_bytes(job.channel, version, release.manifest_url, digest)
    candidate_key = _candidate_key(job)
    await asyncio.to_thread(
        _put_immutable_candidate, dependencies.storage, candidate_key, pointer
    )
    candidate_url = f"{dependencies.public_base_url.rstrip('/')}/{candidate_key}"
    await dependencies.verifier.verify(
        candidate_url,
        expected_version=version,
        expected_manifest_sha256=digest,
        manifest=manifest,
    )

    await _stage(
        db, job, dependencies, attempt_generation, attempt_token, lease_owner,
        "swapping_pointer", 85,
    )
    await _finalize_pointer_swap(
        db, job, release, dependencies, pointer,
        attempt_generation, attempt_token, lease_owner,
    )
    if job.status in TERMINAL_JOB_STATUSES:
        return ReleaseResult(release.release_id, job.channel, version, release.status)

    current_key = f"miniapp/content/{job.channel}/current.json"
    current_url = f"{dependencies.public_base_url.rstrip('/')}/{current_key}"
    await dependencies.verifier.verify(
        current_url,
        expected_version=version,
        expected_manifest_sha256=digest,
        manifest=manifest,
    )

    await _stage(
        db, job, dependencies, attempt_generation, attempt_token, lease_owner,
        "finalizing", 95,
    )
    await _cas_running_job(
        db,
        job,
        attempt_generation,
        attempt_token,
        status="succeeded",
        progress_stage="published",
        progress_percent=100,
        error_code=None,
        heartbeat_at=dependencies.now(),
    )
    return ReleaseResult(release.release_id, job.channel, version, release.status)


async def _execute_rollback(
    db: AsyncSession,
    job: MiniappPublishJob,
    dependencies: PublishDependencies,
    attempt_generation: int,
    attempt_token: str,
    lease_owner: str,
) -> ReleaseResult:
    manifest_definition = _channel_manifest_definition(job.channel)
    await _stage(
        db, job, dependencies, attempt_generation, attempt_token, lease_owner,
        "validating_rollback", 20,
    )
    release = await db.scalar(
        select(MiniappContentRelease).where(
            MiniappContentRelease.channel == job.channel,
            MiniappContentRelease.version == job.target_version,
            MiniappContentRelease.status.in_(("published", "superseded", "rolled_back")),
        )
    )
    if release is None:
        raise ContentValidationError("rollback target is not a valid historical release")
    try:
        workspace = await _lock_workspace_for_pointer(db, job)
    except (ContentValidationError, DraftConflictError):
        if job.result_release_id == release.release_id:
            intended = _pointer_bytes(
                job.channel,
                release.version,
                release.manifest_url,
                release.manifest_sha256,
            )
            await _compensate_current_pointer(
                db,
                job,
                dependencies,
                f"miniapp/content/{job.channel}/current.json",
                intended,
            )
        await db.rollback()
        raise
    if (
        job.previous_release_id is None
        or job.result_release_id is None
        or job.result_release_previous_status is None
    ):
        await _cas_running_job(
            db,
            job,
            attempt_generation,
            attempt_token,
            previous_release_id=workspace.current_release_id,
            result_release_id=release.release_id,
            result_release_previous_status=release.status,
        )
    manifest_content = await asyncio.to_thread(
        _read_all, dependencies.storage, release.manifest_object_key
    )
    if hashlib.sha256(manifest_content).hexdigest() != release.manifest_sha256:
        raise ContentValidationError("rollback manifest SHA-256 does not match its audit row")
    try:
        manifest = manifest_definition.validator(json.loads(manifest_content))
    except (json.JSONDecodeError, UnicodeDecodeError, ContentValidationError) as exc:
        raise ContentValidationError("rollback manifest is invalid") from exc

    pointer = _pointer_bytes(
        job.channel, release.version, release.manifest_url, release.manifest_sha256
    )
    await _stage(
        db, job, dependencies, attempt_generation, attempt_token, lease_owner,
        "verifying_candidate", 60,
    )
    candidate_key = _candidate_key(job)
    await asyncio.to_thread(
        _put_immutable_candidate, dependencies.storage, candidate_key, pointer
    )
    await dependencies.verifier.verify(
        f"{dependencies.public_base_url.rstrip('/')}/{candidate_key}",
        expected_version=release.version,
        expected_manifest_sha256=release.manifest_sha256,
        manifest=manifest,
    )

    await _stage(
        db, job, dependencies, attempt_generation, attempt_token, lease_owner,
        "swapping_pointer", 85,
    )
    await _finalize_pointer_swap(
        db, job, release, dependencies, pointer,
        attempt_generation, attempt_token, lease_owner,
    )
    if job.status in TERMINAL_JOB_STATUSES:
        return ReleaseResult(release.release_id, job.channel, release.version, release.status)

    current_key = f"miniapp/content/{job.channel}/current.json"
    await dependencies.verifier.verify(
        f"{dependencies.public_base_url.rstrip('/')}/{current_key}",
        expected_version=release.version,
        expected_manifest_sha256=release.manifest_sha256,
        manifest=manifest,
    )

    await _cas_running_job(
        db,
        job,
        attempt_generation,
        attempt_token,
        status="rolled_back",
        progress_stage="rolled_back",
        progress_percent=100,
        error_code=None,
        heartbeat_at=dependencies.now(),
    )
    return ReleaseResult(release.release_id, job.channel, release.version, "rolled_back")


__all__ = [
    "CURRENT_POINTER_CACHE_CONTROL",
    "DispatchSummary",
    "DraftMediaRef",
    "DraftImageRef",
    "HttpPublicVerifier",
    "OwnerDraftCase",
    "OwnerDraftSnapshot",
    "PublishDependencies",
    "PublishJobOut",
    "RedisPublishLease",
    "ReleaseResult",
    "StructuredGuideDraftSnapshot",
    "build_owner_manifest",
    "build_stay_guide_manifest",
    "build_travel_manifest",
    "dispatch_publish_outbox",
    "enqueue_publish",
    "enqueue_rollback",
    "execute_publish_job",
    "reconcile_publish_terminal_failure",
]
