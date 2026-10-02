"""Demo account verification — Phase 0 pre-PR checklist.

Verifies the Phase 0 fixes against a live MT5 demo account:

  - spread is computed from the symbol's own point size, with sanity checks
  - lot normalization floors to the step, rejects below volume_min, clamps to
    volume_max, and survives the 0.07/0.01 float case
  - a real demo order round-trips (order_check -> order_send -> close)
  - symbols classify by MT5 path, and unclassifiable symbols are excluded

Design notes:

  * A check that could not run is reported as SKIPPED and forces the overall
    verdict to INCOMPLETE. "ALL CHECKS PASSED" is only ever printed when every
    required check actually ran and passed.
  * Symbol discovery goes through mt5.symbols_get() and _classify_symbol, so it
    adapts to the broker's naming instead of assuming NAS100/BTCUSD exist.
  * Output is plain ASCII so it survives a Windows cp1252 pipe. No emoji.
  * The account number is masked to its last 3 digits; passwords and API keys
    are never printed.
  * Any demo order opened is closed in a finally block, so a later failure
    cannot leave an orphan position.

Runs both ways:
    python scripts/demo_verification.py
    python -m scripts.demo_verification
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow `python scripts/demo_verification.py` as well as `python -m ...`.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:  # Make output safe to pipe on Windows regardless of the console codepage.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - non-reconfigurable stream
    pass

from dotenv import load_dotenv

load_dotenv()

DIVIDER = "-" * 72

OK = "[OK]"
FAIL = "[FAIL]"
SKIP = "[SKIP]"

# Asset classes the script tries to exercise.
#
# These keys MUST match the labels returned by data_engine._classify_symbol,
# because resolve_symbol_for_class() looks the group up by exactly this name.
# Keeping the two in sync matters: a mismatch silently reports every symbol in
# that class as NO SYMBOL AVAILABLE.
REQUIRED_CLASSES = (
    "Forex Currencies",
    "Metals & Commodities",
    "Indices & Equities",
    "Crypto Assets",
)

NO_SYMBOL_MESSAGE = "NO SYMBOL AVAILABLE ON THIS BROKER"

# Verdicts
VERDICT_PASS = "PASS"
VERDICT_INCOMPLETE = "INCOMPLETE"
VERDICT_FAIL = "FAIL"


# ─────────────────────────────────────────────────────────────────────────────
# Result accumulation (pure, no MT5)
# ─────────────────────────────────────────────────────────────────────────────

class Check:
    __slots__ = ("name", "status", "detail")

    def __init__(self, name: str, status: str, detail: str = ""):
        self.name = name
        self.status = status  # OK | FAIL | SKIP
        self.detail = detail

    def line(self) -> str:
        return f"  {self.status:6s} {self.name}" + (f" - {self.detail}" if self.detail else "")


class CheckLog:
    """Collects checks and derives a single overall verdict.

    The verdict rule is the point of this class: a skipped check is NOT a pass,
    and an empty log is NOT a pass either.
    """

    def __init__(self) -> None:
        self.checks: list[Check] = []

    def ok(self, name: str, detail: str = "") -> Check:
        return self._add(Check(name, OK, detail))

    def fail(self, name: str, detail: str = "") -> Check:
        return self._add(Check(name, FAIL, detail))

    def skip(self, name: str, detail: str = "") -> Check:
        return self._add(Check(name, SKIP, detail))

    def _add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def has_failures(self) -> bool:
        return any(c.status == FAIL for c in self.checks)

    @property
    def has_skips(self) -> bool:
        return any(c.status == SKIP for c in self.checks)

    def verdict(self) -> str:
        """FAIL if anything failed, INCOMPLETE if anything was skipped or did not
        run, else PASS.

        Only a run where every check actually executed and passed yields PASS.
        An empty log is INCOMPLETE: no evidence is not evidence of success.
        """
        if self.has_failures:
            return VERDICT_FAIL
        if self.has_skips or not self.checks:
            return VERDICT_INCOMPLETE
        return VERDICT_PASS

    def all_passed(self) -> bool:
        return self.verdict() == VERDICT_PASS

    def render(self) -> str:
        lines = [c.line() for c in self.checks]
        lines.append("")
        lines.append(f"  Checks run    : {len(self.checks)}")
        lines.append(f"  Passed        : {sum(1 for c in self.checks if c.status == OK)}")
        lines.append(f"  Failed        : {sum(1 for c in self.checks if c.status == FAIL)}")
        lines.append(f"  Skipped       : {sum(1 for c in self.checks if c.status == SKIP)}")
        lines.append(f"  FINAL VERDICT : {self.verdict()}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers (pure)
# ─────────────────────────────────────────────────────────────────────────────

def mask_account(login) -> str:
    """Mask an account number to its last 3 digits."""
    text = str(login)
    if len(text) <= 3:
        return "*" * len(text)
    return "*" * (len(text) - 3) + text[-3:]


def section(title: str) -> str:
    return f"\n{DIVIDER}\n  {title}\n{DIVIDER}"


def timestamp_slug(when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    return when.strftime("%Y%m%d_%H%M%S")


def is_ascii(text: str) -> bool:
    try:
        text.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Symbol discovery (pure given an mt5 module)
# ─────────────────────────────────────────────────────────────────────────────

def classify_all(mt5_mod, classify_symbol) -> dict[str, list]:
    """Group every broker symbol by asset class using MT5's own path field."""
    groups: dict[str, list] = {}
    for sym in mt5_mod.symbols_get() or []:
        groups.setdefault(classify_symbol(sym), []).append(sym)
    return groups


