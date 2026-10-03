"""Mutation checks: prove the key tests can actually fail.

A test that passes against correct code AND against broken code is worthless.
This script deliberately breaks one rule at a time, runs the tests that are
supposed to catch it, and records whether they failed as expected. It then
restores the original file from git.

Usage:
    python scripts/mutation_check.py            # run all mutations
    python scripts/mutation_check.py --list     # show what will run

This modifies working-tree files temporarily. Run it on a clean tree only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Mutation:
    name: str
    target: str
    old: str
    new: str
    expect_fail: list[str]   # test node ids that MUST fail while broken
    # Companion edits applied with this mutation and reverted with it. Used
    # where two independent guards make a single-point break unobservable.
    also: list["Mutation"] | None = None


def _run(node_ids: list[str]) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", *node_ids, "-q", "--no-header", "-x", "--tb=no"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, proc.stdout + proc.stderr


MUTATIONS: list[Mutation] = [
    Mutation(
        name="M1: LLM can raise confidence (positive adjust accepted)",
        target="ai_brain.py",
        old="    if not (adjust_min <= adjust <= adjust_max):",
        new="    if False:",
        expect_fail=[
            "tests/test_llm_validator.py::TestApplyLlmAdjustmentCannotRaise::test_positive_adjust_is_rejected",
        ],
    ),
    Mutation(
        name="M2: threshold applied BEFORE adjustment (raw score gates)",
        target="ai_brain.py",
        old="    if final_score > threshold:",
        new="    if float(signal_score) > threshold:",
        expect_fail=[
            "tests/test_llm_validator.py::TestThresholdAppliedAfterAdjustment::test_86_minus_2_trades",
        ],
    ),
    Mutation(
        name="M3: threshold comparison not strictly-greater (>=)",
        target="ai_brain.py",
        old="    if final_score > threshold:",
        new="    if final_score >= threshold:",
        expect_fail=[
            "tests/test_llm_validator.py::TestThresholdAppliedAfterAdjustment::test_score_exactly_at_threshold_does_not_trade",
        ],
    ),
    Mutation(
        name="M4: legacy confidence floored at 60 again",
        target="ai_brain.py",
        old="""    conf = _numeric_or_none(parsed.get("confidence"))
    if conf is None:
        return _safe_hold(
            f"LLM returned non-numeric confidence "
            f"{parsed.get('confidence')!r} — HOLD.",
            snapshot,
        )""",
        new="""    conf = _numeric_or_none(parsed.get("confidence"))
    if conf is None:
        conf = 0
    conf = max(60, min(95, conf))""",
        expect_fail=[
            "tests/test_llm_validator.py::TestLegacyConfidenceHasNoFloor::test_low_confidence_is_not_floored",
            "tests/test_llm_validator.py::TestLegacyConfidenceHasNoFloor::test_zero_confidence_is_not_floored",
        ],
    ),
    Mutation(
        name="M5: price tolerance loosened back to 3%",
        target="config.yaml",
        old="    price: 0.002        # 0.2 % of the computed close",
        new="    price: 0.03         # 3 % of the computed close",
        expect_fail=[
            "tests/test_llm_validator.py::TestHallucinationPerField::test_price_outside_tolerance_is_hallucination",
        ],
    ),
    Mutation(
        name="M6: RSI tolerance treated as relative instead of absolute",
        target="ai_brain.py",
        old="""    if absolute:
        return abs(cited - truth) <= tol""",
        new="""    if absolute:
        return abs(cited - truth) / abs(truth) <= tol""",
        expect_fail=[
            "tests/test_llm_validator.py::TestHallucinationPerField::test_rsi_outside_tolerance_is_hallucination",
            "tests/test_llm_validator.py::TestHallucinationPerField::test_rsi_is_absolute_not_relative",
        ],
    ),
    Mutation(
        name="M7: order_send runs BEFORE order_check",
        target="execution.py",
        old="""    # Pre-flight check via order_check before sending
    check = mt5.order_check(request)""",
        new="""    # MUTATION: send before checking
    mt5.order_send(request)
    check = mt5.order_check(request)""",
        expect_fail=[
            "tests/test_execution_guards.py::TestOrderCheckRunsBeforeSend::test_order_check_is_called_before_order_send",
            "tests/test_execution_guards.py::TestOrderCheckRunsBeforeSend::test_order_check_failure_never_calls_order_send",
        ],
    ),
    Mutation(
        name="M8: order_check failure no longer blocks order_send",
        target="execution.py",
        old="""    if check is None or check.retcode not in (0, mt5.TRADE_RETCODE_DONE):
        err = mt5.last_error() if check is None else f"Code {check.retcode}: {check.comment}"
        return _reject(f"Pre-flight check failed: {err}")""",
        new="""    if False:
        err = ""
        return _reject(f"Pre-flight check failed: {err}")""",
        expect_fail=[
            "tests/test_execution_guards.py::TestOrderCheckRunsBeforeSend::test_order_check_failure_never_calls_order_send",
            "tests/test_execution_guards.py::TestOrderCheckRunsBeforeSend::test_order_check_returning_none_never_calls_order_send",
        ],
    ),
    Mutation(
        name="M9: symbol whitelist gate removed",
        target="execution.py",
        old="""    allowed, reason = _check_symbol_allowed(symbol)
    if not allowed:
        return _reject(reason)""",
        new="""    allowed, reason = True, ""
    if not allowed:
        return _reject(reason)""",
        expect_fail=[
            "tests/test_execution_guards.py::TestSymbolWhitelistEnforcement::test_non_whitelisted_symbol_is_rejected_before_any_order_call",
        ],
    ),
    Mutation(
        name="M10: stop-loss requirement removed",
        target="execution.py",
        old="""    if sl_pips is None or sl_pips <= 0:
        return _reject(
            f"Refusing to place {decision} {symbol} without a stop loss "
            "(sl_pips must be > 0). Every order requires a broker-side SL."
        )""",
        new="""    if False:
        return _reject("unreachable")""",
        expect_fail=[
            "tests/test_execution_guards.py::TestStopLossIsMandatory::test_order_without_sl_is_rejected[0]",
            "tests/test_execution_guards.py::TestStopLossIsMandatory::test_order_without_sl_is_rejected[None]",
        ],
    ),
    Mutation(
        name="M11: lot-below-minimum no longer rejected",
        target="execution.py",
        old="""    normalized_lot, lot_err = _normalize_lot(lot_size, symbol_info)
    if lot_err:
        return _reject(lot_err)""",
        new="""    normalized_lot, lot_err = _normalize_lot(lot_size, symbol_info)
    if lot_err:
        normalized_lot = symbol_info.volume_min""",
        expect_fail=[
            "tests/test_execution_guards.py::TestLotGuards::test_lot_below_volume_min_is_rejected_without_sending",
        ],
    ),
    Mutation(
        name="M12: dropdown offers Unclassified / non-whitelisted symbols",
        target="data_engine.py",
        old="""        if sym.name.upper() not in allowed:
            continue""",
        new="""        if False:
            continue""",
        expect_fail=[
            "tests/test_execution_guards.py::TestDropdownNeverOffersUntradableSymbols::test_non_whitelisted_is_never_offered",
        ],
    ),
    Mutation(
        # Making blocking_checks return every check means an informational skip
        # holds the verdict at INCOMPLETE. The tests that catch this are the
        # optional-class ones, which legitimately produce informational skips.
        # The plain-skip tests still pass: their skips are already blocking.
        name="M13: an informational skip counts as a pass",
        target="scripts/demo_verification.py",
        old="""        return [c for c in self.checks if c.blocking]""",
        new="""        return list(self.checks)""",
        expect_fail=[
            "tests/test_demo_verification.py::TestAssetClassPolicy::test_missing_optional_class_yields_pass",
            "tests/test_demo_verification.py::TestAssetClassPolicy::test_stale_feed_on_permitted_class_is_non_blocking",
        ],
    ),
    Mutation(
        name="M14: empty check log counts as a pass",
        target="scripts/demo_verification.py",
        old="""        if self.has_skips or not self.blocking_checks:
            return VERDICT_INCOMPLETE""",
        new="""        if self.has_skips:
            return VERDICT_INCOMPLETE""",
        expect_fail=[
            "tests/test_demo_verification.py::TestVerdictNeverPassesWithSkips::test_empty_log_is_not_a_pass",
        ],
    ),
    Mutation(
        # Breaking try:/finally: pair-wise replacement is not possible (the
        # finally must bind a try), so instead break the guard inside the
        # finally that decides whether to close. With it disabled, an opened
        # position is never closed and the failure is never recorded.
        name="M15: opened demo order is never closed (finally guard disabled)",
        target="scripts/demo_verification.py",
        old="""        if opened_ticket:
            try:
                time.sleep(1)""",
        new="""        if False:
            try:
                time.sleep(1)""",
        expect_fail=[
            "tests/test_demo_verification.py::TestDemoOrderAlwaysCloses::test_order_opened_and_closed",
            "tests/test_demo_verification.py::TestDemoOrderAlwaysCloses::test_close_failure_is_recorded_not_swallowed",
        ],
    ),
    Mutation(
        name="M16: account number no longer masked",
        target="scripts/demo_verification.py",
        old="""    return "*" * (len(text) - 3) + text[-3:]""",
        new="""    return text""",
        expect_fail=[
            "tests/test_demo_verification.py::TestMasking::test_account_masks_all_but_last_three",
        ],
    ),
    Mutation(
        name="M17: secret scanner credential pattern broken by leading word boundary",
        target="scripts/secret_scan.py",
        old="""            r"(?i)(?:password|passwd|pwd|secret|api[_-]?key|apikey|token|"
            r"access[_-]?key|client[_-]?secret)\\b\"""",
        new="""            r"(?i)\\b(?:password|passwd|pwd|secret|api[_-]?key|apikey|token|"
            r"access[_-]?key|client[_-]?secret)\\b\"""",
        expect_fail=[
            "tests/test_secret_scan.py::TestScannerSelfTest::test_underscore_prefixed_key_is_detected",
        ],
    ),
    Mutation(
        name="M18: scanner report leaks the raw secret on the source line",
        target="scripts/secret_scan.py",
        old="""                out.append(f"    line   : {redact_line(h.line)}")""",
        new="""                out.append(f"    line   : {h.line}")""",
        expect_fail=[
            "tests/test_secret_scan.py::TestMasking::test_report_never_contains_raw_secret",
        ],
    ),
    Mutation(
        name="M19: margin level always displayed (threshold ignored)",
        target="formatting.py",
        old="""    if used < threshold:
        return False""",
        new="""    if False:
        return False""",
        expect_fail=[
            "tests/test_formatting_and_mode.py::TestFormatMarginLevel::test_margin_0_2_is_na",
            "tests/test_formatting_and_mode.py::TestFormatMarginLevel::test_margin_0_99_is_na",
        ],
    ),
    Mutation(
        name="M20: margin threshold hardcoded to 0 instead of config",
        target="formatting.py",
        old="""    if threshold is None:
        threshold = _margin_threshold()""",
        new="""    if threshold is None:
        threshold = 0.0""",
        expect_fail=[
            "tests/test_formatting_and_mode.py::TestFormatMarginLevel::test_threshold_comes_from_config",
            "tests/test_formatting_and_mode.py::TestFormatMarginLevel::test_config_threshold_drives_behaviour",
        ],
    ),
    Mutation(
        name="M21: currency symbol hardcoded to $ again",
        target="formatting.py",
        old="""    return CURRENCY_SYMBOLS.get(str(currency).strip().upper(), "")""",
        new="""    return "$" if currency else \"\"""",
        expect_fail=[
            "tests/test_formatting_and_mode.py::TestCurrencyFormatting::test_format_money_uses_account_currency",
            "tests/test_formatting_and_mode.py::TestCurrencyFormatting::test_unknown_currency_has_no_symbol",
        ],
    ),
    Mutation(
        name="M22: demo-mode guard removed from execute_trade",
        target="execution.py",
        old="""    demo_ok, demo_reason = require_demo_account()
    if not demo_ok:
        return {
            "success": False,
            "status": "REFUSED",
            "message": f"Refusing to place order: {demo_reason}",
            "ticket": None,
        }""",
        new="""    demo_ok, demo_reason = True, ""
    if not demo_ok:
        return {
            "success": False,
            "status": "REFUSED",
            "message": f"Refusing to place order: {demo_reason}",
            "ticket": None,
        }""",
        expect_fail=[
            "tests/test_formatting_and_mode.py::TestNoOrderSentOnLiveAccount::test_execute_trade_refused[2-REAL]",
            "tests/test_formatting_and_mode.py::TestNoOrderSentOnLiveAccount::test_execute_trade_refused[1-CONTEST]",
        ],
    ),
    Mutation(
        name="M23: demo-mode guard removed from close_position",
        target="execution.py",
        old="""    demo_ok, demo_reason = require_demo_account()
    if not demo_ok:
        return {
            "success": False,
            "status": "REFUSED",
            "message": f"Refusing to close: {demo_reason}",
            "ticket": None,
        }""",
        new="""    demo_ok, demo_reason = True, ""
    if not demo_ok:
        return {
            "success": False,
            "status": "REFUSED",
            "message": f"Refusing to close: {demo_reason}",
            "ticket": None,
        }""",
        expect_fail=[
            "tests/test_formatting_and_mode.py::TestNoOrderSentOnLiveAccount::test_close_position_refused_on_real_account",
        ],
    ),
    Mutation(
        name="M24: unknown account mode assumed safe (fails open)",
        target="execution.py",
        old="""    if trade_mode == demo_mode:
        return True, \"\"""",
        new="""    if trade_mode is not None:
        return True, \"\"""",
        expect_fail=[
            "tests/test_formatting_and_mode.py::TestDemoModeGuard::test_real_account_is_refused",
            "tests/test_formatting_and_mode.py::TestDemoModeGuard::test_unknown_mode_is_refused",
        ],
    ),
    Mutation(
        # There are two independent account-mode gates: one in run() and one
        # inside check_demo_order immediately before the send. Removing only one
        # is unobservable, which is defence in depth working as intended. This
        # mutation removes BOTH, which is the real "no gate at all" failure.
        name="M25: verification script has NO account-mode gate at all",
        target="scripts/demo_verification.py",
        old="""    # Refuse outright on a non-demo account: everything below may send orders.
    if not check_demo_account_mode(mt5_mod, log):
        return log""",
        new="""    # Refuse outright on a non-demo account: everything below may send orders.
    if False:
        return log""",
        expect_fail=[
            # Drives run() end to end, so removing the gate call site is caught.
            "tests/test_demo_verification.py::TestAccountModeGuard::test_full_pipeline_sends_nothing_on_non_demo_account[2-REAL]",
        ],
    also=[
        Mutation(
            name="M25b: inner guard in check_demo_order",
            target="scripts/demo_verification.py",
            old="""    if trade_mode != demo_mode:
        log.skip("demo order", f"not a demo account (trade_mode={trade_mode})")""",
            new="""    if False:
        log.skip("demo order", f"not a demo account (trade_mode={trade_mode})")""",
            expect_fail=[],
        ),
    ],
),
    Mutation(
        name="M26: account number masking removed",
        target="formatting.py",
        old="""    return "*" * (len(text) - 3) + text[-3:]""",
        new="""    return text""",
        expect_fail=[
            "tests/test_formatting_and_mode.py::TestMaskAccount::test_masks_all_but_last_three",
        ],
    ),
    Mutation(
        name="M27: scanner stops checking .git/config for remote-URL tokens",
        target="scripts/secret_scan.py",
        old="""    config = repo / ".git" / "config"
    if not config.exists():
        return []""",
        new="""    config = repo / ".git" / "config"
    if True:
        return []""",
        expect_fail=[
            "tests/test_secret_scan.py::TestGitConfigScanning::test_token_in_remote_url_is_detected",
            "tests/test_secret_scan.py::TestGitConfigScanning::test_openrouter_token_in_remote_url_is_detected",
        ],
    ),
    Mutation(
        name="M28: .git/config hits are not surfaced in the report",
        target="scripts/secret_scan.py",
        old="""    config_hits = scan_git_config(repo)""",
        new="""    config_hits = []""",
        # Must target the test that drives main(). The earlier test passes hits
        # straight into render_report, so it cannot observe this mutation.
        expect_fail=[
            "tests/test_secret_scan.py::TestGitConfigScanning::test_main_reports_git_config_hits",
        ],
    ),
    Mutation(
        name="M29: real credentials excused again (synthetic check disabled)",
        target="scripts/verify_history_clean.py",
        old="""    if is_synthetic(value):
        return 0, 0""",
        new="""    if False:
        return 0, 0""",
        expect_fail=[
            "tests/test_secret_scan.py::TestSyntheticValueAllowlist::test_synthetic_value_in_a_test_file_is_not_a_leak",
        ],
    ),
    Mutation(
        name="M30: every value treated as synthetic (fails open)",
        target="scripts/verify_history_clean.py",
        old="""    stripped = value.strip().strip('"').strip("'")
    if stripped in SYNTHETIC_TEST_VALUES:
        return True""",
        new="""    stripped = value.strip().strip('"').strip("'")
    if True:
        return True""",
        expect_fail=[
            "tests/test_secret_scan.py::TestSyntheticValueAllowlist::test_values_not_on_the_allowlist_are_not_synthetic[AKIAIOSFODNN7NOTLISTED]",
            "tests/test_secret_scan.py::TestSyntheticValueAllowlist::test_real_secret_in_a_test_file_is_still_a_leak",
        ],
    ),
    Mutation(
        name="M31: synthetic allowlist widened to excuse everything",
        target="scripts/verify_history_clean.py",
        old="""def is_synthetic(value: str) -> bool:""",
        new="""def is_synthetic(value: str) -> bool:
    return True


def _unused_is_synthetic(value: str) -> bool:""",
        expect_fail=[
            "tests/test_secret_scan.py::TestSyntheticValueAllowlist::test_values_not_on_the_allowlist_are_not_synthetic[AKIAIOSFODNN7NOTLISTED]",
            "tests/test_secret_scan.py::TestSyntheticAllowlistIsSafe::test_allowlist_entries_are_declared_canaries_or_placeholders",
        ],
    ),
    Mutation(
        name="M32: undeclared dependency (PyYAML) removed from requirements.txt",
        target="requirements.txt",
        old="""PyYAML==6.0.3""",
        new="""# PyYAML removed (MUTATION)""",
        expect_fail=[
            "tests/test_dependencies.py::TestRequirementsAreComplete::test_known_config_dependencies_are_declared[yaml]",
            "tests/test_dependencies.py::TestRequirementsAreComplete::test_every_third_party_import_is_declared[config_loader.py]",
        ],
    ),
    Mutation(
        name="M33: python-dotenv removed from requirements.txt",
        target="requirements.txt",
        old="""python-dotenv==1.2.3""",
        new="""# python-dotenv removed (MUTATION)""",
        expect_fail=[
            "tests/test_dependencies.py::TestRequirementsAreComplete::test_known_config_dependencies_are_declared[dotenv]",
        ],
    ),
    Mutation(
        name="M34: dependency test stops checking requirements.txt",
        target="tests/test_dependencies.py",
        old="""def _declared_distributions() -> set[str]:
    if not REQUIREMENTS.exists():
        return set()""",
        new="""def _declared_distributions() -> set[str]:
    return set()""",
        expect_fail=[
            "tests/test_dependencies.py::TestRequirementsAreComplete::test_known_config_dependencies_are_declared[yaml]",
        ],
    ),
    Mutation(
        # F1.3: these three originally mutated inline code in app.py and were
        # checked by tests that read app.py's source text. The computations now
        # live in metrics.py and are tested by calling them, so the mutations
        # target metrics.py and the behaviour tests.
        name="M35: open P&L sign discarded (loss shown as gain)",
        target="metrics.py",
        old="""    return sum(_finite(p.get("profit"), 0.0) for p in positions or [])""",
        new="""    return sum(abs(_finite(p.get("profit"), 0.0)) for p in positions or [])""",
        expect_fail=[
            "tests/test_metrics.py::TestNetOpenPnl::test_losses_sum_to_a_negative_total",
        ],
    ),
    Mutation(
        name="M38: hidden symbols skipped again (no symbol_select activation)",
        target="scripts/demo_verification.py",
        old="""    for sym in hidden_candidates:
        name, reason = _probe_symbol(mt5_mod, sym, select=True)
        if name:
            return name, \"\"""",
        new="""    for sym in hidden_candidates:
        continue""",
        expect_fail=[
            "tests/test_demo_verification.py::TestHiddenSymbolDiscovery::test_hidden_forex_symbol_is_activated_and_picked",
            "tests/test_demo_verification.py::TestHiddenSymbolDiscovery::test_hidden_indices_symbol_is_discovered",
        ],
    ),
    Mutation(
        name="M39: symbol_select never called when probing a hidden symbol",
        target="scripts/demo_verification.py",
        old="""        try:
            mt5_mod.symbol_select(name, True)
        except Exception:
            pass""",
        new="""        pass""",
        expect_fail=[
            "tests/test_demo_verification.py::TestHiddenSymbolDiscovery::test_hidden_symbol_unlocks_a_tick",
        ],
    ),
    Mutation(
        name="M40: HOLD confidence restored to a fabricated 60",
        target="ai_brain.py",
        old="""    "decision": "HOLD",
    "confidence": None,""",
        new="""    "decision": "HOLD",
    "confidence": 60,""",
        expect_fail=[
            "tests/test_metrics.py::TestSafeHoldCarriesNoConfidence::test_safe_hold_confidence_is_none",
            "tests/test_metrics.py::TestSafeHoldCarriesNoConfidence::test_every_fail_safe_path_has_no_confidence",
        ],
    ),
    Mutation(
        name="M41: HOLD badge renders a numeric confidence",
        target="metrics.py",
        old="""    if decision and str(decision).upper() == "HOLD":
        return NO_SCORE""",
        new="""    if False:
        return NO_SCORE""",
        expect_fail=[
            "tests/test_metrics.py::TestFormatLlmConfidence::test_hold_shows_no_score",
            "tests/test_metrics.py::TestFormatLlmConfidence::test_hold_ignores_any_numeric_value",
        ],
    ),
    Mutation(
        name="M42: open risk computed from P&L instead of entry-to-stop",
        target="metrics.py",
        old="""            if tick_value > 0 and tick_size > 0:
                ticks = abs(entry - sl) / tick_size
                risk_amount = ticks * tick_value * volume
                protected = True""",
        new="""            if tick_value > 0 and tick_size > 0:
                risk_amount = abs(_finite(pos.get("profit"), 0.0))
                protected = True""",
        expect_fail=[
            "tests/test_metrics.py::TestComputeOpenRisk::test_risk_is_distance_from_entry_to_stop",
            "tests/test_metrics.py::TestComputeOpenRisk::test_risk_is_independent_of_profit",
        ],
    ),
    Mutation(
        name="M43: unprotected position folded in as zero risk",
        target="metrics.py",
        old="""        if not protected:
            unprotected.append(row)""",
        new="""        if False:
            unprotected.append(row)""",
        expect_fail=[
            "tests/test_metrics.py::TestUnprotectedPositions::test_position_without_sl_is_unprotected",
            "tests/test_metrics.py::TestUnprotectedPositions::test_unprotected_is_counted_separately_not_as_zero_risk",
        ],
    ),
    Mutation(
        name="M44: unknown risk shown as calm green instead of amber",
        target="metrics.py",
        old="""    if summary.has_unprotected:
        return "#F59E0B"   # amber""",
        new="""    if False:
        return "#F59E0B"   # amber""",
        expect_fail=[
            "tests/test_metrics.py::TestRiskColour::test_unprotected_positions_are_amber",
        ],
    ),
    Mutation(
        name="M45: net open P&L sums magnitudes again",
        target="metrics.py",
        old="""    return sum(_finite(p.get("profit"), 0.0) for p in positions or [])""",
        new="""    return sum(abs(_finite(p.get("profit"), 0.0)) for p in positions or [])""",
        expect_fail=[
            "tests/test_metrics.py::TestNetOpenPnl::test_a_loss_is_never_rendered_as_a_gain",
            "tests/test_metrics.py::TestNetOpenPnl::test_losses_sum_to_a_negative_total",
        ],
    ),
    Mutation(
        name="M46: informational skips hold the verdict at INCOMPLETE",
        target="scripts/demo_verification.py",
        old="""        return any(c.status == SKIP for c in self.blocking_checks)""",
        new="""        return any(c.status == SKIP for c in self.checks)""",
        expect_fail=[
            "tests/test_demo_verification.py::TestAssetClassPolicy::test_missing_optional_class_yields_pass",
            "tests/test_demo_verification.py::TestAssetClassPolicy::test_stale_feed_on_permitted_class_is_non_blocking",
        ],
    ),
    Mutation(
        name="M47: optional asset class treated as required",
        target="config_loader.py",
        old="""    def is_required(self, asset_class: str) -> bool:
        \"\"\"Default to required when a class is not configured.

        Failing closed matters: an unconfigured class must not silently become
        optional, or adding a new class would quietly stop being verified.
        \"\"\"
        policy = self.asset_classes.get(asset_class)
        if policy is None:
            return True
        return policy.required""",
        new="""    def is_required(self, asset_class: str) -> bool:
        return True""",
        expect_fail=[
            "tests/test_demo_verification.py::TestAssetClassPolicy::test_config_marks_crypto_optional",
            "tests/test_demo_verification.py::TestAssetClassPolicy::test_missing_optional_class_yields_pass",
        ],
    ),
    Mutation(
        name="M48: stale feed permitted regardless of class",
        target="scripts/demo_verification.py",
        old="""            if stale and stale_ok:""",
        new="""            if stale:""",
        expect_fail=[
            "tests/test_demo_verification.py::TestAssetClassPolicy::test_stale_feed_on_non_permitted_class_still_blocks",
        ],
    ),
    Mutation(
        name="M49: trading path honours the verification stale relaxation",
        target="data_engine.py",
        old="""    if tick_age > max_tick_age_seconds:""",
        new="""    if False:""",
        expect_fail=[
            "tests/test_demo_verification.py::TestTradingPathStalenessUnaffected::test_live_quote_rejects_a_stale_tick",
        ],
    ),
    Mutation(
        name="M50: optional class excuses a wrong spread value",
        target="scripts/demo_verification.py",
        old="""            # A wrong spread value is a real defect regardless of class.
            log.fail(label, f"{sym}: got {actual}, expected {expected}")""",
        new="""            log.skip(label, f"{sym}: got {actual}, expected {expected}",
                     blocking=required)""",
        expect_fail=[
            "tests/test_demo_verification.py::TestAssetClassPolicy::test_wrong_spread_fails_even_on_an_optional_class",
        ],
    ),
    Mutation(
        # The IndentationError that shipped: a statement dedented out of its
        # block, which ast.parse tolerates inside an f-string but compile() does not.
        name="M51: app.py statement dedented out of its function",
        target="app.py",
        old="""    dec_class = dec.lower()
    badge_cls = {"BUY": "green", "SELL": "red", "HOLD": "amber"}.get(dec, "neutral")""",
        new="""dec_class = dec.lower()
    badge_cls = {"BUY": "green", "SELL": "red", "HOLD": "amber"}.get(dec, "neutral")""",
        expect_fail=[
            "tests/test_dependencies.py::TestAllSourceFilesCompile::test_app_compiles_specifically",
            "tests/test_dependencies.py::TestAllSourceFilesCompile::test_no_indentation_error_survives_a_compile",
        ],
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # Phase 1 engine safety rules (design doc section 9)
    # ─────────────────────────────────────────────────────────────────────────
    Mutation(
        # Mutating this repo's own config.yaml proves nothing: the guard is that
        # an ABSENT mode falls back to PAPER. An earlier version flipped the
        # literal to LIVE and no test failed, which showed the test was not
        # guarding the default at all.
        name="M52: absent mode defaults to LIVE instead of PAPER",
        target="engine/config.py",
        old="""    mode = str(raw.get("mode", MODE_PAPER)).upper()""",
        new="""    mode = str(raw.get("mode", MODE_LIVE)).upper()""",
        expect_fail=[
            "tests/test_engine/test_executor_commands.py::TestEngineConfig::test_mode_defaults_to_paper_when_absent",
        ],
    ),
    Mutation(
        name="M52b: repo config.yaml declares LIVE",
        target="config.yaml",
        old="""mode: PAPER""",
        new="""mode: LIVE""",
        expect_fail=[
            "tests/test_engine/test_executor_commands.py::TestEngineConfig::test_loads_the_real_config_is_paper",
        ],
    ),
    Mutation(
        name="M53: LIVE constructible from the config flag alone",
        target="engine/executor.py",
        old="""    if mode == MODE_LIVE:
        if not live_confirmed:
            raise ExecutionRejected(
                "Refusing to construct a LiveExecutor: live confirmation has "
                "not been supplied."
            )""",
        new="""    if mode == MODE_LIVE:
        pass""",
        expect_fail=[
            "tests/test_engine/test_executor_commands.py::TestModeSelection::test_live_requires_confirmation",
        ],
    ),
    Mutation(
        name="M54: orders permitted without a stop loss",
        target="engine/executor.py",
        old="""        if request.sl is None or request.sl <= 0:
            raise ExecutionRejected(""",
        new="""        if False:
            raise ExecutionRejected(""",
        expect_fail=[
            "tests/test_engine/test_executor_commands.py::TestExecutorSafetyGates::test_order_without_sl_is_rejected[0]",
            "tests/test_engine/test_executor_commands.py::TestExecutorSafetyGates::test_order_without_sl_is_rejected[None]",
        ],
    ),
    Mutation(
name="M55: stop-loss check removed from the executor",
        target="engine/executor.py",
        old="""        if request.sl is None or request.sl <= 0:
            raise ExecutionRejected(
                f"Refusing to submit {request.side} {request.symbol}: no stop "
                f"loss. Every order requires a broker-side SL."
            )""",
        new="""        if False:
            raise ExecutionRejected("unreachable in this mutation")""",
        expect_fail=[
            "tests/test_engine/test_executor_commands.py::TestExecutorSafetyGates::test_order_without_sl_is_rejected[0]",
            "tests/test_engine/test_executor_commands.py::TestExecutorSafetyGates::test_order_without_sl_is_rejected[None]",
        ],
    ),
    Mutation(
        # Originally disabled the Phase 1 stub that approved nothing. The stub
        # is gone in Phase 2, so this now targets the property it stood in for:
        # an order inside every limit must actually be approved. Without this,
        # a gate that blocked everything would still satisfy the limit tests.
        name="M56: gate refuses orders that are inside every limit",
        target="engine/risk.py",
        old="""        return self._finish(intent, account, not violations, violations, reasons,
                            risk_amount, risk_pct, projected_pct)""",
        new="""        violations.append("MUTATION: refuse everything")
        return self._finish(intent, account, False, violations, reasons,
                            risk_amount, risk_pct, projected_pct)""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestPerTradeRisk::test_within_limit_is_approved",
            "tests/test_engine/test_risk.py::TestTotalExposure::test_within_limit_allows",
            "tests/test_engine/test_executor_commands.py::TestRiskGateIsNowReal::test_approves_a_trade_inside_the_limits",
        ],
    ),
    Mutation(
        name="M57: EMERGENCY_STOP no longer blocks submissions",
        target="engine/executor.py",
        old="""        if state == STATE_EMERGENCY_STOP:
            raise ExecutionRejected(
                "Refusing to submit: engine is in EMERGENCY_STOP."
            )""",
        new="""        if False:
            raise ExecutionRejected(
                "Refusing to submit: engine is in EMERGENCY_STOP."
            )""",
        expect_fail=[
            "tests/test_engine/test_executor_commands.py::TestExecutorSafetyGates::test_emergency_blocks_submission",
        ],
    ),
    Mutation(
        name="M58: EMERGENCY_STOP becomes escapable",
        target="engine/state.py",
        old="""    STATE_EMERGENCY_STOP: (
        Rule(STATE_OFF, "RESET_EMERGENCY"),
    ),""",
        new="""    STATE_EMERGENCY_STOP: (
        Rule(STATE_OFF, "RESET_EMERGENCY"),
        Rule(STATE_IDLE, "DISABLE_AI"),
    ),""",
        expect_fail=[
            "tests/test_engine/test_state.py::TestEmergencyStopIsTerminal::test_nothing_leaves_emergency_except_reset",
            "tests/test_engine/test_state.py::TestEmergencyStopIsTerminal::test_disabling_ai_does_not_escape_emergency",
        ],
    ),
    Mutation(
        name="M59: EMERGENCY_STOP reset no longer requires confirmation",
        target="engine/state.py",
        old="""            expected = "RESET"
            supplied = str(params.get("confirmation", ""))
            if supplied != expected:
                return False, (
                    f"RESET_EMERGENCY requires confirmation={expected!r}"
                )""",
        new="""            supplied = str(params.get("confirmation", ""))""",
        expect_fail=[
            "tests/test_engine/test_state.py::TestResetEmergency::test_requires_the_exact_confirmation_phrase",
            "tests/test_engine/test_state.py::TestResetEmergency::test_missing_confirmation_is_refused",
        ],
    ),
    Mutation(
        name="M60: gateway timeout resolves to None instead of raising",
        target="engine/gateway.py",
        old="""        if not self.done.wait(timeout=self.timeout):
            raise MT5Timeout(self.name, self.timeout)""",
        new="""        self.done.wait(timeout=self.timeout)""",
        expect_fail=[
            "tests/test_engine/test_gateway.py::TestTimeoutRaises::test_raw_call_timeout_raises",
            "tests/test_engine/test_gateway.py::TestTimeoutRaises::test_mt5timeout_is_a_timeouterror",
        ],
    ),
    Mutation(
        name="M61: timed-out orders retried without reconciliation",
        target="engine/gateway.py",
        old="""        if positions:
            ticket = positions[0].get("ticket")""",
        new="""        if False:
            ticket = None""",
        expect_fail=[
            "tests/test_engine/test_gateway.py::TestOrderReconciliation::test_timeout_that_executed_is_recovered_not_resent",
        ],
    ),
    Mutation(
        name="M62: non-gateway module may import MetaTrader5",
        target="engine/executor.py",
        old="""from engine.gateway import Gateway, OrderOutcome, OrderResult""",
        new="""import MetaTrader5

from engine.gateway import Gateway, OrderOutcome, OrderResult""",
        expect_fail=[
            "tests/test_engine/test_boundaries.py::TestOnlyTheGatewayTouchesMt5::test_engine_module_does_not_import_mt5[executor.py]",
        ],
    ),
    Mutation(
        # Found against the live terminal: MT5 returns None when positions_get
        # is given an explicit None. The gateway always passed both, so it read
        # zero positions while two were open, both carrying our magic number.
        name="M63: positions_get called with explicit None arguments again",
        target="engine/gateway.py",
        old="""        func = self._mt5_or_raise().positions_get
        if ticket is not None:
            result = self.call("positions_get", func, ticket=ticket)
        elif symbol is not None:
            result = self.call("positions_get", func, symbol=symbol)
        else:
            result = self.call("positions_get", func)""",
        new="""        func = self._mt5_or_raise().positions_get
        result = self.call("positions_get", func, symbol=symbol, ticket=ticket)""",
        expect_fail=[
            "tests/test_engine/test_gateway.py::TestPositionsGetArgumentQuirk::test_bare_call_returns_positions",
            "tests/test_engine/test_gateway.py::TestPositionsGetArgumentQuirk::test_reconciliation_sees_real_positions_with_our_magic",
        ],
    ),
    Mutation(
        name="M64: read helper dereferences an unresolved mt5 module",
        target="engine/gateway.py",
        old="""    def account_info(self) -> Any:
        return self.call("account_info", self._mt5_or_raise().account_info)""",
        new="""    def account_info(self) -> Any:
        return self.call("account_info", self._mt5.account_info)""",
        expect_fail=[
            "tests/test_engine/test_gateway.py::TestUninjectedGatewayResolvesMt5::test_read_helpers_do_not_touch_a_none_module",
        ],
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2: risk engine limits. Each limit must prove its test can fail.
    # ─────────────────────────────────────────────────────────────────────────
    Mutation(
        name="M65: per-trade risk limit not enforced",
        target="engine/risk.py",
        old="""            check(within(risk_amount, equity * self.limits.risk_per_trade),""",
        new="""            check(True,  # MUTATION: per-trade limit removed""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestPerTradeRisk::test_above_limit_is_blocked",
            "tests/test_engine/test_risk.py::TestPerTradeRisk::test_a_hair_over_the_limit_is_still_refused",
        ],
    ),
    Mutation(
        name="M66: total exposure limit not enforced",
        target="engine/risk.py",
        old="""                check(within(projected, equity * self.limits.max_total_exposure),""",
        new="""                check(True,  # MUTATION: exposure limit removed""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestTotalExposure::test_projected_over_limit_blocks",
        ],
    ),
    Mutation(
        name="M67: daily loss limit not enforced",
        target="engine/risk.py",
        old="""                check(within(loss_pct, self.limits.daily_loss_limit_pct),""",
        new="""                check(True,  # MUTATION: daily loss limit removed""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestDailyLoss::test_beyond_limit_blocks",
        ],
    ),
    Mutation(
        name="M68: drawdown limit not enforced",
        target="engine/risk.py",
        old="""                check(within(drawdown_pct, self.limits.max_drawdown_pct),""",
        new="""                check(True,  # MUTATION: drawdown limit removed""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestDrawdown::test_beyond_limit_blocks",
        ],
    ),
    Mutation(
        name="M69: open trade count limit not enforced",
        target="engine/risk.py",
        old="""        check(account.open_trade_count < self.limits.max_open_trades,""",
        new="""        check(True,  # MUTATION: open trade limit removed""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestOpenTradeCount::test_at_limit_blocks",
        ],
    ),
    Mutation(
        name="M70: missing equity fails open instead of closed",
        target="engine/risk.py",
        old="""        check(has_equity,
              "equity is unknown; percentages cannot be computed, so the "
              "order is blocked rather than sized against an assumption")""",
        new="""        check(True, "MUTATION: unknown equity no longer blocks")""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestFailClosed::test_missing_equity_blocks",
            "tests/test_engine/test_risk.py::TestFailClosed::test_missing_equity_names_equity_and_its_consequences",
        ],
    ),
    Mutation(
        name="M71: unprotected positions no longer block",
        target="engine/risk.py",
        old="""        if account.unprotected_positions > 0:""",
        new="""        if False:  # MUTATION: unprotected positions excused""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestUnprotectedPositions::test_any_unprotected_position_blocks",
        ],
    ),
    Mutation(
        name="M72: order without a stop loss is permitted",
        target="engine/risk.py",
        old="""        check(intent.has_stop,
              "order has no stop loss, so its worst case is undefined")""",
        new="""        check(True, "MUTATION: missing stop permitted")""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestFailClosed::test_order_without_stop_blocks",
        ],
    ),
    Mutation(
        name="M73: drawdown halt clears on the daily-loss path",
        target="engine/risk.py",
        old="""        if self.halt_kind != "daily_loss":
            return""",
        new="""        if False:
            return""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestHalts::test_drawdown_halt_does_not_clear_automatically",
            "tests/test_engine/test_risk.py::TestHalts::test_manual_halt_is_not_cleared_by_the_daily_path",
        ],
    ),
    Mutation(
        name="M74: spread limit not enforced",
        target="engine/risk.py",
        old="""            check(intent.spread_points <= cap,""",
        new="""            check(True,  # MUTATION: spread limit removed""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestSpreadLimit::test_above_limit_blocks",
            "tests/test_engine/test_risk.py::TestSpreadLimit::test_per_symbol_override_can_be_tighter",
        ],
    ),
    Mutation(
        name="M75: risk amount computed without tick conversion",
        target="engine/risk.py",
        old="""        return (abs(self.entry_price - self.stop_loss)
                / self.tick_size
                * self.tick_value
                * self.volume)""",
        new="""        return abs(self.entry_price - self.stop_loss) * self.volume""",
        expect_fail=[
            "tests/test_engine/test_risk.py::TestRiskAmount::test_risk_is_entry_to_stop_distance",
            "tests/test_engine/test_risk.py::TestPerTradeRisk::test_base_risk_amount",
        ],
    ),
    # ─────────────────────────────────────────────────────────────────────────
    # Phase 2: gate wiring. These prove the executor cannot bypass the gate.
    # ─────────────────────────────────────────────────────────────────────────
    Mutation(
        name="M76: executor accepts a bare True as risk approval",
        target="engine/executor.py",
        old="""        if isinstance(risk_approved, bool):
            raise ExecutionRejected(
                "Refusing to submit: a bare True is not risk approval. Use "
                "RiskApproval.from_decision(decision) so the gate's verdict is "
                "what authorises the order."
            )""",
        new="""        if isinstance(risk_approved, bool):
            if risk_approved:
                return
            raise ExecutionRejected(
                "Refusing to submit: the risk gate did not approve this order."
            )""",
        expect_fail=[
            "tests/test_engine/test_risk_wiring.py::TestApprovalMustComeFromTheGate::test_bare_true_is_refused",
            "tests/test_engine/test_risk_wiring.py::TestApprovalMustComeFromTheGate::test_other_truthy_values_are_refused[1]",
        ],
    ),
    Mutation(
        name="M77: risk approval check removed entirely",
        target="engine/executor.py",
        old="""        if isinstance(risk_approved, RiskApproval):
            if not risk_approved.approved:""",
        new="""        if False:
            if not risk_approved.approved:""",
        expect_fail=[
            "tests/test_engine/test_risk_wiring.py::TestApprovalMustComeFromTheGate::test_a_refusal_object_blocks",
        ],
    ),
    Mutation(
        name="M78: a position without a stop counted as zero risk",
        target="engine/risk.py",
        old="""            if stop <= 0 or entry <= 0 or not tick_value or not tick_size:
                unprotected += 1
                continue""",
        new="""            if stop <= 0 or entry <= 0 or not tick_value or not tick_size:
                continue""",
        expect_fail=[
            "tests/test_engine/test_risk_wiring.py::TestAccountStateFromPositions::test_position_without_a_stop_is_unprotected",
            "tests/test_engine/test_risk_wiring.py::TestUnprotectedBlocksEverything::test_the_live_situation_would_block",
        ],
    ),
    Mutation(
        name="M79: another magic number's position assumed measurable",
        target="engine/risk.py",
        old="""            if magic is not None and int(magic) != self.magic:
                unprotected += 1
                continue""",
        new="""            if False:
                unprotected += 1
                continue""",
        expect_fail=[
            "tests/test_engine/test_risk_wiring.py::TestAccountStateFromPositions::test_another_magic_number_is_unprotected",
        ],
    ),
    Mutation(
        name="M80: intent built without the broker tick spec",
        target="engine/risk.py",
        old="""        spec = self._spec_for(symbol)
        if not spec:
            raise RiskConfigError(
                f"{symbol}: no trade specification known for this symbol; "
                f"run probe_tick_specs.py before sizing it"
            )""",
        new="""        spec = self._spec_for(symbol) or {"trade_tick_value": 1.0,
                                          "trade_tick_size": 0.00001}""",
        expect_fail=[
            "tests/test_engine/test_risk_wiring.py::TestBuildOrderIntent::test_unknown_symbol_is_refused",
        ],
    ),
]


def _git_show(path: str) -> str | None:
    proc = subprocess.run(["git", "show", f"HEAD:{path}"],
                          cwd=str(REPO_ROOT), capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def restore(m: Mutation) -> None:
    """Restore a mutated file exactly, including line endings.

    With core.autocrlf=true, git normalises to CRLF on checkout but stores LF in
    the object database. Reconstructing the file from `git show` therefore yields
    LF, which git then reports as a permanent modification and which blocks the
    next run with 'Refusing to run: working tree is dirty'.

    `git checkout -- <path>` asks git to materialise the file itself, so the
    line endings match exactly. This is safe here because a mutation is
    deliberately temporary and the harness owns the file for its duration.
    """
    for companion in (m.also or []):
        original = getattr(companion, "_original", None)
        if original is not None:
            subprocess.run(
                ["git", "checkout", "--", companion.target],
                cwd=str(REPO_ROOT), capture_output=True,
            )

    targets = [m.target] + [c.target for c in (m.also or [])]
    subprocess.run(["git", "checkout", "--", *targets],
                   cwd=str(REPO_ROOT), capture_output=True)


def apply_mutation(m: Mutation) -> bool:
    """True when the mutation was applied (old text found and replaced).

    Companion edits are applied first; if one cannot be applied the whole
    mutation is rolled back so the tree is never left half-mutated.
    """
    path = REPO_ROOT / m.target
    original = path.read_text(encoding="utf-8")
    if m.old not in original:
        print(f"    SKIP: anchor text not found in {m.target}")
        return False

    applied_companions = []
    for companion in (m.also or []):
        cpath = REPO_ROOT / companion.target
        coriginal = cpath.read_text(encoding="utf-8")
        if companion.old not in coriginal:
            print(f"    SKIP: companion anchor not found in {companion.target}")
            for done in applied_companions:
                (REPO_ROOT / done.target).write_text(done._original, encoding="utf-8")
            return False
        cpath.write_text(coriginal.replace(companion.old, companion.new, 1),
                         encoding="utf-8")
        companion._original = coriginal
        applied_companions.append(companion)

    path.write_text(original.replace(m.old, m.new, 1), encoding="utf-8")
    return True


def restore_legacy(m: Mutation) -> None:
    """Content-based restore, kept for reference.

    Superseded by git checkout because it cannot reproduce CRLF working-tree
    files under core.autocrlf=true.
    """
    path = REPO_ROOT / m.target
    content = _git_show(m.target)
    if content is None:
        return
    path.write_text(content, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for m in MUTATIONS:
            print(f"{m.name}\n  target: {m.target}\n  expect: {len(m.expect_fail)} test(s) to fail\n")
        return 0

    dirty = subprocess.run(["git", "status", "--porcelain"],
                           cwd=str(REPO_ROOT), capture_output=True, text=True)
    if dirty.stdout.strip():
        print("Refusing to run: working tree is dirty. Commit or stash first.")
        return 2

    print("=" * 96)
    print("  MUTATION CHECK — each rule is broken on purpose; its tests MUST fail")
    print("=" * 96)
    print()

    results: list[tuple[Mutation, bool, str]] = []
    for m in MUTATIONS:
        print(f"{m.name}")
        if not apply_mutation(m):
            print("    RESULT: SKIPPED (anchor not found)")
            results.append((m, False, "SKIPPED - anchor not found"))
            print()
            continue

        code, output = _run(m.expect_fail)
        # pytest exit 1 = tests failed, which is what we WANT here.
        # Exit 4 = usage/collection error: the mutation produced invalid
        # Python rather than a behavioural break, which is a broken mutation.
        caught = code == 1
        tail = ""
        m2 = re.findall(r"(\d+) failed", output)
        if m2:
            tail = f"{m2[-1]} test(s) failed as expected"
        elif code == 0:
            tail = "NO TEST FAILED -- these tests do not actually guard this rule"
        elif code == 4:
            tail = "BROKEN MUTATION -- pytest could not collect (invalid syntax?)"
        else:
            tail = f"pytest exit {code}"

        results.append((m, caught, tail))
        print(f"    {'CAUGHT ' if caught else 'MISSED!'} {tail}")
        print()

        restore(m)

    # Everything restored?
    post = subprocess.run(["git", "status", "--porcelain"],
                          cwd=str(REPO_ROOT), capture_output=True, text=True)

    print("=" * 96)
    caught_n = sum(1 for _, c, _ in results if c)
    skipped_n = sum(1 for _, _, s in results if s.startswith("SKIPPED"))
    missed = [m.name for m, c, _ in results if not c and not _.startswith("SKIPPED")]
    print(f"  Mutations run    : {len(results)}")
    print(f"  Caught by tests  : {caught_n}")
    print(f"  Skipped          : {skipped_n}")
    print(f"  MISSED           : {len(missed)}")
    for name in missed:
        print(f"    !! {name}")
    print(f"  Tree restored    : {'yes' if not post.stdout.strip() else 'NO'}")
    if post.stdout.strip():
        print(post.stdout)
    print("=" * 96)

    return 0 if not missed and not post.stdout.strip() else 1


if __name__ == "__main__":
    sys.exit(main())