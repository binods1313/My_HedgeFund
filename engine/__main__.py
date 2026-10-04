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
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from engine.commands import CommandProcessor
from engine.config import (
    MODE_LIVE,
    MODE_PAPER,
    load_engine_config,
    read_mt5_credentials,
)
from engine.equity import EquityTracker, broker_today
from engine.executor import ExecutionRejected, build_executor
from engine.gateway import Gateway
from engine.journal import Journal, resolve_db_path
from engine.keys import keyring
from engine.risk import RiskEngine
from engine.state import StateMachine

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

    _running: bool = True

    def request_stop(self) -> None:
        self._running = False

    # -- telemetry -------------------------------------------------------

    def _write_telemetry(self) -> None:
        """Account, positions and equity into the DB for the UI to read."""
        if not self.gateway.connected:
            return
        try:
            account = self.gateway.account_info()
        except Exception:   # noqa: BLE001 - telemetry must never kill the loop
            return
        if account is None:
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
        """The broker's trading day, from terminal server time.

        Falls back to UTC when the terminal is unreachable. The daily loss
        baseline must reset on the broker's calendar; using the operator's
        local clock would reset it up to a day out of step with the venue.
        """
        server_epoch = None
        try:
            if self.gateway.connected:
                tick = self.gateway.symbol_info_tick("EURUSD")
                if tick is not None and getattr(tick, "time", None):
                    server_epoch = int(tick.time)
        except Exception:   # noqa: BLE001
            server_epoch = None
        return broker_today(server_epoch)

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
        """
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

    def run_once(self) -> None:
        """One iteration: commands, heartbeat, telemetry."""
        self.commands.poll_once()
        self.state.heartbeat(connected=self.gateway.connected)
        self._write_telemetry()

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
        self.journal.log_event("engine", "engine stopped",
                               context={"state": self.state.state})
        self.journal.close()


def _broker_server_epoch(gateway) -> int | None:
    """Terminal server time as a unix epoch, or None when unreachable.

    Used to decide the broker's trading day, so the daily loss baseline resets
    on the venue's calendar rather than the operator's.
    """
    try:
        if not gateway.connected:
            return None
        tick = gateway.symbol_info_tick("EURUSD")
        if tick is not None and getattr(tick, "time", None):
            return int(tick.time)
    except Exception:   # noqa: BLE001
        return None
    return None


def build_engine(*, config_path: Path | str | None = None,
                 root: Path | str | None = None,
                 mt5_module=None,
                 live_confirmed: bool = False,
                 journal_path: Path | str | None = None) -> Engine:
    """Assemble the engine. Raises on invalid config rather than degrading."""
    config = load_engine_config(root=root, config_path=config_path)

    db = Path(journal_path) if journal_path else config.db_path
    journal = Journal(db)

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

    return Engine(config=config, journal=journal, state=state_machine,
                  gateway=gateway, executor=executor, commands=commands,
                  risk=risk, equity_tracker=equity_tracker)


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
        result = engine.reconcile_positions()
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