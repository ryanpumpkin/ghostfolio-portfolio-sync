"""LongBridge OpenAPI adapter.

Callers pass any object that satisfies the `LongBridgeClient` Protocol —
in production `app.adapters.longbridge.client.LongbridgeClient`, in tests a
fake. See detailed-design §4.3.
"""

from __future__ import annotations

import logging

from collections.abc import AsyncIterator, Iterable
from datetime import UTC, datetime
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
    TransactionType,
)

SOURCE_NAME = "longbridge"
_LOG = logging.getLogger("mbp.longbridge.adapter")


class LongBridgeClient(Protocol):
    """SDK wrapper boundary. The real impl wraps `longbridge.openapi`."""

    async def list_positions(self) -> list[Any]: ...

    async def list_balances(self) -> list[Any]: ...

    async def list_transactions(self, *, since: str | None, limit: int | None) -> list[Any]: ...

    async def list_cash_flow(self, *, since: str | None) -> list[Any]: ...

    def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Any]: ...

    async def ping(self) -> bool: ...


def _lookup(raw: Any, *keys: str) -> Any:
    for key in keys:
        if isinstance(raw, dict) and key in raw:
            return raw[key]
        if hasattr(raw, key):
            return getattr(raw, key)
    return None


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _opt_dec(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return Decimal(str(value))


def _map_position(raw: Any) -> Position:
    symbol = _lookup(raw, "symbol")
    if symbol is None:
        raise PermanentError("longbridge position missing symbol")
    quantity = _lookup(raw, "quantity")
    if quantity is None:
        raise PermanentError("longbridge position missing quantity")
    currency = _lookup(raw, "currency")
    if currency is None:
        raise PermanentError("longbridge position missing currency")

    qty = _dec(quantity)
    avg = _opt_dec(_lookup(raw, "cost_price", "avg_cost"))
    last = _opt_dec(_lookup(raw, "last_price", "last_done", "price"))
    mv_raw = _lookup(raw, "market_value")
    upl_raw = _lookup(raw, "unrealized_pnl")
    # market_value falls back through: explicit field → live-quote-based
    # (last * qty) → cost-basis (avg * qty). The cost-basis fallback keeps
    # the dashboard usable even when LongBridge returns null prices
    # outside trading hours.
    mv = (
        _opt_dec(mv_raw)
        if mv_raw is not None
        else last * qty if last is not None
        else avg * qty if avg is not None
        else None
    )
    upl = (
        _opt_dec(upl_raw)
        if upl_raw is not None
        else ((last - avg) * qty if last is not None and avg is not None else None)
    )

    exchange = _lookup(raw, "market", "exchange")
    if exchange is not None:
        exchange = str(exchange)

    return Position(
        source=SOURCE_NAME,
        account_id=_opt_str(_lookup(raw, "account_no", "account_id", "account_channel")),
        symbol=str(symbol),
        exchange=exchange,
        quantity=qty,
        avg_cost=avg,
        last_price=last,
        currency=str(currency),
        market_value=mv,
        unrealized_pnl=upl,
    )


def _map_balance(raw: Any) -> CashBalance:
    currency = _lookup(raw, "currency")
    if currency is None:
        raise PermanentError("longbridge balance missing currency")
    amount = _lookup(raw, "total_cash", "withdraw_cash", "available_cash", "cash", "amount")
    if amount is None:
        raise PermanentError("longbridge balance missing amount")
    return CashBalance(
        source=SOURCE_NAME,
        account_id=_opt_str(_lookup(raw, "account_no", "account_id", "account_channel")),
        currency=str(currency),
        amount=_dec(amount),
    )


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _map_transaction(raw: Any) -> Transaction:
    # `trade_id` first, not `order_id`: one order can fill in several
    # executions, and keying on the order would make those partial fills
    # share an id. The idempotency ledger would then treat the second fill
    # as already pushed and drop it, quietly losing part of a position
    # (§3.3). `trade_id` exists only on executions, so orders still fall
    # through to `order_id`.
    txid = _lookup(raw, "trade_id", "order_id", "transaction_id")
    if txid is None:
        raise PermanentError("longbridge transaction missing id")
    side_raw = _lookup(raw, "side")
    side = str(side_raw).lower() if side_raw is not None else None
    timestamp = _lookup(
        raw,
        "submitted_at",
        "trade_done_at",
        "executed_at",
        "created_at",
        "updated_at",
        "timestamp",
        "time",
    )
    if timestamp is None:
        raise PermanentError("longbridge transaction missing timestamp")
    return Transaction(
        source=SOURCE_NAME,
        account_id=_opt_str(_lookup(raw, "account_no", "account_id", "account_channel")),
        transaction_id=str(txid),
        symbol=_opt_str(_lookup(raw, "symbol")),
        side=side,
        quantity=_opt_dec(_lookup(raw, "quantity", "executed_quantity")),
        price=_opt_dec(_lookup(raw, "price", "executed_price")),
        currency=_opt_str(_lookup(raw, "currency")),
        amount=_opt_dec(_lookup(raw, "amount", "executed_amount")),
        # Commission, apportioned across an order's fills by the client.
        fee=_opt_dec(_lookup(raw, "fee", "commission")),
        fee_currency=_opt_str(_lookup(raw, "fee_currency")),
        timestamp=_parse_ts(timestamp),
    )


def _map_quote(raw: Any) -> Quote:
    symbol = _lookup(raw, "symbol")
    if symbol is None:
        raise PermanentError("longbridge quote missing symbol")
    price = _lookup(raw, "last_done", "last_price", "price")
    if price is None:
        raise PermanentError("longbridge quote missing price")
    currency = _lookup(raw, "currency")
    if currency is None:
        currency = "USD"
    ts_raw = _lookup(raw, "timestamp", "trade_done_at", "updated_at")
    return Quote(
        source=SOURCE_NAME,
        symbol=str(symbol),
        price=_dec(price),
        currency=str(currency),
        timestamp=_parse_ts(ts_raw) if ts_raw is not None else datetime.now(UTC),
    )


def _classify_error(exc: Exception) -> Exception:
    if isinstance(exc, (TransientError, PermanentError)):
        return exc

    message = str(exc).lower()
    code_raw = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    code = str(code_raw).lower() if code_raw is not None else ""

    if (
        "rate limit" in message
        or "too many request" in message
        or "timed out" in message
        or "timeout" in message
        or code in {"429", "301606", "500", "502", "503", "504"}
    ):
        return TransientError(str(exc))

    if (
        "invalid access token" in message
        or "access token" in message
        or "app key" in message
        or "app secret" in message
        or "unauthorized" in message
        or "forbidden" in message
        or "credential" in message
        or code in {"401", "403", "100002", "100004"}
    ):
        return PermanentError(str(exc))

    return exc


def _map_cash_flow(raw: Any) -> Transaction | None:
    """One LongBridge cash movement.

    Direction decides deposit vs withdrawal, and mapping it backwards
    turns a contribution into a withdrawal and pushes the return the
    wrong way — so an unreadable direction falls back to the AMOUNT's
    sign rather than to a default.
    """
    amount = _opt_dec(getattr(raw, "balance", None))
    if amount is None:
        amount = _opt_dec(getattr(raw, "amount", None))
    if amount is None or amount == 0:
        return None
    when = getattr(raw, "business_time", None) or getattr(raw, "time", None)
    if when is None:
        return None
    direction = str(getattr(raw, "direction", "") or "").upper()
    if "OUT" in direction:
        kind = TransactionType.WITHDRAWAL
    elif "IN" in direction:
        kind = TransactionType.DEPOSIT
    else:
        kind = TransactionType.DEPOSIT if amount > 0 else TransactionType.WITHDRAWAL
    ident = str(
        getattr(raw, "transaction_flow_name", None)
        or getattr(raw, "id", None)
        or f"{when}:{amount}"
    )
    return Transaction(
        source=SOURCE_NAME,
        transaction_id=ident,
        external_id=f"{SOURCE_NAME}:cash:{ident}",
        symbol=None,
        side=direction.lower() or None,
        type=kind,
        amount=abs(amount) if kind is TransactionType.DEPOSIT else -abs(amount),
        currency=str(getattr(raw, "currency", None) or "USD").upper(),
        timestamp=_parse_ts(when),
    )


class LongBridgeAdapter(SourceAdapter):
    """LongBridge OpenAPI adapter."""

    source = SOURCE_NAME

    def __init__(
        self,
        client: LongBridgeClient,
        *,
        retry: RetryPolicy | None = None,
        health: HealthTracker | None = None,
    ) -> None:
        self._client = client
        self._retry = retry or RetryPolicy()
        self._health = health or HealthTracker(source=SOURCE_NAME)

    async def _call(self, func: Any) -> Any:
        async def _wrapped() -> Any:
            try:
                return await func()
            except Exception as exc:  # noqa: BLE001 - normalized below
                raise _classify_error(exc) from exc

        try:
            result = await retry_async(_wrapped, policy=self._retry)
        except Exception as exc:
            self._health.record_failure(str(exc))
            raise
        self._health.record_success()
        return result

    async def list_positions(self) -> list[Position]:
        raw = await self._call(self._client.list_positions)
        return [_map_position(item) for item in raw]

    async def list_balances(self) -> list[CashBalance]:
        raw = await self._call(self._client.list_balances)
        return [_map_balance(item) for item in raw]

    async def list_transactions(
        self,
        *,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        async def _do() -> list[Any]:
            return await self._client.list_transactions(since=since, limit=limit)

        raw = await self._call(_do)
        return [_map_transaction(item) for item in raw]

    async def list_cash_movements(
        self, *, since: str | None = None
    ) -> list[Transaction]:
        """Deposits and withdrawals, for the money-weighted return.

        Best-effort: an SDK without `cash_flow`, or a permission the key
        does not carry, costs a returns refinement — not the sync.
        """
        fetch = getattr(self._client, "list_cash_flow", None)
        if not callable(fetch):
            return []
        try:
            raw = await fetch(since=since)
        except Exception as exc:  # noqa: BLE001 — see docstring
            _LOG.warning(
                "longbridge: cash flow unavailable (%s); deposits will be "
                "missing from the returns", exc,
            )
            return []
        return [tx for tx in (_map_cash_flow(r) for r in raw) if tx is not None]

    async def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        async for raw in self._client.stream_quotes(list(symbols)):
            yield _map_quote(raw)

    async def healthcheck(self) -> SourceHealth:
        try:
            ok = await self._client.ping()
            if ok:
                self._health.record_success()
            else:
                self._health.record_failure("longbridge health probe failed")
        except Exception as exc:  # noqa: BLE001 - health probe records and continues
            self._health.record_failure(str(exc))
        return self._health.snapshot()


# Re-export the transient error so adapter callers can raise it inside
# injected SDK wrappers without importing from the private module.
__all__ = ["LongBridgeAdapter", "LongBridgeClient", "TransientError"]
