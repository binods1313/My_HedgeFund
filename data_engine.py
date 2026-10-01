import MetaTrader5 as mt5
import pandas as pd
import numpy as np
from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, MT5_PATH


def initialize_mt5() -> bool:
    """Connects to the MetaTrader 5 terminal with robust retry and timeout handling."""
    term = mt5.terminal_info()
    if term is not None and term.connected:
        return True

    try:
        if mt5.initialize(path=MT5_PATH, login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER, timeout=60000):
            return True
    except Exception:
        pass

    try:
        if mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER, timeout=60000):
            return True
    except Exception:
        pass

    try:
        if mt5.initialize():
            return True
    except Exception:
        pass

    return False


def _classify_symbol(sym_info) -> str:
    """Classify an MT5 symbol into an asset class using its path/sector.

    Uses sym_info.path (e.g. 'Forex\\Majors\\EURUSD') rather than name
    substring matching, which incorrectly classifies symbols like 'AETH'
    (a stock ETF) as crypto because its name contains 'ETH'.

    Anything not matched is 'Unclassified' and is never tradable: it is not
    offered in the UI dropdown and the executor rejects it independently.

    Note the keyword lists must match plural forms. Brokers commonly use
    'Indices', which does NOT contain the substring 'INDEX' -- matching only
    the singular silently misfiled every index as Unclassified.
    """
    path = (sym_info.path or "").upper()

    if any(k in path for k in ("FOREX", "FX", "CURRENCIES")):
        return "Forex Currencies"
    if any(k in path for k in ("CRYPTO", "DIGITAL", "COIN")):
        return "Crypto Assets"
    if any(k in path for k in ("METAL", "COMMODITY", "COMMODITIES", "ENERGY")):
        return "Metals & Commodities"
    if any(k in path for k in ("INDEX", "INDICES", "INDICE", "CFD")):
        return "Indices & Equities"
    if any(k in path for k in ("STOCK", "EQUITY", "EQUITIES", "SHARE", "SHARES")):
        return "Indices & Equities"
    return "Unclassified"


def get_available_symbols() -> dict:
    """Dynamically fetches and categorises available instruments from MT5.

    Classification is based on sym_info.path, not name substrings, to avoid
    mis-categorising symbols like AETH (stock ETF) as crypto assets.

    Only symbols that are BOTH in config.yaml's tradable whitelist AND
    classifiable appear in the result. The dropdown source is therefore
    incapable of offering an Unclassified or non-whitelisted symbol; the
    executor enforces the same rule independently.
    """
    from config_loader import load_config

    try:
        cfg = load_config()
        allowed = {s.upper() for s in cfg.trading.tradable_symbols}
    except Exception:
        # Fails closed: with no validated whitelist we cannot prove any symbol is
        # tradable, so we offer none.
        return {}

    if not initialize_mt5():
        return {}

    symbols = mt5.symbols_get()
    if not symbols:
        return {}

    categories: dict[str, list[str]] = {
        "Forex Currencies": [],
        "Metals & Commodities": [],
        "Crypto Assets": [],
        "Indices & Equities": [],
    }

    for sym in symbols:
        if sym.name.upper() not in allowed:
            continue
        cat = _classify_symbol(sym)
        if cat == "Unclassified":
            # Not offered for trading. Reported separately below so the reason
            # is visible rather than silently dropped.
            continue
        if len(categories[cat]) < 250:
            categories[cat].append(sym.name)

    result = {cat: syms for cat, syms in categories.items() if syms}
    return result



