import MetaTrader5 as mt5


# ─────────────────────────────────────────────────────────────────────────────
# Account-mode guard
# ─────────────────────────────────────────────────────────────────────────────
# Every order-sending path in this module goes through require_demo_account().
# A verification or maintenance script must never be able to place a live order
# by accident, so the guard is a hard refusal on a real account rather than a
# warning. Live trading is not reachable from this module at all; it arrives
# with the engine's own executor, which has its own explicit mode gate.

DEMO_MODE_LABEL = "DEMO"


def is_demo_account() -> tuple[bool, str]:
    """Is the connected MT5 account a demo account?

    Returns (is_demo, reason). An unreadable account is NOT treated as demo:
    if we cannot prove it is a demo account we must refuse to trade.
    """
    acc = mt5.account_info()
    if acc is None:
        return False, "account_info() returned None; cannot confirm demo mode"

    trade_mode = getattr(acc, "trade_mode", None)
    demo_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0)

    if trade_mode is None:
        return False, "account_info() has no trade_mode; cannot confirm demo mode"

    if trade_mode == demo_mode:
        return True, ""

    contest_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_CONTEST", 1)
    real_mode = getattr(mt5, "ACCOUNT_TRADE_MODE_REAL", 2)
    if trade_mode == real_mode:
        return False, (
            f"account trade_mode={trade_mode} is REAL/live; refusing to send "
            f"orders from a verification path"
        )
    if trade_mode == contest_mode:
        return False, (
            f"account trade_mode={trade_mode} is CONTEST; refusing to send "
            f"orders from a verification path"
        )
    return False, f"unrecognised account trade_mode={trade_mode}; refusing to send orders"


def require_demo_account() -> tuple[bool, str]:
    """Gate for order-sending code. Returns (allowed, reason_if_not)."""
    return is_demo_account()


def get_open_positions() -> list:
    """Retrieves all active open trading positions from MT5."""
    positions = mt5.positions_get()
    if positions is None or len(positions) == 0:
        return []

    pos_list = []
    for pos in positions:
        pos_list.append({
            "ticket": pos.ticket,
            "symbol": pos.symbol,
            "type": "BUY" if pos.type == mt5.ORDER_TYPE_BUY else "SELL",
            "volume": pos.volume,
            "price_open": pos.price_open,
            "price_current": pos.price_current,
            "sl": pos.sl,
            "tp": pos.tp,
            "profit": round(pos.profit, 2),
            "swap": pos.swap,
            "comment": pos.comment,
        })
    return pos_list


def close_position(ticket: int) -> dict:
    """Closes a specific open position by ticket."""
    # Closing is still an order. On a live account this module does not act.
    demo_ok, demo_reason = require_demo_account()
    if not demo_ok:
        return {
            "success": False,
            "status": "REFUSED",
            "message": f"Refusing to close: {demo_reason}",
            "ticket": None,
        }

    positions = mt5.positions_get(ticket=ticket)
    if not positions or len(positions) == 0:
        return {"success": False, "message": f"Position #{ticket} not found."}

    pos = positions[0]
    symbol = pos.symbol
    lot = pos.volume
    order_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    price = mt5.symbol_info_tick(symbol).bid if order_type == mt5.ORDER_TYPE_SELL else mt5.symbol_info_tick(symbol).ask

    symbol_info = mt5.symbol_info(symbol)
    filling = mt5.ORDER_FILLING_IOC
    if symbol_info and (symbol_info.filling_mode & 1):
        filling = mt5.ORDER_FILLING_FOK

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "position": ticket,
        "symbol": symbol,
        "volume": lot,
        "type": order_type,
        "price": price,
        "deviation": 25,
        "magic": 100100,
        "comment": f"Close #{ticket}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    result = mt5.order_send(request)
    if result is None:
        return {"success": False, "message": f"Close failed. Error: {mt5.last_error()}"}
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"success": False, "message": f"Close failed (Code {result.retcode}): {result.comment}"}

    return {"success": True, "message": f"Position #{ticket} successfully closed at {price}."}


def close_all_positions() -> list:
    """Closes all active open positions (Emergency Close)."""
    positions = mt5.positions_get()
    if not positions or len(positions) == 0:
        return [{"success": True, "message": "No open positions to close."}]

    results = []
    for pos in positions:
        res = close_position(pos.ticket)
        results.append(res)
    return results


def _normalize_lot(raw_lot: float, info) -> tuple[float, str]:
    """Normalize a raw lot size to broker rules.

    Correct order per §1.2 of Instructions_2.md:
      1. Floor to volume_step (using Decimal to avoid float rounding errors like 0.07/0.01 = 6.999)
      2. If result < volume_min  → REJECT (do NOT round up, that breaks the risk rule)
      3. Clamp to volume_max

    Returns (normalized_lot, error_string).  error_string is empty on success.
    """
    from decimal import Decimal, ROUND_DOWN

    step   = Decimal(str(info.volume_step))
    v_min  = Decimal(str(info.volume_min))
    v_max  = Decimal(str(info.volume_max))
    raw    = Decimal(str(raw_lot))

    # 1. Floor to nearest step
    floored = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step

    # 2. Reject if below minimum — do NOT clamp up (would break risk budget)
    if floored < v_min:
        return 0.0, (
            f"Calculated lot {float(floored):.4f} is below broker minimum "
            f"{float(v_min):.4f}. Risk budget too small — trade rejected."
        )

    # 3. Clamp to maximum
    normalized = min(floored, v_max)
    return float(normalized), ""


def _reject(message: str) -> dict:
    return {"success": False, "status": "REJECTED", "message": message, "ticket": None}


