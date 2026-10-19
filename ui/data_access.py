from __future__ import annotations

import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
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
#: PaperExecutor mints tickets from this floor. A broker ticket is never
#: in this sequence, and a paper ticket is never sent to MT5.
_PAPER_TICKET_FLOOR = 9_000_000


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
    heartbeat_age_s: float | None = None
    currency: str = ""
    balance: float | None = None
    equity: float | None = None
    margin_used: float | None = None
    free_margin: float | None = None
    margin_level: float | None = None
    open_pnl: float | None = None
    open_risk: float | None = None
    #: Is the engine process alive? Derived from heartbeat freshness and its
    #: connected flag, and kept separate from `engine_state`, which is the
    #: *trading* state the readiness gate consumes. An engine heartbeating with
    #: trading switched OFF is online; showing "OFFLINE" for it is what made a
    #: healthy engine look dead on the dashboard.
    engine_online: bool = False
    fetched_at: datetime | None = None


POSITION_COLUMNS = (
    "ticket", "symbol", "side", "lots", "entry", "current", "sl", "tp",
    "pnl", "risk_usd", "opened_at", "magic", "managed",
)


def _is_our_magic(magic: Any) -> bool:
    """True when a position carries our engine's magic number.

    A position with a foreign or unreadable magic is not ours, so the default
    for anything unclear is `False` -- the safe direction, because it withholds
    controls rather than granting them. This never raises on a non-numeric
    value: it returns False instead.
    """
    try:
        from engine.config import load_engine_config
        ours = int(load_engine_config().engine.magic_number)
        return magic is not None and int(magic) == ours
    except (TypeError, ValueError, Exception):
        # A magic that cannot be parsed, or a config that cannot be loaded,
        # means we cannot prove the position is ours -- so it is not.
        return False


def _degraded_snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        connected=False,
        mode="PAPER",
          engine_state="offline",
          engine_online=False,
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


def _configured_db_path() -> Path:
    """The one journal path, read from config and nowhere else.

    Every reader in the UI goes through this. A literal here would mean the
    dashboard reads one journal while the engine writes another, which is
    invisible until the numbers stop agreeing with the broker.
    """
    from engine.config import load_engine_config
    from engine.journal import resolve_db_path
    return Path(resolve_db_path(load_engine_config().engine.db_path))


def _read_engine_status() -> tuple[str, float | None, str | None, bool]:
    """Read the journal's engine state, heartbeat freshness, mode and liveness.

    The first element is the *trading* state the journal records, which is what
    the order-readiness gate consumes. The fourth is whether the engine process
    is alive, which is a different question: a heartbeating engine with trading
    switched OFF (`state=OFF`) is running, not offline. Reporting "offline" for
    it is what made a healthy engine look dead on the dashboard.

    Returns `(state, age, mode, online)`.
    """
    from engine.state import OFFLINE_AFTER_SECONDS

    db_path = _configured_db_path()
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
        return "offline", None, None, False
    state = str(state_row[0] or "OFF").upper()
    mode = str(state_row[1] or "").upper() or None
    if not heartbeat_row:
        return "offline", None, mode, False
    timestamp = datetime.fromisoformat(str(heartbeat_row[0]))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    age = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
    online = bool(heartbeat_row[1]) and age <= OFFLINE_AFTER_SECONDS
    if not online:
        return "offline", age, mode, False

    mapped_state = {
        "OFF": "offline",
        "IDLE": "running",
        "PAUSED": "paused",
        "EMERGENCY_STOP": "halted",
    }.get(state, state.lower())
    return mapped_state, age, mode, True


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
    try:
        db_path = _configured_db_path()
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
    """Return recent read-only engine events for the Journal view.

    ``context_json`` is selected because the message alone is a generic title:
    the guard writes "Account mismatch" for every mismatch while the *detail*
    that tells two incidents apart -- which account was expected, which was
    found, which phase caught it -- lives only in the context blob. Without it
    the journal cannot collapse repeats or name the kind, and the operator sees
    a column of identical rows.
    """
    return _read_journal_frame(
        "SELECT id, ts, category, severity, source, message, context_json "
        "FROM events ORDER BY id DESC LIMIT ?",
        ("id", "ts", "category", "severity", "source", "message", "context_json"),
        limit,
    )


