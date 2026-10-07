# Project Structure

## Overview

This repository is a **Version 2.0** AI trading and execution terminal built around MetaTrader 5, a Streamlit dashboard, and a Python-based decision/execution stack. The project is organized as a hybrid of:

- a front-end control dashboard (`app.py`)
- a data layer for market and account telemetry (`data_engine.py`)
- an AI decision layer (`ai_brain.py`)
- an execution layer for trade routing (`execution.py`)
- a long-running engine runtime (`engine/` package)
- an optional Telegram approval layer (`engine/notifications/telegram_service.py`)
- supporting scripts, tests, and documentation

The codebase is designed to collect market data, evaluate signals, enforce risk rules, and manage and monitor trading activity in a controlled, scriptable way.

**Key V2.0 principle:** the LLM remains **validation only**. The processing chain is:

```
Strategies → Confidence Engine → Risk Engine → LLM Validator → Telegram Approval → Executor
```

Telegram is an **APPROVAL LAYER ONLY** — the human operator's YES/NO is what authorises a trade. Telegram never interacts with MT5 directly, never holds strategy logic, and never contains execution logic. All MT5 access goes exclusively through `engine/gateway.py` (single-threaded gateway owner model).

---

## Repository Layout

```text
HedgeFund_Binod_v2/
├── .env                    # local secrets (gitignored)
├── .env.example            # template for credentials
├── .gitignore              # ignores secrets, local data, generated files
├── .pre-commit-config.yaml # pre-commit quality checks
├── ai_brain.py             # AI signal/decision engine
├── app.py                  # Streamlit UI dashboard
├── backups/                # backup files and historical snapshots
├── config.py               # loads MT5 and LLM env secrets
├── config.yaml             # YAML config for rules, symbols, validation
├── config_loader.py        # strict Pydantic schema validation of config.yaml
├── data/                   # market data / runtime data store
├── data_engine.py          # MT5 market data and account integration layer
├── docs/                   # project documentation
│   ├── README.md
│   ├── api.md
│   ├── architecture.md
│   ├── contributing.md
│   ├── design/
│   ├── getting-started.md
│   ├── Instructions.md
│   ├── Instructions_1.md
│   ├── Instructions_2.md
│   ├── Phase1_Engine_Design.md
│   ├── ui.md
│   ├── V2_Implementation_Plan.md
│   ├── Architecture_Audit_Report.md
│   └── ProjectStructire.md
├── engine/                 # long-running trading runtime
│   ├── __init__.py
│   ├── __main__.py         # entry: python -m engine
│   ├── approval_flow.py    # approval pipeline wiring (risk → LLM → telegram → executor)
│   ├── commands.py         # command channel (ENABLE_AI, PAUSE, RESUME, ...)
│   ├── config.py           # engine configuration from config.yaml
│   ├── dryrun.py           # dry-run / dry-run-cli entry points
│   ├── dryrun_cli.py
│   ├── equity.py           # equity baselines and daily loss tracking
│   ├── executor.py         # order execution (PaperExecutor / LiveExecutor)
│   ├── gateway.py          # MT5 gateway: single-thread owner; all MT5 via here
│   ├── guard.py            # live guard for pre-dispatch structure checks
│   ├── indicators/         # structural and trend indicators
│   │   ├── structure.py
│   │   ├── trend.py
│   │   └── __init__.py
│   ├── journal.py          # SQLite journal, WAL mode, schema migrations
│   ├── keys.py             # provider credential routing (redacted)
│   ├── llm_client.py       # xAI provider client for LLM validation
│   ├── notifications/      # notification layers
│   │   ├── __init__.py
│   │   └── telegram_service.py  # Telegram approval layer
│   ├── risk.py             # Risk Engine: fail-closed limit gate
│   ├── sizing.py           # lot sizing with broker normalization
│   ├── state.py            # state machine + heartbeat writer
│   ├── strategy.py         # confluence-based signal generation
│   └── validation/         # structural validation gates
│       └── xai_client.py
├── execution.py            # trade execution orchestration and order actions
├── formatting.py           # formatting helpers for display and masking
├── metrics.py              # risk, exposure, and performance metrics
├── pytest_output.txt       # test run output (ephemeral)
├── README.md               # project overview and setup instructions
├── reports/                # verification and demo reports
├── requirements.txt        # Python dependencies (see notes below)
├── run.bat                 # Windows launcher for the app
├── scripts/                # operational and verification utilities
│   ├── __init__.py
│   ├── build_purge_expressions.py
│   ├── build_replace_text.py
│   ├── check_xai_key.py
│   ├── demo_verification.py
│   ├── engine_connect_check.py
│   ├── install_precommit_hook.py
│   ├── mutation_check.py
│   ├── precommit_hook.py
│   ├── probe_tick_specs.py
│   ├── purge_engine_db.py
│   ├── secret_scan.py
│   ├── secret_triage.py
│   ├── set_mt5_account.py
│   └── verify_history_clean.py
├── setup.bat               # Windows environment bootstrap script
└── tests/                  # test suite
    ├── test_demo_verification.py
    ├── test_dependencies.py
    ├── test_engine/        # engine unit tests
    │   ├── __init__.py
    │   ├── fake_mt5.py
    │   ├── test_boundaries.py
    │   ├── test_equity.py
    │   ├── test_executor_commands.py
    │   ├── test_gates.py
    │   ├── test_gateway.py
    │   ├── test_guard.py
    │   ├── test_indicators.py
    │   ├── test_llm_client.py
    │   ├── test_risk.py
    │   ├── test_risk_wiring.py
    │   ├── test_state.py
    │   ├── test_strategy_dryrun.py
    │   └── test_telemetry.py
    ├── test_execution_guards.py
    ├── test_formatting_and_mode.py
    ├── test_llm_validator.py
    ├── test_metrics.py
    ├── test_phase0_bugs.py
    ├── test_secret_scan.py
    ├── test_strategy_fixtures.py
    ├── test_ui/
    │   ├── test_data_access.py
    │   ├── test_news_parser.py
    │   ├── test_risk_calc.py
    │   └── test_state.py
    └── test_xm_symbols.py
```

