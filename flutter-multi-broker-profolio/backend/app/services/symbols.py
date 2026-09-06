"""Canonical symbol table with per-source aliases (spec §6.1).

The same instrument carries a different identifier at every source::

    Instrument   IB     Futu        LongBridge   Ghostfolio
    Tencent      700    HK.00700    700.HK       0700.HK

The rule the spec sets is one-directional and worth stating plainly:

    **Every adapter maps INTO canonical form. Only the Ghostfolio exporter
    maps OUT of it.**

That keeps per-source string munging in exactly one module instead of
scattered across four adapters and an exporter. If you find yourself
writing a ``.replace(".HK", "")`` anywhere else, it belongs here.

Canonical form is ``<VENUE>:<CODE>``::

    HK:00700     Hong Kong equity, HKEX 5-digit zero-padded code
    US:VOO       US-listed equity / ETF
    CRYPTO:BTC   crypto asset, venue-independent by design
    CASH:HKD     a currency held as cash

Two design choices worth defending:

* **HK codes are stored 5-digit zero-padded** (``00700``), which is HKEX's
  own official form. Every source disagrees with every other about padding
  (IB says ``700``, Futu ``HK.00700``, LongBridge ``700.HK``, Ghostfolio
  ``0700.HK``), so any choice is arbitrary -- but padding is *lossless*.
  Stripping zeros is not reversible without knowing the target's width,
  which is precisely the bug that makes ``0700`` and ``700`` look like two
  different instruments.

* **Crypto is venue-independent.** BTC bought on Binance and BTC bought on
  Futu are the same asset with one cost basis (§4.5, §6.3). Encoding the
  venue into the symbol would split them into two holdings. Custody
  location is tracked separately, on the position (§4.4).

ISIN is the preferred join key for equities where a source provides one
(IB Flex does -- §4.1); ``exchange + ticker`` is the fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum


class AssetKind(StrEnum):
    """Coarse kind, used to pick a Ghostfolio data source and asset class."""

    EQUITY = "equity"
    ETF = "etf"
    CRYPTO = "crypto"
    COMMODITY = "commodity"
    CASH = "cash"


class Venue(StrEnum):
    """Canonical venue prefix."""

    US = "US"
    HK = "HK"
    SH = "SH"
    SZ = "SZ"
    SG = "SG"
    JP = "JP"
    CRYPTO = "CRYPTO"
    CASH = "CASH"


# -- Exchange alias tables ---------------------------------------------------
# Every spelling any of our four sources has been observed to emit. Keep
# these additive: an unrecognised exchange resolves to None and the caller
# raises rather than guesses (see `resolve`).
_EXCHANGE_ALIASES: dict[Venue, frozenset[str]] = {
    Venue.US: frozenset({
        "US", "USA", "NASDAQ", "NYSE", "ARCA", "AMEX",
        "BATS", "IEX", "OTC", "PINK", "NMS", "MARKET.US", "SMART",
    }),
    Venue.HK: frozenset({
        "HK", "SEHK", "HKEX", "HKG", "HKSE", "MARKET.HK",
    }),
    Venue.SH: frozenset({
        "SH", "SSE", "SHA", "SHANGHAI", "MARKET.CN", "MARKET.SH", "CN",
    }),
    Venue.SZ: frozenset({
        "SZ", "SZSE", "SHE", "SHENZHEN", "MARKET.SZ",
    }),
    Venue.SG: frozenset({
        "SG", "SGX", "SINGAPORE", "MARKET.SG",
    }),
    Venue.JP: frozenset({
        "JP", "TSE", "TSEJ", "TOKYO", "MARKET.JP",
    }),
}

_VENUE_BY_EXCHANGE: dict[str, Venue] = {
    alias: venue for venue, aliases in _EXCHANGE_ALIASES.items() for alias in aliases
}

# Assets that are crypto regardless of which venue reported them. Kept as an
# explicit list rather than inferred: "is this ticker a coin?" has no reliable
# syntactic answer, and guessing wrong silently corrupts cost basis.
_KNOWN_CRYPTO: frozenset[str] = frozenset({
    "BTC", "ETH", "DOGE", "XBT", "USDT", "USDC", "BUSD", "FDUSD", "BNB",
    "SOL", "ADA", "XRP", "DOT", "MATIC", "LTC", "BCH", "LINK", "AVAX",
})

# ISO-4217 codes we treat as cash rather than as an instrument.
_FIAT: frozenset[str] = frozenset({
    "HKD", "USD", "CNY", "CNH", "TWD", "SGD", "JPY", "EUR", "GBP", "AUD",
    "CAD", "CHF", "KRW", "MYR", "THB",
})

# `HK.00700` / `US.VOO` -- Futu's prefixed form.
_FUTU_PREFIXED = re.compile(r"^(US|HK|SH|SZ|SG|JP)\.([A-Z0-9]+)$")
# `700.HK` / `PLTR.US` -- LongBridge's suffixed form.
_SUFFIXED = re.compile(r"^([A-Z0-9]{1,10})\.(US|HK|SH|SZ|SG|JP)$")
# `BTCUSDT` -- Binance-style concatenated spot pair.
#
# The base quantifier is LAZY, and that is load-bearing. With a greedy base
# the engine backtracks from the longest base, so the first quote that
# matches is the *shortest* one -- `ETHFDUSD` split as `ETHFD` + `USD`,
# which then fails the known-crypto check and drops the trade entirely.
# Ordering the alternation longest-first does not help, because alternation
# order only applies at a fixed position. A lazy base tries the shortest
# base first and therefore finds the longest valid quote: `ETH` + `FDUSD`.
_CONCAT_PAIR = re.compile(r"^([A-Z0-9]{2,10}?)(FDUSD|USDT|USDC|BUSD|USD|BTC|ETH|BNB)$")

_HK_CODE_WIDTH = 5


class SymbolResolutionError(ValueError):
    """Raised when a symbol cannot be mapped confidently.

    Deliberately an exception rather than a ``None`` return: a silently
    dropped position becomes a reconciliation mismatch (§6.4) that costs
    far more to diagnose than a loud failure here does to fix.
    """


@dataclass(frozen=True, slots=True)
class CanonicalSymbol:
    """An instrument in canonical form, with the identifiers we can join on."""

    venue: Venue
    code: str
    kind: AssetKind
    isin: str | None = None
    # Source-specific extras (e.g. the quote asset of a crypto pair, §6.2)
    # so a two-legged view can be derived later if it is ever needed.
    # Excluded from equality: two BTC lots are the same instrument whether
    # they were bought against USDT or USD.
    meta: dict[str, str] = field(default_factory=dict, compare=False)

    @property
    def canonical_id(self) -> str:
        return f"{self.venue.value}:{self.code}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.canonical_id


def _normalize_hk(code: str) -> str:
    """Zero-pad an HK code to HKEX's official 5 digits.

    ``700`` -> ``00700``, ``0700`` -> ``00700``, ``00700`` -> ``00700``.
    """
    if not code.isdigit():
        # Not a numeric board code (e.g. a warrant with letters) -- leave it.
        return code.upper()
    digits = code.lstrip("0") or "0"
    if len(digits) > _HK_CODE_WIDTH:
        # Longer than a board-lot code; pad would truncate meaning, so keep.
        return digits
    return digits.rjust(_HK_CODE_WIDTH, "0")


def venue_for_exchange(exchange: str | None) -> Venue | None:
    """Map a source's exchange string onto a canonical venue, or None."""
    if not exchange:
        return None
    return _VENUE_BY_EXCHANGE.get(exchange.strip().upper())


