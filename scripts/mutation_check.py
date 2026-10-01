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
        name="M13: a skipped check counts as a pass",
        target="scripts/demo_verification.py",
        old="""        if self.has_skips or not self.checks:
            return VERDICT_INCOMPLETE""",
        new="""        if False:
            return VERDICT_INCOMPLETE""",
        expect_fail=[
            "tests/test_demo_verification.py::TestVerdictNeverPassesWithSkips::test_one_skip_gives_incomplete",
            "tests/test_demo_verification.py::TestSymbolDiscovery::test_missing_class_makes_verdict_incomplete",
        ],
    ),
    Mutation(
        name="M14: empty check log counts as a pass",
        target="scripts/demo_verification.py",
        old="""        if self.has_skips or not self.checks:
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
]


def _git_show(path: str) -> str | None:
    proc = subprocess.run(["git", "show", f"HEAD:{path}"],
                          cwd=str(REPO_ROOT), capture_output=True)
    if proc.returncode != 0:
        return None
    return proc.stdout.decode("utf-8", "replace")


def apply_mutation(m: Mutation) -> bool:
    """True when the mutation was applied (old text found and replaced)."""
    path = REPO_ROOT / m.target
    original = path.read_text(encoding="utf-8")
    if m.old not in original:
        print(f"    SKIP: anchor text not found in {m.target}")
        return False
    path.write_text(original.replace(m.old, m.new, 1), encoding="utf-8")
    return True


def restore(m: Mutation) -> None:
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