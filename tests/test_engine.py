from __future__ import annotations

import math
import tempfile
from pathlib import Path

from eve_app.engine import CompetitionEngine, phase_state
from eve_app.storage import Storage
from eve_app.strategies import evaluate_candidates


def synthetic_bars(count: int = 350):
    bars = []
    price = 2400.0
    for i in range(count):
        drift = 0.22 if (i // 40) % 2 == 0 else -0.12
        wave = math.sin(i / 5.0) * 0.35
        open_ = price
        close = open_ + drift + wave * 0.18
        high = max(open_, close) + 0.45 + abs(wave) * 0.2
        low = min(open_, close) - 0.42 - abs(wave) * 0.2
        bars.append(
            {
                "time": 1_700_000_000 + i * 300,
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


def test_candidate_evaluation_returns_ranked_results():
    results, rows = evaluate_candidates(synthetic_bars())
    assert len(results) == 5
    assert len(rows) == 350
    assert results == sorted(results, key=lambda r: r["score"], reverse=True)
    assert all("profit_factor" in result for result in results)


def test_session_starts_in_research_phase():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        engine = CompetitionEngine(storage)
        session_payload = engine.start_or_resume("12345", "XAUUSD", "M5")
        session = storage.get_session(session_payload["session_id"])
        assert session is not None
        assert phase_state(session).phase == "RESEARCH"
        assert 3590 <= phase_state(session).remaining_seconds <= 3600


def test_pulse_stores_bars_and_produces_research_ranking():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        engine = CompetitionEngine(storage)
        session_payload = engine.start_or_resume("12345", "XAUUSD", "M5")
        result = engine.process_pulse(
            {
                "session_id": session_payload["session_id"],
                "bid": 2400.0,
                "ask": 2400.2,
                "bars": synthetic_bars(),
            }
        )
        assert result["phase"] == "RESEARCH"
        assert result["action"] == "HOLD"
        assert len(storage.latest_candidate_scores(session_payload["session_id"])) == 5


def test_strategy_freezes_when_research_hour_has_finished():
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        engine = CompetitionEngine(storage)
        started = engine.start_or_resume("12345", "XAUUSD", "M5")
        session_id = started["session_id"]
        now = __import__("time").time()
        with storage.connection() as conn:
            conn.execute(
                "UPDATE sessions SET research_ends_at=?, trading_ends_at=? WHERE id=?",
                (int(now) - 1, int(now) + 3599, session_id),
            )
        result = engine.process_pulse(
            {
                "session_id": session_id,
                "bid": 2400.0,
                "ask": 2400.2,
                "bars": synthetic_bars(),
            }
        )
        session = storage.get_session(session_id)
        assert result["phase"] == "TRADING"
        assert session is not None
        assert session["selected_strategy"]
