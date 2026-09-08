"""Tests for the LongBridge adapter using fake and real SDK clients."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from app.adapters._common import PermanentError, RetryPolicy, TransientError
from app.adapters.longbridge import LongBridgeAdapter, LongbridgeClient
from app.models.domain import SourceHealthStatus


class FakeLBClient:
    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        balances: list[dict[str, Any]] | None = None,
        transactions: list[dict[str, Any]] | None = None,
        quotes: list[dict[str, Any]] | None = None,
        fail_positions: int = 0,
        fail_message: str | None = None,
        ping_result: bool = True,
        ping_raises: Exception | None = None,
    ) -> None:
        self._positions = positions or []
        self._balances = balances or []
        self._transactions = transactions or []
        self._quotes = quotes or []
        self._fail_positions = fail_positions
        self._fail_message = fail_message
        self._ping_result = ping_result
        self._ping_raises = ping_raises
        self.position_calls = 0

    async def list_positions(self) -> list[dict[str, Any]]:
        self.position_calls += 1
        if self._fail_positions > 0:
            self._fail_positions -= 1
            msg = self._fail_message or "rate-limited"
            raise TransientError(msg)
        return self._positions

    async def list_balances(self) -> list[dict[str, Any]]:
        return self._balances

    async def list_transactions(
        self, *, since: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        return self._transactions

    async def stream_quotes(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        for q in self._quotes:
            yield q

    async def ping(self) -> bool:
        if self._ping_raises is not None:
            raise self._ping_raises
        return self._ping_result


class _AuthError(Exception):
    def __init__(self, message: str, code: int = 401) -> None:
        super().__init__(message)
        self.code = code


class _RateLimitError(Exception):
    def __init__(self, message: str = "rate limit", code: int = 429) -> None:
        super().__init__(message)
        self.code = code


def _no_jitter() -> RetryPolicy:
    return RetryPolicy(max_attempts=5, initial_delay=0.0, jitter=0.0)


@pytest.mark.asyncio
async def test_list_positions_maps_fields() -> None:
    client = FakeLBClient(
        positions=[
            {
                "symbol": "700.HK",
                "market": "HK",
                "quantity": "100",
                "cost_price": "300",
                "last_price": "350",
                "currency": "HKD",
                "account_no": "acc-1",
            }
        ]
    )
    adapter = LongBridgeAdapter(client, retry=_no_jitter())
    positions = await adapter.list_positions()
    assert len(positions) == 1
    p = positions[0]
    assert p.source == "longbridge"
    assert p.symbol == "700.HK"
    assert p.quantity == Decimal("100")
    assert p.market_value == Decimal("35000")
    assert p.unrealized_pnl == Decimal("5000")


@pytest.mark.asyncio
async def test_list_balances_and_transactions() -> None:
    client = FakeLBClient(
        balances=[{"currency": "HKD", "total_cash": "12345.67"}],
        transactions=[
            {
                "order_id": "ord-1",
                "symbol": "AAPL.US",
                "side": "Buy",
                "quantity": "10",
                "price": "150.00",
                "currency": "USD",
                "amount": "1500.00",
                "submitted_at": "2025-01-02T03:04:05Z",
            }
        ],
    )
    adapter = LongBridgeAdapter(client, retry=_no_jitter())
    balances = await adapter.list_balances()
    assert balances[0].amount == Decimal("12345.67")

    txs = await adapter.list_transactions(since=None, limit=10)
    assert txs[0].transaction_id == "ord-1"
    assert txs[0].side == "buy"
    assert txs[0].timestamp.year == 2025


@pytest.mark.asyncio
async def test_retries_transient_then_succeeds() -> None:
    client = FakeLBClient(
        positions=[
            {
                "symbol": "X",
                "quantity": "1",
                "currency": "USD",
            }
        ],
        fail_positions=2,
    )
    adapter = LongBridgeAdapter(client, retry=_no_jitter())
    out = await adapter.list_positions()
    assert client.position_calls == 3
    assert out[0].symbol == "X"


@pytest.mark.asyncio
async def test_retries_rate_limit_error_classified_transient() -> None:
    class FakeRateLimitClient(FakeLBClient):
        async def list_positions(self) -> list[dict[str, Any]]:
            self.position_calls += 1
            if self.position_calls < 3:
                raise _RateLimitError()
            return [{"symbol": "Y", "quantity": "1", "currency": "USD"}]

    adapter = LongBridgeAdapter(FakeRateLimitClient(), retry=_no_jitter())
    rows = await adapter.list_positions()
    assert rows[0].symbol == "Y"


@pytest.mark.asyncio
async def test_credential_error_becomes_permanent_error() -> None:
    class BadCredsClient(FakeLBClient):
        async def list_positions(self) -> list[dict[str, Any]]:
            raise _AuthError("invalid access token", code=401)

    adapter = LongBridgeAdapter(BadCredsClient(), retry=_no_jitter())
    with pytest.raises(PermanentError):
        await adapter.list_positions()


@pytest.mark.asyncio
async def test_stream_quotes() -> None:
    client = FakeLBClient(
        quotes=[
            {
                "symbol": "AAPL.US",
                "last_done": "150.50",
                "currency": "USD",
                "timestamp": "2025-01-02T03:04:05+00:00",
            }
        ]
    )
    adapter = LongBridgeAdapter(client, retry=_no_jitter())
    seen = [q async for q in adapter.stream_quotes(["AAPL.US"])]
    assert seen[0].price == Decimal("150.50")


@pytest.mark.asyncio
async def test_healthcheck_records_success_and_failure() -> None:
    ok_client = FakeLBClient()
    adapter = LongBridgeAdapter(ok_client, retry=_no_jitter())
    snap = await adapter.healthcheck()
    assert snap.status is SourceHealthStatus.OK

    bad_client = FakeLBClient(ping_raises=RuntimeError("bad token"))
    adapter = LongBridgeAdapter(bad_client, retry=_no_jitter())
    snap = await adapter.healthcheck()
    assert snap.status is not SourceHealthStatus.OK
    assert snap.message is not None and "bad token" in snap.message


@pytest.mark.asyncio
async def test_integration_real_longbridge_balances_env_gated() -> None:
    app_key = os.getenv("LB_APP_KEY")
    app_secret = os.getenv("LB_APP_SECRET")
    access_token = os.getenv("LB_ACCESS_TOKEN")
    if not (app_key and app_secret and access_token):
        pytest.skip("LongBridge integration env vars not set")

    pytest.importorskip("longbridge.openapi")

    client = LongbridgeClient(
        app_key=app_key,
        app_secret=app_secret,
        access_token=access_token,
    )
    adapter = LongBridgeAdapter(client, retry=RetryPolicy(max_attempts=2, initial_delay=0.1))
    balances = await adapter.list_balances()
    assert len(balances) >= 1


@pytest.mark.asyncio
async def test_integration_real_longbridge_transactions_env_gated() -> None:
    app_key = os.getenv("LB_APP_KEY")
    app_secret = os.getenv("LB_APP_SECRET")
    access_token = os.getenv("LB_ACCESS_TOKEN")
    if not (app_key and app_secret and access_token):
        pytest.skip("LongBridge integration env vars not set")

    pytest.importorskip("longbridge.openapi")

    client = LongbridgeClient(
        app_key=app_key,
        app_secret=app_secret,
        access_token=access_token,
    )
    adapter = LongBridgeAdapter(client, retry=RetryPolicy(max_attempts=2, initial_delay=0.1))
    txs = await adapter.list_transactions(limit=20)
    assert len(txs) >= 1
