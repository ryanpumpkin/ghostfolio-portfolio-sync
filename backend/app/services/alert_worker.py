"""Alert worker service contracts and orchestration helpers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.models.domain import Quote
from app.services.quote_hub import QuoteSourceRegistry


class AlertKind(StrEnum):
    """Supported alert conditions."""

    PRICE_ABOVE = "price_above"
    PRICE_BELOW = "price_below"
    PNL_ABOVE = "pnl_above"
    PNL_BELOW = "pnl_below"
    PNL_PCT_ABOVE = "pnl_pct_above"
    PNL_PCT_BELOW = "pnl_pct_below"


class AlertDefinition(BaseModel):
    """Alert definition loaded from persistent storage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: str
    user_id: str
    source: str
    symbol: str
    kind: AlertKind
    threshold: Decimal
    cooldown_seconds: int = 300
    enabled: bool = True
    server_key_enabled: bool = True
    scope: str = "portfolio"
    cost_basis: Decimal | None = None
    quantity: Decimal | None = None
    last_triggered_at: datetime | None = None
    notification_title: str | None = None
    notification_body: str | None = None


class AlertEvaluation(BaseModel):
    """Result of a fired alert condition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    alert_id: str
    triggered_at: datetime
    observed_value: Decimal
    threshold: Decimal
    reason: str


class AlertTriggerEvent(BaseModel):
    """Stored trigger event for alert history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str = Field(default_factory=lambda: str(uuid4()))
    alert_id: str
    user_id: str
    source: str
    symbol: str
    kind: AlertKind
    triggered_at: datetime
    observed_value: Decimal
    threshold: Decimal
    scope: str
    reason: str


