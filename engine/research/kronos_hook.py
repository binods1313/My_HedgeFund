"""Kronos observational paper hook.

Strictly observational: when the engine writes a PENDING signal in PAPER
mode (and only when ``config.kronos.enabled``), it calls the Kronos service
and journals a ``kronos`` event next to the signal. It never gates, blocks,
modifies, or re-orders anything — the signal row is written first, and the
hook runs after, with every failure swallowed and journaled.

Fail-open contract: any transport error, timeout, None forecast, or malformed
response is journaled as a ``kronos_error`` event and the pipeline continues
unchanged. The hook never raises to its caller.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Optional

from engine.journal import Journal, utc_now_iso
from engine.research.kronos_client import KronosClient

MODEL_LABEL = "Kronos-small/Kronos-Tokenizer-base"
TIMEFRAME = "M15"

_DF_TIME_COL = "time"


def df_to_bars(df, context: int) -> list[dict]:
    """Convert a read_history DataFrame tail into bar dicts for the service."""
    import pandas as pd

    work = df.tail(context)
    out = []
    for _, row in work.iterrows():
        bar = {
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        t = row.get(_DF_TIME_COL)
        if t is None and isinstance(work.index, pd.DatetimeIndex):
            t = work.index[work.index.get_loc(row.name)]
        if t is not None:
            bar["time"] = pd.Timestamp(t).isoformat()
        vol = row.get("tick_volume")
        if vol is not None:
            bar["volume"] = float(vol)
        out.append(bar)
    return out


def journal_forecast(
    *,
    client: KronosClient,
    journal: Journal,
    cfg: Any,
    symbol: str,
    direction: str,
    bars: list[dict],
    decision_id: Optional[str] = None,
    bar_ts: Optional[int] = None,
    stamp: Optional[str] = None,
) -> None:
    """Fetch a forecast and journal it. Never raises. Returns None always."""
    started = time.monotonic()
    result: Optional[dict] = None
    error: Optional[str] = None
    try:
        result = client.forecast(
            symbol, bars,
            pred_len=int(cfg.horizon_bars),
            sample_count=int(cfg.sample_count),
        )
        if result is not None and (
                not isinstance(result, dict)
                or "direction" not in result
                or "agreement" not in result):
            error = "malformed response (missing direction/agreement)"
            result = None
        if result is None:
            error = error or "forecast unavailable (None/timeout/server)"
    except Exception as exc:  # noqa: BLE001 - fail-open
        error = f"{type(exc).__name__}: {exc}"
        result = None

    latency_ms = int((time.monotonic() - started) * 1000)
    ok = result is not None and error is None
    kind = "kronos_ok" if ok else "kronos_error"
    context: dict[str, Any] = {
        "kind": kind,
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "signal_direction": direction,
        "horizon_bars": int(cfg.horizon_bars),
        "sample_count": int(cfg.sample_count),
        "context_window": len(bars),
        "latency_ms": latency_ms,
        "decision_id": decision_id,
        "bar_ts": bar_ts,
        "model": MODEL_LABEL,
        "ts": stamp or utc_now_iso(),
    }
    if ok:
        context.update({
            "forecast_direction": result.get("direction"),
            "agreement": result.get("agreement"),
            "expected_move_pct": result.get("expected_move_pct"),
            "horizon_close_mean": result.get("horizon_close_mean"),
            "last_close": result.get("last_close"),
        })
    else:
        context["error"] = error or "forecast unavailable (None/timeout/server)"

    try:
        journal.log_event(
            "kronos",
            f"{symbol}: {kind} (signal={direction})",
            severity="INFO" if ok else "WARNING",
            source="engine",
            context=context,
        )
    except Exception:  # noqa: BLE001 - journaling must never break the loop
        pass


def build_kronos_hook(config: Any, journal: Journal) -> Optional[Callable]:
    """Return the hook callable, or None when it must stay disabled.

    Only wired when: kronos section exists, `enabled` is true, mode is PAPER
    (never LIVE), and the journal is usable.
    """
    kronos = getattr(config, "kronos", None)
    if kronos is None or not getattr(kronos, "enabled", False):
        return None
    if getattr(config, "mode", "PAPER") != "PAPER":
        return None
    client = KronosClient(endpoint=kronos.endpoint,
                          timeout_s=kronos.timeout_seconds)

    def hook(*, symbol: str, direction: str, df, decision_id=None,
             bar_ts=None, stamp=None) -> None:
        try:
            bars = df_to_bars(df, context=int(kronos.horizon_bars) + 64)
        except Exception as exc:  # noqa: BLE001
            journal.log_event(
                "kronos", f"{symbol}: bar conversion failed: {exc}",
                severity="WARNING", source="engine",
                context={"kind": "kronos_error", "symbol": symbol,
                         "ts": stamp or utc_now_iso()})
            return
        if len(bars) < 64:
            journal.log_event(
                "kronos", f"{symbol}: insufficient bars for forecast ({len(bars)})",
                severity="WARNING", source="engine",
                context={"kind": "kronos_error", "symbol": symbol,
                         "ts": stamp or utc_now_iso()})
            return
        journal_forecast(client=client, journal=journal, cfg=kronos,
                         symbol=symbol, direction=direction, bars=bars,
                         decision_id=decision_id, bar_ts=bar_ts, stamp=stamp)

    return hook
