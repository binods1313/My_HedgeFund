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
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Bump when adding a migration. Never edit a shipped migration in place.
SCHEMA_VERSION = 4

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

MIGRATION_2 = [
    """
    -- The account this journal belongs to. One row, enforced by the CHECK.
    -- A journal written for account A and later attached to account B is the
    -- failure mode this table exists to make impossible to do silently.
    CREATE TABLE IF NOT EXISTS journal_account (
        id              INTEGER PRIMARY KEY CHECK (id = 1),
        login           INTEGER,
        currency        TEXT,
        server          TEXT,
        magic           INTEGER,
        recorded_at     TEXT NOT NULL
    )
    """,
    # Mirrored onto engine_state so a single SELECT can render the account
    # strip alongside the engine state. Not authoritative: journal_account is.
    "ALTER TABLE engine_state ADD COLUMN account_login INTEGER",
    "ALTER TABLE engine_state ADD COLUMN account_currency TEXT",
]

MIGRATION_3 = [
    # Cycle header columns. The original symbols_json/ranked_json blob stays
    # so a reader of schema 2 rows still has those fields. New cycles write
    # one header here and one row per symbol in scan_symbols.
    "ALTER TABLE scan_results ADD COLUMN cycle_id TEXT",
    "ALTER TABLE scan_results ADD COLUMN status TEXT",
    "ALTER TABLE scan_results ADD COLUMN reason TEXT",
    "ALTER TABLE scan_results ADD COLUMN symbol_count INTEGER",
    """
    CREATE TABLE IF NOT EXISTS scan_symbols (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        cycle_id  TEXT NOT NULL,
        symbol    TEXT NOT NULL,
        status    TEXT NOT NULL,
        reason    TEXT NOT NULL DEFAULT '',
        bar_ts    TEXT,
        ts        TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_scan_symbols_cycle ON scan_symbols (cycle_id, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_scan_results_cycle ON scan_results (cycle_id)",
    # Age of the last successful gateway call, in seconds, at the moment the
    # heartbeat was written. Added before this migration was applied to the
    # live journal, so the live file still lands on version 3.
    "ALTER TABLE heartbeats ADD COLUMN gateway_call_age_seconds REAL",
]

MIGRATION_4 = [
    """
    CREATE TABLE signals_v4 (
        id                          INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol                      TEXT NOT NULL,
        direction                   TEXT NOT NULL,
        score                       REAL NOT NULL,
        bar_ts                      INTEGER,
        ts                          TEXT NOT NULL,
        decision_id                 TEXT NOT NULL UNIQUE,
        status                      TEXT NOT NULL DEFAULT 'LEGACY'
                                    CHECK (status IN (
                                        'PENDING', 'APPROVED', 'REJECTED',
                                        'EXPIRED', 'SUPERSEDED', 'EXECUTED',
                                        'FAILED', 'NEEDS_REVIEW', 'LEGACY'
                                    )),
        strategy                    TEXT NOT NULL DEFAULT 'confluence',
        timeframe                   TEXT NOT NULL DEFAULT 'M15',
        entry_price                 REAL,
        stop_loss                   REAL,
        take_profit                 REAL,
        volume                      REAL,
        expires_at                  TEXT,
        decided_by                  TEXT,
        decided_at                  TEXT,
        blocks_json                 TEXT NOT NULL DEFAULT '{}',
        reject_reason               TEXT,
        superseded_by_decision_id   TEXT,
        is_test                     INTEGER NOT NULL DEFAULT 0
                                    CHECK (is_test IN (0, 1)),
        test_tag                    TEXT,
        CHECK (bar_ts IS NOT NULL OR is_test = 1),
        CHECK (status = 'LEGACY' OR expires_at IS NOT NULL)
    )
    """,
    """
    INSERT INTO signals_v4 (
        id, symbol, direction, score, bar_ts, ts, decision_id, status,
        strategy, timeframe, blocks_json, is_test
    )
    SELECT id, symbol, direction, score, bar_ts, ts,
           'LEGACY-' || id, 'LEGACY', 'legacy', 'UNKNOWN', '{}', 0
    FROM signals
    """,
    "DROP TABLE signals",
    "ALTER TABLE signals_v4 RENAME TO signals",
    """
    CREATE UNIQUE INDEX idx_signals_bar_strategy
    ON signals (symbol, strategy, bar_ts)
    WHERE is_test = 0 AND bar_ts IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX idx_signals_one_pending
    ON signals (symbol)
    WHERE status = 'PENDING'
    """,
]

MIGRATIONS: dict[int, list[str]] = {
    1: MIGRATION_1, 2: MIGRATION_2, 3: MIGRATION_3, 4: MIGRATION_4,
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def backup_journal(source: str | Path,
                   destination: str | Path) -> Path:
    """Create a consistent, non-overwriting SQLite backup.

    A passive WAL checkpoint is requested first. SQLite's online backup API
    then copies a consistent snapshot including any committed WAL content;
    copying the main database file alone is never sufficient while WAL is in
    use. The destination is published only after SQLite integrity validation.
    """
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    if source_path == destination_path:
        raise ValueError("journal backup source and destination must differ")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    if destination_path.exists():
        raise FileExistsError(destination_path)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.",
        suffix=".backup",
        dir=destination_path.parent,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)
    try:
        with closing(sqlite3.connect(
                str(source_path), timeout=30.0)) as source_conn:
            source_conn.execute("PRAGMA busy_timeout = 30000")
            source_conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
            with closing(sqlite3.connect(
                    str(temporary_path), timeout=30.0)) as backup_conn:
                source_conn.backup(backup_conn)
                integrity = backup_conn.execute(
                    "PRAGMA integrity_check").fetchone()
                if integrity is None or integrity[0] != "ok":
                    raise sqlite3.DatabaseError(
                        "journal backup integrity check failed: "
                        f"{None if integrity is None else integrity[0]}")
        if destination_path.exists():
            raise FileExistsError(destination_path)
        os.rename(temporary_path, destination_path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()
    return destination_path


class SchemaTooOld(RuntimeError):
    """The journal is older than this code. Callers must not migrate it.

    The UI uses this. The engine is the only writer that may migrate, and
    only after the operator has stopped the previous process and taken a
    backup. The message is the exact string the dashboard shows.
    """

    TEXT = "Journal schema older than code"


class SchemaTooNew(RuntimeError):
    """The database was written by a newer engine than this one."""


class Journal:
    """Thread-safe SQLite access.

    One connection guarded by a lock. SQLite in WAL mode permits concurrent
    readers with a single writer, so the UI reading while the engine writes does
    not block.
    """

    def __init__(self, path: str | Path, timeout: float = 10.0, *,
                 migrate: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.path), timeout=timeout, check_same_thread=False,
            isolation_level=None,  # explicit transactions only
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        if migrate:
            self.migrate()
        else:
            self._refuse_schema_change(existed)

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

    def _refuse_schema_change(self, existed: bool) -> None:
        """Open a journal without migrating an existing file.

        A path that does not exist yet is created at the current schema.
        That is a new file, not an upgrade. An older file is left at its
        own version and refused. A newer file is refused, same as migrate().
        """
        if not existed:
            self.migrate()
            return
        current = self.current_version()
        if current < SCHEMA_VERSION:
            self.close()
            raise SchemaTooOld(SchemaTooOld.TEXT)
        if current > SCHEMA_VERSION:
            raise SchemaTooNew(
                f"Database at {self.path} is schema version {current}, but this "
                f"engine understands at most {SCHEMA_VERSION}. Refusing to "
                f"start: a newer engine has written this file."
            )

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

    def set_state(self, state: str, *, reason: str = "", mode: str = "PAPER",
                  account_login: int | None = None,
                  account_currency: str | None = None) -> None:
        """Record engine state, and optionally the account it applies to.

        The account columns are written whenever the caller knows them, so the
        UI can show the fingerprint next to the state without a second query.
        When unknown they are written as NULL rather than guessed: an engine
        that stamps an unverified login onto its own state row is the exact
        bug this whole mechanism exists to prevent.
        """
        self.execute(
            "INSERT INTO engine_state (id, state, mode, reason, updated_at, "
            "account_login, account_currency) VALUES (1, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET state=excluded.state, "
            "mode=excluded.mode, reason=excluded.reason, "
            "updated_at=excluded.updated_at, "
            "account_login=COALESCE(excluded.account_login, "
            "engine_state.account_login), "
            "account_currency=COALESCE(excluded.account_currency, "
            "engine_state.account_currency)",
            (state, mode, reason, utc_now_iso(), account_login,
             account_currency),
        )

    # -- account fingerprint ------------------------------------------------

    def set_account_fingerprint(self, *, login: int | None = None,
                                currency: str | None = None,
                                server: str | None = None,
                                magic: int | None = None) -> None:
        """Stamp the account this journal belongs to.

        Called once at startup after the guard has confirmed the logged-in
        account. Deliberately not called on a mismatch: a journal for account A
        must never be re-stamped for account B.

        A NULL login is never written. The old form, ``INSERT OR REPLACE``
        with default-NULL arguments, blanked the stamp from any racing writer
        that had no account to record -- and a journal stamped with NULLs then
        reads as unstamped, letting the *next* account adopt it. A stamp
        requires a login; a writer that has none has nothing to stamp.
        """
        if login is None:
            # The NULL-stamp race, closed at the write itself: no login means
            # no identity, and a row of NULLs would read as "unstamped".
            return
        self.execute(
            "INSERT OR REPLACE INTO journal_account "
            "(id, login, currency, server, magic, recorded_at) "
            "VALUES (1, ?, ?, ?, ?, ?)",
            (login, currency, server, magic, utc_now_iso()),
        )

    def get_account_fingerprint(self) -> dict | None:
        """The account this journal was created for, or None if unset."""
        if self.current_version() < 2:
            return None
        return self.query_one(
            "SELECT login, currency, server, magic, recorded_at "
            "FROM journal_account WHERE id = 1")

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

    def heartbeat(self, state: str, *, connected: bool = False,
                  gateway_call_age: float | None = None) -> None:
        import os as _os
        self.execute(
            "INSERT INTO heartbeats "
            "(state, pid, connected, ts, gateway_call_age_seconds) "
            "VALUES (?, ?, ?, ?, ?)",
            (state, _os.getpid(), 1 if connected else 0, utc_now_iso(),
             None if gateway_call_age is None else float(gateway_call_age)),
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

    # -- signals -----------------------------------------------------------

    def record_signal(self, *, symbol: str, direction: str, score: float,
                      bar_ts: int | None,
                      decision_id: str,
                      status: str,
                      strategy: str = "confluence",
                      timeframe: str = "M15",
                      entry_price: float | None = None,
                      stop_loss: float | None = None,
                      take_profit: float | None = None,
                      volume: float | None = None,
                      expires_at: str | None = None,
                      decided_by: str | None = None,
                      decided_at: str | None = None,
                      blocks_json: str | None = None,
                      reject_reason: str | None = None,
                      superseded_by_decision_id: str | None = None,
                      is_test: bool = False,
                      test_tag: str | None = None) -> None:
        """Insert a signal row and supersede any existing PENDING signal.

        The v4 schema enforces a single PENDING row per symbol
        (``idx_signals_one_pending``). When a new PENDING signal is written,
        any prior PENDING row for the same symbol is marked SUPERSEDED.

        For non-test signals: bar_ts must be non-NULL (enforced by the
        table CHECK) and a unique ``(symbol, strategy, bar_ts)`` combination
        is required. Duplicate insertions raise a sqlite ``IntegrityError``
        so the caller knows the signal was already recorded for that bar.
        """
        now = utc_now_iso()
        if status == "PENDING" and not is_test:
            self.execute(
                "UPDATE signals SET status='SUPERSEDED', "
                "superseded_by_decision_id=? WHERE symbol=? AND status='PENDING'",
                (decision_id, symbol),
            )
        self.execute(
            "INSERT INTO signals ("
            "  symbol, direction, score, bar_ts, ts, decision_id, status, "
            "  strategy, timeframe, entry_price, stop_loss, take_profit, "
            "  volume, expires_at, decided_by, decided_at, blocks_json, "
            "  reject_reason, superseded_by_decision_id, is_test, test_tag) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, direction, float(score), bar_ts, now, decision_id, status,
             strategy, timeframe, entry_price, stop_loss, take_profit,
             volume, expires_at, decided_by, decided_at,
             blocks_json or "{}",
             reject_reason, superseded_by_decision_id,
             1 if is_test else 0,
             test_tag),
        )

    # -- trade lifecycle events (§3 gate: one decision ID, real timestamps) --

    #: Event categories the Journal page filters on. Kept as constants so
    #: producers and the UI cannot drift apart by typo.
    LIFECYCLE_SIGNAL_GENERATED = "signal_generated"
    LIFECYCLE_SIGNAL_REJECTED = "signal_rejected"
    LIFECYCLE_RISK_APPROVED = "risk_approved"
    LIFECYCLE_RISK_REJECTED = "risk_rejected"
    LIFECYCLE_ORDER_SENT = "order_sent"
    LIFECYCLE_ORDER_FILLED = "order_filled"
    LIFECYCLE_ORDER_MODIFIED = "order_modified"
    LIFECYCLE_ORDER_CLOSED = "order_closed"
    LIFECYCLE_MT5_RECONNECT = "mt5_reconnect"
    LIFECYCLE_SPREAD_BLOCKED = "spread_filter_blocked"
    LIFECYCLE_NEWS_BLOCKED = "news_filter_blocked"

    LIFECYCLE_CATEGORIES = frozenset({
        LIFECYCLE_SIGNAL_GENERATED, LIFECYCLE_SIGNAL_REJECTED,
        LIFECYCLE_RISK_APPROVED, LIFECYCLE_RISK_REJECTED,
        LIFECYCLE_ORDER_SENT, LIFECYCLE_ORDER_FILLED,
        LIFECYCLE_ORDER_MODIFIED, LIFECYCLE_ORDER_CLOSED,
        LIFECYCLE_MT5_RECONNECT, LIFECYCLE_SPREAD_BLOCKED,
        LIFECYCLE_NEWS_BLOCKED,
    })

    def log_lifecycle(self, category: str, message: str, *,
                      decision_id: str | None = None,
                      severity: str = "INFO", source: str = "engine",
                      context: dict | None = None) -> int:
        """Write one linked lifecycle row. Every row carries decision_id."""
        if category not in self.LIFECYCLE_CATEGORIES:
            raise ValueError(f"unknown lifecycle category {category!r}")
        payload = dict(context or {})
        if decision_id is not None:
            payload["decision_id"] = decision_id
        return self.log_event(category, message, severity=severity,
                              source=source, context=payload)

    def lifecycle_chain(self, decision_id: str) -> list[dict]:
        """All lifecycle rows for one decision ID, oldest first."""
        rows = self.query(
            "SELECT * FROM events ORDER BY id ASC",
        )
        out = []
        for row in rows:
            try:
                ctx = json.loads(row["context_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if (row["category"] in self.LIFECYCLE_CATEGORIES
                    and ctx.get("decision_id") == decision_id):
                out.append(dict(row))
        return out


def project_root() -> Path:
    """Absolute project root, derived from this file's location.

    An absolute path is required: the UI and the engine may run with different
    working directories, and a relative path would open two different databases.
    """
    return Path(__file__).resolve().parent.parent


def resolve_db_path(configured: str) -> Path:
    """Resolve the *configured* database path to an absolute location.

    A configured relative path is anchored to the project root; an absolute
    path is honoured as given, so a deployment can put the database elsewhere.

    The argument is deliberately mandatory. It used to default to a hardcoded
    `data/engine.db`, and four call sites in the UI passed nothing, so they
    silently kept reading the old journal the moment `config.yaml` pointed at a
    new one. Requiring the value makes that mistake impossible to write: a
    caller must say which journal it means, and there is exactly one place that
    decides -- `engine/config.py`, reading `config.yaml`.
    """
    if configured is None:
        raise TypeError(
            "resolve_db_path() requires the configured journal path from "
            "engine.config.load_engine_config().engine.db_path; there is no "
            "default, because a silent default is how two processes end up "
            "reading different journals."
        )
    candidate = Path(configured)
    if candidate.is_absolute():
        return candidate
    return project_root() / candidate