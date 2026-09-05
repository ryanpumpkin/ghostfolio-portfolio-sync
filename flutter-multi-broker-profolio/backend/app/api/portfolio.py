"""Portfolio aggregation endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, Query

from app.api.credential_context import WrappedCredentialsContext, parse_wrapped_credentials_header
from app.middleware.auth import AuthenticatedUser, current_user
from app.models.domain import CashBalance, PartialResult, PortfolioSnapshot, Position, Transaction
from app.services.aggregator import AggregationCredentialContext, PortfolioAggregator
from app.services.dependencies import get_portfolio_aggregator, get_portfolio_snapshot_cache
from app.services.portfolio_cache import PortfolioSnapshotCache

router = APIRouter(tags=["portfolio"])


@router.get("/portfolio", response_model=PortfolioSnapshot)
async def get_portfolio(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    aggregator: Annotated[PortfolioAggregator, Depends(get_portfolio_aggregator)],
    wrapped_creds: Annotated[WrappedCredentialsContext, Depends(parse_wrapped_credentials_header)],
    cache: Annotated[PortfolioSnapshotCache, Depends(get_portfolio_snapshot_cache)],
    background_tasks: BackgroundTasks,
    base_currency: Annotated[str, Query(min_length=3, max_length=3)] = "USD",
) -> PortfolioSnapshot:
    snapshot = await aggregator.get_snapshot(
        user.user_id,
        base_currency=base_currency,
        credential_context=_to_aggregation_credential_context(wrapped_creds),
    )
    # Cache the derived snapshot (no credentials) so the background daily-digest
    # worker can render positions without the client's E2E unwrap key. Only
    # cache results that actually carry data, so a transient all-sources-down
    # response never clobbers a good cache. Written off the response path.
    if snapshot.positions or snapshot.balances:
        background_tasks.add_task(cache.put, user.user_id, snapshot)
    return snapshot


@router.get("/positions", response_model=PartialResult[Position])
async def get_positions(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    aggregator: Annotated[PortfolioAggregator, Depends(get_portfolio_aggregator)],
    wrapped_creds: Annotated[WrappedCredentialsContext, Depends(parse_wrapped_credentials_header)],
    source: str | None = None,
) -> PartialResult[Position]:
    return await aggregator.get_positions(
        user.user_id,
        source=source,
        credential_context=_to_aggregation_credential_context(wrapped_creds),
    )


@router.get("/balances", response_model=PartialResult[CashBalance])
async def get_balances(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    aggregator: Annotated[PortfolioAggregator, Depends(get_portfolio_aggregator)],
    wrapped_creds: Annotated[WrappedCredentialsContext, Depends(parse_wrapped_credentials_header)],
    source: str | None = None,
) -> PartialResult[CashBalance]:
    return await aggregator.get_balances(
        user.user_id,
        source=source,
        credential_context=_to_aggregation_credential_context(wrapped_creds),
    )


@router.get("/transactions", response_model=PartialResult[Transaction])
async def get_transactions(
    user: Annotated[AuthenticatedUser, Depends(current_user)],
    aggregator: Annotated[PortfolioAggregator, Depends(get_portfolio_aggregator)],
    wrapped_creds: Annotated[WrappedCredentialsContext, Depends(parse_wrapped_credentials_header)],
    source: str | None = None,
    since: str | None = None,
    limit: Annotated[int | None, Query(ge=1, le=1000)] = None,
) -> PartialResult[Transaction]:
    return await aggregator.get_transactions(
        user.user_id,
        source=source,
        since=since,
        limit=limit,
        credential_context=_to_aggregation_credential_context(wrapped_creds),
    )


def _to_aggregation_credential_context(
    wrapped_creds: WrappedCredentialsContext,
) -> AggregationCredentialContext:
    return AggregationCredentialContext(
        wrapped_tokens_by_connection=dict(wrapped_creds.tokens_by_connection),
        shared_wrapped_token=wrapped_creds.shared_token,
        unwrap_key=wrapped_creds.key_bytes(),
    )
