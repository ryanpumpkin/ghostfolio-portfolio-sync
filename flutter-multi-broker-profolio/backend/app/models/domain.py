"""Domain models mirroring the Flutter client's domain entities.

These are the wire format for REST responses; broker adapters will produce
them and the aggregator will fan them in. Generic types live alongside the
concrete entities so callers can express partial results with a single
import.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

T = TypeVar("T")


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class TransactionType(StrEnum):
    """What a transaction actually *is* (spec §6.3, §6.3b).

    ``Transaction.side`` is whatever string the source emitted. This enum is
    the normalised meaning, and it exists because one distinction in
    particular must not be left to a free-form string comparison at each
    call site: moving your own assets between your own accounts is a
    **custody change, not a disposal**.
    """

    BUY = "buy"
    SELL = "sell"
    DIVIDEND = "dividend"
    INTEREST = "interest"
    FEE = "fee"
    # Own account -> own account. Binance -> Ledger, bank -> broker,
    # broker -> broker. Never a BUY or a SELL (§6.3).
    TRANSFER = "transfer"
    # Cash in/out where the counterparty is external or not yet known to be
    # the owner's. Kept distinct from TRANSFER so the information is not
    # lost, but excluded from the Ghostfolio push all the same.
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"


#: Types that are **never** pushed to Ghostfolio (§6.3).
#:
#: Ghostfolio tracks the asset, not where it sits — and its own `Type` enum
#: has no TRANSFER member, so this is how the tool is meant to be used, not
#: a workaround. If any of these ever became a BUY or SELL, cost basis
#: would be destroyed silently and every downstream number would be wrong.
#:
#: Note that a *swap* is not in this set: a wallet-level or on-chain swap
#: (Ledger Live, a DEX trade, an exchange "convert") realises a gain and is
#: modelled as a SELL plus a BUY sharing a ``correlation_id`` (§6.3b).
NON_PUSHABLE_TYPES: frozenset[TransactionType] = frozenset({
    TransactionType.TRANSFER,
    TransactionType.DEPOSIT,
    TransactionType.WITHDRAWAL,
})

_SIDE_ALIASES: dict[str, TransactionType] = {
    "buy": TransactionType.BUY,
    "b": TransactionType.BUY,
    "bought": TransactionType.BUY,
    "long": TransactionType.BUY,
    "sell": TransactionType.SELL,
    "s": TransactionType.SELL,
    "sold": TransactionType.SELL,
    "short": TransactionType.SELL,
    "dividend": TransactionType.DIVIDEND,
    "div": TransactionType.DIVIDEND,
    "interest": TransactionType.INTEREST,
    "fee": TransactionType.FEE,
    "commission": TransactionType.FEE,
    "transfer": TransactionType.TRANSFER,
    "transfer_in": TransactionType.TRANSFER,
    "transfer_out": TransactionType.TRANSFER,
    "custody_change": TransactionType.TRANSFER,
    "deposit": TransactionType.DEPOSIT,
    "withdrawal": TransactionType.WITHDRAWAL,
    "withdraw": TransactionType.WITHDRAWAL,
}


def classify_side(side: str | None) -> TransactionType | None:
    """Normalise a source's ``side`` string, or None if unrecognised.

    Returns None rather than guessing. An unrecognised side is surfaced by
    the caller and skipped — inventing a type here is how a transfer would
    become a sale.
    """
    if side is None:
        return None
    return _SIDE_ALIASES.get(side.strip().lower())


class SourceHealthStatus(StrEnum):
    """Coarse health of a single data source."""

    OK = "ok"
    DEGRADED = "degraded"
    DOWN = "down"


class SourceHealth(_Base):
    """Per-source health record attached to aggregated responses."""

    source: str
    status: SourceHealthStatus
    message: str | None = None
    last_success_at: datetime | None = None


class Position(_Base):
    """A holding in a single instrument at a single broker."""

    source: str
    account_id: str | None = None
    symbol: str
    exchange: str | None = None
    quantity: Decimal
    avg_cost: Decimal | None = None
    last_price: Decimal | None = None
    currency: str
    market_value: Decimal | None = None
    unrealized_pnl: Decimal | None = None

    # Where the asset physically sits (§4.4). Distinct from `source`, which
    # is only where the *data* came from — a Ledger holding may be reported
    # by manual entry (source="manual", custody="ledger").
    #
    # This exists because the owner will hold the same asset in two places
    # at once: a sub-minimum BTC balance waiting at Futu to reach the
    # withdrawal threshold, plus the self-custody balance on the Ledger.
    # Ghostfolio only needs the total, but reconciliation (§6.4) compares
    # the authoritative per-venue quantity against the derived one, so
    # summing across custody locations would report drift that isn't real.
    #
    # Convention: lowercase venue name ("futu", "binance", "ibkr") or
    # "ledger" for self-custody. None means "same as source".
    custody: str | None = None

    @property
    def custody_location(self) -> str:
        """Effective custody location, defaulting to the reporting source."""
        return self.custody or self.source


class CashBalance(_Base):
    """Cash held in a single currency at a single broker."""

    source: str
    account_id: str | None = None
    currency: str
    amount: Decimal


class Transaction(_Base):
    """A historical trade or cash movement."""

    source: str
    account_id: str | None = None
    transaction_id: str
    symbol: str | None = None
    # Where the instrument is listed, as the source reported it. Needed
    # because a bare ticker is ambiguous: `resolve()` can place `700.HK`
    # or `US.VOO` on its own, but IBKR emits `VOO` and nothing else, and
    # a venue that cannot be determined means the activity is dropped.
    # `Position` has carried this from the start; transactions lost it.
    exchange: str | None = None
    side: str | None = None  # raw, as the source emitted it
    quantity: Decimal | None = None
    price: Decimal | None = None
    currency: str | None = None
    amount: Decimal | None = None
    timestamp: datetime

    # ── normalised meaning (§6.3) ───────────────────────────────────────
    # Derived from `side` when not supplied, so all four existing adapters
    # keep working unchanged while downstream code gets a reliable enum
    # instead of comparing strings. Adapters may also set it directly when
    # the source distinguishes something `side` cannot express — notably a
    # withdrawal to the owner's own wallet, which is a TRANSFER (§6.3) and
    # not a WITHDRAWAL.
    type: TransactionType | None = None

    # ── costs (§5.6, §5.9, §6.3b) ───────────────────────────────────────
    # A fee is a real cost and must survive into the ledger. `fee_currency`
    # exists because Binance frequently charges commission in BNB rather
    # than the quote asset — recording the number without the asset it was
    # denominated in silently misstates the cost.
    fee: Decimal | None = None
    fee_currency: str | None = None

    # ── identity and linkage ────────────────────────────────────────────
    # Stable id for the idempotency ledger (§3.3). Derived from
    # source-assigned fields only, never from anything we compute, so a
    # change to normalisation cannot orphan already-pushed records.
    external_id: str | None = None
    # The other side of a movement: an address, or an account identifier.
    # Checked against the owner's own-accounts list to decide whether a
    # withdrawal is a disposal or a custody change (§6.3).
    counterparty: str | None = None
    # Links the two legs of a swap (§6.3b). A DOGE->BTC wallet swap is a
    # SELL and a BUY at the same timestamp sharing this id — it realises a
    # gain, unlike a TRANSFER. Never infer a swap from balance changes
    # alone; only ever set this from an explicit swap/convert record.
    correlation_id: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _derive_type(cls, data: object) -> object:
        if isinstance(data, dict) and data.get("type") is None:
            derived = classify_side(data.get("side"))
            if derived is not None:
                return {**data, "type": derived}
        return data

    @property
    def is_pushable(self) -> bool:
        """False for custody changes and cash movements (§6.3)."""
        return self.type is not None and self.type not in NON_PUSHABLE_TYPES


class Quote(_Base):
    """A live market quote for a single symbol."""

    source: str
    symbol: str
    price: Decimal
    currency: str
    timestamp: datetime


class FxRate(_Base):
    """Spot FX rate from `base` to `quote`."""

    base: str
    quote: str
    rate: Decimal
    as_of: datetime


class Connection(_Base):
    """User-configured connection to a data source."""

    source: str
    connection_id: str
    display_name: str
    server_key_mode: bool = False  # True = backend has KMS-encrypted credentials
    enabled: bool = True


class PortfolioSnapshot(_Base):
    """Aggregated portfolio for a user at a moment in time."""

    as_of: datetime
    base_currency: str
    positions: list[Position] = Field(default_factory=list)
    balances: list[CashBalance] = Field(default_factory=list)
    fx_rates: list[FxRate] = Field(default_factory=list)
    source_health: list[SourceHealth] = Field(default_factory=list)
    total_market_value: Decimal | None = None
    total_unrealized_pnl: Decimal | None = None
    # FIFO-matched realized P&L summed across every source × symbol ×
    # currency we can reach historical fills for. Brokers without full
    # trade history (notably IBKR via the standard TWS API) contribute
    # 0 here — they'd need a separate Flex Statement import to populate.
    total_realized_pnl: Decimal | None = None
    # Convenience: total_realized_pnl + total_unrealized_pnl, so the
    # dashboard can show one headline number without re-summing on the
    # client.
    total_return: Decimal | None = None


class PartialResult(BaseModel, Generic[T]):
    """Wrapper for fan-out responses where some sources may fail.

    Carries the successful items plus per-source health so the client can
    render the last-known data without blanking the dashboard on a single
    broker outage (see detailed-design §7.2 Resilience).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[T] = Field(default_factory=list)
    source_health: list[SourceHealth] = Field(default_factory=list)
