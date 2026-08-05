from __future__ import annotations

import importlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

Bar = dict[str, Any]

logger = logging.getLogger(__name__)


def _safe_database_identity(database_url: str) -> dict[str, str]:
    parsed = urlparse(database_url)
    database = parsed.path.lstrip("/") or ""
    return {"host": parsed.hostname or "", "database": database}


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
    symbol_value: str = ""
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
    m1_value: str = "1min"
    m5_value: str = "5min"
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
            symbol_value=_env("EVE_MARKET_CANDLES_SYMBOL", ""),
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
            m1_value=_env("EVE_MARKET_CANDLES_M1_VALUE", "1min"),
            m5_value=_env("EVE_MARKET_CANDLES_M5_VALUE", "5min"),
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
    m1_window_provider: Callable[[int, int], list[Bar]] | None = None

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
        db_identity = _safe_database_identity(config.database_url)
        logger.info(
            "historical database configured",
            extra={
                "historical_db_host": db_identity["host"],
                "historical_db_name": db_identity["database"],
                "historical_source_table": config.table,
            },
        )

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
            timeout_ms = max(0, int(self.config.timeout_seconds) * 1000)
            conn.execute(f"SET statement_timeout = {timeout_ms}")
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
        params = {"symbol": symbol, "timeframe": timeframe}
        logger.info(
            "historical earliest_time attempt",
            extra={
                "historical_symbol": symbol,
                "historical_interval": timeframe,
                "historical_sql_params": params,
            },
        )
        with self._connect() as conn:
            conn.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
            with conn.transaction():
                row = conn.execute(sql, params).fetchone()
        earliest = _ts(row["earliest_time"]) if row and row.get("earliest_time") else None
        logger.info(
            "historical earliest_time result",
            extra={
                "historical_symbol": symbol,
                "historical_interval": timeframe,
                "historical_sql_params": params,
                "historical_earliest_time": earliest,
            },
        )
        return earliest

    def _iter_chunks(self, symbol: str, timeframe: str, start_ts: int, end_ts: int) -> Iterable[list[Bar]]:
        step = self.config.chunk_days * 86400
        cursor = start_ts
        while cursor < end_ts:
            chunk_end = min(end_ts, cursor + step)
            rows = self._fetch(symbol, timeframe, cursor, chunk_end, use_source_filter=True)
            if not rows and self.config.filter_preferred_source:
                rows = self._fetch(symbol, timeframe, cursor, chunk_end, use_source_filter=False)
            yield normalize_bars(rows)
            cursor = chunk_end

    def _fetch_chunked(self, symbol: str, timeframe: str, start_ts: int, end_ts: int) -> list[Bar]:
        out: list[Bar] = []
        for rows in self._iter_chunks(symbol, timeframe, start_ts, end_ts):
            out.extend(rows)
        return normalize_bars(out)

    def _fetch_window(self, symbol: str, timeframe: str, start_ts: int, end_ts: int) -> list[Bar]:
        rows = self._fetch(symbol, timeframe, start_ts, end_ts, use_source_filter=True)
        if not rows and self.config.filter_preferred_source:
            rows = self._fetch(symbol, timeframe, start_ts, end_ts, use_source_filter=False)
        return normalize_bars(rows)

    @staticmethod
    def _symbol_candidates(symbol: str) -> list[str]:
        stripped = symbol.strip()
        candidates = [stripped]
        compact = stripped.replace("/", "")
        if compact.upper() == "XAUUSD":
            variant = "XAU/USD" if "/" not in stripped else "XAUUSD"
            candidates.append(variant)
        return list(dict.fromkeys(candidate for candidate in candidates if candidate))

    def _earliest_for_symbol(self, symbol: str) -> tuple[int | None, int | None]:
        earliest_m5 = self.earliest_time(symbol, self.config.m5_value)
        earliest_m1 = self.earliest_time(symbol, self.config.m1_value)
        return earliest_m5, earliest_m1

    def get_research_dataset(self, symbol: str, end_ts: int) -> HistoricalDataset:
        symbol_candidates = [self.config.symbol_value] if self.config.symbol_value else self._symbol_candidates(symbol)
        query_symbol = symbol_candidates[0]
        earliest_m5: int | None = None
        earliest_m1: int | None = None
        earliest: int | None = None
        for candidate in symbol_candidates:
            candidate_m5, candidate_m1 = self._earliest_for_symbol(candidate)
            candidate_earliest = min([t for t in (candidate_m5, candidate_m1) if t is not None], default=None)
            query_symbol = candidate
            earliest_m5 = candidate_m5
            earliest_m1 = candidate_m1
            earliest = candidate_earliest
            if candidate_earliest is not None:
                break
        if earliest is None:
            reason = (
                f"No completed historical candles found for symbol={query_symbol!r}, "
                f"M5 interval={self.config.m5_value!r}, M1 interval={self.config.m1_value!r}"
            )
            return HistoricalDataset(
                [],
                [],
                {
                    "valid": False,
                    "reason": reason,
                    "source_table": self.config.table,
                    "requested_symbol": symbol,
                    "query_symbol": query_symbol,
                    "m5_interval": self.config.m5_value,
                    "m1_interval": self.config.m1_value,
                    "earliest_m5": earliest_m5,
                    "earliest_m1": earliest_m1,
                    "m5_bars": 0,
                    "m1_bars": 0,
                },
            )
        from .memory import log_memory

        start_ts = earliest_m5 if earliest_m5 is not None else earliest
        if self.config.lookback_days is not None:
            start_ts = max(start_ts, end_ts - self.config.lookback_days * 86400)
        log_memory("before_m5_loading", m5_candles_processed=0, configured_chunk_days=self.config.chunk_days)
        m5: list[Bar] = []
        processed = 0
        for chunk in self._iter_chunks(query_symbol, self.config.m5_value, start_ts, end_ts):
            m5.extend(chunk)
            processed += len(chunk)
            logger.info("historical research progress", extra={"m5_candles_processed": processed, "current_date_range": f"{_dt(start_ts).date().isoformat()} to {_dt(end_ts).date().isoformat()}", "strategies_completed": 0, "configured_chunk_days": self.config.chunk_days})
            del chunk
        m5 = normalize_bars(m5)
        log_memory("after_m5_loading", m5_candles_processed=len(m5), configured_chunk_days=self.config.chunk_days)
        source = "STORED_M5"
        m1_window_provider = (lambda window_start, window_end: self._fetch_window(query_symbol, self.config.m1_value, int(window_start), int(window_end))) if earliest_m1 is not None else None
        valid = bool(m5)
        reason = "OK" if valid else "No completed M5 candles available and M1 fallback unavailable"
        return HistoricalDataset(
            m5_bars=m5,
            m1_bars=[],
            metadata={
                "valid": valid,
                "reason": reason,
                "source_table": self.config.table,
                "requested_symbol": symbol,
                "query_symbol": query_symbol,
                "m5_interval": self.config.m5_value,
                "m1_interval": self.config.m1_value,
                "earliest_m5": earliest_m5,
                "earliest_m1": earliest_m1,
                "m5_source": source,
                "m1_available": earliest_m1 is not None,
                "spread_source": "COLUMN" if self.config.spread_column else "ASSUMED",
                "preferred_source": self.config.preferred_source if self.config.filter_preferred_source else "DISABLED",
                "lookback_days": self.config.lookback_days,
                "chunk_days": self.config.chunk_days,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "m5_bars": len(m5),
                "m1_bars": 0,
                "m1_loading_mode": "ON_DEMAND_WINDOWS" if earliest_m1 is not None else "UNAVAILABLE",
            },
            m1_window_provider=m1_window_provider,
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
