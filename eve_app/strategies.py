from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from statistics import mean
from typing import Any, Callable, Iterable

from .indicators import build_feature_rows, finite
from .memory import log_memory

SignalFn = Callable[[list[dict[str, Any]], int], dict[str, Any] | None]
ExecutionRows = list[dict[str, Any]] | Callable[[int, int], list[dict[str, Any]]]

logger = logging.getLogger(__name__)

TARGET_PROFIT_FACTOR = 2.10
MAX_STRATEGY_CORRELATION = 0.72
MIN_WALK_FORWARD_FOLDS = 3
MIN_OUT_OF_SAMPLE_TRADES = 200
ASSUMED_SLIPPAGE_ATR = 0.015
MIN_SPREAD_ATR = 0.018
SPREAD_POINT_VALUE = 0.01


@dataclass(frozen=True)
class CandidateStrategy:
    name: str
    label: str
    max_hold_bars: int
    signal_fn: SignalFn
    family: str
    indicators: tuple[str, ...]


def _ready(row: dict[str, Any], *keys: str) -> bool:
    return all(row.get(key) is not None for key in keys)


def _action(payload: dict[str, Any] | None, action: str, sl: float, tp: float, conf: float, reasons: list[str]) -> dict[str, Any] | None:
    if payload is None:
        return None
    return {"action": action, "sl_atr": sl, "tp_atr": tp, "confidence": conf, "reasons": reasons, **payload}


def _bar_dir(r: dict[str, Any]) -> int:
    return 1 if float(r["close"]) > float(r["open"]) else -1 if float(r["close"]) < float(r["open"]) else 0


def _recent_release(rows: list[dict[str, Any]], i: int, lookback: int = 3) -> bool:
    start = max(1, i - lookback + 1)
    return any(rows[j - 1].get("squeeze_on") is True and rows[j].get("squeeze_on") is False for j in range(start, i + 1))


def _higher_high(rows: list[dict[str, Any]], i: int) -> bool:
    return i >= 2 and float(rows[i]["high"]) > float(rows[i - 1]["high"]) > float(rows[i - 2]["high"])


def _lower_low(rows: list[dict[str, Any]], i: int) -> bool:
    return i >= 2 and float(rows[i]["low"]) < float(rows[i - 1]["low"]) < float(rows[i - 2]["low"])


