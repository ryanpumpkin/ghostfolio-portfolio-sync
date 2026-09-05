"""Map internal models out to Ghostfolio activities (spec §6.1, §6.3, §7.1).

This module is the **only** place that maps out of canonical form. Adapters
map in (``app.services.symbols``); nothing else maps out.

Two rules from the spec are enforced here rather than left to callers,
because getting either wrong corrupts cost basis silently:

* **§6.3 — transfers are not trades.** A movement between the owner's own
  accounts is a custody change. It is dropped from the Ghostfolio push
  entirely, never rewritten as a BUY or SELL. Ghostfolio's ``Type`` enum
  has no TRANSFER member, which is a good sign the rule matches the tool.
* **§7.1 — crypto symbols are verified, not guessed.** Ghostfolio prices
  crypto through a different data provider than equities and the expected
  ``symbol``/``dataSource`` pairing differs. This module refuses to invent
  one; see ``CryptoSymbolNotVerified``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from app.models.domain import Transaction
from app.services.symbols import AssetKind, CanonicalSymbol, Venue, resolve

_LOG = logging.getLogger("mbp.ghostfolio.mapper")


class ActivityType(StrEnum):
    """Ghostfolio's `Type` enum, verified against 3.67.0's Prisma schema.

    Note what is absent: there is no TRANSFER. §6.3's rule that transfers
    are excluded from the push is not a workaround for a missing feature —
    it is how Ghostfolio is meant to be used. It tracks the asset, not
    where the asset sits.
    """

    BUY = "BUY"
    DIVIDEND = "DIVIDEND"
    FEE = "FEE"
    INTEREST = "INTEREST"
    LIABILITY = "LIABILITY"
    SELL = "SELL"


class DataSource(StrEnum):
    """Ghostfolio's `DataSource` enum, verified against 3.67.0."""

    ALPHA_VANTAGE = "ALPHA_VANTAGE"
    COINGECKO = "COINGECKO"
    EOD_HISTORICAL_DATA = "EOD_HISTORICAL_DATA"
    FINANCIAL_MODELING_PREP = "FINANCIAL_MODELING_PREP"
    GHOSTFOLIO = "GHOSTFOLIO"
    GOOGLE_SHEETS = "GOOGLE_SHEETS"
    MANUAL = "MANUAL"
    RAPID_API = "RAPID_API"
    YAHOO = "YAHOO"


class CryptoSymbolNotVerified(RuntimeError):
    """Raised when a crypto symbol has no verified Ghostfolio mapping.

    §7.1 is explicit: *"Symbol format for crypto — VERIFY, DO NOT GUESS.
    Create one crypto activity manually in the UI, read it back through the
    API, and mirror exactly what the UI produced."*

    Guessing here would produce activities that import cleanly and then
    price at zero, which looks like a market crash rather than a bug. So we
    fail loudly instead. The fix is to do the verification the spec
    describes once per asset, then pass the result as ``crypto_overrides``
    (``{"BTC": "bitcoin"}``, or ``{"BTC": "YAHOO:BTC-USD"}`` to pin a
    different data source).
    """


# Sides we recognise as trades. Anything else is either a custody change
# (§6.3) or a cash movement, and is handled explicitly below.
_BUY_SIDES = frozenset({"buy", "b", "bought", "long"})
_SELL_SIDES = frozenset({"sell", "s", "sold", "short"})

# Movements between the owner's own accounts, and cash flows that are not
# instrument activity. Dropped from the push (§6.3), not rewritten.
_TRANSFER_SIDES = frozenset({
    "transfer", "deposit", "withdrawal", "withdraw", "transfer_in",
    "transfer_out", "custody_change",
})

_DIVIDEND_SIDES = frozenset({"dividend", "div"})
_INTEREST_SIDES = frozenset({"interest"})
_FEE_SIDES = frozenset({"fee", "commission"})

_HK_YAHOO_WIDTH = 4


class SkipReason(StrEnum):
    """Why an activity was not pushed. Surfaced, never silent."""

    TRANSFER = "transfer_excluded_per_6_3"
    UNKNOWN_SIDE = "unrecognised_side"
    NO_SYMBOL = "no_symbol"
    NO_QUANTITY = "no_quantity"
    UNRESOLVED_SYMBOL = "symbol_could_not_be_resolved"


