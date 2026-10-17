# Commit history review

Branch `feature/echofi-ui`. Written 2026-10-16 from `git show` of these
eight commits. Not pushed by this documentation task. PID 44436 was not
restarted, so commits that change engine or UI code are on disk only
until the process that imported the old module is replaced. This review
does not replace that process.

Order below is the order requested, which is also newest-first as of
this writing.

## 94b7511

| Field | Value |
|---|---|
| Commit | `94b75116654c95e9f018de38054fa97633638023` |
| Subject | `docs: record the running loop and the live symbol probe` |
| Purpose | Replace the retired AI-brain-to-execution diagram with the path PID 44436 runs, and replace the symbol search note with the read-only terminal results. Records that XAUUSD is offered on this demo terminal. |
| Stat | 15 files, +3112 / −252. Documentation only: `docs/Architecture_Audit_Report.md`, `docs/AutoTrading_Reality_Check.md`, `docs/FINDINGS_AND_HANDOFF.md`, `docs/Five_Item_Closeout_Report.md`, `docs/Instructions_2.md`, `docs/Last_3_Prompts_Report.md`, `docs/Ownership_Table.md`, `docs/PROGRESS.md`, `docs/Phase0_Audit_Report.md`, `docs/Phase1_Account_Guard_And_Symbols.md`, `docs/Phase1b_Deferred_Gaps_Closed.md`, `docs/Phase2A_Scan_Loop_Design.md`, `docs/ProjectStructre.md`, `docs/StepA_Account_Guard_Wiring.md`, `docs/Symbol_Search.md`. |
| Risk | Low for runtime. The files are docs. One historical example in `docs/PROGRESS.md` that looked like a bearer token was rewritten to a description of an Authorization bearer header or an x-api-key header before the commit. A secret scan after that rewrite reported `HITS 0`. Do not put the token-shaped example back. |
| Status | Committed. Does not change PID 44436. |

## d361bc7

| Field | Value |
|---|---|
| Commit | `d361bc768c75879e083eab6ba06b5083c178a317` |
| Subject | `fix: point the purge script at the configured journal` |
| Purpose | `scripts/purge_engine_db.py` had a hardcoded `data/engine.db` after the journal path moved to the account file. It now resolves `load_engine_config().engine.db_path`. |
| Stat | 1 file, +18 / −7. |
| Risk | High if someone runs it. With `--yes` it deletes the live journal at the configured path (`data/engine_5057151780.db`). The script was not run. |
| Status | Committed. Not executed. |

## 457d3f9

| Field | Value |
|---|---|
| Commit | `457d3f944cfbdce995bd6beaa224e94b8305a49f` |
| Subject | `feat: add the stage 1 scan cycle on temporary databases` |
| Purpose | Schema 3 adds scan-cycle header columns and `scan_symbols`. The cycle runs inside `run_once` after the heartbeat, reads the gateway once, and returns to IDLE. It does not generate a signal or send an order. |
| Stat | 8 files, +661 / −5. `engine/__main__.py`, `engine/config.py`, `engine/gateway.py`, `engine/journal.py`, `engine/scan_cycle.py`, two engine tests, `tests/test_ui/test_data_access.py`. |
| Risk | High if the live database is opened with `Journal`, because `SCHEMA_VERSION` is 3 and the live writer is still on schema 2. High if PID 44436 is restarted without a decision to load the scan. The commit itself did not migrate the live file and did not restart the process. |
| Status | Committed. TESTED-ONLY on temporary databases (`26 passed` on the recorded focused run). Not loaded into PID 44436. Live schema still 2. |

## 474efd7

| Field | Value |
|---|---|
| Commit | `474efd7a4b2d4525bfd228f0bb5b9d7169298ade` |
| Subject | `docs: deprecate ai_brain.py without deleting it` |
| Purpose | The module is not imported by `engine/` or `ui/`. The docstring now says so. The file stays until a reviewed retirement. |
| Stat | 1 file, +9 / −1. `ai_brain.py` docstring only. |
| Risk | Low. No behavior change. Deleting the file later would break `tests/test_llm_validator.py` and `tests/test_metrics.py`, which still import it. `scripts/mutation_check.py` names the file and does not import it. |
| Status | Committed. Not on the execution path of PID 44436. |

