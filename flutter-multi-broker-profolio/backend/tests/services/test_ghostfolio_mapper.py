"""Ghostfolio activity mapping (spec §6.3, §7.1).

§6.3 calls the transfers-are-not-trades rule "the most important rule in
this document" and asks explicitly for tests. Those are `TestTransfers`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.domain import Transaction
from app.services.ghostfolio.client import to_json_number
from app.services.ghostfolio.mapper import (
    ActivityType,
    CryptoSymbolNotVerified,
    DataSource,
    SkipReason,
    external_id_for,
    map_transactions,
    to_ghostfolio_symbol,
)
from app.services.symbols import canonical_crypto, resolve

_ACCOUNTS = {"binance": "acc-binance", "longbridge": "acc-lb", "futu": "acc-futu"}
_WHEN = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)


def _tx(**overrides: object) -> Transaction:
    base: dict[str, object] = {
        "source": "longbridge",
        "transaction_id": "t-1",
        "symbol": "700.HK",
        "side": "buy",
        "quantity": Decimal("100"),
        "price": Decimal("350.5"),
        "currency": "HKD",
        "timestamp": _WHEN,
    }
    base.update(overrides)
    return Transaction(**base)  # type: ignore[arg-type]


class TestTransfers:
    """§6.3 — moving your own coins is a custody change, not a disposal."""

    @pytest.mark.parametrize(
        "side", ["withdrawal", "withdraw", "deposit", "transfer", "transfer_out"]
    )
    def test_transfers_are_never_pushed(self, side: str) -> None:
        mapped, skipped = map_transactions(
            [_tx(source="binance", symbol="BTCUSDT", side=side)],
            account_id_by_source=_ACCOUNTS,
        )
        assert mapped == []
        assert len(skipped) == 1
        assert skipped[0].reason is SkipReason.TRANSFER

    @pytest.mark.parametrize("side", ["withdrawal", "deposit", "transfer"])
    def test_a_transfer_never_becomes_a_buy_or_a_sell(self, side: str) -> None:
        # The failure this guards against is silent: if a withdrawal to the
        # Ledger became a SELL, the cost basis of every coin would be
        # destroyed and every downstream number would be wrong.
        mapped, _ = map_transactions(
            [_tx(source="binance", symbol="BTCUSDT", side=side)],
            account_id_by_source=_ACCOUNTS,
        )
        pushed_types = {activity.payload["type"] for activity in mapped}
        assert ActivityType.BUY.value not in pushed_types
        assert ActivityType.SELL.value not in pushed_types

    def test_a_transfer_is_reported_not_silently_dropped(self) -> None:
        # §6.4: a silent drop looks like reconciliation drift later.
        _, skipped = map_transactions(
            [_tx(source="binance", symbol="BTCUSDT", side="withdrawal")],
            account_id_by_source=_ACCOUNTS,
        )
        assert skipped[0].external_id == "binance:-:t-1"
        assert "withdrawal" in skipped[0].detail

    def test_real_trades_still_pass_through_alongside_transfers(self) -> None:
        mapped, skipped = map_transactions(
            [
                _tx(transaction_id="buy-1", side="buy"),
                _tx(transaction_id="xfer-1", side="withdrawal"),
                _tx(transaction_id="sell-1", side="sell"),
            ],
            account_id_by_source=_ACCOUNTS,
        )
        assert [a.payload["type"] for a in mapped] == ["BUY", "SELL"]
        assert [s.reason for s in skipped] == [SkipReason.TRANSFER]


class TestTradeMapping:
    def test_buy_maps_to_the_expected_payload(self) -> None:
        mapped, skipped = map_transactions([_tx()], account_id_by_source=_ACCOUNTS)
        assert skipped == []
        payload = mapped[0].payload
        assert payload["type"] == "BUY"
        assert payload["symbol"] == "0700.HK"
        assert payload["dataSource"] == DataSource.YAHOO.value
        assert payload["currency"] == "HKD"
        assert payload["quantity"] == Decimal("100")
        assert payload["unitPrice"] == Decimal("350.5")
        assert payload["accountId"] == "acc-lb"

    def test_unit_price_is_derived_from_amount_when_absent(self) -> None:
        mapped, _ = map_transactions(
            [_tx(price=None, amount=Decimal("35050"), quantity=Decimal("100"))],
            account_id_by_source=_ACCOUNTS,
        )
        assert mapped[0].payload["unitPrice"] == Decimal("350.5")

    def test_values_stay_decimal_until_serialisation(self) -> None:
        # §3.2: never float. The conversion happens once, in the client.
        mapped, _ = map_transactions([_tx()], account_id_by_source=_ACCOUNTS)
        assert isinstance(mapped[0].payload["quantity"], Decimal)
        assert isinstance(mapped[0].payload["unitPrice"], Decimal)
        assert isinstance(mapped[0].payload["fee"], Decimal)

    def test_dividends_and_interest_are_recognised(self) -> None:
        mapped, _ = map_transactions(
            [
                _tx(transaction_id="d1", side="dividend"),
                _tx(transaction_id="i1", side="interest", symbol=None),
            ],
            account_id_by_source=_ACCOUNTS,
        )
        assert {a.payload["type"] for a in mapped} == {"DIVIDEND", "INTEREST"}

    def test_unknown_side_is_skipped_loudly(self) -> None:
        _, skipped = map_transactions(
            [_tx(side="rehypothecate")], account_id_by_source=_ACCOUNTS
        )
        assert skipped[0].reason is SkipReason.UNKNOWN_SIDE

    def test_zero_quantity_trade_is_skipped(self) -> None:
        _, skipped = map_transactions(
            [_tx(quantity=Decimal("0"))], account_id_by_source=_ACCOUNTS
        )
        assert skipped[0].reason is SkipReason.NO_QUANTITY


class TestCryptoSymbolsAreVerifiedNotGuessed:
    """§7.1 — 'VERIFY, DO NOT GUESS'."""

    def test_unmapped_crypto_raises_rather_than_inventing_a_symbol(self) -> None:
        with pytest.raises(CryptoSymbolNotVerified, match="Do not guess"):
            to_ghostfolio_symbol(canonical_crypto("BTC"))

    def test_a_verified_override_is_used_verbatim(self) -> None:
        symbol, source = to_ghostfolio_symbol(
            canonical_crypto("BTC"), crypto_overrides={"BTC": "bitcoin"}
        )
        assert (symbol, source) == ("bitcoin", DataSource.COINGECKO)

    def test_an_override_may_carry_its_own_data_source(self) -> None:
        symbol, source = to_ghostfolio_symbol(
            canonical_crypto("BTC"), crypto_overrides={"BTC": "YAHOO:BTC-USD"}
        )
        assert (symbol, source) == ("BTC-USD", DataSource.YAHOO)

    def test_unverified_crypto_is_skipped_not_pushed_wrong(self) -> None:
        mapped, skipped = map_transactions(
            [_tx(source="binance", symbol="BTCUSDT", currency="USDT")],
            account_id_by_source=_ACCOUNTS,
        )
        assert mapped == []
        assert skipped[0].reason is SkipReason.UNRESOLVED_SYMBOL


class TestEquitySymbolMapping:
    @pytest.mark.parametrize(
        ("canonical_input", "exchange", "expected"),
        [
            ("700.HK", None, "0700.HK"),
            ("HK.09988", None, "9988.HK"),
            ("VOO", "NASDAQ", "VOO"),
            ("600519", "SSE", "600519.SS"),
        ],
    )
    def test_maps_out_to_yahoo_convention(
        self, canonical_input: str, exchange: str | None, expected: str
    ) -> None:
        canonical = resolve(canonical_input, exchange=exchange)
        symbol, source = to_ghostfolio_symbol(canonical)
        assert symbol == expected
        assert source is DataSource.YAHOO

    def test_round_trip_matches_the_specs_table(self) -> None:
        # §6.1: IB `700`, Futu `HK.00700`, LongBridge `700.HK`,
        # Ghostfolio `0700.HK`.
        for raw, exchange in [("700", "SEHK"), ("HK.00700", None), ("700.HK", None)]:
            symbol, _ = to_ghostfolio_symbol(resolve(raw, exchange=exchange))
            assert symbol == "0700.HK"


class TestIdempotency:
    """§3.3 — running the sync three times equals running it once."""

    def test_external_id_is_stable_across_runs(self) -> None:
        assert external_id_for(_tx()) == external_id_for(_tx())

    def test_external_id_distinguishes_sources(self) -> None:
        assert external_id_for(_tx(source="futu")) != external_id_for(_tx(source="binance"))

    def test_external_id_distinguishes_accounts(self) -> None:
        assert external_id_for(_tx(account_id="A")) != external_id_for(_tx(account_id="B"))

    def test_external_id_travels_with_the_activity(self) -> None:
        mapped, _ = map_transactions([_tx()], account_id_by_source=_ACCOUNTS)
        assert mapped[0].external_id == "longbridge:-:t-1"
        assert mapped[0].payload["comment"] == "longbridge:-:t-1"


class TestDecimalSerialisation:
    def test_eight_decimal_crypto_quantity_round_trips_exactly(self) -> None:
        # §3.2's whole concern: crypto carries 8+ decimal places.
        quantity = Decimal("0.00460179")
        assert Decimal(repr(to_json_number(quantity))) == quantity

    def test_ledger_holdings_round_trip(self) -> None:
        for value in ("0.00460179", "0.0847144", "131.864"):
            assert Decimal(repr(to_json_number(Decimal(value)))) == Decimal(value)
