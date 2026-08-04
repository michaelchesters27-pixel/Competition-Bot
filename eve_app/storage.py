from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_LOCK = threading.RLock()


def utc_now_ts() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


class Storage:
    def __init__(self, db_path: str | None = None) -> None:
        data_dir = os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or os.getenv("DATA_DIR") or "./data"
        Path(data_dir).mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or str(Path(data_dir) / "eve_competition.db")
        self._initialise()

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _initialise(self) -> None:
        with _LOCK, self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    account_login TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    research_ends_at INTEGER NOT NULL,
                    trading_ends_at INTEGER NOT NULL,
                    selected_strategy TEXT,
                    strategy_snapshot_json TEXT,
                    last_bar_time INTEGER,
                    last_signal_bar_time INTEGER,
                    last_seen_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sessions_lookup
                    ON sessions(account_login, symbol, timeframe, started_at DESC);

                CREATE TABLE IF NOT EXISTS bars (
                    session_id TEXT NOT NULL,
                    bar_time INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    tick_volume REAL NOT NULL DEFAULT 0,
                    spread REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY(session_id, bar_time),
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_bars_session_time
                    ON bars(session_id, bar_time);

                CREATE TABLE IF NOT EXISTS candidate_scores (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    evaluated_at INTEGER NOT NULL,
                    strategy TEXT NOT NULL,
                    score REAL NOT NULL,
                    metrics_json TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_session_time
                    ON candidate_scores(session_id, evaluated_at DESC);

                CREATE TABLE IF NOT EXISTS signals (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    bar_time INTEGER NOT NULL,
                    strategy TEXT NOT NULL,
                    action TEXT NOT NULL,
                    sl REAL NOT NULL,
                    tp REAL NOT NULL,
                    confidence REAL NOT NULL,
                    reasons_json TEXT NOT NULL,
                    indicator_snapshot_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ISSUED',
                    created_at INTEGER NOT NULL,
                    acknowledged_at INTEGER,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_dedupe
                    ON signals(session_id, bar_time, action, strategy);

                CREATE TABLE IF NOT EXISTS deals (
                    deal_ticket TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    signal_id TEXT,
                    order_ticket TEXT,
                    position_id TEXT,
                    deal_type TEXT NOT NULL,
                    entry_type TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    volume REAL NOT NULL,
                    price REAL NOT NULL,
                    profit REAL NOT NULL DEFAULT 0,
                    commission REAL NOT NULL DEFAULT 0,
                    swap REAL NOT NULL DEFAULT 0,
                    comment TEXT,
                    deal_time INTEGER NOT NULL,
                    raw_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_deals_session_time
                    ON deals(session_id, deal_time);
                CREATE INDEX IF NOT EXISTS idx_deals_position
                    ON deals(session_id, position_id, deal_time);

                CREATE TABLE IF NOT EXISTS engine_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    event TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details_json TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_logs_session_time
                    ON engine_logs(session_id, created_at DESC);
                """
            )

    def get_or_create_session(self, account_login: str, symbol: str, timeframe: str) -> dict[str, Any]:
        now = utc_now_ts()
        with _LOCK, self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM sessions
                WHERE account_login=? AND symbol=? AND timeframe=? AND trading_ends_at>?
                ORDER BY started_at DESC LIMIT 1
                """,
                (account_login, symbol, timeframe, now),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE sessions SET last_seen_at=?, updated_at=? WHERE id=?",
                    (now, now, row["id"]),
                )
                return dict(row)

            session_id = uuid.uuid4().hex[:16]
            conn.execute(
                """
                INSERT INTO sessions(
                    id,account_login,symbol,timeframe,started_at,research_ends_at,trading_ends_at,
                    last_seen_at,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)
                """,
                (session_id, account_login, symbol, timeframe, now, now + 3600, now + 7200, now, now, now),
            )
            return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone())

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
            return dict(row) if row else None

    def latest_session(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM sessions ORDER BY started_at DESC LIMIT 1").fetchone()
            return dict(row) if row else None

    def touch_session(self, session_id: str, last_bar_time: int | None = None) -> None:
        now = utc_now_ts()
        with _LOCK, self.connection() as conn:
            if last_bar_time is None:
                conn.execute(
                    "UPDATE sessions SET last_seen_at=?,updated_at=? WHERE id=?",
                    (now, now, session_id),
                )
            else:
                conn.execute(
                    "UPDATE sessions SET last_seen_at=?,last_bar_time=?,updated_at=? WHERE id=?",
                    (now, last_bar_time, now, session_id),
                )

    def set_selected_strategy(self, session_id: str, name: str, snapshot: dict[str, Any]) -> None:
        now = utc_now_ts()
        with _LOCK, self.connection() as conn:
            conn.execute(
                """
                UPDATE sessions SET selected_strategy=?,strategy_snapshot_json=?,updated_at=? WHERE id=?
                """,
                (name, _json(snapshot), now, session_id),
            )

    def set_last_signal_bar(self, session_id: str, bar_time: int) -> None:
        with _LOCK, self.connection() as conn:
            conn.execute(
                "UPDATE sessions SET last_signal_bar_time=?,updated_at=? WHERE id=?",
                (bar_time, utc_now_ts(), session_id),
            )

    def upsert_bars(self, session_id: str, bars: Iterable[dict[str, Any]]) -> int:
        rows = []
        for bar in bars:
            rows.append(
                (
                    session_id,
                    int(bar["time"]),
                    float(bar["open"]),
                    float(bar["high"]),
                    float(bar["low"]),
                    float(bar["close"]),
                    float(bar.get("tick_volume", 0)),
                    float(bar.get("spread", 0)),
                )
            )
        if not rows:
            return 0
        with _LOCK, self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO bars(session_id,bar_time,open,high,low,close,tick_volume,spread)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id,bar_time) DO UPDATE SET
                    open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,
                    tick_volume=excluded.tick_volume,spread=excluded.spread
                """,
                rows,
            )
        return len(rows)

    def get_bars(self, session_id: str, limit: int = 600) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT bar_time AS time,open,high,low,close,tick_volume,spread
                FROM bars WHERE session_id=? ORDER BY bar_time DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def save_candidate_scores(self, session_id: str, results: list[dict[str, Any]]) -> None:
        if not results:
            return
        now = utc_now_ts()
        with _LOCK, self.connection() as conn:
            conn.executemany(
                """
                INSERT INTO candidate_scores(session_id,evaluated_at,strategy,score,metrics_json)
                VALUES(?,?,?,?,?)
                """,
                [(session_id, now, r["name"], float(r["score"]), _json(r)) for r in results],
            )

    def latest_candidate_scores(self, session_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            stamp = conn.execute(
                "SELECT MAX(evaluated_at) AS t FROM candidate_scores WHERE session_id=?",
                (session_id,),
            ).fetchone()["t"]
            if stamp is None:
                return []
            rows = conn.execute(
                """
                SELECT metrics_json FROM candidate_scores
                WHERE session_id=? AND evaluated_at=? ORDER BY score DESC
                """,
                (session_id, stamp),
            ).fetchall()
        return [json.loads(r["metrics_json"]) for r in rows]

    def create_signal(
        self,
        session_id: str,
        bar_time: int,
        strategy: str,
        action: str,
        sl: float,
        tp: float,
        confidence: float,
        reasons: list[str],
        indicator_snapshot: dict[str, Any],
    ) -> dict[str, Any] | None:
        signal_id = uuid.uuid4().hex[:12]
        now = utc_now_ts()
        with _LOCK, self.connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO signals(
                        id,session_id,bar_time,strategy,action,sl,tp,confidence,reasons_json,
                        indicator_snapshot_json,status,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        signal_id,
                        session_id,
                        bar_time,
                        strategy,
                        action,
                        sl,
                        tp,
                        confidence,
                        _json(reasons),
                        _json(indicator_snapshot),
                        "ISSUED",
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT * FROM signals WHERE session_id=? AND bar_time=? AND action=? AND strategy=?
                    """,
                    (session_id, bar_time, action, strategy),
                ).fetchone()
                return dict(row) if row and row["status"] == "ISSUED" else None
            return dict(conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone())

    def acknowledge_signal(self, signal_id: str, status: str = "ACKNOWLEDGED") -> None:
        with _LOCK, self.connection() as conn:
            conn.execute(
                "UPDATE signals SET status=?,acknowledged_at=? WHERE id=?",
                (status, utc_now_ts(), signal_id),
            )


    def pending_signal(self, session_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM signals WHERE session_id=? AND status='ISSUED'
                ORDER BY created_at ASC LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_signal(self, signal_id: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
            return dict(row) if row else None

    def save_deal(self, payload: dict[str, Any]) -> bool:
        now = utc_now_ts()
        with _LOCK, self.connection() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO deals(
                        deal_ticket,session_id,signal_id,order_ticket,position_id,deal_type,entry_type,
                        symbol,volume,price,profit,commission,swap,comment,deal_time,raw_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(payload["deal_ticket"]),
                        payload["session_id"],
                        payload.get("signal_id") or None,
                        str(payload.get("order_ticket", "")),
                        str(payload.get("position_id", "")),
                        payload.get("deal_type", "UNKNOWN"),
                        payload.get("entry_type", "UNKNOWN"),
                        payload.get("symbol", ""),
                        float(payload.get("volume", 0)),
                        float(payload.get("price", 0)),
                        float(payload.get("profit", 0)),
                        float(payload.get("commission", 0)),
                        float(payload.get("swap", 0)),
                        payload.get("comment", ""),
                        int(payload.get("deal_time", now)),
                        _json(payload),
                        now,
                    ),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def add_log(
        self,
        session_id: str,
        event: str,
        message: str,
        level: str = "INFO",
        details: dict[str, Any] | None = None,
    ) -> None:
        with _LOCK, self.connection() as conn:
            conn.execute(
                """
                INSERT INTO engine_logs(session_id,created_at,level,event,message,details_json)
                VALUES(?,?,?,?,?,?)
                """,
                (session_id, utc_now_ts(), level, event, message, _json(details or {})),
            )

    def session_signals(self, session_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE session_id=? ORDER BY created_at DESC",
                (session_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(item.pop("reasons_json"))
            item["indicator_snapshot"] = json.loads(item.pop("indicator_snapshot_json"))
            result.append(item)
        return result

    def session_deals(self, session_id: str) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM deals WHERE session_id=? ORDER BY deal_time ASC",
                (session_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def session_logs(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT created_at,level,event,message,details_json FROM engine_logs
                WHERE session_id=? ORDER BY created_at DESC LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item.pop("details_json") or "{}")
            result.append(item)
        return result
