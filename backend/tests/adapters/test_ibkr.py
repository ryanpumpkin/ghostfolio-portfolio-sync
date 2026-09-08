"""Tests for the IBKR adapter."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

import pytest

from app.adapters._common import PermanentError, RetryPolicy, TransientError
from app.adapters.ibkr import IbkrAdapter, IBKRClient
from app.models.domain import SourceHealthStatus


class FakeIbkrClient:
    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        accounts: list[dict[str, Any]] | None = None,
        executions: list[dict[str, Any]] | None = None,
        quotes: list[dict[str, Any]] | None = None,
        tickle_result: bool = True,
        tickle_raises: Exception | None = None,
        fail_positions: int = 0,
        position_error: Exception | None = None,
    ) -> None:
        self._positions = positions or []
        self._accounts = accounts or []
        self._executions = executions or []
        self._quotes = quotes or []
        self._tickle_result = tickle_result
        self._tickle_raises = tickle_raises
        self._fail_positions = fail_positions
        self._position_error = position_error
        self.tickle_calls = 0
        self.position_calls = 0

    async def tickle(self) -> bool:
        self.tickle_calls += 1
        if self._tickle_raises is not None:
            raise self._tickle_raises
        return self._tickle_result

    async def fetch_positions(self) -> list[dict[str, Any]]:
        self.position_calls += 1
        if self._position_error is not None:
            raise self._position_error
        if self._fail_positions > 0:
            self._fail_positions -= 1
            raise TransientError("rate-limited")
        return self._positions

    async def fetch_account_summary(self) -> list[dict[str, Any]]:
        return self._accounts

    async def fetch_executions(
        self, *, since: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        return self._executions

    async def stream_market_data(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        for q in self._quotes:
            yield q


def _no_jitter() -> RetryPolicy:
    return RetryPolicy(max_attempts=3, initial_delay=0.0, jitter=0.0)


@pytest.mark.asyncio
async def test_positions_and_balances_mapping() -> None:
    client = FakeIbkrClient(
        positions=[
            {
                "acctId": "U1",
                "contractDesc": "AAPL",
                "listingExchange": "NASDAQ",
                "position": "10",
                "avgCost": "150",
                "mktPrice": "170",
                "mktValue": "1700",
                "unrealizedPnl": "200",
                "currency": "USD",
            }
        ],
        accounts=[{"acctId": "U1", "currency": "USD", "cashBalance": "1000"}],
    )
    adapter = IbkrAdapter(client, retry=_no_jitter())
    positions = await adapter.list_positions()
    assert positions[0].symbol == "AAPL"
    assert positions[0].market_value == Decimal("1700")

    balances = await adapter.list_balances()
    assert balances[0].amount == Decimal("1000")
    assert client.tickle_calls == 2


@pytest.mark.asyncio
async def test_positions_retries_transient_then_succeeds() -> None:
    client = FakeIbkrClient(
        positions=[
            {
                "acctId": "U1",
                "contractDesc": "MSFT",
                "position": "5",
                "currency": "USD",
            }
        ],
        fail_positions=2,
    )
    adapter = IbkrAdapter(client, retry=RetryPolicy(max_attempts=5, initial_delay=0.0, jitter=0.0))
    out = await adapter.list_positions()
    assert out[0].symbol == "MSFT"
    assert client.position_calls == 3


@pytest.mark.asyncio
async def test_positions_credential_error_is_permanent() -> None:
    client = FakeIbkrClient(position_error=PermanentError("invalid credentials"))
    adapter = IbkrAdapter(client, retry=RetryPolicy(max_attempts=5, initial_delay=0.0, jitter=0.0))
    with pytest.raises(PermanentError):
        await adapter.list_positions()
    assert client.position_calls == 1


@pytest.mark.asyncio
async def test_transactions_mapping() -> None:
    client = FakeIbkrClient(
        executions=[
            {
                "execId": "exec-1",
                "symbol": "AAPL",
                "side": "BUY",
                "size": "10",
                "price": "150",
                "currency": "USD",
                "net_amount": "1500",
                "time": 1700000000,
            }
        ]
    )
    adapter = IbkrAdapter(client, retry=_no_jitter())
    txs = await adapter.list_transactions()
    assert txs[0].side == "buy"
    assert txs[0].transaction_id == "exec-1"
    assert txs[0].timestamp.year == 2023
    assert client.tickle_calls == 1


@pytest.mark.asyncio
async def test_stream_quotes() -> None:
    client = FakeIbkrClient(
        quotes=[{"symbol": "AAPL", "last": "170", "currency": "USD", "t": 1700000000}]
    )
    adapter = IbkrAdapter(client, retry=_no_jitter())
    out = [q async for q in adapter.stream_quotes(["AAPL"])]
    assert out[0].price == Decimal("170")
    assert client.tickle_calls == 1


@pytest.mark.asyncio
async def test_request_paths_fail_when_tickle_returns_false() -> None:
    client = FakeIbkrClient(
        positions=[
            {"acctId": "U1", "contractDesc": "AAPL", "position": "1", "currency": "USD"},
        ],
        tickle_result=False,
    )
    adapter = IbkrAdapter(client, retry=_no_jitter())
    with pytest.raises(TransientError, match="tickle returned false"):
        await adapter.list_positions()


@pytest.mark.asyncio
async def test_healthcheck_paths() -> None:
    adapter = IbkrAdapter(FakeIbkrClient(tickle_result=True), retry=_no_jitter())
    snap = await adapter.healthcheck()
    assert snap.status is SourceHealthStatus.OK

    adapter = IbkrAdapter(FakeIbkrClient(tickle_result=False), retry=_no_jitter())
    snap = await adapter.healthcheck()
    assert snap.status is not SourceHealthStatus.OK

    adapter = IbkrAdapter(
        FakeIbkrClient(tickle_raises=RuntimeError("net down")), retry=_no_jitter()
    )
    snap = await adapter.healthcheck()
    assert snap.message is not None and "net down" in snap.message


@pytest.mark.asyncio
async def test_keepalive_loop_calls_tickle_and_stops() -> None:
    client = FakeIbkrClient(tickle_result=True)
    adapter = IbkrAdapter(client, retry=_no_jitter(), keepalive_interval=0.001)
    task = adapter.start_keepalive()
    # Idempotent — calling again returns the same task.
    assert adapter.start_keepalive() is task
    await asyncio.sleep(0.02)
    await adapter.stop_keepalive()
    assert client.tickle_calls >= 1
    # Calling stop again is a no-op.
    await adapter.stop_keepalive()


@pytest.mark.asyncio
async def test_keepalive_records_failure_and_keeps_looping() -> None:
    client = FakeIbkrClient(tickle_raises=RuntimeError("boom"))
    adapter = IbkrAdapter(client, retry=_no_jitter(), keepalive_interval=0.001)
    adapter.start_keepalive()
    await asyncio.sleep(0.02)
    await adapter.stop_keepalive()
    assert client.tickle_calls >= 1


@pytest.mark.asyncio
async def test_integration_real_ibkr_transactions_env_gated() -> None:
    account_id = os.getenv("IBKR_ACCOUNT_ID")
    if not account_id:
        pytest.skip("IBKR integration env vars not set")

    pytest.importorskip("ib_insync")

    client = IBKRClient(account_id=account_id)
    adapter = IbkrAdapter(client, retry=RetryPolicy(max_attempts=2, initial_delay=0.1))
    txs = await adapter.list_transactions(limit=20)
    assert len(txs) >= 1