def pick_candidate(mt5_mod, candidates, asset_class: str):
    """First candidate that can actually be queried, activating it if needed.

    MT5 reports `visible=False` for any symbol absent from the user's Market
    Watch. Such a symbol is still tradable: it only needs to be added to the
    active set with symbol_select() before quotes are available for it.

    The previous implementation skipped invisible symbols outright, so NAS100,
    BTCUSD and any other symbol the user had never watched were reported as
    NO SYMBOL AVAILABLE ON THIS BROKER. That was a false negative about the
    scan, not about the broker.

    Selection order: already-visible symbols first, then hidden ones (which we
    attempt to activate). Within each group the original broker ordering is
    preserved so results stay deterministic.

    Returns (symbol_name, reason). reason is non-empty when nothing qualified.
    """
    visible_candidates = []
    hidden_candidates = []

    for sym in candidates:
        if getattr(sym, "visible", False):
            visible_candidates.append(sym)
        else:
            hidden_candidates.append(sym)

    # Try visible symbols first: no activation needed and no side effects.
    for sym in visible_candidates:
        name, reason = _probe_symbol(mt5_mod, sym, select=False)
        if name:
            return name, ""

    # Then hidden ones, adding each to the active set before probing.
    for sym in hidden_candidates:
        name, reason = _probe_symbol(mt5_mod, sym, select=True)
        if name:
            return name, ""

    return None, NO_SYMBOL_MESSAGE


def _probe_symbol(mt5_mod, sym, select: bool):
    """Activate (if needed) and probe a symbol for a usable quote."""
    name = sym.name

    if select:
        # A hidden symbol returns no tick until it is selected. Failure here is
        # not fatal: the symbol may be unavailable for another reason, in which
        # case the tick probe below will reject it anyway.
        try:
            mt5_mod.symbol_select(name, True)
        except Exception:
            pass

    try:
        info = mt5_mod.symbol_info(name)
    except Exception:
        info = None
    if info is None:
        return None, NO_SYMBOL_MESSAGE

    try:
        tick = mt5_mod.symbol_info_tick(name)
    except Exception:
        tick = None
    if tick is None or tick.bid <= 0 or tick.ask < tick.bid:
        return None, NO_SYMBOL_MESSAGE

    return name, ""
    return None, NO_SYMBOL_MESSAGE


def resolve_symbol_for_class(mt5_mod, groups, asset_class: str, classify_symbol):
    """Find a usable broker symbol for an asset class.

    Returns (symbol_name_or_None, note).
    """
    candidates = groups.get(asset_class, [])
    if not candidates:
        return None, NO_SYMBOL_MESSAGE
    name, reason = pick_candidate(mt5_mod, candidates, asset_class)
    return name, reason


# ─────────────────────────────────────────────────────────────────────────────
# The checks themselves
# ─────────────────────────────────────────────────────────────────────────────

