"""Engine Research Agents - 10-Agent Architecture

One module per agent, each with:
  - class constants AGENT_ID, AGENT_NAME, LAYER
  - a pure function run(context) -> AgentResult
  - AgentResult = dataclass/pydantic {agent_id, name, layer, score: float 0..1 | None, label: str, confidence: float, features: dict, ok: bool, error: str | None, latency_ms}
  - strict input validation; on any exception return ok=False, score=None (never raise)
"""

from __future__ import annotations

# Export all 10 agents
from . import agent_01_market_regime  # noqa: F401
from . import agent_02_technical  # noqa: F401
from . import agent_03_multitimeframe  # noqa: F401
from . import agent_04_news  # noqa: F401
from . import agent_05_sentiment  # noqa: F401
from . import agent_06_fundamental  # noqa: F401
from . import agent_07_quant  # noqa: F401
from . import agent_08_strategy  # noqa: F401
from . import agent_09_risk_engine_adapter  # noqa: F401
from . import agent_10_execution_adapter  # noqa: F401

# Agent registry for easy lookup
AGENT_REGISTRY = {
    "agent_01_market_regime": agent_01_market_regime,
    "agent_02_technical": agent_02_technical,
    "agent_03_multitimeframe": agent_03_multitimeframe,
    "agent_04_news": agent_04_news,
    "agent_05_sentiment": agent_05_sentiment,
    "agent_06_fundamental": agent_06_fundamental,
    "agent_07_quant": agent_07_quant,
    "agent_08_strategy": agent_08_strategy,
    "agent_09_risk_engine_adapter": agent_09_risk_engine_adapter,
    "agent_10_execution_adapter": agent_10_execution_adapter,
}

AGENT_IDS = ["01", "02", "03", "05", "06", "07", "08", "09", "10"]

# Layer mapping
LAYER_MAP = {
    "01": "RESEARCH",
    "02": "RESEARCH",
    "03": "RESEARCH",
    "04": "RESEARCH",
    "05": "RESEARCH",
    "07": "RESEARCH",
    "08": "REGULATION_DISPATCH",
    "09": "REGULATION_DISPATCH",
    "10": "REGULATION_DISPATCH",
}

# Role mapping per K1-K8 instructions
ROLE_MAP = {
    "01": "market_regime",
    "02": "technical",
    "03": "technical",
    "04": "news",
    "05": "sentiment",
    "06": "fundamental",
    "07": "quant",
    "08": "strategy_dispatch",
    "09": "risk_engine",
    "10": "execution_adapter",
}