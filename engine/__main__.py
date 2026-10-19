"""Engine entry point: python -m engine

Runs as its own process so it survives the browser closing. Owns the MT5
connection, the state machine, the command poll loop and the heartbeat.

Startup order matters and is deliberate:

  1. Validate config. Invalid config aborts before anything is opened.
  2. Open the journal and migrate. An unknown schema aborts.
  3. Reconcile positions against the broker (adopt-and-log).
  4. Start the gateway.
  5. Enter the loop: poll commands, heartbeat, write telemetry.

Nothing here places an order on its own. Phase 1 has no strategies, so the only
way an order is created is an explicit command, and the Phase 1 risk stub
rejects it regardless.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from engine.account_guard import (
    AccountGuard,
    AccountMismatch,
    Fingerprint,
    classify_foreign_positions,
)
import logging
from engine.commands import CommandProcessor
from engine.config import (
    MODE_LIVE,
    MODE_PAPER,
    load_engine_config,
    read_mt5_credentials,
)
from engine.equity import EquityTracker, broker_today, validate_server_epoch
from engine.executor import ExecutionRejected, build_executor
from engine.gateway import Gateway
from engine.journal import Journal, resolve_db_path
from engine.keys import keyring
from engine.risk import RiskEngine
from engine.state import StateMachine
from engine.notifications import (
    cleanup_pending_trades,
    handle_approval_message,
    init_telegram,
    poll_updates,
    process_no_approval,
    process_yes_approval,
    stop_telegram_polling,
    pending_trade_count,
)

def _warn_foreign_positions(journal, positions, *, magic: int) -> list:
    """Journal foreign positions once per run, never adopt them.

    A position with a magic number that is not ours is somebody else's trade.
    It is counted for exposure and margin by the risk gate, but the engine must
    never manage, modify, close or trail it -- and it must not be written into
    the positions table, where it would look like ours.
    """
    foreign = classify_foreign_positions(positions, magic=magic)
    if foreign:
        journal.log_event(
            "reconcile", "foreign positions present",
            severity="WARNING", source="engine",
            context={
                "count": len(foreign),
                "source": "positions exist on the account with a magic "
                          "number this engine does not own",
                "tickets": [row["ticket"] for row in foreign],
                "symbols": [row["symbol"] for row in foreign],
                "note": "counted for exposure and margin; not managed, "
                        "not modified, not closed",
            })
    return foreign


logger = logging.getLogger(__name__)


BANNER = "=" * 68


@dataclass
class Engine:
    """Holds the long-lived objects and the run loop."""

    config: object
    journal: Journal
    state: StateMachine
    gateway: Gateway
    executor: object
    commands: CommandProcessor
    risk: RiskEngine
    equity_tracker: EquityTracker = None
    guard: AccountGuard | None = None
    _account_verified: bool = False
    _was_connected: bool = False
    _running: bool = True
    _epoch_cache: int | None = None
    _telegram_initialized: bool = False
    _telegram_poll_task = None
    _telegram_stop_event = None
    #: Stage 1 scanner. None in tests that construct an Engine by hand.
    scanner: object | None = None
    #: Stage 1b signal wiring. None unless a strategy module is configured.
    strategy: object | None = None
    #: A repeating telemetry gap is journalled at most this often, in seconds.
    _telemetry_warn_every: float = 300.0
    #: reason -> monotonic time the reason was last journalled.
    _telemetry_warnings: dict | None = None

    def request_stop(self) -> None:
        self._running = False

    # -- Stage 1b signal path ----------------------------------------------

    def read_history(self, symbol: str) -> Any:
        """Fetch closed M15 candle history for one symbol on the owner thread.

        Delegates to the gateway's read_history, which dispatches through
        call() so the MT5 terminal read happens on the engine's owner thread.
        The scanner calls this only when a symbol has a newly closed bar.
        """
        return self.gateway.read_history(symbol)

    def evaluate_strategy(self, symbol: str, df: Any) -> Any:
        """Evaluate the confluence strategy for one symbol.

        Delegates to the strategy module's evaluate() function. Returns a
        Signal (or subclass) whose is_actionable property tells the scanner
        whether a PENDING row should be written.
        """
        if self.strategy is None:
            return None
        # build_engine stores the evaluate function. A double may be an
        # object with an evaluate method. A bare function has no evaluate
        # attribute; calling .evaluate on it raises AttributeError and
        # the scan records no verdict.
        method = getattr(self.strategy, "evaluate", None)
        if callable(method):
            return method(symbol, df)
        return self.strategy(symbol, df)

    @property
    def account(self) -> Any:
        """A fresh AccountState for the risk gate, built from telemetry.

        Falls back to None when the account is not yet verified, so the
        risk pre-check correctly fails closed until the fingerprint matches.
        """
        if not self._account_verified:
            return None
        return self.risk.account_state_from_positions(
            equity=self._last_equity(),
            start_of_day_equity=self._sod_equity(),
            peak_equity=self._peak_equity(),
            open_positions=self._open_positions_for_risk(),
        )

    def _last_equity(self) -> float | None:
        """Most recent account equity reading, or None."""
        try:
            account = self.gateway.account_info()
        except Exception:
            return None
        if account is None:
            return None
        return float(getattr(account, "equity", 0.0) or 0.0)

    def _sod_equity(self) -> float | None:
        """Start-of-day equity for the broker's current trading day."""
        baselines = self.equity_tracker.current() if self.equity_tracker else None
        return baselines.start_of_day_equity if baselines else None

    def _peak_equity(self) -> float | None:
        """Peak equity for the broker's current trading day."""
        baselines = self.equity_tracker.current() if self.equity_tracker else None
        return baselines.peak_equity if baselines else None

    def _open_positions_for_risk(self) -> list[dict]:
        """Open positions as plain dicts for the risk gate.

        Uses the journal's stored positions rather than re-reading the
        terminal, so the risk check stays on the owner thread and does
        not add a second broker round-trip to the cycle.
        """
        try:
            return self.journal.positions()
        except Exception:
            return []

    # -- telegram approval layer -----------------------------------------

    async def _start_telegram_polling(self) -> None:
        """Start the telegram approval message poll loop."""

        self._telegram_stop_event = asyncio.Event()

        async def handler(text: str, from_chat_id: int) -> None:
            await self._handle_telegram_message(text, from_chat_id)

        self._telegram_poll_task = asyncio.create_task(
            poll_updates(self._telegram_stop_event, handler))
        logger.info("Telegram polling started")

    async def _handle_telegram_message(self, text: str, from_chat_id: int) -> None:
        """Process an incoming telegram message.

        Routes YES/NO to the executor / cancellation logic.
        """

        result = await handle_approval_message(text, from_chat_id)
        if result == "unauthorized":
            # Do not answer - ignore unauthorized users entirely.
            return

        # The approved/rejected result from handle_approval_message is a short-
        # lived decision. The pending trade carries the authority, so the next
        # step looks up the trade by its id and acts on that single record.
        trade_id = self._extract_trade_id(text)
        if not trade_id:
            return

        if result == "approved":
            await self._handle_yes_approval(trade_id)
        elif result == "rejected":
            await self._handle_no_approval(trade_id)

    @staticmethod
    def _extract_trade_id(text: str) -> str | None:
        """Extract the trade id from an incoming YES/NO telegram message."""

        import re as _re

        match = _re.match(r"^(?:YES|NO)\s+(\S+)$", text.strip())
        if not match:
            return None
        return match.group(1)

    async def _handle_yes_approval(self, trade_id: str) -> None:
        """Forward a YES to the approval flow's executor."""

        if pending_trade_count() == 0:
            # Nothing to do; this can happen when two approvals arrive at
            # once or when the cache has already been cleared.
            return

        response = await process_yes_approval(trade_id, self.executor,
                                              self.journal, self.gateway)

        if response == "not_found":
            return  # nothing to do; already gone
        if response == "already_processed":
            # A duplicate YES: only one execution is allowed.
            return

        # Single execution per trade_id. The response carries the actual
        # executor result so the operator gets the real outcome.
        if response == "executed":
            logger.info("Telegram approval resulted in a trade")
        else:
            logger.warning("Telegram approval did not result in a trade: %s",
                           response)

    async def _handle_no_approval(self, trade_id: str) -> None:
        """Forward a NO to cancel the pending trade."""

        await process_no_approval(trade_id, self.journal)
        logger.info("Telegram approval cancelled trade %s", trade_id)

    # -- shutdown ----------------------------------------------------------

    def _stop_telegram_polling(self) -> None:
        """Stop the telegram polling loop (cooperative)."""

        if self._telegram_stop_event is None:
            return
        stop_telegram_polling(self._telegram_stop_event)
        if self._telegram_poll_task is not None:
            self._telegram_poll_task.cancel()
            try:
                self._telegram_poll_task = None
                logger.info("Telegram polling stopped")
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass

    # -- telemetry -------------------------------------------------------

    def _warn_telemetry(self, reason: str, detail: str) -> None:
        """Record a telemetry gap in the journal, without flooding it.

        Between 2026-10-07 and 2026-10-10 the engine kept heartbeating while
        `account_info()` returned nothing: heartbeats continued, equity
        snapshots stopped, and no event explained why. The gap must be visible
        to an operator reading the journal, but it must not be written once per
        loop iteration, so each reason is logged at most every 5 minutes.
        """
        if self._telemetry_warnings is None:
            self._telemetry_warnings = {}
        now = time.monotonic()
        last = self._telemetry_warnings.get(reason, 0.0)
        if last and now - last < self._telemetry_warn_every:
            return
        self._telemetry_warnings[reason] = now
        self.journal.log_event(
            "telemetry", detail, severity="WARNING", source="engine",
            context={"reason": reason})

    def _write_telemetry(self) -> None:
        """Account, positions and equity into the DB for the UI to read.

        Blocked while the account fingerprint is unverified: writing account B's
        equity into account A's journal is the mix the whole guard exists to
        prevent, and it would look exactly like legitimate data afterwards.
        """
        if self.guard is not None and not self.guard.may_write_telemetry():
            return
        if not self.gateway.connected:
            return
        try:
            account = self.gateway.account_info()
        except Exception as exc:   # noqa: BLE001 - telemetry must never kill the loop
            self._warn_telemetry(
                "account_read_failed",
                "gateway is connected but the account read raised",
            )
            self.journal.log_event(
                "telemetry", "account read raised", severity="WARNING",
                source="engine",
                context={"error": f"{type(exc).__name__}: {exc}"})
            return
        if account is None:
            self._warn_telemetry(
                "account_empty",
                "gateway is connected but account_info() returned nothing; "
                "no equity snapshot was written",
            )
            return

        equity = float(getattr(account, "equity", 0.0) or 0.0)
        balance = float(getattr(account, "balance", 0.0) or 0.0)
        margin = float(getattr(account, "margin", 0.0) or 0.0)

        try:
            self.journal.execute(
                "INSERT INTO quotes (symbol, bid, ask, spread, tick_time, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET bid=excluded.bid, "
                "ask=excluded.ask, spread=excluded.spread, "
                "tick_time=excluded.tick_time, updated_at=excluded.updated_at",
                ("ACCOUNT_EQUITY", equity, equity, 0.0, int(time.time()),
                 datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        except Exception:   # noqa: BLE001
            pass

        # Feed the equity tracker rather than writing a bare snapshot. This is
        # what supplies start_of_day_equity and peak_equity, without which the
        # Risk Engine correctly refuses every order on 'peak equity unknown'.
        if equity > 0:
            try:
                open_risk = self._current_open_risk()
                self.equity_tracker.observe(
                    equity, balance=balance, margin=margin,
                    open_risk=open_risk,
                    open_positions=self.journal.open_position_count(),
                    now_day=self._broker_today(),
                )
                self._auto_halt_on_breach(equity)
            except Exception:   # noqa: BLE001
                pass

    def _broker_today(self) -> str:
        """The broker's trading day, from a VALIDATED terminal clock.

        Falls back to UTC when no sample is plausible. The daily loss baseline
        must reset on the broker's calendar; using the operator's local clock
        would reset it out of step with the venue, and using an unvalidated
        tick time was measured on this account to be up to a day wrong.
        """
        return broker_today(self._server_epoch())

    def _server_epoch(self) -> int | None:
        """Validated broker server time, memoised per engine instance."""
        if self._epoch_cache is None:
            self._epoch_cache = _broker_server_epoch(self.gateway)
        return self._epoch_cache

    def _current_open_risk(self) -> float | None:
        """Summed entry-to-stop risk across open positions, or None if unknown.

        None when any position cannot be measured, so the gate treats it as
        unknown rather than as zero.
        """
        positions = self.journal.positions()
        if not positions:
            return 0.0
        specs = self._symbol_specs()
        total = 0.0
        for position in positions:
            spec = specs.get(position["symbol"].upper()) or {}
            tick_value = float(spec.get("trade_tick_value") or 0.0)
            tick_size = float(spec.get("trade_tick_size") or 0.0)
            stop = float(position.get("sl") or 0.0)
            entry = float(position.get("price_open") or 0.0)
            volume = float(position.get("volume") or 0.0)
            if not tick_value or not tick_size or stop <= 0 or entry <= 0:
                return None      # unmeasurable; do not report zero risk
            total += abs(entry - stop) / tick_size * tick_value * volume
        return total

    def _symbol_specs(self) -> dict:
        """Trade specifications for the symbols we hold, from the terminal."""
        if not self.gateway.connected:
            return {}
        specs = {}
        for position in self.journal.positions():
            name = position["symbol"]
            if name.upper() in specs:
                continue
            try:
                info = self.gateway.symbol_info(name)
            except Exception:   # noqa: BLE001
                info = None
            if info is not None:
                specs[name.upper()] = {
                    "trade_tick_value": getattr(info, "trade_tick_value", None),
                    "trade_tick_size": getattr(info, "trade_tick_size", None),
                }
        return specs

    def _auto_halt_on_breach(self, equity: float) -> None:
        """Halt the engine when a hard limit is breached.

        The gate blocks individual orders; this stops the engine outright, so a
        breached limit cannot be worked around by a later order that happens to
        measure differently.
        """
        baselines = self.equity_tracker.current(self._broker_today())
        if baselines is None or self.risk.halted:
            return
        drawdown = baselines.drawdown_pct(equity)
        daily_loss = baselines.daily_loss_pct(equity)

        if drawdown is not None and drawdown >= self.risk.limits.max_drawdown_pct:
            self.risk.halt(
                "drawdown",
                f"drawdown {drawdown:.2f}% reached the "
                f"{self.risk.limits.max_drawdown_pct}% limit; manual resume "
                f"required")
            try:
                self.state.transition("PAUSED", "PAUSE", reason="drawdown limit")
            except Exception:   # noqa: BLE001
                pass
        elif daily_loss is not None and daily_loss >= self.risk.limits.daily_loss_limit_pct:
            self.risk.halt(
                "daily_loss",
                f"daily loss {daily_loss:.2f}% reached the "
                f"{self.risk.limits.daily_loss_limit_pct}% limit")
            try:
                self.state.transition("PAUSED", "PAUSE", reason="daily loss limit")
            except Exception:   # noqa: BLE001
                pass
        else:
            # Within limits: clear a daily-loss halt for the new day.
            self.risk.clear_daily_loss_halt()

    # -- reconciliation --------------------------------------------------

    def reconcile_positions(self) -> dict:
        """Adopt broker positions matching our magic number.

        Adopt-and-log, per the resolved decision: leaving live exposure unmanaged
        is the larger risk. A position the broker has and we do not is adopted
        with an events row; one we have and the broker does not is marked
        closed_externally rather than silently deleted.

        Never called directly by the loop: use `_reconcile()`, which gates this
        on the account fingerprint. Called on a mismatched account it deletes
        journal rows whose only real change is that they belong to another
        account.
        """
        if self.guard is not None and not self.guard.may_reconcile():
            self.journal.log_event(
                "reconcile", "blocked: account fingerprint does not match",
                severity="WARNING",
                context={"reason": self.guard.mismatch_reason})
            return {"adopted": [], "vanished": [], "error": "account mismatch"}
        return self._reconcile_positions_unlocked()

    def _reconcile_positions_unlocked(self) -> dict:
        magic = self.config.engine.magic_number
        known = {int(p["ticket"]): p for p in self.journal.positions()}
        adopted, vanished = [], []

        # Reading broker state requires a live connection. Without this the read
        # raises NotConnected and every position looks absent.
        if not self.gateway.connected and not self.gateway.ensure_connected():
            self.journal.log_event(
                "reconcile", "skipped: gateway not connected",
                severity="WARNING",
                context={"error": self.gateway.status.last_error or "unknown"},
            )
            return {"adopted": [], "vanished": [], "error": "not connected"}

        try:
            positions = self.gateway.positions_get() or []
        except Exception as exc:   # noqa: BLE001
            self.journal.log_event(
                "reconcile", "could not read broker positions",
                severity="WARNING", context={"error": str(exc)})
            return {"adopted": [], "vanished": [], "error": str(exc)}

        # Report the positions that are not ours whenever they are seen, so an
        # operator reading the reconcile outcome finds them next to the
        # adoptions rather than having to infer them from exposure alone.
        _warn_foreign_positions(self.journal, positions, magic=magic)

        for pos in positions:
            ticket = int(getattr(pos, "ticket", 0))
            if not ticket or getattr(pos, "magic", None) != magic:
                continue
            if ticket in known:
                continue
            self.journal.upsert_position(
                ticket=ticket,
                symbol=getattr(pos, "symbol", ""),
                side="BUY" if getattr(pos, "type", 0) == 0 else "SELL",
                volume=float(getattr(pos, "volume", 0.0)),
                price_open=float(getattr(pos, "price_open", 0.0)),
                price_current=float(getattr(pos, "price_current", 0.0)),
                sl=float(getattr(pos, "sl", 0.0) or 0.0),
                tp=float(getattr(pos, "tp", 0.0) or 0.0),
                profit=float(getattr(pos, "profit", 0.0) or 0.0),
                magic=magic,
            )
            adopted.append(ticket)
            self.journal.log_event(
                "reconcile", "adopted orphaned broker position",
                severity="WARNING",
                context={"ticket": ticket,
                         "symbol": getattr(pos, "symbol", None),
                         "reason": "present at broker with our magic number, "
                                   "absent from the journal"})

        for ticket, row in known.items():
            # Simulated PAPER fills carry our magic but never exist at the
            # broker: their tickets come from the 9M sequence, not from MT5.
            # Treating them as "absent at the broker" and deleting them is
            # exactly the false-reconcile this gate proves must not happen.
            # Only rows the broker could have produced (a matching trade in
            # `trades` with broker != PAPER) are eligible for the vanished
            # path; simulated rows are owned by the PAPER executor alone.
            if self._is_simulated_position(ticket):
                continue
            if ticket not in {int(p.ticket) for p in positions
                              if getattr(p, "magic", None) == magic}:
                vanished.append(ticket)
                self.journal.log_event(
                    "reconcile", "journal position no longer at broker",
                    severity="WARNING",
                    context={"ticket": ticket, "symbol": row.get("symbol"),
                             "reason": "closed outside the engine"})
                self.journal.delete_position(ticket)

        return {"adopted": adopted, "vanished": vanished, "error": None}

    def _is_simulated_position(self, ticket: int) -> bool:
        """True when the ticket is a PAPER simulation, not a broker fill.

        Simulated tickets come from PaperExecutor's 9M sequence and the
        matching `trades` row carries broker='PAPER'. Either marker alone
        is enough: a real broker ticket never reaches 9M, and a LIVE row
        never carries the PAPER broker tag.
        """
        try:
            if int(ticket) >= 9_000_000:
                return True
        except (TypeError, ValueError):
            return False
        try:
            rows = self.journal.query(
                "SELECT broker FROM trades WHERE ticket = ?", (int(ticket),))
        except Exception:  # noqa: BLE001 - unreadable trades = not simulated
            return False
        return bool(rows) and str(rows[0]["broker"]).upper() == "PAPER"

    # -- backup ----------------------------------------------------------

    def backup_database(self) -> Path | None:
        """Daily copy, WAL checkpoint first so the copy is self-contained."""
        try:
            self.journal.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:   # noqa: BLE001
            pass

        target_dir = self.config.backup_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        destination = target_dir / f"engine_{stamp}.db"
        try:
            shutil.copy2(self.config.db_path, destination)
        except Exception as exc:   # noqa: BLE001
            self.journal.log_event("backup", "backup failed", severity="WARNING",
                                   context={"error": str(exc)})
            return None
        self.journal.log_event("backup", "database backed up",
                               context={"path": str(destination)})
        return destination

    # -- loop ------------------------------------------------------------

    def _verify_account(self, phase: str) -> bool:
        """Check the account fingerprint. Heartbeats continue either way.

        Returns True when the terminal is on the account this journal was made
        for. While False the engine still heartbeats, because the dashboard
        must be able to show "Account mismatch" rather than "offline" -- a
        heartbeat that stops is indistinguishable from a crash, and an operator
        cannot tell a mismatch from a dead process.
        """
        if self.guard is None:
            return False
        try:
            self.guard.check(self.gateway.account_info(), phase=phase)
            return True
        except AccountMismatch:
            return False
        except Exception as exc:   # noqa: BLE001 - guard must never kill the loop
            logger.warning("account guard raised during %s: %s", phase, exc)
            return False

    def _verify_account_transitions(self) -> bool:
        """Verify on the transitions that matter: connect and reconnect.

        A steady-state check every iteration would be redundant, but the
        moments that change the answer are the moments the link comes back:
        the terminal was logged out, someone switched accounts, or MT5 was
        reinitialised. All three look identical from here -- a link that was
        down is now up -- so all three get the same treatment: verify, and if
        it does not match, block everything that writes.
        """
        connected = bool(self.gateway.connected)
        if connected and not self._was_connected:
            self._was_connected = True
            return self._verify_account("reconnect")
        if not connected:
            self._was_connected = False
            return self._account_verified
        return self._account_verified

    def _reconcile(self) -> dict:
        """Reconcile, but only on a matching account fingerprint.

        Reconcile is the one routine that deletes journal rows, and after an
        account switch every journal ticket looks absent at the broker, so an
        unguarded call wipes the positions table. Gating it is therefore the
        whole point of the guard, not defence in depth.
        """
        if self.guard is not None and not self.guard.may_reconcile():
            self.journal.log_event(
                "reconcile", "blocked: account fingerprint does not match",
                severity="WARNING",
                context={"reason": self.guard.mismatch_reason})
            return {"adopted": [], "vanished": [], "error": "account mismatch"}
        return self.reconcile_positions()

    def _scan_cycle(self) -> None:
        """Stage 1 scan. A missing scanner is a no-op for older tests.

        The call sits in ``run_once``, which itself sits inside the
        ``while`` in ``run_forever``. ``CYCLE_TICK`` is emitted from
        ``ScanCycle.run`` only after that call, never at import and never
        after the loop.
        """
        scanner = self.scanner
        if scanner is None:
            return
        try:
            scanner.run_with_engine(self)
        except Exception as exc:  # noqa: BLE001 - a scan must not kill the loop
            self.journal.log_event(
                "scan", "scan cycle failed", severity="ERROR", source="engine",
                context={"error": f"{type(exc).__name__}: {exc}"},
            )

    def run_once(self) -> None:
        """One iteration: commands, heartbeat, telemetry, then one scan."""
        gateway = getattr(self, "gateway", None)
        if gateway is not None:
            # Hand-built test engines use a stub with no watchdog. Production
            # always has one. A missing method is not a stall.
            poll = getattr(gateway, "poll_watchdog", None)
            if poll is not None:
                poll()
        self.commands.poll_once()
        # A reconnect (or reinitialisation) is the moment the account answer
        # changes, so it is checked there rather than every iteration.
        self._verify_account_transitions()
        # Session refresh keeps a long-lived process honest: a swap that happens
        # with no disconnect at all is otherwise invisible. Fingerprint
        # verification runs before telemetry: a recovery event is written
        # inside the guard, and equity stays blocked until that succeeds.
        self._account_verified = self._verify_account("session_refresh")
        readable = bool(getattr(self.gateway, "account_readable", False))
        age = None
        if hasattr(self.gateway, "last_success_age"):
            age = self.gateway.last_success_age()
        self.state.heartbeat(connected=readable, gateway_call_age=age)
        self._write_telemetry()
        # Scan stays inside the iteration, after the heartbeat, so a slow
        # cycle cannot skip the beat and a dedent cannot move it outside
        # the while that calls run_once.
        self._scan_cycle()

    def run_forever(self) -> None:
        poll = self.config.engine.command_poll_seconds
        heartbeat = self.config.engine.heartbeat_seconds
        telemetry_every = heartbeat
        last_heartbeat = 0.0
        last_telemetry = 0.0
        last_backup_day = None

        print(f"[engine] mode={self.config.mode} "
              f"state={self.state.state}")
        print(f"[engine] db={self.config.db_path}")
        print(f"[engine] polling every {poll}s, heartbeat every {heartbeat}s")

        # -- telegram approval layer ----------------------------------------

        if self._telegram_initialized:
            try:
                asyncio.run(self._start_telegram_polling())
            except Exception:  # noqa: BLE001 - telegram must not block startup
                self._telegram_initialized = False

        while self._running:
            started = time.monotonic()
            try:
                self.run_once()
            except KeyboardInterrupt:
                print("\n[engine] interrupted; shutting down")
                break
            except Exception as exc:   # noqa: BLE001 - loop must survive
                self.journal.log_event("engine", "loop iteration failed",
                                       severity="ERROR",
                                       context={"error": f"{type(exc).__name__}: {exc}"})

            # The heartbeat, telemetry and backup gates live inside the loop.
            # They were once indented at method level instead, which put the
            # rate limit *and the sleep* after the loop, so the loop ran with no
            # throttle at all and wrote several hundred heartbeats a second.
            now = time.monotonic()
            if now - last_heartbeat >= heartbeat:
                last_heartbeat = now
            if now - last_telemetry >= telemetry_every:
                last_telemetry = now
            today = datetime.now(timezone.utc).date()
            if last_backup_day != today:
                last_backup_day = today
                self.backup_database()

            elapsed = time.monotonic() - started
            if elapsed < poll:
                time.sleep(poll - elapsed)

        self.shutdown()

    def shutdown(self) -> None:
        print("[engine] shutting down")
        try:
            self.gateway.shutdown_terminal()
        except Exception:   # noqa: BLE001
            pass
        self.gateway.stop()
        self._stop_telegram_polling()
        cleanup_pending_trades()
        self.journal.log_event("engine", "engine stopped",
                               context={"state": self.state.state})
        self.journal.close()


def _broker_server_epoch(gateway) -> int | None:
    """A VALIDATED terminal server time, or None when none can be trusted.

    Several symbols are sampled because the terminal does not report one
    consistent clock: on the XM demo account, forex and index ticks were a day
    stale while a crypto tick was correct to the minute. A single sample would
    therefore seed the wrong trading day, which resets the daily loss limit at
    the wrong moment. Each candidate is validated against the local clock and
    the first plausible one wins; if none is plausible the caller falls back to
    UTC.
    """
    if not gateway.connected:
        return None

    for symbol in ("EURUSD", "BTCUSD#", "US500Cash#", "GBPUSD"):
        try:
            tick = gateway.symbol_info_tick(symbol)
        except Exception:   # noqa: BLE001
            continue
        if tick is None or not getattr(tick, "time", None):
            continue
        validated = validate_server_epoch(int(tick.time))
        if validated is not None:
            return validated
    return None


def _journal_execution_mode(journal: Journal, mode: str) -> None:
    """Record approve_first when it changes. Does not place an order.

    The same value on the next start writes nothing. A different previous
    value writes one INFO row so the journal shows the change.
    """
    import json
    rows = journal.query(
        "SELECT context_json FROM events WHERE category = ? AND message = ? "
        "ORDER BY id DESC LIMIT 1",
        ("engine", "execution mode"),
    )
    previous = None
    if rows:
        try:
            previous = json.loads(rows[0]["context_json"]).get("execution_mode")
        except (TypeError, ValueError):
            previous = None
    if previous == mode:
        return
    journal.log_event(
        "engine", "execution mode", severity="INFO", source="engine",
        context={
            "kind": "execution_mode",
            "execution_mode": mode,
            "previous": previous,
        },
    )


def build_engine(*, config_path: Path | str | None = None,
                 root: Path | str | None = None,
                 mt5_module=None,
                 live_confirmed: bool = False,
                 journal_path: Path | str | None = None) -> Engine:
    """Assemble the engine. Raises on invalid config rather than degrading."""
    config = load_engine_config(root=root, config_path=config_path)

    db = Path(journal_path) if journal_path else config.db_path
    journal = Journal(db)
    _journal_execution_mode(journal, config.execution_mode)

    state_machine = StateMachine(journal)
    state_machine.set_mode(config.mode)

    credentials = read_mt5_credentials()
    gateway = Gateway(
        login=credentials["login"],
        password=credentials["password"],
        server=credentials["server"],
        terminal_path=credentials["terminal_path"] or None,
        magic=config.engine.magic_number,
        call_timeout=config.engine.gateway.call_timeout_seconds,
        backoff=tuple(config.engine.gateway.reconnect_backoff_seconds),
        order_lookback=config.engine.gateway.order_lookback_seconds,
        max_account_reinitializes=config.engine.gateway.account_reinitialize_attempts,
        watchdog_timeout=config.engine.gateway.watchdog_timeout_seconds,
        mt5_module=mt5_module,
        journal=journal,
    )
    gateway.start()
    # start() only spawns the owner thread. Connect explicitly before anything
    # reads broker state: reconciliation ran first and logged 'not connected:
    # not started', because nothing had asked the gateway to connect.
    if not gateway.ensure_connected():
        journal.log_event(
            "gateway", "initial connect failed; trading stays blocked",
            severity="WARNING",
            context={"error": gateway.status.last_error or "unknown"},
        )

    # -- account guard ---------------------------------------------------
    # Built after the gateway, because the guard perches on the thing it
    # verifies. The configured fingerprint comes from the environment, so a
    # terminal logged into a different account fails here rather than after it
    # has already written telemetry into someone else's journal.
    expected_account = gateway.account_info() if gateway.connected else None
    guard = AccountGuard(
        journal=journal,
        configured=Fingerprint(
            login=credentials["login"],
            server=credentials["server"],
        ),
        magic=config.engine.magic_number,
    )
    startup_verified = False
    try:
        guard.check(expected_account, phase="startup")
        startup_verified = True
    except AccountMismatch:
        # Fail closed and keep running: the loop still heartbeats so the
        # dashboard reads "Account mismatch" instead of "offline", but no
        # telemetry, reconcile or order can proceed.
        startup_verified = False
    except Exception as exc:   # noqa: BLE001 - an unreadable account blocks start
        journal.log_event(
            "account", "account read failed during startup verification",
            severity="WARNING", source="engine",
            context={"error": f"{type(exc).__name__}: {exc}"})
        startup_verified = False

    # Foreign positions are journalled once, at startup, and then left alone.
    if startup_verified:
        try:
            _warn_foreign_positions(
                journal, gateway.positions_get() or [],
                magic=config.engine.magic_number)
        except Exception as exc:   # noqa: BLE001 - reporting must not abort start
            journal.log_event(
                "reconcile", "foreign position scan failed",
                severity="WARNING",
                context={"error": f"{type(exc).__name__}: {exc}"})

        # Mirror the fingerprint onto engine_state so the account strip and the
        # state row are read together. journal_account stays authoritative.
        state_machine.journal.set_state(
            state_machine.state,
            reason="account fingerprint verified at startup",
            mode=config.mode,
            account_login=guard.fingerprint.login,
            account_currency=guard.fingerprint.currency,
        )

    def tick_provider(symbol: str):
        if not gateway.connected:
            return None
        return gateway.symbol_info_tick(symbol)

    try:
        executor = build_executor(
            config.mode, journal=journal, gateway=gateway,
            state=state_machine.state, tick_provider=tick_provider,
            live_confirmed=live_confirmed,
        )
    except ExecutionRejected as exc:
        gateway.stop()
        journal.close()
        raise

    equity_tracker = EquityTracker(
        journal, server_epoch_provider=lambda: _broker_server_epoch(gateway))

    risk = RiskEngine(journal)
    risk.set_magic(config.engine.magic_number)

    # Keep the executor's view of state current as the machine moves.
    def sync_state(from_state: str, to_state: str, trigger: str, reason: str) -> None:
        try:
            executor.state = to_state
        except Exception:   # noqa: BLE001
            pass

    state_machine.on_transition = sync_state

    commands = CommandProcessor(journal, state_machine, executor)

    # -- telegram approval layer (optional) -------------------------------
    telegram_initialized = asyncio.run(init_telegram(journal))
    if telegram_initialized:
        journal.log_event("engine", "telegram approval layer enabled",
                          source="engine")

    from engine.scan_cycle import ScanCycle

    scanner = ScanCycle(
        symbols=list(config.scan_symbols),
        max_scan_seconds=float(config.max_scan_seconds),
    )

    strategy = None
    if config.mode == "PAPER" and getattr(config, "ai_signals_enabled", False):
        from engine.strategy import evaluate as _strategy_evaluate
        strategy = _strategy_evaluate

    return Engine(config=config, journal=journal, state=state_machine,
                  gateway=gateway, executor=executor, commands=commands,
                  risk=risk, equity_tracker=equity_tracker,
                  guard=guard, _account_verified=startup_verified,
                  _telegram_initialized=telegram_initialized,
                  scanner=scanner, strategy=strategy)


def _mask_login(login) -> str:
    """Last four digits, at most. Never the whole login, never the password.

    A login is enough to log an account into a terminal, so printing it in a
    log or a screenshot is a disclosure. Four digits are enough to tell an
    operator whether they are looking at the account they meant.
    """
    digits = str(login) if login is not None else ""
    return ("****" + digits[-4:]) if len(digits) >= 4 else digits


def _report_account_check(engine) -> None:
    """Report match / mismatch only. No credentials, no password, no full login."""
    guard = engine.guard
    if guard is None:
        print("[account]  not verified: guard was not constructed")
        return

    row = engine.journal.get_account_fingerprint()
    if row:
        journal_login, journal_currency = row["login"], row["currency"]
        journal_server = row["server"]
    else:
        journal_login = journal_currency = journal_server = None

    print(f"[account]  terminal  {_mask_login(guard.fingerprint.login)}"
          f" | {guard.fingerprint.currency or 'unknown'}"
          f" | {guard.fingerprint.server}")
    print(f"[account]  journal   {_mask_login(journal_login)}"
          f" | {journal_currency or 'unknown'}"
          f" | {journal_server}")

    if guard.mismatch_reason:
        print(f"[account]  MISMATCH -- {guard.mismatch_reason}")
        print("[account]  trading, reconcile, telemetry and orders are blocked")
        return

    if not guard.fingerprint.login:
        print("[account]  no account is logged in")
        print("[account]  trading, reconcile, telemetry and orders are blocked")
        return

    print("[account]  MATCH")


# Handles held by this process, keyed by resolved lock path. Windows
# `msvcrt.locking` is per-handle: two opens in one process do NOT
# conflict, so without this registry the unit test (and any in-process
# second engine) would sail through. Cross-process exclusion still
# comes from the OS lock below.
_HELD_INSTANCE_LOCKS: dict[str, object] = {}


def _acquire_single_instance_lock(config) -> object | None:
    """Refuse a second engine on the same journal. Returns the lock handle.

    Uses an exclusive lock file next to the journal path. A second
    `python -m engine` pointing at the same journal gets None and the
    caller exits with a journaled reason instead of running. Two
    writers on one SQLite journal is how the pid-5516/pid-35972
    double-heartbeat happened.
    """
    import os

    db_path = config.db_path
    lock_path = db_path.parent / (db_path.stem + ".lock")
    key = str(lock_path.resolve()) if hasattr(lock_path, "resolve") else str(lock_path)
    if key in _HELD_INSTANCE_LOCKS:
        return None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(lock_path, "a+b")  # noqa: PTH123 - lock handle, not data
    except OSError:
        return None
    try:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                handle.close()
                return None
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(),
                            fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return None
    except (ImportError, OSError):
        try:
            handle.close()
        except OSError:
            pass
        return None
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"{os.getpid()}".encode())
        handle.flush()
    except OSError:
        pass
    _HELD_INSTANCE_LOCKS[key] = handle
    return handle


def _release_single_instance_lock(config) -> None:
    """Release a lock held by this process (tests / shutdown)."""
    db_path = config.db_path
    lock_path = db_path.parent / (db_path.stem + ".lock")
    key = str(lock_path.resolve()) if hasattr(lock_path, "resolve") else str(lock_path)
    handle = _HELD_INSTANCE_LOCKS.pop(key, None)
    if handle is not None:
        try:
            handle.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine",
        description="Autonomous trading engine (Phase 1 skeleton).")
    parser.add_argument("--config", help="path to config.yaml")
    parser.add_argument("--db", help="override the database path")
    parser.add_argument("--live", action="store_true",
                        help="acknowledge LIVE trading. Requires mode: LIVE "
                             "in config; either alone is refused.")
    parser.add_argument("--once", action="store_true",
                        help="run one iteration and exit")
    parser.add_argument("--check-config", action="store_true",
                        help="validate configuration and exit")
    args = parser.parse_args(argv)

    print(BANNER)
    print("  AI HEDGE FUND - ENGINE (Phase 1 skeleton)")
    print(BANNER)

    try:
        config = load_engine_config(config_path=args.config)
    except Exception as exc:   # noqa: BLE001
        print(f"[FATAL] configuration invalid: {exc}")
        return 2

    print(f"[config] mode={config.mode}")
    print(f"[config] db={config.db_path}")
    print(f"[config] heartbeat={config.engine.heartbeat_seconds}s "
          f"offline_after={config.engine.offline_after_seconds}s")
    print(f"[config] magic={config.engine.magic_number}")

    if args.check_config:
        print("[config] OK")
        return 0

    _instance_lock = _acquire_single_instance_lock(config)
    if _instance_lock is None:
        print(f"[FATAL] another engine is already running on {config.db_path}; "
              f"refusing to start a second writer")
        try:
            journal = Journal(config.db_path)
            try:
                journal.log_event(
                    "engine", "second engine refused by single-instance lock",
                    severity="WARNING", source="engine",
                    context={"db": str(config.db_path)})
            finally:
                journal.close()
        except Exception:  # noqa: BLE001 - refusal must stand on its own
            pass
        return 4

    ring = keyring()
    for line in ring.describe_all():
        print(f"[keys]   {line}")

    try:
        engine = build_engine(config_path=args.config,
                              journal_path=args.db,
                              live_confirmed=args.live)
    except ExecutionRejected as exc:
        print(f"[FATAL] {exc}")
        return 3
    except Exception as exc:   # noqa: BLE001
        print(f"[FATAL] startup failed: {type(exc).__name__}: {exc}")
        return 1

    # Account verification comes after the engine is assembled, because it
    # needs a gateway to ask. It is placed before every other start gate so a
    # mismatch is the first thing an operator sees, not the last.
    _report_account_check(engine)
    if config.is_live and not args.live:
        print("[FATAL] mode is LIVE but --live was not supplied; refusing")
        engine.shutdown()
        return 3

    # Handle a clean Ctrl+C.
    def handle_signal(signum, frame):
        engine.request_stop()

    signal.signal(signal.SIGINT, handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    if engine.config.engine.reconcile_on_start:
        # Gated on the account fingerprint inside _reconcile(); a mismatch
        # returns an error and deletes nothing.
        result = engine._reconcile()
        if result.get("adopted"):
            print(f"[engine] adopted {len(result['adopted'])} position(s)")
        if result.get("vanished"):
            print(f"[engine] {len(result['vanished'])} position(s) closed "
                  f"externally")

    try:
        if args.once:
            engine.run_once()
            engine.shutdown()
        else:
            engine.run_forever()
    except Exception as exc:   # noqa: BLE001
        print(f"[FATAL] run failed: {type(exc).__name__}: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
