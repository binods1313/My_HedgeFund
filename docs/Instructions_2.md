# Operator Instructions 2

Phase 0 audit received and accepted. Good work.

APPROVAL

- Approve Phase 1 now, and Phase 2 after Gate 1 passes. Do not start Phase 3 yet. Verify each gate before the next one starts.
- Before starting the engine: PAPER mode only, do not pass --live, and make sure MT5 is open and logged in.

DECISIONS (record these in the audit document)

- C1 Symbols: keep TARGET's 16-symbol whitelist. The Brain works only on symbols from it.
- C2 Magic number: keep 100100 for everything. Link each Brain order to its decision through the journal (decision ID, ticket).
- C3 Risk: TARGET's risk engine only. Brain SL/TP percentages are discarded.
- C4 Brain role: the Brain is a PROPOSER only. It supplies signal + confidence + reasoning summary as a candidate. The pipeline becomes: Brain proposal → strategy/risk gate → (optional xAI validation, toggle in settings) → executor → journal. The Brain never sends orders and never touches MT5.
- C5 DeepSeek: keep it as a registered provider. Move DEEPSEEK_API_KEY into engine/keys.py, add it to .env.example, with timeouts, retries and a fallback to another registered provider.
- C6 Account: the TARGET engine is the only MT5 talker. The Brain process on port 8000 is not to be run alongside it.
- C7 State: the journal. No in-memory state.
- C8 UI: keep Streamlit on port 8502 for now.
- C9 Process topology: Phase 3 later, after Gates 1-2.
- C10 Venvs: do not merge. Add only what TARGET needs to its own requirements.txt.

PHASE 2 REDEFINED: BRAIN AS PROPOSER (test-first)

1. Create engine/brain.py (internal name only). It takes a market snapshot and returns {symbol, side, confidence, reasoning_summary, key_levels}. It reuses the strict JSON parsing and confidence range check from the SOURCE ai_brain.py, plus the 30s analysis loop idea.
2. Store each proposal as a row in the journal (new table: ai_decisions) with: timestamp, symbol, side, confidence, summary, status (proposed / skipped / sent to risk / rejected / executed), skip reason, risk checks passed or failed, linked order ticket, provider, latency_ms, tokens, cost_usd.
3. Add settings: confidence threshold (default 70), auto-execute vs approve-first (default: approve-first), Brain on/off, run interval, watched symbols. Use the existing commands table and config validation.
4. Port the six SOURCE items you listed in section 7 as covered additions, only where TARGET lacks an equivalent.
5. Mocked tests for: parser failures, provider timeout and fallback, below-threshold skip, duplicate-position skip, risk-gate rejection, journal linkage. PAPER mode must remain unchanged.
6. Fix the _write_telemetry gap from section 4.3 in Phase 1 (log a WARNING event when connected but account_info() is empty).

UI DIRECTION (design after Gate 2; propose, do not build yet)
Requirements from me:

- Fixed top navbar with dropdowns: Overview | Signals | AI Desk | Positions | General (General = Journal, News and Calendar, Research, Settings).
- Right side always visible: live date and time, MT5 connection indicator, dashboard LIVE/STALE indicator, "Show AI confidence" toggle, dark/light switch, settings gear.
- Overview: 4-5 live instrument prices, liquidity (spread and tick volume), running trades, latest signals, floating P&L.
- The words agent, brain, KalkiQuant, DeepSeek, and any model or vendor name must not appear in the UI, tooltips, errors or displayed payloads. Use "AI", "AI Desk", "AI analysis" only.
- AI Desk is the only page showing AI activity. Visual style: dense dark telemetry with glow accents, a decision pipeline view, confidence stream, parallel-check bars, a decision log with skip reasons, and a guardrails panel (spend cap, action limits, never auto-trade while a guardrail trips). Every widget must be driven by real journal data, never decorative fake numbers.
- The animated pipeline visual may be an embedded custom component (canvas or HTML) inside Streamlit.

