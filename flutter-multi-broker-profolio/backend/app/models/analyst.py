"""Pydantic API models for the analyst report.

Mirrors the internal dataclasses in `app.services.analyst.scoring` but
exposed as Pydantic BaseModel subclasses so FastAPI can use them as
`response_model=` and the Flutter client gets a clean JSON shape.

The translation is one-way: internal computation → API model.
Test code imports the internal dataclasses directly; the API layer
converts at the boundary via `AnalystReportApi.from_internal(...)`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class _Base(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SubScoreApi(_Base):
    name: str
    score: float | None
    evidence: dict[str, str]


class TechnicalSnapshotApi(_Base):
    sma_20: float | None = None
    sma_50: float | None = None
    sma_200: float | None = None
    ema_9: float | None = None
    ema_21: float | None = None
    rsi_14: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    atr_14: float | None = None
    support_levels: list[float] = []
    resistance_levels: list[float] = []


class AnalystReportApi(_Base):
    symbol: str
    generated_at: datetime
    label: str  # BUY | HOLD | TRIM | SELL | INSUFFICIENT_DATA
    composite_score: float | None
    last_price: float | None
    avg_cost: float | None
    pct_vs_cost: float | None
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    sub_scores: list[SubScoreApi]
    technicals: TechnicalSnapshotApi
    summary: str

    @classmethod
    def from_internal(cls, internal: Any) -> "AnalystReportApi":
        """Convert a `scoring.AnalystReport` dataclass to the API model.

        We accept `Any` to avoid a circular import between the API model
        layer and the service layer — the dataclass is structurally
        compatible by field name, which is what we rely on here.
        """
        sub_scores = [
            SubScoreApi(
                name=s.name,
                score=s.score,
                evidence=dict(s.evidence),
            )
            for s in internal.sub_scores
        ]
        t = internal.technicals
        technicals = TechnicalSnapshotApi(
            sma_20=t.sma_20,
            sma_50=t.sma_50,
            sma_200=t.sma_200,
            ema_9=t.ema_9,
            ema_21=t.ema_21,
            rsi_14=t.rsi_14,
            macd=t.macd,
            macd_signal=t.macd_signal,
            macd_histogram=t.macd_histogram,
            bb_upper=t.bb_upper,
            bb_middle=t.bb_middle,
            bb_lower=t.bb_lower,
            atr_14=t.atr_14,
            support_levels=list(t.support_levels),
            resistance_levels=list(t.resistance_levels),
        )
        return cls(
            symbol=internal.symbol,
            generated_at=internal.generated_at,
            label=internal.label,
            composite_score=internal.composite_score,
            last_price=internal.last_price,
            avg_cost=internal.avg_cost,
            pct_vs_cost=internal.pct_vs_cost,
            stop_loss=internal.stop_loss,
            take_profit_1=internal.take_profit_1,
            take_profit_2=internal.take_profit_2,
            sub_scores=sub_scores,
            technicals=technicals,
            summary=internal.summary,
        )