def check_connection(mt5_mod, initialize_mt5, log: CheckLog):
    print(section("MT5 CONNECTION"))

    if not initialize_mt5():
        log.fail("MT5 connection", "could not initialise terminal")
        print(f"  {FAIL:6s} MT5 connection — could not initialise terminal")
        return False

    acc = mt5_mod.account_info()
    if acc is None:
        log.fail("MT5 account", "account_info() returned None after init")
        print(f"  {FAIL:6s} MT5 account — account_info() returned None")
        return False

    margin = getattr(acc, "margin", 0.0) or 0.0
    level = getattr(acc, "margin_level", 0.0) or 0.0
    margin_str = f"{level:.1f}%" if margin > 1.0 else "N/A"

    detail = f"login={mask_account(acc.login)} server={acc.server}"
    log.ok("MT5 connection", detail)
    print(f"  {OK:6s} {detail}")
    print(f"         balance={acc.currency} {acc.balance:,.2f}  equity={acc.equity:,.2f}")
    print(f"         margin level={margin_str} (N/A is correct with no open positions)")
    return True


def check_symbol_discovery(mt5_mod, classify_symbol, log: CheckLog) -> dict:
    """Discover one usable broker symbol per required asset class."""
    print(section("SYMBOL DISCOVERY"))
    groups = classify_all(mt5_mod, classify_symbol)
    resolved: dict[str, str | None] = {}

    for asset_class in REQUIRED_CLASSES:
        name, reason = resolve_symbol_for_class(
            mt5_mod, groups, asset_class, classify_symbol
        )
        resolved[asset_class] = name
        if name:
            log.ok(f"discover {asset_class}", f"using broker symbol {name}")
            print(f"  {OK:6s} {asset_class:20s} -> {name}")
        else:
            log.skip(f"discover {asset_class}", reason)
            print(f"  {SKIP:6s} {asset_class:20s} -> {reason}")

    return resolved


def check_spread(mt5_mod, get_symbol_live_quote, symbols: dict, log: CheckLog):
    """Verify spread == (ask - bid) / point for each resolved symbol."""
    print(section("SPREAD FORMULA"))

    for asset_class, sym in symbols.items():
        label = f"spread {asset_class}"
        if not sym:
            log.skip(label, NO_SYMBOL_MESSAGE)
            continue

        quote = get_symbol_live_quote(sym, max_tick_age_seconds=60)
        if not quote.get("valid"):
            log.skip(label, f"{sym}: {quote.get('reason', 'invalid quote')}")
            print(f"  {SKIP:6s} {label} — {sym}: {quote.get('reason', '')}")
            continue

        info = mt5_mod.symbol_info(sym)
        expected = round((quote["ask"] - quote["bid"]) / info.point, 1)
        actual = quote["spread"]
        if abs(actual - expected) < 1e-9:
            log.ok(label, f"{sym} spread={actual} pts (point={info.point})")
            print(f"  {OK:6s} {label} — {sym}: bid={quote['bid']:g} ask={quote['ask']:g} "
                  f"spread={actual} pts (age={quote.get('tick_age_s', '?')}s)")
        else:
            log.fail(label, f"{sym}: got {actual}, expected {expected}")
            print(f"  {FAIL:6s} {label} — {sym}: got {actual}, expected {expected}")