## 3aaf8b6

| Field | Value |
|---|---|
| Commit | `3aaf8b6afd5885394f1e9a35f9091820158e438b` |
| Subject | `docs: record that shadow.enabled is inert` |
| Purpose | The Research Desk toggle (`data/shadow_mode.on`) starts the shadow loop and calls `run_cycle(enabled=True)`. `config_ext.yaml` `shadow.enabled` is loaded and not read by that loop. Comments record the inert value. |
| Stat | 3 files, +16 / −2. `config_ext.yaml`, `config_ext_loader.py`, `learning/runtime.py`. |
| Risk | Low for the engine loop. Medium if a reader sets `shadow.enabled: true` and expects the engine or the learning loop to follow it. The flag was not wired. |
| Status | Committed. Not wired. Not the PAPER engine path. |

## 720f616

| Field | Value |
|---|---|
| Commit | `720f6161a5fa3956525a1afe5ab51b6cddbedbf8` |
| Subject | `docs: describe what the news agent actually counts` |
| Purpose | Agent 04 does not fetch a calendar. It counts impact labels already on `context["events"]`. An empty list returns no event data (`ok=False`, label `NONE`). |
| Stat | 1 file, +16 / −6. `engine/research/agent_04_news.py`. |
| Risk | Low. The change describes the existing count. It does not add a fetch. PID 44436 does not run the Stage 1 scan, and this agent is not that scan. |
| Status | Committed. Behavior of a research helper clarified. Not exercised by the soak. |

## f0af48b

| Field | Value |
|---|---|
| Commit | `f0af48be8d2f3b7035d7e7dbf144326bba873913` |
| Subject | `ui: update dashboard panels and their tests` |
| Purpose | Refresh signals, research, news, and theme panels that were already changed in the working tree, plus tests for the news parser and the research-desk status text. |
| Stat | 11 files, +249 / −49. Under `tests/test_ui/` and `ui/components/`, `ui/services/news.py`, `ui/theme.py`. |
| Risk | Medium for a running Streamlit process: the code is not loaded until that process restarts or reloads. A reload that imports the new `Journal` against the live database would migrate it. This commit is not the A1 severity fix. |
| Status | Committed. Live page not verified. Streamlit was not restarted for this package. |

## 7616d0f

| Field | Value |
|---|---|
| Commit | `7616d0f1be7cac1c04d315b21bdf6dda5df03685` |
| Subject | `ui: render the stored journal severity` |
| Purpose | The badge mapped an unknown severity to INFO, so a stored CRITICAL account mismatch rendered as INFO. The badge now keeps CRITICAL. The journal read includes `context_json`. The status card reads last trade and today's trade count from the same journal. |
| Stat | 4 files, +269 / −16. `ui/components/cards.py`, `ui/components/header.py`, `ui/data_access.py`, `tests/test_ui/test_journal_render.py`. |
| Risk | Medium for the same reload reason as `f0af48b`. The unit test does not open the live page. Detail is in `docs/A1_Severity_Investigation.md`. |
| Status | Committed. HTML test passed (`test_archived_mismatch_without_kind_renders_critical_not_info`). Live Streamlit page NOT VERIFIED. |

## Working tree around these commits

Before this documentation package, `git status` was:

```
?? data/
?? push_prompt.txt
```

`data/*.db` is ignored by `.gitignore`. `data/` shows because the lock
file is untracked. `push_prompt.txt` was left untracked on purpose.
Empty untracked files named `60` and `=` were deleted and were never
tracked, so there is no cleanup commit for them.

This documentation package adds the markdown files under `docs/`. It
does not add a ninth code commit.
