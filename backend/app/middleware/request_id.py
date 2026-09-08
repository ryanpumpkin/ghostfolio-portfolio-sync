"""Request-ID middleware: propagates a correlation header in and out."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.tracing import REQUEST_ID_HEADER, new_request_id, set_request_id


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Read X-Request-ID from inbound request (or generate one), echo on response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        rid = request.headers.get(REQUEST_ID_HEADER) or new_request_id()
        set_request_id(rid)
        request.state.request_id = rid
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = rid
        return response
