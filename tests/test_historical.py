from datetime import datetime, timezone

import pytest

from eve_app.engine import CompetitionEngine
from eve_app.historical import HistoricalConfig, HistoricalDataset, SupabaseMarketCandles, build_m5_from_m1, normalize_bars
from eve_app.storage import Storage
from eve_app.strategies import build_playbook_snapshot, evaluate_live_playbook
from tests.test_engine import synthetic_bars


def _source(config: HistoricalConfig) -> SupabaseMarketCandles:
    source = object.__new__(SupabaseMarketCandles)
    source.config = config
    source._table = SupabaseMarketCandles._safe_table(config.table)
    return source


def test_actual_market_candles_column_defaults_are_used():
    config = HistoricalConfig(database_url="postgresql://reader@example/db")
    source = _source(config)
    sql = source._select_sql()
    assert '"interval"' in sql
    assert '"candle_time"' in sql
    assert '"volume" AS "tick_volume"' in sql
    assert '"is_complete" = true' in sql
    assert '"source" = %(source)s' in sql
    assert '"timeframe"' not in sql


def test_missing_spread_does_not_break_query_and_marks_assumed():
    config = HistoricalConfig(database_url="postgresql://reader@example/db", spread_column="")
    source = _source(config)
    assert '0::double precision AS "spread"' in source._select_sql()

    class FakeSource:
        def get_research_dataset(self, symbol, end_ts):
            return HistoricalDataset(
                m5_bars=synthetic_bars(120),
                m1_bars=[],
                metadata={"valid": True, "spread_source": "ASSUMED"},
            )

    dataset = FakeSource().get_research_dataset("XAUUSD", 1)
    assert dataset.metadata["spread_source"] == "ASSUMED"


def test_candle_time_timestamp_filtering_uses_timestamp_parameters():
    config = HistoricalConfig(database_url="postgresql://reader@example/db")
    source = _source(config)
    captured = {}

    def fake_connect():
        class Tx:
            def __enter__(self): return None
            def __exit__(self, *args): return False
        class Conn:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def transaction(self, *args, **kwargs):
                assert args == ()
                assert kwargs == {}
                return Tx()
            def execute(self, sql, params=None):
                if params and "start_time" in params:
                    captured.update(params)
                class Rows:
                    def fetchall(self): return []
                return Rows()
        return Conn()

    source._connect = fake_connect
    source._fetch("XAUUSD", "M5", 1_700_000_000, 1_700_086_400)
    assert isinstance(captured["start_time"], datetime)
    assert captured["start_time"].tzinfo is not None
    assert isinstance(captured["end_time"], datetime)


def test_historical_transactions_do_not_pass_read_only_keyword():
    config = HistoricalConfig(database_url="postgresql://reader@example/db")
    source = _source(config)
    transaction_kwargs = []

    class Tx:
        def __enter__(self): return None
        def __exit__(self, *args): return False

    class Rows:
        def __init__(self, row=None):
            self.row = row
        def fetchall(self): return []
        def fetchone(self): return self.row

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def transaction(self, *args, **kwargs):
            transaction_kwargs.append(kwargs)
            if "read_only" in kwargs:
                raise TypeError("Connection.transaction() got an unexpected keyword argument 'read_only'")
            return Tx()
        def execute(self, sql, params=None):
            if "MIN" in sql:
                return Rows({"earliest_time": None})
            return Rows()

    source._connect = lambda: Conn()

    source._fetch("XAUUSD", "M5", 1_700_000_000, 1_700_086_400)
    assert source.earliest_time("XAUUSD", "M5") is None
    assert transaction_kwargs == [{}, {}]


def test_completed_candles_only_are_loaded():
    config = HistoricalConfig(database_url="postgresql://reader@example/db")
    assert '"is_complete" = true' in _source(config)._select_sql()


def test_historical_pagination_loads_date_chunks_chronologically():
    config = HistoricalConfig(database_url="postgresql://reader@example/db", chunk_days=1, filter_preferred_source=False)
    source = _source(config)
    calls = []

    def fake_fetch(symbol, timeframe, start_ts, end_ts, use_source_filter=True):
        calls.append((start_ts, end_ts))
        return [{"time": start_ts, "open": 1, "high": 2, "low": 0, "close": 1, "tick_volume": 1, "spread": 0}]

    source._fetch = fake_fetch
    rows = source._fetch_chunked("XAUUSD", "M5", 0, 3 * 86400)
    assert calls == [(0, 86400), (86400, 172800), (172800, 259200)]
    assert [row["time"] for row in rows] == [0, 86400, 172800]


