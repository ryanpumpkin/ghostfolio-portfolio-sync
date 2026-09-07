"""Duplicate-activity detection (§3.3, §6.4).

Modelled on the live finding: four Futu activities existed twice because
a sync had run with an empty ledger. Two were fractional VOO buys whose
combined 0.0353 shares had been showing up as an unexplained
reconciliation "surplus" for days.
"""

from __future__ import annotations

from app.services.ghostfolio.audit import duplicate_external_ids


def _activity(activity_id: str, comment: str, created: str, symbol: str = "VOO") -> dict:
    return {
        "id": activity_id,
        "comment": comment,
        "createdAt": created,
        "type": "BUY",
        "quantity": 0.0179,
        "unitPrice": 557.92,
        "date": "2024-12-16T00:00:00.000Z",
        "SymbolProfile": {"symbol": symbol},
    }


def test_a_clean_portfolio_reports_nothing() -> None:
    assert duplicate_external_ids([
        _activity("1", "futu:-:a", "2026-01-01"),
        _activity("2", "futu:-:b", "2026-01-01"),
    ]) == []


def test_the_second_copy_is_the_one_to_remove() -> None:
    found = duplicate_external_ids([
        _activity("2", "futu:-:a", "2026-02-01"),
        _activity("1", "futu:-:a", "2026-01-01"),
    ])
    assert len(found) == 1
    assert found[0].keep["id"] == "1"
    assert [e["id"] for e in found[0].extra] == ["2"]


def test_three_copies_leave_one() -> None:
    found = duplicate_external_ids([
        _activity(str(n), "futu:-:a", f"2026-01-0{n}") for n in (1, 2, 3)
    ])
    assert found[0].keep["id"] == "1"
    assert len(found[0].extra) == 2


def test_activities_without_an_external_id_are_left_alone() -> None:
    """Rows a human entered in the UI. Two blanks prove nothing."""
    assert duplicate_external_ids([
        _activity("1", "", "2026-01-01"),
        _activity("2", "", "2026-01-01"),
    ]) == []


def test_the_description_names_the_trade() -> None:
    found = duplicate_external_ids([
        _activity("1", "futu:-:a", "2026-01-01"),
        _activity("2", "futu:-:a", "2026-02-01"),
    ])
    described = found[0].describe()
    assert "futu:-:a" in described
    assert "VOO" in described
    assert "2 copies" in described
