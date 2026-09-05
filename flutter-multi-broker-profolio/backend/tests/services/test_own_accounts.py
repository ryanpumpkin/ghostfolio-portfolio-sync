"""Own-accounts registry and transfer resolution (spec §6.3, §4.5)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.domain import Transaction, TransactionType
from app.services.own_accounts import (
    OwnAccountsConfigError,
    OwnAccountsRegistry,
    resolve_transfers,
)

_WHEN = datetime(2026, 3, 1, tzinfo=UTC)
_LEDGER_BTC = "bc1qownaddressownaddressownaddress"


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "own_accounts.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def _movement(**overrides: object) -> Transaction:
    base: dict[str, object] = {
        "source": "binance",
        "transaction_id": "w-1",
        "symbol": "BTCUSDT",
        "side": "withdrawal",
        "quantity": Decimal("0.004"),
        "currency": "USD",
        "timestamp": _WHEN,
    }
    base.update(overrides)
    return Transaction(**base)  # type: ignore[arg-type]


class TestForbiddenMaterialIsRejected:
    """§4.5 — enforced, not merely documented."""

    @pytest.mark.parametrize(
        "key",
        [
            "xpub6CUGRUonZSQ4TWtTMmzXdrXDtypWKiKrhko4egpiMZbpiaQL2jkwSB1icqYh2",
            "zpub6jftahH18ngZxLmXaKw3GSZzZsszmt9WqedkyZdezFtWRFBZqsQH5hyUmb4pC",
            "ypub6Ww3ibxVfGzLrAH1PNcjyAWenMTbbAosGNB6VvmSEgytSER9azLDWCxoJwW7Ke",
            "tpubD6NzVbkrYhZ4XgiXtGrdW5XDAPFCL9h7we1vwNCpn8tGbBcgfVYjXyhWo4E1x",
        ],
    )
    def test_an_xpub_raises(self, tmp_path: Path, key: str) -> None:
        # An xpub derives every address in the account and exposes its
        # entire history. It is a credential, not a config value.
        path = _write(tmp_path, f"addresses:\n  - {key}\n")
        with pytest.raises(OwnAccountsConfigError, match="extended public key"):
            OwnAccountsRegistry.load(path)

    def test_a_seed_phrase_raises(self, tmp_path: Path) -> None:
        seed = " ".join(
            [
                "abandon", "ability", "able", "about", "above", "absent",
                "absorb", "abstract", "absurd", "abuse", "access", "accident",
            ]
        )
        path = _write(tmp_path, f'addresses:\n  - "{seed}"\n')
        with pytest.raises(OwnAccountsConfigError, match="seed phrase"):
            OwnAccountsRegistry.load(path)

    def test_a_seed_phrase_in_a_labelled_entry_also_raises(
        self, tmp_path: Path
    ) -> None:
        seed = " ".join(["zoo"] * 24)
        path = _write(
            tmp_path, f'addresses:\n  - value: "{seed}"\n    label: "backup"\n'
        )
        with pytest.raises(OwnAccountsConfigError, match="seed phrase"):
            OwnAccountsRegistry.load(path)

    def test_a_normal_address_is_not_mistaken_for_a_secret(
        self, tmp_path: Path
    ) -> None:
        # The rejection must not be so eager that it blocks legitimate use.
        path = _write(tmp_path, f"addresses:\n  - {_LEDGER_BTC}\n")
        assert OwnAccountsRegistry.load(path).is_own(_LEDGER_BTC)

    def test_a_short_phrase_is_not_a_seed(self, tmp_path: Path) -> None:
        path = _write(tmp_path, 'account_ids:\n  - "my main brokerage account"\n')
        registry = OwnAccountsRegistry.load(path)
        assert registry.is_own("my main brokerage account")


class TestLoading:
    def test_missing_file_yields_an_empty_registry(self, tmp_path: Path) -> None:
        # Safe default: recognises nothing, so every movement stays a
        # deposit/withdrawal — still never a trade.
        registry = OwnAccountsRegistry.load(tmp_path / "absent.yaml")
        assert registry.is_own(_LEDGER_BTC) is False

    def test_empty_file_yields_an_empty_registry(self, tmp_path: Path) -> None:
        assert OwnAccountsRegistry.load(_write(tmp_path, "")).addresses == frozenset()

    def test_labels_are_kept(self, tmp_path: Path) -> None:
        path = _write(
            tmp_path,
            f'addresses:\n  - value: "{_LEDGER_BTC}"\n    label: "Ledger — BTC"\n',
        )
        assert OwnAccountsRegistry.load(path).label_for(_LEDGER_BTC) == "Ledger — BTC"

    def test_matching_ignores_case(self, tmp_path: Path) -> None:
        # EVM addresses appear both checksummed and lowercased depending on
        # the source; bech32 is case-insensitive by spec. Comparing raw
        # strings would silently miss and turn a transfer back into a
        # withdrawal.
        evm = "0xAbCdEf0123456789AbCdEf0123456789AbCdEf01"
        # Quoted deliberately — see the next test for why.
        path = _write(tmp_path, f'addresses:\n  - "{evm}"\n')
        registry = OwnAccountsRegistry.load(path)
        assert registry.is_own(evm.lower())
        assert registry.is_own(evm.upper())

    def test_unquoted_hex_address_is_caught_with_a_useful_message(
        self, tmp_path: Path
    ) -> None:
        # An EVM address is `0x` plus 40 hex digits, which is also valid
        # YAML for an integer. Unquoted, PyYAML hands back a number and the
        # address is gone before the loader ever sees it. The error has to
        # name the actual fix, or you go hunting in the wrong place.
        path = _write(
            tmp_path,
            "addresses:\n  - 0xAbCdEf0123456789AbCdEf0123456789AbCdEf01\n",
        )
        with pytest.raises(OwnAccountsConfigError, match="quoted"):
            OwnAccountsRegistry.load(path)

    def test_malformed_sections_raise(self, tmp_path: Path) -> None:
        with pytest.raises(OwnAccountsConfigError, match="must be a list"):
            OwnAccountsRegistry.load(_write(tmp_path, "addresses: not-a-list\n"))

    def test_unknown_counterparty_is_not_own(self, tmp_path: Path) -> None:
        path = _write(tmp_path, f"addresses:\n  - {_LEDGER_BTC}\n")
        registry = OwnAccountsRegistry.load(path)
        assert registry.is_own("bc1qsomeoneelse") is False
        assert registry.is_own(None) is False
        assert registry.is_own("") is False

    def test_the_shipped_example_loads(self) -> None:
        example = (
            Path(__file__).resolve().parents[3] / "config" / "own_accounts.example.yaml"
        )
        registry = OwnAccountsRegistry.load(example)
        assert registry.addresses
        assert registry.account_ids


class TestTransferResolution:
    """§6.3 — own account to own account is a custody change."""

    @pytest.fixture
    def registry(self, tmp_path: Path) -> OwnAccountsRegistry:
        return OwnAccountsRegistry.load(
            _write(
                tmp_path,
                f"addresses:\n  - {_LEDGER_BTC}\naccount_ids:\n  - U1234567\n",
            )
        )

    def test_withdrawal_to_own_wallet_becomes_a_transfer(
        self, registry: OwnAccountsRegistry
    ) -> None:
        movement = _movement(counterparty=_LEDGER_BTC)
        assert movement.type is TransactionType.WITHDRAWAL
        assert resolve_transfers([movement], registry)[0].type is TransactionType.TRANSFER

    def test_withdrawal_to_a_stranger_stays_a_withdrawal(
        self, registry: OwnAccountsRegistry
    ) -> None:
        movement = _movement(counterparty="bc1qsomeoneelse")
        assert resolve_transfers([movement], registry)[0].type is TransactionType.WITHDRAWAL

    def test_deposit_from_own_broker_becomes_a_transfer(
        self, registry: OwnAccountsRegistry
    ) -> None:
        movement = _movement(side="deposit", counterparty="U1234567")
        assert resolve_transfers([movement], registry)[0].type is TransactionType.TRANSFER

    def test_a_missing_counterparty_changes_nothing(
        self, registry: OwnAccountsRegistry
    ) -> None:
        movement = _movement()
        assert resolve_transfers([movement], registry)[0].type is TransactionType.WITHDRAWAL

    def test_trades_are_never_touched(self, registry: OwnAccountsRegistry) -> None:
        # The guarantee that matters: this function can only ever move a
        # record between two non-pushable types. It must never create or
        # alter a BUY or SELL, whatever the counterparty says.
        buy = _movement(side="buy", price=Decimal("40000"), counterparty=_LEDGER_BTC)
        sell = _movement(side="sell", price=Decimal("40000"), counterparty=_LEDGER_BTC)
        resolved = resolve_transfers([buy, sell], registry)
        assert [t.type for t in resolved] == [TransactionType.BUY, TransactionType.SELL]

    def test_resolution_never_produces_a_pushable_type(
        self, registry: OwnAccountsRegistry
    ) -> None:
        movements = [
            _movement(side="withdrawal", counterparty=_LEDGER_BTC),
            _movement(side="deposit", counterparty="U1234567"),
            _movement(side="withdrawal", counterparty="stranger"),
        ]
        assert not any(t.is_pushable for t in resolve_transfers(movements, registry))

    def test_an_empty_registry_leaves_everything_alone(self) -> None:
        movements = [_movement(counterparty=_LEDGER_BTC)]
        resolved = resolve_transfers(movements, OwnAccountsRegistry.empty())
        assert resolved[0].type is TransactionType.WITHDRAWAL

    def test_original_records_are_not_mutated(
        self, registry: OwnAccountsRegistry
    ) -> None:
        movement = _movement(counterparty=_LEDGER_BTC)
        resolve_transfers([movement], registry)
        assert movement.type is TransactionType.WITHDRAWAL

    def test_the_raw_side_is_preserved_for_audit(
        self, registry: OwnAccountsRegistry
    ) -> None:
        # The source said "withdrawal"; we reinterpret it but must not
        # rewrite what the source actually reported.
        resolved = resolve_transfers([_movement(counterparty=_LEDGER_BTC)], registry)
        assert resolved[0].side == "withdrawal"
        assert resolved[0].type is TransactionType.TRANSFER