def test_symbol_value_override_is_used_for_historical_queries():
    config = HistoricalConfig(database_url="postgresql://reader@example/db", symbol_value="XAU/USD", m5_value="5min", m1_value="1min")
    source = _source(config)
    earliest_calls = []
    fetch_calls = []

    def fake_earliest(symbol, timeframe):
        earliest_calls.append((symbol, timeframe))
        return 1_700_000_000 if timeframe == "5min" else None

    def fake_fetch_chunked(symbol, timeframe, start_ts, end_ts):
        fetch_calls.append((symbol, timeframe, start_ts, end_ts))
        if timeframe == "5min":
            return [{"time": start_ts, "open": 1, "high": 2, "low": 0, "close": 1, "tick_volume": 1, "spread": 0}]
        return []

    source.earliest_time = fake_earliest
    source._fetch_chunked = fake_fetch_chunked

    dataset = source.get_research_dataset("XAUUSD", 1_700_000_300)

    assert dataset.valid
    assert earliest_calls == [("XAU/USD", "5min"), ("XAU/USD", "1min")]
    assert fetch_calls == [("XAU/USD", "5min", 1_700_000_000, 1_700_000_300), ("XAU/USD", "1min", 1_700_000_000, 1_700_000_300)]
    assert dataset.metadata["requested_symbol"] == "XAUUSD"
    assert dataset.metadata["query_symbol"] == "XAU/USD"
    assert dataset.metadata["m5_interval"] == "5min"
    assert dataset.metadata["m1_interval"] == "1min"
    assert dataset.metadata["earliest_m5"] == 1_700_000_000
    assert dataset.metadata["earliest_m1"] is None
    assert dataset.metadata["m5_bars"] == 1
    assert dataset.metadata["m1_bars"] == 0
    assert dataset.metadata["reason"] == "OK"


def test_xauusd_falls_back_to_slash_symbol_for_historical_queries():
    config = HistoricalConfig(database_url="postgresql://reader@example/db", m5_value="5min", m1_value="1min")
    source = _source(config)
    earliest_calls = []
    fetch_calls = []

    def fake_earliest(symbol, timeframe):
        earliest_calls.append((symbol, timeframe))
        if symbol == "XAU/USD" and timeframe == "5min":
            return 1_700_000_000
        return None

    def fake_fetch_chunked(symbol, timeframe, start_ts, end_ts):
        fetch_calls.append((symbol, timeframe, start_ts, end_ts))
        if symbol == "XAU/USD" and timeframe == "5min":
            return [{"time": start_ts, "open": 1, "high": 2, "low": 0, "close": 1, "tick_volume": 1, "spread": 0}]
        return []

    source.earliest_time = fake_earliest
    source._fetch_chunked = fake_fetch_chunked

    dataset = source.get_research_dataset("XAUUSD", 1_700_000_300)

    assert dataset.valid
    assert earliest_calls == [("XAUUSD", "5min"), ("XAUUSD", "1min"), ("XAU/USD", "5min"), ("XAU/USD", "1min")]
    assert fetch_calls == [("XAU/USD", "5min", 1_700_000_000, 1_700_000_300), ("XAU/USD", "1min", 1_700_000_000, 1_700_000_300)]
    assert dataset.metadata["requested_symbol"] == "XAUUSD"
    assert dataset.metadata["query_symbol"] == "XAU/USD"
    assert dataset.metadata["earliest_m5"] == 1_700_000_000
    assert dataset.metadata["earliest_m1"] is None
    assert dataset.metadata["database_host"] == "example"
    assert dataset.metadata["database_name"] == "db"
    assert dataset.metadata["earliest_queries"] == [
        {"symbol": "XAUUSD", "interval": "5min", "params": {"symbol": "XAUUSD", "timeframe": "5min"}, "earliest_time": None},
        {"symbol": "XAUUSD", "interval": "1min", "params": {"symbol": "XAUUSD", "timeframe": "1min"}, "earliest_time": None},
        {"symbol": "XAU/USD", "interval": "5min", "params": {"symbol": "XAU/USD", "timeframe": "5min"}, "earliest_time": 1_700_000_000},
        {"symbol": "XAU/USD", "interval": "1min", "params": {"symbol": "XAU/USD", "timeframe": "1min"}, "earliest_time": None},
    ]


def test_invalid_historical_dataset_reports_query_diagnostics():
    config = HistoricalConfig(database_url="postgresql://reader@example/db", symbol_value="XAU/USD", m5_value="5min", m1_value="1min")
    source = _source(config)
    source.earliest_time = lambda symbol, timeframe: None

    dataset = source.get_research_dataset("XAUUSD", 1_700_000_300)

    assert not dataset.valid
    assert dataset.metadata["valid"] is False
    assert dataset.metadata["query_symbol"] == "XAU/USD"
    assert dataset.metadata["requested_symbol"] == "XAUUSD"
    assert dataset.metadata["earliest_m5"] is None
    assert dataset.metadata["earliest_m1"] is None
    assert dataset.metadata["m5_bars"] == 0
    assert dataset.metadata["m1_bars"] == 0
    assert "XAU/USD" in dataset.metadata["reason"]
    assert "5min" in dataset.metadata["reason"]
    assert "1min" in dataset.metadata["reason"]


