"""Risk gate.

Phase 1 stub: rejects everything.

That is the safe default, not an oversight. Phase 3 replaces this with the real
gate (exposure limits, correlation, news, sessions, drawdown). Until then, no
order can reach the broker, which means no untested risk logic can be exercised
against a live terminal by accident.

Every rejection is recorded in `risk_decisions` with a reason, so the journal
shows exactly what was blocked and why. A stub that approved silently would be
far more dangerous than one that refuses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from engine.journal import Journal

# Reasons this stub gives. Phase 3 replaces the whole body.
PHASE1_BLOCK_REASONS = (
    "PHASE1_RISK_GATE_STUB: risk assessment is not implemented until Phase 3; "
    "every order is rejected",
)


@dataclass(frozen=True)
class RiskDecision:
    """Verdict for one candidate order."""

    approved: bool
    reasons: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.approved


class RiskEngine:
    """Evaluates a candidate order. Phase 1 approves nothing."""

    def __init__(self, journal: Journal | None = None, *, implemented: bool = False):
        self.journal = journal
        self.implemented = implemented

    @property
    def phase1_stub(self) -> bool:
        return not self.implemented

    def evaluate(self, *, symbol: str, side: str, volume: float, sl: float,
                 tp: float | None = None, equity: float | None = None,
                 trade_id: int | None = None) -> RiskDecision:
        """Assess one order. Phase 1 always returns rejected."""
        reasons: list[str] = []

        if self.phase1_stub:
            reasons.extend(PHASE1_BLOCK_REASONS)

        decision = RiskDecision(approved=False, reasons=reasons)

        if self.journal is not None:
            self.journal.record_risk_decision(
                symbol=symbol, approved=decision.approved,
                reasons=reasons, trade_id=trade_id,
            )
            self.journal.log_event(
                "risk", "order rejected by risk gate",
                severity="WARNING" if not decision.approved else "INFO",
                source="risk",
                context={"symbol": symbol, "side": side, "volume": volume,
                         "approved": decision.approved, "reasons": reasons},
            )

        return decision

    def can_submit(self, *, symbol: str, side: str, volume: float, sl: float,
                   tp: float | None = None, equity: float | None = None) -> bool:
        """Boolean convenience wrapper used by the executor call sites."""
        return self.evaluate(symbol=symbol, side=side, volume=volume,
                             sl=sl, tp=tp, equity=equity).approved

    def status(self) -> dict:
        return {
            "implemented": self.implemented,
            "phase1_stub": self.phase1_stub,
            "blocks_all_orders": self.phase1_stub,
            "reasons": list(PHASE1_BLOCK_REASONS) if self.phase1_stub else [],
        }