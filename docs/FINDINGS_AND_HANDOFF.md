# HedgeFund_Binod_v2 — Consolidated Findings & Handoff

**Date:** 2026-10-13
**Repo:** `E:\HedgeFund_Binod_v2`  ·  **Authoritative account:** `5057151780` (MetaQuotes-Demo, USD)
**Audience:** main coder + orchestrator. This file is self-contained — you should not need to read the earlier reports.
**Current state:** Step A (account guard) complete and tested. **Nothing restarted. Dashboard (PID 15944, port 8501) untouched.** `mode: PAPER`, approve-first, DRY_RUN-equivalent.

---

## 0. TL;DR for the orchestrator

1. Three real safety defects were found and fixed. The most serious was invisible in normal operation.
2. A guard now prevents the engine and the UI from writing against the wrong account. It is tested and green (**1419 passed, 6 skipped**).
3. A fresh journal exists for account 5057151780 and the old one is archived read-only. **`config.yaml` still points at the old journal** — that switch is the single decision blocking the restart.
4. The dashboard is running on a **different port than `run.bat` uses** (8501 vs 8502). Restarting via the script changes the URL. Needs a decision.
5. A broker behaviour was measured that contradicts the spec we were relying on: **`stops_level = 0` is not trustworthy.** Do not trust the tighter-stop arithmetic until this is resolved.

---

## 1. Defect 1 — `has_stop()` could raise instead of refusing

**File:** `engine/risk.py` · **Severity: high, fail-closed path failing unsafely**

`OrderIntent.has_stop` was `self.stop_loss > 0 and self.stop_distance > 0`, and `stop_distance` was `abs(self.entry_price - self.stop_loss)`. The UI order form passes whatever the operator typed, and MT5 reports a missing stop as `0.0`. So:

| Input | Old behaviour | Why it matters |
|---|---|---|
| `None` | `TypeError` | a crash on the fail-closed path, which is the one path that must never throw |
| `""`, `"abc"` | `TypeError` | same |
| `float("nan")` | silently `False` | the quiet one — `float("nan")` *succeeds*, and every comparison against NaN is False, so it returned False for the wrong reason rather than being recognised as missing |
| `0.0`, `-1`, `"0"` | `False` | correct by accident |

**Fix:** added `_as_float_or_none()` (handles `None`, empty, whitespace, non-numeric text, and NaN explicitly) and rebuilt `has_stop` on it. It now returns `False` for every unusable value and never raises. Both the engine path (`engine/risk.py`) and the UI path (`ui/data_access.py`, `SL_REQUIRED` before a journal or executor is opened) are covered.

**Test:** `tests/test_engine/test_risk.py::TestHasStopNeverRaises` — 11 invalid values assert `False` with no raise, plus valid stops, stop-at-entry, NaN entry, and the gate decision rather than just the flag.

---

## 2. Defect 2 — a zero stop fabricated a risk distance

**This is the one to read twice.** It was found while writing the tests for Defect 1; it is not a test-only artefact.

`stop_distance` was `abs(entry_price - stop_loss)`. With `sl = 0.0` and `entry = 1.1`, that is `abs(1.1 - 0.0)` = **`1.1`** — a risk distance measured to a price of zero. `has_stop` was still False, so the order was blocked, **but the number was in the object**, and anything that consumed `stop_distance` for sizing was sizing against a stop that did not exist.

**Why this was live, not theoretical:** both positions that the reconcile dropped on 2026-10-12 — USDCAD `10726505976` and GBPUSD `10745483547`, both at our own magic `100100` — carried `sl = 0.0`. From the pre-restart backup, `sl` and `tp` were `0.0`/`0.0` on both, with unrealised P&L of −2.18 and −2.93.

**Fix:** `_safe_stop_distance()` now returns `0.0` when the stop is missing, non-positive or unparsable. The distance is unknown, and `0.0` is what makes the order block. Test: `test_a_zero_stop_does_not_report_a_distance`.

**General principle worth extracting:** a guard on a *number* is only as sound as the number it guards. Fail-closed flags and fail-closed arithmetic are two separate properties, and the second is invisible until something computes the value rather than merely checking it.

---

## 3. Defect 3 — hardcoded currency on the disconnected path

**File:** `data_engine.py` · **Severity: medium, wrong data shown to operator**

