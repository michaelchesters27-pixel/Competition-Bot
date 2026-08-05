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
            if "current_database" in sql:
                return Rows({"current_database": "market_db"})
            if "current_schema" in sql:
                return Rows({"current_schema": "public"})
            if "current_user" in sql:
                return Rows({"current_user": "readonly_user"})
            if "COUNT(*) AS total_rows" in sql:
                return Rows({"total_rows": 479069})
            if "COUNT(*) AS matching_rows" in sql:
                return Rows({
                    "matching_rows": 12345,
                    "earliest": "2024-01-01 00:00:00+00",
                    "latest": "2026-08-05 00:00:00+00",
                })
            if "SELECT MIN(candle_time)" in sql:
                return Rows({"min": "2024-01-01 00:00:00+00"})
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
    assert payload == {
        "current_database": "market_db",
        "current_schema": "public",
        "current_user": "readonly_user",
        "total_rows": 479069,
        "matching_rows": 12345,
        "earliest": "2024-01-01 00:00:00+00",
        "latest": "2026-08-05 00:00:00+00",
        "earliest_time_result": "2024-01-01 00:00:00+00",
        "database_exception": None,
    }
    assert any("WHERE symbol='XAU/USD'" in sql for sql, _params in calls)
    assert any(params == {"symbol": "XAU/USD", "timeframe": "5min"} for _sql, params in calls)


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
