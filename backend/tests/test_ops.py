"""Tests for /healthz and /metrics."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__


def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["version"] == __version__
    assert "uptime_seconds" in body
    assert isinstance(body["adapters"], list)


def test_metrics_exposition(client: TestClient) -> None:
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    # prometheus_client emits HELP/TYPE comments for every registered metric.
    assert "# HELP" in resp.text or "# TYPE" in resp.text or resp.text == ""
