# AutoTrading Reality Check — what exists today

> Date: 2026-10-14. Branch: `feature/echofi-ui` @ `e30c050`.
> Mode: PAPER, approve-first default, auto-execute OFF.
> Rule: code + journal rows only. No assumptions.

## ONE-PAGE SUMMARY

1. **Enable AI = state OFF to IDLE only.** Button inserts one
   `ENABLE_AI` command row; engine applies it to the state
   machine. Nothing scans, evaluates, validates or executes.

2. **No automatic orders exist today.** The loop
   (`engine/__main__.py::_run_cycle`) polls commands, heartbeats,
   writes telemetry. It never calls strategy, risk, approval or
   executor. The only order paths are the manual dashboard form
   and an ApprovalFlow nobody invokes.

3. **PAPER = simulated fills in the journal.** `PaperExecutor`
   mints 9M+ tickets, writes `trades`/`positions`, broker=PAPER.
   Reconcile only adopts our-magic broker positions and only
   deletes our-magic rows the broker no longer shows. Simulated
   rows are untouched. Foreign magic (incl. 234000) is log-only.

4. **Approval = Telegram YES/NO only.** No dashboard approval
   path exists. `submit_order` is a manual order, not an approval.

5. **Research Desk shows the shadow ledger, not the agents.**
   Agents 01-10 implement `run(context)` but the engine never
   calls them. Empty ledger = "No record yet". Fix wording.

6. **Account mismatch: 1 false trip + real guard action.**
   Old journal: same login both sides, NULL stamp race.
   New journal: 4,430 "no account logged in" blocks while the
   terminal is offline. Guard working as designed.

7. **Emergency stop: engine half PROVEN by test; UI half is a
   close-all button, not a halt.** Gap: no UI halt button.

8. **Journal page shows `events` only.** signals, risk, llm,
   scans, trades, positions, equity have no UI. Fix in gate.

9. **Branch isolation CONFIRMED.** Dexter/AgentRouter/theme on
   `feature/echofi-ui` only. Banned-word guard needs new words.

10. **`ai_brain.py` must not rival `engine/brain.py`.** One owner
    per stage (table below). `engine/brain.py` NOT created yet.

## 1a. Enable AI — what exactly starts

Chain (PROVEN, code):

- `ui/components/enable_ai.py::queue_enable_ai` inserts
  `ENABLE_AI` into `commands`. It never sends an order.

- `engine/__main__.py::Engine._poll_commands` claims it;
  `engine/commands.py::_cmd_enable_ai` runs
  `StateMachine.apply("ENABLE_AI")`.

- `engine/state.py` allows only `OFF --ENABLE_AI--> IDLE`.

What changes: state OFF→IDLE, one `state` event row, one
`command ENABLE_AI -> DONE` row. What does NOT start:
scanning, strategy eval, risk, LLM, Telegram, execution.
`_run_cycle` (`engine/__main__.py:630`) after the transition
only polls again, heartbeats, writes telemetry. No caller in
the loop invokes `strategy`, `risk`, `approval_flow`,
`research`, or `executor`. The SCANNING+ transitions exist in
the table but nothing fires them. Rename the button to
"Arm Paper Engine (state only)". Status: PROVEN.

## 1b. Conditions for an automatic order today

None can be met end-to-end (UNPROVEN chain):

1. Engine IDLE + a scan producing a signal — no scan runs.
2. `engine/strategy.py` confluence (5 blocks agree) — never
   called by the loop.

3. Research 01-10 input — `run(context)` importable, never
   invoked by the engine.

4. `RiskEngine.evaluate` approval + `RiskApproval` — works
   (TESTED-ONLY), no automated caller builds intents.

5. LLM validation — validation-only, no auto caller.
6. Telegram YES — needs a pending trade nobody creates.
7. Executor submit with SL + approval + state OK — gates
   PROVEN by unit test, chain UNPROVEN.

## 1c. PAPER routing + reconcile proof

`build_executor("PAPER")` returns `PaperExecutor`
(`engine/executor.py:528`). `submit` mints ticket 9M+,
fills at live quote, writes `trades` (broker=PAPER) and
`positions`, logs `trade/paper order filled`. No MT5 send.
`LiveExecutor` needs config LIVE + live_confirmed + --live.
TESTED-ONLY (`test_fill_opens_a_position`,
`test_paper_is_the_default`, `test_live_requires_conf`).

Reconcile (`engine/__main__.py:417`): adopts only broker
positions with our magic (100100); foreign (incl. 234000)
is log-only; deletes only our-magic rows absent at broker.
Simulated 9M tickets never collide. So simulated positions
are NOT deleted. Gate adds `test_paper_positions_survive_
reconcile`. After gate: PROVEN.

## 1d. Approval today

Telegram YES/NO only (`engine/notifications/
telegram_service.py::process_yes_approval`,
`process_no_approval`), driven by inbound Telegram polling
in `_poll_telegram`. Dashboard has NO approval path:
`submit_order` is a manual order, not an approval of a
pending trade. No pending-trade queue in the UI. PROVEN.

