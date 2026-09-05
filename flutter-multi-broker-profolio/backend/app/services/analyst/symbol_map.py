"""Translate each broker's position symbol into Longbridge's format.

Longbridge expects `<CODE>.<MARKET>`. Different brokers report symbols
differently:

| Broker     | Position symbol           | Exchange field   | LB symbol  |
|------------|---------------------------|------------------|------------|
| IBKR       | `TSLA`                    | `NASDAQ`         | `TSLA.US`  |
| IBKR       | `IONQ`                    | `NYSE`           | `IONQ.US`  |
| Longbridge | `PLTR.US` (already there) | `Market.US`      | `PLTR.US`  |
| Futu       | `US.VOO`                  | (none)           | `VOO.US`   |
| Futu       | `HK.00823`                | (none)           | `823.HK`   |
| IBKR       | `700`                     | `SEHK`           | `700.HK`   |

We're conservative: anything we don't recognize returns None and the
analyst service simply skips that position rather than guessing.
"""

from __future__ import annotations

import re

from app.models.domain import Position


# US exchanges that map to Longbridge's `.US` suffix.
_US_EXCHANGES: frozenset[str] = frozenset({
    "US",
    "NASDAQ",
    "NYSE",
    "ARCA",
    "AMEX",
    "BATS",
    "OTC",
    "PINK",
    "MARKET.US",
})

_HK_EXCHANGES: frozenset[str] = frozenset({
    "HK",
    "SEHK",
    "HKEX",
    "HKG",
    "MARKET.HK",
})

_SH_EXCHANGES: frozenset[str] = frozenset({
    "SH", "SSE", "SHA", "SHANGHAI", "MARKET.CN", "CN", "MARKET.SH",
})

_SZ_EXCHANGES: frozenset[str] = frozenset({
    "SZ", "SZSE", "SHE", "SHENZHEN", "MARKET.SZ",
})

_SG_EXCHANGES: frozenset[str] = frozenset({
    "SG", "SGX", "SINGAPORE", "MARKET.SG",
})


# A symbol that already looks like `<CODE>.<MARKET>` — short-circuit map.
_ALREADY_LB = re.compile(r"^[A-Z0-9]{1,10}\.[A-Z]{2}$")

# Futu's combined-prefix form, e.g. `US.VOO` or `HK.00823`.
_FUTU_PREFIXED = re.compile(r"^(US|HK|SH|SZ|SG)\.([A-Z0-9]+)$")


def position_to_longbridge_symbol(position: Position) -> str | None:
    """Best-effort: derive a Longbridge `CODE.MARKET` symbol from a Position.

    Returns None when we can't confidently map the symbol — the analyst
    service will skip those positions rather than fetch nonsense.
    """
    return _map(position.symbol, position.source, position.exchange)


def _map(symbol: str | None, source: str, exchange: str | None) -> str | None:
    if not symbol:
        return None
    raw = symbol.strip().upper()
    if not raw:
        return None

    # Case 1: already `CODE.MARKET` (Longbridge native; some IBKR feeds
    # also send this form for non-US tickers).
    if _ALREADY_LB.match(raw):
        return raw

    # Case 2: Futu's `US.VOO` / `HK.00823` form. Need to flip the order
    # and strip leading zeros from HK tickers (Longbridge wants `823.HK`,
    # not `00823.HK`).
    m = _FUTU_PREFIXED.match(raw)
    if m:
        market, code = m.group(1), m.group(2)
        return _format_lb_symbol(code, market)

    # Case 3: bare ticker + exchange field. Common for IBKR.
    exch = (exchange or "").strip().upper() if exchange else ""
    market = _market_for_exchange(exch)
    if market is not None:
        return _format_lb_symbol(raw, market)

    # Case 4: source-specific defaults — Longbridge typically reports US
    # tickers already as `CODE.US`, so a bare ticker from `longbridge`
    # without an exchange almost certainly is a US name.
    if source.lower() == "longbridge" and raw.isalpha():
        return f"{raw}.US"

    return None


def _market_for_exchange(exch: str) -> str | None:
    if not exch:
        return None
    if exch in _US_EXCHANGES:
        return "US"
    if exch in _HK_EXCHANGES:
        return "HK"
    if exch in _SH_EXCHANGES:
        return "SH"
    if exch in _SZ_EXCHANGES:
        return "SZ"
    if exch in _SG_EXCHANGES:
        return "SG"
    return None


def _format_lb_symbol(code: str, market: str) -> str:
    """Build `CODE.MARKET` with broker-specific quirks handled.

    HK tickers are zero-padded by Futu (`00823`) but Longbridge expects
    them un-padded (`823.HK`). Other markets pass through.
    """
    code = code.upper()
    market = market.upper()
    if market == "HK":
        # Strip leading zeros, but never reduce to empty string.
        stripped = code.lstrip("0") or code
        return f"{stripped}.{market}"
    return f"{code}.{market}"