def test_engine_logs_actual_invalid_historical_reason(tmp_path):
    class InvalidSource:
        def get_research_dataset(self, symbol, end_ts):
            return HistoricalDataset(
                m5_bars=[],
                m1_bars=[],
                metadata={
                    "valid": False,
                    "reason": "No completed historical candles found for symbol='XAU/USD', M5 interval='5min', M1 interval='1min'",
                    "earliest_m5": None,
                    "earliest_m1": None,
                    "m5_bars": 0,
                    "m1_bars": 0,
                },
            )

    storage = Storage(str(tmp_path / "test.db"))
    engine = CompetitionEngine(storage, InvalidSource())
    started = engine.start_or_resume("12345", "XAUUSD", "M5", "launch-a")
    session = storage.get_session(started["session_id"])
    assert session is not None
    bars = synthetic_bars(start=int(session["started_at"]) - 408 * 300)

    result = engine.process_pulse({"session_id": started["session_id"], "bid": bars[-1]["close"], "ask": bars[-1]["close"] + 0.1, "bars": bars})

    assert result["action"] == "HOLD"
    logs = storage.session_logs(started["session_id"], limit=10)
    unavailable = next(log for log in logs if log["event"] == "HISTORICAL_DATA_UNAVAILABLE")
    assert unavailable["message"] == "Historical dataset rejected: No completed historical candles found for symbol='XAU/USD', M5 interval='5min', M1 interval='1min'"
    assert unavailable["details"]["earliest_m5"] is None
    assert unavailable["details"]["earliest_m1"] is None
    assert unavailable["details"]["m5_bars"] == 0
    assert unavailable["details"]["m1_bars"] == 0


def test_builds_m5_from_m1_only_as_fallback_helper():
    start = 1_700_000_000 - (1_700_000_000 % 300)
    m1 = []
    price = 2400.0
    for i in range(10):
        m1.append({"time": start + i * 60, "open": price, "high": price + 0.3, "low": price - 0.2, "close": price + 0.1, "tick_volume": 10, "spread": 20})
        price += 0.1
    m5 = build_m5_from_m1(m1)
    assert len(m5) == 2
    assert m5[0]["time"] == start
    assert m5[0]["open"] == 2400.0
    assert m5[0]["tick_volume"] == 50


def test_normalize_bars_orders_datetime_rows():
    rows = [
        {"time": datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc), "open": 2, "high": 3, "low": 1, "close": 2, "tick_volume": 1, "spread": 1},
        {"time": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc), "open": 1, "high": 2, "low": 0, "close": 1, "tick_volume": 1, "spread": 1},
    ]
    out = normalize_bars(rows)
    assert out[0]["time"] < out[1]["time"]


def test_empty_accepted_playbook_does_not_promote_live_strategies():
    snapshot = {"playbook": [], "accepted_strategy_count": 0}
    signal, decision = evaluate_live_playbook(snapshot, [], bid=1.0, ask=1.1)
    assert signal is None
    assert decision["decision"] == "HOLD"


def test_insufficient_evidence_not_promoted_into_playbook():
    bars = synthetic_bars(180)
    rankings, snapshot = build_playbook_snapshot(bars, bars[0]["time"], bars[-1]["time"] + 300)
    assert rankings
    assert all(item["status"] == "INSUFFICIENT_EVIDENCE" for item in rankings)
    assert snapshot["playbook"] == []


def test_failed_historical_connection_cannot_promote_strategy(tmp_path):
    class FailingSource:
        def get_research_dataset(self, symbol, end_ts):
            raise RuntimeError("connection failed")

    storage = Storage(str(tmp_path / "test.db"))
    engine = CompetitionEngine(storage, FailingSource())
    started = engine.start_or_resume("12345", "XAUUSD", "M5", "launch-a")
    session = storage.get_session(started["session_id"])
    assert session is not None
    bars = synthetic_bars(start=int(session["started_at"]) - 408 * 300)
    result = engine.process_pulse({"session_id": started["session_id"], "bid": bars[-1]["close"], "ask": bars[-1]["close"] + 0.1, "bars": bars})
    assert result["action"] == "HOLD"
    assert storage.latest_candidate_scores(started["session_id"]) == []
    logs = storage.session_logs(started["session_id"], limit=10)
    assert any(log["event"] == "HISTORICAL_DATA_UNAVAILABLE" for log in logs)
