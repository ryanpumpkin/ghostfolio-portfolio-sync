"""Unit tests for `IBKRClient` and its pure helpers in `ibkr.adapter`.

`IBKRClient` accepts an injected `ib` (the `ib_insync.IB` instance) so
all SDK-touching code paths can be exercised with a fake.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.adapters._common import PermanentError, TransientError
from app.adapters.ibkr.adapter import (
    IBKRClient,
    _classify_ibkr_error,
    _map_balance,
    _map_position,
    _parse_ts,
)


@pytest.fixture
def stub_ib_insync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject a minimal fake `ib_insync` module with a Stock class for
    code paths that resolve contract types at request time."""
    module = types.ModuleType("ib_insync")

    class _Stock:
        def __init__(self, symbol: str, exchange: str, currency: str) -> None:
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency

    module.Stock = _Stock  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "ib_insync", module)


# ---------------------------------------------------------------------------
# Fake ib_insync.IB
# ---------------------------------------------------------------------------


class _FakeIb:
    def __init__(
        self,
        *,
        connected: bool = True,
        connect_raises: Exception | None = None,
        positions_rows: list[Any] | None = None,
        positions_raises: Exception | None = None,
        summary_rows: list[Any] | None = None,
        trades_rows: list[Any] | None = None,
        trades_raises: Exception | None = None,
        tickers: list[Any] | None = None,
        tickers_raises: Exception | None = None,
    ) -> None:
        self._connected = connected
        self._connect_raises = connect_raises
        self._positions_rows = positions_rows or []
        self._positions_raises = positions_raises
        self._summary_rows = summary_rows or []
        self._trades_rows = trades_rows or []
        self._trades_raises = trades_raises
        self._tickers = tickers or []
        self._tickers_raises = tickers_raises
        self.connect_calls: list[tuple[Any, ...]] = []
        self.pendingTickersEvent = _Event()
        self.req_mkt_data_calls: list[Any] = []
        self.cancel_mkt_data_calls: list[Any] = []
        self._pushed_once = False

    def isConnected(self) -> bool:  # noqa: N802 - ib_insync API name
        return self._connected

    def connect(self, *args: Any, **kwargs: Any) -> None:
        self.connect_calls.append((args, kwargs))
        if self._connect_raises is not None:
            raise self._connect_raises
        self._connected = True

    def positions(self, _account: str) -> list[Any]:
        if self._positions_raises is not None:
            raise self._positions_raises
        return self._positions_rows

    def accountSummary(self, _account: str) -> list[Any]:  # noqa: N802 - SDK API
        return self._summary_rows

    def trades(self) -> list[Any]:
        if self._trades_raises is not None:
            raise self._trades_raises
        return self._trades_rows

    def reqTickers(self, *_contracts: Any) -> list[Any]:  # noqa: N802 - SDK API
        if self._tickers_raises is not None:
            raise self._tickers_raises
        return self._tickers

    def reqMktData(self, contract: Any, *_args: Any) -> None:  # noqa: N802 - SDK API
        if self._tickers_raises is not None:
            raise self._tickers_raises
        self.req_mkt_data_calls.append(contract)

    def cancelMktData(self, contract: Any) -> None:  # noqa: N802 - SDK API
        self.cancel_mkt_data_calls.append(contract)

    def waitOnUpdate(self, timeout: float | None = None) -> bool:  # noqa: N802 - SDK API
        _ = timeout
        if self._pushed_once:
            return False
        self._pushed_once = True
        if self._tickers:
            self.pendingTickersEvent.fire(self._tickers)
        return True


class _Event:
    def __init__(self) -> None:
        self._handlers: list[Any] = []

    def __iadd__(self, handler: Any) -> _Event:
        self._handlers.append(handler)
        return self

    def __isub__(self, handler: Any) -> _Event:
        self._handlers = [item for item in self._handlers if item is not handler]
        return self

    def fire(self, payload: list[Any]) -> None:
        for handler in list(self._handlers):
            handler(payload)