def _signal_family(kind: str) -> SignalFn:
    def signal(rows: list[dict[str, Any]], i: int) -> dict[str, Any] | None:
        if i < 4:
            return None
        r, p = rows[i], rows[i - 1]
        atr = finite(r.get("atr"))
        if atr <= 0:
            return None
        close, open_ = float(r["close"]), float(r["open"])
        high, low = float(r["high"]), float(r["low"])
        body = float(r.get("body_ratio") or 0.0)
        mom, d_mom = finite(r.get("momentum")), finite(r.get("momentum_delta"))
        fast, slow = finite(r.get("t3_fast")), finite(r.get("t3_slow"))
        slope, cov, cvd = finite(r.get("t3_fast_slope")), finite(r.get("change_of_volatility")), finite(r.get("chaikin_delta"))
        bull, bear = None, None

        if kind == "structure_breakout":
            bull = r.get("breakout_up") and body > 0.32 and close > float(p["high"])
            bear = r.get("breakout_down") and body > 0.32 and close < float(p["low"])
        elif kind == "liquidity_sweep":
            bull = low < min(float(x["low"]) for x in rows[i - 4:i]) and close > float(p["close"]) and body > 0.25
            bear = high > max(float(x["high"]) for x in rows[i - 4:i]) and close < float(p["close"]) and body > 0.25
        elif kind == "opening_range":
            base_high = max(float(x["high"]) for x in rows[max(0, i - 6):i - 2])
            base_low = min(float(x["low"]) for x in rows[max(0, i - 6):i - 2])
            bull = close > base_high and body > 0.40
            bear = close < base_low and body > 0.40
        elif kind == "inside_bar_expansion":
            inside = float(p["high"]) < float(rows[i - 2]["high"]) and float(p["low"]) > float(rows[i - 2]["low"])
            bull = inside and close > float(p["high"])
            bear = inside and close < float(p["low"])
        elif kind == "pinbar_reversal":
            rng = max(1e-9, high - low)
            bull = (min(open_, close) - low) / rng > 0.55 and close > open_ and mom > -0.2 * atr
            bear = (high - max(open_, close)) / rng > 0.55 and close < open_ and mom < 0.2 * atr
        elif kind == "engulfing_reversal":
            bull = close > open_ and float(p["close"]) < float(p["open"]) and close > float(p["open"]) and open_ < float(p["close"])
            bear = close < open_ and float(p["close"]) > float(p["open"]) and close < float(p["open"]) and open_ > float(p["close"])
        elif kind == "three_bar_impulse":
            bull = all(_bar_dir(rows[k]) > 0 for k in range(i - 2, i + 1)) and _higher_high(rows, i)
            bear = all(_bar_dir(rows[k]) < 0 for k in range(i - 2, i + 1)) and _lower_low(rows, i)
        elif kind == "micro_pullback":
            bull = i >= 3 and close > float(p["high"]) and float(p["close"]) < float(rows[i - 2]["close"]) and fast > slow
            bear = i >= 3 and close < float(p["low"]) and float(p["close"]) > float(rows[i - 2]["close"]) and fast < slow
        elif kind == "t3_trend":
            bull = fast > slow and slope > 0 and close > fast and mom >= 0
            bear = fast < slow and slope < 0 and close < fast and mom <= 0
        elif kind == "t3_pullback":
            bull = fast > slow and float(p["low"]) <= fast <= close and close > open_
            bear = fast < slow and float(p["high"]) >= fast >= close and close < open_
        elif kind == "squeeze_release":
            bull = _recent_release(rows, i) and close > fast and mom > 0
            bear = _recent_release(rows, i) and close < fast and mom < 0
        elif kind == "squeeze_fade":
            bull = r.get("squeeze_on") is True and close < fast - 0.75 * atr and d_mom > 0
            bear = r.get("squeeze_on") is True and close > fast + 0.75 * atr and d_mom < 0
        elif kind == "momentum_zero_cross":
            bull = finite(p.get("momentum")) <= 0 < mom
            bear = finite(p.get("momentum")) >= 0 > mom
        elif kind == "momentum_acceleration":
            bull = mom > 0 and d_mom > 0.10 * atr and body > 0.25
            bear = mom < 0 and d_mom < -0.10 * atr and body > 0.25
        elif kind == "volatility_expansion":
            bull = cov > 4 and cvd > 0 and close > open_
            bear = cov > 4 and cvd > 0 and close < open_
        elif kind == "volatility_contraction_fade":
            bull = cov < -8 and close < fast - 0.55 * atr and close > open_
            bear = cov < -8 and close > fast + 0.55 * atr and close < open_
        elif kind == "range_edge_bounce":
            hi = max(float(x["high"]) for x in rows[i - 8:i]); lo = min(float(x["low"]) for x in rows[i - 8:i])
            bull = low <= lo + 0.12 * atr and close > open_
            bear = high >= hi - 0.12 * atr and close < open_
        elif kind == "range_midline_reject":
            mid = mean(float(x["close"]) for x in rows[i - 10:i])
            bull = float(p["low"]) < mid and close > mid and close > open_
            bear = float(p["high"]) > mid and close < mid and close < open_
        elif kind == "gap_continuation":
            bull = open_ > float(p["high"]) and close > open_
            bear = open_ < float(p["low"]) and close < open_
        elif kind == "gap_fill":
            bull = open_ < float(p["low"]) and close > open_
            bear = open_ > float(p["high"]) and close < open_
        elif kind == "volume_climax_reversal":
            vol = float(r.get("tick_volume") or 0); avg = mean(float(x.get("tick_volume") or 0) for x in rows[i - 10:i])
            bull = vol > avg * 1.35 and low < float(p["low"]) and close > open_
            bear = vol > avg * 1.35 and high > float(p["high"]) and close < open_
        elif kind == "volume_dryup_break":
            vol = float(p.get("tick_volume") or 0); avg = mean(float(x.get("tick_volume") or 0) for x in rows[i - 10:i])
            bull = vol < avg * 0.75 and close > float(p["high"])
            bear = vol < avg * 0.75 and close < float(p["low"])
        elif kind == "atr_channel_break":
            mid = mean(float(x["close"]) for x in rows[i - 12:i])
            bull = close > mid + 0.9 * atr
            bear = close < mid - 0.9 * atr
        elif kind == "atr_trailing_resume":
            bull = close > float(p["close"]) + 0.35 * atr and fast > slow
            bear = close < float(p["close"]) - 0.35 * atr and fast < slow
        elif kind == "wick_continuation":
            rng = max(1e-9, high - low)
            bull = close > open_ and (open_ - low) / rng > 0.35 and close > float(p["high"])
            bear = close < open_ and (high - open_) / rng > 0.35 and close < float(p["low"])
        elif kind == "close_location_value":
            rng = max(1e-9, high - low)
            bull = (close - low) / rng > 0.82 and close > float(p["close"])
            bear = (high - close) / rng > 0.82 and close < float(p["close"])
        if bull:
            return _action({"family": kind}, "BUY", 0.82, 1.95, 64 + min(22, body * 25 + max(0, cov) * .15), [f"{kind.replace('_', ' ').title()} bullish family trigger"])
        if bear:
            return _action({"family": kind}, "SELL", 0.82, 1.95, 64 + min(22, body * 25 + max(0, cov) * .15), [f"{kind.replace('_', ' ').title()} bearish family trigger"])
        return None
    return signal


