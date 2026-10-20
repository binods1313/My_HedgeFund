"""Read-only confluence replay. Does not write the journal or restart the engine.

Walks closed M15 bars through evaluate() once per bar, then scores:
  (a) ribbon fix + current OTE (end-at-close find_swing_leg)
  (b) ribbon fix + Option 1 (last completed swing before the sweep bar)

Lookback window is capped at 300 closed bars. strategy.py OTE is not imported
as a live-path change; Option 1 is applied only inside this harness.

Stop (harness only):
  wick_BUY  = sweep.level - sweep.extreme
  wick_SELL = sweep.level + sweep.extreme
  buffer    = 1 tick (trade_tick_size, else point)
  beyond    = wick - buffer (BUY) / wick + buffer (SELL)
  ATR       = mean true range of the last 14 closed M15 bars in the window
  broker_min = (trade_stops_level + spread_points) * point
  min_dist  = max(1.0 * ATR, broker_min)
  stop      = min(beyond, entry - min_dist)  BUY
            = max(beyond, entry + min_dist)  SELL
  Reject if no sweep, ATR<=0, wick on the wrong side of entry, or
  |entry-stop| < min_dist. TP = entry ± 2 * |entry-stop|.

Forward SL/TP outcomes are indicative only, not a profitability claim.
Never calls mt5.shutdown() so the running PAPER engine keeps its terminal.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from engine.config import load_engine_config
from engine.indicators.structure import (
    SwingLeg,
    find_pivot_highs,
    find_pivot_lows,
    find_swing_leg,
    in_ote_zone,
    latest_sweep,
    select_structural_leg,
)
from engine.strategy import evaluate, ribbon_matches_side

WINDOW = 300
MIN_BARS = WINDOW  # every evaluated bar has a full 300-bar lookback
REQUEST_COUNTS = (300, 1000, 2000, 5000)
RR = 2.0
MIN_ATR_MULT = 3.0
ATR_PERIOD = 14
REQUIRED_NAMES = ("liquidity sweep", "fair value gap", "fibonacci OTE",
                  "ema ribbon", "vwap")


@dataclass(frozen=True)
class BrokerStops:
    """Broker freeze-level inputs for the harness stop floor."""

    point: float
    tick_size: float
    stops_level_points: float
    spread_points: float


def pip_size(point: float, digits: int, symbol: str) -> float:
    """Forex 3/5-digit pip is 10 points. Metals stay on `point`."""
    name = str(symbol or "").upper()
    if name.startswith(("XAU", "XAG")):
        return float(point)
    if int(digits) in (3, 5):
        return float(point) * 10.0
    return float(point)


def sweep_stop(side: str, entry: float, sweep, atr: float,
               spec: BrokerStops, *, atr_k: float = 1.0
               ) -> tuple[float, float, float] | None:
    """Return (stop, distance, min_dist) or None to reject.

    Distance is always |entry - stop|. min_dist = max(atr_k * ATR, broker_min).
    """
    if sweep is None or side not in ("BUY", "SELL"):
        return None
    if str(getattr(sweep, "direction", "")) != side:
        return None
    if atr is None or not np.isfinite(atr) or atr <= 0:
        return None
    if spec is None or spec.point <= 0:
        return None
    tick = spec.tick_size if spec.tick_size > 0 else spec.point
    if tick <= 0:
        return None
    broker_min = max(0.0, float(spec.stops_level_points) + float(spec.spread_points)) * float(spec.point)
    min_dist = max(float(atr) * float(atr_k), broker_min)
    if min_dist <= 0:
        return None
    entry_f = float(entry)
    level = float(sweep.level)
    extreme = float(sweep.extreme)
    if side == "BUY":
        wick = level - extreme
        if wick >= entry_f:
            return None
        beyond = wick - tick
        stop = min(beyond, entry_f - min_dist)
        dist = entry_f - stop
        if stop >= entry_f or dist + 1e-12 < min_dist:
            return None
    else:
        wick = level + extreme
        if wick <= entry_f:
            return None
        beyond = wick + tick
        stop = max(beyond, entry_f + min_dist)
        dist = stop - entry_f
        if stop <= entry_f or dist + 1e-12 < min_dist:
            return None
    return (float(stop), float(dist), float(min_dist))


def _fetch_closed_m15(symbol: str, mt5) -> pd.DataFrame | None:
    """Closed bars only (start_pos=1). Try larger counts; keep the longest."""
    tf = getattr(mt5, "TIMEFRAME_M15", 15)
    mt5.symbol_select(symbol, True)
    best = None
    tried = []
    for count in REQUEST_COUNTS:
        rates = mt5.copy_rates_from_pos(symbol, tf, 1, int(count))
        n = 0 if rates is None else len(rates)
        tried.append((count, n))
        if n <= 0:
            continue
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        if best is None or len(df) > len(best):
            best = df
        if n < count:
            break
    if best is None:
        print(f"  {symbol}: no history (tried {tried})", flush=True)
        return None
    print(f"  {symbol}: {len(best)} closed M15 bars (tried {tried})", flush=True)
    return best


def _atr(df: pd.DataFrame, n: int = ATR_PERIOD) -> float:
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    close = df["close"].to_numpy(dtype=float)
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev), np.abs(low - prev)))
    if len(tr) == 0:
        return 0.0
    if len(tr) < n:
        return float(tr.mean())
    return float(tr[-n:].mean())


def _leg_ok(leg: SwingLeg | None, atr: float) -> SwingLeg | None:
    if leg is None:
        return None
    span = abs(float(leg.end_price) - float(leg.start_price))
    if span <= 0:
        return None
    if atr > 0 and span < MIN_ATR_MULT * atr:
        return None
    return leg


def last_completed_swing(df: pd.DataFrame, lookback: int = 3) -> SwingLeg | None:
    """Pivot-to-pivot swing whose end pivot is confirmed BEFORE the last bar.

    End price is the later confirmed pivot, not the current close. That is what
    lets a sweep on the last bar sit as a retracement of a finished impulse.
    """
    if len(df) < lookback * 2 + 4:
        return None
    work = df.iloc[:-1]
    highs = find_pivot_highs(work, lookback=lookback)
    lows = find_pivot_lows(work, lookback=lookback)
    events = sorted(
        [(i, p, "H") for i, p in highs] + [(i, p, "L") for i, p in lows]
    )
    if len(events) < 2:
        return None
    end = events[-1]
    start = None
    for prev in reversed(events[:-1]):
        if prev[2] != end[2]:
            start = prev
            break
    if start is None:
        return None
    if start[2] == "L":
        direction = "BULLISH"
        inv = float(start[1])
    else:
        direction = "BEARISH"
        inv = float(start[1])
    return SwingLeg(
        start_index=int(start[0]),
        end_index=int(end[0]),
        start_price=float(start[1]),
        end_price=float(end[1]),
        direction=direction,
        invalidation=inv,
    )


def ote_current(df: pd.DataFrame) -> SwingLeg | None:
    return find_swing_leg(df, lookback=3)


def ote_pre_sweep(df: pd.DataFrame) -> SwingLeg | None:
    atr = _atr(df)
    return _leg_ok(last_completed_swing(df, lookback=3), atr)


def ote_structural(df: pd.DataFrame) -> SwingLeg | None:
    sweep = latest_sweep(df, lookback=20)
    atr = _atr(df)
    lookback_bars = min(80, max(20, len(df) - 2))
    return _leg_ok(
        select_structural_leg(df, sweep=sweep, lookback_bars=lookback_bars, atr=atr),
        atr,
    )


def _resample_h1(df: pd.DataFrame) -> pd.DataFrame | None:
    if "time" not in df.columns or len(df) < 80:
        return None
    work = df.set_index("time")
    h1 = work.resample("1h").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        n=("close", "size"),
    ).dropna()
    return None if h1.empty else h1


def ote_htf_h1_from_h1(h1: pd.DataFrame | None, stamp) -> SwingLeg | None:
    if h1 is None or h1.empty:
        return None
    cut = h1.loc[:stamp]
    if cut.empty:
        return None
    if int(cut["n"].iloc[-1]) < 4:
        cut = cut.iloc[:-1]
    if len(cut) < 30:
        return None
    frame = cut.reset_index()
    atr = _atr(frame)
    return _leg_ok(last_completed_swing(frame, lookback=3), atr)


OTE_FNS = {
    "current": ote_current,
    "pre_sweep": ote_pre_sweep,
    "structural": ote_structural,
}

VARIANTS = (
    ("a_ribbon_fix_current_ote", "fixed", "current"),
    ("b_ribbon_fix_ote_option1", "fixed", "pre_sweep"),
)


def _forward(df: pd.DataFrame, i: int, side: str, sl: float, tp: float) -> str:
    """Walk later CLOSED bars. Same-bar SL+TP = adverse. Indicative only."""
    if i + 1 >= len(df):
        return "no_forward_bar"
    for j in range(i + 1, len(df)):
        low = float(df["low"].iloc[j])
        high = float(df["high"].iloc[j])
        if side == "BUY":
            hit_sl = low <= sl
            hit_tp = high >= tp
        else:
            hit_sl = high >= sl
            hit_tp = low <= tp
        if hit_sl and hit_tp:
            return "ambiguous_adverse"
        if hit_sl:
            return "sl_first"
        if hit_tp:
            return "tp_first"
    return "open"


def _block_map(signal) -> dict[str, bool]:
    return {str(b.name): bool(b.agrees) for b in signal.blocks}


def _agree_count(base: dict[str, bool], *, ribbon_ok: bool, ote_ok: bool) -> int:
    n = 0
    for name, agrees in base.items():
        if name == "ema ribbon":
            if ribbon_ok:
                n += 1
        elif name == "fibonacci OTE":
            if ote_ok:
                n += 1
        elif agrees:
            n += 1
    return n


def _all_required(base: dict[str, bool], *, ribbon_ok: bool, ote_ok: bool) -> bool:
    for name in REQUIRED_NAMES:
        if name == "ema ribbon":
            if not ribbon_ok:
                return False
        elif name == "fibonacci OTE":
            if not ote_ok:
                return False
        else:
            if not base.get(name, False):
                return False
    for name, agrees in base.items():
        if name in REQUIRED_NAMES or name in ("ema ribbon", "fibonacci OTE"):
            continue
        if not agrees:
            return False
    return True


def replay_symbol(symbol: str, df: pd.DataFrame, spec: BrokerStops,
                  pip: float) -> dict:
    """One evaluate() per closed bar; score both variants from that result."""
    buckets = {
        name: {
            "n_eval": 0, "n3": 0, "n4": 0, "n5": 0, "n_signal": 0,
            "buy": 0, "sell": 0, "rejected_stop": 0,
            "by_day": defaultdict(int),
            "outcomes": defaultdict(int), "r_sum": 0.0, "r_n": 0,
            "examples": [], "trades": [],
        }
        for name, _, _ in VARIANTS
    }
    n_bars = len(df)
    t_sym = time.time()
    start_i = WINDOW - 1
    n_plan = max(n_bars - start_i, 0)
    for i in range(start_i, n_bars):
        window = df.iloc[i + 1 - WINDOW:i + 1]
        try:
            signal = evaluate(symbol, window)
        except Exception:
            continue
        base = _block_map(signal)
        wanted = signal.direction or (signal.sweep.direction if signal.sweep else None)
        ribbon_dir = signal.ribbon.direction if signal.ribbon is not None else ""
        ribbon_fixed = ribbon_matches_side(wanted, ribbon_dir)
        price = float(signal.price)
        atr = _atr(window)
        ote_ok = {"current": bool(base.get("fibonacci OTE", False))}
        try:
            leg_ps = ote_pre_sweep(window)
            ote_ok["pre_sweep"] = bool(leg_ps is not None and in_ote_zone(price, leg_ps))
        except Exception:
            ote_ok["pre_sweep"] = False

        stamp = window["time"].iloc[-1]
        day = str(pd.Timestamp(stamp).date())
        placed = sweep_stop(wanted, price, signal.sweep, atr, spec, atr_k=1.0) if wanted else None
        placed_k = {
            1.0: placed,
            0.75: (sweep_stop(wanted, price, signal.sweep, atr, spec, atr_k=0.75)
                   if wanted else None),
            1.5: (sweep_stop(wanted, price, signal.sweep, atr, spec, atr_k=1.5)
                  if wanted else None),
        }

        for name, _ribbon_mode, ote_key in VARIANTS:
            rec = buckets[name]
            rec["n_eval"] += 1
            ribbon_ok = ribbon_fixed
            agree = _agree_count(base, ribbon_ok=ribbon_ok, ote_ok=ote_ok[ote_key])
            if agree == 3:
                rec["n3"] += 1
            elif agree == 4:
                rec["n4"] += 1
            elif agree >= 5:
                rec["n5"] += 1
            if not wanted:
                continue
            if not _all_required(base, ribbon_ok=ribbon_ok, ote_ok=ote_ok[ote_key]):
                continue
            if placed is None:
                rec["rejected_stop"] += 1
                continue
            sl_f, dist, min_dist = placed
            rec["n_signal"] += 1
            if wanted == "BUY":
                rec["buy"] += 1
            else:
                rec["sell"] += 1
            rec["by_day"][day] += 1
            tp = price + dist * RR if wanted == "BUY" else price - dist * RR
            outcome = _forward(df, i, wanted, sl_f, tp)
            rec["outcomes"][outcome] += 1
            r_val = None
            if outcome == "tp_first":
                rec["r_sum"] += RR
                rec["r_n"] += 1
                r_val = RR
            elif outcome in ("sl_first", "ambiguous_adverse"):
                rec["r_sum"] += -1.0
                rec["r_n"] += 1
                r_val = -1.0
            rec["trades"].append({
                "symbol": symbol, "ts": str(stamp), "direction": wanted,
                "entry": price, "sl": sl_f, "tp": tp, "dist": dist,
                "atr": atr, "outcome": outcome, "r": r_val,
                "spread_points": spec.spread_points, "point": spec.point,
            })
            if len(rec["examples"]) < 5:
                blocks = []
                for b in signal.blocks:
                    agrees = b.agrees
                    if b.name == "ema ribbon":
                        agrees = ribbon_ok
                    elif b.name == "fibonacci OTE":
                        agrees = ote_ok[ote_key]
                    blocks.append({
                        "name": b.name, "agrees": agrees, "detail": b.detail,
                    })
                rec["examples"].append({
                    "symbol": symbol,
                    "ts": str(stamp),
                    "direction": wanted,
                    "entry": price,
                    "sl": sl_f,
                    "tp": tp,
                    "rr": RR,
                    "stop_distance": dist,
                    "stop_pips": round(dist / pip, 2) if pip > 0 else None,
                    "stop_atr_mult": round(dist / atr, 3) if atr > 0 else None,
                    "min_dist": min_dist,
                    "atr": atr,
                    "agree": agree,
                    "outcome_indicative": outcome,
                    "blocks": blocks,
                })
        opt1 = buckets["b_ribbon_fix_ote_option1"]
        opt1.setdefault("sens", {0.75: defaultdict(float), 1.5: defaultdict(float)})
        if (wanted and ribbon_fixed and ote_ok.get("pre_sweep")
                and _all_required(base, ribbon_ok=True, ote_ok=True)):
            for k in (0.75, 1.5):
                srec = opt1["sens"][k]
                pk = placed_k[k]
                if pk is None:
                    srec["rejected"] += 1
                    continue
                slk, distk, _md = pk
                tpk = price + distk * RR if wanted == "BUY" else price - distk * RR
                oc = _forward(df, i, wanted, slk, tpk)
                srec["n"] += 1
                srec[oc] += 1
                if oc == "tp_first":
                    srec["r_sum"] += RR
                    srec["r_n"] += 1
                elif oc in ("sl_first", "ambiguous_adverse"):
                    srec["r_sum"] += -1.0
                    srec["r_n"] += 1

        done = i - start_i + 1
        if done == 1 or done % 50 == 0 or done == n_plan:
            print(
                f"    {symbol} {i + 1}/{n_bars}  eval {done}/{n_plan}  "
                f"elapsed {time.time() - t_sym:.1f}s",
                flush=True,
            )

    out = {}
    for name, rec in buckets.items():
        span_days = 1.0 / 96.0
        if "time" in df.columns and rec["n_eval"]:
            t0 = pd.Timestamp(df["time"].iloc[start_i])
            t1 = pd.Timestamp(df["time"].iloc[-1])
            span_days = max((t1 - t0).total_seconds() / 86400.0, 1.0 / 96.0)
        per_week = (rec["n_signal"] / span_days) * 5.0
        oc = dict(rec["outcomes"])
        resolved = (oc.get("tp_first", 0) + oc.get("sl_first", 0)
                    + oc.get("ambiguous_adverse", 0))
        total_out = resolved + oc.get("open", 0)
        amb = oc.get("ambiguous_adverse", 0)
        out[name] = {
            "bars": n_bars,
            "evaluated": rec["n_eval"],
            "n3": rec["n3"],
            "n4": rec["n4"],
            "n5": rec["n5"],
            "signals": rec["n_signal"],
            "rejected_stop": rec["rejected_stop"],
            "buy": rec["buy"],
            "sell": rec["sell"],
            "signals_per_week_est": round(per_week, 3),
            "span_days": round(span_days, 2),
            "days_with_signal": len(rec["by_day"]),
            "outcomes_indicative": oc,
            "ambiguous_share_of_signals": round(amb / rec["n_signal"], 4) if rec["n_signal"] else None,
            "ambiguous_share_of_resolved": round(amb / resolved, 4) if resolved else None,
            "average_R_resolved": round(rec["r_sum"] / rec["r_n"], 4) if rec["r_n"] else None,
            "examples": rec["examples"],
            "trades": rec["trades"],
            "sens": {str(k): dict(v) for k, v in rec.get("sens", {}).items()},
        }
    print(f"  {symbol} done in {time.time() - t_sym:.1f}s", flush=True)
    return out


def _summarise(frames: dict[str, pd.DataFrame], per_symbol: dict) -> dict:
    report = {}
    for name, _, _ in VARIANTS:
        tot = defaultdict(int)
        mix_buy = mix_sell = 0
        out_sum: dict[str, int] = defaultdict(int)
        examples: list[dict] = []
        week_rates = []
        r_weighted = 0.0
        r_weight = 0
        rows = {}
        for symbol in frames:
            row = per_symbol[symbol][name]
            rows[symbol] = {
                "bars": row["bars"],
                "evaluated": row["evaluated"],
                "n3": row["n3"],
                "n4": row["n4"],
                "n5": row["n5"],
                "signals": row["signals"],
                "rejected_stop": row["rejected_stop"],
                "buy": row["buy"],
                "sell": row["sell"],
                "signals_per_week_est": row["signals_per_week_est"],
                "outcomes_indicative": row["outcomes_indicative"],
                "average_R_resolved": row["average_R_resolved"],
            }
            tot["bars"] += row["bars"]
            tot["evaluated"] += row["evaluated"]
            tot["n3"] += row["n3"]
            tot["n4"] += row["n4"]
            tot["n5"] += row["n5"]
            tot["signals"] += row["signals"]
            tot["rejected_stop"] += row["rejected_stop"]
            mix_buy += row["buy"]
            mix_sell += row["sell"]
            for k, v in row["outcomes_indicative"].items():
                out_sum[k] += v
            week_rates.append(row["signals_per_week_est"])
            oc = row["outcomes_indicative"]
            resolved_n = (oc.get("tp_first", 0) + oc.get("sl_first", 0)
                          + oc.get("ambiguous_adverse", 0))
            if row["average_R_resolved"] is not None and resolved_n:
                r_weighted += row["average_R_resolved"] * resolved_n
                r_weight += resolved_n
            for ex in row["examples"]:
                if len(examples) < 5:
                    examples.append(ex)
        mean_week = sum(week_rates) / len(week_rates) if week_rates else 0.0
        amb = out_sum.get("ambiguous_adverse", 0)
        n_sig = tot["signals"]
        resolved = (out_sum.get("tp_first", 0) + out_sum.get("sl_first", 0) + amb)
        report[name] = {
            "totals": dict(tot),
            "direction_mix": {"BUY": mix_buy, "SELL": mix_sell},
            "mean_signals_per_symbol_per_week_est": round(mean_week, 3),
            "outcomes_indicative_not_pnl": dict(out_sum),
            "ambiguous_share_of_signals": round(amb / n_sig, 4) if n_sig else None,
            "ambiguous_share_of_resolved": round(amb / resolved, 4) if resolved else None,
            "average_R_resolved": round(r_weighted / r_weight, 4) if r_weight else None,
            "per_symbol": rows,
            "examples": examples,
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="", help="comma list; default config whitelist")
    parser.add_argument(
        "--out",
        default=str(Path.home() / "AppData" / "Local" / "Temp" / "hf_backtest_confluence.json"),
    )
    args = parser.parse_args()
    cfg = load_engine_config()
    symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else list(cfg.scan_symbols))

    import MetaTrader5 as mt5
    info = mt5.terminal_info()
    if info is None:
        if not mt5.initialize():
            print("MT5 initialize failed; cannot fetch history. Engine not stopped.",
                  flush=True)
            return 1
        print("MT5 initialize() attached (will not shutdown)", flush=True)
    else:
        print("MT5 terminal already attached; no shutdown", flush=True)

    print("fetching closed M15 (start_pos=1), no forming bar; lookback cap 300",
          flush=True)
    frames: dict[str, pd.DataFrame] = {}
    specs: dict[str, BrokerStops] = {}
    pips: dict[str, float] = {}
    fetch_log = {}
    for symbol in symbols:
        df = _fetch_closed_m15(symbol, mt5)
        fetch_log[symbol] = 0 if df is None else len(df)
        if df is not None and len(df) >= MIN_BARS:
            frames[symbol] = df
        elif df is not None:
            print(f"  {symbol}: skip, {len(df)} < {MIN_BARS}", flush=True)
        info = mt5.symbol_info(symbol)
        tick = mt5.symbol_info_tick(symbol)
        if info is None:
            print(f"  {symbol}: no symbol_info; stops will reject", flush=True)
            specs[symbol] = BrokerStops(0.0, 0.0, 0.0, 0.0)
            pips[symbol] = 0.0
            continue
        point = float(getattr(info, "point", 0.0) or 0.0)
        tick_size = float(getattr(info, "trade_tick_size", 0.0) or 0.0) or point
        stops_level = float(getattr(info, "trade_stops_level", 0) or 0)
        spread_pts = float(getattr(tick, "spread", 0) or 0) if tick is not None else 0.0
        if spread_pts <= 0 and tick is not None and point > 0:
            bid = float(getattr(tick, "bid", 0) or 0)
            ask = float(getattr(tick, "ask", 0) or 0)
            if ask > bid > 0:
                spread_pts = (ask - bid) / point
        digits = int(getattr(info, "digits", 5) or 5)
        specs[symbol] = BrokerStops(point, tick_size, stops_level, spread_pts)
        pips[symbol] = pip_size(point, digits, symbol)
        print(
            f"  {symbol} spec point={point} tick={tick_size} "
            f"stops_level={stops_level} spread_pts={spread_pts} "
            f"pip={pips[symbol]}",
            flush=True,
        )

    print(f"\n===== BACKTEST {len(frames)} symbols, lookback={WINDOW}, closed bars =====",
          flush=True)
    t0 = time.time()
    per_symbol = {}
    for symbol, df in frames.items():
        per_symbol[symbol] = replay_symbol(
            symbol, df, specs[symbol], pips.get(symbol) or 0.0,
        )
        Path(args.out).write_text(
            json.dumps({"fetch": fetch_log, "partial": True, "per_symbol": per_symbol},
                       default=str),
            encoding="utf-8",
        )

    report = _summarise(frames, per_symbol)
    payload = {
        "fetch_closed_m15_counts": fetch_log,
        "window": WINDOW,
        "min_bars": MIN_BARS,
        "rr": RR,
        "elapsed_s": round(time.time() - t0, 1),
        "variants": report,
        "note": (
            "INDICATIVE ONLY: SL beyond sweep wick, floor max(1*ATR14, broker min); "
            "TP at 2R. Not a profitability claim. Live evaluate() unchanged until restart."
        ),
    }
    Path(args.out).write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    print("\n===== TOTALS =====", flush=True)
    for name, _, ote in VARIANTS:
        row = report[name]
        print(f"\n--- {name} ote={ote} ---", flush=True)
        print(json.dumps({
            "totals": row["totals"],
            "direction_mix": row["direction_mix"],
            "mean_signals_per_symbol_per_week_est": row["mean_signals_per_symbol_per_week_est"],
            "outcomes_indicative_not_pnl": row["outcomes_indicative_not_pnl"],
            "ambiguous_share_of_signals": row["ambiguous_share_of_signals"],
            "ambiguous_share_of_resolved": row["ambiguous_share_of_resolved"],
            "average_R_resolved": row["average_R_resolved"],
            "per_symbol": row["per_symbol"],
        }, default=str), flush=True)
        print("examples", json.dumps(row["examples"], default=str), flush=True)

    print(f"\nelapsed_s {payload['elapsed_s']}", flush=True)
    extras = _extras_report(per_symbol)
    payload["extras_indicative"] = extras
    Path(args.out).write_text(json.dumps(payload, default=str, indent=2), encoding="utf-8")
    print("\n===== EXTRAS INDICATIVE =====", flush=True)
    print(json.dumps(extras, default=str, indent=2), flush=True)
    print(payload["note"], flush=True)
    print(f"wrote {args.out}", flush=True)
    return 0


def _spread_price(symbol: str, spread_points: float, point: float) -> tuple[float, str]:
    pts = float(spread_points or 0.0)
    source = "live_tick_spread"
    if pts <= 0:
        source = "asset_class_estimate"
        name = str(symbol).upper()
        if name.startswith("XAU"):
            pts = 25.0
        elif name.startswith("XAG"):
            pts = 10.0
        elif "JPY" in name:
            pts = 20.0
        else:
            pts = 10.0
    return pts * float(point), source


def _extras_report(per_symbol: dict) -> dict:
    trades = []
    for _sym, variants in per_symbol.items():
        trades.extend(variants.get("b_ribbon_fix_ote_option1", {}).get("trades") or [])
    trades.sort(key=lambda t: str(t.get("ts") or ""))
    resolved = [t for t in trades if t.get("r") is not None]
    r_vals = [float(t["r"]) for t in resolved]
    n = len(r_vals)
    avg = sum(r_vals) / n if n else None
    se = (math.sqrt(sum((x - avg) ** 2 for x in r_vals) / (n - 1)) / math.sqrt(n)
          if n > 1 else None)
    net = []
    sources = defaultdict(int)
    for t in resolved:
        spread_px, src = _spread_price(t["symbol"], t.get("spread_points") or 0, t.get("point") or 1e-5)
        sources[src] += 1
        dist = float(t["dist"] or 0)
        cost_r = (spread_px / dist) if dist > 0 else 0.0
        net.append(float(t["r"]) - cost_r)
    avg_net = sum(net) / len(net) if net else None
    stamps = [pd.Timestamp(t["ts"]) for t in trades if t.get("ts")]
    weeks = None
    if len(stamps) >= 2:
        weeks = (max(stamps) - min(stamps)).total_seconds() / (7 * 86400)
    split = None
    if stamps:
        ordered = sorted(trades, key=lambda t: str(t.get("ts") or ""))
        cut = max(1, int(len(ordered) * 0.6))
        def _wr(chunk):
            rs = [c for c in chunk if c.get("r") is not None]
            if not rs:
                return {"n": 0, "avg_R": None, "win_rate": None}
            wins = sum(1 for c in rs if c["outcome"] == "tp_first")
            return {
                "n": len(rs),
                "avg_R": round(sum(c["r"] for c in rs) / len(rs), 4),
                "win_rate": round(wins / len(rs), 4),
            }
        split = {"first_60": _wr(ordered[:cut]), "last_40": _wr(ordered[cut:])}
    per_sym = {}
    for t in trades:
        rec = per_sym.setdefault(t["symbol"], {"signals": 0, "tp": 0, "sl": 0, "r_sum": 0.0, "r_n": 0})
        rec["signals"] += 1
        if t.get("outcome") == "tp_first":
            rec["tp"] += 1
        elif t.get("outcome") == "sl_first":
            rec["sl"] += 1
        if t.get("r") is not None:
            rec["r_sum"] += t["r"]
            rec["r_n"] += 1
    per_sym_out = {
        s: {
            "signals": v["signals"], "tp": v["tp"], "sl": v["sl"],
            "avg_R": round(v["r_sum"] / v["r_n"], 4) if v["r_n"] else None,
        }
        for s, v in sorted(per_sym.items())
    }
    rng = random.Random(1)
    random_five = rng.sample(trades, k=min(5, len(trades))) if trades else []
    first_five = trades[:5]
    sens = {}
    for _sym, variants in per_symbol.items():
        block = variants.get("b_ribbon_fix_ote_option1", {}).get("sens") or {}
        for k, rec in block.items():
            acc = sens.setdefault(str(k), {"n": 0, "tp_first": 0, "sl_first": 0,
                                           "r_sum": 0.0, "r_n": 0, "rejected": 0})
            acc["n"] += rec.get("n", 0)
            acc["tp_first"] += rec.get("tp_first", 0)
            acc["sl_first"] += rec.get("sl_first", 0)
            acc["r_sum"] += rec.get("r_sum", 0)
            acc["r_n"] += rec.get("r_n", 0)
            acc["rejected"] += rec.get("rejected", 0)
    for k, acc in sens.items():
        acc["avg_R"] = round(acc["r_sum"] / acc["r_n"], 4) if acc["r_n"] else None
        acc["win_rate"] = round(acc["tp_first"] / acc["r_n"], 4) if acc["r_n"] else None
    return {
        "spread": {
            "rule": "live tick.spread points * point; if 0, asset-class estimate "
                    "FX 10pts, JPY 20pts, XAU 25pts, XAG 10pts",
            "sources": dict(sources),
            "avg_R_before_cost": None if avg is None else round(avg, 4),
            "avg_R_after_cost": None if avg_net is None else round(avg_net, 4),
        },
        "sample_weeks": None if weeks is None else round(weeks, 2),
        "n_resolved": n,
        "se_avg_R": None if se is None else round(se, 4),
        "in_sample_out_of_sample": split,
        "per_symbol": per_sym_out,
        "sensitivity_atr_k": sens,
        "first_five": first_five,
        "random_five_seed_1": random_five,
    }


if __name__ == "__main__":
    raise SystemExit(main())
