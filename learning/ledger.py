"""Forward-only shadow ledger. The engine database is opened read-only."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from learning.bars import Bar
from learning.resolver import resolve_trade

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    direction TEXT NOT NULL,
    score REAL,
    confidence REAL,
    features_json TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    UNIQUE (symbol, timeframe, bar_time, agent_id, agent_version)
);
CREATE TABLE IF NOT EXISTS outcomes (
    prediction_id INTEGER NOT NULL,
    horizon_bars INTEGER NOT NULL,
    forward_return REAL,
    hit INTEGER,
    mfe REAL,
    mae REAL,
    r_multiple_at_rr REAL,
    resolved_at TEXT NOT NULL,
    PRIMARY KEY (prediction_id, horizon_bars)
);
CREATE TABLE IF NOT EXISTS trades_link (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    journal_trade_id INTEGER UNIQUE,
    prediction_ids TEXT NOT NULL,
    realised_pnl REAL
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    old_value TEXT NOT NULL,
    new_value TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'approved', 'rejected', 'applied', 'rolled_back')
    ),
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS shadow_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'degraded')),
    reason TEXT,
    UNIQUE (symbol, timeframe, bar_time, agent_id, agent_version)
);
"""

# Additive on databases that already stored schema_version = 1.
# A version bump would raise "no forward migration" on those files.
_STATUS_DDL = """
CREATE TABLE IF NOT EXISTS shadow_status (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    bar_time TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    agent_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok', 'degraded')),
    reason TEXT,
    UNIQUE (symbol, timeframe, bar_time, agent_id, agent_version)
);
"""


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ensure_status(conn: sqlite3.Connection) -> None:
    conn.executescript(_STATUS_DDL)
    conn.commit()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    if present is None:
        conn.executescript(_DDL)
        conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)",
            (SCHEMA_VERSION,),
        )
        _ensure_status(conn)
        return conn
    version = conn.execute(
        "SELECT version FROM schema_version WHERE id = 1"
    ).fetchone()
    if version is None:
        conn.executescript(_DDL)
        conn.execute(
            "INSERT INTO schema_version (id, version) VALUES (1, ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
    elif int(version["version"]) > SCHEMA_VERSION:
        conn.close()
        raise RuntimeError("learning schema is newer than this code")
    elif int(version["version"]) < SCHEMA_VERSION:
        conn.close()
        raise RuntimeError("learning schema has no forward migration")
    _ensure_status(conn)
    return conn


def record_prediction(conn: sqlite3.Connection, row: dict) -> int:
    """Insert one prediction. A repeated bar returns the existing id."""
    conn.execute(
        """
        INSERT OR IGNORE INTO predictions (
            ts, symbol, timeframe, bar_time, agent_id, agent_version,
            direction, score, confidence, features_json, config_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row.get("ts") or _utc(),
            row["symbol"],
            row["timeframe"],
            row["bar_time"],
            row["agent_id"],
            row["agent_version"],
            row["direction"],
            row.get("score"),
            row.get("confidence"),
            row["features_json"],
            row["config_hash"],
        ),
    )
    conn.commit()
    found = conn.execute(
        """
        SELECT id FROM predictions
        WHERE symbol = ? AND timeframe = ? AND bar_time = ?
          AND agent_id = ? AND agent_version = ?
        """,
        (row["symbol"], row["timeframe"], row["bar_time"], row["agent_id"], row["agent_version"]),
    ).fetchone()
    return int(found["id"])


BUGGY_AGENT02_VERSION = "1"
MARKED_AGENT02_VERSION = "1.macd0"
_MARK_TABLES = ("predictions", "shadow_status")


def mark_buggy_agent02_macd(conn: sqlite3.Connection) -> dict[str, int]:
    """Retag agent 02 rows written while the signal line copied the MACD line.

    The marker is agent_version 1.macd0. Rows, features_json, and outcomes stay.
    A second call changes nothing. This does not run from connect().
    """
    marked = {"predictions": 0, "shadow_status": 0, "skipped": 0}
    for table in _MARK_TABLES:
        rows = conn.execute(
            f"""
            SELECT id, symbol, timeframe, bar_time
            FROM {table}
            WHERE agent_id = ? AND agent_version = ?
            """,
            ("02", BUGGY_AGENT02_VERSION),
        ).fetchall()
        for row in rows:
            clash = conn.execute(
                f"""
                SELECT 1 FROM {table}
                WHERE symbol = ? AND timeframe = ? AND bar_time = ?
                  AND agent_id = ? AND agent_version = ?
                """,
                (
                    row["symbol"],
                    row["timeframe"],
                    row["bar_time"],
                    "02",
                    MARKED_AGENT02_VERSION,
                ),
            ).fetchone()
            if clash is not None:
                marked["skipped"] += 1
                continue
            conn.execute(
                f"UPDATE {table} SET agent_version = ? WHERE id = ?",
                (MARKED_AGENT02_VERSION, row["id"]),
            )
            marked[table] += 1
    conn.commit()
    return marked


def record_shadow_status(conn: sqlite3.Connection, row: dict) -> None:
    """Store ok or degraded separately from predictions used for calibration."""
    conn.execute(
        """
        INSERT INTO shadow_status (
            ts, symbol, timeframe, bar_time, agent_id, agent_version, status, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (symbol, timeframe, bar_time, agent_id, agent_version)
        DO UPDATE SET ts = excluded.ts, status = excluded.status, reason = excluded.reason
        """,
        (
            row.get("ts") or _utc(),
            row["symbol"],
            row["timeframe"],
            row["bar_time"],
            row["agent_id"],
            row["agent_version"],
            row["status"],
            row.get("reason"),
        ),
    )
    conn.commit()


def resolve_open(
    conn: sqlite3.Connection,
    bars: list[Bar],
    *,
    symbol: str,
    timeframe: str,
    horizons: list[int],
    atr_k: float,
    rr: float,
    atr_period: int,
) -> int:
    """Fill missing outcomes. Running it again does not create a second row."""
    index_by_time = {bar.time: index for index, bar in enumerate(bars)}
    pending = conn.execute(
        """
        SELECT id, bar_time, direction FROM predictions
        WHERE symbol = ? AND timeframe = ? AND direction IN ('long', 'short')
        """,
        (symbol, timeframe),
    ).fetchall()
    written = 0
    now = _utc()
    for prediction in pending:
        entry_index = index_by_time.get(str(prediction["bar_time"]))
        if entry_index is None:
            continue
        for horizon in horizons:
            exists = conn.execute(
                "SELECT 1 FROM outcomes WHERE prediction_id = ? AND horizon_bars = ?",
                (prediction["id"], horizon),
            ).fetchone()
            if exists is not None:
                continue
            resolved = resolve_trade(
                bars,
                entry_index,
                str(prediction["direction"]),
                horizon,
                atr_k=atr_k,
                rr=rr,
                atr_period=atr_period,
            )
            if resolved is None:
                continue
            conn.execute(
                """
                INSERT INTO outcomes (
                    prediction_id, horizon_bars, forward_return, hit, mfe, mae,
                    r_multiple_at_rr, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction["id"],
                    horizon,
                    resolved.forward_return,
                    resolved.hit,
                    resolved.mfe,
                    resolved.mae,
                    resolved.r_multiple,
                    now,
                ),
            )
            written += 1
    conn.commit()
    return written


def samples_for(conn: sqlite3.Connection, agent_id: str, horizon: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT p.score AS score, o.hit AS hit, o.r_multiple_at_rr AS r
        FROM outcomes o
        JOIN predictions p ON p.id = o.prediction_id
        WHERE p.agent_id = ? AND o.horizon_bars = ?
          AND COALESCE(json_extract(p.features_json, '$.ok'), 1) != 0
          AND NOT EXISTS (
            SELECT 1 FROM shadow_status s
            WHERE s.symbol = p.symbol AND s.timeframe = p.timeframe
              AND s.bar_time = p.bar_time AND s.agent_id = p.agent_id
              AND s.agent_version = p.agent_version AND s.status = 'degraded'
          )
        ORDER BY o.resolved_at, o.prediction_id
        """,
        (agent_id, horizon),
    ).fetchall()
    return [
        {"score": row["score"], "hit": row["hit"], "r": row["r"]}
        for row in rows
    ]


def latest_predictions(conn: sqlite3.Connection, symbol: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.* FROM predictions p
        JOIN (
            SELECT agent_id, MAX(id) AS id
            FROM predictions
            WHERE symbol = ?
            GROUP BY agent_id
        ) latest ON latest.id = p.id
        ORDER BY p.agent_id
        """,
        (symbol,),
    ).fetchall()


def insert_proposal(conn: sqlite3.Connection, proposal: dict) -> int:
    cursor = conn.execute(
        """
        INSERT INTO proposals (
            created_at, kind, old_value, new_value, evidence_json, status, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            proposal.get("created_at") or _utc(),
            proposal["kind"],
            proposal["old_value"],
            proposal["new_value"],
            proposal["evidence_json"],
            proposal["status"],
            proposal.get("decided_at"),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def set_proposal_status(conn: sqlite3.Connection, proposal_id: int, status: str) -> None:
    conn.execute(
        "UPDATE proposals SET status = ?, decided_at = ? WHERE id = ?",
        (status, _utc(), proposal_id),
    )
    conn.commit()


def list_proposals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM proposals ORDER BY id DESC"
    ).fetchall()


def link_journal(conn: sqlite3.Connection, engine_db: Path) -> int:
    """Copy trade ids from the journal. Realised P&L stays empty when the journal has none."""
    if not engine_db.is_file():
        return 0
    uri = engine_db.resolve().as_uri() + "?mode=ro"
    source = sqlite3.connect(uri, uri=True)
    source.row_factory = sqlite3.Row
    try:
        trades = source.execute(
            "SELECT id, symbol FROM trades ORDER BY id"
        ).fetchall()
    except sqlite3.Error:
        source.close()
        return 0
    source.close()
    linked = 0
    for trade in trades:
        ids = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM predictions WHERE symbol = ? ORDER BY id",
                (trade["symbol"],),
            ).fetchall()
        ]
        conn.execute(
            """
            INSERT OR IGNORE INTO trades_link (journal_trade_id, prediction_ids, realised_pnl)
            VALUES (?, ?, NULL)
            """,
            (int(trade["id"]), json.dumps(ids)),
        )
        linked += 1
    conn.commit()
    return linked