---

## Root-Level Modules

### `app.py`

Main Streamlit application for the command center dashboard. It integrates:

- MT5 market data collection
- AI signal generation
- execution and position management
- account metrics / display formatting
- risk display and telemetry presentation

This is the user-facing operational surface for the system.

### `ai_brain.py`

The decision engine that evaluates market conditions and produces trade signals.

Responsibilities include:

- reading market data
- validating signal quality
- calling the LLM / model layer for decision support
- returning structured decisions such as BUY, SELL, or HOLD

### `data_engine.py`

Core market access and data retrieval layer for MT5.

Responsibilities include:

- initializing the MetaTrader 5 terminal connection
- enumerating symbol availability
- fetching market candles and timeframe data
- retrieving technical indicator data frames
- reading live quotes and account telemetry

### `execution.py`

Order execution and trade management layer.

Responsibilities include:

- submitting or closing positions
- getting open positions and account state
- resolving trading modes and order behavior
- managing execution risk gates and trade actions

### `config.py`

Loads runtime secrets from environment variables and prefixes them with known names such as:

- `MT5_LOGIN`
- `MT5_PASSWORD`
- `MT5_SERVER`
- `MT5_PATH`
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`
- `XAI_API_KEY`
- `GROQ_API_KEY`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

This file is intentionally environment-driven so credentials are not hardcoded into source files. Secrets are never printed or logged.

### `config_loader.py`

Strict YAML loader for `config.yaml`.

It validates the schema with Pydantic and enforces consistency between:

- tradable symbol lists
- symbol whitelist mappings
- verification settings
- LLM tolerances and trading configuration

This is a safety-focused loader intended to fail early on invalid config.

### `formatting.py`

Utility module for displaying values and masking sensitive numbers.

Includes formatting for:

- currency and balance values
- margin levels
- risk percentages
- account masking
- UI-friendly numeric output

### `metrics.py`

Calculates and formats risk and exposure metrics.

Includes:

- open risk calculations
- net open PnL
- gross exposure and other exposure metrics
- confidence and status color formatting

---

## Engine Package (`engine/`)

The `engine/` directory contains the long-running trading runtime. It is started as a standalone process via `python -m engine` rather than being embedded directly in the dashboard.

### `engine/__init__.py`

Package init declaring the public components: `commands`, `config`, `executor`, `gateway`, `journal`, `keys`, `notifications`, `risk`, `state`, `strategy`.

### `engine/__main__.py`

Main engine entry point.

It owns the runtime startup sequence and lifecycle, including:

1. config validation
2. journal opening and schema migrations
3. position reconciliation against the broker
4. MT5 gateway startup
5. telegram approval layer initialization (optional)
6. command polling loop, heartbeat, and telemetry
7. graceful shutdown: gateway stop, telegram polling stop, pending-trade cache clear

### `engine/commands.py`

Defines the command channel. The UI inserts rows into the `commands` table; the engine polls, claims, executes, and records the outcome. Known commands: `ENABLE_AI`, `DISABLE_AI`, `PAUSE`, `RESUME`, `EMERGENCY_STOP`, `RESET_EMERGENCY`, `CLOSE_POSITION`, `CLOSE_ALL`, `UPDATE_CONFIG`.

### `engine/config.py`

Engine-specific configuration read from `config.yaml`. Contains `EngineSettings`, `GatewaySettings`, and `RiskLimits` pydantic models that reject unknown keys. `read_mt5_credentials()` reads from `.env` via the keyring.

### `engine/equity.py`

Tracks equity baselines, start-of-day equity, and peak equity using the broker's server time (not local time) for day boundaries. Includes `EquityTracker` and `validate_server_epoch()`.

### `engine/executor.py`

Execution abstraction with two implementations:

- `PaperExecutor` — simulated fills against live quotes, positions persisted in the DB
- `LiveExecutor` — submits through the gateway with reconciliation

Shared gates enforced on both:

- SL required (`_check_sl`)
- real risk approval required (`_check_risk`)
- halt/paused blocked (`_check_state`)
- pre-dispatch structure invalidation check (`_check_invalidation`)

`build_executor()` constructs the appropriate executor for the configured mode.

### `engine/gateway.py`

The single-threaded MT5 gateway — the **only** place MT5 is accessed. It serializes all MT5 calls onto one owned thread (MT5 is not thread-safe) via a queue, handles reconnection with exponential backoff, and performs order outcome reconciliation (never retries on timeout). All modules, including the telegram approval layer, access MT5 exclusively through this gateway.

### `engine/journal.py`

SQLite journal in WAL mode with forward-only schema migrations. Tables: `schema_version`, `engine_state`, `commands`, `events`, `trades`, `positions`, `quotes`, `signals`, `risk_decisions`, `llm_calls`, `scan_results`, `equity_snapshots`, `heartbeats`. Thread-safe with an RLock. New telegram event types are recorded via `log_event("telegram", ...)`.

### `engine/keys.py`

Provider credential routing through a single `Keyring`. Keys are redacted in all logs and reprs; no key value ever appears in a log line or exception message.

### `engine/llm_client.py`

Thin, typed wrapper over the xAI/OpenRouter LLM endpoint. The LLM receives an exact numeric `Snapshot` (no free text, preventing hallucination). `Validation` verdict is `APPROVE` / `REDUCE` / `REJECT`; the model can only reduce confidence or flag a problem, never authorise an order. Every failure path resolves to `HOLD` (fail-safe); timeouts are not retried.

### `engine/notifications/telegram_service.py`

The **Telegram approval layer**.

Responsibilities:

- Telegram initialization from `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
- Telegram message delivery with MarkdownV2 escaping
- YES/NO approval handling
- Authorization checks (only `TELEGRAM_CHAT_ID` is allowed; all other chats ignored)
- Background polling lifecycle (long-poll `get_updates`)
- Journaling of telegram events
- Shutdown cleanup (pending-trade cache cleared)

