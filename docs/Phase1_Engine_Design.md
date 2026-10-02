# Phase 1 — Engine Skeleton: Design Blueprint

**Status:** proposal, awaiting review. No code written.
**Branch:** `phase-1-engine-skeleton` (from `main` @ `1d05b81`, tag `phase-0-complete`)

## 0. Goal and non-goals

Phase 0 built the safety envelope: order guards, symbol whitelist, LLM
validation, hallucination detection, mutation-tested. What does **not** exist yet
is a process that runs without a browser, owns the MT5 connection, and can be
commanded.

**Goal:** a standalone engine process that owns MT5, persists state, accepts
commands from the UI, and can execute PAPER trades end to end.

**Non-goals (deliberately):** no strategies, no scanner, no autonomous loop, no
LIVE trading. Risk Engine is Phase 3. Autonomy is Phase 8. Shipping any of that
now would put unvalidated logic in front of the broker.

## 1. Package layout

```
engine/
  __main__.py     entry point: python -m engine
  config.py       config.yaml load + validation (wraps config_loader)
  state.py        state machine, transition table, heartbeat
  gateway.py      the ONLY module permitted to touch the MT5 API
  executor.py     Executor ABC + PaperExecutor + LiveExecutor
  journal.py      SQLite schema, migrations, repositories
  risk.py         Phase 1 stub: rejects everything until Phase 3
  commands.py     command definitions and the poll loop
```

Two invariants:

- **`gateway.py` is the only engine module that imports `MetaTrader5`.**
  Enforced by a test that greps every engine module for the import.
- **The UI stops importing `MetaTrader5` during Phase 1.**

  **This is not yet true and must not be presented as done.** As of
  `phase-0-complete`, `app.py:5` still has `import MetaTrader5 as mt5` and uses
  `mt5.TIMEFRAME_M15` / `mt5.TIMEFRAME_H4` directly (app.py:906-907). Phase 0
  was scoped to order-safety rules, not to the UI/engine split, so the UI kept
  its market-data access.

  Phase 1 removes it. Two steps, in order:
  1. Move timeframe constants into `data_engine` (or a small `timeframes`
     module) so the UI no longer needs the MT5 symbol.
  2. Once `app.py` imports nothing from MetaTrader5, add the test asserting
     that, mirroring the existing engine-only test.

  Until step 2 lands, there is **no test guaranteeing the UI stays
  MT5-free**, and a future edit could reintroduce the import. That is the real
  gap, and the test is what closes it.

## 2. Database

SQLite, WAL mode. The UI reads; the engine writes. They share the file and
never both write a transaction.

| Table | Purpose | Key columns |
|---|---|---|
| `schema_version` | migration ledger | `version` PK, `applied_at` |
| `engine_state` | current state, one live row | `id=1`, `state`, `reason`, `mode`, `updated_at` |
| `commands` | UI → engine queue | `id` PK, `command`, `params_json`, `status`, `result`, `ts`, `claimed_at` |
| `events` | append-only audit | `id` PK, `ts`, `category`, `severity`, `source`, `message`, `context_json` |
| `trades` | orders and their lifecycle | `id` PK, `ticket`, `symbol`, `side`, `volume`, `sl`, `tp`, `status`, `ts` |
| `signals` | strategy output (Phase 5+) | `id`, `symbol`, `direction`, `score`, `bar_ts` |
| `risk_decisions` | every gate verdict | `id`, `trade_id`, `approved`, `reasons_json`, `ts` |
| `llm_calls` | latency and verdict | `id`, `ts`, `decision`, `confidence`, `ok`, `error` |
| `scan_results` | scanner output (Phase 6+) | `id`, `ts`, `symbols_json`, `ranked_json` |
| `equity_snapshots` | equity curve | `id`, `ts`, `equity`, `balance`, `margin`, `open_risk` |
| `quotes` | latest tick per symbol | `symbol` PK, `bid`, `ask`, `spread`, `tick_time` |
| `positions` | live position mirror | `ticket` PK, `symbol`, `volume`, `sl`, `tp`, `profit`, `updated_at` |
| `heartbeats` | liveness | `id` PK, `ts`, `state`, `pid` |

**Indexes:** `commands(status, ts)` for the poll, `events(ts)`, `events(category, ts)`,
`trades(ticket)` unique, `positions(symbol)`, `quotes(tick_time)`.

**Migrations:** numbered, forward-only, each in its own transaction, recorded in
`schema_version`. A failing migration aborts startup — the engine refuses to run
against a schema it does not understand. Daily backup copies the file to
`backups/engine_YYYY-MM-DD.db`; WAL checkpoint before copy.

**Contention:** WAL allows one writer. The UI is read-only against engine-owned
tables and inserts only into `commands`. Any engine write inside an explicit
transaction.

## 3. State machine

Nine states. Transitions are declared, not ad hoc:

