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


def get_available_symbols() -> dict:
    """Dynamically fetches and categorizes available instruments directly from MT5."""
    if not initialize_mt5():
        return {
            "Popular Watchlist": ["USDCAD", "XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "BTCUSD"],
            "Forex Majors": ["EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD", "NZDUSD"],
        }

    symbols = mt5.symbols_get()
    if not symbols:
        return {"Popular Watchlist": ["USDCAD", "XAUUSD", "EURUSD", "GBPUSD", "USDJPY"]}

    symbol_names = [s.name for s in symbols]
    symbol_set = set(symbol_names)

    # 1. Popular curated list (check availability on broker)
    popular_candidates = [
        "XAUUSD", "USDCAD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF",
        "BTCUSD", "ETHUSD", "US500", "USTEC", "US30", "NVDA", "AMD", "MSFT", "AAPL"
    ]
    popular = [s for s in popular_candidates if s in symbol_set]
    if not popular:
        popular = symbol_names[:10]

    # 2. Forex Majors & Crosses
    forex = [s for s in symbol_names if len(s) == 6 and any(s.startswith(cur) for cur in ["EUR", "GBP", "USD", "AUD", "NZD", "CAD", "CHF", "JPY"])]

    # 3. Metals & Energy
    metals = [s for s in symbol_names if any(x in s for x in ["XAU", "XAG", "GOLD", "SILVER", "OIL", "BRENT", "WTI"])]

    # 4. Crypto
    crypto = [s for s in symbol_names if any(x in s for x in ["BTC", "ETH", "SOL", "XRP", "LTC", "DOGE"])]

    # 5. Global Indices & Equities
    indices_stocks = [s for s in symbol_names if any(x in s for x in ["US500", "USTEC", "US30", "GER40", "UK100", "NVDA", "AMD", "MSFT", "AAPL", "TSLA", "AMZN"])]

    categories = {
        "Popular Watchlist": popular,
        "Forex Currencies": forex[:50] if forex else symbol_names[:30],
        "Metals & Commodities": metals[:30] if metals else ["XAUUSD"],
        "Crypto Assets": crypto[:30] if crypto else ["BTCUSD"],
        "Indices & Equities": indices_stocks[:30] if indices_stocks else [],
        "All MT5 Instruments": symbol_names[:250],
    }

    return categories


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


def get_symbol_live_quote(symbol: str) -> dict:
    """Fetches real-time live bid, ask, spread, and price digits for an instrument."""
    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    info = mt5.symbol_info(symbol)

    if not tick or not info:
        return {"bid": 0.0, "ask": 0.0, "spread": 0.0, "digits": 2, "time": "N/A", "description": ""}

    spread = (tick.ask - tick.bid) / (info.point * 10 if info.digits in (3, 5) else info.point)
    return {
        "bid": tick.bid,
        "ask": tick.ask,
        "spread": round(spread, 1),
        "digits": info.digits,
        "time": pd.to_datetime(tick.time, unit="s").strftime("%H:%M:%S"),
        "description": info.description or "",
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
