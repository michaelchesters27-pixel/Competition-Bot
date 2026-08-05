from __future__ import annotations

import math
from statistics import pstdev
from typing import Any

from .memory import bounded_tail


def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, value in enumerate(values):
        running += value
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    if not values or period <= 0:
        return out
    alpha = 2.0 / (period + 1.0)
    seed_count = min(period, len(values))
    seed = sum(values[:seed_count]) / seed_count
    current = seed
    for i, value in enumerate(values):
        if i == 0:
            current = value
        else:
            current = alpha * value + (1.0 - alpha) * current
        if i >= period - 1:
            out[i] = current
    return out


def ema_nullable(values: list[float | None], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    clean_idx = [i for i, v in enumerate(values) if v is not None]
    if not clean_idx:
        return out
    alpha = 2.0 / (period + 1.0)
    current: float | None = None
    seen = 0
    for i, value in enumerate(values):
        if value is None:
            continue
        seen += 1
        current = value if current is None else alpha * value + (1.0 - alpha) * current
        if seen >= period:
            out[i] = current
    return out


def true_range(bars: list[dict[str, Any]]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        if prev_close is None:
            out.append(high - low)
        else:
            out.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return out


def atr(bars: list[dict[str, Any]], period: int = 14) -> list[float | None]:
    return ema(true_range(bars), period)


def tillson_t3(closes: list[float], period: int = 8, volume_factor: float = 0.7) -> list[float | None]:
    e1 = ema(closes, period)
    e2 = ema_nullable(e1, period)
    e3 = ema_nullable(e2, period)
    e4 = ema_nullable(e3, period)
    e5 = ema_nullable(e4, period)
    e6 = ema_nullable(e5, period)

    a = volume_factor
    c1 = -(a**3)
    c2 = 3 * (a**2) + 3 * (a**3)
    c3 = -6 * (a**2) - 3 * a - 3 * (a**3)
    c4 = 1 + 3 * a + 3 * (a**2) + (a**3)
    out: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if None in (e3[i], e4[i], e5[i], e6[i]):
            continue
        out[i] = c1 * float(e6[i]) + c2 * float(e5[i]) + c3 * float(e4[i]) + c4 * float(e3[i])
    return out


def rolling_std(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = pstdev(values[i - period + 1 : i + 1])
    return out


def rolling_high(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = max(values[i - period + 1 : i + 1])
    return out


def rolling_low(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = min(values[i - period + 1 : i + 1])
    return out


def linreg_endpoint(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    x = list(range(period))
    sx = sum(x)
    sxx = sum(v * v for v in x)
    den = period * sxx - sx * sx
    if den == 0:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        sy = sum(window)
        sxy = sum(j * y for j, y in zip(x, window))
        slope = (period * sxy - sx * sy) / den
        intercept = (sy - slope * sx) / period
        out[i] = intercept + slope * (period - 1)
    return out


def squeeze_momentum(
    bars: list[dict[str, Any]],
    length: int = 20,
    bb_mult: float = 2.0,
    kc_mult: float = 1.5,
) -> tuple[list[bool | None], list[float | None]]:
    closes = [float(b["close"]) for b in bars]
    highs = [float(b["high"]) for b in bars]
    lows = [float(b["low"]) for b in bars]

    basis = sma(closes, length)
    std = rolling_std(closes, length)
    kc_basis = ema(closes, length)
    range_ema = ema(true_range(bars), length)
    hh = rolling_high(highs, length)
    ll = rolling_low(lows, length)
    close_sma = sma(closes, length)

    squeeze_on: list[bool | None] = [None] * len(bars)
    raw_momentum = [0.0] * len(bars)
    raw_ready = [False] * len(bars)

    for i in range(len(bars)):
        if None not in (basis[i], std[i], kc_basis[i], range_ema[i]):
            bb_upper = float(basis[i]) + bb_mult * float(std[i])
            bb_lower = float(basis[i]) - bb_mult * float(std[i])
            kc_upper = float(kc_basis[i]) + kc_mult * float(range_ema[i])
            kc_lower = float(kc_basis[i]) - kc_mult * float(range_ema[i])
            squeeze_on[i] = bb_lower > kc_lower and bb_upper < kc_upper

        if None not in (hh[i], ll[i], close_sma[i]):
            midpoint = ((float(hh[i]) + float(ll[i])) / 2.0 + float(close_sma[i])) / 2.0
            raw_momentum[i] = closes[i] - midpoint
            raw_ready[i] = True

    momentum = linreg_endpoint(raw_momentum, length)
    for i, ready in enumerate(raw_ready):
        if not ready:
            momentum[i] = None
    return squeeze_on, momentum


def chaikin_volatility(
    bars: list[dict[str, Any]], ema_period: int = 10, roc_period: int = 10
) -> list[float | None]:
    ranges = [float(b["high"]) - float(b["low"]) for b in bars]
    smoothed = ema(ranges, ema_period)
    out: list[float | None] = [None] * len(bars)
    for i in range(roc_period, len(bars)):
        current = smoothed[i]
        previous = smoothed[i - roc_period]
        if current is None or previous in (None, 0):
            continue
        out[i] = ((float(current) - float(previous)) / float(previous)) * 100.0
    return out


def change_of_volatility(
    bars: list[dict[str, Any]], atr_period: int = 14, change_period: int = 5
) -> list[float | None]:
    a = atr(bars, atr_period)
    out: list[float | None] = [None] * len(bars)
    for i in range(change_period, len(bars)):
        current = a[i]
        previous = a[i - change_period]
        if current is None or previous in (None, 0):
            continue
        out[i] = ((float(current) - float(previous)) / float(previous)) * 100.0
    return out


def candle_body_ratio(bar: dict[str, Any]) -> float:
    full = float(bar["high"]) - float(bar["low"])
    if full <= 0:
        return 0.0
    return abs(float(bar["close"]) - float(bar["open"])) / full


def recent_breakout(bars: list[dict[str, Any]], index: int, lookback: int = 6) -> tuple[bool, bool]:
    if index < lookback:
        return False, False
    prior = bars[index - lookback : index]
    prior_high = max(float(b["high"]) for b in prior)
    prior_low = min(float(b["low"]) for b in prior)
    close = float(bars[index]["close"])
    return close > prior_high, close < prior_low


def build_feature_rows(bars: list[dict[str, Any]], max_rows: int | None = None) -> list[dict[str, Any]]:
    bars = bounded_tail(sorted(bars, key=lambda bar: int(bar["time"])), max_rows)
    if not bars:
        return []
    closes = [float(b["close"]) for b in bars]
    t3_fast = tillson_t3(closes, 8, 0.7)
    t3_slow = tillson_t3(closes, 21, 0.7)
    squeeze_on, momentum = squeeze_momentum(bars)
    cv = chaikin_volatility(bars)
    cov = change_of_volatility(bars)
    atr_values = atr(bars)

    rows: list[dict[str, Any]] = []
    for i, bar in enumerate(bars):
        up_break, down_break = recent_breakout(bars, i)
        fast_slope = None
        slow_slope = None
        mom_delta = None
        cv_delta = None
        if i > 0:
            if t3_fast[i] is not None and t3_fast[i - 1] is not None:
                fast_slope = float(t3_fast[i]) - float(t3_fast[i - 1])
            if t3_slow[i] is not None and t3_slow[i - 1] is not None:
                slow_slope = float(t3_slow[i]) - float(t3_slow[i - 1])
            if momentum[i] is not None and momentum[i - 1] is not None:
                mom_delta = float(momentum[i]) - float(momentum[i - 1])
            if cv[i] is not None and cv[i - 1] is not None:
                cv_delta = float(cv[i]) - float(cv[i - 1])

        rows.append(
            {
                **bar,
                "t3_fast": t3_fast[i],
                "t3_slow": t3_slow[i],
                "t3_fast_slope": fast_slope,
                "t3_slow_slope": slow_slope,
                "squeeze_on": squeeze_on[i],
                "momentum": momentum[i],
                "momentum_delta": mom_delta,
                "chaikin_volatility": cv[i],
                "chaikin_delta": cv_delta,
                "change_of_volatility": cov[i],
                "atr": atr_values[i],
                "body_ratio": candle_body_ratio(bar),
                "breakout_up": up_break,
                "breakout_down": down_break,
            }
        )
    return rows


def finite(value: Any, fallback: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return fallback
    return value if math.isfinite(value) else fallback
