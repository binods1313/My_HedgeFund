#!/usr/bin/env python3
"""Time Machine for trader-timemachine, ported to MetaTrader5 M15 bars.

Replays a planned FX/metals trade before you risk a dollar: N simulated runs of
the rest of the UTC day from your entry, plus the same rule on every past full
weekday in the data. This is a port of D:/skills/day-trader-skill/
trader-timemachine/timemachine.py that swaps the Yahoo 5-minute NYSE grid for
MT5-style M15 (or other intraday) UTC bars. The Monte Carlo engine, the odds,
the percentiles, the average R, the verdict line and the seed reproducibility
are kept identical to the original.

    python -m scripts.timemachine_mt5 run                         # trader/plan.json, 10,000 replays, seed 19
    python -m scripts.timemachine_mt5 run --write                 # also save trader/timemachine.json
    python -m scripts.timemachine_mt5 run --until 22:00           # end each replay at your flat-by time
    python -m scripts.timemachine_mt5 run --seed 7 --replays 20000 --json
    python -m scripts.timemachine_mt5 run --symbol EURUSD --date 2026-11-03 --entry 1.11890 \\
        --stop 1.11790 --target 1.12000 --entry-time "00:15"
    python -m scripts.timemachine_mt5 chart                       # ASCII fan chart of the replays
    python -m scripts.timemachine_mt5 chart --from trader/timemachine.json

How the replays work (same algorithm, same random draws as the original):
  1. Bars come from MT5-style JSON cache files in data/market_bars/ named
     <SYMBOL>__<TIMEFRAME>.json ({"rows": [{"time": "2026-10-19T00:00:00",
     "open": .., "high": .., "low": .., "close": .., "spread": null}]}).
     Times are UTC-ish ISO. If the file is missing, the run tries to fetch via
     the MetaTrader5 python package (the MT5 terminal must be running):
     copy_rates_from_pos(symbol, TF, 0, n). If neither works it fails with one
     clear line. Bars are treated as UTC; a session is one weekday (Mon-Fri)
     UTC day with ALL its bars (96 x M15). Half days, weekend bars, gaps and
     today are left out. Bars that have not closed yet are never used.
  2. The pool: for every bar slot 1..N-1 of the day, each past session's bar
     as ratios to the previous bar's close: (open, high, low, close)/prev close.
  3. One replay starts at the entry price and walks the rest of the day. For
     each bar i it picks a slot j = i + randint(-window, window), kept in
     1..N-1, then one past bar from pool[j], and scales it to the current
     price. The stop is checked before the target in every bar, so a bar that
     touches both counts as the stop (the conservative call).
  4. Outcomes: "target" or "stop" (whichever was touched first), or "neither"
     when the trade is still open at --until (default 23:45 UTC, the last
     complete bar). "green" = target first, or above the entry at --until
     (below for a sell). R = the target's R at the target, -1 at the stop,
     otherwise (price at --until - entry)/risk.
  5. The price at --until: median and 5th to 95th percentile of all replays.
     The fan chart uses a second run of 2,000 paths with the same seed.
  6. History: the plan's own rule (an opening-range breakout with the same
     opening range, stop rule and target R) re-run on every past full weekday,
     first close beyond the range = entry, same stop-first rule.

The verdict line is unchanged from the original: it looks at the replays that
hit one side (target or stop) and compares the target's share of them,
target/(target+stop), with the share that breaks even at the plan's target R:
1/(1+R). Gap in percentage points: <5 "Close to a coin flip" (1R) /
"Close to break-even" (other R); 5-15 "Leans toward the target/stop"; >=15
"Strongly toward the target/stop".

MT5 intraday bars only. The run refuses with a plain message when there are
fewer than 20 full weekday sessions or the entry bar is not in the data. Same
plan, same data and same --seed give the same numbers on any machine. A
simulation built from the pair's own recent bars is not a prediction:
central-bank moves, news and liquidity gaps can do what the last sessions
never did.

Data: data/market_bars/<SYM>__<TF>.json cache, else the running MetaTrader5
terminal. --timeframe defaults to M15 (M5/M30/H1 also accepted). Times are UTC.
--now "2026-11-03 10:00" pins the clock (bare times are UTC).
"""

import argparse
import datetime
import json
import math
import os
import random
import re
import statistics
import sys

UA = "Mozilla/5.0 (compatible; hedgefund-binod/timemachine-mt5)"
FETCH_BARS = 120 * 96     # ~120 days of M15 when fetching from the terminal
SESSION_BARS = 96          # M15 bars in a 24h UTC day (1440 / 15)
MIN_SESSIONS = 20
SAMPLE_PATHS = 160
BAND_PATHS = 2000
BAND_Q = (0.05, 0.25, 0.5, 0.75, 0.95)
HIST_BINS = 24
COIN_FLIP = 0.05           # verdict thresholds, see the docstring
STRONG = 0.15
MANY_NEITHER = 0.40
EPOCH = datetime.datetime(1970, 1, 1)
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
DEFAULT_UNTIL = 23 * 60 + 45   # last complete M15 bar of the UTC day
DEFAULT_TIME_FRAME = "M15"

TF_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30,
    "H1": 60, "H4": 240, "D1": 1440,
}
_MT5_TF = None


class TMError(Exception):
    pass


# ---------------------------------------------------------------- time (UTC-based)

