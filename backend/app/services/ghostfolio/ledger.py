"""Idempotency and resumability ledger for the Ghostfolio push (§3.3, §3.4).

Two spec requirements meet here:

* **§3.3 — idempotent by construction.** "Running the sync three times must
  produce exactly the same state as running it once." Each source record
  gets a stable external id; this table records which ones have been
  pushed, and the sync skips the rest.
* **§3.4 — resumable.** "Persist progress per source. An aborted run must
  not restart from zero."

Why a local ledger when Ghostfolio already deduplicates
-------------------------------------------------------
Testing against the live instance showed Ghostfolio's import dedup takes
the ``comment`` field into account, and the exporter writes the external
id there — so a re-import genuinely creates no duplicates. That is a
useful backstop, but it is not a substitute for this table:

1. It is undocumented behaviour. It is not in the API contract and could
   change in any release; the whole reason §7.1 exists is that
   Ghostfolio's import surface moves.
2. It does not give resumability. Ghostfolio can tell us "this row already
   exists" only after we have re-fetched, re-normalised and re-uploaded
   everything — which for the one-off Binance crawl (§5) means re-hitting
   an API whose key gets revoked afterwards.
3. It cannot record a cursor, so an aborted run has no idea where it got
   to.

Storage
-------
SQLite, on local disk. Deliberately not Firestore: this is derived state
that can be rebuilt from the sources, so it does not belong in the one
cloud dependency the stack has (§11.5), and a sync job should not fail
because an auth service is down.

WAL mode plus a single transaction per batch means an interrupted run
leaves the ledger consistent — either the whole batch is recorded or none
of it is.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterable, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from app.services.ghostfolio.mapper import MappedActivity

_LOG = logging.getLogger("mbp.ghostfolio.ledger")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pushed_activities (
    external_id   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    ghostfolio_id TEXT,
    pushed_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pushed_source ON pushed_activities(source);

CREATE TABLE IF NOT EXISTS sync_cursors (
    source     TEXT PRIMARY KEY,
    cursor     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SyncLedger:
    """Records what has been pushed, and how far each source has got."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # A connection is not safe to share across threads, and the sync
        # may run under a thread pool; serialise access rather than hand
        # out per-thread connections that would each need their own WAL
        # checkpointing.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL: a reader never blocks the writer, and an interrupted process
        # leaves a recoverable database rather than a truncated one.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> SyncLedger:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            self._conn.commit()

    # ── idempotency (§3.3) ──────────────────────────────────────────────

    def is_pushed(self, external_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM pushed_activities WHERE external_id = ?",
                (external_id,),
            ).fetchone()
        return row is not None

    def pushed_ids(self, external_ids: Iterable[str]) -> set[str]:
        """Which of these have already been pushed. One query, not N."""
        ids = list(external_ids)
        if not ids:
            return set()
        found: set[str] = set()
        # SQLite caps host parameters (999 on older builds); chunk so a
        # large historical import cannot blow the limit.
        with self._lock:
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                placeholders = ",".join("?" * len(chunk))
                rows = self._conn.execute(
                    f"SELECT external_id FROM pushed_activities "  # noqa: S608 - placeholders only
                    f"WHERE external_id IN ({placeholders})",
                    chunk,
                ).fetchall()
                found.update(row["external_id"] for row in rows)
        return found

    def filter_unpushed(
        self, activities: Sequence[MappedActivity]
    ) -> list[MappedActivity]:
        """Drop activities already recorded as pushed.

        This is what makes a third run identical to the first (§3.3).
        """
        if not activities:
            return []
        already = self.pushed_ids(a.external_id for a in activities)
        remaining = [a for a in activities if a.external_id not in already]
        if already:
            _LOG.info(
                "ledger: skipping %d already-pushed activities, %d to push",
                len(already),
                len(remaining),
            )
        return remaining

    def record_pushed(
        self,
        activities: Sequence[MappedActivity],
        *,
        ghostfolio_ids: dict[str, str] | None = None,
    ) -> None:
        """Mark activities as pushed, all-or-nothing.

        Call this only **after** the import has been accepted. Recording
        first would mean a failed import silently marks records as done and
        they are never retried — data loss that no later run can detect.
        """
        if not activities:
            return
        ids = ghostfolio_ids or {}
        stamp = _now()
        rows = [
            (a.external_id, a.source, ids.get(a.external_id), stamp)
            for a in activities
        ]
        with self._transaction() as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO pushed_activities "
                "(external_id, source, ghostfolio_id, pushed_at) "
                "VALUES (?, ?, ?, ?)",
                rows,
            )
        _LOG.info("ledger: recorded %d pushed activities", len(rows))

    def forget(self, external_ids: Iterable[str]) -> int:
        """Remove ledger entries so they will be pushed again.

        Needed when activities are deleted in Ghostfolio directly — without
        this the ledger would claim they exist forever.
        """
        ids = list(external_ids)
        if not ids:
            return 0
        with self._transaction() as conn:
            cursor = conn.executemany(
                "DELETE FROM pushed_activities WHERE external_id = ?",
                [(i,) for i in ids],
            )
            deleted = cursor.rowcount
        return max(deleted, 0)

    def count(self, source: str | None = None) -> int:
        with self._lock:
            if source is None:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM pushed_activities"
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM pushed_activities WHERE source = ?",
                    (source,),
                ).fetchone()
        return int(row["n"])

    # ── resumability (§3.4) ─────────────────────────────────────────────

    def get_cursor(self, source: str) -> str | None:
        """Where this source got to last time, or None for a first run."""
        with self._lock:
            row = self._conn.execute(
                "SELECT cursor FROM sync_cursors WHERE source = ?", (source,)
            ).fetchone()
        return row["cursor"] if row else None

    def set_cursor(self, source: str, cursor: str) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO sync_cursors (source, cursor, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(source) DO UPDATE SET cursor=excluded.cursor, "
                "updated_at=excluded.updated_at",
                (source, cursor, _now()),
            )

    def cursors(self) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT source, cursor FROM sync_cursors"
            ).fetchall()
        return {row["source"]: row["cursor"] for row in rows}


__all__ = ["SyncLedger"]
