"""The engine state machine.

Nine states with declared transitions. Nothing sets `state` directly; every
change goes through `transition()`, which validates the move and writes an
`events` row.

Two rules are load-bearing:

  - EMERGENCY_STOP is terminal. Nothing leaves it except an explicit reset with
    typed confirmation. Not a timeout, not a reconnect, not a restart.
  - An illegal transition raises rather than being ignored. A bug that tries to
    jump SCANNING -> EXECUTING should fail loudly, because the alternative is a
    trade placed from a state the risk logic never evaluated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from engine.journal import (
    ALL_STATES,
    STATE_ANALYZING,
    STATE_EMERGENCY_STOP,
    STATE_EXECUTING,
    STATE_IDLE,
    STATE_MONITORING,
    STATE_OFF,
    STATE_PAUSED,
    STATE_SCANNING,
    STATE_VALIDATING,
    Journal,
)

HEARTBEAT_SECONDS = 5
OFFLINE_AFTER_SECONDS = 15


class IllegalTransition(RuntimeError):
    """A transition that is not in the table."""

    def __init__(self, from_state: str, to_state: str, trigger: str):
        self.from_state = from_state
        self.to_state = to_state
        self.trigger = trigger
        super().__init__(
            f"Illegal transition {from_state} -> {to_state} on trigger "
            f"{trigger!r}. Allowed from {from_state}: "
            f"{sorted(ALLOWED.get(from_state, ())) or ['(none)']}"
        )


@dataclass(frozen=True)
class Rule:
    """One declared transition."""

    to_state: str
    trigger: str


# -- the transition table -----------------------------------------------------

#: Pause and EMERGENCY_STOP are reachable from every operational state. They are
#: listed per state rather than via a wildcard so the table stays greppable and
#: so OFF -> PAUSED is deliberately absent (an engine that is off has nothing to
#: pause).
ALLOWED: dict[str, tuple[Rule, ...]] = {
    STATE_OFF: (
        Rule(STATE_IDLE, "ENABLE_AI"),
    ),
    STATE_IDLE: (
        Rule(STATE_SCANNING, "CYCLE_TICK"),
        Rule(STATE_OFF, "DISABLE_AI"),
        Rule(STATE_PAUSED, "PAUSE"),
        Rule(STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
    ),
    STATE_SCANNING: (
        Rule(STATE_ANALYZING, "SCAN_COMPLETE"),
        Rule(STATE_IDLE, "SCAN_EMPTY"),
        Rule(STATE_PAUSED, "PAUSE"),
        Rule(STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
    ),
    STATE_ANALYZING: (
        Rule(STATE_VALIDATING, "SIGNAL_PRODUCED"),
        Rule(STATE_IDLE, "NO_SIGNAL"),
        Rule(STATE_PAUSED, "PAUSE"),
        Rule(STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
    ),
    STATE_VALIDATING: (
        Rule(STATE_EXECUTING, "RISK_APPROVED"),
        Rule(STATE_IDLE, "RISK_REJECTED"),
        Rule(STATE_PAUSED, "PAUSE"),
        Rule(STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
    ),
    STATE_EXECUTING: (
        Rule(STATE_MONITORING, "ORDER_SETTLED"),
        Rule(STATE_IDLE, "ORDER_FAILED"),
        Rule(STATE_PAUSED, "PAUSE"),
        Rule(STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
    ),
    STATE_MONITORING: (
        Rule(STATE_IDLE, "CYCLE_COMPLETE"),
        Rule(STATE_PAUSED, "PAUSE"),
        Rule(STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
    ),
    STATE_PAUSED: (
        Rule(STATE_IDLE, "RESUME"),
        Rule(STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
    ),
    # Terminal. RESET_EMERGENCY is the only exit and it must be requested
    # explicitly; nothing automatic may use that trigger.
    STATE_EMERGENCY_STOP: (
        Rule(STATE_OFF, "RESET_EMERGENCY"),
    ),
}


def allowed_from(state: str) -> set[str]:
    """Target states reachable from `state`."""
    return {rule.to_state for rule in ALLOWED.get(state, ())}


def is_allowed(state: str, to_state: str, trigger: str) -> bool:
    return any(
        rule.to_state == to_state and rule.trigger == trigger
        for rule in ALLOWED.get(state, ())
    )


def triggers_from(state: str) -> set[str]:
    return {rule.trigger for rule in ALLOWED.get(state, ())}


class StateMachine:
    """Holds the current state and applies declared transitions."""

    def __init__(self, journal: Journal, *,
                 on_transition: Callable[[str, str, str, str], None] | None = None):
        self.journal = journal
        self.on_transition = on_transition

        existing = journal.get_state()
        if existing and existing.get("state") in ALL_STATES:
            # A restart resumes where it left off, except that EMERGENCY_STOP is
            # preserved: an engine that crashed mid-emergency must come back up
            # still halted.
            self._state = existing["state"]
            self._mode = existing.get("mode", "PAPER")
        else:
            self._state = STATE_OFF
            self._mode = "PAPER"
            journal.set_state(self._state, reason="startup", mode=self._mode)
            journal.log_event("state", "engine initialised",
                              context={"state": self._state})

    @property
    def state(self) -> str:
        return self._state

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.journal.set_state(self._state, reason="mode change", mode=mode)

    def can(self, to_state: str, trigger: str) -> bool:
        return is_allowed(self._state, to_state, trigger)

    def transition(self, to_state: str, trigger: str, reason: str = "") -> str:
        """Move to `to_state`. Raises IllegalTransition if not declared.

        Every accepted transition writes an events row with from, to, trigger and
        reason, which is what makes the audit trail complete.
        """
        if to_state not in ALL_STATES:
            raise IllegalTransition(self._state, to_state, trigger)

        if not self.can(to_state, trigger):
            raise IllegalTransition(self._state, to_state, trigger)

        previous = self._state
        self._state = to_state
        self.journal.set_state(to_state, reason=reason, mode=self._mode)
        self.journal.log_event(
            "state",
            f"{previous} -> {to_state}",
            context={"from": previous, "to": to_state,
                     "trigger": trigger, "reason": reason},
        )
        if self.on_transition:
            self.on_transition(previous, to_state, trigger, reason)
        return to_state

    # -- convenience for the command layer --------------------------------

    def apply(self, command: str, params: dict | None = None) -> tuple[bool, str]:
        """Apply a state-affecting command.

        Returns (ok, message). Failure is a message, not an exception, because a
        rejected command is a normal outcome that the UI should display.
        """
        params = params or {}
        mapping = {
            "ENABLE_AI": (STATE_IDLE, "ENABLE_AI"),
            "DISABLE_AI": (STATE_OFF, "DISABLE_AI"),
            "PAUSE": (STATE_PAUSED, "PAUSE"),
            "RESUME": (STATE_IDLE, "RESUME"),
            "EMERGENCY_STOP": (STATE_EMERGENCY_STOP, "EMERGENCY_STOP"),
        }

        if command == "RESET_EMERGENCY":
            # Requires the exact confirmation phrase. Emergency paths never
            # prompt, but clearing one is deliberate and must be typed.
            expected = "RESET"
            supplied = str(params.get("confirmation", ""))
            if supplied != expected:
                return False, (
                    f"RESET_EMERGENCY requires confirmation={expected!r}"
                )
            try:
                self.transition(STATE_OFF, "RESET_EMERGENCY",
                                reason="operator reset")
                return True, "emergency cleared"
            except IllegalTransition as exc:
                return False, str(exc)

        if command not in mapping:
            return False, f"unknown state command {command!r}"

        target, trigger = mapping[command]
        try:
            self.transition(target, trigger,
                            reason=str(params.get("reason", "")))
            return True, f"now {target}"
        except IllegalTransition as exc:
            return False, str(exc)

    # -- liveness ---------------------------------------------------------

    def heartbeat(self, connected: bool = False) -> None:
        self.journal.heartbeat(self._state, connected=connected)

    def is_alive(self, offline_after: int = OFFLINE_AFTER_SECONDS) -> bool:
        """False when the newest heartbeat is older than `offline_after`."""
        age = self.journal.seconds_since_heartbeat()
        if age is None:
            return False
        return age <= offline_after

    def heartbeat_age(self) -> float | None:
        return self.journal.seconds_since_heartbeat()

    def reset_to_off(self, reason: str = "reset") -> None:
        """Force the machine to OFF without consulting the table.

        Used only by startup and by tests. Production code goes through
        transition() so that every move is logged.
        """
        self._state = STATE_OFF
        self.journal.set_state(STATE_OFF, reason=reason, mode=self._mode)