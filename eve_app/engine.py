from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .historical import HistoricalConfig, HistoricalDataset, SupabaseMarketCandles
from .memory import bounded_tail
from .storage import Storage, utc_now_ts
from .strategies import build_playbook_snapshot, evaluate_candidates, evaluate_live_playbook


@dataclass(frozen=True)
class PhaseState:
    phase: str
    elapsed_seconds: int
    remaining_seconds: int


def phase_state(session: dict[str, Any], now: int | None = None) -> PhaseState:
    current = utc_now_ts() if now is None else int(now)
    started = int(session["started_at"])
    research_end = int(session["research_ends_at"])
    trading_end = int(session["trading_ends_at"])
    elapsed = max(0, current - started)
    if current < research_end:
        return PhaseState("RESEARCH", elapsed, research_end - current)
    if current < trading_end:
        return PhaseState("TRADING", elapsed, trading_end - current)
    return PhaseState("COMPLETE", elapsed, 0)


class CompetitionEngine:
    def __init__(self, storage: Storage, historical_source: SupabaseMarketCandles | None = None) -> None:
        self.storage = storage
        if historical_source is not None:
            self.historical_source = historical_source
        else:
            config = HistoricalConfig.from_env()
            self.historical_source = SupabaseMarketCandles(config) if config else None

    def _historical_dataset(self, session: dict[str, Any], fallback_bars: list[dict[str, Any]], end_ts: int) -> HistoricalDataset:
        if not self.historical_source:
            self.storage.add_log(
                session["id"],
                "HISTORICAL_DATA_UNAVAILABLE",
                "Supabase historical source is not configured; research promotion is disabled.",
                level="WARNING",
                details={"reason": "Supabase historical source is not configured"},
            )
            return HistoricalDataset(
                m5_bars=[],
                m1_bars=[],
                metadata={"valid": False, "source": "NONE", "reason": "Supabase historical source is not configured"},
            )
        try:
            dataset = self.historical_source.get_research_dataset(str(session["symbol"]), end_ts)
            if dataset.valid:
                return dataset
            reason = str(dataset.metadata.get("reason") or "Historical dataset is invalid")
            self.storage.add_log(
                session["id"],
                "HISTORICAL_DATA_UNAVAILABLE",
                f"Historical dataset rejected: {reason}",
                level="WARNING",
                details=dataset.metadata,
            )
            return dataset
        except Exception as exc:
            self.storage.add_log(
                session["id"],
                "HISTORICAL_DATA_UNAVAILABLE",
                f"Historical candle database unavailable; research promotion is disabled: {exc}",
                level="WARNING",
                details={"valid": False, "error": str(exc)},
            )
            return HistoricalDataset(
                m5_bars=[],
                m1_bars=[],
                metadata={"valid": False, "source": "NONE", "reason": str(exc)},
            )

    def start_or_resume(
        self, account_login: str, symbol: str, timeframe: str, launch_id: str
    ) -> dict[str, Any]:
        session = self.storage.get_or_create_session(account_login, symbol, timeframe, launch_id)
        existing_logs = self.storage.session_logs(session["id"], limit=1)
        if not existing_logs:
            self.storage.add_log(
                session["id"],
                "SESSION_STARTED",
                "Fresh two-hour competition run started from this EA attachment. Research is locked for 60 minutes.",
                details={
                    "research_seconds": 3600,
                    "trading_seconds": 3600,
                    "timeframe": timeframe,
                    "launch_id": launch_id,
                    "engine_version": "2.00",
                },
            )
        return self._session_payload(session)

    def process_pulse(self, payload: dict[str, Any]) -> dict[str, Any]:
        session_id = str(payload["session_id"])
        session = self.storage.get_session(session_id)
        if not session:
            raise KeyError("Unknown session")

        bars = payload.get("bars") or []
        if bars:
            self.storage.upsert_bars(session_id, bars)

        stored_bars = bounded_tail(self.storage.get_bars(session_id, limit=800), 800)
        # MT5 sends the currently forming bar as the last row, so [-2] is the last closed M5 candle.
        closed_bar_time = int(stored_bars[-2]["time"]) if len(stored_bars) >= 2 else None
        phase = phase_state(session)

        if phase.phase == "RESEARCH":
            if closed_bar_time and closed_bar_time != session.get("last_bar_time") and len(stored_bars) >= 80:
                evaluation_end = min(utc_now_ts(), int(session["research_ends_at"]))
                dataset = self._historical_dataset(session, stored_bars, evaluation_end)
                if not dataset.valid:
                    self.storage.touch_session(session_id, closed_bar_time)
                    return self._pulse_payload(session, phase_state(session), None)
                research_start = int(dataset.m5_bars[0]["time"])
                scores, _rows, regime = evaluate_candidates(
                    dataset.m5_bars, research_start, evaluation_end, dataset.m1_window_provider or dataset.m1_bars, dataset.metadata
                )
                self.storage.save_candidate_scores(session_id, scores)
                self.storage.touch_session(session_id, closed_bar_time)
                leader = scores[0] if scores else None
                if leader:
                    self.storage.add_log(
                        session_id,
                        "RESEARCH_UPDATE",
                        f"Research updated. Regime: {regime['name']}. Current playbook leader: {leader['label']}.",
                        details={
                            "leader": leader["name"],
                            "score": leader["score"],
                            "research_trades": leader["trades"],
                            "net_r": leader["net_r"],
                            "regime": regime,
                            "historical_data": dataset.metadata,
                        },
                    )
            else:
                self.storage.touch_session(session_id)

        session = self.storage.get_session(session_id) or session
        phase = phase_state(session)

        if phase.phase in ("TRADING", "COMPLETE") and not session.get("selected_strategy"):
            if len(stored_bars) >= 80:
                dataset = self._historical_dataset(session, stored_bars, int(session["research_ends_at"]))
                if not dataset.valid:
                    self.storage.touch_session(session_id)
                    return self._pulse_payload(session, phase_state(session), None)
                research_start = int(dataset.m5_bars[0]["time"])
                rankings, snapshot = build_playbook_snapshot(
                    dataset.m5_bars,
                    research_start,
                    int(session["research_ends_at"]),
                    dataset.m1_window_provider or dataset.m1_bars,
                    dataset.metadata,
                )
                self.storage.save_candidate_scores(session_id, rankings)
                self.storage.set_selected_strategy(session_id, "adaptive_research_playbook", snapshot)
                top_labels = [item["label"] for item in rankings[:3]]
                self.storage.add_log(
                    session_id,
                    "PLAYBOOK_FROZEN",
                    "Research finished. A ranked multi-strategy playbook was frozen instead of relying on one rare trigger.",
                    details={
                        "regime": snapshot.get("regime"),
                        "top_modules": top_labels,
                        "playbook": snapshot.get("playbook"),
                        "accepted_strategy_count": snapshot.get("accepted_strategy_count"),
                        "minimum_confidence": snapshot.get("minimum_confidence"),
                        "historical_data": dataset.metadata,
                    },
                )
                session = self.storage.get_session(session_id) or session

        pending = self.storage.pending_signal(session_id)
        if pending:
            return self._pulse_payload(session, phase_state(session), pending)

        if phase.phase == "TRADING" and session.get("selected_strategy") and closed_bar_time:
            signal_bar_closed_at = closed_bar_time + 300
            if (
                signal_bar_closed_at >= int(session["research_ends_at"])
                and closed_bar_time != session.get("last_signal_bar_time")
            ):
                try:
                    snapshot = json.loads(session.get("strategy_snapshot_json") or "{}")
                except json.JSONDecodeError:
                    snapshot = {}
                signal, decision = evaluate_live_playbook(
                    snapshot,
                    stored_bars,
                    float(payload.get("bid", 0)),
                    float(payload.get("ask", 0)),
                )
                self.storage.set_last_signal_bar(session_id, closed_bar_time)

                if signal:
                    saved = self.storage.create_signal(
                        session_id=session_id,
                        bar_time=int(signal["bar_time"]),
                        strategy=str(signal["strategy"]),
                        action=str(signal["action"]),
                        sl=float(signal["sl"]),
                        tp=float(signal["tp"]),
                        confidence=float(signal["confidence"]),
                        reasons=list(signal["reasons"]),
                        indicator_snapshot=dict(signal["indicator_snapshot"]),
                    )
                    if saved:
                        self.storage.add_log(
                            session_id,
                            "TRADE_SIGNAL",
                            f"{signal['action']} issued by {signal['strategy_label']} at {signal['confidence']:.1f}% evidence confidence.",
                            details={
                                "signal_id": saved["id"],
                                "bar_time": signal["bar_time"],
                                "strategy": signal["strategy"],
                                "sl": signal["sl"],
                                "tp": signal["tp"],
                                "confidence": signal["confidence"],
                                "reasons": signal["reasons"],
                                "decision": decision,
                            },
                        )
                        return self._pulse_payload(
                            self.storage.get_session(session_id) or session,
                            phase_state(session),
                            saved,
                        )
                else:
                    self.storage.add_log(
                        session_id,
                        "M5_HOLD",
                        "Closed M5 candle assessed; no playbook module had enough directional evidence.",
                        details=decision,
                    )

        self.storage.touch_session(session_id)
        return self._pulse_payload(self.storage.get_session(session_id) or session, phase_state(session), None)

    def acknowledge_signal(self, session_id: str, signal_id: str, accepted: bool, message: str = "") -> None:
        status = "ACCEPTED" if accepted else "REJECTED"
        self.storage.acknowledge_signal(signal_id, status)
        self.storage.add_log(
            session_id,
            "SIGNAL_ACK",
            f"MT5 {status.lower()} signal {signal_id}. {message}".strip(),
            level="INFO" if accepted else "WARNING",
            details={"signal_id": signal_id, "accepted": accepted, "message": message},
        )

    def record_deal(self, payload: dict[str, Any]) -> bool:
        saved = self.storage.save_deal(payload)
        if saved:
            self.storage.add_log(
                payload["session_id"],
                "DEAL_RECORDED",
                f"MT5 deal {payload['deal_ticket']} recorded: {payload.get('entry_type')} {payload.get('deal_type')}.",
                details=payload,
            )
        return saved

    def _session_payload(self, session: dict[str, Any]) -> dict[str, Any]:
        phase = phase_state(session)
        return {
            "session_id": session["id"],
            "phase": phase.phase,
            "elapsed_seconds": phase.elapsed_seconds,
            "remaining_seconds": phase.remaining_seconds,
            "started_at": session["started_at"],
            "research_ends_at": session["research_ends_at"],
            "trading_ends_at": session["trading_ends_at"],
            "selected_strategy": session.get("selected_strategy") or "",
        }

    def _pulse_payload(
        self,
        session: dict[str, Any],
        phase: PhaseState,
        signal: dict[str, Any] | None,
    ) -> dict[str, Any]:
        selected = session.get("selected_strategy") or ""
        result = {
            "session_id": session["id"],
            "phase": phase.phase,
            "elapsed_seconds": phase.elapsed_seconds,
            "remaining_seconds": phase.remaining_seconds,
            "selected_strategy": selected,
            "action": "HOLD",
        }
        if signal:
            result.update(
                {
                    "action": signal["action"],
                    "signal_id": signal["id"],
                    "sl": float(signal["sl"]),
                    "tp": float(signal["tp"]),
                    "confidence": float(signal["confidence"]),
                    "reasons": json.loads(signal["reasons_json"]),
                }
            )
        return result