Report back with: (1) whether Streamlit can deliver the fixed dropdown navbar, live updates without flicker, and the theme switch cleanly; (2) if not, the lightest option, such as a thin read-only API/SSE layer over the journal with a new frontend. Do not change the UI framework without my approval.

Start Phase 1 and report at Gate 1.

ADDENDUM: AUTO-EXECUTE MODE (applies to Phase 2)

The AI proposer must support two execution modes, switchable by me at any time:

1. APPROVE-FIRST (default on a fresh install): each proposal waits in the Signals list as "Awaiting review" until I approve or reject it.
2. AUTO-EXECUTE: a proposal that passes every check goes straight to the executor with no manual approval.

In both modes the pipeline is unchanged:
AI proposal -> confidence threshold -> TARGET risk gate (stop-loss, lot cap, exposure cap, max positions, duplicate check, news blackout, spread check, daily loss limit, drawdown limit, kill switch) -> optional validation -> executor -> journal.
Auto-execute only removes the manual approval step. It never bypasses a risk check.

REQUIREMENTS

1. Setting: trading.execution_mode = "approve_first" | "auto" in config, validated loudly, changeable from the UI through the commands table (no restart). Persist it. Journal every mode change with timestamp and reason.
2. Enabling auto-execute requires a confirmation dialog that states: current trading mode (PAPER or LIVE), confidence threshold, max lots, max open positions, and daily loss limit. I must type the word AUTO to confirm.
3. PAPER and LIVE are separate gates. Auto-execute in PAPER is allowed after the confirmation above. Auto-execute in LIVE additionally requires the existing live_confirmation_required step. Switching from PAPER to LIVE must automatically fall back to approve-first, and I must re-enable auto-execute deliberately.
4. Separate, stricter limits that apply only in auto mode (all configurable in Settings > Trading, with defaults):
   - Auto confidence threshold (default 80, never lower than the normal threshold).
   - Max auto orders per day (default 10).
   - Max auto orders per symbol per day (default 2).
   - Max lot size per auto order (default 0.01).
   - Cooldown per symbol after an auto order or an auto loss (default 30 min).
   - Auto-pause trigger: N consecutive auto losses (default 3) or daily loss limit hit.
5. Auto-fallback: if any of these happen, switch to approve-first immediately, journal the reason, and show a visible banner plus a Telegram alert:
   - MT5 disconnects or the data goes stale.
   - AI provider failures exceed a threshold (default 3 in a row) or the AI spend cap is hit.
   - The risk gate reports an inconsistency.
   - Engine restart (auto mode never resumes silently after a restart; it comes back as approve-first).
   - The kill switch is used.
6. Each auto-executed order is marked "Auto" in Signals, Positions and Journal, and is linked to the decision ID, magic number and ticket. Approved-by-me orders are marked "Manual approval".
7. Always allowed while in auto mode: per-position close, close-all, and a one-click "Pause auto-execute" button in the navbar (visible only when auto is on) that switches to approve-first without a confirmation.
8. UI placement: a mode switch in Settings > Trading, and an "AI mode" status chip in the AI Desk header showing "Approve-first" or "Auto" (amber when auto, with a live count of auto orders today against the daily limit). The navbar chip must not use vendor or internal names.
9. Tests (mocked MT5): auto mode with threshold met sends the order; below-threshold is skipped; every risk-gate rejection blocks an auto order; daily and per-symbol caps stop orders; consecutive-loss pause; each fallback trigger reverts to approve-first; PAPER to LIVE switch resets to approve-first; engine restart resets to approve-first; the journal linkage is correct.
10. Acceptance: with auto mode on in PAPER, I can see a proposal become an executed paper trade end to end, with the whole chain visible, and every limit above can be demonstrated to stop it.

Mode defaults to approve-first. Do not enable auto-execute for me, and do not default it to on in any config or migration.
