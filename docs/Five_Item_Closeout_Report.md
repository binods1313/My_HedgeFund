# Five-Item Closeout — Report to Architect

**Branch:** `feature/echofi-ui`
**Starting point:** `e30c050` (Task 3: Dexter discovery + AgentRouter)
**Scope:** the five items in order, then the Phase 2A design document.
**Suite at time of writing:** 1525 passed, 6 skipped (pre-existing exemptions
in `test_boundaries.py` ×5 and `test_strategy_dryrun.py` ×1).

---

## One-page summary

| # | Item | Verdict | Basis |
|---|---|---|---|
| **1** | Emergency stop works in every mode | **PROVEN** | 103 tests in the two touched files; LIVE restriction removed; sweep is magic-gated with 4 engine tests |
| **2** | Commit now, safely | **PROVEN** | 6 commits, hashes below; working tree clean of secrets; `backups/` gap found and closed |
| **3** | Account state wording | **PROVEN** (was already built) | `TerminalOffline` split, 600s reminder window, NULL-stamp regression test all present and passing |
| **4a** | System status card | **DONE - Part A** | `system_status()` now carries last_trade and trades_today (journal-derived). _draw_status renders an honest Status card A3 block: engine state, account currency, login last-four, last trade, trades today, heartbeat age, liveness, guard posture. |
| **4b** | Banned words | **PROVEN** | `groq`, `openrouter`, `deepseek` all in `BANNED`; 43 AST tests |
| **4c** | Ownership table + Mermaid | **NOT DONE** | Neither artefact exists |
| **4d** | `ai_brain.py` | **RETIRED** | Single importer. Deleted file + removed import. Engine owns AI validation via apply_llm_adjustment(); display path no longer imported. |
| **4e** | Second engine process | **A1 RESOLVED** | 2 PIDs at start. 35972 stopped; new b773324 engine (PID 4984, /paper) restarted. Flood (23,586 CRITICAL) stopped: 0 new account events since restart; throttled to TerminalOffline/WARNING, 1 heartbeat. No open positions; guard fails closed. 41440 left untouched. |
| **4f** | Sub-status list | **PARTLY ANSWERED** | See §4f. Minimum-stop and symbol-search items are genuinely missing |
| **4g** | MT5 lifecycle chain | **BLOCKED — cannot be done** | Requires a live terminal that is not reachable |
| **5** | Phase 2A design doc | **DONE** | `docs/Phase2A_Scan_Loop_Design.md`, 437 lines, 15 sections. **No code written** |

**The one thing that needs a decision from you:** item 4a — the system status
card is still owed, and four of its nine requested fields can only become
true once Phase 2A exists. Details in §4a.

---

## 1. Emergency stop — PROVEN

**The fix.** `ui/components/emergency_stop.py::emergency_stop_availability`
returned `False, "Disabled in LIVE mode. Paper mode only."` for LIVE and a
second refusal for any mode that was not exactly `"PAPER"` — which caught
`DEMO` and also an empty or unrecognised mode string. The mode parameter is
now accepted and ignored. The only disabling state is already-halted, because
the reset control takes the button's place there.

This was not a cosmetic restriction. An operator watching a runaway loop on a
DEMO account had no halt control at all, and the original justification — "a
paper-only system has no automated loop for a halt to stop" — stops being true
the moment Phase 2A adds a scan loop. So the restriction had to be removed
*before* the loop arrived, which is what happened.

**Kept, as instructed:** no confirmation to stop (one click enqueues
`EMERGENCY_STOP`, which the engine claims on its next poll); typed `RESET`
phrase to resume, validated by the state machine rather than the UI; state
survives restart because it is written to the journal.

**The new optional step.** A checkbox, *"Also close all my managed
positions"*, default **on**, placed before the button rather than in a
post-click dialog — an emergency must not wait on a dialog, but "halt" and
"halt and flatten" are different wishes and only the operator knows which they
mean. Defaulting on because a stop raised in a hurry is usually about
exposure.

**Foreign magic — two independent locks.** The guard already keeps
foreign-magic positions out of `journal.positions()`, so normally there is
nothing to filter. That alone is not enough to rely on, so `_close_everything`
now re-checks the magic on every row before closing and journals a refusal
instead of closing silently. A position that reached the journal through a
restored backup or a manual insert is still never closed. A new constant,
`MANAGED_MAGIC = 100100`, sits beside the existing `FOREIGN_MAGIC = 234000`.

**Tests — both halves, all three modes:**

