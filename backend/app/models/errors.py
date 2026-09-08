"""Uniform error envelope returned by the global exception handler."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ErrorEnvelope(BaseModel):
    """Uniform JSON error body for all 4xx/5xx responses."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(description="Machine-readable error code (e.g. http_404).")
    message: str = Field(description="Human-readable summary.")
    request_id: str | None = Field(default=None, description="Correlation ID for log lookup.")
    details: dict[str, Any] | None = None
