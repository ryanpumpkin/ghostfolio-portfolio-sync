"""The portfolio-boundary cash record (§6.3, §8).

A duplicated deposit does not merely double a number — it silently
improves the return by pretending less was contributed than really was.
That is why this store is keyed and idempotent rather than append-only.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.services.cashflows import CashFlowStore, CashMovement


def _m(ident: str, amount: str, *, source: str = "ibkr",
       kind: str = "deposit", internal: bool = False) -> CashMovement:
    return CashMovement(
        external_id=ident, source=source, when=date(2025, 3, 6),
        amount=Decimal(amount), currency="USD", kind=kind, internal=internal,
    )


class TestStore:
    def test_round_trips_through_the_file(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        CashFlowStore(path).record([_m("a", "100"), _m("b", "-50")])
        reloaded = CashFlowStore(path).all()
        assert [m.external_id for m in reloaded] == ["a", "b"]
        assert reloaded[0].amount == Decimal("100")

    def test_re_recording_the_same_id_does_not_duplicate(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        assert store.record([_m("a", "100")]) == 1
        assert store.record([_m("a", "100")]) == 0
        assert len(store.all()) == 1

    def test_decimal_survives_the_json_round_trip(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        CashFlowStore(path).record([_m("a", "1784.25597281451865")])
        assert CashFlowStore(path).all()[0].amount == Decimal(
            "1784.25597281451865"
        )

    def test_replace_source_drops_movements_the_broker_forgot(
        self, tmp_path
    ) -> None:
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        store.record([_m("a", "100"), _m("b", "200")])
        store.replace_source("ibkr", [_m("a", "100")])
        assert [m.external_id for m in store.all()] == ["a"]

    def test_replace_source_leaves_other_sources_alone(self, tmp_path) -> None:
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        store.record([_m("a", "100"), _m("f1", "50", source="futu")])
        store.replace_source("ibkr", [])
        assert [m.external_id for m in store.all()] == ["f1"]

    def test_internal_transfers_are_kept_but_excluded(self, tmp_path) -> None:
        """A move between your own accounts is real money that is not a
        contribution. Storing it makes the exclusion auditable."""
        path = tmp_path / "cash.json"
        store = CashFlowStore(path)
        store.record([
            _m("a", "100"),
            _m("t", "-500", kind="transfer", internal=True),
        ])
        assert len(store.all()) == 2
        assert [m.external_id for m in store.external()] == ["a"]

    def test_a_corrupt_file_refuses_rather_than_starting_empty(
        self, tmp_path
    ) -> None:
        """Silently forgetting every contribution would inflate the
        return and look like a good day."""
        path = tmp_path / "cash.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="cannot read"):
            CashFlowStore(path)

    def test_a_missing_file_is_simply_empty(self, tmp_path) -> None:
        assert CashFlowStore(tmp_path / "absent.json").all() == []
