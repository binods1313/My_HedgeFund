# Work Report — Deferred Gaps Closed After the Accepted Reality Check

> Date: 2026-10-14. Branch: `feature/echofi-ui` @ `e30c050`.
> Scope: PAPER only, approve-first default, auto-execute OFF.
> Follows: `docs/AutoTrading_Reality_Check.md` (accepted).

## Trigger

Your acceptance:

> "Report accepted. The reconcile fix, the instance lock and
> the lifecycle events are good. Findings that matter: Enable
> AI only changes OFF to IDLE, so no scan or trade chain runs
> today. Phase 2 therefore starts with the scan loop, not the
> proposer."

Two instructions were extracted from that: (1) the four
deferred gaps in the reality check should close, and (2)
Phase 2 is confirmed to begin at the scan loop. This report
covers (1) only.

## Bottom line

All four deferred items from the reality check are now closed
with tests. **No new trading capability was added.** The
engine still cannot scan, propose, or execute on its own, and
that is unchanged by design. What changed is that the gaps
the reality check flagged as missing now exist, are wired,
and are proven.

**Nothing is committed.** Every file below is untracked or
modified in the working tree. Branch HEAD is still `e30c050`.

Test status: **1376 passed, 6 skipped** (full suite,
`tests/`, excluding `test_secret_scan.py`, which is a
repo-history scan). The 6 skips are pre-existing intentional
exemptions in `test_boundaries.py` (5) and
`test_strategy_dryrun.py` (1), unrelated to this work.

## 1. Emergency stop — the UI half (was: "no UI halt button")

**Gap in the reality check:** the engine half of emergency
stop was proven (`engine/commands.py::_cmd_emergency_stop`,
terminal `EMERGENCY_STOP` in `engine/state.py`), but the
dashboard only offered a close-all button. A close-all is not
a halt: it shuts positions and lets the loop keep running.

**What was built:** `ui/components/emergency_stop.py`.

- One click enqueues `EMERGENCY_STOP` into the command queue.
  The engine claims it on its next poll and halts. It never
  sends an order — the same rule as the rest of the UI.
- No confirmation dialog. An emergency must not wait on one.
- Leaving the halted state is the deliberate direction:
  `RESET_EMERGENCY` requires the operator to type the exact
  phrase `RESET`, validated by the engine's state machine, not
  by this module. `RESET_PHRASE` is defined here with a
  comment tying it to `engine/state.py` so the two cannot
  drift; a wrong phrase queues and then fails loudly in the
  engine rather than being silently ignored here.
- **PAPER only.** `emergency_stop_availability()` returns
  unavailable in LIVE with "Disabled in LIVE mode. Paper mode
  only." The reasoning, stated in the docstring: this system
  is paper-only, and on a live account there is no automated
  loop for a halt to stop — the manual close controls are the
  live-account safety path.
- An already-halted engine reports unavailable rather than
  inviting a second stop; the reset control takes its place.

Tests: `tests/test_ui/test_emergency_stop.py`, **17 collected**.

## 2. Journal tables + System Status card (was: "Journal page shows `events` only")

**Gap in the reality check:** the Journal tab rendered the
`events` table alone. The seven tables that actually carry
trading history — signals, risk decisions, AI calls, scan
results, trades, positions, lifecycle — had no operator view,
and there was no status summary derived from journal facts.

**What was built:** `ui/components/journal_tables.py` plus
`read_journal_table()` / `system_status()` in
`ui/data_access.py`.

- `JOURNAL_TABLES` is an explicit whitelist mapping table name
  to its allowed columns. `read_journal_table()` refuses any
  name outside it with `ValueError` rather than coercing — a
  typo must surface, not render an empty table that looks like
  "no data".
- Opens the journal read-only. It never creates or mutates a
  file. An empty table is stated as a fact about the journal,
  not an error.
- Filters: `symbol` on the four tables that carry a symbol
  column, `decision_id` on the lifecycle chain. Both raise
  `ValueError` when applied to a table they do not fit,
  because a filter that silently does nothing would show an
  operator an unfiltered list under a filtered heading.
- The table picker renders its labels from `_TABLE_LABELS`,
  which is keyed by the same names the reader accepts, so the
  widget cannot name a table outside the whitelist.