_FAMILIES: list[tuple[str, str, int, tuple[str, ...]]] = [
    ("structure_breakout", "Structure Breakout", 8, ("price_structure", "body")),
    ("liquidity_sweep", "Liquidity Sweep Reversal", 6, ("price_structure", "wick")),
    ("opening_range", "Opening Range Break", 7, ("session_range",)),
    ("inside_bar_expansion", "Inside Bar Expansion", 5, ("candle_pattern",)),
    ("pinbar_reversal", "Pinbar Reversal", 5, ("wick", "momentum")),
    ("engulfing_reversal", "Engulfing Reversal", 5, ("candle_pattern",)),
    ("three_bar_impulse", "Three Bar Impulse", 6, ("price_action",)),
    ("micro_pullback", "Micro Pullback Resume", 6, ("price_action", "t3")),
    ("t3_trend", "T3 Trend Alignment", 7, ("t3", "momentum")),
    ("t3_pullback", "T3 Pullback", 7, ("t3",)),
    ("squeeze_release", "Squeeze Release", 8, ("squeeze",)),
    ("squeeze_fade", "Squeeze Fade", 5, ("squeeze", "momentum")),
    ("momentum_zero_cross", "Momentum Zero Cross", 6, ("momentum",)),
    ("momentum_acceleration", "Momentum Acceleration", 5, ("momentum", "body")),
    ("volatility_expansion", "Volatility Expansion", 6, ("volatility",)),
    ("volatility_contraction_fade", "Volatility Contraction Fade", 5, ("volatility", "t3")),
    ("range_edge_bounce", "Range Edge Bounce", 5, ("range",)),
    ("range_midline_reject", "Range Midline Reject", 5, ("range", "close_location")),
    ("gap_continuation", "Gap Continuation", 4, ("gap",)),
    ("gap_fill", "Gap Fill Reversal", 4, ("gap",)),
    ("volume_climax_reversal", "Volume Climax Reversal", 5, ("tick_volume", "price_structure")),
    ("volume_dryup_break", "Volume Dry-Up Break", 6, ("tick_volume",)),
    ("atr_channel_break", "ATR Channel Break", 7, ("atr", "range")),
    ("atr_trailing_resume", "ATR Trailing Resume", 6, ("atr", "t3")),
    ("wick_continuation", "Wick Continuation", 5, ("wick",)),
    ("close_location_value", "Close Location Value", 4, ("close_location",)),
]

CANDIDATES = [CandidateStrategy(name, label, hold, _signal_family(name), name, indicators) for name, label, hold, indicators in _FAMILIES]


def candidate_by_name(name: str) -> CandidateStrategy:
    for candidate in CANDIDATES:
        if candidate.name == name:
            return candidate
    raise KeyError(name)


def _execution_cost_r(rows: list[dict[str, Any]], signal_index: int, sl_distance: float) -> float:
    row = rows[signal_index]
    atr_value = finite(row.get("atr"))
    spread_points = finite(row.get("spread"))
    spread_price = spread_points * SPREAD_POINT_VALUE if spread_points > 0 else atr_value * MIN_SPREAD_ATR
    realistic_spread = max(spread_price, atr_value * MIN_SPREAD_ATR)
    slippage = atr_value * ASSUMED_SLIPPAGE_ATR
    return (realistic_spread + slippage) / sl_distance if sl_distance > 0 else 0.0


