from __future__ import annotations

import logging
import os
import resource
import tracemalloc
from typing import Any

logger = logging.getLogger(__name__)

_TRACEMALLOC_ENABLED = os.getenv("EVE_RESEARCH_TRACEMALLOC", "false").strip().lower() in {"1", "true", "yes", "on"}
if _TRACEMALLOC_ENABLED and not tracemalloc.is_tracing():
    tracemalloc.start()


def current_rss_mb() -> float:
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes. Railway is Linux, but keep this portable.
    return usage / (1024.0 * 1024.0) if usage > 10_000_000 else usage / 1024.0


def memory_snapshot() -> dict[str, float]:
    snapshot = {"rss_mb": round(current_rss_mb(), 3)}
    if tracemalloc.is_tracing():
        current, peak = tracemalloc.get_traced_memory()
        snapshot.update({"tracemalloc_current_mb": round(current / 1_048_576, 3), "tracemalloc_peak_mb": round(peak / 1_048_576, 3)})
    return snapshot


def log_memory(stage: str, **extra: Any) -> None:
    logger.info("research memory checkpoint", extra={"research_memory_stage": stage, **memory_snapshot(), **extra})
