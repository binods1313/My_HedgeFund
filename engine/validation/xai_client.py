"""xAI structural validation gate.

Asymmetric by construction. The model may only KILL a setup, never enable one:

    < 3 of 5 confluences   -> intent is NONE; xAI is never consulted
    >= 3 of 5 confluences  -> intent compiled, then sent here
    VALIDATE = TRUE         -> proceeds to the risk gate
    VALIDATE = FALSE        -> killed immediately, never reaching risk

That asymmetry is enforced in code, not in the prompt: `gate()` returns a
decision object whose `may_proceed` is only ever True when both the confluence
count and the model's verdict allow it. A future edit that tried to let xAI
promote a 2-of-5 setup would have to invert that expression, and the tests pin
the behaviour from both directions.

Every outcome is recorded, including a provider failure. A broken provider does
NOT mean "approved"; it means the setup is killed, because the gate exists to
catch spatial structure the indicators missed and a gate that fails open is not a
gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.keys import Keyring
from engine.llm_client import (
    ProviderError,
    Snapshot,
    Validation,
    Verdict,
    XAIClient,
)

PROVIDER = "xai"

#: Minimum confluences before xAI is consulted at all.
MIN_CONFLUENCES_FOR_XAI = 3

#: Journal message recorded when the model rejects a setup.
REJECTION_LOG = "xAI Gate Rejected: Spatial constraints invalid"

#: Recorded when the provider itself fails. Distinct from a rejection, because
#: one is the model declining and the other is the system not working.
PROVIDER_FAILURE_LOG = "xAI Gate Unavailable: Setup killed fail-safe"


@dataclass(frozen=True)
class SpatialContext:
    """The six fields the gate sends. Light, strict, numeric."""

    symbol: str
    timeframe: str
    fvg_size: float
    sweep_type: str
    ote_distance: float
    trend_bias: str

    def to_snapshot(self) -> Snapshot:
        return Snapshot(
            symbol=self.symbol,
            timeframe=self.timeframe,
            price=self.ote_distance,
            indicators={
                "fvg_size": float(self.fvg_size),
                "sweep_type_points": float(self.sweep_type == "BUY"),
            },
            context={
                "ote_distance": float(self.ote_distance),
                "trend_bias_points": float(self.trend_bias == "BULLISH"),
            },
        )

    def describe(self) -> str:
        return (
            f"{self.symbol} {self.timeframe} sweep={self.sweep_type} "
            f"fvg_size={self.fvg_size:g} ote_distance={self.ote_distance:g} "
            f"trend_bias={self.trend_bias}"
        )


@dataclass
class GateDecision:
    """The gate's verdict, including how it was reached."""

    passed: bool
    reason: str
    confluences: int
    consulted: bool
    provider_ok: bool
    model_validate: bool | None = None
    log_message: str = ""

    @property
    def may_proceed(self) -> bool:
        """Only ever True on a clean pass.

        Consulted-by-model AND provider-reachable AND model-validated. There is no
        branch here that lets a model response raise a below-threshold setup.
        """
        return self.passed and self.consulted and self.provider_ok \
            and self.model_validate is True


@dataclass
class XAIValidationGate:
    """Applies the asymmetry: it can only remove a setup."""

    client: Optional[XAIClient] = None
    journal: object | None = None
    min_confluences: int = MIN_CONFLUENCES_FOR_XAI

    def __post_init__(self):
        if self.client is None:
            self.client = XAIClient()

    def is_available(self) -> bool:
        return bool(self.client and self.client.is_configured())

    def gate(self, context: SpatialContext, confluences: int) -> GateDecision:
        """Decide whether an intent survives the structural gate."""
        if confluences < self.min_confluences:
            # Below threshold the strategy never builds an intent, so the model
            # is not asked. Consulting it here would imply it could rescue a
            # setup the mathematics already rejected.
            self._log("INFO", f"{REJECTION_LOG.split(':')[0]}: "
                              f"not consulted; only {confluences}/5 confluences")
            return GateDecision(
                passed=False,
                reason=f"{confluences} of 5 confluences is below the "
                       f"{self.min_confluences} required",
                confluences=confluences,
                consulted=False,
                provider_ok=True,
            )

        if not self.is_available():
            # Fail closed. A gate that cannot reach the model must not approve.
            self._log("WARNING", PROVIDER_FAILURE_LOG)
            return GateDecision(
                passed=False,
                reason=PROVIDER_FAILURE_LOG,
                confluences=confluences,
                consulted=False,
                provider_ok=False,
            )

        self._log("INFO", f"xAI gate consulting: {context.describe()}")

        try:
            result: Validation = self.client.validate_trade(
                context.to_snapshot())          # type: ignore[union-attr]
        except ProviderError as exc:
            # Never 'approved'. The gate exists to catch structure the
            # indicators missed; an unreachable gate is not a passing gate.
            self._log("WARNING", PROVIDER_FAILURE_LOG)
            return GateDecision(
                passed=False,
                reason=f"{PROVIDER_FAILURE_LOG}: {exc}",
                confluences=confluences,
                consulted=True,
                provider_ok=False,
            )

        validated = self._interpret(result)
        if not validated:
            self._log("WARNING", REJECTION_LOG)
            return GateDecision(
                passed=False,
                reason=REJECTION_LOG,
                confluences=confluences,
                consulted=True,
                provider_ok=True,
                model_validate=False,
                log_message=REJECTION_LOG,
            )

        self._log("INFO", "xAI gate passed")
        return GateDecision(
            passed=True,
            reason="xAI validated the spatial structure",
            confluences=confluences,
            consulted=True,
            provider_ok=True,
            model_validate=True,
        )

    @staticmethod
    def _interpret(result: Validation) -> bool:
        """Map a model response onto pass or fail.

        A REJECT verdict, or any adjustment below zero, fails the setup: the
        model signalling reduced confidence has declined to vouch for it.
        """
        if result.verdict is Verdict.REJECT:
            return False
        if result.confidence_adjust is not None and result.confidence_adjust < 0:
            return False
        return True

    def _log(self, severity: str, message: str) -> None:
        if self.journal is not None:
            try:
                self.journal.log_event("xai_gate", message, severity=severity,
                                       source="validation")
            except Exception:   # noqa: BLE001 - logging must not change the verdict
                pass