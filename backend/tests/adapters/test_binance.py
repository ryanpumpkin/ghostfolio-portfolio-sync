"""Tests for the Binance adapter (binance.com + binance.us)."""

from __future__ import annotations

import os
import sys
import types
from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, cast

import pytest

from app.adapters._common import PermanentError, RetryPolicy
from app.adapters.binance import (
    BinanceAdapter,
    BinanceHost,
    sign_query,
)
from app.adapters.binance.adapter import BinanceCredentials, HttpxBinanceClient
from app.models.domain import SourceHealthStatus


class FakeBinanceClient:
    def __init__(
        self,
        *,
        host: BinanceHost = BinanceHost.COM,
        account: dict[str, Any] | None = None,
        trades: list[dict[str, Any]] | None = None,
        trades_by_symbol: dict[str, list[dict[str, Any]]] | None = None,
        deposits: list[dict[str, Any]] | None = None,
        withdrawals: list[dict[str, Any]] | None = None,
        quotes: list[dict[str, Any]] | None = None,
        klines: dict[str, list[Any]] | None = None,
        ticker_prices: dict[str, str] | None = None,
        fail_account: int = 0,
        ping_result: bool = True,
        ping_raises: Exception | None = None,
    ) -> None:
        self.host = host
        self._account = account or {
            "canTrade": False,
            "canWithdraw": False,
            "permissions": ["READ_ONLY"],
            "balances": [],
        }
        self._trades = trades or []
        self._trades_by_symbol = trades_by_symbol or {}
        self._deposits = deposits or []
        self._withdrawals = withdrawals or []
        self._quotes = quotes or []
        self._klines = klines or {}
        self._ticker_prices = ticker_prices or {}
        self._fail_account = fail_account
        self._ping_result = ping_result
        self._ping_raises = ping_raises
        self.account_calls = 0
        self.trade_calls: list[tuple[str | None, str | None, int | None]] = []

    async def get_account(self) -> dict[str, Any]:
        self.account_calls += 1
        if self._fail_account > 0:
            self._fail_account -= 1
            from app.adapters._common import TransientError

            raise TransientError("rate-limited")
        return self._account

    async def get_my_trades(
        self, *, symbol: str | None, since: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        self.trade_calls.append((symbol, since, limit))
        if symbol is not None and symbol in self._trades_by_symbol:
            return self._trades_by_symbol[symbol]
        return self._trades

    async def get_deposit_history(self, *, since: str | None) -> list[dict[str, Any]]:
        return self._deposits

    async def get_withdraw_history(self, *, since: str | None) -> list[dict[str, Any]]:
        return self._withdrawals

    async def get_klines(
        self, *, symbol: str, interval: str, limit: int
    ) -> list[Any]:
        return self._klines.get(symbol, [])

    async def get_ticker_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            price = self._ticker_prices.get(symbol)
            if price is not None:
                out.append({"symbol": symbol, "price": price})
        return out

    async def stream_mini_tickers(
        self, symbols: list[str]
    ) -> AsyncIterator[dict[str, Any]]:
        for q in self._quotes:
            yield q

    async def ping(self) -> bool:
        if self._ping_raises is not None:
            raise self._ping_raises
        return self._ping_result

    async def close(self) -> None:
        return None


def _no_jitter() -> RetryPolicy:
    return RetryPolicy(max_attempts=2, initial_delay=0.0, jitter=0.0)


def _read_only_account(**balances_kwargs: Any) -> dict[str, Any]:
    return {
        "canTrade": False,
        "canWithdraw": False,
        "permissions": ["SPOT"],
        "balances": balances_kwargs.get(
            "balances",
            [
                {"asset": "BTC", "free": "0.5", "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "0"},
                {"asset": "ETH", "free": "0", "locked": "0"},
            ],
        ),
    }


def test_sign_query_is_deterministic_hmac_sha256() -> None:
    a = sign_query("s3cr3t", {"a": "1", "b": "2"})
    b = sign_query("s3cr3t", {"a": "1", "b": "2"})
    assert a == b
    assert "signature=" in a
    # Different secret -> different signature.
    c = sign_query("other", {"a": "1", "b": "2"})
    assert a != c


def test_binance_host_urls() -> None:
    assert BinanceHost.COM.rest_base == "https://api.binance.com"
    assert BinanceHost.US.rest_base == "https://api.binance.us"
    assert BinanceHost.COM.ws_base.startswith("wss://")
    assert BinanceHost.US.ws_base.startswith("wss://")


@pytest.mark.asyncio
async def test_rejects_trade_enabled_key() -> None:
    bad = {
        "canTrade": True,
        "canWithdraw": False,
        "permissions": ["SPOT"],
        "balances": [],
    }
    client = FakeBinanceClient(account=bad)
    adapter = BinanceAdapter(client, retry=_no_jitter())
    with pytest.raises(PermanentError):
        await adapter.verify_read_only()


@pytest.mark.asyncio
async def test_rejects_withdraw_enabled_key() -> None:
    bad = {
        "canTrade": False,
        "canWithdraw": True,
        "permissions": ["READ_ONLY"],
        "balances": [],
    }
    adapter = BinanceAdapter(FakeBinanceClient(account=bad), retry=_no_jitter())
    with pytest.raises(PermanentError):
        await adapter.verify_read_only()


@pytest.mark.asyncio
async def test_accepts_spot_permission_when_key_is_read_only() -> None:
    adapter = BinanceAdapter(FakeBinanceClient(account=_read_only_account()), retry=_no_jitter())
    await adapter.verify_read_only()


@pytest.mark.asyncio
async def test_list_positions_skips_zero_and_maps() -> None:
    client = FakeBinanceClient(
        account=_read_only_account(),
        ticker_prices={"BTCUSDT": "65000"},
    )
    adapter = BinanceAdapter(client, retry=_no_jitter())
    positions = await adapter.list_positions()
    symbols = {p.symbol for p in positions}
    assert symbols == {"BTC", "USDT"}
    btc = next(p for p in positions if p.symbol == "BTC")
    assert btc.quantity == Decimal("0.5")
    assert btc.last_price == Decimal("65000")
    assert btc.market_value == Decimal("32500.0")
    assert btc.currency == "USDT"


@pytest.mark.asyncio
async def test_list_positions_falls_back_to_klines_if_ticker_missing() -> None:
    client = FakeBinanceClient(
        account=_read_only_account(
            balances=[
                {"asset": "BTC", "free": "0.5", "locked": "0"},
                {"asset": "USDT", "free": "100", "locked": "0"},
            ]
        ),
        klines={"BTCUSDT": [[0, "0", "0", "0", "64000", "0"]]},
    )
    adapter = BinanceAdapter(client, retry=_no_jitter())
    positions = await adapter.list_positions()
    btc = next(p for p in positions if p.symbol == "BTC")
    assert btc.last_price == Decimal("64000")


@pytest.mark.asyncio
async def test_list_balances_skips_zero() -> None:
    client = FakeBinanceClient(account=_read_only_account())
    adapter = BinanceAdapter(client, retry=_no_jitter())
    balances = await adapter.list_balances()
    assert {b.currency for b in balances} == {"BTC", "USDT"}


@pytest.mark.asyncio
async def test_transactions_merge_and_sort() -> None:
    client = FakeBinanceClient(
        account=_read_only_account(),
        trades_by_symbol={
            "BTCUSDT": [
                {
                    "id": 42,
                    "symbol": "BTCUSDT",
                    "isBuyer": True,
                    "qty": "0.1",
                    "price": "50000",
                    "quoteQty": "5000",
                    "commissionAsset": "USDT",
                    "time": 1700000000000,
                }
            ]
        },
        deposits=[
            {
                "txId": "dep-1",
                "coin": "USDT",
                "amount": "1000",
                "insertTime": 1690000000000,
            }
        ],
        withdrawals=[
            {
                "id": "wd-1",
                "coin": "USDT",
                "amount": "500",
                "applyTime": 1710000000000,
            }
        ],
    )
    adapter = BinanceAdapter(client, retry=_no_jitter())
    txs = await adapter.list_transactions()
    sides = [t.side for t in txs]
    assert sides == ["deposit", "buy", "withdrawal"]
    assert txs[0].timestamp < txs[1].timestamp < txs[2].timestamp
    assert txs[1].currency == "USDT"
    assert txs[1].amount == Decimal("5000")
    assert client.trade_calls and client.trade_calls[0][0] == "BTCUSDT"
    assert client.trade_calls[0][1] is not None


@pytest.mark.asyncio
async def test_transactions_limits_symbol_fanout_to_20() -> None:
    balances = [
        {"asset": f"COIN{i}", "free": "1", "locked": "0"}
        for i in range(25)
    ]
    client = FakeBinanceClient(account=_read_only_account(balances=balances), trades=[])
    adapter = BinanceAdapter(client, retry=_no_jitter())
    await adapter.list_transactions()
    assert len(client.trade_calls) == 20


def test_httpx_client_parses_iso_since_to_ms() -> None:
    parsed = HttpxBinanceClient._to_int_ms("2026-05-19T00:00:00Z")
    assert parsed is not None
    assert parsed > 0


@pytest.mark.asyncio
async def test_httpx_client_stream_mini_tickers_uses_trade_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeSocket:
        async def __aenter__(self) -> _FakeSocket:
            return self

        async def __aexit__(
            self,
            exc_type: Any,
            exc: Any,
            tb: Any,
        ) -> None:
            _ = (exc_type, exc, tb)

        async def recv(self) -> dict[str, Any]:
            return {"data": {"s": "BTCUSDT", "p": "100", "E": 1700000000000}}

    class _FakeBsm:
        def __init__(self, _sdk: Any) -> None:
            self.streams: list[str] = []

        def multiplex_socket(self, streams: list[str]) -> _FakeSocket:
            self.streams = streams
            return _FakeSocket()

    streams_mod = types.ModuleType("binance.streams")
    streams_mod.BinanceSocketManager = _FakeBsm  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "binance.streams", streams_mod)

    client = HttpxBinanceClient(
        BinanceCredentials(api_key="k", api_secret="s"),
        host=BinanceHost.COM,
    )
    client._sdk = cast(Any, object())  # type: ignore[attr-defined]

    gen = client.stream_mini_tickers(["BTCUSDT"])
    first = await anext(gen)
    assert first["s"] == "BTCUSDT"
    await gen.aclose()


@pytest.mark.asyncio
async def test_stream_quotes_infers_quote_currency() -> None:
    client = FakeBinanceClient(
        quotes=[
            {"s": "BTCUSDT", "c": "50000", "E": 1700000000000},
            {"s": "ETHBTC", "c": "0.05", "E": 1700000000000},
        ]
    )
    adapter = BinanceAdapter(client, retry=_no_jitter())
    out = [q async for q in adapter.stream_quotes(["BTCUSDT", "ETHBTC"])]
    assert out[0].currency == "USDT"
    assert out[1].currency == "BTC"


@pytest.mark.asyncio
async def test_healthcheck_paths() -> None:
    adapter = BinanceAdapter(FakeBinanceClient(), retry=_no_jitter())
    snap = await adapter.healthcheck()
    assert snap.status is SourceHealthStatus.OK

    adapter = BinanceAdapter(FakeBinanceClient(ping_result=False), retry=_no_jitter())
    snap = await adapter.healthcheck()
    assert snap.status is not SourceHealthStatus.OK

    adapter = BinanceAdapter(
        FakeBinanceClient(ping_raises=RuntimeError("net")), retry=_no_jitter()
    )
    snap = await adapter.healthcheck()
    assert snap.message is not None and "net" in snap.message


@pytest.mark.asyncio
async def test_supports_binance_us_host() -> None:
    client = FakeBinanceClient(host=BinanceHost.US, account=_read_only_account())
    adapter = BinanceAdapter(client, retry=_no_jitter())
    assert adapter.host is BinanceHost.US
    positions = await adapter.list_positions()
    assert any(p.symbol == "BTC" for p in positions)


@pytest.mark.asyncio
async def test_retries_rate_limited_account_then_succeeds() -> None:
    client = FakeBinanceClient(account=_read_only_account(), fail_account=1)
    adapter = BinanceAdapter(client, retry=RetryPolicy(max_attempts=3, initial_delay=0.0, jitter=0.0))
    positions = await adapter.list_positions()
    assert client.account_calls == 2
    assert any(p.symbol == "BTC" for p in positions)


@pytest.mark.asyncio
async def test_trade_or_withdraw_key_rejected_on_transactions_init_check() -> None:
    bad = {
        "canTrade": True,
        "canWithdraw": False,
        "permissions": ["SPOT"],
        "balances": [],
    }
    adapter = BinanceAdapter(FakeBinanceClient(account=bad), retry=_no_jitter())
    with pytest.raises(PermanentError):
        await adapter.list_transactions()


class _PagedMyTradesSdk:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def get_my_trades(self, **params: Any) -> Any:
        self.calls.append(dict(params))
        start = int(params.get("startTime", 0))
        limit = int(params.get("limit", 0))
        if start == 1000:
            return [
                {"id": i + 1, "symbol": "BTCUSDT", "time": start + i}
                for i in range(limit)
            ]
        if start > 1000:
            return [{"id": 999999, "symbol": "BTCUSDT", "time": start}]
        return []


@pytest.mark.asyncio
async def test_httpx_client_get_my_trades_pages_when_limit_exceeds_single_call() -> None:
    sdk = _PagedMyTradesSdk()
    client = HttpxBinanceClient(
        BinanceCredentials(api_key="k", api_secret="s"),
        host=BinanceHost.COM,
    )
    client._sdk = sdk  # type: ignore[assignment]

    rows = await client.get_my_trades(symbol="BTCUSDT", since="1000", limit=1001)
    assert len(rows) == 1001
    assert len(sdk.calls) == 2
    assert int(sdk.calls[0]["startTime"]) == 1000
    assert int(sdk.calls[1]["startTime"]) == 2000


@pytest.mark.asyncio
async def test_integration_real_binance_balances_env_gated() -> None:
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    region = (os.getenv("BINANCE_REGION") or "com").lower()
    if not (api_key and api_secret):
        pytest.skip("Binance integration env vars not set")

    pytest.importorskip("binance.async_client")

    host = BinanceHost.US if region in {"us", "binance.us"} else BinanceHost.COM
    client = HttpxBinanceClient(
        BinanceCredentials(api_key=api_key, api_secret=api_secret),
        host=host,
    )
    try:
        adapter = BinanceAdapter(client, retry=RetryPolicy(max_attempts=2, initial_delay=0.1))
        balances = await adapter.list_balances()
        assert isinstance(balances, list)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_integration_real_binance_transactions_env_gated() -> None:
    api_key = os.getenv("BINANCE_API_KEY")
    api_secret = os.getenv("BINANCE_API_SECRET")
    region = (os.getenv("BINANCE_REGION") or "com").lower()
    if not (api_key and api_secret):
        pytest.skip("Binance integration env vars not set")

    pytest.importorskip("binance.async_client")

    host = BinanceHost.US if region in {"us", "binance.us"} else BinanceHost.COM
    client = HttpxBinanceClient(
        BinanceCredentials(api_key=api_key, api_secret=api_secret),
        host=host,
    )
    try:
        adapter = BinanceAdapter(client, retry=RetryPolicy(max_attempts=2, initial_delay=0.1))
        txs = await adapter.list_transactions(limit=20)
        assert len(txs) >= 1
    finally:
        await client.close()
