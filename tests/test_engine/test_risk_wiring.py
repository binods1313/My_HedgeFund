"""Risk gate wiring tests.

Two properties matter here:

  - The executor cannot be handed a bare True and proceed. A caller must carry
    a verdict the gate actually produced, so the gate cannot be skipped by
    asserting the answer rather than asking.
  - Account state is derived from live positions, and a position whose risk
    cannot be measured is counted as unprotected rather than as zero risk.

Run: python -m pytest tests/test_engine/test_risk_wiring.py -v
"""
from pathlib import Path

import pytest

from engine.executor import (
    ExecutionRejected,
    PaperExecutor,
    RiskApproval,
)
from engine.journal import Journal
from engine.risk import (
    AccountState,
    OrderIntent,
    RiskConfigError,
    RiskEngine,
    RiskLimits,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Trade specs measured from MetaQuotes-Demo via probe_tick_specs.py.
LIVE_SPECS = {
    "EURUSD": {"trade_tick_value": 0.8886124316879194, "trade_tick_size": 1e-05},
    "XAUUSD": {"trade_tick_value": 0.1, "trade_tick_size": 0.01},
    "US30": {"trade_tick_value": 0.008886124316879193, "trade_tick_size": 0.01},
}


class StubTick:
    def __init__(self, bid=1.10000, ask=1.10002):
        self.bid = bid
        self.ask = ask
        self.time = 0


def ticks(symbol):
    return StubTick()


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "wiring.db")
    yield j
    j.close()


@pytest.fixture
def risk(journal):
    engine = RiskEngine(journal)
    engine.set_symbol_specs(LIVE_SPECS)
    return engine


def healthy(**overrides):
    data = dict(equity=100_000.0, start_of_day_equity=100_000.0,
                peak_equity=100_000.0, open_trade_count=0,
                total_open_risk=0.0, unprotected_positions=0)
    data.update(overrides)
    return AccountState(**data)


# ─────────────────────────────────────────────────────────────────────────────
# Approval cannot be asserted, only requested
# ─────────────────────────────────────────────────────────────────────────────

class TestApprovalMustComeFromTheGate:

    def _executor(self, journal):
        return PaperExecutor(journal=journal, tick_provider=ticks)

    def test_bare_true_is_refused(self, journal):
        """The shortcut the change exists to close."""
        ex = self._executor(journal)
        request = ex.prepare_order("EURUSD", "BUY", 0.10, 1.09500)
        with pytest.raises(ExecutionRejected) as excinfo:
            ex.submit(request, risk_approved=True)
        message = str(excinfo.value)
        assert "bare True" in message, (
            "a bare True must be refused explicitly so the caller knows to "
            f"use the gate; got: {message}"
        )
        assert journal.open_position_count() == 0

    @pytest.mark.parametrize("bogus", [None, 1, "yes", object(), 0.5])
    def test_other_truthy_values_are_refused(self, journal, bogus):
        ex = self._executor(journal)
        request = ex.prepare_order("EURUSD", "BUY", 0.10, 1.09500)
        with pytest.raises(ExecutionRejected):
            ex.submit(request, risk_approved=bogus)

    def test_a_refusal_object_blocks(self, journal):
        ex = self._executor(journal)
        request = ex.prepare_order("EURUSD", "BUY", 0.10, 1.09500)
        refusal = RiskApproval(approved=False,
                               reasons=("risk_per_trade_pct exceeded",))
        with pytest.raises(ExecutionRejected) as excinfo:
            ex.submit(request, risk_approved=refusal)
        assert "risk_per_trade_pct exceeded" in str(excinfo.value), (
            "the refusal's reasons must reach the caller"
        )

    def test_a_real_approval_proceeds(self, journal, risk):
        ex = self._executor(journal)
        request = ex.prepare_order("EURUSD", "BUY", 0.10, 1.09900)
        intent = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=0.10,
            entry_price=1.10000, stop_loss=1.09900)
        decision = risk.evaluate(intent, healthy())
        approval = RiskApproval.from_decision(decision, "EURUSD")
        assert approval.approved is True

        result = ex.submit(request, risk_approved=approval)
        assert result.ok is True
        assert journal.open_position_count() == 1

    def test_from_decision_carries_the_risk_figures(self, risk):
        intent = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=0.10,
            entry_price=1.10000, stop_loss=1.09900)
        decision = risk.evaluate(intent, healthy())
        approval = RiskApproval.from_decision(decision, "EURUSD")
        assert approval.symbol == "EURUSD"
        assert approval.risk_amount == pytest.approx(8.886, abs=0.01)
        assert approval.risk_pct == pytest.approx(0.0089, abs=0.001)


