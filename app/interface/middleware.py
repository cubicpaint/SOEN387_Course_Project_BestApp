"""Attach fresh correlation headers and emit one safe request log."""

import logging
from time import monotonic
from uuid import uuid4

from starlette.datastructures import MutableHeaders

from app.interface.errors import error_response

logger = logging.getLogger("bestapp.http")


class RequestContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        correlation_id = str(uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation_id
        started = monotonic()
        status = 500
        response_started = False

        async def send_with_headers(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                status = message["status"]
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["X-Correlation-ID"] = correlation_id
                headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_headers)
        except Exception as error:
            logger.error("internal_error", extra={
                "event": "internal_error", "correlation_id": correlation_id,
                "exception_type": type(error).__name__,
            })
            if response_started:
                raise
            response = error_response(500, "internal_error", "An unexpected error occurred.", retryable=True)
            await response(scope, receive, send_with_headers)
        finally:
            route = scope.get("route")
            logger.info("http_request", extra={
                "event": "http_request", "correlation_id": correlation_id,
                "method": scope["method"], "path": getattr(route, "path", "[unmatched]"),
                "status_code": status, "duration_ms": round((monotonic() - started) * 1000, 2),
            })
