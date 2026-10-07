"""Tests for engine/research/ agents.

Covers:
- all 10 agents import normally
- AgentResult schema
- bad input returns ok=False and score=None without raising
- agents 09 and 10 make zero LLM calls
- AST check that no engine/research file imports MetaTrader5
"""

from __future__ import annotations

import ast
import pathlib

import pytest

AGENT_MODULES = [
    "engine.research.agent_01_market_regime",
    "engine.research.agent_02_technical",
    "engine.research.agent_03_multitimeframe",
    "engine.research.agent_04_news",
    "engine.research.agent_05_sentiment",
    "engine.research.agent_06_fundamental",
    "engine.research.agent_07_quant",
    "engine.research.agent_08_strategy",
    "engine.research.agent_09_risk_engine_adapter",
    "engine.research.agent_10_execution_adapter",
]

LLM_IMPORTS = {"openai", "anthropic", "xai", "groq", "litellm", "ollama", "openrouter", "llm", "llamacpp"}


# ---------------------------------------------------------------------------
# 1. All 10 agents import normally
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_name", AGENT_MODULES)
def test_agent_imports(module_name):
    mod = __import__(module_name, fromlist=["run", "AgentResult"])
    assert hasattr(mod, "run"), f"{module_name} missing run()"
    assert hasattr(mod, "AgentResult"), f"{module_name} missing AgentResult"


# ---------------------------------------------------------------------------
# 2. AgentResult schema
# ---------------------------------------------------------------------------

def test_agent_result_schema():
    from engine.research.agent_01_market_regime import AgentResult

    result = AgentResult(
        agent_id="01",
        name="Test",
        layer="RESEARCH",
        score=50.0,
        label="TRENDING",
        confidence=0.8,
        features={},
        ok=True,
        error=None,
        latency_ms=1.0,
    )
    assert result.agent_id == "01"
    assert result.ok is True
    assert result.score == 50.0
    assert result.error is None


# ---------------------------------------------------------------------------
# 3. Bad input returns ok=False and score=None without raising
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_name", AGENT_MODULES)
def test_agent_bad_input_returns_failure(module_name):
    mod = __import__(module_name, fromlist=["run"])
    result = mod.run({})
    assert result.ok is False
    assert result.score is None or result.score == -1


# ---------------------------------------------------------------------------
# 4. Agents 09 and 10 make zero LLM calls
# ---------------------------------------------------------------------------

def test_agent_09_no_llm_imports():
    source = pathlib.Path(__file__).resolve().parents[2] / "engine" / "research" / "agent_09_risk_engine_adapter.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0].lower())
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0].lower())
    assert not (imports & LLM_IMPORTS), f"agent_09 imports LLM-related modules: {imports & LLM_IMPORTS}"


def test_agent_10_no_llm_imports():
    source = pathlib.Path(__file__).resolve().parents[2] / "engine" / "research" / "agent_10_execution_adapter.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0].lower())
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0].lower())
    assert not (imports & LLM_IMPORTS), f"agent_10 imports LLM-related modules: {imports & LLM_IMPORTS}"


# ---------------------------------------------------------------------------
# 5. AST check: no engine/research file imports MetaTrader5
# ---------------------------------------------------------------------------

def test_no_metatrader5_in_research():
    research_dir = pathlib.Path(__file__).resolve().parents[2] / "research"
    for py_file in research_dir.glob("*.py"):
        if py_file.name.startswith("__"):
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "MetaTrader5" not in alias.name, f"{py_file.name} imports MetaTrader5"
            if isinstance(node, ast.ImportFrom):
                if node.module and "MetaTrader5" in node.module:
                    pytest.fail(f"{py_file.name} imports MetaTrader5")
