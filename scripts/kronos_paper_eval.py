"""Kronos paper scorecard: does the model have directional edge on MT5 bars?

Read-only. For each symbol/timeframe it slides a context window over the bars
and asks Kronos for a forecast, then compares the predicted direction with what
actually happened over the same horizon. Output is a hit rate vs the base rate
(share of UP moves) — the honest test of whether Kronos adds information before
we ever let it near a live signal.

Two modes:
  --local   run inside the Kronos venv (D:/skills/Kronos/.venv) importing the
            model directly — no HTTP, deterministic, fastest. Use for scoring.
  (default) HTTP mode against the running Kronos server
            (D:/skills/Kronos/kronos_server.py) — the path the engine will use.

Usage:
    D:/skills/Kronos/.venv/Scripts/python.exe -m scripts.kronos_paper_eval \
        --symbol EURUSD --timeframe M15 --local --max-windows 10
    .venv\\Scripts\\python.exe -m scripts.kronos_paper_eval \
        --symbol EURUSD --timeframe M15 --max-windows 10
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIN_CONTEXT = 64
MIN_MOVE = 1e-9

# --- local mode imports (only work inside the Kronos venv) ---------------
try:  # pragma: no cover
    import torch  # noqa: F401
    sys.path.insert(0, r"D:/skills/Kronos")
    from kronos_server import KronosApp, load_models
    LOCAL = True
except Exception:  # noqa: BLE001
    LOCAL = False

if not LOCAL:
    from engine.research.kronos_client import KronosClient  # noqa: E402


def load_bars(symbol: str, timeframe: str) -> Optional[list[dict]]:
    path = ROOT / "data" / "market_bars" / f"{symbol}__{timeframe}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("rows") or data.get("bars")
    try:
        import MetaTrader5 as mt5
        from datetime import datetime, timezone
        tf = getattr(mt5, f"TIMEFRAME_{timeframe}", mt5.TIMEFRAME_M15)
        if mt5.initialize():
            rates = mt5.copy_rates_from_pos(symbol, tf, 0, 2000)
            mt5.shutdown()
            if rates is not None and len(rates):
                return [
                    {
                        "time": datetime.fromtimestamp(int(r["time"]), tz=timezone.utc).isoformat(),
                        "open": float(r["open"]), "high": float(r["high"]),
                        "low": float(r["low"]), "close": float(r["close"]),
                        "volume": float(r["tick_volume"]),
                    }
                    for r in rates
                ]
    except Exception:  # noqa: BLE001
        pass
    return None


def build_payload(bars: list[dict], pred_len: int, samples: int) -> dict:
    return {
        "symbol": "?",
        "bars": bars,
        "pred_len": int(pred_len),
        "sample_count": int(samples),
        "top_k": 1,
        "top_p": 1.0,
    }


def evaluate(forecast_fn, bars: list[dict], *,
             context: int, pred_len: int, samples: int, step: int,
             max_windows: int) -> dict:
    n = len(bars)
    windows = []
    k = context
    while k + pred_len <= n and len(windows) < max_windows:
        windows.append((k, bars[k - context:k], bars[k + pred_len - 1]))
        k += step

    results = []
    attempted = 0
    none_failures = 0
    flat_moves = 0
    for k, ctx_bars, future_last in windows:
        attempted += 1
        fc = forecast_fn(build_payload(ctx_bars, pred_len, samples))
        if fc is None:
            none_failures += 1
            continue
        last_close = float(ctx_bars[-1]["close"])
        actual_move = float(future_last["close"]) - last_close
        if abs(actual_move) <= MIN_MOVE:
            flat_moves += 1
            continue
        actual_dir = "UP" if actual_move > 0 else "DOWN"
        results.append({
            "k": k,
            "t_from": str(ctx_bars[0].get("time")),
            "t_to": str(future_last.get("time")),
            "pred_dir": fc.get("direction"),
            "actual_dir": actual_dir,
            "hit": fc.get("direction") == actual_dir,
            "agreement": fc.get("agreement"),
            "expected_move_pct": fc.get("expected_move_pct"),
            "actual_move_pct": actual_move / last_close * 100.0,
        })

    base = {"n_windows": 0, "attempted": attempted,
            "n_forecast_none": none_failures, "n_flat_moves": flat_moves,
            "note": "no usable windows"}
    if not results:
        return base

    hits = sum(1 for r in results if r["hit"])
    up = sum(1 for r in results if r["actual_dir"] == "UP")
    base_up = up / len(results)
    acc = hits / len(results)
    return {
        "n_windows": len(results),
        "attempted": attempted,
        "n_forecast_none": none_failures,
        "n_flat_moves": flat_moves,
        "accuracy": round(acc, 4),
        "base_rate_up": round(base_up, 4),
        "edge_over_base": round(acc - max(base_up, 1 - base_up), 4),
        "agreement_mean": round(sum(r["agreement"] or 0 for r in results) / len(results), 4),
        "mean_abs_expected_move_pct": round(
            sum(abs(r["expected_move_pct"] or 0) for r in results) / len(results), 4),
        "hit_misses": f"{hits}/{len(results)}",
        "date_from": min(r["t_from"] for r in results),
        "date_to": max(r["t_to"] for r in results),
        "windows": results,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol")
    ap.add_argument("--timeframe", default="M15")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--context", type=int, default=256)
    ap.add_argument("--pred-len", type=int, default=8)
    ap.add_argument("--samples", type=int, default=8)
    ap.add_argument("--step", type=int, default=96)
    ap.add_argument("--max-windows", type=int, default=20)
    ap.add_argument("--endpoint", default="http://127.0.0.1:8610")
    ap.add_argument("--local", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", help="write machine-readable JSON to this path")
    args = ap.parse_args()

    if args.local and not LOCAL:
        print("ERROR: --local requires running under the Kronos venv "
              "(D:/skills/Kronos/.venv/Scripts/python.exe)", file=sys.stderr)
        return 2

    if args.local:
        print("loading Kronos models (local mode)...", file=sys.stderr)
        tokenizer, model, predictor = load_models()
        app = KronosApp(tokenizer, model, predictor)
        forecast_fn = app.forecast
    else:
        client = KronosClient(args.endpoint)
        if not client.health():
            print("ERROR: Kronos server not reachable. Start it with:\n"
                  "  cd D:/skills/Kronos && .venv/Scripts/python.exe kronos_server.py",
                  file=sys.stderr)
            return 2
        forecast_fn = lambda payload: client.forecast(  # noqa: E731
            payload["symbol"], payload["bars"], pred_len=payload["pred_len"],
            sample_count=payload["sample_count"], strict=False)

    if args.all:
        symbols = [p.stem.split("__")[0] for p in (ROOT / "data" / "market_bars").glob("*.json")]
        symbols = sorted(set(symbols))
    elif args.symbol:
        symbols = [args.symbol]
    else:
        ap.error("specify --symbol or --all")

    out = {"mode": "local" if args.local else "http", "timeframe": args.timeframe,
           "context": args.context, "pred_len": args.pred_len,
           "samples": args.samples, "step": args.step,
           "generated_at": json.dumps(__import__("datetime").datetime.now(
               __import__("datetime").timezone.utc).isoformat()),
           "symbols": {}}
    for symbol in symbols:
        bars = load_bars(symbol, args.timeframe)
        if not bars:
            out["symbols"][symbol] = {"error": f"no data for {symbol} {args.timeframe}"}
            continue
        if len(bars) < MIN_CONTEXT + args.pred_len:
            out["symbols"][symbol] = {"error": f"only {len(bars)} bars; need >= {MIN_CONTEXT + args.pred_len}"}
            continue
        out["symbols"][symbol] = evaluate(
            forecast_fn, bars, context=args.context, pred_len=args.pred_len,
            samples=args.samples, step=args.step, max_windows=args.max_windows)

    if args.json:
        print(json.dumps(out, indent=2, default=str))
    elif args.out:
        path = Path(args.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
        print(f"wrote {path}")
    else:
        for symbol, r in out["symbols"].items():
            if "error" in r:
                print(f"{symbol}: {r['error']}")
                continue
            print(f"\n{symbol} {args.timeframe}: {r['n_windows']} windows "
                  f"({r['attempted']} attempted)")
            if r["n_windows"]:
                print(f"  accuracy {r['accuracy']*100:.1f}%  ({r['hit_misses']})  "
                      f"(base rate UP {r['base_rate_up']*100:.1f}%)")
                print(f"  edge over base {r['edge_over_base']*100:+.1f}pp  "
                      f"mean agreement {r['agreement_mean']*100:.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
