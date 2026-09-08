"""Env-gated integration tests for the IBKR gateway sidecar."""

from __future__ import annotations

import asyncio
import os

import pytest

from app.adapters.ibkr.adapter import IBKRClient


def _integration_env() -> tuple[str, int, str | None] | None:
    host = os.getenv("IBKR_GATEWAY_HOST")
    port_raw = os.getenv("IBKR_GATEWAY_PORT")
    account_id = os.getenv("IBKR_ACCOUNT_ID")
    if not host or not port_raw:
        return None
    try:
        port = int(port_raw)
    except ValueError:
        pytest.skip("IBKR_GATEWAY_PORT must be an integer")
    return host, port, account_id


@pytest.mark.asyncio
async def test_ibkr_gateway_positions_live_integration() -> None:
    env = _integration_env()
    if env is None:
        pytest.skip("Set IBKR_GATEWAY_HOST and IBKR_GATEWAY_PORT to run integration test")

    host, port, account_id = env
    client = IBKRClient(host=host, port=port, account_id=account_id, connect_timeout=15.0)

    ok = await asyncio.wait_for(client.tickle(), timeout=20.0)
    assert ok is True

    positions = await asyncio.wait_for(client.fetch_positions(), timeout=30.0)
    assert positions, "Expected at least one IBKR position row from live gateway"
