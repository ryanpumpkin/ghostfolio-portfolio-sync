"""What the last sync found, kept for a digest that runs days later.

The monthly digest (§10) runs offline: it cannot re-ask a broker, and
Futu's OpenD is not even running when it fires. Without a record of what
the last sync found, the digest would print "Reconciliation: clean"
having checked nothing at all — and a false reassurance is worse than no
line, because it is the line you stop reading.

So each sync writes its §6.4 findings here and the digest reads them
back, along with WHEN they were written. Age is part of the finding: a
clean report from three weeks ago is not the same claim as a clean
report from this morning, and the digest says which it has.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_LOG = logging.getLogger("mbp.health")


@dataclass(slots=True)
class SourceFindings:
    """One source's last reconciliation, as the sync saw it."""

    source: str
    checked_at: datetime
    gaps: list[str] = field(default_factory=list)
    surplus: list[str] = field(default_factory=list)
    no_cost: list[str] = field(default_factory=list)
    partial: str = ""

    @property
    def needs_attention(self) -> bool:
        # A gap is expected and already corrected by an opening balance.
        # SURPLUS is not: it means a duplicate or a lost disposal.
        return bool(self.surplus or self.no_cost or self.partial)

    def lines(self) -> list[str]:
        out = [f"  {self.source}: {s}   <-- investigate" for s in self.surplus]
        out += [f"  {self.source}: {s} (no cost available)" for s in self.no_cost]
        if self.partial:
            out.append(f"  {self.source}: {self.partial}")
        return out


class HealthStore:
    """Per-source findings from the most recent sync of each."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._rows: dict[str, SourceFindings] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            _LOG.warning("cannot read health store %s: %s", self.path, exc)
            return
        for source, row in (payload.get("sources") or {}).items():
            self._rows[source] = SourceFindings(
                source=source,
                checked_at=datetime.fromisoformat(row["checked_at"]),
                gaps=list(row.get("gaps") or []),
                surplus=list(row.get("surplus") or []),
                no_cost=list(row.get("no_cost") or []),
                partial=str(row.get("partial") or ""),
            )

    def record(
        self,
        *,
        source: str,
        gaps: list[str],
        surplus: list[str],
        no_cost: list[str],
        partial: str = "",
    ) -> None:
        self._rows[source] = SourceFindings(
            source=source,
            checked_at=datetime.now(UTC),
            gaps=gaps,
            surplus=surplus,
            no_cost=no_cost,
            partial=partial,
        )
        self._flush()

    def all(self) -> list[SourceFindings]:
        return sorted(self._rows.values(), key=lambda f: f.source)

    def stalest(self) -> datetime | None:
        """When the LEAST recently checked source was last looked at."""
        if not self._rows:
            return None
        return min(f.checked_at for f in self._rows.values())

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sources": {
                f.source: {
                    "checked_at": f.checked_at.isoformat(),
                    "gaps": f.gaps,
                    "surplus": f.surplus,
                    "no_cost": f.no_cost,
                    "partial": f.partial,
                }
                for f in self._rows.values()
            }
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)


__all__ = ["HealthStore", "SourceFindings"]
