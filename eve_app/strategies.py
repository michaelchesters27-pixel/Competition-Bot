from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

from .indicators import build_feature_rows, finite

SignalFn = Callable[[list[dict[str, Any]], int], dict[str, Any] | None]


@dataclass(frozen=True)
class CandidateStrategy:
    name: str
    label: str
    max_hold_bars: int
    signal_fn: SignalFn


def _ready(row: dict[str, Any], *keys: str) -> bool:
    return all(row.get(k) is not None for k in keys)


def _recent_release(rows: list[dict[str, Any]], i: int, lookback: int = 3) -> bool:
    start = max(1, i - lookback + 1)
    for j in range(start, i + 1):
        if rows[j - 1].get("squeeze_on") is True and rows[j].get("squeeze_on") is False:
            return True
    return False


def squeeze_release_t3(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
    r = rows[i]
    if not _ready(
        r,
        "t3_fast",
        "t3_slow",
        "t3_fast_slope",
        "momentum",
        "momentum_delta",
        "chaikin_volatility",
        "change_of_volatility",
        "atr",
    ):
        return None
    if not _recent_release(rows, i, 3):
        return None
    close = float(r["close"])
    fast = float(r["t3_fast"])
    slow = float(r["t3_slow"])
    slope = float(r["t3_fast_slope"])
    mom = float(r["momentum"])
    mom_d = float(r["momentum_delta"])
    cv = float(r["chaikin_volatility"])
    cov = float(r["change_of_volatility"])

    if close > fast > slow and slope > 0 and mom > 0 and mom_d > 0 and cv > -5 and cov > -10:
        return {
            "action": "BUY",
            "sl_atr": 1.05,
            "tp_atr": 1.85,
            "confidence": 82 + min(10, max(0, cov) * 0.2),
            "reasons": [
                "Bollinger/Keltner squeeze released",
                "T3 trend aligned upward",
                "Bullish momentum is accelerating",
                "Volatility is supporting expansion",
            ],
        }
    if close < fast < slow and slope < 0 and mom < 0 and mom_d < 0 and cv > -5 and cov > -10:
        return {
            "action": "SELL",
            "sl_atr": 1.05,
            "tp_atr": 1.85,
            "confidence": 82 + min(10, max(0, cov) * 0.2),
            "reasons": [
                "Bollinger/Keltner squeeze released",
                "T3 trend aligned downward",
                "Bearish momentum is accelerating",
                "Volatility is supporting expansion",
            ],
        }
    return None


def t3_pullback_resume(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
    if i < 2:
        return None
    r = rows[i]
    p = rows[i - 1]
    if not _ready(
        r,
        "t3_fast",
        "t3_slow",
        "t3_fast_slope",
        "t3_slow_slope",
        "momentum",
        "momentum_delta",
        "chaikin_delta",
        "atr",
    ) or p.get("t3_fast") is None:
        return None
    atr = float(r["atr"])
    if atr <= 0:
        return None
    fast = float(r["t3_fast"])
    slow = float(r["t3_slow"])
    prior_fast = float(p["t3_fast"])
    close = float(r["close"])
    open_ = float(r["open"])
    prior_low = float(p["low"])
    prior_high = float(p["high"])
    mom = float(r["momentum"])
    mom_d = float(r["momentum_delta"])
    cv_d = float(r["chaikin_delta"])

    bullish_touch = prior_low <= prior_fast + 0.15 * atr and float(p["close"]) >= slow - 0.25 * atr
    bearish_touch = prior_high >= prior_fast - 0.15 * atr and float(p["close"]) <= slow + 0.25 * atr

    if (
        fast > slow
        and float(r["t3_fast_slope"]) > 0
        and float(r["t3_slow_slope"]) >= 0
        and bullish_touch
        and close > open_
        and close > fast
        and mom > 0
        and mom_d > 0
        and cv_d >= -2
    ):
        return {
            "action": "BUY",
            "sl_atr": 0.95,
            "tp_atr": 1.55,
            "confidence": 78 + min(12, float(r["body_ratio"]) * 12),
            "reasons": [
                "Price pulled back into the rising T3",
                "Bullish candle reclaimed the fast T3",
                "Momentum resumed with the trend",
                "Chaikin volatility is stable or rising",
            ],
        }
    if (
        fast < slow
        and float(r["t3_fast_slope"]) < 0
        and float(r["t3_slow_slope"]) <= 0
        and bearish_touch
        and close < open_
        and close < fast
        and mom < 0
        and mom_d < 0
        and cv_d >= -2
    ):
        return {
            "action": "SELL",
            "sl_atr": 0.95,
            "tp_atr": 1.55,
            "confidence": 78 + min(12, float(r["body_ratio"]) * 12),
            "reasons": [
                "Price pulled back into the falling T3",
                "Bearish candle rejected the fast T3",
                "Momentum resumed with the trend",
                "Chaikin volatility is stable or rising",
            ],
        }
    return None


def volatility_structure_break(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
    r = rows[i]
    if not _ready(r, "momentum", "momentum_delta", "chaikin_delta", "change_of_volatility", "atr"):
        return None
    body = float(r["body_ratio"])
    cov = float(r["change_of_volatility"])
    cv_d = float(r["chaikin_delta"])
    mom = float(r["momentum"])
    mom_d = float(r["momentum_delta"])

    if r["breakout_up"] and body >= 0.55 and cov > 0 and cv_d > 0 and mom > 0 and mom_d >= 0:
        return {
            "action": "BUY",
            "sl_atr": 1.10,
            "tp_atr": 2.00,
            "confidence": 80 + min(12, body * 10 + min(cov, 10) * 0.2),
            "reasons": [
                "M5 close broke recent structure high",
                "Breakout candle has a decisive body",
                "Change of Volatility is positive",
                "Chaikin volatility and momentum are expanding",
            ],
        }
    if r["breakout_down"] and body >= 0.55 and cov > 0 and cv_d > 0 and mom < 0 and mom_d <= 0:
        return {
            "action": "SELL",
            "sl_atr": 1.10,
            "tp_atr": 2.00,
            "confidence": 80 + min(12, body * 10 + min(cov, 10) * 0.2),
            "reasons": [
                "M5 close broke recent structure low",
                "Breakout candle has a decisive body",
                "Change of Volatility is positive",
                "Chaikin volatility and momentum are expanding",
            ],
        }
    return None


def t3_momentum_continuation(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
    if i < 1:
        return None
    r = rows[i]
    p = rows[i - 1]
    if not _ready(
        r,
        "t3_fast",
        "t3_slow",
        "t3_fast_slope",
        "momentum",
        "momentum_delta",
        "chaikin_volatility",
        "change_of_volatility",
        "atr",
    ):
        return None
    fast = float(r["t3_fast"])
    slow = float(r["t3_slow"])
    close = float(r["close"])
    body = float(r["body_ratio"])
    mom = float(r["momentum"])
    mom_d = float(r["momentum_delta"])
    cv = float(r["chaikin_volatility"])
    cov = float(r["change_of_volatility"])

    if (
        fast > slow
        and float(r["t3_fast_slope"]) > 0
        and close > fast
        and close > float(p["high"])
        and body >= 0.5
        and mom > 0
        and mom_d > 0
        and cv > -8
        and cov > -8
    ):
        return {
            "action": "BUY",
            "sl_atr": 0.90,
            "tp_atr": 1.45,
            "confidence": 76 + min(14, body * 12),
            "reasons": [
                "Fast and slow T3 are bullish",
                "Price closed above the prior M5 high",
                "Momentum is positive and strengthening",
                "Volatility is not contracting sharply",
            ],
        }
    if (
        fast < slow
        and float(r["t3_fast_slope"]) < 0
        and close < fast
        and close < float(p["low"])
        and body >= 0.5
        and mom < 0
        and mom_d < 0
        and cv > -8
        and cov > -8
    ):
        return {
            "action": "SELL",
            "sl_atr": 0.90,
            "tp_atr": 1.45,
            "confidence": 76 + min(14, body * 12),
            "reasons": [
                "Fast and slow T3 are bearish",
                "Price closed below the prior M5 low",
                "Momentum is negative and strengthening",
                "Volatility is not contracting sharply",
            ],
        }
    return None


def squeeze_reversion(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
    if i < 2:
        return None
    r = rows[i]
    p = rows[i - 1]
    if not _ready(r, "t3_fast", "momentum", "momentum_delta", "atr"):
        return None
    if r.get("squeeze_on") is not True:
        return None
    atr = float(r["atr"])
    if atr <= 0:
        return None
    fast = float(r["t3_fast"])
    close = float(r["close"])
    open_ = float(r["open"])
    distance = close - fast
    mom = float(r["momentum"])
    mom_d = float(r["momentum_delta"])

    if distance < -1.15 * atr and close > open_ and mom < 0 and mom_d > 0 and close > float(p["close"]):
        tp_atr = max(0.8, min(1.4, abs(distance) / atr * 0.8))
        return {
            "action": "BUY",
            "sl_atr": 0.80,
            "tp_atr": tp_atr,
            "confidence": 72 + min(14, abs(distance) / atr * 5),
            "reasons": [
                "Volatility remains compressed",
                "Price is stretched below the T3",
                "Bearish momentum is weakening",
                "A bullish reversal candle has formed",
            ],
        }
    if distance > 1.15 * atr and close < open_ and mom > 0 and mom_d < 0 and close < float(p["close"]):
        tp_atr = max(0.8, min(1.4, abs(distance) / atr * 0.8))
        return {
            "action": "SELL",
            "sl_atr": 0.80,
            "tp_atr": tp_atr,
            "confidence": 72 + min(14, abs(distance) / atr * 5),
            "reasons": [
                "Volatility remains compressed",
                "Price is stretched above the T3",
                "Bullish momentum is weakening",
                "A bearish reversal candle has formed",
            ],
        }
    return None


CANDIDATES: list[CandidateStrategy] = [
    CandidateStrategy("squeeze_release_t3", "T3 Squeeze Release", 8, squeeze_release_t3),
    CandidateStrategy("t3_pullback_resume", "T3 Pullback Resume", 7, t3_pullback_resume),
    CandidateStrategy("volatility_structure_break", "Volatility Structure Break", 8, volatility_structure_break),
    CandidateStrategy("t3_momentum_continuation", "T3 Momentum Continuation", 6, t3_momentum_continuation),
    CandidateStrategy("squeeze_reversion", "Squeeze Mean Reversion", 5, squeeze_reversion),
]


def candidate_by_name(name: str) -> CandidateStrategy:
    for candidate in CANDIDATES:
        if candidate.name == name:
            return candidate
    raise KeyError(name)


def _trade_result(
    rows: list[dict[str, Any]],
    signal_index: int,
    signal: dict[str, Any],
    max_hold_bars: int,
) -> tuple[float, int, float, float]:
    entry_index = signal_index + 1
    if entry_index >= len(rows):
        return 0.0, entry_index, 0.0, 0.0
    entry = float(rows[entry_index]["open"])
    atr_value = finite(rows[signal_index].get("atr"))
    if atr_value <= 0:
        return 0.0, entry_index, 0.0, 0.0

    sl_distance = atr_value * float(signal["sl_atr"])
    tp_distance = atr_value * float(signal["tp_atr"])
    action = signal["action"]
    sl = entry - sl_distance if action == "BUY" else entry + sl_distance
    tp = entry + tp_distance if action == "BUY" else entry - tp_distance
    reward_r = tp_distance / sl_distance
    last_index = min(len(rows) - 1, entry_index + max_hold_bars - 1)
    mfe_r = 0.0
    mae_r = 0.0

    for j in range(entry_index, last_index + 1):
        high = float(rows[j]["high"])
        low = float(rows[j]["low"])
        if action == "BUY":
            mfe_r = max(mfe_r, (high - entry) / sl_distance)
            mae_r = min(mae_r, (low - entry) / sl_distance)
            if low <= sl:
                return -1.0, j, mfe_r, mae_r
            if high >= tp:
                return reward_r, j, mfe_r, mae_r
        else:
            mfe_r = max(mfe_r, (entry - low) / sl_distance)
            mae_r = min(mae_r, (entry - high) / sl_distance)
            if high >= sl:
                return -1.0, j, mfe_r, mae_r
            if low <= tp:
                return reward_r, j, mfe_r, mae_r

    final_close = float(rows[last_index]["close"])
    result_r = (final_close - entry) / sl_distance if action == "BUY" else (entry - final_close) / sl_distance
    return result_r, last_index, mfe_r, mae_r


def backtest_candidate(candidate: CandidateStrategy, rows: list[dict[str, Any]]) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    i = 55
    while i < len(rows) - 1:
        signal = candidate.signal_fn(rows, i)
        if signal is None:
            i += 1
            continue
        result_r, exit_index, mfe_r, mae_r = _trade_result(rows, i, signal, candidate.max_hold_bars)
        trades.append(
            {
                "signal_time": int(rows[i]["time"]),
                "action": signal["action"],
                "result_r": result_r,
                "mfe_r": mfe_r,
                "mae_r": mae_r,
                "confidence": signal["confidence"],
            }
        )
        i = max(i + 1, exit_index + 1)

    wins = [t for t in trades if t["result_r"] > 0]
    losses = [t for t in trades if t["result_r"] < 0]
    gross_profit = sum(t["result_r"] for t in wins)
    gross_loss = abs(sum(t["result_r"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
    net_r = sum(t["result_r"] for t in trades)
    avg_r = net_r / len(trades) if trades else 0.0
    win_rate = len(wins) / len(trades) * 100.0 if trades else 0.0

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in trades:
        equity += trade["result_r"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    recent = trades[-8:]
    recent_r = sum(t["result_r"] for t in recent)
    sample_factor = min(1.0, len(trades) / 8.0)
    score = (
        net_r * 7.0
        + avg_r * 18.0
        + min(profit_factor, 3.0) * 10.0
        + (win_rate - 50.0) * 0.22
        + recent_r * 3.0
        - max_drawdown * 5.0
    ) * sample_factor
    if not trades:
        score = -100.0
    elif len(trades) < 3:
        score -= 18.0

    return {
        "name": candidate.name,
        "label": candidate.label,
        "score": round(score, 3),
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(win_rate, 2),
        "profit_factor": round(profit_factor, 3),
        "net_r": round(net_r, 3),
        "average_r": round(avg_r, 3),
        "max_drawdown_r": round(max_drawdown, 3),
        "recent_r": round(recent_r, 3),
        "max_hold_bars": candidate.max_hold_bars,
        "trade_examples": trades[-5:],
    }


def evaluate_candidates(bars: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows = build_feature_rows(bars)
    results = [backtest_candidate(candidate, rows) for candidate in CANDIDATES]
    results.sort(key=lambda item: item["score"], reverse=True)
    return results, rows


def live_signal(
    strategy_name: str,
    bars: list[dict[str, Any]],
    bid: float,
    ask: float,
) -> dict[str, Any] | None:
    rows = build_feature_rows(bars)
    if len(rows) < 60:
        return None
    candidate = candidate_by_name(strategy_name)
    i = len(rows) - 2  # only the last fully closed M5 candle
    signal = candidate.signal_fn(rows, i)
    if signal is None:
        return None
    atr_value = finite(rows[i].get("atr"))
    if atr_value <= 0:
        return None
    action = signal["action"]
    entry = ask if action == "BUY" else bid
    sl_distance = atr_value * float(signal["sl_atr"])
    tp_distance = atr_value * float(signal["tp_atr"])
    sl = entry - sl_distance if action == "BUY" else entry + sl_distance
    tp = entry + tp_distance if action == "BUY" else entry - tp_distance

    snapshot_keys = [
        "time",
        "open",
        "high",
        "low",
        "close",
        "t3_fast",
        "t3_slow",
        "t3_fast_slope",
        "t3_slow_slope",
        "squeeze_on",
        "momentum",
        "momentum_delta",
        "chaikin_volatility",
        "chaikin_delta",
        "change_of_volatility",
        "atr",
        "body_ratio",
        "breakout_up",
        "breakout_down",
    ]
    snapshot = {key: rows[i].get(key) for key in snapshot_keys}
    return {
        **signal,
        "bar_time": int(rows[i]["time"]),
        "entry_reference": entry,
        "sl": sl,
        "tp": tp,
        "indicator_snapshot": snapshot,
        "strategy_label": candidate.label,
    }