| From | To | Trigger |
|---|---|---|
| OFF | IDLE | `ENABLE_AI` |
| IDLE | SCANNING | cycle tick (Phase 6) |
| SCANNING | ANALYZING | scan complete |
| ANALYZING | VALIDATING | signal produced (Phase 5) |
| VALIDATING | EXECUTING | risk APPROVED |
| EXECUTING | MONITORING | order settled |
| MONITORING | IDLE | cycle complete |
| any | PAUSED | `PAUSE`, risk limit, gateway disconnect |
| any | EMERGENCY_STOP | `EMERGENCY_STOP`, `CLOSE_ALL` |
| PAUSED | IDLE | `RESUME` |
| EMERGENCY_STOP | OFF | `RESET_EMERGENCY` only |

**EMERGENCY_STOP is terminal.** Nothing leaves it except an explicit reset, and
the reset demands typed confirmation. Not from a timeout, not from a reconnect,
not from a restart.

Every transition writes an `events` row with from, to, trigger, and reason.
Illegal transitions raise and log rather than being silently ignored — a bug
that tries to jump SCANNING → EXECUTING should fail loudly.

**Heartbeat:** every 5 s while alive. UI shows ENGINE OFFLINE when the newest
heartbeat is older than 15 s. That gap is intentional: three missed beats is
noise, four is a dead process.

## 4. Command channel

UI inserts a row; the engine polls every 1 s and claims it atomically
(`UPDATE ... SET status='IN_PROGRESS' WHERE id=? AND status='PENDING'`), so a
restart cannot double-execute a claimed command.

| Command | Params | Notes |
|---|---|---|
| `ENABLE_AI` | — | OFF → IDLE |
| `DISABLE_AI` | — | IDLE → OFF |
| `PAUSE` | `reason` | blocks new orders |
| `RESUME` | — | only from PAUSED |
| `EMERGENCY_STOP` | `close_positions: bool` | always works |
| `RESET_EMERGENCY` | `confirmation: str` | must equal the displayed phrase |
| `CLOSE_POSITION` | `ticket` | |
| `CLOSE_ALL` | — | |
| `UPDATE_CONFIG` | `patch: dict` | revalidated before applying |

Status lifecycle: `PENDING → IN_PROGRESS → DONE | FAILED`. Every transition is
timestamped, and `FAILED` always carries a reason. A command that fails validation
is `FAILED`, never silently dropped.

**Idempotency:** each carries a `dedupe_key` with a unique index. `RESET_EMERGENCY`
is naturally idempotent; `CLOSE_POSITION` checks the position exists before
acting. Replaying a `DONE` command with the same key is a no-op.

## 5. Gateway

One thread owns MT5. `mt5.initialize()` and `login()` run **inside** that thread,
never at import.

**Request model:** callers submit `(fn, args, callback)` to a queue and wait on
a `threading.Event` with a timeout. Per-call default 10 s.

**On timeout: raise `TimeoutError`. Never return `None`.** A silent `None` is
indistinguishable from "the call returned nothing", which is how ghost orders
appear.

**Order timeout reconciliation.** This is the important one. If `order_send`
times out, the order may still have executed. The gateway:

1. Raises `TimeoutError` — it does not retry.
2. Calls `history_orders_get(position=...)` over a lookback window to find the
   order.
3. Calls `positions_get(symbol=...)` to see whether it opened.
4. Resolves to one of: `EXECUTED(ticket)`, `NOT_EXECUTED` (safe to retry), or
   `UNKNOWN` (halt and alert — never retry, never assume).

Retrying blind after a timeout is how you get duplicate positions.

**Reconnect:** exponential backoff 1 → 2 → 4 → 8 → 16 → 30 s (capped), with
jitter. While disconnected, `connected=False`, trading is blocked, the state
machine is held in PAUSED, and commands other than `EMERGENCY_STOP` queue.

**Serialization:** MT5 is not thread-safe. All calls funnel through the one
thread. The UI can never block on a hung call because it never calls MT5.

## 6. Executor

```
Executor (ABC)
  prepare_order(...) -> OrderRequest      # pure, no I/O
  submit(request) -> ExecutionResult
  close(ticket) -> ExecutionResult
```

**PaperExecutor** simulates: fills at the current ask/bid, tracks positions in
the `positions` table, computes P&L from quotes, honours SL/TP on each tick.
It must implement the same interface faithfully, or PAPER proves nothing.

**LiveExecutor** wraps the Phase 0 rules: lot normalization, `order_check` before
`order_send`, `retcode` verified on every response, `type_filling` read per
symbol, `stops_level` respected, broker-side SL mandatory, magic number set.

**Enforcing "only the executor sends orders":**

1. `gateway.py` exposes `send_order()` and nothing else order-related.
2. No other engine module imports `MetaTrader5`.
3. A test greps every engine module: `order_send` appears only in `gateway.py`
   and `executor.py`.
4. A test patches `gateway.send_order` and asserts a trade attempt through any
   other path fails.

**Mode:** `config.yaml` `mode: PAPER | LIVE`. PAPER is the default. LIVE requires
both the flag *and* a typed confirmation command from the UI — either alone is
refused. The badge is always visible; the UI shows the live mode prominently, and
switching requires confirmation.

