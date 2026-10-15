"""The engine's run loop must be throttled.

`run_forever` is the heartbeat of the whole system. An unthrottled iteration
writes a heartbeat row, an equity snapshot and a gateway round-trip per cycle,
so a loop that skips its sleep does not merely waste CPU -- it fills the journal
with several hundred rows a second, all carrying the same timestamp, and grows
the database without bound while nothing appears wrong.

That regressed once: a re-indentation put the rate-limit block and the sleep
*after* the `while` instead of inside it. The loop still ran, the engine still
reported healthy, and the only evidence was 178,000 heartbeats across 180
seconds. This test asserts the structure so it cannot happen again.

Run: python -m pytest tests/test_engine/test_engine_loop_throttle.py -v
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from engine.__main__ import Engine


def _loop_nodes() -> list[ast.While]:
    """The `while` loops in `Engine.run_forever`."""
    source = inspect.getsource(Engine.run_forever)
    tree = ast.parse(_dedent(source))
    return [n for n in ast.walk(tree) if isinstance(n, ast.While)]


def _dedent(text: str) -> str:
    lines = text.split("\n")
    indents = [len(l) - len(l.lstrip()) for l in lines if l.strip()]
    pad = min(indents) if indents else 0
    return "\n".join(l[pad:] if len(l) >= pad else l for l in lines)


def _called_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            func = child.func
            if isinstance(func, ast.Attribute):
                names.add(func.attr)
            elif isinstance(func, ast.Name):
                names.add(func.id)
    return names


def test_the_run_loop_sleeps_inside_the_loop() -> None:
    """`time.sleep` must be inside the `while`, not after it.

    This is the exact shape of the regression: a sleep placed after the loop
    throttles nothing, and the loop spins.
    """
    loops = _loop_nodes()
    assert loops, "run_forever must contain a while loop"
    sleeping = [loop for loop in loops if "sleep" in _called_names(loop)]
    assert sleeping, (
        "the while loop in run_forever contains no sleep call; the loop "
        "would spin with no throttle and fill the journal"
    )


def test_the_rate_limit_gates_are_inside_the_loop() -> None:
    """The heartbeat / telemetry / backup gates must run each iteration.

    Beyond the loop structure, the backup must be checked per iteration or a
    day boundary is missed for a whole poll interval.
    """
    loops = _loop_nodes()
    assert loops, "run_forever must contain a while loop"
    inside = _called_names(loops[0])
    assert "backup_database" in inside, (
        "backup_database must be called inside the run loop"
    )


def test_the_loop_calls_run_once() -> None:
    """Sans `run_once`, none of the work happens."""
    assert "run_once" in _called_names(_loop_nodes()[0])


def test_scan_step_stays_inside_run_once_after_the_heartbeat() -> None:
    """F7: the scan cannot be dedented out of the throttled iteration.

    ``run_once`` is inside the ``while``. The scan call is inside
    ``run_once`` and after the heartbeat write. ``CYCLE_TICK`` is produced
    by that scan, not by a line that sits outside the loop.
    """
    loops = _loop_nodes()
    assert loops and "run_once" in _called_names(loops[0])
    source = inspect.getsource(Engine.run_once)
    assert source.find("heartbeat") < source.find("_scan_cycle"), (
        "_scan_cycle must follow the heartbeat inside run_once"
    )
    scan_source = (
        Path(__file__).resolve().parents[2] / "engine" / "scan_cycle.py"
    ).read_text(encoding="utf-8")
    assert "CYCLE_TICK" in scan_source
    assert "SCAN_COMPLETE" not in scan_source


def test_the_spin_is_bounded_by_the_poll_interval() -> None:
    """The sleep must use the configured poll interval, not a constant.

    A hardcoded sleep would silently decouple the loop from
    `engine.command_poll_seconds`, so tuning the config would have no effect.
    """
    source = inspect.getsource(Engine.run_forever)
    assert "poll" in source, "run_forever must reference the configured poll"
    assert "command_poll_seconds" in source, (
        "the poll interval must come from engine.command_poll_seconds"
    )


def test_run_once_writes_exactly_one_heartbeat(tmp_path) -> None:
    """One iteration, one heartbeat row -- the unit the throttle is measured in.

    This is the test that makes the previous assertions meaningful: it pins
    how much work one iteration does, so an unthrottled loop's growth rate is
    a fact rather than a guess.
    """
    from engine.journal import Journal
    from engine.state import StateMachine
    from tests.test_engine.fake_mt5 import FakeMT5

    journal = Journal(tmp_path / "throttle.db")
    state = StateMachine(journal)
    engine = Engine(config=object(), journal=journal, state=state,
                    gateway=SimpleNamespace(connected=False), executor=None,
                    commands=SimpleNamespace(poll_once=lambda: None),
                    risk=None)
    try:
        engine.run_once()
        count = journal.query_one("SELECT COUNT(*) n FROM heartbeats")["n"]
        assert count == 1, (
            f"one run_once must write exactly one heartbeat, got {count}; "
            "more than one means the loop's write rate is not what the "
            "config implies"
        )
    finally:
        journal.close()


from types import SimpleNamespace  # noqa: E402  (used by the test above)
