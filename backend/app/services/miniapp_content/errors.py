"""Stable publisher errors and retry classification."""

from __future__ import annotations

from dataclasses import dataclass

from app.services.miniapp_content.storage import (
    OSSAccessDeniedError,
    OSSNotConfigured,
    OSSRequestTimeout,
    StorageError,
)
from app.services.miniapp_content.media import MediaVerificationError


class MiniappContentError(RuntimeError):
    code = "content_error"
    retryable = False

    def __init__(self, message: str, *, trace_id: str | None = None) -> None:
        self.trace_id = trace_id
        super().__init__(message)


class ContentValidationError(MiniappContentError):
    code = "content_validation_error"


@dataclass(frozen=True)
class PublicationPolicyIssue:
    path: str
    reason: str


class PublicationPolicyError(ContentValidationError):
    code = "publication_policy_error"

    def __init__(self, issues: list[PublicationPolicyIssue]) -> None:
        if not issues:
            raise ValueError("publication policy error requires at least one issue")
        self.issues = tuple(issues)
        super().__init__("guide publication privacy or approval policy failed")


class DraftConflictError(MiniappContentError):
    code = "draft_conflict"


class PublishBusyError(MiniappContentError):
    code = "publish_busy"


class IdempotencyConflictError(MiniappContentError):
    code = "idempotency_conflict"


class PointerSwapError(MiniappContentError):
    code = "pointer_swap_error"


class PublicSmokeTimeout(MiniappContentError):
    code = "public_smoke_timeout"
    retryable = True


class PublicVerificationError(MiniappContentError):
    code = "public_verification_error"


class WorkerLostError(MiniappContentError):
    code = "worker_lost"


RETRY_DELAYS_SECONDS = (1, 3, 9)


def error_code(error: BaseException) -> str:
    if isinstance(error, MiniappContentError):
        return error.code
    if isinstance(error, StorageError):
        return error.__class__.__name__
    return error.__class__.__name__


def retry_delay_for(error: BaseException, retry_count: int) -> int | None:
    """Return the bounded retry delay for exact Task 4 transient classes."""

    if retry_count < 0 or retry_count >= len(RETRY_DELAYS_SECONDS):
        return None
    if isinstance(error, (OSSRequestTimeout, PublicSmokeTimeout)):
        return RETRY_DELAYS_SECONDS[retry_count]
    if isinstance(error, StorageError) and error.status is not None:
        if 500 <= error.status <= 599:
            return RETRY_DELAYS_SECONDS[retry_count]
    return None


__all__ = [
    "ContentValidationError",
    "DraftConflictError",
    "IdempotencyConflictError",
    "MiniappContentError",
    "MediaVerificationError",
    "OSSAccessDeniedError",
    "OSSNotConfigured",
    "OSSRequestTimeout",
    "PointerSwapError",
    "PublicationPolicyError",
    "PublicationPolicyIssue",
    "PublicSmokeTimeout",
    "PublicVerificationError",
    "PublishBusyError",
    "RETRY_DELAYS_SECONDS",
    "WorkerLostError",
    "error_code",
    "retry_delay_for",
]