def _load_tradable_whitelist() -> set[str]:
    """Read the tradable whitelist from config.yaml.

    Fails closed: if config cannot be loaded we cannot prove the symbol is
    allowed to trade, so the caller must reject. The exception is propagated
    and converted to a rejection by the caller.
    """
    from config_loader import load_config
    return {s.upper() for s in load_config().trading.tradable_symbols}


def _check_symbol_allowed(symbol: str) -> tuple[bool, str]:
    """Gate 1: is this symbol tradable at all?

    Runs BEFORE any MT5 call so a disallowed symbol can never reach the broker.
    Two independent conditions must hold:
      - the symbol appears in config.yaml's trading.tradable_symbols
      - MT5 classifies it as a known asset class (not "Unclassified")

    Returns (allowed, reason).
    """
    from data_engine import _classify_symbol

    try:
        whitelist = _load_tradable_whitelist()
    except Exception as exc:
        return False, f"Cannot load tradable whitelist from config: {exc}"

    if symbol.upper() not in whitelist:
        return False, (
            f"Symbol '{symbol}' is not in the config.yaml tradable whitelist; "
            "refusing to place an order"
        )

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return False, f"Symbol '{symbol}' not found on server"

    asset_class = _classify_symbol(sym_info)
    if asset_class == "Unclassified":
        return False, (
            f"Symbol '{symbol}' classifies as Unclassified; refusing to trade it"
        )

    return True, asset_class


def execute_trade(symbol: str, decision: str, lot_size: float = 0.01, sl_pips: float = 0, tp_pips: float = 0) -> dict:
    """Places market orders with dynamic filling mode, risk guardrails, and optional SL/TP."""
    decision = str(decision).upper().strip()

    if decision == "HOLD":
        return {
            "success": True,
            "status": "HOLD",
            "message": f"Decision is HOLD for {symbol}. Risk exposure unchanged.",
            "ticket": None,
        }

    if decision not in ("BUY", "SELL"):
        return {
            "success": False,
            "status": "ERROR",
            "message": f"Invalid decision '{decision}'. Expected BUY, SELL, or HOLD.",
            "ticket": None,
        }

    # Every order carries a broker-side stop loss. Without one there is no
    # defined worst case, so an order without an SL is never sent.
    if sl_pips is None or sl_pips <= 0:
        return _reject(
            f"Refusing to place {decision} {symbol} without a stop loss "
            "(sl_pips must be > 0). Every order requires a broker-side SL."
        )

    # Account-mode gate comes FIRST, before any broker query about the symbol.
    # On a live account this module must refuse without touching symbol data.
    demo_ok, demo_reason = require_demo_account()
    if not demo_ok:
        return {
            "success": False,
            "status": "REFUSED",
            "message": f"Refusing to place order: {demo_reason}",
            "ticket": None,
        }

    # Whitelist gate runs before any MT5 order call.
    allowed, reason = _check_symbol_allowed(symbol)
    if not allowed:
        return _reject(reason)

    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return {
            "success": False,
            "status": "ERROR",
            "message": f"Symbol '{symbol}' not found on server.",
            "ticket": None,
        }

    if not symbol_info.visible:
        if not mt5.symbol_select(symbol, True):
            return {
                "success": False,
                "status": "ERROR",
                "message": f"Failed to activate symbol '{symbol}'.",
                "ticket": None,
            }

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {
            "success": False,
            "status": "ERROR",
            "message": f"Failed to retrieve market tick for '{symbol}'.",
            "ticket": None,
        }

    point = symbol_info.point
    digits = symbol_info.digits

    if decision == "BUY":
        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask
        sl = round(price - (sl_pips * point * 10), digits) if sl_pips > 0 else 0.0
        tp = round(price + (tp_pips * point * 10), digits) if tp_pips > 0 else 0.0
    else:
        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid
        sl = round(price + (sl_pips * point * 10), digits) if sl_pips > 0 else 0.0
        tp = round(price - (tp_pips * point * 10), digits) if tp_pips > 0 else 0.0

    # Determine broker filling mode
    filling_mode = mt5.ORDER_FILLING_IOC
    if symbol_info.filling_mode & 1:
        filling_mode = mt5.ORDER_FILLING_FOK
    elif symbol_info.filling_mode & 2:
        filling_mode = mt5.ORDER_FILLING_IOC

    # Normalize lot to broker rules (§1.2: floor → reject if < min → clamp to max)
    normalized_lot, lot_err = _normalize_lot(lot_size, symbol_info)
    if lot_err:
        return _reject(lot_err)

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": normalized_lot,
        "type": order_type,
        "price": price,
        "sl": sl,
        "tp": tp,
        "deviation": 25,
        "magic": 100100,
        "comment": f"AI PM {decision}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_mode,
    }

    # Pre-flight check via order_check before sending
    check = mt5.order_check(request)
    if check is None or check.retcode not in (0, mt5.TRADE_RETCODE_DONE):
        err = mt5.last_error() if check is None else f"Code {check.retcode}: {check.comment}"
        return _reject(f"Pre-flight check failed: {err}")

    result = mt5.order_send(request)
    if result is None:
        return {
            "success": False,
            "status": "ERROR",
            "message": f"Order placement failed. MT5 Error: {mt5.last_error()}",
            "ticket": None,
        }

    if result.retcode != mt5.TRADE_RETCODE_DONE:
        if result.retcode == 10027:
            msg = (
                "Trade rejected (Code 10027: AutoTrading disabled by client). "
                "Enable the 'Algo Trading' button (green) in the MetaTrader 5 toolbar."
            )
        else:
            msg = f"Trade rejected by broker (Code {result.retcode}): {result.comment}"

        return _reject(msg)

    return {
        "success": True,
        "status": "EXECUTED",
        "message": f"Executed {decision} {lot_size} lot(s) on {symbol} at {price} (Order #{result.order})",
        "ticket": result.order,
        "price": price,
    }
