"""Stage 1 scan cycle.

One cycle reads the market once, filters each symbol in isolation, and
returns the state machine to IDLE.

Stage 1b (signal writing) is an optional extension: when ``read_history``
and ``evaluate_strategy`` callables are injected, the cycle additionally
reads M15 history for bars that just closed, evaluates the confluence
strategy, runs a risk pre-check, and writes PENDING or REJECTED rows
to the signals journal. It does not submit an order. The transition
that would enter ANALYZING is not emitted.

Skip rows are written when the reason changes, and again as a reminder
after ``remind_every`` seconds. Identical repeats inside that window
write nothing.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from engine.journal import Journal, utc_now_iso
from engine.state import STATE_IDLE, STATE_SCANNING

# Market-closed is first on purpose. A symbol that is both closed and
# stale must be recorded as MARKET_CLOSED, never as STALE_QUOTE.
_SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
_TRADE_DISABLED = "TRADE_DISABLED"
_MARKET_CLOSED = "MARKET_CLOSED"
_STALE_QUOTE = "STALE_QUOTE"
_NO_NEW_BAR = "NO_NEW_CLOSED_BAR"
_BUDGET = "CYCLE_BUDGET_EXHAUSTED"
_TERMINAL = "TERMINAL_OFFLINE"
_MISMATCH = "ACCOUNT_MISMATCH"

# Signal-related constants
_MIN_BARS_FOR_SIGNAL = 206
_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
_RISK_PRECHECK_FAILED = "RISK_PRECHECK_FAILED"
_DRIFT_BLOCKED = "DRIFT_BLOCKED"
_STRATEGY_NO_SIGNAL = "STRATEGY_NO_SIGNAL"

# Deterministic UUID5 namespace for signal decision IDs.
_SIGNAL_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_STRATEGY_LABEL = "confluence"
_TIMEFRAME_LABEL = "M15"

# Expiry bounds: a PENDING signal lives at most 30 minutes from creation,
# or 15 minutes past the bar close that generated it — whichever is first.
_EXPIRY_BAR_WINDOW = 900
_EXPIRY_CREATION_WINDOW = 1800


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
    signals: int = 0


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

         # Signal-writing callables — only wired when the engine exposes
         # a strategy evaluator and a risk pre-checker. Stage 1 (no signal
         # generation) leaves both as None so the cycle behaves exactly
         # as before.
        read_history = getattr(engine, "read_history", None)
        evaluate_strategy = getattr(engine, "evaluate_strategy", None)
        build_order_intent = None
        risk_engine = getattr(engine, "risk", None)
        _engine_config = getattr(engine, "config", None)
        _gateway = getattr(engine, "gateway", None)
        if risk_engine is not None:
            def _resolve_volume_cap(symbol: str, volume_min: float | None) -> float | None:
                """Return the per-symbol volume cap, or None if it cannot be resolved.

                Looks up an explicit ``trading.symbols.<symbol>.volume_cap`` from
                config. When absent, falls back to ``volume_min *
                volume_cap_default_multiplier`` (default 10x). When neither the
                explicit cap nor volume_min is available, returns None — the
                caller treats None as fail-closed (no signal written).
                """
                caps = getattr(_engine_config, "volume_caps", {}) or {}
                explicit = caps.get(symbol.upper())
                if explicit is not None and explicit > 0:
                    return explicit
                if volume_min is not None and volume_min > 0:
                    mult = getattr(_engine_config, "volume_cap_default_multiplier", 10)
                    return volume_min * mult
                return None

            def build_order_intent(symbol: str, direction: str, entry_price: float,
                                   stop_loss: float, view: dict) -> tuple[float, str, list[str]]:
                intent = risk_engine.build_order_intent(
                    symbol=symbol, side=direction, volume=1.0,
                    entry_price=entry_price, stop_loss=stop_loss,
                    spread_points=None, max_spread_points=None,
                )
                decision = risk_engine.evaluate(intent, engine.account)
                if not decision.approved:
                    return 0.0, _RISK_PRECHECK_FAILED, decision.reasons

                # Build a proper SymbolSpec for LotSizer from the broker spec.
                from ui.risk_calc import SymbolSpec
                info = _gateway.symbol_info(symbol) if _gateway else None
                if info is None:
                    return 0.0, _RISK_PRECHECK_FAILED, ["spec_unavailable"]

                tick_value_loss = (getattr(info, "trade_tick_value_loss", None)
                                   or getattr(info, "trade_tick_value", None))
                volume_min = getattr(info, "volume_min", None)
                volume_cap = _resolve_volume_cap(symbol, volume_min)

                spec = SymbolSpec(
                    symbol=symbol,
                    category="forex",
                    digits=int(getattr(info, "digits", 5) or 5),
                    point=float(getattr(info, "point", 1e-5) or 1e-5),
                    tick_size=float(intent.tick_size),
                    tick_value_loss=float(tick_value_loss or 0.0),
                    contract_size=float(getattr(info, "trade_contract_size", 1.0) or 1.0),
                    volume_min=float(volume_min or 0.0),
                    volume_max=float(getattr(info, "volume_max", 0.0) or 0.0),
                    volume_step=float(getattr(info, "volume_step", 0.0) or 0.0),
                )

                from engine.sizing import LotSizer
                lot_sizer = LotSizer(spec, volume_cap=volume_cap)
                result = lot_sizer.size(entry_price, stop_loss, intent.risk_amount)
                if not result.ok:
                    reason = result.reason or "UNSIZABLE"
                    if reason.startswith("LOTS_BELOW_MINIMUM") or "below" in reason.lower():
                        reason = "Too small to size within the risk limit"
                    return 0.0, _RISK_PRECHECK_FAILED, [reason]
                return result.lots, "", []

        return self.run(
            state=engine.state,
            journal=engine.journal,
            read_market=read_market,
            mode=str(mode),
            terminal_up=terminal_up,
            writes_allowed=writes_allowed,
            read_history=read_history,
            evaluate_strategy=evaluate_strategy,
            build_order_intent=build_order_intent,
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
        read_history: Callable[[str], Any] | None = None,
        evaluate_strategy: Callable[[str, Any], Any] | None = None,
        build_order_intent: Callable[[str, str, float, float, dict],
                                     tuple[float, str, list[str]]] | None = None,
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
                if reason != "CYCLE_INTERVAL":
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
            computed = scanned = errors = deferred = skipped = signals = 0
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
                    if status == "scanned" and read_history is not None and evaluate_strategy is not None:
                        wrote = self._try_signal(
                            journal, cycle_id, symbol, view, stamp,
                            read_history, evaluate_strategy, build_order_intent,
                        )
                        if wrote is not None:
                            signals += wrote
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
                signals=signals,
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
        """Filter, then optionally compute.

        Existence precedes trade-mode, which precedes market-open, which
        precedes staleness: a symbol the broker does not list is NOT_FOUND,
        one that is listed but non-executable is TRADE_DISABLED, one that is
        closed wins over stale. A closed *and* stale symbol is MARKET_CLOSED,
        never STALE_QUOTE.
        """
        trade_mode = view.get("trade_mode")
        if trade_mode is None:
            return "skipped", _SYMBOL_NOT_FOUND, False
        if trade_mode == 0:
            return "skipped", _TRADE_DISABLED, False
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

    def _compute_decision_id(self, symbol: str, bar_close_ts: str) -> str:
        """Deterministic UUID5 so the same bar never yields two decisions."""
        return str(uuid.uuid5(
            _SIGNAL_NAMESPACE, f"{symbol}|{_STRATEGY_LABEL}|{bar_close_ts}",
        ))

    def _compute_expiry(self, bar_close_ts: str, created_iso: str) -> str:
        """Signal expiry in UTC ISO.

        ``min(bar_close_ts + 900s, signal_created_at + 1800s)``.

        No weekend clamping: a signal whose raw expiry falls on a Saturday
        or Sunday is simply expired at that time. The market is closed
        anyway, so no approval can occur. Forward-clamping to Monday would
        extend validity across the weekend, which is forbidden.

        ``bar_close_ts`` may arrive as either an ISO-8601 string or a Unix
        timestamp string (what the gateway stores from ``tick.time``).

        Clock skew guard: when the bar close is more than ``MAX_CLOCK_SKEW``
        (900 s) away from the signal-creation stamp, the two values are on
        different clocks (e.g. the broker terminal is 3 h ahead of the
        engine). Falling back to the creation bound alone keeps the expiry
        on one consistent clock and prevents an immediate expiry (bar in
        the past) or an over-long hold (bar in the far future).
        """
        from engine.equity import MAX_CLOCK_SKEW_SECONDS

        try:
            close_dt = datetime.fromisoformat(bar_close_ts)
        except ValueError:
            close_dt = datetime.fromtimestamp(
                float(bar_close_ts), tz=timezone.utc,
            )
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        created_dt = datetime.fromisoformat(created_iso)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)

        skew = abs((close_dt - created_dt).total_seconds())
        if skew > MAX_CLOCK_SKEW_SECONDS:
            # The bar timestamp is from a different clock than the creation
            # stamp (broker terminal 3 h ahead is the measured case). Use only
            # the creation bound so the expiry stays on one consistent clock.
            return (created_dt + timedelta(
                seconds=_EXPIRY_CREATION_WINDOW
            )).isoformat()

        raw_expiry = min(
            close_dt + timedelta(seconds=_EXPIRY_BAR_WINDOW),
            created_dt + timedelta(seconds=_EXPIRY_CREATION_WINDOW),
        )
        return raw_expiry.isoformat()

    def _try_signal(self, journal: Journal, cycle_id: str, symbol: str,
                    view: dict, stamp: str,
                    read_history: Callable[[str], Any],
                    evaluate_strategy: Callable[[str, Any], Any],
                    build_order_intent: Callable | None) -> int | None:
        """Attempt to read history, evaluate the strategy, and write a signal.

        Returns 1 if a signal row was written (PENDING or REJECTED), 0 if the
        symbol was skipped for insufficient history, or None if no action was
        taken (callables not wired).  Every row written is visible to the UI.
        """
        bar_ts = view.get("bar_ts")
        if bar_ts is None:
            return None
        bar_close_ts = str(bar_ts)
        decision_id = self._compute_decision_id(symbol, bar_close_ts)

        try:
            df = read_history(symbol)
        except Exception as exc:
            journal.log_event(
                "signal", f"{symbol}: history read failed: {exc}",
                severity="WARNING", source="engine",
                context={"kind": "history_read_error", "symbol": symbol,
                         "cycle_id": cycle_id, "ts": stamp},
            )
            return None

        if df is None or len(df) < _MIN_BARS_FOR_SIGNAL:
            journal.log_event(
                "signal",
                f"{symbol}: insufficient history ({len(df) if df is not None else 0} rows, "
                f"need {_MIN_BARS_FOR_SIGNAL})",
                severity="DEBUG", source="engine",
                context={"kind": _INSUFFICIENT_HISTORY, "symbol": symbol,
                         "bar_ts": bar_close_ts, "cycle_id": cycle_id, "ts": stamp},
            )
            return 0

        try:
            signal = evaluate_strategy(symbol, df)
        except Exception as exc:
            journal.log_event(
                "signal", f"{symbol}: strategy evaluate failed: {exc}",
                severity="WARNING", source="engine",
                context={"kind": "strategy_error", "symbol": symbol,
                         "cycle_id": cycle_id, "ts": stamp},
            )
            return None

        if signal is None or not getattr(signal, "is_actionable", False):
            direction = getattr(signal, "direction", None) if signal else None
            journal.log_event(
                "signal",
                f"{symbol}: strategy returned no actionable signal "
                f"(direction={direction})",
                severity="DEBUG", source="engine",
                context={"kind": _STRATEGY_NO_SIGNAL, "symbol": symbol,
                         "bar_ts": bar_close_ts, "decision_id": decision_id,
                         "cycle_id": cycle_id, "ts": stamp},
            )
            return 0

        # Strategy produced a signal — run drift check and risk pre-check.
        entry_price = float(signal.price)
        direction = signal.direction

        # Drift check
        executable_quote = self._drift_quote(view, direction)
        if executable_quote is not None and signal.price is not None:
            stop_loss = self._derive_stop(signal, df)
            if stop_loss is not None:
                drift = abs(executable_quote - entry_price)
                threshold = 0.5 * abs(entry_price - stop_loss)
                if threshold > 0 and drift > threshold:
                    self._write_signal_row(
                        journal, symbol, direction, signal, bar_close_ts,
                        stamp, decision_id, "REJECTED",
                        reject_reason=_DRIFT_BLOCKED,
                        blocks_json=self._blocks_json(signal),
                        is_test=False,
                    )
                    journal.log_lifecycle(
                        Journal.LIFECYCLE_SIGNAL_REJECTED,
                        f"{symbol}: drift blocked (drift={drift:.5f} > threshold={threshold:.5f})",
                        decision_id=decision_id, context={"kind": _DRIFT_BLOCKED},
                    )
                    return 1

        # Risk pre-check
        stop_loss = self._derive_stop(signal, df)
        if stop_loss is None:
            self._write_signal_row(
                journal, symbol, direction, signal, bar_close_ts,
                stamp, decision_id, "REJECTED",
                reject_reason="no_stop_loss",
                blocks_json=self._blocks_json(signal),
                is_test=False,
            )
            journal.log_lifecycle(
                Journal.LIFECYCLE_SIGNAL_REJECTED,
                f"{symbol}: no stop loss derived from bars",
                decision_id=decision_id, context={"kind": "no_stop_loss"},
            )
            return 1

        if build_order_intent is not None:
            volume, risk_reason, risk_reasons = build_order_intent(
                symbol, direction, entry_price, stop_loss, view,
            )
            if risk_reason:
                self._write_signal_row(
                    journal, symbol, direction, signal, bar_close_ts,
                    stamp, decision_id, "REJECTED",
                    reject_reason=risk_reason,
                    blocks_json=self._blocks_json(signal),
                    is_test=False,
                )
                journal.log_lifecycle(
                    Journal.LIFECYCLE_SIGNAL_REJECTED,
                    f"{symbol}: {risk_reason} — {'; '.join(risk_reasons)}",
                    decision_id=decision_id, context={"kind": risk_reason},
                )
                return 1

            score = getattr(signal, "confidence", 0.0)
            if score is None:
                score = 0.0
            take_profit = getattr(signal, "take_profit", None)
            expires_at = self._compute_expiry(bar_close_ts, stamp)
            self._write_signal_row(
                journal, symbol, direction, signal, bar_close_ts,
                stamp, decision_id, "PENDING",
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                volume=volume,
                expires_at=expires_at,
                decided_by="scan",
                decided_at=stamp,
                blocks_json=self._blocks_json(signal),
                is_test=False,
            )
            journal.log_lifecycle(
                Journal.LIFECYCLE_SIGNAL_GENERATED,
                f"{symbol}: PENDING signal written (decision={decision_id})",
                decision_id=decision_id, context={"kind": "signal_pending"},
            )
            return 1

        # No risk pre-check callable — write PENDING without sizing.
        expires_at = self._compute_expiry(bar_close_ts, stamp)
        self._write_signal_row(
            journal, symbol, direction, signal, bar_close_ts,
            stamp, decision_id, "PENDING",
            entry_price=entry_price,
            stop_loss=self._derive_stop(signal, df),
            take_profit=getattr(signal, "take_profit", None),
            volume=None,
            expires_at=expires_at,
            decided_by="scan",
            decided_at=stamp,
            blocks_json=self._blocks_json(signal),
            is_test=False,
        )
        journal.log_lifecycle(
            Journal.LIFECYCLE_SIGNAL_GENERATED,
            f"{symbol}: PENDING signal written (decision={decision_id})",
            decision_id=decision_id, context={"kind": "signal_pending"},
        )
        return 1

    def _drift_quote(self, view: dict, direction: str) -> float | None:
        """Return the executable quote for drift checking.

        ASK for BUY, BID for SELL — the price the engine would actually fill at.
        Returns None when the snapshot is missing the quote.
        """
        if direction == "BUY":
            return view.get("ask")
        if direction == "SELL":
            return view.get("bid")
        return None

    @staticmethod
    def _derive_stop(signal: Any, df) -> float | None:
        """Derive a stop-loss price from the bar data and signal.

        Uses the signal's own stop if it exposes one; otherwise falls back to
        the low (BUY) or high (SELL) of the just-closed bar.
        """
        stop = getattr(signal, "stop_loss", None)
        if stop is not None:
            return float(stop)
        if df is None or len(df) == 0:
            return None
        direction = getattr(signal, "direction", None)
        if direction == "BUY":
            return float(df["low"].iloc[-1])
        if direction == "SELL":
            return float(df["high"].iloc[-1])
        return None

    @staticmethod
    def _blocks_json(signal: Any) -> str:
        """Serialize the signal's block evaluations to JSON for audit."""
        import json
        blocks = getattr(signal, "blocks", []) or []
        return json.dumps([{
            "name": getattr(b, "name", str(b)),
            "agrees": getattr(b, "agrees", None),
            "detail": getattr(b, "detail", None),
        } for b in blocks])

    def _write_signal_row(self, journal: Journal, symbol: str, direction: str,
                          signal: Any, bar_ts: str, stamp: str,
                          decision_id: str, status: str, *,
                          entry_price: float | None = None,
                          stop_loss: float | None = None,
                          take_profit: float | None = None,
                          volume: float | None = None,
                          expires_at: str | None = None,
                          decided_by: str | None = None,
                          decided_at: str | None = None,
                          blocks_json: str = "{}",
                          reject_reason: str | None = None,
                          is_test: bool = False) -> None:
        """Write a single row to the v4 signals table.

        The schema CHECK requires expires_at IS NOT NULL for non-LEGACY
        rows. When the caller omits it (REJECTED rows), compute it from
        the bar close so the row is always valid.
        """
        if expires_at is None and status != "LEGACY":
            expires_at = self._compute_expiry(bar_ts, stamp)
        score = getattr(signal, "confidence", 0.0)
        if score is None:
            score = 0.0
        journal.record_signal(
            symbol=symbol,
            direction=direction,
            score=float(score),
            bar_ts=bar_ts,
            decision_id=decision_id,
            status=status,
            strategy=_STRATEGY_LABEL,
            timeframe=_TIMEFRAME_LABEL,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            volume=volume,
            expires_at=expires_at,
            decided_by=decided_by,
            decided_at=decided_at,
            blocks_json=blocks_json,
            reject_reason=reject_reason,
            is_test=is_test,
            test_tag="TEST SIGNAL" if is_test else None,
        )

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
