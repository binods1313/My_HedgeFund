#!/usr/bin/env python
r"""Forward-test scorer: scores live journal signals against historical bars.

Read-only — no journal writes. Scores each production signal (is_test=0) by:
1. Fetching M15 bar data from the signal's bar_ts onward (bar_ts is the M15
   closed-bar open in MetaTrader server time — UTC+3 broker in this deployment;
   the signal ts field is UTC and is used for cutoffs, grouping, and display).
2. Determining whether TP or SL was hit first (or still open).
3. Subtracting spread cost (matching backtest_confluence.py cost model).
4. Grouping results by week and by symbol.

Usage:
    .venv\Scripts\python -m scripts.forward_test_scorer --db data\engine_5057151780.db --weeks 26 --verbose
    .venv\Scripts\python -m scripts.forward_test_scorer --db data\engine_5057151780.db --json
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from quote_clock import detect_broker_offset_seconds

logger = logging.getLogger("forward_test_scorer")

DEFAULT_WEEKS = 26
DEFAULT_MAX_BARS = 500

BarList = list[tuple[int, float, float, float, float]]
BarProvider = Callable[[sqlite3.Connection, str, int, int], BarList]
SpreadProvider = Callable[[str], float]
PointProvider = Callable[[str], float]


def _print_epoch_example() -> dict:
    """Print raw epoch 1792618200 -> broker -> UTC -> IST.

    This settles the broker offset question: epoch 1792618200 is
    2026-10-21T21:30:00 on a UTC+3 broker clock, which equals
    2026-10-21T18:30:00 UTC, which is 2026-10-22T00:00:00 IST.
    The signal ts event at 18:45:45 UTC follows the bar close — not an error.
    """
    epoch = 1792618200
    utc_now_for_detection = epoch - 10800
    offset = detect_broker_offset_seconds([epoch], utc_now_for_detection)
    broker_dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    utc_dt = datetime.fromtimestamp(epoch - (offset or 0), tz=timezone.utc)
    ist_offset = 5.5 * 3600
    ist_dt = datetime.fromtimestamp(epoch - (offset or 0) + ist_offset, tz=timezone.utc)
    result = {
        "broker_epoch": epoch,
        "offset_seconds": offset,
        "broker_time": broker_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "utc_time": utc_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "ist_time": ist_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"),
    }
    return result


@dataclass
class ScoredSignal:
    signal_id: int
    symbol: str
    direction: str
    entry_price: float
    stop_loss: float
    take_profit: float
    bar_ts: int
    score: float
    bar_time: str
    signal_ts: str
    outcome: str
    gross_r: float
    spread_r: float
    net_r: float
    hit_bars: int


def asset_class_spread_points(symbol: str) -> float:
    """Fallback spread estimates matching backtest_confluence.py _spread_price."""
    s = symbol.upper()
    if s.startswith(("XAU", "GOLD")):
        return 25.0
    if s.startswith("XAG"):
        return 10.0
    if "JPY" in s:
        return 20.0
    return 10.0


def default_point_size(symbol: str) -> float:
    """Infer point size from symbol name."""
    s = symbol.upper()
    if "JPY" in s or s.endswith("JPY"):
        return 0.01
    return 0.0001


def default_spread(symbol: str) -> float:
    """Try live MT5 tick info, fall back to asset class estimate."""
    try:
        import MetaTrader5 as mt5
        if not mt5.initialize():
            return asset_class_spread_points(symbol)
        info = mt5.symbol_info(symbol)
        if info and info.get("trade_tick_size", 0) > 0:
            spread = info.get("trade_stored_spread", 0) or info.get("spread", 0)
            if spread > 0:
                mt5.shutdown()
                return spread / info["trade_tick_size"]
        mt5.shutdown()
    except Exception:
        pass
    return asset_class_spread_points(symbol)


def _fetch_bars_from_table(
    conn: sqlite3.Connection,
    symbol: str,
    start_ts: int,
    max_bars: int = DEFAULT_MAX_BARS,
) -> BarList:
    """Fetch M15 bars from the journal's bar data table.

    ``start_ts`` is the signal's ``bar_ts`` — an MT5 broker-server epoch
    (UTC+3 in this deployment). The signal bar itself has already closed
    when the signal is emitted, so bars with ``ts > start_ts`` are fetched
    (strictly after the signal bar). Bar rows in the table are also stored
    as broker-server epochs, so this is a same-clock comparison.

    Returns list of (timestamp, open, high, low, close) sorted ascending.
    Falls back to empty list if no bars table exists.
    """
    table = f"bars_{symbol.upper()}"
    try:
        cur = conn.execute(
            f"SELECT ts, open, high, low, close FROM {table} WHERE ts > ? ORDER BY ts LIMIT ?",
            (start_ts, max_bars),
        )
        return cur.fetchall()
    except sqlite3.OperationalError:
        try:
            cur = conn.execute(
                "SELECT ts, open, high, low, close FROM bars WHERE symbol=? AND ts > ? ORDER BY ts LIMIT ?",
                (symbol, start_ts, max_bars),
            )
            return cur.fetchall()
        except sqlite3.OperationalError:
            logger.warning("No bars table found for symbol %s; using empty bars", symbol)
            return []


def score_bar_hit(
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    bar: tuple[int, float, float, float, float],
) -> Optional[str]:
    """Check if a single bar hits TP or SL. Returns 'TP', 'SL', or None.

    Same-bar TP+SL is counted as ADVERSE (SL), since the intra-bar price
    order is unknown and the conservative outcome is the stop loss.
    """
    _, _, high, low, _ = bar
    if direction == "BUY":
        if stop_loss > 0 and low <= stop_loss:
            return "SL"
        if take_profit > 0 and high >= take_profit:
            return "TP"
    else:
        if stop_loss > 0 and high >= stop_loss:
            return "SL"
        if take_profit > 0 and low <= take_profit:
            return "TP"
    return None


def _format_signal_ts(ts: str | None) -> str:
    """Render a signal's UTC ``ts`` field for display.

    The ``ts`` column is an ISO-8601 UTC string from the engine — the correct
    reference for human-readable output. ``bar_ts`` (an MT5 server-time epoch)
    must not be passed to ``datetime.fromtimestamp`` without the broker offset.
    """
    if not ts:
        return "N/A"
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return str(ts)


def _parse_utc(ts: str | None) -> datetime | None:
    """Parse a signal ``ts`` (UTC ISO) into a timezone-aware datetime, or None."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def score_signal(
    signal: sqlite3.Row,
    bars: BarList,
    symbol: str,
    spread_provider: SpreadProvider = default_spread,
    point_provider: PointProvider = default_point_size,
) -> ScoredSignal:
    """Score a single signal against its bars."""
    entry_price = float(signal["entry_price"] or 0)
    stop_loss = float(signal["stop_loss"] or 0)
    take_profit = float(signal["take_profit"] or 0)
    direction = signal["direction"]
    point = point_provider(symbol)

    spread_pts = spread_provider(symbol)
    spread_px = spread_pts * point
    dist = abs(entry_price - stop_loss) if stop_loss > 0 else 0.0
    spread_r = (spread_px / dist) if dist > 0 else 0.0

    gross_r = 0.0
    net_r = 0.0
    outcome = "OPEN"
    hit_bars = 0

    if not bars or entry_price == 0 or stop_loss == 0:
        outcome = "NO_DATA"
    else:
        for i, bar in enumerate(bars):
            hit = score_bar_hit(direction, entry_price, stop_loss, take_profit, bar)
            if hit:
                hit_bars = i + 1
                if direction == "BUY":
                    r_val = (take_profit - entry_price) / (entry_price - stop_loss) if hit == "TP" else -1.0
                else:
                    r_val = (entry_price - take_profit) / (stop_loss - entry_price) if hit == "TP" else -1.0
                gross_r = float(r_val)
                net_r = gross_r - spread_r
                outcome = hit
                break

    return ScoredSignal(
        signal_id=signal["id"],
        symbol=symbol,
        direction=direction,
        entry_price=entry_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        bar_ts=signal["bar_ts"] or 0,
        score=float(signal["score"] or 0),
        bar_time=_format_signal_ts(signal["ts"]),
        signal_ts=str(signal["ts"] or ""),
        outcome=outcome,
        gross_r=round(gross_r, 4),
        spread_r=round(spread_r, 4),
        net_r=round(net_r, 4),
        hit_bars=hit_bars,
    )