class PushDispatchResult(BaseModel):
    """Outcome of sending one push message to many tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    success_count: int = 0
    failure_count: int = 0
    unregistered_tokens: list[str] = Field(default_factory=list)


class AlertRepository(Protocol):
    """Persistence adapter for alert definitions and events."""

    async def list_active_server_key_alerts(self) -> list[AlertDefinition]: ...

    async def record_trigger_event(self, event: AlertTriggerEvent) -> None: ...

    async def mark_alert_triggered(self, alert_id: str, triggered_at: datetime) -> None: ...


class DeviceTokenStore(Protocol):
    """Persistence adapter for per-user FCM registration tokens."""

    async def list_device_tokens(self, user_id: str) -> list[str]: ...

    async def remove_device_token(self, user_id: str, token: str) -> None: ...


class WorkerLeaseStore(Protocol):
    """Simple distributed lease contract used to prevent duplicate ticks."""

    async def try_acquire(
        self,
        *,
        lease_name: str,
        owner_id: str,
        ttl_seconds: int,
        now: datetime,
    ) -> bool: ...


class AlertQuoteProvider(Protocol):
    """Batch quote lookup for alert symbols."""

    async def fetch_latest_quotes(self, grouped_symbols: Mapping[str, set[str]]) -> dict[tuple[str, str], Quote]:
        ...


class PushGateway(Protocol):
    """Push notification sender abstraction."""

    async def send_alert(
        self,
        *,
        tokens: list[str],
        alert: AlertDefinition,
        evaluation: AlertEvaluation,
    ) -> PushDispatchResult: ...


def evaluate_alert(
    alert: AlertDefinition,
    *,
    quote: Quote,
    now: datetime,
) -> AlertEvaluation | None:
    """Evaluate whether an alert fires for the provided quote."""
    if not alert.enabled:
        return None
    if alert.last_triggered_at is not None:
        next_allowed = alert.last_triggered_at + timedelta(seconds=alert.cooldown_seconds)
        if now < next_allowed:
            return None

    observed = _observed_value(alert, quote)
    if observed is None:
        return None

    fired = _threshold_met(alert.kind, observed, alert.threshold)
    if not fired:
        return None

    return AlertEvaluation(
        alert_id=alert.alert_id,
        triggered_at=now,
        observed_value=observed,
        threshold=alert.threshold,
        reason=f"{alert.kind.value} threshold crossed",
    )


def _observed_value(alert: AlertDefinition, quote: Quote) -> Decimal | None:
    if alert.kind in {AlertKind.PRICE_ABOVE, AlertKind.PRICE_BELOW}:
        return quote.price

    if alert.cost_basis is None or alert.quantity is None:
        return None

    pnl_value = (quote.price - alert.cost_basis) * alert.quantity
    if alert.kind in {AlertKind.PNL_ABOVE, AlertKind.PNL_BELOW}:
        return pnl_value

    denominator = alert.cost_basis * alert.quantity
    if denominator == Decimal("0"):
        return None
    return (pnl_value / denominator) * Decimal("100")


def _threshold_met(kind: AlertKind, observed: Decimal, threshold: Decimal) -> bool:
    if kind in {AlertKind.PRICE_ABOVE, AlertKind.PNL_ABOVE, AlertKind.PNL_PCT_ABOVE}:
        return observed >= threshold
    return observed <= threshold


class AdapterQuoteProvider:
    """Collect first quote per requested symbol from source adapters."""

    def __init__(
        self,
        registry: QuoteSourceRegistry,
        *,
        stream_timeout_seconds: float = 3.0,
    ) -> None:
        self._registry = registry
        self._stream_timeout_seconds = stream_timeout_seconds
        self._logger = get_logger(__name__)

    async def fetch_latest_quotes(self, grouped_symbols: Mapping[str, set[str]]) -> dict[tuple[str, str], Quote]:
        tasks = [
            self._fetch_for_source(source=source.lower(), symbols={symbol.upper() for symbol in symbols if symbol})
            for source, symbols in grouped_symbols.items()
            if symbols
        ]
        if not tasks:
            return {}

        results = await asyncio.gather(*tasks, return_exceptions=True)
        merged: dict[tuple[str, str], Quote] = {}
        for result in results:
            if isinstance(result, BaseException):
                self._logger.warning("alert_quote_fetch_source_error", error=str(result))
                continue
            merged.update(result)
        return merged

    async def _fetch_for_source(self, *, source: str, symbols: set[str]) -> dict[tuple[str, str], Quote]:
        adapter = self._registry.for_source(source)
        if adapter is None:
            self._logger.warning("alert_quote_missing_adapter", source=source)
            return {}

        pending = set(symbols)
        found: dict[tuple[str, str], Quote] = {}
        try:
            async with asyncio.timeout(self._stream_timeout_seconds):
                async for quote in adapter.stream_quotes(sorted(pending)):
                    symbol = quote.symbol.upper()
                    if symbol not in pending:
                        continue
                    found[(source, symbol)] = quote
                    pending.remove(symbol)
                    if not pending:
                        break
        except TimeoutError:
            self._logger.warning(
                "alert_quote_fetch_timeout",
                source=source,
                pending_symbols=sorted(pending),
            )
        except Exception as exc:  # noqa: BLE001 - isolate one source failure from the tick
            self._logger.warning(
                "alert_quote_fetch_failed",
                source=source,
                error=str(exc),
            )
        return found


class FirebaseAdminPushGateway:
    """Push gateway backed by Firebase Admin SDK multicast send."""

    def __init__(self, *, messaging_module: Any | None = None) -> None:
        self._messaging_module = messaging_module

    async def send_alert(
        self,
        *,
        tokens: list[str],
        alert: AlertDefinition,
        evaluation: AlertEvaluation,
    ) -> PushDispatchResult:
        if not tokens:
            return PushDispatchResult()

        messaging = self._messaging_module
        if messaging is None:
            from firebase_admin import messaging as fb_messaging

            messaging = fb_messaging

        title = alert.notification_title or f"{alert.symbol} alert triggered"
        body = alert.notification_body or (
            f"{alert.kind.value}: observed {evaluation.observed_value} vs threshold {alert.threshold}"
        )
        data = {
            "alert_id": alert.alert_id,
            "scope": alert.scope,
            "source": alert.source,
            "symbol": alert.symbol,
            "kind": alert.kind.value,
            "deep_link": f"mbp://alerts/{alert.alert_id}?scope={alert.scope}",
        }

        message = messaging.MulticastMessage(
            tokens=tokens,
            notification=messaging.Notification(title=title, body=body),
            data=data,
        )
        batch_response = await asyncio.to_thread(messaging.send_each_for_multicast, message)
        unregistered: list[str] = []
        success_count = 0
        failure_count = 0
        for token, response in zip(tokens, batch_response.responses, strict=False):
            success = bool(getattr(response, "success", False))
            if success:
                success_count += 1
                continue
            failure_count += 1
            exception = getattr(response, "exception", None)
            if exception is not None and _is_unregistered_error(exception):
                unregistered.append(token)

        return PushDispatchResult(
            success_count=success_count,
            failure_count=failure_count,
            unregistered_tokens=unregistered,
        )


def _is_unregistered_error(exception: Exception) -> bool:
    code_attr = str(getattr(exception, "code", "")).lower()
    text = str(exception).lower()
    return "unregistered" in code_attr or "registration-token-not-registered" in text


class AlertEngine:
    """One-tick alert evaluation runner."""

    def __init__(
        self,
        *,
        alerts: AlertRepository,
        quote_provider: AlertQuoteProvider,
        device_tokens: DeviceTokenStore,
        push_gateway: PushGateway,
    ) -> None:
        self._alerts = alerts
        self._quote_provider = quote_provider
        self._device_tokens = device_tokens
        self._push_gateway = push_gateway
        self._logger = get_logger(__name__)

    async def run_once(self, *, now: datetime | None = None) -> int:
        tick_now = now or datetime.now(UTC)
        alerts = await self._alerts.list_active_server_key_alerts()
        grouped = _group_symbols(alerts)
        quotes = await self._quote_provider.fetch_latest_quotes(grouped)

        fired = 0
        for alert in alerts:
            quote = quotes.get((alert.source.lower(), alert.symbol.upper()))
            if quote is None:
                continue
            evaluation = evaluate_alert(alert, quote=quote, now=tick_now)
            if evaluation is None:
                continue

            event = AlertTriggerEvent(
                alert_id=alert.alert_id,
                user_id=alert.user_id,
                source=alert.source,
                symbol=alert.symbol,
                kind=alert.kind,
                triggered_at=evaluation.triggered_at,
                observed_value=evaluation.observed_value,
                threshold=evaluation.threshold,
                scope=alert.scope,
                reason=evaluation.reason,
            )
            await self._alerts.record_trigger_event(event)
            await self._alerts.mark_alert_triggered(alert.alert_id, evaluation.triggered_at)
            await self._dispatch(alert=alert, evaluation=evaluation)
            fired += 1

        self._logger.info("alert_tick_complete", fired=fired, alerts_loaded=len(alerts))
        return fired

    async def _dispatch(self, *, alert: AlertDefinition, evaluation: AlertEvaluation) -> None:
        tokens = await self._device_tokens.list_device_tokens(alert.user_id)
        if not tokens:
            return
        result = await self._push_gateway.send_alert(tokens=tokens, alert=alert, evaluation=evaluation)
        for token in result.unregistered_tokens:
            await self._device_tokens.remove_device_token(alert.user_id, token)


def _group_symbols(alerts: list[AlertDefinition]) -> dict[str, set[str]]:
    grouped: defaultdict[str, set[str]] = defaultdict(set)
    for alert in alerts:
        grouped[alert.source.lower()].add(alert.symbol.upper())
    return dict(grouped)


class InMemoryAlertRepository:
    """Test/local in-memory alert persistence implementation."""

    def __init__(self, alerts: list[AlertDefinition] | None = None) -> None:
        self._alerts: dict[str, AlertDefinition] = {alert.alert_id: alert for alert in alerts or []}
        self.events: list[AlertTriggerEvent] = []

    async def list_active_server_key_alerts(self) -> list[AlertDefinition]:
        return [
            alert
            for alert in self._alerts.values()
            if alert.enabled and alert.server_key_enabled
        ]

    async def record_trigger_event(self, event: AlertTriggerEvent) -> None:
        self.events.append(event)

    async def mark_alert_triggered(self, alert_id: str, triggered_at: datetime) -> None:
        current = self._alerts.get(alert_id)
        if current is None:
            return
        self._alerts[alert_id] = current.model_copy(update={"last_triggered_at": triggered_at})


class InMemoryDeviceTokenStore:
    """Test/local in-memory user token storage."""

    def __init__(self, tokens_by_user: dict[str, list[str]] | None = None) -> None:
        self._tokens_by_user: dict[str, list[str]] = {
            user_id: list(tokens) for user_id, tokens in (tokens_by_user or {}).items()
        }

    async def list_device_tokens(self, user_id: str) -> list[str]:
        return list(self._tokens_by_user.get(user_id, []))

    async def remove_device_token(self, user_id: str, token: str) -> None:
        tokens = self._tokens_by_user.get(user_id)
        if tokens is None:
            return
        self._tokens_by_user[user_id] = [existing for existing in tokens if existing != token]


class InMemoryWorkerLeaseStore:
    """Process-local lease store suitable for deterministic tests."""

    def __init__(self) -> None:
        self._leases: dict[str, tuple[str, datetime]] = {}
        self._lock = asyncio.Lock()

    async def try_acquire(
        self,
        *,
        lease_name: str,
        owner_id: str,
        ttl_seconds: int,
        now: datetime,
    ) -> bool:
        async with self._lock:
            active = self._leases.get(lease_name)
            if active is not None:
                current_owner, expires_at = active
                if expires_at > now and current_owner != owner_id:
                    return False

            self._leases[lease_name] = (
                owner_id,
                now + timedelta(seconds=ttl_seconds),
            )
            return True


class FirestoreAlertRepository:
    """Firestore-backed repository placeholder until integration module lands."""

    async def list_active_server_key_alerts(self) -> list[AlertDefinition]:
        msg = "FirestoreAlertRepository is not wired yet"
        raise NotImplementedError(msg)

    async def record_trigger_event(self, event: AlertTriggerEvent) -> None:
        _ = event
        msg = "FirestoreAlertRepository is not wired yet"
        raise NotImplementedError(msg)

    async def mark_alert_triggered(self, alert_id: str, triggered_at: datetime) -> None:
        _ = (alert_id, triggered_at)
        msg = "FirestoreAlertRepository is not wired yet"
        raise NotImplementedError(msg)


class FirestoreDeviceTokenStore:
    """Firestore-backed token store placeholder until integration module lands."""

    async def list_device_tokens(self, user_id: str) -> list[str]:
        _ = user_id
        msg = "FirestoreDeviceTokenStore is not wired yet"
        raise NotImplementedError(msg)

    async def remove_device_token(self, user_id: str, token: str) -> None:
        _ = (user_id, token)
        msg = "FirestoreDeviceTokenStore is not wired yet"
        raise NotImplementedError(msg)


class FirestoreWorkerLeaseStore:
    """Firestore-backed singleton lease placeholder until integration module lands."""

    async def try_acquire(
        self,
        *,
        lease_name: str,
        owner_id: str,
        ttl_seconds: int,
        now: datetime,
    ) -> bool:
        _ = (lease_name, owner_id, ttl_seconds, now)
        msg = "FirestoreWorkerLeaseStore is not wired yet"
        raise NotImplementedError(msg)


__all__ = [
    "AdapterQuoteProvider",
    "AlertDefinition",
    "AlertEngine",
    "AlertEvaluation",
    "AlertKind",
    "AlertQuoteProvider",
    "AlertRepository",
    "AlertTriggerEvent",
    "DeviceTokenStore",
    "FirebaseAdminPushGateway",
    "FirestoreAlertRepository",
    "FirestoreDeviceTokenStore",
    "FirestoreWorkerLeaseStore",
    "InMemoryAlertRepository",
    "InMemoryDeviceTokenStore",
    "InMemoryWorkerLeaseStore",
    "PushDispatchResult",
    "PushGateway",
    "WorkerLeaseStore",
    "evaluate_alert",
]
