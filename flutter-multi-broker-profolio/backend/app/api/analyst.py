"""Analyst report endpoints.

The analyst service depends on a dedicated Longbridge service-account
(env vars `MBP_LB_ANALYST_*`). When those aren't configured we
deliberately return **503 Service Unavailable** with a clear hint
rather than 500'ing — the Flutter client uses this signal to hide the
Analysis tab gracefully on installations that haven't enabled it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.credential_context import (
    WrappedCredentialsContext,
    parse_wrapped_credentials_header,
)
from app.middleware.auth import AuthenticatedUser, current_user
from app.models.analyst import AnalystReportApi
from app.models.domain import Position
from app.services.aggregator import (
    AggregationCredentialContext,
    PortfolioAggregator,
)
from app.services.analyst.longbridge_data import MarketDataUnavailable
from app.services.analyst.service import AnalystService
from app.services.dependencies import (
    get_analyst_service,
    get_portfolio_aggregator,
)


router = APIRouter(tags=["analyst"], prefix="/analyst")


@router.get(
    "",
    response_model=list[AnalystReportApi],
    summary="Analyst reports for every position the user holds",
)
async def list_analyst_reports(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    analyst: Annotated[AnalystService, Depends(get_analyst_service)],
    aggregator: Annotated[
        PortfolioAggregator, Depends(get_portfolio_aggregator)
    ],
    wrapped_creds: Annotated[
        WrappedCredentialsContext, Depends(parse_wrapped_credentials_header)
    ],
) -> list[AnalystReportApi]:
    positions = await _user_positions(user, aggregator, wrapped_creds)
    try:
        return await analyst.reports_for_positions(positions)
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get(
    "/{symbol}",
    response_model=AnalystReportApi,
    summary="Analyst report for a single symbol from the user's positions",
)
async def get_analyst_report(
    symbol: str,
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    analyst: Annotated[AnalystService, Depends(get_analyst_service)],
    aggregator: Annotated[
        PortfolioAggregator, Depends(get_portfolio_aggregator)
    ],
    wrapped_creds: Annotated[
        WrappedCredentialsContext, Depends(parse_wrapped_credentials_header)
    ],
    source: Annotated[
        str | None,
        Query(description="Optional source filter; only positions from this "
              "broker are considered when resolving the symbol."),
    ] = None,
) -> AnalystReportApi:
    """Resolve the user's matching position(s) for `symbol` and build a
    report. The symbol may be in any form the user's broker reports it
    (e.g. `AAPL`, `US.VOO`, `823.HK`, `TSLA.US`) — the symbol mapper
    normalises it. If the symbol isn't in the user's positions we
    still try to map it as a raw ticker and return a report without
    cost-basis context."""
    positions = await _user_positions(user, aggregator, wrapped_creds)
    target = symbol.strip().upper()

    matches = [
        p for p in positions
        if (source is None or p.source.lower() == source.lower())
        and _matches_symbol(p, target)
    ]
    if matches:
        # Prefer the position with the largest quantity if the user
        # somehow holds the same symbol across multiple connections.
        pick = max(matches, key=lambda p: float(p.quantity or 0))
        try:
            report = await analyst.report_for_position(pick)
        except MarketDataUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if report is None:
            raise HTTPException(
                status_code=400,
                detail=f"Could not map symbol '{symbol}' to a Longbridge feed.",
            )
        return report

    # No matching position. Build a synthetic Position so the user can
    # still query analyst data on watchlist-style tickers.
    synthetic = Position(
        source="unknown",
        symbol=target,
        currency="USD",
        quantity=0,  # type: ignore[arg-type] — Decimal coercion handled by model
    )
    try:
        report = await analyst.report_for_position(synthetic)
    except MarketDataUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if report is None:
        raise HTTPException(
            status_code=400,
            detail=f"Could not map symbol '{symbol}' to a Longbridge feed.",
        )
    return report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _matches_symbol(position: Position, target: str) -> bool:
    """Loose match: the target string appears in the position's raw
    symbol (case-insensitive). Catches users typing `TSLA` and matching
    a position whose raw symbol is `TSLA.US`, `US.TSLA`, etc."""
    if not position.symbol:
        return False
    return target in position.symbol.upper()


async def _user_positions(
    user: AuthenticatedUser,
    aggregator: PortfolioAggregator,
    wrapped_creds: WrappedCredentialsContext,
) -> list[Position]:
    credentials = AggregationCredentialContext(
        wrapped_tokens_by_connection=dict(wrapped_creds.tokens_by_connection),
        shared_wrapped_token=wrapped_creds.shared_token,
        unwrap_key=wrapped_creds.key_bytes(),
    )
    result = await aggregator.get_positions(
        user.user_id,
        credential_context=credentials,
    )
    return list(result.items)
