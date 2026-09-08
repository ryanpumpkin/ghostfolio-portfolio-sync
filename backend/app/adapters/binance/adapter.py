"""Binance adapter (binance.com + binance.us).

Read-only API key + secret, HMAC-SHA256 signed REST calls and a
WebSocket mini-ticker stream. We deliberately reject keys that have
trade or withdraw permissions at connect time as a defence-in-depth
sanity check (detailed-design §4.3, task spec).

The `BinanceClient` Protocol is the SDK boundary — production wires up
`HttpxBinanceClient` against the live host, tests inject a fake.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import urllib.parse
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, cast

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

SOURCE_NAME = "binance"
_STABLE_ASSETS = {"USDT", "USDC", "BUSD", "USD"}
_MAX_TRADE_SYMBOLS = 20
_MAX_MY_TRADES_PER_CALL = 1000


class BinanceHost(StrEnum):
    """Which Binance deployment to talk to."""

    COM = "binance.com"
    US = "binance.us"

    @property
    def rest_base(self) -> str:
        return "https://api.binance.com" if self is BinanceHost.COM else "https://api.binance.us"

    @property
    def ws_base(self) -> str:
        return (
            "wss://stream.binance.com:9443"
            if self is BinanceHost.COM
            else "wss://stream.binance.us:9443"
        )

    @property
    def sdk_tld(self) -> str:
        return "com" if self is BinanceHost.COM else "us"


def sign_query(secret: str, params: dict[str, Any]) -> str:
    """HMAC-SHA256 sign a query-string dict, returning `query&signature=...`."""
    query = urllib.parse.urlencode(params, doseq=True)
    sig = hmac.new(secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{query}&signature={sig}"


@dataclass(slots=True)
class BinanceCredentials:
    api_key: str
    api_secret: str


class BinanceClient(Protocol):
    """SDK boundary for Binance REST + WS calls."""

    host: BinanceHost

    async def get_account(self) -> dict[str, Any]: ...

    async def get_my_trades(
        self,
        *,
        symbol: str | None,
        since: str | None,
        limit: int | None,
    ) -> list[dict[str, Any]]: ...

    async def get_deposit_history(
        self, *, since: str | None
    ) -> list[dict[str, Any]]: ...

    async def get_withdraw_history(
        self, *, since: str | None
    ) -> list[dict[str, Any]]: ...

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[Any]: ...

    async def get_ticker_prices(self, symbols: list[str]) -> list[dict[str, Any]]: ...

    def stream_mini_tickers(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]: ...

    async def ping(self) -> bool: ...

    async def close(self) -> None: ...


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def _opt_dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _ts_ms(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromtimestamp(int(value) / 1000.0, tz=UTC)


def _spot_balance_to_position(
    raw: dict[str, Any],
    *,
    last_price: Decimal | None = None,
    quote_currency: str | None = None,
) -> Position | None:
    """Map a Binance spot balance row to a Position (skipping zero balances)."""
    qty = _dec(raw["free"]) + _dec(raw.get("locked", "0"))
    if qty == 0:
        return None
    asset = raw["asset"]
    market_value = last_price * qty if last_price is not None else None
    return Position(
        source=SOURCE_NAME,
        account_id=None,
        symbol=asset,
        exchange="BINANCE",
        quantity=qty,
        avg_cost=None,
        last_price=last_price,
        currency=quote_currency or asset,
        market_value=market_value,
        unrealized_pnl=None,
    )


def _spot_balance_to_cash(raw: dict[str, Any]) -> CashBalance | None:
    """For stable/fiat assets we also expose a CashBalance row."""
    qty = _dec(raw["free"]) + _dec(raw.get("locked", "0"))
    if qty == 0:
        return None
    return CashBalance(
        source=SOURCE_NAME,
        account_id=None,
        currency=raw["asset"],
        amount=qty,
    )


def _infer_quote_currency(symbol: str) -> str:
    for suffix in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH"):
        if symbol.endswith(suffix):
            return suffix
    return "USD"


def _map_trade(raw: dict[str, Any]) -> Transaction:
    side = "buy" if raw.get("isBuyer", False) else "sell"
    symbol = str(raw.get("symbol", ""))
    quote_qty = _opt_dec(raw.get("quoteQty"))
    qty = _opt_dec(raw.get("qty"))
    price = _opt_dec(raw.get("price"))
    amount = quote_qty
    if amount is None and qty is not None and price is not None:
        amount = qty * price
    quote_currency = raw.get("quoteAsset")
    if not isinstance(quote_currency, str) or not quote_currency:
        quote_currency = _infer_quote_currency(symbol) if symbol else None
    return Transaction(
        source=SOURCE_NAME,
        account_id=None,
        transaction_id=str(raw.get("id") or raw.get("orderId") or raw.get("tradeId")),
        symbol=symbol or None,
        side=side,
        quantity=qty,
        price=price,
        currency=quote_currency,
        amount=amount,
        timestamp=_ts_ms(raw["time"]),
    )


def _map_deposit(raw: dict[str, Any]) -> Transaction:
    return Transaction(
        source=SOURCE_NAME,
        account_id=None,
        transaction_id=str(raw.get("txId") or raw["id"]),
        symbol=None,
        side="deposit",
        quantity=None,
        price=None,
        currency=raw["coin"],
        amount=_opt_dec(raw.get("amount")),
        timestamp=_ts_ms(raw.get("insertTime") or raw["time"]),
    )


def _map_withdrawal(raw: dict[str, Any]) -> Transaction:
    return Transaction(
        source=SOURCE_NAME,
        account_id=None,
        transaction_id=str(raw.get("id") or raw["txId"]),
        symbol=None,
        side="withdrawal",
        quantity=None,
        price=None,
        currency=raw["coin"],
        amount=_opt_dec(raw.get("amount")),
        timestamp=_ts_ms(raw.get("applyTime") or raw["time"]),
    )


def _map_quote(raw: dict[str, Any]) -> Quote:
    symbol = raw.get("s") or raw["symbol"]
    price = raw.get("c") or raw.get("price") or raw["p"]
    ts_value = raw.get("E") or raw.get("time")
    timestamp = _ts_ms(ts_value) if ts_value is not None else datetime.now(UTC)
    # Spot pairs end in USDT/USDC/BUSD/USD — treat the suffix as the quote currency.
    currency = _infer_quote_currency(symbol) if isinstance(symbol, str) else "USD"
    return Quote(
        source=SOURCE_NAME,
        symbol=symbol,
        price=_dec(price),
        currency=currency,
        timestamp=timestamp,
    )


def _trade_timestamp_ms(raw: dict[str, Any]) -> int:
    value = raw.get("time")
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _assert_read_only(account: dict[str, Any]) -> None:
    """Reject keys with trade or withdraw permissions enabled."""
    if account.get("canTrade") or account.get("canWithdraw"):
        raise PermanentError(
            "Binance API key allows trade/withdraw; refuse to use"
        )


class BinanceAdapter(SourceAdapter):
    """Binance spot adapter (works against `.com` or `.us`)."""

    source = SOURCE_NAME

    def __init__(
        self,
        client: BinanceClient,
        *,
        retry: RetryPolicy | None = None,
        health: HealthTracker | None = None,
    ) -> None:
        self._client = client
        self._retry = retry or RetryPolicy()
        self._health = health or HealthTracker(source=SOURCE_NAME)
        self._verified_read_only = False

    @property
    def host(self) -> BinanceHost:
        return self._client.host

    async def _call(self, func: Callable[[], Awaitable[Any]]) -> Any:
        try:
            result = await retry_async(func, policy=self._retry)
        except Exception as exc:
            self._health.record_failure(str(exc))
            raise
        self._health.record_success()
        return result

    async def verify_read_only(self) -> None:
        account = await self._call(self._client.get_account)
        _assert_read_only(account)
        self._verified_read_only = True

    async def _get_verified_account(self) -> dict[str, Any]:
        account = cast(dict[str, Any], await self._call(self._client.get_account))
        _assert_read_only(account)
        self._verified_read_only = True
        return account

    async def _ensure_verified(self) -> None:
        if self._verified_read_only:
            return
        await self.verify_read_only()

    async def _price_from_klines(
        self, *, asset: str
    ) -> tuple[Decimal | None, str | None]:
        for quote_currency in ("USDT", "USD"):
            symbol = f"{asset}{quote_currency}"
            async def _fetch_klines(symbol_for_call: str = symbol) -> list[Any]:
                return await self._client.get_klines(
                    symbol=symbol_for_call,
                    interval="1m",
                    limit=1,
                )
            try:
                klines = cast(list[Any], await self._call(_fetch_klines))
            except PermanentError:
                continue
            if not klines:
                continue
            row = klines[-1]
            if isinstance(row, Sequence) and len(row) >= 5:
                return _dec(row[4]), quote_currency
            if isinstance(row, dict) and "close" in row:
                return _dec(row["close"]), quote_currency
        return None, None

    @staticmethod
    def _nonzero_balances(account: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for row in account.get("balances", []):
            if not isinstance(row, dict):
                continue
            qty = _dec(row.get("free", "0")) + _dec(row.get("locked", "0"))
            if qty > 0:
                out.append(row)
        return out

    async def _price_lookup_for_account(
        self,
        account: dict[str, Any],
    ) -> dict[str, tuple[Decimal, str]]:
        prices: dict[str, tuple[Decimal, str]] = {}
        symbols: list[str] = []
        asset_by_symbol: dict[str, str] = {}
        for row in self._nonzero_balances(account):
            asset = row.get("asset")
            if not isinstance(asset, str) or asset in _STABLE_ASSETS:
                continue
            symbol = f"{asset}USDT"
            if symbol in asset_by_symbol:
                continue
            symbols.append(symbol)
            asset_by_symbol[symbol] = asset

        if symbols:
            async def _fetch_prices() -> list[dict[str, Any]]:
                return await self._client.get_ticker_prices(symbols)

            for row in cast(list[dict[str, Any]], await self._call(_fetch_prices)):
                symbol_raw = row.get("symbol")
                price = row.get("price")
                if not isinstance(symbol_raw, str) or price is None:
                    continue
                asset = asset_by_symbol.get(symbol_raw)
                if asset is None:
                    continue
                prices[asset] = (_dec(price), _infer_quote_currency(symbol_raw))

        # Fallback for symbols that don't have a direct ticker quote.
        for row in self._nonzero_balances(account):
            asset = row.get("asset")
            if not isinstance(asset, str) or asset in _STABLE_ASSETS or asset in prices:
                continue
            price, quote_currency = await self._price_from_klines(asset=asset)
            if price is not None:
                prices[asset] = (price, quote_currency or "USD")
        return prices

    @staticmethod
    def _default_since_ms(now: datetime | None = None) -> str:
        at = now if now is not None else datetime.now(UTC)
        return str(int((at - timedelta(days=90)).timestamp() * 1000))

    def _trade_symbols_for_account(self, account: dict[str, Any]) -> list[str]:
        symbols: list[str] = []
        seen: set[str] = set()
        for row in self._nonzero_balances(account):
            asset = row.get("asset")
            if not isinstance(asset, str) or asset in _STABLE_ASSETS:
                continue
            symbol = f"{asset}USDT"
            if symbol in seen:
                continue
            symbols.append(symbol)
            seen.add(symbol)
            if len(symbols) >= _MAX_TRADE_SYMBOLS:
                break
        return symbols

    async def list_positions(self) -> list[Position]:
        account = await self._get_verified_account()
        prices = await self._price_lookup_for_account(account)
        out: list[Position] = []
        for row in account.get("balances", []):
            price: Decimal | None = None
            price_currency: str | None = None
            asset = row.get("asset")
            if isinstance(asset, str):
                priced = prices.get(asset)
                if priced is not None:
                    price, price_currency = priced
            pos = _spot_balance_to_position(
                row,
                last_price=price,
                quote_currency=price_currency,
            )
            if pos is not None:
                out.append(pos)
        return out

    async def list_balances(self) -> list[CashBalance]:
        account = await self._get_verified_account()
        out: list[CashBalance] = []
        for row in account.get("balances", []):
            bal = _spot_balance_to_cash(row)
            if bal is not None:
                out.append(bal)
        return out

    async def list_transactions(
        self,
        *,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        account = await self._get_verified_account()
        since_ms = since if since is not None else self._default_since_ms()
        symbols = self._trade_symbols_for_account(account)
        trades: list[dict[str, Any]] = []
        for symbol in symbols:
            async def _trades_for_symbol(symbol_for_call: str = symbol) -> list[dict[str, Any]]:
                return await self._client.get_my_trades(
                    symbol=symbol_for_call,
                    since=since_ms,
                    limit=limit,
                )
            trades.extend(cast(list[dict[str, Any]], await self._call(_trades_for_symbol)))

        async def _deposits() -> list[dict[str, Any]]:
            return await self._client.get_deposit_history(since=since_ms)

        async def _withdrawals() -> list[dict[str, Any]]:
            return await self._client.get_withdraw_history(since=since_ms)

        deposits = await self._call(_deposits)
        withdrawals = await self._call(_withdrawals)
        out: list[Transaction] = [_map_trade(t) for t in trades]
        out.extend(_map_deposit(d) for d in deposits)
        out.extend(_map_withdrawal(w) for w in withdrawals)
        out.sort(key=lambda tx: tx.timestamp)
        if limit is not None and limit >= 0 and len(out) > limit:
            out = out[-limit:]
        return out

    async def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        await self._ensure_verified()
        async for raw in self._client.stream_mini_tickers(list(symbols)):
            yield _map_quote(raw)

    async def healthcheck(self) -> SourceHealth:
        try:
            ok = await self._client.ping()
            if ok:
                self._health.record_success()
            else:
                self._health.record_failure("ping failed")
        except Exception as exc:  # noqa: BLE001
            self._health.record_failure(str(exc))
        return self._health.snapshot()


class _PythonBinanceAsyncClient(Protocol):
    async def get_account(self, **params: Any) -> Any: ...

    async def get_my_trades(self, **params: Any) -> Any: ...

    async def get_deposit_history(self, **params: Any) -> Any: ...

    async def get_withdraw_history(self, **params: Any) -> Any: ...

    async def get_klines(self, **params: Any) -> Any: ...

    async def get_symbol_ticker(self, **params: Any) -> Any: ...

    async def ping(self) -> Any: ...

    async def close_connection(self) -> Any: ...


class HttpxBinanceClient:  # pragma: no cover - real network wrapper
    """Live Binance client wrapper backed by `python-binance` AsyncClient."""

    def __init__(
        self,
        creds: BinanceCredentials,
        *,
        host: BinanceHost = BinanceHost.COM,
    ) -> None:
        self._creds = creds
        self.host = host
        self._sdk: _PythonBinanceAsyncClient | None = None

    async def _sdk_client(self) -> _PythonBinanceAsyncClient:
        if self._sdk is not None:
            return self._sdk
        try:
            module = importlib.import_module("binance.async_client")
        except ImportError as exc:  # pragma: no cover
            raise PermanentError("python-binance is not installed") from exc
        async_client_cls = getattr(module, "AsyncClient", None)
        if async_client_cls is None:  # pragma: no cover
            raise PermanentError("python-binance AsyncClient is unavailable")
        created = await async_client_cls.create(
            api_key=self._creds.api_key,
            api_secret=self._creds.api_secret,
            tld=self.host.sdk_tld,
        )
        self._sdk = cast(_PythonBinanceAsyncClient, created)
        return self._sdk

    @staticmethod
    def _to_int_ms(value: str | None) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                return None
            return int(parsed.timestamp() * 1000)

    @staticmethod
    def _translate_exception(exc: Exception) -> Exception:
        message = str(exc)
        status_code = getattr(exc, "status_code", None)
        if status_code in (418, 429, 500, 502, 503, 504):
            return TransientError(message)
        return PermanentError(message)

    async def get_account(self) -> dict[str, Any]:
        sdk = await self._sdk_client()
        try:
            response = await sdk.get_account()
        except Exception as exc:
            raise self._translate_exception(exc) from exc
        if not isinstance(response, dict):
            raise PermanentError("Unexpected Binance account response shape")
        return response

    async def get_my_trades(
        self, *, symbol: str | None, since: str | None, limit: int | None
    ) -> list[dict[str, Any]]:
        sdk = await self._sdk_client()
        if symbol is None:
            return []

        end_time = int(datetime.now(UTC).timestamp() * 1000)
        start_time = self._to_int_ms(since)
        if start_time is None:
            start_time = int((datetime.now(UTC) - timedelta(days=90)).timestamp() * 1000)

        out: list[dict[str, Any]] = []
        cursor = start_time
        remaining = limit if limit is not None and limit >= 0 else None
        while cursor <= end_time:
            page_size = _MAX_MY_TRADES_PER_CALL
            if remaining is not None:
                if remaining <= 0:
                    break
                page_size = min(page_size, remaining)
            params: dict[str, Any] = {
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end_time,
                "limit": page_size,
            }
            try:
                response = await sdk.get_my_trades(**params)
            except Exception as exc:
                raise self._translate_exception(exc) from exc
            if not isinstance(response, list):
                raise PermanentError("Unexpected Binance myTrades response shape")
            page = [item for item in response if isinstance(item, dict)]
            if not page:
                break
            out.extend(page)
            if remaining is not None:
                remaining -= len(page)
            if len(page) < page_size:
                break
            last_ts = _trade_timestamp_ms(page[-1])
            cursor = max(cursor + 1, last_ts + 1)

        out.sort(key=_trade_timestamp_ms)
        return out

    async def get_deposit_history(self, *, since: str | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        start_time = self._to_int_ms(since)
        if start_time is not None:
            params["startTime"] = start_time
        sdk = await self._sdk_client()
        try:
            response = await sdk.get_deposit_history(**params)
        except Exception as exc:
            raise self._translate_exception(exc) from exc
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if isinstance(response, dict):
            rows = response.get("depositList", [])
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
        raise PermanentError("Unexpected Binance deposit history response shape")

    async def get_withdraw_history(self, *, since: str | None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        start_time = self._to_int_ms(since)
        if start_time is not None:
            params["startTime"] = start_time
        sdk = await self._sdk_client()
        try:
            response = await sdk.get_withdraw_history(**params)
        except Exception as exc:
            raise self._translate_exception(exc) from exc
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        if isinstance(response, dict):
            rows = response.get("withdrawList", [])
            if isinstance(rows, list):
                return [item for item in rows if isinstance(item, dict)]
        raise PermanentError("Unexpected Binance withdraw history response shape")

    async def get_klines(
        self,
        *,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[Any]:
        sdk = await self._sdk_client()
        try:
            response = await sdk.get_klines(symbol=symbol, interval=interval, limit=limit)
        except Exception as exc:
            raise self._translate_exception(exc) from exc
        if isinstance(response, list):
            return response
        raise PermanentError("Unexpected Binance klines response shape")

    async def get_ticker_prices(self, symbols: list[str]) -> list[dict[str, Any]]:
        sdk = await self._sdk_client()
        if not symbols:
            return []
        out: list[dict[str, Any]] = []
        for symbol in symbols:
            try:
                row = await sdk.get_symbol_ticker(symbol=symbol)
            except Exception as exc:
                raise self._translate_exception(exc) from exc
            if isinstance(row, dict):
                out.append(row)
        return out

    async def stream_mini_tickers(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        if not symbols:
            return
        sdk = await self._sdk_client()
        try:
            streams_mod = importlib.import_module("binance.streams")
        except ModuleNotFoundError as exc:
            raise PermanentError("python-binance streams module is unavailable") from exc
        bsm_cls = getattr(streams_mod, "BinanceSocketManager", None)
        if bsm_cls is None:
            raise PermanentError("python-binance BinanceSocketManager is unavailable")

        streams = [f"{symbol.lower()}@trade" for symbol in symbols if symbol]
        manager = bsm_cls(sdk)
        try:
            socket = manager.multiplex_socket(streams)
        except Exception as exc:
            raise self._translate_exception(exc) from exc
        async with socket as socket_conn:
            while True:
                try:
                    frame = await socket_conn.recv()
                except Exception as exc:
                    raise self._translate_exception(exc) from exc
                if not isinstance(frame, dict):
                    continue
                data = frame.get("data")
                payload = data if isinstance(data, dict) else frame
                yield payload

    async def ping(self) -> bool:
        try:
            sdk = await self._sdk_client()
            await sdk.ping()
        except Exception:
            return False
        return True

    async def close(self) -> None:
        sdk = self._sdk
        if sdk is None:
            return
        self._sdk = None
        try:
            await sdk.close_connection()
        except Exception:
            return
