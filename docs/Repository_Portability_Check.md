# Repository portability check

Written 2026-10-16. Read-only inspection of `E:\HedgeFund_Binod_v2` on
branch `feature/echofi-ui`. No source file was edited. PID 44436 was not
restarted.

## Answers

**Can another machine clone and run the repository without the temp
files that were created under `C:\Users\Binod\AppData\Local\Temp`?**

**YES.**

Those files are not in the git tree, nothing in the engine imports them,
and a search of the repository for `hf_commit_` on 2026-10-16 returned
no matches. The engine entry is `python -m engine`. It does not read
that temp directory.

**Can a fresh clone run the full stack on a different machine with no
other setup?**

**NO.**

A clone has no virtualenv, no `.env`, no MetaTrader 5 terminal, and no
live journal. Those are local setup, not the temp files. One tracked
script also hardcodes this machine, and it is not the engine entry.

## Temp files under `C:\Users\Binod\AppData\Local\Temp`

During the 2026-10-15 session, probe and note files were written there
(examples: `hf_evidence_ro.py`, `hf_symbols_live.py`, `hf_commit_*.txt`).
They were deleted at the end of that session. They are not in `git
ls-files`. The 2026-10-16 content search for `hf_commit_` found nothing
in the repository. The search skipped `data\engine_5057151780.lock`
because another process has locked a portion of it (os error 33). That
skip is the live lock, not a temp-file dependency.

| Class | Are the AppData temp files in this class? |
|---|---|
| Temporary artifacts | Yes. Session probes and commit-message scratch. Removed after use. |
| Build artifacts | No. The engine is not built from them. There is no compile step that reads them. |
| Runtime dependencies | No. `engine/`, `ui/`, and `config.yaml` do not import or open them. |

`pytest_*.txt` files that a local run may leave in the repo are ignored
by `.gitignore` line 96. They are local test logs. They are not required
to start the engine. Some of those ignored logs contain
`C:\Users\Binod\AppData` paths. They are not tracked.

## Hardcoded paths in tracked code

Search on 2026-10-16 of `*.py`, `*.yml`, `*.yaml`, `*.toml`, `*.cfg`,
`*.ini`, `*.bat`, and `*.ps1` for `C:\Users\Binod`, `AppData`,
`Local\Temp`, and `hf_commit_`:

One hit.

`scripts/task3_collect.py`:

```python
outdir = r'C:\Users\Binod\AppData\Local\Temp\kilo'
# ...
cwd=r'E:\HedgeFund_Binod_v2',
```

Lines 28 and 36. The script runs a fixed list of pytest files and writes
their output under that temp directory. It is a one-off collector. It is
tracked (`git` knows the file). It is not called by `python -m engine`,
by `build_engine`, or by the UI startup path.

No other tracked source file in that extension set contained
`C:\Users\Binod` or `AppData`.

## What a clone does contain

`config.yaml` journal path is relative:

```yaml
db_path: data/engine_5057151780.db       # resolved to an absolute path under the project root
```

The absolute path printed by `--check-config` on this machine
(`E:\HedgeFund_Binod_v2\data\engine_5057151780.db`) is the resolution of
that relative path. It is not a second hardcoded constant in `config.yaml`.

`.gitignore` excludes, among other local state:

```
.env*
.venv/
*.db
backups/
logs/
pytest_*.txt
data/shadow_mode.on
```

So a clone does not include the virtualenv, secrets, the live journal,
backups, or the shadow-mode toggle file.

## What a clone does not need from this machine

| Item | Needed to run? | Evidence |
|---|---|---|
| `C:\Users\Binod\AppData\Local\Temp\hf_commit_*.txt` | No | Not in the repo. Search `hf_commit_` returned no matches. |
| Probe scripts that were written in that temp directory and then deleted | No | Not imported. Not tracked. |
| `scripts/task3_collect.py`'s temp output directory | No, unless someone runs that collector | The engine does not call it. On another machine the collector itself would fail or write to a missing user path. |
| This machine's `data\engine_5057151780.db` | No for a fresh PAPER journal | `*.db` is ignored. A new process creates its journal at the configured relative path. Copying this live file is a separate decision and must not be done by constructing `Journal` while PID 44436 is writing it. |

## What another machine still needs

These are outside the temp-file question. They are why the full-stack
answer is NO.

- A Python environment installed from the project requirements. `.venv/`
  is gitignored and is not in the clone.
- A gitignored `.env` for keys the process expects. This package does not
  list those keys.
- Windows and a local MetaTrader 5 terminal. The gateway uses the
  MetaTrader5 package and a logged-in terminal. The 2026-10-15 probe was
  against MetaQuotes-Demo on this desktop.
- A decision before opening an existing schema-2 journal with the current
  `Journal` class. `SCHEMA_VERSION` is 3. A fresh empty file is a
  different case from this machine's live file.

Historical docs (`docs/PROGRESS.md` and other notes) contain
`C:\Users\Binod` and `AppData` paths as narrative of this machine. They are
not read by the engine at startup.

## Round 2 re-verification (2026-10-16)

Re-ran the same search after writing the Round 2 documentation package.

### Search scope

`*.py`, `*.yml`, `*.yaml`, `*.toml`, `*.cfg`, `*.ini`, `*.bat`, `*.ps1`

### Results

One hit in a **tracked** file:

```
scripts/task3_collect.py:28: outdir = r'C:\Users\Binod\AppData\Local\Temp\kilo'
```

This is unchanged from Round 1. It is a one-off collector script not called
by `python -m engine`, the UI, or the engine build path.

No hits in any new file created for this package:
`docs/evidence/*.md`, `docs/Architect_Review_Round2_Completion_Report.md`,
`docs/Round2_Code_Change_Log.md`, `docs/Symbol_Survey_Proposal.md`, the
updated `docs/Architect_Handoff_2026-10-15.md`, or this file.

### `.venv/` exclusion

`.venv/pyvenv.cfg` contains `C:\Users\Binod` paths, but `.venv/` is covered by
`.gitignore` line 89 and is not in the git tree. A fresh clone has no
`.venv/`. This is expected and was not present in the Round 1 source search
(the Round 1 search did not recurse into `.venv`).

### Answer to the core question

**Can a fresh clone run on another machine without the AppData temp files?**

**YES.** Confirmed again. The only tracked hardcoded path is
`scripts/task3_collect.py`, which is not on the engine or UI startup path.
The new documentation files contain no hardcoded paths.

**Can a fresh clone run the full stack on a different machine with no other
setup?**

**NO.** Unchanged from Round 1: needs Python venv, `.env` secrets, and a
local MT5 terminal.

## Evidence summary

| Claim | Label |
|---|---|
| Another machine can clone and run without the AppData temp files (`hf_commit_*.txt` and the deleted probe scripts). | YES. They are temporary artifacts, not build artifacts, not runtime dependencies. |
| A fresh clone runs the full PAPER stack with no venv, no `.env`, and no local MT5. | NO. |
| Tracked engine and UI code contain `C:\Users\Binod`. | No match in the 2026-10-16 source search. |
| The repository is free of every machine-specific path. | No. `scripts/task3_collect.py` hardcodes `C:\Users\Binod\AppData\Local\Temp\kilo` and `E:\HedgeFund_Binod_v2`. That script is not the engine entry. |
