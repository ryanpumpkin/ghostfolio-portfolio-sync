"""Ghostfolio sync orchestration (spec §3.3, §6.3, §6.4)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.adapters._common import PermanentError, TransientError
from app.models.domain import Transaction, TransactionType
from app.services.ghostfolio.ledger import SyncLedger
from app.services.ghostfolio.sync import GhostfolioSync

_WHEN = datetime(2026, 3, 1, 9, 30, tzinfo=UTC)
_ACCOUNTS = {"binance": "acc-b", "longbridge": "acc-lb"}
_OVERRIDES = {"BTC": "bitcoin", "ETH": "ethereum"}


class _FakeClient:
    """Records what it was asked to import; can be told to fail."""

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self._fail_with = fail_with

    async def import_activities(
        self, activities: list[dict[str, Any]]
    ) -> dict[str, Any]:
        if self._fail_with is not None:
            raise self._fail_with
        self.batches.append(activities)
        return {"activities": activities}


def _tx(tid: str, source: str = "longbridge", **overrides: object) -> Transaction:
    base: dict[str, object] = {
        "source": source,
        "transaction_id": tid,
        "symbol": "700.HK" if source == "longbridge" else "BTCUSDT",
        "side": "buy",
        "quantity": Decimal("10"),
        "price": Decimal("100"),
        "currency": "HKD" if source == "longbridge" else "USD",
        "timestamp": _WHEN,
    }
    base.update(overrides)
    return Transaction(**base)  # type: ignore[arg-type]


@pytest.fixture
def ledger(tmp_path: Path):
    with SyncLedger(tmp_path / "sync.db") as led:
        yield led


def _sync(client: _FakeClient, ledger: SyncLedger) -> GhostfolioSync:
    return GhostfolioSync(
        client=client,  # type: ignore[arg-type]
        ledger=ledger,
        account_id_by_source=_ACCOUNTS,
        crypto_overrides=_OVERRIDES,
    )


class TestIdempotentRuns:
    async def test_running_three_times_pushes_once(self, ledger: SyncLedger) -> None:
        client = _FakeClient()
        sync = _sync(client, ledger)
        records = [_tx("a"), _tx("b")]

        first = await sync.push(records)
        second = await sync.push(records)
        third = await sync.push(records)

        assert (first.pushed, second.pushed, third.pushed) == (2, 0, 0)
        assert (second.already_pushed, third.already_pushed) == (2, 2)
        assert len(client.batches) == 1

    async def test_new_records_still_get_through(self, ledger: SyncLedger) -> None:
        client = _FakeClient()
        sync = _sync(client, ledger)
        await sync.push([_tx("a")])
        report = await sync.push([_tx("a"), _tx("b")])
        assert report.pushed == 1
        assert report.already_pushed == 1


class TestFailureIsolation:
    async def test_a_failing_source_does_not_lose_the_others(
        self, ledger: SyncLedger
    ) -> None:
        # Ghostfolio rejects an import as a unit, so batching globally
        # would mean one bad LongBridge symbol also loses every Binance
        # trade in the same run.
        class _SelectiveClient(_FakeClient):
            async def import_activities(
                self, activities: list[dict[str, Any]]
            ) -> dict[str, Any]:
                if any(a["symbol"] == "0700.HK" for a in activities):
                    raise PermanentError("400 bad symbol")
                self.batches.append(activities)
                return {"activities": activities}

        client = _SelectiveClient()
        sync = _sync(client, ledger)
        report = await sync.push([_tx("lb-1"), _tx("bin-1", source="binance")])

        assert report.failed_sources == ["longbridge"]
        assert report.pushed == 1
        assert ledger.count("binance") == 1
        assert ledger.count("longbridge") == 0

    async def test_a_failed_push_is_not_recorded_so_it_retries(
        self, ledger: SyncLedger
    ) -> None:
        # The silent-data-loss case: recording before the push would mark
        # these done forever.
        failing = _FakeClient(fail_with=TransientError("503"))
        report = await _sync(failing, ledger).push([_tx("a")])
        assert report.pushed == 0
        assert not report.ok
        assert ledger.count() == 0

        recovered = _FakeClient()
        second = await _sync(recovered, ledger).push([_tx("a")])
        assert second.pushed == 1
        assert second.ok

    async def test_report_is_not_ok_when_any_source_failed(
        self, ledger: SyncLedger
    ) -> None:
        report = await _sync(
            _FakeClient(fail_with=PermanentError("400")), ledger
        ).push([_tx("a")])
        assert report.ok is False


class TestExclusionsAreAccounted:
    async def test_transfers_are_skipped_and_reported(
        self, ledger: SyncLedger
    ) -> None:
        client = _FakeClient()
        report = await _sync(client, ledger).push(
            [
                _tx("buy-1"),
                _tx("xfer-1", source="binance", side="withdrawal"),
            ]
        )
        assert report.pushed == 1
        assert report.skip_counts() == {"transfer_excluded_per_6_3": 1}

    async def test_a_transfer_never_reaches_the_client(
        self, ledger: SyncLedger
    ) -> None:
        client = _FakeClient()
        await _sync(client, ledger).push(
            [_tx("x", source="binance", type=TransactionType.TRANSFER, side=None)]
        )
        assert client.batches == []

    async def test_unresolved_symbols_are_surfaced(self, ledger: SyncLedger) -> None:
        # An unverified crypto symbol is a holding that would silently
        # never appear; §6.4 needs to know about it now, not as a
        # mystery quantity mismatch later.
        sync = GhostfolioSync(
            client=_FakeClient(),  # type: ignore[arg-type]
            ledger=ledger,
            account_id_by_source=_ACCOUNTS,
            crypto_overrides={},  # nothing verified
        )
        report = await sync.push([_tx("b1", source="binance")])
        assert report.pushed == 0
        assert report.skip_counts() == {"symbol_could_not_be_resolved": 1}
        assert sync.unresolved_symbols(report)

    async def test_summary_mentions_skips(self, ledger: SyncLedger) -> None:
        report = await _sync(_FakeClient(), ledger).push(
            [_tx("buy-1"), _tx("x", source="binance", side="withdrawal")]
        )
        assert "skipped=1" in report.summary()
        assert "pushed=1" in report.summary()


class TestBatching:
    async def test_each_source_gets_its_own_batch(self, ledger: SyncLedger) -> None:
        client = _FakeClient()
        await _sync(client, ledger).push(
            [_tx("lb-1"), _tx("lb-2"), _tx("bin-1", source="binance")]
        )
        assert len(client.batches) == 2
        assert sorted(len(b) for b in client.batches) == [1, 2]

    async def test_fees_never_share_a_batch_with_trades(
        self, ledger: SyncLedger
    ) -> None:
        """Ghostfolio files a same-batch BUY under the fee's ghost asset.

        Probed against 3.67.0: a FEE and a BUY of SOFI imported together
        came back sharing symbol `886aa1a9-…`, the UUID Ghostfolio minted
        for the fee. The BUY's real asset profile was untouched. That is
        how VOO ended up split across three instruments.

        The mapper already stops a fee naming a tradeable symbol, so the
        two cannot collide; keeping them in separate requests means the
        corruption cannot come back through some other route.
        """
        client = _FakeClient()
        report = await _sync(client, ledger).push([
            _tx("lb-1"),
            _tx("lb-fee", type=TransactionType.FEE, side=None,
                quantity=None, price=None, amount=Decimal("-2.50")),
        ])
        assert report.pushed == 2
        assert len(client.batches) == 2
        types = [sorted({str(a["type"]) for a in batch}) for batch in client.batches]
        assert types == [["BUY"], ["FEE"]]

    async def test_nothing_to_push_calls_nothing(self, ledger: SyncLedger) -> None:
        client = _FakeClient()
        report = await _sync(client, ledger).push([])
        assert client.batches == []
        assert report.pushed == 0
        assert report.ok


class TestOwnAccountsIntegration:
    """§6.3 — the registry has to actually change what the sync does."""

    async def test_withdrawal_to_own_wallet_is_recognised_as_a_transfer(
        self, ledger: SyncLedger, tmp_path: Path
    ) -> None:
        from app.services.own_accounts import OwnAccountsRegistry

        own = tmp_path / "own.yaml"
        own.write_text("addresses:\n  - bc1qmine\n", encoding="utf-8")

        client = _FakeClient()
        sync = GhostfolioSync(
            client=client,  # type: ignore[arg-type]
            ledger=ledger,
            account_id_by_source=_ACCOUNTS,
            crypto_overrides=_OVERRIDES,
            own_accounts=OwnAccountsRegistry.load(own),
        )
        report = await sync.push(
            [_tx("w1", source="binance", side="withdrawal", counterparty="bc1qmine")]
        )
        assert report.pushed == 0
        assert report.skip_counts() == {"transfer_excluded_per_6_3": 1}
        assert client.batches == []

    async def test_sync_works_without_a_registry(self, ledger: SyncLedger) -> None:
        # The default must be safe, not broken: no registry means
        # movements stay withdrawals, which are still never pushed.
        client = _FakeClient()
        report = await _sync(client, ledger).push(
            [_tx("w1", source="binance", side="withdrawal", counterparty="bc1qmine")]
        )
        assert report.pushed == 0
        assert client.batches == []
