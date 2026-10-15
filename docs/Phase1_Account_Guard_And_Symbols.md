# Phase 1 — Account Fingerprint Guard, Has-Stop Fix, Symbol Mapping, Journal Switch

**Date:** 2026-10-12
**Scope:** Priority 1 safety fix, risk-gate fix, symbol remap proposal, journal archive and switch procedure.
**Mode:** `PAPER`. Approve-first. No orders, no provider calls, no Telegram, no credential output.
**Nothing restarted.** The dashboard (PID 15944, port 8501) was left running throughout.

---

## 1. Verified state at the start of this pass

| Check | Result |
|---|---|
| Port 8000 (retired SOURCE) | **CLOSED** — nothing listening |
| TARGET dashboard | Running, PID 15944, port 8501. **Not touched.** |
| Engine (`python -m engine`) | **Not running.** (A transient PID 17868 was seen once by a `like` match and was gone before inspection; no engine since.) |
| `--check-config` | `mode=PAPER`, `db=data\engine.db`, `heartbeat=5s`, `offline_after=15s`, `magic=100100` |
| Logged-in account | `login=5057151780`, `server=MetaQuotes-Demo`, `currency=USD`, `equity=99,984.17` — read directly |
| Open positions | USDJPY BUY 0.01, EURUSD SELL 0.01, both `magic=234000` — **foreign, untouched** |
| `config.yaml` `db` path | **unchanged**, `data/engine.db` |

`--check-config` does **not** verify the MT5 account or credentials today. Extending it is item 6 in §8.

---

## 2. Account fingerprint guard — implemented

### 2.1 Journal storage (migration v1 → v2)

`engine/journal.py`: `SCHEMA_VERSION` 1 → 2. `MIGRATION_2`, applied only to a **new** journal, never to a shipped one:

```sql
CREATE TABLE IF NOT EXISTS journal_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    login INTEGER, currency TEXT, server TEXT, magic INTEGER,
    recorded_at TEXT NOT NULL);

ALTER TABLE engine_state ADD COLUMN account_login INTEGER;
ALTER TABLE engine_state ADD COLUMN account_currency TEXT;
```

New accessors `set_account_fingerprint()` / `get_account_fingerprint()`. `set_state()` now accepts `account_login` / `account_currency` and writes them with `COALESCE`, so a later caller that does not know the account cannot blank an existing stamp.

The live `data/engine.db` stays at **v1, untouched**. The migration was exercised on a fresh journal.

### 2.2 Guard module — `engine/account_guard.py` (new)

- `Fingerprint(login, currency, server)` with `matches()` treating a missing field on **either** side as a mismatch, never a wildcard. An unreported currency is a mismatch specifically because it is the case that would otherwise look verified.
- `fingerprint_from_account()` reads currency **only** from `account_info().currency`. No default, no hardcoded USD or EUR anywhere.
- `AccountGuard.check(account, phase=...)` raises `AccountMismatch` and writes a **CRITICAL** `account` event naming the phase, both fingerprints and every blocked operation. Rejects: no account logged in; live ≠ configured; live ≠ the fingerprint the journal was stamped for. Stamps the journal **only** on a first successful match, never on a mismatch.
- Write gates, all `False` while mismatched: `may_reconcile`, `may_write_telemetry`, `may_submit_orders`, `may_modify_orders`.
- `classify_foreign_positions(positions, magic)` returns non-matching-magic positions for exposure, margin and max-open-trade counting, and for display. It never writes them.
- `FOREIGN_MAGIC = 234000` — the retired system's magic.

### 2.3 Guard test results — `tests/test_engine/test_account_guard.py`

| Requirement | Test | Result |
|---|---|---|
| startup mismatch | `test_a_mismatch_blocks_startup_and_journals_critical` — CRITICAL event, phase `startup`, blocks trading / reconcile / order submission | pass |
| mismatch does not re-stamp | `test_a_mismatch_never_stamps_the_new_account` — journal keeps 5057151780 | pass |
| reconcile protection | `test_reconcile_cannot_touch_the_journal_after_a_switch` — 2 journal rows survive a mismatched check | pass |
| runtime account change | `test_a_mid_session_switch_blocks_every_write` — all four gates `False` | pass |
| reconnect / reinit / refresh | `test_each_phase_is_named_in_the_journal` — `reconnect`, `mt5_reinit`, `session_refresh` | pass |
| EUR account | `test_a_matching_currency_is_accepted[EUR-account]` | pass |
| USD account | `test_a_matching_currency_is_accepted[USD-account]` | pass |
| unreadable account | `test_an_unreadable_account_blocks_startup` — `None` is a mismatch, not a pass | pass |
| recovery after restore | `test_a_recoverable_check_restores_the_gates` | pass |
| foreign positions | `TestForeignPositions` (5 tests) — classified, counted, never written | pass |
| non-vacuity | `test_the_guard_actually_raises` (positive control) | pass |

**The reconcile test is the load-bearing one.** After an account switch every journal ticket looks absent at the broker, so an unguarded reconcile would delete the whole `positions` table in one pass. The test asserts the rows survive.

