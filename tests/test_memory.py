from eve_app.memory import bounded_tail, compact_trade_summary


def test_bounded_tail_keeps_recent_items_only():
    assert bounded_tail(range(10), 3) == [7, 8, 9]


def test_compact_trade_summary_counts_sources_and_bounds_examples():
    trades = [{"result_r": i, "execution_source": "M1" if i % 2 else "M5"} for i in range(8)]

    summary = compact_trade_summary(trades, examples=3)

    assert summary["trade_count"] == 8
    assert summary["m1_trade_simulation_count"] == 4
    assert summary["m5_fallback_trade_simulation_count"] == 4
    assert summary["trade_examples"] == trades[-3:]