def parse_clock(text, what):
    """'14:30', '2:30 PM' or '2:30pm' -> minutes after midnight UTC."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*(?:([AaPp])\.?\s*[Mm]\.?)?\s*$", str(text or ""))
    if not m:
        raise TMError("%s wants a UTC time like 14:30 or 2:30 PM, not %r" % (what, text))
    h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    if mi > 59 or h > 23 or (ap and not 1 <= h <= 12):
        raise TMError("%s %r is not a clock time" % (what, text))
    if ap == "p" and h != 12:
        h += 12
    elif ap == "a" and h == 12:
        h = 0
    elif not ap and 1 <= h <= 8:
        h += 12
    return h * 60 + mi


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def clock24(minutes):
    return "%02d:%02d" % divmod(minutes, 60)


def parse_now(text):
    """ISO time -> naive UTC. A time without an offset is taken as UTC."""
    if not text:
        return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)
    t = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TMError("--now wants a time like \"2026-11-03 10:00\" (UTC) or ...Z (UTC)")
    if dt.tzinfo is None:
        return dt.replace(tzinfo=None)
    return (dt - dt.utcoffset()).replace(tzinfo=None)


def utc_of(ts):
    return EPOCH + datetime.timedelta(seconds=int(ts))


def iso_of(value):
    """'2026-10-19T00:00:00' (or any ISO-ish text) -> naive UTC datetime."""
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            raise TMError("bar time %r is not ISO" % value)
    if dt.tzinfo is not None:
        dt = (dt - dt.utcoffset()).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------- data

def _bar_file(symbol, timeframe, root):
    return os.path.join(root, "%s__%s.json" % (symbol.upper(), timeframe.upper()))


def load_cache_bars(symbol, timeframe, root):
    """MT5-style JSON cache -> list of raw row dicts (empty when missing/bad)."""
    path = _bar_file(symbol, timeframe, root)
    if not os.path.exists(path):
        return None, path
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        raise TMError("could not read the bar cache %s" % path)
    rows = payload.get("rows") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise TMError("%s is not an MT5-style bar cache (expected {\"rows\": [...]})" % path)
    return rows, path


def _mt5_tf(timeframe):
    global _MT5_TF
    if _MT5_TF is None:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            raise TMError("MetaTrader5 package is not installed here; drop a SYMBOL__%s.json cache "
                          "into data/market_bars/ instead" % timeframe)
        code = getattr(mt5, "TIMEFRAME_" + timeframe, None)
        if code is None:
            raise TMError("MetaTrader5 has no timeframe TIMEFRAME_%s" % timeframe)
        _MT5_TF = (mt5, code)
    return _MT5_TF


def fetch_mt5_bars(symbol, timeframe):
    """Live fetch from a running MetaTrader5 terminal -> sorted bar dicts."""
    mt5, code = _mt5_tf(timeframe)
    if not mt5.initialize():
        raise TMError("could not start the MetaTrader5 terminal: %s" % (mt5.last_error() or "unknown"))
    rates = mt5.copy_rates_from_pos(symbol, code, 0, FETCH_BARS)
    if rates is None:
        raise TMError("MetaTrader5 returned no bars for %s %s: %s"
                      % (symbol, timeframe, mt5.last_error() or "no data"))
    rows = []
    for t, o, h, l, c, _v, _sp, _rl in rates:
        if None in (o, h, l, c):
            continue
        rows.append({"time": datetime.datetime.fromtimestamp(int(t), datetime.UTC)
                     .replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S"),
                     "open": float(o), "high": float(h), "low": float(l),
                     "close": float(c), "spread": None})
    rows.sort(key=lambda r: r["time"])
    return rows


def parse_bars(rows, now_utc, tf_min):
    """Raw MT5 rows -> (bars, dropped). Complete on-grid bars only, oldest first.
    Bars that have not closed yet (start + tf_min > now) are never used."""
    interval = datetime.timedelta(minutes=tf_min)
    dropped = {"no_price": 0, "off_grid": 0, "not_complete": 0, "weekend": 0}
    bars = {}
    for row in rows:
        try:
            o, h, l, c = (float(row.get(k)) for k in ("open", "high", "low", "close"))
        except (TypeError, ValueError):
            dropped["no_price"] += 1
            continue
        try:
            start = iso_of(row.get("time"))
        except TMError:
            dropped["off_grid"] += 1
            continue
        mins = start.hour * 60 + start.minute
        if mins % tf_min or mins >= 1440:
            dropped["off_grid"] += 1
            continue
        if start.date().weekday() >= 5:
            dropped["weekend"] += 1
            continue
        if start + interval > now_utc:
            dropped["not_complete"] += 1
            continue
        key = (start.date().isoformat(), mins // tf_min)
        bars[key] = {"date": start.date().isoformat(), "slot": mins // tf_min,
                     "start": start.strftime("%H:%M"), "o": o, "h": h, "l": l, "c": c}
    return bars, dropped


def split_sessions(bars):
    """bars: {date, slot, ...} -> {date: {slot: bar}}."""
    days = {}
    for key, b in bars.items():
        days.setdefault(key[0], {})[key[1]] = b
    return days


def full_sessions(days, before, n_slots):
    """Full weekday sessions (all n_slots bars) strictly before the plan's date,
    oldest first, plus the short ones left out (weekdays with gaps, Friday's
    early close, ...)."""
    full, short = [], []
    for d in sorted(days):
        if d >= before:
            continue
        s = days[d]
        if len(s) == n_slots:
            full.append((d, [s[i] for i in range(n_slots)]))
        else:
            short.append({"date": d, "bars": len(s)})
    return full, short


# ---------------------------------------------------------------- the simulation (ported from sim.py)

def build_pool(sessions, n_slots):
    """pool[i] = every past session's bar i as (o, h, l, c)/bar i-1's close."""
    pool = [[] for _ in range(n_slots)]
    for _, D in sessions:
        for i in range(1, n_slots):
            pc = D[i - 1]["c"]
            pool[i].append((D[i]["o"] / pc, D[i]["h"] / pc, D[i]["l"] / pc, D[i]["c"] / pc))
    return pool


def pct(a, q):
    return a[min(len(a) - 1, int(q * len(a)))]


def simulate(pool, entry, stop, target, k, last, replays=10000, seed=19, window=2,
             side="buy", n_slots=SESSION_BARS, prec=5):
    """Monte Carlo replays of bars k+1..last. Same draws and order as the reel's sim.py."""
    long = side == "buy"
    risk = (entry - stop) if long else (stop - entry)
    r_target = round(((target - entry) if long else (entry - target)) / risk, 4)

    def slot(rng, i):
        return i if window == 0 else min(n_slots - 1, max(1, i + rng.randint(-window, window)))

    rng = random.Random(seed)
    choice = rng.choice
    outcomes, closes, paths = [], [], []
    for n in range(replays):
        p = entry
        outcome = None
        keep = n < SAMPLE_PATHS
        path = [round(entry, prec)]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            hi, lo, cl = p * h, p * l, p * c
            if outcome is None:
                if long:
                    if lo <= stop:
                        outcome = "stop"        # both inside one bar: count the loss (conservative)
                    elif hi >= target:
                        outcome = "target"
                else:
                    if hi >= stop:
                        outcome = "stop"
                    elif lo <= target:
                        outcome = "target"
            p = cl
            if keep:
                path.append(round(p, prec))
        outcomes.append(outcome or "close")
        closes.append(p)
        if keep:
            paths.append(path)
    cnt = {x: outcomes.count(x) for x in ("target", "stop", "close")}
    green = sum(1 for o, p in zip(outcomes, closes)
                if o == "target" or (o == "close" and (p > entry if long else p < entry)))
    Rs = [r_target if o == "target" else -1.0 if o == "stop" else ((p - entry) if long else (entry - p)) / risk
          for o, p in zip(outcomes, closes)]
    srt = sorted(closes)
    # percentile bands per bar for the fan chart: a second run of 2,000 paths, same seed
    rng = random.Random(seed)
    choice = rng.choice
    allp = []
    for n in range(BAND_PATHS):
        p = entry
        path = [round(entry, prec)]
        for i in range(k + 1, last + 1):
            o, h, l, c = choice(pool[slot(rng, i)])
            p = p * c
            path.append(round(p, prec))
        allp.append(path)
    bands = []
    for j in range(last - k + 1):
        col = sorted(pp[j] for pp in allp)
        bands.append([round(pct(col, q), prec) for q in BAND_Q])
    lo_, hi_ = pct(srt, 0.01), pct(srt, 0.99)
    W = (hi_ - lo_) / HIST_BINS
    bins = [0] * HIST_BINS
    for c in srt:
        b = int((c - lo_) / W) if W > 0 else 0
        if 0 <= b < HIST_BINS:
            bins[b] += 1
    return {
        "replays": replays, "seed": seed, "window": window, "bars_left": last - k,
        "p_target": round(cnt["target"] / replays, 4), "p_stop": round(cnt["stop"] / replays, 4),
        "p_neither": round(cnt["close"] / replays, 4), "p_green": round(green / replays, 4),
        "median_close": round(pct(srt, 0.5), prec), "p5_close": round(pct(srt, 0.05), prec),
        "p95_close": round(pct(srt, 0.95), prec), "avg_R": round(statistics.mean(Rs), 3),
        "r_at_target": r_target, "band_paths": BAND_PATHS, "bands": bands, "sample_paths": paths,
        "hist": {"lo": round(lo_, prec), "hi": round(hi_, prec), "bins": bins},
    }