### 2.4 Hardcoded currency removed

`data_engine.py:369` returned `"currency": "USD"` on the **disconnected** path — a `$` shown on an account nobody had queried. Now `None`.

`formatting.py:21-22` left as-is: it is a currency→symbol **lookup table**, not a default, and its docstring already records that a hardcoded `$` on a EUR account was the original bug. It now receives only `account_info().currency` or `None`.

---

## 3. Has-stop fix — `engine/risk.py`

`has_stop()` and `stop_distance` no longer raise. Added `_as_float_or_none` (handles `None`, `""`, whitespace, non-numeric text and NaN — the quiet one, because `float("nan")` succeeds and every comparison against it is `False`) and `_safe_stop_distance`.

**Design flaw found while testing:** `stop_distance` was `abs(entry - stop)`, so `sl = 0.0` reported a distance of `1.1` to a price of zero. Both positions the reconcile dropped carried `sl=0.0`, and a position could have been sized against a stop that did not exist. `_safe_stop_distance` now returns `0.0` when the stop is missing, non-positive or unparsable, so `has_stop` is `False` and the order blocks.

| Case | `has_stop` | Test |
|---|---|---|
| `None`, `""`, `"   "`, `"0"`, `"0.0"`, `0`, `0.0`, `-1`, `-0.0001`, `"abc"`, `nan` | `False`, no raise | `test_invalid_stops_return_false` (11 ids) |
| `1.095`, `"1.095"`, `0.00001`, `1.11111` | `True` | `test_valid_stops_return_true` |
| stop at entry | `False` | `test_a_stop_at_the_entry_is_not_a_stop` |
| `sl = 0.0` | `False`, distance `0.0` | `test_a_zero_stop_does_not_report_a_distance` |
| NaN entry price | `False` | `test_a_nan_entry_price_is_also_safe` |
| gate decision, not just the flag | refused | `test_the_risk_gate_still_refuses_an_invalid_stop` |

Both paths covered: engine path `engine/risk.py` (`evaluate` hard-refuses); UI path `ui/data_access.py:558-560` (`SL_REQUIRED` before a journal or executor is opened).

---

## 4. Symbol mapping proposal — **for approval, nothing committed**

Read-only survey of account **5057151780** (`symbols_get()`, 12,306 instruments offered). **10 of 16 whitelist symbols map; 6 are not offered.** No config changed, no fallback applied, nothing committed.

| Whitelist | Proposed broker name | Contract | tick_size | vol_step | vol_min | stops_level | Status |
|---|---|---|---|---|---|---|---|
| EURUSD | EURUSD | 100000 | 1e-05 | 0.01 | 0.01 | 0 | mapped |
| GBPUSD | GBPUSD | 100000 | 1e-05 | 0.01 | 0.01 | 0 | mapped |
| USDJPY | USDJPY | 100000 | 0.001 | 0.01 | 0.01 | 0 | mapped |
| USDCAD | USDCAD | 100000 | 1e-05 | 0.01 | 0.01 | 0 | mapped |
| USDCHF | USDCHF | 100000 | 1e-05 | 0.01 | 0.01 | 0 | mapped |
| AUDUSD | AUDUSD | 100000 | 1e-05 | 0.01 | 0.01 | 0 | mapped |
| NZDUSD | NZDUSD | 100000 | 1e-05 | 0.01 | 0.01 | 0 | mapped |
| EURGBP# | EURGBP | 100000 | 1e-05 | 0.01 | 0.01 | 0 | mapped (`#` dropped) |
| EURJPY# | EURJPY | 100000 | 0.001 | 0.01 | 0.01 | 0 | mapped (`#` dropped) |
| GBPJPY# | GBPJPY | 100000 | 0.001 | 0.01 | 0.01 | 0 | mapped (`#` dropped) |
| BTCUSD# | — | — | — | — | — | — | **not offered** |
| ETHUSD# | — | — | — | — | — | — | **not offered** |
| US500Cash# | — | — | — | — | — | — | **not offered** |
| US30Cash# | — | — | — | — | — | — | **not offered** |
| US100Cash# | — | — | — | — | — | — | **not offered** |
| UK100Cash# | — | — | — | — | — | — | **not offered** |

**Offered but not whitelisted:** `XAUUSD` (contract `100`, `path=Metals\XAUUSD`, `vol_step 0.01`, `vol_min 0.01`, `stops_level 0`). Needs an explicit decision.

Two findings that affect risk sizing:

1. **`trade_stops_level = 0` on every mapped symbol.** `_min_stop_distance` treats 0 as "no broker minimum". A zero broker minimum is unusual — confirm it is a real value and not an unread field before relying on tighter stops.
2. **`trade_tick_size` differs by pair** (`1e-05` for most, `0.001` for JPY crosses). Risk sizing must use the per-symbol broker spec, which `OrderIntent.from_symbol_spec` already does — confirming the lesson recorded in `tests/test_engine/test_risk.py::TestTickSpecConversion` from the earlier live probe.