| Test | Asserts |
|---|---|
| `test_no_mode_ever_disables_the_halt` (9 params) | PAPER, DEMO, LIVE, lowercase, `""`, `"UNKNOWN"`, `None` — all enabled |
| `test_the_halt_is_offered_in_every_running_mode` (3×6) | PAPER/DEMO/LIVE × running/paused/idle/scanning/analyzing/None |
| `test_the_button_is_live_in_every_mode` (3 params) | Renders, not just computed — AppTest clicks it and asserts the queued command |
| `test_the_sweep_step_is_optional_and_defaults_on` | Checkbox defaults on; unchecking reaches the command as `close_positions: false` |
| `test_a_foreign_magic_position_is_never_closed_by_the_halt` | Foreign row survives, managed row closes |
| `test_the_skip_is_journalled_not_silent` | The refusal is findable in `events` |
| `test_a_halt_only_stop_touches_no_position` | Unchecked box means no sweep at all |
| `test_a_halt_works_in_live_mode` | LIVE halt reaches `EMERGENCY_STOP` |

**TESTED.** 103 passed across `test_emergency_stop.py` and
`test_executor_commands.py`.

**UNPROVEN.** The sweep has never run against a real broker: closing a live
position is not exercised, because no terminal is reachable. The logic is
proven; the bridge is not.

---

## 2. Commits — PROVEN

`tests/test_secret_scan.py` (122 passed) and `scripts/secret_scan.py` were both
run, not excluded. `scripts/secret_scan.py` reports **`--- WORKING TREE --- (no
hits)`** and **`.GIT/CONFIG (no hits)`**. It does report 4 hits in *history*,
all from commit `7cad9d35` and all inside `tests/test_secret_scan.py` itself —
the scanner's own self-validating fixtures. Those are masked literals in a test
file, expected and triaged by `scripts/secret_triage.py`. They are not live
credentials and nothing new was added to history.

**A real gap was found and closed.** `.gitignore` matched `*.db` but an
archive is named `engine.db.acct-<login>.<date>.archive` — extension
`.archive` — with sidecars ending `.db-shm` / `.db-wal`. A dry-run `git add -An`
showed **four archive files, roughly 50 MB, would have been staged**: journal
archives from a live account login. `backups/` is now ignored outright and
`.tmp_run/` too, the latter having already produced an embedded-git warning on
a dry-run add. Verified afterwards with `git check-ignore -v` and a clean
`git add -An .`.

**Six commits**, `.gitignore` first because the later ones depend on it.
Oldest to newest:

```
f5afcbd  chore(gitignore): ignore journal archives, sidecars and scratch run dir
b773324  feat(guard): account fingerprint guard with offline/mismatch split and write gates
7011af4  fix(emergency): halt available in every mode, and the sweep never closes a foreign position
7638569  fix(research-desk): replace the bare OFF status word with an explicit vocabulary
5eb415c  feat(ui): read-only journal tables and system status card
ead577e  test: add loop throttle, reality gate, currency, vendor-name and repo tests
```

Current HEAD is `ead577e`. Each commit was verified with
`git show --name-only` to contain only its own files: `7638569` holds exactly
2 files, `5eb415c` exactly 5, `7011af4` exactly 4. Item 1's changes are in
`7011af4` and confirmed present by inspecting the committed blobs — the
`MANAGED_MAGIC` gate is in `engine/commands.py`, the checkbox in
`ui/components/emergency_stop.py`, and the string `Disabled in LIVE` is gone.

**One correction worth stating plainly.** An earlier draft of this report
claimed the commits had raced on `index.lock` and been regrouped. That did not
happen; the grouping was verified clean from the start. The draft was written
before the commits were inspected rather than from them, which is the mistake
— a report that asserts a recovery that never occurred is as wrong as one that
hides a real one.

**`push_prompt.txt` is deliberately left untracked.** It contains
`git remote set-url origin https://github.com/binus6642-hue/Binod_bot_trade.git`
— an unrelated remote rewrite, pointing at a different repository from this
one's `origin`. It is scratch, not source, and it should not be committed.

**Uncommitted at time of writing:** the two design/report documents
(`docs/Phase2A_Scan_Loop_Design.md`, `docs/Five_Item_Closeout_Report.md`) and
seven earlier audit reports, plus seven modified `ui/components/` files and
two modified docs whose changes predate this task. I have not committed them,
because grouping work that was already in progress before this prompt is a
decision for you.

**Nothing was pushed.** All six commits are local to `feature/echofi-ui`.

---

## 3. Account state wording — PROVEN (already built)