`get_account_telemetry()` returned `"currency": "USD"` when `account_info()` was `None`. The HUD therefore showed a `$` sign on an account nobody had read, and would have shown the same on a EUR account.

**Fix:** returns `None`. `formatting.py`'s symbol table is left alone — it is a currency→symbol **lookup**, not a default, and now only ever receives `account_info().currency` or `None`.

**Test:** `tests/test_ui/test_currency_rendering.py` (11 tests) — a USD account never renders `€`, a EUR account never renders `$`, an unlisted currency borrows no symbol, and `None` does not become USD.

---

## 4. Defect 4 (latent, now fixed) — currency could differ on the same login

`OrderIntent.has_stop` aside, the deeper issue was that nothing compared the **currency** between the terminal and the journal. An account can keep the same login across a server or currency change; that combination would have passed a login-only check. The guard below now requires login **and** currency.

---

## 5. The account fingerprint guard

**New file:** `engine/account_guard.py` · **Wired into:** `engine/__main__.py`, `ui/data_access.py`

**Why it exists.** On 2026-10-12 the terminal was observed on account `113363533` and roughly 20 minutes later on `5057151780` — the same `terminal64` PID, switched by something outside this task. Nothing in MT5 announces that. Without a guard the engine would keep writing account B's equity into account A's journal, adopt account B's positions into account A's table, and measure a daily loss limit against a balance it did not start the day with.

### 5.1 What it stores

`engine/journal.py` migration **v1 → v2** creates one table and adds two columns:

```sql
CREATE TABLE IF NOT EXISTS journal_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),   -- exactly one row, ever
    login INTEGER, currency TEXT, server TEXT, magic INTEGER,
    recorded_at TEXT NOT NULL);

ALTER TABLE engine_state ADD COLUMN account_login INTEGER;
ALTER TABLE engine_state ADD COLUMN account_currency TEXT;
```

`journal_account` is authoritative; the `engine_state` columns are a mirror so the account strip and the state row read together. `journal.py` also has `_ensure_column`, a tolerant patcher, so the columns can be added to an existing DB without a destructive rebuild. **The live `data/engine.db` was left at v1.**

### 5.2 How it decides

`Fingerprint(login, currency, server).matches(other)` treats a **missing field on either side as a mismatch, never a wildcard** — including an unreported currency, which is the case that would otherwise look verified because the login is right. Currency is read only from `account_info().currency`; there is no default.

One deliberate asymmetry: `matches_configured()` compares only login and server against the `.env` expectation, because a currency is not configurable — the environment holds none. The **journal** comparison uses the full `matches()`, which does check currency.

### 5.3 Where it is called

| Phase | Method | Note |
|---|---|---|
| startup | `build_engine()` | after gateway connect, before any reader |
| reconnect / MT5 reinit | `_verify_account_transitions()` | on the `connected` False→True edge |
| session refresh | `run_once()` | every loop iteration |

**Why the edge, and not inside the gateway:** `engine/gateway.py` runs every MT5 call on a **single owner thread** through a request queue (`ensure_connected` → `_owner_ensure_connected` → `_ensure_connected`), and MT5's Python API is not thread-safe. Calling into that thread from the engine would break its single-thread contract — that is exactly the class of bug the queue was built to prevent. An engine-level edge check is the correct place.

### 5.4 What it blocks, and what it keeps doing

**Blocked while mismatched:** reconcile, telemetry writes, order submission, order modification.

**Kept running: heartbeats.** This is deliberate. A heartbeat that stops is indistinguishable from a crash, so the operator cannot tell a mismatch from a dead process. With heartbeats continuing the dashboard shows **`Account mismatch`** rather than `offline`. Tested directly: after a switch, equity stays frozen, positions stay frozen, heartbeats keep rising.

**Reconcile is the load-bearing gate.** It is the one routine that deletes journal rows. After an account switch *every* journal ticket looks absent at the broker, so an unguarded reconcile would delete the whole `positions` table in one pass. The public `reconcile_positions()` now refuses first; the lock-free body is `_reconcile_positions_unlocked()`.

### 5.5 The UI half

