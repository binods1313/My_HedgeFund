"""Agents 01-08 on one fixture. Missing providers stay ok=False with no score."""

from __future__ import annotations

import numpy as np

from engine.research import AGENT_MODULES


def test_agents_01_through_08_on_a_fixed_fixture() -> None:
    prices = np.linspace(1.10, 1.25, 40)
    returns = np.diff(prices) / prices[:-1]
    context = {
        "prices": prices,
        "returns": returns,
        "prices_by_tf": {"H1": prices, "H4": prices * 1.01},
        "confluence": {
            "rule_statuses": {
                "01": {"triggered": True, "conflicting": False},
            }
        },
    }
    for agent_id in ("01", "02", "03", "07", "08"):
        result = AGENT_MODULES[agent_id].run(context)
        assert result.ok is True, f"{agent_id} {result.error}"
        assert result.score is not None
    for agent_id in ("04", "05", "06"):
        result = AGENT_MODULES[agent_id].run({})
        assert result.ok is False
        assert result.score is None
        assert result.error
    assert "09" not in context
    assert "10" not in context