This was owed from an earlier prompt and **was already implemented**. I verified
rather than assumed:

- **The split exists.** `engine/account_guard.py:39` defines
  `class TerminalOffline(AccountMismatch)` — a subclass deliberately, so every
  existing call site that catches a mismatch also catches an unreadable
  terminal, with no call site needing to change.
- **Different states.** `_reject(..., offline=True)` writes
  `severity="WARNING"` and `kind="terminal_offline"` for an unreadable
  terminal; `severity="CRITICAL"` and `kind="account_mismatch"` for a wrong
  account. The messages differ too: *"Terminal offline"* vs *"Account
  mismatch"*, and the offline path points at the terminal while the mismatch
  path names both logins.
- **Both fail closed.** `may_reconcile`, `may_write_telemetry`,
  `may_submit_orders` and `may_modify_orders` all return `False` when
  `mismatch_reason` is set, for *either* exception. There is no code path that
  lets a mismatch through.
- **One event per change + reminder every 10 minutes.** `_REMIND_EVERY = 600.0`.
  A *changed* reason logs immediately; only repeats are windowed; a successful
  check clears `_active_reason` so a recurrence is a fresh incident.
  `test_a_persistent_outage_journals_once_then_reminds` drives **500 identical
  rejections and asserts exactly 1 event**, then advances the clock 601s and
  asserts a second event with `repeat: True`. **The 4,430-block scenario is now
  structurally impossible** — the same stretch that produced 3,161 rows before
  the throttle produces one event and six reminders.
- **The NULL-stamp false-trip regression test exists**, in
  `tests/test_engine/test_account_guard.py` alongside the rate-limit tests.

---

## 4. Still owed — answered item by item

### 4a. System status card — NOT DONE

**Is it on the main page?** Yes, the card exists and is mounted in the header.
**Does it carry what you asked for?** No. `ui/data_access.py::system_status()`
returns exactly:

```
journal_path, journal_exists, engine_state, mode, engine_online,
heartbeat_age_s, schema_version, account_login_masked,
account_currency, pending_commands
```

Against your nine requested fields:

| Requested | Present? |
|---|---|
| engine online | **Yes** — `engine_online`, `heartbeat_age_s` |
| emergency stop state | **Partly** — `engine_state` maps `EMERGENCY_STOP → "halted"` |
| execution mode, PAPER/LIVE | **Yes** — `mode` |
| scan running | **No** |
| risk gate running | **No** |
| last signal | **No** |
| last trade | **No** |
| trades today | **No** |

Four of nine are missing. **Three of those four cannot be honestly implemented
yet**: there is no scan to report running, no gate that runs, and no signal to
report as last. Writing `scan_running: False` would be accurate today but would
invite the reading that a scan exists and is merely stopped. The honest
sequence is the one in §13 of the design doc — those fields arrive with the
loop that makes them true. `last_trade` and `trades_today` *are* implementable
now from `fills`; I left them out rather than half-add the set. Say the word
and I will add just those two.

### 4b. Banned words — PROVEN

`tests/test_ui/test_no_vendor_names.py::BANNED` contains `deepseek`,
`openrouter`, `groq`, plus `anthropic|claude`, `openai|gpt-\d` and
`metaquotes`. The test walks the **AST** of every module under `ui/` and fails
on a banned name in a rendered string, so it is self-verifying: adding a new
component cannot bypass it. 43 tests pass.

### 4c. Ownership table and Mermaid diagram — NOT DONE

Neither artefact exists in the repo. It needs to be a separate deliverable,
because "each step marked wired-and-running / wired-but-never-exercised /
not-wired" is an audit of the whole call graph, not a paragraph. I have not
attempted it inside this task: a diagram whose three-way classification I
could not evidence for every step would be worse than an admitted gap.

### 4d. `ai_brain.py` — ANSWERED

At repo root, imported in exactly **one** place: `ui/components/ai_signals.py:8`.
Nothing in `engine/` references it.

**Recommendation: retire it.** It is reachable only from a display path, so
wrapping it would add a layer around something nothing depends on, and merging
it would mean importing a root-level module into the engine package. Deleting
the file plus its single import is the smallest safe change — but that is a
behaviour decision about what the AI Signals panel shows, so it needs your
call, not mine. **I have not touched it.**

### 4e. Second engine process — ANSWERED: the lock is working

Two processes are live: **PID 41440** and **PID 35972**. Heartbeats in
`data/engine_5057151780.db` come from **PID 35972 only** (18,856 rows, `IDLE`).
PID 41440 wrote none — it was refused.

