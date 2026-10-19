"""Production-only UI statistics. TEST SIGNAL rows never feed these numbers.

Data sources (where each statistic is read from):

- Equity change, max drawdown, Sharpe
    `equity_snapshots.equity` via `get_equity_history`. Broker account
    equity, not the trades table. PAPER TEST SIGNAL fills do not move
    this series. Still documented here so a later trade-fed equity
    reconstruction must call `production_realised_profits`.
- Win/loss
    Closed `trades` whose linked `events.decision_id` is not in
    `signals.is_test = 1`. Profit comes from `events.context_json.profit`.
- Trades today / last trade
    `trades` filtered by `classify_trades(...).is_test`.
- Equity curve (Exchange analytics)
    Same `equity_snapshots` as Portfolio drawdown.
- Research Desk sample counts
    Shadow `learning.db` predictions/outcomes. Journal trades are not
    the sample source. `link_journal` skips `is_test` tickets so a TEST
    SIGNAL fill cannot enter `trades_link`.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlite3

from engine.test_signal import classify_trades, production_realised_profits

SOURCES = {
    "equity_change": "equity_snapshots (broker)",
    "max_drawdown": "equity_snapshots (broker)",
    "sharpe": "equity_snapshots (broker)",
    "win_loss": "closed production trades; signals.is_test=1 excluded",
    "trades_today": "trades.ts today; signals.is_test=1 excluded",
    "last_trade": "latest production trade; TEST SIGNAL skipped",
    "equity_curve": "equity_snapshots (broker)",
    "research_samples": "learning ledger predictions/outcomes",
    "research_trade_links": "trades_link skips is_test journal tickets",
}


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def production_stats(db_path: Path | None) -> dict[str, Any]:
    """Read-only production trade stats. Empty/missing journal → zeros, not guesses."""
    empty = {
        "realised": [],
        "trades_today": 0,
        "last_trade": None,
        "production_trade_count": 0,
        "sources": SOURCES,
        "readable": False,
    }
    if db_path is None or not Path(db_path).exists():
        return empty
    try:
        with closing(_connect(Path(db_path))) as conn:
            rows = classify_trades(conn)
            realised = production_realised_profits(conn)
    except sqlite3.Error:
        return empty
    today = datetime.now(timezone.utc).date().isoformat()
    production = [row for row in rows if not row["is_test"]]
    today_n = sum(
        1 for row in production
        if str(row["ts"] or "").startswith(today)
    )
    last = production[-1] if production else None
    return {
        "realised": realised,
        "trades_today": today_n,
        "last_trade": last,
        "production_trade_count": len(production),
        "sources": SOURCES,
        "readable": True,
    }
