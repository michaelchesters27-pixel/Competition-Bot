from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .engine import phase_state
from .storage import Storage, utc_now_ts


def _weighted_price(deals: list[dict[str, Any]]) -> float:
    total_volume = sum(float(d.get("volume", 0)) for d in deals)
    if total_volume <= 0:
        return 0.0
    return sum(float(d.get("price", 0)) * float(d.get("volume", 0)) for d in deals) / total_volume


def _position_reports(storage: Storage, session: dict[str, Any]) -> list[dict[str, Any]]:
    signals = {s["id"]: s for s in storage.session_signals(session["id"])}
    deals = storage.session_deals(session["id"])
    bars = storage.get_bars(session["id"], 1000)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for deal in deals:
        key = str(deal.get("position_id") or deal.get("order_ticket") or deal["deal_ticket"])
        grouped[key].append(deal)

    lots: list[dict[str, Any]] = []
    for position_id, items in grouped.items():
        items.sort(key=lambda d: (int(d["deal_time"]), int(d["deal_ticket"])))
        open_lots: list[dict[str, Any]] = []

        def open_new_lot(deal: dict[str, Any], volume: float, factor: float = 1.0) -> None:
            if volume <= 1e-9:
                return
            lot = {
                "trade_id": str(deal["deal_ticket"]),
                "position_id": position_id,
                "signal_id": str(deal.get("signal_id") or ""),
                "direction": str(deal["deal_type"]).upper(),
                "opened_at": int(deal["deal_time"]),
                "entry_price": float(deal["price"]),
                "volume": volume,
                "remaining_volume": volume,
                "opening_commission": float(deal.get("commission", 0)) * factor,
                "opening_swap": float(deal.get("swap", 0)) * factor,
                "opening_profit": float(deal.get("profit", 0)) * factor,
                "exit_chunks": [],
                "deal_count": 1,
            }
            lots.append(lot)
            open_lots.append(lot)

        def close_existing(deal: dict[str, Any], close_volume: float, factor: float = 1.0) -> float:
            remaining = close_volume
            total_for_allocation = close_volume if close_volume > 0 else 1.0
            while remaining > 1e-9 and open_lots:
                lot = open_lots[0]
                allocated = min(remaining, float(lot["remaining_volume"]))
                share = allocated / total_for_allocation
                lot["exit_chunks"].append(
                    {
                        "volume": allocated,
                        "price": float(deal["price"]),
                        "time": int(deal["deal_time"]),
                        "profit": float(deal.get("profit", 0)) * factor * share,
                        "commission": float(deal.get("commission", 0)) * factor * share,
                        "swap": float(deal.get("swap", 0)) * factor * share,
                    }
                )
                lot["deal_count"] += 1
                lot["remaining_volume"] = max(0.0, float(lot["remaining_volume"]) - allocated)
                remaining -= allocated
                if lot["remaining_volume"] <= 1e-9:
                    open_lots.pop(0)
            return remaining

        for deal in items:
            entry_type = str(deal["entry_type"]).upper()
            volume = float(deal.get("volume", 0))
            if entry_type == "IN":
                open_new_lot(deal, volume)
            elif entry_type in {"OUT", "OUT_BY"}:
                close_existing(deal, volume)
            elif entry_type == "INOUT":
                existing_volume = sum(float(lot["remaining_volume"]) for lot in open_lots)
                closing_volume = min(volume, existing_volume)
                opening_volume = max(0.0, volume - closing_volume)
                if volume > 0:
                    close_existing(deal, closing_volume, closing_volume / volume)
                    open_new_lot(deal, opening_volume, opening_volume / volume)

    reports: list[dict[str, Any]] = []
    for lot in lots:
        signal_id = lot["signal_id"]
        signal = signals.get(signal_id)
        closed_volume = sum(float(chunk["volume"]) for chunk in lot["exit_chunks"])
        status = "CLOSED" if lot["remaining_volume"] <= 1e-9 else "OPEN"
        exit_price = None
        closed_at = None
        if closed_volume > 0:
            exit_price = sum(float(c["price"]) * float(c["volume"]) for c in lot["exit_chunks"]) / closed_volume
            closed_at = max(int(c["time"]) for c in lot["exit_chunks"])

        gross_profit = float(lot["opening_profit"]) + sum(float(c["profit"]) for c in lot["exit_chunks"])
        commission = float(lot["opening_commission"]) + sum(float(c["commission"]) for c in lot["exit_chunks"])
        swap = float(lot["opening_swap"]) + sum(float(c["swap"]) for c in lot["exit_chunks"])
        net_profit = gross_profit + commission + swap
        entry_price = float(lot["entry_price"])
        direction = lot["direction"]

        result_r = None
        mfe_r = None
        mae_r = None
        if signal and entry_price and float(signal["sl"]):
            risk_distance = abs(entry_price - float(signal["sl"]))
            if risk_distance > 0:
                if exit_price is not None and status == "CLOSED":
                    result_r = (
                        (exit_price - entry_price) / risk_distance
                        if direction == "BUY"
                        else (entry_price - exit_price) / risk_distance
                    )
                end_time = closed_at or utc_now_ts()
                window = [b for b in bars if int(lot["opened_at"]) <= int(b["time"]) <= end_time]
                if window:
                    if direction == "BUY":
                        mfe_r = max((float(b["high"]) - entry_price) / risk_distance for b in window)
                        mae_r = min((float(b["low"]) - entry_price) / risk_distance for b in window)
                    else:
                        mfe_r = max((entry_price - float(b["low"])) / risk_distance for b in window)
                        mae_r = min((entry_price - float(b["high"])) / risk_distance for b in window)

        reports.append(
            {
                "trade_id": lot["trade_id"],
                "position_id": lot["position_id"],
                "signal_id": signal_id,
                "strategy": signal["strategy"] if signal else "",
                "direction": direction,
                "status": status,
                "opened_at": lot["opened_at"],
                "closed_at": closed_at,
                "duration_seconds": (closed_at or utc_now_ts()) - int(lot["opened_at"]),
                "volume": round(float(lot["volume"]), 4),
                "remaining_volume": round(float(lot["remaining_volume"]), 4),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "stop_loss": float(signal["sl"]) if signal else None,
                "take_profit": float(signal["tp"]) if signal else None,
                "confidence": float(signal["confidence"]) if signal else None,
                "reasons": signal["reasons"] if signal else [],
                "indicator_snapshot": signal["indicator_snapshot"] if signal else {},
                "gross_profit": round(gross_profit, 2),
                "commission": round(commission, 2),
                "swap": round(swap, 2),
                "net_profit": round(net_profit, 2),
                "result_r": round(result_r, 3) if result_r is not None else None,
                "mfe_r": round(mfe_r, 3) if mfe_r is not None else None,
                "mae_r": round(mae_r, 3) if mae_r is not None else None,
                "deal_count": int(lot["deal_count"]),
            }
        )
    reports.sort(key=lambda item: item["opened_at"], reverse=True)
    return reports


