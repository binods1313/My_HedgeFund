"""Read-only probe: does the broker populate tick_value and tick_size?

The Risk Engine converts a price distance to a stop into account currency:

    risk = |entry - stop| / tick_size * tick_value * volume

If either field is missing, OrderIntent refuses to build, and NO order can ever
be sized. Phase 1's HUD reported "2 UNPROTECTED" because these values came back
empty through get_symbol_info_safe(), so this needs confirming against the live
terminal before the gate is wired to the executor.

READ-ONLY. Opens the connection, reads symbol specifications, closes. Places no
orders and never calls order_send or order_check.

Usage:
    .venv\\Scripts\\python.exe scripts\\probe_tick_specs.py
    .venv\\Scripts\\python.exe scripts\\probe_tick_specs.py --symbols EURUSD,XAUUSD
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from engine.config import read_mt5_credentials

# The primary symbols the Risk Engine will size against, grouped by asset class.
# These are the whitelist entries that actually quote on this broker.
DEFAULT_SYMBOLS = [
    "EURUSD", "GBPUSD", "USDJPY", "USDCAD", "USDCHF", "AUDUSD",
    "XAUUSD", "XAGUSD",
    "US30", "USTEC", "US500",
]

# Fields the engine needs, and what each is used for.
REQUIRED = ("trade_tick_value", "trade_tick_size")


def probe(symbol: str) -> dict:
    """Read one symbol's trade specification. Read-only."""
    import MetaTrader5 as mt5

    info = mt5.symbol_info(symbol)
    if info is None:
        return {"symbol": symbol, "ok": False, "reason": "symbol_info returned None"}

    tick_value = getattr(info, "trade_tick_value", None)
    tick_size = getattr(info, "trade_tick_size", None)
    point = getattr(info, "point", None)
    digits = getattr(info, "digits", None)
    volume_min = getattr(info, "volume_min", None)
    volume_step = getattr(info, "volume_step", None)

    missing = [f for f in REQUIRED
               if getattr(info, f, None) in (None, 0.0)]

    # Money value of ONE POINT of price movement, per 1.0 lot.
    #
    # MT5's trade_tick_value is the value of one trade_tick_size move, not of
    # one point. The two are usually equal, but the conversion must go through
    # point/tick_size rather than dividing by tick_size: dividing reported
    # 88,861.24 per point for EURUSD, where the sane figure is 0.8886. That is
    # the wrong direction to be wrong in -- it would inflate computed risk by
    # 100,000x and block every order against the per-trade cap.
    #
    # A pip is 10 points for 5-digit and 3-digit quotes, and 1 point for
    # indices quoted in whole points, so pip value is derived per symbol.
    tick_money_per_point = None
    pip_size_points = None
    if tick_value and tick_size:
        tick_money_per_point = tick_value * (point / tick_size)
        pip_size_points = (10 if digits in (3, 5) else 1)

    return {
        "symbol": symbol,
        "ok": not missing,
        "missing": missing,
        "trade_tick_value": tick_value,
        "trade_tick_size": tick_size,
        "tick_money_per_point": tick_money_per_point,
        "pip_size_points": pip_size_points,
        "point": point,
        "digits": digits,
        "volume_min": volume_min,
        "volume_step": volume_step,
        "path": getattr(info, "path", ""),
        "visible": getattr(info, "visible", None),
        "trade_mode": getattr(info, "trade_mode", None),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", help="comma-separated symbols to probe")
    args = parser.parse_args(argv)

    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else DEFAULT_SYMBOLS)

    print("=" * 78)
    print("  LIVE TICK SPECIFICATION PROBE (read-only, no orders)")
    print("=" * 78)

    creds = read_mt5_credentials()
    import MetaTrader5 as mt5

    kwargs = {"path": creds["terminal_path"]} if creds["terminal_path"] else {}
    if not mt5.initialize(**kwargs):
        print(f"  [FAIL] initialize() failed: {mt5.last_error()}")
        return 1
    if creds["login"] and not mt5.login(creds["login"], creds["password"],
                                        creds["server"]):
        print(f"  [FAIL] login() failed: {mt5.last_error()}")
        mt5.shutdown()
        return 1

    account = mt5.account_info()
    if account is not None:
        print(f"  account  : ***{str(account.login)[-3:]}  "
              f"{account.currency}  ({getattr(account, 'server', '?')})")
    print()

    usable, unusable, missing_symbol = [], [], []

    try:
        for symbol in symbols:
            row = probe(symbol)
            if row.get("reason") == "symbol_info returned None":
                missing_symbol.append(symbol)
                print(f"  {symbol:10s} NOT AVAILABLE ON THIS BROKER")
                continue

            if row["ok"]:
                usable.append(symbol)
                print(f"  {symbol:10s} OK")
                print(f"      tick_value      : {row['trade_tick_value']}")
                print(f"      tick_size       : {row['trade_tick_size']}")
                print(f"      value per point : "
                      f"{row['tick_money_per_point']} per lot")
                if row["pip_size_points"]:
                    pip_value = (row["tick_money_per_point"]
                                 * row["pip_size_points"])
                    print(f"      value per pip   : {pip_value} per lot "
                          f"({row['pip_size_points']} points/pip)")
                print(f"      point/digits    : {row['point']} / "
                      f"{row['digits']}")
                print(f"      volume          : min={row['volume_min']} "
                      f"step={row['volume_step']}")
            else:
                unusable.append(symbol)
                print(f"  {symbol:10s} UNUSABLE FOR SIZING")
                print(f"      missing      : {', '.join(row['missing'])}")
                print(f"      tick_value   : {row['trade_tick_value']}")
                print(f"      tick_size    : {row['trade_tick_size']}")
                print(f"      point        : {row['point']}  digits: {row['digits']}")
                print(f"      path         : {row['path']}")
            print()
    finally:
        mt5.shutdown()

    print("=" * 78)
    print("  SUMMARY")
    print("=" * 78)
    print(f"  symbols probed      : {len(symbols)}")
    print(f"  usable for sizing   : {len(usable)}  {usable}")
    print(f"  missing tick fields : {len(unusable)}  {unusable}")
    print(f"  not on this broker  : {len(missing_symbol)}  {missing_symbol}")
    print()

    if unusable:
        print("  RESULT: FAIL - these symbols cannot be sized by the Risk Engine.")
        print("          OrderIntent raises on a missing tick spec, so no order")
        print("          for them can be built. Remove them from the tradable")
        print("          whitelist or find the correct broker symbol name.")
        return 1

    print("  RESULT: OK - every available symbol reports both tick fields,")
    print("          so the Risk Engine can size an order against them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())