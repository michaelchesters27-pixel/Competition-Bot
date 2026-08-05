from __future__ import annotations

import math
import tempfile
import time
from pathlib import Path

from eve_app.engine import CompetitionEngine, phase_state
from eve_app.storage import Storage
from eve_app.strategies import build_playbook_snapshot, evaluate_candidates, evaluate_live_playbook


def synthetic_bars(count: int = 420, start: int | None = None, direction: float = 0.20):
    bars = []
    start = start or (int(time.time()) - count * 300)
    price = 2400.0
    for i in range(count):
        wave = math.sin(i / 5.0) * 0.20
        open_ = price
        close = open_ + direction + wave * 0.12
        high = max(open_, close) + 0.30 + abs(wave) * 0.10
        low = min(open_, close) - 0.28 - abs(wave) * 0.10
        bars.append(
            {
                "time": start + i * 300,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "tick_volume": 100 + i,
                "spread": 20,
            }
        )
        price = close
    return bars


def test_candidate_evaluation_returns_ranked_playbook():
    bars = synthetic_bars()
    end = bars[-1]["time"] + 300
    start = end - 3600
    results, rows, regime = evaluate_candidates(bars, start, end)
    assert len(results) == 6
    assert len(rows) == 420
    assert results == sorted(results, key=lambda result: result["score"], reverse=True)
    assert regime["name"] in {"TREND_UP", "TREND_DOWN", "EXPANSION", "COMPRESSION", "RANGE_MIXED"}


def test_new_attachment_launch_id_creates_fresh_session():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        engine = CompetitionEngine(storage)
        first = engine.start_or_resume("12345", "XAUUSD", "M5", "launch-a")
        resumed = engine.start_or_resume("12345", "XAUUSD", "M5", "launch-a")
        second = engine.start_or_resume("12345", "XAUUSD", "M5", "launch-b")
        assert first["session_id"] == resumed["session_id"]
        assert first["session_id"] != second["session_id"]
        session = storage.get_session(second["session_id"])
        assert session is not None
        assert phase_state(session).phase == "RESEARCH"


def test_research_pulse_stores_actual_window_ranking():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        engine = CompetitionEngine(storage)
        started = engine.start_or_resume("12345", "XAUUSD", "M5", "launch-a")
        session = storage.get_session(started["session_id"])
        assert session is not None
        bars = synthetic_bars(start=int(session["started_at"]) - 408 * 300)
        result = engine.process_pulse(
            {
                "session_id": started["session_id"],
                "bid": bars[-1]["close"] - 0.05,
                "ask": bars[-1]["close"] + 0.05,
                "bars": bars,
            }
        )
        assert result["phase"] == "RESEARCH"
        assert result["action"] == "HOLD"
        assert len(storage.latest_candidate_scores(started["session_id"])) == 6


def test_playbook_freezes_and_live_model_can_issue_signal():
    bars = synthetic_bars(direction=0.24)
    research_end = bars[-3]["time"] - 300
    research_start = research_end - 3600
    rankings, snapshot = build_playbook_snapshot(bars, research_start, research_end)
    assert rankings
    assert snapshot["playbook"]
    signal, decision = evaluate_live_playbook(
        snapshot,
        bars,
        bid=bars[-1]["close"] - 0.05,
        ask=bars[-1]["close"] + 0.05,
    )
    assert decision["bar_time"] == bars[-2]["time"]
    assert signal is not None
    assert signal["action"] == "BUY"
    assert signal["strategy"] in snapshot["playbook"]
    assert signal["tp"] > signal["sl"]
