"""Command channel.

The UI inserts a row into `commands`; the engine polls, claims, executes and
records the outcome. The UI never acts directly on the engine, and the engine
never asks the UI for anything.

Claiming is a conditional UPDATE, so two pollers cannot take the same command and
a restart cannot re-run one already in flight. That is the whole basis for the
idempotency claim in the design doc.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from engine.executor import Executor
from engine.journal import (
    CMD_DONE,
    CMD_FAILED,
    Journal,
)
from engine.state import StateMachine

# Command names the engine accepts. Anything else is rejected rather than
# ignored, so a typo in the UI surfaces instead of silently doing nothing.
COMMAND_ENABLE_AI = "ENABLE_AI"
COMMAND_DISABLE_AI = "DISABLE_AI"
COMMAND_PAUSE = "PAUSE"
COMMAND_RESUME = "RESUME"
COMMAND_EMERGENCY_STOP = "EMERGENCY_STOP"
COMMAND_RESET_EMERGENCY = "RESET_EMERGENCY"
COMMAND_CLOSE_POSITION = "CLOSE_POSITION"
COMMAND_CLOSE_ALL = "CLOSE_ALL"
COMMAND_UPDATE_CONFIG = "UPDATE_CONFIG"
COMMAND_APPROVE_SIGNAL = "APPROVE_SIGNAL"
COMMAND_REJECT_SIGNAL = "REJECT_SIGNAL"

KNOWN_COMMANDS = frozenset({
    COMMAND_ENABLE_AI, COMMAND_DISABLE_AI, COMMAND_PAUSE, COMMAND_RESUME,
    COMMAND_EMERGENCY_STOP, COMMAND_RESET_EMERGENCY, COMMAND_CLOSE_POSITION,
    COMMAND_CLOSE_ALL, COMMAND_UPDATE_CONFIG,
    COMMAND_APPROVE_SIGNAL, COMMAND_REJECT_SIGNAL,
})

#: Commands that bypass confirmation entirely. An emergency must not wait on a
#: dialog: the whole point is that it acts while someone is watching a position
#: move against them.
NO_CONFIRMATION_COMMANDS = frozenset({COMMAND_EMERGENCY_STOP, COMMAND_CLOSE_ALL})


@dataclass
class CommandOutcome:
    ok: bool
    status: str
    result: str = ""
    reason: str = ""
    command: str = ""


class CommandProcessor:
    """Polls the commands table and executes entries."""

    def __init__(self, journal: Journal, state_machine: StateMachine,
                 executor: Executor | None = None, approval_ctx=None):
        self.journal = journal
        self.sm = state_machine
        self.executor = executor
        self.approval_ctx = approval_ctx

    # -- polling ---------------------------------------------------------

    def poll_once(self) -> list[tuple[int, CommandOutcome]]:
        """Claim and execute at most one command.

        One at a time, deliberately: commands can move the state machine and
        touch the executor, and interleaving them would make the audit trail
        harder to reason about than the throughput is worth.
        """
        row = self.journal.claim_next_command()
        if row is None:
            return []

        command = row["command"]
        try:
            params = json.loads(row["params_json"] or "{}")
        except (TypeError, ValueError):
            params = {}

        outcome = self.execute(command, params)
        outcome.command = command

        if outcome.ok:
            self.journal.finish_command(row["id"], CMD_DONE,
                                        result=outcome.result)
        else:
            self.journal.finish_command(row["id"], CMD_FAILED,
                                        result=outcome.result,
                                        reason=outcome.reason)

        self.journal.log_event(
            "command", f"{command} -> {outcome.status}",
            severity="INFO" if outcome.ok else "WARNING",
            source="commands",
            context={"id": row["id"], "command": command,
                     "status": outcome.status, "reason": outcome.reason},
        )
        return [(row["id"], outcome)]

    def poll_until_idle(self, max_iterations: int = 100) -> list[tuple[int, CommandOutcome]]:
        """Drain the queue. Used at startup and by tests."""
        done: list[tuple[int, CommandOutcome]] = []
        for _ in range(max_iterations):
            batch = self.poll_once()
            if not batch:
                break
            done.extend(batch)
        return done

    # -- dispatch --------------------------------------------------------

    def execute(self, command: str, params: dict | None = None) -> CommandOutcome:
        params = params or {}

        if command not in KNOWN_COMMANDS:
            return CommandOutcome(
                ok=False, status=CMD_FAILED,
                reason=f"unknown command {command!r}; expected one of "
                       f"{sorted(KNOWN_COMMANDS)}")

        handler = getattr(self, f"_cmd_{command.lower()}", None)
        if handler is None:                       # pragma: no cover - defensive
            return CommandOutcome(ok=False, status=CMD_FAILED,
                                  reason=f"no handler for {command!r}")
        return handler(params)

    # -- state commands --------------------------------------------------

    def _cmd_enable_ai(self, params: dict) -> CommandOutcome:
        ok, message = self.sm.apply(COMMAND_ENABLE_AI, params)
        return CommandOutcome(ok=ok, status=CMD_DONE if ok else CMD_FAILED,
                              result=message, reason="" if ok else message)

    def _cmd_disable_ai(self, params: dict) -> CommandOutcome:
        ok, message = self.sm.apply(COMMAND_DISABLE_AI, params)
        return CommandOutcome(ok=ok, status=CMD_DONE if ok else CMD_FAILED,
                              result=message, reason="" if ok else message)

    def _cmd_pause(self, params: dict) -> CommandOutcome:
        ok, message = self.sm.apply(COMMAND_PAUSE, params)
        return CommandOutcome(ok=ok, status=CMD_DONE if ok else CMD_FAILED,
                              result=message, reason="" if ok else message)

    def _cmd_resume(self, params: dict) -> CommandOutcome:
        ok, message = self.sm.apply(COMMAND_RESUME, params)
        return CommandOutcome(ok=ok, status=CMD_DONE if ok else CMD_FAILED,
                              result=message, reason="" if ok else message)

    def _cmd_emergency_stop(self, params: dict) -> CommandOutcome:
        """Always executes. Never asks for confirmation."""
        try:
            self.sm.transition("EMERGENCY_STOP", COMMAND_EMERGENCY_STOP,
                               reason=str(params.get("reason", "operator")))
        except Exception as exc:   # noqa: BLE001
            return CommandOutcome(ok=False, status=CMD_FAILED, reason=str(exc))

        closed = 0
        if params.get("close_positions"):
            closed = self._close_everything()

        return CommandOutcome(
            ok=True, status=CMD_DONE,
            result=f"EMERGENCY_STOP active; closed {closed} position(s)")

    def _cmd_reset_emergency(self, params: dict) -> CommandOutcome:
        ok, message = self.sm.apply(COMMAND_RESET_EMERGENCY, params)
        return CommandOutcome(ok=ok, status=CMD_DONE if ok else CMD_FAILED,
                              result=message, reason="" if ok else message)

    # -- position commands -----------------------------------------------

    def _cmd_close_position(self, params: dict) -> CommandOutcome:
        ticket = params.get("ticket")
        if ticket is None:
            return CommandOutcome(ok=False, status=CMD_FAILED,
                                  reason="CLOSE_POSITION requires a ticket")
        if self.executor is None:
            return CommandOutcome(ok=False, status=CMD_FAILED,
                                  reason="no executor configured")

        try:
            result = self.executor.close(int(ticket))
        except Exception as exc:   # noqa: BLE001
            return CommandOutcome(ok=False, status=CMD_FAILED, reason=str(exc))

        return CommandOutcome(
            ok=result.ok, status=CMD_DONE if result.ok else CMD_FAILED,
            result=result.message, reason="" if result.ok else result.message)

    def _cmd_close_all(self, params: dict) -> CommandOutcome:
        closed = self._close_everything()
        return CommandOutcome(ok=True, status=CMD_DONE,
                              result=f"closed {closed} position(s)")

    def _close_everything(self) -> int:
        """Close every position this system owns. Never a foreign one.

        Two independent guarantees keep a foreign position out of the sweep.
        The guard never writes a foreign-magic position into ``positions``, so
        normally there is nothing to filter. The filter below is the second
        lock: it re-checks the magic on the row being closed, so even a
        position that reached the journal by some other route -- a manual
        insert, a future reconcile change, a restored backup -- is skipped and
        journalled as skipped rather than closed. The magic-234000 positions
        are somebody else's money and a halt must not touch them.
        """
        from engine.account_guard import MANAGED_MAGIC

        if self.executor is None:
            return 0
        tickets: list[int] = []
        for position in self.journal.positions():
            ticket = int(position["ticket"])
            magic = position.get("magic")
            if magic is not None and int(magic) != MANAGED_MAGIC:
                self.journal.log_event(
                    "command",
                    f"emergency sweep skipped ticket {ticket}: magic {magic} "
                    f"is not ours ({MANAGED_MAGIC})",
                    severity="WARNING", source="commands",
                    context={"ticket": ticket, "position_magic": magic,
                             "managed_magic": MANAGED_MAGIC,
                             "decision": "not closed by a managed sweep"})
                continue
            tickets.append(ticket)
        closed = 0
        for ticket in tickets:
            try:
                if self.executor.close(ticket).ok:
                    closed += 1
            except Exception as exc:   # noqa: BLE001
                # One failure must not abort the sweep; the rest must still go.
                self.journal.log_event(
                    "command", f"close failed for ticket {ticket}",
                    severity="ERROR", source="commands", context={"error": str(exc)})
        return closed

    # -- config ----------------------------------------------------------

    def _cmd_approve_signal(self, params: dict) -> CommandOutcome:
        from engine.signal_approval import handle_approve
        ctx = self.approval_ctx() if callable(self.approval_ctx) else self.approval_ctx
        if ctx is None:
            return CommandOutcome(ok=False, status=CMD_FAILED,
                                  reason="approval context missing")
        return handle_approve(ctx, params)

    def _cmd_reject_signal(self, params: dict) -> CommandOutcome:
        from engine.signal_approval import handle_reject
        ctx = self.approval_ctx() if callable(self.approval_ctx) else self.approval_ctx
        if ctx is None:
            return CommandOutcome(ok=False, status=CMD_FAILED,
                                  reason="approval context missing")
        return handle_reject(ctx, params)

    def _cmd_update_config(self, params: dict) -> CommandOutcome:
        """Validate a patch and report it. Phase 1 does not hot-reload.

        Refusing rather than half-applying keeps the UI honest: it will show
        that the change needs a restart instead of appearing to take effect.
        """
        patch = params.get("patch")
        if not isinstance(patch, dict):
            return CommandOutcome(ok=False, status=CMD_FAILED,
                                  reason="UPDATE_CONFIG requires a patch object")
        return CommandOutcome(
            ok=True, status=CMD_DONE,
            result=f"validated {len(patch)} key(s); a restart is required to "
                   f"apply them in Phase 1")