def _journal_paper_position_rows() -> list[dict[str, Any]]:
    """Open PAPER fills from the journal. They never appear in MT5."""
    try:
        journal = Journal(_ui_journal_path(), migrate=False)
    except Exception:  # noqa: BLE001
        return []
    try:
        trades = journal.query(
            "SELECT ticket, symbol, side, volume, entry_price, sl, tp, "
            "magic, ts FROM trades WHERE broker = 'PAPER' AND status = ?",
            ("OPEN",),
        )
        positions = {int(row["ticket"]): row for row in journal.positions()}
        rows: list[dict[str, Any]] = []
        for trade in trades:
            ticket = int(trade["ticket"])
            if ticket < _PAPER_TICKET_FLOOR:
                continue
            pos = positions.get(ticket, {})
            rows.append({
                "ticket": ticket,
                "symbol": trade["symbol"],
                "type": trade["side"],
                "volume": trade["volume"],
                "price_open": pos.get("price_open", trade["entry_price"]),
                "price_current": pos.get("price_current", trade["entry_price"]),
                "sl": pos.get("sl", trade["sl"]),
                "tp": pos.get("tp", trade["tp"]),
                "profit": pos.get("profit", 0.0),
                "magic": pos.get("magic", trade["magic"]),
                "time": trade["ts"],
            })
        return rows
    except Exception as exc:  # noqa: BLE001
        LOG.warning("journal paper positions unreadable: %s", exc)
        return []
    finally:
        try:
            journal.close()
        except Exception:  # noqa: BLE001
            pass