# ---------------------------------------------------------------- stop rules and the history replay

def price_prec(value):
    """Decimal places to display/round a price, from its magnitude:
    >=100 -> 2 (stocks, gold), >=10 -> 3 (JPY pairs), else 5 (FX)."""
    v = abs(float(value))
    if v >= 100:
        return 2
    if v >= 10:
        return 3
    return 5


def away(x, long, prec):
    """Round a stop to the given precision, away from the entry: down for a
    buy, up for a sell."""
    step = 10.0 ** (-prec)
    q = round(x / step)
    if long:
        return (q - 1) * step if q * step >= x else q * step
    return (q + 1) * step if q * step <= x else q * step


def opening_range(D, n):
    """(high, low) of the first n bars of a session given as {slot: bar}."""
    rows = [D[i] for i in range(n)]
    return max(b["h"] for b in rows), min(b["l"] for b in rows)


def rule_stop(rule, oh, ol, entry, long, prec):
    r = rule["stop_rule"]
    if r == "or_mid":
        return away((oh + ol) / 2, long, prec)
    if r in ("or_low", "or_high"):
        return away(ol if long else oh, long, prec)
    frac = rule["stop_pct"] / 100.0
    return away(entry * (1 - frac) if long else entry * (1 + frac), long, prec)


def history_replay(sessions, rule, last, side="buy", n_slots=SESSION_BARS, prec=5, tf_min=15):
    """The plan's rule on every past full weekday: entry = the first bar after
    the opening range to close beyond it (up to the --until bar), then stop
    first, then target, bar by bar, else the close at --until."""
    long = side == "buy"
    n = rule["or_minutes"] // tf_min
    trades = []
    for d, D in sessions:
        oh, ol = opening_range(D, n)
        kk = next((i for i in range(n, last + 1) if (D[i]["c"] > oh if long else D[i]["c"] < ol)), None)
        if kk is None:
            continue
        e = round(D[kk]["c"], prec)
        s = rule_stop(rule, oh, ol, e, long, prec)
        risk = (e - s) if long else (s - e)
        if risk <= 0:
            continue
        t = round(e + rule["target_r"] * risk, prec) if long else round(e - rule["target_r"] * risk, prec)
        oc = None
        for b in D[kk + 1:last + 1]:
            if (b["l"] <= s) if long else (b["h"] >= s):
                oc = "stop"
                break
            if (b["h"] >= t) if long else (b["l"] <= t):
                oc = "target"
                break
        cl = D[last]["c"]
        R = ((t - e) if long else (e - t)) / risk if oc == "target" else -1.0 if oc == "stop" else \
            ((cl - e) if long else (e - cl)) / risk
        trades.append({"date": d, "time": clock12(tf_min * (kk + 1)), "entry": e, "stop": s, "target": t,
                       "outcome": oc or "close", "R": round(R, 2)})
    return {
        "sessions": len(sessions), "triggered": len(trades),
        "target": sum(1 for x in trades if x["outcome"] == "target"),
        "stop": sum(1 for x in trades if x["outcome"] == "stop"),
        "close": sum(1 for x in trades if x["outcome"] == "close"),
        "green": sum(1 for x in trades if x["R"] > 0),
        "avg_R": round(statistics.mean(x["R"] for x in trades), 3) if trades else None,
        "rule": rule, "trades": trades,
    }


