"""FX service with pluggable providers and layered caching."""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol

import httpx

from app.models.domain import FxRate


class FxRateUnavailableError(RuntimeError):
    """Raised when a rate cannot be resolved from provider(s)."""


class FxProvider(Protocol):
    """Provider interface for direct FX pair fetches."""

    async def fetch_rate(self, base: str, quote: str) -> FxRate | None: ...


class FxCacheStore(Protocol):
    """External cache interface (Firestore-backed in production)."""

    async def get_rate(self, base: str, quote: str) -> FxRate | None: ...

    async def set_rate(self, rate: FxRate, *, ttl_seconds: float) -> None: ...


class NullFxCacheStore:
    """No-op cache store used until Firestore wiring lands."""

    async def get_rate(self, base: str, quote: str) -> FxRate | None:
        _ = (base, quote)
        return None

    async def set_rate(self, rate: FxRate, *, ttl_seconds: float) -> None:
        _ = (rate, ttl_seconds)


@dataclass(slots=True)
class _CacheEntry:
    rate: FxRate
    expires_at: float


class InMemoryFxCacheStore:
    """Simple TTL cache store matching the Firestore store interface."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], _CacheEntry] = {}

    async def get_rate(self, base: str, quote: str) -> FxRate | None:
        key = (base.upper(), quote.upper())
        item = self._items.get(key)
        if item is None:
            return None
        if item.expires_at <= time.monotonic():
            self._items.pop(key, None)
            return None
        return item.rate

    async def set_rate(self, rate: FxRate, *, ttl_seconds: float) -> None:
        key = (rate.base.upper(), rate.quote.upper())
        self._items[key] = _CacheEntry(rate=rate, expires_at=time.monotonic() + ttl_seconds)


class FrankfurterProvider:
    """ECB-derived FX rates via frankfurter.dev. No API key required.

    Endpoint shape: GET /latest?from=USD&to=HKD returns
    `{"amount": 1, "base": "USD", "date": "...", "rates": {"HKD": 7.79}}`.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str = "https://api.frankfurter.dev/v1",
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._base_url = base_url.rstrip("/")

    async def fetch_rate_on(
        self, base: str, quote: str, on: date
    ) -> FxRate | None:
        """The rate published for a past date.

        Frankfurter serves `/YYYY-MM-DD` with the same shape as
        `/latest`, and answers with the most recent publication at or
        before that date — so weekends and holidays resolve to the
        preceding business day rather than failing.

        Needed because a cost basis is a historical fact: converting a
        2024 trade at today's rate silently reprices it.
        """
        return await self._fetch(on.isoformat(), base, quote)

    async def fetch_rate(self, base: str, quote: str) -> FxRate | None:
        return await self._fetch("latest", base, quote)

    async def _fetch(self, path: str, base: str, quote: str) -> FxRate | None:
        response = await self._client.get(
            f"{self._base_url}/{path}",
            params={"base": base, "symbols": quote},
        )
        # Frankfurter returns 404 for unsupported currencies (e.g. CNH,
        # the offshore yuan that IBKR uses for HK-traded forex). Treat
        # that as "rate unavailable" so the caller can try the reverse
        # pair, triangulate via USD, or — as a last resort — surface a
        # clean `FxRateUnavailableError` that `get_rates_for` is already
        # written to swallow. Anything else (5xx, network, etc.) keeps
        # propagating so genuine outages aren't masked.
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
        rates = payload.get("rates", {}) if isinstance(payload, dict) else {}
        value = rates.get(quote)
        if value is None:
            return None
        # `as_of` must carry the date the rate was PUBLISHED, not the
        # moment we asked. Stamping every rate with `now` made a
        # historical lookup indistinguishable from a current one, so a
        # caller converting a 2024 trade could not tell whether it got
        # the 2024 rate or today's — and had to assume the worse.
        #
        # Frankfurter echoes the effective date, which for a weekend or
        # holiday is the preceding business day.
        published = payload.get("date") if isinstance(payload, dict) else None
        as_of = datetime.now(UTC)
        if isinstance(published, str):
            try:
                as_of = datetime.fromisoformat(published).replace(tzinfo=UTC)
            except ValueError:
                pass
        return FxRate(
            base=base,
            quote=quote,
            rate=Decimal(str(value)),
            as_of=as_of,
        )


