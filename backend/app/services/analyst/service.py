"""Analyst service orchestrator.

Owns the per-symbol pipeline:

    fetch quote + candles + capital + indexes + institution rating
        → run scoring engine
        → return AnalystReport

and the in-memory cache that fronts that pipeline so 50 users asking
about the same symbol on the same day cost us one broker call total.

Cache key is `(symbol, base_currency_native_only)` — the report
doesn't include any user-specific data (avg_cost is bound from the
caller's position when they fetch their own report), so cross-user
sharing is safe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.models.analyst import AnalystReportApi
from app.models.domain import Position
from app.services.analyst import longbridge_data
from app.services.analyst.longbridge_data import (
    Candle,
    CapitalDistribution,
    CalcIndexes,
    MarketDataUnavailable,
    QuoteSnapshot,
)
from app.services.analyst.scoring import AnalystInputs, build_report
from app.services.analyst.symbol_map import position_to_longbridge_symbol


_LOG = logging.getLogger("mbp.analyst")


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _CachedFetch:
    """Cached raw fetch (before scoring). Avg_cost is applied per-call
    because it depends on the caller's position, not the symbol."""

    quote: QuoteSnapshot | None
    candles: list[Candle]
    capital: CapitalDistribution | None
    indexes: CalcIndexes | None
    analyst_target: float | None
    analyst_recommendation: str | None
    expires_at: float


class AnalystService:
    """Produces per-position AnalystReport objects.

    Construct once at app startup (see `app.services.dependencies`) and
    inject as a FastAPI dependency.
    """

    def __init__(self, *, cache_ttl_seconds: float = 3600.0) -> None:
        # 1 hour default — market data is "fresh enough" for an analyst
        # report at that cadence, well under the broker rate-limit budget.
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, _CachedFetch] = {}
        self._cache_lock = asyncio.Lock()

    # ----- public API ----------------------------------------------------

    async def report_for_position(self, position: Position) -> AnalystReportApi | None:
        """Build a report for a single user position. Returns None when
        the symbol can't be mapped to a Longbridge symbol.

        Raises `MarketDataUnavailable` if the service-account creds
        aren't configured (the caller turns that into a 503).
        """
        symbol = position_to_longbridge_symbol(position)
        if not symbol:
            _LOG.debug(
                "analyst skip — no LB symbol for %s/%s/%s",
                position.source,
                position.symbol,
                position.exchange,
            )
            return None
        fetch = await self._fetch_or_get(symbol)
        avg_cost = float(position.avg_cost) if position.avg_cost is not None else None
        qty = float(position.quantity) if position.quantity is not None else None
        internal = build_report(
            AnalystInputs(
                symbol=symbol,
                quote=fetch.quote,
                candles=fetch.candles,
                capital=fetch.capital,
                indexes=fetch.indexes,
                avg_cost=avg_cost,
                quantity=qty,
            ),
            analyst_target=fetch.analyst_target,
            analyst_recommendation=fetch.analyst_recommendation,
        )
        return AnalystReportApi.from_internal(internal)

    async def daily_closes_for_position(self, position: Position) -> list[float] | None:
        """Return the daily close series (oldest first) for a position's symbol,
        or None when the symbol can't be mapped to a Longbridge symbol.

        Reuses the same 1-hour fetch cache as `report_for_position`, so building
        a watchlist signal for a symbol the user also holds costs no extra broker
        call. Raises `MarketDataUnavailable` if the service-account creds aren't
        configured (the caller falls back to the Yahoo scrape)."""
        symbol = position_to_longbridge_symbol(position)
        if not symbol:
            return None
        fetch = await self._fetch_or_get(symbol)
        return [c.close for c in fetch.candles]

    async def reports_for_positions(
        self, positions: Iterable[Position]
    ) -> list[AnalystReportApi]:
        """Fan-out wrapper. Skips positions whose symbols don't map; on
        per-symbol fetch failure, that symbol is omitted from the result
        rather than failing the whole batch."""
        positions_list = list(positions)
        if not positions_list:
            return []
        results = await asyncio.gather(
            *(self.report_for_position(p) for p in positions_list),
            return_exceptions=True,
        )
        out: list[AnalystReportApi] = []
        for pos, r in zip(positions_list, results, strict=True):
            if isinstance(r, MarketDataUnavailable):
                # Re-raise — global config issue, not per-symbol. The
                # caller will surface this as a 503 once and stop.
                raise r
            if isinstance(r, BaseException):
                _LOG.warning(
                    "analyst report failed for %s: %s", pos.symbol, r
                )
                continue
            if r is None:
                continue
            out.append(r)
        return out

    # ----- fetch + cache ------------------------------------------------

    async def _fetch_or_get(self, symbol: str) -> _CachedFetch:
        async with self._cache_lock:
            entry = self._cache.get(symbol)
            now = time.monotonic()
            if entry is not None and entry.expires_at > now:
                return entry

        # Fetch outside the lock — long network calls shouldn't serialize
        # other symbols' work.
        fetch = await self._fetch_fresh(symbol)
        async with self._cache_lock:
            # Replace under lock so two concurrent misses for the same
            # symbol don't both upsert (both fetched, but the last write
            # wins which is fine for a 1h-stale cache).
            self._cache[symbol] = fetch
        return fetch

    async def _fetch_fresh(self, symbol: str) -> _CachedFetch:
        # Run quote / candles / capital / indexes / rating in parallel.
        quote_task = longbridge_data.fetch_quotes([symbol])
        candles_task = longbridge_data.fetch_daily_candles(symbol, count=220)
        capital_task = longbridge_data.fetch_capital_distribution(symbol)
        indexes_task = longbridge_data.fetch_calc_indexes(symbol)
        rating_task = _fetch_institution_rating_safe(symbol)

        results = await asyncio.gather(
            quote_task, candles_task, capital_task, indexes_task, rating_task,
            return_exceptions=True,
        )
        quote_result, candles_result, capital_result, indexes_result, rating_result = results

        # If ANY single fetch raised MarketDataUnavailable, the whole
        # service is unconfigured — propagate immediately so the caller
        # can return a 503 cleanly.
        for r in results:
            if isinstance(r, MarketDataUnavailable):
                raise r

        def _value(v: Any) -> Any:
            return v if not isinstance(v, BaseException) else None

        quotes_list = _value(quote_result) or []
        quote = quotes_list[0] if quotes_list else None
        candles = _value(candles_result) or []
        capital = _value(capital_result)
        indexes = _value(indexes_result)
        rating_tuple = _value(rating_result) or (None, None)
        analyst_target, analyst_recommendation = rating_tuple

        return _CachedFetch(
            quote=quote,
            candles=candles,
            capital=capital,
            indexes=indexes,
            analyst_target=analyst_target,
            analyst_recommendation=analyst_recommendation,
            expires_at=time.monotonic() + self._cache_ttl,
        )

    # ----- maintenance ------------------------------------------------

    def reset_for_tests(self) -> None:
        self._cache.clear()