def _trade_result(rows: list[dict[str, Any]], signal_index: int, signal: dict[str, Any], max_hold_bars: int, end_index: int | None = None, execution_rows: ExecutionRows | None = None) -> tuple[float, int, float, float, str]:
    entry_index = signal_index + 1
    if entry_index >= len(rows):
        return 0.0, entry_index, 0.0, 0.0, "NO_ENTRY"
    atr_value = finite(rows[signal_index].get("atr"))
    if atr_value <= 0:
        return 0.0, entry_index, 0.0, 0.0, "NO_ATR"
    sl_distance = atr_value * float(signal["sl_atr"])
    tp_distance = atr_value * float(signal["tp_atr"])
    cost_r = _execution_cost_r(rows, signal_index, sl_distance)
    action = str(signal["action"])
    raw_entry = float(rows[entry_index]["open"])
    entry = raw_entry + cost_r * sl_distance / 2 if action == "BUY" else raw_entry - cost_r * sl_distance / 2
    sl = entry - sl_distance if action == "BUY" else entry + sl_distance
    tp = entry + tp_distance if action == "BUY" else entry - tp_distance
    reward_r = tp_distance / sl_distance
    last_index = min(len(rows) - 1, entry_index + max_hold_bars - 1)
    if end_index is not None:
        last_index = min(last_index, end_index)
    mfe_r = mae_r = 0.0
    execution_source = "M5"
    scan_rows: Iterable[dict[str, Any]] = (rows[j] for j in range(entry_index, last_index + 1))
    if execution_rows:
        start_time = int(rows[entry_index]["time"])
        end_time = int(rows[last_index]["time"]) + 300
        lower = execution_rows(start_time, end_time) if callable(execution_rows) else [r for r in execution_rows if start_time <= int(r["time"]) < end_time]
        if lower:
            scan_rows = lower
            execution_source = "M1"
    for j, scan in enumerate(scan_rows, start=entry_index):
        high, low = float(scan["high"]), float(scan["low"])
        if action == "BUY":
            mfe_r = max(mfe_r, (high - entry) / sl_distance); mae_r = min(mae_r, (low - entry) / sl_distance)
            if low <= sl: return -1.0 - cost_r, j, mfe_r, mae_r, execution_source
            if high >= tp: return reward_r - cost_r, j, mfe_r, mae_r, execution_source
        else:
            mfe_r = max(mfe_r, (entry - low) / sl_distance); mae_r = min(mae_r, (entry - high) / sl_distance)
            if high >= sl: return -1.0 - cost_r, j, mfe_r, mae_r, execution_source
            if low <= tp: return reward_r - cost_r, j, mfe_r, mae_r, execution_source
    final_close = float(rows[last_index]["close"])
    result_r = (final_close - entry) / sl_distance if action == "BUY" else (entry - final_close) / sl_distance
    return result_r - cost_r, last_index, mfe_r, mae_r, execution_source


def _research_indices(rows: list[dict[str, Any]], start_time: int, end_time: int) -> list[int]:
    return [i for i, row in enumerate(rows) if i >= 55 and int(row["time"]) >= start_time and int(row["time"]) + 300 <= end_time]


def research_regime(rows: list[dict[str, Any]], start_time: int, end_time: int) -> dict[str, Any]:
    indices = _research_indices(rows, start_time, end_time) or list(range(max(55, len(rows) - 14), max(55, len(rows) - 1)))
    selected = [rows[i] for i in indices if i < len(rows)]
    if not selected:
        return {"name": "UNCLASSIFIED", "net_atr": 0.0, "squeeze_fraction": 0.0, "trend_alignment": 0.0}
    atr_values = [finite(row.get("atr")) for row in selected if finite(row.get("atr")) > 0]
    average_atr = sum(atr_values) / len(atr_values) if atr_values else 1.0
    net_atr = (float(selected[-1]["close"]) - float(selected[0]["open"])) / average_atr
    squeeze_values = [row.get("squeeze_on") for row in selected if row.get("squeeze_on") is not None]
    squeeze_fraction = sum(1 for value in squeeze_values if value) / len(squeeze_values) if squeeze_values else 0.0
    average_cov = mean(finite(row.get("change_of_volatility")) for row in selected)
    average_body = mean(float(row.get("body_ratio") or 0.0) for row in selected)
    name = "TREND_UP" if net_atr >= 1.1 else "TREND_DOWN" if net_atr <= -1.1 else "COMPRESSION" if squeeze_fraction >= .45 else "EXPANSION" if average_cov >= 3 or average_body >= .48 else "RANGE_MIXED"
    return {"name": name, "bars_observed": len(selected), "net_atr": round(net_atr, 3), "squeeze_fraction": round(squeeze_fraction, 3), "average_change_of_volatility": round(average_cov, 3), "average_body_ratio": round(average_body, 3)}


def _metrics(trades: list[dict[str, Any]]) -> dict[str, float]:
    return _metrics_from_values([float(t["result_r"]) for t in trades])


def _metrics_from_values(values: list[float]) -> dict[str, float]:
    wins = sum(1 for value in values if value > 0)
    losses = sum(1 for value in values if value < 0)
    gp = sum(value for value in values if value > 0)
    gl = abs(sum(value for value in values if value < 0))
    net = sum(values)
    pf = gp / gl if gl > 0 else (gp if gp > 0 else 0.0)
    return {"profit_factor": pf, "net_r": net, "average_r": net / len(values) if values else 0.0, "win_rate": wins / len(values) * 100 if values else 0.0, "wins": wins, "losses": losses}


