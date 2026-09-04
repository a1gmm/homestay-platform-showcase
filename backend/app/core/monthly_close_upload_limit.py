"""Pre-parser request body protection for public monthly-close intake."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import json
from typing import Any

from app.services.monthly_close.documents import MAX_MONTHLY_CLOSE_DOCUMENT_BYTES


_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
MAX_MONTHLY_CLOSE_INTAKE_BODY_BYTES = (
    MAX_MONTHLY_CLOSE_DOCUMENT_BYTES + _MULTIPART_OVERHEAD_BYTES
)


async def _send_too_large(send: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
    body = json.dumps(
        {
            "detail": {
                "code": "document_too_large",
                "message": "文件超过 10MB",
            }
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class MonthlyCloseIntakeBodyLimitMiddleware:
    """Reject oversized public uploads before Starlette parses multipart data."""

    def __init__(self, app, max_body_bytes: int = MAX_MONTHLY_CLOSE_INTAKE_BODY_BYTES):
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope, receive, send) -> None:
        if not (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and str(scope.get("path", "")).startswith(
                "/api/v1/monthly-close/intake/"
            )
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                if int(content_length) > self.max_body_bytes:
                    await _send_too_large(send)
                    return
            except ValueError:
                await _send_too_large(send)
                return

        received = 0
        overflow = False

        async def limited_receive():
            nonlocal received, overflow
            message = await receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    overflow = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message) -> None:
            if not overflow:
                await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not overflow:
                raise
        if overflow:
            await _send_too_large(send)