class _Obj:
    def __init__(self, **kwargs: Any) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)


# ---------------------------------------------------------------------------
# _classify_ibkr_error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        "Authentication failed",
        "Invalid credential",
        "Login required",
        "Permission denied",
        "Client not connected",
        "Invalid account id",
    ],
)
def test_classify_permanent_markers(msg: str) -> None:
    assert isinstance(_classify_ibkr_error(Exception(msg)), PermanentError)


@pytest.mark.parametrize(
    "msg",
    [
        "Request timeout",
        "Service temporarily unavailable",
        "try again later",
        "Connection reset by peer",
        "Rate limit hit",
        "Too many requests",
    ],
)
def test_classify_transient_markers(msg: str) -> None:
    assert isinstance(_classify_ibkr_error(Exception(msg)), TransientError)


def test_classify_unknown_defaults_to_transient() -> None:
    assert isinstance(_classify_ibkr_error(Exception("strange")), TransientError)


# ---------------------------------------------------------------------------
# _parse_ts
# ---------------------------------------------------------------------------


def test_parse_ts_datetime_naive() -> None:
    dt = datetime(2026, 5, 1, 12, 0)
    assert _parse_ts(dt) == dt.replace(tzinfo=UTC)


def test_parse_ts_datetime_aware() -> None:
    dt = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    assert _parse_ts(dt) == dt


def test_parse_ts_unix_timestamp() -> None:
    assert _parse_ts(1735689600).year == 2025


