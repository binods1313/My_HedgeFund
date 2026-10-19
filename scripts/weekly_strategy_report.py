"""Read-only weekly strategy evaluation report. Does not change engine rules.

Per symbol and UTC day: bars evaluated, max agreeing blocks, counts of bars
with 3/4/5 agreeing, and which blocks most often disagree when exactly 3 agree.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _db_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    from engine.config import load_engine_config
    from engine.journal import resolve_db_path
    cfg = load_engine_config()
    return resolve_db_path(cfg.engine.db_path)


def _load_evals(db: Path) -> list[dict]:
    uri = db.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        rows = conn.execute(
            "SELECT id, ts, context_json FROM events "
            "WHERE json_extract(context_json, '$.kind') = 'strategy_evaluation' "
            "ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    out: list[dict] = []
    for row in rows:
        try:
            ctx = json.loads(row["context_json"] or "{}")
        except (TypeError, ValueError):
            continue
        out.append({
            "id": int(row["id"]),
            "ts": str(row["ts"] or ""),
            "symbol": str(ctx.get("symbol") or "?"),
            "agree": int(ctx.get("agree") or 0),
            "bar": ctx.get("bar_open_epoch"),
            "blocks": ctx.get("blocks") or [],
        })
    return out


def _day(ts: str) -> str:
    return ts[:10] if ts else "unknown"


def summarise(evals: list[dict]) -> dict:
    """One record per (symbol, utc_day, bar). Later evals overwrite earlier."""
    bars: dict[tuple, dict] = {}
    for item in evals:
        key = (item["symbol"], _day(item["ts"]), item["bar"])
        bars[key] = item
    per: dict[tuple[str, str], dict] = defaultdict(lambda: {
        "bars": 0, "max_agree": 0, "n3": 0, "n4": 0, "n5": 0,
        "disagree_when_3": defaultdict(int),
    })
    for (symbol, day, _bar), item in bars.items():
        rec = per[(symbol, day)]
        rec["bars"] += 1
        agree = int(item["agree"])
        rec["max_agree"] = max(rec["max_agree"], agree)
        if agree == 3:
            rec["n3"] += 1
            for block in item["blocks"]:
                if isinstance(block, dict) and not block.get("agrees"):
                    name = str(block.get("name") or "?")
                    rec["disagree_when_3"][name] += 1
        elif agree == 4:
            rec["n4"] += 1
        elif agree >= 5:
            rec["n5"] += 1
    return per


def format_report(per: dict, *, db: Path, n_events: int) -> str:
    lines = [
        f"weekly strategy report (read-only)",
        f"db: {db}",
        f"strategy_evaluation events: {n_events}",
        f"unique symbol/day/bar groups: {sum(r['bars'] for r in per.values())}",
        "",
        f"{'symbol':<8} {'day':<10} {'bars':>5} {'max':>3} {'n3':>4} {'n4':>4} {'n5':>4}  disagree-when-3",
    ]
    for (symbol, day) in sorted(per):
        rec = per[(symbol, day)]
        disagree = rec["disagree_when_3"]
        if disagree:
            ranked = sorted(disagree.items(), key=lambda kv: (-kv[1], kv[0]))
            detail = ", ".join(f"{name}={n}" for name, n in ranked)
        else:
            detail = "—"
        lines.append(
            f"{symbol:<8} {day:<10} {rec['bars']:>5} {rec['max_agree']:>3} "
            f"{rec['n3']:>4} {rec['n4']:>4} {rec['n5']:>4}  {detail}"
        )
    if not per:
        lines.append("(no strategy_evaluation events)")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="journal path (default: config.yaml engine.db_path)")
    args = parser.parse_args(argv)
    db = _db_path(args.db)
    if not db.exists():
        print(f"journal not found: {db}")
        return 1
    evals = _load_evals(db)
    text = format_report(summarise(evals), db=db, n_events=len(evals))
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
