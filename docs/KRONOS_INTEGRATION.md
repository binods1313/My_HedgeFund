# Kronos Integration — ML Forecast Block (Paper Phase)

Status: **ADVISORY / PAPER ONLY**. Kronos forecasts are journaled and scored,
but do **not** gate orders. `config.yaml → kronos.enabled` defaults to
`false`; enable only after the scorecard shows a stable edge.

## What was added

| Path | What |
|---|---|
| `D:/skills/Kronos/kronos_server.py` | Standalone inference server (stdlib HTTP). Loads Kronos models once, serves `POST /forecast` with per-sample agreement stats. Runs in its own venv. |
| `D:/skills/Kronos/.venv` | Python 3.12 venv (torch 2.15, pandas 2.2.2). Separate from the engine venv on purpose: torch/pandas-2.x conflict with the engine's pinned pandas 3.0.6. |
| `D:/skills/Kronos/smoke_test.py` | CPU smoke test (synthetic bars). |
| `engine/research/kronos_client.py` | Engine-side HTTP client (pure `requests`, fail-open → `None`). |
| `engine/research/kronos_hook.py` | Observational hook: after a PENDING signal is written in PAPER mode, calls the service and journals a `kronos` event. Strictly fail-open, never gates. |
| `scripts/kronos_paper_eval.py` | Scorecard: sliding-window backtest of Kronos directional accuracy vs base rate. Dual mode: `--local` (Kronos venv, direct import) or HTTP. `--out FILE` writes machine-readable JSON. |
| `config.yaml` / `config_loader.py` / `engine/config.py` | `kronos` section + `KronosSettings` model (strict, disabled default), surfaced on `EngineConfig.kronos`. |
| `engine/scan_cycle.py` | `ScanCycle.kronos_hook` wiring (constructor + `run()` + `_maybe_kronos` after PENDING writes). Also repairs two pre-existing HEAD bugs (stray indent in `ScanOutcome`; missing `journal` binding in `run_with_engine`). |

## How to run

```bash
# 1. Start the server (separate venv; keep running for 24/7 use)
cd D:/skills/Kronos
export TRANSFORMERS_OFFLINE= HF_HUB_OFFLINE=
.venv/Scripts/python.exe kronos_server.py --port 8610
#    ^ NOTE: this environment sets TRANSFORMERS_OFFLINE=1; clear it or model
#      downloads fail with "offline mode is enabled".

# 2. Score the model on real M15 bars (local mode = no HTTP)
cd E:/HedgeFund_Binod_v2
D:/skills/Kronos/.venv/Scripts/python.exe -m scripts.kronos_paper_eval \
    --symbol GBPUSD --timeframe M15 --local --max-windows 20

# 3. Same via HTTP (the path the engine will use)
.venv/Scripts/python.exe -m scripts.kronos_paper_eval \
    --symbol GBPUSD --timeframe M15 --max-windows 20
```

## First scorecard results (2026-11-04, M15, context 256, horizon 8, 8 samples)

| Symbol | Windows | Accuracy | Base rate (UP) | Edge over base |
|---|---|---|---|---|
| GBPUSD | 12 | 83.3% (10/12) | 50% | **+33.3pp** |
| XAUUSD | 12 | 58.3% (7/12) | 33.3% | −8.3pp |
| EURUSD | 10 | 50.0% (5/10) | 20% | −30.0pp |

Small samples — treat as preliminary. GBPUSD is the only symbol with a
positive edge so far; expand windows and re-score before trusting it.

## Integration points (next steps)

1. **Observational wiring (paper):** when `kronos.enabled` and a signal is
   produced in paper mode, journal a `KRONOS_FORECAST` event next to the
   signal (symbol, direction, agreement, expected_move_pct). No gating.
2. **Gate (only after edge proven):** require agreement ≥ `min_agreement`
   before the confidence engine accepts a strategy signal; disagreement →
   downgrade or REJECTED with reason `KRONOS_DISAGREES`.
3. **VPS:** run the server as a Windows service (NSSM) on the same box as the
   engine; keep the socket on localhost. Never expose it publicly.

## Observational paper hook (v0.2)

When `kronos.enabled: true` (default **false**) and the engine is in
`PAPER` mode, `ScanCycle._maybe_kronos()` runs **after** the scan cycle
writes a PENDING signal row. It calls the Kronos service over localhost HTTP
and journals one `kronos` event:

- success → `kronos_ok` (INFO) with `forecast_direction`, `agreement`,
  `expected_move_pct`, `horizon_close_mean`, `last_close`, `context_window`,
  `horizon_bars`, `sample_count`, `latency_ms`, `model`, `decision_id`,
  `bar_ts`, `signal_direction`.
- any failure → `kronos_error` (WARNING) with the reason, and the pipeline
  continues unchanged. Failures covered: server down (ConnectionError),
  timeout, None forecast, malformed response (missing direction/agreement),
  hook exceptions.

The hook never gates, blocks, modifies, or re-orders anything. The signal row
is written first; the hook is fire-and-forget. `config.yaml → kronos.enabled`
stays false until the scorecard shows a stable edge.

Commands:

```bash
# Local-mode scorecard (Kronos venv, no HTTP) with machine-readable output:
D:/skills/Kronos/.venv/Scripts/python.exe -m scripts.kronos_paper_eval \
    --symbol GBPUSD --timeframe M15 --local --step 32 --max-windows 200 \
    --out docs/research/kronos_GBPUSD.json

# HTTP mode (engine path):
.venv/Scripts/python.exe -m scripts.kronos_paper_eval \
    --symbol GBPUSD --timeframe M15 --step 32 --max-windows 200 \
    --out docs/research/kronos_GBPUSD.json
```

Scorecard JSON fields per symbol: `n_windows`, `attempted`,
`n_forecast_none`, `n_flat_moves`, `accuracy`, `base_rate_up`,
`edge_over_base`, `agreement_mean`, `hit_misses`, `date_from`, `date_to`,
and `windows` (per-window records). Honest interpretation: positive
`edge_over_base` = model beats betting the dominant direction; run enough
windows (≥50) before trusting it.

## Gotchas (learned the hard way)

- This host sets `TRANSFORMERS_OFFLINE=1` → all HuggingFace downloads fail
  with "offline mode is enabled". Unset it before first model load.
- `KronosPredictor.predict` requires `x_timestamp`/`y_timestamp` as
  `pd.Series` (not `DatetimeIndex`) — the module uses `.dt` accessors.
- MT5 `copy_rates_from_pos` returns unix-seconds ints for `time`; convert to
  ISO before sending to the server (the server parses ISO only).
- `max_context` is 512; feeding more bars silently trims to the last 512.
- CPU inference: ~12–15s per 8-bar/8-sample forecast (18-core host). With a
  16-symbol scan at M15 cadence, forecast incrementally or batch
  (`predict_batch` exists upstream).
- The `--local` scorecard runs under the Kronos venv, which does NOT have the
  MetaTrader5 package — dump JSON bar caches with the engine venv first.

## Files touched
- `config.yaml` (+kronos section) · `config_loader.py` (+KronosSettings)
- `engine/research/kronos_client.py` (new)
- `scripts/kronos_paper_eval.py` (new)
- `data/market_bars/{EURUSD,XAUUSD,GBPUSD}__M15.json` (new caches)
