import MetaTrader5 as mt5


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

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(lot_size),
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
                "👉 Enable the 'Algo Trading' button (green) in the top toolbar of MetaTrader 5."
            )
        else:
            msg = f"Trade rejected by broker (Code {result.retcode}): {result.comment}"

        return {
            "success": False,
            "status": "REJECTED",
            "message": msg,
            "ticket": None,
        }

    return {
        "success": True,
        "status": "EXECUTED",
        "message": f"Executed {decision} {lot_size} lot(s) on {symbol} at {price} (Order #{result.order})",
        "ticket": result.order,
        "price": price,
    }