def _run_trades(candidate: CandidateStrategy, rows: list[dict[str, Any]], allowed: set[int], execution_rows: ExecutionRows | None = None) -> list[dict[str, Any]]:
    trades = []; last_allowed = max(allowed) if allowed else 54; i = 55
    while i < len(rows) - 1:
        if i not in allowed: i += 1; continue
        sig = candidate.signal_fn(rows, i)
        if not sig: i += 1; continue
        result_r, exit_index, mfe_r, mae_r, execution_source = _trade_result(rows, i, sig, candidate.max_hold_bars, last_allowed, execution_rows)
        trades.append({"signal_time": int(rows[i]["time"]), "action": sig["action"], "result_r": result_r, "mfe_r": mfe_r, "mae_r": mae_r, "confidence": sig["confidence"], "execution_source": execution_source})
        i = max(i + 1, exit_index + 1)
    return trades


def _run_trade_summary(candidate: CandidateStrategy, rows: list[dict[str, Any]], allowed: set[int], execution_rows: ExecutionRows | None = None) -> dict[str, Any]:
    trade_count = wins = losses = 0
    gross_profit = gross_loss = net_r = 0.0
    examples: list[dict[str, Any]] = []
    last_allowed = max(allowed) if allowed else 54
    i = 55
    m1_count = m5_count = 0
    dd = equity = peak = 0.0
    while i < len(rows) - 1:
        if i not in allowed:
            i += 1
            continue
        sig = candidate.signal_fn(rows, i)
        if not sig:
            i += 1
            continue
        result_r, exit_index, mfe_r, mae_r, execution_source = _trade_result(rows, i, sig, candidate.max_hold_bars, last_allowed, execution_rows)
        trade_count += 1
        net_r += result_r
        if result_r > 0:
            wins += 1
            gross_profit += result_r
        elif result_r < 0:
            losses += 1
            gross_loss += abs(result_r)
        equity += result_r
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
        if execution_source == "M1":
            m1_count += 1
        else:
            m5_count += 1
        examples.append({"signal_time": int(rows[i]["time"]), "action": sig["action"], "result_r": result_r, "mfe_r": mfe_r, "mae_r": mae_r, "confidence": sig["confidence"], "execution_source": execution_source})
        if len(examples) > 5:
            examples.pop(0)
        i = max(i + 1, exit_index + 1)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
    metrics = {"profit_factor": profit_factor, "net_r": net_r, "average_r": net_r / trade_count if trade_count else 0.0, "win_rate": wins / trade_count * 100 if trade_count else 0.0, "wins": wins, "losses": losses}
    return {"metrics": metrics, "trades": trade_count, "max_drawdown_r": dd, "m1_count": m1_count, "m5_count": m5_count, "examples": examples}


