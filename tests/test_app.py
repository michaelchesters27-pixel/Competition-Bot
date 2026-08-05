from __future__ import annotations

import tempfile
from pathlib import Path

import app as app_module
from eve_app.storage import Storage


def test_research_endpoint_returns_historical_data(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        storage = Storage(str(Path(tmp) / "test.db"))
        session = storage.get_or_create_session("123", "XAUUSD", "M5", "launch-a")
        metadata = {"valid": True, "query_symbol": "XAU/USD", "m5_interval": "5min"}
        storage.set_selected_strategy(
            session["id"],
            "adaptive_research_playbook",
            {"regime": {"historical_data": metadata}, "playbook": []},
        )
        monkeypatch.setattr(app_module, "storage", storage)

        client = app_module.app.test_client()
        response = client.get(f"/api/research?session_id={session['id']}")

        assert response.status_code == 200
        assert response.get_json()["historical_data"] == metadata


def test_historical_check_returns_database_visibility(monkeypatch):
    config = app_module.HistoricalConfig(
        database_url="postgresql://reader:secret@db.example.com:5432/market_db?sslmode=require",
        table="public.market_candles",
        symbol_value="XAU/USD",
        m5_value="5min",
        m1_value="1min",
    )
    calls = []

    class Tx:
        def __enter__(self): return None
        def __exit__(self, *args): return False

    class Rows:
        def __init__(self, row):
            self.row = row
        def fetchone(self): return self.row

    class Conn:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def transaction(self): return Tx()
        def execute(self, sql, params=None):
            calls.append((sql, params))
            if "current_schema" in sql:
                return Rows({"current_schema": "public"})
            if "COUNT(*) AS count FROM public.market_candles" in sql:
                return Rows({"count": 479069})
            if "MIN(candle_time)" in sql:
                return Rows({
                    "min_candle_time": "2024-01-01 00:00:00+00",
                    "max_candle_time": "2026-08-05 00:00:00+00",
                    "count": 479069,
                })
            return Rows({})

    class Psycopg:
        def connect(self, database_url, row_factory=None):
            assert database_url == config.database_url
            assert row_factory is DictRow.dict_row
            return Conn()

    class DictRow:
        dict_row = object()

    def fake_import_module(name):
        if name == "psycopg":
            return Psycopg()
        if name == "psycopg.rows":
            return DictRow
        raise AssertionError(name)

    monkeypatch.setattr(app_module.HistoricalConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(app_module.importlib, "import_module", fake_import_module)

    response = app_module.app.test_client().get("/api/historical-check")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["database_host"] == "db.example.com"
    assert payload["database_name"] == "market_db"
    assert payload["current_schema"] == "public"
    assert payload["market_candles_count"] == 479069
    assert payload["xauusd_5min_completed"] == {
        "min_candle_time": "2024-01-01 00:00:00+00",
        "max_candle_time": "2026-08-05 00:00:00+00",
        "count": 479069,
    }
    assert payload["configured"] == {
        "table": "public.market_candles",
        "symbol": "XAU/USD",
        "m5_value": "5min",
        "m1_value": "1min",
    }
    assert payload["database_exception"] is None
    assert any("WHERE symbol = 'XAU/USD'" in sql for sql, _params in calls)
    assert all("SET statement_timeout" not in sql for sql, _params in calls)


def test_historical_check_returns_database_exception(monkeypatch):
    config = app_module.HistoricalConfig(database_url="postgresql://reader@example.com/db")

    class Psycopg:
        def connect(self, *args, **kwargs):
            raise RuntimeError("permission denied for table market_candles")

    def fake_import_module(name):
        if name == "psycopg":
            return Psycopg()
        if name == "psycopg.rows":
            return type("Rows", (), {"dict_row": object()})
        raise AssertionError(name)

    monkeypatch.setattr(app_module.HistoricalConfig, "from_env", classmethod(lambda cls: config))
    monkeypatch.setattr(app_module.importlib, "import_module", fake_import_module)

    payload = app_module.app.test_client().get("/api/historical-check").get_json()

    assert payload["database_exception"] == "permission denied for table market_candles"
