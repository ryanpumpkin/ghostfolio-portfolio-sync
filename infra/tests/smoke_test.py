"""
infra/tests/smoke_test.py

Pytest-based alternative to the shell smoke test.

Run from repo root after `docker compose up -d && sleep 10`:

    pytest infra/tests/smoke_test.py -v

Environment variables:
    BACKEND_URL  – base URL of the running backend (default: http://localhost:8000)
"""
from __future__ import annotations

import os

import httpx
import pytest

BACKEND_URL = os.environ.get("BACKEND_URL", "http://localhost:8000")


@pytest.fixture(scope="module")
def healthz_response() -> httpx.Response:
    """Fetch /healthz once and share across tests in this module."""
    url = f"{BACKEND_URL}/healthz"
    resp = httpx.get(url, timeout=10.0)
    return resp


def test_healthz_returns_200(healthz_response: httpx.Response) -> None:
    """GET /healthz must return HTTP 200."""
    assert healthz_response.status_code == 200, (
        f"/healthz returned {healthz_response.status_code}: {healthz_response.text}"
    )


def test_healthz_body_status_ok(healthz_response: httpx.Response) -> None:
    """Response JSON must contain status == 'ok'."""
    data = healthz_response.json()
    assert data.get("status") == "ok", f"Unexpected status in /healthz: {data}"


def test_healthz_body_has_version(healthz_response: httpx.Response) -> None:
    """Response JSON must include a version string."""
    data = healthz_response.json()
    assert "version" in data, f"Missing 'version' in /healthz response: {data}"
    assert isinstance(data["version"], str) and data["version"], (
        f"'version' must be a non-empty string, got: {data['version']!r}"
    )


def test_healthz_body_has_uptime(healthz_response: httpx.Response) -> None:
    """Response JSON must include uptime_seconds (numeric, >= 0)."""
    data = healthz_response.json()
    assert "uptime_seconds" in data, f"Missing 'uptime_seconds' in /healthz: {data}"
    assert isinstance(data["uptime_seconds"], (int, float)) and data["uptime_seconds"] >= 0
