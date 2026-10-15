"""execution_mode accepts only approve_first. It does not place an order."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from engine.config import (
    EXECUTION_MODE_APPROVE_FIRST,
    _parse_execution_mode,
    load_engine_config,
)
from engine.journal import Journal
from engine.__main__ import _journal_execution_mode

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def test_missing_mode_defaults_to_approve_first():
    assert _parse_execution_mode(None) == EXECUTION_MODE_APPROVE_FIRST
    assert _parse_execution_mode("  ") == EXECUTION_MODE_APPROVE_FIRST
    assert load_engine_config().execution_mode == EXECUTION_MODE_APPROVE_FIRST


def test_auto_execute_is_rejected(tmp_path):
    source = (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
    raw = yaml.safe_load(source)
    raw["execution_mode"] = "auto_execute"
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="not implemented"):
        load_engine_config(config_path=path)


def test_a_mode_change_is_one_info_row(tmp_path):
    journal = Journal(tmp_path / "mode.db")
    try:
        _journal_execution_mode(journal, EXECUTION_MODE_APPROVE_FIRST)
        _journal_execution_mode(journal, EXECUTION_MODE_APPROVE_FIRST)
        rows = journal.query(
            "SELECT severity, context_json FROM events WHERE message = ?",
            ("execution mode",),
        )
        assert len(rows) == 1
        assert rows[0]["severity"] == "INFO"
        context = json.loads(rows[0]["context_json"])
        assert context["kind"] == "execution_mode"
        assert context["execution_mode"] == EXECUTION_MODE_APPROVE_FIRST
        assert context["previous"] is None
    finally:
        journal.close()
