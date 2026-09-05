"""Verified Ghostfolio symbol config (spec §7.1)."""

from __future__ import annotations

from decimal import Decimal
from datetime import UTC, datetime

import pytest

from app.models.domain import Transaction
from app.services.ghostfolio.config import SymbolConfigError, load_crypto_overrides
from app.services.ghostfolio.mapper import DataSource, map_transactions


class TestShippedConfig:
    def test_the_real_config_loads(self) -> None:
        overrides = load_crypto_overrides()
        assert overrides

    @pytest.mark.parametrize("asset", ["BTC", "ETH", "DOGE"])
    def test_covers_every_asset_the_owner_actually_holds(self, asset: str) -> None:
        # §4.5 lists exactly these three on the Ledger. A missing entry
        # means that asset silently stops being pushed.
        assert asset in load_crypto_overrides()

    def test_maps_to_coingecko_ids_not_guessed_tickers(self) -> None:
        overrides = load_crypto_overrides()
        assert overrides["BTC"] == "bitcoin"
        assert overrides["ETH"] == "ethereum"
        assert overrides["DOGE"] == "dogecoin"


class TestConfigDrivesTheMapper:
    def test_crypto_activity_uses_the_verified_symbol(self) -> None:
        tx = Transaction(
            source="binance",
            transaction_id="t-1",
            symbol="BTCUSDT",
            side="buy",
            quantity=Decimal("0.00460179"),
            price=Decimal("41234.56"),
            currency="USD",
            timestamp=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
        )
        mapped, skipped = map_transactions(
            [tx],
            account_id_by_source={"binance": "acc-binance"},
            crypto_overrides=load_crypto_overrides(),
        )
        assert skipped == []
        payload = mapped[0].payload
        assert payload["symbol"] == "bitcoin"
        assert payload["dataSource"] == DataSource.COINGECKO.value
        # The precision that §3.2 exists to protect.
        assert payload["quantity"] == Decimal("0.00460179")

    def test_without_the_config_crypto_is_skipped_not_guessed(self) -> None:
        tx = Transaction(
            source="binance",
            transaction_id="t-1",
            symbol="BTCUSDT",
            side="buy",
            quantity=Decimal("1"),
            price=Decimal("1"),
            currency="USD",
            timestamp=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
        )
        mapped, skipped = map_transactions(
            [tx], account_id_by_source={}, crypto_overrides={}
        )
        assert mapped == []
        assert skipped


class TestMissingConfigFailsLoudly:
    def test_a_missing_file_raises_rather_than_returning_empty(self, tmp_path) -> None:
        # Returning {} would degrade into silently dropping every crypto
        # activity, which looks like the coins were sold.
        with pytest.raises(SymbolConfigError, match="cannot read"):
            load_crypto_overrides(str(tmp_path / "nope.yaml"))

    def test_a_malformed_file_raises(self, tmp_path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("just a string", encoding="utf-8")
        with pytest.raises(SymbolConfigError):
            load_crypto_overrides(str(bad))
