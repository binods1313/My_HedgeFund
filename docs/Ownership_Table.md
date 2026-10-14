# Ownership Table

Defines clear ownership boundaries to prevent module duplication and ensure single source of truth.

## Core Components

| Component | Owner | Purpose | Notes |
|---|---|---|---|
| i_brain.py | AI Brain Owner | LLM validation, confidence scoring. Deterministic unit-testable logic with no I/O/network. | Only place where LLM confidence affects trade permission. Cannot rival engine/brain. |
| engine/brain.py | Engine Owner (Not Created Yet) | Execution-time brain for runtime strategy logic. | MUST NOT be created until Stage 2+ per instructions. One owner only - cannot duplicate ai_brain logic. |
| engine/__main__.py | Engine Owner | Main engine loop, cycle polling, state machine transitions. | Handles commands, heartbeats, telemetry. Never calls strategy/risk/approval/exec in current state (as designed). |
| engine/state.py | Engine Owner | State machine (OFF -> IDLE -> SCANNING -> EVALUATING -> WAITING_APPROVAL -> EXECUTING -> ERROR -> STOPPING -> OFF). | Transitions: ENABLE_AI takes OFF->IDLE only. |
| engine/commands.py | Engine Owner | Command ingestion/processing (ENABLE_AI, EMERGENCY_STOP, CLOSE_ALL, etc.) | Validates and executes commands with idempotency. |
| execution.py | Execution Owner | PaperExecutor (simulated fills), order submission, reconciliation logic. | PAPER mode writes simulated trades/positions with our magic. Foreign magic (incl 234000) is log-only. |
| data_engine.py | Data Owner | Market data, indicators, symbol classification, spread/point calculations. | Handles broker-specific symbol naming (e.g. XM # suffix, *Cash#). |
| ui/components/header.py | UI Owner | System status card. | Shows honest state: trades_today None on closed connection, real journal values. |
| ui/components/research_desk.py | UI Owner | Research desk UI. | Agent cards with disabled reasons. |
| ui/data_access.py | UI Owner | Data access layer for UI. | Read-only DB queries; proper connection handling. |
| ui/components/enable_ai.py | UI Owner | Enable AI button -> inserts ENABLE_AI command. | Does not execute trades directly. |
| ui/components/emergency_stop.py | UI Owner | Emergency stop UI control. | Close-all and halt controls. |
| journal.py / DB | Data Owner | Journal/DB schema and writes. | Account guard, event logging. |
