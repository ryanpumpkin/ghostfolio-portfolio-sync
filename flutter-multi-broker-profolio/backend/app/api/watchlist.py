"""Watchlist endpoints: manage symbols and retrieve buy/sell signals."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.settings import Settings, get_settings
from app.middleware.auth import AuthenticatedUser, current_user
from app.services.dependencies import get_analyst_service, get_portfolio_snapshot_cache
from app.services.portfolio_cache import PortfolioSnapshotCache
from app.services.watchlist import (
    WatchlistRepository,
    fetch_signals,
    send_digest_email,
)

router = APIRouter(prefix="/watchlist", tags=["watchlist"])


@lru_cache(maxsize=1)
def _get_repo() -> WatchlistRepository:
    return WatchlistRepository()


def get_watchlist_repo() -> WatchlistRepository:
    return _get_repo()


def _safe_analyst_service() -> Any | None:
    """Return the analyst service, or None if its SDK/config is unavailable.
    Lets signal/digest generation prefer the Longbridge feed but fall back to
    the Yahoo scrape without ever 500-ing on installs that lack the analyst."""
    try:
        return get_analyst_service()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class AddSymbolRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=20)


class WatchlistEntryResponse(BaseModel):
    symbol: str
    added_at: str


class SignalResponse(BaseModel):
    symbol: str
    signal: str
    price: float | None
    currency: str
    rsi: float | None
    sma20: float | None
    sma50: float | None
    reason: str
    refreshed_at: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[WatchlistEntryResponse])
async def list_watchlist(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    repo: Annotated[WatchlistRepository, Depends(get_watchlist_repo)],
) -> list[dict[str, Any]]:
    return [
        {"symbol": e.symbol, "added_at": e.added_at.isoformat()}
        for e in await repo.list(user.user_id)
    ]


@router.post("", response_model=WatchlistEntryResponse, status_code=201)
async def add_to_watchlist(
    body: AddSymbolRequest,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    repo: Annotated[WatchlistRepository, Depends(get_watchlist_repo)],
) -> dict[str, Any]:
    entry = await repo.add(user.user_id, body.symbol, email=user.email)
    return {"symbol": entry.symbol, "added_at": entry.added_at.isoformat()}


@router.delete("/{symbol}", status_code=204)
async def remove_from_watchlist(
    symbol: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    repo: Annotated[WatchlistRepository, Depends(get_watchlist_repo)],
) -> None:
    removed = await repo.remove(user.user_id, symbol)
    if not removed:
        raise HTTPException(status_code=404, detail=f"Symbol '{symbol.upper()}' not in watchlist")


@router.get("/signals", response_model=list[SignalResponse])
async def get_signals(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    repo: Annotated[WatchlistRepository, Depends(get_watchlist_repo)],
) -> list[dict[str, Any]]:
    symbols = await repo.symbols(user.user_id)
    signals = await fetch_signals(symbols, analyst=_safe_analyst_service())
    return [
        {
            "symbol": s.symbol,
            "signal": s.signal.value,
            "price": s.price,
            "currency": s.currency,
            "rsi": s.rsi,
            "sma20": s.sma20,
            "sma50": s.sma50,
            "reason": s.reason,
            "refreshed_at": s.refreshed_at.isoformat(),
        }
        for s in signals
    ]


@router.post("/digest", status_code=202)
async def trigger_digest(
    background_tasks: BackgroundTasks,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    repo: Annotated[WatchlistRepository, Depends(get_watchlist_repo)],
    cache: Annotated[PortfolioSnapshotCache, Depends(get_portfolio_snapshot_cache)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Trigger the daily digest email immediately (for testing or manual sends)."""
    if not settings.gmail_app_password or not settings.gmail_from_email:
        raise HTTPException(status_code=503, detail="Gmail not configured (set MBP_GMAIL_FROM_EMAIL and MBP_GMAIL_APP_PASSWORD)")

    recipient = user.email or settings.gmail_digest_recipient
    if not recipient:
        raise HTTPException(status_code=422, detail="No recipient email — user email unknown and MBP_GMAIL_DIGEST_RECIPIENT not set")

    symbols = await repo.symbols(user.user_id)
    if not symbols:
        raise HTTPException(status_code=422, detail="Watchlist is empty")

    async def _send() -> None:
        analyst = _safe_analyst_service()
        signals = await fetch_signals(symbols, analyst=analyst)
        cached = await cache.get(user.user_id)
        snapshot, cached_at = cached if cached else (None, None)
        reports: list[Any] = []
        if analyst is not None and snapshot is not None and snapshot.positions:
            try:
                reports = await analyst.reports_for_positions(snapshot.positions)
            except Exception:  # noqa: BLE001 — analysis is best-effort, never block the digest
                reports = []
        send_digest_email(
            to_email=recipient,
            from_email=settings.gmail_from_email,  # type: ignore[arg-type]
            app_password=settings.gmail_app_password,  # type: ignore[arg-type]
            signals=signals,
            snapshot=snapshot,
            snapshot_cached_at=cached_at,
            analysis_reports=reports,
        )

    background_tasks.add_task(_send)
    return {"status": "queued", "recipient": recipient}
