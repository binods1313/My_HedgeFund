"""SQLite journal: schema, migrations, and typed accessors.

WAL mode. The UI reads engine-owned tables and inserts only into `commands`;
the engine owns every other write. One writer at a time.

Migrations are numbered, forward-only, and each runs in its own transaction with
a `schema_version` row written on success. Startup aborts if the on-disk schema
is newer than this module understands: an engine that guesses at an unknown
schema is more dangerous than one that refuses to start.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Bump when adding a migration. Never edit a shipped migration in place.
SCHEMA_VERSION = 1

# -- state machine values, mirrored from engine.state -------------------------
STATE_OFF = "OFF"
STATE_IDLE = "IDLE"
STATE_SCANNING = "SCANNING"
STATE_ANALYZING = "ANALYZING"
STATE_VALIDATING = "VALIDATING"
STATE_EXECUTING = "EXECUTING"
STATE_MONITORING = "MONITORING"
STATE_PAUSED = "PAUSED"
STATE_EMERGENCY_STOP = "EMERGENCY_STOP"

ALL_STATES = (
    STATE_OFF, STATE_IDLE, STATE_SCANNING, STATE_ANALYZING, STATE_VALIDATING,
    STATE_EXECUTING, STATE_MONITORING, STATE_PAUSED, STATE_EMERGENCY_STOP,
)

# -- command status values ----------------------------------------------------
CMD_PENDING = "PENDING"
CMD_IN_PROGRESS = "IN_PROGRESS"
CMD_DONE = "DONE"
CMD_FAILED = "FAILED"

# -- trade status values ------------------------------------------------------
TRADE_PENDING = "PENDING"
TRADE_OPEN = "OPEN"
TRADE_CLOSED = "CLOSED"
TRADE_FAILED = "FAILED"
TRADE_ADOPTED = "ADOPTED"

# -- telegram approval event types --------------------------------------------
TELEGRAM_SIGNAL_FOUND = "SIGNAL_FOUND"
TELEGRAM_SIGNAL_SENT_TO_TELEGRAM = "SIGNAL_SENT_TO_TELEGRAM"
TELEGRAM_SIGNAL_APPROVED = "SIGNAL_APPROVED"
TELEGRAM_SIGNAL_REJECTED = "SIGNAL_REJECTED"
TELEGRAM_TRADE_EXECUTED = "TRADE_EXECUTED"
TELEGRAM_EXECUTION_FAILED = "EXECUTION_FAILED"
TELEGRAM_TRADE_CANCELLED = "TRADE_CANCELLED"

MIGRATION_1 = [
    """
    CREATE TABLE IF NOT EXISTS schema_version (
        version     INTEGER PRIMARY KEY,
        applied_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS engine_state (
        id          INTEGER PRIMARY KEY CHECK (id = 1),
        state       TEXT NOT NULL,
        mode        TEXT NOT NULL DEFAULT 'PAPER',
        reason      TEXT NOT NULL DEFAULT '',
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS commands (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        command      TEXT NOT NULL,
        params_json  TEXT NOT NULL DEFAULT '{}',
        status       TEXT NOT NULL DEFAULT 'PENDING',
        result       TEXT,
        reason       TEXT,
        dedupe_key   TEXT,
        ts           TEXT NOT NULL,
        claimed_at   TEXT,
        finished_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT NOT NULL,
        category      TEXT NOT NULL,
        severity      TEXT NOT NULL DEFAULT 'INFO',
        source        TEXT NOT NULL DEFAULT 'engine',
        message       TEXT NOT NULL,
        context_json  TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trades (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket      INTEGER UNIQUE,
        symbol      TEXT NOT NULL,
        side        TEXT NOT NULL,
        volume      REAL NOT NULL,
        entry_price REAL,
        exit_price  REAL,
        sl          REAL,
        tp          REAL,
        status      TEXT NOT NULL,
        magic       INTEGER NOT NULL DEFAULT 100100,
        broker      TEXT NOT NULL DEFAULT 'MT5',
        ts          TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        ticket      INTEGER PRIMARY KEY,
        symbol      TEXT NOT NULL,
        side        TEXT NOT NULL,
        volume      REAL NOT NULL,
        price_open  REAL NOT NULL,
        price_current REAL,
        sl          REAL,
        tp          REAL,
        profit      REAL NOT NULL DEFAULT 0.0,
        magic       INTEGER NOT NULL DEFAULT 100100,
        updated_at  TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quotes (
        symbol     TEXT PRIMARY KEY,
        bid        REAL NOT NULL,
        ask        REAL NOT NULL,
        spread     REAL,
        tick_time  INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS signals (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol     TEXT NOT NULL,
        direction  TEXT NOT NULL,
        score      REAL NOT NULL,
        bar_ts     INTEGER NOT NULL,
        ts         TEXT NOT NULL,
        UNIQUE (symbol, bar_ts)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_decisions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_id      INTEGER,
        symbol        TEXT NOT NULL,
        approved      INTEGER NOT NULL,
        reasons_json  TEXT NOT NULL DEFAULT '[]',
        ts            TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS llm_calls (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        provider   TEXT NOT NULL DEFAULT 'openrouter',
        decision   TEXT,
        confidence REAL,
        ok         INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER,
        error      TEXT,
        ts         TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scan_results (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        symbols_json TEXT NOT NULL,
        ranked_json  TEXT NOT NULL DEFAULT '[]',
        ts           TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity_snapshots (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        equity      REAL NOT NULL,
        balance     REAL,
        margin      REAL,
        open_risk   REAL,
        open_positions INTEGER NOT NULL DEFAULT 0,
        ts          TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS heartbeats (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        state     TEXT NOT NULL,
        pid       INTEGER,
        connected INTEGER NOT NULL DEFAULT 0,
        ts        TEXT NOT NULL
    )
    """,
    # Indexes sized to the actual access patterns: the command poll, the
    # heartbeat staleness probe, and recent-history reads.
    "CREATE INDEX IF NOT EXISTS idx_commands_status_ts ON commands (status, ts)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_commands_dedupe "
        "ON commands (dedupe_key) WHERE dedupe_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_events_ts ON events (ts)",
    "CREATE INDEX IF NOT EXISTS idx_events_category_ts ON events (category, ts)",
    "CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions (symbol)",
    "CREATE INDEX IF NOT EXISTS idx_quotes_tick_time ON quotes (tick_time)",
    "CREATE INDEX IF NOT EXISTS idx_trades_status ON trades (status)",
    "CREATE INDEX IF NOT EXISTS idx_signals_symbol_bar ON signals (symbol, bar_ts)",
    "CREATE INDEX IF NOT EXISTS idx_heartbeats_ts ON heartbeats (ts)",
    "CREATE INDEX IF NOT EXISTS idx_equity_ts ON equity_snapshots (ts)",
]

MIGRATIONS: dict[int, list[str]] = {1: MIGRATION_1}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SchemaTooNew(RuntimeError):
    """The database was written by a newer engine than this one."""


class Journal:
    """Thread-safe SQLite access.

    One connection guarded by a lock. SQLite in WAL mode permits concurrent
    readers with a single writer, so the UI reading while the engine writes does
    not block.
    """

    def __init__(self, path: str | Path, timeout: float = 10.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=timeout, check_same_thread=False,
            isolation_level=None,  # explicit transactions only
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self.migrate()

    def _configure(self) -> None:
        cur = self._conn.cursor()
        # WAL survives a crashed process; NORMAL sync is the standard pairing
        # and avoids an fsync per commit that a 1 s heartbeat would otherwise pay.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        # Fail loudly rather than hanging forever on a contended write.
        cur.execute("PRAGMA busy_timeout=10000")
        cur.close()

    # -- migrations ------------------------------------------------------

    def current_version(self) -> int:
        try:
            row = self._conn.execute(
                "SELECT MAX(version) AS v FROM schema_version").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row["v"]) if row and row["v"] is not None else 0

    def migrate(self) -> int:
        """Apply pending migrations. Returns the resulting version."""
        current = self.current_version()

        if current > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"Database at {self.path} is schema version {current}, but this "
                f"engine understands at most {SCHEMA_VERSION}. Refusing to "
                f"start: a newer engine has written this file."
            )

        for version in sorted(MIGRATIONS):
            if version <= current:
                continue
            statements = MIGRATIONS[version]
            with self._lock:
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for sql in statements:
                        self._conn.execute(sql)
                    self._conn.execute(
                        "INSERT OR REPLACE INTO schema_version (version, applied_at) "
                        "VALUES (?, ?)", (version, utc_now_iso()),
                    )
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                else:
                    self._conn.execute("COMMIT")
        return self.current_version()

    # -- core helpers ----------------------------------------------------

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchone()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    # -- events ----------------------------------------------------------

    def log_event(self, category: str, message: str, *, severity: str = "INFO",
                  source: str = "engine", context: dict | None = None) -> int:
        cur = self.execute(
            "INSERT INTO events (ts, category, severity, source, message, context_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (utc_now_iso(), category, severity, source, message,
             json.dumps(context or {}, default=str)),
        )
        return int(cur.lastrowid)

    def recent_events(self, limit: int = 50, category: str | None = None) -> list[dict]:
        if category:
            rows = self.query(
                "SELECT * FROM events WHERE category = ? ORDER BY id DESC LIMIT ?",
                (category, limit),
            )
        else:
            rows = self.query(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # -- engine state ----------------------------------------------------

    def set_state(self, state: str, *, reason: str = "", mode: str = "PAPER") -> None:
        self.execute(
            "INSERT INTO engine_state (id, state, mode, reason, updated_at) "
            "VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, mode=excluded.mode, "
            "reason=excluded.reason, updated_at=excluded.updated_at",
            (state, mode, reason, utc_now_iso()),
        )

    def get_state(self) -> dict | None:
        row = self.query_one("SELECT * FROM engine_state WHERE id = 1")
        return dict(row) if row else None

    # -- commands --------------------------------------------------------

    def enqueue_command(self, command: str, params: dict | None = None,
                        dedupe_key: str | None = None) -> int:
        cur = self.execute(
            "INSERT INTO commands (command, params_json, status, dedupe_key, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (command, json.dumps(params or {}, default=str), CMD_PENDING,
             dedupe_key, utc_now_iso()),
        )
        return int(cur.lastrowid)

    def claim_next_command(self) -> dict | None:
        """Atomically claim the oldest PENDING command.

        The UPDATE ... WHERE status='PENDING' is the guard: two pollers cannot
        claim the same row, and a restart cannot re-run an in-flight command.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM commands WHERE status = ? ORDER BY id LIMIT 1",
                (CMD_PENDING,),
            ).fetchone()
            if row is None:
                return None
            self._conn.execute(
                "UPDATE commands SET status = ?, claimed_at = ? "
                "WHERE id = ? AND status = ?",
                (CMD_IN_PROGRESS, utc_now_iso(), row["id"], CMD_PENDING),
            )
            return dict(row)

    def finish_command(self, command_id: int, status: str, *,
                       result: str = "", reason: str = "") -> None:
        self.execute(
            "UPDATE commands SET status = ?, result = ?, reason = ?, finished_at = ? "
            "WHERE id = ?",
            (status, result, reason, utc_now_iso(), command_id),
        )

    def pending_command_count(self) -> int:
        row = self.query_one(
            "SELECT COUNT(*) AS n FROM commands WHERE status = ?", (CMD_PENDING,))
        return int(row["n"]) if row else 0

    # -- heartbeats ------------------------------------------------------

    def heartbeat(self, state: str, *, connected: bool = False) -> None:
        import os as _os
        self.execute(
            "INSERT INTO heartbeats (state, pid, connected, ts) VALUES (?, ?, ?, ?)",
            (state, _os.getpid(), 1 if connected else 0, utc_now_iso()),
        )

    def last_heartbeat(self) -> dict | None:
        row = self.query_one("SELECT * FROM heartbeats ORDER BY id DESC LIMIT 1")
        return dict(row) if row else None

    def seconds_since_heartbeat(self) -> float | None:
        hb = self.last_heartbeat()
        if not hb:
            return None
        try:
            then = datetime.fromisoformat(hb["ts"])
        except (TypeError, ValueError):
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - then).total_seconds()

    # -- positions -------------------------------------------------------

    def upsert_position(self, **fields) -> None:
        ticket = int(fields["ticket"])
        self.execute(
            "INSERT INTO positions (ticket, symbol, side, volume, price_open, "
            "price_current, sl, tp, profit, magic, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticket) DO UPDATE SET symbol=excluded.symbol, "
            "side=excluded.side, volume=excluded.volume, "
            "price_open=excluded.price_open, price_current=excluded.price_current, "
            "sl=excluded.sl, tp=excluded.tp, profit=excluded.profit, "
            "magic=excluded.magic, updated_at=excluded.updated_at",
            (ticket, fields["symbol"], fields["side"], float(fields["volume"]),
             float(fields["price_open"]), fields.get("price_current"),
             fields.get("sl"), fields.get("tp"),
             float(fields.get("profit", 0.0)),
             int(fields.get("magic", 100100)), utc_now_iso()),
        )

    def delete_position(self, ticket: int) -> None:
        self.execute("DELETE FROM positions WHERE ticket = ?", (int(ticket),))

    def positions(self) -> list[dict]:
        return [dict(r) for r in self.query("SELECT * FROM positions ORDER BY ticket")]

    def open_position_count(self) -> int:
        row = self.query_one("SELECT COUNT(*) AS n FROM positions")
        return int(row["n"]) if row else 0

    # -- equity ----------------------------------------------------------

    def snapshot_equity(self, equity: float, *, balance: float | None = None,
                        margin: float | None = None, open_risk: float | None = None) -> None:
        self.execute(
            "INSERT INTO equity_snapshots (equity, balance, margin, open_risk, "
            "open_positions, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (float(equity), balance, margin, open_risk,
             self.open_position_count(), utc_now_iso()),
        )

    # -- risk decisions --------------------------------------------------

    def record_risk_decision(self, symbol: str, approved: bool,
                             reasons: list[str], trade_id: int | None = None) -> int:
        cur = self.execute(
            "INSERT INTO risk_decisions (trade_id, symbol, approved, reasons_json, ts) "
            "VALUES (?, ?, ?, ?, ?)",
            (trade_id, symbol, 1 if approved else 0,
             json.dumps(reasons), utc_now_iso()),
        )
        return int(cur.lastrowid)


def project_root() -> Path:
    """Absolute project root, derived from this file's location.

    An absolute path is required: the UI and the engine may run with different
    working directories, and a relative path would open two different databases.
    """
    return Path(__file__).resolve().parent.parent


def resolve_db_path(configured: str | None = None) -> Path:
    """Resolve the database path to an absolute location under the project root.

    A configured relative path is anchored to the project root. An absolute path
    is honoured as given, so a deployment can put the database elsewhere.
    """
    if configured is None:
        return project_root() / "data" / "engine.db"
    candidate = Path(configured)
    if candidate.is_absolute():
        return candidate
    return project_root() / candidate