def test_parse_ts_iso_string() -> None:
    assert _parse_ts("2026-05-01T12:00:00Z") == datetime(2026, 5, 1, 12, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Mappers
# ---------------------------------------------------------------------------


def test_map_position_required_fields() -> None:
    raw = {
        "acctId": "U123",
        "contractDesc": "AAPL",
        "listingExchange": "NASDAQ",
        "position": "10",
        "avgCost": "150",
        "mktPrice": "200",
        "mktValue": "2000",
        "unrealizedPnl": "500",
        "currency": "USD",
    }
    p = _map_position(raw)
    assert p.symbol == "AAPL"
    assert p.quantity == Decimal("10")
    assert p.avg_cost == Decimal("150")
    assert p.market_value == Decimal("2000")
    assert p.currency == "USD"


def test_map_position_alternate_keys() -> None:
    raw = {
        "account_id": "U999",
        "symbol": "TSLA",
        "exchange": "NASDAQ",
        "position": "5",
        "avg_cost": "100",
        "currency": "USD",
    }
    p = _map_position(raw)
    assert p.account_id == "U999"
    assert p.symbol == "TSLA"
    assert p.avg_cost == Decimal("100")


def test_map_balance_basic() -> None:
    b = _map_balance(
        {
            "acctId": "U1",
            "currency": "HKD",
            "cashBalance": "1234.56",
        },
    )
    assert b.currency == "HKD"
    assert b.amount == Decimal("1234.56")


# ---------------------------------------------------------------------------
# IBKRClient — with injected fake ib
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tickle_returns_connected_state() -> None:
    ib = _FakeIb(connected=True)
    client = IBKRClient(ib=ib, account_id="U1")
    assert await client.tickle() is True


@pytest.mark.asyncio
async def test_connect_when_disconnected_then_calls_connect() -> None:
    ib = _FakeIb(connected=False)
    client = IBKRClient(ib=ib, host="h", port=1234, account_id="U1")
    assert await client.tickle() is True
    assert len(ib.connect_calls) == 1
    args, kwargs = ib.connect_calls[0]
    assert args == ("h", 1234)
    assert kwargs["readonly"] is True
    assert kwargs["account"] == "U1"


@pytest.mark.asyncio
async def test_connect_failure_classified() -> None:
    ib = _FakeIb(connected=False, connect_raises=Exception("auth failed"))
    client = IBKRClient(ib=ib)
    with pytest.raises(PermanentError):
        await client.tickle()


@pytest.mark.asyncio
async def test_connect_unsuccessful_raises_transient() -> None:
    class _StubIb(_FakeIb):
        def connect(self, *args: Any, **kwargs: Any) -> None:
            self.connect_calls.append((args, kwargs))
            # Stays disconnected.

    ib = _StubIb(connected=False)
    client = IBKRClient(ib=ib)
    with pytest.raises(TransientError):
        await client.tickle()


@pytest.mark.asyncio
async def test_fetch_positions_maps_attributes() -> None:
    contract = _Obj(localSymbol="AAPL", primaryExchange="NASDAQ", currency="USD")
    row = _Obj(
        account="U1",
        contract=contract,
        position=10,
        avgCost=150,
        marketPrice=200,
        marketValue=2000,
        unrealizedPNL=500,
    )
    ib = _FakeIb(positions_rows=[row])
    client = IBKRClient(ib=ib, account_id="U1")
    rows = await client.fetch_positions()
    assert rows[0]["contractDesc"] == "AAPL"
    assert rows[0]["currency"] == "USD"


@pytest.mark.asyncio
async def test_fetch_positions_falls_back_to_symbol_and_exchange() -> None:
    contract = _Obj(symbol="TSLA", exchange="NASDAQ", currency="USD")
    row = _Obj(account="U1", contract=contract, position=1, avgCost=1)
    ib = _FakeIb(positions_rows=[row])
    client = IBKRClient(ib=ib)
    rows = await client.fetch_positions()
    assert rows[0]["contractDesc"] == "TSLA"
    assert rows[0]["listingExchange"] == "NASDAQ"


@pytest.mark.asyncio
async def test_fetch_positions_classifies_errors() -> None:
    ib = _FakeIb(positions_raises=Exception("timeout"))
    client = IBKRClient(ib=ib)
    with pytest.raises(TransientError):
        await client.fetch_positions()


@pytest.mark.asyncio
async def test_fetch_account_summary_filters_tags_and_blank_currency() -> None:
    rows_in = [
        _Obj(tag="CashBalance", currency="USD", value="1000", account="U1"),
        _Obj(tag="CashBalance", currency="HKD", value="2000", account="U1"),
        _Obj(tag="UnusedTag", currency="USD", value="9", account="U1"),
        _Obj(tag="CashBalance", currency="", value="3", account="U1"),
        _Obj(tag="CashBalance", currency="EUR", value="", account="U1"),
    ]
    ib = _FakeIb(summary_rows=rows_in)
    client = IBKRClient(ib=ib)
    rows_out = await client.fetch_account_summary()
    assert len(rows_out) == 2
    assert {r["currency"] for r in rows_out} == {"USD", "HKD"}


@pytest.mark.asyncio
async def test_fetch_account_summary_drops_derived_total_and_base_rows() -> None:
    # `TotalCashValue` is IBKR's derived sum in the account base currency,
    # and the synthetic "BASE" currency row mirrors that same total. Either
    # one kept alongside the per-currency `CashBalance` rows double-counts
    # the cash, which would inflate every allocation percentage that
    # divides by total value (spec §8.3, §9.2).
    rows_in = [
        _Obj(tag="CashBalance", currency="USD", value="1000", account="U1"),
        _Obj(tag="TotalCashValue", currency="USD", value="7777", account="U1"),
        _Obj(tag="CashBalance", currency="BASE", value="8888", account="U1"),
    ]
    client = IBKRClient(ib=_FakeIb(summary_rows=rows_in))
    rows_out = await client.fetch_account_summary()
    assert rows_out == [{"acctId": "U1", "currency": "USD", "cashBalance": "1000"}]


@pytest.mark.asyncio
async def test_fetch_executions_maps_filters_sorts_and_limits() -> None:
    contract = _Obj(localSymbol="AAPL", currency="USD")
    now = datetime.now(UTC)
    exec1 = _Obj(
        acctNumber="U1", execId="e1", side="BOT", shares=5, price=100,
        time=now.replace(microsecond=0) - timedelta(days=2),
    )
    exec2 = _Obj(
        acctNumber="U1", execId="e2", side="SLD", shares=3, price=110,
        time=now.replace(microsecond=0) - timedelta(days=1),
    )
    trade = _Obj(
        contract=contract,
        fills=[
            _Obj(execution=exec2),
            _Obj(execution=exec1),
            _Obj(execution=None),  # filtered
        ],
    )
    ib = _FakeIb(trades_rows=[trade])
    client = IBKRClient(ib=ib)

    since = (now - timedelta(days=1, hours=12)).isoformat().replace("+00:00", "Z")
    rows = await client.fetch_executions(since=since, limit=None)
    assert [r["execId"] for r in rows] == ["e2"]

    rows = await client.fetch_executions(since=None, limit=1)
    assert [r["execId"] for r in rows] == ["e2"]  # sort ascending, take last


@pytest.mark.asyncio
async def test_fetch_executions_applies_90d_default_window_when_since_absent() -> None:
    contract = _Obj(localSymbol="AAPL", currency="USD")
    trade = _Obj(
        contract=contract,
        fills=[
            _Obj(
                execution=_Obj(
                    acctNumber="U1",
                    execId="recent",
                    side="BOT",
                    shares=1,
                    price=1,
                    time=datetime.now(UTC) - timedelta(days=5),
                ),
            ),
            _Obj(
                execution=_Obj(
                    acctNumber="U1",
                    execId="old",
                    side="BOT",
                    shares=1,
                    price=1,
                    time=datetime.now(UTC) - timedelta(days=120),
                ),
            ),
        ],
    )
    ib = _FakeIb(trades_rows=[trade])
    client = IBKRClient(ib=ib)

    rows = await client.fetch_executions(since=None, limit=None)
    assert [r["execId"] for r in rows] == ["recent"]


@pytest.mark.asyncio
async def test_fetch_executions_classifies_errors() -> None:
    ib = _FakeIb(trades_raises=Exception("permission denied"))
    client = IBKRClient(ib=ib)
    with pytest.raises(PermanentError):
        await client.fetch_executions(since=None, limit=None)


@pytest.mark.asyncio
async def test_fetch_account_summary_classifies_errors() -> None:
    class _ErroringIb(_FakeIb):
        def accountSummary(self, _account: str) -> list[Any]:  # noqa: N802
            raise Exception("rate limit")

    ib = _ErroringIb()
    client = IBKRClient(ib=ib)
    with pytest.raises(TransientError):
        await client.fetch_account_summary()


@pytest.mark.asyncio
async def test_stream_market_data_yields_quotes(stub_ib_insync: None) -> None:
    class _Ticker:
        def __init__(self, symbol: str, price: float, currency: str = "USD") -> None:
            self.contract = _Obj(symbol=symbol, currency=currency)
            self._price = price

        def marketPrice(self) -> float:  # noqa: N802 - SDK API
            return self._price

    class _NanTicker(_Ticker):
        def marketPrice(self) -> float:  # noqa: N802
            return float("nan")

    ib = _FakeIb(tickers=[_Ticker("AAPL", 200.0), _NanTicker("TSLA", 0.0)])
    client = IBKRClient(ib=ib)
    gen = client.stream_market_data(["AAPL", "TSLA"])
    first = await anext(gen)
    assert first["symbol"] == "AAPL"
    assert first["price"] == "200.0"
    await gen.aclose()
    assert ib.req_mkt_data_calls


@pytest.mark.asyncio
async def test_stream_market_data_classifies_errors(stub_ib_insync: None) -> None:
    ib = _FakeIb(tickers_raises=Exception("rate limit"))
    client = IBKRClient(ib=ib)
    with pytest.raises(TransientError):
        gen = client.stream_market_data(["AAPL"])
        await anext(gen)
