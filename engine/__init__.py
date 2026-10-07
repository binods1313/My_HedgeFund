"""Engine package for V2.0 AI Trading Terminal.

Scope: process skeleton, state machine, gateway, journal, Telegram approval,
  execution, and all Phase 1–10 components.

The processing chain is:
  Strategies → Confidence Engine → Risk Engine → LLM Validator → Telegram
  Approval → Executor

Telegram acts as an APPROVAL LAYER ONLY. The LLM remains validation-only
and never becomes the trade decision maker.
"""

__all__ = [
    "commands",
    "config",
    "executor",
    "gateway",
    "journal",
    "keys",
    "notifications",
    "risk",
    "state",
    "strategy",
]