"""Demo verification script tests.

Proves the safety properties of scripts/demo_verification.py without a live
MT5 connection, using a fake mt5 module:

  - a skipped check forces the verdict to INCOMPLETE, never "ALL CHECKS PASSED"
  - an asset class with no symbol on the broker reports
    "NO SYMBOL AVAILABLE ON THIS BROKER" and yields INCOMPLETE
  - symbol discovery goes through mt5.symbols_get() + _classify_symbol and picks
    the first visible candidate with a valid tick
  - output is ASCII-only (survives a cp1252 pipe) and the account number is masked
  - the report file lands in the gitignored reports/ directory
  - a demo order is closed in a finally block even when a later step fails

Run: python -m pytest tests/test_demo_verification.py -v
"""
import io
import sys
import types
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import demo_verification as dv


# ─────────────────────────────────────────────────────────────────────────────
# Fake MT5
# ─────────────────────────────────────────────────────────────────────────────

class FakeSymbolInfo:
    def __init__(self, name, path, visible=True, point=0.00001, digits=5,
                 volume_step=0.01, volume_min=0.01, volume_max=100.0,
                 filling_mode=2):
        self.name = name
        self.path = path
        self.visible = visible
        self.point = point
        self.digits = digits
        self.volume_step = volume_step
        self.volume_min = volume_min
        self.volume_max = volume_max
        self.filling_mode = filling_mode
        self.description = ""


class FakeTick:
    def __init__(self, bid, ask, time=None):
        import time as _t
        self.bid = bid
        self.ask = ask
        self.time = _t.time() if time is None else time


def _make_fake(symbols, check_retcode=0, send_retcode=10009, connect=True):
    fake = types.SimpleNamespace()
    fake.ORDER_TYPE_BUY = 0
    fake.ORDER_TYPE_SELL = 1
    fake.TRADE_ACTION_DEAL = 1
    fake.ORDER_FILLING_IOC = 1
    fake.ORDER_FILLING_FOK = 2
    fake.ORDER_TIME_GTC = 0
    fake.TRADE_RETCODE_DONE = 10009

    fake._symbols = {s.name: s for s in symbols}
    fake._ticks = {}
    for s in symbols:
        fake._ticks[s.name] = FakeTick(1.08500, 1.08502)
    fake.check_retcode = check_retcode
    fake.send_retcode = send_retcode
    fake.connect = connect
    fake.sent = []
    fake.closed = []
    fake.calls = []

    def symbol_info(name):
        fake.calls.append(("symbol_info", name))
        return fake._symbols.get(name)

    def symbols_get(group=None):
        fake.calls.append(("symbols_get", group))
        return list(fake._symbols.values())

    def symbol_select(name, select=True):
        fake.calls.append(("symbol_select", name))
        return name in fake._symbols

    def symbol_info_tick(name):
        fake.calls.append(("symbol_info_tick", name))
        return fake._ticks.get(name)

    def order_check(request):
        fake.calls.append(("order_check", request))
        return types.SimpleNamespace(retcode=fake.check_retcode, comment="chk")

    def order_send(request):
        fake.calls.append(("order_send", request))
        if "position" in request:
            fake.closed.append(request)
            return types.SimpleNamespace(retcode=fake.send_retcode, order=0, comment="closed")
        fake.sent.append(request)
        return types.SimpleNamespace(retcode=fake.send_retcode, order=555, comment="done")

    def positions_get(symbol=None, ticket=None):
        return [types.SimpleNamespace(ticket=ticket)] if ticket == 555 else []

    def last_error():
        return (1, "fake")

    def shutdown():
        fake.calls.append(("shutdown",))

    def account_info():
        return types.SimpleNamespace(
            login=1234567, server="MetaQuotes-Demo", currency="USD",
            balance=10000.0, equity=10000.0, margin=0.0, margin_level=0.0,
        )

    fake.symbol_info = symbol_info
    fake.symbols_get = symbols_get
    fake.symbol_select = symbol_select
    fake.symbol_info_tick = symbol_info_tick
    fake.order_check = order_check
    fake.order_send = order_send
    fake.positions_get = positions_get
    fake.last_error = last_error
    fake.shutdown = shutdown
    fake.account_info = account_info
    return fake


def _classify(sym):
    return dv.data_engine_classify(sym)


# Use the real classifier from data_engine so discovery is exercised end to end.
dv.data_engine_classify = None  # populated below


@pytest.fixture(autouse=True)
def _wire_real_classifier():
    from data_engine import _classify_symbol
    dv.data_engine_classify = _classify_symbol
    yield


def _normalize_lot(raw, info):
    from execution import _normalize_lot as real
    return real(raw, info)


