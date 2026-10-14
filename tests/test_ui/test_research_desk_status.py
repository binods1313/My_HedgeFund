"""Research Desk analysis cards must show the §A5 status, never "OFF".

The Desk reads the shadow ledger, not the agents, so an empty ledger used to
render every card as "OFF" -- a word that wrongly implied the module did not
exist. All ten modules implement ``run(context)``; the engine loop simply has
no live caller for any of them yet. These tests pin the §A5 vocabulary and the
"Not implemented" rule for a true placeholder, so the wording cannot silently
regress to the old single word.
"""

from __future__ import annotations

import types

import pytest

from ui.components import research_desk as desk


def _card_script() -> str:
    return (
        "import streamlit as st\n"
        "from ui.components.research_desk import _agent_cards\n"
        "_agent_cards(None, {}, 4, 20)\n"
    )


def _rendered_texts() -> list[str]:
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_string(_card_script())
    app.run(timeout=30)
    assert not app.error, f"card render raised: {list(app.error)}"
    return [item.value for item in app.text]


def test_no_card_renders_the_word_off() -> None:
    texts = _rendered_texts()
    assert texts, "the cards rendered nothing"
    assert "OFF" not in texts, (
        f"the old misleading status word is back: {texts}")


def test_every_agent_card_shows_a_status_word() -> None:
    from engine.research import AGENT_IDS

    texts = _rendered_texts()
    # One status word follows each "<id> <name>" header line.
    for index in range(len(AGENT_IDS)):
        header = texts[index * 2]
        status = texts[index * 2 + 1]
        assert status in desk._STATUS_WORDS, (
            f"card {header!r} shows {status!r}, not a §A5 word")


def test_deterministic_adapters_read_deterministic() -> None:
    assert desk._implementation_status("09") == desk._STATUS_DETERMINISTIC
    assert desk._implementation_status("10") == desk._STATUS_DETERMINISTIC


def test_research_modules_read_implemented_but_disabled() -> None:
    for agent_id in ("01", "02", "03", "04", "05", "06", "07", "08"):
        assert desk._implementation_status(agent_id) == desk._STATUS_DISABLED, (
            f"agent {agent_id} should read {desk._STATUS_DISABLED!r}")


def test_no_module_is_live_because_the_engine_never_calls_them() -> None:
    # Nothing is wired into the loop yet, so "Implemented, live" is reserved.
    texts = _rendered_texts()
    assert desk._STATUS_LIVE not in texts
    assert desk._STATUS_LIVE in desk._STATUS_WORDS


def test_a_placeholder_module_reads_not_implemented() -> None:
    stub = types.ModuleType("stub_agent")

    def run(context):  # noqa: ANN001 - placeholder signature
        raise NotImplementedError("not built yet")

    stub.run = run
    assert desk._is_stub(stub) is True
    original = desk.AGENT_MODULES.get("01")
    desk.AGENT_MODULES["01"] = stub
    try:
        assert desk._implementation_status("01") == desk._STATUS_NOT_IMPLEMENTED
    finally:
        if original is not None:
            desk.AGENT_MODULES["01"] = original


def test_a_real_module_is_not_a_stub() -> None:
    from engine.research import AGENT_MODULES

    assert desk._is_stub(AGENT_MODULES["01"]) is False
    assert desk._implementation_status("01") == desk._STATUS_DISABLED


def test_the_vocabulary_is_exactly_four_words() -> None:
    assert len(desk._STATUS_WORDS) == 4
    assert len(set(desk._STATUS_WORDS)) == 4
    assert "OFF" not in desk._STATUS_WORDS


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
