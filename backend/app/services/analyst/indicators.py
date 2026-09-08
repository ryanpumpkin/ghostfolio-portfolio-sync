"""Technical indicators for the analyst service.

Pure Python, no numpy / pandas / talib dependencies. Functions accept
plain lists of floats and return lists of the same length, with `None`
in positions that don't have enough history to compute the indicator
yet (so the index aligns with the input series — easy to zip with
candles when rendering).

Conventions:
- "closes", "highs", "lows", "volumes" are oldest-first lists.
- All numeric inputs may be float or Decimal; outputs are float.
- We use Wilder's smoothing (RMA) for RSI and ATR, which differs from
  a vanilla EMA — this is the convention every charting platform uses,
  and the convention every trader expects when they read "RSI(14)".
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


def _to_float(x: object) -> float:
    """Coerce any numeric-ish input to float. None passes through."""
    if x is None:
        raise TypeError("None is not a valid numeric input")
    return float(x)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Simple Moving Average
# ---------------------------------------------------------------------------


def sma(values: Sequence[float], period: int) -> list[float | None]:
    """Simple moving average. Returns list aligned with input length.

    Positions [0 .. period-2] are None (not enough history).
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    out: list[float | None] = []
    running = 0.0
    for i, raw in enumerate(values):
        v = _to_float(raw)
        running += v
        if i >= period:
            running -= _to_float(values[i - period])
        if i + 1 >= period:
            out.append(running / period)
        else:
            out.append(None)
    return out


# ---------------------------------------------------------------------------
# Exponential Moving Average
# ---------------------------------------------------------------------------


def ema(values: Sequence[float], period: int) -> list[float | None]:
    """Exponential moving average with alpha = 2/(period+1).

    Seeded with the SMA of the first `period` values so EMA(period)[period-1]
    matches what every charting tool draws as "first EMA point".
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(values)
    out: list[float | None] = [None] * n
    if n < period:
        return out

    seed = sum(_to_float(v) for v in values[:period]) / period
    out[period - 1] = seed
    alpha = 2.0 / (period + 1)
    prev = seed
    for i in range(period, n):
        v = _to_float(values[i])
        cur = (v - prev) * alpha + prev
        out[i] = cur
        prev = cur
    return out


# ---------------------------------------------------------------------------
# RSI (Wilder)
# ---------------------------------------------------------------------------


def rsi(closes: Sequence[float], period: int = 14) -> list[float | None]:
    """Relative Strength Index using Wilder's smoothing (RMA).

    First `period` positions are None (need `period` changes which means
    `period+1` closes, but we keep the standard "RSI is first valid at
    bar `period`" indexing every chart uses).
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    n = len(closes)
    out: list[float | None] = [None] * n
    if n <= period:
        return out

    gains = 0.0
    losses = 0.0
    # Seed the average gain/loss over the first `period` changes (closes
    # 1..period).
    for i in range(1, period + 1):
        change = _to_float(closes[i]) - _to_float(closes[i - 1])
        if change >= 0:
            gains += change
        else:
            losses -= change  # store as positive magnitude
    avg_gain = gains / period
    avg_loss = losses / period

    def _rsi_from(avg_g: float, avg_l: float) -> float:
        if avg_l == 0:
            return 100.0
        rs = avg_g / avg_l
        return 100.0 - (100.0 / (1.0 + rs))

    out[period] = _rsi_from(avg_gain, avg_loss)

    # Wilder's smoothing: avg = (prev_avg * (period-1) + current) / period
    for i in range(period + 1, n):
        change = _to_float(closes[i]) - _to_float(closes[i - 1])
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


# ---------------------------------------------------------------------------
# MACD
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MacdSeries:
    macd: list[float | None]
    signal: list[float | None]
    histogram: list[float | None]


def macd(
    closes: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> MacdSeries:
    """MACD line, signal line, and histogram.

    macd       = EMA(fast) - EMA(slow)
    signal     = EMA(macd, signal)
    histogram  = macd - signal
    """
    if fast <= 0 or slow <= 0 or signal <= 0:
        raise ValueError("periods must be > 0")
    if fast >= slow:
        raise ValueError("fast period must be < slow period")
    fast_e = ema(closes, fast)
    slow_e = ema(closes, slow)
    macd_line: list[float | None] = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_e, slow_e, strict=True)
    ]
    # Build signal EMA over the slice where macd_line is not None.
    first_idx = next(
        (i for i, v in enumerate(macd_line) if v is not None),
        len(macd_line),
    )
    macd_clean = [v for v in macd_line[first_idx:] if v is not None]
    signal_inner = ema(macd_clean, signal)
    signal_line: list[float | None] = [None] * len(macd_line)
    for j, val in enumerate(signal_inner):
        signal_line[first_idx + j] = val
    hist: list[float | None] = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(macd_line, signal_line, strict=True)
    ]
    return MacdSeries(macd=macd_line, signal=signal_line, histogram=hist)


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BollingerSeries:
    upper: list[float | None]
    middle: list[float | None]
    lower: list[float | None]


