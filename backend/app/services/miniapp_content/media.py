"""Validated private media ingestion and immutable public promotion."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import RLock
from typing import Callable, Protocol
from uuid import uuid4
import warnings

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

from app.services.miniapp_content.storage import (
    IMMUTABLE_CACHE_CONTROL,
    ContentStorage,
    StorageError,
    StorageConflictError,
    StorageKeyError,
    StorageNotConfiguredError,
    StorageNotFoundError,
)


MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_WIDTH = 8192
MAX_IMAGE_HEIGHT = 8192
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_PROCESSING_BYTES = 160 * 1024 * 1024
MIN_PAIR_SOURCE_LONG_EDGE = 640
THUMBNAIL_LONG_EDGE = 480
THUMBNAIL_MAX_BYTES = 96 * 1024
DISPLAY_LONG_EDGE = 1600
DISPLAY_MAX_BYTES = 220 * 1024
MAX_VIDEO_BYTES = 80 * 1024 * 1024
VIDEO_READ_CHUNK_BYTES = 1024 * 1024
VIDEO_UPLOAD_EXPIRES_SECONDS = 15 * 60
FFPROBE_TIMEOUT_SECONDS = 10
TEMP_DISK_RESERVE_BYTES = 16 * 1024 * 1024
PUBLIC_VERIFY_CHUNK_BYTES = 1024 * 1024

_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", "jpg", frozenset({".jpg", ".jpeg"})),
    "PNG": ("image/png", "png", frozenset({".png"})),
    "WEBP": ("image/webp", "webp", frozenset({".webp"})),
}
_MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
}
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class MediaError(RuntimeError):
    pass


class MediaValidationError(MediaError):
    pass


class MediaVerificationError(MediaError):
    pass


class MediaStateError(MediaError):
    pass


@dataclass(frozen=True)
class StoredMedia:
    object_key: str
    sha256: str
    byte_size: int
    mime_type: str


@dataclass(frozen=True)
class PreparedDraftMedia:
    stored: StoredMedia
    content: bytes


@dataclass(frozen=True)
class ImageDerivative:
    media_id: str
    role: str
    stored: StoredMedia
    width: int
    height: int
    content: bytes


@dataclass(frozen=True)
class ImageDerivativePair:
    derivative_set_id: str
    source_sha256: str
    thumbnail: ImageDerivative
    display: ImageDerivative


@dataclass(frozen=True)
class PublishedMedia:
    object_key: str
    sha256: str
    byte_size: int
    mime_type: str


@dataclass(frozen=True)
class PresignedUpload:
    media_id: str
    object_key: str
    upload_url: str
    expires_seconds: int
    headers: dict[str, str]


@dataclass(frozen=True)
class VideoProbe:
    container_format: str
    video_codec: str
    audio_codec: str
    width: int = 1920
    height: int = 1080


@dataclass(frozen=True)
class VideoUploadRecord:
    media_id: str
    quarantine_key: str
    draft_key: str
    original_name: str
    declared_size: int
    expected_sha256: str
    status: str = "uploading"
    stored_media: StoredMedia | None = None
    error: str | None = None
    pending_draft_cleanup: bool = False
    width: int | None = None
    height: int | None = None


class VideoUploadStore(Protocol):
    def create(self, record: VideoUploadRecord) -> None: ...

    def get(self, media_id: str) -> VideoUploadRecord: ...

    def claim(self, media_id: str) -> VideoUploadRecord: ...

    def set_dimensions(self, media_id: str, width: int, height: int) -> None: ...

    def finalize(self, media_id: str, stored_media: StoredMedia) -> None: ...

    def reconcile_after_finalize_error(self, media_id: str) -> VideoUploadRecord:
        """Clear the failed transaction and return freshly read durable state."""
        ...

    def prepare_retry(
        self, media_id: str, error: str, *, pending_draft_cleanup: bool
    ) -> None: ...

    def fail(self, media_id: str, error: str) -> None: ...


class InMemoryVideoUploadStore:
    """Thread-safe test/reference state store; production APIs inject persistence."""

    def __init__(self) -> None:
        self._records: dict[str, VideoUploadRecord] = {}
        self._lock = RLock()

    def create(self, record: VideoUploadRecord) -> None:
        with self._lock:
            if record.media_id in self._records:
                raise MediaStateError("video upload already exists")
            self._records[record.media_id] = record

    def get(self, media_id: str) -> VideoUploadRecord:
        with self._lock:
            try:
                return self._records[media_id]
            except KeyError as error:
                raise MediaStateError("unknown video upload") from error

    def claim(self, media_id: str) -> VideoUploadRecord:
        with self._lock:
            record = self.get(media_id)
            if record.status == "finalized":
                return record
            if record.status != "uploading":
                raise MediaStateError(f"video upload cannot finalize from {record.status}")
            claimed = replace(record, status="finalizing")
            self._records[media_id] = claimed
            return claimed

    def finalize(self, media_id: str, stored_media: StoredMedia) -> None:
        with self._lock:
            record = self.get(media_id)
            if record.status != "finalizing":
                raise MediaStateError("video upload lost its finalizing claim")
            self._records[media_id] = replace(
                record,
                status="finalized",
                stored_media=stored_media,
                error=None,
                pending_draft_cleanup=False,
            )

    def set_dimensions(self, media_id: str, width: int, height: int) -> None:
        with self._lock:
            record = self.get(media_id)
            if record.status != "finalizing":
                raise MediaStateError("video upload lost its finalizing claim")
            self._records[media_id] = replace(
                record, width=width, height=height
            )

    def reconcile_after_finalize_error(self, media_id: str) -> VideoUploadRecord:
        return self.get(media_id)

    def prepare_retry(
        self, media_id: str, error: str, *, pending_draft_cleanup: bool
    ) -> None:
        with self._lock:
            record = self.get(media_id)
            if record.status != "finalizing":
                raise MediaStateError("video upload lost its finalizing claim")
            self._records[media_id] = replace(
                record,
                status="uploading",
                error=error[:240],
                pending_draft_cleanup=pending_draft_cleanup,
            )

    def fail(self, media_id: str, error: str) -> None:
        with self._lock:
            record = self.get(media_id)
            if record.status != "finalized":
                self._records[media_id] = replace(
                    record, status="failed", error=error[:240]
                )


ProbeVideo = Callable[[Path, int], VideoProbe]
DiskFreeBytes = Callable[[Path], int]


def _default_probe_video(path: Path, timeout_seconds: int) -> VideoProbe:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=format_name:stream=codec_type,codec_name,width,height",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=True,
        text=True,
        timeout=timeout_seconds,
    )
    payload = json.loads(result.stdout)
    streams = payload.get("streams", [])
    video_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "video"), {}
    )
    audio_stream = next(
        (stream for stream in streams if stream.get("codec_type") == "audio"), {}
    )
    container_format = str(payload.get("format", {}).get("format_name", ""))
    return VideoProbe(
        container_format,
        str(video_stream.get("codec_name", "")),
        str(audio_stream.get("codec_name", "")),
        int(video_stream.get("width") or 0),
        int(video_stream.get("height") or 0),
    )


def _default_disk_free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _safe_original_suffix(original_name: str) -> str:
    if not original_name or Path(original_name).name != original_name:
        raise MediaValidationError("media filename must not contain a path")
    return Path(original_name).suffix.lower()


def _normalize_embedded_color_to_srgb(image: Image.Image) -> Image.Image:
    icc_profile = image.info.get("icc_profile")
    if not icc_profile:
        return image
    alpha = image.getchannel("A") if "A" in image.getbands() else None
    normalized = ImageCms.profileToProfile(
        image.convert("RGB"),
        ImageCms.ImageCmsProfile(BytesIO(icc_profile)),
        ImageCms.createProfile("sRGB"),
        outputMode="RGB",
    )
    if alpha is not None:
        normalized.putalpha(alpha)
    return normalized


def _validate_image(content: bytes, content_type: str, original_name: str) -> tuple[bytes, str, str]:
    if not content or len(content) > MAX_IMAGE_BYTES:
        raise MediaValidationError("image must be between 1 byte and 8 MB")
    suffix = _safe_original_suffix(original_name)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as candidate:
                image_format = str(candidate.format or "").upper()
                definition = _IMAGE_FORMATS.get(image_format)
                if definition is None:
                    raise MediaValidationError("image format is not allowed")
                actual_mime, output_suffix, allowed_suffixes = definition
                if content_type != actual_mime or suffix not in allowed_suffixes:
                    raise MediaValidationError("image MIME, signature, and extension differ")
                width, height = candidate.size
                if width > MAX_IMAGE_WIDTH or height > MAX_IMAGE_HEIGHT:
                    raise MediaValidationError("image dimensions exceed the limit")
                if width * height > MAX_IMAGE_PIXELS:
                    raise MediaValidationError("image pixel count exceeds the limit")
                if width * height * 4 > MAX_IMAGE_PROCESSING_BYTES:
                    raise MediaValidationError("image processing memory exceeds the limit")
                candidate.verify()

            with Image.open(BytesIO(content)) as decoded:
                normalized = ImageOps.exif_transpose(decoded)
                normalized.load()
                if normalized.width * normalized.height > MAX_IMAGE_PIXELS:
                    raise MediaValidationError("decoded image pixel count exceeds the limit")
                if normalized.width * normalized.height * 4 > MAX_IMAGE_PROCESSING_BYTES:
                    raise MediaValidationError("decoded image processing memory exceeds the limit")
                normalized = _normalize_embedded_color_to_srgb(normalized)
                if image_format == "JPEG" and normalized.mode not in {"RGB", "L"}:
                    normalized = normalized.convert("RGB")
                output = BytesIO()
                save_options: dict[str, object] = {}
                if image_format == "JPEG":
                    save_options = {"quality": 90, "optimize": True}
                normalized.save(output, format=image_format, **save_options)
                sanitized = output.getvalue()
    except MediaValidationError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
        ImageCms.PyCMSError,
    ) as error:
        raise MediaValidationError("image failed complete safe decoding") from error
    if not sanitized or len(sanitized) > MAX_IMAGE_BYTES:
        raise MediaValidationError("sanitized image exceeds the size limit")
    return sanitized, actual_mime, output_suffix


def _fit_long_edge(image: Image.Image, long_edge: int) -> Image.Image:
    width, height = image.size
    scale = min(1.0, long_edge / max(width, height))
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image.copy()


def _jpeg_derivative(
    image: Image.Image,
    *,
    long_edge: int,
    max_bytes: int | None,
    qualities: tuple[int, ...],
) -> tuple[bytes, int, int]:
    candidate = _fit_long_edge(image.convert("RGB"), long_edge)
    while True:
        for quality in qualities:
            output = BytesIO()
            candidate.save(
                output,
                format="JPEG",
                quality=quality,
                optimize=True,
                progressive=True,
            )
            content = output.getvalue()
            if max_bytes is None or len(content) <= max_bytes:
                return content, candidate.width, candidate.height
        if min(candidate.size) <= 320:
            raise MediaValidationError("image cannot meet the derivative byte budget")
        candidate = candidate.resize(
            (max(1, round(candidate.width * 0.9)), max(1, round(candidate.height * 0.9))),
            Image.Resampling.LANCZOS,
        )


def _derivative_identity(source_sha256: str, role: str) -> tuple[str, str]:
    derivative_set_id = f"MDS-{source_sha256[:20]}"
    media_digest = hashlib.sha256(f"{derivative_set_id}:{role}".encode()).hexdigest()
    return derivative_set_id, f"MCM-{media_digest[:20]}"


def public_media_object_key(
    media: StoredMedia,
    *,
    derivative_set_id: str | None = None,
    derivative_role: str | None = None,
) -> str:
    suffix = _MIME_EXTENSIONS.get(media.mime_type)
    if suffix is None or not _SHA256.fullmatch(media.sha256):
        raise MediaValidationError("stored media metadata is invalid")
    if (derivative_set_id is None) != (derivative_role is None):
        raise MediaValidationError("derivative public identity is incomplete")
    if derivative_set_id is None:
        return f"miniapp/media/{media.sha256}.{suffix}"
    if (
        media.mime_type not in {"image/jpeg", "image/png", "image/webp"}
        or not re.fullmatch(r"MDS-[0-9a-f]{20}", derivative_set_id)
        or derivative_role not in {"thumbnail", "display"}
    ):
        raise MediaValidationError("image derivative public identity is invalid")
    return (
        f"miniapp/media/image-sets/{derivative_set_id}/"
        f"{derivative_role}/{media.sha256}.{suffix}"
    )


class ContentMediaService:
    def __init__(
        self,
        storage: ContentStorage,
        *,
        upload_store: VideoUploadStore,
        probe_video: ProbeVideo = _default_probe_video,
        temp_directory: Path | None = None,
        disk_free_bytes: DiskFreeBytes = _default_disk_free_bytes,
    ) -> None:
        self._storage = storage
        self._upload_store = upload_store
        self._probe_video = probe_video
        self._temp_directory = Path(temp_directory or tempfile.gettempdir())
        self._disk_free_bytes = disk_free_bytes

    def prepare_draft_media(
        self, content: bytes, content_type: str, original_name: str
    ) -> PreparedDraftMedia:
        sanitized, actual_mime, suffix = _validate_image(
            content, content_type, original_name
        )
        digest = hashlib.sha256(sanitized).hexdigest()
        object_key = f"miniapp/drafts/{uuid4().hex}.{suffix}"
        return PreparedDraftMedia(
            StoredMedia(object_key, digest, len(sanitized), actual_mime),
            sanitized,
        )

    def write_draft_media(self, prepared: PreparedDraftMedia) -> StoredMedia:
        stored = prepared.stored
        self._storage.put(
            stored.object_key,
            prepared.content,
            content_type=stored.mime_type,
            acl="private",
        )
        return stored

    def upload_draft_media(
        self, content: bytes, content_type: str, original_name: str
    ) -> StoredMedia:
        return self.write_draft_media(
            self.prepare_draft_media(content, content_type, original_name)
        )

    def prepare_image_derivative_pair(
        self, content: bytes, content_type: str, original_name: str
    ) -> ImageDerivativePair:
        source_sha256 = hashlib.sha256(content).hexdigest()
        sanitized, _actual_mime, _suffix = _validate_image(
            content, content_type, original_name
        )
        try:
            with Image.open(BytesIO(sanitized)) as decoded:
                decoded.load()
                source_image = decoded.convert("RGB")
        except (UnidentifiedImageError, OSError, ValueError) as error:
            raise MediaValidationError("image failed derivative decoding") from error
        if max(source_image.size) < MIN_PAIR_SOURCE_LONG_EDGE:
            raise MediaValidationError(
                "image pair source must be at least 640 pixels on its long edge"
            )

        specs = (
            ("thumbnail", THUMBNAIL_LONG_EDGE, THUMBNAIL_MAX_BYTES, (68, 60, 52, 45)),
            ("display", DISPLAY_LONG_EDGE, DISPLAY_MAX_BYTES, (92, 90, 88, 84, 80, 76, 72, 68, 60, 52, 45)),
        )
        derivatives: dict[str, ImageDerivative] = {}
        for role, long_edge, max_bytes, qualities in specs:
            derivative_set_id, media_id = _derivative_identity(source_sha256, role)
            derivative_bytes, width, height = _jpeg_derivative(
                source_image,
                long_edge=long_edge,
                max_bytes=max_bytes,
                qualities=qualities,
            )
            derivative_sha256 = hashlib.sha256(derivative_bytes).hexdigest()
            object_key = (
                f"miniapp/drafts/image-sets/{derivative_set_id}/"
                f"g1/{role}-{derivative_sha256}.jpg"
            )
            derivatives[role] = ImageDerivative(
                media_id=media_id,
                role=role,
                stored=StoredMedia(
                    object_key=object_key,
                    sha256=derivative_sha256,
                    byte_size=len(derivative_bytes),
                    mime_type="image/jpeg",
                ),
                width=width,
                height=height,
                content=derivative_bytes,
            )
        if derivatives["thumbnail"].stored.sha256 == derivatives["display"].stored.sha256:
            raise MediaValidationError("image derivative identities collided")
        return ImageDerivativePair(
            derivative_set_id=f"MDS-{source_sha256[:20]}",
            source_sha256=source_sha256,
            thumbnail=derivatives["thumbnail"],
            display=derivatives["display"],
        )

    @staticmethod
    def image_pair_with_keys(
        pair: ImageDerivativePair,
        *,
        thumbnail_key: str,
        display_key: str,
    ) -> ImageDerivativePair:
        prefix = f"miniapp/drafts/image-sets/{pair.derivative_set_id}/"
        if (
            thumbnail_key == display_key
            or not thumbnail_key.startswith(prefix)
            or not display_key.startswith(prefix)
            or "/thumbnail-" not in thumbnail_key
            or "/display-" not in display_key
        ):
            raise MediaValidationError("reserved image derivative keys are invalid")
        return replace(
            pair,
            thumbnail=replace(
                pair.thumbnail,
                stored=replace(pair.thumbnail.stored, object_key=thumbnail_key),
            ),
            display=replace(
                pair.display,
                stored=replace(pair.display.stored, object_key=display_key),
            ),
        )

    def write_image_derivative_pair(
        self, pair: ImageDerivativePair
    ) -> ImageDerivativePair:
        for derivative in (pair.thumbnail, pair.display):
            metadata = {
                "sha256": derivative.stored.sha256,
                "source-sha256": pair.source_sha256,
                "derivative-set-id": pair.derivative_set_id,
                "derivative-role": derivative.role,
            }
            try:
                self._storage.put(
                    derivative.stored.object_key,
                    derivative.content,
                    content_type=derivative.stored.mime_type,
                    acl="private",
                    metadata=metadata,
                )
            except StorageError as write_error:
                try:
                    head = self._verify_object_bytes(
                        derivative.stored.object_key,
                        derivative.stored.sha256,
                        derivative.stored.byte_size,
                        derivative.stored.mime_type,
                    )
                except (StorageError, MediaVerificationError):
                    raise write_error
                if head.acl != "private" or dict(head.metadata) != metadata:
                    raise MediaVerificationError(
                        "image derivative acknowledgement could not be reconciled"
                    )
            else:
                head = self._verify_object_bytes(
                    derivative.stored.object_key,
                    derivative.stored.sha256,
                    derivative.stored.byte_size,
                    derivative.stored.mime_type,
                )
                if head.acl != "private" or dict(head.metadata) != metadata:
                    raise MediaVerificationError(
                        "stored image derivative provenance does not match"
                    )
        return pair

    def generate_image_derivative_pair(
        self, content: bytes, content_type: str, original_name: str
    ) -> ImageDerivativePair:
        pair = self.prepare_image_derivative_pair(content, content_type, original_name)
        return self.write_image_derivative_pair(pair)

    def create_video_upload(
        self, original_name: str, byte_size: int, sha256: str
    ) -> PresignedUpload:
        if _safe_original_suffix(original_name) != ".mp4":
            raise MediaValidationError("video filename must end in .mp4")
        if not 1 <= byte_size <= MAX_VIDEO_BYTES:
            raise MediaValidationError("video must be between 1 byte and 80 MB")
        if not _SHA256.fullmatch(sha256):
            raise MediaValidationError("video SHA-256 declaration is invalid")
        media_id = f"MCM-{uuid4().hex[:20]}"
        object_id = uuid4().hex
        object_key = f"miniapp/quarantine/{object_id}.mp4"
        record = VideoUploadRecord(
            media_id=media_id,
            quarantine_key=object_key,
            draft_key=f"miniapp/drafts/{object_id}.mp4",
            original_name=original_name,
            declared_size=byte_size,
            expected_sha256=sha256,
        )
        self._upload_store.create(record)
        try:
            upload_url = self._storage.sign(
                "PUT",
                object_key,
                expires_seconds=VIDEO_UPLOAD_EXPIRES_SECONDS,
                content_type="video/mp4",
                acl="private",
            )
        except Exception as error:
            self._upload_store.fail(media_id, error.__class__.__name__)
            raise
        return PresignedUpload(
            media_id=media_id,
            object_key=object_key,
            upload_url=upload_url,
            expires_seconds=VIDEO_UPLOAD_EXPIRES_SECONDS,
            headers={
                "Content-Type": "video/mp4",
                "x-oss-object-acl": "private",
            },
        )

    def finalize_video_upload(self, media_id: str, *, cleanup_quarantine: bool = True) -> StoredMedia:
        record = self._upload_store.claim(media_id)
        if record.status == "finalized":
            assert record.stored_media is not None
            if cleanup_quarantine:
                self._storage.delete(record.quarantine_key)
            return record.stored_media

        temp_path: Path | None = None
        retry_preserved = False
        try:
            if record.pending_draft_cleanup:
                try:
                    self._storage.delete(record.draft_key)
                except Exception as error:
                    self._upload_store.prepare_retry(
                        media_id,
                        error.__class__.__name__,
                        pending_draft_cleanup=True,
                    )
                    retry_preserved = True
                    raise
            try:
                source_head = self._storage.head(record.quarantine_key)
            except StorageNotFoundError as error:
                raise MediaVerificationError("quarantine upload is missing") from error
            if source_head.content_type != "video/mp4":
                raise MediaVerificationError("video MIME does not match video/mp4")
            if source_head.byte_size != record.declared_size:
                raise MediaVerificationError("video size does not match declaration")
            required_free = record.declared_size + TEMP_DISK_RESERVE_BYTES
            if self._disk_free_bytes(self._temp_directory) < required_free:
                raise MediaVerificationError("temporary disk has insufficient free space")

            with tempfile.NamedTemporaryFile(
                mode="wb",
                suffix=".mp4",
                prefix="miniapp-video-",
                dir=self._temp_directory,
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                digest = hashlib.sha256()
                total = 0
                for chunk in self._storage.read(
                    record.quarantine_key, chunk_size=VIDEO_READ_CHUNK_BYTES
                ):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > record.declared_size or total > MAX_VIDEO_BYTES:
                        raise MediaVerificationError(
                            "video stream exceeds its declared size"
                        )
                    temp_file.write(chunk)
                    digest.update(chunk)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            if total != record.declared_size:
                raise MediaVerificationError("video stream ended before declared size")
            if digest.hexdigest() != record.expected_sha256:
                raise MediaVerificationError("video SHA-256 does not match declaration")

            try:
                probe = self._probe_video(temp_path, FFPROBE_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired as error:
                raise MediaVerificationError("ffprobe timed out") from error
            except (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError) as error:
                raise MediaVerificationError("ffprobe could not verify video") from error
            if not isinstance(probe, VideoProbe):
                raise MediaVerificationError("ffprobe returned malformed stream data")
            container_formats = {
                item.strip().lower()
                for item in probe.container_format.split(",")
                if item.strip()
            }
            if not container_formats.intersection({"mp4", "mov"}):
                raise MediaVerificationError("video container must be MP4/MOV")
            if probe.video_codec.lower() not in {"h264", "avc1"}:
                raise MediaVerificationError("video must use H.264")
            if probe.audio_codec.lower() != "aac":
                raise MediaVerificationError("video must use AAC")
            if probe.width <= 0 or probe.height <= 0:
                raise MediaVerificationError("video dimensions are invalid")

            draft_key = record.draft_key
            try:
                self._storage.copy(
                    record.quarantine_key,
                    draft_key,
                    content_type="video/mp4",
                    acl="private",
                    metadata={"sha256": record.expected_sha256},
                    source_etag=source_head.etag,
                )
            except StorageConflictError as error:
                # A prior request can have copied this exact immutable draft
                # before its DB commit failed.  Read-back makes that crash
                # window a safe retry rather than an unrecoverable conflict.
                try:
                    self._verify_object_bytes(
                        draft_key, record.expected_sha256, record.declared_size, "video/mp4"
                    )
                except StorageNotFoundError:
                    raise MediaVerificationError(
                        "quarantine upload changed during finalization"
                    ) from error
            self._verify_object_bytes(
                draft_key,
                record.expected_sha256,
                record.declared_size,
                "video/mp4",
            )
            stored = StoredMedia(
                draft_key,
                record.expected_sha256,
                record.declared_size,
                "video/mp4",
            )
            try:
                self._upload_store.set_dimensions(
                    media_id, probe.width, probe.height
                )
                self._upload_store.finalize(media_id, stored)
            except Exception as persistence_error:
                try:
                    durable = self._upload_store.reconcile_after_finalize_error(media_id)
                except Exception as reconciliation_error:
                    retry_preserved = True
                    raise MediaStateError(
                        "video finalize outcome could not be reconciled"
                    ) from reconciliation_error
                if (
                    durable.status == "finalized"
                    and durable.draft_key == draft_key
                    and durable.stored_media == stored
                ):
                    if cleanup_quarantine:
                        self._storage.delete(record.quarantine_key)
                    return stored
                if durable.status == "finalized":
                    retry_preserved = True
                    raise MediaStateError(
                        "video finalize durable state does not match copied draft"
                    ) from persistence_error
                try:
                    self._storage.delete(draft_key)
                except Exception as cleanup_error:
                    self._upload_store.prepare_retry(
                        media_id,
                        persistence_error.__class__.__name__,
                        pending_draft_cleanup=True,
                    )
                    retry_preserved = True
                    raise cleanup_error from persistence_error
                self._upload_store.prepare_retry(
                    media_id,
                    persistence_error.__class__.__name__,
                    pending_draft_cleanup=False,
                )
                retry_preserved = True
                raise
            if cleanup_quarantine:
                self._storage.delete(record.quarantine_key)
            return stored
        except Exception as error:
            if not retry_preserved:
                self._upload_store.fail(media_id, error.__class__.__name__)
            raise
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    def sign_draft_preview(
        self, object_key: str, expires_seconds: int = 900
    ) -> str:
        if (
            not object_key.startswith("miniapp/drafts/")
            or ".." in object_key.split("/")
            or "\\" in object_key
        ):
            raise StorageKeyError(
                "sign", object_key, "only private draft objects can be previewed"
            )
        return self._storage.sign(
            "GET", object_key, expires_seconds=expires_seconds
        )

    def promote_media(
        self,
        media: StoredMedia,
        *,
        derivative_set_id: str | None = None,
        derivative_role: str | None = None,
    ) -> PublishedMedia:
        public_key = public_media_object_key(
            media,
            derivative_set_id=derivative_set_id,
            derivative_role=derivative_role,
        )
        try:
            public_head = self._storage.head(public_key)
        except StorageNotFoundError:
            source_head = self._verify_object_bytes(
                media.object_key,
                media.sha256,
                media.byte_size,
                media.mime_type,
            )
            try:
                self._storage.copy(
                    media.object_key,
                    public_key,
                    content_type=media.mime_type,
                    acl="public-read",
                    cache_control=IMMUTABLE_CACHE_CONTROL,
                    metadata={"sha256": media.sha256},
                    source_etag=source_head.etag,
                )
            except StorageConflictError:
                # Another publisher may have won the immutable-key race. The
                # mandatory read-back below decides whether it is reusable. If
                # no target exists, the source changed after verification.
                try:
                    self.verify_public_object(
                        public_key,
                        media.sha256,
                        media.byte_size,
                        media.mime_type,
                    )
                except StorageNotFoundError as error:
                    raise MediaVerificationError(
                        "draft changed before immutable promotion"
                    ) from error
            else:
                self.verify_public_object(
                    public_key, media.sha256, media.byte_size, media.mime_type
                )
        else:
            self._verify_public_head(
                public_head, media.sha256, media.byte_size, media.mime_type
            )
        return PublishedMedia(
            public_key, media.sha256, media.byte_size, media.mime_type
        )

    def verify_public_object(
        self,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
        expected_mime: str,
    ) -> None:
        head = self._storage.head(object_key)
        self._verify_public_head(
            head, expected_sha256, expected_size, expected_mime
        )

    def _verify_public_head(
        self,
        head,
        expected_sha256: str,
        expected_size: int,
        expected_mime: str,
    ) -> None:
        if head.acl != "public-read":
            raise MediaVerificationError("public object ACL is not public-read")
        if head.content_type != expected_mime:
            raise MediaVerificationError("public object MIME does not match")
        if head.byte_size != expected_size:
            raise MediaVerificationError("public object size does not match")
        if head.cache_control != IMMUTABLE_CACHE_CONTROL:
            raise MediaVerificationError("public object cache policy is not immutable")
        if head.metadata.get("sha256") != expected_sha256:
            raise MediaVerificationError("public object SHA-256 metadata does not match")
        self._verify_object_bytes(
            head.object_key,
            expected_sha256,
            expected_size,
            expected_mime,
            head=head,
        )

    def _verify_object_bytes(
        self,
        object_key: str,
        expected_sha256: str,
        expected_size: int,
        expected_mime: str,
        *,
        head=None,
    ):
        head = head or self._storage.head(object_key)
        if head.content_type != expected_mime:
            raise MediaVerificationError("object MIME does not match")
        if head.byte_size != expected_size:
            raise MediaVerificationError("object size does not match")
        digest = hashlib.sha256()
        total = 0
        for chunk in self._storage.read(
            object_key, chunk_size=PUBLIC_VERIFY_CHUNK_BYTES
        ):
            total += len(chunk)
            if total > expected_size:
                raise MediaVerificationError("object read-back exceeds expected size")
            digest.update(chunk)
        if total != expected_size:
            raise MediaVerificationError("object read-back size does not match")
        if digest.hexdigest() != expected_sha256:
            raise MediaVerificationError("object read-back SHA-256 does not match")
        return head


_default_service: ContentMediaService | None = None


def configure_default_media_service(service: ContentMediaService) -> None:
    global _default_service
    _default_service = service


def _service() -> ContentMediaService:
    if _default_service is None:
        raise StorageNotConfiguredError(
            "configure", None, "content media service has not been injected"
        )
    return _default_service


def upload_draft_media(
    content: bytes, content_type: str, original_name: str
) -> StoredMedia:
    return _service().upload_draft_media(content, content_type, original_name)


def create_video_upload(
    original_name: str, byte_size: int, sha256: str
) -> PresignedUpload:
    return _service().create_video_upload(original_name, byte_size, sha256)


def finalize_video_upload(media_id: str) -> StoredMedia:
    return _service().finalize_video_upload(media_id)


def sign_draft_preview(object_key: str, expires_seconds: int = 900) -> str:
    return _service().sign_draft_preview(object_key, expires_seconds)


def promote_media(media: StoredMedia) -> PublishedMedia:
    return _service().promote_media(media)


def verify_public_object(
    object_key: str,
    expected_sha256: str,
    expected_size: int,
    expected_mime: str,
) -> None:
    _service().verify_public_object(
        object_key, expected_sha256, expected_size, expected_mime
    )


__all__ = [
    "MAX_IMAGE_BYTES",
    "MAX_VIDEO_BYTES",
    "ContentMediaService",
    "InMemoryVideoUploadStore",
    "MediaError",
    "MediaStateError",
    "MediaValidationError",
    "MediaVerificationError",
    "PresignedUpload",
    "PreparedDraftMedia",
    "PublishedMedia",
    "StoredMedia",
    "VideoProbe",
    "VideoUploadRecord",
    "VideoUploadStore",
    "configure_default_media_service",
    "create_video_upload",
    "finalize_video_upload",
    "promote_media",
    "sign_draft_preview",
    "upload_draft_media",
    "verify_public_object",
]