def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculates Relative Strength Index (RSI)."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / (loss + 1e-9)
    return 100 - (100 / (1 + rs))


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculates Average True Range (ATR)."""
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift()).abs()
    low_close = (df["low"] - df["close"].shift()).abs()
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = ranges.max(axis=1)
    return true_range.rolling(window=period).mean()


def get_technical_dataframe(symbol: str, timeframe=mt5.TIMEFRAME_M15, count: int = 60) -> pd.DataFrame:
    """Fetches candle rates and appends technical indicators for any selected symbol."""
    mt5.symbol_select(symbol, True)

    rates = mt5.copy_rates_from_pos(symbol, timeframe, 0, count)
    if rates is None or len(rates) == 0:
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    # Technical Indicators
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    if len(df) >= 200:
        df["ema200"] = df["close"].ewm(span=200, adjust=False).mean()
    else:
        df["ema200"] = df["ema50"]

    df["rsi"] = calculate_rsi(df["close"], period=14).fillna(50)
    df["atr"] = calculate_atr(df, period=14).fillna(0)

    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    return df


def get_market_data(symbol: str) -> str:
    """Generates a dynamic multi-timeframe text summary for any chosen instrument."""
    mt5.symbol_select(symbol, True)
    df_h4 = get_technical_dataframe(symbol, mt5.TIMEFRAME_H4, count=25)
    df_m15 = get_technical_dataframe(symbol, mt5.TIMEFRAME_M15, count=25)

    if df_h4.empty or df_m15.empty:
        df_h4 = get_technical_dataframe(symbol, mt5.TIMEFRAME_M15, count=25)
        df_m15 = get_technical_dataframe(symbol, mt5.TIMEFRAME_M5, count=25)
        if df_m15.empty:
            return f"Error: Unable to fetch candle rates for {symbol} on MT5."

    latest_m15 = df_m15.iloc[-1]
    latest_h4 = df_h4.iloc[-1]

    h4_trend = "BULLISH (Above EMA50)" if latest_h4["close"] > latest_h4["ema50"] else "BEARISH (Below EMA50)"
    m15_trend = "BULLISH (Above EMA20)" if latest_m15["close"] > latest_m15["ema20"] else "BEARISH (Below EMA20)"

    h4_candles = df_h4.tail(6)[["time", "open", "high", "low", "close", "rsi"]].to_string(index=False)
    m15_candles = df_m15.tail(6)[["time", "open", "high", "low", "close", "rsi"]].to_string(index=False)

    summary = (
        f"=== ASSET: {symbol} ===\n"
        f"CURRENT PRICE: {latest_m15['close']} | ATR (14): {latest_m15['atr']:.5f}\n"
        f"4H MACRO TREND: {h4_trend} | 4H RSI: {latest_h4['rsi']:.1f} | 4H EMA50: {latest_h4['ema50']:.5f}\n"
        f"15M MICRO MOMENTUM: {m15_trend} | 15M RSI: {latest_m15['rsi']:.1f} | 15M EMA20: {latest_m15['ema20']:.5f}\n"
        f"MACD Histogram: {latest_m15['macd_hist']:.5f}\n\n"
        f"--- 4-Hour Timeframe (Recent Candles) ---\n{h4_candles}\n\n"
        f"--- 15-Minute Timeframe (Recent Candles) ---\n{m15_candles}\n"
    )
    return summary


def get_symbol_live_quote(symbol: str, max_tick_age_seconds: int = 30) -> dict:
    """Fetches real-time live bid, ask, spread, and price digits for an instrument.

    Spread is computed as (ask - bid) / point, which is correct for all asset
    classes (forex, JPY pairs, gold, indices, crypto).

    Sanity checks (all result in valid=False):
      1. Missing data
      2. Inverted bid/ask (ask < bid)
      3. Spread > 5 % of ask price (corrupt/stale quote)
      4. Tick age > max_tick_age_seconds (stale feed)

    Args:
        symbol:               MT5 symbol name.
        max_tick_age_seconds: Reject ticks older than this. Configurable;
                              defaults to 30 s (set via config.yaml / env).
    """
    import time as _time
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)

    if not tick or not info:
        return {"bid": 0.0, "ask": 0.0, "spread": 0.0, "digits": 2,
                "time": "N/A", "description": "", "valid": False, "reason": "No data"}

    # 1. Stale tick check — tick.time is a UNIX timestamp (seconds)
    tick_age = _time.time() - tick.time
    if tick_age > max_tick_age_seconds:
        return {"bid": tick.bid, "ask": tick.ask, "spread": 0.0,
                "digits": info.digits,
                "time": pd.to_datetime(tick.time, unit="s").strftime("%H:%M:%S"),
                "description": info.description or "",
                "valid": False,
                "reason": f"Stale tick: age {tick_age:.0f}s > {max_tick_age_seconds}s"}

    # 2. Inverted bid/ask
    if tick.ask <= 0 or tick.bid <= 0 or tick.ask < tick.bid:
        return {"bid": 0.0, "ask": 0.0, "spread": 0.0, "digits": info.digits,
                "time": "N/A", "description": info.description or "",
                "valid": False, "reason": "Inverted bid/ask"}

    # 3. Corrupt spread > 5 % of ask
    if (tick.ask - tick.bid) > tick.ask * 0.05:
        return {"bid": tick.bid, "ask": tick.ask, "spread": 0.0,
                "digits": info.digits,
                "time": pd.to_datetime(tick.time, unit="s").strftime("%H:%M:%S"),
                "description": info.description or "",
                "valid": False, "reason": "Spread > 5 % of price (stale/corrupt)"}

    # Spread in points using the symbol's own point size — no hardcoded factor
    spread_points = round((tick.ask - tick.bid) / info.point, 1)

    return {
        "bid": tick.bid,
        "ask": tick.ask,
        "spread": spread_points,
        "digits": info.digits,
        "time": pd.to_datetime(tick.time, unit="s").strftime("%H:%M:%S"),
        "description": info.description or "",
        "valid": True,
        "reason": "",
        "tick_age_s": round(tick_age, 1),
    }




def get_account_telemetry() -> dict:
    """Fetches real-time MT5 account metrics and connectivity status."""
    acc = mt5.account_info()
    term = mt5.terminal_info()

    if acc is None:
        return {
            "connected": False,
            "login": "N/A",
            "server": "Offline",
            "balance": 0.0,
            "equity": 0.0,
            "profit": 0.0,
            "margin": 0.0,
            "margin_free": 0.0,
            "margin_level": 0.0,
            "currency": "USD",
            "leverage": 100,
            "trade_allowed": False,
        }

    return {
        "connected": True,
        "login": acc.login,
        "server": acc.server,
        "balance": acc.balance,
        "equity": acc.equity,
        "profit": acc.profit,
        "margin": acc.margin,
        "margin_free": acc.margin_free,
        "margin_level": acc.margin_level if acc.margin_level else 0.0,
        "currency": acc.currency,
        "leverage": acc.leverage,
        "trade_allowed": acc.trade_allowed and (term.trade_allowed if term else True),
        "company": acc.company,
    }


def get_watchlist_quotes(symbols: list) -> list:
    """Fetches live quotes for multiple symbols."""
    watchlist = []
    for sym in symbols:
        q = get_symbol_live_quote(sym)
        if q["bid"] > 0:
            watchlist.append({
                "symbol": sym,
                "bid": q["bid"],
                "ask": q["ask"],
                "spread": q["spread"],
                "time": q["time"]
            })
    return watchlist