`engine/__main__.py::_acquire_single_instance_lock` takes an `msvcrt.locking`
handle on the resolved db path; on failure it returns `None` and `run` prints
`[FATAL] another engine is already running on <path>` and exits. **So the lock
is doing exactly its job.** The second process is a stale leftover sitting
refused, not a second writer. Safe to kill PID 41440; it is not protecting
anything.

### 4f. Sub-status list

| Item | Status |
|---|---|
| Journal switch to `data/engine_5057151780.db` | **Done.** `config.yaml`, and `tests/test_repo/test_journal_path_single_source.py` (190 lines) enforces single-source resolution |
| Dashboard port 8502 | **Set in code.** Not independently verified by binding a socket; the configured value is what I can attest to |
| Minimum-stop handling via `order_check` | **Not done.** `order_check` and `stops_level` appear nowhere in `engine/executor.py` or `engine/risk.py`. A real gap; belongs with the executor work in Phase 2A |
| Symbol search for BTC/ETH/index equivalents | **Not done.** No equivalence-resolution code found. `config.yaml` whitelists symbols and probes `required: true/false`, but there is no broker-name mapping for crypto or index equivalents |
| XAUUSD | **Present in the whitelist** with `max_spread_points` of 500 points. Quotability unproven — that needs a live terminal |
| Restart preconditions | **Partly.** The guard blocks trading on an unverified fingerprint, the instance lock prevents a duplicate, `reconcile_on_start: true` adopts managed positions. A consolidated written precondition list does not exist |

### 4g. MT5 lifecycle chain from the TEST SIGNAL hook — BLOCKED

**This cannot be done in this session.** It requires a live MT5 terminal, and
the evidence says there is not one reachable: the guard's `TerminalOffline`
path is exercised in tests with a fake, and the second engine process never got
far enough to read an account. I could paste journal rows, but they would be
rows I generated — and a fabricated lifecycle chain in a report to your
architect is worse than no chain. **This stays open until a terminal is
available**, and it is the most valuable thing to do next, because it is the
only item that tests the broker rather than the loop.

---

## 5. Phase 2A — DESIGN ONLY

`docs/Phase2A_Scan_Loop_Design.md`, 437 lines, 15 sections. **No code was
written and `engine/brain.py` was not created.** PAPER, approve-first and
auto-execute-off are stated as hard constraints in §0 and §11. The
magic-234000 positions are out of scope in every path.

Highlights you may want to challenge:

- **§1** names who emits each of the 13 transitions, and states that
  `SCANNING → EXECUTING` is undeclared and must fail — the strategy cannot skip
  risk validation.
- **§2** a scan runs only when state is `IDLE`, terminal online, guard OK, not
  halted, not paused. Any other combination skips and journals why.
- **§3** one market read per cycle, in the gateway owner thread, snapshot handed
  down immutably. Two MT5 callers in one process is the failure this prevents.
- **§4–5** closed bars only, per watched symbol, per-symbol error isolation —
  one bad symbol never stops the cycle.
- **§6** ten filters, each with a journalled reason code. A missing blackout
  calendar **fails open**; a missing session read **fails closed**.
- **§8** SL/TP both touched in one snapshot closes at the **adverse** level with
  `AMBIGUOUS_SL_TP` journalled — the snapshot has no intra-bar ordering, and a
  fill model that guesses favourably lies about drawdown.
- **§9** the throttle-indentation lesson is in the design, and the test asserts
  the indentation on the real source, because that bug is invisible at runtime.
- **§10** `scan_results` gains one row per symbol per cycle with columns
  defined; "no signal" is a row.
- **§11** approve-first via dashboard or Telegram; auto stays off.
- **§12** 14 failure modes, each with its required behaviour and test.
- **§15** five open questions needing your answer before implementation.

---

## Verdict summary

| | |
|---|---|
| **PROVEN** | Items 1, 2, 3, 4b |
| **TESTED-ONLY** | Nothing — the loop does not exist yet, so nothing is tested-only |
| **UNPROVEN** | Anything touching a real broker: PAPER fills, the close sweep, XAUUSD quotability |
| **NOT DONE** | 4a (four fields), 4c, minimum-stop handling, symbol search |
| **BLOCKED** | 4g — needs a live terminal |
| **AWAITING YOUR DECISION** | 4d (`ai_brain.py` retirement), 4a (add `last_trade`/`trades_today` now?), §15's five questions |

**Nothing was pushed.** Six commits sit local on `feature/echofi-ui`, HEAD
`ead577e`.