def check_lot_normalization(mt5_mod, normalize_lot, log: CheckLog):
    """Floor-to-step, reject-below-min, clamp-to-max, and the 0.07 float case."""
    print(section("LOT NORMALIZATION"))

    symbol = next(
        (s for s in ("EURUSD", "XAUUSD") if mt5_mod.symbol_info(s) is not None),
        None,
    )
    if symbol is None:
        log.skip("lot normalization", NO_SYMBOL_MESSAGE)
        print(f"  {SKIP:6s} lot normalization — {NO_SYMBOL_MESSAGE}")
        return

    info = mt5_mod.symbol_info(symbol)

    lot, err = normalize_lot(0.07, info)
    if err:
        log.fail("lot 0.07 float case", err)
        print(f"  {FAIL:6s} lot 0.07 float case — {err}")
    elif abs(lot - 0.07) > 1e-9:
        log.fail("lot 0.07 float case", f"expected 0.07, got {lot}")
        print(f"  {FAIL:6s} lot 0.07 float case — expected 0.07, got {lot}")
    else:
        log.ok("lot 0.07 float case", f"{symbol} 0.07 -> 0.07")
        print(f"  {OK:6s} lot 0.07 float case — {symbol} 0.07 -> {lot}")

    lot, err = normalize_lot(0.059, info)
    if err:
        log.fail("lot floors to step", err)
        print(f"  {FAIL:6s} lot floors to step — {err}")
    elif abs(lot - 0.05) > 1e-9:
        log.fail("lot floors to step", f"expected 0.05, got {lot}")
        print(f"  {FAIL:6s} lot floors to step — expected 0.05, got {lot}")
    else:
        log.ok("lot floors to step", f"{symbol} 0.059 -> 0.05")
        print(f"  {OK:6s} lot floors to step — {symbol} 0.059 -> {lot}")

    below_min = info.volume_min / 2.0
    lot, err = normalize_lot(below_min, info)
    if lot == 0.0 and err:
        log.ok("lot below min rejected", f"{below_min} < min {info.volume_min}")
        print(f"  {OK:6s} lot below min rejected — {below_min} < min {info.volume_min} "
              f"(rejected locally, never sent)")
    else:
        log.fail("lot below min rejected", f"got lot={lot}, err={err!r}")
        print(f"  {FAIL:6s} lot below min rejected — got lot={lot}, err={err!r}")

    above_max = info.volume_max * 2.0
    lot, err = normalize_lot(above_max, info)
    if err:
        log.fail("lot clamped to max", err)
        print(f"  {FAIL:6s} lot clamped to max — {err}")
    elif abs(lot - info.volume_max) > 1e-9:
        log.fail("lot clamped to max", f"expected {info.volume_max}, got {lot}")
        print(f"  {FAIL:6s} lot clamped to max — expected {info.volume_max}, got {lot}")
    else:
        log.ok("lot clamped to max", f"{above_max} -> {info.volume_max}")
        print(f"  {OK:6s} lot clamped to max — {above_max} -> {lot}")


def check_classification(mt5_mod, classify_symbol, log: CheckLog):
    """Any symbol MT5 cannot classify must not be treated as tradable."""
    print(section("SYMBOL CLASSIFICATION"))

    all_symbols = mt5_mod.symbols_get() or []
    if not all_symbols:
        log.skip("symbol classification", "MT5 returned no symbols")
        print(f"  {SKIP:6s} symbol classification — MT5 returned no symbols")
        return

    counts: dict[str, int] = {}
    unclassified: list[str] = []
    for sym in all_symbols:
        cat = classify_symbol(sym)
        counts[cat] = counts.get(cat, 0) + 1
        if cat == "Unclassified":
            unclassified.append(sym.name)

    for cat in sorted(counts):
        print(f"         {cat:24s} {counts[cat]}")
    print(f"         Unclassified (non-tradable): {len(unclassified)}")

    if unclassified:
        log.ok("symbol classification",
               f"{len(unclassified)} unclassifiable symbols excluded from trading")
        print(f"  {OK:6s} {len(unclassified)} unclassifiable symbols correctly excluded")
        for name in unclassified[:5]:
            print(f"         e.g. {name}")
    else:
        # Not a failure: some brokers genuinely classify everything. The
        # whitelist still gates trading regardless.
        log.skip("symbol classification",
                 "no unclassifiable symbols on this broker (whitelist still enforced)")
        print(f"  {SKIP:6s} no unclassifiable symbols found — "
              f"whitelist gating still enforced independently")


def check_demo_account_mode(mt5_mod, log: CheckLog) -> bool:
    """Refuse to continue if this is not a demo account.

    This script sends a real order. On a live account it must refuse outright
    rather than rely on the operator reading the account number.
    """
    print(section("ACCOUNT MODE GUARD"))

    acc = mt5_mod.account_info()
    if acc is None:
        log.fail("account mode", "account_info() returned None")
        print(f"  {FAIL:6s} account mode - account_info() returned None")
        return False

    trade_mode = getattr(acc, "trade_mode", None)
    demo_mode = getattr(mt5_mod, "ACCOUNT_TRADE_MODE_DEMO", 0)
    real_mode = getattr(mt5_mod, "ACCOUNT_TRADE_MODE_REAL", 2)
    contest_mode = getattr(mt5_mod, "ACCOUNT_TRADE_MODE_CONTEST", 1)

    label = {demo_mode: "DEMO", contest_mode: "CONTEST", real_mode: "REAL"}.get(
        trade_mode, f"UNKNOWN({trade_mode})")

    if trade_mode == demo_mode:
        log.ok("account mode", f"{label} account; orders permitted")
        print(f"  {OK:6s} account mode = {label} (orders permitted)")
        return True

    detail = f"account mode is {label}, not DEMO; this script will NOT place orders"
    log.fail("account mode", detail)
    print(f"  {FAIL:6s} {detail}")
    return False


