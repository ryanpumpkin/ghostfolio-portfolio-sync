"""Durable record of money crossing the portfolio boundary (§6.3, §8).

Why this exists
---------------
A money-weighted return on the *portfolio* needs flows at its boundary:
cash you put in, cash you took out, and what it is worth now. Trades are
internal — buying VOO moves money between two pockets you own.

But §6.3 keeps deposits and withdrawals out of Ghostfolio, correctly:
they are not trades and pushing them would corrupt cost basis. So the
one system that holds the whole history cannot answer the question, and
the brokers that can are only reachable during a sync — Futu's OpenD
runs for a few minutes a day and is down the rest of the time.

This store is the bridge. Each sync records whatever cash movements its
source reported; anything offline reads them back later.

Idempotency
-----------
Keyed by the source's own external id, so re-running a sync overwrites
rather than duplicates. That matters more here than for trades: a
duplicated deposit does not merely double a position, it silently
improves the return by pretending you contributed less than you did.

Transfers are stored too, and flagged
-------------------------------------
A movement between two accounts you own is NOT a portfolio flow — it
nets to zero and counting it as a contribution understates return.
`OwnAccountsRegistry` decides that at sync time (§6.3), and the verdict
is persisted with the row so a later reader does not have to re-derive
it from addresses it may no longer have.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

_LOG = logging.getLogger("mbp.cashflows")


@dataclass(frozen=True, slots=True)
class CashMovement:
    """One deposit, withdrawal or own-account transfer."""

    external_id: str
    source: str
    when: date
    amount: Decimal
    currency: str
    kind: str
    #: True when both ends are accounts the owner controls, so it must
    #: not count as a portfolio contribution.
    internal: bool = False
    account_id: str | None = None

    def as_row(self) -> dict[str, str | bool | None]:
        row = asdict(self)
        row["when"] = self.when.isoformat()
        row["amount"] = str(self.amount)
        return row

    @classmethod
    def from_row(cls, row: dict) -> CashMovement:
        return cls(
            external_id=str(row["external_id"]),
            source=str(row["source"]),
            when=date.fromisoformat(str(row["when"])),
            amount=Decimal(str(row["amount"])),
            currency=str(row["currency"]),
            kind=str(row["kind"]),
            internal=bool(row.get("internal", False)),
            account_id=row.get("account_id"),
        )


class CashFlowStore:
    """A JSON file of cash movements, keyed by external id.

    JSON rather than SQLite: this is small, append-mostly, and its whole
    value is being readable by a person checking why a return figure
    moved. A file you can open beats a query you have to write.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._rows: dict[str, CashMovement] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Refuse rather than start empty: silently forgetting every
            # recorded contribution would inflate the return and look
            # like a good day.
            msg = f"cannot read cash-flow store {self._path}: {exc}"
            raise RuntimeError(msg) from exc
        for row in payload.get("movements", []):
            movement = CashMovement.from_row(row)
            self._rows[movement.external_id] = movement

    def record(self, movements: list[CashMovement]) -> int:
        """Add or replace movements. Returns how many were new."""
        added = 0
        for movement in movements:
            if movement.external_id not in self._rows:
                added += 1
            self._rows[movement.external_id] = movement
        if movements:
            self._flush()
        return added

    def replace_source(self, source: str, movements: list[CashMovement]) -> int:
        """Swap out one source's movements wholesale.

        Used when a source can report its complete history each run: a
        movement the broker has stopped reporting should disappear
        rather than linger from an earlier sync.
        """
        self._rows = {
            key: row for key, row in self._rows.items() if row.source != source
        }
        return self.record(movements)

    def all(self) -> list[CashMovement]:
        return sorted(self._rows.values(), key=lambda m: (m.when, m.external_id))

    def external(self) -> list[CashMovement]:
        """Only movements that actually cross the portfolio boundary."""
        return [m for m in self.all() if not m.internal]

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "written": datetime.now().astimezone().isoformat(),
            "movements": [m.as_row() for m in self.all()],
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        _LOG.info("cash-flow store: %d movement(s) in %s", len(self._rows), self._path)


__all__ = ["CashFlowStore", "CashMovement"]