def to_ghostfolio_symbol(
    canonical: CanonicalSymbol,
    *,
    crypto_overrides: dict[str, str] | None = None,
) -> tuple[str, DataSource]:
    """Map canonical form out to Ghostfolio's ``(symbol, dataSource)``.

    Equities resolve deterministically to Yahoo's convention. Crypto does
    not — it must have been verified against the running instance first.
    """
    if canonical.kind is AssetKind.CRYPTO:
        overrides = crypto_overrides or {}
        mapped = overrides.get(canonical.code) or overrides.get(canonical.canonical_id)
        if not mapped:
            raise CryptoSymbolNotVerified(
                f"no verified Ghostfolio symbol for {canonical.canonical_id}. "
                "Create one activity for this asset manually in the Ghostfolio "
                "UI, read it back via GET /api/v1/order, and record the exact "
                "symbol it produced. Do not guess (§7.1)."
            )
        # An override may carry its own data source as "SOURCE:symbol".
        if ":" in mapped:
            source_name, _, symbol = mapped.partition(":")
            return symbol, DataSource(source_name.upper())
        return mapped, DataSource.COINGECKO

    if canonical.venue is Venue.HK:
        # Yahoo uses a 4-digit HK code (`0700.HK`, `9988.HK`); canonical
        # stores HKEX's official 5 digits. Narrow, don't truncate.
        digits = canonical.code.lstrip("0") or "0"
        padded = digits.rjust(_HK_YAHOO_WIDTH, "0") if len(digits) <= _HK_YAHOO_WIDTH else digits
        return f"{padded}.HK", DataSource.YAHOO

    if canonical.venue is Venue.US:
        return canonical.code, DataSource.YAHOO
    if canonical.venue is Venue.SH:
        return f"{canonical.code}.SS", DataSource.YAHOO
    if canonical.venue is Venue.SZ:
        return f"{canonical.code}.SZ", DataSource.YAHOO
    if canonical.venue is Venue.SG:
        return f"{canonical.code}.SI", DataSource.YAHOO
    if canonical.venue is Venue.JP:
        return f"{canonical.code}.T", DataSource.YAHOO

    # CASH has no Ghostfolio instrument; callers filter it out before here.
    raise CryptoSymbolNotVerified(
        f"no Ghostfolio mapping for {canonical.canonical_id}"
    )


def external_id_for(transaction: Transaction) -> str:
    """Stable external id for the idempotency ledger (§3.3, §7.1).

    Derived only from fields the source itself assigns, so re-running a sync
    produces the same id. Deliberately excludes anything we compute — a
    normalisation change must not orphan every previously pushed record.
    """
    account = transaction.account_id or "-"
    return f"{transaction.source}:{account}:{transaction.transaction_id}"


def _classify(side: str | None) -> ActivityType | SkipReason:
    if side is None:
        return SkipReason.UNKNOWN_SIDE
    normalized = side.strip().lower()
    if normalized in _BUY_SIDES:
        return ActivityType.BUY
    if normalized in _SELL_SIDES:
        return ActivityType.SELL
    if normalized in _DIVIDEND_SIDES:
        return ActivityType.DIVIDEND
    if normalized in _INTEREST_SIDES:
        return ActivityType.INTEREST
    if normalized in _FEE_SIDES:
        return ActivityType.FEE
    if normalized in _TRANSFER_SIDES:
        return SkipReason.TRANSFER
    return SkipReason.UNKNOWN_SIDE


def _iso(moment: datetime) -> str:
    aware = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()


class MappedActivity:
    """One activity, plus the bookkeeping the sync layer needs."""

    __slots__ = ("external_id", "payload", "source")

    def __init__(self, *, external_id: str, payload: dict[str, object], source: str) -> None:
        self.external_id = external_id
        self.payload = payload
        self.source = source


class SkippedActivity:
    """One activity that was deliberately not pushed, and why."""

    __slots__ = ("external_id", "reason", "detail")

    def __init__(self, *, external_id: str, reason: SkipReason, detail: str = "") -> None:
        self.external_id = external_id
        self.reason = reason
        self.detail = detail


