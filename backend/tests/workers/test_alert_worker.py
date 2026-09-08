"""Tests for alert worker trigger evaluation, push dispatch, and singleton lease."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.models.domain import Quote
from app.services.alert_worker import (
    AlertDefinition,
    AlertEngine,
    AlertEvaluation,
    AlertKind,
    FirebaseAdminPushGateway,
    InMemoryAlertRepository,
    InMemoryDeviceTokenStore,
    InMemoryWorkerLeaseStore,
    PushDispatchResult,
    evaluate_alert,
)
from app.workers.alerts import AlertWorker


def _quote(*, source: str = "binance", symbol: str = "BTCUSDT", price: str = "100") -> Quote:
    return Quote(
        source=source,
        symbol=symbol,
        price=Decimal(price),
        currency="USD",
        timestamp=datetime.now(UTC),
    )


def _alert(
    *,
    alert_id: str = "alert-1",
    kind: AlertKind = AlertKind.PRICE_ABOVE,
    threshold: str = "100",
    cooldown_seconds: int = 60,
    last_triggered_at: datetime | None = None,
) -> AlertDefinition:
    return AlertDefinition(
        alert_id=alert_id,
        user_id="user-1",
        source="binance",
        symbol="BTCUSDT",
        kind=kind,
        threshold=Decimal(threshold),
        cooldown_seconds=cooldown_seconds,
        last_triggered_at=last_triggered_at,
    )


def test_trigger_logic_supports_threshold_direction_and_debounce() -> None:
    now = datetime(2026, 5, 18, tzinfo=UTC)

    above = evaluate_alert(
        _alert(kind=AlertKind.PRICE_ABOVE, threshold="100"),
        quote=_quote(price="101"),
        now=now,
    )
    assert isinstance(above, AlertEvaluation)
    assert above.observed_value == Decimal("101")

    below_does_not_fire = evaluate_alert(
        _alert(kind=AlertKind.PRICE_ABOVE, threshold="100"),
        quote=_quote(price="99"),
        now=now,
    )
    assert below_does_not_fire is None

    cooldown_blocked = evaluate_alert(
        _alert(
            kind=AlertKind.PRICE_ABOVE,
            threshold="100",
            cooldown_seconds=120,
            last_triggered_at=now - timedelta(seconds=30),
        ),
        quote=_quote(price="101"),
        now=now,
    )
    assert cooldown_blocked is None

    cooldown_elapsed = evaluate_alert(
        _alert(
            kind=AlertKind.PRICE_ABOVE,
            threshold="100",
            cooldown_seconds=60,
            last_triggered_at=now - timedelta(seconds=120),
        ),
        quote=_quote(price="102"),
        now=now,
    )
    assert isinstance(cooldown_elapsed, AlertEvaluation)

    price_below = evaluate_alert(
        _alert(kind=AlertKind.PRICE_BELOW, threshold="95"),
        quote=_quote(price="94"),
        now=now,
    )
    assert isinstance(price_below, AlertEvaluation)


class _StaticQuoteProvider:
    def __init__(self, quote: Quote) -> None:
        self._quote = quote

    async def fetch_latest_quotes(self, grouped_symbols: dict[str, set[str]]) -> dict[tuple[str, str], Quote]:
        if "binance" not in grouped_symbols:
            return {}
        if "BTCUSDT" not in grouped_symbols["binance"]:
            return {}
        return {("binance", "BTCUSDT"): self._quote}


class _PushWithInvalidToken:
    async def send_alert(
        self,
        *,
        tokens: list[str],
        alert: AlertDefinition,
        evaluation: AlertEvaluation,
    ) -> PushDispatchResult:
        _ = (alert, evaluation)
        return PushDispatchResult(
            success_count=1,
            failure_count=max(0, len(tokens) - 1),
            unregistered_tokens=["token-old"],
        )


@pytest.mark.asyncio
async def test_lease_allows_only_one_replica_to_process_tick() -> None:
    alerts = InMemoryAlertRepository(
        alerts=[
            _alert(alert_id="server-key-alert", kind=AlertKind.PRICE_ABOVE, threshold="100"),
            AlertDefinition(
                alert_id="e2e-alert-skipped",
                user_id="user-1",
                source="binance",
                symbol="BTCUSDT",
                kind=AlertKind.PRICE_ABOVE,
                threshold=Decimal("100"),
                server_key_enabled=False,
            ),
        ]
    )
    devices = InMemoryDeviceTokenStore(tokens_by_user={"user-1": ["token-new", "token-old"]})
    engine = AlertEngine(
        alerts=alerts,
        quote_provider=_StaticQuoteProvider(_quote(price="101")),
        device_tokens=devices,
        push_gateway=_PushWithInvalidToken(),
    )
    lease = InMemoryWorkerLeaseStore()

    worker_a = AlertWorker(
        engine=engine,
        lease_store=lease,
        owner_id="worker-a",
        interval_seconds=60,
        lease_ttl_seconds=30,
    )
    worker_b = AlertWorker(
        engine=engine,
        lease_store=lease,
        owner_id="worker-b",
        interval_seconds=60,
        lease_ttl_seconds=30,
    )

    processed = await asyncio.gather(worker_a.run_single_tick(), worker_b.run_single_tick())

    assert processed.count(True) == 1
    assert processed.count(False) == 1
    assert len(alerts.events) == 1
    remaining_tokens = await devices.list_device_tokens("user-1")
    assert remaining_tokens == ["token-new"]


class _FakeMessaging:
    class Notification:
        def __init__(self, *, title: str, body: str) -> None:
            self.title = title
            self.body = body

    class MulticastMessage:
        def __init__(self, *, tokens: list[str], notification: Any, data: dict[str, str]) -> None:
            self.tokens = tokens
            self.notification = notification
            self.data = data

    class _Resp:
        def __init__(self, *, success: bool, exception: Exception | None = None) -> None:
            self.success = success
            self.exception = exception

    class _Batch:
        def __init__(self, responses: list[Any]) -> None:
            self.responses = responses

    class _UnregisteredError(Exception):
        def __init__(self) -> None:
            super().__init__("registration-token-not-registered")
            self.code = "UNREGISTERED"

    def __init__(self) -> None:
        self.messages: list[Any] = []

    def send_each_for_multicast(self, message: Any) -> Any:
        self.messages.append(message)
        responses = [
            self._Resp(success=True),
            self._Resp(success=False, exception=self._UnregisteredError()),
        ]
        return self._Batch(responses)


@pytest.mark.asyncio
async def test_fcm_dispatch_with_mocked_admin_sdk_reports_unregistered_tokens() -> None:
    fake_messaging = _FakeMessaging()
    gateway = FirebaseAdminPushGateway(messaging_module=fake_messaging)
    alert = _alert(alert_id="alert-42")
    evaluation = AlertEvaluation(
        alert_id=alert.alert_id,
        triggered_at=datetime.now(UTC),
        observed_value=Decimal("110"),
        threshold=Decimal("100"),
        reason="price_above threshold crossed",
    )

    result = await gateway.send_alert(
        tokens=["token-1", "token-old"],
        alert=alert,
        evaluation=evaluation,
    )

    assert result.success_count == 1
    assert result.failure_count == 1
    assert result.unregistered_tokens == ["token-old"]
    assert len(fake_messaging.messages) == 1
    sent = fake_messaging.messages[0]
    assert sent.data["alert_id"] == "alert-42"
    assert sent.data["scope"] == "portfolio"
    assert sent.data["deep_link"] == "mbp://alerts/alert-42?scope=portfolio"