# ---------------------------------------------------------------- the plan

def _num(v, what):
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise TMError("the plan needs a number for %s, got %r" % (what, v))
    if not x > 0 or math.isinf(x):
        raise TMError("the plan's %s must be a positive price, got %r" % (what, v))
    return x


def load_plan(a, now_utc, tf_min):
    """The plan from --plan (trader/plan.json) or from the flags, checked and normalized."""
    tf = a.timeframe.upper()
    if a.symbol:
        missing = [f for f in ("entry", "stop", "target") if getattr(a, f) is None]
        if missing:
            raise TMError("with --symbol also give %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        raw = {"symbol": a.symbol, "side": a.side, "date": a.date or now_utc.date().isoformat(),
               "entry": a.entry, "stop": a.stop, "target": a.target, "entry_time": a.entry_time,
               "setup": a.setup, "opening_range": {"minutes": a.or_minutes}, "stop_rule": a.stop_rule,
               "interval": tf}
        src = "flags"
    else:
        if not os.path.exists(a.plan):
            raise TMError("no plan at %s: pass --symbol --date --entry --stop --target "
                          "[--entry-time] instead" % a.plan)
        try:
            with open(a.plan, encoding="utf-8") as fh:
                raw = json.load(fh)
        except ValueError as e:
            raise TMError("%s is not valid JSON: %s" % (a.plan, e))
        if not isinstance(raw, dict):
            raise TMError("%s is not a plan" % a.plan)
        raw = dict(raw)
        raw.setdefault("interval", tf)
        src = a.plan
    return normalize_plan(raw, src, tf_min)


