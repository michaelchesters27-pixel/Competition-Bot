from datetime import datetime, timezone

from eve_app.engine import CompetitionEngine
from eve_app.historical import HistoricalConfig, HistoricalDataset, SupabaseMarketCandles, build_m5_from_m1, normalize_bars
from eve_app.storage import Storage
from eve_app.strategies import build_playbook_snapshot, evaluate_live_playbook
from tests.test_engine import synthetic_bars


def _source(config: HistoricalConfig) -> SupabaseMarketCandles:
    source = object.__new__(SupabaseMarketCandles)
    source.config = config
    return source


class Response:
    def __init__(self, data):
        self.data = data


class RecordingQuery:
    def __init__(self, batches):
        self.batches = batches
        self.calls = []
        self.range_start = 0
        self.range_end = 0
    def select(self, columns): self.calls.append(("select", columns)); return self
    def eq(self, column, value): self.calls.append(("eq", column, value)); return self
    def gte(self, column, value): self.calls.append(("gte", column, value)); return self
    def lt(self, column, value): self.calls.append(("lt", column, value)); return self
    def order(self, column, desc=False): self.calls.append(("order", column, desc)); return self
    def range(self, start, end): self.calls.append(("range", start, end)); self.range_start = start; self.range_end = end; return self
    def limit(self, count): self.calls.append(("limit", count)); return self
    def execute(self):
        idx = self.range_start // max(1, self.range_end - self.range_start + 1)
        return Response(self.batches[idx] if idx < len(self.batches) else [])


class RecordingClient:
    def __init__(self, batches=None):
        self.batches = batches or [[]]
        self.queries = []
    def table(self, name):
        query = RecordingQuery(self.batches)
        query.calls.append(("table", name))
        self.queries.append(query)
        return query


def test_actual_market_candles_column_defaults_are_used():
    config = HistoricalConfig(supabase_url="https://example.supabase.co", service_role_key="key")
    source = _source(config)
    client = RecordingClient()
    source.client = client
    source._fetch("XAUUSD", "M5", 1_700_000_000, 1_700_086_400)
    calls = client.queries[0].calls
    assert ("select", "candle_time,open,high,low,close,volume") in calls
    assert ("eq", "interval", "M5") in calls
    assert ("eq", "is_complete", True) in calls
    assert ("eq", "source", "twelve_data") in calls
    assert all(call[1] != "timeframe" for call in calls if len(call) > 1)


def test_missing_spread_does_not_break_loading_and_marks_assumed():
    config = HistoricalConfig(supabase_url="https://example.supabase.co", service_role_key="key", spread_column="")
    source = _source(config)
    source.earliest_time = lambda symbol, timeframe: 1_700_000_000
    source._fetch_chunked = lambda symbol, timeframe, start_ts, end_ts: normalize_bars([
        {"candle_time": datetime(2024, 1, 1, tzinfo=timezone.utc), "open": 1, "high": 2, "low": 0, "close": 1, "volume": 10}
    ], config) if timeframe == "M5" else []
    dataset = source.get_research_dataset("XAUUSD", 1_700_086_400)
    assert dataset.valid
    assert dataset.metadata["spread_source"] == "ASSUMED"
    assert dataset.m5_bars[0]["spread"] == 0


def test_candle_time_timestamp_filtering_uses_iso_timestamp_parameters():
    config = HistoricalConfig(supabase_url="https://example.supabase.co", service_role_key="key")
    source = _source(config)
    client = RecordingClient()
    source.client = client
    source._fetch("XAUUSD", "M5", 1_700_000_000, 1_700_086_400)
    calls = client.queries[0].calls
    start_filter = next(call for call in calls if call[0] == "gte")
    end_filter = next(call for call in calls if call[0] == "lt")
    assert start_filter[1] == "candle_time"
    assert end_filter[1] == "candle_time"
    assert isinstance(start_filter[2], str)
    assert "+00:00" in start_filter[2]


def test_completed_candles_only_are_loaded():
    config = HistoricalConfig(supabase_url="https://example.supabase.co", service_role_key="key")
    source = _source(config)
    client = RecordingClient()
    source.client = client
    source._fetch("XAUUSD", "M5", 1_700_000_000, 1_700_086_400)
    assert ("eq", "is_complete", True) in client.queries[0].calls


def test_historical_pagination_loads_pages_inside_date_chunks():
    config = HistoricalConfig(supabase_url="https://example.supabase.co", service_role_key="key", page_size=2, filter_preferred_source=False)
    source = _source(config)
    client = RecordingClient([
        [{"candle_time": "2024-01-01T00:00:00+00:00", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1}, {"candle_time": "2024-01-01T00:05:00+00:00", "open": 2, "high": 3, "low": 1, "close": 2, "volume": 1}],
        [{"candle_time": "2024-01-01T00:10:00+00:00", "open": 3, "high": 4, "low": 2, "close": 3, "volume": 1}],
    ])
    source.client = client
    rows = source._fetch("XAUUSD", "M5", 1_700_000_000, 1_700_086_400)
    assert len(rows) == 3
    assert ("range", 0, 1) in client.queries[0].calls
    assert ("range", 2, 3) in client.queries[1].calls


def test_historical_chunking_loads_date_chunks_chronologically():
    config = HistoricalConfig(supabase_url="https://example.supabase.co", service_role_key="key", chunk_days=1, filter_preferred_source=False)
    source = _source(config)
    calls = []
    def fake_fetch(symbol, timeframe, start_ts, end_ts, use_source_filter=True):
        calls.append((start_ts, end_ts))
        return [{"time": start_ts, "open": 1, "high": 2, "low": 0, "close": 1, "tick_volume": 1, "spread": 0}]
    source._fetch = fake_fetch
    rows = source._fetch_chunked("XAUUSD", "M5", 0, 3 * 86400)
    assert calls == [(0, 86400), (86400, 172800), (172800, 259200)]
    assert [row["time"] for row in rows] == [0, 86400, 172800]


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
    config = HistoricalConfig(supabase_url="", service_role_key="")
    rows = [
        {"candle_time": datetime(2024, 1, 1, 0, 5, tzinfo=timezone.utc), "open": 2, "high": 3, "low": 1, "close": 2, "volume": 1},
        {"candle_time": datetime(2024, 1, 1, 0, 0, tzinfo=timezone.utc), "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1},
    ]
    out = normalize_bars(rows, config)
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