`ui/data_access.py::_journal_account_matches()` reads the journal fingerprint and the live account and refuses with `ACCOUNT_MISMATCH` on any mismatch, unreadable state, or missing fingerprint. Both `submit_order()` and `close_position()` call it **before any broker call**. Without it, a dashboard left open across an account switch would submit against the wrong account and the journal would record it as this system's own.

---

## 6. Foreign positions

Two positions are currently open on the account — **EURUSD** and **USDJPY** — both carrying `magic = 234000` and comment `AI Hedge Fund Bot`. That magic and comment belong to the **retired SOURCE system**, not to this one (`config.yaml` magic is `100100`).

Rules now in force:
- **Never** manage, modify, close, trail or reconcile them. (The operator is closing them manually.)
- Journal **exactly one** `reconcile`/`WARNING` event, `"foreign positions present"`, carrying count, tickets and symbols — emitted at startup and at each reconcile.
- **Count them** in total exposure, margin and max-open-trades. `AccountState.open_trade_count` is built from the broker position list without a magic filter, so foreign positions consume slots.
- **Exclude them** from the journal's `positions` table and from adoption.
- `get_active_positions()` now emits `magic` and `managed` columns; `managed=False` for any non-`100100` magic, including `None`, `""` and `0`. This is what the UI reads to hide its close/edit controls.

**Proof a foreign position reduces room:** `test_a_foreign_position_alone_can_block_an_order` — four of ours → approved; add one foreign → refused. The only thing that changed is a position this engine does not own.

---

## 7. The account-switch incident (context for why the guard was written)

Sequence observed on 2026-10-12, all read-only, no orders placed:

1. Terminal on `113363533`. Two positions at **our own magic 100100** (USDCAD, GBPUSD) were present.
2. ~20 min later the same `terminal64` process was on `5057151780`.
3. `history_orders_get` / `history_deals_get` for both old tickets returned **0 orders and 0 deals** — not evidence of how they closed, but evidence of an account switch. Their close time, method and price are recorded **unverified**; no SSOT claim is made.
4. Two positions at **magic 234000** were found open on the new account and attributed to the retired SOURCE system (its magic number and its exact comment string).

No old-account rows were imported, and no mixing was done.

---

## 8. Journal archive and fresh journal

| Item | Value |
|---|---|
| Archive | `backups/engine.db.acct-113363533.2026-10-12.archive` — 14,127,104 bytes, `IsReadOnly=True`, plus archived `-wal`/`-shm` |
| Created by | **copy, not move** — the dashboard holds the live file open |
| Fresh journal | `data/engine_5057151780.db`, schema **v2**, stamped `5057151780 / USD / MetaQuotes-Demo / magic 100100`, `engine_state = OFF/PAPER` with the account columns set |
| Rows imported | **none** |
| Accounts mixed | **none** |
| `config.yaml` `db:` | **STILL `data/engine.db`** — deliberately unchanged, awaiting approval |

---

## 9. Schema version "5" vs "v1 → v2" — reconciled

These were never in conflict; they measured different things.

- `docs/Phase0_Audit_Report.md` §2.4 counted the **tables** in the live DB, listed twelve, and labelled the list "schema_version 5". A mislabel. The live `schema_version` row was never 5.
- The real constant is `SCHEMA_VERSION` in `engine/journal.py`: `1`, now `2`.

The fresh journal at v2 contains all ten tables the UI and engine need (`heartbeats`, `equity_snapshots`, `quotes`, `signals`, `trades`, `positions`, `risk_decisions`, `llm_calls`, `events`, `commands`), plus `engine_state`, `schema_version`, `journal_account` — all empty except the state and fingerprint rows.

---

## 10. `--check-config` now verifies the account

```
[config] mode=PAPER
[config] db=E:\HedgeFund_Binod_v2\data\engine.db
[config] heartbeat=5s offline_after=15s
[config] magic=100100
[keys]   …
[account]  terminal  ****1780 | USD | MetaQuotes-Demo
[account]  journal   ****1780 | USD | MetaQuotes-Demo
[account]  MATCH
[config] OK
```

It runs **before** the `--check-config` early return, so the account report always prints. On mismatch it prints `MISMATCH -- <reason>` and `trading, reconcile, telemetry and orders are blocked`. `_mask_login()` prints the last four digits at most — a full login is enough to log into a terminal, so it is never printed. No password is ever printed.