def normalize_plan(raw, src="flags", tf_min=15):
    """A plan dict (trader/plan.json shape) -> the checked plan the time machine runs."""
    sym = str(raw.get("symbol") or "").strip().upper()
    if not re.match(r"^[A-Z][A-Z0-9.#\-]{0,11}$", sym):
        raise TMError("the plan needs a symbol, got %r" % raw.get("symbol"))
    side = str(raw.get("side") or "buy").lower()
    if side not in ("buy", "sell"):
        raise TMError("side must be buy or sell, got %r" % raw.get("side"))
    interval = str(raw.get("interval") or "").strip().upper().replace(" ", "")
    if interval in ("M15", "15M"):
        interval = "M15"
    elif interval == "H1":
        interval = "H1"
    elif interval == "M5":
        interval = "M5"
    elif interval == "M30":
        interval = "M30"
    elif interval == "H4":
        interval = "H4"
    if TF_MINUTES.get(interval) != tf_min:
        raise TMError("the plan says %s but --timeframe is M%d: they must match"
                      % (interval or "?", tf_min))
    try:
        date = datetime.date.fromisoformat(str(raw.get("date") or "")).isoformat()
    except ValueError:
        raise TMError("the plan needs a date like 2026-11-03, got %r" % raw.get("date"))
    entry, stop, target = (_num(raw.get(k), k) for k in ("entry", "stop", "target"))
    long = side == "buy"
    if long and not stop < entry < target:
        raise TMError("a buy needs stop < entry < target (got stop %s, entry %s, target %s)"
                      % (stop, entry, target))
    if not long and not target < entry < stop:
        raise TMError("a sell needs target < entry < stop (got target %s, entry %s, stop %s)"
                      % (target, entry, stop))
    prec = max(price_prec(x) for x in (entry, stop, target))
    k = None
    if raw.get("entry_time"):
        close_min = parse_clock(raw["entry_time"], "entry_time")
        if close_min % tf_min:
            raise TMError("entry_time %s is not on the M%d grid (use the bar's close, e.g. 01:00)"
                          % (raw["entry_time"], tf_min))
        k = close_min // tf_min - 1
    if raw.get("entry_bar") is not None:
        try:
            kb = int(raw["entry_bar"])
        except (TypeError, ValueError):
            raise TMError("entry_bar must be a bar number, got %r" % raw["entry_bar"])
        if k is not None and kb != k:
            raise TMError("the plan disagrees with itself: entry_bar %d but entry_time %s is bar %d"
                          % (kb, raw["entry_time"], k))
        k = kb
    if k is None:
        k = 0 if not raw.get("entry_time") else k
    if k is None:
        raise TMError("the plan needs entry_time (the entry bar's close, e.g. 01:00) or entry_bar")
    n_slots = 1440 // tf_min
    if not 0 <= k <= n_slots - 2:
        raise TMError("the entry must close between %s and %s UTC to leave bars to replay (got %s)"
                      % (clock12(tf_min), clock12(tf_min * (n_slots - 1)),
                         clock12(tf_min * (k + 1))))
    orng = raw.get("opening_range") or {}
    or_raw = orng.get("minutes") if isinstance(orng, dict) else None
    if or_raw is None:
        or_min = tf_min
    else:
        try:
            or_min = int(or_raw)
        except (TypeError, ValueError):
            raise TMError("opening_range.minutes must be a number")
    if or_min < tf_min or or_min % tf_min or or_min >= 1440:
        raise TMError("opening_range.minutes must be a multiple of %d (got %s)" % (tf_min, or_min))
    risk = (entry - stop) if long else (stop - entry)
    return {
        "symbol": sym, "side": side, "date": date, "setup": str(raw.get("setup") or "orb"),
        "entry": entry, "entry_time": clock12(tf_min * (k + 1)), "entry_bar": k,
        "stop": stop, "target": target, "risk_per_unit": round(risk, prec),
        "target_r": round(((target - entry) if long else (entry - target)) / risk, 2),
        "opening_range": dict(orng, minutes=or_min) if isinstance(orng, dict) else {"minutes": or_min},
        "stop_rule": str(raw.get("stop_rule") or "auto"), "interval": "M%d" % tf_min,
        "flat_by": raw.get("flat_by"), "from": src,
    }


def resolve_rule(plan, entry_day, tf_min=15):
    """The rule the history replay uses: orb, the plan's opening range, its stop
    rule and the plan's target R. 'auto' works the stop rule out from the plan
    day's own opening range."""
    if plan["setup"].lower() != "orb":
        return None
    long = plan["side"] == "buy"
    n = plan["opening_range"]["minutes"] // tf_min
    prec = max(price_prec(x) for x in (plan["entry"], plan["stop"], plan["target"]))
    rule = {"setup": "orb", "or_minutes": plan["opening_range"]["minutes"], "stop_rule": plan["stop_rule"],
            "target_r": plan["target_r"],
            "stop_pct": round(100.0 * plan["risk_per_unit"] / plan["entry"], 4)}
    far = "or_low" if long else "or_high"
    if rule["stop_rule"] == "auto":
        rule["stop_rule"] = "pct"
        if all(i in entry_day for i in range(n)):
            oh, ol = opening_range(entry_day, n)
            if abs(away((oh + ol) / 2, long, prec) - plan["stop"]) < 0.5 * 10.0 ** (-prec):
                rule["stop_rule"] = "or_mid"
            elif abs(away(ol if long else oh, long, prec) - plan["stop"]) < 0.5 * 10.0 ** (-prec):
                rule["stop_rule"] = far
    elif rule["stop_rule"] in ("or_low", "or_high") and rule["stop_rule"] != far:
        rule["stop_rule"] = "pct"
    elif rule["stop_rule"] not in ("or_mid", far):
        rule["stop_rule"] = "pct"
    if rule["stop_rule"] != "pct":
        del rule["stop_pct"]
    return rule


# ---------------------------------------------------------------- the run

def _until12(mc):
    t = mc.get("until") or clock24(DEFAULT_UNTIL)
    return clock12(int(t[:2]) * 60 + int(t[3:]))


def lean(mc):
    """(gap, share, break_even): the target's share of the replays that hit one
    side, minus the share that breaks even at the target's R. None when nothing hit a side."""
    pt, ps = mc["p_target"], mc["p_stop"]
    if pt + ps <= 0:
        return None
    r = mc.get("r_at_target") or 1.0
    share, even = pt / (pt + ps), 1.0 / (1.0 + r)
    return share - even, share, even


