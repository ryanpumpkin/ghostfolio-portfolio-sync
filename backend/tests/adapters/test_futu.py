"""Tests for the Futu adapter."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.adapters._common import RetryPolicy
from app.adapters.futu import FutuAdapter
from app.adapters.futu.client import FutuOpenDClient
from app.models.domain import SourceHealthStatus


class FakeFutuClient:
    def __init__(
        self,
        *,
        positions: list[dict[str, Any]] | None = None,
        accounts: list[dict[str, Any]] | None = None,
        orders: list[dict[str, Any]] | None = None,
        quotes: list[dict[str, Any]] | None = None,
        fail_positions: int = 0,
        ping_result: bool = True,
        ping_raises: Exception | None = None,
    ) -> None:
        self._positions = positions or []
        self._accounts = accounts or []
        self._orders = orders or []
        self._quotes = quotes or []
        self._fail_positions = fail_positions
        self._ping_result = ping_result
        self._ping_raises = ping_raises
        self.position_calls = 0
        # Tripwires. The adapter must never reach for these: an OpenD
        # session that is never unlocked cannot place, modify or cancel
        # an order, and that is the whole point of §4.3 rule 2.
        self.unlock_calls = 0
        self.lock_calls = 0

    async def unlock_trade(self, password: str) -> None:  # pragma: no cover
        self.unlock_calls += 1
        raise AssertionError(
            "adapter called unlock_trade — reads must never unlock (§4.3 rule 2)"
        )

    async def lock_trade(self) -> None:  # pragma: no cover
        self.lock_calls += 1
        raise AssertionError("adapter called lock_trade — it should never unlock")

    async def fetch_positions(self) -> list[dict[str, Any]]:
        self.position_calls += 1
        if self._fail_positions > 0:
            self._fail_positions -= 1
            raise RuntimeError("rate limit")
        return self._positions

    async def fetch_accounts(self) -> list[dict[str, Any]]:
        return self._accounts

    async def fetch_history_deals(
        self, *, since: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        return self._orders

    async def subscribe_quotes(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        for q in self._quotes:
            yield q

    async def ping(self) -> bool:
        if self._ping_raises is not None:
            raise self._ping_raises
        return self._ping_result


def _no_jitter() -> RetryPolicy:
    return RetryPolicy(max_attempts=2, initial_delay=0.0, jitter=0.0)


@pytest.mark.asyncio
async def test_positions_are_read_without_unlocking() -> None:
    # §4.3 rule 2. Verified against real OpenD 10.6.6608 on 2026-09-06:
    # a session that never calls unlock_trade still returns accinfo,
    # positions and deal history. The repo previously believed the
    # opposite (BROKER_INTEGRATION_DETAILS §C.5), and that single wrong
    # assertion is why a trade password existed at all.
    client = FakeFutuClient(
        positions=[
            {
                "acc_id": 99,
                "code": "HK.00700",
                "trd_market": "HK",
                "qty": "100",
                "cost_price": "300",
                "nominal_price": "350",
                "market_val": "35000",
                "pl_val": "5000",
                "currency": "HKD",
            }
        ]
    )
    adapter = FutuAdapter(client, retry=_no_jitter())
    positions = await adapter.list_positions()
    assert positions[0].symbol == "HK.00700"
    assert positions[0].quantity == Decimal("100")
    assert client.unlock_calls == 0
    assert client.lock_calls == 0


@pytest.mark.asyncio
async def test_balances_are_read_without_unlocking() -> None:
    client = FakeFutuClient(
        accounts=[{"acc_id": 1, "currency": "HKD", "cash": "1000"}]
    )
    adapter = FutuAdapter(client, retry=_no_jitter())
    balances = await adapter.list_balances()
    assert balances[0].amount == Decimal("1000")
    assert client.unlock_calls == 0
    assert client.lock_calls == 0


@pytest.mark.asyncio
async def test_transactions_are_read_without_unlocking() -> None:
    client = FakeFutuClient(orders=[])
    adapter = FutuAdapter(client, retry=_no_jitter())
    await adapter.list_transactions()
    assert client.unlock_calls == 0


@pytest.mark.asyncio
async def test_adapter_exposes_no_password_parameter() -> None:
    # A password parameter is how this creeps back in. There must be
    # nowhere to put one.
    import inspect

    params = inspect.signature(FutuAdapter.__init__).parameters
    assert not [p for p in params if "password" in p or "unlock" in p]


@pytest.mark.asyncio
async def test_transactions_mapping() -> None:
    client = FakeFutuClient(
        orders=[
            {
                "acc_id": 1,
                "order_id": "o-1",
                "code": "HK.00700",
                "trd_side": "BUY",
                "qty": "100",
                "price": "350",
                "currency": "HKD",
                "dealt_amount": "35000",
                "create_time": "2025-01-02T03:04:05+00:00",
            }
        ]
    )
    adapter = FutuAdapter(client, retry=_no_jitter())
    txs = await adapter.list_transactions()
    assert txs[0].transaction_id == "o-1"
    assert txs[0].side == "buy"


@pytest.mark.asyncio
async def test_retries_rate_limited_call_then_succeeds() -> None:
    client = FakeFutuClient(
        positions=[
            {
                "code": "HK.00700",
                "qty": "1",
                "currency": "HKD",
            }
        ],
        fail_positions=2,
    )
    adapter = FutuAdapter(client, retry=RetryPolicy(max_attempts=5, initial_delay=0.0, jitter=0.0))
    rows = await adapter.list_positions()
    assert client.position_calls == 3
    assert rows[0].symbol == "HK.00700"


@pytest.mark.asyncio
async def test_stream_quotes_and_health() -> None:
    client = FakeFutuClient(
        quotes=[
            {
                "code": "HK.00700",
                "last_price": "350",
                "currency": "HKD",
                "timestamp": "2025-01-02T00:00:00+00:00",
            }
        ]
    )
    adapter = FutuAdapter(client, retry=_no_jitter())
    quotes = [q async for q in adapter.stream_quotes(["HK.00700"])]
    assert quotes[0].price == Decimal("350")

    snap = await adapter.healthcheck()
    assert snap.status is SourceHealthStatus.OK

    bad = FutuAdapter(
        FakeFutuClient(ping_result=False), retry=_no_jitter()
    )
    snap = await bad.healthcheck()
    assert snap.status is not SourceHealthStatus.OK

    boom = FutuAdapter(
        FakeFutuClient(ping_raises=RuntimeError("opend down")), retry=_no_jitter()
    )
    snap = await boom.healthcheck()
    assert snap.message is not None and "opend down" in snap.message


@pytest.mark.asyncio
async def test_integration_real_futu_positions_env_gated() -> None:
    host = os.getenv("FUTU_OPEND_HOST")
    port_raw = os.getenv("FUTU_OPEND_PORT")
    # No password: the point of these tests is that reads work against a
    # LOCKED session (§4.3 rule 2).
    if not (host and port_raw):
        pytest.skip("FUTU_OPEND_HOST/FUTU_OPEND_PORT not set")

    pytest.importorskip("futu")
    try:
        port = int(port_raw)
    except ValueError:
        pytest.skip("FUTU_OPEND_PORT is not an integer")

    client = FutuOpenDClient(host=host, port=port)
    adapter = FutuAdapter(
        client,
        retry=RetryPolicy(max_attempts=2, initial_delay=0.1, jitter=0.0),
    )
    positions = await adapter.list_positions()
    assert len(positions) >= 1


@pytest.mark.asyncio
async def test_integration_real_futu_transactions_env_gated() -> None:
    host = os.getenv("FUTU_OPEND_HOST")
    port_raw = os.getenv("FUTU_OPEND_PORT")
    # No password: the point of these tests is that reads work against a
    # LOCKED session (§4.3 rule 2).
    if not (host and port_raw):
        pytest.skip("FUTU_OPEND_HOST/FUTU_OPEND_PORT not set")

    pytest.importorskip("futu")
    try:
        port = int(port_raw)
    except ValueError:
        pytest.skip("FUTU_OPEND_PORT is not an integer")

    client = FutuOpenDClient(host=host, port=port)
    adapter = FutuAdapter(
        client,
        retry=RetryPolicy(max_attempts=2, initial_delay=0.1, jitter=0.0),
    )
    txs = await adapter.list_transactions(limit=20)
    assert len(txs) >= 1


class TestDealMapping:
    """Futu deal rows are thinner than they look (§5.6, §3.3).

    A deal carries code, qty, price, side, market and timestamps — no
    currency, no fee, and an order_id that several deals can share.
    """

    def _deal(self, **overrides):
        base = {
            "deal_id": "9178032281698908682",
            "order_id": "FH1CC28FAD3E248000",
            "code": "HK.00823",
            "deal_market": "HK",
            "qty": "103",
            "price": "32.996",
            "trd_side": "BUY",
            "create_time": "2024-09-03 10:00:00.000",
        }
        base.update(overrides)
        return base

    def test_currency_comes_from_the_market(self):
        from app.adapters.futu.adapter import _map_transaction

        # Without this an HK trade is recorded in USD — the mapper
        # defaults a missing currency to USD, so the cost is overstated
        # by roughly the HKD/USD rate.
        assert _map_transaction(self._deal()).currency == "HKD"
        assert _map_transaction(
            self._deal(code="US.VOO", deal_market="US")
        ).currency == "USD"

    def test_currency_falls_back_to_the_code_prefix(self):
        from app.adapters.futu.adapter import _map_transaction

        assert _map_transaction(
            self._deal(deal_market="")
        ).currency == "HKD"

    def test_an_unknown_market_is_left_unset_not_guessed(self):
        from app.adapters.futu.adapter import _map_transaction

        tx = _map_transaction(self._deal(code="XX.1234", deal_market="XX"))
        assert tx.currency is None

    def test_deals_of_one_order_keep_distinct_ids(self):
        from app.adapters.futu.adapter import _map_transaction

        first = _map_transaction(self._deal(deal_id="d1"))
        second = _map_transaction(self._deal(deal_id="d2"))
        # Same order, two fills: keying on order_id would make the
        # idempotency ledger drop the second (§3.3).
        assert first.transaction_id != second.transaction_id

    def test_fee_is_carried_through(self):
        from decimal import Decimal

        from app.adapters.futu.adapter import _map_transaction

        tx = _map_transaction(self._deal(fee="18.5"))
        assert tx.fee == Decimal("18.5")
        assert tx.fee_currency == "HKD"


class TestHistoryWalkDoesNotRepeatItself:
    """Chunk boundaries used to be queried twice (§3.3).

    `history_deal_list_query` takes dates and includes both ends, and the
    walk stepped back one microsecond between chunks — which lands on the
    same day. Every boundary day was fetched twice.

    Live cost: four Futu deals reached Ghostfolio in duplicate, every one
    of them on an exact 30-day boundary. One added 20 SOFI shares the
    account does not hold. Another duplicated a SELL, which drove the
    replayed quantity negative and made the opening-balance pass invent a
    BUY of 3 to cover it — a fabricated cost basis, from a date-arithmetic
    slip.
    """

    def test_chunks_never_share_a_day(self) -> None:
        from app.adapters.futu.client import _history_chunks

        end = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        start = end - timedelta(days=365)
        chunks = _history_chunks(start, end)
        assert len(chunks) > 1
        for (_, earlier_end), (later_start, _) in zip(
            chunks[1:], chunks[:-1], strict=True
        ):
            assert earlier_end.date() < later_start.date()

    def test_the_whole_window_is_still_covered(self) -> None:
        from app.adapters.futu.client import _history_chunks

        end = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        start = end - timedelta(days=100)
        chunks = _history_chunks(start, end)
        assert chunks[0][1] == end
        assert chunks[-1][0] == start
        # No day falls between two consecutive chunks.
        for (_, earlier_end), (later_start, _) in zip(
            chunks[1:], chunks[:-1], strict=True
        ):
            assert (later_start.date() - earlier_end.date()).days == 1

    def test_a_window_shorter_than_a_chunk_is_one_chunk(self) -> None:
        from app.adapters.futu.client import _history_chunks

        end = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
        assert len(_history_chunks(end - timedelta(days=3), end)) == 1


class TestDealDedupe:
    def test_a_repeated_deal_id_is_kept_once(self) -> None:
        from app.adapters.futu.client import _dedupe_deals

        rows = [
            {"deal_id": "1", "code": "US.SOFI", "qty": 20},
            {"deal_id": "1", "code": "US.SOFI", "qty": 20},
            {"deal_id": "2", "code": "US.VOO", "qty": 1},
        ]
        assert [r["deal_id"] for r in _dedupe_deals(rows)] == ["1", "2"]

    def test_rows_without_a_deal_id_are_not_collapsed(self) -> None:
        """Two unidentified rows are two rows, not one."""
        from app.adapters.futu.client import _dedupe_deals

        rows = [{"code": "US.SOFI", "qty": 20}, {"code": "US.SOFI", "qty": 20}]
        assert len(_dedupe_deals(rows)) == 2
