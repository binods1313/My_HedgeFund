# Stage 1 Report — Part A (A1, A2, A3)

Date: 2026-10-14. Mode: PAPER. `--live` not used. No trading agent wired in. Nothing in Part B started.

## One-page summary

| Item | Status | Evidence |
|---|---|---|
| **A1** Account-mismatch rows | **PROVEN** resolved | 23,586 old rows archived; 0 new rows since engine start |
| A1 second writer | **PROVEN** none | only `engine/account_guard.py` writes "Account mismatch" |
| A1 kind / detail / severity display | **TESTED** | 9 tests, `tests/test_ui/test_journal_render.py` |
| A1 collapse repeats | **TESTED + on real rows** | 50 real rows → 1 group |
| A1 600s throttle | **TESTED** | 46 guard tests pass |
| A1 journal path UI = engine | **TESTED** | 4 tests; live paths identical |
| A1 engine restart on current code | **PROVEN running** | 1 instance, PAPER, lock held, 0 mismatch rows |
| A2 analysis agents 01–10 | **PROVEN** from code | `docs/A2_Analysis_Agents.md`; no agent calls a model or needs a key (01–08) |
| A2 card reasons replace "Implemented but disabled" | **TESTED** | 12 tests, `test_research_desk_status.py` |
| A3 system status card, honest | **TESTED + PROVEN live** | 5 tests; live read shows real values |
| A3 false-zero bug | **FIXED** | trade query ran on a closed connection and was silently swallowed |
| A3 `ai_brain.py` retirement | **DECISION NEEDED** | file deleted (staged); breaks 2 test files + 5 tests |
| Full test suite | **NOT GREEN** | interrupted at collection (`ai_brain` import) |

**Bottom line:** A1 and A2 are done and proven on the evidence. A3's status card is done. **The full test suite is broken** by the `ai_brain.py` deletion, and that must be decided before anyone trusts a green number again.

## A1 — Account mismatch journal

**Problem.** The journal showed "Account mismatch" repeatedly as CRITICAL.

**Finding.** `data/engine_5057151780.db` held 23,586 mismatch rows, written about once per second with `phase: session_refresh` and reason "no account is logged in". They lack the `repeat` and `kind` keys that current code writes, so they came from engine code that predates the throttle (commit `b773324`). Only `engine/account_guard.py` writes this message, so there is no live second writer.

**Actions.**
- Archived the 23,586 rows to `backups/archive/account_mismatch_rows_20261014T044034Z.jsonl`. Journal left unchanged.
- Stopped child engine PID 41712 on your instruction; parent 4984 exited with it.
- Restarted engine on current code: PAPER, one instance (wrapper 25472 → launcher 1180 → engine 17472), lock held. No mismatch rows since start.
- Added `tests/test_ui/test_journal_render.py` (9 tests) and `tests/test_ui/test_journal_path.py` (4 tests).

**Corrections made during this work.**
- I first said A1 was resolved. It was only that the condition had not recurred; the throttle was not tested until I ran the guard tests (46 passed).
- Two of my test assertions were wrong (masking, and clock timezone). Fixed the tests, not the code.
- The renderer depends on newest-first input; that is what the real query returns. The test now feeds that order and documents why.

**Open:** PID 41440 was not running when checked, so it could not be stopped or confirmed.

## A2 — Analysis agents

Full table: `docs/A2_Analysis_Agents.md`.

- **Agents 01–08:** deterministic code over `context` inputs. No LLM SDK, HTTP client, or key import in any `engine/research/agent_*.py`. No engine module imports `engine.research`. Real blocker: **the missing scan loop**.
- **Agent 06:** needs `FINANCIAL_DATASETS_API_KEY`, **not set**. Dexter is **not installed** (`E:\tools\dexter` absent).
- **Agents 09 / 10:** deterministic, no model call (invariant I4). Show "No decision yet".
- **Shadow cap of 20:** is `dexter.daily_cap: 20` (Dexter calls per day), **not** a shadow-cycle cap. The shadow loop has no counter.
- **`shadow.enabled: false` in `config_ext.yaml` is never read.** The UI toggle is session state. Config flag is inert.
- **Agent 04** docstring says ForexFactory; the code only counts impact labels in the list it is given.