def verdict(mc):
    """One line from the numbers, see the docstring for the thresholds."""
    pt, ps = mc["p_target"], mc["p_stop"]
    lv = lean(mc)
    if lv is None:
        return "No replay reached the target or the stop by %s: the price at %s decides every one." % (
            _until12(mc), _until12(mc))
    d, share, even = lv
    r = mc.get("r_at_target") or 1.0
    one_r = abs(r - 1.0) < 0.005
    if abs(d) < COIN_FLIP:
        head = "Close to a coin flip" if one_r else "Close to break-even"
    elif abs(d) < STRONG:
        head = "Leans toward the target" if d > 0 else "Leans toward the stop"
    else:
        head = "Strongly toward the target" if d > 0 else "Strongly toward the stop"
    odds = "target first %.1f %% vs stop first %.1f %%" % (100 * pt, 100 * ps)
    if one_r:
        return "%s: %s." % (head, odds)
    return ("%s: at %gR the target has to come first in %.0f %% of the replays that hit one side; "
            "it did in %.0f %% (%s)." % (head, round(r, 2), 100 * even, 100 * share, odds))


def verdict_notes(mc):
    """The lines printed right below the verdict, so nothing reads as one-sided."""
    notes = []
    pn = mc.get("p_neither", 0)
    if pn > MANY_NEITHER and lean(mc) is not None:
        notes.append("%.1f %% of the replays hit neither by %s; the price at %s decides those."
                     % (100 * pn, _until12(mc), _until12(mc)))
    lv = lean(mc)
    avg = mc.get("avg_R")
    if lv and abs(lv[0]) >= COIN_FLIP and avg is not None and avg != 0 and (avg > 0) != (lv[0] > 0):
        notes.append("Average R is %+.3f all the same: the replays that hit neither pull it the other way." % avg)
    return notes


def time_machine(plan, now_utc, data_root, replays=10000, seed=19, window=2, until=None,
                 source="cache", tf_min=15):
    """A normalized plan + MT5-style bars -> the timemachine.json document."""
    if not 1 <= replays <= 1000000:
        raise TMError("--replays must be between 1 and 1,000,000")
    if not 0 <= window <= 10:
        raise TMError("--window must be between 0 and 10 bars")
    sym = plan["symbol"]
    tf = "M%d" % tf_min
    n_slots = 1440 // tf_min
    rows, path = load_cache_bars(sym, tf, data_root)
    if rows is None:
        rows = fetch_mt5_bars(sym, tf)
        source = "mt5"
    bars, dropped = parse_bars(rows, now_utc, tf_min)
    if not bars:
        raise TMError("no complete %s bars for %s in the data (%s)" % (tf, sym, source))
    k = plan["entry_bar"]
    until_min = DEFAULT_UNTIL if until is None else parse_clock(until, "--until")
    if until_min % tf_min or until_min > DEFAULT_UNTIL:
        raise TMError("--until must be on the M%d grid and no later than %s"
                      % (tf_min, clock12(DEFAULT_UNTIL)))
    last = until_min // tf_min - 1
    if last <= k:
        raise TMError("--until %s leaves nothing to replay after the %s entry"
                      % (clock12(until_min), plan["entry_time"]))
    days = split_sessions(bars)
    entry_day = days.get(plan["date"], {})
    if k not in entry_day:
        reason = ("no bars for that day" if not entry_day else
                  "the data stops at the %s bar" % entry_day[max(entry_day)]["start"])
        raise TMError("the entry is not in the data: no complete %s bar (closing %s) on %s for %s (%s)"
                      % (clock24(tf_min * k), plan["entry_time"], plan["date"], sym, reason))
    eb = entry_day[k]
    warn = []
    prec = max(price_prec(x) for x in (plan["entry"], plan["stop"], plan["target"]))
    step = 10.0 ** (-prec)
    if not eb["l"] - step <= plan["entry"] <= eb["h"] + step:
        warn.append("the plan's entry %.*f is outside that bar's range %.*f to %.*f"
                    % (prec, plan["entry"], prec, eb["l"], prec, eb["h"]))
    sessions, short = full_sessions(days, plan["date"], n_slots)
    if len(sessions) < MIN_SESSIONS:
        raise TMError("only %d full session(s) of %s before %s in the data; the time machine needs at least %d "
                      "(cache %s)" % (len(sessions), sym, plan["date"], MIN_SESSIONS, path))
    pool = build_pool(sessions, n_slots)
    mc = simulate(pool, plan["entry"], plan["stop"], plan["target"], k, last, replays, seed, window,
                  plan["side"], n_slots, prec)
    mc = dict(mc, until=clock24(until_min), pool_sessions=len(sessions), pool_from=sessions[0][0],
              pool_to=sessions[-1][0],
              band_times=[clock24(tf_min * (j + 1)) for j in range(k, last + 1)])
    rule = resolve_rule(plan, entry_day, tf_min)
    plan_out = {kk: v for kk, v in plan.items() if v is not None}
    if rule:
        plan_out["stop_rule"] = rule["stop_rule"]
        history = history_replay(sessions, rule, last, plan["side"], n_slots, prec, tf_min)
    else:
        history = {"sessions": len(sessions), "triggered": 0, "rule": None, "trades": [],
                   "note": "skipped: the history replay knows the opening-range breakout (orb) only, "
                           "this plan's setup is %r" % plan["setup"]}
    last_bar = {"date": plan["date"], "start": clock24(tf_min * (last + 1))}
    return {
        "created_by": "trader-timemachine",
        "created_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "data": {"symbol": sym, "interval": tf, "range": "previous full UTC weekdays",
                 "last_bar": "%s %s UTC" % (last_bar["date"], last_bar["start"]),
                 "dropped": dropped, "short_sessions_left_out": short},
        "plan": plan_out,
        "mc": mc,
        "history": history,
        "verdict": verdict(mc),
        "verdict_notes": verdict_notes(mc),
        "warnings": warn,
        "note": ("A simulation built from %s's own last %d full UTC weekdays. It is not a prediction: "
                 "central-bank moves, news and liquidity gaps can do things those sessions never did. "
                 "Same seed, same numbers." % (sym, len(sessions))),
    }