def map_transactions(
    transactions: Iterable[Transaction],
    *,
    account_id_by_source: dict[str, str],
    crypto_overrides: dict[str, str] | None = None,
) -> tuple[list[MappedActivity], list[SkippedActivity]]:
    """Map internal transactions to Ghostfolio activities.

    Returns ``(mapped, skipped)``. Nothing is dropped silently — every
    exclusion carries a reason so the digest and ``source_health`` can
    report it (§6.4).
    """
    mapped: list[MappedActivity] = []
    skipped: list[SkippedActivity] = []

    for transaction in transactions:
        external_id = external_id_for(transaction)
        classification = _classify(transaction.side)

        if isinstance(classification, SkipReason):
            skipped.append(
                SkippedActivity(
                    external_id=external_id,
                    reason=classification,
                    detail=f"side={transaction.side!r}",
                )
            )
            continue

        activity_type = classification
        needs_instrument = activity_type in (
            ActivityType.BUY,
            ActivityType.SELL,
            ActivityType.DIVIDEND,
        )

        if needs_instrument and not transaction.symbol:
            skipped.append(
                SkippedActivity(external_id=external_id, reason=SkipReason.NO_SYMBOL)
            )
            continue

        symbol_out: str
        data_source: DataSource | None
        if transaction.symbol:
            try:
                canonical = resolve(
                    transaction.symbol, currency=transaction.currency
                )
                symbol_out, data_source = to_ghostfolio_symbol(
                    canonical, crypto_overrides=crypto_overrides
                )
            except (ValueError, CryptoSymbolNotVerified) as exc:
                skipped.append(
                    SkippedActivity(
                        external_id=external_id,
                        reason=SkipReason.UNRESOLVED_SYMBOL,
                        detail=str(exc),
                    )
                )
                continue
        else:
            # FEE / INTEREST need no instrument; Ghostfolio resolves a
            # default data source for these types server-side.
            symbol_out, data_source = _cash_placeholder(transaction), None

        quantity = transaction.quantity
        if needs_instrument and (quantity is None or quantity <= 0):
            skipped.append(
                SkippedActivity(
                    external_id=external_id,
                    reason=SkipReason.NO_QUANTITY,
                    detail=f"quantity={quantity!r}",
                )
            )
            continue

        unit_price = transaction.price
        if unit_price is None and transaction.amount is not None and quantity:
            unit_price = transaction.amount / quantity

        payload: dict[str, object] = {
            "currency": (transaction.currency or "USD").upper(),
            "date": _iso(transaction.timestamp),
            # The internal model has no fee field yet — see §5.6/§5.9/§6.3b.
            # 0 is the honest value until it does; it is never a guess.
            "fee": Decimal("0"),
            "quantity": quantity if quantity is not None else Decimal("0"),
            "symbol": symbol_out,
            "type": activity_type.value,
            "unitPrice": unit_price if unit_price is not None else Decimal("0"),
            # Carries the external id into Ghostfolio so a human reading the
            # UI can trace a row back to its source record. The idempotency
            # ledger, not this field, is authoritative (§3.3).
            "comment": external_id,
        }
        if data_source is not None:
            payload["dataSource"] = data_source.value

        account_id = account_id_by_source.get(transaction.source)
        if account_id:
            payload["accountId"] = account_id

        mapped.append(
            MappedActivity(
                external_id=external_id, payload=payload, source=transaction.source
            )
        )

    if skipped:
        _LOG.info(
            "ghostfolio mapping skipped %d of %d activities: %s",
            len(skipped),
            len(skipped) + len(mapped),
            {r.value: sum(1 for s in skipped if s.reason is r) for r in SkipReason},
        )
    return mapped, skipped


def _cash_placeholder(transaction: Transaction) -> str:
    """Symbol used for instrument-less activities (FEE, INTEREST)."""
    return (transaction.currency or "USD").upper()


__all__ = [
    "ActivityType",
    "CryptoSymbolNotVerified",
    "DataSource",
    "MappedActivity",
    "SkipReason",
    "SkippedActivity",
    "external_id_for",
    "map_transactions",
    "to_ghostfolio_symbol",
]