def is_crypto_asset(asset: str) -> bool:
    return asset.strip().upper() in _KNOWN_CRYPTO


def is_fiat(code: str) -> bool:
    return code.strip().upper() in _FIAT


def canonical_cash(currency: str) -> CanonicalSymbol:
    """Cash in a currency, e.g. ``CASH:HKD``."""
    cur = currency.strip().upper()
    if not cur:
        raise SymbolResolutionError("cash balance has no currency")
    return CanonicalSymbol(venue=Venue.CASH, code=cur, kind=AssetKind.CASH)


def canonical_crypto(asset: str, *, quote_asset: str | None = None) -> CanonicalSymbol:
    """A crypto asset, venue-independent (§6.2).

    ``quote_asset`` is retained in ``meta`` so the disposed leg of a pair
    trade can be reconstructed if it is ever needed, without flooding the
    ledger with meaningless stablecoin activity now.
    """
    code = asset.strip().upper()
    if not code:
        raise SymbolResolutionError("crypto asset has no symbol")
    meta = {"quote_asset": quote_asset.strip().upper()} if quote_asset else {}
    return CanonicalSymbol(
        venue=Venue.CRYPTO, code=code, kind=AssetKind.CRYPTO, meta=meta
    )


def split_crypto_pair(symbol: str) -> tuple[str, str] | None:
    """Split ``BTCUSDT`` into ``("BTC", "USDT")``, or None if not a pair."""
    raw = symbol.strip().upper()
    match = _CONCAT_PAIR.match(raw)
    if not match:
        return None
    base, quote = match.group(1), match.group(2)
    if base == quote:
        return None
    return base, quote


