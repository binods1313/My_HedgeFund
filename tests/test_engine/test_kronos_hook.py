"""Tests for the observational Kronos hook (fail-open, never gates)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd
import pytest
import requests

from engine.journal import Journal
from engine.research.kronos_hook import (
    build_kronos_hook,
    df_to_bars,
    journal_forecast,
)

MODEL = "Kronos-small/Kronos-Tokenizer-base"


def make_cfg(**overrides) -> SimpleNamespace:
    base = {
        "enabled": True,
        "endpoint": "http://127.0.0.1:8610",
        "timeout_seconds": 5.0,
        "horizon_bars": 8,
        "sample_count": 8,
        "min_agreement": 0.6,
        "max_age_seconds": 600,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_df(n: int = 128) -> pd.DataFrame:
    rng = __import__("numpy").random.default_rng(1)
    close = 1.08 + rng.normal(0, 0.0002, n).cumsum()
    op = close.copy(); op[0] = close[0]
    df = pd.DataFrame({
        "time": pd.date_range("2026-11-01", periods=n, freq="15min"),
        "open": op, "high": close + 0.0003, "low": close - 0.0003,
        "close": close, "tick_volume": rng.integers(100, 2000, n).astype(float),
    })
    return df


class FakeClient:
    """Returns a canned forecast or raises like the real client."""

    def __init__(self, result=None, exc=None, calls=None):
        self.result = result
        self.exc = exc
        self.calls = calls if calls is not None else []

    def forecast(self, symbol, bars, **kwargs):
        self.calls.append((symbol, len(bars), kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


GOOD = {
    "symbol": "EURUSD", "pred_len": 8, "sample_count": 8,
    "last_close": 1.08100, "horizon_close_mean": 1.08130,
    "direction": "UP", "agreement": 0.75, "expected_move_pct": 0.03,
    "final_close_pctiles": [1.08, 1.0813, 1.0825],
    "bars": [],
}


def _event(journal, kind):
    rows = journal.recent_events(limit=50, category="kronos")
    for r in rows:
        ctx = json.loads(r["context_json"])
        if ctx.get("kind") == kind:
            return r, ctx
    return None, None


def test_df_to_bars_rejects_short():
    df = make_df(10)
    bars = df_to_bars(df, context=8)
    assert len(bars) == 8
    assert set(bars[0]) >= {"open", "high", "low", "close", "time", "volume"}


def test_journal_forecast_success(tmp_path):
    journal = Journal(tmp_path / "k.db")
    client = FakeClient(result=GOOD)
    journal_forecast(client=client, journal=journal, cfg=make_cfg(),
                     symbol="EURUSD", direction="BUY",
                     bars=df_to_bars(make_df(), 128), decision_id="d1",
                     bar_ts=123, stamp="2026-11-04T00:00:00Z")
    row, ctx = _event(journal, "kronos_ok")
    assert row is not None
    assert ctx["signal_direction"] == "BUY"
    assert ctx["forecast_direction"] == "UP"
    assert ctx["agreement"] == 0.75
    assert ctx["context_window"] == 128
    assert ctx["horizon_bars"] == 8
    assert ctx["decision_id"] == "d1"
    assert ctx["bar_ts"] == 123
    assert ctx["model"] == MODEL
    assert "latency_ms" in ctx


@pytest.mark.parametrize("exc", [requests.ConnectionError("down"),
                                 requests.Timeout("slow")])
def test_journal_forecast_fail_open_on_transport(tmp_path, exc):
    journal = Journal(tmp_path / "k.db")
    client = FakeClient(exc=exc)
    journal_forecast(client=client, journal=journal, cfg=make_cfg(),
                     symbol="EURUSD", direction="SELL",
                     bars=df_to_bars(make_df(), 128))
    row, ctx = _event(journal, "kronos_error")
    assert row is not None
    assert "error" in ctx
    assert "Connection" in ctx["error"] or "Timeout" in ctx["error"]


def test_journal_forecast_none_is_error(tmp_path):
    journal = Journal(tmp_path / "k.db")
    client = FakeClient(result=None)
    journal_forecast(client=client, journal=journal, cfg=make_cfg(),
                     symbol="EURUSD", direction="BUY",
                     bars=df_to_bars(make_df(), 128))
    _, ctx = _event(journal, "kronos_error")
    assert ctx is not None
    assert "unavailable" in ctx["error"]


def test_journal_forecast_malformed_is_error(tmp_path):
    journal = Journal(tmp_path / "k.db")
    client = FakeClient(result={"not": "a forecast"})
    journal_forecast(client=client, journal=journal, cfg=make_cfg(),
                     symbol="EURUSD", direction="BUY",
                     bars=df_to_bars(make_df(), 128))
    _, ctx = _event(journal, "kronos_error")
    assert ctx is not None


def test_build_hook_disabled_when_not_enabled(tmp_path):
    journal = Journal(tmp_path / "k.db")
    cfg_disabled = make_cfg(enabled=False)
    assert build_kronos_hook(cfg_disabled, journal) is None


def test_build_hook_disabled_in_live(tmp_path):
    journal = Journal(tmp_path / "k.db")
    cfg_live = SimpleNamespace(mode="LIVE", kronos=make_cfg())
    assert build_kronos_hook(cfg_live, journal) is None


def test_build_hook_returns_callable(tmp_path):
    journal = Journal(tmp_path / "k.db")
    cfg_paper = SimpleNamespace(mode="PAPER", kronos=make_cfg())
    hook = build_kronos_hook(cfg_paper, journal)
    assert hook is not None
    # missing bars -> journals error, does not raise
    hook(symbol="EURUSD", direction="BUY", df=make_df(10), decision_id="x")
    _, ctx = _event(journal, "kronos_error")
    assert ctx is not None
