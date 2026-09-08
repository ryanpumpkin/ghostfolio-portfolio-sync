"""Access-logging middleware emitting one JSON line per request."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.logging import get_logger

_log = get_logger("http")


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log method, path, status, and duration for each HTTP request."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            _log.info(
                "http_request",
                method=request.method,
                path=request.url.path,
                status=status,
                duration_ms=round(duration_ms, 2),
            )