def build_dashboard(storage: Storage, session_id: str | None = None) -> dict[str, Any]:
    session = storage.get_session(session_id) if session_id else storage.latest_session()
    if not session:
        return {
            "has_session": False,
            "message": "Attach the EA to an XAUUSD M5 chart to start the two-hour session.",
        }

    phase = phase_state(session)
    candidates = storage.latest_candidate_scores(session["id"])
    reports = _position_reports(storage, session)
    closed = [r for r in reports if r["status"] == "CLOSED"]
    wins = [r for r in closed if r["net_profit"] > 0]
    losses = [r for r in closed if r["net_profit"] < 0]
    gross_wins = sum(r["net_profit"] for r in wins)
    gross_losses = abs(sum(r["net_profit"] for r in losses))
    profit_factor = gross_wins / gross_losses if gross_losses > 0 else (gross_wins if gross_wins else 0)
    net_profit = sum(r["net_profit"] for r in reports)
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0
    result_rs = [r["result_r"] for r in closed if r["result_r"] is not None]

    strategy_snapshot = json.loads(session["strategy_snapshot_json"]) if session.get("strategy_snapshot_json") else None
    return {
        "has_session": True,
        "session": {
            "id": session["id"],
            "account_login": session["account_login"],
            "symbol": session["symbol"],
            "timeframe": session["timeframe"],
            "started_at": session["started_at"],
            "research_ends_at": session["research_ends_at"],
            "trading_ends_at": session["trading_ends_at"],
            "phase": phase.phase,
            "elapsed_seconds": phase.elapsed_seconds,
            "remaining_seconds": phase.remaining_seconds,
            "selected_strategy": session.get("selected_strategy") or "",
            "last_seen_at": session.get("last_seen_at"),
        },
        "candidate_scores": candidates,
        "strategy_snapshot": strategy_snapshot,
        "trade_reports": reports,
        "stats": {
            "trades": len(reports),
            "closed_trades": len(closed),
            "open_trades": len([r for r in reports if r["status"] == "OPEN"]),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(win_rate, 2),
            "profit_factor": round(profit_factor, 3),
            "net_profit": round(net_profit, 2),
            "average_r": round(sum(result_rs) / len(result_rs), 3) if result_rs else 0.0,
        },
        "logs": storage.session_logs(session["id"], limit=80),
    }