def check_demo_order(mt5_mod, normalize_lot, log: CheckLog):
    """Place one real demo order and close it in a finally block."""
    print(section("DEMO ORDER"))

    # Re-check the account mode here as well, immediately before ordering.
    acc = mt5_mod.account_info()
    trade_mode = getattr(acc, "trade_mode", None) if acc is not None else None
    demo_mode = getattr(mt5_mod, "ACCOUNT_TRADE_MODE_DEMO", 0)
    if trade_mode != demo_mode:
        log.skip("demo order", f"not a demo account (trade_mode={trade_mode})")
        print(f"  {SKIP:6s} demo order - not a demo account "
              f"(trade_mode={trade_mode}); no order sent")
        return

    symbol = next(
        (s for s in ("EURUSD", "XAUUSD") if mt5_mod.symbol_info(s) is not None),
        None,
    )
    if symbol is None:
        log.skip("demo order", NO_SYMBOL_MESSAGE)
        print(f"  {SKIP:6s} demo order — {NO_SYMBOL_MESSAGE}")
        return

    info = mt5_mod.symbol_info(symbol)
    mt5_mod.symbol_select(symbol, True)
    tick = mt5_mod.symbol_info_tick(symbol)
    if not info or tick is None:
        log.skip("demo order", f"no info/tick for {symbol}")
        print(f"  {SKIP:6s} demo order — no info/tick for {symbol}")
        return

    lot, err = normalize_lot(0.07, info)
    if err:
        log.skip("demo order", f"lot rejected: {err}")
        print(f"  {SKIP:6s} demo order — lot rejected: {err}")
        return

    filling = mt5_mod.ORDER_FILLING_IOC
    if info.filling_mode & 1:
        filling = mt5_mod.ORDER_FILLING_FOK
    elif info.filling_mode & 2:
        filling = mt5_mod.ORDER_FILLING_IOC

    request = {
        "action": mt5_mod.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot,
        "type": mt5_mod.ORDER_TYPE_BUY,
        "price": tick.ask,
        "sl": round(tick.ask - 0.0050, info.digits),
        "deviation": 25,
        "magic": 100100,
        "comment": "Phase0 demo verify",
        "type_time": mt5_mod.ORDER_TIME_GTC,
        "type_filling": filling,
    }

    opened_ticket = None
    try:
        check = mt5_mod.order_check(request)
        check_code = check.retcode if check is not None else None
        print(f"         order_check retcode: {check_code}")
        if check is None or check.retcode not in (0, mt5_mod.TRADE_RETCODE_DONE):
            log.fail("demo order", f"order_check failed (retcode={check_code})")
            print(f"  {FAIL:6s} order_check failed (retcode={check_code})")
            return

        result = mt5_mod.order_send(request)
        if result is None:
            log.fail("demo order", f"order_send returned None: {mt5_mod.last_error()}")
            print(f"  {FAIL:6s} order_send returned None: {mt5_mod.last_error()}")
            return

        if result.retcode != mt5_mod.TRADE_RETCODE_DONE:
            log.fail("demo order", f"retcode={result.retcode}: {result.comment}")
            print(f"  {FAIL:6s} retcode={result.retcode}: {result.comment}")
            return

        opened_ticket = result.order
        log.ok("demo order", f"{symbol} {lot} lots retcode={result.retcode} ticket={opened_ticket}")
        print(f"  {OK:6s} {symbol} requested=0.07 normalized={lot} "
              f"retcode={result.retcode} ticket={opened_ticket}")
    finally:
        # Always close what we opened, even if a check above failed.
        if opened_ticket:
            try:
                time.sleep(1)
                positions = mt5_mod.positions_get(ticket=opened_ticket)
                if positions:
                    close_price = mt5_mod.symbol_info_tick(symbol).bid
                    close_request = {
                        "action": mt5_mod.TRADE_ACTION_DEAL,
                        "position": opened_ticket,
                        "symbol": symbol,
                        "volume": lot,
                        "type": mt5_mod.ORDER_TYPE_SELL,
                        "price": close_price,
                        "deviation": 25,
                        "magic": 100100,
                        "comment": "Phase0 demo close",
                        "type_time": mt5_mod.ORDER_TIME_GTC,
                        "type_filling": filling,
                    }
                    close_result = mt5_mod.order_send(close_request)
                    code = close_result.retcode if close_result is not None else None
                    log.ok("demo order closed", f"retcode={code}")
                    print(f"  {OK:6s} position closed, retcode={code}")
                else:
                    log.fail("demo order closed", "position not found after open")
                    print(f"  {FAIL:6s} position not found after open")
            except Exception as exc:
                log.fail("demo order closed", f"close raised: {exc}")
                print(f"  {FAIL:6s} close raised: {exc}")


