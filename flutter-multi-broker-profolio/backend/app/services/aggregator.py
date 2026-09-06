"""Portfolio aggregation service with resilient fan-out and per-source caching."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol, TypeVar, cast

from app.adapters.base import SourceAdapter
from app.core.e2e_backend import WrappedCredentialError, unwrap_from_backend
from app.models.domain import (
    CashBalance,
    Connection,
    FxRate,
    PartialResult,
    PortfolioSnapshot,
    Position,
    SourceHealth,
    SourceHealthStatus,
    Transaction,
)
from app.services.adapter_factory import AdapterFactory
from app.services.connection_status import (
    ConnectionStatusEventPublisher,
    ConnectionSyncStatus,
    ConnectionSyncStatusEvent,
    NoopConnectionStatusPublisher,
)
from app.services.fx import FxService
from app.services.vault import CredentialVaultService

T = TypeVar("T")


@dataclass(slots=True, frozen=True)
class AggregationCredentialContext:
    """Request-scoped credential data needed for adapter construction."""

    wrapped_tokens_by_connection: dict[str, str] = field(default_factory=dict)
    shared_wrapped_token: str | None = None
    unwrap_key: bytes | None = None

    def token_for(self, connection_id: str) -> str | None:
        return self.wrapped_tokens_by_connection.get(connection_id) or self.shared_wrapped_token


class ConnectionRepository(Protocol):
    """Loads user connections from storage."""

    async def list_connections(self, user_id: str) -> list[Connection]: ...


class AdapterRegistry(Protocol):
    """Legacy adapter lookup by connection source."""

    def for_connection(self, connection: Connection) -> SourceAdapter | None: ...


@dataclass(slots=True)
class _TtlEntry:
    value: Any
    expires_at: float


@dataclass(slots=True)
class _SourceSlice:
    source_health: SourceHealth
    positions: list[Position]
    balances: list[CashBalance]
    # Realized P&L from this source's historical fills, grouped by the
    # native trade currency. Empty when the broker doesn't expose
    # historical fills (IBKR without Flex Statements) or the fetch
    # failed. Conversion to base currency happens at the aggregator
    # level using the same FX table that drives the position totals.
    realized_by_currency: dict[str, Decimal] = field(default_factory=dict)


_BUY_SIDES: frozenset[str] = frozenset({"buy", "b", "bot", "buys"})
_SELL_SIDES: frozenset[str] = frozenset({"sell", "s", "sld", "sells"})


def _parse_since_iso(value: str) -> datetime:
    """Parse an ISO-8601 timestamp from the API into a tz-aware UTC datetime.

    Accepts the trailing-Z form Flutter sends (`2024-01-01T00:00:00Z`)
    as well as standard `+HH:MM` offsets. A naive datetime is assumed
    to be UTC.
    """
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _txn_fetch_window(since: str | None) -> tuple[str | None, str]:
    """Map a precise ``since`` onto a stable, day-floored broker fetch window.

    Returns ``(fetch_since_iso, cache_bucket)``.

    The snapshot path derives ``since`` from ``now() - 90d`` on every
    request, so it carries microsecond precision that would bust a
    per-window cache on each refresh. Flooring to the UTC day keeps the
    cache key stable so repeated portfolio refreshes within a day reuse a
    single broker call. ``since=None`` ("all history") maps to
    ``(None, "all")``, preserving the full-history read pattern for the
    Transactions screen.
    """
    if since is None:
        return None, "all"
    floored = _parse_since_iso(since).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    iso = floored.isoformat()
    return iso, iso


def _compute_realized_by_currency(
    transactions: list[Transaction],
) -> dict[str, Decimal]:
    """FIFO-match buys to sells per (symbol, currency) and sum realized P&L.

    Buys go onto a per-(symbol, currency) FIFO queue. Sells consume from
    the head of the queue; the matched-quantity × (sell_price − buy_price)
    is added to that currency's running realized total.

    Unmatched sells (short sales or sells whose corresponding buys
    predate the visible history window) are skipped rather than guessed
    at — we'd otherwise have to fabricate a cost basis, which would
    silently produce wrong numbers.
    """
    by_key: dict[tuple[str, str], list[Transaction]] = {}
    for t in transactions:
        if t.symbol is None or t.quantity is None or t.price is None:
            continue
        if t.side is None:
            continue
        side = t.side.strip().lower()
        if side not in _BUY_SIDES and side not in _SELL_SIDES:
            continue
        currency = (t.currency or "").upper()
        if not currency:
            continue
        by_key.setdefault((t.symbol, currency), []).append(t)

    realized: dict[str, Decimal] = {}
    for (_symbol, currency), txns in by_key.items():
        txns.sort(key=lambda x: x.timestamp)
        buy_queue: list[tuple[Decimal, Decimal]] = []  # (qty_remaining, price)
        sym_realized = Decimal("0")
        for t in txns:
            side = (t.side or "").strip().lower()
            qty = abs(t.quantity)
            price = t.price
            if side in _BUY_SIDES:
                buy_queue.append((qty, price))
                continue
            # Sell — consume FIFO
            remaining = qty
            while remaining > 0 and buy_queue:
                buy_qty, buy_price = buy_queue[0]
                matched = min(remaining, buy_qty)
                sym_realized += (price - buy_price) * matched
                remaining -= matched
                if remaining == 0 and matched == buy_qty:
                    buy_queue.pop(0)
                elif matched == buy_qty:
                    buy_queue.pop(0)
                else:
                    buy_queue[0] = (buy_qty - matched, buy_price)
        realized[currency] = realized.get(currency, Decimal("0")) + sym_realized
    return realized


class InMemoryConnectionRepository:
    """Simple repository implementation used by tests and local boot."""

    def __init__(self, connections: list[Connection] | None = None) -> None:
        self._connections = connections or []

    async def list_connections(self, user_id: str) -> list[Connection]:
        _ = user_id
        return [c for c in self._connections if c.enabled]


class PortfolioAggregator:
    """Aggregates data from all enabled source adapters for a user."""

    def __init__(
        self,
        *,
        connections: ConnectionRepository,
        adapters: AdapterRegistry,
        fx: FxService,
        ttl_seconds: float = 10.0,
        transactions_ttl_seconds: float = 600.0,
        adapter_factory: AdapterFactory | None = None,
        vault_service: CredentialVaultService | None = None,
        status_publisher: ConnectionStatusEventPublisher | None = None,
    ) -> None:
        self._connections = connections
        self._adapters = adapters
        self._fx = fx
        self._ttl_seconds = ttl_seconds
        # Transactions are historical fills — closed trades from last
        # month don't change. Re-fetching them every 10 s just burns
        # broker API quota and slows down the snapshot (Longbridge's
        # `history_executions` walks paginated history backwards). A
        # 10-minute TTL is plenty: realized P&L only moves when you
        # place a new sell order, and a 10-min lag on that metric is
        # fine for a portfolio tracker.
        self._transactions_ttl_seconds = transactions_ttl_seconds
        self._adapter_factory = adapter_factory
        self._vault_service = vault_service
        self._status_publisher = status_publisher or NoopConnectionStatusPublisher()
        self._cache: dict[tuple[str, str, str], _TtlEntry] = {}

    async def get_snapshot(
        self,
        user_id: str,
        *,
        base_currency: str = "USD",
        credential_context: AggregationCredentialContext | None = None,
    ) -> PortfolioSnapshot:
        """Get a unified portfolio snapshot for one user."""
        base = base_currency.upper()
        connections = await self._enabled_connections(user_id)
        log = logging.getLogger("mbp.aggregator")
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "get_snapshot user_id=%s connections_found=%d kinds=%s wrapped_keys=%s",
                user_id,
                len(connections),
                [c.source for c in connections],
                list(credential_context.wrapped_tokens_by_connection.keys())
                if credential_context is not None
                and credential_context.wrapped_tokens_by_connection
                else [],
            )

        tasks = [
            self._collect_source_snapshot(user_id, conn, credential_context=credential_context)
            for conn in connections
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        positions: list[Position] = []
        balances: list[CashBalance] = []
        health: list[SourceHealth] = []
        realized_currencies: dict[str, Decimal] = {}

        for conn, result in zip(connections, results, strict=True):
            if isinstance(result, BaseException):
                health.append(
                    SourceHealth(
                        source=conn.source,
                        status=SourceHealthStatus.DOWN,
                        message=str(result),
                    )
                )
                continue
            positions.extend(result.positions)
            balances.extend(result.balances)
            health.append(result.source_health)
            for ccy, amount in result.realized_by_currency.items():
                realized_currencies[ccy] = (
                    realized_currencies.get(ccy, Decimal("0")) + amount
                )

        fx_pairs = self._pairs_for_snapshot(base, positions, balances)
        # Also need FX legs for every currency that contributed realized
        # P&L — they may not appear in current positions/balances.
        fx_pairs = fx_pairs | {
            (ccy, base) for ccy in realized_currencies if ccy != base
        }
        fx_by_pair = await self._fx.get_rates_for(fx_pairs)
        fx_rates = list(fx_by_pair.values())

        total_market_value = self._sum_positions(positions, base, fx_by_pair)
        total_balance_value = self._sum_balances(balances, base, fx_by_pair)
        total_unrealized = self._sum_unrealized(positions, base, fx_by_pair)

        # Convert per-currency realized totals to base. Currencies with
        # no available FX rate are silently dropped (same policy as
        # _sum_balances) — better to under-report than to fabricate.
        total_realized = Decimal("0")
        for ccy, amount in realized_currencies.items():
            total_realized += PortfolioAggregator._to_base(
                amount, ccy, base, fx_by_pair
            )

        total_return = total_realized + total_unrealized

        snapshot = PortfolioSnapshot(
            as_of=datetime.now(UTC),
            base_currency=base,
            positions=positions,
            balances=balances,
            fx_rates=fx_rates,
            source_health=health,
            total_market_value=total_market_value + total_balance_value,
            total_unrealized_pnl=total_unrealized,
            total_realized_pnl=total_realized,
            total_return=total_return,
        )
        return snapshot

    async def get_positions(
        self,
        user_id: str,
        *,
        source: str | None = None,
        credential_context: AggregationCredentialContext | None = None,
    ) -> PartialResult[Position]:
        connections = await self._filtered_connections(user_id, source=source)
        tasks = [
            self._list_positions_for_connection(
                user_id,
                conn,
                credential_context=credential_context,
            )
            for conn in connections
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[Position] = []
        health: list[SourceHealth] = []
        for conn, result in zip(connections, results, strict=True):
            if isinstance(result, BaseException):
                health.append(
                    SourceHealth(source=conn.source, status=SourceHealthStatus.DOWN, message=str(result))
                )
                continue
            items.extend(result)
            health.append(SourceHealth(source=conn.source, status=SourceHealthStatus.OK))
        return PartialResult(items=items, source_health=health)

    async def get_balances(
        self,
        user_id: str,
        *,
        source: str | None = None,
        credential_context: AggregationCredentialContext | None = None,
    ) -> PartialResult[CashBalance]:
        connections = await self._filtered_connections(user_id, source=source)
        tasks = [
            self._list_balances_for_connection(
                user_id,
                conn,
                credential_context=credential_context,
            )
            for conn in connections
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        items: list[CashBalance] = []
        health: list[SourceHealth] = []
        for conn, result in zip(connections, results, strict=True):
            if isinstance(result, BaseException):
                health.append(
                    SourceHealth(source=conn.source, status=SourceHealthStatus.DOWN, message=str(result))
                )
                continue
            items.extend(result)
            health.append(SourceHealth(source=conn.source, status=SourceHealthStatus.OK))
        return PartialResult(items=items, source_health=health)

    async def get_transactions(
        self,
        user_id: str,
        *,
        source: str | None = None,
        since: str | None = None,
        limit: int | None = None,
        credential_context: AggregationCredentialContext | None = None,
    ) -> PartialResult[Transaction]:
        connections = await self._filtered_connections(user_id, source=source)
        tasks = [
            self._list_transactions_for_connection(
                user_id,
                conn,
                since=since,
                limit=limit,
                credential_context=credential_context,
            )
            for conn in connections
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        items: list[Transaction] = []
        health: list[SourceHealth] = []
        for conn, result in zip(connections, results, strict=True):
            if isinstance(result, BaseException):
                health.append(
                    SourceHealth(source=conn.source, status=SourceHealthStatus.DOWN, message=str(result))
                )
                continue
            items.extend(result)
            health.append(SourceHealth(source=conn.source, status=SourceHealthStatus.OK))

        # Broker adapters aren't 100% consistent about timezone-awareness:
        # IBKR fills come back tz-aware (UTC offsets in the wire format),
        # but some Longbridge / Futu paths emit naive datetimes when the
        # raw response doesn't carry a timezone. Sorting a mixed list
        # raises `TypeError: can't compare offset-naive and offset-aware
        # datetimes`. Coerce both sides to a single key shape — treat
        # naive timestamps as UTC, which matches every adapter's intent.
        def _sort_key(row: Transaction) -> datetime:
            ts = row.timestamp
            if ts.tzinfo is None:
                return ts.replace(tzinfo=UTC)
            return ts.astimezone(UTC)

        items.sort(key=_sort_key, reverse=True)
        if limit is not None:
            items = items[:limit]
        return PartialResult(items=items, source_health=health)

    async def _collect_source_snapshot(
        self,
        user_id: str,
        conn: Connection,
        *,
        credential_context: AggregationCredentialContext | None,
    ) -> _SourceSlice:
        pos_task = self._list_positions_for_connection(
            user_id,
            conn,
            credential_context=credential_context,
        )
        bal_task = self._list_balances_for_connection(
            user_id,
            conn,
            credential_context=credential_context,
        )
        # Pull recent history for realized P&L. Full 3-year walks with
        # per-chunk throttle delays (Futu: 3.1 s × 36 chunks = ~2 min)
        # block the portfolio snapshot unacceptably. 90 days captures
        # most meaningful realized activity; the dedicated /v1/transactions
        # endpoint handles deeper history with explicit since= filtering.
        snapshot_since = (datetime.now(UTC) - timedelta(days=90)).isoformat()
        txn_task = self._list_transactions_for_connection(
            user_id,
            conn,
            since=snapshot_since,
            limit=200,
            credential_context=credential_context,
        )
        pos_result, bal_result, txn_result = await asyncio.gather(
            pos_task, bal_task, txn_task, return_exceptions=True
        )

        positions: list[Position] = []
        balances: list[CashBalance] = []
        errors: list[str] = []

        if isinstance(pos_result, BaseException):
            errors.append(f"positions: {pos_result}")
        else:
            positions = pos_result

        if isinstance(bal_result, BaseException):
            errors.append(f"balances: {bal_result}")
        else:
            balances = bal_result

        # Transactions are a soft dependency for realized P&L. Failures
        # are NOT promoted into source_health — the source is still
        # "OK" for the dashboard's current-state view; we just lose the
        # historical-fills computation.
        realized_by_currency: dict[str, Decimal] = {}
        if not isinstance(txn_result, BaseException):
            try:
                realized_by_currency = _compute_realized_by_currency(txn_result)
            except Exception:  # noqa: BLE001
                logging.getLogger("mbp.aggregator").exception(
                    "compute_realized_by_currency failed for connection %s",
                    conn.connection_id,
                )

        if not errors:
            status = SourceHealthStatus.OK
            message = None
        elif positions or balances:
            status = SourceHealthStatus.DEGRADED
            message = "; ".join(errors)
        else:
            status = SourceHealthStatus.DOWN
            message = "; ".join(errors)

        return _SourceSlice(
            source_health=SourceHealth(
                source=conn.source,
                status=status,
                message=message,
                last_success_at=datetime.now(UTC) if status is SourceHealthStatus.OK else None,
            ),
            positions=positions,
            balances=balances,
            realized_by_currency=realized_by_currency,
        )

    async def _list_positions_for_connection(
        self,
        user_id: str,
        conn: Connection,
        *,
        credential_context: AggregationCredentialContext | None,
    ) -> list[Position]:
        key = (user_id, conn.connection_id, "positions")

        async def _load() -> list[Position]:
            adapter = await self._resolve_adapter(
                user_id=user_id,
                conn=conn,
                purpose="list_positions",
                credential_context=credential_context,
            )
            try:
                rows = await adapter.list_positions()
                await self._emit_status(
                    user_id=user_id,
                    connection_id=conn.connection_id,
                    status=ConnectionSyncStatus.OK,
                )
                return rows
            except Exception as exc:  # noqa: BLE001 - propagate while recording status
                await self._emit_status(
                    user_id=user_id,
                    connection_id=conn.connection_id,
                    status=ConnectionSyncStatus.ERROR,
                    error_message=str(exc),
                )
                raise

        return await self._cached(key, _load)

    async def _list_balances_for_connection(
        self,
        user_id: str,
        conn: Connection,
        *,
        credential_context: AggregationCredentialContext | None,
    ) -> list[CashBalance]:
        key = (user_id, conn.connection_id, "balances")

        async def _load() -> list[CashBalance]:
            adapter = await self._resolve_adapter(
                user_id=user_id,
                conn=conn,
                purpose="list_balances",
                credential_context=credential_context,
            )
            try:
                rows = await adapter.list_balances()
                await self._emit_status(
                    user_id=user_id,
                    connection_id=conn.connection_id,
                    status=ConnectionSyncStatus.OK,
                )
                return rows
            except Exception as exc:  # noqa: BLE001 - propagate while recording status
                await self._emit_status(
                    user_id=user_id,
                    connection_id=conn.connection_id,
                    status=ConnectionSyncStatus.ERROR,
                    error_message=str(exc),
                )
                raise

        return await self._cached(key, _load)

    async def _list_transactions_for_connection(
        self,
        user_id: str,
        conn: Connection,
        *,
        since: str | None,
        limit: int | None,
        credential_context: AggregationCredentialContext | None,
    ) -> list[Transaction]:
        # Pass a *bounded* window through to the broker rather than always
        # pulling full history. Some brokers (notably Futu) rate-limit their
        # history query so aggressively (10 calls / 30 s, ~3 s/chunk) that a
        # multi-year walk takes minutes and blows the upstream proxy timeout,
        # hanging the entire portfolio refresh. We instead fetch only what
        # the caller asked for (`since`), cache it for
        # `transactions_ttl_seconds`, and serve narrower reads from that
        # cached window. The fetch window is floored to the UTC day so the
        # snapshot path — which derives `since` from `now() - 90d` on every
        # request — reuses a single broker call per day instead of busting
        # the cache each refresh. This still collapses repeated taps (same
        # window → same cache bucket) so Futu's "10/30 s" and Longbridge's
        # 429002 rate limits don't fire.
        fetch_since, window_bucket = _txn_fetch_window(since)

        async def _load() -> list[Transaction]:
            adapter = await self._resolve_adapter(
                user_id=user_id,
                conn=conn,
                purpose="list_transactions",
                credential_context=credential_context,
            )
            try:
                rows = await adapter.list_transactions(since=fetch_since, limit=None)
                await self._emit_status(
                    user_id=user_id,
                    connection_id=conn.connection_id,
                    status=ConnectionSyncStatus.OK,
                )
                return rows
            except Exception as exc:  # noqa: BLE001
                await self._emit_status(
                    user_id=user_id,
                    connection_id=conn.connection_id,
                    status=ConnectionSyncStatus.ERROR,
                    error_message=str(exc),
                )
                raise

        key = (user_id, conn.connection_id, "transactions", window_bucket)
        all_rows = await self._cached(
            key, _load, ttl_seconds=self._transactions_ttl_seconds
        )

        # Fast path: caller wanted the full (unfiltered, unlimited) window.
        if since is None and limit is None:
            return all_rows

        # Server-side filter by `since` so we don't drag the entire
        # history across the wire when the UI only wants the last 30
        # days.
        filtered: list[Transaction]
        if since is not None:
            since_dt = _parse_since_iso(since)

            def _at_or_after(row: Transaction) -> bool:
                ts = row.timestamp
                # Defensive: some adapters emit naive datetimes from
                # broker payloads. Compare both sides as tz-aware UTC.
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)
                return ts >= since_dt

            filtered = [r for r in all_rows if _at_or_after(r)]
        else:
            filtered = list(all_rows)

        if limit is not None and limit >= 0:
            # The aggregator sorts the merged set newest-first before
            # applying `limit` again at the outer layer, so we pre-trim
            # here by recency to avoid keeping huge per-connection
            # lists in memory.
            filtered.sort(key=lambda r: r.timestamp, reverse=True)
            filtered = filtered[:limit]

        return filtered

    async def _resolve_adapter(
        self,
        *,
        user_id: str,
        conn: Connection,
        purpose: str,
        credential_context: AggregationCredentialContext | None,
    ) -> SourceAdapter:
        if self._adapter_factory is not None and self._vault_service is not None:
            plaintext = await self._resolve_plaintext_credentials(
                user_id=user_id,
                conn=conn,
                purpose=purpose,
                credential_context=credential_context,
            )
            return self._adapter_factory.for_connection(
                connection_kind=conn.source,
                plaintext_creds=plaintext,
            )

        adapter = self._adapters.for_connection(conn)
        if adapter is None:
            msg = f"No adapter configured for source '{conn.source}'"
            raise LookupError(msg)
        return adapter

    async def _resolve_plaintext_credentials(
        self,
        *,
        user_id: str,
        conn: Connection,
        purpose: str,
        credential_context: AggregationCredentialContext | None,
    ) -> str:
        vault = self._vault_service
        if vault is None:
            msg = "vault service is required for credential resolution"
            raise LookupError(msg)

        if conn.server_key_mode:
            return await vault.use_credential(
                user_id=user_id,
                connection_id=conn.connection_id,
                purpose=purpose,
                client_token=None,
                fn=lambda plaintext: plaintext,
            )

        token = credential_context.token_for(conn.connection_id) if credential_context else None
        if token is None:
            msg = "missing wrapped credentials for e2e connection"
            raise WrappedCredentialError(msg)
        if credential_context is None or credential_context.unwrap_key is None:
            msg = "missing unwrap key for wrapped credentials"
            raise WrappedCredentialError(msg)
        return unwrap_from_backend(token, key=credential_context.unwrap_key)

    async def _emit_status(
        self,
        *,
        user_id: str,
        connection_id: str,
        status: ConnectionSyncStatus,
        error_message: str | None = None,
    ) -> None:
        await self._status_publisher.publish(
            user_id=user_id,
            event=ConnectionSyncStatusEvent(
                connection_id=connection_id,
                status=status,
                last_sync_at=datetime.now(UTC),
                error_message=error_message,
            ),
        )

    async def _enabled_connections(self, user_id: str) -> list[Connection]:
        rows = await self._connections.list_connections(user_id)
        return [row for row in rows if row.enabled]

    async def _filtered_connections(self, user_id: str, *, source: str | None) -> list[Connection]:
        rows = await self._enabled_connections(user_id)
        if source is None:
            return rows
        source_l = source.lower()
        return [row for row in rows if row.source.lower() == source_l]

    async def _cached(
        self,
        key: tuple[str, str, str],
        loader: Callable[[], Awaitable[T]],
        *,
        ttl_seconds: float | None = None,
    ) -> T:
        cached = self._cache.get(key)
        now = time.monotonic()
        if cached is not None and cached.expires_at > now:
            return cast(T, cached.value)

        fresh = await loader()
        effective_ttl = ttl_seconds if ttl_seconds is not None else self._ttl_seconds
        self._cache[key] = _TtlEntry(value=fresh, expires_at=now + effective_ttl)
        return fresh

    @staticmethod
    def _pairs_for_snapshot(
        base_currency: str,
        positions: list[Position],
        balances: list[CashBalance],
    ) -> set[tuple[str, str]]:
        pairs: set[tuple[str, str]] = set()
        for pos in positions:
            curr = pos.currency.upper()
            if curr != base_currency:
                pairs.add((curr, base_currency))
        for bal in balances:
            curr = bal.currency.upper()
            if curr != base_currency:
                pairs.add((curr, base_currency))
        return pairs

    @staticmethod
    def _sum_positions(
        positions: list[Position],
        base_currency: str,
        fx_by_pair: dict[tuple[str, str], FxRate],
    ) -> Decimal:
        total = Decimal("0")
        for pos in positions:
            value = pos.market_value
            if value is None and pos.last_price is not None:
                value = pos.last_price * pos.quantity
            if value is None:
                continue
            total += PortfolioAggregator._to_base(value, pos.currency, base_currency, fx_by_pair)
        return total

    @staticmethod
    def _sum_balances(
        balances: list[CashBalance],
        base_currency: str,
        fx_by_pair: dict[tuple[str, str], FxRate],
    ) -> Decimal:
        total = Decimal("0")
        for bal in balances:
            total += PortfolioAggregator._to_base(bal.amount, bal.currency, base_currency, fx_by_pair)
        return total

    @staticmethod
    def _sum_unrealized(
        positions: list[Position],
        base_currency: str,
        fx_by_pair: dict[tuple[str, str], FxRate],
    ) -> Decimal:
        total = Decimal("0")
        for pos in positions:
            if pos.unrealized_pnl is None:
                continue
            total += PortfolioAggregator._to_base(
                pos.unrealized_pnl,
                pos.currency,
                base_currency,
                fx_by_pair,
            )
        return total

    @staticmethod
    def _to_base(
        amount: Decimal,
        currency: str,
        base_currency: str,
        fx_by_pair: dict[tuple[str, str], FxRate],
    ) -> Decimal:
        curr = currency.upper()
        if curr == base_currency:
            return amount
        pair = (curr, base_currency)
        fx = fx_by_pair.get(pair)
        if fx is None:
            # Contributing 0 keeps a single unsupported currency from
            # blanking the whole dashboard (detailed-design §7.2), but a
            # SILENT zero is how a misconfigured FX provider erases every
            # foreign holding from net worth without anyone noticing —
            # and every allocation percentage is computed against that
            # total (§8.3). Never let it pass unlogged.
            if amount != 0:
                logging.getLogger("mbp.aggregator").warning(
                    "no FX rate for %s->%s: excluding %s %s from the base-currency "
                    "total. Check MBP_FX_PROVIDER — exchangerate.host requires an "
                    "API key since late 2024 and returns missing_access_key without "
                    "one; frankfurter needs no key.",
                    curr,
                    base_currency,
                    amount,
                    curr,
                )
            return Decimal("0")
        return amount * fx.rate


__all__ = [
    "AdapterRegistry",
    "AggregationCredentialContext",
    "ConnectionRepository",
    "InMemoryConnectionRepository",
    "PortfolioAggregator",
]