- No action buttons anywhere in the module. Its docstring
  states the reason: a history view that can act on its rows
  is a trading surface wearing a history view's clothes.
- JSON-ish columns are flattened to readable text before
  display.
- `system_status()` feeds one card: engine ONLINE/OFFLINE +
  state + mode, heartbeat age, masked account + currency,
  schema version + pending command count. **Nothing in it
  talks to a process** — it reads journal facts only. If the
  journal does not exist it says so and returns.

Tests: `tests/test_ui/test_data_access.py` extended (whitelist
rejection, read-only guarantee, filter misapplication);
`tests/test_repo/test_journal_path_single_source.py`, **5
collected**, enforces that runtime code does not name a
journal file itself.

## 3. Research Desk status vocabulary (was: single word "OFF")

**Gap in the reality check:** §A5 required real
implementation-status words. The single word "OFF" was wrong
in a specific way: it implied a module did not exist.

**What was built:** a four-word vocabulary in
`ui/components/research_desk.py`, replacing "OFF".

| Word | Meaning |
| --- | --- |
| `Implemented, live` | the engine actually invokes it |
| `Implemented but disabled` | real code, not currently wired |
| `Deterministic` | modules 09, 10 — no model call |
| `Not implemented` | a true placeholder whose `run()` is a stub |

`Implemented, live` never renders today, and the code comment
says why: the engine loop wires no research module. It is
reserved for a module the engine genuinely invokes, so the
word cannot be claimed early. `Not implemented` is the rule
for a real stub.

Tests: `tests/test_ui/test_research_desk_status.py`, **8
collected**.

## 4. Banned vocabulary reaching the operator

**Gap:** vendor, model and internal project names could reach
an operator through a rendered string.

**What was built:** `tests/test_ui/test_no_vendor_names.py`
walks the AST of every module under `ui/` and inspects only
strings an operator can see — arguments of `st.*` calls,
`return` values, and module-level string constants where label
tables live. 20+ banned patterns matched on word boundaries,
case-insensitively, including `agents?`, `brain`, `xai`,
`grok`, `openai`, `gemini`, `llama`, `claude`, `infoway`,
`dexter`, `agentrouter`, `anchor`, `kalkiquant`,
`metaquotes`, `terminal64`.

The allow-list is narrow and documented, which is what makes
the test trustworthy. It deliberately does **not** flag:
docstrings; identifiers and attribute names (including the
internal `ai_brain` module, the `AGENT_IDS` constant,
`render_infoway_chip`); environment variable names; `key=`
arguments and `accent_card`'s second argument (widget
identifiers, never pixels); HTTP hosts, paths and log
messages.

Two properties worth your attention, because a guard test that
cannot fail is worth nothing:

- `test_the_collection_is_not_empty` — an earlier version
  resolved the wrong parent directory, so `rglob` found
  nothing and pytest reported one *skipped* parametrize
  instead of a failure. That is an empty scan masquerading as
  a clean result. The test now asserts the scan covers at
  least 10 modules and the specific files it was written for.
- `test_the_scan_actually_finds_a_planted_offence` — a
  positive control. It plants banned words in a temp module
  and asserts the scanner catches a rendered subheader and a
  rendered caption, while correctly *not* flagging an internal
  module import or a widget key.

Both were added after that empty-path bug, so the guard is
verified against its own failure mode.

Also cleaned: 4 rendered strings scrubbed in
`ui/components/research_desk.py`.

## Account guard hardening (carried from the accepted work)

The reconcile fix you accepted is backed by
`tests/test_engine/test_account_guard.py`, **38 collected**,
and `test_account_guard_e2e.py`, **8 collected**. Two guard
behaviours were tightened during this stretch:

- **False-trip immunity.** The one false trip recorded in the
  reality check (old journal, NULL stamp race, identical
  logins) is now covered by a regression test, so the
  first-run stamp race cannot silently reintroduce it.
- **Journal stamping.** A verified guard action stamps
  `journal_account` + currency + server. The test asserts the
  stamp is written, so a block that cannot be explained
  afterwards is a test failure rather than a forensic puzzle.

