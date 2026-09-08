"""Operational endpoints: /healthz and /metrics.

These live at the app root (not under /v1) so probes work without versioning.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app import __version__

router = APIRouter(tags=["ops"])

_START_TIME = time.monotonic()


def _uptime_seconds() -> float:
    return round(time.monotonic() - _START_TIME, 3)


def get_adapter_healths() -> list[dict[str, Any]]:
    """Return per-adapter health summaries.

    Empty until backend-adapters lands; callers must tolerate an empty list.
    """
    return []


@router.get("/healthz", summary="Liveness + readiness probe")
def healthz() -> dict[str, Any]:
    """Liveness + readiness combined."""
    return {
        "status": "ok",
        "version": __version__,
        "uptime_seconds": _uptime_seconds(),
        "adapters": get_adapter_healths(),
    }


@router.get("/metrics", summary="Prometheus metrics")
def metrics() -> Response:
    """Expose the default Prometheus collector registry."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
