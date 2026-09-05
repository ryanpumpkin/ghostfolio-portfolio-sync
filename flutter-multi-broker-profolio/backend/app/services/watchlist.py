"""Watchlist service: in-memory storage, signal generation, and email digest."""

from __future__ import annotations

import asyncio
import smtplib
import ssl
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import StrEnum
from typing import Any

import httpx

from app.core.logging import get_logger
from app.models.domain import PortfolioSnapshot, Position
from app.services.analyst.indicators import rsi as _indicator_rsi, sma as _indicator_sma
from app.services.analyst.longbridge_data import MarketDataUnavailable
from app.services.analyst.symbol_map import position_to_longbridge_symbol

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------


class SignalKind(StrEnum):
    STRONG_BUY = "strong_buy"
    BUY = "buy"
    SELL = "sell"
    STRONG_SELL = "strong_sell"
    HOLD = "hold"


def _kind_for_score(score: int) -> SignalKind:
    """Map the ensemble net score (-3..+3) to a verdict tier.

    All three factors agreeing (±3) is a STRONG verdict; two of three (±2)
    is a plain BUY/SELL; anything weaker is HOLD."""
    if score >= 3:
        return SignalKind.STRONG_BUY
    if score == 2:
        return SignalKind.BUY
    if score <= -3:
        return SignalKind.STRONG_SELL
    if score == -2:
        return SignalKind.SELL
    return SignalKind.HOLD


class WatchlistEntry:
    __slots__ = ("symbol", "added_at")

    def __init__(self, symbol: str, added_at: datetime) -> None:
        self.symbol = symbol
        self.added_at = added_at


class WatchlistSignal:
    __slots__ = ("symbol", "signal", "price", "currency", "rsi", "sma20", "sma50", "reason", "refreshed_at")

    def __init__(
        self,
        *,
        symbol: str,
        signal: SignalKind,
        price: float | None,
        currency: str,
        rsi: float | None,
        sma20: float | None,
        sma50: float | None,
        reason: str,
        refreshed_at: datetime,
    ) -> None:
        self.symbol = symbol
        self.signal = signal
        self.price = price
        self.currency = currency
        self.rsi = rsi
        self.sma20 = sma20
        self.sma50 = sma50
        self.reason = reason
        self.refreshed_at = refreshed_at


# ---------------------------------------------------------------------------
# In-memory watchlist repository
# ---------------------------------------------------------------------------


def _coerce_dt(raw: Any) -> datetime:
    """Best-effort parse of a stored timestamp into an aware UTC datetime."""
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


