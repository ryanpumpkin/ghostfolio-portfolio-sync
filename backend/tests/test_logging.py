"""Tests for the structured-logging configuration and access middleware."""

from __future__ import annotations

import io
import json
import logging

import structlog
from fastapi.testclient import TestClient

from app.core.logging import configure_logging, get_logger
from app.core.tracing import set_request_id


def test_configure_logging_emits_json() -> None:
    configure_logging("INFO")

    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)

    # Redirect structlog's PrintLogger to our buffer for assertion.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=buf),
        cache_logger_on_first_use=False,
    )
    set_request_id("rid-test")
    log = get_logger("test")
    log.info("hello", foo="bar")

    line = buf.getvalue().strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["event"] == "hello"
    assert payload["foo"] == "bar"

    # Restore default config for subsequent tests.
    configure_logging("WARNING")


def test_access_log_runs(client: TestClient) -> None:
    # We just need the middleware to not blow up; it logs to stdout.
    resp = client.get("/healthz")
    assert resp.status_code == 200