def resolve(
    symbol: str | None,
    *,
    exchange: str | None = None,
    currency: str | None = None,
    isin: str | None = None,
    kind_hint: AssetKind | None = None,
) -> CanonicalSymbol:
    """Map any source's instrument identifier into canonical form.

    Tries, in order: concatenated crypto pair (``BTCUSDT``), bare crypto
    asset (``BTC``), Futu's prefixed form (``HK.00700``), LongBridge's
    suffixed form (``700.HK``), then a bare ticker disambiguated by
    ``exchange``.

    Raises ``SymbolResolutionError`` when no rule applies -- never guesses.
    """
    if symbol is None or not symbol.strip():
        raise SymbolResolutionError("empty symbol")
    raw = symbol.strip().upper()

    # 1. Concatenated crypto pair, e.g. `BTCUSDT` (Binance).
    pair = split_crypto_pair(raw)
    if pair is not None and (is_crypto_asset(pair[0]) or kind_hint is AssetKind.CRYPTO):
        return canonical_crypto(pair[0], quote_asset=pair[1])

    # 2. Bare crypto asset, e.g. `BTC`.
    if kind_hint is AssetKind.CRYPTO or is_crypto_asset(raw):
        return canonical_crypto(raw, quote_asset=currency)

    # 3. Futu's `HK.00700` / `US.VOO`.
    match = _FUTU_PREFIXED.match(raw)
    if match:
        return _build(Venue(match.group(1)), match.group(2), isin=isin, kind_hint=kind_hint)

    # 4. LongBridge's `700.HK` / `PLTR.US`.
    match = _SUFFIXED.match(raw)
    if match:
        return _build(Venue(match.group(2)), match.group(1), isin=isin, kind_hint=kind_hint)

    # 5. Bare ticker + exchange field (IB's usual shape).
    venue = venue_for_exchange(exchange)
    if venue is not None:
        return _build(venue, raw, isin=isin, kind_hint=kind_hint)

    raise SymbolResolutionError(
        f"cannot resolve symbol {symbol!r} (exchange={exchange!r}): no venue "
        "could be determined. Add the exchange alias to _EXCHANGE_ALIASES "
        "rather than guessing at the call site."
    )


def _build(
    venue: Venue,
    code: str,
    *,
    isin: str | None,
    kind_hint: AssetKind | None,
) -> CanonicalSymbol:
    normalized = _normalize_hk(code) if venue is Venue.HK else code.upper()
    cleaned_isin = isin.strip().upper() if isin else None
    return CanonicalSymbol(
        venue=venue,
        code=normalized,
        kind=kind_hint or AssetKind.EQUITY,
        isin=cleaned_isin or None,
    )


def display_symbol(canonical_id: str) -> str:
    """Human-facing form of a canonical id, for the digest and UI.

    ``CRYPTO:BTC`` -> ``BTC``, ``HK:00700`` -> ``00700.HK``,
    ``US:VOO`` -> ``VOO``, ``CASH:HKD`` -> ``HKD``.

    Canonical ids are internal join keys. Putting one in front of a human
    ("CRYPTO:BTC") is both noisier to read and wider on a phone screen,
    which matters because §10's digest is meant to be legible on a lock
    screen without opening anything.
    """
    venue, _, code = canonical_id.partition(":")
    if not code:
        return canonical_id
    if venue in ("CRYPTO", "CASH", "US"):
        return code
    return f"{code}.{venue}"


__all__ = [
    "AssetKind",
    "CanonicalSymbol",
    "SymbolResolutionError",
    "Venue",
    "canonical_cash",
    "canonical_crypto",
    "display_symbol",
    "is_crypto_asset",
    "is_fiat",
    "resolve",
    "split_crypto_pair",
    "venue_for_exchange",
]