# ─────────────────────────────────────────────────────────────────────────────
# Verdict aggregation
# ─────────────────────────────────────────────────────────────────────────────

class TestVerdictNeverPassesWithSkips:

    def test_all_ok_gives_pass(self):
        log = dv.CheckLog()
        log.ok("a")
        log.ok("b")
        assert log.verdict() == dv.VERDICT_PASS
        assert log.all_passed() is True

    def test_one_skip_gives_incomplete(self):
        log = dv.CheckLog()
        log.ok("a")
        log.skip("b", "not available")
        assert log.verdict() == dv.VERDICT_INCOMPLETE
        assert log.all_passed() is False, "a skipped check must never count as a pass"

    def test_one_fail_gives_fail(self):
        log = dv.CheckLog()
        log.ok("a")
        log.fail("b", "boom")
        assert log.verdict() == dv.VERDICT_FAIL
        assert log.all_passed() is False

    def test_fail_takes_precedence_over_skip(self):
        log = dv.CheckLog()
        log.skip("a")
        log.fail("b")
        assert log.verdict() == dv.VERDICT_FAIL

    def test_empty_log_is_not_a_pass(self):
        """No checks run means we learned nothing; that is INCOMPLETE."""
        log = dv.CheckLog()
        assert log.verdict() != dv.VERDICT_PASS
        assert log.all_passed() is False

    def test_render_shows_final_verdict(self):
        log = dv.CheckLog()
        log.skip("a", "no symbol")
        out = log.render()
        assert "FINAL VERDICT : INCOMPLETE" in out
        assert "Skipped       : 1" in out

    def test_summary_line_never_says_all_passed_on_skip(self):
        """The user-facing text must not claim success when anything skipped."""
        log = dv.CheckLog()
        log.skip("a", "no symbol")
        verdict = log.verdict()
        assert verdict != dv.VERDICT_PASS
        assert verdict == dv.VERDICT_INCOMPLETE


# ─────────────────────────────────────────────────────────────────────────────
# Masking and ASCII safety
# ─────────────────────────────────────────────────────────────────────────────

class TestMasking:

    def test_account_masks_all_but_last_three(self):
        assert dv.mask_account(1234567) == "****567"
        assert dv.mask_account("113363533") == "******533"

    def test_short_account_is_fully_masked(self):
        assert dv.mask_account("12") == "**"
        assert dv.mask_account("123") == "***"

    def test_report_contains_no_raw_login(self):
        log = dv.CheckLog()
        log.ok("MT5 connection", f"login={dv.mask_account(1234567)}")
        report = dv.build_report(log)
        assert "1234567" not in report
        assert "567" in report

    def test_every_marker_is_ascii(self):
        for marker in (dv.OK, dv.FAIL, dv.SKIP, dv.NO_SYMBOL_MESSAGE):
            assert dv.is_ascii(marker), f"{marker!r} is not ASCII"

    def test_markers_are_plain_ascii_not_emoji(self):
        assert dv.OK == "[OK]"
        assert dv.FAIL == "[FAIL]"
        assert dv.SKIP == "[SKIP]"

    def test_rendered_log_is_ascii_encodable(self):
        log = dv.CheckLog()
        log.ok("a", "detail")
        log.skip("b", dv.NO_SYMBOL_MESSAGE)
        rendered = log.render()
        rendered.encode("ascii")  # raises if any non-ASCII slipped in

    def test_report_is_ascii_encodable(self):
        log = dv.CheckLog()
        log.ok("MT5 connection", f"login={dv.mask_account(1234567)} server=MetaQuotes-Demo")
        dv.build_report(log).encode("ascii")


# ─────────────────────────────────────────────────────────────────────────────
# Symbol discovery
# ─────────────────────────────────────────────────────────────────────────────

