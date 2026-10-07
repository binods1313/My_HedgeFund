"""Research package. Importing it does not run or wire any agent."""

from __future__ import annotations

from . import (
    agent_01_market_regime,
    agent_02_technical,
    agent_03_multitimeframe,
    agent_04_news,
    agent_05_sentiment,
    agent_06_fundamental,
    agent_07_quant,
    agent_08_strategy,
    agent_09_risk_engine_adapter,
    agent_10_execution_adapter,
)

AGENT_MODULES = {
    "01": agent_01_market_regime,
    "02": agent_02_technical,
    "03": agent_03_multitimeframe,
    "04": agent_04_news,
    "05": agent_05_sentiment,
    "06": agent_06_fundamental,
    "07": agent_07_quant,
    "08": agent_08_strategy,
    "09": agent_09_risk_engine_adapter,
    "10": agent_10_execution_adapter,
}

AGENT_IDS = ["01", "02", "03", "04", "05", "06", "07", "08", "09", "10"]

__all__ = ["AGENT_IDS", "AGENT_MODULES"]
