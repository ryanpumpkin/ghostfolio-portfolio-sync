"""Tests for the FX service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.domain import FxRate
from app.services.fx import FxService, InMemoryFxCacheStore


class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch_rate(self, base: str, quote: str) -> FxRate | None:
        self.calls.append((base, quote))
        table: dict[tuple[str, str], Decimal] = {
            ("EUR", "USD"): Decimal("1.10"),
            ("USD", "HKD"): Decimal("7.80"),
        }
        rate = table.get((base, quote))
        if rate is None:
            return None
        return FxRate(base=base, quote=quote, rate=rate, as_of=datetime.now(UTC))


@pytest.mark.asyncio
async def test_fx_service_triangulates_via_usd_when_direct_pair_missing() -> None:
    provider = _FakeProvider()
    service = FxService(provider=provider, firestore_cache=InMemoryFxCacheStore(), ttl_seconds=60.0)

    eur_hkd = await service.get_rate("EUR", "HKD")
    assert eur_hkd.rate == Decimal("8.5800")

    cached = await service.get_rate("EUR", "HKD")
    assert cached.rate == eur_hkd.rate

    # Direct pair was absent; service resolved via EUR/USD and USD/HKD.
    assert ("EUR", "USD") in provider.calls
    assert ("USD", "HKD") in provider.calls


# ---------------------------------------------------------------------------
# FrankfurterProvider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_frankfurter_provider_parses_rate() -> None:
    import httpx

    from app.services.fx import FrankfurterProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/latest")
        assert request.url.params["base"] == "USD"
        assert request.url.params["symbols"] == "HKD"
        return httpx.Response(
            200,
            json={
                "amount": 1,
                "base": "USD",
                "date": "2026-05-18",
                "rates": {"HKD": 7.79},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FrankfurterProvider(client=client)
    rate = await provider.fetch_rate("USD", "HKD")
    assert rate is not None
    assert rate.rate == Decimal("7.79")


@pytest.mark.asyncio
async def test_frankfurter_provider_returns_none_when_quote_absent() -> None:
    import httpx

    from app.services.fx import FrankfurterProvider

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"base": "USD", "rates": {"EUR": 0.9}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FrankfurterProvider(client=client)
    rate = await provider.fetch_rate("USD", "HKD")
    assert rate is None


@pytest.mark.asyncio
async def test_frankfurter_reports_the_published_date_not_now() -> None:
    """`as_of` is what tells a historical rate from a current one.

    Stamping every rate with `datetime.now()` made a 2024 lookup
    indistinguishable from today's, so a caller converting an old trade
    could not tell whether it got real history — and had to report an
    approximation it may not have made.
    """
    from datetime import date

    import httpx

    from app.services.fx import FrankfurterProvider

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/2024-12-10")
        return httpx.Response(
            200, json={"amount": 1, "base": "USD", "date": "2024-12-10",
                       "rates": {"HKD": 7.7712}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FrankfurterProvider(client, base_url="https://x/v1")
    rate = await provider.fetch_rate_on("USD", "HKD", date(2024, 12, 10))
    assert rate is not None
    assert rate.as_of.date() == date(2024, 12, 10)
    assert str(rate.rate) == "7.7712"


@pytest.mark.asyncio
async def test_a_weekend_resolves_to_the_preceding_business_day() -> None:
    from datetime import date

    import httpx

    from app.services.fx import FrankfurterProvider

    def handler(request: httpx.Request) -> httpx.Response:
        # Asked for Sunday; Frankfurter answers with Friday's rate.
        return httpx.Response(
            200, json={"base": "USD", "date": "2024-12-06",
                       "rates": {"HKD": 7.77}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = FrankfurterProvider(client, base_url="https://x/v1")
    rate = await provider.fetch_rate_on("USD", "HKD", date(2024, 12, 8))
    assert rate is not None
    assert rate.as_of.date() == date(2024, 12, 6)
