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
  one; see ``CryptoSymbolNotVerifiedError``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from app.models.domain import NON_PUSHABLE_TYPES, Transaction, TransactionType
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


class CryptoSymbolNotVerifiedError(RuntimeError):
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


# Internal type -> Ghostfolio activity type. Only these five cross the
# boundary; everything in NON_PUSHABLE_TYPES is excluded by §6.3, and the
# classification itself now lives in the domain model rather than being
# re-derived from strings here.
_TYPE_TO_ACTIVITY: dict[TransactionType, ActivityType] = {
    TransactionType.BUY: ActivityType.BUY,
    TransactionType.SELL: ActivityType.SELL,
    TransactionType.DIVIDEND: ActivityType.DIVIDEND,
    TransactionType.INTEREST: ActivityType.INTEREST,
    TransactionType.FEE: ActivityType.FEE,
}

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
            raise CryptoSymbolNotVerifiedError(
                f"no verified Ghostfolio symbol for {canonical.canonical_id}. "
                "Verify one against the running instance and record it in "
                "config/ghostfolio_symbols.yaml:\n"
                "  GET /api/v1/symbol/lookup?query=<name>     (what it knows)\n"
                "  GET /api/v1/symbol/<dataSource>/<symbol>   (does it price?)\n"
                "Do not guess (§7.1) — Yahoo's own BTC-USD is a 404 here."
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
    raise CryptoSymbolNotVerifiedError(
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


def _classify(transaction: Transaction) -> ActivityType | SkipReason:
    """Decide what, if anything, this transaction becomes in Ghostfolio."""
    tx_type = transaction.type
    if tx_type is None:
        return SkipReason.UNKNOWN_SIDE
    if tx_type in NON_PUSHABLE_TYPES:
        return SkipReason.TRANSFER
    activity = _TYPE_TO_ACTIVITY.get(tx_type)
    return activity if activity is not None else SkipReason.UNKNOWN_SIDE


def _resolve_fee(transaction: Transaction) -> Decimal:
    """The fee to send, in the activity's own currency (§5.6, §5.9).

    Ghostfolio stores a single ``fee`` number with no currency of its own,
    so it is implicitly denominated in the activity's currency. Binance
    frequently charges commission in **BNB** rather than the quote asset,
    and §5.6 requires converting that to the transaction currency at the
    trade-time rate — which has to happen upstream, where the rate is
    known.

    If an unconverted foreign-currency fee reaches here, sending the raw
    number would state a BNB amount as though it were USD. That is a silent
    mis-statement of cost basis, so we drop the fee and shout instead.

    Dropping the fee rather than the whole trade is deliberate: the trade
    is the large, load-bearing number and losing it would be a permanently
    wrong position (§5.5), whereas a missing fee is small, recoverable, and
    will show up in reconciliation (§6.4).
    """
    fee = transaction.fee
    if fee is None:
        return Decimal("0")

    fee_currency = (transaction.fee_currency or "").strip().upper()
    tx_currency = (transaction.currency or "").strip().upper()
    if fee_currency and tx_currency and fee_currency != tx_currency:
        _LOG.warning(
            "dropping unconverted fee on %s:%s — %s %s cannot be sent as %s. "
            "Convert to the transaction currency at the trade-time rate "
            "upstream (§5.6). Trade itself is unaffected.",
            transaction.source,
            transaction.transaction_id,
            fee,
            fee_currency,
            tx_currency,
        )
        return Decimal("0")
    return fee


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
        external_id = transaction.external_id or external_id_for(transaction)
        classification = _classify(transaction)

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
        # Types that are meaningless without an instrument. A FEE is not
        # one of them — it may or may not name a symbol — but when it
        # does, the symbol is resolved all the same (see below).
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
        data_source: DataSource
        # A non-tradeable activity NEVER names an instrument, even when
        # the source told us which one the charge relates to.
        #
        # Verified against the running 3.67.0 instance rather than assumed
        # (§7.1): importing one FEE with `symbol=SOFI, dataSource=YAHOO`
        # does not attach it to SoFi. Ghostfolio treats FEE, INTEREST and
        # LIABILITY as non-tradeable and mints its own asset — MANUAL,
        # with a random UUID for a symbol and the string we sent as its
        # *name*. There is no payload that avoids this.
        #
        # Worse, and this is the part that corrupted the portfolio: in the
        # same import batch, a BUY of SOFI is then filed under that UUID
        # as well. Probed directly — the FEE and the BUY came back sharing
        # symbol `886aa1a9-…`, differing only in dataSource. That is how
        # VOO ended up split across three instruments (3.9538 + 1.5835 +
        # 2.6742 of the same ETF), and how a ghost "The Coca-Cola Company"
        # came to hold +10 shares against a -10 in its twin.
        #
        # So a fee is booked against its currency. The money is not lost:
        # Ghostfolio's `fee` is subtracted from performance wherever the
        # activity sits. What is lost is the attribution to the
        # instrument, which Ghostfolio cannot represent anyway.
        if transaction.symbol and needs_instrument:
            try:
                canonical = resolve(
                    transaction.symbol,
                    exchange=transaction.exchange,
                    currency=transaction.currency,
                )
                symbol_out, data_source = to_ghostfolio_symbol(
                    canonical, crypto_overrides=crypto_overrides
                )
            except (ValueError, CryptoSymbolNotVerifiedError) as exc:
                skipped.append(
                    SkippedActivity(
                        external_id=external_id,
                        reason=SkipReason.UNRESOLVED_SYMBOL,
                        detail=str(exc),
                    )
                )
                continue
        else:
            # A charge, not a holding. The currency is the placeholder, so
            # every account-level cost of one currency collapses into a
            # single MANUAL asset named "USD" rather than one per
            # instrument — and, crucially, that name can never collide
            # with a real ticker and capture its trades.
            symbol_out, data_source = _cash_placeholder(transaction), DataSource.MANUAL

        quantity = transaction.quantity

        # A cash-settled dividend carries an `amount` and no share count:
        # Flex's CashTransaction rows, and every other source that reports
        # the money rather than the per-share rate. Ghostfolio values an
        # activity as quantity x unitPrice, so the cash total is expressed
        # as 1 x amount. Without this the dividend is dropped for having no
        # quantity and the income silently disappears.
        if (
            activity_type is ActivityType.DIVIDEND
            and (quantity is None or quantity <= 0)
            and transaction.amount is not None
        ):
            quantity = Decimal("1")

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

        # For a FEE the money *is* the fee: withholding tax, an account
        # charge, a commission adjustment. These arrive as an amount with
        # no fee field of their own, and sending fee=0 records the event
        # while throwing away what it cost. Ghostfolio's `fee` is what it
        # subtracts, so the amount belongs there and the activity itself
        # is valueless (quantity x unitPrice = 0).
        fee_out = _resolve_fee(transaction)
        if (
            activity_type is ActivityType.FEE
            and transaction.fee is None
            and transaction.amount is not None
        ):
            fee_out = abs(transaction.amount)
            unit_price = Decimal("0")

        payload: dict[str, object] = {
            "currency": (transaction.currency or "USD").upper(),
            "date": _iso(transaction.timestamp),
            # A real cost when the source reported one (§5.6, §5.9, §6.3b);
            # 0 only when it genuinely did not. Never a guess. See
            # `_resolve_fee` for why a mismatched fee currency drops the
            # fee but keeps the trade.
            "fee": fee_out,
            "quantity": quantity if quantity is not None else Decimal("0"),
            "symbol": symbol_out,
            "type": activity_type.value,
            "unitPrice": unit_price if unit_price is not None else Decimal("0"),
            # Carries the external id into Ghostfolio so a human reading the
            # UI can trace a row back to its source record. The idempotency
            # ledger, not this field, is authoritative (§3.3).
            "comment": external_id,
            # Always sent. Omitting it is what made Ghostfolio invent a
            # UUID-symbol profile and then file the instrument's other
            # activities under it.
            "dataSource": data_source.value,
        }

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
    """Symbol used for instrument-less activities (FEE, INTEREST).

    The ``GF_`` prefix is not decoration. Ghostfolio 3.67 rejects a
    MANUAL activity outright unless its symbol is a UUID or starts with
    ``GF_``::

        400 activities.0.symbol ("HKD") must be a UUID or start with the
            prefix "GF_" for the data source ("MANUAL")

    which is also *why* it mints a UUID when no dataSource is given —
    that is the only other symbol shape it will accept. A UUID is a new
    asset on every import; ``GF_USD`` is the same asset every time, so a
    year of account charges collects in one readable row instead of a
    fresh unidentifiable one per sync.
    """
    return f"GF_{(transaction.currency or 'USD').upper()}"


__all__ = [
    "ActivityType",
    "CryptoSymbolNotVerifiedError",
    "DataSource",
    "MappedActivity",
    "SkipReason",
    "SkippedActivity",
    "external_id_for",
    "map_transactions",
    "to_ghostfolio_symbol",
]