---

## 11. Broker findings — symbol remap and the `stops_level` trap

Read-only against 5057151780, 12,306 instruments offered.

### 11.1 Mapping (proposal, nothing committed)

**10 of 16 whitelist symbols map.** The three `#`-suffixed pairs map to plain names (the `#` is dropped): `EURGBP#`→`EURGBP`, `EURJPY#`→`EURJPY`, `GBPJPY#`→`GBPJPY`. All ten: `contract=100000`, `vol_step=0.01`, `vol_min=0.01`.

**Not offered at all:** `BTCUSD#`, `ETHUSD#`, `US500Cash#`, `US30Cash#`, `US100Cash#`, `UK100Cash#`. `XAUUSD` **is** offered (contract `100`, `Metals\XAUUSD`) but is not whitelisted.

### 11.2 `stops_level = 0` is not a trustworthy minimum

`symbol_info` reports `trade_stops_level = 0` on every mapped symbol, which reads as "no minimum stop distance". Verified with `order_check` (which does **not** send an order):

| Symbol | SL submitted | retcode | Meaning |
|---|---|---|---|
| EURUSD | 1 point | `0` Done | a 1-point stop is genuinely accepted |
| USDJPY | 1 point | `10016` Invalid stops | **rejected** |
| USDJPY | 5 points | `0` Done | accepted |
| USDJPY | ≥10 points | `0` Done | accepted |

**Conclusion:** the broker enforces a real minimum between 1 and 5 points on USDJPY that `symbol_info` does not report. `_min_stop_distance` must not read `0` as "no minimum" and then rely on tighter stops. Recommended: when sizing a stop, `order_check` the candidate and step outward on `10016`. **Open item — not relied upon here.**

Also: `trade_freeze_level` is `0` on both symbols, and `filling_mode` on this account is **FOK only**. The FOK→IOC→RETURN fallback chain from SOURCE's `execution.py` will normally take the first branch, meaning the IOC and RETURN paths are untested on this broker.

### 11.3 Stale symbol names still in code

`engine/__main__.py::_broker_server_epoch` samples `("EURUSD", "BTCUSD#", "US500Cash#", "GBPUSD")` to validate the terminal clock. Two of those are now known-unavailable. It degrades gracefully (it skips `None` and time-less ticks), so this is cleanup, not a bug — but the list should be updated to the mapped names.

---

## 12. What the engine does not do

Worth stating plainly because it bounds the blast radius of a restart:

`Engine.run_once()` performs **only** `commands.poll_once()`, account verification, `state.heartbeat()`, and `_write_telemetry()`. There is **no strategy evaluation, no risk evaluation and no order call** in the loop. The only way an order is created is an explicit command routed through the risk gate. **Starting the engine cannot place an order on its own** — verified in Gate 1 of this task and unchanged since.

---

## 13. Test status (fresh, this pass)

| Suite | Result |
|---|---|
| `pytest tests/test_engine -q` | **643 passed, 6 skipped** |
| `pytest tests/test_ui -q` | **229 passed** |
| `pytest tests/test_ui/test_currency_rendering.py` | **11 passed** |
| `pytest -q` (full) | **1419 passed, 6 skipped**, exit 0 |

Mocked MT5, mocked providers. No broker, no AI, no Telegram. `tests/test_engine/conftest.py` has an autouse fixture installing `MT5_LOGIN=999999999` matching `FakeMT5`'s account, so no engine test is blocked by the guard for an unrelated reason.

---

## 14. Files changed