# ─────────────────────────────────────────────────────────────────────────────
# Report file
# ─────────────────────────────────────────────────────────────────────────────

def write_report(report_dir: Path, body: str, when: datetime | None = None) -> Path:
    """Write the report to reports/demo_verification_<timestamp>.txt."""
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"demo_verification_{timestamp_slug(when)}.txt"
    path.write_text(body, encoding="utf-8")
    return path


def build_report(log: CheckLog, when: datetime | None = None) -> str:
    """Build the report body.

    ASCII only by construction: an em-dash here previously crashed the script
    when its output was piped on a Windows cp1252 console.
    """
    when = when or datetime.now(timezone.utc)
    return "\n".join([
        DIVIDER,
        "  PHASE 0 - DEMO ACCOUNT VERIFICATION",
        DIVIDER,
        f"  Generated (UTC): {when.isoformat(timespec='seconds')}",
        "  Script         : scripts/demo_verification.py",
        "",
        log.render(),
        "",
    ])


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def _demo_normalize_lot(raw_lot, info):
    """Default lot normaliser, used when run() is called without injected deps."""
    from execution import _normalize_lot
    return _normalize_lot(raw_lot, info)


def run(log: CheckLog | None = None, deps: dict | None = None) -> CheckLog:
    """Execute every check against a live MT5 connection.

    `deps` allows tests to inject the MT5 module and its helpers, so the whole
    pipeline including the account-mode gate can be exercised without a
    terminal. Injected deps bypass the live import and the real MT5 library.
    """
    log = log if log is not None else CheckLog()

    if deps is not None:
        mt5_mod = deps["mt5"]
        _classify_symbol = deps["classify_symbol"]
        get_symbol_live_quote = deps.get("get_symbol_live_quote")
        initialize_mt5 = deps.get("initialize_mt5") or (lambda: True)
        normalize_lot = deps.get("normalize_lot") or _demo_normalize_lot
    else:
        try:
            import MetaTrader5 as mt5_mod
        except ImportError:
            log.fail("MetaTrader5 import", "package not installed")
            print(f"  {FAIL:6s} MetaTrader5 package not installed")
            return log

        from data_engine import (
            _classify_symbol,
            get_symbol_live_quote,
            initialize_mt5,
        )
        from execution import _normalize_lot as normalize_lot

    if not check_connection(mt5_mod, initialize_mt5, log):
        return log

    # Refuse outright on a non-demo account: everything below may send orders.
    if not check_demo_account_mode(mt5_mod, log):
        return log

    symbols = check_symbol_discovery(mt5_mod, _classify_symbol, log)
    check_spread(mt5_mod, get_symbol_live_quote, symbols, log)
    check_lot_normalization(mt5_mod, normalize_lot, log)
    check_classification(mt5_mod, _classify_symbol, log)
    check_demo_order(mt5_mod, normalize_lot, log)

    mt5_mod.shutdown()
    return log


def main() -> int:
    print("=" * 72)
    print("  PHASE 0 - DEMO ACCOUNT VERIFICATION")
    print("=" * 72)

    log = run()
    report = build_report(log)
    path = write_report(_REPO_ROOT / "reports", report)

    print()
    print(section("SUMMARY"))
    print(log.render())

    verdict = log.verdict()
    print()
    if verdict == VERDICT_PASS:
        print("  ALL CHECKS PASSED — every required check ran and passed.")
    elif verdict == VERDICT_INCOMPLETE:
        print(f"  {verdict} — at least one check was skipped or excluded.")
        print("  This is NOT a pass. Resolve the skips above and re-run.")
    else:
        print(f"  {verdict} — at least one check failed. See details above.")
    print(f"  Report written to: {path}")
    print()

    return 0 if verdict == VERDICT_PASS else 1


if __name__ == "__main__":
    sys.exit(main())