def get_signals(conn: sqlite3.Connection, weeks: int) -> list[sqlite3.Row]:
    """Fetch production signals (is_test=0) within the last `weeks` weeks.

    Filters on the signal's ``ts`` column (UTC ISO text), not ``bar_ts``,
    because ``bar_ts`` is a MetaTrader server-time epoch that is not Unix UTC.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(weeks=weeks)
    cutoff_str = cutoff_dt.strftime("%Y-%m-%dT%H:%M:%S")
    cur = conn.execute(
        """SELECT * FROM signals
           WHERE is_test=0 AND ts >= ? ORDER BY ts ASC""",
        (cutoff_str,),
    )
    return cur.fetchall()


def score_signals(
    conn: sqlite3.Connection,
    weeks: int = DEFAULT_WEEKS,
    max_bars: int = DEFAULT_MAX_BARS,
    verbose: bool = False,
    bar_provider: Optional[BarProvider] = None,
    spread_provider: Optional[SpreadProvider] = None,
    point_provider: Optional[PointProvider] = None,
) -> list[ScoredSignal]:
    """Score all production signals in the journal."""
    conn.row_factory = sqlite3.Row
    signals = get_signals(conn, weeks)

    if verbose:
        logger.info("Found %d production signals in last %d weeks", len(signals), weeks)

    bar_provider = bar_provider or _fetch_bars_from_table
    spread_provider = spread_provider or default_spread
    point_provider = point_provider or default_point_size

    results: list[ScoredSignal] = []
    for sig in signals:
        symbol = sig["symbol"]
        bars = bar_provider(conn, symbol, sig["bar_ts"] or 0, max_bars)
        scored = score_signal(sig, bars, symbol, spread_provider, point_provider)
        results.append(scored)
        if verbose:
            logger.info(
                "  [%d] %s %s entry=%.5f SL=%.5f TP=%.5f -> %s (R=%.2f net=%.2f bars=%d)",
                scored.signal_id,
                scored.symbol,
                scored.direction,
                scored.entry_price,
                scored.stop_loss,
                scored.take_profit,
                scored.outcome,
                scored.gross_r,
                scored.net_r,
                scored.hit_bars,
            )

    return results


def build_weekly_results(scored: list[ScoredSignal]) -> list[dict]:
    """Group scored signals by ISO week, computing aggregate metrics per week per symbol.

    Groups on the signal's UTC ``ts`` field, not ``bar_ts`` (which is MT5
    server time and would group weeks incorrectly).
    """
    week_map: dict[str, dict] = {}
    for s in scored:
        if not s.signal_ts:
            continue
        dt = _parse_utc(s.signal_ts)
        if dt is None:
            continue
        week_start = dt.strftime("%Y-W%W")
        if week_start not in week_map:
            week_map[week_start] = {"week": week_start, "symbols": {}}
        sym_data = week_map[week_start]["symbols"]
        if s.symbol not in sym_data:
            sym_data[s.symbol] = {
                "symbol": s.symbol,
                "total": 0,
                "wins": 0,
                "losses": 0,
                "open": 0,
                "no_data": 0,
                "gross_r_sum": 0.0,
                "net_r_sum": 0.0,
                "resolved": 0,
            }
        sd = sym_data[s.symbol]
        sd["total"] += 1
        if s.outcome == "TP":
            sd["wins"] += 1
            sd["resolved"] += 1
            sd["gross_r_sum"] += s.gross_r
            sd["net_r_sum"] += s.net_r
        elif s.outcome == "SL":
            sd["losses"] += 1
            sd["resolved"] += 1
            sd["gross_r_sum"] += s.gross_r
            sd["net_r_sum"] += s.net_r
        elif s.outcome == "OPEN":
            sd["open"] += 1
        elif s.outcome == "NO_DATA":
            sd["no_data"] += 1

    for wk in week_map.values():
        for sd in wk["symbols"].values():
            if sd["resolved"] > 0:
                sd["win_rate_%"] = round(sd["wins"] / sd["resolved"] * 100, 1)
                sd["avg_gross_r"] = round(sd["gross_r_sum"] / sd["resolved"], 3)
                sd["avg_net_r"] = round(sd["net_r_sum"] / sd["resolved"], 3)
            else:
                sd["win_rate_%"] = 0.0
                sd["avg_gross_r"] = 0.0
                sd["avg_net_r"] = 0.0
            sd["cost_bps"] = round(
                (sd["gross_r_sum"] - sd["net_r_sum"]) / sd["resolved"] * 10000
                if sd["resolved"] > 0
                else 0.0,
                1,
            )

    return list(week_map.values())


def build_summary(scored: list[ScoredSignal]) -> dict:
    """Build overall summary stats."""
    resolved = [s for s in scored if s.outcome in ("TP", "SL")]
    wins = [s for s in resolved if s.outcome == "TP"]
    losses = [s for s in resolved if s.outcome == "SL"]
    open_signals = [s for s in scored if s.outcome == "OPEN"]
    no_data = [s for s in scored if s.outcome == "NO_DATA"]

    gross_rs = [s.gross_r for s in resolved]
    net_rs = [s.net_r for s in resolved]
    spread_rs = [s.spread_r for s in resolved]

    summary = {
        "total_signals": len(scored),
        "resolved": len(resolved),
        "wins": len(wins),
        "losses": len(losses),
        "open": len(open_signals),
        "no_data": len(no_data),
    }

    if resolved:
        win_rate = len(wins) / len(resolved)
        lose_rate = len(losses) / len(resolved)
        avg_gross_r = sum(gross_rs) / len(gross_rs)
        avg_net_r = sum(net_rs) / len(net_rs)
        avg_spread_r = sum(spread_rs) / len(spread_rs)
        std_gross = (sum((r - avg_gross_r) ** 2 for r in gross_rs) / len(gross_rs)) ** 0.5
        std_net = (sum((r - avg_net_r) ** 2 for r in net_rs) / len(net_rs)) ** 0.5
        se_gross = std_gross / (len(gross_rs) ** 0.5) if len(gross_rs) > 1 else 0.0
        se_net = std_net / (len(net_rs) ** 0.5) if len(net_rs) > 1 else 0.0

        win_gross = [s.gross_r for s in wins]
        loss_gross = [s.gross_r for s in losses]
        avg_win_gross = sum(win_gross) / len(win_gross) if win_gross else 0.0
        avg_loss_gross = sum(loss_gross) / len(loss_gross) if loss_gross else 0.0
        avg_win_net = sum(s.net_r for s in wins) / len(wins) if wins else 0.0
        avg_loss_net = sum(s.net_r for s in losses) / len(losses) if losses else 0.0

        summary.update(
            {
                "win_rate_%": round(win_rate * 100, 1),
                "avg_gross_r": round(avg_gross_r, 3),
                "avg_net_r": round(avg_net_r, 3),
                "avg_spread_r": round(avg_spread_r, 4),
                "std_gross_r": round(std_gross, 3),
                "std_net_r": round(std_net, 3),
                "se_gross_r": round(se_gross, 3),
                "se_net_r": round(se_net, 3),
                "expectancy_gross": round(win_rate * avg_win_gross + lose_rate * avg_loss_gross, 3),
                "expectancy_net": round(win_rate * avg_win_net + lose_rate * avg_loss_net, 3),
            }
        )
    else:
        summary.update(
            {
                "win_rate_%": 0.0,
                "avg_gross_r": 0.0,
                "avg_net_r": 0.0,
                "avg_spread_r": 0.0,
                "std_gross_r": 0.0,
                "std_net_r": 0.0,
                "se_gross_r": 0.0,
                "se_net_r": 0.0,
                "expectancy_gross": 0.0,
                "expectancy_net": 0.0,
            }
        )

    return summary


def format_report(scored: list[ScoredSignal], weekly: list[dict], summary: dict) -> str:
    """Format the forward-test report as text."""
    lines: list[str] = []
    lines.append("=" * 70)
    lines.append("FORWARD-TEST SCORER REPORT")
    lines.append(f"  Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("=" * 70)

    if not scored:
        lines.append("")
        lines.append("No production signals found in the journal.")
        lines.append("  - No production signals yet; the strategy has not produced a real signal.")
        lines.append("")
        return "\n".join(lines)

    lines.append("")
    lines.append("## Summary")
    lines.append(f"  Total signals:  {summary['total_signals']}")
    lines.append(f"  Resolved:       {summary['resolved']} (W:{summary['wins']} / L:{summary['losses']})")
    lines.append(f"  Still open:     {summary['open']}")
    lines.append(f"  No bar data:    {summary['no_data']}")
    if summary["resolved"] > 0:
        lines.append("")
        lines.append(f"  Win rate:       {summary['win_rate_%']}%")
        lines.append(f"  Avg gross R:    {summary['avg_gross_r']}  (±{summary['se_gross_r']})")
        lines.append(f"  Avg net R:      {summary['avg_net_r']}  (±{summary['se_net_r']})")
        lines.append(f"  Avg spread R:   {summary['avg_spread_r']}")
        lines.append(f"  Expectancy:     gross={summary['expectancy_gross']} net={summary['expectancy_net']}")
    lines.append("")
    lines.append("## Weekly Results")
    lines.append("")

    if not weekly:
        lines.append("  (no weekly breakdown available)")
        lines.append("")
    else:
        for wk in weekly:
            lines.append(f"  {wk['week']}")
            for sd in wk["symbols"].values():
                lines.append(
                    f"    {sd['symbol']}: W={sd['wins']} L={sd['losses']} "
                    f"open={sd['open']} nodata={sd['no_data']} "
                    f"WR={sd['win_rate_%']}% avgR={sd['avg_net_r']} "
                    f"cost={sd['cost_bps']}bps"
                )
            lines.append("")

    lines.append("## Signals")
    lines.append("")
    lines.append(
        f"  {'ID':>5}  {'SYMBOL':<10} {'DIR':<5} {'ENTRY':>9} {'SL':>9} {'TP':>9} "
        f"{'OUT':<7} {'grossR':>7} {'netR':>7} {'bars':>5}"
    )
    for s in scored:
        lines.append(
            f"  {s.signal_id:>5}  {s.symbol:<10} {s.direction:<5} "
            f"{s.entry_price:>9.5f} {s.stop_loss:>9.5f} {s.take_profit:>9.5f} "
            f"{s.outcome:<7} {s.gross_r:>7.2f} {s.net_r:>7.2f} {s.hit_bars:>5}"
        )

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Forward-test scorer for live journal signals"
    )
    parser.add_argument("--db", required=True, help="Path to the journal SQLite DB")
    parser.add_argument(
        "--weeks", type=int, default=DEFAULT_WEEKS,
        help=f"Weeks to look back (default {DEFAULT_WEEKS})",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--verbose", action="store_true", help="Verbose logging")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: Database not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        scored = score_signals(conn, weeks=args.weeks, verbose=args.verbose)
        weekly = build_weekly_results(scored)
        summary = build_summary(scored)

        if args.json:
            output = {
                "summary": summary,
                "weekly": weekly,
                "signals": [
                    {
                        "id": s.signal_id,
                        "symbol": s.symbol,
                        "direction": s.direction,
                        "entry_price": s.entry_price,
                        "stop_loss": s.stop_loss,
                        "take_profit": s.take_profit,
                        "bar_ts": s.bar_ts,
                        "bar_time": s.bar_time,
                        "score": s.score,
                        "outcome": s.outcome,
                        "gross_r": s.gross_r,
                        "spread_r": s.spread_r,
                        "net_r": s.net_r,
                        "hit_bars": s.hit_bars,
                    }
                    for s in scored
                ],
            }
            print(json.dumps(output, indent=2, default=str))
        else:
            report = format_report(scored, weekly, summary)
            print(report)

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