class WatchlistRepository:
    """Per-user watchlist persisted to Firestore at ``watchlists/{uid}``.

    Each user's watchlist is a single document holding a ``symbols`` array
    (``[{symbol, added_at}]``) plus the user's ``email`` (so the digest worker
    can address mail per-user). A process-level in-memory store is used as a
    fallback when Firebase Admin is not configured (local/test) or Firestore is
    momentarily unreachable — this preserves the previous behaviour without
    crashing, while production survives restarts.

    The daily-digest "last sent" date is persisted alongside, at
    ``watchlists_meta/digest``, so a restart after the digest hour does not
    re-send the same day's email.
    """

    _META_COLLECTION = "watchlists_meta"
    _DIGEST_DOC = "digest"

    def __init__(self, *, firestore_client: Any | None = None) -> None:
        self._firestore_client = firestore_client
        self._mem: dict[str, list[WatchlistEntry]] = defaultdict(list)
        self._mem_email: dict[str, str] = {}
        self._mem_digest_date: str | None = None

    # -- Firestore plumbing -------------------------------------------------

    def _client(self) -> Any | None:
        if self._firestore_client is not None:
            return self._firestore_client
        try:
            from firebase_admin import firestore

            return firestore.client()
        except Exception:
            return None

    @staticmethod
    def _doc(client: Any, user_id: str) -> Any:
        return client.collection("watchlists").document(user_id)

    @staticmethod
    def _entry_from_dict(item: dict[str, Any]) -> WatchlistEntry | None:
        sym = item.get("symbol")
        if not isinstance(sym, str) or not sym.strip():
            return None
        return WatchlistEntry(symbol=sym.strip().upper(), added_at=_coerce_dt(item.get("added_at")))

    async def _read(self, client: Any, user_id: str) -> tuple[list[WatchlistEntry], str | None]:
        snap = await asyncio.to_thread(self._doc(client, user_id).get)
        if not getattr(snap, "exists", False):
            return [], None
        data = snap.to_dict() or {}
        entries: list[WatchlistEntry] = []
        for item in data.get("symbols") or []:
            if isinstance(item, dict):
                entry = self._entry_from_dict(item)
                if entry is not None:
                    entries.append(entry)
        email = data.get("email") if isinstance(data.get("email"), str) else None
        return entries, email

    async def _write(
        self, client: Any, user_id: str, entries: list[WatchlistEntry], email: str | None
    ) -> None:
        payload: dict[str, Any] = {
            "symbols": [{"symbol": e.symbol, "added_at": e.added_at.isoformat()} for e in entries],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if email:
            payload["email"] = email
        await asyncio.to_thread(lambda: self._doc(client, user_id).set(payload, merge=True))

    # -- Public API (async) -------------------------------------------------

    async def list(self, user_id: str) -> list[WatchlistEntry]:
        client = self._client()
        if client is None:
            return list(self._mem[user_id])
        try:
            entries, _ = await self._read(client, user_id)
            return entries
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchlist_read_failed", user_id=user_id, error=str(exc))
            return list(self._mem[user_id])

    async def symbols(self, user_id: str) -> list[str]:
        return [e.symbol for e in await self.list(user_id)]

    async def add(self, user_id: str, symbol: str, *, email: str | None = None) -> WatchlistEntry:
        symbol = symbol.strip().upper()
        client = self._client()
        if client is None:
            if email:
                self._mem_email[user_id] = email
            for e in self._mem[user_id]:
                if e.symbol == symbol:
                    return e
            entry = WatchlistEntry(symbol=symbol, added_at=datetime.now(UTC))
            self._mem[user_id].append(entry)
            return entry
        entries, cur_email = await self._read(client, user_id)
        for e in entries:
            if e.symbol == symbol:
                return e
        entry = WatchlistEntry(symbol=symbol, added_at=datetime.now(UTC))
        entries.append(entry)
        await self._write(client, user_id, entries, email or cur_email)
        return entry

    async def remove(self, user_id: str, symbol: str) -> bool:
        symbol = symbol.strip().upper()
        client = self._client()
        if client is None:
            before = len(self._mem[user_id])
            self._mem[user_id] = [e for e in self._mem[user_id] if e.symbol != symbol]
            return len(self._mem[user_id]) < before
        entries, email = await self._read(client, user_id)
        kept = [e for e in entries if e.symbol != symbol]
        if len(kept) == len(entries):
            return False
        await self._write(client, user_id, kept, email)
        return True

    async def all_user_ids(self) -> list[str]:
        client = self._client()
        if client is None:
            return list(self._mem.keys())
        try:
            result = await asyncio.to_thread(client.collection("watchlists").get)
            try:
                docs = list(result)
            except TypeError:
                docs = list(getattr(result, "docs", []) or [])
            return [d.id for d in docs]
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchlist_list_users_failed", error=str(exc))
            return list(self._mem.keys())

    async def email_for(self, user_id: str) -> str | None:
        client = self._client()
        if client is None:
            return self._mem_email.get(user_id)
        try:
            _, email = await self._read(client, user_id)
            return email
        except Exception:  # noqa: BLE001
            return self._mem_email.get(user_id)

    # -- Digest state (restart-safe "last sent" marker) ---------------------

    async def get_digest_state(self) -> str | None:
        client = self._client()
        if client is None:
            return self._mem_digest_date
        try:
            doc = client.collection(self._META_COLLECTION).document(self._DIGEST_DOC)
            snap = await asyncio.to_thread(doc.get)
            if not getattr(snap, "exists", False):
                return None
            data = snap.to_dict() or {}
            value = data.get("last_sent_date")
            return value if isinstance(value, str) else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchlist_digest_state_read_failed", error=str(exc))
            return self._mem_digest_date

    async def set_digest_state(self, date_str: str) -> None:
        self._mem_digest_date = date_str
        client = self._client()
        if client is None:
            return
        try:
            doc = client.collection(self._META_COLLECTION).document(self._DIGEST_DOC)
            await asyncio.to_thread(lambda: doc.set({"last_sent_date": date_str}, merge=True))
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchlist_digest_state_write_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Signal engine using Yahoo Finance
# ---------------------------------------------------------------------------

_YF_HOSTS = [
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
]
_YF_PARAMS = {
    "range": "1y",
    "interval": "1d",
    "includePrePost": "false",
    "events": "none",
}
_YF_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://finance.yahoo.com/",
}