Unmapped symbols must **fail closed** under the existing `config_loader` whitelist check. No silent fallback.

---

## 5. Journal archive and switch procedure

**Done:**

| Item | Value |
|---|---|
| Archive | `backups/engine.db.acct-113363533.2026-10-12.archive` — 14,127,104 bytes, `IsReadOnly=True`, plus archived `-wal` and `-shm` |
| Fresh journal | `data/engine_5057151780.db`, schema **v2**, stamped `login=5057151780, currency=USD, server=MetaQuotes-Demo, magic=100100`, `engine_state=OFF/PAPER` |
| Rows imported from 113363533 | **none** |
| Accounts mixed | **none** |
| `config.yaml` db path | **unchanged** |

The archive used **copy, not move** — the running dashboard holds `data/engine.db` open. No old-account positions were reconciled, imported or searched for.

**Switch procedure — NOT executed, requires your approval:**

```powershell
# 1. Stop the dashboard (it holds data/engine.db open)
Stop-Process -Name python -Id 15944 -Force

# 2. point config.yaml journal.db at data/engine_5057151780.db

# 3. verify before starting
& E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -m engine --check-config
```

---

## 6. Tests run this pass

| Command | Result |
|---|---|
| `pytest tests/test_engine/test_account_guard.py tests/test_engine/test_risk.py tests/test_engine/test_telemetry.py` | 102 passed, exit 0 |
| `pytest` (full suite) | **1396 passed, 6 skipped**, exit 0 |

All via `E:\HedgeFund_Binod_v2\.venv\Scripts\python.exe -m pytest`, `-p no:cacheprovider`, external `--basetemp`. Mocked MT5, mock providers. No broker, no AI, no Telegram.

---

## 7. Files changed this pass

| File | Change |
|---|---|
| `engine/journal.py` | `SCHEMA_VERSION` 1→2; `MIGRATION_2`; `set_state` accepts + `COALESCE`-writes account columns; `set_account_fingerprint` / `get_account_fingerprint` |
| `engine/account_guard.py` | **new** — fingerprint, guard, write gates, foreign classification |
| `engine/risk.py` | `_as_float_or_none`, `_safe_stop_distance`; `has_stop` / `stop_distance` no longer raise; zero-stop distance no longer fabricates a risk distance |
| `data_engine.py` | disconnected path returns `currency: None` instead of `"USD"` |
| `tests/test_engine/test_account_guard.py` | **new** — 27 guard tests including 2 controls |
| `tests/test_engine/test_risk.py` | added `TestHasStopNeverRaises` (18 cases) |
| `data/engine_5057151780.db` | **new** — fresh v2 journal, dormant |
| `backups/engine.db.acct-113363533.2026-10-12.archive` | **new** — read-only archive |

Not changed: `config.yaml`, `.env`, any credentials, the live `data/engine.db`, the dashboard, any broker position, any risk limit.

---

## 8. Remaining work — honest status

| # | Item | Status |
|---|---|---|
| 1 | Account guard module, journal storage, migration, tests | **Done** |
| 2 | Has-stop fail-closed fix + tests | **Done** |
| 3 | Hardcoded currency removed | **Done** |
| 4 | Symbol mapping survey | **Done — proposal only** |
| 5 | Journal archive + fresh journal | **Done** |
| 6 | **Guard wired into `engine/__main__.py`**: startup / reconnect / reinit / session-refresh checks, reconcile gate, telemetry gate, foreign-magic startup block, `--check-config` account verification | **Not started** |
| 7 | UI: account tooltip `****1780 \| USD`, "Not managed by this system" view, "Account mismatch" display | **Not started** |
| 8 | Journal switch (`config.yaml` db path) | **Blocked on restart approval** |
| 9 | `engine/brain.py` proposer, `ai_decisions` table + migration, settings via commands table, DeepSeek in `engine/keys.py` + `.env.example` | **Not started** |
| 10 | Six SOURCE capability ports, each documented | **Not started** |
| 11 | Auto-execute addendum (ten requirements) | **Not started** |
| 12 | Navbar prototype on a branch + screenshots | **Not started** |
| 13 | Gate 2 PAPER journal evidence for `ai_decisions` | **Blocked on 8 + 9** |

**Why 6–13 were not delivered.** Item 6 wires the engine's startup and run loop. With the restart policy in force, the engine cannot be run to verify the wiring, so its only evidence would be unit tests — and I was not willing to trade a verified-green suite (**1396 passed**) for an unverified change to the loop. Item 9 is a large body of new code that the stated priority places behind the guard.

### Restart conditions

| # | Condition | State |
|---|---|---|
| 1 | Port 8000 closed | **confirmed** |
| 2 | Account guard merged | code complete, **wiring outstanding** |
| 3 | Guard tests pass | **yes** |
| 4 | Fresh journal created | **yes** (dormant) |
| 5 | Symbol mapping reviewed | **awaiting decision** |
| 6 | Logged-in account = 5057151780 | **verified** |
| 7 | Explicit operator approval | **outstanding** |

Mode remains `PAPER`. Approve-first remains the default.
