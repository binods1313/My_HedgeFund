"""One RunSnapshot object is shared by every component in a run."""

from __future__ import annotations

import datetime as dt

import pandas as pd

from ui.components import snapshot as snap
from ui.data_access import AccountSnapshot
from ui.services.news import FetchResult


def _account() -> AccountSnapshot:
    return AccountSnapshot(
        connected=False,
        mode="PAPER",
        engine_state="offline",
        heartbeat_age_s=None,
        currency="USD",
        balance=99995.56,
        equity=99995.55,
        margin_used=0.0,
        free_margin=99995.55,
        margin_level=None,
        open_pnl=-3.67,
        open_risk=float("inf"),
        fetched_at=dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc),
    )


def test_two_callers_in_one_run_receive_the_same_snapshot(monkeypatch) -> None:
    snap._CACHE.clear()
    state: dict = {"selected_asset": "EURUSD"}
    monkeypatch.setattr(snap, "st", type("Session", (), {"session_state": state})())
    calls = {"account": 0, "positions": 0}
    positions = pd.DataFrame([{"ticket": 11, "sl": 0.0}])

    def account():
        calls["account"] += 1
        return _account()

    def active_positions():
        calls["positions"] += 1
        return positions

    monkeypatch.setattr(snap, "get_account_snapshot", account)
    monkeypatch.setattr(snap, "get_active_positions", active_positions)
    monkeypatch.setattr(snap, "recent_events", lambda *_args, **_kwargs: pd.DataFrame())
    monkeypatch.setattr(
        snap,
        "fetch_news",
        lambda *_args, **_kwargs: FetchResult([], dt.datetime.now(dt.timezone.utc), "ok", []),
    )
    monkeypatch.setattr(snap, "live_quote", lambda *_args, **_kwargs: {"valid": False, "bid": 1.1, "ask": 1.2})

    try:
        loaded = snap.load_shared_snapshot(refresh=True)
        header = snap.load_shared_snapshot(refresh=False)
        portfolio = snap.load_shared_snapshot(refresh=False)
        assert loaded is header is portfolio
        assert header.account.equity == 99995.55
        assert header.account.open_pnl == -3.67
        assert calls == {"account": 1, "positions": 1}
        assert state["_run_snapshot"] is loaded
    finally:
        snap._CACHE.clear()


def test_tickets_without_stop_lists_missing_and_zero_stops() -> None:
    frame = pd.DataFrame(
        [
            {"ticket": 1, "sl": 0},
            {"ticket": 2, "sl": None},
            {"ticket": 3, "sl": ""},
            {"ticket": 4, "sl": 1.105},
            {"ticket": 5, "sl": False},
        ]
    )
    assert snap.tickets_without_stop(frame) == [1, 2, 3]
