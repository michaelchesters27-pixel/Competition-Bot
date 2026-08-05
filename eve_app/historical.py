from __future__ import annotations

import importlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

Bar = dict[str, Any]


def _env(name: str, default: str) -> str:
    return os.getenv(name, default).strip()


def _ts(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return int(value.timestamp())
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _dt(timestamp: int) -> datetime:
    return datetime.fromtimestamp(int(timestamp), timezone.utc)


def _floor_m5(timestamp: int) -> int:
    return timestamp - timestamp % 300


@dataclass(frozen=True)
class HistoricalConfig:
    database_url: str
    table: str = "public.market_candles"
    symbol_column: str = "symbol"
    timeframe_column: str = "interval"
    time_column: str = "candle_time"
    open_column: str = "open"
    high_column: str = "high"
    low_column: str = "low"
    close_column: str = "close"
    volume_column: str = "volume"
    spread_column: str = ""
    source_column: str = "source"
    complete_column: str = "is_complete"
    preferred_source: str = "twelve_data"
    filter_preferred_source: bool = True
    m1_value: str = "M1"
    m5_value: str = "M5"
    lookback_days: int | None = None
    chunk_days: int = 30
    timeout_seconds: int = 20
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "HistoricalConfig | None":
        enabled = _env("EVE_HISTORICAL_ENABLED", "true").lower() not in {"0", "false", "no", "off"}
        url = _env("EVE_MARKET_CANDLES_DATABASE_URL", "")
        if not enabled or not url:
            return None
        lookback_raw = _env("EVE_HISTORICAL_LOOKBACK_DAYS", "")
        lookback_days = int(lookback_raw) if lookback_raw else None
        return cls(
            database_url=url,
            table=_env("EVE_MARKET_CANDLES_TABLE", "public.market_candles"),
            symbol_column=_env("EVE_MARKET_CANDLES_SYMBOL_COLUMN", "symbol"),
            timeframe_column=_env("EVE_MARKET_CANDLES_TIMEFRAME_COLUMN", "interval"),
            time_column=_env("EVE_MARKET_CANDLES_TIME_COLUMN", "candle_time"),
            open_column=_env("EVE_MARKET_CANDLES_OPEN_COLUMN", "open"),
            high_column=_env("EVE_MARKET_CANDLES_HIGH_COLUMN", "high"),
            low_column=_env("EVE_MARKET_CANDLES_LOW_COLUMN", "low"),
            close_column=_env("EVE_MARKET_CANDLES_CLOSE_COLUMN", "close"),
            volume_column=_env("EVE_MARKET_CANDLES_VOLUME_COLUMN", "volume"),
            spread_column=_env("EVE_MARKET_CANDLES_SPREAD_COLUMN", ""),
            source_column=_env("EVE_MARKET_CANDLES_SOURCE_COLUMN", "source"),
            complete_column=_env("EVE_MARKET_CANDLES_COMPLETE_COLUMN", "is_complete"),
            preferred_source=_env("EVE_MARKET_CANDLES_SOURCE_VALUE", "twelve_data"),
            filter_preferred_source=_env("EVE_MARKET_CANDLES_FILTER_SOURCE", "true").lower() not in {"0", "false", "no", "off"},
            m1_value=_env("EVE_MARKET_CANDLES_M1_VALUE", "M1"),
            m5_value=_env("EVE_MARKET_CANDLES_M5_VALUE", "M5"),
            lookback_days=lookback_days,
            chunk_days=max(1, int(_env("EVE_HISTORICAL_CHUNK_DAYS", "30"))),
            timeout_seconds=int(_env("EVE_HISTORICAL_QUERY_TIMEOUT_SECONDS", "20")),
            enabled=enabled,
        )


@dataclass(frozen=True)
class HistoricalDataset:
    m5_bars: list[Bar]
    m1_bars: list[Bar]
    metadata: dict[str, Any]

    @property
    def valid(self) -> bool:
        return bool(self.metadata.get("valid"))


class SupabaseMarketCandles:
    def __init__(self, config: HistoricalConfig) -> None:
        self._psycopg = importlib.import_module("psycopg")
        rows_module = importlib.import_module("psycopg.rows")
        self._dict_row = rows_module.dict_row
        self.config = config
        self._table = self._safe_table(config.table)

    @staticmethod
    def _safe_identifier(value: str) -> str:
        if not value.replace("_", "").isalnum() or not value:
            raise ValueError(f"Unsafe SQL identifier: {value!r}")
        return f'"{value}"'

    @classmethod
    def _safe_table(cls, value: str) -> str:
        parts = value.split(".")
        if len(parts) not in {1, 2}:
            raise ValueError("Historical candle table must be table or schema.table")
        return ".".join(cls._safe_identifier(part) for part in parts)

    def _select_sql(self, use_source_filter: bool = True) -> str:
        c = self.config
        cols = {
            "time": c.time_column,
            "open": c.open_column,
            "high": c.high_column,
            "low": c.low_column,
            "close": c.close_column,
            "tick_volume": c.volume_column,
        }
        selected = [f"{self._safe_identifier(src)} AS {self._safe_identifier(alias)}" for alias, src in cols.items()]
        if c.spread_column:
            selected.append(f"{self._safe_identifier(c.spread_column)} AS {self._safe_identifier('spread')}")
        else:
            selected.append(f"0::double precision AS {self._safe_identifier('spread')}")
        source_clause = f"\n              AND {self._safe_identifier(c.source_column)} = %(source)s" if use_source_filter and c.filter_preferred_source else ""
        return f"""
            SELECT {','.join(selected)}
            FROM {self._table}
            WHERE {self._safe_identifier(c.symbol_column)} = %(symbol)s
              AND {self._safe_identifier(c.timeframe_column)} = %(timeframe)s
              AND {self._safe_identifier(c.complete_column)} = true
              AND {self._safe_identifier(c.time_column)} >= %(start_time)s
              AND {self._safe_identifier(c.time_column)} < %(end_time)s{source_clause}
            ORDER BY {self._safe_identifier(c.time_column)} ASC
        """

    def _connect(self):
        return self._psycopg.connect(self.config.database_url, row_factory=self._dict_row)

    def _fetch(self, symbol: str, timeframe: str, start_ts: int, end_ts: int, use_source_filter: bool = True) -> list[Bar]:
        params = {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": self.config.preferred_source,
            "start_time": _dt(start_ts),
            "end_time": _dt(end_ts),
        }
        with self._connect() as conn:
            conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            conn.execute("SET statement_timeout = %s", (self.config.timeout_seconds * 1000,))
            with conn.transaction():
                rows = conn.execute(self._select_sql(use_source_filter), params).fetchall()
        return normalize_bars(rows)

    def earliest_time(self, symbol: str, timeframe: str) -> int | None:
        c = self.config
        sql = f"""
            SELECT MIN({self._safe_identifier(c.time_column)}) AS earliest_time
            FROM {self._table}
            WHERE {self._safe_identifier(c.symbol_column)} = %(symbol)s
              AND {self._safe_identifier(c.timeframe_column)} = %(timeframe)s
              AND {self._safe_identifier(c.complete_column)} = true
        """
        with self._connect() as conn:
            conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            with conn.transaction():
                row = conn.execute(sql, {"symbol": symbol, "timeframe": timeframe}).fetchone()
        return _ts(row["earliest_time"]) if row and row.get("earliest_time") else None

    def _fetch_chunked(self, symbol: str, timeframe: str, start_ts: int, end_ts: int) -> list[Bar]:
        out: list[Bar] = []
        step = self.config.chunk_days * 86400
        cursor = start_ts
        while cursor < end_ts:
            chunk_end = min(end_ts, cursor + step)
            rows = self._fetch(symbol, timeframe, cursor, chunk_end, use_source_filter=True)
            if not rows and self.config.filter_preferred_source:
                rows = self._fetch(symbol, timeframe, cursor, chunk_end, use_source_filter=False)
            out.extend(rows)
            cursor = chunk_end
        return normalize_bars(out)

    def get_research_dataset(self, symbol: str, end_ts: int) -> HistoricalDataset:
        earliest_m5 = self.earliest_time(symbol, self.config.m5_value)
        earliest_m1 = self.earliest_time(symbol, self.config.m1_value)
        earliest = min([t for t in (earliest_m5, earliest_m1) if t is not None], default=None)
        if earliest is None:
            return HistoricalDataset([], [], {"valid": False, "reason": "No completed historical candles found", "source_table": self.config.table})
        start_ts = earliest
        if self.config.lookback_days is not None:
            start_ts = max(start_ts, end_ts - self.config.lookback_days * 86400)
        m5 = self._fetch_chunked(symbol, self.config.m5_value, start_ts, end_ts)
        m1 = self._fetch_chunked(symbol, self.config.m1_value, start_ts, end_ts)
        source = "STORED_M5"
        if not m5 and m1:
            m5 = build_m5_from_m1(m1)
            source = "BUILT_FROM_M1_FALLBACK"
        valid = bool(m5)
        return HistoricalDataset(
            m5_bars=m5,
            m1_bars=m1,
            metadata={
                "valid": valid,
                "reason": "OK" if valid else "No completed M5 candles available and M1 fallback unavailable",
                "source_table": self.config.table,
                "m5_source": source,
                "m1_available": bool(m1),
                "spread_source": "COLUMN" if self.config.spread_column else "ASSUMED",
                "preferred_source": self.config.preferred_source if self.config.filter_preferred_source else "DISABLED",
                "lookback_days": self.config.lookback_days,
                "chunk_days": self.config.chunk_days,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "m5_bars": len(m5),
                "m1_bars": len(m1),
            },
        )


def normalize_bars(rows: Iterable[dict[str, Any]]) -> list[Bar]:
    bars = []
    for row in rows:
        bars.append(
            {
                "time": _ts(row["time"]),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "tick_volume": float(row.get("tick_volume") or 0),
                "spread": float(row.get("spread") or 0),
            }
        )
    return sorted(bars, key=lambda bar: int(bar["time"]))


def build_m5_from_m1(m1_bars: list[Bar]) -> list[Bar]:
    buckets: dict[int, list[Bar]] = {}
    for bar in normalize_bars(m1_bars):
        buckets.setdefault(_floor_m5(int(bar["time"])), []).append(bar)
    out: list[Bar] = []
    for bucket_time in sorted(buckets):
        rows = buckets[bucket_time]
        if not rows:
            continue
        out.append(
            {
                "time": bucket_time,
                "open": float(rows[0]["open"]),
                "high": max(float(row["high"]) for row in rows),
                "low": min(float(row["low"]) for row in rows),
                "close": float(rows[-1]["close"]),
                "tick_volume": sum(float(row.get("tick_volume") or 0) for row in rows),
                "spread": sum(float(row.get("spread") or 0) for row in rows) / len(rows),
            }
        )
    return out
