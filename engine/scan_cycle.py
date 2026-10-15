"""Stage 1 scan cycle.

One cycle reads the market once, filters each symbol in isolation, and
returns the state machine to IDLE. It does not generate a signal and it
does not submit an order. The transition that would enter ANALYZING is
not emitted. That state belongs to a strategy this stage does not contain.

Skip rows are written when the reason changes, and again as a reminder
after ``remind_every`` seconds. Identical repeats inside that window
write nothing.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from engine.journal import Journal, utc_now_iso
from engine.state import STATE_IDLE, STATE_SCANNING

# Market-closed is first on purpose. A symbol that is both closed and
# stale must be recorded as MARKET_CLOSED, never as STALE_QUOTE.
_MARKET_CLOSED = "MARKET_CLOSED"
_STALE_QUOTE = "STALE_QUOTE"
_NO_NEW_BAR = "NO_NEW_CLOSED_BAR"
_BUDGET = "CYCLE_BUDGET_EXHAUSTED"
_TERMINAL = "TERMINAL_OFFLINE"
_MISMATCH = "ACCOUNT_MISMATCH"


@dataclass
class ScanOutcome:
    """What one attempt did. Tests assert this; the journal is the audit."""

    status: str
    reason: str = ""
    cycle_id: str = ""
    computed: int = 0
    scanned: int = 0
    errors: int = 0
    deferred: int = 0
    skipped: int = 0


@dataclass
class ScanCycle:
    """The scan step the engine loop calls after the heartbeat."""

    symbols: list[str]
    max_scan_seconds: float = 10.0
    remind_every: float = 600.0
    interval_seconds: float = 60.0
    _active_reason: str | None = None
    _last_journaled: dict[str, float] = field(default_factory=dict)
    _last_bar: dict[str, str] = field(default_factory=dict)
    _last_attempt: float | None = None

    def run_with_engine(self, engine) -> ScanOutcome:
        """Adapt the live engine. Reads the gateway once per started cycle."""
        mode = getattr(getattr(engine, "config", None), "mode", "PAPER")

        def read_market(symbols: list[str]) -> dict:
            return engine.gateway.snapshot(symbols)

        def terminal_up() -> bool:
            guard = getattr(engine, "guard", None)
            if guard is not None and getattr(guard, "mismatch_reason", None):
                if "offline" in str(guard.mismatch_reason):
                    return False
            return bool(getattr(engine.gateway, "connected", False))

        def writes_allowed() -> bool:
            guard = getattr(engine, "guard", None)
            if guard is None:
                return True
            if getattr(guard, "mismatch_reason", None):
                return False
            may = getattr(guard, "may_write_telemetry", None)
            return True if may is None else bool(may())

        return self.run(
            state=engine.state,
            journal=engine.journal,
            read_market=read_market,
            mode=str(mode),
            terminal_up=terminal_up,
            writes_allowed=writes_allowed,
        )

    def run(
        self,
        *,
        state,
        journal: Journal,
        read_market: Callable[[list[str]], dict],
        mode: str = "PAPER",
        terminal_up: Callable[[], bool] | None = None,
        writes_allowed: Callable[[], bool] | None = None,
        monotonic: Callable[[], float] | None = None,
        compute: Callable[[str, dict], Any] | None = None,
        now_iso: str | None = None,
    ) -> ScanOutcome:
        """Attempt one cycle. Returns to IDLE if it entered SCANNING."""
        clock = monotonic or time.monotonic
        online = terminal_up or (lambda: True)
        allowed = writes_allowed or (lambda: True)
        stamp = now_iso or utc_now_iso()
        now_m = clock()
        ticked = False

        def _finish(outcome: ScanOutcome) -> ScanOutcome:
            if ticked and getattr(state, "state", None) == STATE_SCANNING:
                state.transition(STATE_IDLE, "SCAN_EMPTY", reason=outcome.reason or outcome.status)
            return outcome

        try:
            reason = self._precheck(state, mode, online, allowed, now_m)
            if reason:
                self._note_skip(journal, reason, now_m, stamp)
                return ScanOutcome(status="skipped", reason=reason)

            self._last_attempt = now_m
            self._active_reason = None
            state.transition(STATE_SCANNING, "CYCLE_TICK", reason="scan cycle")
            ticked = True

            try:
                snapshot = read_market(list(self.symbols))
            except Exception as exc:
                self._guard_event(journal, _TERMINAL, f"{type(exc).__name__}: {exc}", stamp)
                return _finish(ScanOutcome(status="empty", reason=_TERMINAL))

            views = snapshot.get("symbols", snapshot) if isinstance(snapshot, dict) else {}
            cycle_id = uuid.uuid4().hex
            started = clock()
            computed = scanned = errors = deferred = skipped = 0
            overrun_logged = False
            abort_reason = ""

            for index, symbol in enumerate(self.symbols):
                if not online():
                    abort_reason = _TERMINAL
                    self._guard_event(journal, _TERMINAL, "terminal dropped mid-cycle", stamp)
                    deferred += self._defer_rest(journal, cycle_id, index, _TERMINAL, stamp)
                    break
                if not allowed():
                    abort_reason = _MISMATCH
                    self._guard_event(
                        journal, _MISMATCH,
                        "account fingerprint does not match; writes blocked",
                        stamp,
                    )
                    break
                if clock() - started >= self.max_scan_seconds:
                    abort_reason = _BUDGET
                    if not overrun_logged:
                        journal.log_event(
                            "scan", "cycle budget exhausted",
                            severity="WARNING", source="engine",
                            context={"kind": "cycle_budget", "deferred_from": symbol},
                        )
                        overrun_logged = True
                    deferred += self._defer_rest(journal, cycle_id, index, _BUDGET, stamp)
                    break

                view = views.get(symbol) if isinstance(views, dict) else None
                if not isinstance(view, dict):
                    view = {}
                try:
                    status, why, did_compute = self._one_symbol(symbol, view, compute)
                except Exception as exc:
                    status, why, did_compute = "error", f"{type(exc).__name__}: {exc}", False
                    errors += 1
                else:
                    if status == "error":
                        errors += 1
                    elif status == "skipped":
                        skipped += 1
                    else:
                        scanned += 1
                    if did_compute:
                        computed += 1
                self._symbol_row(journal, cycle_id, symbol, status, why, view.get("bar_ts"), stamp)

            header_status = "empty" if scanned == 0 else "recorded"
            self._header(
                journal, cycle_id, header_status, abort_reason, stamp,
                scanned + errors + deferred + skipped,
            )
            return _finish(ScanOutcome(
                status=header_status,
                reason=abort_reason,
                cycle_id=cycle_id,
                computed=computed,
                scanned=scanned,
                errors=errors,
                deferred=deferred,
                skipped=skipped,
            ))
        except Exception:
            if ticked and getattr(state, "state", None) == STATE_SCANNING:
                state.transition(STATE_IDLE, "SCAN_EMPTY", reason="scan failed")
            raise

    def _precheck(self, state, mode, online, allowed, now_m) -> str | None:
        if str(mode).upper() != "PAPER":
            return "NOT_PAPER"
        current = getattr(state, "state", None)
        if current != STATE_IDLE:
            return f"STATE_{current}"
        if self.interval_seconds > 0 and self._last_attempt is not None:
            if now_m - self._last_attempt < self.interval_seconds:
                return "CYCLE_INTERVAL"
        if not self.symbols:
            return "NO_SYMBOLS"
        if not online():
            return _TERMINAL
        if not allowed():
            return _MISMATCH
        return None

    def _one_symbol(self, symbol, view, compute) -> tuple[str, str, bool]:
        """Filter, then optionally compute. Market-closed wins over staleness."""
        if not view.get("market_open", False):
            return "skipped", _MARKET_CLOSED, False
        if view.get("stale"):
            return "skipped", _STALE_QUOTE, False
        bar = view.get("bar_ts")
        bar_key = "" if bar is None else str(bar)
        if not bar_key or self._last_bar.get(symbol) == bar_key:
            return "skipped", _NO_NEW_BAR, False
        if compute is not None:
            compute(symbol, view)
        self._last_bar[symbol] = bar_key
        return "scanned", "", True if compute is not None else False

    def _defer_rest(self, journal, cycle_id, start, reason, stamp) -> int:
        count = 0
        for symbol in self.symbols[start:]:
            self._symbol_row(journal, cycle_id, symbol, "deferred", reason, None, stamp)
            count += 1
        return count

    def _note_skip(self, journal, reason, now_m, stamp) -> None:
        changed = reason != self._active_reason
        last = self._last_journaled.get(reason)
        due = last is None or (now_m - last >= self.remind_every)
        if not changed and not due:
            return
        self._last_journaled[reason] = now_m
        self._active_reason = reason
        self._header(journal, uuid.uuid4().hex, "skipped", reason, stamp, 0)

    def _header(self, journal, cycle_id, status, reason, stamp, count) -> None:
        journal.execute(
            "INSERT INTO scan_results "
            "(symbols_json, ranked_json, ts, cycle_id, status, reason, symbol_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("[]", "[]", stamp, cycle_id, status, reason or "", int(count)),
        )

    def _symbol_row(self, journal, cycle_id, symbol, status, reason, bar_ts, stamp) -> None:
        journal.execute(
            "INSERT INTO scan_symbols "
            "(cycle_id, symbol, status, reason, bar_ts, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (cycle_id, symbol, status, reason or "", None if bar_ts is None else str(bar_ts), stamp),
        )

    def _guard_event(self, journal, kind, detail, stamp) -> None:
        offline = kind == _TERMINAL
        journal.log_event(
            "account",
            "Terminal offline" if offline else "Account mismatch",
            severity="WARNING" if offline else "CRITICAL",
            source="engine",
            context={"kind": "terminal_offline" if offline else "account_mismatch",
                     "reason": detail, "phase": "scan", "ts": stamp},
        )
