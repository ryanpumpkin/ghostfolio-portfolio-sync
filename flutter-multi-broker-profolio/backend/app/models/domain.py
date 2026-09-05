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

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


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
    side: str | None = None  # "buy" / "sell" / "deposit" / "withdrawal" / ...
    quantity: Decimal | None = None
    price: Decimal | None = None
    currency: str | None = None
    amount: Decimal | None = None
    timestamp: datetime


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
