"""Package init exposes agents 01..10. Importing it must not call run."""

from engine.research import AGENT_IDS, AGENT_MODULES


def test_agent_ids_are_01_through_10() -> None:
    assert AGENT_IDS == [f"{number:02d}" for number in range(1, 11)]
    assert list(AGENT_MODULES) == AGENT_IDS
    for agent_id, module in AGENT_MODULES.items():
        assert module.AGENT_ID == agent_id
        assert callable(module.run)