# Simple in-memory cache: symbol → (WatchlistSignal, cached_at)
_signal_cache: dict[str, tuple[WatchlistSignal, datetime]] = {}
_CACHE_TTL_SECONDS = 900  # 15 minutes
# Yahoo rate-limits aggressively (HTTP 429). Retry each symbol across hosts a
# few times with growing backoff; if it still fails, we serve the last cached
# value (even if stale) rather than reporting "data unavailable".
_YF_MAX_ATTEMPTS = 3
_YF_RETRY_BACKOFF = 1.5  # seconds, multiplied by attempt index


# ---------------------------------------------------------------------------
# Signal model — trend/momentum ensemble (backtested)
# ---------------------------------------------------------------------------
# The verdict is a majority vote of three factors, each independently validated
# on a 16-symbol basket over 200 trading days to lead future returns — where the
# old RSI/SMA *mean-reversion* rule did not (its BUY days actually trailed
# buy-and-hold). The fix is to read these as *trend/momentum*, i.e. buy strength:
#   1. 3-month price momentum   (BUY days averaged ~+4% over buy-and-hold)
#   2. SMA20-vs-SMA50 trend
#   3. RSI(14) read as a trend gauge (>55 strong / <45 weak), not overbought/sold
# BUY when the net score is >= +2, SELL when <= -2, otherwise HOLD.
_MOMENTUM_LOOKBACK = 63  # ~3 trading months
_MOMENTUM_BUY_PCT = 5.0
_MOMENTUM_SELL_PCT = -5.0
_RSI_TREND_HI = 55.0
_RSI_TREND_LO = 45.0


def _signal_from_closes(
    closes: list[float],
) -> tuple[SignalKind, str, float | None, float | None, float | None]:
    """Compute ``(signal, reason, rsi, sma20, sma50)`` from a daily close series
    (oldest first) using the backtested trend/momentum ensemble. RSI and SMA use
    the same ``indicators`` math the analyst engine and the backtest used."""
    series = [c for c in closes if c is not None]
    rsi_v = _indicator_rsi(series, 14)[-1] if len(series) >= 15 else None
    sma20 = _indicator_sma(series, 20)[-1] if len(series) >= 20 else None
    sma50 = _indicator_sma(series, 50)[-1] if len(series) >= 50 else None

    reasons: list[str] = []
    score = 0

    if len(series) > _MOMENTUM_LOOKBACK and series[-1 - _MOMENTUM_LOOKBACK] > 0:
        base = series[-1 - _MOMENTUM_LOOKBACK]
        mom = (series[-1] - base) / base * 100.0
        if mom > _MOMENTUM_BUY_PCT:
            score += 1
            reasons.append(f"3-mo momentum {mom:+.1f}% (strong)")
        elif mom < _MOMENTUM_SELL_PCT:
            score -= 1
            reasons.append(f"3-mo momentum {mom:+.1f}% (weak)")
        else:
            reasons.append(f"3-mo momentum {mom:+.1f}% (flat)")

    if sma20 is not None and sma50 is not None:
        if sma20 > sma50:
            score += 1
            reasons.append("SMA20 > SMA50 (uptrend)")
        else:
            score -= 1
            reasons.append("SMA20 < SMA50 (downtrend)")

    if rsi_v is not None:
        if rsi_v > _RSI_TREND_HI:
            score += 1
            reasons.append(f"RSI {rsi_v:.0f} (strong)")
        elif rsi_v < _RSI_TREND_LO:
            score -= 1
            reasons.append(f"RSI {rsi_v:.0f} (weak)")
        else:
            reasons.append(f"RSI {rsi_v:.0f} (neutral)")

    kind = _kind_for_score(score)
    return kind, "; ".join(reasons) or "insufficient data", rsi_v, sma20, sma50