Nothing else: **no MT5 logic, no strategy logic, no AI logic.** MT5 access remains entirely in `engine/gateway.py`.

Key types and functions:

- `PendingTrade` — trade awaiting approval (`trade_id`, `symbol`, `direction`, `entry`, `stop_loss`, `take_profit`, `lot_size`, `confidence`, `rationale`, `created_at`). Only one execution per `trade_id` is allowed; duplicate YES arrives are rejected with "Trade already processed."
- `init_telegram()` — initializes the bot; fails gracefully if credentials are missing (engine still starts)
- `_markdown_v2_escape()` — escapes `_`, `*`, `[`, `]`, `(`, `)`, `~`, `` ` ``, `>`, `#`, `+`, `-`, `=`, `|`, `{`, `}`, `.`, `!` per MarkdownV2
- `_build_approval_message()` — builds the approval prompt
- `store_pending_trade()` / `get_pending_trade()` / `remove_pending_trade()` — central pending-trade store
- `send_approval_message()` — sends the approval prompt
- `handle_approval_message()` — parses `YES <trade_id>` / `NO <trade_id>`
- `process_yes_approval()` — forwards YES to the executor, journals the result
- `process_no_approval()` — cancels and journals the NO
- `poll_updates()` — background polling loop
- `cleanup_pending_trades()` — clears the cache on shutdown

### `engine/risk.py`

The Risk Engine — a fail-closed limit gate. Every check either passes or blocks; none degrades to "allow" on missing data. Covers:

- per-trade risk as a percentage of equity
- total portfolio exposure
- daily loss (start-of-day equity)
- drawdown from peak equity
- open trade count
- per-symbol max spread
- unprotected positions (counted rather than assumed zero risk)

