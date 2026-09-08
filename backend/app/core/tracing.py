"""Request-tracing helpers (correlation IDs propagated via contextvars)."""

from __future__ import annotations

import uuid
from contextvars import ContextVar

REQUEST_ID_HEADER = "X-Request-ID"

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)


def current_request_id() -> str | None:
    """Return the request ID for the active context, if any."""
    return _request_id_var.get()


def set_request_id(value: str) -> None:
    """Set the request ID for the active context."""
    _request_id_var.set(value)


def new_request_id() -> str:
    """Generate a fresh request ID."""
    return uuid.uuid4().hex
