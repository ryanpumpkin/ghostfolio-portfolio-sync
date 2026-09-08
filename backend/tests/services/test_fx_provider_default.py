"""FX provider default and the silent-zero hazard (spec §6.6, §8.3).

The base-currency convention is "current rate" (ARCHITECTURE_NOTES §11),
so every foreign holding depends on a working spot-rate lookup. When that
lookup fails the aggregator contributes 0 rather than erroring, which
keeps one bad currency from blanking the dashboard — but also means a
misconfigured provider silently erases every foreign holding from net
worth, and every allocation percentage is computed against that wrong
total.

These tests pin the two things that stop that being silent.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.core.settings import Settings
from app.models.domain import FxRate
from app.services.aggregator import PortfolioAggregator


class TestProviderDefault:
    def test_default_provider_needs_no_api_key(self) -> None:
        # exchangerate.host retired its free no-key tier in late 2024 and
        # returns missing_access_key without one. ARCHITECTURE_NOTES §5 and
        # RUNBOOK both already said frankfurter; the settings default was
        # the odd one out, and the deployed .env inherited the wrong value
        # from .env.example.
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.fx_provider == "frankfurter"

    def test_default_needs_no_key_configured(self) -> None:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.fx_provider_api_key is None


class TestMissingRateIsNeverSilent:
    def _convert(self, caplog, amount: Decimal, currency: str) -> Decimal:
        with caplog.at_level(logging.WARNING, logger="mbp.aggregator"):
            return PortfolioAggregator._to_base(amount, currency, "HKD", {})

    def test_missing_rate_still_contributes_zero(self, caplog) -> None:
        # Behaviour preserved: one unsupported currency must not blank the
        # whole dashboard (detailed-design §7.2).
        assert self._convert(caplog, Decimal("1000"), "USD") == Decimal("0")

    def test_missing_rate_logs_a_warning(self, caplog) -> None:
        self._convert(caplog, Decimal("1000"), "USD")
        assert any(
            "no FX rate for USD->HKD" in record.getMessage()
            for record in caplog.records
        )

    def test_the_warning_names_the_excluded_amount(self, caplog) -> None:
        # "excluding 1000 USD" is what makes the log actionable — it says
        # how much value just vanished from the total.
        self._convert(caplog, Decimal("1000"), "USD")
        assert any(
            "excluding 1000 USD" in record.getMessage()
            for record in caplog.records
        )

    def test_the_warning_names_the_likely_cause(self, caplog) -> None:
        # The failure is a config mistake, so the log has to point at the
        # config rather than leave you hunting through market data.
        self._convert(caplog, Decimal("1000"), "USD")
        combined = " ".join(r.getMessage() for r in caplog.records)
        assert "MBP_FX_PROVIDER" in combined
        assert "frankfurter" in combined

    def test_a_zero_amount_does_not_warn(self, caplog) -> None:
        # Nothing was lost, so nothing to shout about.
        self._convert(caplog, Decimal("0"), "USD")
        assert not caplog.records

    def test_base_currency_needs_no_rate_and_does_not_warn(self, caplog) -> None:
        with caplog.at_level(logging.WARNING, logger="mbp.aggregator"):
            result = PortfolioAggregator._to_base(
                Decimal("1000"), "HKD", "HKD", {}
            )
        assert result == Decimal("1000")
        assert not caplog.records

    def test_a_present_rate_converts_and_does_not_warn(self, caplog) -> None:
        from datetime import UTC, datetime

        rates = {
            ("USD", "HKD"): FxRate(
                base="USD", quote="HKD", rate=Decimal("7.8403"),
                as_of=datetime.now(UTC),
            )
        }
        with caplog.at_level(logging.WARNING, logger="mbp.aggregator"):
            result = PortfolioAggregator._to_base(
                Decimal("1000"), "USD", "HKD", rates
            )
        assert result == Decimal("7840.3000")
        assert not caplog.records
