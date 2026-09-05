"""Daily watchlist digest worker — sends an email once per day at a configured UTC hour."""

from __future__ import annotations

import asyncio
import os
import signal
from datetime import UTC, datetime
from typing import Any


from app.api.watchlist import get_watchlist_repo
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.services.analyst.longbridge_data import MarketDataUnavailable
from app.services.dependencies import get_analyst_service, get_portfolio_snapshot_cache
from app.services.watchlist import fetch_signals, send_digest_email

logger = get_logger(__name__)


class DailyDigestWorker:
    """Sends watchlist digest emails once per day at the configured UTC hour."""

    def __init__(self, *, digest_hour_utc: int = 8, manage_signals: bool = True) -> None:
        self._digest_hour = digest_hour_utc
        self._manage_signals = manage_signals
        self._stop_event = asyncio.Event()
        self._last_sent_date: str | None = None

    def request_shutdown(self) -> None:
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
            except (NotImplementedError, RuntimeError):
                pass

    async def run(self) -> None:
        # Embedded in the app lifespan we must NOT hijack SIGINT/SIGTERM —
        # uvicorn owns those for graceful shutdown. Only the standalone
        # entrypoint installs handlers.
        if self._manage_signals:
            self.install_signal_handlers()
        # Restore the last-sent date from persistent storage so a restart
        # after the digest hour does not re-send the same day's email.
        repo = get_watchlist_repo()
        try:
            self._last_sent_date = await repo.get_digest_state()
        except Exception as exc:  # noqa: BLE001
            logger.warning("daily_digest_state_load_failed", error=str(exc))
        logger.info(
            "daily_digest_worker_started",
            digest_hour_utc=self._digest_hour,
            last_sent_date=self._last_sent_date,
        )
        try:
            while not self._stop_event.is_set():
                now = datetime.now(UTC)
                today = now.strftime("%Y-%m-%d")
                if now.hour >= self._digest_hour and self._last_sent_date != today:
                    await self._send_all_digests()
                    self._last_sent_date = today
                    await repo.set_digest_state(today)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=300.0)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            pass
        logger.info("daily_digest_worker_stopped")

    async def _send_all_digests(self) -> None:
        settings = get_settings()
        if not settings.gmail_from_email or not settings.gmail_app_password:
            logger.warning("daily_digest_skipped_no_gmail_config")
            return

        repo = get_watchlist_repo()
        cache = get_portfolio_snapshot_cache()
        try:
            analyst = get_analyst_service()
        except Exception:  # noqa: BLE001 — SDK/config missing; signals fall back to Yahoo
            analyst = None
        for user_id in await repo.all_user_ids():
            symbols = await repo.symbols(user_id)
            if not symbols:
                continue
            recipient = await repo.email_for(user_id) or settings.gmail_digest_recipient
            if not recipient:
                logger.warning("daily_digest_no_recipient", user_id=user_id)
                continue
            try:
                signals = await fetch_signals(symbols, analyst=analyst)
                cached = await cache.get(user_id)
                snapshot, cached_at = cached if cached else (None, None)
                reports = await _analysis_reports(snapshot)
                send_digest_email(
                    to_email=recipient,
                    from_email=settings.gmail_from_email,
                    app_password=settings.gmail_app_password,
                    signals=signals,
                    snapshot=snapshot,
                    snapshot_cached_at=cached_at,
                    analysis_reports=reports,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("daily_digest_send_failed", user_id=user_id, error=str(exc))


async def _analysis_reports(snapshot: Any) -> list[Any]:
    """Build per-position analyst reports for the digest. The analyst service
    uses its own market-data feed (no user credentials needed), so this works
    in the background. Returns [] when analysis is unconfigured or fails so a
    missing analyst feed never blocks the digest email."""
    if snapshot is None or not getattr(snapshot, "positions", None):
        return []
    try:
        return await get_analyst_service().reports_for_positions(snapshot.positions)
    except MarketDataUnavailable:
        logger.info("daily_digest_analysis_unconfigured")
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("daily_digest_analysis_failed", error=str(exc))
        return []


async def run_daily_digest_worker() -> None:
    hour = int(os.getenv("MBP_DIGEST_HOUR_UTC", "8"))
    worker = DailyDigestWorker(digest_hour_utc=hour)
    await worker.run()