def _walk_forward(candidate: CandidateStrategy, rows: list[dict[str, Any]], indices: list[int], execution_rows: ExecutionRows | None = None) -> dict[str, Any]:
    if len(indices) < MIN_WALK_FORWARD_FOLDS * 3:
        return {"folds": [], "oos_expectancy": 0.0, "oos_profit_factor": 0.0, "active_folds": 0, "completed": False, "reason": "insufficient historical bars for TRAIN_VALIDATION_TEST"}
    folds = []
    fold_size = max(1, len(indices) // (MIN_WALK_FORWARD_FOLDS + 2))
    for fold in range(MIN_WALK_FORWARD_FOLDS):
        train_end = (fold + 1) * fold_size
        validation_end = train_end + fold_size
        test_end = validation_end + fold_size
        train_bars = train_end
        validation_bars = validation_end - train_end
        test_stop = test_end if fold < MIN_WALK_FORWARD_FOLDS - 1 else len(indices)
        test_bars = test_stop - validation_end
        if not train_bars or not validation_bars or not test_bars:
            continue
        validation_summary = _run_trade_summary(candidate, rows, set(indices[train_end:validation_end]), execution_rows)
        test_summary = _run_trade_summary(candidate, rows, set(indices[validation_end:test_stop]), execution_rows)
        folds.append({
            "fold": fold + 1,
            "train_bars": train_bars,
            "validation_bars": validation_bars,
            "test_bars": test_bars,
            "validation_trades": validation_summary["trades"],
            "oos_trades": test_summary["trades"],
            "validation": validation_summary["metrics"],
            **test_summary["metrics"],
        })
    active = [f for f in folds if f["oos_trades"]]
    completed = len(folds) >= MIN_WALK_FORWARD_FOLDS and all(f["train_bars"] and f["validation_bars"] and f["test_bars"] for f in folds)
    return {"folds": folds, "oos_expectancy": mean(f["average_r"] for f in active) if active else 0.0, "oos_profit_factor": mean(min(10.0, f["profit_factor"]) for f in active) if active else 0.0, "active_folds": len(active), "completed": completed, "reason": "complete" if completed else "incomplete TRAIN_VALIDATION_TEST folds"}


def backtest_candidate(candidate: CandidateStrategy, rows: list[dict[str, Any]], start_time: int, end_time: int, regime_name: str, execution_rows: ExecutionRows | None = None) -> dict[str, Any]:
    indices = _research_indices(rows, start_time, end_time)
    allowed = set(indices)
    summary = _run_trade_summary(candidate, rows, allowed, execution_rows)
    m = summary["metrics"]
    log_memory("before_walk_forward", strategy=candidate.name, m5_candles_processed=len(rows))
    wf = _walk_forward(candidate, rows, indices, execution_rows)
    log_memory("after_walk_forward", strategy=candidate.name, m5_candles_processed=len(rows))
    dd = summary["max_drawdown_r"]
    pf_gap = float(wf["oos_profit_factor"]) - TARGET_PROFIT_FACTOR
    score = wf["oos_expectancy"] * 100 + min(float(wf["oos_profit_factor"]), 3.5) * 20 + pf_gap * 25 - dd * 6 + min(summary["trades"], 8)
    if wf["oos_profit_factor"] < TARGET_PROFIT_FACTOR: score -= (TARGET_PROFIT_FACTOR - float(wf["oos_profit_factor"])) * 45
    oos_trades = sum(int(f.get("oos_trades", 0)) for f in wf.get("folds", []))
    rejection_reasons = []
    if len(rows) < 120: rejection_reasons.append("insufficient historical data")
    if not wf.get("completed"): rejection_reasons.append("incomplete walk-forward validation")
    if oos_trades < MIN_OUT_OF_SAMPLE_TRADES: rejection_reasons.append(f"fewer than {MIN_OUT_OF_SAMPLE_TRADES} completed out-of-sample trades")
    status = "INSUFFICIENT_EVIDENCE" if rejection_reasons else "ACCEPTED"
    m1_count = summary["m1_count"]
    m5_count = summary["m5_count"]
    return {"name": candidate.name, "label": candidate.label, "family": candidate.family, "indicators": list(candidate.indicators), "score": round(score, 3), "status": status, "accepted": status == "ACCEPTED", "rejection_reason": "; ".join(rejection_reasons), "oos_trades": oos_trades, "m1_trade_simulation_count": m1_count, "m5_fallback_trade_simulation_count": m5_count, "trades": summary["trades"], "wins": int(m["wins"]), "losses": int(m["losses"]), "win_rate": round(m["win_rate"], 2), "profit_factor": round(m["profit_factor"], 3), "net_r": round(m["net_r"], 3), "average_r": round(m["average_r"], 3), "oos_expectancy": round(wf["oos_expectancy"], 3), "oos_profit_factor": round(wf["oos_profit_factor"], 3), "profit_factor_target": TARGET_PROFIT_FACTOR, "walk_forward": wf, "max_drawdown_r": round(dd, 3), "max_hold_bars": candidate.max_hold_bars, "trade_examples": summary["examples"]}


def _pearson(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or len(a) < 2: return 0.0
    ma, mb = mean(a), mean(b); da = [x - ma for x in a]; db = [x - mb for x in b]
    den = math.sqrt(sum(x*x for x in da) * sum(y*y for y in db))
    return sum(x*y for x, y in zip(da, db)) / den if den else 0.0


def _online_pearson(pairs: Iterable[tuple[float, float]]) -> float:
    n = 0
    sum_x = sum_y = sum_xx = sum_yy = sum_xy = 0.0
    for x, y in pairs:
        n += 1
        sum_x += x
        sum_y += y
        sum_xx += x * x
        sum_yy += y * y
        sum_xy += x * y
    if n < 2:
        return 0.0
    numerator = n * sum_xy - sum_x * sum_y
    den_x = n * sum_xx - sum_x * sum_x
    den_y = n * sum_yy - sum_y * sum_y
    denominator = math.sqrt(max(0.0, den_x) * max(0.0, den_y))
    return numerator / denominator if denominator else 0.0


def _signal_byte_vector(candidate: CandidateStrategy, rows: list[dict[str, Any]], indices: list[int]) -> bytearray:
    out = bytearray()
    for i in indices:
        sig = candidate.signal_fn(rows, i)
        out.append(2 if sig and sig["action"] == "BUY" else 0 if sig and sig["action"] == "SELL" else 1)
    return out


def _byte_signal_correlation(left: bytearray, right: bytearray) -> float:
    def decode(value: int) -> float:
        return 1.0 if value == 2 else -1.0 if value == 0 else 0.0
    return _online_pearson((decode(x), decode(y)) for x, y in zip(left, right))


def _apply_correlation_filter(results: list[dict[str, Any]], rows: list[dict[str, Any]], indices: list[int]) -> list[dict[str, Any]]:
    log_memory("before_signal_correlation_filter", m5_candles_processed=len(rows))
    accepted = []
    accepted_vectors: dict[str, bytearray] = {}
    candidates = {c.name: c for c in CANDIDATES}
    for item in results:
        vector = _signal_byte_vector(candidates[item["name"]], rows, indices)
        corr = max((abs(_byte_signal_correlation(vector, accepted_vectors[other["name"]])) for other in accepted), default=0.0)
        item["max_strategy_correlation"] = round(corr, 3)
        item["correlation_threshold"] = MAX_STRATEGY_CORRELATION
        item["correlation_status"] = "ACCEPTED" if corr <= MAX_STRATEGY_CORRELATION else "REJECTED_HIGH_CORRELATION"
        if corr <= MAX_STRATEGY_CORRELATION:
            accepted.append(item)
            accepted_vectors[item["name"]] = vector
        else:
            del vector
    log_memory("after_signal_correlation_filter", m5_candles_processed=len(rows), accepted_strategy_count=len(accepted))
    return accepted + [item for item in results if item.get("correlation_status") == "REJECTED_HIGH_CORRELATION"]


def _indicator_correlation(rows: list[dict[str, Any]], indices: list[int]) -> dict[str, float]:
    keys = ["t3_fast_slope", "momentum", "momentum_delta", "change_of_volatility", "chaikin_delta", "body_ratio", "atr"]
    out = {}
    for x_i, x in enumerate(keys):
        for y in keys[x_i + 1:]:
            out[f"{x}:{y}"] = round(_online_pearson((finite(rows[i].get(x)), finite(rows[i].get(y))) for i in indices), 3)
    return out


def evaluate_candidates(bars: list[dict[str, Any]], start_time: int | None = None, end_time: int | None = None, execution_bars: ExecutionRows | None = None, metadata: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    log_memory("before_feature_build", m5_candles_processed=len(bars))
    rows = build_feature_rows(bars)
    log_memory("after_feature_build", m5_candles_processed=len(rows))
    if not rows: return [], [], {"name": "UNCLASSIFIED"}
    start_time = int(start_time if start_time is not None else rows[max(0, len(rows) - 14)]["time"])
    end_time = int(end_time if end_time is not None else int(rows[-1]["time"]) + 300)
    regime = research_regime(rows, start_time, end_time)
    results = []
    for completed, candidate in enumerate(CANDIDATES, start=1):
        log_memory("before_strategy", strategy=candidate.name, m5_candles_processed=len(rows), strategies_completed=completed - 1)
        results.append(backtest_candidate(candidate, rows, start_time, end_time, str(regime["name"]), execution_bars))
        log_memory("after_strategy", strategy=candidate.name, m5_candles_processed=len(rows), strategies_completed=completed)
        logger.info("historical research progress", extra={"m5_candles_processed": len(rows), "current_date_range": f"{start_time} to {end_time}", "strategies_completed": completed, "configured_chunk_days": (metadata or {}).get("chunk_days")})
    results.sort(key=lambda x: (x["correlation_status"] == "ACCEPTED" if "correlation_status" in x else True, x["oos_expectancy"], x["oos_profit_factor"], x["score"]), reverse=True)
    indices = _research_indices(rows, start_time, end_time) or list(range(55, max(55, len(rows) - 1)))
    results = _apply_correlation_filter(results, rows, indices)
    results.sort(key=lambda x: (x["correlation_status"] == "ACCEPTED", x["oos_expectancy"], x["oos_profit_factor"], x["score"]), reverse=True)
    regime["indicator_correlation"] = _indicator_correlation(rows, indices)
    regime["strategy_correlation_threshold"] = MAX_STRATEGY_CORRELATION
    regime["candidate_families_tested"] = len(CANDIDATES)
    regime["objective"] = "statistically rigorous edge discovery using chronological TRAIN_VALIDATION_TEST walk-forward validation"
    regime["minimum_oos_trades"] = MIN_OUT_OF_SAMPLE_TRADES
    regime["historical_data"] = metadata or {}
    return results, rows, regime


def build_playbook_snapshot(bars: list[dict[str, Any]], research_start: int, research_end: int, execution_bars: ExecutionRows | None = None, metadata: dict[str, Any] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    log_memory("before_playbook_creation", m5_candles_processed=len(bars))
    rankings, _rows, regime = evaluate_candidates(bars, research_start, research_end, execution_bars, metadata)
    accepted = [r for r in rankings if r.get("status") == "ACCEPTED" and r.get("correlation_status") == "ACCEPTED"]
    playbook = [item["name"] for item in accepted[:10]]
    vals = [float(item["oos_expectancy"]) for item in accepted] or [0.0]
    low, high = min(vals), max(vals); span = max(1.0, high - low)
    boosts = {item["name"]: round((float(item["oos_expectancy"]) - low) / span * 5.0, 3) for item in accepted}
    log_memory("after_playbook_creation", m5_candles_processed=len(bars), accepted_strategy_count=len(accepted))
    return rankings, {"selected_at": research_end, "selection_basis": "Walk-forward OOS expectancy, PF>2.10 objective, realistic spread/slippage, and low strategy-correlation family selection", "regime": regime, "playbook": playbook, "research_boosts": boosts, "minimum_confidence": 60.0, "ranking": rankings, "accepted_strategy_count": len(accepted), "profit_factor_target": TARGET_PROFIT_FACTOR, "max_strategy_correlation": MAX_STRATEGY_CORRELATION}


def evaluate_live_playbook(snapshot: dict[str, Any], bars: list[dict[str, Any]], bid: float, ask: float) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    rows = build_feature_rows(bars)
    if len(rows) < 60: return None, {"decision": "HOLD", "reason": "Insufficient M5 history for indicator calculation"}
    i = len(rows) - 2; row = rows[i]
    playbook = list(snapshot.get("playbook") or [])
    if not playbook:
        return None, {"decision": "HOLD", "reason": "No strategy met the research acceptance criteria", "accepted_strategy_count": int(snapshot.get("accepted_strategy_count") or 0)}
    boosts = dict(snapshot.get("research_boosts") or {}); minimum = float(snapshot.get("minimum_confidence") or 60.0)
    evaluated = []
    for rank, name in enumerate(playbook):
        try: c = candidate_by_name(name)
        except KeyError: continue
        sig = c.signal_fn(rows, i)
        if not sig:
            evaluated.append({"strategy": name, "label": c.label, "status": "NO_TRIGGER", "rank": rank + 1}); continue
        effective = min(96.0, float(sig["confidence"]) + float(boosts.get(name, 0.0)))
        evaluated.append({"strategy": name, "label": c.label, "status": "QUALIFIED" if effective >= minimum else "BELOW_THRESHOLD", "rank": rank + 1, "action": sig["action"], "base_confidence": round(float(sig["confidence"]), 2), "effective_confidence": round(effective, 2), "signal": {**sig, "strategy": name, "strategy_label": c.label, "effective_confidence": effective, "research_rank": rank + 1, "research_boost": float(boosts.get(name, 0.0)), "max_hold_bars": c.max_hold_bars} if effective >= minimum else None})
    qualified = [e for e in evaluated if e.get("signal")]
    if not qualified: return None, {"decision": "HOLD", "bar_time": int(row["time"]), "reason": "No independent family reached the live evidence threshold", "evaluated": evaluated}
    qualified.sort(key=lambda e: (float(e["signal"]["effective_confidence"]), -int(e["signal"]["research_rank"])), reverse=True)
    chosen = dict(qualified[0]["signal"]); atr_value = finite(row.get("atr")); action = str(chosen["action"]); entry = ask if action == "BUY" else bid
    sl_distance = atr_value * float(chosen["sl_atr"]); tp_distance = atr_value * float(chosen["tp_atr"])
    sl = entry - sl_distance if action == "BUY" else entry + sl_distance; tp = entry + tp_distance if action == "BUY" else entry - tp_distance
    keys = ["time", "open", "high", "low", "close", "t3_fast", "t3_slow", "t3_fast_slope", "t3_slow_slope", "squeeze_on", "momentum", "momentum_delta", "chaikin_volatility", "chaikin_delta", "change_of_volatility", "atr", "body_ratio", "breakout_up", "breakout_down"]
    snap = {k: row.get(k) for k in keys}; snap.update({"research_regime": snapshot.get("regime"), "research_rank": chosen["research_rank"], "research_boost": chosen["research_boost"], "all_live_evaluations": evaluated, "profit_factor_target": snapshot.get("profit_factor_target")})
    return {"strategy": chosen["strategy"], "strategy_label": chosen["strategy_label"], "action": action, "bar_time": int(row["time"]), "sl": sl, "tp": tp, "confidence": round(float(chosen["effective_confidence"]), 2), "reasons": list(chosen["reasons"]) + [f"Research playbook rank: {chosen['research_rank']}"], "indicator_snapshot": snap}, {"decision": action, "bar_time": int(row["time"]), "chosen_strategy": chosen["strategy"], "confidence": round(float(chosen["effective_confidence"]), 2), "evaluated": evaluated}