# ---------------------------------------------------------------- output

def pc(x):
    return "%.1f %%" % (100.0 * x)


def fmt_price(x, prec):
    return "%.*f" % (prec, x)


def report(doc):
    p, m, h = doc["plan"], doc["mc"], doc["history"]
    long = p["side"] == "buy"
    until = clock12(int(m["until"][:2]) * 60 + int(m["until"][3:]))
    wd = WEEKDAYS[datetime.date.fromisoformat(p["date"]).weekday()]
    r = p["target_r"]
    prec = max(price_prec(x) for x in (p["entry"], p["stop"], p["target"]))
    tf = m.get("interval") or "M15"
    lines = [
        "%s %s, %s %s: entry %s at %s (bar %d), stop %s, target %s (%sR, risk %s a unit)."
        % (p["symbol"], p["side"], wd, p["date"], fmt_price(p["entry"], prec), p["entry_time"],
           p["entry_bar"], fmt_price(p["stop"], prec), fmt_price(p["target"], prec),
           ("%g" % r), fmt_price(p["risk_per_unit"], prec)),
        "%s replays of the rest of the session, %s to %s (%d %s bars), seed %d."
        % ("{:,}".format(m["replays"]), p["entry_time"], until, m["bars_left"], tf, m["seed"]),
        "Each bar is drawn whole from %s's own %s bars at the same time of day (+-%d bars) over its last "
        "%d full sessions (%s to %s)." % (p["symbol"], tf, m["window"], m["pool_sessions"],
                                          m["pool_from"], m["pool_to"]),
        "",
        "  target first   %s" % pc(m["p_target"]),
        "  stop first     %s" % pc(m["p_stop"]),
        "  neither        %-8s still open at %s" % (pc(m["p_neither"]), until),
        "  green          %-8s target first, or %s the entry at %s" % (pc(m["p_green"]),
                                                                       "above" if long else "below", until),
        "",
        "  Price at %s: median %s, 90 %% of replays between %s and %s."
        % (until, fmt_price(m["median_close"], prec), fmt_price(m["p5_close"], prec),
           fmt_price(m["p95_close"], prec)),
        "  Average R: %+.3f (R = the %s risk: %sR at the target, -1R at the stop)."
        % (m["avg_R"], fmt_price(p["risk_per_unit"], prec), ("+%g" % m["r_at_target"])),
        "",
    ]
    if h.get("rule"):
        ru = h["rule"]
        stop_words = {"or_mid": "opening-range midpoint", "or_low": "opening-range low",
                      "or_high": "opening-range high"}.get(ru["stop_rule"],
                                                           "%.2f %% from the entry" % ru.get("stop_pct", 0))
        if h["triggered"]:
            lines.append("History: the same rule (%d-min opening range, stop at the %s, %gR target) on each of "
                         "those %d real sessions fired %d times: %d target, %d stop, %d neither. Average R %+.3f."
                         % (ru["or_minutes"], stop_words, ru["target_r"], h["sessions"], h["triggered"],
                            h["target"], h["stop"], h["close"], h["avg_R"]))
        else:
            lines.append("History: the same rule never fired in those %d sessions." % h["sessions"])
    else:
        lines.append("History: " + h.get("note", "skipped."))
    lines += ["", doc["verdict"]] + list(doc.get("verdict_notes") or []) + [""]
    for w in doc.get("warnings") or []:
        lines.append("Note: " + w + ".")
    if doc["data"]["short_sessions_left_out"]:
        lines.append("Left out %d short session(s) (half days or gaps): %s."
                     % (len(doc["data"]["short_sessions_left_out"]),
                        ", ".join(x["date"] for x in doc["data"]["short_sessions_left_out"])))
    lines.append(doc["note"] + " Repeat this run with --seed %d." % m["seed"])
    return lines


