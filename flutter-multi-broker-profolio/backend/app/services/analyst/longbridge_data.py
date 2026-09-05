"""Longbridge market-data fetcher used by the analyst service.

This module is intentionally separate from the per-user LongBridge
*trading* adapter in `app.adapters.longbridge`. Reasons:

1. Analyst reports use only **public market data** (quotes, candles,
   capital flow, calc indexes). The data is identical regardless of
   which authenticated account fetches it, so a single shared service
   account works for every user — no need to pull each user's
   e2e-encrypted credentials through the request just to get a TSLA
   quote.

2. The service-account credentials live in env vars
   (`MBP_LB_ANALYST_APP_KEY` / `_APP_SECRET` / `_ACCESS_TOKEN`). When
   any of them is unset, every fetcher raises `MarketDataUnavailable`
   so the API layer can return a clean 503 with "Configure
   MBP_LB_ANALYST_* to enable analyst reports" instead of crashing.

3. Calls are batched and cached at the service layer above; this file
   only owns the SDK plumbing and a singleton client.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import threading
import types
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, UTC
from typing import Any


_LOG = logging.getLogger("mbp.analyst.longbridge")


class MarketDataUnavailable(RuntimeError):
    """Raised when the analyst service-account credentials aren't set
    or the SDK can't be loaded. The API layer translates this to a 503
    with a configuration-hint message."""


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    turnover: float


@dataclass(frozen=True, slots=True)
class QuoteSnapshot:
    symbol: str
    last_price: float | None
    prev_close: float | None
    open: float | None
    high: float | None
    low: float | None
    volume: float | None
    turnover: float | None
    timestamp: datetime | None


@dataclass(frozen=True, slots=True)
class CapitalDistribution:
    """Net buy/sell flow buckets in NATIVE currency of the symbol."""

    in_large: float
    in_medium: float
    in_small: float
    out_large: float
    out_medium: float
    out_small: float

    @property
    def net_large(self) -> float:
        return self.in_large - self.out_large

    @property
    def net_medium(self) -> float:
        return self.in_medium - self.out_medium

    @property
    def net_small(self) -> float:
        return self.in_small - self.out_small

    @property
    def net_total(self) -> float:
        return self.net_large + self.net_medium + self.net_small


@dataclass(frozen=True, slots=True)
class CalcIndexes:
    market_cap: float | None
    pe: float | None
    pb: float | None
    dps_rate: float | None
    turnover_rate: float | None


# ---------------------------------------------------------------------------
# Singleton fetcher
# ---------------------------------------------------------------------------


_SDK_TYPES_LOCK = threading.Lock()
_SDK_TYPES: tuple[type[Any], type[Any], Any, Any] | None = None
_QUOTE_CTX_LOCK = threading.Lock()
_QUOTE_CTX: Any | None = None


def _load_sdk_types() -> tuple[type[Any], type[Any], Any, Any]:
    """Return (Config, QuoteContext, Period enum, CalcIndex enum).

    Cached so we only import once per process. Each call returns the
    same tuple.
    """
    global _SDK_TYPES
    if _SDK_TYPES is not None:
        return _SDK_TYPES
    with _SDK_TYPES_LOCK:
        if _SDK_TYPES is not None:
            return _SDK_TYPES
        try:
            module = importlib.import_module("longbridge.openapi")
        except ModuleNotFoundError as exc:
            raise MarketDataUnavailable(
                "longbridge SDK not installed in backend container"
            ) from exc
        if not isinstance(module, types.ModuleType):
            raise MarketDataUnavailable("longbridge SDK module didn't load")

        config_cls = getattr(module, "Config", None)
        quote_cls = getattr(module, "QuoteContext", None)
        period_enum = getattr(module, "Period", None)
        calc_index_enum = getattr(module, "CalcIndex", None)
        if not isinstance(config_cls, type) or not isinstance(quote_cls, type):
            raise MarketDataUnavailable(
                "longbridge SDK missing Config / QuoteContext classes"
            )
        _SDK_TYPES = (config_cls, quote_cls, period_enum, calc_index_enum)
        return _SDK_TYPES


def _service_credentials() -> tuple[str, str, str]:
    app_key = os.getenv("MBP_LB_ANALYST_APP_KEY", "").strip()
    app_secret = os.getenv("MBP_LB_ANALYST_APP_SECRET", "").strip()
    access_token = os.getenv("MBP_LB_ANALYST_ACCESS_TOKEN", "").strip()
    missing = [
        name
        for name, value in (
            ("MBP_LB_ANALYST_APP_KEY", app_key),
            ("MBP_LB_ANALYST_APP_SECRET", app_secret),
            ("MBP_LB_ANALYST_ACCESS_TOKEN", access_token),
        )
        if not value
    ]
    if missing:
        raise MarketDataUnavailable(
            "Analyst service-account credentials unset: " + ", ".join(missing)
        )
    return app_key, app_secret, access_token


def _ensure_quote_context() -> Any:
    """Return the singleton QuoteContext, constructing it on first use."""
    global _QUOTE_CTX
    if _QUOTE_CTX is not None:
        return _QUOTE_CTX
    with _QUOTE_CTX_LOCK:
        if _QUOTE_CTX is not None:
            return _QUOTE_CTX
        config_cls, quote_cls, _period, _calc = _load_sdk_types()
        app_key, app_secret, access_token = _service_credentials()
        if hasattr(config_cls, "from_app_key"):
            config = config_cls.from_app_key(app_key, app_secret, access_token)
        elif hasattr(config_cls, "from_apikey"):
            config = config_cls.from_apikey(app_key, app_secret, access_token)
        else:
            raise MarketDataUnavailable(
                "longbridge Config missing from_app_key constructor"
            )
        _QUOTE_CTX = quote_cls(config)
        _LOG.info("analyst Longbridge QuoteContext constructed")
        return _QUOTE_CTX


def reset_for_tests() -> None:
    """Clear the singleton + cached SDK types. Tests only."""
    global _QUOTE_CTX, _SDK_TYPES
    _QUOTE_CTX = None
    _SDK_TYPES = None


# ---------------------------------------------------------------------------
# Public fetchers — coroutines, all run SDK calls on a worker thread
# ---------------------------------------------------------------------------


async def fetch_quotes(symbols: Sequence[str]) -> list[QuoteSnapshot]:
    if not symbols:
        return []
    ctx = _ensure_quote_context()
    raw = await asyncio.to_thread(ctx.quote, list(symbols))
    out: list[QuoteSnapshot] = []
    for q in _iter_response(raw, attr="secu_quote"):
        out.append(
            QuoteSnapshot(
                symbol=str(getattr(q, "symbol", "")),
                last_price=_to_float_or_none(getattr(q, "last_done", None)),
                prev_close=_to_float_or_none(getattr(q, "prev_close", None)),
                open=_to_float_or_none(getattr(q, "open", None)),
                high=_to_float_or_none(getattr(q, "high", None)),
                low=_to_float_or_none(getattr(q, "low", None)),
                volume=_to_float_or_none(getattr(q, "volume", None)),
                turnover=_to_float_or_none(getattr(q, "turnover", None)),
                timestamp=_to_datetime_or_none(getattr(q, "timestamp", None)),
            )
        )
    return out


async def fetch_daily_candles(symbol: str, count: int = 200) -> list[Candle]:
    """Fetch the last `count` daily candles, oldest first.

    Defaults to 200 — enough for SMA(200), MACD(26), and the swing
    detector on the same call.
    """
    if count <= 0:
        return []
    _, _, period_enum, _ = _load_sdk_types()
    ctx = _ensure_quote_context()
    period_day = getattr(period_enum, "Day", None) if period_enum else None
    if period_day is None:
        raise MarketDataUnavailable("longbridge SDK missing Period.Day enum")

    adjust_type = await _resolve_adjust_type()
    raw = await asyncio.to_thread(
        ctx.candlesticks,
        symbol,
        period_day,
        count,
        adjust_type,
    )
    out: list[Candle] = []
    for row in _iter_response(raw, attr=None):
        ts = _to_datetime_or_none(getattr(row, "timestamp", None))
        if ts is None:
            continue
        out.append(
            Candle(
                time=ts,
                open=_to_float_or_none(getattr(row, "open", None)) or 0.0,
                high=_to_float_or_none(getattr(row, "high", None)) or 0.0,
                low=_to_float_or_none(getattr(row, "low", None)) or 0.0,
                close=_to_float_or_none(getattr(row, "close", None)) or 0.0,
                volume=_to_float_or_none(getattr(row, "volume", None)) or 0.0,
                turnover=_to_float_or_none(getattr(row, "turnover", None)) or 0.0,
            )
        )
    out.sort(key=lambda c: c.time)
    return out


async def fetch_capital_distribution(symbol: str) -> CapitalDistribution | None:
    ctx = _ensure_quote_context()
    raw = await asyncio.to_thread(ctx.capital_distribution, symbol)
    cap_in = getattr(raw, "capital_in", None)
    cap_out = getattr(raw, "capital_out", None)
    if cap_in is None or cap_out is None:
        return None
    return CapitalDistribution(
        in_large=_to_float_or_none(getattr(cap_in, "large", None)) or 0.0,
        in_medium=_to_float_or_none(getattr(cap_in, "medium", None)) or 0.0,
        in_small=_to_float_or_none(getattr(cap_in, "small", None)) or 0.0,
        out_large=_to_float_or_none(getattr(cap_out, "large", None)) or 0.0,
        out_medium=_to_float_or_none(getattr(cap_out, "medium", None)) or 0.0,
        out_small=_to_float_or_none(getattr(cap_out, "small", None)) or 0.0,
    )


async def fetch_calc_indexes(symbol: str) -> CalcIndexes | None:
    """Fetch P/E, P/B, market cap, dividend yield, turnover rate.

    SDK 4.1.0 attribute names differ from intuition:
      * Enum: `PeTtmRatio`, `PbRatio`, `TotalMarketValue`,
        `DividendRatioTtm`, `TurnoverRate` (NOT `Pe` / `Pb` /
        `MarketCap`).
      * Response attrs on `SecurityCalcIndex`: `pe_ttm_ratio`,
        `pb_ratio`, `total_market_value`, `dividend_ratio_ttm`,
        `turnover_rate`.
    Earlier versions of this file used the intuitive names; they all
    silently resolved to None and every report came back without
    valuation/earnings sub-scores.

    We keep the legacy fallback attribute names as `or`-chains so
    test fakes that hand-build response objects with `pe`/`pb` keep
    working — the production SDK populates the long names.
    """
    _, _, _, calc_index_enum = _load_sdk_types()
    if calc_index_enum is None:
        return None
    indexes = [
        getattr(calc_index_enum, name, None)
        for name in (
            "LastDone",  # SDK requires at least one — used by sanity check
            "PeTtmRatio",
            "PbRatio",
            "TotalMarketValue",
            "DividendRatioTtm",
            "TurnoverRate",
        )
    ]
    indexes = [idx for idx in indexes if idx is not None]
    if not indexes:
        return None
    ctx = _ensure_quote_context()
    raw = await asyncio.to_thread(ctx.calc_indexes, [symbol], indexes)
    rows = _iter_response(raw, attr=None)
    if not rows:
        return None
    row = rows[0]
    return CalcIndexes(
        market_cap=_to_float_or_none(
            getattr(row, "total_market_value", None)
            or getattr(row, "mkt_cap", None)
        ),
        pe=_to_float_or_none(
            getattr(row, "pe_ttm_ratio", None) or getattr(row, "pe", None)
        ),
        pb=_to_float_or_none(
            getattr(row, "pb_ratio", None) or getattr(row, "pb", None)
        ),
        dps_rate=_to_float_or_none(getattr(row, "dividend_ratio_ttm", None)),
        turnover_rate=_to_float_or_none(getattr(row, "turnover_rate", None)),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_adjust_type() -> Any:
    """Return the SDK's `AdjustType.NoAdjust` if it exists.

    Some SDK versions require the adjust_type parameter; older ones
    default it. Best-effort.
    """
    try:
        module = importlib.import_module("longbridge.openapi")
    except ModuleNotFoundError:
        return None
    enum = getattr(module, "AdjustType", None)
    if enum is None:
        return None
    return getattr(enum, "NoAdjust", None) or getattr(enum, "ForwardAdjust", None)


def _iter_response(raw: Any, *, attr: str | None) -> list[Any]:
    """SDK responses occasionally come back wrapped (e.g. response with
    `.secu_quote` list) and occasionally as a bare list. Normalize.
    """
    if raw is None:
        return []
    if attr:
        inner = getattr(raw, attr, None)
        if inner is not None:
            try:
                return list(inner)
            except TypeError:
                pass
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        return list(raw)
    except TypeError:
        return [raw]


def _to_float_or_none(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, str) and not v.strip():
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_datetime_or_none(v: Any) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        if v.tzinfo is None:
            return v.replace(tzinfo=UTC)
        return v
    if isinstance(v, (int, float)):
        try:
            return datetime.fromtimestamp(v, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
