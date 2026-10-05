"""Dry-run CLI: python -m engine.dryrun

Exercises the full pre-submission path against LIVE candles and stops before
order_send, so you can see exactly what would be sent and whether the risk gate
would allow it.

READ-ONLY. This command reads candles and account state, evaluates the five
blocks, builds a real OrderIntent and runs the real RiskEngine. It cannot place
an order: the module holds no reference to the submission entry point.

Usage:
    python -m engine.dryrun --symbol EURUSD
    python -m engine.dryrun --symbol BTCUSD# --direction BUY
    python -m engine.dryrun --symbol EURUSD --stop-pips 30 --volume 0.02
    python -m engine.dryrun --symbol EURUSD --no-vwap
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from engine.config import load_engine_config, read_mt5_credentials
from engine.dryrun import DEFAULT_BARS, DEFAULT_VOLUME, run_dry_run
from engine.equity import EquityTracker
from engine.journal import Journal
from engine.risk import RiskEngine, RiskLimits
from engine.sizing import LotSizer
from engine.validation import XAIValidationGate


def fetch_bars(symbol: str, timeframe_name: str, count: int) -> pd.DataFrame:
    """Live candles with the indicator columns the strategy reads.

    The MT5 imports here are deliberate: this is the boundary layer. It reads
    from the terminal and hands plain data to engine.dryrun, which never imports
    MetaTrader5. Every MT5 access in the engine therefore stays behind the
    gateway or behind this one adapter.
    """
    import MetaTrader5 as mt5

    from data_engine import get_technical_dataframe, initialize_mt5

    if not initialize_mt5():
        raise RuntimeError("cannot connect to MT5")

    timeframes = {
        "m15": mt5.TIMEFRAME_M15,
        "h1": mt5.TIMEFRAME_H1,
        "h4": mt5.TIMEFRAME_H4,
    }
    timeframe = timeframes[timeframe_name]

    df = get_technical_dataframe(symbol, timeframe, count=count)
    if df is None or df.empty:
        raise RuntimeError(
            f"no candles for {symbol} on {timeframe_name}. The symbol may not "
            f"be selected in this terminal."
        )
    # The structural rules need tick_volume for VWAP; MT5 supplies it.
    return df


def _build_sizer(symbol: str):
    """LotSizer from the broker's live spec, or None when unavailable.

    ui.data_access.get_symbol_spec is reused rather than reimplemented so the
    size the dashboard shows and the size we compute cannot drift apart. It
    returns None rather than raising when the broker omits a field, and the
    dry run falls back to its own arithmetic in that case.
    """
    try:
        from ui.data_access import get_symbol_spec
        spec = get_symbol_spec(symbol, "forex")
        if spec is None:
            return None
        return LotSizer(spec)
    except Exception:   # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engine.dryrun",
        description="Dry-run the full path to order submission. Places no order.")
    parser.add_argument("--symbol", required=True,
                        help="broker symbol name, e.g. EURUSD or BTCUSD#")
    parser.add_argument("--timeframe", default="m15",
                        choices=("m15", "h1", "h4"))
    parser.add_argument("--bars", type=int, default=DEFAULT_BARS)
    parser.add_argument("--direction", choices=("BUY", "SELL"),
                        help="force the setup direction instead of letting "
                             "the sweep decide")
    parser.add_argument("--volume", type=float, default=DEFAULT_VOLUME)
    parser.add_argument("--stop-pips", type=float, default=None,
                        help="stop distance; default is 1%% from entry")
    parser.add_argument("--no-vwap", action="store_true",
                        help="do not require the VWAP block to agree")
    parser.add_argument("--json", action="store_true",
                        help="emit machine-readable output")
    args = parser.parse_args(argv)

    print("=" * 78)
    print("  DRY RUN — no order will be sent")
    print("=" * 78)

    config = load_engine_config()
    creds = read_mt5_credentials()

    try:
        bars = fetch_bars(args.symbol, args.timeframe, args.bars)
    except Exception as exc:   # noqa: BLE001
        print(f"  [FAIL] could not fetch candles: {exc}")
        return 1

    print(f"  symbol      : {args.symbol}")
    print(f"  timeframe   : {args.timeframe}")
    print(f"  bars        : {len(bars)}")
    print(f"  last close  : {bars['close'].iloc[-1]}")
    print(f"  volume col  : "
          f"{'tick_volume' if 'tick_volume' in bars.columns else 'ABSENT'}")
    print()

    # Account state from the live engine database and terminal.
    journal = Journal(config.db_path)
    try:
        tracker = EquityTracker(journal)
        baselines = tracker.current()
        equity = baselines.peak_equity if baselines else None
        start_of_day = baselines.start_of_day_equity if baselines else None
        peak = baselines.peak_equity if baselines else None

        if equity is None:
            print("  [FAIL] no equity baseline on record.")
            print("         Run once to seed it:")
            print("             python -m engine --once")
            return 1

        positions = journal.positions()
        unprotected = sum(1 for p in positions if not p.get("sl"))
        open_risk = None
        if positions:
            print(f"  open positions in journal: {len(positions)}"
                  f" ({unprotected} unprotected)")
            if unprotected:
                print("  [NOTE] an unprotected position blocks all new trades "
                      "by design.")

        specs = {}
        import MetaTrader5 as mt5
        from data_engine import initialize_mt5
        if initialize_mt5():
            info = mt5.symbol_info(args.symbol)
            if info is not None:
                specs[args.symbol] = {
                    "trade_tick_value": getattr(info, "trade_tick_value", None),
                    "trade_tick_size": getattr(info, "trade_tick_size", None),
                }
                print(f"  tick spec   : value={specs[args.symbol]['trade_tick_value']}"
                      f" size={specs[args.symbol]['trade_tick_size']}")

        result = run_dry_run(
            args.symbol,
            bars,
            equity=equity,
            start_of_day_equity=start_of_day,
            peak_equity=peak,
            total_open_risk=open_risk if open_risk is not None else 0.0,
            open_trade_count=len(positions),
            unprotected_positions=unprotected,
            specs=specs,
            volume=args.volume,
            stop_distance_pips=args.stop_pips,
            limits=RiskLimits(
                risk_per_trade_pct=config.risk.risk_per_trade_pct,
                daily_loss_limit_pct=config.risk.daily_loss_limit_pct,
                max_drawdown_pct=config.risk.max_drawdown_pct,
                max_open_trades=config.risk.max_open_trades,
                max_total_exposure_pct=config.risk.max_total_exposure_pct,
            ),
            risk=RiskEngine(journal),
            direction=args.direction,
            require_vwap=not args.no_vwap,
            xai_gate=XAIValidationGate(journal=journal),
            sizer=_build_sizer(args.symbol),
        )
    finally:
        journal.close()

    if args.json:
        import json
        print(json.dumps({
            "symbol": result.symbol,
            "ran": result.ran,
            "direction": result.direction,
            "reason": result.reason,
            "gate_approved": result.gate_approved,
            "risk_amount": result.risk_amount,
            "risk_pct": result.risk_pct,
            "max_volume": result.max_volume,
            "bars_used": result.bars_used,
            "blocks": [
                {"name": b.name, "agrees": b.agrees, "detail": b.detail}
                for b in (result.signal.blocks if result.signal else [])
            ],
        }, indent=2))
    else:
        print(result.report())

    return 0 if result.gate_approved else 1


if __name__ == "__main__":
    sys.exit(main())