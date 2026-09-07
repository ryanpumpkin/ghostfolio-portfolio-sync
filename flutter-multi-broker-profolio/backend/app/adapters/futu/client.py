"""Concrete Futu OpenD client backed by the official `futu-api` package."""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
from decimal import Decimal, InvalidOperation
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

from app.adapters._common import PermanentError, TransientError
from app.core.settings import get_settings

_LOG = logging.getLogger("mbp.futu.client")

# ── Process-level trade-context cache ────────────────────────────────────────
# OpenSecTradeContext takes ~40 s to establish its TCP+protocol handshake with
# OpenD. FutuOpenDClient is instantiated per HTTP request (via AdapterFactory),
# so without this cache every portfolio refresh would pay the 40 s cost.
# Keyed by (kind, host, port, conn_key_path) so different connections get
# different contexts; same configuration reuses the warm, already-connected
# instance. `kind` is in the key because the two contexts are different
# classes reading different accounts — without it the crypto context would
# evict the securities one and each call would re-handshake.
_TRADE_CTX_CACHE: dict[tuple[str, str, int, str], Any] = {}
_TRADE_CTX_CACHE_LOCK = threading.Lock()

# ── Trading accounts ─────────────────────────────────────────────────────────
# Futu keeps crypto in a SEPARATE trading account reached through a separate
# context class. `OpenSecTradeContext` covers HK and US stock under one
# account id no matter which market it filters on, but it cannot see crypto
# at all — which is why a real BTC holding was absent from the portfolio
# total while every stock position reconciled perfectly.
#
# Each kind costs its own ~40 s handshake, so this is a real price, not a
# free union. It is worth paying: a silently missing holding understates the
# total and nothing in the numbers says so.
_SEC = "sec"
_CRYPTO = "crypto"

# Futu OpenD rate-limits `history_deal_list_query` to 10 calls per
# 30 seconds (server-side). Sleeping ~3.1 s between chunks keeps us
# safely under the cap while still letting a multi-year pagination
# walk complete eventually.
_HISTORY_THROTTLE_SECONDS = 3.1

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many request",
    "too many requests",
    "quota",
    "throttle",
    "temporarily unavailable",
    # Futu's own rate-limit wording. Before adding this, the broker's
    # "Maximum 10 times per 30 seconds" responses bubbled up as
    # opaque RuntimeErrors instead of TransientErrors, which made
    # them un-retryable and tripped the source_health "down" state
    # instead of being absorbed as a soft fault.
    "high frequency",
    "maximum 10 times",
    "timeout",
)
_CREDENTIAL_MARKERS = (
    "unlock",
    "password",
    "pwd",
    "credential",
    "permission",
    "unauthorized",
    "forbidden",
    "auth",
)
# Default lookback when the caller asks for "all history" (since=None).
# Was 90 days, which hid every buy-and-hold user's actual trades.
# Futu's `history_deal_list_query` retains ~2 years server-side; ask
# for 3 to leave headroom and let the broker return whatever it has.
# The chunked pagination below walks the range 30 days at a time so a
# wider window doesn't make the request slower per call — just more
# chunks if the broker actually has data going back that far.
_DEFAULT_TX_WINDOW_DAYS = 365 * 3
_HISTORY_CHUNK_DAYS = 30
#: `order_fee_query` takes a list, so fees cost far fewer round trips than
#: one call per order would. Kept modest so a rate limit costs one batch.
_FEE_BATCH_SIZE = 50