Every decision is written to `risk_decisions` with reasons, whether approved or blocked. `halt()` trips a hard halt; `clear_daily_loss_halt()` clears it each new trading day; `clear_manual_halt()` requires operator action for drawdown/manual halts.

### `engine/sizing.py`

Lot sizing: `LotSizer.size()` computes lots from risk budget and stop distance and normalises against the broker's `volume_step` and `volume_min`/`volume_max`. Fails closed if the raw lot is below the broker minimum before normalisation.

### `engine/state.py`

Engine state machine with 9 states: `OFF`, `IDLE`, `SCANNING`, `ANALYZING`, `VALIDATING`, `EXECUTING`, `MONITORING`, `PAUSED`, `EMERGENCY_STOP`. Explicit transition table; `IllegalTransition` raised for invalid transitions. `EMERGENCY_STOP` is terminal (only `RESET_EMERGENCY` exits) and survives restart. Journal-persisted durability.

### `engine/strategy.py`

Confluence assembly: five blocks (liquidity sweep, fair value gap, Fibonacci OTE, EMA ribbon, VWAP). A signal exists only when all required blocks agree; any opposing block is a conflict and the signal is rejected rather than down-weighted. Produces a `Signal` (`is_actionable` property) and `to_intent()` converts it to an `OrderIntent` (requires a broker tick specification).

### `engine/validation/` / `engine/indicators/`

Structural validation gates and the indicator set used by the strategy layer (swing leg / sweep detection, FVG, VWAP, EMA ribbon).

### `engine/approval_flow.py`

Approval pipeline wiring. It wires the existing components into the V2.0 chain:

```
signal → risk.evaluate() → llm_client.validate_trade() → telegram approval → executor.submit()
```

It never places an order itself; it holds only approval state and wires the components together.

---

## Notification Layer (`engine/notifications/`)

New in this update: the Telegram approval layer.

- `telegram_service.py` — the approval layer implementation (above)
- `__init__.py` — re-exports telegram event types from `engine.journal` plus the public telegram functions

Telegram events journal into the existing `events` table via `journal.log_event("telegram", ...)`:

- `SIGNAL_FOUND` — a signal entered the pipeline
- `SIGNAL_SENT_TO_TELEGRAM` — approval prompt sent
- `SIGNAL_FAILED_TO_SEND` — approval prompt failed to send
- `TRADE_EXECUTED` — order submitted successfully
- `TRADE_EXECUTED_FAILED` — executor rejected the order
- `EXECUTION_FAILED` — executor submission failed
- `SIGNAL_REJECTED` — operator NO received
- `TRADE_CANCELLED` — pending trade cancelled

`TELEGRAM_SIGNAL_APPROVED` is defined as a constant but is not currently emitted by the telegram service.

---

## Scripts and Automation

The `scripts/` directory contains operational and verification utilities used during development, testing, and deployment.

Key examples:

- `demo_verification.py` — verifies demo and validation behavior
- `engine_connect_check.py` — checks engine connectivity and readiness
- `secret_scan.py` and `secret_triage.py` — scan for secrets and review high-risk values
- `purge_engine_db.py` — resets or removes engine journal state
- `probe_tick_specs.py` — checks trading symbol/price metadata (required before sizing)
- `set_mt5_account.py` — sets or updates MT5 config/account information
- `verify_history_clean.py` — validates history or state normalization
- `install_precommit_hook.py` — ensures local quality gates are installed
- `dryrun.py` / `dryrun_cli.py` — dry-run modes that do not submit orders

The scripts are operational support tools, not the core trading logic itself.

---

## Data and Reports

### `data/`

This directory is reserved for runtime or generated market data. It may hold cache files, feed storage, or local data snapshots created during execution (including `engine.db`, `engine.db-shm`, `engine.db-wal`).

### `reports/`

Contains generated verification and demo logs, such as:

- demo verification outputs
- historical validation artifacts
- QA and operational reports

These are useful for auditing behavior and documenting runtime checks.

### `backups/`

Holds backup or historical copies used during development or operational recovery.

---

## Test Suite

The repository includes both broad and engine-focused tests under `tests/`.

### Engine tests

Located under `tests/test_engine/`:

- `fake_mt5.py` — fake MT5 module for tests
- `test_boundaries.py` — boundary/edge-case tests
- `test_equity.py` — equity tracker tests
- `test_executor_commands.py` — executor and command tests
- `test_gates.py` — risk gate tests
- `test_gateway.py` — gateway tests
- `test_guard.py` — live guard structural checks
- `test_indicators.py` — indicator tests
- `test_llm_client.py` — LLM client tests
- `test_risk.py` — risk engine tests (limits, fail-closed, halts)
- `test_risk_wiring.py` — risk integration tests
- `test_state.py` — state machine durability tests
- `test_strategy_dryrun.py` — strategy dry-run tests
- `test_telemetry.py` — telemetry tests

