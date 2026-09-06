"""LongBridge SDK-backed client wrapper.

This module wraps the official `longbridge` SDK so `LongBridgeAdapter`
can stay pure and testable behind a Protocol boundary.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import types
from decimal import Decimal, InvalidOperation
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.adapters._common import PermanentError, TransientError

_LOG = logging.getLogger("mbp.longbridge.client")
# Default lookback when caller passes since=None. Was 90 days, which hid
# every buy-and-hold user's actual trade history. Walk back 5 years —
# Longbridge's `history_executions` retention is generous, and the
# 30-day chunked pagination below means a wider default just means
# more sequential broker calls if the user actually has data that far
# back.
_DEFAULT_TX_WINDOW_DAYS = 365 * 5
_HISTORY_CHUNK_DAYS = 30
# Longbridge returns OpenAPI 429002 ("api request is limited, please
# slow down request frequency") if we issue chunks back-to-back during
# a multi-year walk. The published rate isn't documented precisely,
# but ~3 s between calls reliably stays under it.
_HISTORY_THROTTLE_SECONDS = 3.1
# `order_detail` is one call per order, so a full history walk makes as
# many as there are orders. Slower than needed risks a long sync; faster
# risks 429002 and losing every fee at once.
_CHARGE_THROTTLE_SECONDS = 1.1


@dataclass(slots=True)
class LongbridgeCredentials:
    app_key: str
    app_secret: str
    access_token: str


class LongbridgeClient:  # pragma: no cover - integration exercised via env-gated test
    """Thin async wrapper around LongBridge quote + trade contexts."""

    def __init__(
        self,
        *,
        app_key: str,
        app_secret: str,
        access_token: str,
        quote_poll_interval: float = 1.0,
    ) -> None:
        config_cls, quote_context_cls, trade_context_cls = _load_sdk_types()

        # Official SDK helper for app-key auth flow.
        if hasattr(config_cls, "from_app_key"):
            config = config_cls.from_app_key(app_key, app_secret, access_token)
        elif hasattr(config_cls, "from_apikey"):
            config = config_cls.from_apikey(app_key, app_secret, access_token)
        else:
            raise PermanentError("longbridge Config missing from_app_key/from_apikey constructor")
        self._quote_ctx = quote_context_cls(config)
        self._trade_ctx = trade_context_cls(config)
        self._quote_poll_interval = quote_poll_interval

    async def list_positions(self) -> list[Any]:
        # The LongBridge SDK returns a `StockPositionsResponse` whose
        # `.channels` attribute is a list of `StockPositionChannel`,
        # each with its own `.positions`. Some older SDK versions
        # returned the list directly, so we handle both shapes.
        result = await _to_thread(self._trade_ctx.stock_positions)
        channels = _to_iterable(result, attribute="channels")
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug("stock_positions channel_count=%d", len(channels))
        raw_rows: list[Any] = []
        for idx, channel in enumerate(channels):
            ch_name = getattr(channel, "account_channel", None) or getattr(
                channel, "channel", None,
            )
            positions = getattr(channel, "positions", None)
            n = len(list(positions)) if positions is not None else 0
            if _LOG.isEnabledFor(logging.DEBUG):
                _LOG.debug(
                    "stock_positions channel[%d] name=%s positions=%d",
                    idx,
                    ch_name,
                    n,
                )
            if positions is None:
                raw_rows.append(channel)
                continue
            raw_rows.extend(list(positions))

        # The trade-side `stock_positions` endpoint returns `last_price: null`
        # for many positions. Fetch live quotes for the symbols and inject the
        # price so the aggregator can compute market_value and unrealized_pnl.
        symbols = [
            str(getattr(p, "symbol", "")) for p in raw_rows if getattr(p, "symbol", None)
        ]
        price_by_symbol: dict[str, Any] = {}
        if symbols:
            try:
                quotes = await _to_thread(self._quote_ctx.quote, symbols)
                # Try both the documented attribute name and the bare list
                # shape; older SDK versions returned the list directly.
                quote_iter = _to_iterable(quotes, attribute="secu_quote")
                if _LOG.isEnabledFor(logging.DEBUG):
                    _LOG.debug(
                        "quote-enrich: symbols=%s quote_count=%d quotes_type=%s",
                        symbols,
                        len(quote_iter),
                        type(quotes).__name__,
                    )
                for q in quote_iter:
                    sym = getattr(q, "symbol", None) or (
                        q.get("symbol") if isinstance(q, dict) else None
                    )
                    price = getattr(q, "last_done", None) or (
                        q.get("last_done") if isinstance(q, dict) else None
                    )
                    if sym and price is not None:
                        price_by_symbol[str(sym)] = price
                if _LOG.isEnabledFor(logging.DEBUG):
                    _LOG.debug("quote-enrich: prices_resolved=%s", price_by_symbol)
            except Exception as exc:  # noqa: BLE001 - quote enrichment is best-effort
                _LOG.warning("quote-enrich failed: %s", exc, exc_info=True)

        # Convert each SDK position to a dict so we can reliably merge in
        # the live price. The SDK returns immutable (slotted) dataclasses,
        # so setattr() silently fails — and the adapter would then see
        # null last_price and fall back to cost-basis. Building a fresh
        # dict guarantees the merged price is visible downstream.
        out: list[dict[str, Any]] = []
        for raw in raw_rows:
            row = _position_to_dict(raw)
            sym = str(row.get("symbol") or "")
            live = price_by_symbol.get(sym)
            if live is not None:
                row["last_price"] = live
                row["last_done"] = live
            out.append(row)
        return out

    async def list_balances(self) -> list[Any]:
        # Same shape concern as list_positions: the response object
        # exposes a `.list` (or `.accounts`) attribute containing the
        # actual rows on newer SDK versions.
        result = await _to_thread(self._trade_ctx.account_balance)
        accounts = _to_iterable(result, attribute="list", attribute_fallback="accounts")
        rows: list[Any] = []
        for account in accounts:
            cash_infos = getattr(account, "cash_infos", None)
            if cash_infos is None:
                rows.append(account)
                continue
            for cash in cash_infos:
                payload = {
                    "account_channel": getattr(account, "account_channel", None),
                    "currency": getattr(cash, "currency", None),
                    "withdraw_cash": getattr(cash, "withdraw_cash", None),
                    "available_cash": getattr(cash, "available_cash", None),
                    "cash": getattr(cash, "cash", None),
                    "total_cash": getattr(cash, "withdraw_cash", None),
                }
                rows.append(payload)
        return rows

    async def list_transactions(
        self,
        *,
        since: str | None,
        limit: int | None,
    ) -> list[Any]:
        # Walk newest → oldest with a throttle between chunks. If we
        # hit Longbridge's 429002 mid-walk after collecting some rows,
        # surface what we have rather than failing the whole call —
        # the caller's cache will store the partial result so the next
        # refresh skips re-walking the chunks we already have.
        start_at = _history_start(since)
        end_at = datetime.now(UTC)
        rows: list[Any] = []
        chunks_done = 0
        cursor_end = end_at
        while cursor_end >= start_at:
            chunk_start = max(
                cursor_end - timedelta(days=_HISTORY_CHUNK_DAYS),
                start_at,
            )

            if chunks_done > 0:
                await asyncio.sleep(_HISTORY_THROTTLE_SECONDS)

            try:
                result = await _to_thread(
                    _history_executions_call,
                    self._trade_ctx,
                    start_at=chunk_start,
                    end_at=cursor_end,
                )
                chunk_rows = _history_rows(result)
                rows.extend(chunk_rows)
            except TransientError as exc:
                if rows:
                    _LOG.warning(
                        "longbridge history walk aborted at chunk %d "
                        "(%s..%s): %s; returning %d partial rows",
                        chunks_done,
                        chunk_start,
                        cursor_end,
                        exc,
                        len(rows),
                    )
                    break
                raise

            chunks_done += 1
            if limit is not None and limit >= 0 and len(rows) >= limit:
                break
            if chunk_start <= start_at:
                break
            cursor_end = chunk_start - timedelta(microseconds=1)

        threshold = _parse_since(since) if since is not None else start_at
        kept: list[Any] = []
        for row in rows:
            timestamp = _row_timestamp(row)
            if timestamp is None or timestamp >= threshold:
                kept.append(row)
        fallback_ts = datetime.max.replace(tzinfo=UTC)
        kept.sort(
            key=lambda row: _row_timestamp(row) or fallback_ts,
            reverse=True,
        )
        if limit is not None and limit >= 0:
            kept = kept[:limit]
        return await self._with_order_side(kept, start_at=start_at, end_at=end_at)

    async def _with_order_side(
        self, executions: list[Any], *, start_at: datetime, end_at: datetime
    ) -> list[Any]:
        """Attach the buy/sell side to each execution.

        LongBridge's `Execution` object carries order_id, price, quantity,
        symbol and timestamps -- and **no side**. Direction lives on the
        order, so without this join every fill reaches the mapper with
        `side=None` and is discarded as unrecognised. Currency is only on
        the order too, and Ghostfolio cannot record an activity without it.

        A fill whose order cannot be found keeps `side=None` and is
        skipped downstream with a reason. That is deliberate: inferring a
        direction from a positive quantity would silently turn a sale into
        a purchase, which is exactly the kind of guess that destroys cost
        basis (§6.3).
        """
        if not executions:
            return executions

        try:
            orders = _history_rows_by_key(
                await _to_thread(
                    _history_orders_call,
                    self._trade_ctx,
                    start_at=start_at,
                    end_at=end_at,
                ),
                keys=("orders", "items", "list"),
            )
        except Exception as exc:  # noqa: BLE001 -- degrade, never fail the walk
            _LOG.warning(
                "longbridge: could not fetch order history to resolve trade "
                "sides (%s); executions will be skipped rather than guessed",
                exc,
            )
            orders = []

        by_order_id = {
            str(order_id): order
            for order in orders
            if (order_id := _attr(order, "order_id")) is not None
        }

        charges = await self._order_charges(sorted(by_order_id))

        merged: list[Any] = []
        unmatched = 0
        # An order's commission is charged once, but it can fill in
        # several executions — the live data has one RUN order that filled
        # as seven. Attaching the whole charge to each fill would multiply
        # the fee by seven, so it is split across them in proportion to
        # quantity.
        filled_qty: dict[str, Decimal] = {}
        for execution in executions:
            order_id = str(_attr(execution, "order_id") or "")
            quantity = _dec_or_none(_attr(execution, "quantity")) or Decimal("0")
            filled_qty[order_id] = filled_qty.get(order_id, Decimal("0")) + quantity

        for execution in executions:
            row = {
                name: value
                for name in (
                    "order_id", "trade_id", "symbol", "price", "quantity",
                    "trade_done_at",
                )
                if (value := _attr(execution, name)) is not None
            }
            order_id = str(row.get("order_id", ""))
            charge = charges.get(order_id)
            if charge is not None:
                total, currency = charge
                row["fee"] = _split_charge(
                    total,
                    fill_quantity=_dec_or_none(row.get("quantity")) or Decimal("0"),
                    order_quantity=filled_qty.get(order_id) or Decimal("0"),
                )
                row["fee_currency"] = currency

            order = by_order_id.get(order_id)
            if order is None:
                unmatched += 1
            else:
                side = _attr(order, "side")
                if side is not None:
                    # OrderSide.Buy -> "Buy": the SDK enum's repr is
                    # `OrderSide.Buy`, so take the member name rather than
                    # str() of the whole enum.
                    row["side"] = getattr(side, "name", None) or str(side).rsplit(
                        ".", 1
                    )[-1]
                for name in ("currency", "account_no"):
                    if (value := _attr(order, name)) is not None:
                        row[name] = value
            merged.append(row)

        if unmatched:
            _LOG.warning(
                "longbridge: %d of %d execution(s) had no matching order; "
                "their side is unknown and they will not be pushed",
                unmatched,
                len(executions),
            )
        return merged

    async def _order_charges(
        self, order_ids: list[str]
    ) -> dict[str, tuple[Decimal, str]]:
        """Commission per order, from `order_detail`.

        Neither the execution nor the order carries a fee — only the
        per-order detail call does, one round trip each. Fees were being
        recorded as 0, which understates cost basis and overstates every
        return built on it.

        `free_amount` is a commission waiver. It is subtracted only when
        `free_status` says the waiver is actually settled: treating a
        pending waiver as applied would understate the cost, and
        overstating a return is the more dangerous direction to be wrong
        in. A detail call that fails costs that order its fee, not the
        trade — same trade-off as §5.6.
        """
        detail_fn = getattr(self._trade_ctx, "order_detail", None)
        if not callable(detail_fn) or not order_ids:
            return {}

        charges: dict[str, tuple[Decimal, str]] = {}
        for index, order_id in enumerate(order_ids):
            if index:
                await asyncio.sleep(_CHARGE_THROTTLE_SECONDS)
            try:
                detail = await _to_thread(detail_fn, order_id)
            except Exception as exc:  # noqa: BLE001 — one order, not the walk
                _LOG.warning("longbridge: no charge detail for %s: %s", order_id, exc)
                continue

            charge = _attr(detail, "charge_detail")
            total = _dec_or_none(_attr(charge, "total_amount")) if charge else None
            if total is None:
                continue
            currency = str(
                _attr(charge, "currency") or _attr(detail, "currency") or ""
            ).upper()

            net = _apply_waiver(
                total,
                waiver=_dec_or_none(_attr(detail, "free_amount")),
                status=str(_attr(detail, "free_status") or ""),
            )
            if net != total:
                _LOG.debug(
                    "longbridge %s: charge %s less settled waiver -> %s",
                    order_id, total, net,
                )
            charges[order_id] = (net, currency)

        _LOG.info("longbridge: resolved charges for %d order(s)", len(charges))
        return charges

    async def ping(self) -> bool:
        await _to_thread(self._trade_ctx.account_balance)
        return True

    async def stream_quotes(self, symbols: list[str]) -> AsyncIterator[Any]:
        if not symbols:
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        push_wired = False

        def _enqueue_quote(row: Any) -> None:
            now = datetime.now(UTC)
            if isinstance(row, dict):
                payload = dict(row)
            else:
                payload = {
                    "symbol": getattr(row, "symbol", None),
                    "last_done": getattr(row, "last_done", None),
                    "currency": getattr(row, "currency", None),
                    "timestamp": getattr(row, "timestamp", now),
                }
            payload.setdefault("timestamp", now)
            loop.call_soon_threadsafe(queue.put_nowait, payload)

        subscribe_fn = getattr(self._quote_ctx, "subscribe", None)
        set_handler = getattr(self._quote_ctx, "set_on_quote", None)
        if callable(subscribe_fn) and callable(set_handler):
            subtype_value: Any = None
            sdk_subtype = getattr(importlib.import_module("longbridge.openapi"), "SubType", None)
            if sdk_subtype is not None:
                subtype_value = (
                    getattr(sdk_subtype, "Quote", None)
                    or getattr(sdk_subtype, "QUOTE", None)
                )

            def on_quote(rows: Any) -> None:
                for row in rows:
                    _enqueue_quote(row)

            await _to_thread(set_handler, on_quote)
            if subtype_value is not None:
                await _to_thread(subscribe_fn, symbols, [subtype_value])
            else:
                await _to_thread(subscribe_fn, symbols)
            push_wired = True

        # Prime with a snapshot so callers don't wait for next market event.
        quotes = await _to_thread(self._quote_ctx.quote, symbols)
        for quote in quotes:
            _enqueue_quote(quote)

        if push_wired:
            while True:
                yield await queue.get()
        else:
            while True:
                quotes = await _to_thread(self._quote_ctx.quote, symbols)
                for quote in quotes:
                    _enqueue_quote(quote)
                while not queue.empty():
                    yield await queue.get()
                await asyncio.sleep(self._quote_poll_interval)


def _position_to_dict(raw: Any) -> dict[str, Any]:
    """Snapshot an SDK position object into a plain dict.

    The LongBridge SDK returns immutable (slotted) dataclasses, so
    `setattr` silently fails when we try to inject a live price.
    Cloning to a dict produces a mutable copy the adapter can read
    via dict-style access AND lets us drop in merged fields.

    If `raw` is already a dict, we pass it through. Otherwise we
    enumerate the known fields from `_lookup` in the adapter.
    """
    if isinstance(raw, dict):
        return dict(raw)
    # Known fields the adapter looks for, plus a few aliases used by
    # different LongBridge SDK versions.
    field_names = (
        "symbol",
        "quantity",
        "currency",
        "cost_price",
        "avg_cost",
        "last_price",
        "last_done",
        "price",
        "market_value",
        "unrealized_pnl",
        "market",
        "exchange",
        "account_no",
        "account_id",
        "account_channel",
        "available_quantity",
        "init_quantity",
    )
    out: dict[str, Any] = {}
    for name in field_names:
        value = getattr(raw, name, None)
        if value is not None:
            out[name] = value
    return out


def _to_iterable(
    value: Any,
    *,
    attribute: str,
    attribute_fallback: str | None = None,
) -> list[Any]:
    """Coerce an SDK response into a list of rows.

    Newer LongBridge SDK versions wrap query results in a typed response
    object (e.g. `StockPositionsResponse`) whose actual list lives under
    a named attribute. Older versions return the list directly. Handle
    both by:
      1. If `value` is iterable (list/tuple/dict-values), return list(value).
      2. Else if it has `.<attribute>` attribute, use that.
      3. Else if `attribute_fallback` is given and present, use that.
      4. Else return an empty list.
    """
    if value is None:
        return []
    # Already iterable list-likes — most importantly excludes plain objects
    # so we don't accidentally iterate field-by-field.
    if isinstance(value, list | tuple):
        return list(value)
    primary = getattr(value, attribute, None)
    if primary is not None:
        return list(primary)
    if attribute_fallback is not None:
        fallback = getattr(value, attribute_fallback, None)
        if fallback is not None:
            return list(fallback)
    # Last resort: try iterating; if not iterable, give up.
    try:
        return list(iter(value))
    except TypeError:
        return []


async def _to_thread(fn: Any, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover - thin asyncio glue exercised only through real SDK calls
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - normalized below
        raise _classify_sdk_error(exc) from exc


def _load_sdk_types() -> tuple[type[Any], type[Any], type[Any]]:  # pragma: no cover - imports real longbridge SDK; tested by env-gated integration test
    try:
        module = importlib.import_module("longbridge.openapi")
    except ModuleNotFoundError as exc:
        msg = "longbridge SDK not installed; add dependency and install backend requirements"
        raise PermanentError(msg) from exc

    if not isinstance(module, types.ModuleType):
        raise PermanentError("longbridge SDK module failed to load")

    config_cls = getattr(module, "Config", None)
    quote_context_cls = getattr(module, "QuoteContext", None)
    trade_context_cls = getattr(module, "TradeContext", None)
    if (
        not isinstance(config_cls, type)
        or not isinstance(quote_context_cls, type)
        or not isinstance(trade_context_cls, type)
    ):
        raise PermanentError("longbridge SDK missing Config/QuoteContext/TradeContext")
    return config_cls, quote_context_cls, trade_context_cls


def _classify_sdk_error(exc: Exception) -> Exception:
    if isinstance(exc, (PermanentError, TransientError)):
        return exc

    message = str(exc).lower()
    code_raw = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    code = str(code_raw).lower() if code_raw is not None else ""

    if (
        "rate limit" in message
        or "too many request" in message
        or "timeout" in message
        or "temporarily unavailable" in message
        # Longbridge's own rate-limit wording: error code 429002 with
        # "api request is limited, please slow down request frequency".
        # Without these markers the error was bubbling up as a generic
        # OpenApiException, which never got classified as transient
        # and so was un-retryable.
        or "is limited" in message
        or "slow down" in message
        or code in {"429", "429002", "301606", "500", "502", "503", "504"}
    ):
        return TransientError(str(exc))

    if (
        "invalid access token" in message
        or "access token" in message
        or "invalid app key" in message
        or "app secret" in message
        or "unauthorized" in message
        or "forbidden" in message
        or "credential" in message
        or code in {"401", "403", "100002", "100004"}
    ):
        return PermanentError(str(exc))

    return exc


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _history_start(since: str | None) -> datetime:
    if since is not None:
        return _parse_since(since)
    return datetime.now(UTC) - timedelta(days=_DEFAULT_TX_WINDOW_DAYS)


def _history_executions_call(
    trade_ctx: Any,
    *,
    start_at: datetime,
    end_at: datetime,
) -> Any:
    history_fn = getattr(trade_ctx, "history_executions", None)
    if not callable(history_fn):
        return trade_ctx.today_executions()
    attempts: tuple[dict[str, Any], ...] = (
        {"symbol": None, "start_at": start_at, "end_at": end_at},
        {"start_at": start_at, "end_at": end_at},
        {"symbol": None, "start": start_at, "end": end_at},
        {"start": start_at, "end": end_at},
    )
    for kwargs in attempts:
        try:
            return history_fn(**kwargs)
        except TypeError:
            continue
    return history_fn(start_at, end_at)


def _apply_waiver(total: Decimal, *, waiver: Decimal | None, status: str) -> Decimal:
    """Subtract a commission waiver, but only once it is settled.

    LongBridge reports `free_amount` alongside the charge. Treating a
    pending waiver as applied understates the cost, and overstating a
    return is the more dangerous direction to be wrong in — so anything
    other than a settled status leaves the gross charge in place.
    """
    if waiver is None:
        return total
    if status.rsplit(".", 1)[-1].strip().lower() != "ready":
        return total
    return max(total - abs(waiver), Decimal("0"))


def _split_charge(
    charge: Decimal, *, fill_quantity: Decimal, order_quantity: Decimal
) -> Decimal:
    """Apportion an order's single commission across one of its fills.

    An order is charged once but can fill many times — the live data has
    a RUN order that filled as seven 1-share executions. Attaching the
    whole charge to each fill would multiply the fee by seven.
    """
    if not order_quantity:
        return charge
    return charge * (fill_quantity / order_quantity)


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _attr(row: Any, name: str) -> Any:
    """Read one field from an SDK object or a dict, whichever we were given."""
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def _history_orders_call(
    trade_ctx: Any,
    *,
    start_at: datetime,
    end_at: datetime,
) -> Any:
    """Fetch order history, tolerating the SDK's several keyword spellings.

    Mirrors `_history_executions_call`: the longbridge -> longport rename
    shifted parameter names more than once, and a TypeError here would
    otherwise cost every trade its direction.
    """
    history_fn = getattr(trade_ctx, "history_orders", None)
    if not callable(history_fn):
        today_fn = getattr(trade_ctx, "today_orders", None)
        return today_fn() if callable(today_fn) else []
    attempts: tuple[dict[str, Any], ...] = (
        {"start_at": start_at, "end_at": end_at},
        {"symbol": None, "start_at": start_at, "end_at": end_at},
        {"start": start_at, "end": end_at},
    )
    for kwargs in attempts:
        try:
            return history_fn(**kwargs)
        except TypeError:
            continue
    return history_fn(start_at, end_at)


def _history_rows_by_key(result: Any, *, keys: tuple[str, ...]) -> list[Any]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in keys:
            value = result.get(key)
            if isinstance(value, list):
                return value
        return []
    for key in keys:
        value = getattr(result, key, None)
        if isinstance(value, list):
            return value
    return []


def _history_rows(result: Any) -> list[Any]:
    if isinstance(result, dict):
        for key in ("executions", "trades", "items", "list"):
            value = result.get(key)
            if isinstance(value, list):
                return value
    rows = _to_iterable(
        result,
        attribute="executions",
        attribute_fallback="trades",
    )
    if rows:
        return rows
    return _to_iterable(result, attribute="list")


def _row_timestamp(row: Any) -> datetime | None:
    keys = (
        "trade_done_at",
        "submitted_at",
        "executed_at",
        "created_at",
        "timestamp",
        "time",
        "updated_at",
    )
    value: Any | None = None
    for key in keys:
        value = getattr(row, key, None)
        if value is None and isinstance(row, dict):
            value = row.get(key)
        if value is not None:
            break
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        ts = float(value)
        if ts > 1e12:
            ts /= 1000.0
        return datetime.fromtimestamp(ts, tz=UTC)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
