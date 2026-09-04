"""Strict object storage boundary for mini-program content.

This module intentionally does not reuse :mod:`app.services.oss_service`.
Room-image deletion is best effort; content publishing requires every storage
failure to remain visible to its caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import import_module
import re
from typing import Iterable, Iterator, Mapping, Protocol, runtime_checkable


IMMUTABLE_CACHE_CONTROL = "public, max-age=31536000, immutable"
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        "application/json",
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
    }
)
_PUBLIC_MEDIA_KEY = re.compile(
    r"miniapp/media/(?:[0-9a-f]{64}\.(?:jpg|png|webp|mp4)"
    r"|image-sets/MDS-[0-9a-f]{20}/(?:thumbnail|display)/"
    r"[0-9a-f]{64}\.(?:jpg|png|webp))\Z"
)
_PUBLIC_CONTENT_KEY = re.compile(
    r"miniapp/content/(?:owner|stay_guide|travel)/"
    r"(?:[A-Za-z0-9][A-Za-z0-9._-]*/)*[A-Za-z0-9][A-Za-z0-9._-]*\Z"
)
_PUBLIC_RELEASE_MANIFEST_KEY = re.compile(
    r"miniapp/releases/(?:owner|stay_guide|travel)/"
    r"[A-Za-z0-9][A-Za-z0-9._-]*/manifest\.json\Z"
)
_PUBLIC_CANDIDATE_KEY = re.compile(
    r"miniapp/content/(?:owner|stay_guide|travel)/candidates/"
    r"[A-Za-z0-9][A-Za-z0-9._-]*/[1-9][0-9]*-[a-f0-9]{32}\.json\Z"
)
_CURRENT_POINTER_KEY = re.compile(
    r"miniapp/content/(?:owner|stay_guide|travel)/current\.json\Z"
)
_METADATA_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")


class StorageError(RuntimeError):
    """Base error for a failed strict content-storage operation."""

    def __init__(
        self,
        operation: str,
        object_key: str | None,
        message: str,
        *,
        request_id: str | None = None,
        status: int | None = None,
        provider_code: str | None = None,
        retryable: bool = False,
    ) -> None:
        self.operation = operation
        self.object_key = object_key
        self.request_id = request_id
        self.status = status
        self.provider_code = provider_code
        self.retryable = retryable
        super().__init__(message)


class OSSNotConfigured(StorageError):
    pass


class StorageKeyError(StorageError):
    pass


class StorageNotFoundError(StorageError):
    pass


class OSSAccessDeniedError(StorageError):
    pass


class OSSRequestTimeout(StorageError):
    pass


class StorageConflictError(StorageError):
    pass


class StorageWriteError(StorageError):
    pass


class StorageHeadError(StorageError):
    pass


class StorageReadError(StorageError):
    pass


class StorageCopyError(StorageError):
    pass


class StorageDeleteError(StorageError):
    pass


class StorageSignError(StorageError):
    pass


# Descriptive adapter aliases retained for callers that do not need to know the
# provider. The canonical names match the later publisher retry/error contract.
StorageNotConfiguredError = OSSNotConfigured
StorageAccessDeniedError = OSSAccessDeniedError
StorageTimeoutError = OSSRequestTimeout


@dataclass(frozen=True)
class ObjectHead:
    object_key: str
    byte_size: int
    content_type: str
    etag: str
    acl: str
    cache_control: str | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class ContentStorage(Protocol):
    """The complete object-store surface available to content services."""

    def put(
        self,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        acl: str,
        cache_control: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None: ...

    def head(self, object_key: str) -> ObjectHead: ...

    def read(self, object_key: str, *, chunk_size: int) -> Iterable[bytes]: ...

    def copy(
        self,
        source_key: str,
        target_key: str,
        *,
        content_type: str,
        acl: str,
        cache_control: str | None = None,
        metadata: Mapping[str, str] | None = None,
        source_etag: str | None = None,
    ) -> None: ...

    def delete(self, object_key: str) -> None: ...

    def delete_current_pointer(self, object_key: str) -> None: ...

    def sign(
        self,
        method: str,
        object_key: str,
        *,
        expires_seconds: int,
        content_type: str | None = None,
        acl: str | None = None,
    ) -> str: ...


def _safe_key(object_key: str, operation: str) -> str:
    if (
        not object_key
        or object_key.startswith("/")
        or "\\" in object_key
        or any(part in {"", ".", ".."} for part in object_key.split("/"))
        or any(ord(char) < 32 for char in object_key)
    ):
        raise StorageKeyError(operation, object_key, "unsafe content object key")
    return object_key


def _key_kind(object_key: str, operation: str) -> str:
    key = _safe_key(object_key, operation)
    if key.startswith("miniapp/drafts/"):
        return "draft"
    if key.startswith("miniapp/quarantine/"):
        return "quarantine"
    if _PUBLIC_MEDIA_KEY.fullmatch(key):
        return "media"
    if _PUBLIC_RELEASE_MANIFEST_KEY.fullmatch(key):
        return "release"
    if _PUBLIC_CANDIDATE_KEY.fullmatch(key):
        return "candidate"
    if _PUBLIC_CONTENT_KEY.fullmatch(key):
        return "public"
    raise StorageKeyError(operation, key, "object key is outside the content allowlist")


def _validated_headers(
    *,
    operation: str,
    object_key: str,
    kind: str,
    content_type: str,
    acl: str,
    cache_control: str | None,
    metadata: Mapping[str, str] | None,
    source_etag: str | None = None,
) -> dict[str, str]:
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise StorageKeyError(operation, object_key, "content type is not allowed")
    expected_acl = "private" if kind in {"draft", "quarantine"} else "public-read"
    if acl != expected_acl:
        raise StorageKeyError(operation, object_key, f"{kind} objects require {expected_acl} ACL")
    if kind in {"media", "release"} and cache_control != IMMUTABLE_CACHE_CONTROL:
        raise StorageKeyError(
            operation, object_key, "public immutable objects require immutable caching"
        )
    if kind in {"draft", "quarantine"} and cache_control is not None:
        raise StorageKeyError(operation, object_key, "private objects cannot set public caching")

    headers = {"Content-Type": content_type}
    if cache_control is not None:
        headers["Cache-Control"] = cache_control
    if source_etag is not None:
        if not source_etag or any(ord(char) < 32 for char in source_etag):
            raise StorageKeyError(operation, object_key, "invalid source etag")
        headers["x-oss-copy-source-if-match"] = source_etag
    if kind == "candidate" and cache_control != "no-store":
        raise StorageKeyError(
            operation, object_key, "candidate pointers require no-store caching"
        )
    if kind in {"media", "release", "candidate"}:
        headers["x-oss-forbid-overwrite"] = "true"
    if operation == "copy":
        headers["x-oss-metadata-directive"] = "REPLACE"
    for name, value in (metadata or {}).items():
        normalized = name.lower()
        if not _METADATA_KEY.fullmatch(normalized) or any(ord(c) < 32 for c in value):
            raise StorageKeyError(operation, object_key, "invalid object metadata")
        headers[f"x-oss-meta-{normalized}"] = value
    headers["x-oss-object-acl"] = acl
    return headers


def _header_value(headers: Mapping[str, object], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return str(value)
    return None


def _request_id(error: Exception) -> str | None:
    value = getattr(error, "request_id", None)
    return str(value) if value else None


def _translate_error(
    error: Exception,
    *,
    operation: str,
    object_key: str,
    default: type[StorageError],
) -> StorageError:
    raw_status = getattr(error, "status", None)
    try:
        status = int(raw_status) if raw_status is not None else None
    except (TypeError, ValueError):
        status = None
    raw_code = getattr(error, "code", None)
    code = str(raw_code) if raw_code not in {None, ""} else None
    is_timeout = (
        status in {408, 504}
        or (code is not None and "timeout" in code.lower())
        or error.__class__.__name__ == "RequestError"
    )
    common = {
        "operation": operation,
        "object_key": object_key,
        "message": f"content storage {operation} failed",
        "request_id": _request_id(error),
        "status": status,
        "provider_code": code,
        "retryable": bool(
            is_timeout
            or status == 429
            or (status is not None and 500 <= status <= 599)
        ),
    }
    if status == 404 or code in {"NoSuchKey", "NoSuchObject"}:
        return StorageNotFoundError(**common)
    if status in {401, 403} or code in {"AccessDenied", "InvalidAccessKeyId"}:
        return StorageAccessDeniedError(**common)
    if status in {409, 412} or code in {"ObjectAlreadyExists", "PreconditionFailed"}:
        return StorageConflictError(**common)
    if is_timeout:
        return StorageTimeoutError(**common)
    return default(**common)


class AliyunContentStorage:
    """Aliyun OSS implementation with strict operation and key semantics."""

    def __init__(self, *, bucket: object, bucket_name: str) -> None:
        if bucket is None or not bucket_name:
            raise StorageNotConfiguredError(
                "configure", None, "content OSS bucket is not configured"
            )
        self._bucket = bucket
        self._bucket_name = bucket_name

    @classmethod
    def from_credentials(
        cls,
        *,
        access_key_id: str,
        access_key_secret: str,
        endpoint: str,
        bucket_name: str,
        connect_timeout: int = 20,
    ) -> "AliyunContentStorage":
        if not all((access_key_id, access_key_secret, endpoint, bucket_name)):
            raise StorageNotConfiguredError(
                "configure", None, "content OSS credentials are incomplete"
            )
        try:
            oss2 = import_module("oss2")
        except (ImportError, ModuleNotFoundError) as error:
            raise StorageNotConfiguredError(
                "configure", None, "Aliyun OSS SDK is unavailable"
            ) from error
        try:
            auth = oss2.Auth(access_key_id, access_key_secret)
            bucket = oss2.Bucket(
                auth, endpoint, bucket_name, connect_timeout=connect_timeout
            )
        except Exception as error:
            raise StorageNotConfiguredError(
                "configure", None, "Aliyun content OSS initialization failed"
            ) from error
        return cls(bucket=bucket, bucket_name=bucket_name)

    @classmethod
    def from_settings(cls, settings: object) -> "AliyunContentStorage":
        """Build only from dedicated content credentials; never use legacy OSS."""
        endpoint = str(
            getattr(settings, "MINIAPP_CONTENT_OSS_UPLOAD_ENDPOINT", "")
            or getattr(settings, "MINIAPP_CONTENT_OSS_ENDPOINT", "")
        )
        return cls.from_credentials(
            access_key_id=str(
                getattr(settings, "MINIAPP_CONTENT_OSS_ACCESS_KEY_ID", "")
            ),
            access_key_secret=str(
                getattr(settings, "MINIAPP_CONTENT_OSS_ACCESS_KEY_SECRET", "")
            ),
            endpoint=endpoint,
            bucket_name=str(getattr(settings, "MINIAPP_CONTENT_OSS_BUCKET", "")),
        )

    def put(
        self,
        object_key: str,
        content: bytes,
        *,
        content_type: str,
        acl: str,
        cache_control: str | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        kind = _key_kind(object_key, "put")
        headers = _validated_headers(
            operation="put",
            object_key=object_key,
            kind=kind,
            content_type=content_type,
            acl=acl,
            cache_control=cache_control,
            metadata=metadata,
        )
        try:
            self._bucket.put_object(object_key, content, headers=headers)
        except Exception as error:
            raise _translate_error(
                error,
                operation="put",
                object_key=object_key,
                default=StorageWriteError,
            ) from error

    def head(self, object_key: str) -> ObjectHead:
        _key_kind(object_key, "head")
        try:
            result = self._bucket.head_object(object_key)
            acl_result = self._bucket.get_object_acl(object_key)
            headers = getattr(result, "headers", {}) or {}
            metadata = {
                key[len("x-oss-meta-") :].lower(): str(value)
                for key, value in headers.items()
                if key.lower().startswith("x-oss-meta-")
            }
            return ObjectHead(
                object_key=object_key,
                byte_size=int(getattr(result, "content_length")),
                content_type=str(getattr(result, "content_type")),
                etag=str(getattr(result, "etag")),
                acl=str(getattr(acl_result, "acl")),
                cache_control=_header_value(headers, "Cache-Control"),
                metadata=metadata,
            )
        except Exception as error:
            raise _translate_error(
                error,
                operation="head",
                object_key=object_key,
                default=StorageHeadError,
            ) from error

    def read(self, object_key: str, *, chunk_size: int) -> Iterator[bytes]:
        _key_kind(object_key, "read")
        if chunk_size <= 0 or chunk_size > 8 * 1024 * 1024:
            raise StorageKeyError("read", object_key, "invalid read chunk size")
        body = None
        try:
            body = self._bucket.get_object(object_key)
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    break
                yield bytes(chunk)
        except Exception as error:
            raise _translate_error(
                error,
                operation="read",
                object_key=object_key,
                default=StorageReadError,
            ) from error
        finally:
            if body is not None:
                try:
                    body.close()
                except Exception as error:
                    raise _translate_error(
                        error,
                        operation="read",
                        object_key=object_key,
                        default=StorageReadError,
                    ) from error

    def copy(
        self,
        source_key: str,
        target_key: str,
        *,
        content_type: str,
        acl: str,
        cache_control: str | None = None,
        metadata: Mapping[str, str] | None = None,
        source_etag: str | None = None,
    ) -> None:
        _key_kind(source_key, "copy")
        target_kind = _key_kind(target_key, "copy")
        headers = _validated_headers(
            operation="copy",
            object_key=target_key,
            kind=target_kind,
            content_type=content_type,
            acl=acl,
            cache_control=cache_control,
            metadata=metadata,
            source_etag=source_etag,
        )
        try:
            self._bucket.copy_object(
                self._bucket_name, source_key, target_key, headers=headers
            )
        except Exception as error:
            raise _translate_error(
                error,
                operation="copy",
                object_key=target_key,
                default=StorageCopyError,
            ) from error

    def delete(self, object_key: str) -> None:
        kind = _key_kind(object_key, "delete")
        if kind not in {"draft", "quarantine"}:
            raise StorageKeyError(
                "delete", object_key, "public immutable objects cannot be deleted"
            )
        try:
            self._bucket.delete_object(object_key)
        except Exception as error:
            translated = _translate_error(
                error,
                operation="delete",
                object_key=object_key,
                default=StorageDeleteError,
            )
            if isinstance(translated, StorageNotFoundError):
                return
            raise translated from error

    def delete_current_pointer(self, object_key: str) -> None:
        key = _safe_key(object_key, "delete_current_pointer")
        if not _CURRENT_POINTER_KEY.fullmatch(key):
            raise StorageKeyError(
                "delete_current_pointer",
                key,
                "only an exact channel current pointer may be compensated",
            )
        try:
            self._bucket.delete_object(key)
        except Exception as error:
            translated = _translate_error(
                error,
                operation="delete_current_pointer",
                object_key=key,
                default=StorageDeleteError,
            )
            if isinstance(translated, StorageNotFoundError):
                return
            raise translated from error

    def sign(
        self,
        method: str,
        object_key: str,
        *,
        expires_seconds: int,
        content_type: str | None = None,
        acl: str | None = None,
    ) -> str:
        normalized_method = method.upper()
        kind = _key_kind(object_key, "sign")
        if not 1 <= expires_seconds <= 900:
            raise StorageKeyError("sign", object_key, "signature expiry is outside policy")
        if (normalized_method, kind) not in {("PUT", "quarantine"), ("GET", "draft")}:
            raise StorageKeyError("sign", object_key, "method and object key cannot be signed")
        headers: dict[str, str] = {}
        if normalized_method == "PUT":
            if content_type != "video/mp4" or acl != "private":
                raise StorageKeyError(
                    "sign", object_key, "quarantine uploads require private video/mp4"
                )
            headers = {
                "Content-Type": content_type,
                "x-oss-object-acl": acl,
            }
        elif content_type is not None or acl is not None:
            raise StorageKeyError("sign", object_key, "preview signatures do not accept headers")
        try:
            return str(
                self._bucket.sign_url(
                    normalized_method,
                    object_key,
                    expires_seconds,
                    headers=headers or None,
                )
            )
        except Exception as error:
            raise _translate_error(
                error,
                operation="sign",
                object_key=object_key,
                default=StorageSignError,
            ) from error


__all__ = [
    "IMMUTABLE_CACHE_CONTROL",
    "AliyunContentStorage",
    "ContentStorage",
    "ObjectHead",
    "OSSAccessDeniedError",
    "OSSNotConfigured",
    "OSSRequestTimeout",
    "StorageAccessDeniedError",
    "StorageConflictError",
    "StorageCopyError",
    "StorageDeleteError",
    "StorageError",
    "StorageHeadError",
    "StorageKeyError",
    "StorageNotConfiguredError",
    "StorageNotFoundError",
    "StorageReadError",
    "StorageSignError",
    "StorageTimeoutError",
    "StorageWriteError",
]
