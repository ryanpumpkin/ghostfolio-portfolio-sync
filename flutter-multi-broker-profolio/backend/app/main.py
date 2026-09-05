"""FastAPI app factory and module-level instance."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import __version__
from app.api import api_router
from app.api.ops import router as ops_router
from app.core.logging import configure_logging, get_logger
from app.core.settings import Settings, get_settings
from app.middleware.logging import AccessLogMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.models.errors import ErrorEnvelope
from app.workers.daily_digest import DailyDigestWorker


def _envelope(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    rid = getattr(request.state, "request_id", None)
    body = ErrorEnvelope(code=code, message=message, request_id=rid, details=details)
    return JSONResponse(status_code=status_code, content=body.model_dump(mode="json"))


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start/stop background workers alongside the app.

    Launches the daily watchlist digest worker. ``manage_signals=False`` so it
    does not steal SIGINT/SIGTERM from uvicorn's graceful shutdown.
    """
    log = get_logger(__name__)
    worker = DailyDigestWorker(
        digest_hour_utc=int(os.getenv("MBP_DIGEST_HOUR_UTC", "8")),
        manage_signals=False,
    )
    task = asyncio.create_task(worker.run(), name="daily-digest-worker")
    try:
        yield
    finally:
        worker.request_shutdown()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except (TimeoutError, asyncio.CancelledError):
            task.cancel()
        except Exception as exc:  # noqa: BLE001
            log.warning("daily_digest_worker_shutdown_error", error=str(exc))


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a configured FastAPI application.

    Accepting a settings override lets tests construct an app without
    relying on the cached global instance.
    """
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    log = get_logger(__name__)

    app = FastAPI(
        title="Multi-Broker Portfolio Backend",
        version=__version__,
        description="Proxy service for broker / exchange APIs.",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID"],
    )
    app.add_middleware(AccessLogMiddleware)
    # RequestIdMiddleware added LAST so it runs FIRST (Starlette stacks LIFO).
    app.add_middleware(RequestIdMiddleware)

    # Make the create_app-supplied settings the value resolved by Depends(get_settings).
    captured_settings = settings
    app.dependency_overrides[get_settings] = lambda: captured_settings

    app.include_router(ops_router)
    app.include_router(api_router)

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _envelope(
            request,
            status_code=exc.status_code,
            code=f"http_{exc.status_code}",
            message=str(exc.detail) if exc.detail else "HTTP error",
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_exc(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _envelope(
            request,
            status_code=422,
            code="validation_error",
            message="Request validation failed",
            details={"errors": exc.errors()},
        )

    @app.exception_handler(Exception)
    async def _unhandled_exc(request: Request, exc: Exception) -> JSONResponse:
        log.error("unhandled_exception", error=str(exc), exc_info=True)
        return _envelope(
            request,
            status_code=500,
            code="internal_error",
            message="Internal server error",
        )

    # Silence unused-handler warnings from strict linters.
    _ = (_http_exc, _validation_exc, _unhandled_exc, HTTPException)

    return app


app = create_app()
