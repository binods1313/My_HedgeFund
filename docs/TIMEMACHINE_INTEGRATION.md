# Time Machine — Pre-Approval Hook (Paper)

The MT5 port of `trader-timemachine` (`scripts/timemachine_mt5.py`) is now
**fully wired** into the paper pipeline as an advisory pre-approval check.

## How it behaves

1. A signal passes risk checks (scan → risk → LLM validation).
2. Before the Telegram approval alert is sent, `ApprovalFlow` runs the
   Time Machine as a subprocess (`python -m scripts.timemachine_mt5 run
   --json`).
3. Its verdict/edge metrics are:
   - attached to the `PendingTrade`,
   - **appended to the Telegram approval message** (odds + verdict + notes),
   - journaled as a `timemachine` event (`timemachine_ok` / `timemachine_error`).
4. **Fail-open:** any error, timeout, bad-plan refusal, or malformed output
   is journaled and the pipeline continues unchanged — it never blocks,
   gates, or modifies an order.

## Modes

- **Automatic:** `config.yaml → timemachine.enabled: true` (default **true**).
  Only runs in PAPER mode (the approval flow itself is PAPER-only).
- **Manual:** unchanged — run the CLI anytime:
  ```bash
  .venv/Scripts/python.exe -m scripts.timemachine_mt5 run \
      --symbol EURUSD --entry 1.1175 --stop 1.1169 --target 1.1187 \
      --date 2026-11-04 --replays 10000 --seed 19
  ```

## Config (`timemachine:` section)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | run the hook before Telegram approval |
| `replays` | `2000` | Monte Carlo replays (manual CLI can use 10000) |
| `seed` | `19` | deterministic reproducibility |
| `window` | `2` | time-of-day slot matching ± bars |
| `until` | `"23:45"` | flatten horizon for "neither" outcomes |
| `timeout_seconds` | `60` | subprocess cap; on timeout → fail-open |
| `show_in_telegram` | `true` | append verdict block to approval message |

## Files

- `engine/research/timemachine_hook.py` — runner + parser + fail-open journaling
- `engine/approval_flow.py` — calls hook before `send_approval_message`
- `engine/notifications/telegram_service.py` — `PendingTrade.timemachine` + message block
- `config.yaml` / `config_loader.py` / `engine/config.py` — `TimemachineSettings`
- `tests/test_engine/test_timemachine_hook.py` — unit + integration + fail-open

## Notes

- Target fallback: if the signal has no TP, a 2R target is derived from the
  stop distance.
- The plan date used is the UTC date at approval time (the pool is always
  built from previous full weekdays, so today's partial session is excluded).
- Runtime ≈ 1s for 2000 replays — negligible at M15 cadence.
