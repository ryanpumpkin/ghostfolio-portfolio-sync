"""Idempotency and resumability ledger (spec §3.3, §3.4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.ghostfolio.ledger import SyncLedger
from app.services.ghostfolio.mapper import MappedActivity


def _activity(external_id: str, source: str = "binance") -> MappedActivity:
    return MappedActivity(
        external_id=external_id, payload={"type": "BUY"}, source=source
    )


@pytest.fixture
def ledger(tmp_path: Path):
    with SyncLedger(tmp_path / "sync.db") as led:
        yield led


class TestIdempotency:
    """§3.3 — 'Running the sync three times must produce exactly the same
    state as running it once.'"""

    def test_three_runs_push_exactly_what_one_run_pushed(
        self, ledger: SyncLedger
    ) -> None:
        source_records = [_activity(f"binance:-:{i}") for i in range(5)]

        pushed_per_run: list[int] = []
        for _ in range(3):
            # Each run re-reads the same source records, as a real sync would.
            to_push = ledger.filter_unpushed(source_records)
            pushed_per_run.append(len(to_push))
            ledger.record_pushed(to_push)

        assert pushed_per_run == [5, 0, 0]
        assert ledger.count() == 5

    def test_filter_removes_only_what_was_recorded(self, ledger: SyncLedger) -> None:
        first_batch = [_activity("a"), _activity("b")]
        ledger.record_pushed(first_batch)

        mixed = [_activity("a"), _activity("b"), _activity("c")]
        assert [a.external_id for a in ledger.filter_unpushed(mixed)] == ["c"]

    def test_recording_twice_is_harmless(self, ledger: SyncLedger) -> None:
        batch = [_activity("a")]
        ledger.record_pushed(batch)
        ledger.record_pushed(batch)
        assert ledger.count() == 1

    def test_empty_batches_are_no_ops(self, ledger: SyncLedger) -> None:
        assert ledger.filter_unpushed([]) == []
        ledger.record_pushed([])
        assert ledger.count() == 0

    def test_large_batches_survive_the_sqlite_parameter_cap(
        self, ledger: SyncLedger
    ) -> None:
        # SQLite caps host parameters at 999 on older builds. The one-off
        # Binance crawl (§5) pushes far more than that in one go, so the
        # lookup chunks. Without chunking this raises OperationalError.
        many = [_activity(f"binance:-:{i}") for i in range(2500)]
        ledger.record_pushed(many)
        assert ledger.count() == 2500
        assert ledger.filter_unpushed(many) == []


class TestOrderingSafety:
    def test_unrecorded_activities_are_retried(self, ledger: SyncLedger) -> None:
        # The failure this guards: if the ledger were written BEFORE the
        # import, a failed import would mark records done and they would
        # never be retried — silent data loss no later run could detect.
        batch = [_activity("a"), _activity("b")]
        to_push = ledger.filter_unpushed(batch)
        assert len(to_push) == 2
        # Import fails -> record_pushed is never called.
        assert ledger.filter_unpushed(batch) == to_push

    def test_a_partial_run_only_records_what_succeeded(
        self, ledger: SyncLedger
    ) -> None:
        batch = [_activity("a"), _activity("b"), _activity("c")]
        ledger.record_pushed(batch[:2])  # third source failed
        remaining = ledger.filter_unpushed(batch)
        assert [a.external_id for a in remaining] == ["c"]


class TestForget:
    def test_forgetting_lets_an_activity_be_pushed_again(
        self, ledger: SyncLedger
    ) -> None:
        # Needed when rows are deleted in Ghostfolio directly; otherwise
        # the ledger claims they exist forever and they never come back.
        batch = [_activity("a")]
        ledger.record_pushed(batch)
        assert ledger.filter_unpushed(batch) == []

        ledger.forget(["a"])
        assert [a.external_id for a in ledger.filter_unpushed(batch)] == ["a"]

    def test_forgetting_something_unknown_is_harmless(
        self, ledger: SyncLedger
    ) -> None:
        assert ledger.forget(["never-seen"]) == 0
        assert ledger.forget([]) == 0


class TestPerSourceAccounting:
    def test_counts_are_per_source(self, ledger: SyncLedger) -> None:
        ledger.record_pushed(
            [
                _activity("b1", source="binance"),
                _activity("b2", source="binance"),
                _activity("l1", source="longbridge"),
            ]
        )
        assert ledger.count("binance") == 2
        assert ledger.count("longbridge") == 1
        assert ledger.count() == 3

    def test_ghostfolio_ids_are_stored_when_known(self, ledger: SyncLedger) -> None:
        batch = [_activity("a")]
        ledger.record_pushed(batch, ghostfolio_ids={"a": "gf-uuid-1"})
        assert ledger.count() == 1


class TestResumability:
    """§3.4 — 'An aborted run must not restart from zero.'"""

    def test_no_cursor_on_a_first_run(self, ledger: SyncLedger) -> None:
        assert ledger.get_cursor("binance") is None

    def test_cursor_round_trips(self, ledger: SyncLedger) -> None:
        ledger.set_cursor("binance", "fromId=12345")
        assert ledger.get_cursor("binance") == "fromId=12345"

    def test_cursor_updates_in_place(self, ledger: SyncLedger) -> None:
        ledger.set_cursor("binance", "fromId=1")
        ledger.set_cursor("binance", "fromId=2")
        assert ledger.get_cursor("binance") == "fromId=2"
        assert ledger.cursors() == {"binance": "fromId=2"}

    def test_cursors_are_independent_per_source(self, ledger: SyncLedger) -> None:
        ledger.set_cursor("binance", "fromId=1")
        ledger.set_cursor("ibkr", "2026-01-01")
        assert ledger.cursors() == {"binance": "fromId=1", "ibkr": "2026-01-01"}


class TestDurability:
    def test_state_survives_reopening(self, tmp_path: Path) -> None:
        # The point of §3.4: a crashed process resumes, it does not restart.
        path = tmp_path / "sync.db"
        with SyncLedger(path) as first:
            first.record_pushed([_activity("a"), _activity("b")])
            first.set_cursor("binance", "fromId=99")

        with SyncLedger(path) as second:
            assert second.count() == 2
            assert second.get_cursor("binance") == "fromId=99"
            assert second.filter_unpushed([_activity("a")]) == []

    def test_creates_its_parent_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "sync.db"
        with SyncLedger(nested) as led:
            led.record_pushed([_activity("a")])
        assert nested.exists()
