"""Tests for request-scoped adapter factory."""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import pytest

from app.adapters.binance import BinanceAdapter, BinanceHost
from app.adapters.ibkr.adapter import IbkrAdapter
from app.adapters.longbridge.adapter import LongBridgeAdapter
from app.services.adapter_factory import (
    AdapterCredentialError,
    AdapterFactory,
    AdapterUnavailableError,
)

# ---------------------------------------------------------------------------
# Binance
# ---------------------------------------------------------------------------


def test_factory_builds_binance_adapter_default_host() -> None:
    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="binance",
        plaintext_creds=json.dumps({"apiKey": "k", "apiSecret": "s"}),
    )

    assert isinstance(adapter, BinanceAdapter)
    assert adapter.host is BinanceHost.COM


def test_factory_builds_binance_us_when_host_specified() -> None:
    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="binance",
        plaintext_creds=json.dumps({"api_key": "k", "api_secret": "s", "host": "binance.us"}),
    )

    assert isinstance(adapter, BinanceAdapter)
    assert adapter.host is BinanceHost.US


def test_factory_builds_binance_us_when_region_specified() -> None:
    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="binance",
        plaintext_creds=json.dumps({"api_key": "k", "api_secret": "s", "region": "us"}),
    )

    assert isinstance(adapter, BinanceAdapter)
    assert adapter.host is BinanceHost.US


def test_factory_rejects_invalid_credential_json() -> None:
    factory = AdapterFactory()
    with pytest.raises(AdapterCredentialError):
        factory.for_connection(connection_kind="binance", plaintext_creds="nope")


def test_factory_rejects_missing_required_fields() -> None:
    factory = AdapterFactory()
    with pytest.raises(AdapterCredentialError):
        factory.for_connection(
            connection_kind="binance",
            plaintext_creds=json.dumps({"apiKey": "k"}),
        )


def test_factory_rejects_unknown_connection_kind() -> None:
    factory = AdapterFactory()
    with pytest.raises(AdapterUnavailableError):
        factory.for_connection(
            connection_kind="kraken", plaintext_creds=json.dumps({"x": "y"}),
        )


# ---------------------------------------------------------------------------
# LongBridge — patches the real SDK loader so we can build without longbridge
# installed in the test env.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_longbridge_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for `longbridge.openapi` with the three symbols the
    LongbridgeClient constructor looks up."""

    class _Config:
        @classmethod
        def from_app_key(cls, app_key: str, app_secret: str, access_token: str) -> _Config:
            instance = cls()
            instance.app_key = app_key  # type: ignore[attr-defined]
            instance.app_secret = app_secret  # type: ignore[attr-defined]
            instance.access_token = access_token  # type: ignore[attr-defined]
            return instance

    class _QuoteContext:
        def __init__(self, config: Any) -> None:
            self.config = config

    class _TradeContext:
        def __init__(self, config: Any) -> None:
            self.config = config

    parent = types.ModuleType("longbridge")
    openapi = types.ModuleType("longbridge.openapi")
    openapi.Config = _Config  # type: ignore[attr-defined]
    openapi.QuoteContext = _QuoteContext  # type: ignore[attr-defined]
    openapi.TradeContext = _TradeContext  # type: ignore[attr-defined]
    parent.openapi = openapi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "longbridge", parent)
    monkeypatch.setitem(sys.modules, "longbridge.openapi", openapi)


def test_factory_builds_longbridge_adapter(stub_longbridge_sdk: None) -> None:
    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="longbridge",
        plaintext_creds=json.dumps(
            {"appKey": "ak", "appSecret": "as", "accessToken": "at"},
        ),
    )
    assert isinstance(adapter, LongBridgeAdapter)


def test_factory_longbridge_accepts_snake_case_keys(
    stub_longbridge_sdk: None,
) -> None:
    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="longbridge",
        plaintext_creds=json.dumps(
            {"app_key": "ak", "app_secret": "as", "access_token": "at"},
        ),
    )
    assert isinstance(adapter, LongBridgeAdapter)


def test_factory_longbridge_rejects_missing_field(
    stub_longbridge_sdk: None,
) -> None:
    factory = AdapterFactory()
    with pytest.raises(AdapterCredentialError):
        factory.for_connection(
            connection_kind="longbridge",
            plaintext_creds=json.dumps({"appKey": "ak", "appSecret": "as"}),
        )


# ---------------------------------------------------------------------------
# IBKR — no SDK needed because IBKRClient defers ib_insync import until
# `_ensure_ib` is called.
# ---------------------------------------------------------------------------


def test_factory_builds_ibkr_adapter_without_account_id() -> None:
    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="ibkr",
        plaintext_creds=json.dumps({"username": "u", "password": "p"}),
    )
    assert isinstance(adapter, IbkrAdapter)


def test_factory_builds_ibkr_adapter_with_account_id() -> None:
    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="ibkr",
        plaintext_creds=json.dumps({"accountId": "U12345"}),
    )
    assert isinstance(adapter, IbkrAdapter)


# ---------------------------------------------------------------------------
# Futu — patches the futu SDK loader.
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_futu_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the `futu` module exposing only the symbols the
    OpenD client constructor needs (none in this case — constructor just
    calls `_load_futu_sdk()` which imports the module)."""
    module = types.ModuleType("futu")
    module.RET_OK = 0  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "futu", module)


def test_factory_builds_futu_adapter(stub_futu_sdk: None) -> None:
    from app.adapters.futu.adapter import FutuAdapter

    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="futu",
        plaintext_creds=json.dumps({"account": "a", "password": "p"}),
    )
    assert isinstance(adapter, FutuAdapter)


def test_factory_futu_passes_optional_acc_id(stub_futu_sdk: None) -> None:
    from app.adapters.futu.adapter import FutuAdapter

    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="futu",
        plaintext_creds=json.dumps({"accId": "12345", "trdEnv": "REAL"}),
    )
    assert isinstance(adapter, FutuAdapter)


def test_factory_futu_ignores_non_numeric_acc_id(stub_futu_sdk: None) -> None:
    from app.adapters.futu.adapter import FutuAdapter

    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="futu",
        plaintext_creds=json.dumps({"accId": "not-a-number"}),
    )
    assert isinstance(adapter, FutuAdapter)


@pytest.mark.asyncio
async def test_factory_futu_ignores_any_supplied_trade_password(
    stub_futu_sdk: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4.3 rule 2 — a password in the credentials must go nowhere.

    Older Flutter builds still send `tradeUnlockPassword`. The factory
    must accept the connection and simply never use it: reads do not
    require unlock (verified against real OpenD), so an unlocked session
    is not needed and an unlockable one is a liability.
    """
    from app.adapters.futu.client import FutuOpenDClient

    unlocked_with: list[str] = []

    async def _unlock_trade(self: FutuOpenDClient, password: str) -> None:
        unlocked_with.append(password)

    async def _fetch_positions(self: FutuOpenDClient) -> list[dict[str, Any]]:
        return [{"code": "HK.00700", "qty": "1", "currency": "HKD"}]

    monkeypatch.setattr(FutuOpenDClient, "unlock_trade", _unlock_trade, raising=False)
    monkeypatch.setattr(FutuOpenDClient, "fetch_positions", _fetch_positions, raising=False)

    factory = AdapterFactory()
    adapter = factory.for_connection(
        connection_kind="futu",
        plaintext_creds=json.dumps({"tradeUnlockPassword": "unlock-123"}),
    )

    rows = await adapter.list_positions()
    assert rows[0].symbol == "HK.00700"
    assert unlocked_with == [], "factory wired a trade password into the adapter"
