"""Aliyun OSS object storage wrapper.

Thin layer over oss2 SDK that:
- Lazily constructs a Bucket singleton from app settings
- Uploads image bytes under a deterministic key `rooms/{room_id}/{uuid}.{ext}`
- Returns the public URL (bucket is configured as 公共读)
- Supports deletion by object_key

Configured by settings.OSS_* env vars. In environments without OSS credentials
(local dev without keys, test suite), callers receive RuntimeError from the
service — endpoints should either 503 or use an in-memory test double.
"""
from __future__ import annotations

import logging
import uuid
from typing import Optional
from urllib.parse import quote

from app.core.config import settings

logger = logging.getLogger(__name__)

_CONTENT_TYPE_TO_EXT = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

ALLOWED_CONTENT_TYPES = frozenset(_CONTENT_TYPE_TO_EXT.keys())
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB

_bucket = None


class OSSNotConfigured(RuntimeError):
    """Raised when OSS operations are attempted without valid credentials."""


def _endpoint_to_host(endpoint: str) -> str:
    """Strip protocol for oss2.Auth/Bucket construction."""
    if endpoint.startswith("https://"):
        return endpoint[len("https://"):]
    if endpoint.startswith("http://"):
        return endpoint[len("http://"):]
    return endpoint


def _get_bucket():
    """Lazy init + cache a single oss2.Bucket instance."""
    global _bucket
    if _bucket is not None:
        return _bucket
    if not (
        settings.OSS_ACCESS_KEY_ID
        and settings.OSS_ACCESS_KEY_SECRET
        and settings.OSS_BUCKET
        and settings.OSS_ENDPOINT
    ):
        raise OSSNotConfigured(
            "OSS credentials not configured. Set OSS_ACCESS_KEY_ID / "
            "OSS_ACCESS_KEY_SECRET / OSS_BUCKET / OSS_ENDPOINT env vars."
        )

    # Import here so test environments without oss2 installed can still import
    # this module for patching purposes.
    import oss2

    auth = oss2.Auth(settings.OSS_ACCESS_KEY_ID, settings.OSS_ACCESS_KEY_SECRET)
    # 上传/删除走加速端点（OSS_UPLOAD_ENDPOINT，生产=oss-accelerate）——跨海写显著变快。
    # 空则退回区域端点。connect_timeout 兼作读写 socket 超时（配合 upload_image 重试）。
    upload_ep = settings.OSS_UPLOAD_ENDPOINT or settings.OSS_ENDPOINT
    _bucket = oss2.Bucket(auth, upload_ep, settings.OSS_BUCKET, connect_timeout=20)
    return _bucket


def _build_public_url(object_key: str) -> str:
    """Construct a public URL for a bucket-accessible object.

    Format: https://{bucket}.{endpoint-host}/{percent-encoded-key}

    The object_key is percent-encoded (preserving `/`) so URLs with spaces or
    other reserved characters in room_id are valid in HTML / CSS contexts —
    e.g. CSS `background: url(...)` rejects unquoted unencoded spaces.
    """
    host = _endpoint_to_host(settings.OSS_ENDPOINT)
    encoded_key = quote(object_key, safe="/")
    return f"https://{settings.OSS_BUCKET}.{host}/{encoded_key}"


def upload_image(
    content: bytes,
    content_type: str,
    room_id: str,
) -> tuple[str, str]:
    """Upload image bytes; return (public_url, object_key).

    Caller is responsible for pre-validating size (`MAX_IMAGE_BYTES`) and
    content_type (`ALLOWED_CONTENT_TYPES`). This function does not re-validate
    to keep OSS operations isolated from HTTP-layer concerns.
    """
    import oss2

    ext = _CONTENT_TYPE_TO_EXT.get(content_type, "bin")
    object_key = f"rooms/{room_id}/{uuid.uuid4().hex}.{ext}"
    # 跨海写偶发超时/连接中断 → 重试 3 次，每次失败清缓存重建连接。最后一次仍失败才抛。
    for attempt in range(3):
        try:
            _get_bucket().put_object(
                object_key, content, headers={"Content-Type": content_type},
            )
            return _build_public_url(object_key), object_key
        except oss2.exceptions.RequestError:
            reset_bucket_cache()
            if attempt == 2:
                raise
    raise RuntimeError("unreachable")  # pragma: no cover


def delete_object(object_key: str) -> None:
    """Delete an OSS object. Swallows "not found" so retry is idempotent."""
    try:
        bucket = _get_bucket()
        bucket.delete_object(object_key)
    except OSSNotConfigured:
        raise
    except Exception as e:
        # Deletion is best-effort; we don't want OSS hiccups to block DB cleanup.
        logger.warning("OSS delete failed for %s: %s", object_key, e)


def reset_bucket_cache() -> None:
    """Test-only: force next _get_bucket() to re-init from env."""
    global _bucket
    _bucket = None