## 1e. Research Desk: why OFF / No record yet

The Desk (`ui/components/research_desk.py::_agent_cards`)
reads `data/learning.db` predictions, NOT the agents. Empty
ledger = "OFF" / "no closed-bar record yet". All 10 agents
implement `run(context)` (Implemented, none is a stub):

01 market-regime, 02 technical (EMA-MACD), 03 MTF,
04 news (needs events ctx), 05 sentiment, 06 fundamental,
07 quant, 08 strategy, 09 risk-adapter, 10 exec-adapter.

But the engine loop never calls any of them, and the shadow
thread only fills learning rows. Correct status words (§A5):
Implemented but disabled (01-08: no live caller; 09-10:
deterministic, shown OFF by `_DETERMINISTIC`). Gate fix:
replace "OFF" with exact status words + "Not implemented"
rule for true placeholders.

## 1f. Where history lives in the UI

- Trade history (`trades`): NO page. Gap.
- Signal history (`signals`): NO page. Gap.
- Risk decisions (`risk_decisions`): NO page. Gap.
- Execution decisions (`trade/*` events): Journal page shows
  events; structured trades/positions: NO page. Gap.

- Journal page today = `events` table only
  (`render_journal_view`). Gate adds read-only tabs.

## 1g. Emergency stop in the UI — does it work?

Two different things share a name:

- Engine halt: `EMERGENCY_STOP` command → terminal state
  (`engine/commands.py:154`, `engine/state.py`). PROVEN:
  `test_emergency_needs_no_confirmation`,
  `test_emergency_blocks_submission`,
  `test_closing_is_allowed_while_emergency`.

- UI "Emergency close all" at
  `ui/components/positions_matrix.py:90`: loops
  `close_position(ticket)`. Closes positions; does NOT
  enqueue EMERGENCY_STOP, does NOT halt the engine. Gap
  fixed in gate: real halt button enqueuing the command
  (PAPER) + test.

## 6. Account mismatch evidence (masked)

Rule: last-4 + currency only. Full logins stay out.

Old journal `data/engine.db` — 1 row:

- 2026-10-13T00:49:54Z | phase=startup | expected \*1780
  (config) | actual \*1780 live on MetaQuotes-Demo (USD) |
  journal stamp NULL | reason "configured ... is not the
  logged-in ..." with identical logins. FALSE TRIP: first-
  run stamp race; file later stamps journal_account
  (\*1780/USD/MetaQuotes-Demo) + engine_state OFF/PAPER
  verified. Same file, self-healed.

New journal `data/engine_5057151780.db` — 4,430 rows,
all `phase=session_refresh`, reason "no account is logged
in" (live NULL vs journal \*1780). REAL guard action while
the terminal is offline; writes correctly blocked. Latest
sample: 2026-10-13T21:59:51Z, pid 35972 heartbeats.

Second process: pid 5516 wrote old-journal heartbeats;
pid 35972 writes new-journal heartbeats. Two processes or
a restart across a journal-path switch. Gate fix:
single-instance lock refusing a second `python -m engine`

- journal-path doc update.

## ENABLE_AI popup — the 8 answers (from code)

The popup text is accurate: "Queue ENABLE_AI on the paper
engine. This does not send an order."

1. **Command inserted:** exactly one row,
   `command = "ENABLE_AI"`, `params = {"source": "ui"}`,
   into the `commands` table
   (`ui/components/enable_ai.py::queue_enable_ai`, via
   `Journal.enqueue_command`). Nothing else is written.

2. **Handler:** `engine/commands.py::CommandProcessor.
   _cmd_enable_ai` (line 108), reached through
   `Engine._poll_commands` (`engine/__main__.py:582`)
   which claims the row and calls `execute()`.

3. **Transition:** `OFF --ENABLE_AI--> IDLE`
   (`engine/state.py::ALLOWED`, `StateMachine.apply`).
   From any other state the command FAILS loudly, no retry.

4. **What begins automatically: nothing.** After IDLE the
   loop only polls commands, heartbeats, writes telemetry.
   No scanning, no strategy, no risk, no research agents,
   no signal generation, no Telegram approvals. Zero callers
   of strategy/risk/research/approval/executor in the loop.

5. **Visible evidence after success:**
   - Journal: two rows — `state OFF -> IDLE` and
     `command ENABLE_AI -> DONE`. Nothing else.
   - AI Signals: no change (engine writes neither).
   - Research Desk: no change (reads learning.db only).
6. **ENGINE ONLINE vs AI ENABLED:** ONLINE = liveness
   (fresh heartbeat + connected flag). AI ENABLED = trading
   state IDLE. A heartbeating engine with trading OFF is
   ONLINE but NOT enabled — the header shows both badges
   separately for exactly this reason.

7. **Disable command:** `DISABLE_AI`
   (`engine/commands.py:29`, maps to `IDLE --> OFF`).
   Same command-table path.

8. **Can ENABLE_AI cause an automated trade? No.**
   PROVEN: post-transition loop has no order path;
   `PaperExecutor.submit` needs a `RiskApproval` no
   automatic caller constructs.