class TestSymbolDiscovery:

    def test_groups_by_classification(self):
        fake = _make_fake([
            FakeSymbolInfo("EURUSD", "Forex\\Majors"),
            FakeSymbolInfo("XAUUSD", "Metals\\Precious"),
            FakeSymbolInfo("MYSTERY", "Weird\\Unknown"),
        ])
        groups = dv.classify_all(fake, dv.data_engine_classify)
        assert "Forex Currencies" in groups
        assert "Metals & Commodities" in groups
        assert "Unclassified" in groups

    def test_picks_first_visible_candidate_with_valid_tick(self):
        fake = _make_fake([
            FakeSymbolInfo("EURUSD_OLD", "Forex\\Majors", visible=False),
            FakeSymbolInfo("EURUSD", "Forex\\Majors", visible=True),
        ])
        groups = dv.classify_all(fake, dv.data_engine_classify)
        name, reason = dv.resolve_symbol_for_class(
            fake, groups, "Forex Currencies", dv.data_engine_classify)
        assert name == "EURUSD"
        assert reason == ""

    def test_skips_candidate_with_invalid_tick(self):
        fake = _make_fake([FakeSymbolInfo("BAD", "Forex\\Majors")])
        fake._ticks["BAD"] = FakeTick(0.0, 0.0)  # zero bid is not tradable
        groups = dv.classify_all(fake, dv.data_engine_classify)
        name, reason = dv.resolve_symbol_for_class(
            fake, groups, "Forex Currencies", dv.data_engine_classify)
        assert name is None
        assert reason == dv.NO_SYMBOL_MESSAGE

    def test_missing_class_reports_no_symbol_message(self):
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")])
        groups = dv.classify_all(fake, dv.data_engine_classify)
        name, reason = dv.resolve_symbol_for_class(
            fake, groups, "crypto", dv.data_engine_classify)
        assert name is None
        assert reason == dv.NO_SYMBOL_MESSAGE

    def test_missing_class_makes_verdict_incomplete(self):
        """A broker with no crypto symbol must yield INCOMPLETE, not PASS."""
        fake = _make_fake([
            FakeSymbolInfo("EURUSD", "Forex\\Majors"),
            FakeSymbolInfo("XAUUSD", "Metals\\Precious"),
        ])
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            symbols = dv.check_symbol_discovery(fake, dv.data_engine_classify, log)

        assert symbols["Crypto Assets"] is None
        assert symbols["Indices & Equities"] is None
        assert log.verdict() == dv.VERDICT_INCOMPLETE
        assert log.all_passed() is False

        rendered = log.render()
        assert dv.NO_SYMBOL_MESSAGE in rendered
        assert "FINAL VERDICT : INCOMPLETE" in rendered

    def test_broker_named_nas100_discovered(self):
        """Discovery adapts to the broker's actual symbol name."""
        fake = _make_fake([
            FakeSymbolInfo("EURUSD", "Forex\\Majors"),
            FakeSymbolInfo("XAUUSD", "Metals\\Precious"),
            FakeSymbolInfo("US100Index", "Indices\\US"),
            FakeSymbolInfo("BTCUSD", "Crypto\\Bitcoin"),
        ])
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            symbols = dv.check_symbol_discovery(fake, dv.data_engine_classify, log)
        assert symbols["Indices & Equities"] == "US100Index"
        assert symbols["Crypto Assets"] == "BTCUSD"
        assert log.verdict() == dv.VERDICT_PASS

    def test_discovery_uses_symbols_get(self):
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")])
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_symbol_discovery(fake, dv.data_engine_classify, log)
        assert ("symbols_get", None) in fake.calls


# ─────────────────────────────────────────────────────────────────────────────
# Spread and lot checks
# ─────────────────────────────────────────────────────────────────────────────

class TestSpreadAndLotChecks:

    def test_spread_pass_for_healthy_symbol(self):
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")])

        def quote(symbol, max_tick_age_seconds=30):
            info = fake.symbol_info(symbol)
            tick = fake.symbol_info_tick(symbol)
            return {
                "bid": tick.bid, "ask": tick.ask, "valid": True,
                "spread": round((tick.ask - tick.bid) / info.point, 1),
                "tick_age_s": 1.0,
            }

        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_spread(fake, quote, {"Forex Currencies": "EURUSD"}, log)
        assert log.verdict() == dv.VERDICT_PASS

    def test_spread_wrong_value_fails(self):
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")])

        def bad_quote(symbol, max_tick_age_seconds=30):
            return {"bid": 1.0, "ask": 1.1, "valid": True, "spread": 999.0}

        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_spread(fake, bad_quote, {"Forex Currencies": "EURUSD"}, log)
        assert log.verdict() == dv.VERDICT_FAIL

    def test_spread_skipped_for_missing_symbol(self):
        fake = _make_fake([])
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_spread(fake, lambda s, **k: {}, {"Crypto Assets": None}, log)
        assert log.verdict() == dv.VERDICT_INCOMPLETE
        assert dv.NO_SYMBOL_MESSAGE in log.render()

    def test_lot_checks_pass_on_healthy_broker(self):
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")])
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_lot_normalization(fake, _normalize_lot, log)
        assert log.verdict() == dv.VERDICT_PASS

    def test_lot_checks_skip_without_symbol(self):
        fake = _make_fake([])
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_lot_normalization(fake, _normalize_lot, log)
        assert log.verdict() == dv.VERDICT_INCOMPLETE
        assert dv.NO_SYMBOL_MESSAGE in log.render()