class ExchangerateHostProvider:
    """Provider backed by exchangerate.host. Requires an API key as of
    late 2024 — the free no-key tier was retired."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str = "https://api.exchangerate.host",
        api_key: str | None = None,
    ) -> None:
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key

    async def fetch_rate(self, base: str, quote: str) -> FxRate | None:
        params: dict[str, str] = {"from": base, "to": quote}
        if self._api_key:
            params["access_key"] = self._api_key
        response = await self._client.get(f"{self._base_url}/convert", params=params)
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result")
        if result is None:
            return None
        return FxRate(
            base=base,
            quote=quote,
            rate=Decimal(str(result)),
            as_of=datetime.now(UTC),
        )


class OpenExchangeRatesProvider:
    """Alternative provider backed by OpenExchangeRates."""

    def __init__(
        self,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str = "https://openexchangerates.org/api",
    ) -> None:
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(timeout=10.0)
        self._base_url = base_url.rstrip("/")

    async def fetch_rate(self, base: str, quote: str) -> FxRate | None:
        params = {"app_id": self._api_key, "symbols": f"{base},{quote}"}
        response = await self._client.get(f"{self._base_url}/latest.json", params=params)
        response.raise_for_status()
        payload = response.json()
        rates_raw = payload.get("rates")
        if not isinstance(rates_raw, dict):
            return None
        rates = {str(k).upper(): Decimal(str(v)) for k, v in rates_raw.items()}
        base_u = base.upper()
        quote_u = quote.upper()

        if quote_u == "USD":
            base_rate = rates.get(base_u)
            if base_rate is None or base_rate == 0:
                return None
            value = Decimal("1") / base_rate
        elif base_u == "USD":
            quote_rate = rates.get(quote_u)
            if quote_rate is None:
                return None
            value = quote_rate
        else:
            base_rate = rates.get(base_u)
            quote_rate = rates.get(quote_u)
            if base_rate is None or quote_rate is None or base_rate == 0:
                return None
            value = quote_rate / base_rate

        ts = payload.get("timestamp")
        as_of = datetime.now(UTC)
        if isinstance(ts, int):
            as_of = datetime.fromtimestamp(ts, tz=UTC)

        return FxRate(base=base_u, quote=quote_u, rate=value, as_of=as_of)


class FxService:
    """Fetches FX rates with in-process + external cache and triangulation."""

    def __init__(
        self,
        provider: FxProvider,
        *,
        firestore_cache: FxCacheStore | None = None,
        ttl_seconds: float = 300.0,
    ) -> None:
        self._provider = provider
        self._firestore_cache = firestore_cache or NullFxCacheStore()
        self._ttl_seconds = ttl_seconds
        self._memory: dict[tuple[str, str], _CacheEntry] = {}
        #: Historical rates never change, so they are cached for the
        #: life of the process with no TTL.
        self._historic: dict[tuple[str, str, str], FxRate] = {}

    async def get_rate_on(self, base: str, quote: str, on: date) -> FxRate:
        """The rate for a pair as at a past date.

        Falls back to the current rate when the provider cannot serve
        history — and says so through `as_of`, which then carries today
        rather than the date asked for. A caller that needs to know
        whether it got real history can compare the two; nothing here
        pretends an approximation is a fact.
        """
        base_u, quote_u = base.upper(), quote.upper()
        if base_u == quote_u:
            return FxRate(
                base=base_u, quote=quote_u, rate=Decimal("1"),
                as_of=datetime(on.year, on.month, on.day, tzinfo=UTC),
            )
        key = (base_u, quote_u, on.isoformat())
        if (hit := self._historic.get(key)) is not None:
            return hit

        fetch_on = getattr(self._provider, "fetch_rate_on", None)
        rate: FxRate | None = None
        if callable(fetch_on):
            rate = await fetch_on(base_u, quote_u, on)
            if rate is None:
                reverse = await fetch_on(quote_u, base_u, on)
                if reverse is not None and reverse.rate != 0:
                    rate = FxRate(
                        base=base_u, quote=quote_u,
                        rate=Decimal("1") / reverse.rate, as_of=reverse.as_of,
                    )
        if rate is None:
            rate = await self.get_rate(base_u, quote_u)
        self._historic[key] = rate
        return rate

    async def get_rate(self, base: str, quote: str) -> FxRate:
        """Return the FX rate for one pair, triangulating via USD when needed."""
        base_u = base.upper()
        quote_u = quote.upper()
        if base_u == quote_u:
            return FxRate(base=base_u, quote=quote_u, rate=Decimal("1"), as_of=datetime.now(UTC))

        cached = await self._get_cached(base_u, quote_u)
        if cached is not None:
            return cached

        direct = await self._provider.fetch_rate(base_u, quote_u)
        if direct is not None:
            await self._remember(direct)
            return direct

        reverse = await self._provider.fetch_rate(quote_u, base_u)
        if reverse is not None and reverse.rate != 0:
            inverted = FxRate(
                base=base_u,
                quote=quote_u,
                rate=Decimal("1") / reverse.rate,
                as_of=reverse.as_of,
            )
            await self._remember(inverted)
            return inverted

        if base_u != "USD" and quote_u != "USD":
            leg1 = await self.get_rate(base_u, "USD")
            leg2 = await self.get_rate("USD", quote_u)
            triangulated = FxRate(
                base=base_u,
                quote=quote_u,
                rate=leg1.rate * leg2.rate,
                as_of=max(leg1.as_of, leg2.as_of),
            )
            await self._remember(triangulated)
            return triangulated

        raise FxRateUnavailableError(f"FX rate unavailable for {base_u}/{quote_u}")

    async def get_rates_for(self, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], FxRate]:
        """Batch-resolve rates for multiple pairs.

        Soft-fails on individual pairs: a pair with no available rate is
        simply omitted from the returned map. Callers (the portfolio
        aggregator) already treat a missing rate as "skip this conversion",
        so a single unsupported currency must not blow up the entire
        dashboard refresh.
        """
        wanted = {(b.upper(), q.upper()) for b, q in pairs}
        out: dict[tuple[str, str], FxRate] = {}
        for base, quote in wanted:
            try:
                out[(base, quote)] = await self.get_rate(base, quote)
            except FxRateUnavailableError:
                import logging
                logging.getLogger("mbp.fx").info(
                    "get_rates_for: skipping unavailable pair %s/%s",
                    base,
                    quote,
                )
        return out

    async def _get_cached(self, base: str, quote: str) -> FxRate | None:
        key = (base, quote)
        mem = self._memory.get(key)
        if mem is not None:
            if mem.expires_at > time.monotonic():
                return mem.rate
            self._memory.pop(key, None)

        persisted = await self._firestore_cache.get_rate(base, quote)
        if persisted is not None:
            self._memory[key] = _CacheEntry(
                rate=persisted,
                expires_at=time.monotonic() + self._ttl_seconds,
            )
            return persisted
        return None

    async def _remember(self, rate: FxRate) -> None:
        key = (rate.base.upper(), rate.quote.upper())
        self._memory[key] = _CacheEntry(
            rate=rate,
            expires_at=time.monotonic() + self._ttl_seconds,
        )
        await self._firestore_cache.set_rate(rate, ttl_seconds=self._ttl_seconds)


__all__ = [
    "ExchangerateHostProvider",
    "FrankfurterProvider",
    "FxCacheStore",
    "FxProvider",
    "FxRateUnavailableError",
    "FxService",
    "InMemoryFxCacheStore",
    "NullFxCacheStore",
    "OpenExchangeRatesProvider",
]