### Top-level tests

- `test_demo_verification.py`
- `test_dependencies.py`
- `test_execution_guards.py`
- `test_formatting_and_mode.py`
- `test_llm_validator.py`
- `test_metrics.py`
- `test_phase0_bugs.py`
- `test_secret_scan.py`
- `test_strategy_fixtures.py`
- `test_xm_symbols.py`

These tests validate safety, data flow, execution contracts, and the new telegram approval flow (in future additions).

---

## Configuration Model

The project uses a dual configuration approach:

1. `.env` / `config.py` for secrets and local runtime values
2. `config.yaml` + `config_loader.py` for structured application config

This separation is important because secrets should never be committed into source control, while the application config itself may be checked in and validated strictly.

Typical configuration includes:

- MT5 login/server/path
- API credentials
- LLM provider settings
- tradable symbol whitelist
- verification rules
- display and risk thresholds

---

## Execution Flow

At a high level, the project follows this flow:

1. Application boots via `run.bat` or `streamlit run app.py`
2. `config.py` loads environment secrets
3. `data_engine.py` connects to MT5 and fetches market and account data
4. `ai_brain.py` evaluates the market and decides on a signal
5. Signal passes through the engine: **Risk Engine → LLM Validator → Telegram Approval → Executor**
6. `engine/` continues the long-running runtime for journaling, command processing, and telemetry management
7. `tests/` validate safety, data flow, and execution contracts

This creates a layered architecture where the UI is operationally rich, but the real correctness and safety logic is enforced in lower layers. Telegram sits between the LLM validator and the executor: no order reaches the broker without an explicit operator YES, and no order is lost if the browser UI closes.

---

## Documentation Folder

The `docs/` directory contains both general and implementation-oriented project documentation.

Notable files:

- `README.md` — documentation index
- `getting-started.md` — local setup walkthrough
- `architecture.md` — architecture notes
- `api.md` — API documentation
- `contributing.md` — contribution standards
- `Instructions*.md` — operational or project instructions
- `Phase1_Engine_Design.md` — engine design ideas
- `V2_Implementation_Plan.md` — roadmap and phase deliverables
- `Architecture_Audit_Report.md` — project audit and evaluation record
- `ProjectStructire.md` — this file

---

## Setup and Launch

### Windows quick start

- `setup.bat` creates the environment and installs dependencies
- `run.bat` launches the terminal or app

### Manual run

```powershell
python -m venv .venv
\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

### Engine runtime

```powershell
python -m engine
```

---

## Operational Notes

- Secrets and credentials live in `.env` and should never be committed.
- Project is Windows-focused and assumes MetaTrader 5 installation on the host machine.
- The runtime mix of dashboard, MT5 access, and long-lived engine process is intentional and designed to keep the terminal resilient if the browser UI is closed.
- Safety and validation are emphasized through config schema checks, tests, and risk thresholds.
- The LLM is **not** the trading strategy — it is a validation layer only.
- Telegram is an approval layer only: it never calls MT5 directly, never contains strategy or execution logic, and ignores all unauthorized chats.
- All MT5 access goes exclusively through `engine/gateway.py`.

---

## Dependency Notes

`requirements.txt` includes:

- `MetaTrader5==5.0.6231` — MetaTrader 5 terminal automation
- `pandas`, `numpy`, `pyarrow`, `pydantic` — data and validation
- `streamlit`, `plotly`, `altair` — dashboard and charts
- `requests` — HTTP client (LLM and telegram calls)
- `python-dotenv`, `PyYAML` — config and secrets
- `watchdog`, `defusedxml` — file watching and safe XML
- `groq==1.7.0` — Groq LLM provider (fallback for validation)
- `pytest==9.1.1` — test runner (test-only)

---

## Summary

This project is a multi-layer algorithmic trading terminal with:

- a Streamlit front end for monitoring and control
- an MT5 data acquisition layer
- AI-based decision logic (LLM validation only)
- execution and order orchestration
- an engine package for long-running operational control
- a Telegram approval layer (human operator authorises each trade)
- test and verification tooling to enforce correctness and safety

The structure is modular, with each layer intentionally separated so the trading logic, config validation, execution checks, and UI presentation can evolve with minimal cross-coupling. The approval layer sits between the LLM validator and the executor, ensuring that human authorisation is the final gate before any order reaches the broker, while Telegram itself never touches MT5 directly.