| File | Change |
|---|---|
| `engine/account_guard.py` | **new** — `Fingerprint`, `AccountGuard`, `classify_foreign_positions`, `AccountMismatch` |
| `engine/__main__.py` | guard wiring (startup / transition / session refresh), `_reconcile()`, telemetry + reconcile gating, foreign-position reporting, `run_once` heartbeat-after-verify, `_mask_login`, `_report_account_check` |
| `engine/risk.py` | `_as_float_or_none`, `_safe_stop_distance`; `has_stop` / `stop_distance` no longer raise, no fabricated distance |
| `engine/journal.py` | `SCHEMA_VERSION` 1→2, `MIGRATION_2`, `set_state` account columns, `set_account_fingerprint` / `get_account_fingerprint` |
| `data_engine.py` | disconnected path `currency: None` |
| `ui/data_access.py` | `_journal_account_matches`, `_is_our_magic`, `POSITION_COLUMNS` + `magic`/`managed`, gating in `submit_order` / `close_position` |
| `tests/test_engine/test_account_guard.py` | **new** |
| `tests/test_engine/test_account_guard_e2e.py` | **new** — mid-run switch, wrong-journal startup, recovery, foreign-position room |
| `tests/test_engine/test_risk.py` | `TestHasStopNeverRaises` |
| `tests/test_ui/test_currency_rendering.py` | **new** |
| `tests/test_ui/test_data_access.py` | `TestAccountMismatch`, `_stub_journal_account` |
| `tests/test_engine/conftest.py` | **new** — autouse matching credential |
| `data/engine_5057151780.db` | **new** — fresh v2 journal, dormant |
| `backups/engine.db.acct-113363533.2026-10-12.archive` | **new** — read-only |

Not changed: `config.yaml`, `.env`, any credentials, the live `data/engine.db`, the dashboard, any broker position, any risk limit.

---

## 15. Open items and the two blocking decisions

### 15.1 Decisions needed

1. **Journal switch** — approve `config.yaml` `db: data/engine.db` → `data/engine_5057151780.db`. This is what makes every subsequent restart observable in the right place. **Everything downstream waits on this.**
2. **Dashboard port** — the running instance is on **8501 (PID 15944)**, but `run.bat` specifies **8502**. Restarting via the script changes the URL you use. Either approve the move to 8502, or the dashboard must be relaunched on 8501 deliberately.

### 15.2 Open technical items

| # | Item | Note |
|---|---|---|
| 1 | `stops_level` minimum-stop reliability | §11.2 — do not trust tighter stops until resolved |
| 2 | `_broker_server_epoch` stale symbol names | §11.3 |
| 3 | IOC/RETURN filling-mode paths untested on this broker | FOK-only account |
| 4 | UI work: "Not managed by this system" view, account tooltip `****1780 \| USD`, `ACCOUNT_MISMATCH` display | data layer done, rendering not |
| 5 | Phase 2: `engine/brain.py` proposer, `ai_decisions` table + migration, settings via commands table, DeepSeek in `engine/keys.py` + `.env.example`, six SOURCE ports, auto-execute addendum | **not started**, deliberately behind this gate |
| 6 | Navbar prototype on a branch + screenshots | **not started** |

### 15.3 Restart procedure (only after both decisions)

```powershell
# 1. Full suite green
& E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider

# 2. Account verification (against the NEW journal once config.yaml points to it)
& E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -m engine --check-config
# expect: [account]  MATCH

# 3. Engine, PAPER only, never --live
& E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -m engine

# 4. Confirm in the journal
#    - heartbeats advancing, connected=1
#    - equity_snapshots gaining rows
#    - quotes row ACCOUNT_EQUITY rising
#    - NO new trades, NO new signals, NO llm_calls, NO risk_decisions
```

If anything fails — the guard trips, an account mismatch, an unexpected order, a schema error — **stop the engine, report, and wait.** Do not retry silently.

---

## 16. Verify it yourself

Read-only, safe to run in any order:

```powershell
# account the terminal is actually on
& E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -m engine --check-config

# fresh journal fingerprint and tables
python -c "import sqlite3; c=sqlite3.connect(r'file:E:\HedgeFund_Binod_v2\data\engine_5057151780.db?mode=ro',uri=True); print(c.execute('SELECT login,currency,server,magic FROM journal_account').fetchall()); print(sorted(r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")))"

# the zero-stop distance bug, before and after
python -c "from engine.risk import OrderIntent; i=OrderIntent(symbol='EURUSD',side='BUY',volume=0.10,entry_price=1.1,stop_loss=0.0,tick_value=1.0,tick_size=0.00001); print('distance',i.stop_distance,'has_stop',i.has_stop)"
# -> distance 0.0, has_stop False   (old: distance 1.1)
```

---

## 17. Related documents

Superseded in detail by this file, kept for history: `docs/Phase0_Audit_Report.md`, `docs/Phase1_Account_Guard_And_Symbols.md`, `docs/StepA_Account_Guard_Wiring.md`. If you read only one document, read this one.
