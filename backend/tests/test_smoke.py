"""Smoke tests for the FastAPI app boot."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app import __version__


def test_app_boots(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["version"] == __version__