async def fetch_signal(symbol: str, client: httpx.AsyncClient) -> WatchlistSignal:
    now = datetime.now(UTC)

    # Return cached result if still fresh
    cached = _signal_cache.get(symbol)
    if cached is not None:
        sig, cached_at = cached
        age = (now - cached_at).total_seconds()
        if age < _CACHE_TTL_SECONDS:
            return sig

    # Retry across hosts with growing backoff; Yahoo throttles bursts hard.
    last_exc: Exception = RuntimeError("no hosts")
    for attempt in range(_YF_MAX_ATTEMPTS):
        for host in _YF_HOSTS:
            url = f"{host}/v8/finance/chart/{symbol}"
            try:
                resp = await client.get(url, params=_YF_PARAMS, headers=_YF_HEADERS, timeout=12.0)
                if resp.status_code in (429, 999):
                    last_exc = Exception(f"{resp.status_code} Too Many Requests from {host}")
                    continue
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                result = data["chart"]["result"][0]
                meta = result.get("meta", {})
                currency = meta.get("currency", "USD")
                price: float | None = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
                indicators = result.get("indicators", {})
                closes_raw: list[float | None] = indicators.get("quote", [{}])[0].get("close", [])
                closes = [c for c in closes_raw if c is not None]
                signal, reason, rsi, sma20, sma50 = _signal_from_closes(closes)
                sig = WatchlistSignal(
                    symbol=symbol,
                    signal=signal,
                    price=price,
                    currency=currency,
                    rsi=rsi,
                    sma20=sma20,
                    sma50=sma50,
                    reason=reason,
                    refreshed_at=now,
                )
                _signal_cache[symbol] = (sig, now)
                return sig
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                continue
        # All hosts failed this round — back off before retrying.
        if attempt < _YF_MAX_ATTEMPTS - 1:
            await asyncio.sleep(_YF_RETRY_BACKOFF * (attempt + 1))

    # Exhausted retries. Prefer a stale cached value over a useless "unavailable"
    # row — a quote from earlier today is far more informative than nothing.
    if cached is not None:
        stale_sig, cached_at = cached
        age_min = int((now - cached_at).total_seconds() // 60)
        logger.warning("watchlist_signal_serving_stale", symbol=symbol, age_min=age_min, error=str(last_exc))
        return WatchlistSignal(
            symbol=stale_sig.symbol,
            signal=stale_sig.signal,
            price=stale_sig.price,
            currency=stale_sig.currency,
            rsi=stale_sig.rsi,
            sma20=stale_sig.sma20,
            sma50=stale_sig.sma50,
            reason=f"{stale_sig.reason} (cached {age_min}m ago — live fetch rate-limited)",
            refreshed_at=cached_at,
        )

    logger.warning("watchlist_signal_fetch_failed", symbol=symbol, error=str(last_exc))
    return WatchlistSignal(
        symbol=symbol,
        signal=SignalKind.HOLD,
        price=None,
        currency="USD",
        rsi=None,
        sma20=None,
        sma50=None,
        reason=f"data unavailable: {last_exc}",
        refreshed_at=now,
    )


def _synthetic_position(symbol: str) -> Position:
    """Build a throw-away Position for a watchlist ticker so the analyst's
    symbol mapper can resolve it to a Longbridge feed. Numeric codes are
    treated as HK (e.g. ``700`` → ``700.HK``); ``CODE.MARKET`` and Futu's
    ``US.VOO`` forms pass through the mapper; bare alphabetic tickers default
    to US (the mapper's ``longbridge`` source rule)."""
    raw = symbol.strip().upper()
    exchange = "HK" if raw.replace(".", "").isdigit() else None
    return Position(
        source="longbridge",
        symbol=raw,
        exchange=exchange,
        currency="USD",
        quantity=Decimal(0),
    )


def _currency_for_lb_symbol(lb: str) -> str:
    if lb.endswith(".HK"):
        return "HKD"
    if lb.endswith((".SH", ".SZ")):
        return "CNY"
    return "USD"


def _signal_from_analyst_closes(symbol: str, position: Position, closes: list[float]) -> WatchlistSignal:
    """Build a watchlist signal from the analyst feed's daily close series using
    the backtested trend/momentum ensemble. Same model as the Yahoo path — only
    the data source differs (Longbridge, which isn't IP rate-limited)."""
    kind, reason, rsi_v, sma20, sma50 = _signal_from_closes(closes)
    return WatchlistSignal(
        symbol=symbol,
        signal=kind,
        price=closes[-1] if closes else None,
        currency=_currency_for_lb_symbol(position_to_longbridge_symbol(position) or ""),
        rsi=rsi_v,
        sma20=sma20,
        sma50=sma50,
        reason=reason,
        refreshed_at=datetime.now(UTC),
    )


async def _signals_via_analyst(symbols: list[str], analyst: Any) -> list[WatchlistSignal]:
    """Compute signals from the Longbridge analyst feed (no Yahoo, no rate
    limits). Symbols the mapper can't resolve fall back to the Yahoo scrape.
    Raises MarketDataUnavailable if the analyst service-account is unconfigured
    so the caller can fall back to Yahoo for the whole batch."""
    resolved: dict[str, WatchlistSignal] = {}
    unmapped: list[str] = []
    for sym in symbols:
        if sym in resolved or sym in unmapped:
            continue
        position = _synthetic_position(sym)
        closes = await analyst.daily_closes_for_position(position)  # may raise MarketDataUnavailable
        if not closes:
            unmapped.append(sym)
        else:
            resolved[sym] = _signal_from_analyst_closes(sym, position, closes)
    if unmapped:
        async with httpx.AsyncClient() as client:
            for i, sym in enumerate(unmapped):
                if i > 0:
                    await asyncio.sleep(0.6)
                resolved[sym] = await fetch_signal(sym, client)
    return [resolved[s] for s in symbols]


async def fetch_signals(symbols: list[str], *, analyst: Any | None = None) -> list[WatchlistSignal]:
    if not symbols:
        return []
    # Prefer the Longbridge analyst feed — it's already the source for the
    # Position Analysis section and isn't subject to Yahoo's IP rate-limiting.
    if analyst is not None:
        try:
            return await _signals_via_analyst(symbols, analyst)
        except MarketDataUnavailable:
            logger.info("watchlist_signals_analyst_unconfigured_fallback_yahoo")
        except Exception as exc:  # noqa: BLE001
            logger.warning("watchlist_signals_analyst_failed_fallback_yahoo", error=str(exc))
    # Fallback: Yahoo scrape. Fetch sequentially (not concurrently) — Yahoo
    # rate-limits by IP, so a burst of parallel requests is what trips 429.
    async with httpx.AsyncClient() as client:
        results: list[WatchlistSignal] = []
        for i, sym in enumerate(symbols):
            if i > 0:
                await asyncio.sleep(0.6)
            results.append(await fetch_signal(sym, client))
        return results


# ---------------------------------------------------------------------------
# Email digest sender
# ---------------------------------------------------------------------------


def _signal_emoji(s: SignalKind) -> str:
    # Watchlist stocks aren't owned, so the neutral verdict reads as "WAIT"
    # (no clear entry yet) rather than the misleading "HOLD".
    return {
        "strong_buy": "🟢🟢 STRONG BUY",
        "buy": "🟢 BUY",
        "sell": "🔴 SELL",
        "strong_sell": "🔴🔴 STRONG SELL",
        "hold": "🟡 WAIT",
    }.get(s.value, s.value.upper())


_CELL = "padding:8px;border:1px solid #ddd"


def _money(value: Decimal | None, currency: str = "") -> str:
    if value is None:
        return "N/A"
    return f"{value:,.2f} {currency}".strip()


def _money_cell(value: Decimal | None, currency: str = "") -> str:
    return f"<td style='{_CELL}'>{_money(value, currency)}</td>"


def _pnl_cell(value: Decimal | None, currency: str = "") -> str:
    if value is None:
        return f"<td style='{_CELL}'>N/A</td>"
    color = "#1a8a1a" if value >= 0 else "#c0392b"
    sign = "+" if value >= 0 else ""
    return f"<td style='{_CELL};color:{color};font-weight:bold'>{sign}{_money(value, currency)}</td>"


def _per_currency(pairs: dict[str, Decimal], *, pnl: bool) -> str:
    if not pairs:
        return "—"
    parts = []
    for ccy, amount in sorted(pairs.items()):
        if pnl:
            color = "#1a8a1a" if amount >= 0 else "#c0392b"
            sign = "+" if amount >= 0 else ""
            parts.append(f"<span style='color:{color}'>{sign}{amount:,.2f} {ccy}</span>")
        else:
            parts.append(f"{amount:,.2f} {ccy}")
    return "<br>".join(parts)


def build_positions_section_html(snapshot: PortfolioSnapshot, cached_at: datetime | None) -> str:
    """Render an all-brokers positions block: headline totals, per-broker
    summary, and per-position detail. Totals are in the snapshot's base
    currency (FX-converted by the aggregator); per-broker/per-position values
    are shown in their native currency."""
    base = snapshot.base_currency
    as_of = (cached_at or snapshot.as_of).strftime("%Y-%m-%d %H:%M UTC")

    headline = (
        f"<p style='margin:4px 0'>"
        f"<b>Total Value:</b> {_money(snapshot.total_market_value, base)} &nbsp;|&nbsp; "
        f"<b>Unrealized:</b> {_money(snapshot.total_unrealized_pnl, base)} &nbsp;|&nbsp; "
        f"<b>Realized:</b> {_money(snapshot.total_realized_pnl, base)} &nbsp;|&nbsp; "
        f"<b>Total Return:</b> {_money(snapshot.total_return, base)}"
        f"</p>"
    )

    # Per-broker summary (grouped by source, aggregated per currency).
    pos_by_source: dict[str, list[Any]] = defaultdict(list)
    for p in snapshot.positions:
        pos_by_source[p.source].append(p)
    cash_by_source: dict[str, dict[str, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    for b in snapshot.balances:
        cash_by_source[b.source][b.currency] += b.amount

    summary_rows = ""
    for source in sorted(set(pos_by_source) | set(cash_by_source)):
        ps = pos_by_source.get(source, [])
        mv: dict[str, Decimal] = defaultdict(Decimal)
        pnl: dict[str, Decimal] = defaultdict(Decimal)
        for p in ps:
            if p.market_value is not None:
                mv[p.currency] += p.market_value
            if p.unrealized_pnl is not None:
                pnl[p.currency] += p.unrealized_pnl
        summary_rows += (
            f"<tr>"
            f"<td style='{_CELL}'><b>{source}</b></td>"
            f"<td style='{_CELL}'>{len(ps)}</td>"
            f"<td style='{_CELL}'>{_per_currency(mv, pnl=False)}</td>"
            f"<td style='{_CELL}'>{_per_currency(pnl, pnl=True)}</td>"
            f"<td style='{_CELL}'>{_per_currency(dict(cash_by_source.get(source, {})), pnl=False)}</td>"
            f"</tr>"
        )

    detail_rows = ""
    for p in sorted(snapshot.positions, key=lambda x: (x.source, x.symbol)):
        detail_rows += (
            f"<tr>"
            f"<td style='{_CELL}'><b>{p.symbol}</b></td>"
            f"<td style='{_CELL}'>{p.source}</td>"
            f"<td style='{_CELL}'>{p.quantity:g}</td>"
            f"{_money_cell(p.avg_cost, p.currency)}"
            f"{_money_cell(p.last_price, p.currency)}"
            f"{_money_cell(p.market_value, p.currency)}"
            f"{_pnl_cell(p.unrealized_pnl, p.currency)}"
            f"</tr>"
        )
    if not detail_rows:
        detail_rows = f"<tr><td colspan='7' style='{_CELL};color:#999'>No positions in last sync</td></tr>"

    return f"""
<h2>Portfolio — Positions</h2>
<p style='color:#999;font-size:11px;margin:0 0 6px'>As of last app sync: {as_of}</p>
{headline}
<h3 style='margin:14px 0 4px'>By Broker</h3>
<table style='border-collapse:collapse;width:100%'>
<thead><tr style='background:#f0f0f0'>
  <th style='{_CELL}'>Broker</th>
  <th style='{_CELL}'>Positions</th>
  <th style='{_CELL}'>Market Value</th>
  <th style='{_CELL}'>Unrealized P&amp;L</th>
  <th style='{_CELL}'>Cash</th>
</tr></thead>
<tbody>{summary_rows}</tbody>
</table>
<h3 style='margin:14px 0 4px'>Positions</h3>
<table style='border-collapse:collapse;width:100%'>
<thead><tr style='background:#f0f0f0'>
  <th style='{_CELL}'>Symbol</th>
  <th style='{_CELL}'>Broker</th>
  <th style='{_CELL}'>Qty</th>
  <th style='{_CELL}'>Avg Cost</th>
  <th style='{_CELL}'>Last</th>
  <th style='{_CELL}'>Market Value</th>
  <th style='{_CELL}'>Unrealized P&amp;L</th>
</tr></thead>
<tbody>{detail_rows}</tbody>
</table>
"""


_VERDICT_COLORS = {
    "BUY": "#1a8a1a",
    "HOLD": "#b8860b",
    "TRIM": "#c0392b",
    "SELL": "#c0392b",
    "INSUFFICIENT_DATA": "#999",
}


def _num(value: float | None, places: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:,.{places}f}"


def _pct_span(value: float | None) -> str:
    if value is None:
        return "—"
    color = "#1a8a1a" if value >= 0 else "#c0392b"
    sign = "+" if value >= 0 else ""
    return f"<span style='color:{color}'>{sign}{value:.1f}%</span>"


def build_analysis_section_html(reports: list[Any]) -> str:
    """Render the per-position analyst verdicts: headline label, composite
    score, key levels (stop / take-profit), and a one-line rationale. Reports
    come from ``AnalystService`` which uses its own market-data feed, so this
    works in the background worker without the user's broker credentials."""
    if not reports:
        return ""
    rows = ""
    for r in sorted(reports, key=lambda x: x.symbol):
        label = r.label or "INSUFFICIENT_DATA"
        verdict_color = _VERDICT_COLORS.get(label, "#555")
        verdict_text = label.replace("_", " ")
        score = "—" if r.composite_score is None else f"{r.composite_score:g}"
        tp = " / ".join(p for p in (_num(r.take_profit_1), _num(r.take_profit_2)) if p != "—") or "—"
        rows += (
            f"<tr>"
            f"<td style='{_CELL}'><b>{r.symbol}</b></td>"
            f"<td style='{_CELL};color:{verdict_color};font-weight:bold'>{verdict_text}</td>"
            f"<td style='{_CELL}'>{score}</td>"
            f"<td style='{_CELL}'>{_num(r.last_price)}</td>"
            f"<td style='{_CELL}'>{_pct_span(r.pct_vs_cost)}</td>"
            f"<td style='{_CELL}'>{_num(r.stop_loss)}</td>"
            f"<td style='{_CELL}'>{tp}</td>"
            f"<td style='{_CELL};color:#555;font-size:12px'>{r.summary}</td>"
            f"</tr>"
        )
    return f"""
<h3 style='margin:14px 0 4px'>Position Analysis</h3>
<table style='border-collapse:collapse;width:100%'>
<thead><tr style='background:#f0f0f0'>
  <th style='{_CELL}'>Symbol</th>
  <th style='{_CELL}'>Verdict</th>
  <th style='{_CELL}'>Score</th>
  <th style='{_CELL}'>Last</th>
  <th style='{_CELL}'>vs Cost</th>
  <th style='{_CELL}'>Stop</th>
  <th style='{_CELL}'>Take Profit</th>
  <th style='{_CELL}'>Notes</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<p style='color:#999;font-size:11px;margin:4px 0'>Composite score −100…100 (BUY ≥ 60, HOLD ≥ −20, TRIM &gt; −60, else SELL). Not financial advice.</p>
"""


def build_digest_html(
    signals: list[WatchlistSignal],
    user_email: str,
    *,
    snapshot: PortfolioSnapshot | None = None,
    snapshot_cached_at: datetime | None = None,
    analysis_reports: list[Any] | None = None,
) -> str:
    rows = ""
    for s in signals:
        price_str = f"{s.price:.2f} {s.currency}" if s.price else "N/A"
        rsi_str = f"{s.rsi:.1f}" if s.rsi else "N/A"
        rows += (
            f"<tr>"
            f"<td style='padding:8px;border:1px solid #ddd'><b>{s.symbol}</b></td>"
            f"<td style='padding:8px;border:1px solid #ddd'>{price_str}</td>"
            f"<td style='padding:8px;border:1px solid #ddd'><b>{_signal_emoji(s.signal)}</b></td>"
            f"<td style='padding:8px;border:1px solid #ddd'>{rsi_str}</td>"
            f"<td style='padding:8px;border:1px solid #ddd;color:#555;font-size:12px'>{s.reason}</td>"
            f"</tr>"
        )
    date_str = datetime.now(UTC).strftime("%Y-%m-%d")
    positions_html = (
        build_positions_section_html(snapshot, snapshot_cached_at) if snapshot is not None else ""
    )
    analysis_html = build_analysis_section_html(analysis_reports or [])
    return f"""
<html><body style='font-family:Arial,sans-serif'>
<h2>Portfolio Watchlist Digest — {date_str}</h2>
<p>Daily summary for <b>{user_email}</b></p>
{positions_html}
{analysis_html}
<h2>Watchlist Signals</h2>
<table style='border-collapse:collapse;width:100%'>
<thead><tr style='background:#f0f0f0'>
  <th style='padding:8px;border:1px solid #ddd'>Symbol</th>
  <th style='padding:8px;border:1px solid #ddd'>Price</th>
  <th style='padding:8px;border:1px solid #ddd'>Signal</th>
  <th style='padding:8px;border:1px solid #ddd'>RSI(14)</th>
  <th style='padding:8px;border:1px solid #ddd'>Reason</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>
<p style='color:#999;font-size:11px'>Signals use a trend/momentum ensemble — 3-month price momentum, SMA(20/50) trend, and RSI(14) — favouring strength over mean-reversion. Not financial advice.</p>
</body></html>
"""


def send_digest_email(
    *,
    to_email: str,
    from_email: str,
    app_password: str,
    signals: list[WatchlistSignal],
    snapshot: PortfolioSnapshot | None = None,
    snapshot_cached_at: datetime | None = None,
    analysis_reports: list[Any] | None = None,
) -> None:
    subject = f"Portfolio Digest — {datetime.now(UTC).strftime('%Y-%m-%d')}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    html = build_digest_html(
        signals,
        to_email,
        snapshot=snapshot,
        snapshot_cached_at=snapshot_cached_at,
        analysis_reports=analysis_reports,
    )
    msg.attach(MIMEText(html, "html"))
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(from_email, app_password)
        server.sendmail(from_email, to_email, msg.as_string())
    logger.info("watchlist_digest_sent", to=to_email, symbols=[s.symbol for s in signals])


__all__ = [
    "WatchlistEntry",
    "WatchlistRepository",
    "WatchlistSignal",
    "SignalKind",
    "fetch_signals",
    "send_digest_email",
    "build_digest_html",
    "build_analysis_section_html",
    "build_positions_section_html",
]
