from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

T = TypeVar("T")


def bounded_tail(items: Iterable[T], limit: int | None) -> list[T]:
    """Return at most the most recent ``limit`` items without exposing full history."""
    values = list(items)
    if limit is None or limit <= 0 or len(values) <= limit:
        return values
    return values[-limit:]


def compact_trade_summary(trades: list[dict[str, Any]], examples: int = 5) -> dict[str, Any]:
    """Summarise historical trades while keeping only a small example tail."""
    m1_count = sum(1 for trade in trades if trade.get("execution_source") == "M1")
    m5_count = sum(1 for trade in trades if trade.get("execution_source") == "M5")
    return {
        "trade_count": len(trades),
        "m1_trade_simulation_count": m1_count,
        "m5_fallback_trade_simulation_count": m5_count,
        "trade_examples": bounded_tail(trades, examples),
    }