def render_chart(doc, height=17):
    """ASCII fan chart of the bands: one column per bar."""
    p, m = doc["plan"], doc["mc"]
    bands, times = m["bands"], m["band_times"]
    prec = max(price_prec(x) for x in (p["entry"], p["stop"], p["target"]))
    lo = min([b[0] for b in bands] + [p["stop"], p["target"]])
    hi = max([b[4] for b in bands] + [p["stop"], p["target"]])
    pad = (hi - lo) * 0.04 or 0.01
    lo, hi = lo - pad, hi + pad
    step = (hi - lo) / (height - 1)

    def row(price):
        return max(0, min(height - 1, int(round((hi - price) / step))))

    grid = [[" "] * len(bands) for _ in range(height)]
    for j, b in enumerate(bands):
        r5, r25, r50, r75, r95 = [row(x) for x in b]
        for r in range(r95, r5 + 1):
            grid[r][j] = "."
        for r in range(r75, r25 + 1):
            grid[r][j] = ":"
        grid[r50][j] = "*"
    marks = {row(p["target"]): "target", row(p["stop"]): "stop"}
    if row(p["entry"]) not in marks:
        marks[row(p["entry"])] = "entry"
    for r in (row(p["target"]), row(p["stop"])):
        grid[r] = ["-" if ch == " " else ch for ch in grid[r]]
    out = ["%s %s, %s replay paths from the %s entry (seed %d)" % (
        p["symbol"], p["date"], "{:,}".format(m.get("band_paths", BAND_PATHS)), p["entry_time"], m["seed"])]
    for r in range(height):
        label = marks.get(r)
        price = {"target": p["target"], "stop": p["stop"], "entry": p["entry"]}.get(label, hi - r * step)
        left = ("%9s |" % fmt_price(price, prec)) if (label or r in (0, height - 1)) else "          |"
        out.append(left + "".join(grid[r]) + ("  " + label if label else ""))
    axis = [" "] * len(bands)
    for j in sorted({0, len(bands) // 2, len(bands) - 1}):
        t = times[j]
        hh, mm = int(t[:2]), t[3:]
        lab = "%d:%s" % (hh % 12 or 12, mm)
        s = max(0, min(j, len(bands) - len(lab)))
        axis[s:s + len(lab)] = list(lab)
    out.append("          +" + "-" * len(bands))
    out.append("           " + "".join(axis))
    out.append("* median   : middle half of the paths   . 90 % of the paths   - stop and target")
    return out


# ---------------------------------------------------------------- commands

def _write(path, doc):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")


def compute(a):
    now = parse_now(a.now)
    tf_min = TF_MINUTES[a.timeframe.upper()]
    plan = load_plan(a, now, tf_min)
    data_root = os.path.abspath(a.data_dir)
    return time_machine(plan, now, data_root, a.replays, a.seed, a.window, a.until,
                        source="cache", tf_min=tf_min)


def cmd_run(a):
    doc = compute(a)
    if a.write:
        _write(a.write, doc)
    if a.json:
        slim = dict(doc, mc={k: v for k, v in doc["mc"].items() if k != "sample_paths"})
        print(json.dumps(slim, indent=2))
    else:
        print("\n".join(report(doc)))
        if a.write:
            print("Wrote %s." % a.write)
        print("Next: size it to your budget and your max loss per trade.")
    return 0


def cmd_chart(a):
    if a.from_file:
        if not os.path.exists(a.from_file):
            raise TMError("no %s: run timemachine_mt5.py run --write first" % a.from_file)
        try:
            with open(a.from_file, encoding="utf-8") as fh:
                doc = json.load(fh)
            doc["mc"]["bands"]
        except (ValueError, KeyError, TypeError):
            raise TMError("%s is not a time machine result" % a.from_file)
    else:
        doc = compute(a)
    print("\n".join(render_chart(doc)))
    print("\n".join([doc.get("verdict", "")] + list(doc.get("verdict_notes") or [])))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time Machine: replay a planned FX/metals trade "
                                             "(trader-timemachine, MT5 port).")
    sub = ap.add_subparsers(dest="cmd")

    def common(p):
        p.add_argument("--plan", default="trader/plan.json", help="the plan from trader-exit")
        p.add_argument("--symbol", help="or give the plan as flags: symbol (e.g. EURUSD)")
        p.add_argument("--side", default="buy", choices=["buy", "sell"])
        p.add_argument("--date", help="the plan's day, YYYY-MM-DD (UTC; default today)")
        p.add_argument("--entry", type=float)
        p.add_argument("--stop", type=float)
        p.add_argument("--target", type=float)
        p.add_argument("--entry-time", help='the entry bar\'s close, UTC, e.g. "01:00" (default 00:15)')
        p.add_argument("--setup", default="orb")
        p.add_argument("--or-minutes", type=int, default=None)
        p.add_argument("--stop-rule", default="auto", choices=["auto", "or_mid", "or_low", "or_high", "pct"])
        p.add_argument("--replays", type=int, default=10000)
        p.add_argument("--seed", type=int, default=19)
        p.add_argument("--window", type=int, default=2, help="draw from the same time of day +- this many bars")
        p.add_argument("--until", help="end each replay at this UTC time (default 23:45)")
        p.add_argument("--timeframe", default=DEFAULT_TIME_FRAME, choices=sorted(TF_MINUTES),
                       help="bar timeframe (default M15)")
        p.add_argument("--data-dir", default="data/market_bars",
                       help="folder holding <SYM>__<TF>.json caches")
        p.add_argument("--now", help='pretend it is this time ("2026-11-03 10:00" UTC, or ISO with Z)')

    p = sub.add_parser("run", help="replay the plan: replays plus the history")
    common(p)
    p.add_argument("--json", action="store_true")
    p.add_argument("--write", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="save the result (default trader/timemachine.json)")
    p = sub.add_parser("chart", help="ASCII fan chart of the replays")
    common(p)
    p.add_argument("--from", dest="from_file", nargs="?", const="trader/timemachine.json", metavar="PATH",
                   help="draw a saved result instead of running again")
    a = ap.parse_args(argv)
    if not a.cmd:
        ap.print_help()
        return 1
    try:
        return {"run": cmd_run, "chart": cmd_chart}[a.cmd](a)
    except TMError as e:
        print("timemachine_mt5.py: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
