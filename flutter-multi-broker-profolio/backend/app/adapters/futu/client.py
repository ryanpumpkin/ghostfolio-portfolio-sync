"""Concrete Futu OpenD client backed by the official `futu-api` package."""

from __future__ import annotations

import asyncio
import importlib
import logging
import threading
import time
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
# Keyed by (host, port, conn_key_path) so different connections get different
# contexts; same configuration reuses the warm, already-connected instance.
_TRADE_CTX_CACHE: dict[tuple[str, int, str], Any] = {}
_TRADE_CTX_CACHE_LOCK = threading.Lock()

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
        self._ctx_cache_key: tuple[str, int, str] = (
            self._host,
            self._port,
            settings.futu_conn_key_path or "",
        )

    # Per-call timeouts (seconds). The first call pays ~40 s to connect;
    # subsequent calls reuse _shared_trd_ctx so they are near-instant.
    # Unlock timeout must cover the one-time connection cost (40 s) plus
    # the actual unlock RPC (~2 s), so 60 s is the safe lower bound.
    # After the first successful connection all fetches finish in < 5 s.
    _UNLOCK_TIMEOUT = 60.0
    _FETCH_TIMEOUT = 30.0
    _PING_TIMEOUT = 10.0

    async def unlock_trade(self, password: str) -> None:
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._unlock_trade_sync, password),
                timeout=self._UNLOCK_TIMEOUT,
            )
        except TimeoutError as exc:
            raise TransientError(
                f"unlock_trade timed out after {self._UNLOCK_TIMEOUT}s — OpenD may be unresponsive"
            ) from exc

    async def lock_trade(self) -> None:
        await asyncio.to_thread(self._lock_trade_sync)

    async def fetch_positions(self) -> list[dict[str, Any]]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch_positions_sync),
                timeout=self._FETCH_TIMEOUT,
            )
        except TimeoutError as exc:
            raise TransientError(
                f"fetch_positions timed out after {self._FETCH_TIMEOUT}s"
            ) from exc

    async def fetch_accounts(self) -> list[dict[str, Any]]:
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._fetch_accounts_sync),
                timeout=self._FETCH_TIMEOUT,
            )
        except TimeoutError as exc:
            raise TransientError(
                f"fetch_accounts timed out after {self._FETCH_TIMEOUT}s"
            ) from exc

    async def fetch_history_deals(
        self,
        *,
        since: str | None,
        limit: int | None,
    ) -> list[dict[str, Any]]:
        try:
            rows = await asyncio.wait_for(
                asyncio.to_thread(self._fetch_history_deals_sync, since, limit),
                timeout=self._FETCH_TIMEOUT * 10,  # history walk is multi-chunk
            )
        except TimeoutError as exc:
            raise TransientError(
                f"fetch_history_deals timed out after {self._FETCH_TIMEOUT * 10}s"
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

    def _new_trade_context(self) -> Any:
        """Create a brand-new OpenSecTradeContext (expensive — ~40 s)."""
        settings = get_settings()
        if settings.futu_conn_key_path:
            # The futu SDK registers the RSA private key globally via
            # SysConfig.set_init_rsa_file() rather than as a constructor
            # argument. Once set, constructing the context with
            # is_encrypt=True is enough to encrypt trade-side calls.
            self._sdk.SysConfig.set_init_rsa_file(settings.futu_conn_key_path)
            return self._sdk.OpenSecTradeContext(
                host=self._host,
                port=self._port,
                is_encrypt=True,
            )
        return self._sdk.OpenSecTradeContext(host=self._host, port=self._port)

    def _get_shared_trade_ctx(self) -> Any:
        """Return the process-level persistent trade context for this config.

        OpenSecTradeContext takes ~40 s to establish the TCP+protocol
        handshake with OpenD. FutuOpenDClient is re-created per request,
        but the context lives in the module-level _TRADE_CTX_CACHE so the
        expensive connect cost is paid only ONCE for the backend process
        lifetime. Thread-safe via _TRADE_CTX_CACHE_LOCK.
        """
        ctx = _TRADE_CTX_CACHE.get(self._ctx_cache_key)
        if ctx is not None:
            return ctx
        with _TRADE_CTX_CACHE_LOCK:
            ctx = _TRADE_CTX_CACHE.get(self._ctx_cache_key)
            if ctx is None:
                _LOG.info("futu: establishing trade context (one-time ~40 s handshake)")
                ctx = self._new_trade_context()
                _TRADE_CTX_CACHE[self._ctx_cache_key] = ctx
                _LOG.info("futu: trade context ready")
        return ctx

    def _trd_env(self) -> Any:
        trd_env_enum = getattr(self._sdk, "TrdEnv", None)
        if trd_env_enum is None:
            return self._trd_env_raw
        return getattr(trd_env_enum, self._trd_env_raw.upper(), trd_env_enum.REAL)

    def _unlock_trade_sync(self, password: str) -> None:
        import hashlib

        trade_ctx = self._get_shared_trade_ctx()
        # Futu OpenD expects an MD5 hash, not plaintext.
        # If the caller already supplied a 32-char hex digest, use it
        # directly; otherwise hash it first.
        is_md5 = len(password) == 32 and all(c in "0123456789abcdefABCDEF" for c in password)
        password_md5 = password if is_md5 else hashlib.md5(password.encode()).hexdigest()
        try:
            ret, data = trade_ctx.unlock_trade(password_md5=password_md5)
        except TypeError:
            # Older SDK versions used `password=` (plaintext).
            ret, data = trade_ctx.unlock_trade(password=password)
        _ensure_ok(ret, data, operation="unlock_trade")
        # Note: do NOT close the shared context here.

    def _lock_trade_sync(self) -> None:
        trade_ctx = self._get_shared_trade_ctx()
        ret, data = trade_ctx.unlock_trade(is_unlock=False)
        _ensure_ok(ret, data, operation="lock_trade")

    def _fetch_positions_sync(self) -> list[dict[str, Any]]:
        trade_ctx = self._get_shared_trade_ctx()
        kwargs = {"trd_env": self._trd_env()}
        if self._acc_id is not None:
            kwargs["acc_id"] = self._acc_id
        ret, frame = trade_ctx.position_list_query(**kwargs)
        _ensure_ok(ret, frame, operation="position_list_query")
        return _rows_from_payload(frame)

    def _fetch_accounts_sync(self) -> list[dict[str, Any]]:
        trade_ctx = self._get_shared_trade_ctx()
        kwargs = {"trd_env": self._trd_env()}
        if self._acc_id is not None:
            kwargs["acc_id"] = self._acc_id
        ret, frame = trade_ctx.accinfo_query(**kwargs)
        _ensure_ok(ret, frame, operation="accinfo_query")
        return _rows_from_payload(frame)

    def _fetch_history_deals_sync(
        self,
        since: str | None,
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
        trade_ctx = self._get_shared_trade_ctx()
        start_at, end_at = _history_window(since)
        out: list[dict[str, Any]] = []
        chunks_done = 0
        cursor_end = end_at
        while cursor_end >= start_at:
            chunk_start = max(
                cursor_end - timedelta(days=_HISTORY_CHUNK_DAYS),
                start_at,
            )

            if chunks_done > 0:
                # Pre-throttle so consecutive chunks never collide with
                # the broker's window. Cheap if the loop only runs
                # once.
                time.sleep(_HISTORY_THROTTLE_SECONDS)

            try:
                kwargs: dict[str, Any] = {
                    "trd_env": self._trd_env(),
                    "start": _futu_day(chunk_start),
                    "end": _futu_day(cursor_end),
                }
                if self._acc_id is not None:
                    kwargs["acc_id"] = self._acc_id
                ret, frame = trade_ctx.history_deal_list_query(**kwargs)
                _ensure_ok(ret, frame, operation="history_deal_list_query")
                out.extend(_rows_from_payload(frame))
            except TransientError as exc:
                if out:
                    _LOG.warning(
                        "futu history walk aborted at chunk %d (%s..%s): %s; "
                        "returning %d partial rows",
                        chunks_done,
                        chunk_start,
                        cursor_end,
                        exc,
                        len(out),
                    )
                    break
                raise

            chunks_done += 1
            if limit is not None and limit >= 0 and len(out) >= limit:
                break
            if chunk_start <= start_at:
                break
            cursor_end = chunk_start - timedelta(microseconds=1)
        return out

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
