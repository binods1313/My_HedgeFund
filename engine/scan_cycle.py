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
from engine.risk import RiskConfigError
from engine.state import STATE_IDLE, STATE_SCANNING
from quote_clock import detect_broker_offset_seconds

# Market-closed is first on purpose. A symbol that is both closed and
# stale must be recorded as MARKET_CLOSED, never as STALE_QUOTE.
_SYMBOL_NOT_FOUND = "SYMBOL_NOT_FOUND"
_TRADE_DISABLED = "TRADE_DISABLED"
_MARKET_CLOSED = "MARKET_CLOSED"
_STALE_QUOTE = "STALE_QUOTE"
_NO_NEW_BAR = "NO_NEW_CLOSED_BAR"
_BAR_NOT_ALIGNED = "BAR_TIME_NOT_ALIGNED"
_OFFSET_UNSAFE = "BROKER_OFFSET_UNSAFE"
_BUDGET = "CYCLE_BUDGET_EXHAUSTED"
_TERMINAL = "TERMINAL_OFFLINE"
_MISMATCH = "ACCOUNT_MISMATCH"

# Signal-related constants
_MIN_BARS_FOR_SIGNAL = 206
_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
_RISK_PRECHECK_FAILED = "RISK_PRECHECK_FAILED"
_DRIFT_BLOCKED = "DRIFT_BLOCKED"
_STRATEGY_NO_SIGNAL = "STRATEGY_NO_SIGNAL"
_STOP_QUALITY = "stop_quality"
_MISSING_TICK_SPEC = "MISSING_TICK_SPEC"

# Deterministic UUID5 namespace for signal decision IDs.
_SIGNAL_NAMESPACE = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
_STRATEGY_LABEL = "confluence"
_TIMEFRAME_LABEL = "M15"

# Expiry bounds: a PENDING signal lives at most 30 minutes from creation,
# or 15 minutes past the bar close that generated it — whichever is first.
_EXPIRY_BAR_WINDOW = 900
_EXPIRY_CREATION_WINDOW = 1800
_M15_SECONDS = 900


def closed_bar_open_epoch(df: Any) -> int | None:
    """Return the last closed bar's open epoch, or None.

    The epoch is the history frame's last ``time`` value, which MetaTrader
    stores as the bar open. It is accepted only when it falls on an M15
    boundary (divisible by 900). A tick time such as 05:17:42 is refused.
    """
    if not _is_history_frame(df) or len(df) == 0 or "time" not in df.columns:
        return None
    epoch = _as_broker_epoch(df["time"].iloc[-1])
    if epoch is None or epoch % _M15_SECONDS != 0:
        return None
    return epoch


def _is_history_frame(df: Any) -> bool:
    try:
        import pandas as pd
    except ImportError:
        return False
    return isinstance(df, pd.DataFrame)


def _as_broker_epoch(raw: Any) -> int | None:
    """Integer seconds of a history ``time`` cell.

    Naive datetimes and pandas timestamps are the broker epoch decoded as
    UTC, which is how ``pandas.to_datetime(..., unit='s')`` renders the
    column. Their nanosecond value is that epoch. ``Timestamp.timestamp()``
    is not used for naive values, because it would apply the machine zone.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    value = getattr(raw, "value", None)
    if isinstance(value, int) and value > 10**14:
        # pandas Timestamp is a datetime subclass. Its nanosecond value is
        # the epoch; Timestamp.timestamp() would apply the machine zone
        # to a naive value, so it is not used here.
        if getattr(raw, "tzinfo", None) is None:
            return int(value // 1_000_000_000)
        return int(raw.timestamp())
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            raw = raw.replace(tzinfo=timezone.utc)
        return int(raw.timestamp())
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if number != number:  # NaN
        return None
    return int(round(number))


def _tick_epoch(value: Any) -> int | None:
    """A snapshot tick epoch, or None when the field is not an integer."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return None


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


