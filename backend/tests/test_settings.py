"""Tests for the Settings loader."""

from __future__ import annotations

from app.core.settings import Settings, get_settings


def test_defaults() -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.app_name == "mbp-backend"
    # frankfurter, not exchangerate.host: the latter needs an API key since
    # late 2024, and a failed rate lookup contributes 0 rather than
    # erroring — silently erasing every foreign holding from net worth.
    # See tests/services/test_fx_provider_default.py.
    assert s.fx_provider == "frankfurter"
    assert s.cors_origins == ["*"]
    assert s.auth_disabled is False


def test_get_settings_cached() -> None:
    a = get_settings()
    b = get_settings()
    assert a is b