# ─────────────────────────────────────────────────────────────────────────────
# Building an intent from live broker data
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildOrderIntent:

    def test_uses_the_broker_spec(self, risk):
        intent = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=0.10,
            entry_price=1.10000, stop_loss=1.09900)
        assert intent.risk_amount == pytest.approx(8.886, abs=0.01)

    def test_gold_uses_its_own_spec(self, risk):
        """A 10.00 price move on XAUUSD at 0.10 lots risks EUR 10.

        Gold's tick_value of 0.1 is the value of a 0.01 move, so a 10.00 move is
        1,000 ticks: 1,000 x 0.1 x 0.10 lots = EUR 10. The spec is deliberately
        very different from forex, which is why it must come from the broker
        rather than be assumed.
        """
        intent = risk.build_order_intent(
            symbol="XAUUSD", side="BUY", volume=0.10,
            entry_price=2400.00, stop_loss=2390.00)
        assert intent.risk_amount == pytest.approx(10.0, abs=0.01)

    def test_symbols_with_different_specs_give_different_risk(self, risk):
        """A shared assumed spec would misprice at least one of them."""
        same_distance_forex = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=0.10,
            entry_price=1.10000, stop_loss=1.09900)
        same_distance_gold = risk.build_order_intent(
            symbol="XAUUSD", side="BUY", volume=0.10,
            entry_price=2400.00, stop_loss=2390.00)
        assert same_distance_gold.risk_amount == pytest.approx(
            10.0, abs=0.01)
        assert same_distance_forex.risk_amount == pytest.approx(
            8.886, abs=0.01)
        assert same_distance_forex.risk_amount != \
            pytest.approx(same_distance_gold.risk_amount, abs=0.5)

    def test_unknown_symbol_is_refused(self, risk):
        """Sizing against an assumed tick spec would be a guess."""
        with pytest.raises(RiskConfigError) as excinfo:
            risk.build_order_intent(symbol="NOSUCH", side="BUY", volume=0.1,
                                    entry_price=1.0, stop_loss=0.9)
        assert "probe_tick_specs" in str(excinfo.value), (
            "the refusal should point at the tool that supplies the spec"
        )

    def test_spec_with_zero_tick_value_is_refused(self, journal):
        engine = RiskEngine(journal)
        engine.set_symbol_specs({"X": {"trade_tick_value": 0.0,
                                       "trade_tick_size": 0.01}})
        with pytest.raises(RiskConfigError):
            engine.build_order_intent(symbol="X", side="BUY", volume=0.1,
                                      entry_price=100.0, stop_loss=99.0)


# ─────────────────────────────────────────────────────────────────────────────
# Account state from live positions
# ─────────────────────────────────────────────────────────────────────────────

