"""Contract test: every concrete adapter satisfies `SourceAdapter`."""

from __future__ import annotations

import pytest

from app.adapters.base import SourceAdapter
from app.adapters.binance import BinanceAdapter, BinanceHost
from app.adapters.futu import FutuAdapter
from app.adapters.ibkr import IbkrAdapter
from app.adapters.longbridge import LongBridgeAdapter
from tests.adapters.test_binance import FakeBinanceClient
from tests.adapters.test_futu import FakeFutuClient
from tests.adapters.test_ibkr import FakeIbkrClient
from tests.adapters.test_longbridge import FakeLBClient


@pytest.mark.parametrize(
    "adapter",
    [
        LongBridgeAdapter(FakeLBClient()),
        IbkrAdapter(FakeIbkrClient()),
        FutuAdapter(FakeFutuClient()),
        BinanceAdapter(FakeBinanceClient(host=BinanceHost.COM)),
        BinanceAdapter(FakeBinanceClient(host=BinanceHost.US)),
    ],
    ids=["longbridge", "ibkr", "futu", "binance.com", "binance.us"],
)
def test_adapter_implements_source_adapter(adapter: object) -> None:
    assert isinstance(adapter, SourceAdapter)
    # Required attribute on every adapter for source-tagged responses.
    assert getattr(adapter, "source", None)


def test_source_names_are_unique() -> None:
    sources = {
        LongBridgeAdapter(FakeLBClient()).source,
        IbkrAdapter(FakeIbkrClient()).source,
        FutuAdapter(FakeFutuClient()).source,
        BinanceAdapter(FakeBinanceClient()).source,
    }
    assert sources == {"longbridge", "ibkr", "futu", "binance"}
