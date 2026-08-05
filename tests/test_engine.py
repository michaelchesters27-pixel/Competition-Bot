from __future__ import annotations

import math
import tempfile
import time
from pathlib import Path

from eve_app.engine import CompetitionEngine, phase_state
from eve_app.historical import HistoricalDataset
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


class StaticHistoricalSource:
    def __init__(self, bars):
        self.bars = bars

    def get_research_dataset(self, symbol, end_ts):
        return HistoricalDataset(
            m5_bars=[bar for bar in self.bars if int(bar["time"]) < int(end_ts)],
            m1_bars=[],
            metadata={"valid": True, "source": "TEST"},
        )


def test_candidate_evaluation_returns_ranked_playbook():
    bars = synthetic_bars()
    end = bars[-1]["time"] + 300
    start = end - 3600
    results, rows, regime = evaluate_candidates(bars, start, end)
    assert len(results) >= 25
    assert regime["candidate_families_tested"] >= 25
    assert "indicator_correlation" in regime
    assert all("oos_expectancy" in result for result in results)
    assert all("correlation_status" in result for result in results)
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
        started_bars = synthetic_bars()
        engine = CompetitionEngine(storage, StaticHistoricalSource(started_bars))
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
        scores = storage.latest_candidate_scores(started["session_id"])
        assert len(scores) >= 25
        assert all("oos_profit_factor" in score for score in scores)


def test_playbook_freezes_and_live_model_can_issue_signal():
    bars = synthetic_bars(direction=0.24)
    research_end = bars[-3]["time"] - 300
    research_start = research_end - 3600
    rankings, snapshot = build_playbook_snapshot(bars, research_start, research_end)
    assert rankings
    assert snapshot["accepted_strategy_count"] == 0
    assert snapshot["playbook"] == []
    assert all(item["status"] == "INSUFFICIENT_EVIDENCE" for item in rankings)
    signal, decision = evaluate_live_playbook(
        snapshot,
        bars,
        bid=bars[-1]["close"] - 0.05,
        ask=bars[-1]["close"] + 0.05,
    )
    assert signal is None
    assert decision["decision"] == "HOLD"


def test_research_evaluation_memory_stays_bounded():
    import tracemalloc

    bars = synthetic_bars(count=1200)
    research_start = bars[0]["time"]
    research_end = bars[-1]["time"] + 300

    tracemalloc.start()
    try:
        _rankings, rows, _regime = evaluate_candidates(bars, research_start, research_end)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(rows) == len(bars)
    assert peak < 64 * 1024 * 1024