Evidence for the real action is unchanged: 4,430
`session_refresh` blocks in the new journal with the terminal
offline, live account NULL vs journal `*1780`.

## What is explicitly NOT done — read this before planning Phase 2

Your instruction was that Phase 2 starts with the **scan
loop, not the proposer**. Nothing in that direction has been
built. Verified by search, not by assumption:

- **No scan loop exists.** A search for `scan` across
  `engine/` returns only `STATE_SCANNING` in `engine/state.py`
  and `engine/journal.py`, plus one comment in
  `strategy.py`. There is no scanner, no `run_scan`, no
  `scan_universe`.
- **`CYCLE_TICK` is defined but never emitted.** The
  transition table in `engine/state.py:72` defines
  `IDLE --CYCLE_TICK--> SCANNING`, and `SCANNING` defines
  `SCAN_COMPLETE` / `SCAN_EMPTY`. But searching
  `engine/__main__.py` for `CYCLE_TICK` returns **nothing**.
  The state machine is ready for a scan cycle that no code
  triggers.
- **`scan_results` exists and nothing writes to it.**
  `engine/journal.py:181` creates the table. There is no
  insert path.
- **The main loop does not scan.**
  `engine/__main__.py::run_forever` calls `run_once()` on a
  `command_poll_seconds` sleep. `run_once()` polls commands,
  verifies account transitions, does a session-refresh account
  check, heartbeats and writes telemetry. There is no market
  read, no indicator computation, no signal path.

So `ENABLE_AI` moving OFF → IDLE still starts nothing. The
proposer is untouched and unreachable. This confirms your
reading rather than changing it.

## Recommendation

Phase 2 should open with the scan cycle, wired to the state
transitions that already exist:

1. A `CYCLE_TICK` emitter in `run_once()`, gated on state
   being IDLE, honouring the existing poll cadence.
2. A scanner that reads market data and writes real
   `scan_results` rows.
3. `SCAN_COMPLETE` / `SCAN_EMPTY` returns, so the cycle is
   observable in the journal.

Then, and only then, the proposer — a proposer fed by nothing
is untestable, and the state machine will not let it run
anyway: `ANALYZING` is reachable only from `SCANNING`.

The loop throttle is already covered by
`tests/test_engine/test_engine_loop_throttle.py`, **5
collected**, so a new scan step inherits that guard rather
than needing a new one. One caution recorded in
`__main__.py`: the heartbeat, telemetry and backup gates sit
*inside* the loop body deliberately. They were once indented
at method level instead, which put the rate limit and the
sleep after the loop, so the loop ran unthrottled and wrote
several hundred heartbeats a second. A scan step must not
repeat that indentation.

## Files

Added:

- `ui/components/emergency_stop.py`
- `ui/components/journal_tables.py`
- `tests/test_ui/test_emergency_stop.py` (17)
- `tests/test_ui/test_research_desk_status.py` (8)
- `tests/test_ui/test_no_vendor_names.py` (43)
- `tests/test_ui/test_currency_rendering.py` (11)
- `tests/test_engine/test_account_guard.py` (38)
- `tests/test_engine/test_account_guard_e2e.py` (8)
- `tests/test_engine/test_engine_loop_throttle.py` (5)
- `tests/test_engine/test_reality_gate.py` (5)
- `tests/test_engine/conftest.py`
- `tests/test_repo/test_journal_path_single_source.py` (5)

Modified: `ui/data_access.py`,
`ui/components/research_desk.py`,
`ui/components/auxiliary_views.py`, `ui/components/header.py`,
`ui/components/enable_ai.py`, `engine/journal.py`,
`engine/__main__.py`, `engine/risk.py`, `engine/executor.py`.

Verification command:

```
python -m pytest tests -q --ignore=tests/test_secret_scan.py
# 1376 passed, 6 skipped in 30.29s
```

## Open items (unchanged, still need your approval)

- `engine/brain.py` vs `ai_brain.py` ownership — one owner,
  not two.
- Ownership table + architecture diagram.
- Phase 2 scan loop: ready to start, not started.
- **Git:** none of this is committed. HEAD is `e30c050`; the
  work above is untracked or modified in the working tree. It
  needs `git add` plus a commit before it is reviewable as a
  diff or deployable.
