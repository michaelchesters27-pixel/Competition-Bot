from __future__ import annotations

import importlib
import json
import os
from functools import wraps
from typing import Any, Callable

from flask import Flask, Response, jsonify, render_template, request

from eve_app.engine import CompetitionEngine
from eve_app.historical import HistoricalConfig, _safe_database_identity
from eve_app.reporting import build_dashboard
from eve_app.storage import Storage

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

storage = Storage()
engine = CompetitionEngine(storage)
API_KEY = os.getenv("EVE_API_KEY", "replace-this-key")


def require_api_key(fn: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any):
        provided = request.headers.get("X-API-Key", "")
        if provided != API_KEY:
            return Response("ERROR|UNAUTHORIZED", status=401, mimetype="text/plain")
        return fn(*args, **kwargs)

    return wrapper


def clean_field(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "/").replace("\r", " ").replace("\n", " ")


def start_wire(payload: dict[str, Any]) -> str:
    fields = [
        "OK",
        payload["session_id"],
        payload["phase"],
        payload["elapsed_seconds"],
        payload["remaining_seconds"],
        payload.get("selected_strategy") or "-",
    ]
    return "|".join(clean_field(v) for v in fields)


def pulse_wire(payload: dict[str, Any]) -> str:
    fields = [
        "OK",
        payload["session_id"],
        payload["phase"],
        payload["elapsed_seconds"],
        payload["remaining_seconds"],
        payload.get("selected_strategy") or "-",
        payload.get("action", "HOLD"),
        payload.get("signal_id") or "-",
        payload.get("sl", 0),
        payload.get("tp", 0),
        payload.get("confidence", 0),
        "; ".join(payload.get("reasons", [])) or "-",
    ]
    return "|".join(clean_field(v) for v in fields)


def _historical_check_payload() -> dict[str, Any]:
    config = HistoricalConfig.from_env()
    payload: dict[str, Any] = {
        "database_host": "",
        "database_name": "",
        "current_schema": None,
        "market_candles_count": None,
        "xauusd_5min_completed": {"min_candle_time": None, "max_candle_time": None, "count": None},
        "configured": {"table": None, "symbol": None, "m5_value": None, "m1_value": None},
        "database_exception": None,
    }
    if config is None:
        payload["database_exception"] = "Historical database is not configured"
        return payload

    identity = _safe_database_identity(config.database_url)
    payload["database_host"] = identity["host"]
    payload["database_name"] = identity["database"]
    payload["configured"] = {
        "table": config.table,
        "symbol": config.symbol_value,
        "m5_value": config.m5_value,
        "m1_value": config.m1_value,
    }

    try:
        psycopg = importlib.import_module("psycopg")
        rows_module = importlib.import_module("psycopg.rows")
        with psycopg.connect(config.database_url, row_factory=rows_module.dict_row) as conn:
            conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            with conn.transaction():
                schema_row = conn.execute("SELECT current_schema() AS current_schema").fetchone()
                count_row = conn.execute("SELECT COUNT(*) AS count FROM public.market_candles").fetchone()
                range_row = conn.execute(
                    """
                    SELECT MIN(candle_time) AS min_candle_time,
                           MAX(candle_time) AS max_candle_time,
                           COUNT(*) AS count
                    FROM public.market_candles
                    WHERE symbol = 'XAU/USD'
                      AND interval = '5min'
                      AND is_complete = true
                    """
                ).fetchone()
        payload["current_schema"] = schema_row["current_schema"] if schema_row else None
        payload["market_candles_count"] = int(count_row["count"]) if count_row else None
        if range_row:
            payload["xauusd_5min_completed"] = {
                "min_candle_time": str(range_row["min_candle_time"]) if range_row["min_candle_time"] is not None else None,
                "max_candle_time": str(range_row["max_candle_time"]) if range_row["max_candle_time"] is not None else None,
                "count": int(range_row["count"]),
            }
    except Exception as exc:
        payload["database_exception"] = clean_field(exc)
    return payload


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    return jsonify({"ok": True, "service": "EVE Competition Scalper", "version": "2.00"})


@app.post("/api/session/start")
@require_api_key
def session_start():
    try:
        payload = request.get_json(force=True)
        account_login = str(payload.get("account_login", "")).strip()
        symbol = str(payload.get("symbol", "")).strip()
        timeframe = str(payload.get("timeframe", "M5")).strip().upper()
        launch_id = str(payload.get("launch_id", "")).strip()
        if not account_login or not symbol or not launch_id:
            return Response("ERROR|MISSING_ACCOUNT_SYMBOL_OR_LAUNCH_ID", status=400, mimetype="text/plain")
        if not symbol.upper().startswith("XAUUSD"):
            return Response("ERROR|EA_MUST_BE_ATTACHED_TO_XAUUSD", status=400, mimetype="text/plain")
        if timeframe != "M5":
            return Response("ERROR|EA_MUST_BE_ATTACHED_TO_M5", status=400, mimetype="text/plain")
        session = engine.start_or_resume(account_login, symbol, timeframe, launch_id)
        return Response(start_wire(session), mimetype="text/plain")
    except Exception as exc:
        app.logger.exception("session start failed")
        return Response(f"ERROR|{clean_field(exc)}", status=500, mimetype="text/plain")


@app.post("/api/pulse")
@require_api_key
def pulse():
    try:
        payload = request.get_json(force=True)
        result = engine.process_pulse(payload)
        return Response(pulse_wire(result), mimetype="text/plain")
    except KeyError as exc:
        return Response(f"ERROR|{clean_field(exc)}", status=404, mimetype="text/plain")
    except Exception as exc:
        app.logger.exception("pulse failed")
        return Response(f"ERROR|{clean_field(exc)}", status=500, mimetype="text/plain")


@app.post("/api/signal/ack")
@require_api_key
def signal_ack():
    try:
        payload = request.get_json(force=True)
        engine.acknowledge_signal(
            session_id=str(payload["session_id"]),
            signal_id=str(payload["signal_id"]),
            accepted=bool(payload.get("accepted", False)),
            message=str(payload.get("message", "")),
        )
        return Response("OK", mimetype="text/plain")
    except Exception as exc:
        app.logger.exception("signal ack failed")
        return Response(f"ERROR|{clean_field(exc)}", status=500, mimetype="text/plain")


@app.post("/api/deal")
@require_api_key
def deal():
    try:
        payload = request.get_json(force=True)
        saved = engine.record_deal(payload)
        return Response("OK|SAVED" if saved else "OK|DUPLICATE", mimetype="text/plain")
    except Exception as exc:
        app.logger.exception("deal record failed")
        return Response(f"ERROR|{clean_field(exc)}", status=500, mimetype="text/plain")


@app.get("/api/historical-check")
def historical_check():
    return jsonify(_historical_check_payload())


@app.get("/api/dashboard")
def dashboard():
    session_id = request.args.get("session_id")
    return jsonify(build_dashboard(storage, session_id))


@app.get("/api/report/<session_id>")
def report(session_id: str):
    data = build_dashboard(storage, session_id)
    if not data.get("has_session"):
        return jsonify(data), 404
    return jsonify(data)


@app.get("/api/research")
def research_dashboard():
    session_id = request.args.get("session_id")
    data = build_dashboard(storage, session_id)
    if not data.get("has_session"):
        return jsonify(data), 404
    return jsonify({
        "session": data["session"],
        "research_dashboard": data["research_dashboard"],
        "strategy_snapshot": data.get("strategy_snapshot"),
        "historical_data": data.get("historical_data", {}),
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
