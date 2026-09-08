"""Ghostfolio activity mapping (spec §6.3, §7.1).

§6.3 calls the transfers-are-not-trades rule "the most important rule in
this document" and asks explicitly for tests. Those are `TestTransfers`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.domain import Transaction, TransactionType
from app.services.ghostfolio.client import to_json_number
from app.services.ghostfolio.mapper import (
    ActivityType,
    CryptoSymbolNotVerifiedError,
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
        with pytest.raises(CryptoSymbolNotVerifiedError, match="Do not guess"):
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


class TestCashSettledIncomeAndCosts:
    """A dividend or fee reported as money, with no share count (§6.1).

    Found live: IBKR's Flex CashTransaction rows carry `amount` and
    nothing else. The mapper dropped all five dividends for having no
    quantity, and recorded six withholding-tax rows with fee=0 — the
    events survived, the money did not.
    """

    def _tx(self, **overrides) -> Transaction:
        base = dict(
            source="ibkr",
            account_id="U1",
            transaction_id="x1",
            symbol="VOO",
            exchange="ARCA",
            currency="USD",
            timestamp=datetime(2026, 6, 30, tzinfo=UTC),
        )
        base.update(overrides)
        return Transaction(**base)

    def test_cash_dividend_survives_without_a_share_count(self) -> None:
        mapped, skipped = map_transactions(
            [self._tx(type=TransactionType.DIVIDEND, amount=Decimal("14.22"))],
            account_id_by_source={"ibkr": "acct"},
        )
        assert not skipped
        payload = mapped[0].payload
        assert payload["type"] == "DIVIDEND"
        # Ghostfolio values an activity as quantity x unitPrice, so the
        # cash total is carried as 1 x amount.
        assert payload["quantity"] == Decimal("1")
        assert payload["unitPrice"] == Decimal("14.22")

    def test_withholding_tax_records_what_it_cost(self) -> None:
        mapped, _ = map_transactions(
            [self._tx(type=TransactionType.FEE, amount=Decimal("-1.42"))],
            account_id_by_source={"ibkr": "acct"},
        )
        payload = mapped[0].payload
        assert payload["type"] == "FEE"
        # The magnitude, in the fee field Ghostfolio actually subtracts.
        assert payload["fee"] == Decimal("1.42")
        assert payload["unitPrice"] == Decimal("0")

    def test_an_explicit_fee_still_wins_over_the_amount(self) -> None:
        # A trade's own commission must not be overwritten by this path.
        mapped, _ = map_transactions(
            [self._tx(
                type=TransactionType.BUY,
                quantity=Decimal("2"),
                price=Decimal("100"),
                amount=Decimal("200"),
                fee=Decimal("0.35"),
                fee_currency="USD",
            )],
            account_id_by_source={"ibkr": "acct"},
        )
        assert mapped[0].payload["fee"] == Decimal("0.35")


class TestFeesNeverCarryATradeableSymbol:
    """A FEE names its currency, never an instrument (§7.1).

    Probed against the running 3.67.0 instance rather than reasoned
    about: importing one FEE with `symbol=SOFI, dataSource=YAHOO` does
    not attach it to SoFi. Ghostfolio treats FEE/INTEREST/LIABILITY as
    non-tradeable and mints a MANUAL asset with a random UUID symbol,
    keeping the string we sent only as its *name*.

    And in the same import batch, a BUY of SOFI is then filed under that
    UUID too — the probe got both back sharing symbol `886aa1a9-…`,
    differing only in dataSource. That is how VOO came to be split across
    three instruments holding 3.9538, 1.5835 and 2.6742 shares of the
    same ETF, and how a ghost "The Coca-Cola Company" held +10 shares
    against a -10 in its twin.

    The fee amount is not lost by booking it against the currency:
    Ghostfolio subtracts `fee` wherever the activity sits. Only the
    attribution to the instrument is, and Ghostfolio has nowhere to put
    that.
    """

    def _tx(self, **overrides) -> Transaction:
        base = dict(
            source="ibkr", account_id="U1", transaction_id="t1",
            currency="USD", timestamp=datetime(2026, 6, 30, tzinfo=UTC),
        )
        base.update(overrides)
        return Transaction(**base)

    def test_withholding_tax_does_not_claim_the_instrument(self) -> None:
        mapped, skipped = map_transactions(
            [self._tx(
                type=TransactionType.FEE, symbol="VOO", exchange="ARCA",
                amount=Decimal("-3.00"),
            )],
            account_id_by_source={"ibkr": "acct"},
        )
        assert not skipped
        payload = mapped[0].payload
        # The source said this tax was on VOO. Sending VOO anyway would
        # not file it under VOO — it would drag VOO's own trades onto a
        # UUID asset in the same batch.
        assert payload["symbol"] == "GF_USD"
        assert payload["dataSource"] == DataSource.MANUAL.value
        # The money still counts.
        assert payload["fee"] == Decimal("3.00")

    def test_an_account_level_fee_still_uses_the_currency(self) -> None:
        # A charge with no instrument is the only case a placeholder fits.
        mapped, _ = map_transactions(
            [self._tx(type=TransactionType.FEE, amount=Decimal("-5"))],
            account_id_by_source={"ibkr": "acct"},
        )
        payload = mapped[0].payload
        # GF_-prefixed because 3.67 rejects any other MANUAL symbol that
        # is not a UUID — and a UUID would be a new asset every sync.
        assert payload["symbol"] == "GF_USD"
        assert payload["dataSource"] == DataSource.MANUAL.value

    def test_a_fee_needs_no_quantity(self) -> None:
        # Only BUY/SELL/DIVIDEND require one; a FEE with none must not be
        # dropped for it.
        mapped, skipped = map_transactions(
            [self._tx(
                type=TransactionType.FEE, symbol="VOO", exchange="ARCA",
                amount=Decimal("-1.42"),
            )],
            account_id_by_source={"ibkr": "acct"},
        )
        assert not skipped
        assert mapped[0].payload["quantity"] == Decimal("0")

    def test_an_unresolvable_fee_symbol_costs_nothing(self) -> None:
        """A fee is never dropped for a symbol it was never going to use.

        A BUY of an unrecognised ticker must be skipped — putting it in
        at a guessed identity is a permanently wrong position. A fee has
        no position to get wrong, and its symbol is discarded either way,
        so refusing it would lose a real cost over an irrelevance.
        """
        mapped, skipped = map_transactions(
            [self._tx(type=TransactionType.FEE, symbol="MYSTERY",
                      amount=Decimal("-1"))],
            account_id_by_source={"ibkr": "acct"},
        )
        assert not skipped
        assert mapped[0].payload["symbol"] == "GF_USD"
        assert mapped[0].payload["fee"] == Decimal("1")


class TestStablecoinCurrency:
    """A USDT-quoted trade must reach Ghostfolio as USD.

    Ghostfolio validates an activity's currency against ISO-4217 and
    rejects the ENTIRE import otherwise. Live, all five Binance buys came
    back "activities.N.currency must be a valid ISO4217 currency code"
    because the pair was BTCUSDT — losing the whole cost basis over a
    currency code.
    """

    def test_usdt_becomes_usd(self) -> None:
        from app.services.ghostfolio.mapper import activity_currency

        assert activity_currency("USDT") == "USD"

    def test_other_stablecoins_too(self) -> None:
        from app.services.ghostfolio.mapper import activity_currency

        for code in ("USDC", "BUSD", "FDUSD", "TUSD", "DAI"):
            assert activity_currency(code) == "USD"

    def test_real_currencies_are_untouched(self) -> None:
        from app.services.ghostfolio.mapper import activity_currency

        for code in ("USD", "HKD", "JPY", "EUR"):
            assert activity_currency(code) == code

    def test_missing_currency_still_defaults_to_usd(self) -> None:
        from app.services.ghostfolio.mapper import activity_currency

        assert activity_currency(None) == "USD"

    def test_an_unknown_code_is_passed_through_not_invented(self) -> None:
        # Ghostfolio will reject it and say so, which is the right
        # outcome — better than silently relabelling it as dollars.
        from app.services.ghostfolio.mapper import activity_currency

        assert activity_currency("XYZ") == "XYZ"


class TestNonFiatCurrencyIsRefused:
    """One bad currency must not take a good batch down with it.

    Ghostfolio validates currency against ISO-4217 and answers 400 for
    the WHOLE import. Live: four withdrawal network fees denominated in
    BTC, ETH and DOGE failed a batch that also carried five valid buys.
    """

    def test_crypto_currency_is_not_fiat(self) -> None:
        from app.services.symbols import is_fiat_currency

        for code in ("BTC", "ETH", "DOGE", "USDT"):
            assert not is_fiat_currency(code)

    def test_real_currencies_are_fiat(self) -> None:
        from app.services.symbols import is_fiat_currency

        for code in ("USD", "HKD", "JPY", "hkd"):
            assert is_fiat_currency(code)

    def test_a_coin_denominated_fee_is_skipped_not_sent(self) -> None:
        from app.services.ghostfolio.mapper import map_transactions

        fee = Transaction(
            source="binance", transaction_id="w1",
            symbol=None, side=None, type=TransactionType.FEE,
            amount=Decimal("0.00003"), currency="BTC",
            timestamp=datetime(2025, 3, 6, tzinfo=UTC),
        )
        mapped, skipped = map_transactions(
            [fee], account_id_by_source={"binance": "a1"}
        )
        assert mapped == []
        assert skipped[0].reason is SkipReason.NON_FIAT_CURRENCY

    def test_a_usdt_trade_survives_the_same_check(self) -> None:
        """USDT maps to USD before the check, so it must NOT be refused."""
        from app.services.ghostfolio.mapper import map_transactions

        buy = Transaction(
            source="binance", transaction_id="t1",
            symbol="BTCUSDT", side="buy", type=TransactionType.BUY,
            quantity=Decimal("0.00078"), price=Decimal("95000"),
            currency="USDT", timestamp=datetime(2024, 12, 10, tzinfo=UTC),
        )
        mapped, skipped = map_transactions(
            [buy], account_id_by_source={"binance": "a1"},
            crypto_overrides={"BTC": "bitcoin"},
        )
        assert skipped == []
        assert mapped[0].payload["currency"] == "USD"
