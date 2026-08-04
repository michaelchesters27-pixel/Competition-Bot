from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .storage import Storage, utc_now_ts
from .strategies import evaluate_candidates, live_signal


@dataclass(frozen=True)
class PhaseState:
    phase: str
    elapsed_seconds: int
    remaining_seconds: int


def phase_state(session: dict[str, Any], now: int | None = None) -> PhaseState:
    now = now or utc_now_ts()
    started = int(session["started_at"])
    research_end = int(session["research_ends_at"])
    trading_end = int(session["trading_ends_at"])
    elapsed = max(0, now - started)
    if now < research_end:
        return PhaseState("RESEARCH", elapsed, research_end - now)
    if now < trading_end:
        return PhaseState("TRADING", elapsed, trading_end - now)
    return PhaseState("COMPLETE", elapsed, 0)


class CompetitionEngine:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    def start_or_resume(self, account_login: str, symbol: str, timeframe: str) -> dict[str, Any]:
        session = self.storage.get_or_create_session(account_login, symbol, timeframe)
        if int(session["created_at"]) == int(session["started_at"]):
            existing_logs = self.storage.session_logs(session["id"], limit=1)
            if not existing_logs:
                self.storage.add_log(
                    session["id"],
                    "SESSION_STARTED",
                    "Two-hour XAUUSD competition session started. Research is locked for the first 60 minutes.",
                    details={
                        "research_seconds": 3600,
                        "trading_seconds": 3600,
                        "timeframe": timeframe,
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

        stored_bars = self.storage.get_bars(session_id, limit=700)
        closed_bar_time = int(stored_bars[-2]["time"]) if len(stored_bars) >= 2 else None
        phase = phase_state(session)

        if phase.phase == "RESEARCH":
            if closed_bar_time and closed_bar_time != session.get("last_bar_time") and len(stored_bars) >= 80:
                scores, _ = evaluate_candidates(stored_bars)
                self.storage.save_candidate_scores(session_id, scores)
                self.storage.touch_session(session_id, closed_bar_time)
                leader = scores[0] if scores else None
                if leader:
                    self.storage.add_log(
                        session_id,
                        "RESEARCH_UPDATE",
                        f"Research ranking updated. Current leader: {leader['label']}.",
                        details={
                            "leader": leader["name"],
                            "score": leader["score"],
                            "trades": leader["trades"],
                            "net_r": leader["net_r"],
                        },
                    )
            else:
                self.storage.touch_session(session_id)

        session = self.storage.get_session(session_id) or session
        phase = phase_state(session)

        if phase.phase in ("TRADING", "COMPLETE") and not session.get("selected_strategy"):
            if len(stored_bars) >= 80:
                scores, _ = evaluate_candidates(stored_bars)
                self.storage.save_candidate_scores(session_id, scores)
                selected = scores[0]
                snapshot = {
                    "selected_at": utc_now_ts(),
                    "selection_basis": "Highest research score at the end of the exact 60-minute research phase",
                    "ranking": scores,
                }
                self.storage.set_selected_strategy(session_id, selected["name"], snapshot)
                self.storage.add_log(
                    session_id,
                    "STRATEGY_FROZEN",
                    f"Research finished. {selected['label']} was frozen for the trading hour.",
                    details=selected,
                )
                session = self.storage.get_session(session_id) or session

        pending = self.storage.pending_signal(session_id)
        if pending:
            return self._pulse_payload(session, phase_state(session), pending)

        if phase.phase == "TRADING" and session.get("selected_strategy") and closed_bar_time:
            signal_bar_closed_at = closed_bar_time + 300
            if signal_bar_closed_at >= int(session["research_ends_at"]) and closed_bar_time != session.get("last_signal_bar_time"):
                signal = live_signal(
                    str(session["selected_strategy"]),
                    stored_bars,
                    float(payload.get("bid", 0)),
                    float(payload.get("ask", 0)),
                )
                self.storage.set_last_signal_bar(session_id, closed_bar_time)
                if signal:
                    saved = self.storage.create_signal(
                        session_id=session_id,
                        bar_time=int(signal["bar_time"]),
                        strategy=str(session["selected_strategy"]),
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
                            f"{signal['action']} signal issued by {signal['strategy_label']}.",
                            details={
                                "signal_id": saved["id"],
                                "bar_time": signal["bar_time"],
                                "sl": signal["sl"],
                                "tp": signal["tp"],
                                "confidence": signal["confidence"],
                                "reasons": signal["reasons"],
                            },
                        )
                        return self._pulse_payload(self.storage.get_session(session_id) or session, phase_state(session), saved)

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