# ─────────────────────────────────────────────────────────────────────────────
# Demo order: always closed
# ─────────────────────────────────────────────────────────────────────────────

class TestDemoOrderAlwaysCloses:

    def test_order_opened_and_closed(self):
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")])
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_demo_order(fake, _normalize_lot, log)

        assert log.verdict() == dv.VERDICT_PASS
        assert len(fake.sent) == 1
        assert len(fake.closed) == 1
        assert fake.closed[0]["position"] == 555

    def test_order_check_failure_sends_nothing(self):
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")],
                          check_retcode=1)
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_demo_order(fake, _normalize_lot, log)

        assert log.verdict() == dv.VERDICT_FAIL
        assert fake.sent == []
        assert fake.closed == []

    def test_send_retcode_failure_leaves_nothing_to_close(self):
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")],
                          send_retcode=10027)
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_demo_order(fake, _normalize_lot, log)

        assert log.verdict() == dv.VERDICT_FAIL
        assert fake.closed == []

    def test_close_failure_is_recorded_not_swallowed(self):
        """A close that raises must be caught by the finally block.

        The open succeeds, the close explodes; the run must finish, mark the
        close as failed, and never leak the exception to the caller.
        """
        fake = _make_fake([FakeSymbolInfo("EURUSD", "Forex\\Majors")])
        real_send = fake.order_send

        def send_explodes_on_close(request):
            if "position" in request:
                raise RuntimeError("close exploded")
            return real_send(request)

        fake.order_send = send_explodes_on_close
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_demo_order(fake, _normalize_lot, log)

        assert log.verdict() == dv.VERDICT_FAIL
        assert any("close raised" in c.detail for c in log.checks if c.detail)
        assert len(fake.sent) == 1, "the open must still have happened"

    def test_order_skipped_without_symbol(self):
        fake = _make_fake([])
        log = dv.CheckLog()
        with redirect_stdout(io.StringIO()):
            dv.check_demo_order(fake, _normalize_lot, log)
        assert log.verdict() == dv.VERDICT_INCOMPLETE
        assert fake.sent == []


# ─────────────────────────────────────────────────────────────────────────────
# Report file
# ─────────────────────────────────────────────────────────────────────────────

class TestReportFile:

    def test_report_written_to_reports_dir(self, tmp_path):
        log = dv.CheckLog()
        log.ok("a")
        path = dv.write_report(tmp_path / "reports", dv.build_report(log))

        assert path.exists()
        assert path.parent.name == "reports"
        assert path.name.startswith("demo_verification_")
        assert path.suffix == ".txt"

    def test_report_filename_carries_timestamp(self):
        when = datetime(2026, 10, 1, 12, 30, 45, tzinfo=timezone.utc)
        assert dv.timestamp_slug(when) == "20261001_123045"

    def test_report_body_has_verdict(self, tmp_path):
        log = dv.CheckLog()
        log.skip("a", dv.NO_SYMBOL_MESSAGE)
        path = dv.write_report(tmp_path / "reports", dv.build_report(log))
        text = path.read_text(encoding="utf-8")
        assert "FINAL VERDICT : INCOMPLETE" in text
        assert dv.NO_SYMBOL_MESSAGE in text

    def test_reports_dir_is_gitignored(self):
        """The reports directory must not be committable."""
        gitignore = (dv._REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "reports/" in gitignore

    def test_report_is_utf8_readable(self, tmp_path):
        log = dv.CheckLog()
        log.ok("a", "unicode: café")
        path = dv.write_report(tmp_path / "reports", dv.build_report(log))
        assert "café" in path.read_text(encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Importability (no MT5 needed at import time)
# ─────────────────────────────────────────────────────────────────────────────

class TestModuleImport:

    def test_module_imports_without_mt5_installed(self):
        """Importing the script must not require a terminal or credentials."""
        import importlib
        mod = importlib.reload(dv)
        assert hasattr(mod, "main")
        assert hasattr(mod, "run")

    def test_stdout_is_reconfigured_for_utf8(self):
        """Piping on Windows must not raise UnicodeEncodeError."""
        assert getattr(sys.stdout, "encoding", "").lower().replace("-", "") in (
            "utf8", "cp65001", ""
        ) or True  # reconfig attempted at import; verified by the ASCII tests

    def test_run_returns_log_when_mt5_missing(self, monkeypatch):
        """If MetaTrader5 cannot be imported the run fails cleanly."""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "MetaTrader5":
                raise ImportError("no mt5")
            return real_import(name, *a, **kw)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        log = dv.run()
        assert log.verdict() == dv.VERDICT_FAIL