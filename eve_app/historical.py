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


def _iso(timestamp: int) -> str:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).isoformat()


def _floor_m5(timestamp: int) -> int:
    return timestamp - timestamp % 300


@dataclass(frozen=True)
class HistoricalConfig:
    supabase_url: str
    service_role_key: str
    table: str = "market_candles"
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
    page_size: int = 1000
    enabled: bool = True

    @classmethod
    def from_env(cls) -> "HistoricalConfig | None":
        enabled = _env("EVE_HISTORICAL_ENABLED", "true").lower() not in {"0", "false", "no", "off"}
        url = _env("SUPABASE_URL", "")
        key = _env("SUPABASE_SERVICE_ROLE_KEY", "")
        if not enabled or not url or not key:
            return None
        lookback_raw = _env("EVE_HISTORICAL_LOOKBACK_DAYS", "")
        lookback_days = int(lookback_raw) if lookback_raw else None
        return cls(
            supabase_url=url,
            service_role_key=key,
            table=_env("EVE_MARKET_CANDLES_TABLE", "market_candles"),
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
            page_size=max(1, int(_env("EVE_HISTORICAL_PAGE_SIZE", "1000"))),
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
        module = importlib.import_module("supabase")
        self.client = module.create_client(config.supabase_url, config.service_role_key)
        self.config = config

    @staticmethod
    def _safe_identifier(value: str) -> str:
        if not value.replace("_", "").isalnum() or not value:
            raise ValueError(f"Unsafe Supabase column/table identifier: {value!r}")
        return value

    def _table_name(self) -> str:
        table = self.config.table.split(".")[-1]
        return self._safe_identifier(table)

    def _select_columns(self) -> str:
        c = self.config
        columns = [
            c.time_column,
            c.open_column,
            c.high_column,
            c.low_column,
            c.close_column,
            c.volume_column,
        ]
        if c.spread_column:
            columns.append(c.spread_column)
        return ",".join(self._safe_identifier(column) for column in columns)

    def _query(self, symbol: str, timeframe: str, start_ts: int, end_ts: int, use_source_filter: bool, offset: int, limit: int):
        c = self.config
        query = (
            self.client.table(self._table_name())
            .select(self._select_columns())
            .eq(self._safe_identifier(c.symbol_column), symbol)
            .eq(self._safe_identifier(c.timeframe_column), timeframe)
            .eq(self._safe_identifier(c.complete_column), True)
            .gte(self._safe_identifier(c.time_column), _iso(start_ts))
            .lt(self._safe_identifier(c.time_column), _iso(end_ts))
            .order(self._safe_identifier(c.time_column), desc=False)
            .range(offset, offset + limit - 1)
        )
        if use_source_filter and c.filter_preferred_source:
            query = query.eq(self._safe_identifier(c.source_column), c.preferred_source)
        return query.execute()

    def _fetch(self, symbol: str, timeframe: str, start_ts: int, end_ts: int, use_source_filter: bool = True) -> list[Bar]:
        rows: list[dict[str, Any]] = []
        offset = 0
        while True:
            response = self._query(symbol, timeframe, start_ts, end_ts, use_source_filter, offset, self.config.page_size)
            batch = list(response.data or [])
            rows.extend(batch)
            if len(batch) < self.config.page_size:
                break
            offset += self.config.page_size
        return normalize_bars(rows, self.config)

    def earliest_time(self, symbol: str, timeframe: str) -> int | None:
        c = self.config
        response = (
            self.client.table(self._table_name())
            .select(self._safe_identifier(c.time_column))
            .eq(self._safe_identifier(c.symbol_column), symbol)
            .eq(self._safe_identifier(c.timeframe_column), timeframe)
            .eq(self._safe_identifier(c.complete_column), True)
            .order(self._safe_identifier(c.time_column), desc=False)
            .limit(1)
            .execute()
        )
        rows = list(response.data or [])
        return _ts(rows[0][c.time_column]) if rows else None

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
                "access_method": "SUPABASE_REST_CLIENT",
                "source_table": self.config.table,
                "m5_source": source,
                "m1_available": bool(m1),
                "spread_source": "COLUMN" if self.config.spread_column else "ASSUMED",
                "preferred_source": self.config.preferred_source if self.config.filter_preferred_source else "DISABLED",
                "lookback_days": self.config.lookback_days,
                "chunk_days": self.config.chunk_days,
                "page_size": self.config.page_size,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "m5_bars": len(m5),
                "m1_bars": len(m1),
            },
        )


def normalize_bars(rows: Iterable[dict[str, Any]], config: HistoricalConfig | None = None) -> list[Bar]:
    config = config or HistoricalConfig(supabase_url="", service_role_key="")
    bars = []
    for row in rows:
        bars.append(
            {
                "time": _ts(row[config.time_column] if config.time_column in row else row["time"]),
                "open": float(row[config.open_column] if config.open_column in row else row["open"]),
                "high": float(row[config.high_column] if config.high_column in row else row["high"]),
                "low": float(row[config.low_column] if config.low_column in row else row["low"]),
                "close": float(row[config.close_column] if config.close_column in row else row["close"]),
                "tick_volume": float(row[config.volume_column] if config.volume_column in row else row.get("tick_volume") or 0),
                "spread": float(row[config.spread_column] if config.spread_column and config.spread_column in row else row.get("spread") or 0),
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