class TestAccountStateFromPositions:

    def test_no_positions_gives_zero_exposure(self, risk):
        state = risk.account_state_from_positions(
            equity=100_000.0, start_of_day_equity=100_000.0,
            peak_equity=100_000.0, open_positions=[])
        assert state.total_open_risk == pytest.approx(0.0)
        assert state.unprotected_positions == 0

    def test_protected_position_contributes_its_risk(self, risk):
        state = risk.account_state_from_positions(
            equity=100_000.0, start_of_day_equity=100_000.0,
            peak_equity=100_000.0,
            open_positions=[{"symbol": "EURUSD", "volume": 0.10,
                             "price_open": 1.10000, "sl": 1.09900,
                             "magic": 100100}],
        )
        assert state.total_open_risk == pytest.approx(8.886, abs=0.01)
        assert state.unprotected_positions == 0

    def test_position_without_a_stop_is_unprotected(self, risk):
        """Counting it as zero risk would understate exposure."""
        state = risk.account_state_from_positions(
            equity=100_000.0, start_of_day_equity=100_000.0,
            peak_equity=100_000.0,
            open_positions=[{"symbol": "GBPUSD", "volume": 0.01,
                             "price_open": 1.32951, "sl": 0.0,
                             "magic": 100100}],
        )
        assert state.unprotected_positions == 1
        assert state.total_open_risk == pytest.approx(0.0)

    def test_position_on_an_unknown_symbol_is_unprotected(self, risk):
        """Without a spec its distance cannot be priced."""
        state = risk.account_state_from_positions(
            equity=100_000.0, start_of_day_equity=100_000.0,
            peak_equity=100_000.0,
            open_positions=[{"symbol": "MYSTERY", "volume": 0.01,
                             "price_open": 1.0, "sl": 0.99,
                             "magic": 100100}],
        )
        assert state.unprotected_positions == 1

    def test_another_magic_number_is_unprotected(self, risk):
        """We cannot measure another EA's risk, so we do not pretend to."""
        state = risk.account_state_from_positions(
            equity=100_000.0, start_of_day_equity=100_000.0,
            peak_equity=100_000.0,
            open_positions=[{"symbol": "EURUSD", "volume": 0.01,
                             "price_open": 1.10000, "sl": 1.09900,
                             "magic": 999999}],
        )
        assert state.unprotected_positions == 1

    def test_open_count_is_taken_from_the_positions(self, risk):
        positions = [{"symbol": "EURUSD", "volume": 0.1, "price_open": 1.1,
                      "sl": 1.099, "magic": 100100} for _ in range(3)]
        state = risk.account_state_from_positions(
            equity=100_000.0, start_of_day_equity=100_000.0,
            peak_equity=100_000.0, open_positions=positions)
        assert state.open_trade_count == 3

    def test_halt_state_propagates_into_the_account(self, risk):
        risk.halt("drawdown", "limit breached")
        state = risk.account_state_from_positions(
            equity=100_000.0, start_of_day_equity=100_000.0,
            peak_equity=100_000.0, open_positions=[])
        assert state.trading_halted is True
        assert state.halt_reason


# ─────────────────────────────────────────────────────────────────────────────
# Directive 1: unprotected positions block everything
# ─────────────────────────────────────────────────────────────────────────────

class TestUnprotectedBlocksEverything:
    """An unprotected position is an anomaly, not a partial risk budget.

    It blocks all new trades regardless of direction or size, per the owner's
    directive, because the system's own accounting is demonstrably wrong while
    that position exists.
    """

    def test_unprotected_blocks_a_tiny_order(self, risk):
        intent = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=0.01,
            entry_price=1.10000, stop_loss=1.09999)
        decision = risk.evaluate(intent, healthy(unprotected_positions=1))
        assert decision.approved is False

    def test_unprotected_blocks_a_large_order(self, risk):
        intent = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=5.0,
            entry_price=1.10000, stop_loss=1.09000)
        decision = risk.evaluate(intent, healthy(unprotected_positions=1))
        assert decision.approved is False

    def test_unprotected_blocks_a_reducing_order(self, risk):
        """Even an order that would net risk down is refused.

        A new order with a new stop is not a hedge; it is a new position that
        assumes the anomaly has been understood.
        """
        intent = risk.build_order_intent(
            symbol="EURUSD", side="SELL", volume=0.01,
            entry_price=1.10000, stop_loss=1.10001)
        decision = risk.evaluate(intent, healthy(unprotected_positions=1))
        assert decision.approved is False
        assert any("no stop loss" in v for v in decision.violations)

    def test_the_live_situation_would_block(self, risk):
        """Your account currently holds GBPUSD with sl=0.

        With the gate wired, that position alone prevents any new trade. This
        is the intended behaviour and the reason the HUD showed UNPROTECTED.
        """
        state = risk.account_state_from_positions(
            equity=99_989.89, start_of_day_equity=99_999.25,
            peak_equity=100_000.0,
            open_positions=[
                {"symbol": "USDCAD", "volume": 0.01, "price_open": 1.41802,
                 "sl": 0.0, "magic": 100100},
                {"symbol": "GBPUSD", "volume": 0.01, "price_open": 1.32951,
                 "sl": 0.0, "magic": 100100},
            ],
        )
        assert state.unprotected_positions == 2

        intent = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=0.01,
            entry_price=1.10000, stop_loss=1.09900)
        decision = risk.evaluate(intent, state)
        assert decision.approved is False, (
            "two unprotected positions must block all new trading"
        )

    def test_clearing_the_anomaly_unblocks_trading(self, risk):
        """Once every position has a stop, trading resumes."""
        state = risk.account_state_from_positions(
            equity=100_000.0, start_of_day_equity=100_000.0,
            peak_equity=100_000.0,
            open_positions=[{"symbol": "EURUSD", "volume": 0.10,
                             "price_open": 1.10000, "sl": 1.09900,
                             "magic": 100100}],
        )
        assert state.unprotected_positions == 0
        intent = risk.build_order_intent(
            symbol="EURUSD", side="BUY", volume=0.10,
            entry_price=1.10000, stop_loss=1.09900)
        assert risk.evaluate(intent, state).approved is True


