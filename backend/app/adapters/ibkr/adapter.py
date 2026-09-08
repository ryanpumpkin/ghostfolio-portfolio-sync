"""IBKR Client Portal Gateway adapter.

The adapter talks to a co-located gateway through an injected `IbkrClient`
Protocol. For production, `IBKRClient` wraps `ib_insync` and connects to the
gateway host/port configured via `MBP_IB_GATEWAY_HOST` /
`MBP_IB_GATEWAY_PORT`.

A keep-alive ping loop is exposed because IBKR sessions can expire after
periods of inactivity (detailed-design §4.3 / §7.2).
"""

from __future__ import annotations

import asyncio
import importlib
import math
import os
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol

from app.adapters._common import (
    HealthTracker,
    PermanentError,
    RetryPolicy,
    TransientError,
    retry_async,
)
from app.adapters.base import SourceAdapter
from app.models.domain import (
    CashBalance,
    Position,
    Quote,
    SourceHealth,
    Transaction,
)

SOURCE_NAME = "ibkr"
_DEFAULT_TX_WINDOW_DAYS = 90


class IbkrClient(Protocol):
    """Thin wrapper around the CP Gateway HTTP/WS endpoints."""

    async def tickle(self) -> bool: ...

    async def fetch_positions(self) -> list[dict[str, Any]]: ...

    async def fetch_account_summary(self) -> list[dict[str, Any]]: ...

    async def fetch_executions(
        self, *, since: str | None, limit: int | None
    ) -> list[dict[str, Any]]: ...

    def stream_market_data(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]: ...


def _first_finite(*values: Any) -> float | None:
    """Return the first numeric value that's a real, positive finite float.

    ib_insync surfaces missing prices as `NaN` (especially `last` outside
    market hours), so a plain `if x is not None` lets NaN slip through
    and corrupts every downstream multiplication. IBKR also uses 0.0 as the
    "no last trade" sentinel and -1.0 as the "no bid/ask" sentinel, both of
    which are finite but not real prices — accepting them makes `last=0.0`
    win over a valid `close`. Filter out None, NaN/Infinity, and any
    non-positive value in one pass so price selection falls through to
    `close` when there's no live quote.
    """
    for v in values:
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f) and f > 0:
            return f
    return None


def _mid(bid: Any, ask: Any) -> float | None:
    b = _first_finite(bid)
    a = _first_finite(ask)
    if b is None or a is None:
        return None
    return (b + a) / 2.0


def _classify_ibkr_error(exc: Exception) -> Exception:
    message = str(exc).lower()
    permanent_markers = (
        "auth",
        "credential",
        "login",
        "permission",
        "not connected",
        "invalid account",
    )
    transient_markers = (
        "timeout",
        "temporarily",
        "try again",
        "connection reset",
        "rate limit",
        "too many requests",
    )
    if any(marker in message for marker in permanent_markers):
        return PermanentError(str(exc))
    if any(marker in message for marker in transient_markers):
        return TransientError(str(exc))
    return TransientError(str(exc))


# Module-level cache for live IB connections, keyed by (host, port,
# client_id). The aggregator runs positions and balances concurrently via
# `asyncio.gather`, each through its own freshly-built IBKRClient. Without
# this cache, both clients call `connectAsync` at the same time against
# the gateway with the same clientId — ib_insync's internal Futures from
# the racing connections end up bound to different sub-tasks of the same
# loop, surfacing as "got Future attached to a different loop". The lock
# serializes connects so only one IB instance is built per gateway, and
# ib_insync's persistent-connection model is honored (one long-lived
# session per process instead of disconnect-per-request).
_IB_CACHE: dict[tuple[str, int, int], Any] = {}
_IB_CACHE_LOCK = asyncio.Lock()


