"""Compress large, frequently polled operational reads, never streams or tokens."""

from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send


class OperationalReadCompressionMiddleware:
    # Exact paths deliberately exclude auth, lock codes, downloads and chat SSE.
    # No response caching: every poll still observes the current business state.
    PATHS = frozenset({
        "/api/v1/rooms",
        "/api/v1/rooms/availability/calendar",
        "/api/v1/orders",
        "/api/v1/orders/pending-room",
    })

    def __init__(self, app: ASGIApp):
        self.app = app
        self.compressed = GZipMiddleware(app, minimum_size=1000, compresslevel=3)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] == "http" and scope.get("method") == "GET"
                and scope.get("path") in self.PATHS):
            await self.compressed(scope, receive, send)
        else:
            await self.app(scope, receive, send)