def _preload_specs(journal: Journal, risk_engine: Any,
                    gateway: Any, symbols: list[str]) -> None:
     """Load tick specs for all symbols from the broker and cache them.

     Each symbol is probed individually via ``gateway.symbol_info``. A symbol
     whose spec cannot be loaded (broker returns None, attribute missing, or an
     exception) gets a visible WARNING logged and is skipped — the cycle
     continues with the remaining symbols rather than aborting.
     """
     existing = dict(getattr(risk_engine, "_specs", {}) or {})
     for symbol in symbols:
         name = str(symbol).upper()
         if name in existing:
             continue
         try:
             info = gateway.symbol_info(symbol)
         except Exception:
             journal.log_event(
                 "signal",
                 f"{symbol}: tick spec load failed (gateway error)",
                 severity="WARNING", source="engine",
                 context={"kind": _MISSING_TICK_SPEC, "symbol": name},
             )
             continue
         if info is None:
             journal.log_event(
                 "signal",
                 f"{symbol}: tick spec unavailable from broker (symbol_info None)",
                 severity="WARNING", source="engine",
                 context={"kind": _MISSING_TICK_SPEC, "symbol": name},
             )
             continue
         tick_value = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
         tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0)
         if tick_value and tick_size:
             existing[name] = {
                 "trade_tick_value": tick_value,
                 "trade_tick_size": tick_size,
             }
         else:
             journal.log_event(
                 "signal",
                 f"{symbol}: tick spec missing trade_tick_value/trade_tick_size",
                 severity="WARNING", source="engine",
                 context={"kind": _MISSING_TICK_SPEC, "symbol": name},
             )
             continue
     if hasattr(risk_engine, "set_symbol_specs"):
          risk_engine.set_symbol_specs(existing)
     else:
          journal.log_event(
               "scan",
               "risk engine lacks set_symbol_specs; skipping tick-spec cache",
               severity="DEBUG", source="engine",
               context={"kind": "spec_cache_skipped"},
          )


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
    #: Whole-hour broker offset in seconds. None detects it from fresh ticks.
    broker_offset_seconds: int | None = None
    #: Last offset this process accepted. A later jump other than ±3600s
    #: (DST) is refused until the process restarts.
    _accepted_offset: int | None = None
    #: Observational Kronos hook (callable or None). Called after a PENDING
    #: signal row is written; never gates or modifies anything.
    kronos_hook: Callable | None = None

    def run_with_engine(self, engine) -> ScanOutcome:
        """Adapt the live engine. Reads the gateway once per started cycle."""
        mode = getattr(getattr(engine, "config", None), "mode", "PAPER")
        journal = getattr(engine, "journal", None)

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
        kronos_hook = getattr(engine, "kronos_hook", None)
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

        # Preload tick specs for all scan symbols from the gateway so the
        # risk pre-check does not raise RiskConfigError for a missing spec.
        # Specs are cached on the risk engine; each symbol that cannot be
        # loaded gets a visible per-symbol error (not a swallowed cycle
        # failure) and the remaining symbols continue.
        if risk_engine is not None and _gateway is not None:
            _preload_specs(journal, risk_engine, _gateway, list(self.symbols))

        def build_order_intent(symbol: str, direction: str, entry_price: float,
                               stop_loss: float, view: dict) -> tuple[float, str, list[str]]:
            try:
                intent = risk_engine.build_order_intent(
                    symbol=symbol, side=direction, volume=1.0,
                    entry_price=entry_price, stop_loss=stop_loss,
                    spread_points=None, max_spread_points=None,
                )
            except RiskConfigError as exc:
                # A missing spec must not abort the whole scan cycle.
                # It degrades to a per-symbol risk rejection so the
                # REJECTED row is written with a visible reason.
                journal.log_event(
                    "signal",
                    f"{symbol}: {exc}",
                    severity="WARNING", source="engine",
                    context={"kind": _MISSING_TICK_SPEC, "symbol": symbol},
                )
                return 0.0, _RISK_PRECHECK_FAILED, [str(exc)]

            decision = risk_engine.evaluate(intent, engine.account)
            if not decision.approved:
                return 0.0, _RISK_PRECHECK_FAILED, \
                    list(getattr(decision, "reasons", ()) or ())

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
            kronos_hook=kronos_hook,
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
        kronos_hook: Callable | None = None,
        utc_now_epoch: float | None = None,
    ) -> ScanOutcome:
        """Attempt one cycle. Returns to IDLE if it entered SCANNING."""
        clock = monotonic or time.monotonic
        online = terminal_up or (lambda: True)
        allowed = writes_allowed or (lambda: True)
        stamp = now_iso or utc_now_iso()
        now_m = clock()
        ticked = False
        if kronos_hook is not None:
            self.kronos_hook = kronos_hook

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
            now_epoch = time.time() if utc_now_epoch is None else float(utc_now_epoch)
            offset = self._cycle_offset(views, now_epoch)
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
                signal_path = read_history is not None and evaluate_strategy is not None
                history_df = None
                bar_open_epoch = None
                try:
                    if signal_path:
                        status, why, did_compute, history_df, bar_open_epoch = (
                            self._signal_symbol(symbol, view, compute, read_history, offset)
                        )
                    else:
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
                    if (status == "scanned" and signal_path
                            and bar_open_epoch is not None and offset is not None):
                        wrote = self._try_signal(
                            journal, cycle_id, symbol, view, stamp,
                            history_df, int(bar_open_epoch), int(offset),
                            evaluate_strategy, build_order_intent,
                        )
                        if wrote is not None:
                            signals += wrote
                if signal_path:
                    stored_bar = None if bar_open_epoch is None else str(bar_open_epoch)
                else:
                    stored_bar = view.get("bar_ts")
                self._symbol_row(journal, cycle_id, symbol, status, why, stored_bar, stamp)

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

    def _eligibility(self, view: dict) -> tuple[str, str] | None:
        """Return a skip pair, or None when the symbol may be scanned.

        Existence precedes trade-mode, which precedes market-open, which
        precedes staleness: a symbol the broker does not list is NOT_FOUND,
        one that is listed but non-executable is TRADE_DISABLED, one that is
        closed wins over stale. A closed *and* stale symbol is MARKET_CLOSED,
        never STALE_QUOTE.
        """
        trade_mode = view.get("trade_mode")
        if trade_mode is None:
            return "skipped", _SYMBOL_NOT_FOUND
        if trade_mode == 0:
            return "skipped", _TRADE_DISABLED
        if not view.get("market_open", False):
            return "skipped", _MARKET_CLOSED
        if view.get("stale"):
            return "skipped", _STALE_QUOTE
        return None

    def _one_symbol(self, symbol, view, compute) -> tuple[str, str, bool]:
        """Stage 1 path: the snapshot tick time is only a change detector.

        Signal writing does not use this. A wired cycle calls
        ``_signal_symbol``, which takes the bar open from the history frame.
        """
        skip = self._eligibility(view)
        if skip is not None:
            return skip[0], skip[1], False
        bar = view.get("bar_ts")
        bar_key = "" if bar is None else str(bar)
        if not bar_key or self._last_bar.get(symbol) == bar_key:
            return "skipped", _NO_NEW_BAR, False
        if compute is not None:
            compute(symbol, view)
        self._last_bar[symbol] = bar_key
        return "scanned", "", True if compute is not None else False

    def _signal_symbol(self, symbol, view, compute, read_history, offset):
        """New-bar detection from the closed bar's open time.

        Returns status, reason, did_compute, history frame, bar-open epoch.
        The snapshot tick time is not a bar identity and is not stored.
        An unaligned open is refused. An unsafe broker offset does not
        consume the bar, so the next cycle can try again.
        """
        skip = self._eligibility(view)
        if skip is not None:
            return skip[0], skip[1], False, None, None
        try:
            df = read_history(symbol)
        except Exception as exc:
            return "error", f"{type(exc).__name__}: {exc}", False, None, None
        epoch = closed_bar_open_epoch(df)
        if epoch is None:
            if not _is_history_frame(df) or len(df) == 0:
                return "skipped", _NO_NEW_BAR, False, None, None
            return "skipped", _BAR_NOT_ALIGNED, False, df, None
        if self._last_bar.get(symbol) == str(epoch):
            return "skipped", _NO_NEW_BAR, False, df, epoch
        if offset is None:
            return "skipped", _OFFSET_UNSAFE, False, df, epoch
        if compute is not None:
            compute(symbol, view)
        self._last_bar[symbol] = str(epoch)
        return "scanned", "", compute is not None, df, epoch

    def _cycle_offset(self, views: Any, utc_now: float) -> int | None:
        """Broker offset for this cycle, in seconds, or None to fail closed.

        An explicit ``broker_offset_seconds`` is used as given (tests and a
        known override). Otherwise the offset is detected from fresh tick
        epochs on the snapshot. A detected value is kept only when it is
        the first this process has seen, the same as the last, or exactly
        one hour away from the last.
        """
        if self.broker_offset_seconds is not None:
            return int(self.broker_offset_seconds)
        epochs: list[int] = []
        if isinstance(views, dict):
            for view in views.values():
                if isinstance(view, dict):
                    epoch = _tick_epoch(view.get("bar_ts"))
                    if epoch is not None:
                        epochs.append(epoch)
        detected = detect_broker_offset_seconds(epochs, utc_now)
        return self._accept_offset(detected)

    def _accept_offset(self, detected: int | None) -> int | None:
        """Accept a detected offset, or None when the jump is not DST."""
        if detected is None:
            return None
        previous = self._accepted_offset
        if previous is None or detected == previous or abs(detected - previous) == 3600:
            self._accepted_offset = int(detected)
            return int(detected)
        return None

    def _compute_decision_id(self, symbol: str, bar_open_epoch: int) -> str:
        """Deterministic UUID5 so the same closed bar never yields two decisions.

        Inputs, in order, joined by ``|``: symbol, strategy label
        ``confluence``, and the closed bar's open epoch (integer seconds
        from the history frame).
        """
        return str(uuid.uuid5(
            _SIGNAL_NAMESPACE,
            f"{symbol}|{_STRATEGY_LABEL}|{int(bar_open_epoch)}",
        ))

    def _expiry_from_bar_open(self, bar_open_epoch: int, created_iso: str,
                              offset_seconds: int) -> str:
        """Convert a broker bar open to UTC, then apply the expiry formula.

        ``offset_seconds`` is broker-server minus UTC. Subtracting it from
        the history epoch puts the bar open on the UTC clock. The bar close
        is 15 minutes after that open. Expiry is
        ``min(bar_close_utc + 900s, created_utc + 1800s)``.
        """
        open_utc = datetime.fromtimestamp(
            int(bar_open_epoch) - int(offset_seconds), tz=timezone.utc,
        )
        close_utc = open_utc + timedelta(minutes=15)
        return self._compute_expiry(close_utc.isoformat(), created_iso)

    def _compute_expiry(self, bar_close_utc: str, created_iso: str) -> str:
        """Signal expiry in UTC ISO.

        ``min(bar_close_utc + 900s, created_utc + 1800s)``.

        Both arguments are UTC. Broker epochs are converted by
        ``_expiry_from_bar_open`` before they reach this function. There is
        no skew fallback and no weekend clamp: a Friday close that expires
        on Friday stays on Friday.
        """
        try:
            close_dt = datetime.fromisoformat(bar_close_utc)
        except ValueError:
            close_dt = datetime.fromtimestamp(
                float(bar_close_utc), tz=timezone.utc,
            )
        if close_dt.tzinfo is None:
            close_dt = close_dt.replace(tzinfo=timezone.utc)
        created_dt = datetime.fromisoformat(created_iso)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)
        raw_expiry = min(
            close_dt + timedelta(seconds=_EXPIRY_BAR_WINDOW),
            created_dt + timedelta(seconds=_EXPIRY_CREATION_WINDOW),
        )
        return raw_expiry.isoformat()

    def _try_signal(self, journal: Journal, cycle_id: str, symbol: str,
                    view: dict, stamp: str,
                    df: Any, bar_open_epoch: int, offset_seconds: int,
                    evaluate_strategy: Callable[[str, Any], Any],
                    build_order_intent: Callable | None) -> int | None:
        """Evaluate one closed bar and write at most one signal row.

        ``bar_open_epoch`` is the history frame's last open, already checked
        to be M15-aligned. The decision id, the stored ``bar_ts``, and the
        idempotency key all use that integer. This method does not send an
        order.

        Returns 1 if a signal row was written (PENDING or REJECTED), 0 if the
        symbol was skipped, or None if evaluation failed.
        """
        decision_id = self._compute_decision_id(symbol, bar_open_epoch)
        decision_inputs = f"{symbol}|{_STRATEGY_LABEL}|{int(bar_open_epoch)}"
        existing = journal.query(
            "SELECT id FROM signals WHERE symbol = ? AND strategy = ? "
            "AND bar_ts = ? AND is_test = 0",
            (symbol, _STRATEGY_LABEL, int(bar_open_epoch)),
        )
        if existing:
            return 0

        bar_ts = int(bar_open_epoch)
        expires_at = self._expiry_from_bar_open(
            bar_ts, stamp, int(offset_seconds))

        if df is None or len(df) < _MIN_BARS_FOR_SIGNAL:
            journal.log_event(
                "signal",
                f"{symbol}: insufficient history ({len(df) if df is not None else 0} rows, "
                f"need {_MIN_BARS_FOR_SIGNAL})",
                severity="DEBUG", source="engine",
                context={"kind": _INSUFFICIENT_HISTORY, "symbol": symbol,
                         "bar_ts": bar_ts, "cycle_id": cycle_id, "ts": stamp},
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

        self._log_evaluation(
            journal, symbol, signal, bar_ts, decision_id, decision_inputs,
            cycle_id, stamp,
        )

        if signal is None or not getattr(signal, "is_actionable", False):
            direction = getattr(signal, "direction", None) if signal else None
            journal.log_event(
                "signal",
                f"{symbol}: strategy returned no actionable signal "
                f"(direction={direction})",
                severity="DEBUG", source="engine",
                context={"kind": _STRATEGY_NO_SIGNAL, "symbol": symbol,
                         "bar_ts": bar_ts, "decision_id": decision_id,
                         "cycle_id": cycle_id, "ts": stamp},
            )
            return 0

        if getattr(signal, "stop_loss", None) is None:
            from engine.strategy import attach_trade_levels, broker_stops_from_view
            attach_trade_levels(signal, df, broker_stops_from_view(view))
        if getattr(signal, "stop_loss", None) is None:
            reason = getattr(signal, "reject_reason", None) or _STOP_QUALITY
            self._write_signal_row(
                journal, symbol, signal.direction, signal, bar_ts,
                stamp, decision_id, "REJECTED",
                reject_reason=reason,
                blocks_json=self._blocks_json(signal),
                expires_at=expires_at,
                is_test=False,
            )
            journal.log_lifecycle(
                Journal.LIFECYCLE_SIGNAL_REJECTED,
                f"{symbol}: {reason}",
                decision_id=decision_id, context={"kind": reason},
            )
            return 1

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
                        journal, symbol, direction, signal, bar_ts,
                        stamp, decision_id, "REJECTED",
                        reject_reason=_DRIFT_BLOCKED,
                        blocks_json=self._blocks_json(signal),
                        expires_at=expires_at,
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
                journal, symbol, direction, signal, bar_ts,
                stamp, decision_id, "REJECTED",
                reject_reason="no_stop_loss",
                blocks_json=self._blocks_json(signal),
                expires_at=expires_at,
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
                    journal, symbol, direction, signal, bar_ts,
                    stamp, decision_id, "REJECTED",
                    reject_reason=risk_reason,
                    blocks_json=self._blocks_json(signal),
                    expires_at=expires_at,
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
            self._write_signal_row(
                journal, symbol, direction, signal, bar_ts,
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
            self._maybe_kronos(journal, symbol, signal, decision_id, bar_ts, stamp, df)
            return 1

        # No risk pre-check callable — write PENDING without sizing.
        self._write_signal_row(
            journal, symbol, direction, signal, bar_ts,
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
        self._maybe_kronos(journal, symbol, signal, decision_id, bar_ts, stamp, df)
        return 1

    def _maybe_kronos(self, journal, symbol, signal, decision_id, bar_ts,
                      stamp, df) -> None:
        """Run the observational Kronos hook after a PENDING write.

        Strictly fail-open: any hook error is journaled as `kronos_error`
        and never propagates to the scan cycle. Returns None always.
        """
        hook = self.kronos_hook
        if hook is None:
            return
        try:
            hook(
                symbol=symbol,
                direction=getattr(signal, "direction", None),
                df=df,
                decision_id=decision_id,
                bar_ts=bar_ts,
                stamp=stamp,
            )
        except Exception as exc:  # noqa: BLE001 - fail-open
            try:
                journal.log_event(
                    "kronos",
                    f"{symbol}: hook raised: {exc}",
                    severity="WARNING", source="engine",
                    context={"kind": "kronos_error", "symbol": symbol,
                             "ts": stamp},
                )
            except Exception:  # noqa: BLE001
                pass

    def _log_evaluation(self, journal, symbol, signal, bar_open_epoch,
                        decision_id, decision_inputs, cycle_id, stamp) -> None:
        """Record one evaluate() result: five blocks, counts, decision."""
        blocks = list(getattr(signal, "blocks", []) or []) if signal is not None else []
        agree = sum(1 for block in blocks if getattr(block, "agrees", False))
        payload = [{
            "name": getattr(block, "name", str(block)),
            "agrees": bool(getattr(block, "agrees", False)),
            "detail": getattr(block, "detail", None),
        } for block in blocks]
        decision = getattr(signal, "direction", None) if signal is not None else None
        journal.log_event(
            "signal",
            f"{symbol}: evaluate decision={decision or 'NONE'} "
            f"agree={agree} disagree={len(blocks) - agree}",
            severity="INFO", source="engine",
            context={
                "kind": "strategy_evaluation",
                "symbol": symbol,
                "decision": decision,
                "agree": agree,
                "disagree": len(blocks) - agree,
                "blocks": payload,
                "bar_open_epoch": int(bar_open_epoch),
                "decision_id": decision_id,
                "decision_id_inputs": decision_inputs,
                "cycle_id": cycle_id,
                "ts": stamp,
            },
        )

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
            expires_at = self._expiry_from_bar_open(int(bar_ts), stamp, 0)
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