class IBKRClient:
    """`ib_insync` wrapper for IBKR gateway sidecar calls.

    The wrapper normalizes `accountSummary()`, `positions()`, and `trades()`
    responses to dict payloads consumed by `IbkrAdapter` map functions.
    """

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        client_id: int = 1,
        account_id: str | None = None,
        connect_timeout: float = 10.0,
        ib: Any | None = None,
    ) -> None:
        self._host = host or os.getenv("MBP_IB_GATEWAY_HOST", "localhost")
        self._port = port or int(os.getenv("MBP_IB_GATEWAY_PORT", "5000"))
        self._client_id = client_id
        self._account_id = account_id
        self._connect_timeout = connect_timeout
        # `ib` is only supplied by tests (they pass a fake). For real use
        # the IB instance is resolved through `_IB_CACHE` in `_connect`
        # so concurrent IBKRClients share one persistent connection.
        self._ib: Any | None = ib
        self._ib_injected = ib is not None

    def _ensure_ib(self) -> Any:
        if self._ib is None:  # pragma: no cover - exercises real ib_insync import, covered by env-gated integration test
            try:
                ib_insync_mod = importlib.import_module("ib_insync")
            except ModuleNotFoundError as exc:
                raise PermanentError("ib_insync is not installed") from exc
            ib_cls = getattr(ib_insync_mod, "IB", None)
            if ib_cls is None:
                raise PermanentError("ib_insync is not installed")
            self._ib = ib_cls()
        return self._ib

    async def _connect(self) -> Any:
        # Tests inject a fake IB and bypass the shared cache.
        if self._ib_injected:
            ib = self._ib
            assert ib is not None
            if bool(ib.isConnected()):
                return ib
            try:
                ib.connect(
                    self._host,
                    self._port,
                    clientId=self._client_id,
                    timeout=self._connect_timeout,
                    readonly=True,
                    account=self._account_id or "",
                )
            except Exception as exc:  # noqa: BLE001 - normalized below
                raise _classify_ibkr_error(exc) from exc
            if not bool(ib.isConnected()):
                raise TransientError("IBKR gateway connection failed")
            return ib

        # Production path: share a single persistent IB instance per
        # gateway across concurrent IBKRClients (see _IB_CACHE comment).
        #
        # ib_insync's `Connection.connectAsync` resolves its event loop via
        # `asyncio.get_event_loop_policy().get_event_loop()` (in its
        # `util.getLoop`), NOT `asyncio.get_running_loop()`. Uvicorn
        # creates its loop without calling `asyncio.set_event_loop`, so
        # the policy hands ib_insync a *different* loop than the one our
        # task is actually running on — every Future the ib_insync
        # internals create then trips "got Future attached to a different
        # loop". Pin the policy loop here.
        running_loop = asyncio.get_running_loop()
        try:
            policy_loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            policy_loop = None
        if policy_loop is not running_loop:
            asyncio.set_event_loop(running_loop)

        key = (self._host, self._port, self._client_id)
        async with _IB_CACHE_LOCK:
            ib = _IB_CACHE.get(key)
            if ib is not None and bool(ib.isConnected()):
                self._ib = ib
                return ib

            # Stale or first-time entry — (re)create and connect.
            self._ib = None
            ib = self._ensure_ib()
            try:
                # Use ib_insync's async API directly on the calling event
                # loop. The sync `ib.connect(...)` lazy-calls
                # `asyncio.get_event_loop()` internally, which raises in a
                # worker thread on Python 3.10+, so wrapping it in
                # `asyncio.to_thread` (as we used to) breaks under the
                # FastAPI executor.
                await ib.connectAsync(
                    self._host,
                    self._port,
                    clientId=self._client_id,
                    timeout=self._connect_timeout,
                    readonly=True,
                    account=self._account_id or "",
                )
            except Exception as exc:  # noqa: BLE001 - normalized below
                _IB_CACHE.pop(key, None)
                raise _classify_ibkr_error(exc) from exc
            if not bool(ib.isConnected()):
                _IB_CACHE.pop(key, None)
                raise TransientError("IBKR gateway connection failed")

            # Allow delayed/frozen market data for accounts that don't
            # have live-quote entitlements (most retail accounts). This
            # is a no-op when live data is available. 3 = delayed,
            # 4 = delayed-frozen; either yields a usable last-price for
            # the dashboard's market-value computation.
            req_market_data_type = getattr(ib, "reqMarketDataType", None)
            if req_market_data_type is not None:
                try:
                    req_market_data_type(3)
                except Exception:  # noqa: BLE001
                    pass

            _IB_CACHE[key] = ib
            return ib

    async def tickle(self) -> bool:
        ib = await self._connect()
        return bool(ib.isConnected())

    async def fetch_positions(self) -> list[dict[str, Any]]:
        ib = await self._connect()
        try:
            # `ib.positions()` is auto-populated by connectAsync's
            # `reqPositionsAsync` and works across multi-account
            # gateways. It carries account/contract/quantity/avgCost
            # but NOT market data — we fill that in below via
            # `reqTickersAsync`.
            #
            # We deliberately avoid `ib.portfolio()` / single-account
            # `reqAccountUpdates`: when the gateway has multiple managed
            # accounts, `connectAsync` has already subscribed via
            # multi-account `reqAccountUpdatesMulti`, which conflicts
            # with the legacy single-account endpoint — `reqAccountUpdates`
            # never receives `accountDownloadEnd` and hangs forever.
            rows = ib.positions(self._account_id or "")
        except Exception as exc:  # noqa: BLE001
            raise _classify_ibkr_error(exc) from exc

        # Filter forex/CASH and any junk rows before fetching tickers —
        # forex pairs aren't securities and have no useful market quote
        # to fetch.
        keepers: list[Any] = []
        for row in rows:
            contract = getattr(row, "contract", None)
            sec_type = str(getattr(contract, "secType", "")).upper()
            # IBKR reports forex pairs (e.g. USD.HKD, USD.CNH) as
            # positions whenever an auto-conversion or FX trade happens.
            # They represent currency exposure, not a security — they
            # belong on the Cash card, not the positions list. They also
            # pollute the FX-pair set the portfolio aggregator builds,
            # which is how the dashboard was 500'ing on Frankfurter's
            # "CNH not supported" 404.
            if sec_type == "CASH":
                continue
            if contract is None:
                continue
            keepers.append(row)

        # Best-effort: pull a snapshot quote per contract so the
        # dashboard can show market value + unrealized P&L. Tickers may
        # legitimately be missing (no market-data entitlement for a
        # contract, weekend close, etc.) — fall back to NaN/None and
        # let the UI render "—".
        prices: dict[int, Any] = {}
        req_tickers = getattr(ib, "reqTickersAsync", None)
        if req_tickers is not None and keepers:
            contracts = [r.contract for r in keepers]
            try:
                # `reqTickersAsync` issues snapshot market-data requests and
                # only resolves once every contract emits `tickSnapshotEnd`.
                # On a delayed-data account (no live entitlement) IBKR drips
                # those out slowly — measured ~13 s for a handful of US
                # equities — so a tight timeout silently drops every price
                # and the dashboard renders $0.00. Give it real headroom;
                # the aggregator already tolerates this call running long.
                tickers = await asyncio.wait_for(
                    req_tickers(*contracts, regulatorySnapshot=False),
                    timeout=25.0,
                )
                for ticker in tickers or []:
                    contract = getattr(ticker, "contract", None)
                    if contract is None:
                        continue
                    # Prefer last trade, then close, then mid of bid/ask.
                    price = _first_finite(
                        getattr(ticker, "last", None),
                        getattr(ticker, "close", None),
                        _mid(getattr(ticker, "bid", None), getattr(ticker, "ask", None)),
                        getattr(ticker, "marketPrice", lambda: None)()
                        if callable(getattr(ticker, "marketPrice", None))
                        else getattr(ticker, "marketPrice", None),
                    )
                    if price is not None:
                        prices[getattr(contract, "conId", 0)] = price
            except Exception:  # noqa: BLE001 - dashboard tolerates missing prices
                pass

        out: list[dict[str, Any]] = []
        for row in keepers:
            contract = row.contract
            quantity_raw = getattr(row, "position", 0)
            try:
                quantity = float(quantity_raw)
            except (TypeError, ValueError):
                quantity = 0.0
            avg_cost_raw = getattr(row, "avgCost", None)
            if avg_cost_raw is None:
                avg_cost_raw = getattr(row, "averageCost", "")
            try:
                avg_cost = float(avg_cost_raw)
            except (TypeError, ValueError):
                avg_cost = None

            con_id = getattr(contract, "conId", 0)
            market_price = prices.get(con_id)
            market_value = (
                market_price * quantity
                if market_price is not None
                else None
            )
            unrealized_pnl = (
                market_value - avg_cost * quantity
                if market_value is not None and avg_cost is not None
                else None
            )

            out.append(
                {
                    "acctId": getattr(row, "account", None),
                    "contractDesc": getattr(contract, "localSymbol", None)
                    or getattr(contract, "symbol", None),
                    "listingExchange": getattr(contract, "primaryExchange", None)
                    or getattr(contract, "exchange", None),
                    "position": str(quantity_raw),
                    "avgCost": str(avg_cost_raw) if avg_cost_raw not in (None, "") else "",
                    "mktPrice": "" if market_price is None else str(market_price),
                    "mktValue": "" if market_value is None else str(market_value),
                    "unrealizedPnl": ""
                    if unrealized_pnl is None
                    else str(unrealized_pnl),
                    "currency": getattr(contract, "currency", "USD"),
                }
            )
        return out

    async def fetch_account_summary(self) -> list[dict[str, Any]]:
        ib = await self._connect()
        try:
            # Prefer `accountValues()` over `accountSummary()`:
            #   * accountValues is auto-populated by `connectAsync`
            #     (StartupFetch.ACCOUNT_UPDATES), so it's a cache read with
            #     no extra round trip.
            #   * accountSummary lazy-issues a fresh subscription via
            #     `util.run(reqAccountSummary…)` — this both deadlocks on
            #     the running loop AND leaks subscriptions at the gateway
            #     (IBKR limits us to one active account-summary stream).
            # Both surfaces return rows with the same shape (tag/currency/
            # value), so the filter logic below is unchanged. Skip the
            # synthetic "BASE" currency that IBKR appends for the
            # base-currency total.
            account_values = getattr(ib, "accountValues", None)
            if account_values is not None:
                rows = account_values(self._account_id or "")
            else:
                rows = ib.accountSummary(self._account_id or "")
        except Exception as exc:  # noqa: BLE001
            raise _classify_ibkr_error(exc) from exc

        out: list[dict[str, Any]] = []
        for row in rows:
            tag = str(getattr(row, "tag", ""))
            # `TotalCashValue` is a derived sum in the account base
            # currency — keeping it alongside per-currency `CashBalance`
            # rows double-counts in the UI. LongBridge/Futu only emit
            # per-currency cash, so we match that.
            if tag != "CashBalance":
                continue
            # IBKR emits a synthetic "BASE" currency row that mirrors the
            # base-currency total — skip it for the same reason.
            if str(getattr(row, "currency", "")).strip().upper() == "BASE":
                continue
            currency = str(getattr(row, "currency", "")).strip()
            value = str(getattr(row, "value", "")).strip()
            if not currency or not value:
                continue
            out.append(
                {
                    "acctId": getattr(row, "account", None),
                    "currency": currency,
                    "cashBalance": value,
                }
            )
        return out

    async def fetch_executions(
        self, *, since: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        ib = await self._connect()
        try:
            trades = ib.trades()
        except Exception as exc:  # noqa: BLE001
            raise _classify_ibkr_error(exc) from exc

        out: list[dict[str, Any]] = []
        for trade in trades:
            contract = getattr(trade, "contract", None)
            fills = getattr(trade, "fills", None) or []
            for fill in fills:
                execution = getattr(fill, "execution", None)
                if execution is None:
                    continue
                out.append(
                    {
                        "acctId": getattr(execution, "acctNumber", None),
                        "execId": getattr(execution, "execId", None),
                        "symbol": getattr(contract, "localSymbol", None)
                        or getattr(contract, "symbol", None),
                        "side": getattr(execution, "side", None),
                        "size": str(getattr(execution, "shares", "")),
                        "price": str(getattr(execution, "price", "")),
                        "currency": getattr(contract, "currency", None),
                        "net_amount": None,
                        "time": getattr(execution, "time", None),
                    }
                )

        since_dt = _since_or_default(since)
        out = [row for row in out if _parse_ts(row["time"]) >= since_dt]
        out.sort(key=lambda row: _parse_ts(row["time"]))
        if limit is not None and limit >= 0:
            out = out[-limit:]
        return out

    async def stream_market_data(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        ib = await self._connect()
        try:
            ib_insync_mod = importlib.import_module("ib_insync")
        except ModuleNotFoundError as exc:  # pragma: no cover - env/setup issue
            raise PermanentError("ib_insync is not installed") from exc
        stock_cls = getattr(ib_insync_mod, "Stock", None)
        if stock_cls is None:  # pragma: no cover - env/setup issue
            raise PermanentError("ib_insync is not installed")

        if not symbols:
            return

        contracts = [stock_cls(symbol, "SMART", "USD") for symbol in symbols]
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        stop_event = threading.Event()

        def _to_payload(ticker: Any) -> dict[str, Any] | None:
            contract = getattr(ticker, "contract", None)
            symbol = getattr(contract, "symbol", None)
            if symbol is None:
                return None
            price = getattr(ticker, "marketPrice", lambda: None)()
            if price is None or price != price:  # NaN guard
                return None
            return {
                "symbol": symbol,
                "price": str(price),
                "currency": getattr(contract, "currency", "USD"),
                "timestamp": datetime.now(UTC),
            }

        def _pump() -> None:
            listener = getattr(ib, "pendingTickersEvent", None)

            def _on_pending(tickers: list[Any]) -> None:
                for ticker in tickers:
                    payload = _to_payload(ticker)
                    if payload is None:
                        continue
                    loop.call_soon_threadsafe(queue.put_nowait, payload)

            try:
                for contract in contracts:
                    ib.reqMktData(contract, "", False, False)
                if listener is not None:
                    listener += _on_pending
                while not stop_event.is_set():
                    ib.waitOnUpdate(timeout=1)
            except Exception as exc:  # noqa: BLE001 - normalized on async side
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    {"__error__": str(_classify_ibkr_error(exc))},
                )
            finally:
                if listener is not None:
                    try:
                        listener -= _on_pending
                    except Exception:
                        pass
                for contract in contracts:
                    try:
                        ib.cancelMktData(contract)
                    except Exception:
                        pass

        pump_task = asyncio.create_task(asyncio.to_thread(_pump))
        try:
            while True:
                payload = await queue.get()
                error = payload.get("__error__")
                if isinstance(error, str) and error:
                    raise _classify_ibkr_error(RuntimeError(error))
                yield payload
        except asyncio.CancelledError:
            raise
        finally:
            stop_event.set()
            pump_task.cancel()
            await asyncio.gather(pump_task, return_exceptions=True)


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def _opt_dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(float(value), tz=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _map_position(raw: dict[str, Any]) -> Position:
    return Position(
        source=SOURCE_NAME,
        account_id=raw.get("acctId") or raw.get("account_id"),
        symbol=raw["contractDesc"] if "contractDesc" in raw else raw["symbol"],
        exchange=raw.get("listingExchange") or raw.get("exchange"),
        quantity=_dec(raw["position"]),
        avg_cost=_opt_dec(raw.get("avgCost") or raw.get("avg_cost")),
        last_price=_opt_dec(raw.get("mktPrice")),
        currency=raw["currency"],
        market_value=_opt_dec(raw.get("mktValue")),
        unrealized_pnl=_opt_dec(raw.get("unrealizedPnl")),
    )


def _map_balance(raw: dict[str, Any]) -> CashBalance:
    return CashBalance(
        source=SOURCE_NAME,
        account_id=raw.get("acctId") or raw.get("account_id"),
        currency=raw["currency"],
        amount=_dec(raw["cashBalance"] if "cashBalance" in raw else raw["amount"]),
    )


def _map_transaction(raw: dict[str, Any]) -> Transaction:
    side_raw = raw.get("side")
    side = side_raw.lower() if isinstance(side_raw, str) else None
    return Transaction(
        source=SOURCE_NAME,
        account_id=raw.get("acctId") or raw.get("account_id"),
        transaction_id=str(raw.get("execId") or raw["transaction_id"]),
        symbol=raw.get("symbol"),
        side=side,
        quantity=_opt_dec(raw.get("size") or raw.get("quantity")),
        price=_opt_dec(raw.get("price")),
        currency=raw.get("currency"),
        amount=_opt_dec(raw.get("net_amount") or raw.get("amount")),
        timestamp=_parse_ts(raw.get("time") or raw["timestamp"]),
    )


def _map_quote(raw: dict[str, Any]) -> Quote:
    return Quote(
        source=SOURCE_NAME,
        symbol=raw["symbol"],
        price=_dec(raw.get("last") or raw["price"]),
        currency=raw["currency"],
        timestamp=_parse_ts(raw.get("t") or raw["timestamp"]),
    )


def _since_or_default(since: str | None) -> datetime:
    if since is None:
        return datetime.now(UTC) - timedelta(days=_DEFAULT_TX_WINDOW_DAYS)
    return _parse_ts(since)


class IbkrAdapter(SourceAdapter):
    """IBKR adapter."""

    source = SOURCE_NAME

    def __init__(
        self,
        client: IbkrClient,
        *,
        retry: RetryPolicy | None = None,
        health: HealthTracker | None = None,
        keepalive_interval: float = 60.0,
    ) -> None:
        self._client = client
        self._retry = retry or RetryPolicy()
        self._health = health or HealthTracker(source=SOURCE_NAME)
        self._keepalive_interval = keepalive_interval
        self._keepalive_task: asyncio.Task[None] | None = None

    async def _call(self, func: Callable[[], Awaitable[Any]]) -> Any:
        try:
            result = await retry_async(func, policy=self._retry)
        except Exception as exc:
            self._health.record_failure(str(exc))
            raise
        self._health.record_success()
        return result

    async def _tickle_before_request(self) -> None:
        async def _tickle() -> None:
            ok = await self._client.tickle()
            if not ok:
                raise TransientError("tickle returned false")

        await self._call(_tickle)

    async def list_positions(self) -> list[Position]:
        await self._tickle_before_request()
        raw = await self._call(self._client.fetch_positions)
        return [_map_position(item) for item in raw]

    async def list_balances(self) -> list[CashBalance]:
        await self._tickle_before_request()
        raw = await self._call(self._client.fetch_account_summary)
        return [_map_balance(item) for item in raw]

    async def list_transactions(
        self,
        *,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        async def _do() -> list[dict[str, Any]]:
            return await self._client.fetch_executions(since=since, limit=limit)

        await self._tickle_before_request()
        raw = await self._call(_do)
        return [_map_transaction(item) for item in raw]

    async def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        await self._tickle_before_request()
        async for raw in self._client.stream_market_data(list(symbols)):
            yield _map_quote(raw)

    async def healthcheck(self) -> SourceHealth:
        try:
            ok = await self._client.tickle()
            if ok:
                self._health.record_success()
            else:
                self._health.record_failure("tickle returned false")
        except Exception as exc:  # noqa: BLE001
            self._health.record_failure(str(exc))
        return self._health.snapshot()

    async def _keepalive_loop(
        self,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        while True:
            try:
                await self._client.tickle()
            except Exception as exc:  # noqa: BLE001 - keepalive must keep looping
                self._health.record_failure(str(exc))
            await sleep(self._keepalive_interval)

    def start_keepalive(self) -> asyncio.Task[None]:
        """Spawn the CP Gateway tickle loop."""
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        return self._keepalive_task

    async def stop_keepalive(self) -> None:
        task = self._keepalive_task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._keepalive_task = None