def get_active_positions() -> pd.DataFrame:
    """Return the fixed schema the UI expects, even when the broker is offline."""
    try:
        raw = get_open_positions() if initialize_mt5() else []
        seen = {int(pos["ticket"]) for pos in raw if pos.get("ticket") is not None}
        for pos in _journal_paper_position_rows():
            ticket = int(pos["ticket"])
            if ticket not in seen:
                raw.append(pos)
                seen.add(ticket)
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
                # A position whose magic is not ours is somebody else's. The
                # flag travels with the row so the UI can show it under
                # "Not managed by this system" and offer no controls: the
                # dashboard must never present a close or edit button that
                # would act on another system's trade.
                "magic": pos.get("magic"),
                "managed": _is_our_magic(pos.get("magic")),
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
        engine_state, heartbeat_age_s, engine_mode, engine_online = (
            _read_engine_status())

        return AccountSnapshot(
            connected=True,
            mode=engine_mode if engine_mode in {"LIVE", "PAPER"} else _broker_mode(),
            engine_state=engine_state,
            engine_online=engine_online,
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


def _journal_account_matches() -> Result | None:
    """Refuse a broker write unless the terminal is on the journal's account.

    Returns None when it is safe to proceed, or a Result carrying the refusal.
    The check reads the journal's `journal_account` row and the live account,
    and treats anything unreadable as a mismatch -- including a missing
    fingerprint, which is how a journal left over from another account looks
    until it is stamped. Currency comes from the broker's `currency` field and
    nowhere else, so a EUR account is not silently treated as USD.

    This is the UI half of the engine's AccountGuard. Without it a dashboard
    left open across an account switch would submit an order against the wrong
    account, and the journal would record it as this system's own.
    """
    try:
        telemetry = data_engine.get_account_telemetry() or {}
        live_login = telemetry.get("login")
        live_currency = telemetry.get("currency")

        if not telemetry.get("connected") or live_login in (None, "N/A", ""):
            # Not a mismatch: a terminal that is not answering cannot confirm
            # any account, and the operator's fix is reconnection, not a
            # login change. Mirrors the engine-side TerminalOffline split.
            return Result(False, "TERMINAL_OFFLINE",
                          "Broker or terminal is not answering; order not "
                          "submitted.")

        from engine.account_guard import Fingerprint, fingerprint_from_account
        live = fingerprint_from_account(
            SimpleNamespace(login=live_login, currency=live_currency,
                            server=telemetry.get("server")))
        if live.login is None:
            return Result(False, "TERMINAL_OFFLINE",
                          "Broker or terminal is not answering; order not "
                          "submitted.")

        from engine.journal import Journal, SchemaTooOld
        try:
            journal = Journal(_configured_db_path(), migrate=False)
        except SchemaTooOld:
            return Result(False, "SCHEMA_OLD", SchemaTooOld.TEXT)
        try:
            stored = journal.get_account_fingerprint()
        finally:
            journal.close()

        if not stored or stored.get("login") is None:
            return Result(False, "ACCOUNT_MISMATCH",
                          "This journal has no account fingerprint; "
                          "order not submitted.")
        if not live.matches(Fingerprint(
                login=stored.get("login"), currency=stored.get("currency"))):
            return Result(False, "ACCOUNT_MISMATCH",
                          "Account mismatch; order not submitted.")
        return None
    except Exception as exc:
        LOG.warning("account fingerprint check failed: %s", exc)
        return Result(False, "ACCOUNT_MISMATCH",
                      "Account could not be verified; order not submitted.")


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

    mismatch = _journal_account_matches()
    if mismatch is not None:
        return mismatch

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

    from engine.journal import SchemaTooOld
    try:
        journal = Journal(config.db_path, migrate=False)
    except SchemaTooOld:
        return Result(False, "SCHEMA_OLD", SchemaTooOld.TEXT)
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


def _ui_journal_path() -> Path:
    """The journal the dashboard writes and reads.

    ``submit_order`` uses ``config.db_path``. Close and the positions
    matrix must use the same path so a paper fill is visible to close.
    """
    try:
        config = load_engine_config()
        path = getattr(config, "db_path", None)
        if path:
            return Path(path)
    except Exception:  # noqa: BLE001
        pass
    return _configured_db_path()


def _is_paper_ticket(journal: Journal, ticket: int) -> bool:
    """True only when the journal proves this ticket is a PAPER fill.

    Both ``broker=PAPER`` and the 9_000_000 sequence are required. A
    broker ticket, even one whose number happens to look similar, must
    take the live close path. A paper ticket must never reach
    ``mt5.order_send``.
    """
    row = journal.query_one(
        "SELECT broker, ticket FROM trades WHERE ticket = ?", (int(ticket),))
    if row is None:
        return False
    try:
        number = int(row["ticket"])
    except (TypeError, ValueError):
        return False
    return str(row["broker"] or "") == "PAPER" and number >= _PAPER_TICKET_FLOOR


def _close_paper_ticket(journal: Journal, ticket: int) -> Result:
    """Close a paper fill through PaperExecutor. Never calls the broker."""
    from engine.executor import PaperExecutor

    snapshot = get_account_snapshot()
    executor = PaperExecutor(
        journal=journal,
        state=_ENGINE_STATE_MAP.get(snapshot.engine_state),
        tick_provider=_live_tick,
    )
    try:
        result = executor.close(int(ticket), reason="MANUAL")
    except ExecutionRejected as exc:
        return Result(False, "EXECUTION_REJECTED", str(exc), int(ticket))
    except Exception as exc:  # noqa: BLE001
        LOG.warning("paper close failed for %s: %s", ticket, exc)
        return Result(False, "EXECUTION_ERROR",
                      "The paper close failed; see application log", int(ticket))
    if not result.ok:
        return Result(False, str(result.status),
                      str(result.message or result.status), int(ticket))
    return Result(True, str(result.status),
                  str(result.message or "simulated close"), int(ticket))


def queue_signal_decision(command: str, decision_id: str) -> Result:
    """Enqueue APPROVE_SIGNAL or REJECT_SIGNAL. Does not call an executor."""
    from engine.commands import COMMAND_APPROVE_SIGNAL, COMMAND_REJECT_SIGNAL
    from engine.journal import SchemaTooOld

    if command not in (COMMAND_APPROVE_SIGNAL, COMMAND_REJECT_SIGNAL):
        return Result(False, "BAD_COMMAND", "not an approval command")
    did = str(decision_id or "").strip()
    if not did:
        return Result(False, "BAD_DECISION_ID", "decision_id required")
    journal = None
    try:
        journal = Journal(_ui_journal_path(), migrate=False)
        journal.enqueue_command(
            command, {"decision_id": did, "source": "ui"},
            dedupe_key=f"{command}:{did}",
        )
    except SchemaTooOld:
        return Result(False, "SCHEMA_OLD", SchemaTooOld.TEXT)
    except sqlite3.IntegrityError:
        return Result(False, "ALREADY_QUEUED", "already decided")
    except Exception as exc:  # noqa: BLE001
        LOG.warning("queue_signal_decision failed: %s", exc)
        return Result(False, "QUEUE_FAILED", "could not queue the command")
    finally:
        if journal is not None:
            try:
                journal.close()
            except Exception:  # noqa: BLE001
                pass
    return Result(True, "QUEUED", f"{command} queued")


def close_position(ticket: int) -> Result:
    """Close a paper ticket in the journal, or a broker ticket on MT5.

    Account mismatch is checked first. A PAPER 9_000_000-sequence ticket
    is closed through ``PaperExecutor.close`` and never reaches
    ``mt5.order_send``. Any other ticket keeps the existing broker close.
    """
    mismatch = _journal_account_matches()
    if mismatch is not None:
        return mismatch

    ticket = int(ticket)
    from engine.journal import SchemaTooOld
    journal = None
    try:
        journal = Journal(_ui_journal_path(), migrate=False)
        if _is_paper_ticket(journal, ticket):
            return _close_paper_ticket(journal, ticket)
    except SchemaTooOld:
        return Result(False, "SCHEMA_OLD", SchemaTooOld.TEXT, ticket)
    except Exception as exc:  # noqa: BLE001
        LOG.warning("close_position journal read failed for %s: %s", ticket, exc)
    finally:
        if journal is not None:
            try:
                journal.close()
            except Exception:  # noqa: BLE001
                pass

    try:
        payload = broker_close_position(ticket)
    except Exception as exc:
        LOG.warning("close_position failed for %s: %s", ticket, exc)
        return Result(False, "BROKER_ERROR", "Close request failed; see application log", ticket)

    if not isinstance(payload, dict):
        LOG.warning("close_position returned an unexpected result type: %s", type(payload).__name__)
        return Result(False, "BROKER_ERROR", "Broker returned an invalid close result", ticket)
    if not payload.get("success", False):
        return Result(False, str(payload.get("status", "CLOSE_FAILED")), str(payload.get("message", "Close failed")), ticket)
    return Result(True, str(payload.get("status", "CLOSED")),
                  str(payload.get("message") or "closed"), ticket)


# -- read-only journal tables (Journal tab) ---------------------------------

#: The tables the Journal tab may open. A whitelist, not a user-supplied name:
#: an arbitrary table name would turn a read-only view into a read-any-file
#: primitive, and every query is built from these fixed strings instead.
JOURNAL_TABLES: dict[str, tuple[str, ...]] = {
    "signals": ("id", "symbol", "direction", "score", "bar_ts", "ts"),
    "risk_decisions": ("id", "trade_id", "symbol", "approved",
                       "reasons_json", "ts"),
    "llm_calls": ("id", "decision", "confidence", "ok", "latency_ms",
                  "error", "ts"),   # `provider` is excluded on purpose: it
                                    # stores a vendor name, and no vendor
                                    # name may reach an operator.
    "scan_results": ("id", "symbols_json", "ranked_json", "ts"),
    "trades": ("id", "ticket", "symbol", "side", "volume", "entry_price",
               "exit_price", "sl", "tp", "status", "magic", "ts",
               "updated_at"),
    "positions": ("ticket", "symbol", "side", "volume", "price_open",
                  "price_current", "sl", "tp", "profit", "magic",
                  "updated_at"),
    # The lifecycle chain is the events table restricted to the categories
    # `Journal.log_lifecycle` writes, with the decision ID lifted out of the
    # context JSON so it can be filtered and shown as a column.
    "lifecycle": ("id", "ts", "category", "severity", "message",
                  "decision_id"),
}

#: Tables that have a symbol column the filter can apply to. The others
#: genuinely do not dimension by symbol; the filter is refused rather than
#: silently ignored, because a filter that does nothing is a lie about the
#: data on screen.
_SYMBOL_TABLES = frozenset({"signals", "risk_decisions", "trades", "positions"})

#: Lifecycle categories, in the order the chain reads best. Mirrors
#: `Journal.LIFECYCLE_CATEGORIES`; the query is built from this tuple so a
#: new category has exactly one place to be added.
_LIFECYCLE_CATEGORIES = (
    "signal_generated", "signal_rejected", "risk_approved", "risk_rejected",
    "order_sent", "order_filled", "order_modified", "order_closed",
    "spread_filter_blocked", "news_filter_blocked", "mt5_reconnect",
)



def read_journal_table(
    name: str,
    *,
    db_path: Path | None = None,
    limit: int = 200,
    symbol: str | None = None,
    decision_id: str | None = None,
) -> pd.DataFrame:
    """Read one journal table read-only. Never creates or mutates a database.

    ``name`` must be one of :data:`JOURNAL_TABLES`; anything else raises
    ``ValueError`` rather than being coerced, so a typo surfaces instead of
    rendering an empty table that looks like "no data".

    ``symbol`` filters the four tables that carry a symbol column;
    ``decision_id`` filters the lifecycle chain via the decision ID stored in
    each row's context JSON. Both raise ``ValueError`` when applied to a
    table they do not fit -- a filter that silently does nothing would show
    an operator an unfiltered list under a filtered heading.
    """
    if name not in JOURNAL_TABLES:
        raise ValueError(
            f"unknown journal table {name!r}; expected one of "
            f"{sorted(JOURNAL_TABLES)}"
        )
    columns = JOURNAL_TABLES[name]
    if symbol and name not in _SYMBOL_TABLES:
        raise ValueError(f"table {name!r} has no symbol column to filter by")
    if decision_id and name != "lifecycle":
        raise ValueError(
            "decision IDs live in the lifecycle chain; "
            f"table {name!r} does not carry them"
        )

    where = ""
    params: list[Any] = []
    if name == "lifecycle":
        placeholders = ", ".join("?" for _ in _LIFECYCLE_CATEGORIES)
        where = f"WHERE category IN ({placeholders})"
        params.extend(_LIFECYCLE_CATEGORIES)
        if decision_id:
            where += " AND json_extract(context_json, '$.decision_id') = ?"
            params.append(str(decision_id))
        if symbol:
            where += " AND json_extract(context_json, '$.symbol') = ?"
            params.append(str(symbol))
        query = (
            "SELECT id, ts, category, severity, message, "
            "json_extract(context_json, '$.decision_id') AS decision_id "
            f"FROM events {where} ORDER BY id DESC LIMIT ?"
        )
    else:
        selected = ", ".join(columns)
        clauses: list[str] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(str(symbol))
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        # `positions` is keyed by ticket and has no `id` column; ordering by
        # a column the table does not have would fail the whole read.
        order = "ticket" if name == "positions" else "id"
        query = (f"SELECT {selected} FROM {name} {where} "
                 f"ORDER BY {order} DESC LIMIT ?")

    try:
        target = db_path if db_path is not None else _configured_db_path()
        if not target.exists():
            return pd.DataFrame(columns=columns)
        row_limit = max(1, min(int(limit), 1000))
        with closing(sqlite3.connect(
                target.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(query, (*params, row_limit)).fetchall()
        return pd.DataFrame([dict(row) for row in rows], columns=columns)
    except sqlite3.Error as exc:
        LOG.warning("read-only journal read failed for %s: %s", name, exc)
        return pd.DataFrame(columns=columns)



def system_status(db_path: Path | None = None) -> dict[str, Any]:
    """Read-only system facts for the System Status card.

    Everything here is read from the journal itself -- never from a live
    process, never from the broker -- so the card can be trusted exactly as
    far as the journal can, and no further. The account login is masked to
    its last four digits: a full login in a UI screenshot is a credential
    fragment nobody needs to share.

    The card is the honest window onto the loop: engine state and mode, the
    MT5 account it is bound to, whether the process is online, the heartbeat
    age, pending commands, and the last executed trade plus trades-today for
    this session. Those two trade fields come from the journal, so they stay
    correct whether or not the terminal is reachable.
    """
    target = db_path if db_path is not None else _configured_db_path()
    status: dict[str, Any] = {
        "journal_path": str(target),
        "journal_exists": target.exists(),
        "engine_state": None,
        "mode": None,
        "engine_online": False,
        "heartbeat_age_s": None,
        "schema_version": None,
        "account_login_masked": None,
        "account_currency": None,
        "account_server_type": None,
        "pending_commands": None,
        "last_trade": None,
        # None means "unknown" (journal unreadable or not read). A real zero is
        # only reported after the trades table was actually counted.
        "trades_today": None,
        "scan_loop": "no journal",
        "scan_running": None,
        "last_cycle_ts": None,
        "symbols_scanned": None,
        "schema_notice": None,
        "execution_mode": None,
        "signals": "No signals yet",
        "pending_signals": None,
    }
    if not target.exists():
        return status
    try:
        with closing(sqlite3.connect(
                target.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            version = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version").fetchone()
            from engine.journal import SCHEMA_VERSION
            ver = int(version["v"]) if version and version["v"] is not None else 0
            schema_old = ver < SCHEMA_VERSION
            scan_row = None
            if not schema_old:
                try:
                    scan_row = conn.execute(
                        "SELECT ts, status, symbol_count FROM scan_results "
                        "ORDER BY id DESC LIMIT 1"
                    ).fetchone()
                except sqlite3.OperationalError:
                    scan_row = None
            state = conn.execute(
                "SELECT state, mode FROM engine_state WHERE id = 1"
            ).fetchone()
            heartbeat = conn.execute(
                "SELECT ts, connected FROM heartbeats ORDER BY id DESC LIMIT 1"
            ).fetchone()
            account = conn.execute(
                "SELECT login, currency, server FROM journal_account WHERE id = 1"
            ).fetchone()
            pending = conn.execute(
                "SELECT COUNT(*) AS n FROM commands WHERE status = 'PENDING'"
            ).fetchone()
            # -- pending signals (for the needs-review banner) ---------------
            pending_signals = None
            try:
                sig_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM signals WHERE status = 'NEEDS_REVIEW'"
                ).fetchone()
                pending_signals = int(sig_row["n"]) if sig_row else 0
            except sqlite3.OperationalError:
                pass
            # -- last executed trade + trades today (journal-derived) --------
            # Run on the open connection. Querying after the block above closes
            # it raised ProgrammingError, which was swallowed, so the card never
            # showed a real trade count. trades_today stays None unless counted.
            trade = conn.execute(
                "SELECT ticket, symbol, side, volume, entry_price, "
                "exit_price, status, ts FROM trades ORDER BY id DESC LIMIT 1"
            ).fetchone()
            today = datetime.now(timezone.utc).date()
            counted = conn.execute(
                "SELECT COUNT(*) AS n FROM trades WHERE DATE(ts) = ?",
                (today.isoformat(),),
            ).fetchone()
    except sqlite3.Error as exc:
        LOG.warning("system status read failed: %s", exc)
        return status

    from engine.state import OFFLINE_AFTER_SECONDS

    if trade is not None:
        status["last_trade"] = {
            "ticket": trade["ticket"],
            "symbol": trade["symbol"],
            "side": trade["side"],
            "volume": trade["volume"],
            "entry": trade["entry_price"],
            "exit": trade["exit_price"],
            "status": trade["status"],
            "ts": trade["ts"],
        }
    status["trades_today"] = int(counted["n"]) if counted else None

    status["schema_version"] = version["v"] if version else None
    if schema_old:
        status["schema_notice"] = "Journal schema older than code"
        status["scan_loop"] = "Journal schema older than code"
    else:
        running = bool(state and str(state["state"] or "").upper() == "SCANNING")
        status["scan_running"] = running
        if scan_row is None:
            status["last_cycle_ts"] = None
            status["symbols_scanned"] = 0
            status["scan_loop"] = (
                f"scan_running={running} last_cycle_ts=— symbols_scanned=0"
            )
        else:
            counted = scan_row["symbol_count"]
            status["last_cycle_ts"] = scan_row["ts"]
            status["symbols_scanned"] = 0 if counted is None else int(counted)
            status["scan_loop"] = (
                f"scan_running={running} last_cycle_ts={status['last_cycle_ts']} "
                f"symbols_scanned={status['symbols_scanned']}"
            )
    try:
        from engine.config import load_engine_config
        status["execution_mode"] = load_engine_config().execution_mode
    except Exception:  # noqa: BLE001 - the card still renders without it
        status["execution_mode"] = None
    if state:
        status["engine_state"] = str(state["state"] or "").upper() or None
        status["mode"] = str(state["mode"] or "").upper() or None
    if heartbeat:
        try:
            stamp = datetime.fromisoformat(str(heartbeat["ts"]))
        except ValueError:
            stamp = None
        if stamp is not None:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - stamp).total_seconds()
            status["heartbeat_age_s"] = max(0.0, age)
            status["engine_online"] = (
                bool(heartbeat["connected"])
                and age <= OFFLINE_AFTER_SECONDS
            )
    if account and account["login"] is not None:
        digits = str(account["login"])[-4:]
        status["account_login_masked"] = f"****{digits}"
        status["account_currency"] = account["currency"]
        server = account["server"] if "server" in account.keys() else None
        if server:
            status["account_server_type"] = (
                "demo" if "demo" in str(server).lower() else "live"
            )
    if pending is not None:
        status["pending_commands"] = int(pending["n"])
    status["pending_signals"] = pending_signals
    return status

    return Result(True, "OK", str(payload.get("message", "Position closed")), int(ticket))
