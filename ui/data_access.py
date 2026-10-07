from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from contextlib import closing

import pandas as pd

import data_engine
from execution import close_position as broker_close_position
from execution import get_open_positions
from data_engine import get_account_telemetry, initialize_mt5

# Seam: tests patch initialize_mt5 on this module, and _initialize_mt5
# delegates to it at call time so the patch is respected.
def _initialize_mt5() -> bool:
    return initialize_mt5()
from metrics import compute_open_risk, net_open_pnl

from engine.config import MODE_LIVE, load_engine_config
from engine.equity import EquityTracker
from engine.executor import ExecutionRejected, RiskApproval, build_executor
from engine.guard import make_live_guard
from engine.journal import STATE_EMERGENCY_STOP, STATE_PAUSED, Journal
from engine.risk import OrderIntent, RiskEngine, RiskLimits

LOG = logging.getLogger(__name__)

#: The engine-state names the journal uses, keyed by the names `_read_engine_status`
#: reports. Only these two block: an engine that is merely offline is not an
#: engine that has halted, and the tab must stay usable while it is not running.
_ENGINE_STATE_MAP = {
    "paused": STATE_PAUSED,
    "halted": STATE_EMERGENCY_STOP,
}

#: Bars pulled to re-derive the swing anchor for a hand-entered order. Matches
#: the harness's lookback headroom so a manual anchor is comparable to an
#: automated one.
_ANCHOR_BARS = 100


@dataclass(frozen=True)
class Result:
    ok: bool
    code: str
    message: str
    ticket: int | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    connected: bool
    mode: str
    engine_state: str
    heartbeat_age_s: float | None
    currency: str
    balance: float | None
    equity: float | None
    margin_used: float | None
    free_margin: float | None
    margin_level: float | None
    open_pnl: float | None
    open_risk: float | None
    fetched_at: datetime


POSITION_COLUMNS = (
    "ticket", "symbol", "side", "lots", "entry", "current", "sl", "tp",
    "pnl", "risk_usd", "opened_at",
)


def _degraded_snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        connected=False,
        mode="PAPER",
        engine_state="offline",
        heartbeat_age_s=None,
        currency="",
        balance=None,
        equity=None,
        margin_used=None,
        free_margin=None,
        margin_level=None,
        open_pnl=None,
        open_risk=None,
        fetched_at=datetime.now(timezone.utc),
    )


