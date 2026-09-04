"""Centralized, non-sensitive error responses for content-management routes."""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

from app.services.miniapp_content.errors import (
    ContentValidationError,
    DraftConflictError,
    IdempotencyConflictError,
    MiniappContentError,
    PublicationPolicyError,
    PublishBusyError,
)
from app.services.miniapp_content.media import (
    MediaError,
    MediaStateError,
    MediaValidationError,
    MediaVerificationError,
)
from app.services.miniapp_content.storage import (
    OSSAccessDeniedError,
    OSSNotConfigured,
    OSSRequestTimeout,
    StorageError,
)


def _envelope(code: str, message: str, retryable: bool) -> dict[str, object]:
    return {
        "code": code,
        "message": message,
        "trace_id": uuid4().hex,
        "retryable": retryable,
    }


def install_content_exception_handlers(app: FastAPI) -> None:
    """Register the one content exception mapper used by both API routers."""

    @app.exception_handler(RequestValidationError)
    async def content_request_validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if request.url.path.startswith("/api/v1/miniapp-content/"):
            return JSONResponse(
                status_code=422,
                content=_envelope("content_request_invalid", "content request is invalid", False),
            )
        return await request_validation_exception_handler(request, exc)

    @app.exception_handler(DraftConflictError)
    @app.exception_handler(IdempotencyConflictError)
    @app.exception_handler(PublishBusyError)
    async def content_conflict_handler(
        _request: Request, exc: MiniappContentError
    ) -> JSONResponse:
        return JSONResponse(status_code=409, content=_envelope(exc.code, str(exc), exc.retryable))

    @app.exception_handler(ContentValidationError)
    async def content_validation_handler(
        _request: Request, exc: ContentValidationError
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content=_envelope(exc.code, str(exc), exc.retryable))

    @app.exception_handler(PublicationPolicyError)
    async def publication_policy_handler(
        _request: Request, exc: PublicationPolicyError
    ) -> JSONResponse:
        content = _envelope(exc.code, str(exc), exc.retryable)
        content["issues"] = [
            {"path": issue.path, "reason": issue.reason} for issue in exc.issues
        ]
        return JSONResponse(status_code=422, content=content)

    @app.exception_handler(MiniappContentError)
    async def generic_content_error_handler(
        _request: Request, exc: MiniappContentError
    ) -> JSONResponse:
        return JSONResponse(status_code=500, content=_envelope(exc.code, "content operation failed", exc.retryable))

    @app.exception_handler(MediaVerificationError)
    @app.exception_handler(MediaValidationError)
    @app.exception_handler(MediaStateError)
    async def media_error_handler(_request: Request, exc: MediaError) -> JSONResponse:
        return JSONResponse(status_code=422, content=_envelope("media_validation_error", str(exc), False))

    @app.exception_handler(OSSNotConfigured)
    async def storage_unavailable_handler(
        _request: Request, _exc: OSSNotConfigured
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content=_envelope("content_storage_unavailable", "content storage is unavailable", False))

    @app.exception_handler(OSSAccessDeniedError)
    async def storage_denied_handler(
        _request: Request, _exc: OSSAccessDeniedError
    ) -> JSONResponse:
        return JSONResponse(status_code=502, content=_envelope("content_storage_denied", "content storage request was denied", False))

    @app.exception_handler(OSSRequestTimeout)
    async def storage_timeout_handler(
        _request: Request, _exc: OSSRequestTimeout
    ) -> JSONResponse:
        return JSONResponse(status_code=503, content=_envelope("content_storage_timeout", "content storage timed out", True))

    @app.exception_handler(StorageError)
    async def storage_error_handler(_request: Request, _exc: StorageError) -> JSONResponse:
        return JSONResponse(status_code=502, content=_envelope("content_storage_error", "content storage request failed", False))