**Risk gate:** Phase 1 `risk.py` returns `REJECTED` for everything. This is
deliberate — a stub that approves would be dangerous, and a stub that rejects
means no trade can reach the broker before Phase 3 ships a real gate. Manual
`CLOSE_POSITION` stays available so the command path can be exercised.

## 7. config.yaml additions

```yaml
mode: PAPER                      # PAPER | LIVE
live_confirmation_required: true

symbols:
  mapping:                       # logical name -> broker symbol
    NAS100: US100Index           # populated from verification discovery
    BTCUSD: COIN

engine:
  heartbeat_seconds: 5
  offline_after_seconds: 15
  command_poll_seconds: 1
  gateway:
    call_timeout_seconds: 10
    reconnect_backoff_seconds: [1, 2, 4, 8, 16, 30]
    order_lookback_seconds: 120
  db_path: data/engine.db
  backup_dir: backups

risk:                            # Phase 3 fills these in
  max_risk_per_trade_pct: 1.0
  max_total_risk_pct: 5.0
  max_daily_loss_pct: 2.0
```

Validated at startup with pydantic. Invalid config refuses to start — the engine
logs the offending field and exits non-zero. Symbol mapping is checked against
the broker at startup; unresolved symbols are reported and excluded.

## 8. Restart reconciliation

On startup, before accepting commands:

1. Read all positions with our magic number from MT5.
2. Compare against the `positions` table.
3. MT5 has one we do not → insert (adopted).
4. We have one MT5 does not → mark `CLOSED_EXTERNALLY`, never silently delete.
5. Discrepancies write an `events` row.

No orphans, no duplicates. Any mismatch is logged loudly and surfaces in the UI.

## 9. Test plan

**Unit:** state machine (every legal transition passes, every illegal one is
rejected); gateway (timeout raises, backoff schedule, reconciliation outcomes);
commands (pending → done → failed, claim is atomic, dedupe); config (bad values
refuse start, unresolved symbols reported); PaperExecutor (fills, SL/TP hits,
P&L); reconciliation with fake positions.

**Mutation checks** for safety rules:

| Rule | Mutation |
|---|---|
| PAPER is default | default flips to LIVE |
| LIVE needs typed confirmation | confirmation alone accepted |
| Risk gate blocks | stub returns APPROVED |
| Order without SL rejected | SL check removed |
| Emergency stop blocks orders | emergency state still permits sends |
| Only executor calls `order_send` | direct call from another module |
| EMERGENCY_STOP is terminal | timeout auto-resets it |
| Gateway timeout raises | returns `None` |
| Idempotency | dedupe check removed |
| Command channel is insert-only | UI performs an action directly |

**The Streamlit app must import without `MetaTrader5` by the end of Phase 1.**
This is a Phase 1 deliverable, not a Phase 0 guarantee: `app.py:5` currently
imports it (see §1). The test is added as part of step 2 above, and must fail
before the import is removed — otherwise it does not prove anything.

## 10. Acceptance

1. `python -m engine` runs standalone; closing the browser does not stop it.
2. Heartbeat every 5 s; killing the engine shows ENGINE OFFLINE within 15 s.
3. ENABLE_AI / DISABLE_AI / PAUSE / EMERGENCY from the UI change state, and
   transitions appear in `events`.
4. Restart restores positions without duplicates.
5. PAPER works end to end: manual order, then `CLOSE_POSITION` via the command
   channel.
6. Full pytest output, all passing.

## 11. Open questions

1. **DB location.** `data/engine.db` relative to repo, or an absolute path in
   config? Absolute is safer against a cwd difference between UI and engine.
2. **Command poll interval.** 1 s costs a query per second per process. Is 1 s
   acceptable, or should the UI wake the engine via a filesystem flag?
3. **`CLOSE_ALL` in emergency.** Should it require confirmation? It should, but
   an emergency that stalls on a dialog is arguably worse than one that does not.
   My default: EMERGENCY_STOP never prompts; `CLOSE_ALL` prompts unless it comes
   bundled with the stop.
4. **Position adoption on restart.** If MT5 has a position with our magic number
   that we have no record of, do we adopt it silently (logged) or refuse to start?
   I lean adopt-with-event, because refusing could leave unmanaged exposure.
5. **Multi-account.** One engine, one MT5 account? I assume yes for Phase 1.
6. **Magic number.** Phase 0 uses `100100`. Keep it, or allocate a distinct range
   per module so gateway and executor traffic is separable?

## 12. Risks

| Risk | Mitigation |
|---|---|
| Timeouts causing duplicate orders | Reconciliation, never blind retry |
| Paper/live divergence hiding a live bug | Identical interface; live rules re-verified in Phase 1 |
| Gateway thread death | Heartbeat + supervised restart |
| Two writers corrupting SQLite | WAL; UI read-only outside `commands` |
| Risk stub accidentally permissive | Phase 1 stub rejects everything; mutation-tested |
| Config drift between processes | Single `config.yaml`, validated by both |