# ---------------------------------------------------------------------------
# Institution rating fetcher (kept here, not in longbridge_data, because
# it's a thin wrapper over the SDK and we don't need it as a public DTO).
# ---------------------------------------------------------------------------


async def _fetch_institution_rating_safe(
    symbol: str,
) -> tuple[float | None, str | None]:
    """Return `(target_price, recommendation)` or `(None, None)` if not
    available. Errors are swallowed — institution rating is the one
    signal we can do without entirely."""
    try:
        return await _fetch_institution_rating(symbol)
    except MarketDataUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        _LOG.debug("institution rating fetch failed for %s: %s", symbol, exc)
        return None, None


async def _fetch_institution_rating(
    symbol: str,
) -> tuple[float | None, str | None]:
    # The SDK doesn't expose a top-level `institution_rating` method on
    # QuoteContext in all versions — some use `participants` or only
    # ship it via the higher-level REST surface. We probe at runtime
    # and gracefully no-op if the method is missing. This keeps the
    # analyst running even when the SDK lags the OpenAPI surface.
    ctx = longbridge_data._ensure_quote_context()  # noqa: SLF001 — internal helper
    fn = getattr(ctx, "institution_rating", None) or getattr(ctx, "instrating", None)
    if fn is None:
        return None, None
    raw = await asyncio.to_thread(fn, symbol)
    target = _to_float(getattr(raw, "target_price", None))
    recommendation = getattr(raw, "recommend", None) or getattr(raw, "recommendation", None)
    if recommendation is not None:
        recommendation = str(recommendation).lower()
    return target, recommendation


def _to_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None
