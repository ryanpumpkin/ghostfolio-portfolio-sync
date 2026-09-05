"""Composite scoring for the analyst report.

Takes the raw data produced by the Longbridge fetchers + the position
the user holds, runs the technical indicators over the candle series,
and produces an `AnalystReport` with:

  * Seven sub-scores in the range [-100, +100]:
      trend, momentum, flow, volatility, valuation, sentiment, earnings
  * A weighted composite in the same range.
  * A headline label drawn from the composite:
      BUY (composite >= +60)
      HOLD ([-20, +60))
      TRIM ((-60, -20))
      SELL (composite <= -60)
  * Stop-loss + take-profit zones derived from ATR + recent swing levels.
  * Per-sub-score "evidence" — the actual numbers that drove the score,
    so the UI can show the reasoning, not just the verdict.

Design principles:
  * Every sub-score is computed from inputs that may be missing.
    Sub-scores return None when there's not enough data, and the
    composite weights them out — no fabricated signals.
  * The thresholds (e.g. RSI < 30 is +60, > 70 is -60) are tuned to be
    conservative — the report is meant to inform, not to trade for
    you. A "BUY" needs broad agreement across multiple sub-scores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, UTC

from app.services.analyst import indicators
from app.services.analyst.longbridge_data import (
    Candle,
    CapitalDistribution,
    CalcIndexes,
    QuoteSnapshot,
)


# ---------------------------------------------------------------------------
# Data wrapper (everything the scorer needs in one place)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AnalystInputs:
    symbol: str
    quote: QuoteSnapshot | None
    candles: list[Candle]
    capital: CapitalDistribution | None
    indexes: CalcIndexes | None
    # User's position context (so we can compute "vs your cost basis")
    avg_cost: float | None = None
    quantity: float | None = None


@dataclass(frozen=True, slots=True)
class SubScore:
    name: str
    score: float | None  # -100 to +100, None if insufficient data
    evidence: dict[str, str]


@dataclass(frozen=True, slots=True)
class TechnicalSnapshot:
    sma_20: float | None
    sma_50: float | None
    sma_200: float | None
    ema_9: float | None
    ema_21: float | None
    rsi_14: float | None
    macd: float | None
    macd_signal: float | None
    macd_histogram: float | None
    bb_upper: float | None
    bb_middle: float | None
    bb_lower: float | None
    atr_14: float | None
    # The most recent few support / resistance levels, derived from
    # swing-high / swing-low pivots over the last ~60 sessions.
    support_levels: list[float] = field(default_factory=list)
    resistance_levels: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class AnalystReport:
    symbol: str
    generated_at: datetime
    label: str  # BUY | HOLD | TRIM | SELL | INSUFFICIENT_DATA
    composite_score: float | None
    last_price: float | None
    avg_cost: float | None
    pct_vs_cost: float | None  # +14.1 means up 14.1% from your cost
    stop_loss: float | None
    take_profit_1: float | None
    take_profit_2: float | None
    sub_scores: list[SubScore]
    technicals: TechnicalSnapshot
    summary: str  # short plain-English reasoning


# Weights for the composite. The flow / trend / momentum cluster
# dominates because those are the signals that actually predict
# near-term price action. Valuation gets less weight because most
# brokerage names with extreme P/E are still tradeable.
_WEIGHTS: dict[str, float] = {
    "trend": 0.22,
    "momentum": 0.20,
    "flow": 0.20,
    "sentiment": 0.15,
    "valuation": 0.10,
    "volatility": 0.08,
    "earnings": 0.05,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_report(
    inputs: AnalystInputs,
    *,
    analyst_target: float | None = None,
    analyst_recommendation: str | None = None,
) -> AnalystReport:
    """Produce a full report. `analyst_target` and `analyst_recommendation`
    come from `institution_rating` and feed the sentiment sub-score."""
    closes = [c.close for c in inputs.candles]
    highs = [c.high for c in inputs.candles]
    lows = [c.low for c in inputs.candles]
    last_price = (
        inputs.quote.last_price if inputs.quote else None
    ) or (closes[-1] if closes else None)

    tech = _compute_technicals(closes, highs, lows)
    pct_vs_cost = (
        ((last_price - inputs.avg_cost) / inputs.avg_cost * 100.0)
        if last_price is not None
        and inputs.avg_cost is not None
        and inputs.avg_cost != 0
        else None
    )

    subs = [
        _score_trend(last_price, tech),
        _score_momentum(tech),
        _score_flow(inputs.capital),
        _score_sentiment(
            last_price, analyst_target=analyst_target,
            recommendation=analyst_recommendation,
        ),
        _score_valuation(inputs.indexes),
        _score_volatility(tech, last_price),
        _score_earnings(inputs.indexes),
    ]

    composite = _weighted_composite(subs)
    label = _label_for(composite)

    stop_loss, tp1, tp2 = _compute_stops_and_targets(
        last_price=last_price,
        avg_cost=inputs.avg_cost,
        atr_14=tech.atr_14,
        supports=tech.support_levels,
        resistances=tech.resistance_levels,
    )

    summary = _build_summary(label, subs, pct_vs_cost)

    return AnalystReport(
        symbol=inputs.symbol,
        generated_at=datetime.now(UTC),
        label=label,
        composite_score=composite,
        last_price=last_price,
        avg_cost=inputs.avg_cost,
        pct_vs_cost=pct_vs_cost,
        stop_loss=stop_loss,
        take_profit_1=tp1,
        take_profit_2=tp2,
        sub_scores=subs,
        technicals=tech,
        summary=summary,
    )


# ---------------------------------------------------------------------------
# Technicals
# ---------------------------------------------------------------------------


def _compute_technicals(
    closes: list[float],
    highs: list[float],
    lows: list[float],
) -> TechnicalSnapshot:
    sma_20 = indicators.latest(indicators.sma(closes, 20))
    sma_50 = indicators.latest(indicators.sma(closes, 50))
    sma_200 = indicators.latest(indicators.sma(closes, 200))
    ema_9 = indicators.latest(indicators.ema(closes, 9))
    ema_21 = indicators.latest(indicators.ema(closes, 21))
    rsi_14 = indicators.latest(indicators.rsi(closes, 14))
    macd_series = indicators.macd(closes)
    bb = indicators.bollinger(closes, 20, 2.0)
    atr_14 = indicators.latest(indicators.atr(highs, lows, closes, 14))
    swings = indicators.swing_levels(highs, lows, lookback=5)
    last_price = closes[-1] if closes else None

    supports: list[float] = []
    resistances: list[float] = []
    if last_price is not None:
        for s in reversed(swings):
            if s.kind == "low" and s.price < last_price and len(supports) < 3:
                supports.append(s.price)
            elif s.kind == "high" and s.price > last_price and len(resistances) < 3:
                resistances.append(s.price)
            if len(supports) >= 3 and len(resistances) >= 3:
                break

    return TechnicalSnapshot(
        sma_20=sma_20,
        sma_50=sma_50,
        sma_200=sma_200,
        ema_9=ema_9,
        ema_21=ema_21,
        rsi_14=rsi_14,
        macd=indicators.latest(macd_series.macd),
        macd_signal=indicators.latest(macd_series.signal),
        macd_histogram=indicators.latest(macd_series.histogram),
        bb_upper=indicators.latest(bb.upper),
        bb_middle=indicators.latest(bb.middle),
        bb_lower=indicators.latest(bb.lower),
        atr_14=atr_14,
        support_levels=supports,
        resistance_levels=resistances,
    )


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------


def _score_trend(price: float | None, t: TechnicalSnapshot) -> SubScore:
    if price is None or (t.sma_20 is None and t.sma_50 is None and t.sma_200 is None):
        return SubScore("trend", None, {"reason": "insufficient candles"})

    points = 0.0
    weights = 0.0
    evidence: dict[str, str] = {}

    for label, ma_value, weight in (
        ("vs SMA20", t.sma_20, 0.40),
        ("vs SMA50", t.sma_50, 0.35),
        ("vs SMA200", t.sma_200, 0.25),
    ):
        if ma_value is None or ma_value == 0:
            continue
        weights += weight
        delta_pct = (price - ma_value) / ma_value * 100.0
        # Map +/- 10% over the MA to a +/- 100 sub-component, clipped.
        bucket = max(-100.0, min(100.0, delta_pct * 10.0))
        points += bucket * weight
        evidence[label] = f"price {price:.2f} vs MA {ma_value:.2f}  ({delta_pct:+.2f}%)"

    if weights == 0.0:
        return SubScore("trend", None, {"reason": "no valid MAs"})
    score = points / weights
    return SubScore("trend", round(score, 1), evidence)


def _score_momentum(t: TechnicalSnapshot) -> SubScore:
    if t.rsi_14 is None and t.macd_histogram is None:
        return SubScore("momentum", None, {"reason": "insufficient history"})
    parts: list[float] = []
    evidence: dict[str, str] = {}
    if t.rsi_14 is not None:
        # RSI: <30 oversold (bullish reversal: +60), >70 overbought
        # (bearish reversal: -60), neutral around 50.
        if t.rsi_14 <= 30:
            score = +60.0 + (30 - t.rsi_14) * 2.0
        elif t.rsi_14 >= 70:
            score = -60.0 - (t.rsi_14 - 70) * 2.0
        else:
            # Linear ramp inside the 30-70 band so RSI 60 reads "mild
            # overbought, slightly bearish" not pure neutral.
            score = (50.0 - t.rsi_14) * 1.5
        parts.append(max(-100.0, min(100.0, score)))
        evidence["RSI(14)"] = (
            f"{t.rsi_14:.1f} "
            + ("oversold" if t.rsi_14 < 30 else "overbought" if t.rsi_14 > 70 else "neutral")
        )
    if (
        t.macd is not None
        and t.macd_signal is not None
        and t.macd_histogram is not None
    ):
        # Histogram > 0 = MACD above signal (bullish). Magnitude
        # relative to MACD line is the strength.
        if t.macd_signal != 0:
            magnitude = abs(t.macd_histogram) / max(abs(t.macd_signal), 1e-6) * 50.0
        else:
            magnitude = 30.0
        sign = 1.0 if t.macd_histogram > 0 else -1.0
        parts.append(max(-100.0, min(100.0, sign * magnitude)))
        evidence["MACD hist"] = (
            f"{t.macd_histogram:+.3f}  ({'bullish' if t.macd_histogram > 0 else 'bearish'})"
        )
    if not parts:
        return SubScore("momentum", None, {"reason": "no valid signals"})
    return SubScore("momentum", round(sum(parts) / len(parts), 1), evidence)


def _score_flow(cap: CapitalDistribution | None) -> SubScore:
    if cap is None:
        return SubScore("flow", None, {"reason": "capital distribution unavailable"})
    gross_total = (
        cap.in_large + cap.in_medium + cap.in_small
        + cap.out_large + cap.out_medium + cap.out_small
    )
    if gross_total == 0:
        return SubScore("flow", 0.0, {"reason": "zero turnover"})

    # Weight large > medium > small. Large bucket is what we care
    # about — institutional money.
    weighted_net = (
        cap.net_large * 0.55
        + cap.net_medium * 0.30
        + cap.net_small * 0.15
    )
    # Normalize against gross volume — score is the net flow as a
    # share of total turnover, scaled to the [-100, +100] band.
    score = max(-100.0, min(100.0, weighted_net / gross_total * 400.0))
    evidence = {
        "large net": f"{cap.net_large:+,.0f}",
        "medium net": f"{cap.net_medium:+,.0f}",
        "small net": f"{cap.net_small:+,.0f}",
        "total net": f"{cap.net_total:+,.0f}",
    }
    return SubScore("flow", round(score, 1), evidence)


def _score_sentiment(
    price: float | None,
    *,
    analyst_target: float | None,
    recommendation: str | None,
) -> SubScore:
    if price is None or analyst_target is None or analyst_target == 0:
        if recommendation is None:
            return SubScore(
                "sentiment", None, {"reason": "no analyst data"}
            )
        # Recommendation-only fallback.
        rec_map = {
            "strong_buy": 80.0,
            "buy": 50.0,
            "hold": 0.0,
            "sell": -50.0,
            "strong_sell": -80.0,
        }
        score = rec_map.get(recommendation.strip().lower(), 0.0)
        return SubScore(
            "sentiment", score, {"recommendation": recommendation}
        )
    upside_pct = (analyst_target - price) / price * 100.0
    # +20% upside → +100. -20% downside → -100. Linear in between.
    score = max(-100.0, min(100.0, upside_pct * 5.0))
    evidence = {
        "target": f"{analyst_target:.2f}",
        "upside": f"{upside_pct:+.2f}%",
    }
    if recommendation:
        evidence["recommendation"] = recommendation
    return SubScore("sentiment", round(score, 1), evidence)


def _score_valuation(indexes: CalcIndexes | None) -> SubScore:
    if indexes is None:
        return SubScore("valuation", None, {"reason": "calc indexes unavailable"})
    # PE and PB drive the score. Negative or very high PE penalizes;
    # moderate PE neutral; low PE bonus. PB > 10 is rich, < 1 is cheap.
    evidence: dict[str, str] = {}
    parts: list[float] = []
    if indexes.pe is not None:
        evidence["P/E"] = f"{indexes.pe:.2f}"
        if indexes.pe < 0:
            parts.append(-60.0)  # losing money
        elif indexes.pe < 10:
            parts.append(+60.0)
        elif indexes.pe < 20:
            parts.append(+25.0)
        elif indexes.pe < 35:
            parts.append(0.0)
        elif indexes.pe < 60:
            parts.append(-25.0)
        else:
            parts.append(-60.0)
    if indexes.pb is not None:
        evidence["P/B"] = f"{indexes.pb:.2f}"
        if indexes.pb < 1:
            parts.append(+50.0)
        elif indexes.pb < 3:
            parts.append(+20.0)
        elif indexes.pb < 6:
            parts.append(0.0)
        elif indexes.pb < 12:
            parts.append(-30.0)
        else:
            parts.append(-60.0)
    if not parts:
        return SubScore("valuation", None, {"reason": "no PE/PB"})
    return SubScore("valuation", round(sum(parts) / len(parts), 1), evidence)


def _score_volatility(t: TechnicalSnapshot, price: float | None) -> SubScore:
    """Volatility score is a RISK gauge, not a direction call.

    Higher score = lower volatility = safer. Lower score = higher
    volatility = the position is more likely to move violently in
    either direction (so the user should size accordingly).
    """
    if t.atr_14 is None or price is None or price == 0:
        return SubScore("volatility", None, {"reason": "ATR unavailable"})
    atr_pct = t.atr_14 / price * 100.0
    # ATR < 1% of price → very calm (+80). > 8% → very volatile (-80).
    if atr_pct < 1:
        score = +80.0
    elif atr_pct < 2:
        score = +40.0
    elif atr_pct < 4:
        score = 0.0
    elif atr_pct < 6:
        score = -30.0
    elif atr_pct < 8:
        score = -60.0
    else:
        score = -80.0
    return SubScore(
        "volatility", score, {"ATR/price": f"{atr_pct:.2f}%"}
    )


def _score_earnings(indexes: CalcIndexes | None) -> SubScore:
    if indexes is None or indexes.pe is None:
        return SubScore("earnings", None, {"reason": "no PE data"})
    if indexes.pe < 0:
        return SubScore(
            "earnings", -40.0, {"P/E": f"{indexes.pe:.2f}", "note": "unprofitable"}
        )
    if indexes.pe < 25:
        return SubScore(
            "earnings", +30.0, {"P/E": f"{indexes.pe:.2f}", "note": "earning, fair multiple"}
        )
    return SubScore(
        "earnings", 0.0, {"P/E": f"{indexes.pe:.2f}", "note": "earning, rich multiple"}
    )


# ---------------------------------------------------------------------------
# Composite + label
# ---------------------------------------------------------------------------


def _weighted_composite(subs: list[SubScore]) -> float | None:
    total = 0.0
    used_weight = 0.0
    for s in subs:
        if s.score is None:
            continue
        w = _WEIGHTS.get(s.name, 0.0)
        total += s.score * w
        used_weight += w
    if used_weight == 0:
        return None
    return round(total / used_weight, 1)


def _label_for(composite: float | None) -> str:
    if composite is None:
        return "INSUFFICIENT_DATA"
    if composite >= 60:
        return "BUY"
    if composite >= -20:
        return "HOLD"
    if composite > -60:
        return "TRIM"
    return "SELL"


# ---------------------------------------------------------------------------
# Stops + targets
# ---------------------------------------------------------------------------


def _compute_stops_and_targets(
    *,
    last_price: float | None,
    avg_cost: float | None,
    atr_14: float | None,
    supports: list[float],
    resistances: list[float],
) -> tuple[float | None, float | None, float | None]:
    """Compute stop-loss, take-profit-1, take-profit-2.

    Stop is the max of (2 × ATR below last price) and (user's cost
    basis − tiny buffer) — so we never set a stop above the user's
    break-even point unnecessarily. Targets are the nearest two
    resistance pivots; if not enough swings, fall back to ATR multiples.
    """
    if last_price is None:
        return None, None, None
    stop: float | None = None
    if atr_14 is not None:
        atr_stop = last_price - 2.0 * atr_14
        candidates = [atr_stop]
        if avg_cost is not None and avg_cost > 0:
            # If we're already in profit, don't set a stop above cost
            # basis — that's premature exit. If we're underwater,
            # respect the ATR stop (don't extend it deeper just to
            # avoid the loss).
            if last_price > avg_cost:
                candidates.append(avg_cost * 0.995)  # break-even-ish
        stop = max(candidates)

    tp1: float | None = None
    tp2: float | None = None
    if resistances:
        tp1 = resistances[0]
        if len(resistances) > 1:
            tp2 = resistances[1]
    if tp1 is None and atr_14 is not None:
        tp1 = last_price + 2.0 * atr_14
    if tp2 is None and atr_14 is not None:
        tp2 = last_price + 4.0 * atr_14
    return stop, tp1, tp2


# ---------------------------------------------------------------------------
# Summary text
# ---------------------------------------------------------------------------


def _build_summary(
    label: str, subs: list[SubScore], pct_vs_cost: float | None
) -> str:
    score_map = {s.name: s.score for s in subs if s.score is not None}
    flow = score_map.get("flow")
    trend = score_map.get("trend")
    momentum = score_map.get("momentum")
    sentiment = score_map.get("sentiment")

    bits: list[str] = []
    if pct_vs_cost is not None:
        bits.append(f"{pct_vs_cost:+.1f}% vs your cost.")
    if flow is not None:
        if flow > 30:
            bits.append("Large-order flow is net buying.")
        elif flow < -30:
            bits.append("Large-order flow is net selling.")
    if trend is not None and momentum is not None:
        if trend > 20 and momentum > 20:
            bits.append("Trend and momentum both bullish.")
        elif trend < -20 and momentum < -20:
            bits.append("Trend and momentum both bearish.")
        elif trend > 20 and momentum < -20:
            bits.append("Uptrend, but short-term momentum is fading.")
        elif trend < -20 and momentum > 20:
            bits.append("Downtrend, but a short-term bounce is forming.")
    if sentiment is not None and sentiment > 40:
        bits.append("Analyst consensus sees meaningful upside.")
    elif sentiment is not None and sentiment < -40:
        bits.append("Analyst consensus is bearish.")

    headline = {
        "BUY": "Composite signal: BUY.",
        "HOLD": "Composite signal: HOLD.",
        "TRIM": "Composite signal: TRIM — consider reducing position.",
        "SELL": "Composite signal: SELL.",
        "INSUFFICIENT_DATA": "Not enough data to score this position.",
    }[label]
    return headline + " " + " ".join(bits)
