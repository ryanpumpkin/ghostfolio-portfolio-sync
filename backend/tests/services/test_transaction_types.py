"""Transaction classification and the transfer/swap distinction.

§6.3 calls transfers-are-not-trades "the most important rule in this
document". §6.3b then says the *opposite* case — a swap — is "the single
easiest place to corrupt cost basis" and asks for a test. Both are here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.domain import (
    NON_PUSHABLE_TYPES,
    Position,
    Transaction,
    TransactionType,
    classify_side,
)
from app.services.ghostfolio.mapper import SkipReason, map_transactions

_WHEN = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
_ACCOUNTS = {"binance": "acc-b", "longbridge": "acc-lb", "ledger": "acc-ledger"}


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


class TestSideDerivation:
    @pytest.mark.parametrize(
        ("side", "expected"),
        [
            ("buy", TransactionType.BUY),
            ("BUY", TransactionType.BUY),
            ("  Sold ", TransactionType.SELL),
            ("dividend", TransactionType.DIVIDEND),
            ("commission", TransactionType.FEE),
            ("withdrawal", TransactionType.WITHDRAWAL),
            ("transfer_out", TransactionType.TRANSFER),
        ],
    )
    def test_maps_known_sides(self, side: str, expected: TransactionType) -> None:
        assert classify_side(side) is expected

    def test_unknown_side_returns_none_rather_than_guessing(self) -> None:
        # Inventing a type here is exactly how a transfer becomes a sale.
        assert classify_side("rehypothecate") is None
        assert classify_side(None) is None

    def test_type_is_derived_automatically_from_side(self) -> None:
        # All four existing adapters set only `side`; they must keep working.
        assert _tx(side="buy").type is TransactionType.BUY

    def test_explicit_type_wins_over_side(self) -> None:
        # The case that matters: Binance reports side="withdrawal", but if
        # the counterparty is the owner's own Ledger address it is a
        # TRANSFER, not a WITHDRAWAL (§6.3). The adapter must be able to
        # say so without lying about the raw `side` the source emitted.
        tx = _tx(
            source="binance",
            side="withdrawal",
            type=TransactionType.TRANSFER,
            counterparty="bc1qownaddress",
        )
        assert tx.side == "withdrawal"
        assert tx.type is TransactionType.TRANSFER


class TestPushability:
    @pytest.mark.parametrize(
        "tx_type",
        [TransactionType.TRANSFER, TransactionType.DEPOSIT, TransactionType.WITHDRAWAL],
    )
    def test_custody_and_cash_movements_are_never_pushed(
        self, tx_type: TransactionType
    ) -> None:
        assert tx_type in NON_PUSHABLE_TYPES
        assert _tx(type=tx_type, side=None).is_pushable is False

    @pytest.mark.parametrize(
        "tx_type",
        [
            TransactionType.BUY,
            TransactionType.SELL,
            TransactionType.DIVIDEND,
            TransactionType.INTEREST,
            TransactionType.FEE,
        ],
    )
    def test_real_activity_is_pushed(self, tx_type: TransactionType) -> None:
        assert _tx(type=tx_type, side=None).is_pushable is True


class TestSwapsAreNotTransfers:
    """§6.3b — the naive reading ('the coins never left my custody, so
    nothing happened') is wrong. A swap realises a gain."""

    def _swap_legs(self) -> list[Transaction]:
        # DOGE -> BTC via a wallet swap. Both legs, same timestamp, linked.
        return [
            Transaction(
                source="ledger", transaction_id="swap-1-out", symbol="DOGE",
                side="sell", quantity=Decimal("131.864"), price=Decimal("0.09"),
                currency="USD", timestamp=_WHEN, correlation_id="swap-1",
            ),
            Transaction(
                source="ledger", transaction_id="swap-1-in", symbol="BTC",
                side="buy", quantity=Decimal("0.00014"), price=Decimal("79771"),
                currency="USD", timestamp=_WHEN, correlation_id="swap-1",
            ),
        ]

    def test_both_legs_are_pushed(self) -> None:
        # Unlike a TRANSFER, neither leg is dropped.
        legs = self._swap_legs()
        assert all(leg.is_pushable for leg in legs)

    def test_legs_are_a_sell_and_a_buy(self) -> None:
        types = {leg.type for leg in self._swap_legs()}
        assert types == {TransactionType.SELL, TransactionType.BUY}

    def test_legs_share_a_correlation_id_and_timestamp(self) -> None:
        legs = self._swap_legs()
        assert len({leg.correlation_id for leg in legs}) == 1
        assert len({leg.timestamp for leg in legs}) == 1

    def test_a_swap_reaches_ghostfolio_as_two_activities(self) -> None:
        mapped, skipped = map_transactions(
            self._swap_legs(),
            account_id_by_source={"ledger": "acc-ledger"},
            crypto_overrides={"DOGE": "dogecoin", "BTC": "bitcoin"},
        )
        assert skipped == []
        assert sorted(a.payload["type"] for a in mapped) == ["BUY", "SELL"]

    def test_a_transfer_of_the_same_asset_is_still_dropped(self) -> None:
        # The contrast that makes the rule concrete: same coins, same
        # wallet, but no disposal — so nothing is pushed.
        transfer = Transaction(
            source="binance", transaction_id="xfer-1", symbol="BTCUSDT",
            side="withdrawal", type=TransactionType.TRANSFER,
            quantity=Decimal("0.004"), currency="USD", timestamp=_WHEN,
        )
        mapped, skipped = map_transactions(
            [transfer], account_id_by_source=_ACCOUNTS,
            crypto_overrides={"BTC": "bitcoin"},
        )
        assert mapped == []
        assert skipped[0].reason is SkipReason.TRANSFER


class TestFees:
    def test_a_fee_in_the_trade_currency_is_carried_through(self) -> None:
        mapped, _ = map_transactions(
            [_tx(fee=Decimal("12.50"), fee_currency="HKD")],
            account_id_by_source=_ACCOUNTS,
        )
        assert mapped[0].payload["fee"] == Decimal("12.50")

    def test_a_fee_with_no_currency_stated_is_trusted(self) -> None:
        mapped, _ = map_transactions(
            [_tx(fee=Decimal("12.50"))], account_id_by_source=_ACCOUNTS
        )
        assert mapped[0].payload["fee"] == Decimal("12.50")

    def test_an_unconverted_bnb_fee_is_dropped_not_mis_stated(self) -> None:
        # §5.6: Binance often charges commission in BNB. Ghostfolio has one
        # fee number and no fee currency, so sending 0.01 BNB as though it
        # were 0.01 USD would silently mis-state cost basis.
        mapped, _ = map_transactions(
            [_tx(source="binance", currency="USDT", symbol="BTCUSDT",
                 fee=Decimal("0.01"), fee_currency="BNB")],
            account_id_by_source=_ACCOUNTS,
            crypto_overrides={"BTC": "bitcoin"},
        )
        assert mapped[0].payload["fee"] == Decimal("0")

    def test_the_trade_survives_a_dropped_fee(self) -> None:
        # Losing the trade would be a permanently wrong position (§5.5);
        # losing the fee is small and shows up in reconciliation (§6.4).
        mapped, skipped = map_transactions(
            [_tx(source="binance", currency="USDT", symbol="BTCUSDT",
                 quantity=Decimal("0.5"), price=Decimal("80000"),
                 fee=Decimal("0.01"), fee_currency="BNB")],
            account_id_by_source=_ACCOUNTS,
            crypto_overrides={"BTC": "bitcoin"},
        )
        assert skipped == []
        assert mapped[0].payload["quantity"] == Decimal("0.5")

    def test_no_fee_reported_means_zero_not_a_guess(self) -> None:
        mapped, _ = map_transactions([_tx()], account_id_by_source=_ACCOUNTS)
        assert mapped[0].payload["fee"] == Decimal("0")


class TestExternalId:
    def test_an_adapter_supplied_external_id_is_preferred(self) -> None:
        mapped, _ = map_transactions(
            [_tx(external_id="lb:custom:abc")], account_id_by_source=_ACCOUNTS
        )
        assert mapped[0].external_id == "lb:custom:abc"
        assert mapped[0].payload["comment"] == "lb:custom:abc"

    def test_it_is_derived_when_absent(self) -> None:
        mapped, _ = map_transactions([_tx()], account_id_by_source=_ACCOUNTS)
        assert mapped[0].external_id == "longbridge:-:t-1"


class TestCustodyLocation:
    """§4.4 — the same asset held in two places must not be summed blindly."""

    def test_custody_defaults_to_the_reporting_source(self) -> None:
        position = Position(
            source="futu", symbol="BTC", quantity=Decimal("0.001"), currency="USD"
        )
        assert position.custody_location == "futu"

    def test_custody_can_differ_from_source(self) -> None:
        # A Ledger holding entered manually: reported by "manual", held on
        # "ledger".
        position = Position(
            source="manual", custody="ledger", symbol="BTC",
            quantity=Decimal("0.00460179"), currency="USD",
        )
        assert position.source == "manual"
        assert position.custody_location == "ledger"

    def test_the_same_asset_in_two_places_stays_distinguishable(self) -> None:
        # The §4.4 scenario: a sub-minimum balance waiting at Futu to reach
        # the withdrawal threshold, plus the self-custody balance. Summing
        # these before reconciliation would report drift that is not real.
        at_futu = Position(source="futu", custody="futu", symbol="BTC",
                           quantity=Decimal("0.002"), currency="USD")
        on_ledger = Position(source="manual", custody="ledger", symbol="BTC",
                             quantity=Decimal("0.00460179"), currency="USD")
        by_location = {p.custody_location: p.quantity for p in (at_futu, on_ledger)}
        assert by_location == {
            "futu": Decimal("0.002"),
            "ledger": Decimal("0.00460179"),
        }
        assert sum(by_location.values()) == Decimal("0.00660179")


class TestBackwardCompatibility:
    def test_a_transaction_built_the_old_way_still_works(self) -> None:
        # Every one of the four adapters constructs Transactions without
        # any of the new fields. None of them may break.
        tx = Transaction(
            source="futu", transaction_id="x", symbol="HK.00700", side="buy",
            quantity=Decimal("100"), price=Decimal("350"), currency="HKD",
            timestamp=_WHEN,
        )
        assert tx.type is TransactionType.BUY
        assert tx.fee is None
        assert tx.correlation_id is None

    def test_a_position_built_the_old_way_still_works(self) -> None:
        position = Position(
            source="ibkr", symbol="VOO", quantity=Decimal("10"), currency="USD"
        )
        assert position.custody is None
        assert position.custody_location == "ibkr"
