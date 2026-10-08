"""Shadow ledger, resolver, calibration, and the order boundary."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from learning.bars import Bar
from learning.calibrate import by_bucket, had_edge, summarise, wilson_interval
from learning.ledger import (
    connect,
    link_journal,
    record_prediction,
    resolve_open,
    samples_for,
)
from learning.propose import apply_proposal, bounded_value, build_proposal, current_values, rollback_to
from learning.resolver import resolve_trade

ROOT = Path(__file__).resolve().parents[1]


def _bar(index: int, close: float, *, high: float | None = None, low: float | None = None, spread: float | None = None) -> Bar:
    return Bar(
        time=f"t{index}",
        open=close,
        high=close if high is None else high,
        low=close if low is None else low,
        close=close,
        spread=spread,
    )


def _quiet(count: int, close: float = 100.0) -> list[Bar]:
    return [_bar(index, close, high=close + 1.0, low=close) for index in range(count)]


def test_long_target_short_stop_and_neither() -> None:
    long_tp = _quiet(1) + [_bar(1, 100.2, high=102.5, low=99.5)]
    hit = resolve_trade(long_tp, 0, "long", 1, atr_k=1.0, rr=2.0, atr_period=1)
    assert hit is not None and hit.hit == 1 and hit.r_multiple == 2.0

    long_sl = _quiet(1) + [_bar(1, 99.0, high=103.0, low=98.0)]
    stopped = resolve_trade(long_sl, 0, "long", 1, atr_k=1.0, rr=2.0, atr_period=1)
    assert stopped is not None and stopped.hit == 0 and stopped.r_multiple == -1.0

    short_tp = _quiet(1) + [_bar(1, 98.5, high=100.4, low=97.5)]
    short = resolve_trade(short_tp, 0, "short", 1, atr_k=1.0, rr=2.0, atr_period=1)
    assert short is not None and short.hit == 1 and short.r_multiple == 2.0

    inside = _quiet(1) + [_bar(1, 100.2, high=100.4, low=99.6)]
    neither = resolve_trade(inside, 0, "long", 1, atr_k=1.0, rr=2.0, atr_period=1)
    assert neither is not None and neither.hit is None
    assert neither.r_multiple == pytest.approx(0.2)

    assert resolve_trade(inside, 0, "flat", 1, atr_k=1.0, rr=2.0, atr_period=1) is None


def test_same_bar_stop_wins_and_spread_moves_the_fill() -> None:
    both = _quiet(1) + [_bar(1, 100.0, high=103.0, low=98.0)]
    stopped = resolve_trade(both, 0, "long", 1, atr_k=1.0, rr=2.0, atr_period=1)
    assert stopped is not None and stopped.hit == 0

    spread = [_bar(0, 100.0, high=101.0, low=100.0, spread=0.5), _bar(1, 102.2, high=102.6, low=100.2)]
    # Fill 100.5, distance 1, target 102.5. High 102.6 hits the target.
    targeted = resolve_trade(spread, 0, "long", 1, atr_k=1.0, rr=2.0, atr_period=1)
    assert targeted is not None and targeted.hit == 1


def test_future_bars_do_not_change_a_resolved_horizon() -> None:
    bars = _quiet(4)
    bars.append(_bar(4, 100.2, high=100.4, low=99.6))
    bars.append(_bar(5, 100.1, high=100.3, low=99.7))
    first = resolve_trade(bars, 3, "long", 2, atr_k=1.0, rr=2.0, atr_period=1)
    extended = bars + [_bar(6, 50.0, high=180.0, low=10.0)]
    second = resolve_trade(extended, 3, "long", 2, atr_k=1.0, rr=2.0, atr_period=1)
    assert first == second
    assert resolve_trade(bars[:5], 3, "long", 2, atr_k=1.0, rr=2.0, atr_period=1) is None


def test_resolver_is_idempotent(tmp_path: Path) -> None:
    bars = _quiet(1) + [_bar(1, 100.2, high=102.5, low=99.5)]
    conn = connect(tmp_path / "learning.db")
    row = {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "bar_time": "t0",
        "agent_id": "01",
        "agent_version": "1",
        "direction": "long",
        "score": 0.2,
        "confidence": 0.5,
        "features_json": "{}",
        "config_hash": "abc",
    }
    first = record_prediction(conn, row)
    second = record_prediction(conn, row)
    assert first == second
    assert resolve_open(conn, bars, symbol="EURUSD", timeframe="H1", horizons=[1], atr_k=1, rr=2, atr_period=1) == 1
    assert resolve_open(conn, bars, symbol="EURUSD", timeframe="H1", horizons=[1], atr_k=1, rr=2, atr_period=1) == 0
    assert len(samples_for(conn, "01", 1)) == 1
    conn.close()


def test_wilson_and_insufficient_bucket() -> None:
    low, high = wilson_interval(50, 100)
    assert low == pytest.approx(0.403830, abs=1e-4)
    assert high == pytest.approx(0.596170, abs=1e-4)
    small = [{"score": 0.0, "hit": 1, "r": 1.0} for _ in range(29)]
    summary = summarise(small, minimum=30)
    assert summary["insufficient"] is True
    large = [{"score": 0.0, "hit": 1, "r": 0.4} for _ in range(30)]
    ready = summarise(large, minimum=30)
    assert ready["insufficient"] is False
    assert ready["hit_rate"] == 1.0
    buckets = by_bucket(large, minimum=30)
    assert buckets[0]["bucket"] == "40-60"
    assert had_edge(large, minimum=30) is True
    assert had_edge(small, minimum=30) is None


def test_proposal_bounds_walk_forward_and_rollback(tmp_path: Path) -> None:
    assert bounded_value(1.0, 2.0, shrinkage=0.5, max_change=0.10) == pytest.approx(1.1)
    conn = connect(tmp_path / "learning.db")
    rejected = build_proposal(
        conn,
        kind="weight",
        old_value="1.0",
        target="2.0",
        older=[1.0, 1.0],
        newer=[0.2, 0.2],
        samples=40,
        minimum=30,
        shrinkage=0.5,
        max_change=0.10,
    )
    assert rejected["status"] == "rejected"
    assert rejected["new_value"] == "1.0"
    with pytest.raises(ValueError):
        apply_proposal(conn, rejected["id"], tmp_path / "weights", {"weight": "1.0"})

    accepted = build_proposal(
        conn,
        kind="weight",
        old_value="1.0",
        target="2.0",
        older=[0.1, 0.1],
        newer=[0.4, 0.4],
        samples=40,
        minimum=30,
        shrinkage=0.5,
        max_change=0.10,
    )
    assert accepted["status"] == "proposed"
    assert float(accepted["new_value"]) == pytest.approx(1.1)
    weights = tmp_path / "weights"
    version = apply_proposal(conn, accepted["id"], weights, {"weight": accepted["new_value"]})
    assert version == 1
    assert current_values(weights)["weight"] == accepted["new_value"]
    moved = build_proposal(
        conn,
        kind="weight",
        old_value=accepted["new_value"],
        target="2.0",
        older=[0.1],
        newer=[0.8],
        samples=40,
        minimum=30,
        shrinkage=0.5,
        max_change=0.10,
    )
    assert moved["status"] == "proposed"
    second = apply_proposal(conn, moved["id"], weights, {"weight": moved["new_value"]})
    assert second == 2
    assert current_values(weights)["weight"] == moved["new_value"]
    restored = rollback_to(conn, moved["id"], weights, 1)
    assert restored == 3
    assert current_values(weights)["weight"] == accepted["new_value"]
    conn.close()


def test_missing_journal_links_nothing(tmp_path: Path) -> None:
    conn = connect(tmp_path / "learning.db")
    assert link_journal(conn, tmp_path / "missing.db") == 0
    conn.close()


def test_learning_package_cannot_place_orders() -> None:
    forbidden_modules = {
        "MetaTrader5",
        "telegram",
        "engine.risk",
        "engine.executor",
        "engine.commands",
        "engine.gateway",
        "engine.state",
    }
    forbidden_attrs = {"order_send", "close_position", "submit_order", "modify_position"}
    offenders: list[str] = []
    for path in (ROOT / "learning").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in forbidden_modules or alias.name in forbidden_modules:
                        offenders.append(f"{path.name}:{node.lineno}:{alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if node.module in forbidden_modules or node.module.split(".")[0] in {"MetaTrader5", "telegram"}:
                    offenders.append(f"{path.name}:{node.lineno}:{node.module}")
            elif isinstance(node, ast.Attribute) and node.attr in forbidden_attrs:
                offenders.append(f"{path.name}:{node.lineno}:{node.attr}")
            elif isinstance(node, ast.Name) and node.id in forbidden_attrs:
                offenders.append(f"{path.name}:{node.lineno}:{node.id}")
    assert offenders == []


def test_shadow_cycle_records_and_skips_order_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ui.services.dexter_client import DexterReport
    from learning.shadow import run_cycle

    def _na(symbol: str, asset_class: str, **_kwargs: object) -> DexterReport:
        return DexterReport(
            symbol=symbol,
            asset_class=asset_class,
            status="na",
            summary="no company fundamentals for this asset class",
            metrics={},
            sources=[],
            as_of="2026-01-01T00:00:00Z",
            tool_calls=0,
            model="none",
        )

    monkeypatch.setattr("learning.shadow.research", _na)
    root = tmp_path / "bars"
    rows = [
        {"time": f"t{index}", "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1 + index * 0.0001}
        for index in range(40)
    ]
    from learning.bars import publish_closed_rows

    publish_closed_rows("EURUSD", "H1", rows, root)
    database = tmp_path / "learning.db"
    first = run_cycle(
        enabled=True,
        bar_root=root,
        db_path=database,
        symbols=[("EURUSD", "forex")],
    )
    conn = connect(database)
    stored = conn.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()["n"]
    conn.close()
    second = run_cycle(
        enabled=True,
        bar_root=root,
        db_path=database,
        symbols=[("EURUSD", "forex")],
    )
    assert first > 0
    assert second > 0
    conn = connect(database)
    assert conn.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()["n"] == stored
    agents = {row["agent_id"] for row in conn.execute("SELECT agent_id FROM predictions")}
    assert "09" not in agents
    assert "10" not in agents
    assert "01" in agents
    assert run_cycle(enabled=False, db_path=database) == 0
    conn.close()


def test_degraded_shadow_is_not_a_calibration_row(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from learning.shadow import _record

    conn = connect(tmp_path / "learning.db")
    recorded = _record(
        conn,
        symbol="EURUSD",
        timeframe="H1",
        bar_time="t0",
        agent_id="08",
        result=SimpleNamespace(
            ok=False, error="name missing", label="ERROR",
            score=9, confidence=0.9, features={},
        ),
        config_hash="abc",
    )
    assert recorded is False
    assert conn.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()["n"] == 0
    status = conn.execute("SELECT status, reason FROM shadow_status").fetchone()
    assert status["status"] == "degraded"
    assert status["reason"] == "name missing"

    record_prediction(
        conn,
        {
            "symbol": "EURUSD",
            "timeframe": "H1",
            "bar_time": "t1",
            "agent_id": "08",
            "agent_version": "1",
            "direction": "long",
            "score": 3,
            "confidence": 0.9,
            "features_json": '{"ok": false}',
            "config_hash": "abc",
        },
    )
    prediction_id = conn.execute("SELECT id FROM predictions").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO outcomes (
            prediction_id, horizon_bars, forward_return, hit, mfe, mae,
            r_multiple_at_rr, resolved_at
        ) VALUES (?, 1, 0.1, 1, 0.1, 0.0, 1.0, 't')
        """,
        (prediction_id,),
    )
    conn.commit()
    assert samples_for(conn, "08", 1) == []

    record_prediction(
        conn,
        {
            "symbol": "EURUSD",
            "timeframe": "H1",
            "bar_time": "t2",
            "agent_id": "08",
            "agent_version": "1",
            "direction": "long",
            "score": 2,
            "confidence": 0.5,
            "features_json": '{"ok": true}',
            "config_hash": "abc",
        },
    )
    valid_id = conn.execute(
        "SELECT id FROM predictions WHERE bar_time = 't2'"
    ).fetchone()["id"]
    conn.execute(
        """
        INSERT INTO outcomes (
            prediction_id, horizon_bars, forward_return, hit, mfe, mae,
            r_multiple_at_rr, resolved_at
        ) VALUES (?, 1, 0.2, 1, 0.2, 0.0, 1.0, 't')
        """,
        (valid_id,),
    )
    conn.commit()
    samples = samples_for(conn, "08", 1)
    assert [row["score"] for row in samples] == [2]
    conn.close()


def test_existing_ledger_gains_status_table_without_a_version_bump(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "old.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL)")
    raw.execute("INSERT INTO schema_version (id, version) VALUES (1, 1)")
    raw.commit()
    raw.close()
    conn = connect(path)
    assert conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()["version"] == 1
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'shadow_status'"
    ).fetchone()
    conn.close()