Card text now reads the real reason per agent (`_disabled_reason`). The four-word §A5 status set is unchanged.

## A3 — Housekeeping

**System status card.** Built from journal facts, read-only.
- Live values: engine IDLE, PAPER, heartbeat online; last trade "No trade yet"; trades today 0 (a real count); scan loop "Not built yet"; signals "No signals yet"; account ****1780 USD.
- Added a PAPER disclaimer: "PAPER fills are simulated. They do not prove MT5 execution, fills or broker stop rules."
- Removed an unreachable branch in the heartbeat line.

**False-zero bug, fixed.** The trade query ran after its connection had closed. It raised `ProgrammingError`, which the code swallowed, so the trade count never worked. I reproduced this. Fix: run the trade reads inside the open connection. `trades_today` is now `None` ("unknown") when the journal cannot be read, and a real `0` only after the table is counted.

**`ai_brain.py`.**
- Checked the AI Signals tab: it uses `engine.dryrun`, `engine.sizing`, `data_engine`, `metrics`, `execution`. None imports `ai_brain`. The tab does not depend on it.
- The file is **deleted** in the working tree; the deletion is **staged but not committed** (last committed in `c56aae7`, 2 Oct 2026, 458 lines).
- It is still imported by `tests/test_llm_validator.py`, `tests/test_metrics.py`, and `scripts/mutation_check.py`. `tests/test_dependencies.py` also flags it as an undeclared third-party dependency, which is wrong: it is a local module.

**Test counts (replaces stale figures).**
- Full suite: **not runnable** (collection error).
- Excluding `test_llm_validator.py`: **1,472 passed, 6 skipped, 5 failed**. All 5 failures come from the missing `ai_brain`.
- `tests/test_ui`: **326 passed**.
- Previous "1112 passed, 0 failed" is retained as history only in `docs/PROGRESS.md` and is no longer true.

## Decision needed from you

**Restore `ai_brain.py`, or retire it properly.** Both are reasonable; they differ in risk.

1. **Restore** (`git restore --staged ai_brain.py; git restore ai_brain.py`). Brings the suite back to collecting with no test edits. Recommended if the LLM validator is still wanted, because `test_llm_validator.py` covers a safety gate.
2. **Retire.** Requires rewriting `test_llm_validator.py` and `test_metrics.py` against their new home, fixing `test_dependencies.py`'s classification of local modules, and updating `scripts/mutation_check.py`. I have not made these changes because they touch safety tests.

I have not made either change. Tell me which.

## Still open in Part A

- Minimum-stop handling via `order_check` (B8): belongs to Stage 3, not done.
- Symbol search for BTC/ETH/index equivalents (read-only): not started.
- XAUUSD quotability check: not started.
- Written restart checklist and ownership table: not started.
- Live lifecycle chain with pasted journal rows (TEST SIGNAL): not started; needs Stage 3.

These were listed in your Part A but are either Stage 3 work or need a separate read-only pass. I have not claimed them.

## Not done, per instruction

- Part B (Phase 2A scan loop): **not started**. Stage 1 is gated on your review, and Stage 2 waits for your "go".
- No change to trading logic, no order path, no `--live`.
- `engine/brain.py` not created.

## Files changed in this stage

- `ui/components/research_desk.py` — reason line per analysis card
- `ui/data_access.py` — status: `trades_today` None-when-unknown; trade reads on open connection; scan-loop and signals fields
- `ui/components/header.py` — status card: last trade, trades today, scan loop, signals, PAPER disclaimer; unreachable branch removed
- `tests/test_ui/test_journal_render.py` (new, 9)
- `tests/test_ui/test_journal_path.py` (new, 4)
- `tests/test_ui/test_status_card_honesty.py` (new, 5)
- `tests/test_ui/test_research_desk_status.py` — 5 new tests for reasons
- `docs/A2_Analysis_Agents.md` (new)
- `docs/PROGRESS.md` — stale counts replaced with current, marked superseded
- `backups/archive/account_mismatch_rows_20261014T044034Z.jsonl` (new archive)

Staged, not by me in this stage: deletion of `ai_brain.py` (already staged before this work).
