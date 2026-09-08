"""Background alert worker runtime."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import UTC, datetime
from uuid import uuid4

from app.core.logging import get_logger
from app.services.alert_worker import (
    AdapterQuoteProvider,
    AlertEngine,
    FirebaseAdminPushGateway,
    FirestoreAlertRepository,
    FirestoreDeviceTokenStore,
    FirestoreWorkerLeaseStore,
    WorkerLeaseStore,
)
from app.services.dependencies import get_adapter_registry


class AlertWorker:
    """Periodic background loop that evaluates and dispatches alerts."""

    def __init__(
        self,
        *,
        engine: AlertEngine,
        lease_store: WorkerLeaseStore,
        interval_seconds: float = 60.0,
        lease_name: str = "alert-worker",
        lease_ttl_seconds: int = 55,
        owner_id: str | None = None,
    ) -> None:
        self._engine = engine
        self._lease_store = lease_store
        self._interval_seconds = interval_seconds
        self._lease_name = lease_name
        self._lease_ttl_seconds = lease_ttl_seconds
        self._owner_id = owner_id or str(uuid4())
        self._stop_event = asyncio.Event()
        self._logger = get_logger(__name__)
        self._installed_signals: list[signal.Signals] = []

    async def run(self) -> None:
        """Run periodic ticks until a shutdown signal is received."""
        self.install_signal_handlers()
        self._logger.info(
            "alert_worker_started",
            owner_id=self._owner_id,
            interval_seconds=self._interval_seconds,
            lease_name=self._lease_name,
            lease_ttl_seconds=self._lease_ttl_seconds,
        )
        while not self._stop_event.is_set():
            tick_started = time.monotonic()
            try:
                await self.run_single_tick()
            except Exception as exc:  # noqa: BLE001 - keep loop alive
                self._logger.exception("alert_worker_tick_failed", error=str(exc))

            elapsed = time.monotonic() - tick_started
            sleep_seconds = max(0.0, self._interval_seconds - elapsed)
            if sleep_seconds <= 0:
                continue
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_seconds)
            except TimeoutError:
                continue

        self._logger.info("alert_worker_stopped", owner_id=self._owner_id)

    async def run_single_tick(self) -> bool:
        """Run one tick if this replica acquires the singleton lease."""
        now = datetime.now(UTC)
        acquired = await self._lease_store.try_acquire(
            lease_name=self._lease_name,
            owner_id=self._owner_id,
            ttl_seconds=self._lease_ttl_seconds,
            now=now,
        )
        if not acquired:
            self._logger.debug("alert_worker_lease_skipped", owner_id=self._owner_id)
            return False
        fired = await self._engine.run_once(now=now)
        self._logger.info("alert_worker_tick", owner_id=self._owner_id, fired=fired)
        return True

    def request_shutdown(self) -> None:
        """Request graceful shutdown from signal handlers or callers."""
        self._stop_event.set()

    def install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers that stop the async loop cleanly."""
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signum, self.request_shutdown)
                self._installed_signals.append(signum)
            except (NotImplementedError, RuntimeError):
                # Some platforms/event loops do not support custom signal handlers.
                continue


def build_default_worker() -> AlertWorker:
    """Build default worker wiring using placeholder Firestore integrations."""
    interval_seconds = float(os.getenv("MBP_ALERT_WORKER_INTERVAL_SECONDS", "60"))
    lease_ttl_seconds = int(os.getenv("MBP_ALERT_WORKER_LEASE_TTL_SECONDS", "55"))
    lease_name = os.getenv("MBP_ALERT_WORKER_LEASE_NAME", "alert-worker")

    engine = AlertEngine(
        alerts=FirestoreAlertRepository(),
        quote_provider=AdapterQuoteProvider(get_adapter_registry()),
        device_tokens=FirestoreDeviceTokenStore(),
        push_gateway=FirebaseAdminPushGateway(),
    )
    return AlertWorker(
        engine=engine,
        lease_store=FirestoreWorkerLeaseStore(),
        interval_seconds=interval_seconds,
        lease_name=lease_name,
        lease_ttl_seconds=lease_ttl_seconds,
    )


async def run_alert_worker() -> None:
    """Entrypoint for worker process."""
    worker = build_default_worker()
    await worker.run()


__all__ = ["AlertWorker", "build_default_worker", "run_alert_worker"]