def bollinger(
    closes: Sequence[float],
    period: int = 20,
    stddev: float = 2.0,
) -> BollingerSeries:
    """Bollinger Bands: SMA(period) ± stddev * rolling-std(period)."""
    if period <= 0:
        raise ValueError("period must be > 0")
    middle = sma(closes, period)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        window = [_to_float(closes[j]) for j in range(i - period + 1, i + 1)]
        mean = sum(window) / period
        variance = sum((x - mean) ** 2 for x in window) / period
        std = variance ** 0.5
        upper[i] = mean + stddev * std
        lower[i] = mean - stddev * std
    return BollingerSeries(upper=upper, middle=middle, lower=lower)


# ---------------------------------------------------------------------------
# ATR (Wilder)
# ---------------------------------------------------------------------------


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> list[float | None]:
    """Average True Range using Wilder's smoothing (RMA).

    TR_i = max(high_i - low_i,
               |high_i - close_{i-1}|,
               |low_i  - close_{i-1}|)
    """
    if period <= 0:
        raise ValueError("period must be > 0")
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs/lows/closes must have equal length")
    n = len(closes)
    out: list[float | None] = [None] * n
    if n < period + 1:
        return out

    trs: list[float] = [0.0] * n
    for i in range(1, n):
        h = _to_float(highs[i])
        lo = _to_float(lows[i])
        prev_c = _to_float(closes[i - 1])
        trs[i] = max(h - lo, abs(h - prev_c), abs(lo - prev_c))

    # Seed: simple average of first `period` TRs (indices 1..period).
    seed = sum(trs[1 : period + 1]) / period
    out[period] = seed
    prev = seed
    for i in range(period + 1, n):
        cur = (prev * (period - 1) + trs[i]) / period
        out[i] = cur
        prev = cur
    return out


# ---------------------------------------------------------------------------
# VWAP (intraday — cumulative from start of provided series)
# ---------------------------------------------------------------------------


def vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
) -> list[float | None]:
    """Volume-Weighted Average Price (cumulative from index 0).

    Typical price = (H + L + C) / 3.
    Pass an intraday series (e.g. 5-minute bars for one trading day) and
    the result at index `i` is VWAP from open through bar `i`.
    """
    if not (len(highs) == len(lows) == len(closes) == len(volumes)):
        raise ValueError("highs/lows/closes/volumes must have equal length")
    n = len(closes)
    out: list[float | None] = []
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(n):
        tp = (_to_float(highs[i]) + _to_float(lows[i]) + _to_float(closes[i])) / 3.0
        v = _to_float(volumes[i])
        cum_pv += tp * v
        cum_v += v
        out.append(cum_pv / cum_v if cum_v > 0 else None)
    return out


# ---------------------------------------------------------------------------
# Swing highs / lows (auto S/R)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SwingLevel:
    index: int
    price: float
    kind: str  # "high" | "low"


def swing_levels(
    highs: Sequence[float],
    lows: Sequence[float],
    lookback: int = 5,
) -> list[SwingLevel]:
    """Detect swing-high / swing-low pivots.

    A bar `i` is a swing high if `highs[i]` is strictly greater than every
    high in `[i - lookback, i + lookback]` excluding itself. Mirror for
    lows. The endpoints (where the lookback window can't be filled) are
    skipped.

    Use the latest few swing highs/lows as auto support/resistance.
    """
    if lookback <= 0:
        raise ValueError("lookback must be > 0")
    if len(highs) != len(lows):
        raise ValueError("highs/lows must have equal length")
    n = len(highs)
    out: list[SwingLevel] = []
    for i in range(lookback, n - lookback):
        h_i = _to_float(highs[i])
        l_i = _to_float(lows[i])
        is_swing_high = all(
            _to_float(highs[j]) < h_i
            for j in range(i - lookback, i + lookback + 1)
            if j != i
        )
        if is_swing_high:
            out.append(SwingLevel(index=i, price=h_i, kind="high"))
            continue  # a single bar can't be both
        is_swing_low = all(
            _to_float(lows[j]) > l_i
            for j in range(i - lookback, i + lookback + 1)
            if j != i
        )
        if is_swing_low:
            out.append(SwingLevel(index=i, price=l_i, kind="low"))
    return out


# ---------------------------------------------------------------------------
# Convenience: read the latest valid value from a series
# ---------------------------------------------------------------------------


def latest(series: Sequence[float | None]) -> float | None:
    """Return the last non-None value, or None if all None."""
    for v in reversed(series):
        if v is not None:
            return v
    return None
