# Dexter Integration Plan

## D1 — Dexter Discovery Summary

**Repository:** `virattt/dexter` (https://github.com/virattt/dexter)
**License:** MIT
**Runtime:** Bun v1.0+
**Version observed:** v2026.2.x series (latest releases: v2026.2.16, v2026.2.24)

### Install path / runtime
Searched on 2026-10-07. Dexter's install path was **not found**. Do not treat a missing path as "install it now."

- `where.exe bun` → `C:\Users\Binod\.bun\bin\bun.exe` (Bun 1.4.0). That directory contains `bun.exe` and `bunx.exe` only. There is no `dexter` shim.
- `where.exe dexter` → not on PATH.
- Windows Terminal settings (`%LOCALAPPDATA%\Packages\Microsoft.WindowsTerminal_8wekyb3d8bbwe\LocalState\settings.json`) has four profiles. None is a Dexter command line.
- WSL Ubuntu: `command -v bun` and `command -v dexter` both missing.
- No directory named dexter within three levels of `C:\Users\Binod`, `E:\`, or `D:\` (AppData walked only one level). No `package.json` mentioning dexter within four levels of `repos`, `E:\`, or `.bun`.

The operator reports Dexter v2026.2.6 with model `openrouter/pony-alpha`. That copy was not on the paths above. Record the real path here once it is pointed out. Until then there is nothing to wrap.

### Task 5 search (2026-10-07, read-only, Dexter was not started)

Searched again. No install path.

- Directory name `dexter`, depth 4, under `E:\` and `C:\Users\Binod`, skipping `node_modules` and `.git`: no hit.
- `package.json` with `"name": "dexter"`, depth 3, under `C:\Users\Binod\repos` and `E:\`, skipping `node_modules` and `.git`: no hit.
- `where.exe bun` → `C:\Users\Binod\.bun\bin\bun.exe`.
- `where.exe dexter` → not on PATH.
- Windows Terminal `settings.json`: no `dexter` string.
- WSL Ubuntu: `command -v bun` and `command -v dexter` both missing.
- The shell start directory is `C:\Users\Binod`, which is inside the search above.

Dexter upstream is TypeScript on Bun (`virattt/dexter`). Python cannot import its `Agent` class.

### README highlights
- Autonomous financial research agent using task planning, self-reflection, and real-time market data
- Built with LangChain, supports multiple LLM providers
- Includes WhatsApp gateway for chat-based interaction
- Scratchpad logs all tool calls to `.dexter/scratchpad/*.jsonl`

### Env vars needed (names only)
- `OPENAI_API_KEY` (required)
- `FINANCIAL_DATASETS_API_KEY` (required — institutional-grade market data)
- `ANTHROPIC_API_KEY` (optional)
- `GOOGLE_API_KEY` (optional)
- `XAI_API_KEY` (optional)
- `OPENROUTER_API_KEY` (optional)
- `OLLAMA_BASE_URL` (optional, default `http://127.0.0.1:11434`)
- `EXASEARCH_API_KEY` (optional, web search)
- `TAVILY_API_KEY` (optional, web search fallback)
- `LANGSEARCH_API_KEY` (optional, web search)
- `PERPLEXITY_API_KEY` (optional, web search)
- `X_BEARER_TOKEN` (optional, X/Twitter search)

### LLM providers supported
OpenAI, Anthropic, Google Gemini, xAI/Grok, OpenRouter, Ollama (local).

### Custom OpenAI-compatible base URL
Yes. Supported via:
- **OpenRouter** (`OPENROUTER_API_KEY`) — provides OpenAI-compatible API with custom model routing
- **Ollama** (`OLLAMA_BASE_URL`) — local LLM server at custom base URL
- AgentRouter can be used by configuring it as an OpenRouter-compatible endpoint or by routing through a supported provider that accepts custom base URLs.

### Data provider
**Financial Datasets API** (`financialdatasets.ai`) — key env var: `FINANCIAL_DATASETS_API_KEY`
Provides: income statements, balance sheets, cash flow, market data, insider trades, institutional holdings, filings.

---

## D2 — Non-Interactive Mode

**Finding:** Dexter has **no dedicated one-shot CLI mode** with JSON output flags.

### Available modes:
1. **Interactive TUI** (`bun start`) — primary mode, uses `@mariozechner/pi-tui`
2. **WhatsApp Gateway** (`bun run gateway`) — HTTP server that receives WhatsApp messages and processes them through Dexter
3. **Programmatic Library API** — `Agent.create()` + `agent.run(query)` async generator; exports from `src/agent/index.ts`

### Relevant source excerpts:
```typescript
// src/cli.tsx — strictly interactive TUI
export async function runCli() {
  const tui = new TUI(new ProcessTerminal());
  // ... event loop with editor, chat log, overlays
  tui.start();
  await new Promise<void>((resolve) => {
    process.once('exit', finish);
    process.once('SIGINT', finish);
    process.once('SIGTERM', finish);
  });
}
```

```typescript
// src/agent/agent.ts — programmatic API
export class Agent {
  static async create(config: AgentConfig = {}): Promise<Agent>
  async *run(query: string, inMemoryHistory?: InMemoryChatHistory): AsyncGenerator<AgentEvent>
}
```

**Conclusion:** Dexter is TypeScript running on Bun. A Python process cannot import `Agent`. Two honest options remain: a small Bun script that prints one strict JSON object on stdout, or a local wrapper process that Python calls. Parsing the interactive TUI is not an option. This file is a plan only. No wrapper was built in Task 4.

---

## D3 — Capability Mapping to Whitelist

**Config whitelist categories:** `forex` (10 pairs), `metals_commodities` (0), `crypto` (2), `indices` (4)

| Dexter Capability | Forex | Metals/Commodities | Crypto | Indices | Stocks |
|---|---|---|---|---|---|
| `get_market_data` (prices, news, insider activity) | ✅ (prices, news) | ✅ (if symbols present) | ✅ | ✅ | ✅ |
| `get_financials` (income, balance, cash flow) | ❌ N/A | ❌ N/A | ❌ N/A | ❌ N/A | ✅ |
| `read_filings` (SEC 10-K, 10-Q, 8-K) | ❌ N/A | ❌ N/A | ❌ N/A | ❌ N/A | ✅ |
| `stock_screener` (filter by criteria) | ❌ N/A | ❌ N/A | ❌ N/A | ❌ N/A | ✅ |
| `web_search` / `web_fetch` | ✅ (macro news) | ✅ | ✅ | ✅ | ✅ |

**Notes:**
- FX pairs have no company fundamentals, SEC filings, or insider data — marked N/A for `get_financials`, `read_filings`, `stock_screener`.
- `get_market_data` supports stocks and crypto natively; indices may resolve via ETF proxies or index-tracking tickers.
- `metals_commodities` is empty in config.yaml — no current mapping.

---

## D4 — Integration Options & Recommendation

### Option A: One-shot subprocess with timeout and JSON output
- Spawn `bun run src/index.tsx` with query piped to stdin
- Parse TUI output (unreliable — TUI uses escape sequences and progress indicators)
- **Verdict:** NOT RECOMMENDED. No JSON output mode exists; parsing interactive TUI is brittle.

### Option B: Small local HTTP wrapper
- Run Dexter's WhatsApp gateway locally, send HTTP requests mimicking WhatsApp message format
- Parse agent responses from gateway callbacks
- **Verdict:** POSSIBLE but heavyweight. Requires understanding WhatsApp gateway protocol; adds network overhead.

### Option C: Skip Dexter, call Financial Datasets API directly
- Bypass Dexter, call `financialdatasets.ai` endpoints directly for prices, financials, filings
- Lose Dexter's task planning, self-validation, and multi-step reasoning
- **Verdict:** VIABLE for simple data lookups but loses Dexter's core value (autonomous research planning).

### RECOMMENDATION: Bun wrapper that prints strict JSON
Python stays on the engine side. A small Bun script, run only after the install path is known, would call Dexter's TypeScript `Agent` and write one JSON object to stdout. Python would launch that script with a timeout and parse the JSON. A local wrapper is the other acceptable shape. Importing `Agent` from Python is not possible.

The data provider key name is `FINANCIAL_DATASETS_API_KEY`. FX pairs have no company fundamentals, filings, or insider data. Agent 06 returns `N/A` for FX. It must not invent a score.

### Implementation constraints:
- Dexter is advisory only — its output can never approve, size, or place a trade (LLM validation-only rule)
- Output is untrusted data (browses web) — parse into strict schema; treat as prompt-injection-capable
- Results cached per symbol and per day
- Calls rate-limited and capped
- No secrets in arguments or logs
- Label outputs `"source: Dexter"` in dashboard

---

## Agent Mapping

| Spec agent | Module | Dexter |
|---|---|---|
| 01 Market Regime | `engine/research/agent_01_market_regime.py` | No |
| 02 Technical | `engine/research/agent_02_technical.py` | No |
| 03 Multi-Timeframe | `engine/research/agent_03_multitimeframe.py` | No |
| 04 News | `engine/research/agent_04_news.py` | No. Uses the calendar feed, not company filings |
| 05 Sentiment | `engine/research/agent_05_sentiment.py` | Only if a real source is configured. Never fabricate |
| 06 Fundamental | `engine/research/agent_06_fundamental.py` | Primary Dexter consumer. `N/A` for FX and commodities |
| 07 Quant | `engine/research/agent_07_quant.py` | No. Deterministic stats |
| 08 Strategy | `engine/research/agent_08_strategy.py` | No. Rule table |
| 09 Risk Engine Adapter | `engine/research/agent_09_risk_engine_adapter.py` | No. Calls `engine/risk.py` |
| 10 Execution Adapter | `engine/research/agent_10_execution_adapter.py` | No. Existing approval flow only |

---

## Dashboard Design: "🧠 Research Desk"

### Layout (using `ui/theme.py` tokens)

```
┌─────────────────────────────────────────────────────────────────┐
│ 🧠 RESEARCH DESK                                        [Refresh]│
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐          │
│  │ Agent 04 │ │ Agent 05 │ │ Agent 06 │ │ Agent 07 │  ...      │
│  │ News     │ │Sentiment │ │Fundamental│ │ Quant   │          │
│  │ OK 🟢   │ │ N/A ⚪  │ │ DEGRADED │ │ OK 🟢   │          │
│  │ Score: 8 │ │ Score: -│ │ Score: 6 │ │ Score: 7 │          │
│  │ 2m ago   │ │ -       │ │ 5m ago   │ │ 1m ago   │          │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘          │
│                                                                 │
│  ┌─ DEXTER PANEL ──────────────────────────────────────────────┐│
│  │ Last query: "Analyze AAPL fundamentals"                     ││
│  │ Result: Revenue $94B, EPS $6.13, P/E 28.4                   ││
│  │ Sources: financialdatasets.ai, sec.gov                      ││
│  │ Data age: 2h  |  Calls today: 3/50                          ││
│  └─────────────────────────────────────────────────────────────┘│
│                                                                 │
│  Overview strip: ● 04 News (real) ● 06 Fundamental (stale)     │
│                  ○ 05 Sentiment (no data) ○ 07 Quant (real)    │
└─────────────────────────────────────────────────────────────────┘
```

### Card states:
- **OK 🟢** — Fresh data returned, score > threshold
- **DEGRADED 🟡** — Data returned but stale (>24h) or partial
- **N/A ⚪** — Asset class has no data (e.g., FX for fundamentals)
- **ERROR 🔴** — Dexter unreachable or error

### Dexter panel fields:
- Last query (truncated)
- Result summary (1-2 lines)
- Sources (domain list)
- Data age (human-readable)
- Call count against daily cap
- Status indicator

### Theme tokens applied:
- Cards: `background: var(--panel)`, `border: 1px solid var(--border)`, `border-radius: var(--radius)`
- Text: `color: var(--text)` for body, `color: var(--muted)` for timestamps
- Status badges: `.status-badge` classes (live=green, stale=warn, offline=loss)
- Mono values: `font-family: var(--font-mono)` for scores, timestamps

---

## Implementation Checklist (future, not in this session)

- [ ] Add `DEXTER_API_KEY` to `config_ext.yaml` + loader
- [ ] After the install path is known, add a Bun JSON wrapper. Do not import `Agent` from Python
- [ ] Implement timeout + AbortSignal wrapper
- [ ] Build strict output schema parser (reject untrusted fields)
- [ ] Add per-symbol/per-day cache layer
- [ ] Wire Agents 04, 05, 06, 07 to dexter_client
- [ ] Build "🧠 Research Desk" view in `ui/research_desk.py`
- [ ] Add `source: Dexter` labels to dashboard outputs
- [ ] Rate-limit calls (configurable cap, default 50/day)
- [ ] Add tests: mock Agent, verify timeout, verify schema enforcement

## Task 7 — section 9 was not in the instruction file

Checked `docs/Instructions_2.md` on 2026-10-08. The file is section 10 only. It says to execute Task 6 exactly as written in section 9 of that same file, and to stop that part if section 9 is missing. Section 9 is missing. Dexter was not installed. No wrapper, Python client, `learning/` runner, outcome ledger, or Research Desk view was added.

`INFOWAY_API_KEY` is a different vendor. It does not substitute for `FINANCIAL_DATASETS_API_KEY`. Dexter stays in fixture mode until a financialdatasets.ai key exists, and that work is still blocked on the missing section 9 text plus an install path. The discovery notes above still stand: no Dexter binary was found on 2026-10-07, and this session did not search again or start Bun.
