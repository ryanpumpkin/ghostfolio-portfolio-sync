"""Server-side cache of each user's most recent portfolio snapshot.

Broker connections are E2E-encrypted: the backend can only decrypt credentials
in-request, when the Flutter client supplies its unwrap key. The background
daily-digest worker has no such key, so it cannot fetch positions live. To let
the digest email include positions anyway, the ``/portfolio`` endpoint writes
the *derived* snapshot here (market values + P&L only — NEVER credentials) and
the worker reads it back.

Stored at ``portfolio_snapshots/{uid}`` in Firestore, with an in-memory
fallback for local/test runs or when Firestore is momentarily unavailable.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger
from app.models.domain import PortfolioSnapshot

logger = get_logger(__name__)


def _coerce_dt(raw: Any) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return datetime.now(UTC)


class PortfolioSnapshotCache:
    """Caches the latest PortfolioSnapshot per user. Derived data only."""

    _COLLECTION = "portfolio_snapshots"
    # Portfolio polling can be frequent; the digest only needs day-level
    # freshness, so cap Firestore writes to one per interval per user.
    _MIN_WRITE_INTERVAL = 60.0

    def __init__(self, *, firestore_client: Any | None = None) -> None:
        self._firestore_client = firestore_client
        self._mem: dict[str, dict[str, Any]] = {}
        self._last_write: dict[str, float] = {}

    def _client(self) -> Any | None:
        if self._firestore_client is not None:
            return self._firestore_client
        try:
            from firebase_admin import firestore

            return firestore.client()
        except Exception:
            return None

    def _doc(self, client: Any, user_id: str) -> Any:
        return client.collection(self._COLLECTION).document(user_id)

    async def put(self, user_id: str, snapshot: PortfolioSnapshot) -> None:
        now = time.monotonic()
        last = self._last_write.get(user_id)
        if last is not None and (now - last) < self._MIN_WRITE_INTERVAL:
            return
        self._last_write[user_id] = now
        payload = {
            "snapshot": snapshot.model_dump(mode="json"),
            "cached_at": datetime.now(UTC).isoformat(),
        }
        client = self._client()
        if client is None:
            self._mem[user_id] = payload
            return
        try:
            await asyncio.to_thread(lambda: self._doc(client, user_id).set(payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("portfolio_cache_write_failed", user_id=user_id, error=str(exc))
            self._mem[user_id] = payload

    async def get(self, user_id: str) -> tuple[PortfolioSnapshot, datetime] | None:
        client = self._client()
        payload: dict[str, Any] | None = None
        if client is None:
            payload = self._mem.get(user_id)
        else:
            try:
                snap = await asyncio.to_thread(self._doc(client, user_id).get)
                if getattr(snap, "exists", False):
                    payload = snap.to_dict()
            except Exception as exc:  # noqa: BLE001
                logger.warning("portfolio_cache_read_failed", user_id=user_id, error=str(exc))
                payload = self._mem.get(user_id)
        if not payload or "snapshot" not in payload:
            return None
        try:
            snapshot = PortfolioSnapshot.model_validate(payload["snapshot"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("portfolio_cache_parse_failed", user_id=user_id, error=str(exc))
            return None
        return snapshot, _coerce_dt(payload.get("cached_at"))


__all__ = ["PortfolioSnapshotCache"]
