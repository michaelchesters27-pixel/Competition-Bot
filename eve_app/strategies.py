from __future__ import annotations

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
    return all(row.get(key) is not None for key in keys)


def _recent_release(rows: list[dict[str, Any]], i: int, lookback: int = 3) -> bool:
    start = max(1, i - lookback + 1)
    return any(
        rows[j - 1].get("squeeze_on") is True and rows[j].get("squeeze_on") is False
        for j in range(start, i + 1)
    )


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
    ) or not _recent_release(rows, i, 3):
        return None

    close = float(r["close"])
    fast = float(r["t3_fast"])
    slow = float(r["t3_slow"])
    slope = float(r["t3_fast_slope"])
    momentum = float(r["momentum"])
    momentum_delta = float(r["momentum_delta"])
    cov = float(r["change_of_volatility"])

    if close > fast > slow and slope > 0 and momentum > 0 and momentum_delta >= 0 and cov > -15:
        return {
            "action": "BUY",
            "sl_atr": 1.00,
            "tp_atr": 1.65,
            "confidence": 78 + min(10, max(0.0, cov) * 0.25),
            "reasons": [
                "Bollinger/Keltner squeeze released",
                "Fast and slow T3 are aligned upward",
                "Momentum is bullish at the release",
                "Volatility is not collapsing",
            ],
        }
    if close < fast < slow and slope < 0 and momentum < 0 and momentum_delta <= 0 and cov > -15:
        return {
            "action": "SELL",
            "sl_atr": 1.00,
            "tp_atr": 1.65,
            "confidence": 78 + min(10, max(0.0, cov) * 0.25),
            "reasons": [
                "Bollinger/Keltner squeeze released",
                "Fast and slow T3 are aligned downward",
                "Momentum is bearish at the release",
                "Volatility is not collapsing",
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
        "momentum",
        "momentum_delta",
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
    momentum = float(r["momentum"])
    momentum_delta = float(r["momentum_delta"])
    body = float(r["body_ratio"])

    bullish_touch = float(p["low"]) <= prior_fast + 0.28 * atr and float(p["close"]) >= slow - 0.40 * atr
    bearish_touch = float(p["high"]) >= prior_fast - 0.28 * atr and float(p["close"]) <= slow + 0.40 * atr

    if (
        fast > slow
        and float(r["t3_fast_slope"]) > 0
        and bullish_touch
        and close > open_
        and close >= fast - 0.08 * atr
        and momentum > 0
        and momentum_delta >= -0.08 * atr
        and body >= 0.24
    ):
        return {
            "action": "BUY",
            "sl_atr": 0.90,
            "tp_atr": 1.40,
            "confidence": 70 + min(16, body * 18),
            "reasons": [
                "Price pulled back into a rising T3 structure",
                "The closed M5 candle turned back upward",
                "Momentum remains on the bullish side",
                "The pullback did not break the slow T3 trend",
            ],
        }
    if (
        fast < slow
        and float(r["t3_fast_slope"]) < 0
        and bearish_touch
        and close < open_
        and close <= fast + 0.08 * atr
        and momentum < 0
        and momentum_delta <= 0.08 * atr
        and body >= 0.24
    ):
        return {
            "action": "SELL",
            "sl_atr": 0.90,
            "tp_atr": 1.40,
            "confidence": 70 + min(16, body * 18),
            "reasons": [
                "Price pulled back into a falling T3 structure",
                "The closed M5 candle turned back downward",
                "Momentum remains on the bearish side",
                "The pullback did not break the slow T3 trend",
            ],
        }
    return None


def volatility_structure_break(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
    r = rows[i]
    if not _ready(r, "momentum", "momentum_delta", "change_of_volatility", "atr"):
        return None
    body = float(r["body_ratio"])
    cov = float(r["change_of_volatility"])
    momentum = float(r["momentum"])
    momentum_delta = float(r["momentum_delta"])

    if r["breakout_up"] and body >= 0.38 and cov > -12 and momentum > 0 and momentum_delta >= -0.02:
        return {
            "action": "BUY",
            "sl_atr": 1.00,
            "tp_atr": 1.70,
            "confidence": 73 + min(15, body * 15 + max(0.0, cov) * 0.15),
            "reasons": [
                "M5 close broke the recent structure high",
                "The breakout candle closed with a useful body",
                "Momentum supports the upside break",
                "Volatility is adequate for continuation",
            ],
        }
    if r["breakout_down"] and body >= 0.38 and cov > -12 and momentum < 0 and momentum_delta <= 0.02:
        return {
            "action": "SELL",
            "sl_atr": 1.00,
            "tp_atr": 1.70,
            "confidence": 73 + min(15, body * 15 + max(0.0, cov) * 0.15),
            "reasons": [
                "M5 close broke the recent structure low",
                "The breakout candle closed with a useful body",
                "Momentum supports the downside break",
                "Volatility is adequate for continuation",
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
        "change_of_volatility",
        "atr",
    ):
        return None

    fast = float(r["t3_fast"])
    slow = float(r["t3_slow"])
    close = float(r["close"])
    body = float(r["body_ratio"])
    momentum = float(r["momentum"])
    momentum_delta = float(r["momentum_delta"])
    cov = float(r["change_of_volatility"])

    if (
        fast > slow
        and float(r["t3_fast_slope"]) > 0
        and close > fast
        and close > float(p["close"])
        and body >= 0.28
        and momentum > 0
        and momentum_delta >= -0.03
        and cov > -18
    ):
        return {
            "action": "BUY",
            "sl_atr": 0.85,
            "tp_atr": 1.30,
            "confidence": 68 + min(17, body * 20),
            "reasons": [
                "Fast and slow T3 are bullish",
                "The latest M5 candle continued upward",
                "Price remains above the fast T3",
                "Momentum remains positive",
            ],
        }
    if (
        fast < slow
        and float(r["t3_fast_slope"]) < 0
        and close < fast
        and close < float(p["close"])
        and body >= 0.28
        and momentum < 0
        and momentum_delta <= 0.03
        and cov > -18
    ):
        return {
            "action": "SELL",
            "sl_atr": 0.85,
            "tp_atr": 1.30,
            "confidence": 68 + min(17, body * 20),
            "reasons": [
                "Fast and slow T3 are bearish",
                "The latest M5 candle continued downward",
                "Price remains below the fast T3",
                "Momentum remains negative",
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
    momentum = float(r["momentum"])
    momentum_delta = float(r["momentum_delta"])

    if distance < -0.85 * atr and close > open_ and momentum_delta > 0 and close > float(p["close"]):
        return {
            "action": "BUY",
            "sl_atr": 0.78,
            "tp_atr": 1.05,
            "confidence": 66 + min(17, abs(distance) / atr * 8),
            "reasons": [
                "Volatility is compressed",
                "Price is stretched below the fast T3",
                "Bearish momentum is losing force",
                "A bullish M5 reversal candle closed",
            ],
        }
    if distance > 0.85 * atr and close < open_ and momentum_delta < 0 and close < float(p["close"]):
        return {
            "action": "SELL",
            "sl_atr": 0.78,
            "tp_atr": 1.05,
            "confidence": 66 + min(17, abs(distance) / atr * 8),
            "reasons": [
                "Volatility is compressed",
                "Price is stretched above the fast T3",
                "Bullish momentum is losing force",
                "A bearish M5 reversal candle closed",
            ],
        }
    return None


def adaptive_directional_scalp(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
    """Broad M5 evidence model used to prevent a single rare trigger from controlling the hour."""
    if i < 2:
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
        "change_of_volatility",
        "atr",
    ):
        return None

    atr = float(r["atr"])
    if atr <= 0:
        return None

    fast = float(r["t3_fast"])
    slow = float(r["t3_slow"])
    close = float(r["close"])
    open_ = float(r["open"])
    body = float(r["body_ratio"])
    momentum = float(r["momentum"])
    momentum_delta = float(r["momentum_delta"])
    cov = float(r["change_of_volatility"])
    cv_delta = finite(r.get("chaikin_delta"))

    buy = 0.0
    sell = 0.0
    buy_reasons: list[str] = []
    sell_reasons: list[str] = []

    if fast > slow:
        buy += 1.50
        buy_reasons.append("Fast T3 is above slow T3")
    elif fast < slow:
        sell += 1.50
        sell_reasons.append("Fast T3 is below slow T3")

    if float(r["t3_fast_slope"]) > 0:
        buy += 1.00
        buy_reasons.append("Fast T3 slope is rising")
    elif float(r["t3_fast_slope"]) < 0:
        sell += 1.00
        sell_reasons.append("Fast T3 slope is falling")

    if close > fast:
        buy += 0.75
    elif close < fast:
        sell += 0.75

    if momentum > 0:
        buy += 1.25
        buy_reasons.append("Squeeze momentum is above zero")
    elif momentum < 0:
        sell += 1.25
        sell_reasons.append("Squeeze momentum is below zero")

    if momentum_delta > 0:
        buy += 0.75
    elif momentum_delta < 0:
        sell += 0.75

    if close > open_:
        buy += 0.70 + min(0.55, body)
        buy_reasons.append("The latest M5 candle closed bullish")
    elif close < open_:
        sell += 0.70 + min(0.55, body)
        sell_reasons.append("The latest M5 candle closed bearish")

    if close > float(p["high"]):
        buy += 0.70
        buy_reasons.append("Price closed above the prior M5 high")
    if close < float(p["low"]):
        sell += 0.70
        sell_reasons.append("Price closed below the prior M5 low")

    if r.get("breakout_up"):
        buy += 0.80
    if r.get("breakout_down"):
        sell += 0.80

    if cov > 0:
        if close >= open_:
            buy += 0.35
        else:
            sell += 0.35
    if cv_delta > 0:
        if close >= open_:
            buy += 0.25
        else:
            sell += 0.25

    edge = abs(buy - sell)
    best = max(buy, sell)
    if best < 4.00 or edge < 1.20 or body < 0.18:
        return None

    action = "BUY" if buy > sell else "SELL"
    reasons = buy_reasons if action == "BUY" else sell_reasons
    confidence = min(88.0, 55.0 + best * 4.4 + edge * 1.5)
    # A slightly closer target is intentional for the broad fallback setup.
    return {
        "action": action,
        "sl_atr": 0.88,
        "tp_atr": 1.22,
        "confidence": confidence,
        "reasons": reasons[:5]
        + [f"Directional evidence score: {best:.2f} versus {min(buy, sell):.2f}"],
        "evidence_scores": {"buy": round(buy, 3), "sell": round(sell, 3), "edge": round(edge, 3)},
    }


CANDIDATES: list[CandidateStrategy] = [
    CandidateStrategy("adaptive_directional_scalp", "Adaptive Directional Scalp", 5, adaptive_directional_scalp),
    CandidateStrategy("t3_pullback_resume", "T3 Pullback Resume", 7, t3_pullback_resume),
    CandidateStrategy("t3_momentum_continuation", "T3 Momentum Continuation", 6, t3_momentum_continuation),
    CandidateStrategy("volatility_structure_break", "Volatility Structure Break", 8, volatility_structure_break),
    CandidateStrategy("squeeze_release_t3", "T3 Squeeze Release", 8, squeeze_release_t3),
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
    end_index: int | None = None,
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
    action = str(signal["action"])
    sl = entry - sl_distance if action == "BUY" else entry + sl_distance
    tp = entry + tp_distance if action == "BUY" else entry - tp_distance
    reward_r = tp_distance / sl_distance
    last_index = min(len(rows) - 1, entry_index + max_hold_bars - 1)
    if end_index is not None:
        last_index = min(last_index, end_index)
    if last_index < entry_index:
        return 0.0, entry_index, 0.0, 0.0
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


def _research_indices(rows: list[dict[str, Any]], start_time: int, end_time: int) -> list[int]:
    return [
        i
        for i, row in enumerate(rows)
        if i >= 55 and int(row["time"]) >= start_time and int(row["time"]) + 300 <= end_time
    ]


def research_regime(rows: list[dict[str, Any]], start_time: int, end_time: int) -> dict[str, Any]:
    indices = _research_indices(rows, start_time, end_time)
    if len(indices) < 3:
        indices = list(range(max(55, len(rows) - 14), max(55, len(rows) - 1)))
    selected = [rows[i] for i in indices if i < len(rows)]
    if not selected:
        return {"name": "UNCLASSIFIED", "net_atr": 0.0, "squeeze_fraction": 0.0, "trend_alignment": 0.0}

    atr_values = [finite(row.get("atr")) for row in selected if finite(row.get("atr")) > 0]
    average_atr = sum(atr_values) / len(atr_values) if atr_values else 1.0
    net_move = float(selected[-1]["close"]) - float(selected[0]["open"])
    net_atr = net_move / average_atr if average_atr else 0.0
    bullish_alignment = sum(1 for row in selected if finite(row.get("t3_fast")) > finite(row.get("t3_slow"))) / len(selected)
    bearish_alignment = sum(1 for row in selected if finite(row.get("t3_fast")) < finite(row.get("t3_slow"))) / len(selected)
    trend_alignment = max(bullish_alignment, bearish_alignment)
    squeeze_values = [row.get("squeeze_on") for row in selected if row.get("squeeze_on") is not None]
    squeeze_fraction = sum(1 for value in squeeze_values if value) / len(squeeze_values) if squeeze_values else 0.0
    cov_values = [finite(row.get("change_of_volatility")) for row in selected]
    average_cov = sum(cov_values) / len(cov_values) if cov_values else 0.0
    average_body = sum(float(row.get("body_ratio") or 0.0) for row in selected) / len(selected)

    if abs(net_atr) >= 1.10 and trend_alignment >= 0.62:
        name = "TREND_UP" if net_atr > 0 else "TREND_DOWN"
    elif squeeze_fraction >= 0.45:
        name = "COMPRESSION"
    elif average_cov >= 3.0 or average_body >= 0.48:
        name = "EXPANSION"
    else:
        name = "RANGE_MIXED"

    return {
        "name": name,
        "bars_observed": len(selected),
        "net_atr": round(net_atr, 3),
        "trend_alignment": round(trend_alignment, 3),
        "squeeze_fraction": round(squeeze_fraction, 3),
        "average_change_of_volatility": round(average_cov, 3),
        "average_body_ratio": round(average_body, 3),
    }


def _regime_boost(strategy_name: str, regime_name: str) -> float:
    mapping: dict[str, dict[str, float]] = {
        "TREND_UP": {
            "adaptive_directional_scalp": 15,
            "t3_pullback_resume": 14,
            "t3_momentum_continuation": 13,
            "volatility_structure_break": 8,
        },
        "TREND_DOWN": {
            "adaptive_directional_scalp": 15,
            "t3_pullback_resume": 14,
            "t3_momentum_continuation": 13,
            "volatility_structure_break": 8,
        },
        "EXPANSION": {
            "volatility_structure_break": 16,
            "adaptive_directional_scalp": 13,
            "squeeze_release_t3": 11,
            "t3_momentum_continuation": 10,
        },
        "COMPRESSION": {
            "squeeze_release_t3": 16,
            "squeeze_reversion": 13,
            "adaptive_directional_scalp": 8,
        },
        "RANGE_MIXED": {
            "adaptive_directional_scalp": 12,
            "squeeze_reversion": 10,
            "t3_pullback_resume": 7,
        },
    }
    return mapping.get(regime_name, {}).get(strategy_name, 4.0)


def backtest_candidate(
    candidate: CandidateStrategy,
    rows: list[dict[str, Any]],
    start_time: int,
    end_time: int,
    regime_name: str,
) -> dict[str, Any]:
    trades: list[dict[str, Any]] = []
    allowed = set(_research_indices(rows, start_time, end_time))
    last_allowed_index = max(allowed) if allowed else 54
    i = 55
    while i < len(rows) - 1:
        if i not in allowed:
            i += 1
            continue
        signal = candidate.signal_fn(rows, i)
        if signal is None:
            i += 1
            continue
        result_r, exit_index, mfe_r, mae_r = _trade_result(
            rows, i, signal, candidate.max_hold_bars, last_allowed_index
        )
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

    wins = [trade for trade in trades if trade["result_r"] > 0]
    losses = [trade for trade in trades if trade["result_r"] < 0]
    gross_profit = sum(trade["result_r"] for trade in wins)
    gross_loss = abs(sum(trade["result_r"] for trade in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
    net_r = sum(trade["result_r"] for trade in trades)
    average_r = net_r / len(trades) if trades else 0.0
    win_rate = len(wins) / len(trades) * 100.0 if trades else 0.0

    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for trade in trades:
        equity += trade["result_r"]
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    # With only twelve M5 candles in the research hour, sparse signals are expected.
    # Regime fit is therefore part of the score rather than pretending a tiny backtest is conclusive.
    score = _regime_boost(candidate.name, regime_name)
    score += net_r * 8.0 + average_r * 10.0 + min(profit_factor, 2.5) * 5.0
    score += (win_rate - 50.0) * 0.10 if trades else 0.0
    score -= max_drawdown * 4.0
    score += min(len(trades), 4) * 2.5

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
        "average_r": round(average_r, 3),
        "max_drawdown_r": round(max_drawdown, 3),
        "recent_r": round(net_r, 3),
        "max_hold_bars": candidate.max_hold_bars,
        "regime_boost": _regime_boost(candidate.name, regime_name),
        "trade_examples": trades[-5:],
    }


def evaluate_candidates(
    bars: list[dict[str, Any]], start_time: int | None = None, end_time: int | None = None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    rows = build_feature_rows(bars)
    if not rows:
        return [], [], {"name": "UNCLASSIFIED"}
    start_time = int(start_time if start_time is not None else rows[max(0, len(rows) - 14)]["time"])
    end_time = int(end_time if end_time is not None else int(rows[-1]["time"]) + 300)
    regime = research_regime(rows, start_time, end_time)
    results = [backtest_candidate(candidate, rows, start_time, end_time, str(regime["name"])) for candidate in CANDIDATES]
    results.sort(key=lambda item: item["score"], reverse=True)
    return results, rows, regime


def build_playbook_snapshot(
    bars: list[dict[str, Any]], research_start: int, research_end: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rankings, _rows, regime = evaluate_candidates(bars, research_start, research_end)
    playbook = [item["name"] for item in rankings]
    # All modules remain eligible. Research determines priority and a small confidence boost.
    score_values = [float(item["score"]) for item in rankings]
    low = min(score_values) if score_values else 0.0
    high = max(score_values) if score_values else 1.0
    span = max(1.0, high - low)
    boosts = {item["name"]: round((float(item["score"]) - low) / span * 5.0, 3) for item in rankings}
    snapshot = {
        "selected_at": research_end,
        "selection_basis": "Actual first-hour XAUUSD regime and M5 opportunities; historical bars used only to warm indicators",
        "regime": regime,
        "playbook": playbook,
        "research_boosts": boosts,
        "minimum_confidence": 58.0,
        "ranking": rankings,
    }
    return rankings, snapshot


def evaluate_live_playbook(
    snapshot: dict[str, Any], bars: list[dict[str, Any]], bid: float, ask: float
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    rows = build_feature_rows(bars)
    if len(rows) < 60:
        return None, {"decision": "HOLD", "reason": "Insufficient M5 history for indicator calculation"}

    i = len(rows) - 2  # last fully closed M5 candle
    row = rows[i]
    playbook = list(snapshot.get("playbook") or [candidate.name for candidate in CANDIDATES])
    boosts = dict(snapshot.get("research_boosts") or {})
    minimum = float(snapshot.get("minimum_confidence") or 58.0)
    evaluated: list[dict[str, Any]] = []

    for rank, name in enumerate(playbook):
        try:
            candidate = candidate_by_name(name)
        except KeyError:
            continue
        signal = candidate.signal_fn(rows, i)
        if not signal:
            evaluated.append({"strategy": name, "label": candidate.label, "status": "NO_TRIGGER", "rank": rank + 1})
            continue
        effective = min(96.0, float(signal["confidence"]) + float(boosts.get(name, 0.0)))
        evaluated.append(
            {
                "strategy": name,
                "label": candidate.label,
                "status": "QUALIFIED" if effective >= minimum else "BELOW_THRESHOLD",
                "rank": rank + 1,
                "action": signal["action"],
                "base_confidence": round(float(signal["confidence"]), 2),
                "effective_confidence": round(effective, 2),
            }
        )
        if effective < minimum:
            continue
        signal = dict(signal)
        signal["strategy"] = name
        signal["strategy_label"] = candidate.label
        signal["effective_confidence"] = effective
        signal["research_rank"] = rank + 1
        signal["research_boost"] = float(boosts.get(name, 0.0))
        signal["max_hold_bars"] = candidate.max_hold_bars
        evaluated[-1]["signal"] = signal

    qualified = [item for item in evaluated if item.get("signal")]
    if not qualified:
        decision = {
            "decision": "HOLD",
            "bar_time": int(row["time"]),
            "reason": "No playbook module reached the live evidence threshold on the closed M5 candle",
            "evaluated": evaluated,
        }
        return None, decision

    qualified.sort(
        key=lambda item: (
            float(item["signal"]["effective_confidence"]),
            -int(item["signal"]["research_rank"]),
        ),
        reverse=True,
    )
    chosen = dict(qualified[0]["signal"])
    atr_value = finite(row.get("atr"))
    action = str(chosen["action"])
    entry = ask if action == "BUY" else bid
    sl_distance = atr_value * float(chosen["sl_atr"])
    tp_distance = atr_value * float(chosen["tp_atr"])
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
    indicator_snapshot = {key: row.get(key) for key in snapshot_keys}
    indicator_snapshot["research_regime"] = snapshot.get("regime")
    indicator_snapshot["research_rank"] = chosen["research_rank"]
    indicator_snapshot["research_boost"] = chosen["research_boost"]
    indicator_snapshot["all_live_evaluations"] = evaluated
    if chosen.get("evidence_scores"):
        indicator_snapshot["evidence_scores"] = chosen["evidence_scores"]

    signal_result = {
        "strategy": chosen["strategy"],
        "strategy_label": chosen["strategy_label"],
        "action": action,
        "bar_time": int(row["time"]),
        "sl": sl,
        "tp": tp,
        "confidence": round(float(chosen["effective_confidence"]), 2),
        "reasons": list(chosen["reasons"])
        + [f"Research playbook rank: {chosen['research_rank']}"],
        "indicator_snapshot": indicator_snapshot,
    }
    decision = {
        "decision": action,
        "bar_time": int(row["time"]),
        "chosen_strategy": chosen["strategy"],
        "confidence": signal_result["confidence"],
        "evaluated": evaluated,
    }
    return signal_result, decision
