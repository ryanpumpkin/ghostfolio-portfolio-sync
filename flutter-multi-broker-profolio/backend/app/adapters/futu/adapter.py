"""Futu OpenD adapter.

The official `futu-api` SDK uses a long-lived TCP connection to a local
OpenD process. This adapter goes through an injected `FutuClient`
Protocol so tests can replace it.

This adapter is READ-ONLY by construction. It never calls
`unlock_trade`, so the OpenD session it uses cannot place, modify or
cancel an order even if this host were compromised (spec §4.3 rule 2).

That is possible because unlock is required only for order operations,
not for reads. Verified three ways on 2026-09-06:

  * Futu's own docs scope it to "Place Order or Modify or Cancel Orders".
  * The SDK's position_list_query / accinfo_query / history_deal_list_query
    contain no unlock gate; `_ctx_unlock` is only used to re-unlock after
    a socket reconnect.
  * Empirically, against real OpenD 10.6.6608 with a locked session:
    accinfo_query OK, position_list_query OK (2 rows),
    history_deal_list_query OK.

The repo previously believed the opposite (BROKER_INTEGRATION_DETAILS
§C.5 claimed "All trade_ctx queries require unlock_trade first"), and
that single wrong assertion is why a trade password existed in settings
and .env at all. Do not reintroduce it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
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
)

SOURCE_NAME = "futu"
class FutuClient(Protocol):
    """OpenD wrapper."""

    async def fetch_positions(self) -> list[dict[str, Any]]: ...

    async def fetch_accounts(self) -> list[dict[str, Any]]: ...

    async def fetch_history_deals(
        self, *, since: str | None, limit: int | None
    ) -> list[dict[str, Any]]: ...

    def subscribe_quotes(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]: ...

    async def ping(self) -> bool: ...


def _dec(v: Any) -> Decimal:
    return Decimal(str(v))


def _opt_dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    return Decimal(str(v))


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _map_position(raw: dict[str, Any]) -> Position:
    qty = _dec(raw["qty"])
    last = _opt_dec(raw.get("nominal_price"))
    avg = _opt_dec(raw.get("cost_price"))
    return Position(
        source=SOURCE_NAME,
        account_id=str(raw["acc_id"]) if "acc_id" in raw else None,
        symbol=raw["code"],
        exchange=raw.get("trd_market"),
        quantity=qty,
        avg_cost=avg,
        last_price=last,
        currency=raw["currency"],
        market_value=_opt_dec(raw.get("market_val")),
        unrealized_pnl=_opt_dec(raw.get("pl_val")),
    )


def _map_balance(raw: dict[str, Any]) -> CashBalance:
    return CashBalance(
        source=SOURCE_NAME,
        account_id=str(raw["acc_id"]) if "acc_id" in raw else None,
        currency=raw["currency"],
        amount=_dec(raw["cash"]),
    )


#: Futu deal rows name the market, never the currency. Settlement
#: currency follows the market, and the mapper defaults a missing one to
#: USD — so an HK trade with no currency was being recorded as USD, at
#: roughly 7.8x its real cost.
_MARKET_CURRENCY = {"HK": "HKD", "US": "USD", "CN": "CNY", "SG": "SGD", "JP": "JPY"}


def _deal_currency(raw: dict[str, Any]) -> str | None:
    if currency := raw.get("currency"):
        return str(currency).upper()
    market = str(raw.get("deal_market") or "").upper()
    if currency := _MARKET_CURRENCY.get(market):
        return currency
    # A prefixed code carries the venue when deal_market does not.
    code = str(raw.get("code") or "")
    if "." in code:
        return _MARKET_CURRENCY.get(code.split(".", 1)[0].upper())
    return None


def _map_transaction(raw: dict[str, Any]) -> Transaction:
    side_raw = raw.get("trd_side")
    side = side_raw.lower() if isinstance(side_raw, str) else None
    return Transaction(
        source=SOURCE_NAME,
        account_id=str(raw["acc_id"]) if "acc_id" in raw else None,
        # `deal_id`, not `order_id`: one order can fill in several deals,
        # and keying on the order makes them share an id — the
        # idempotency ledger then drops all but the first and part of a
        # position silently disappears (§3.3).
        transaction_id=str(raw.get("deal_id") or raw["order_id"]),
        symbol=raw.get("code"),
        side=side,
        quantity=_opt_dec(raw.get("qty")),
        price=_opt_dec(raw.get("price")),
        currency=_deal_currency(raw),
        amount=_opt_dec(raw.get("dealt_amount") or raw.get("amount")),
        # Commission, apportioned across an order's deals by the client.
        fee=_opt_dec(raw.get("fee")),
        fee_currency=raw.get("fee_currency") or _deal_currency(raw),
        timestamp=_parse_ts(raw["create_time"]),
    )


def _map_quote(raw: dict[str, Any]) -> Quote:
    return Quote(
        source=SOURCE_NAME,
        symbol=raw["code"],
        price=_dec(raw["last_price"]),
        currency=raw["currency"],
        timestamp=_parse_ts(raw.get("data_date") or raw["timestamp"]),
    )


def _normalize_error(exc: Exception) -> Exception:
    if isinstance(exc, PermanentError | TransientError):
        return exc
    message = str(exc)
    lowered = message.lower()
    if any(
        marker in lowered
        for marker in (
            "rate limit",
            "too many request",
            "too many requests",
            "quota",
            "throttle",
            "temporarily unavailable",
            "timeout",
        )
    ):
        return TransientError(message)
    if any(
        marker in lowered
        for marker in (
            "unlock",
            "password",
            "pwd",
            "credential",
            "permission",
            "unauthorized",
            "forbidden",
            "auth",
        )
    ):
        return PermanentError(message)
    return exc


class FutuAdapter(SourceAdapter):
    """Futu OpenD adapter."""

    source = SOURCE_NAME

    def __init__(
        self,
        client: FutuClient,
        *,
        retry: RetryPolicy | None = None,
        health: HealthTracker | None = None,
    ) -> None:
        self._client = client
        self._retry = retry or RetryPolicy()
        self._health = health or HealthTracker(source=SOURCE_NAME)

    async def _call(self, func: Callable[[], Awaitable[Any]]) -> Any:
        async def _wrapped() -> Any:
            try:
                return await func()
            except Exception as exc:  # noqa: BLE001
                raise _normalize_error(exc) from exc

        try:
            result = await retry_async(_wrapped, policy=self._retry)
        except Exception as exc:
            self._health.record_failure(str(exc))
            raise
        self._health.record_success()
        return result

    async def list_positions(self) -> list[Position]:
        async def _do() -> list[dict[str, Any]]:
            return await self._client.fetch_positions()

        raw = await self._call(_do)
        return [_map_position(item) for item in raw]

    async def list_balances(self) -> list[CashBalance]:
        async def _do() -> list[dict[str, Any]]:
            return await self._client.fetch_accounts()

        raw = await self._call(_do)
        return [_map_balance(item) for item in raw]

    async def list_transactions(
        self,
        *,
        since: str | None = None,
        limit: int | None = None,
    ) -> list[Transaction]:
        async def _do() -> list[dict[str, Any]]:
            return await self._client.fetch_history_deals(since=since, limit=limit)

        raw = await self._call(_do)
        return [_map_transaction(item) for item in raw]

    async def stream_quotes(self, symbols: Iterable[str]) -> AsyncIterator[Quote]:
        async for raw in self._client.subscribe_quotes(list(symbols)):
            yield _map_quote(raw)

    async def healthcheck(self) -> SourceHealth:
        try:
            ok = await self._client.ping()
            if ok:
                self._health.record_success()
            else:
                self._health.record_failure("OpenD ping failed")
        except Exception as exc:  # noqa: BLE001
            self._health.record_failure(str(exc))
        return self._health.snapshot()


__all__ = [
    "SOURCE_NAME",
    "FutuAdapter",
    "FutuClient",
]
