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
