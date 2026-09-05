"""Canonical symbol table (spec §6.1)."""

from __future__ import annotations

import pytest

from app.services.symbols import (
    AssetKind,
    SymbolResolutionError,
    Venue,
    canonical_cash,
    canonical_crypto,
    resolve,
    split_crypto_pair,
    venue_for_exchange,
)


class TestTheSpecsOwnExample:
    """§6.1's table: the same instrument, four different identifiers."""

    @pytest.mark.parametrize(
        ("symbol", "exchange"),
        [
            ("700", "SEHK"),      # IB
            ("HK.00700", None),   # Futu
            ("700.HK", None),     # LongBridge
            ("0700", "HKEX"),     # Ghostfolio's form, read back
            ("00700", "SEHK"),    # HKEX official
        ],
    )
    def test_every_source_form_of_tencent_collapses_to_one_symbol(
        self, symbol: str, exchange: str | None
    ) -> None:
        assert resolve(symbol, exchange=exchange).canonical_id == "HK:00700"

    def test_the_forms_compare_equal_not_merely_similar(self) -> None:
        # If these were unequal, one holding would appear as several and
        # reconciliation (§6.4) would report drift that does not exist.
        ib = resolve("700", exchange="SEHK")
        futu = resolve("HK.00700")
        longbridge = resolve("700.HK")
        assert ib == futu == longbridge


class TestHongKongPadding:
    def test_padding_is_lossless_across_widths(self) -> None:
        assert resolve("9988.HK").canonical_id == "HK:09988"
        assert resolve("HK.09988").canonical_id == "HK:09988"
        assert resolve("9988", exchange="SEHK").canonical_id == "HK:09988"

    def test_non_numeric_hk_codes_are_left_alone(self) -> None:
        # Warrants and structured products are not 5-digit board codes;
        # padding them would invent an instrument.
        assert resolve("HK.MSFT").code == "MSFT"


class TestCrypto:
    @pytest.mark.parametrize(
        ("pair", "expected"),
        [
            ("BTCUSDT", ("BTC", "USDT")),
            ("ETHUSDC", ("ETH", "USDC")),
            ("DOGEUSDT", ("DOGE", "USDT")),
            ("ETHBTC", ("ETH", "BTC")),
            ("BNBBTC", ("BNB", "BTC")),
        ],
    )
    def test_splits_concatenated_pairs(self, pair: str, expected: tuple[str, str]) -> None:
        assert split_crypto_pair(pair) == expected

    def test_longest_quote_asset_wins(self) -> None:
        # Regression: a greedy base quantifier backtracks from the longest
        # base, so the *shortest* quote matched first and `ETHFDUSD` split
        # as ("ETHFD", "USD") — which then failed the known-crypto check
        # and dropped the trade entirely. A missed trade is a permanently
        # wrong cost basis (§5.5).
        assert split_crypto_pair("ETHFDUSD") == ("ETH", "FDUSD")
        assert split_crypto_pair("BTCFDUSD") == ("BTC", "FDUSD")

    def test_pair_resolves_to_the_base_asset_only(self) -> None:
        # §6.2: one activity in the quote currency, not two legs.
        assert resolve("BTCUSDT").canonical_id == "CRYPTO:BTC"

    def test_quote_asset_is_retained_for_later(self) -> None:
        # §6.2 says to store it so the two-legged view can be derived.
        assert resolve("BTCUSDT").meta["quote_asset"] == "USDT"

    def test_crypto_is_venue_independent(self) -> None:
        # §4.5/§6.3: BTC on Binance and BTC on Futu are one asset with one
        # cost basis. Encoding the venue would split the holding.
        from_binance = resolve("BTCUSDT")
        from_futu = canonical_crypto("BTC")
        assert from_binance == from_futu

    def test_bare_coin_ticker_is_recognised(self) -> None:
        assert resolve("DOGE").kind is AssetKind.CRYPTO


class TestRefusalToGuess:
    def test_bare_ticker_without_exchange_raises(self) -> None:
        # Guessing a venue would silently file a holding under the wrong
        # market. §6.1 wants aliases added here, not inferred at call sites.
        with pytest.raises(SymbolResolutionError, match="no venue"):
            resolve("AAPL")

    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(SymbolResolutionError):
            resolve("   ")

    def test_unknown_exchange_returns_none(self) -> None:
        assert venue_for_exchange("MOONBASE") is None


class TestCash:
    def test_cash_is_its_own_venue(self) -> None:
        cash = canonical_cash("hkd")
        assert cash.canonical_id == "CASH:HKD"
        assert cash.venue is Venue.CASH
        assert cash.kind is AssetKind.CASH

    def test_cash_requires_a_currency(self) -> None:
        with pytest.raises(SymbolResolutionError):
            canonical_cash("")


class TestOtherVenues:
    @pytest.mark.parametrize(
        ("symbol", "exchange", "expected"),
        [
            ("VOO", "NASDAQ", "US:VOO"),
            ("US.VOO", None, "US:VOO"),
            ("PLTR.US", None, "US:PLTR"),
            ("TSLA", "SMART", "US:TSLA"),
            ("600519", "SSE", "SH:600519"),
            ("000001", "SZSE", "SZ:000001"),
        ],
    )
    def test_resolves(self, symbol: str, exchange: str | None, expected: str) -> None:
        assert resolve(symbol, exchange=exchange).canonical_id == expected
