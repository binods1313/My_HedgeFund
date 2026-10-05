# UI architecture

This dashboard keeps the trading engine as the source of truth and exposes a thin Streamlit UI layer. All live numbers are either pulled from the MT5 broker or from the engine journal; the UI never invents portfolio state.

## Theme tokens

The theme lives in `ui/theme.py` and is mirrored in `.streamlit/config.toml` so the page does not flash a white shell before styling loads.

- Background: `#121318`
- Panel: `#1A1C24`
- Border: `#262A36`
- Text: `#E6E8EE`
- Accent: `#B8F2FF`
- Gain: `#00E676`
- Loss: `#FF3B30`

## State

The session-state defaults are defined in `ui/state.py` and are initialised once at startup via `init_state(whitelist)`. The state is validated against the current whitelist so a config change cannot leave stale asset selections behind.

## Data access

`ui/data_access.py` owns the contract for read-only broker data and order submission:

- `get_account_snapshot()` returns an `AccountSnapshot` built from the live telemetry.
- `get_active_positions()` always returns the same DataFrame schema, even when empty.
- `close_position()` calls the broker adapter and converts errors to typed results instead of bubbling raw exceptions.
- `submit_order()` is an **adapter onto the engine**, described below.

### Order submission: the engine seam

Manual orders do not reach the broker. `submit_order()` builds a real engine
`Executor` in-process and submits through it, so a hand-entered trade passes the
same gates as an automated one:

```
submit_order()
  ├─ load_engine_config()
  │    ├─ mode == LIVE  → Result(False, "LIVE_UI_DISABLED")   # refused here,
  │    │                                                       # before anything is built
  │    └─ mode == PAPER → continue
  ├─ RiskEngine.evaluate() → RiskApproval            (engine/risk.py)
  ├─ make_live_guard() → LiveGuard                   (engine/guard.py)
  ├─ build_executor(mode, journal, state, tick_provider, live_guard)
  └─ executor.prepare_order() → executor.submit(request, approval)
                                                     (engine/executor.py)
```

Gate order inside `submit` is `_check_sl` → `_check_risk` → `_check_state` →
`_check_invalidation`. An `ExecutionRejected` returns as
`Result(ok=False, code="EXECUTION_REJECTED", message=str(exc))` — the engine's own
sentence, unparaphrased, so the frontend banner reports the reason the backend
actually gave rather than a UI-authored summary of it.

**LIVE is refused from the UI by design.** Constructing a `LiveExecutor` here
would build a `Gateway` and open a second MT5 session inside Streamlit, competing
with the one `data_engine` already owns. Live trading stays CLI-only; the UI obeys
`config.yaml` for PAPER and refuses LIVE outright.

**Absolute prices.** `OrderRequest.sl`/`tp` are absolute prices, so the adapter
converts the caller's pip-ish units with the ×10 point convention inherited from
the legacy path: `round(price ∓ sl * point * 10, digits)`. Changing that
convention would silently rescale every stop the UI sends, so it is preserved
rather than modernised.

**The anchor is re-checked at the dispatch instant.** `engine/guard.py` builds a
`LiveGuard` from caller-supplied callables, because the engine layer must not
import `data_engine`. The closure re-reads the live price and tests the structural
leg's 100% invalidation line immediately before dispatch. It fails **closed**:
absent candles, an unreadable price, or a raising provider all block the order — a
check that cannot run is not a check that passed. An absent *leg*, by contrast,
passes: `select_structural_leg` is strict, so blocking on absence would reject
nearly every manual order for being unevaluable rather than for being broken.

The AI Signals tab passes the leg its intent was actually built on
(`leg=dry_run.signal.leg`); the manual path derives one from a fresh candle
snapshot. Re-deriving on the AI path would test a *different* structure and defeat
the point of handing one in.

**Account equity.** The risk-percentage gates are evaluated against **live broker
equity**, with the journal supplying only `start_of_day_equity` and `peak_equity`.
`engine/dryrun_cli.py` uses `baselines.peak_equity`; a percentage floor measured
against a stale peak is not a floor on current capital.

**Known wart.** `LiveGuard.price_provider` is declared but never read by
`verify()`, so the `invalidation_check` closure captures the price source itself.
Documented rather than fixed; changing it would touch the guard's signature.

## Risk guardrails

`ui/risk_calc.py` keeps the computations pure and importable without Streamlit or MT5. It handles lot normalization, tick-size conversions, and stop-risk calculations with explicit safeguards for invalid data.

## News feed

`ui/services/news.py` fetches the public ForexFactory XML feed, parses it with defused XML parsing, and falls back to cached data when the network is unavailable. It never raises on malformed XML and keeps the feed read-only.

## Repository hygiene

Three runtime artifacts are excluded in `.gitignore` because they are regenerated
on every run: committing them adds churn, and a tracked cache is worse than an
untracked one because it silently diverges from the machine that produced it.

| Pattern | Why |
|---|---|
| `*.db-wal` | SQLite write-ahead log — memory-mapped companion to `data/engine.db`, rewritten on every transaction commit. |
| `*.db-shm` | SQLite shared-memory index for the WAL. Same lifetime as the log. |
| `data/news_cache.json` | Transient ForexFactory cache written by `ui/services/news.py`; regenerated on the next fetch. |

`*.db` (line 21) does **not** cover the first two: a suffix pattern matches a
complete final extension only, so `engine.db-wal` is not a `*.db`. These barriers
are load-bearing, not decorative.

Verify with:

```
git check-ignore -v data/engine.db-wal data/engine.db-shm data/news_cache.json
```

## Architectural invariants

Enforced by `tests/test_engine/test_boundaries.py` on the AST rather than by
review, because a single stray import reintroduces the bug and no functional test
would notice:

- `engine/gateway.py` is the only engine module that may import `MetaTrader5`.
- No module outside `engine/keys.py` may read `os.environ` for a secret.
- No module under `ui/` may call `order_send`, `send_order`, `order_check`, or
  `execute_trade`.

The scans parse the AST rather than matching text: `ui/data_access.py` names
`execution.execute_trade` inside a docstring whose whole purpose is to deny it, so
a substring match would report the module's own documentation as the violation it
disclaims.
