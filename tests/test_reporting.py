from __future__ import annotations

import tempfile
from pathlib import Path

from eve_app.reporting import build_dashboard
from eve_app.storage import Storage


def test_empty_dashboard():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        data = build_dashboard(storage)
        assert data["has_session"] is False


def test_trade_ledger_builds_full_report_from_open_and_close_deals():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        session = storage.get_or_create_session("123", "XAUUSD", "M5", "launch-a")
        signal = storage.create_signal(
            session_id=session["id"],
            bar_time=1700000000,
            strategy="adaptive_directional_scalp",
            action="BUY",
            sl=2398.0,
            tp=2404.0,
            confidence=84.0,
            reasons=["T3, momentum and candle direction aligned"],
            indicator_snapshot={"atr": 2.0, "evidence_scores": {"buy": 6.2, "sell": 1.4}},
        )
        assert signal is not None
        storage.save_deal(
            {
                "session_id": session["id"],
                "signal_id": signal["id"],
                "deal_ticket": "1",
                "order_ticket": "11",
                "position_id": "101",
                "deal_type": "BUY",
                "entry_type": "IN",
                "symbol": "XAUUSD",
                "volume": 0.01,
                "price": 2400.0,
                "profit": 0,
                "commission": -0.05,
                "swap": 0,
                "comment": f"EVE|{signal['id']}",
                "deal_time": 1700000300,
            }
        )
        storage.save_deal(
            {
                "session_id": session["id"],
                "signal_id": "",
                "deal_ticket": "2",
                "order_ticket": "12",
                "position_id": "101",
                "deal_type": "SELL",
                "entry_type": "OUT",
                "symbol": "XAUUSD",
                "volume": 0.01,
                "price": 2404.0,
                "profit": 4.0,
                "commission": -0.05,
                "swap": 0,
                "comment": "tp",
                "deal_time": 1700000600,
            }
        )
        data = build_dashboard(storage, session["id"])
        report = data["trade_reports"][0]
        assert report["status"] == "CLOSED"
        assert report["signal_id"] == signal["id"]
        assert report["net_profit"] == 3.9
        assert report["result_r"] == 2.0
        assert report["indicator_snapshot"]["evidence_scores"]["buy"] == 6.2


def test_dashboard_exposes_historical_data_from_strategy_snapshot():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        session = storage.get_or_create_session("123", "XAUUSD", "M5", "launch-a")
        metadata = {
            "valid": True,
            "requested_symbol": "XAUUSD",
            "query_symbol": "XAU/USD",
            "m5_interval": "5min",
            "earliest_m5": 1700000000,
        }
        storage.set_selected_strategy(
            session["id"],
            "adaptive_research_playbook",
            {"regime": {"historical_data": metadata}, "playbook": []},
        )

        data = build_dashboard(storage, session["id"])

        assert data["historical_data"] == metadata


def test_dashboard_exposes_historical_data_from_engine_activity_logs():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        session = storage.get_or_create_session("123", "XAUUSD", "M5", "launch-a")
        metadata = {"valid": False, "reason": "No completed historical candles", "query_symbol": "XAU/USD"}
        storage.add_log(
            session["id"],
            "HISTORICAL_DATA_UNAVAILABLE",
            "Historical dataset rejected",
            level="WARNING",
            details=metadata,
        )

        data = build_dashboard(storage, session["id"])

        assert data["historical_data"] == metadata
        assert data["logs"][0]["details"] == metadata
