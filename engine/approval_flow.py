"""Approval flow orchestration for V2.0 AI Trading Terminal.

This module implements the V2.0 signal processing pipeline:

    Strategies -> Confidence Engine -> Risk Engine -> LLM Validator ->
    Telegram Approval -> Executor

The LLM remains validation-only. Telegram is an APPROVAL LAYER ONLY - the
human operator's YES/NO is what authorises a trade. The telegram_service
holds no MT5 access, no strategy logic, and no AI logic.

This module is deliberately thin: it wires existing components together and
keeps the approval state out of telegram_service.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from engine.config import EngineConfig, read_mt5_credentials
from engine.executor import ExecutionRejected, ExecutionResult, OrderRequest, \
    RiskApproval, build_executor
from engine.journal import Journal, utc_now_iso
from engine.llm_client import Snapshot, Verdict, Validation, XAIClient
from engine.notifications import (
    cleanup_pending_trades,
    get_pending_trade,
    init_telegram,
    process_no_approval,
    process_yes_approval,
    send_approval_message,
    store_pending_trade,
)
from engine.risk import AccountState, OrderIntent, RiskDecision, RiskEngine
from engine.sizing import LotSizer
from engine.state import StateMachine

logger = logging.getLogger(__name__)


class ApprovalError(RuntimeError):
    """An error in the approval pipeline."""


# The approval pipeline cannot proceed without this many required components.
_MISSING_COMPONENTS = ["risk_engine", "llm_client", "executor", "journal",
                       "gateway", "state_machine"]


class ApprovalFlow:
    """Orchestrates the signal -> telegram -> execution pipeline.

    One instance per engine process. All MT5 access goes through the gateway
    supplied to the constructor; the flow never creates a terminal connection
    of its own.
    """

    def __init__(self, *,
                 journal: Journal,
                 state_machine: StateMachine,
                 risk_engine: RiskEngine,
                 llm_client: XAIClient,
                 executor,
                 gateway,
                 config: EngineConfig,
                 on_state_change=None):
        self.journal = journal
        self.state_machine = state_machine
        self.risk_engine = risk_engine
        self.llm_client = llm_client
        self.executor = executor
        self.gateway = gateway
        self.config = config
        self.on_state_change = on_state_change
        self._telegram_initialized = False

    def _get_state(self) -> str | None:
        return getattr(self.state_machine, "state", None)

    def _set_state(self, state: str) -> None:
        self.state_machine.set_state(state, reason="approval_flow")
        if self.on_state_change is not None:
            self.on_state_change(state)

    # ---------------------------------------------------------------------
    # Pipeline entry points
    # ---------------------------------------------------------------------

    async def process_signal(self, symbol: str, *, confidence: float,
                             entry: float, stop_loss: float, take_profit: float,
                             lot_size: float, rationale: str,
                             side: str = "BUY") -> str:
        """Run one signal through the full V2 pipeline.

        The signal passes Strategies -> Confidence Engine -> Risk Engine ->
        LLM Validator, then awaits human approval via Telegram before the
        executor runs.

        Returns one of:
          - "approved_and_executed"  - YES received and order submitted
          - "approved_but_not_executed" - YES received but execution failed
          - "rejected"               - NO received or risk/LLM rejected
          - "telegram_unavailable"   - telegram not configured; blocked
          - "blocked"                - engine halted / state blocked execution
        """

        state = self._get_state()

        # -- Safety gate 1: engine must not run while halted ----------------
        if state in ("PAUSED", "EMERGENCY_STOP"):
            logger.info("Signal blocked: engine state=%s", state)
            return "blocked"

        # -- Gate 2: telegram must be configured ----------------------------
        if not self._telegram_initialized:
            await self._init_telegram()

        if not self._telegram_initialized:
            logger.warning("Telegram not configured; pipeline blocked")
            return "telegram_unavailable"

        # -- Gate 3: risk gate ------------------------------------------------
        risk_result = self.risk_engine.evaluate(
            OrderIntent.from_symbol_spec(
                symbol=symbol,
                side=side,
                volume=lot_size,
                entry_price=entry,
                stop_loss=stop_loss,
                tick_value=0.01,  # placeholder; real spec comes from broker
                tick_size=0.00001,
            ),
            AccountState(
                equity=100_000.0,  # placeholder; real value from account
                start_of_day_equity=100_000.0,
            ),
        )
        if not risk_result.approved:
            logger.warning("Risk gate rejected signal: %s",
                           "; ".join(risk_result.violations))
            return "rejected"

        # -- Gate 4: LLM validation (validation ONLY, never authorises) -------
        llm_decision = await self._validate_with_llm(symbol, confidence)
        if llm_decision.verdict is Verdict.REJECT:
            logger.warning("LLM validation REJECTED signal")
            return "rejected"
        if llm_decision.verdict is Verdict.REDUCE:
            logger.warning("LLM validation REDUCED confidence for signal")

        # -- Gate 5: Telegram approval --------------------------------------
        await self._submit_for_approval(symbol, side, confidence, entry,
                                        stop_loss, take_profit, lot_size,
                                        llm_decision.rationale)

        trade = get_pending_trade("")
        if trade is None:
            logger.error("Approval message sent but trade not stored")
            return "blocked"

        return "pending_approval"

    async def _init_telegram(self) -> None:
        """Initialize telegram once per engine process."""
        if self._telegram_initialized:
            return
        self._telegram_initialized = await init_telegram(self.journal)

    async def _validate_with_llm(self, symbol: str, confidence: float) -> Validation:
        """Ask the LLM to validate. Fails safe on any error."""

        snapshot = Snapshot(
            symbol=symbol,
            timeframe="M15",
            price=0.0,
            indicators={
                "confidence": confidence,
            },
        )

        try:
            return self.llm_client.validate_trade(snapshot)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM validation failed; treating as REJECT for safety")
            self.journal.log_event("llm", f"validation error: {exc}",
                                   severity="WARNING", source="llm")
            return Validation(verdict=Verdict.REJECT, confidence_adjust=0.0,
                              rationale=f"LLM unavailable: {exc}")

    async def _submit_for_approval(self, symbol: str, side: str,
                                   confidence: float, entry: float,
                                   stop_loss: float, take_profit: float,
                                   lot_size: float, llm_rationale: str) -> None:
        """Create a pending trade and request Telegram approval."""

        trade_id = uuid.uuid4().hex[:7]

        from engine.notifications import PendingTrade

        # Time Machine advisory pre-approval check (fail-open).
        tm_summary = None
        tm_cfg = getattr(self.config, "timemachine", None)
        if tm_cfg is not None and getattr(tm_cfg, "enabled", False):
            from engine.research.timemachine_hook import run_timemachine
            tm_summary = await run_timemachine(
                tm_cfg,
                symbol=symbol, direction=side, entry=entry, stop=stop_loss,
                target=take_profit, journal=self.journal,
            )

        trade = PendingTrade(
            trade_id=trade_id,
            symbol=symbol,
            direction=side,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=take_profit,
            lot_size=lot_size,
            confidence=confidence,
            rationale=llm_rationale,
            timemachine=tm_summary,
        )

        ok = store_pending_trade(trade, self.journal)
        if not ok:
            raise ApprovalError("failed to store pending trade")

        sent = await send_approval_message(trade, self.journal)
        if not sent:
            raise ApprovalError("failed to send telegram approval message")

        logger.info("Signal pending Telegram approval: %s", trade_id)

    async def process_yes(self, trade_id: str, executor=None,
                          journal=None) -> str:
        """Forward a YES to the executor via the approval flow."""

        journal = journal or self.journal
        if executor is None:
            executor = self.executor

        return await process_yes_approval(trade_id, executor, journal,
                                          self.gateway)

    async def process_no(self, trade_id: str) -> str:
        """Forward a NO to cancel the pending trade."""

        return await process_no_approval(trade_id, self.journal)


# ---------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------

def build_approval_flow(config: EngineConfig, journal: Journal,
                        state_machine: StateMachine, gateway,
                        risk_engine: RiskEngine, llm_client: XAIClient,
                        executor) -> ApprovalFlow:
    """Construct the approval flow from engine components."""

    return ApprovalFlow(
        journal=journal,
        state_machine=state_machine,
        risk_engine=risk_engine,
        llm_client=llm_client,
        executor=executor,
        gateway=gateway,
        config=config,
        on_state_change=None,
    )