class FutuOpenDClient:  # pragma: no cover - SDK-bound; exercised via real OpenD integration test
    """Thin async wrapper around OpenD quote/trade contexts."""

    def __init__(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        trd_env: str | None = None,
        acc_id: int | None = None,
        quote_poll_interval: float = 1.0,
    ) -> None:
        settings = get_settings()
        self._host = host or settings.futu_opend_host
        self._port = port or settings.futu_opend_port
        self._trd_env_raw = trd_env or "REAL"
        self._acc_id = acc_id
        self._quote_poll_interval = quote_poll_interval
        self._sdk = _load_futu_sdk()
        # Per-instance cache key for the process-level _TRADE_CTX_CACHE.
        # The actual context lives in the module-level dict so it survives
        # across requests (each request creates a new FutuOpenDClient).
        settings = get_settings()
        self._conn_key = settings.futu_conn_key_path or ""
        self._enable_crypto = settings.futu_enable_crypto
        #: Reset per history walk; the broker's rate limit is per account,
        #: not per context, so the throttle has to span both walks.
        self._chunks_done = 0

    def _cache_key(self, kind: str) -> tuple[str, str, int, str]:
        return (kind, self._host, self._port, self._conn_key)

    def _kinds(self) -> list[str]:
        """Which trading accounts this client reads, securities first."""
        return [_SEC, _CRYPTO] if self._enable_crypto else [_SEC]

    # Per-call timeouts (seconds). The first call pays ~40 s to connect;
    # subsequent calls reuse _shared_trd_ctx so they are near-instant.
    _FETCH_TIMEOUT = 30.0

    #: Budget for the FIRST call on a cold process, which pays for the
    #: OpenSecTradeContext handshake as well as the query itself.
    #:
    #: This has to exceed the ~40 s handshake documented above, and
    #: `_FETCH_TIMEOUT` does not: a cold `fetch_positions` was timing out
    #: at 30 s having never reached the query, which is arithmetically
    #: impossible to succeed. Long-running backends never saw it because
    #: the context was already warm; a one-shot sync job hits it every
    #: single time.
    _CONNECT_TIMEOUT = 120.0
    _PING_TIMEOUT = 10.0

    def _budget(self, base: float) -> float:
        """Add the handshake cost to the first call, and only the first.

        A warm context answers in well under a second, so keeping the
        tight budget for subsequent calls preserves the fast-failure
        behaviour that keeps a dead OpenD from blocking a refresh.
        """
        kinds = self._kinds()
        cold = [k for k in kinds if self._cache_key(k) not in _TRADE_CTX_CACHE]
        # Accounts are queried one after another, so the query budget
        # scales with how many there are; each cold context adds its
        # handshake ON TOP rather than replacing it. `max()` of the two
        # is the same arithmetic mistake that made cold `fetch_positions`
        # time out before it ever reached the query — a three-year deal
        # walk across two accounts needs ~600 s of querying and ~240 s of
        # handshake, and a budget of max(300, 240) cannot cover either.
        #
        # A warm single account still gets exactly `base`, which is what
        # keeps a dead OpenD from blocking a refresh.
        return base * len(kinds) + self._CONNECT_TIMEOUT * len(cold)

    async def fetch_positions(self) -> list[dict[str, Any]]:
        budget = self._budget(self._FETCH_TIMEOUT)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch_positions_sync),
                timeout=budget,
            )
        except TimeoutError as exc:
            raise TransientError(
                f"fetch_positions timed out after {budget}s"
            ) from exc

    async def fetch_accounts(self) -> list[dict[str, Any]]:
        budget = self._budget(self._FETCH_TIMEOUT)
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch_accounts_sync),
                timeout=budget,
            )
        except TimeoutError as exc:
            raise TransientError(
                f"fetch_accounts timed out after {budget}s"
            ) from exc

    async def fetch_history_deals(
        self,
        *,
        since: str | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        budget = self._budget(self._FETCH_TIMEOUT * 10)  # walk is multi-chunk
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_history_deals_sync, since, limit),
                timeout=budget,
            )
        except TimeoutError as exc:
            raise TransientError(
                f"fetch_history_deals timed out after {budget}s"
            ) from exc
        return rows[:limit] if limit is not None and limit >= 0 else rows

    async def ping(self) -> bool:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._ping_sync),
                timeout=self._PING_TIMEOUT,
            )
        except TimeoutError as exc:
            raise TransientError(
                f"ping timed out after {self._PING_TIMEOUT}s"
            ) from exc

    async def subscribe_quotes(self, symbols: list[str]) -> AsyncIterator[dict[str, Any]]:
        if not symbols:
            return
        quote_ctx = self._sdk.OpenQuoteContext(host=self._host, port=self._port)
        try:
            sub_type = getattr(getattr(self._sdk, "SubType", None), "QUOTE", None)
            if sub_type is not None:
                ret, data = quote_ctx.subscribe(symbols, [sub_type], is_first_push=False)
                _ensure_ok(ret, data, operation="subscribe")

            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            error_queue: asyncio.Queue[Exception] = asyncio.Queue()
            push_wired = False

            handler_base = getattr(self._sdk, "StockQuoteHandlerBase", None)
            if isinstance(handler_base, type) and hasattr(quote_ctx, "set_handler"):
                sdk = self._sdk

                class _QuoteHandler(handler_base):  # type: ignore[misc, valid-type]
                    def on_recv_rsp(self, rsp_pb: Any) -> tuple[Any, Any]:
                        ret_code, payload = super().on_recv_rsp(rsp_pb)
                        if ret_code != sdk.RET_OK:
                            message = _extract_error_message(payload)
                            lowered = message.lower()
                            exc: Exception
                            if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
                                exc = TransientError(message)
                            elif any(marker in lowered for marker in _CREDENTIAL_MARKERS):
                                exc = PermanentError(message)
                            else:
                                exc = RuntimeError(message)
                            loop.call_soon_threadsafe(error_queue.put_nowait, exc)
                            return ret_code, payload

                        for row in _rows_from_payload(payload):
                            if "timestamp" not in row:
                                row["timestamp"] = datetime.now(UTC).isoformat()
                            loop.call_soon_threadsafe(queue.put_nowait, row)
                        return ret_code, payload

                quote_ctx.set_handler(_QuoteHandler())
                push_wired = True

            # Immediate snapshot so callers don't wait indefinitely for first tick.
            ret, frame = quote_ctx.get_stock_quote(symbols)
            _ensure_ok(ret, frame, operation="get_stock_quote")
            for row in _rows_from_payload(frame):
                if "timestamp" not in row:
                    row["timestamp"] = datetime.now(UTC).isoformat()
                yield row

            if push_wired:
                while True:
                    if not error_queue.empty():
                        raise await error_queue.get()
                    try:
                        row = await asyncio.wait_for(queue.get(), timeout=10.0)
                        yield row
                    except TimeoutError:
                        # Keep socket warm while waiting for market movement.
                        await asyncio.sleep(0)
            else:
                while True:
                    ret, frame = quote_ctx.get_stock_quote(symbols)
                    _ensure_ok(ret, frame, operation="get_stock_quote")
                    rows = _rows_from_payload(frame)
                    for row in rows:
                        if "timestamp" not in row:
                            row["timestamp"] = datetime.now(UTC).isoformat()
                        yield row
                    await asyncio.sleep(self._quote_poll_interval)
        finally:
            quote_ctx.close()

    def _new_trade_context(self, kind: str) -> Any:
        """Create a brand-new trade context for one account (~40 s)."""
        settings = get_settings()
        factory = (
            self._sdk.OpenCryptoTradeContext
            if kind == _CRYPTO
            else self._sdk.OpenSecTradeContext
        )
        if settings.futu_conn_key_path:
            # The futu SDK registers the RSA private key globally via
            # SysConfig.set_init_rsa_file() rather than as a constructor
            # argument. Once set, constructing the context with
            # is_encrypt=True is enough to encrypt trade-side calls.
            self._sdk.SysConfig.set_init_rsa_file(settings.futu_conn_key_path)
            return factory(host=self._host, port=self._port, is_encrypt=True)
        return factory(host=self._host, port=self._port)

    def _get_shared_trade_ctx(self, kind: str = _SEC) -> Any:
        """Return the process-level persistent trade context for this config.

        OpenSecTradeContext takes ~40 s to establish the TCP+protocol
        handshake with OpenD. FutuOpenDClient is re-created per request,
        but the context lives in the module-level _TRADE_CTX_CACHE so the
        expensive connect cost is paid only ONCE for the backend process
        lifetime. Thread-safe via _TRADE_CTX_CACHE_LOCK.
        """
        key = self._cache_key(kind)
        ctx = _TRADE_CTX_CACHE.get(key)
        if ctx is not None:
            return ctx
        with _TRADE_CTX_CACHE_LOCK:
            ctx = _TRADE_CTX_CACHE.get(key)
            if ctx is None:
                _LOG.info(
                    "futu: establishing %s trade context "
                    "(one-time ~40 s handshake)", kind,
                )
                ctx = self._new_trade_context(kind)
                _TRADE_CTX_CACHE[key] = ctx
                _LOG.info("futu: %s trade context ready", kind)
        return ctx

    def _query_each(
        self,
        operation: str,
        run: Any,
    ) -> list[dict[str, Any]]:
        """Run one query against every enabled account and union the rows.

        Crypto is best-effort. An account without the permission, or one
        OpenD cannot reach, answers with an error — and losing the entire
        stock portfolio to that would be a poor trade. A securities
        failure propagates, because that *is* the portfolio.

        The failure is logged rather than swallowed: a crypto holding
        that quietly stops arriving would understate the total exactly
        the way its absence did before this existed.
        """
        rows: list[dict[str, Any]] = []
        for kind in self._kinds():
            try:
                rows.extend(run(self._get_shared_trade_ctx(kind), kind))
            except Exception as exc:  # noqa: BLE001 — see docstring
                if kind == _SEC:
                    raise
                _LOG.warning(
                    "futu: %s unavailable for the %s account (%s); "
                    "those holdings are NOT included",
                    operation, kind, exc,
                )
        return rows

    def _trd_env(self) -> Any:
        trd_env_enum = getattr(self._sdk, "TrdEnv", None)
        if trd_env_enum is None:
            return self._trd_env_raw
        return getattr(trd_env_enum, self._trd_env_raw.upper(), trd_env_enum.REAL)

    def _query_kwargs(self, kind: str) -> dict[str, Any]:
        """Common query arguments for one account.

        `acc_id` is deliberately securities-only: it names an account in
        *that* list, and the crypto account has an id of its own.
        Forwarding the stock account's id to the crypto context asks for
        an account that does not exist there.
        """
        kwargs: dict[str, Any] = {"trd_env": self._trd_env()}
        if self._acc_id is not None and kind == _SEC:
            kwargs["acc_id"] = self._acc_id
        return kwargs

    def _fetch_positions_sync(self) -> list[dict[str, Any]]:
        def run(ctx: Any, kind: str) -> list[dict[str, Any]]:
            ret, frame = ctx.position_list_query(**self._query_kwargs(kind))
            _ensure_ok(ret, frame, operation="position_list_query")
            return _rows_from_payload(frame)

        return self._query_each("position_list_query", run)

    def _fetch_accounts_sync(self) -> list[dict[str, Any]]:
        def run(ctx: Any, kind: str) -> list[dict[str, Any]]:
            ret, frame = ctx.accinfo_query(**self._query_kwargs(kind))
            _ensure_ok(ret, frame, operation="accinfo_query")
            return _rows_from_payload(frame)

        return self._query_each("accinfo_query", run)

    def _fetch_history_deals_sync(
        self,
        since: str | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        start_at, end_at = _history_window(since)
        # The rate limit is per account, not per context, so the chunk
        # counter spans both walks: without it the crypto walk's first
        # call would land immediately after the securities walk's last.
        self._chunks_done = 0

        def run(ctx: Any, kind: str) -> list[dict[str, Any]]:
            rows = self._walk_deals(ctx, kind, start_at, end_at, limit)
            return self._attach_fees(ctx, kind, _dedupe_deals(rows))

        return self._query_each("history_deal_list_query", run)

    def _walk_deals(
        self,
        trade_ctx: Any,
        kind: str,
        start_at: datetime,
        end_at: datetime,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        # Walk newest → oldest so the user sees recent trades first and
        # the loop can bail early once `limit` is reached without
        # missing the most relevant rows. Between chunks we sleep
        # ~3.1 s to stay under Futu's 10-calls-per-30-s rate limit.
        # If a chunk raises a `TransientError` (rate limit / network
        # blip) after we've already collected something, return the
        # partial result instead of propagating — the cache will store
        # whatever we managed to fetch and subsequent calls reuse it
        # rather than starting from scratch.
        out: list[dict[str, Any]] = []
        for chunk_start, cursor_end in _history_chunks(start_at, end_at):
            if self._chunks_done > 0:
                # Pre-throttle so consecutive chunks never collide with
                # the broker's window. Cheap if the loop only runs
                # once.
                time.sleep(_HISTORY_THROTTLE_SECONDS)

            try:
                kwargs = self._query_kwargs(kind)
                kwargs["start"] = _futu_day(chunk_start)
                kwargs["end"] = _futu_day(cursor_end)
                ret, frame = trade_ctx.history_deal_list_query(**kwargs)
                _ensure_ok(ret, frame, operation="history_deal_list_query")
                out.extend(_rows_from_payload(frame))
            except TransientError as exc:
                if out:
                    _LOG.warning(
                        "futu %s history walk aborted at chunk %d (%s..%s): "
                        "%s; returning %d partial rows",
                        kind,
                        self._chunks_done,
                        chunk_start,
                        cursor_end,
                        exc,
                        len(out),
                    )
                    break
                raise

            self._chunks_done += 1
            if limit is not None and limit >= 0 and len(out) >= limit:
                break
        return out

    def _attach_fees(
        self, trade_ctx: Any, kind: str, deals: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Add commission to each deal from `order_fee_query`.

        Futu's deal rows carry code, qty, price, side and timestamps —
        and no fee at all. Every Futu trade was therefore pushed with
        fee=0, which understates cost basis and overstates every return
        built on it.

        The fee is charged per *order*, so it is apportioned across that
        order's deals in proportion to quantity: a single order that
        filled in several deals would otherwise be charged its whole
        commission once per fill.

        A failed lookup costs those deals their fee, not the deals
        themselves — the same trade-off as §5.6, because a trade is the
        load-bearing number and a missing fee is small and recoverable.
        """
        fee_fn = getattr(trade_ctx, "order_fee_query", None)
        order_ids = sorted({
            str(row.get("order_id") or "") for row in deals
        } - {""})
        if not callable(fee_fn) or not order_ids:
            return deals

        fees: dict[str, Decimal] = {}
        for batch_start in range(0, len(order_ids), _FEE_BATCH_SIZE):
            batch = order_ids[batch_start:batch_start + _FEE_BATCH_SIZE]
            if batch_start:
                time.sleep(_HISTORY_THROTTLE_SECONDS)
            try:
                kwargs: dict[str, Any] = {"order_id_list": batch}
                if self._acc_id is not None and kind == _SEC:
                    kwargs["acc_id"] = self._acc_id
                ret, frame = fee_fn(**kwargs)
                _ensure_ok(ret, frame, operation="order_fee_query")
            except Exception as exc:  # noqa: BLE001 — fees only, never the walk
                _LOG.warning("futu: order_fee_query failed for %d order(s): %s",
                             len(batch), exc)
                continue
            for row in _rows_from_payload(frame):
                order_id = str(row.get("order_id") or "")
                amount = _dec_or_none(
                    row.get("fee_amount")
                    if row.get("fee_amount") is not None
                    else row.get("feeAmount")
                )
                if order_id and amount is not None:
                    fees[order_id] = abs(amount)

        if not fees:
            _LOG.warning("futu: no commission resolved; fees stay 0")
            return deals

        filled: dict[str, Decimal] = {}
        for row in deals:
            order_id = str(row.get("order_id") or "")
            filled[order_id] = filled.get(order_id, Decimal("0")) + (
                _dec_or_none(row.get("qty")) or Decimal("0")
            )

        for row in deals:
            order_id = str(row.get("order_id") or "")
            fee = fees.get(order_id)
            if fee is None:
                continue
            whole = filled.get(order_id) or Decimal("0")
            share = _dec_or_none(row.get("qty")) or Decimal("0")
            row["fee"] = fee * (share / whole) if whole else fee
            # Futu charges in the deal's own currency, and the deal does
            # not name one — the market does. HK codes settle in HKD, US
            # in USD; anything else is left unset rather than guessed,
            # because a fee labelled with the wrong currency is worse
            # than one with none (the mapper drops it, §5.6).
            market = str(row.get("deal_market") or "").upper()
            currency = {"HK": "HKD", "US": "USD"}.get(market)
            if currency:
                row["fee_currency"] = currency

        _LOG.info("futu: commission attached for %d order(s)", len(fees))
        return deals

    def _ping_sync(self) -> bool:
        quote_ctx = self._sdk.OpenQuoteContext(host=self._host, port=self._port)
        try:
            ret, data = quote_ctx.get_global_state()
            _ensure_ok(ret, data, operation="get_global_state")
            return True
        finally:
            quote_ctx.close()


def _load_futu_sdk() -> Any:  # pragma: no cover - imports real futu SDK; covered by integration test
    try:
        return importlib.import_module("futu")
    except ModuleNotFoundError as exc:
        msg = "futu-api is not installed; add the `futu-api` dependency"
        raise RuntimeError(msg) from exc


def _dec_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "n/a", "none"}:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _rows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    if hasattr(payload, "to_dict"):
        rows = payload.to_dict("records")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    if isinstance(payload, dict):
        return [payload]
    msg = f"unsupported futu payload type: {type(payload)!r}"
    raise RuntimeError(msg)


def _extract_error_message(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        value = payload.get("msg") or payload.get("message") or payload.get("err_msg")
        if value is not None:
            return str(value)
    return str(payload)


def _ensure_ok(ret_code: Any, payload: Any, *, operation: str) -> None:
    sdk = _load_futu_sdk()
    if ret_code == sdk.RET_OK:
        return
    message = f"{operation} failed: {_extract_error_message(payload)}"
    lowered = message.lower()
    if any(marker in lowered for marker in _RATE_LIMIT_MARKERS):
        raise TransientError(message)
    if any(marker in lowered for marker in _CREDENTIAL_MARKERS):
        raise PermanentError(message)
    raise RuntimeError(message)


def _history_chunks(
    start_at: datetime, end_at: datetime
) -> list[tuple[datetime, datetime]]:
    """Split a window into non-overlapping day-aligned chunks, newest first.

    `history_deal_list_query` takes dates, not timestamps, and treats
    both ends as inclusive. An earlier version stepped back by one
    microsecond, which lands on the *same day* — so every chunk boundary
    day was queried twice and every deal on it came back twice.

    That is not a cosmetic duplicate. Four Futu deals were pushed to
    Ghostfolio in duplicate this way, all four on exact 30-day
    boundaries (2024-12-16, 2025-01-15, 2025-07-14). One added 20 SOFI
    shares the account did not hold; another duplicated a SELL, which
    drove the replayed quantity negative and made the opening-balance
    pass invent a BUY to cover it.
    """
    chunks: list[tuple[datetime, datetime]] = []
    cursor_end = end_at
    while cursor_end >= start_at:
        chunk_start = max(cursor_end - timedelta(days=_HISTORY_CHUNK_DAYS), start_at)
        chunks.append((chunk_start, cursor_end))
        if chunk_start <= start_at:
            break
        # A whole day back, because the query is day-granular and
        # inclusive at both ends.
        cursor_end = chunk_start - timedelta(days=1)
    return chunks


def _dedupe_deals(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per deal id, first occurrence wins.

    Belt and braces beside `_history_chunks`. The window arithmetic is
    now correct, but a duplicate here is silent and expensive — it
    becomes a position the account does not hold — and Futu is free to
    repeat a row for reasons of its own. Rows with no deal id are left
    alone rather than collapsed onto each other.
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    duplicates = 0
    for row in rows:
        deal_id = str(row.get("deal_id") or "")
        if not deal_id:
            out.append(row)
            continue
        if deal_id in seen:
            duplicates += 1
            continue
        seen.add(deal_id)
        out.append(row)
    if duplicates:
        _LOG.warning(
            "futu returned %d repeated deal(s); kept one of each", duplicates
        )
    return out


def _history_window(since: str | None) -> tuple[datetime, datetime]:
    end_at = datetime.now(UTC)
    if since is None:
        start_at = end_at - timedelta(days=_DEFAULT_TX_WINDOW_DAYS)
    else:
        start_at = _parse_since(since)
    return start_at, end_at


def _parse_since(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _futu_day(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d")


__all__ = ["FutuOpenDClient"]
