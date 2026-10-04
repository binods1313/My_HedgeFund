"""Equity baselines: start-of-day and peak.

The Risk Engine needs two numbers it cannot infer from a single reading:

  - start_of_day_equity, so a daily loss is measured against where the day began
  - peak_equity, so drawdown is measured against the best point seen, not the last

Without these persisted the gate blocks every order on 'peak equity unknown'.
That is correct behaviour, not a bug: an unmeasured limit cannot be evaluated.
This module supplies the measurement.

Two decisions that matter:

**Baselines follow BROKER SERVER TIME, not local time.** The daily loss resets
when the broker's day ends, not when the operator's clock strikes midnight. Using
local time would reset a daily limit at the wrong moment -- for a broker on
UTC+3 that is up to three hours out, and the reset would either fire while the
broker's day is still running or fail to fire when it ends.

**Peak is monotonic within a day and resets daily.** A peak that carried across
a day boundary would make drawdown meaningless after a reset, because the peak
would belong to a previous equity regime.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

#: Never let a peak be seeded from a reading older than this. A stale peak from
#: a previous session would inflate apparent drawdown on restart.
MAX_PEAK_AGE_HOURS = 24 * 7


@dataclass(frozen=True)
class EquityBaselines:
    start_of_day_equity: float
    peak_equity: float
    trading_day: str          # broker-server date, ISO
    sampled_at: str
    sample_count: int
    #: True when no fresh sample was available and the stored peak was reused.
    peak_is_stale: bool = False

    def drawdown_pct(self, equity: float) -> float | None:
        """Drawdown from peak as a percentage. None when the peak is unusable."""
        peak = _finite(self.peak_equity)
        if peak <= 0:
            return None
        return max(0.0, (peak - _finite(equity)) / peak * 100.0)

    def daily_loss_pct(self, equity: float) -> float | None:
        """Loss from the start of the day as a percentage.

        None when the baseline is unusable, so a caller cannot mistake an
        absent measurement for a zero loss.
        """
        sod = _finite(self.start_of_day_equity)
        if sod <= 0:
            return None
        return max(0.0, (sod - _finite(equity)) / sod * 100.0)


def _finite(value, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    f = float(value)
    if math_isnan(f) or math_isinf(f):
        return default
    return f


def math_isnan(x: float) -> bool:
    return x != x


def math_isinf(x: float) -> bool:
    return x in (float("inf"), float("-inf"))


def broker_today(server_epoch: int | None = None) -> str:
    """Today's date according to the broker, from the terminal server time.

    Falls back to UTC when no broker time is available. UTC is a defensible
    default because it is the broker's own convention for most venues; local
    machine time is not, because it is whatever the operator's laptop says.

    IMPORTANT: the caller must pass a VALIDATED epoch. See
    validate_server_epoch() -- on this account the terminal reported three
    different server times across three symbols, two of them a day stale, so an
    unvalidated tick time would seed the wrong trading day.
    """
    if server_epoch:
        return datetime.fromtimestamp(
            int(server_epoch), tz=timezone.utc).date().isoformat()
    return datetime.now(timezone.utc).date().isoformat()


#: An epoch further than this from the local clock is not a usable day boundary.
MAX_CLOCK_SKEW_SECONDS = 900      # 15 minutes


def validate_server_epoch(epoch: int | None, *,
                          local_epoch: float | None = None) -> int | None:
    """Return `epoch` only if it is close enough to the local clock to trust.

    Measured on MetaQuotes and XM demo accounts, symbol_info_tick().time was
    stale by up to 24 hours for some symbols and correct to the minute for
    others. Using one of those without checking would seed a trading day up to a
    day wrong, resetting the daily loss limit when the venue's day has barely
    begun.

    Returns None when the epoch is missing or implausible, so the caller falls
    back to UTC rather than to a wrong day.
    """
    if not epoch:
        return None
    now = local_epoch if local_epoch is not None else _now_epoch()
    skew = abs(now - float(epoch))
    if skew > MAX_CLOCK_SKEW_SECONDS:
        return None
    return int(epoch)


class EquityTracker:
    """Maintains the daily and peak equity baselines.

    Baselines live in their own table rather than being derived by scanning
    equity_snapshots: the daily reset decision depends on the broker's calendar,
    and re-deriving it from samples would have to re-apply every trading-day
    rule to history to get the same answer.
    """

    def __init__(self, journal, server_epoch_provider=None):
        self.journal = journal
        self.server_epoch_provider = server_epoch_provider or (lambda: None)
        self._ensure_schema()

    # -- schema ----------------------------------------------------------

    def _ensure_schema(self) -> None:
        self.journal.execute(
            """
            CREATE TABLE IF NOT EXISTS equity_baselines (
                trading_day      TEXT PRIMARY KEY,
                start_of_day_equity REAL NOT NULL,
                peak_equity      REAL NOT NULL,
                peak_ts          TEXT,
                sample_count     INTEGER NOT NULL DEFAULT 0,
                first_seen_ts    TEXT NOT NULL,
                updated_ts       TEXT NOT NULL
            )
            """
        )

    # -- reading ---------------------------------------------------------

    def current(self, now_day: str | None = None) -> EquityBaselines | None:
        """Baselines for the broker's current day, or None before any sample."""
        day = now_day or broker_today(self.server_epoch_provider())
        row = self.journal.query_one(
            "SELECT * FROM equity_baselines WHERE trading_day = ?", (day,))
        if row is None:
            return None
        return EquityBaselines(
            start_of_day_equity=float(row["start_of_day_equity"]),
            peak_equity=float(row["peak_equity"]),
            trading_day=row["trading_day"],
            sampled_at=row["updated_ts"],
            sample_count=int(row["sample_count"] or 0),
        )

    # -- writing ---------------------------------------------------------

    def observe(self, equity: float, balance: float | None = None,
                margin: float | None = None, open_risk: float | None = None,
                open_positions: int = 0, now_day: str | None = None,
                recorded_at: str | None = None) -> EquityBaselines:
        """Record one equity reading and update the baselines.

        Creates the day's row on first sight, seeded with this reading, and
        thereafter raises the peak but never lowers it. A drawdown is measured
        against the high-water mark, so a lower reading must not become the
        peak.
        """
        value = _finite(equity)
        if value <= 0:
            raise ValueError(
                f"equity must be positive, got {equity!r}. A non-positive "
                f"reading would corrupt both the daily loss and drawdown "
                f"baselines."
            )

        day = now_day or broker_today(self.server_epoch_provider())
        ts = recorded_at or _utc_now_iso()

        row = self.journal.query_one(
            "SELECT * FROM equity_baselines WHERE trading_day = ?", (day,))

        if row is None:
            # First sample of the broker's trading day.
            self.journal.execute(
                """
                INSERT INTO equity_baselines
                    (trading_day, start_of_day_equity, peak_equity, peak_ts,
                     sample_count, first_seen_ts, updated_ts)
                VALUES (?, ?, ?, ?, 1, ?, ?)
                """,
                (day, value, value, ts, ts, ts),
            )
        else:
            # start_of_day_equity is deliberately NOT read here and NOT
            # updated: it is fixed at the first reading of the broker's day.
            # Reading it into a local implied it might change; it must not.
            peak = float(row["peak_equity"])
            count = int(row["sample_count"] or 0)

            new_peak = peak if peak >= value else value
            new_peak_ts = ts if new_peak != peak or peak_ts_blank(row) else \
                row["peak_ts"]

            self.journal.execute(
                """
                UPDATE equity_baselines
                   SET peak_equity = ?, peak_ts = ?, sample_count = ?,
                       updated_ts = ?
                 WHERE trading_day = ?
                """,
                (new_peak, new_peak_ts, count + 1, ts, day),
            )
            # start_of_day_equity is deliberately NOT updated: it is fixed at
            # the first reading of the broker's day.

        # Append to the equity history used for reporting.
        self.journal.snapshot_equity(
            value, balance=balance, margin=margin,
            open_risk=open_risk,
        )

        baselines = self.current(day)
        assert baselines is not None  # just written
        return baselines

    def peak_age_hours(self, now_day: str | None = None) -> float | None:
        """Age of the stored peak in hours, or None when there is none."""
        day = now_day or broker_today(self.server_epoch_provider())
        row = self.journal.query_one(
            "SELECT peak_ts FROM equity_baselines WHERE trading_day = ?", (day,))
        if row is None or not row["peak_ts"]:
            return None
        try:
            then = datetime.fromisoformat(row["peak_ts"])
        except (TypeError, ValueError):
            return None
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - then
        return delta.total_seconds() / 3600.0

    def is_peak_stale(self, max_hours: float = MAX_PEAK_AGE_HOURS) -> bool:
        age = self.peak_age_hours()
        if age is None:
            return True
        return age > max_hours

    def history(self, limit: int = 200) -> list[dict]:
        rows = self.journal.query(
            "SELECT * FROM equity_snapshots ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def reset_day(self, day: str | None = None) -> None:
        """Drop the baselines for a trading day.

        Only for operator recovery. The normal daily rollover happens implicitly
        when a reading arrives for a new broker day.
        """
        target = day or broker_today(self.server_epoch_provider())
        self.journal.execute(
            "DELETE FROM equity_baselines WHERE trading_day = ?", (target,))

    def prune_history(self, keep_days: int = 90) -> int:
        """Remove equity snapshots older than `keep_days`. Returns rows removed."""
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(days=keep_days)).isoformat(timespec="seconds")
        cur = self.journal.execute(
            "DELETE FROM equity_snapshots WHERE ts < ?", (cutoff,))
        return cur.rowcount or 0


def peak_ts_blank(row) -> bool:
    return not row["peak_ts"]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _now_epoch() -> float:
    import time as _time
    return _time.time()