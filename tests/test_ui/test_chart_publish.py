"""Closed bars published for shadow mode drop the forming row."""

from __future__ import annotations

import pandas as pd

from ui.components.chart import _publish_closed


def test_publish_drops_the_forming_bar_and_ignores_point_spread(monkeypatch) -> None:
    captured: dict = {}

    def _capture(symbol: str, timeframe: str, rows: list[dict], root=None) -> None:
        captured["symbol"] = symbol
        captured["timeframe"] = timeframe
        captured["rows"] = rows

    monkeypatch.setattr("ui.components.chart.publish_closed_rows", _capture)
    frame = pd.DataFrame(
        {
            "time": ["a", "b", "c"],
            "open": [1.0, 1.0, 1.0],
            "high": [1.1, 1.1, 1.1],
            "low": [0.9, 0.9, 0.9],
            "close": [1.0, 1.0, 1.0],
            "spread": [12, 12, 12],
        }
    )
    _publish_closed("EURUSD", "15M", frame)
    assert captured["symbol"] == "EURUSD"
    assert captured["timeframe"] == "M15"
    assert [row["time"] for row in captured["rows"]] == ["a", "b"]
    assert "spread" not in captured["rows"][0]


def test_publish_swallows_oserror(monkeypatch) -> None:
    def _boom(*_args, **_kwargs):
        raise OSError("disk")

    monkeypatch.setattr("ui.components.chart.publish_closed_rows", _boom)
    frame = pd.DataFrame(
        {
            "time": ["a", "b"],
            "open": [1.0, 1.0],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
        }
    )
    _publish_closed("EURUSD", "All", frame)