# ─────────────────────────────────────────────────────────────────────────────
# Live specs match the shipped probe output
# ─────────────────────────────────────────────────────────────────────────────

class TestShippedSpecAssumptions:

    def test_probe_script_exists(self):
        assert (REPO_ROOT / "scripts" / "probe_tick_specs.py").exists()

    def test_probe_covers_every_whitelisted_symbol(self):
        """A symbol in the whitelist but absent from the probe is unverified."""
        import yaml
        raw = yaml.safe_load(
            (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
        source = (REPO_ROOT / "scripts" / "probe_tick_specs.py").read_text(
            encoding="utf-8")
        missing = [s for s in raw["trading"]["tradable_symbols"]
                   if s not in source]
        assert not missing, (
            f"these whitelist symbols are not probed and would be unsized: "
            f"{missing}"
        )

    def test_probe_fails_when_a_whitelisted_symbol_is_absent(self):
        """A broker-missing symbol is a config error, not a pass.

        Six whitelist entries are not offered by MetaQuotes-Demo, so the probe
        must exit non-zero and name them rather than reporting a clean pass.
        """
        source = (REPO_ROOT / "scripts" / "probe_tick_specs.py").read_text(
            encoding="utf-8")
        assert "if missing_symbol:" in source, (
            "the probe must treat broker-absent symbols as a failure"
        )
        assert "RESULT: FAIL" in source
        assert "not offered by this broker" in source

    def test_probe_is_read_only(self):
        """It must never place an order.

        Checked on executable code rather than the raw text: the module
        documents that it calls neither order_send nor order_check, and those
        names appear in that prose. Stripping comments and docstrings first
        keeps the check meaningful instead of tripping over the documentation.
        """
        import ast
        import io
        import tokenize

        path = REPO_ROOT / "scripts" / "probe_tick_specs.py"
        source = path.read_text(encoding="utf-8")

        # Rebuild the source with comments and docstrings removed.
        out = []
        prev_type = tokenize.INDENT
        last_lineno = -1
        last_col = 0
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING and prev_type in (
                    tokenize.INDENT, tokenize.NEWLINE, tokenize.NL,
                    tokenize.DEDENT, None):
                # A bare string expression: a docstring.
                continue
            if tok.type == tokenize.NL and prev_type == tokenize.COMMENT:
                continue
            if tok.start[0] > last_lineno:
                last_col = 0
            if tok.start[1] < last_col:
                continue
            out.append(tok)
            prev_type = tok.type
            last_lineno, last_col = tok.end

        executable = tokenize.untokenize(out)

        # The check itself, on executable code only.
        assert "order_send" not in executable, (
            "the tick probe must stay read-only; it may not send orders"
        )
        assert "order_check" not in executable

        # And a belt-and-braces AST pass: no attribute access on the module.
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("order_send", "order_check"), (
                    f"probe calls mt5.{node.attr} on line {node.lineno}"
                )

    def test_every_shipped_whitelist_symbol_is_probed(self):
        """A symbol in the whitelist but absent from the probe would be unsized."""
        import yaml
        raw = yaml.safe_load(
            (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
        source = (REPO_ROOT / "scripts" / "probe_tick_specs.py").read_text(
            encoding="utf-8")
        probed = raw["trading"]["tradable_symbols"]
        missing = [s for s in probed if s not in source]
        assert not missing, (
            f"these whitelist symbols are not in the probe's default list and "
            f"would be unverified: {missing}"
        )