# A2 — Analysis agents 01–10: what each one is and why it is not live

Status of every claim below: **verified from code or config in this session**. Nothing here wires an agent into the trading path. PAPER mode only.

## Summary

- **No agent 01–08 makes a model call or needs a provider key.** Each is deterministic code over inputs passed in `context`. The grep for LLM SDK / HTTP / key imports across `engine/research/agent_*.py` returned nothing.
- **No engine runtime module calls the research agents.** `engine/__main__.py`, `approval_flow.py`, `strategy.py`, `executor.py`, `risk.py` do not import `engine.research`.
- **So the real blocker for 01–08 is the missing scan loop**, not a key. The card now says so.
- **Agent 06** reads fundamentals through the Dexter adapter. It needs `FINANCIAL_DATASETS_API_KEY`, which is **not set**, and Dexter is **not installed** at `E:\tools\dexter` on this machine.
- **Agents 09 and 10** are deterministic adapters with no LLM call (invariant I4). They show "No decision yet" until a real decision is journalled.

## Per-agent table

| # | Name | Model call | Inputs (from `context`) | Output | Cost / call | Daily cap | Key needed | Why not live now |
|---|---|---|---|---|---|---|---|---|
| 01 | Market regime | none (numpy) | `prices` (≥20) | score 0–100, TRENDING/RANGING | 0 | — | none | Waiting for scan loop |
| 02 | Technical | none (numpy) | `prices` | EMA ribbon, RSI, MACD | 0 | — | none | Waiting for scan loop |
| 03 | Multi-timeframe | none | `prices_by_tf` | alignment vector | 0 | — | none | Waiting for scan loop |
| 04 | News | none | `events` (calendar list) | impact score, HIGH/MED/LOW | 0 | — | none | Waiting for scan loop |
| 05 | Sentiment | none | `sentiment_data` | sentiment vector | 0 | — | none | Waiting for scan loop |
| 06 | Fundamental | Dexter adapter | `fundamental_data` | fundamentals (equities/indices) | Dexter call | `dexter.daily_cap: 20` | `FINANCIAL_DATASETS_API_KEY` | **Needs key (not set)**; Dexter not installed |
| 07 | Quant | none (numpy) | `returns` | z-score, win prob, factor tilt | 0 | — | none | Waiting for scan loop |
| 08 | Strategy | none | `confluence` | deterministic rules | 0 | — | none | Waiting for scan loop |
| 09 | Risk engine adapter | **none (I4)** | — | risk decision | 0 | — | none | No decision yet (journal empty for this card) |
| 10 | Execution adapter | **none (I4)** | — | execution decision | 0 | — | none | No execution decision yet |

**Note on agent 04.** Its docstring says "ForexFactory", but the code only counts `impact` labels in the `events` list it is given. It does not fetch ForexFactory itself. The fetch, if any, must happen upstream and is not in this file. I have not verified an upstream fetch exists.

## Shadow research and the cap of 20

- **The "20" is `dexter.daily_cap: 20`** in `config_ext.yaml`. It caps **Dexter calls per day**, enforced by `budget_today()` in `ui/services/dexter_client.py`.
- **It is not a shadow-cycle cap.** The shadow loop has no counter.
- **The Shadow toggle** (Research Desk) writes a flag file (`sync_flag`) and starts a daemon thread (`ensure_thread`). The thread calls `learning.shadow.run_cycle(enabled=True)` every 30 seconds while the flag file exists.
- **`shadow.enabled: false` in `config_ext.yaml` is never read** anywhere in the code. The UI toggle is session state, not that config flag. This setting is currently inert.
- The card's "calls today N / cap" shows the **Dexter** budget. It is correct for Dexter, and the label should not be read as a shadow-cycle budget.

## What changed in the UI

Each analysis card now shows a **reason line** under its status word (`_disabled_reason` in `ui/components/research_desk.py`):

- 01, 02, 03, 04, 05, 07, 08: `Waiting for scan loop (not built yet)`
- 06: `Needs FINANCIAL_DATASETS_API_KEY (not set)` while the key is absent; otherwise the scan-loop reason
- 09: `No decision yet`
- 10: `No execution decision yet`

The four-word §A5 status vocabulary is unchanged. Tests: `tests/test_ui/test_research_desk_status.py` (12 passed).

## Open items found while doing A2

1. `shadow.enabled` in `config_ext.yaml` is unused. Either wire it or remove it.
2. Agent 04 docstring says ForexFactory but does not fetch it.
3. Dexter is not installed (`E:\tools\dexter` absent), so agent 06 cannot run even after its key is set.
