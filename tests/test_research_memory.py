from __future__ import annotations

import tracemalloc

from eve_app.strategies import MIN_OUT_OF_SAMPLE_TRADES, evaluate_candidates


def _synthetic_m5(count: int) -> list[dict[str, float]]:
    bars = []
    price = 1900.0
    for i in range(count):
        drift = ((i % 17) - 8) * 0.01
        open_ = price
        close = open_ + drift
        high = max(open_, close) + 0.35 + (i % 5) * 0.01
        low = min(open_, close) - 0.35 - (i % 7) * 0.01
        bars.append({"time": 1_600_000_000 + i * 300, "open": open_, "high": high, "low": low, "close": close, "tick_volume": float(100 + i % 50), "spread": 2.0})
        price = close
    return bars


def test_large_synthetic_research_memory_stays_bounded():
    bars = _synthetic_m5(8_000)
    tracemalloc.start()
    before_current, before_peak = tracemalloc.get_traced_memory()

    rankings, rows, regime = evaluate_candidates(
        bars,
        int(bars[55]["time"]),
        int(bars[-2]["time"]) + 300,
        metadata={"chunk_days": 30},
    )

    current, peak = tracemalloc.get_traced_memory()
    peak_growth_mb = (peak - before_peak) / 1_048_576
    current_growth_mb = (current - before_current) / 1_048_576

    assert len(rows) == len(bars)
    assert rankings
    assert regime["minimum_oos_trades"] == MIN_OUT_OF_SAMPLE_TRADES == 200
    assert peak_growth_mb < 90
    assert current_growth_mb < 70
    assert all(len(item.get("trade_examples", [])) <= 5 for item in rankings)
