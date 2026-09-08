"""Unit tests for the pure helpers in `app.adapters.longbridge.client`.

The `LongbridgeClient` class itself is exercised only via env-gated
integration tests because it instantiates the real SDK. These helpers
are pure and worth covering directly.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.adapters._common import PermanentError, TransientError
from app.adapters.longbridge.client import (
    _classify_sdk_error,
    _history_rows,
    _history_start,
    _parse_since,
    _row_timestamp,
    _to_iterable,
)

# ---------------------------------------------------------------------------
# _parse_since
# ---------------------------------------------------------------------------


def test_parse_since_utc_z_suffix() -> None:
    parsed = _parse_since("2026-01-15T12:30:00Z")
    assert parsed == datetime(2026, 1, 15, 12, 30, tzinfo=UTC)


def test_parse_since_offset() -> None:
    parsed = _parse_since("2026-01-15T20:30:00+08:00")
    assert parsed.utcoffset() is not None
    assert parsed.astimezone(UTC) == datetime(2026, 1, 15, 12, 30, tzinfo=UTC)


def test_parse_since_naive_defaults_to_utc() -> None:
    parsed = _parse_since("2026-01-15T12:30:00")
    assert parsed.tzinfo == UTC


def test_history_start_defaults_to_a_generous_lookback() -> None:
    # See the matching Futu test: 90 days lost the entire history of a
    # long-term portfolio. §4.1 — a missed trade is a permanently wrong
    # cost basis, so err wide.
    start = _history_start(None)
    delta_days = (datetime.now(UTC) - start).days
    assert delta_days >= 365 * 2, f"lookback shrank to {delta_days} days"


def test_history_start_uses_since_value_when_provided() -> None:
    parsed = _history_start("2026-01-15T12:30:00Z")
    assert parsed == datetime(2026, 1, 15, 12, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _row_timestamp
# ---------------------------------------------------------------------------


class _Row:
    def __init__(self, trade_done_at: object | None) -> None:
        self.trade_done_at = trade_done_at


def test_row_timestamp_attr_datetime_naive_gets_utc() -> None:
    ts = datetime(2026, 1, 15, 9, 0)
    assert _row_timestamp(_Row(ts)) == ts.replace(tzinfo=UTC)


def test_row_timestamp_attr_datetime_aware_preserved() -> None:
    ts = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    assert _row_timestamp(_Row(ts)) == ts


def test_row_timestamp_attr_iso_string() -> None:
    assert _row_timestamp(_Row("2026-01-15T09:00:00Z")) == datetime(
        2026, 1, 15, 9, 0, tzinfo=UTC,
    )


def test_row_timestamp_dict_key() -> None:
    row = {"trade_done_at": "2026-02-01T00:00:00Z"}
    assert _row_timestamp(row) == datetime(2026, 2, 1, tzinfo=UTC)


def test_row_timestamp_supports_numeric_millis() -> None:
    row = {"timestamp": 1736899200000}
    assert _row_timestamp(row) == datetime(2025, 1, 15, tzinfo=UTC)


def test_row_timestamp_missing_returns_none() -> None:
    assert _row_timestamp(_Row(None)) is None
    assert _row_timestamp({}) is None


# ---------------------------------------------------------------------------
# _classify_sdk_error
# ---------------------------------------------------------------------------


class _CodedError(Exception):
    def __init__(self, message: str, code: object) -> None:
        super().__init__(message)
        self.code = code


class _StatusError(Exception):
    def __init__(self, message: str, status_code: object) -> None:
        super().__init__(message)
        self.status_code = status_code


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit exceeded",
        "Too many requests, try again later",
        "Request timeout",
        "Service temporarily unavailable",
    ],
)
def test_classify_transient_by_message(message: str) -> None:
    classified = _classify_sdk_error(Exception(message))
    assert isinstance(classified, TransientError)


@pytest.mark.parametrize("code", ["429", "500", "502", "503", "504", "301606"])
def test_classify_transient_by_code(code: str) -> None:
    classified = _classify_sdk_error(_CodedError("boom", code))
    assert isinstance(classified, TransientError)


def test_classify_transient_by_status_code() -> None:
    classified = _classify_sdk_error(_StatusError("boom", 503))
    assert isinstance(classified, TransientError)


@pytest.mark.parametrize(
    "message",
    [
        "Invalid access token",
        "Access token expired",
        "Invalid app key",
        "wrong app secret",
        "Unauthorized request",
        "Forbidden",
        "Invalid credential",
    ],
)
def test_classify_permanent_by_message(message: str) -> None:
    classified = _classify_sdk_error(Exception(message))
    assert isinstance(classified, PermanentError)


@pytest.mark.parametrize("code", ["401", "403", "100002", "100004"])
def test_classify_permanent_by_code(code: str) -> None:
    classified = _classify_sdk_error(_CodedError("nope", code))
    assert isinstance(classified, PermanentError)


def test_classify_pass_through_when_already_classified() -> None:
    err = TransientError("already classified")
    assert _classify_sdk_error(err) is err
    err2 = PermanentError("already classified")
    assert _classify_sdk_error(err2) is err2


def test_classify_unknown_exception_passes_through() -> None:
    err = ValueError("something else entirely")
    assert _classify_sdk_error(err) is err


# ---------------------------------------------------------------------------
# _to_iterable
# ---------------------------------------------------------------------------


class _ResponseWith:
    def __init__(self, **fields: object) -> None:
        for k, v in fields.items():
            setattr(self, k, v)


def test_to_iterable_none() -> None:
    assert _to_iterable(None, attribute="channels") == []


def test_to_iterable_already_a_list() -> None:
    items = [1, 2, 3]
    assert _to_iterable(items, attribute="channels") == [1, 2, 3]


def test_to_iterable_tuple() -> None:
    assert _to_iterable((1, 2), attribute="channels") == [1, 2]


def test_to_iterable_uses_primary_attribute() -> None:
    resp = _ResponseWith(channels=[1, 2])
    assert _to_iterable(resp, attribute="channels") == [1, 2]


def test_to_iterable_falls_back_to_secondary_attribute() -> None:
    resp = _ResponseWith(accounts=[7, 8])
    assert _to_iterable(resp, attribute="list", attribute_fallback="accounts") == [
        7,
        8,
    ]


def test_to_iterable_iterates_if_iter_supported() -> None:
    class _Iter:
        def __iter__(self) -> object:
            return iter([10, 20])

    assert _to_iterable(_Iter(), attribute="missing") == [10, 20]


def test_to_iterable_unsupported_returns_empty() -> None:
    resp = _ResponseWith(unrelated="thing")
    assert _to_iterable(resp, attribute="channels") == []


def test_history_rows_prefers_executions_attribute() -> None:
    resp = _ResponseWith(executions=[{"id": "1"}], trades=[{"id": "2"}])
    assert _history_rows(resp) == [{"id": "1"}]


def test_history_rows_supports_dict_items_shape() -> None:
    assert _history_rows({"items": [{"id": "x"}]}) == [{"id": "x"}]


class TestOrderCharges:
    """Commission comes from `order_detail`, once per order (§5.6).

    Neither the execution nor the order carries a fee, so every LongBridge
    activity was pushed with fee=0 — understating cost basis and
    overstating every return built on it.
    """

    def test_a_settled_waiver_reduces_the_charge(self) -> None:
        from decimal import Decimal

        from app.adapters.longbridge.client import _apply_waiver

        assert _apply_waiver(
            Decimal("1.03"), waiver=Decimal("-0.99"), status="CommissionFreeStatus.Ready"
        ) == Decimal("0.04")

    def test_an_unsettled_waiver_is_not_assumed(self) -> None:
        # Treating a pending waiver as applied understates the cost, and
        # overstating a return is the more dangerous way to be wrong.
        from decimal import Decimal

        from app.adapters.longbridge.client import _apply_waiver

        assert _apply_waiver(
            Decimal("1.03"), waiver=Decimal("-0.99"),
            status="CommissionFreeStatus.Pending",
        ) == Decimal("1.03")

    def test_a_waiver_never_makes_the_fee_negative(self) -> None:
        from decimal import Decimal

        from app.adapters.longbridge.client import _apply_waiver

        assert _apply_waiver(
            Decimal("0.50"), waiver=Decimal("-9.99"), status="Ready"
        ) == Decimal("0")

    def test_charge_is_split_across_an_orders_fills(self) -> None:
        """One order, seven fills, one commission.

        The live data has a RUN order that filled as seven 1-share
        executions. Attaching the whole charge to each would multiply the
        fee by seven.
        """
        from decimal import Decimal

        from app.adapters.longbridge.client import _split_charge

        # 7 fills of 1 share each, out of 7 shares total.
        shares = [
            _split_charge(Decimal("2.10"), fill_quantity=Decimal("1"),
                          order_quantity=Decimal("7"))
            for _ in range(7)
        ]
        assert sum(shares) == Decimal("2.10")

    def test_an_unfilled_order_does_not_divide_by_zero(self) -> None:
        from decimal import Decimal

        from app.adapters.longbridge.client import _split_charge

        assert _split_charge(
            Decimal("1"), fill_quantity=Decimal("0"), order_quantity=Decimal("0")
        ) == Decimal("1")
