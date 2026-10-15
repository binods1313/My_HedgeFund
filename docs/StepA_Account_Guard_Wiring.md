# Step A — Account Fingerprint Guard Wiring (completed)

**Date:** 2026-10-13
**Status:** Step A delivered. **No restart performed.** The dashboard (PID 15944, port 8501) was left running.
**Mode:** `PAPER`. Approve-first. No orders, no provider calls, no Telegram, no credential output.

---

## 1. Schema reconciliation (asked, answered)

**The two numbers were not in conflict — they describe different things.**

| Claim | Where | What it actually measured |
|---|---|---|
| "schema version 5" | `docs/Phase0_Audit_Report.md` §2.4 | The **count of tables** in the live DB, listed as a set. §2.4 enumerates twelve tables and calls that list "schema_version 5" — a mislabel. The live DB's `schema_version` row was never 5. |
| "v1 → v2" | This report, §2.1 | The real `SCHEMA_VERSION` constant in `engine/journal.py`, read as `SCHEMA_VERSION = 1`, now `2`. |

**Confirmed for the fresh journal `data/engine_5057151780.db`:** every table the UI and engine need is present.

| Table | Present | Rows |
|---|---|---|
| `heartbeats` | PRESENT | 0 |
| `equity_snapshots` | PRESENT | 0 |
| `quotes` | PRESENT | 0 |
| `signals` | PRESENT | 0 |
| `trades` | PRESENT | 0 |
| `positions` | PRESENT | 0 |
| `risk_decisions` | PRESENT | 0 |
| `llm_calls` | PRESENT | 0 |
| `events` | PRESENT | 0 |
| `commands` | PRESENT | 0 |
| `engine_state` | PRESENT | 1 (`OFF/PAPER`, with `account_login=5057151780`, `account_currency=USD`) |
| `schema_version` | PRESENT | 1 row |
| `journal_account` | PRESENT | 1 row — `login=5057151780, currency=USD, server=MetaQuotes-Demo, magic=100100` |

`engine/journal.py` also defines `_ensure_column`, a tolerant schema patcher, so the new columns can be added to an existing DB without a destructive rebuild.

## 2. Dashboard port discrepancy (asked, answered)