def _read_engine_status() -> tuple[str, float | None, str | None]:
    """Read the engine-owned state and heartbeat without opening a writer."""
    from engine.journal import resolve_db_path
    from engine.state import OFFLINE_AFTER_SECONDS

    db_path = Path(resolve_db_path())
    if not db_path.exists():
        return "offline", None, None

    with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.execute("PRAGMA query_only = ON")
        state_row = conn.execute(
            "SELECT state, mode FROM engine_state WHERE id = 1"
        ).fetchone()
        heartbeat_row = conn.execute(
            "SELECT ts, connected FROM heartbeats ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if not state_row:
        return "offline", None, None
    state = str(state_row[0] or "OFF").upper()
    mode = str(state_row[1] or "").upper() or None
    if not heartbeat_row:
        return "offline", None, mode
    timestamp = datetime.fromisoformat(str(heartbeat_row[0]))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
    if age > OFFLINE_AFTER_SECONDS or not bool(heartbeat_row[1]):
        return "offline", age, mode

    mapped_state = {
        "OFF": "offline",
        "IDLE": "running",
        "PAUSED": "paused",
        "EMERGENCY_STOP": "halted",
    }.get(state, state.lower())
    return mapped_state, age, mode


def _broker_mode() -> str:
    account = data_engine.mt5.account_info()
    if account is None:
        return "PAPER"
    trade_mode = getattr(account, "trade_mode", None)
    if trade_mode == getattr(data_engine.mt5, "ACCOUNT_TRADE_MODE_REAL", object()):
        return "LIVE"
    return "PAPER"


def get_symbol_spec(asset: str, category: str):
    """Build a pure risk spec from the broker's live symbol specification."""
    try:
        info = data_engine.get_symbol_info_safe(asset)
        if not info:
            return None
        from ui.risk_calc import SymbolSpec

        tick_value_loss = _float_or_none(info.get("trade_tick_value_loss"))
        if tick_value_loss is None:
            tick_value_loss = _float_or_none(info.get("trade_tick_value"))
        required = {
            "digits": info.get("digits"),
            "point": _float_or_none(info.get("point")),
            "tick_size": _float_or_none(info.get("trade_tick_size")),
            "tick_value_loss": tick_value_loss,
            "contract_size": _float_or_none(info.get("trade_contract_size")),
            "volume_min": _float_or_none(info.get("volume_min")),
            "volume_max": _float_or_none(info.get("volume_max")),
            "volume_step": _float_or_none(info.get("volume_step")),
        }
        if any(value is None for value in required.values()):
            return None
        return SymbolSpec(
            symbol=asset,
            category=category.lower(),
            digits=int(required["digits"]),
            point=required["point"],
            tick_size=required["tick_size"],
            tick_value_loss=required["tick_value_loss"],
            contract_size=required["contract_size"],
            volume_min=required["volume_min"],
            volume_max=required["volume_max"],
            volume_step=required["volume_step"],
            stops_level_points=int(info.get("trade_stops_level") or 0),
        )
    except Exception as exc:
        LOG.warning("get_symbol_spec failed for %s: %s", asset, exc)
        return None


def get_required_margin(asset: str, direction: str, lots: float) -> float | None:
    """Ask MT5 for projected margin; unknown results remain unknown."""
    try:
        if not initialize_mt5():
            return None
        side = str(direction).upper()
        if side not in {"BUY", "SELL"}:
            return None
        mt5 = data_engine.mt5
        tick = mt5.symbol_info_tick(asset)
        if tick is None:
            return None
        is_buy = side == "BUY"
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = tick.ask if is_buy else tick.bid
        return _float_or_none(mt5.order_calc_margin(order_type, asset, float(lots), price))
    except Exception as exc:
        LOG.warning("get_required_margin failed for %s: %s", asset, exc)
        return None


def _read_journal_frame(query: str, columns: tuple[str, ...], limit: int) -> pd.DataFrame:
    from engine.journal import resolve_db_path

    try:
        db_path = Path(resolve_db_path())
        if not db_path.exists():
            return pd.DataFrame(columns=columns)
        row_limit = max(1, min(int(limit), 1000))
        with closing(sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(query, (row_limit,)).fetchall()
        return pd.DataFrame([dict(row) for row in rows], columns=columns)
    except Exception as exc:
        LOG.warning("read-only journal query failed: %s", exc)
        return pd.DataFrame(columns=columns)


def get_equity_history(limit: int = 200) -> pd.DataFrame:
    """Return recent engine-owned equity snapshots; never creates a journal."""
    return _read_journal_frame(
        "SELECT ts, equity FROM equity_snapshots ORDER BY id DESC LIMIT ?",
        ("ts", "equity"),
        limit,
    ).iloc[::-1].reset_index(drop=True)


def get_recent_events(limit: int = 100) -> pd.DataFrame:
    """Return recent read-only engine events for the Journal view."""
    return _read_journal_frame(
        "SELECT id, ts, category, severity, source, message FROM events ORDER BY id DESC LIMIT ?",
        ("id", "ts", "category", "severity", "source", "message"),
        limit,
    )


def get_active_positions() -> pd.DataFrame:
    """Return the fixed schema the UI expects, even when the broker is offline."""
    try:
        raw = get_open_positions() if initialize_mt5() else []
        symbol_infos = {}
        for pos in raw:
            symbol = pos.get("symbol")
            if symbol:
                info = data_engine.get_symbol_info_safe(symbol)
                if info:
                    symbol_infos[symbol] = info
        risk_infos = {
            symbol: {
                **info,
                "trade_tick_value": info.get("trade_tick_value_loss") or info.get("trade_tick_value"),
            }
            for symbol, info in symbol_infos.items()
        }
        summary = compute_open_risk(raw, risk_infos, 0.0)
        risks_by_ticket = {risk.ticket: risk for risk in summary.positions}

        rows: list[dict[str, Any]] = []
        for pos in raw:
            risk = risks_by_ticket.get(int(pos.get("ticket") or 0))
            opened_at = pos.get("time")
            if isinstance(opened_at, datetime):
                opened_at = opened_at.astimezone(timezone.utc).isoformat()
            rows.append({
                "ticket": pos.get("ticket"),
                "symbol": pos.get("symbol", ""),
                "side": pos.get("type", ""),
                "lots": pos.get("volume"),
                "entry": pos.get("price_open"),
                "current": pos.get("price_current"),
                "sl": pos.get("sl"),
                "tp": pos.get("tp"),
                "pnl": pos.get("profit"),
                "risk_usd": math.inf if risk is not None and not risk.protected else risk.risk_amount if risk else None,
                "opened_at": opened_at,
            })
        return pd.DataFrame(rows, columns=POSITION_COLUMNS)
    except Exception as exc:
        LOG.warning("get_active_positions failed; returning empty schema: %s", exc)
        return pd.DataFrame(columns=POSITION_COLUMNS)


def get_account_snapshot() -> AccountSnapshot:
    """Read the broker snapshot once and degrade cleanly on any failure."""
    try:
        connected = bool(_initialize_mt5())
        telemetry = get_account_telemetry() if connected else {}
        if not connected or not telemetry.get("connected"):
            return _degraded_snapshot()

        positions = get_open_positions()
        currency = str(telemetry.get("currency") or "")
        equity = _float_or_none(telemetry.get("equity"))
        margin_used = _float_or_none(telemetry.get("margin"))
        margin_level = None
        if margin_used is not None and margin_used > 0 and equity is not None:
            margin_level = equity / margin_used * 100.0

        symbol_infos = {}
        for pos in positions:
            symbol = pos.get("symbol")
            if symbol:
                symbol_infos[symbol] = data_engine.get_symbol_info_safe(symbol) or {}
        risk_infos = {
            symbol: {
                **info,
                "trade_tick_value": info.get("trade_tick_value_loss") or info.get("trade_tick_value"),
            }
            for symbol, info in symbol_infos.items()
        }
        risk = compute_open_risk(positions, risk_infos, equity or 0.0, currency)
        open_risk = math.inf if risk.has_unprotected else risk.total_risk_amount
        engine_state, heartbeat_age_s, engine_mode = _read_engine_status()

        return AccountSnapshot(
            connected=True,
            mode=engine_mode if engine_mode in {"LIVE", "PAPER"} else _broker_mode(),
            engine_state=engine_state,
            heartbeat_age_s=heartbeat_age_s,
            currency=currency,
            balance=_float_or_none(telemetry.get("balance")),
            equity=equity,
            margin_used=margin_used,
            free_margin=_float_or_none(telemetry.get("margin_free")),
            margin_level=margin_level,
            open_pnl=net_open_pnl(positions),
            open_risk=open_risk,
            fetched_at=datetime.now(timezone.utc),
        )
    except Exception as exc:
        LOG.warning("get_account_snapshot failed; returning degraded snapshot: %s", exc)
        return _degraded_snapshot()


def fetch_live_broker_symbols(category: str | None = None) -> list[str]:
    """Fetch tradeable symbols from the MT5 broker, with fallback to static whitelist.

    Queries `data_engine.mt5.symbols_get()` and filters out disabled or invisible
    instruments by validating properties: `symbol_info.trade_mode != mt5.SYMBOL_TRADE_MODE_DISABLED`
    and `symbol_info.visible == True`.
    If a category is requested, maps symbol properties (like path or currency leg
    characteristics) into their respective sector definitions dynamically.
    On broker failure or exception, falls back seamlessly to reading the static
    whitelist values via `config_loader.py` and logs a warning.
    """
    try:
        if not data_engine.initialize_mt5():
            raise RuntimeError("MT5 not initialized")

        all_symbols = data_engine.mt5.symbols_get()
        if all_symbols is None:
            return []

        active: list[str] = []
        for sym in all_symbols:
            info = data_engine.get_symbol_info_safe(sym.name)
            if info is None:
                continue
            # Filter out disabled or invisible instruments
            if getattr(info, "trade_mode", None) == data_engine.mt5.SYMBOL_TRADE_MODE_DISABLED:
                continue
            if not getattr(info, "visible", False):
                continue
            active.append(sym.name)

        # Category mapping - dynamically map symbol properties to sector definitions
        if category:
            categorized: list[str] = []
            for symbol in active:
                info = data_engine.get_symbol_info_safe(symbol)
                if info is None:
                    continue
                # Map based on symbol characteristics
                path = getattr(info, "path", "").lower()
                # Forex typically has currency pairs; stocks have different paths
                if any(currency in path for currency in ["forex", "majors", "minors"]):
                    if category.lower() in {"forex", "fx"}:
                        categorized.append(symbol)
                elif any(crypto in path for crypto in ["crypto", "coins"]):
                    if category.lower() in {"crypto", "digital"}:
                        categorized.append(symbol)
                else:
                    # Default: include if no specific filter mismatch
                    if not category.lower() or category.lower() == "all":
                        categorized.append(symbol)
            return categorized

        return active

    except Exception as exc:
        LOG.warning("fetch_live_broker_symbols failed: %s", exc)
        # Fallback to static whitelist via config_loader
        try:
            from execution import config_loader
            whitelist = config_loader.load_whitelist()
            return whitelist.get(category, whitelist.get("all", []))
        except Exception:
            return []


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


class _Tick:
    """Attribute-access view over a quote dict.

    `PaperExecutor._quote` reads `tick.bid` / `tick.ask`, while
    `data_engine.get_symbol_live_quote` returns a dict. Handing the dict
    straight to the executor raises AttributeError inside `_quote`, so the two
    shapes are bridged here rather than by changing either side's contract.
    """

    __slots__ = ("bid", "ask")

    def __init__(self, bid: float, ask: float):
        self.bid = bid
        self.ask = ask


def _live_tick(asset: str) -> _Tick | None:
    """A validated live quote, or None when the feed cannot be trusted."""
    quote = data_engine.get_symbol_live_quote(asset)
    if not quote or not quote.get("valid"):
        return None
    bid = _float_or_none(quote.get("bid"))
    ask = _float_or_none(quote.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask <= 0:
        return None
    return _Tick(bid, ask)


def _anchor_bars(asset: str):
    """A fresh candle snapshot for the guard's invalidation re-check.

    Deliberately returns the frame as-is, including an empty one: the guard
    treats an empty frame as "no candles" and fails the order closed, which is
    the behaviour we want from a feed that has gone quiet.
    """
    try:
        return data_engine.get_technical_dataframe(
            asset, data_engine.TIMEFRAME_M15, count=_ANCHOR_BARS)
    except Exception as exc:   # noqa: BLE001 - the guard fails closed on None
        LOG.warning("anchor snapshot failed for %s: %s", asset, exc)
        return None


def _highest_journal_ticket(journal: Journal) -> int:
    """Highest ticket already in the journal, or 0 when it is empty.

    `PaperExecutor._next_ticket` is a per-instance counter starting at
    9_000_000. A second executor against the same database would mint the same
    tickets and collide on the `trades` UNIQUE and `positions` primary key, so
    the counter is seeded past whatever is already stored.
    """
    try:
        row = journal.query_one("SELECT MAX(ticket) AS highest FROM trades")
    except Exception as exc:   # noqa: BLE001
        LOG.warning("could not read the highest journal ticket: %s", exc)
        return 0
    if not row or row["highest"] is None:
        return 0
    return int(row["highest"])


def submit_order(asset: str, direction: str, lots: float,
                 sl: float | None = None, tp: float | None = None,
                 *, leg: Any = None) -> Result:
    """Submit through the institutional Executor, never the broker directly.

    Every manual order from the Exchange tab now takes the same path an
    automated one does: the RiskEngine approves it with a real `RiskApproval`,
    a `LiveGuard` re-checks the 100% swing anchor at the instant of dispatch,
    and the fill is journaled. The legacy `execution.execute_trade` shortcut is
    no longer reachable from this module.

    `leg` is the structural anchor the intent was built on, when the caller has
    one (the AI Signals path does). Without it the anchor is re-derived from a
    fresh snapshot, so a hand-entered order is checked against the structure
    that exists now rather than skipped.

    The `Result` contract is unchanged, so call sites do not have to care.
    """
    side = str(direction or "").upper()
    if side not in {"BUY", "SELL"}:
        return Result(False, "BAD_DIRECTION",
                      f"direction must be BUY or SELL, got {direction!r}")

    volume = _float_or_none(lots)
    if volume is None or volume <= 0:
        return Result(False, "BAD_VOLUME", f"volume must be > 0, got {lots!r}")

    try:
        config = load_engine_config()
    except Exception as exc:
        LOG.warning("submit_order: engine config unavailable: %s", exc)
        return Result(False, "CONFIG_ERROR",
                      "Engine configuration could not be loaded; see application log")

    if config.mode == MODE_LIVE:
        # Refused before anything is constructed. Building a LiveExecutor here
        # would open a second MT5 session inside Streamlit, and live trading is
        # a CLI-only operation by design.
        return Result(False, "LIVE_UI_DISABLED",
                      "Live submission is disabled from the dashboard. "
                      "Run the engine CLI to trade a live account.")

    snapshot = get_account_snapshot()
    if not snapshot.connected:
        return Result(False, "BROKER_OFFLINE",
                      "Broker is not connected; order not submitted.")

    info = data_engine.get_symbol_info_safe(asset)
    if not info:
        return Result(False, "SYMBOL_UNAVAILABLE",
                      f"No broker specification for {asset}; order not submitted.")

    point = _float_or_none(info.get("point"))
    digits = info.get("digits")
    tick_value = _float_or_none(info.get("trade_tick_value_loss"))
    if tick_value is None:
        tick_value = _float_or_none(info.get("trade_tick_value"))
    tick_size = _float_or_none(info.get("trade_tick_size"))

    if not point or point <= 0 or digits is None:
        return Result(False, "SYMBOL_UNAVAILABLE",
                      f"{asset}: the broker supplied no usable point/digits spec.")
    if not tick_value or not tick_size:
        return Result(False, "SYMBOL_UNAVAILABLE",
                      f"{asset}: the broker supplied no usable tick value/size, "
                      f"so the risk cannot be measured and the order is refused.")

    tick = _live_tick(asset)
    if tick is None:
        return Result(False, "NO_QUOTE",
                      f"No valid live quote for {asset}; order not submitted.")
    price = tick.ask if side == "BUY" else tick.bid

    # -- stops: pips in, absolute prices out ----------------------------
    # The ×10 point convention is the legacy path's, preserved exactly so
    # `ui.components.trading.execution_stop_units` and every caller's
    # arithmetic remain correct. Changing it here would silently rescale
    # every stop the UI sends.
    sl_units = _float_or_none(sl)
    if sl_units is None or sl_units <= 0:
        return Result(False, "SL_REQUIRED", "Every order requires a stop loss.")

    offset = sl_units * point * 10
    sl_price = round(price - offset if side == "BUY" else price + offset,
                     int(digits))
    if sl_price <= 0:
        return Result(False, "SL_REQUIRED",
                      f"{asset}: a {sl_units:g} stop from {price} lands at "
                      f"{sl_price}, which is not a valid price.")

    tp_price = None
    tp_units = _float_or_none(tp)
    if tp_units and tp_units > 0:
        tp_offset = tp_units * point * 10
        tp_price = round(price + tp_offset if side == "BUY" else price - tp_offset,
                         int(digits))
        if tp_price <= 0:
            tp_price = None

    journal = Journal(config.db_path)
    try:
        # -- account baselines ------------------------------------------
        # The journal supplies the day-open and peak the broker cannot; the
        # live snapshot supplies equity, because risk percentage has to be
        # measured against what the account is worth now, not against a peak
        # recorded earlier. A fresh database has no baselines yet, so the
        # snapshot stands in rather than bricking the tab.
        baselines = EquityTracker(journal).current()
        equity = _float_or_none(snapshot.equity)
        if equity is None and baselines is not None:
            equity = baselines.peak_equity
        if not equity or equity <= 0:
            return Result(False, "NO_EQUITY",
                          "No usable equity figure; the risk gate cannot be "
                          "evaluated, so the order is refused.")

        start_of_day = (baselines.start_of_day_equity if baselines else None) or equity
        peak = (baselines.peak_equity if baselines else None) or equity

        # -- risk gate ---------------------------------------------------
        # A pydantic RiskLimits (engine.config) is not the frozen dataclass the
        # engine consumes, so the fields are mapped explicitly, exactly as
        # engine.dryrun_cli does.
        limits = RiskLimits(
            risk_per_trade_pct=config.risk.risk_per_trade_pct,
            daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
            max_drawdown_pct=config.risk.max_drawdown_pct,
            max_open_trades=config.risk.max_open_trades,
            max_total_exposure_pct=config.risk.max_total_exposure_pct,
        )
        risk = RiskEngine(journal=journal, limits=limits)
        risk.set_magic(config.engine.magic_number)
        risk.set_symbol_specs({asset: {"trade_tick_value": tick_value,
                                       "trade_tick_size": tick_size}})

        positions = journal.positions()
        account = risk.account_state_from_positions(
            equity=equity, start_of_day_equity=start_of_day, peak_equity=peak,
            open_positions=positions,
        )

        intent = OrderIntent.from_symbol_spec(
            symbol=asset, side=side, volume=volume, entry_price=price,
            stop_loss=sl_price, tick_value=tick_value, tick_size=tick_size,
        )
        decision = risk.evaluate(intent, account)
        approval = RiskApproval.from_decision(decision, asset)
        if not approval.approved:
            detail = "; ".join(approval.reasons) or "the risk gate approved no reason"
            return Result(False, "RISK_REJECTED", detail)

        # -- structural guard --------------------------------------------
        # One guard for both channels. The price is re-read here, at the
        # instant of dispatch, rather than reused from the entry quote above:
        # the question is whether the anchor is broken *now*.
        def _entry_price(symbol: str) -> float | None:
            current = _live_tick(symbol)
            if current is None:
                return None
            return current.ask if side == "BUY" else current.bid

        guard = make_live_guard(side=side, leg=leg,
                                bars_provider=_anchor_bars,
                                price_provider=_entry_price)

        executor = build_executor(
            config.mode, journal=journal,
            state=_ENGINE_STATE_MAP.get(snapshot.engine_state),
            tick_provider=_live_tick, live_guard=guard,
        )
        # Seed the ticket counter past anything already stored, so two
        # processes sharing this database cannot mint the same ticket.
        executor._next_ticket = max(9_000_000, _highest_journal_ticket(journal))

        request = executor.prepare_order(asset, side, volume, sl_price, tp_price)
        try:
            result = executor.submit(request, approval)
        except ExecutionRejected as exc:
            # The verbatim engine reason, so the console banner shows the
            # engine's own words rather than a paraphrase.
            return Result(False, "EXECUTION_REJECTED", str(exc))
        except Exception as exc:   # noqa: BLE001
            LOG.warning("submit_order: executor raised: %s", exc)
            return Result(False, "EXECUTION_ERROR",
                          "The executor failed; see application log")
    finally:
        journal.close()

    return Result(bool(result.ok), str(result.status),
                  str(result.message or result.status), result.ticket)


def close_position(ticket: int) -> Result:
    """Close a position through the existing engine path and surface any refusal cleanly."""
    try:
        payload = broker_close_position(int(ticket))
    except Exception as exc:
        LOG.warning("close_position failed for %s: %s", ticket, exc)
        return Result(False, "BROKER_ERROR", "Close request failed; see application log", int(ticket))

    if not isinstance(payload, dict):
        LOG.warning("close_position returned an unexpected result type: %s", type(payload).__name__)
        return Result(False, "BROKER_ERROR", "Broker returned an invalid close result", int(ticket))
    if not payload.get("success", False):
        return Result(False, str(payload.get("status", "CLOSE_FAILED")), str(payload.get("message", "Close failed")), int(ticket))
    return Result(True, "OK", str(payload.get("message", "Position closed")), int(ticket))