| Report | Value | Resolution |
|---|---|---|
| My report | port **8501** | The dashboard really is on 8501 — confirmed this pass: local port 8501, owning **PID 15944**. |
| `run.bat` | `--server.port 8502` | `run.bat` would use 8502. The running dashboard was **not** started from `run.bat`; it was started some other way (an operator's shell, or an earlier launch) and is now the authoritative instance. |

**I did not stop it.** Per the instruction to confirm the PID before stopping, and because the PID is correct and the process is the dashboard, no `Stop-Process` was issued. The port discrepancy is recorded, not "fixed" — changing how the dashboard starts is a Phase 3 change.

**Consequence for the restart procedure:** restarting the dashboard must go through `run.bat` (which lands on 8502), not by killing PID 15944 and hoping the same port returns. That changes the URL the operator uses. Flagged before any restart, per the policy.

---

## 3. Step A implementation

### 3.a Guard call sites

| Requirement | Where | Implementation |
|---|---|---|
| startup | `engine/__main__.py` `build_engine()` | `guard.check(..., phase="startup")` after the gateway connects but before any reader. On mismatch, `startup_verified=False`. |
| every reconnect | `engine/__main__.py` `_verify_account_transitions()` | On a `connected` False→True edge, verifies with `phase="reconnect"`. A reconnect is indistinguishable from a reinitialisation from the engine's side, so both are treated as reconnect. |
| MT5 re-initialisation | same method | Same edge. The gateway owns the MT5 thread, so an engine-level edge check is the only correct place; calling into the owner thread would break its single-thread contract. |
| session refresh | `run_once()` | Every loop iteration, `phase="session_refresh"`, so a swap with no disconnect at all is caught. |
| reconcile gated on `may_reconcile` | `reconcile_positions()` | The lock-free body is now `_reconcile_positions_unlocked()`; the public method refuses with `"account mismatch"` first. |
| telemetry gated on `may_write_telemetry` | `_write_telemetry()` | Returns before reading the account. |
| heartbeats continue | `run_once()` | Heartbeat is issued **after** verification and regardless of outcome, so the dashboard shows `Account mismatch` rather than `offline`. |

### 3.c UI order path

- New `ui/data_access.py::_journal_account_matches()` — reads the journal's `journal_account` fingerprint and compares it against the live account's login and currency. Refuses with `ACCOUNT_MISMATCH` on any mismatch, any unreadable state, or any missing fingerprint.
- `submit_order()` and `close_position()` both call it **before any broker call**.
- `get_active_positions()` now emits `magic` and `managed` columns. `managed` is `False` for any position whose magic is not ours — the retired system's `234000` included, and `None`, `""` and `0`, which are treated as not-ours rather than as ours.
- `_is_our_magic()` never raises; an unparsable magic or an unloadable config returns `False`.

### 3.d Foreign positions

- `engine/__main__.py::_warn_foreign_positions(journal, positions, magic=)` — module-level, called at startup and at every reconcile. Writes exactly one `reconcile`/`WARNING` event, `"foreign positions present"`, carrying the count, tickets and symbols.
- Foreign positions are **never** written to `positions`. `reconcile_positions` still only adopts positions whose magic matches.
- Exposure, margin and max-open-trade counting: `AccountState.open_trade_count` is built from the broker's position list without a magic filter, so foreign positions consume slots.

### 3.e `--check-config`

```text
[account]  terminal  ****1780 | USD | MetaQuotes-Demo
[account]  journal   ****1780 | USD | MetaQuotes-Demo
[account]  MATCH
[config] OK
```

The check is placed **before** the `--check-config` early return, so the account report is always printed. On a mismatch it prints `MISMATCH -- <reason>` and `trading, reconcile, telemetry and orders are blocked`. `_mask_login()` prints the last four digits at most. No password or full login is ever printed.

One design note recorded: `Fingerprint.matches_configured()` compares only login and server against the `.env` expectation, because a currency is not configurable — the environment holds no currency. The journal fingerprint comparison still uses the full `matches()`, which does check currency.

### 3.g Currency

- `data_engine.py` disconnected path: `"currency": "USD"` → `"currency": None`.
- New `tests/test_ui/test_currency_rendering.py` (11 tests), asserting a USD account never renders `€`, a EUR account never renders `$`, an unlisted currency borrows no symbol, `None` does not become USD, each currency maps to its own symbol, and the disconnected path reports `None`.

---

## 4. Step A test results

| Suite | Result |
|---|---|
| `pytest tests/test_engine -q` | **643 passed, 6 skipped**, exit 0 |
| `pytest tests/test_ui -q` | **229 passed**, exit 0 |
| `pytest tests/test_ui/test_currency_rendering.py` | **11 passed** |
| `pytest -q` (full suite) | **1419 passed, 6 skipped**, exit 0 |

All via `E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -m pytest`, `-p no:cacheprovider`, external `--basetemp`. Mocked MT5, mocked providers, no broker, no AI, no Telegram.

### Guard coverage mapped to the requirements

| Requirement | Test |
|---|---|
| startup mismatch | `test_a_mismatch_blocks_startup_and_journals_critical`, `test_a_mismatch_never_stamps_the_new_account`, `test_an_unreadable_account_blocks_startup` |
| reconcile protection | `test_reconcile_cannot_touch_the_journal_after_a_switch` |
| runtime account change | `test_a_switch_writes_nothing_and_deletes_nothing` (3 iterations: equity frozen at 1, positions frozen at 1, heartbeats rising), `test_a_mid_session_switch_blocks_every_write` |
| reconnect / reinit / refresh | `test_each_phase_is_named_in_the_journal` (all three phases journalled) |
| currency EUR | `test_a_matching_currency_is_accepted[EUR-account]` |
| currency USD | `test_a_matching_currency_is_accepted[USD-account]` |
| foreign positions reduce room | `test_a_foreign_position_consumes_an_open_trade_slot`, `test_a_foreign_position_alone_can_block_an_order` (4 of ours → approved; +1 foreign → refused) |
| foreign positions not adopted | `test_the_guard_does_not_adopt_foreign_positions` |
| UI mismatch | `TestAccountMismatch` (4 tests: submit blocked, close blocked, foreign magic has no controls, positions carry `managed`) |
| non-vacuity | `test_the_guard_actually_raises` (positive control) |

---

## 5. Broker specification findings for the symbol remap

Read-only against account 5057151780, 12,306 instruments.

**10 of 16 whitelist symbols map.** All three `#`-suffixed pairs map to plain names. `contract=100000`, `vol_step=0.01`, `vol_min=0.01` for all ten. `XAUUSD` is offered but not whitelisted.

`stops_level` from `symbol_info` is **0 on every mapped symbol**, which is a claim that no minimum stop distance exists. Verified directly with `order_check` (which does not send an order):

| Symbol | SL | `order_check` retcode | Meaning |
|---|---|---|---|
| EURUSD | 1 pt | `0` "Done" | a 1-point stop is genuinely accepted |
| EURUSD | 10 pt | `0` "Done" | accepted |
| USDJPY | 1 pt | `10016` "Invalid stops" | **rejected** |
| USDJPY | 5 pt | `0` "Done" | accepted |
| USDJPY | ≥10 pt | `0` "Done" | accepted |

**Conclusion: `stops_level=0` is not reliable as a minimum-stop source.** USDJPY enforces a real minimum between 1 and 5 points that `symbol_info` does not report. `_min_stop_distance` must not treat 0 as "no minimum" without a broker check. Recommended: when sizing a stop, run `order_check` on the candidate and, on `10016`, step outward until it passes — or ask the broker for the true value. **This is an open item and the tighter-stop arithmetic is not relied upon here.**

Also noted: `trade_freeze_level` is `0` on both, and `filling_mode` on this account is `FOK` only — so the FOK→IOC→RETURN fallback in SOURCE's `execution.py` will normally take the first branch, and the IOC/RETURN paths are untested on this broker.

---

## 6. Remaining blockers

| # | Item | Status |
|---|---|---|
| 1 | Journal switch: `config.yaml` `db:` → `data/engine_5057151780.db` | **NOT DONE** — awaits approval |
| 2 | Restart the engine | **NOT DONE** — awaits items 3–7 |
| 3 | Restart the dashboard via `run.bat` (which uses port **8502**, not the current 8501) | **NOT DONE** — the port change needs your explicit sign-off |
| 4 | Engine-side UI magic filter (`ui/data_access.py`) is done; the journal-switch is not | — |
| 5 | `stops_level` minimum-stop reliability | **OPEN** — see §5 |
| 6 | Phase 2 (`brain.py`, `ai_decisions`, settings, DeepSeek provider, six SOURCE ports, auto-execute) | **NOT STARTED** — by design, behind this gate |
| 7 | Navbar prototype on a branch | **NOT STARTED** |

### Your restart conditions, against the current state

| # | Condition | State |
|---|---|---|
| 1 | Port 8000 confirmed closed | **confirmed** — nothing listening |
| 2 | Account guard merged | **code complete and tested** |
| 3 | Account guard tests pass | **yes** — 643 engine, 229 UI |
| 4 | Fresh journal created | **yes** — `data/engine_5057151780.db` |

*(Continuing the table in the working notes: 5 symbol mapping, 6 logged-in account verified, 7 explicit operator approval.)*

That is as far as the honest list goes without you approving the journal switch, which is the step that makes every subsequent restart observable in the right place.
