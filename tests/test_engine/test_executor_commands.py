"""Executor, risk, command-channel and config tests.

The safety rules under test:

  - PAPER is the default.
  - LIVE needs both the config flag and an explicit confirmation.
  - An order without a stop loss is rejected.
  - An unapproved order is rejected.
  - PAUSED and EMERGENCY_STOP block submissions, but never a close.
  - Commands move the state machine, and unknown commands fail loudly.

Run: python -m pytest tests/test_engine/test_executor_commands.py -v
"""
import types

import pytest

from engine.commands import (
    COMMAND_CLOSE_ALL,
    COMMAND_CLOSE_POSITION,
    COMMAND_EMERGENCY_STOP,
    COMMAND_ENABLE_AI,
    COMMAND_RESET_EMERGENCY,
    KNOWN_COMMANDS,
    NO_CONFIRMATION_COMMANDS,
    CommandProcessor,
)
from engine.config import (
    MODE_LIVE,
    MODE_PAPER,
    EngineSettings,
    RiskLimits,
    load_engine_config,
)
from engine.executor import (
    MODE_PAPER as EXEC_PAPER,
    ExecutionRejected,
    LiveExecutor,
    OrderRequest,
    PaperExecutor,
    build_executor,
)
from engine.journal import (
    STATE_EMERGENCY_STOP,
    STATE_IDLE,
    STATE_OFF,
    STATE_PAUSED,
    Journal,
)
from engine.risk import RiskEngine
from engine.state import StateMachine

REPO_ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent.parent


class StubTick:
    def __init__(self, bid=1.10000, ask=1.10002):
        self.bid = bid
        self.ask = ask
        self.time = 0


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / "engine.db")
    yield j
    j.close()


@pytest.fixture
def sm(journal):
    return StateMachine(journal)


def ticks(symbol):
    return StubTick()


# ─────────────────────────────────────────────────────────────────────────────
# Risk gate
# ─────────────────────────────────────────────────────────────────────────────

class TestPhase1RiskGate:
    """Phase 1 blocks everything. That is the safe default, not an omission."""

    def test_approves_nothing(self, journal):
        risk = RiskEngine(journal)
        decision = risk.evaluate(symbol="EURUSD", side="BUY", volume=0.01,
                                 sl=1.09500)
        assert decision.approved is False
        assert decision.reasons, "a rejection must state why"
        assert bool(decision) is False

    def test_can_submit_is_false(self, journal):
        risk = RiskEngine(journal)
        assert risk.can_submit(symbol="EURUSD", side="BUY", volume=0.01,
                               sl=1.09500) is False

    def test_status_reports_the_stub(self, journal):
        status = RiskEngine(journal).status()
        assert status["phase1_stub"] is True
        assert status["blocks_all_orders"] is True

    def test_every_rejection_is_recorded(self, journal):
        risk = RiskEngine(journal)
        risk.evaluate(symbol="EURUSD", side="BUY", volume=0.01, sl=1.09500)
        rows = journal.query("SELECT * FROM risk_decisions")
        assert rows, "risk decisions must be journaled"
        assert rows[-1]["approved"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# PaperExecutor
# ─────────────────────────────────────────────────────────────────────────────

class TestPaperExecutor:

    def _executor(self, journal, state=None):
        return PaperExecutor(journal=journal, state=state,
                             tick_provider=ticks)

    def test_fill_opens_a_position(self, journal):
        ex = self._executor(journal)
        result = ex.submit(
            ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
            risk_approved=True)
        assert result.ok is True
        assert result.ticket is not None
        assert result.price == pytest.approx(1.10002)   # ask for a BUY
        assert journal.open_position_count() == 1

    def test_sell_fills_at_bid(self, journal):
        ex = self._executor(journal)
        result = ex.submit(
            ex.prepare_order("EURUSD", "SELL", 0.01, 1.10500),
            risk_approved=True)
        assert result.price == pytest.approx(1.10000)   # bid for a SELL

    def test_close_reduces_to_zero_positions(self, journal):
        ex = self._executor(journal)
        fill = ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
                         risk_approved=True)
        closed = ex.close(fill.ticket)
        assert closed.ok is True
        assert journal.open_position_count() == 0

    def test_close_of_unknown_ticket_is_not_an_error(self, journal):
        ex = self._executor(journal)
        result = ex.close(999_999)
        assert result.ok is False
        assert result.status == "NOT_FOUND"

    def test_stop_loss_closes_the_position(self, journal):
        ex = self._executor(journal)
        fill = ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.10500),
                         risk_approved=True)
        # Price falls through the stop.
        ex.tick_provider = lambda s: StubTick(bid=1.09000, ask=1.09002)
        closed = ex.on_tick("EURUSD")
        assert closed, "a stop hit must close the position"
        assert journal.open_position_count() == 0

    def test_take_profit_closes_the_position(self, journal):
        ex = self._executor(journal)
        fill = ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500, tp=1.10500),
                         risk_approved=True)
        ex.tick_provider = lambda s: StubTick(bid=1.11000, ask=1.11002)
        closed = ex.on_tick("EURUSD")
        assert closed, "a take-profit hit must close the position"
        assert journal.open_position_count() == 0

    def test_mark_to_market_updates_profit(self, journal):
        ex = self._executor(journal)
        ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09000),
                  risk_approved=True)
        ex.tick_provider = lambda s: StubTick(bid=1.11000, ask=1.11002)
        ex.on_tick("EURUSD")
        rows = journal.query("SELECT * FROM positions")
        assert rows[0]["profit"] > 0, "a rising price must show a profit"


class TestExecutorSafetyGates:
    """Rules that apply to both execution modes."""

    @pytest.mark.parametrize("sl", [0, 0.0, -1.0, None])
    def test_order_without_sl_is_rejected(self, journal, sl):
        ex = PaperExecutor(journal=journal, tick_provider=ticks)
        request = OrderRequest(symbol="EURUSD", side="BUY", volume=0.01, sl=sl)
        with pytest.raises(ExecutionRejected) as excinfo:
            ex.submit(request, risk_approved=True)
        assert "stop loss" in str(excinfo.value).lower()
        assert journal.open_position_count() == 0

    def test_unapproved_order_is_rejected(self, journal):
        ex = PaperExecutor(journal=journal, tick_provider=ticks)
        request = ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500)
        with pytest.raises(ExecutionRejected) as excinfo:
            ex.submit(request, risk_approved=False)
        assert "risk gate" in str(excinfo.value)
        assert journal.open_position_count() == 0

    def test_paused_blocks_submission(self, journal):
        ex = PaperExecutor(journal=journal, tick_provider=ticks,
                           state=STATE_PAUSED)
        with pytest.raises(ExecutionRejected) as excinfo:
            ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
                      risk_approved=True)
        assert "PAUSED" in str(excinfo.value)

    def test_emergency_blocks_submission(self, journal):
        ex = PaperExecutor(journal=journal, tick_provider=ticks,
                           state=STATE_EMERGENCY_STOP)
        with pytest.raises(ExecutionRejected) as excinfo:
            ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
                      risk_approved=True)
        assert "EMERGENCY_STOP" in str(excinfo.value)

    def test_closing_is_allowed_while_emergency(self, journal):
        """Closing reduces risk; it must never be gated by a halt."""
        ex = PaperExecutor(journal=journal, tick_provider=ticks)
        fill = ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
                         risk_approved=True)
        ex.state = STATE_EMERGENCY_STOP
        result = ex.close(fill.ticket)
        assert result.ok is True, "a halt must not prevent closing a position"


class TestModeSelection:

    def test_paper_is_the_default(self, journal):
        ex = build_executor(EXEC_PAPER, journal=journal, tick_provider=ticks)
        assert ex.mode == EXEC_PAPER
        assert isinstance(ex, PaperExecutor)

    def test_live_requires_confirmation(self, journal):
        """The config flag alone must not enable live trading."""
        with pytest.raises(ExecutionRejected) as excinfo:
            build_executor(EXEC_PAPER and MODE_LIVE, journal=journal,
                           live_confirmed=False)
        assert "confirmation" in str(excinfo.value).lower()

    def test_live_with_confirmation_is_constructible(self, journal):
        ex = build_executor(MODE_LIVE, journal=journal, live_confirmed=True)
        assert isinstance(ex, LiveExecutor)
        assert ex.mode == MODE_LIVE

    def test_live_executor_refuses_construction_unconfirmed(self, journal):
        with pytest.raises(ExecutionRejected):
            LiveExecutor(journal=journal, live_confirmed=False)

    def test_unknown_mode_is_refused(self, journal):
        with pytest.raises(ExecutionRejected) as excinfo:
            build_executor("YOLO", journal=journal)
        assert "unknown mode" in str(excinfo.value)

    def test_live_still_requires_sl_and_risk(self, journal):
        ex = build_executor(MODE_LIVE, journal=journal, live_confirmed=True)
        request = ex.prepare_order("EURUSD", "BUY", 0.01, 0.0)
        with pytest.raises(ExecutionRejected):
            ex.submit(request, risk_approved=True)


# ─────────────────────────────────────────────────────────────────────────────
# Command channel
# ─────────────────────────────────────────────────────────────────────────────

class TestCommandChannel:

    def _processor(self, journal, sm, executor=None):
        return CommandProcessor(journal, sm, executor)

    def test_pending_to_done(self, journal, sm):
        proc = self._processor(journal, sm)
        journal.enqueue_command(COMMAND_ENABLE_AI)
        proc.poll_once()
        assert sm.state == STATE_IDLE
        row = journal.query_one("SELECT * FROM commands ORDER BY id DESC LIMIT 1")
        assert row["status"] == "DONE"

    def test_unknown_command_fails(self, journal, sm):
        proc = self._processor(journal, sm)
        journal.enqueue_command("LAUNCH_MISSILES")
        proc.poll_once()
        row = journal.query_one("SELECT * FROM commands ORDER BY id DESC LIMIT 1")
        assert row["status"] == "FAILED"
        assert "unknown command" in row["reason"].lower()

    def test_failed_state_command_is_marked_failed(self, journal, sm):
        proc = self._processor(journal, sm)
        # DISABLE_AI is illegal from OFF.
        journal.enqueue_command("DISABLE_AI")
        proc.poll_once()
        row = journal.query_one("SELECT * FROM commands ORDER BY id DESC LIMIT 1")
        assert row["status"] == "FAILED"
        assert "Illegal transition" in row["reason"]

    def test_claiming_is_exclusive(self, journal, sm):
        """Two pollers must not take the same command."""
        journal.enqueue_command(COMMAND_ENABLE_AI)
        first = journal.claim_next_command()
        second = journal.claim_next_command()
        assert first is not None
        assert second is None, "a claimed command must not be re-claimed"

    def test_pending_count_decreases_after_claim(self, journal, sm):
        journal.enqueue_command(COMMAND_ENABLE_AI)
        assert journal.pending_command_count() == 1
        journal.claim_next_command()
        assert journal.pending_command_count() == 0

    def test_emergency_needs_no_confirmation(self, journal, sm):
        proc = self._processor(journal, sm)
        sm.transition(STATE_IDLE, "ENABLE_AI")
        journal.enqueue_command(COMMAND_EMERGENCY_STOP)
        proc.poll_once()
        assert sm.state == STATE_EMERGENCY_STOP
        assert COMMAND_EMERGENCY_STOP in NO_CONFIRMATION_COMMANDS

    def test_close_position_requires_a_ticket(self, journal, sm):
        proc = self._processor(journal, sm)
        journal.enqueue_command(COMMAND_CLOSE_POSITION, {})
        proc.poll_once()
        row = journal.query_one("SELECT * FROM commands ORDER BY id DESC LIMIT 1")
        assert row["status"] == "FAILED"
        assert "ticket" in row["reason"].lower()

    def test_close_position_closes(self, journal, sm):
        ex = PaperExecutor(journal=journal, tick_provider=ticks)
        proc = self._processor(journal, sm, ex)
        fill = ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
                         risk_approved=True)
        journal.enqueue_command(COMMAND_CLOSE_POSITION,
                                {"ticket": fill.ticket})
        proc.poll_once()
        assert journal.open_position_count() == 0

    def test_close_all_closes_everything(self, journal, sm):
        ex = PaperExecutor(journal=journal, tick_provider=ticks)
        proc = self._processor(journal, sm, ex)
        for _ in range(3):
            ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
                      risk_approved=True)
        assert journal.open_position_count() == 3
        journal.enqueue_command(COMMAND_CLOSE_ALL)
        proc.poll_once()
        assert journal.open_position_count() == 0

    def test_close_all_survives_one_failure(self, journal, sm):
        """A single failure must not abort the sweep."""
        ex = PaperExecutor(journal=journal, tick_provider=ticks)
        proc = self._processor(journal, sm, ex)
        for _ in range(2):
            ex.submit(ex.prepare_order("EURUSD", "BUY", 0.01, 1.09500),
                      risk_approved=True)

        real_close = ex.close

        def flaky_close(ticket):
            if ticket == 9000001:
                raise RuntimeError("simulated close failure")
            return real_close(ticket)

        ex.close = flaky_close
        journal.enqueue_command(COMMAND_CLOSE_ALL)
        proc.poll_once()
        # One closed despite the failure on the other.
        assert journal.open_position_count() == 1, (
            "the sweep must continue past one failure"
        )

    def test_reset_emergency_via_channel(self, journal, sm):
        proc = self._processor(journal, sm)
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP")
        journal.enqueue_command(COMMAND_RESET_EMERGENCY,
                                {"confirmation": "RESET"})
        proc.poll_once()
        assert sm.state == STATE_OFF

    def test_wrong_confirmation_via_channel_is_refused(self, journal, sm):
        proc = self._processor(journal, sm)
        sm.transition(STATE_IDLE, "ENABLE_AI")
        sm.transition(STATE_EMERGENCY_STOP, "EMERGENCY_STOP")
        journal.enqueue_command(COMMAND_RESET_EMERGENCY,
                                {"confirmation": "wrong"})
        proc.poll_once()
        assert sm.state == STATE_EMERGENCY_STOP

    def test_poll_until_idle_drains_the_queue(self, journal, sm):
        proc = self._processor(journal, sm)
        journal.enqueue_command(COMMAND_ENABLE_AI)
        journal.enqueue_command(COMMAND_ENABLE_AI)   # duplicate, will fail
        done = proc.poll_until_idle()
        assert len(done) == 2
        assert journal.pending_command_count() == 0

    def test_every_command_is_written_to_the_event_log(self, journal, sm):
        proc = self._processor(journal, sm)
        journal.enqueue_command(COMMAND_ENABLE_AI)
        proc.poll_once()
        messages = [e["message"] for e in journal.recent_events(50, "command")]
        assert any(COMMAND_ENABLE_AI in m for m in messages)

    def test_known_commands_are_declared(self):
        assert COMMAND_ENABLE_AI in KNOWN_COMMANDS
        assert COMMAND_EMERGENCY_STOP in KNOWN_COMMANDS
        assert COMMAND_CLOSE_ALL in KNOWN_COMMANDS


class TestDedupeKey:

    def test_duplicate_dedupe_key_is_rejected(self, journal):
        journal.enqueue_command(COMMAND_ENABLE_AI, dedupe_key="abc")
        with pytest.raises(Exception):
            journal.enqueue_command(COMMAND_ENABLE_AI, dedupe_key="abc")

    def test_null_dedupe_key_allows_duplicates(self, journal):
        journal.enqueue_command(COMMAND_ENABLE_AI)
        journal.enqueue_command(COMMAND_ENABLE_AI)
        assert journal.pending_command_count() == 2


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

class TestEngineConfig:

    def test_loads_the_real_config(self):
        cfg = load_engine_config(root=REPO_ROOT)
        assert cfg.mode in (MODE_PAPER, MODE_LIVE)

    def test_loads_the_real_config_is_paper(self):
        """The shipped config must be PAPER.

        Phase 1 is PAPER-only in practice. A config.yaml that declared LIVE
        would mean live trading was one edit away without the confirmation
        gate being exercised, so it is pinned here.
        """
        cfg = load_engine_config(root=REPO_ROOT)
        assert cfg.mode == MODE_PAPER, (
            "config.yaml must declare PAPER; LIVE requires both the flag and "
            "an operator confirmation, and Phase 1 has no strategies to trade"
        )

    def test_db_path_is_absolute(self):
        cfg = load_engine_config(root=REPO_ROOT)
        assert cfg.db_path.is_absolute(), (
            "a relative DB path would open two databases when the UI and the "
            "engine run with different working directories"
        )

    def test_backup_dir_is_absolute(self):
        cfg = load_engine_config(root=REPO_ROOT)
        assert cfg.backup_dir.is_absolute()

    def test_offline_after_exceeds_heartbeat(self):
        settings = EngineSettings(heartbeat_seconds=5, offline_after_seconds=15)
        assert settings.offline_after_seconds > settings.heartbeat_seconds

    def test_offline_after_not_greater_than_heartbeat_is_rejected(self):
        with pytest.raises(Exception):
            EngineSettings(heartbeat_seconds=30, offline_after_seconds=30)

    def test_decreasing_backoff_is_rejected(self):
        with pytest.raises(Exception):
            EngineSettings(gateway={"reconnect_backoff_seconds": [4, 2, 1]})

    def test_empty_backoff_is_rejected(self):
        with pytest.raises(Exception):
            EngineSettings(gateway={"reconnect_backoff_seconds": []})

    def test_unknown_engine_key_is_rejected(self):
        """A typo must not be silently ignored."""
        with pytest.raises(Exception):
            EngineSettings(heartbeat_secondz=5)

    def test_invalid_mode_is_refused(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.yaml"
            cfg.write_text("mode: YOLO\n", encoding="utf-8")
            with pytest.raises(Exception):
                load_engine_config(config_path=cfg)

    def test_mode_defaults_to_paper_when_absent(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.yaml"
            cfg.write_text("engine:\n  magic_number: 100100\n", encoding="utf-8")
            loaded = load_engine_config(config_path=cfg)
            assert loaded.mode == MODE_PAPER, "PAPER must be the default"

    def test_missing_config_file_is_refused(self):
        with pytest.raises(FileNotFoundError):
            load_engine_config(config_path="/nonexistent/config.yaml")

    def test_risk_per_trade_cannot_exceed_total_exposure(self):
        with pytest.raises(Exception):
            RiskLimits(risk_per_trade_pct=20.0, max_total_exposure_pct=10.0)

    def test_symbol_mapping_resolution(self):
        cfg = load_engine_config(root=REPO_ROOT)
        resolved = cfg.resolve_symbol("EURUSD")
        assert resolved is None or isinstance(resolved, str)
        assert cfg.resolve_symbol("NOT_MAPPED") is None
        assert "NOT_MAPPED" in cfg.unresolved_symbols(["NOT_MAPPED"])

    def test_crypto_stays_optional(self):
        """Owner decision: revisit in Phase 2, so keep it required: false."""
        import yaml
        raw = yaml.safe_load(
            (REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
        crypto = raw["verification"]["asset_classes"]["Crypto Assets"]
        assert crypto["required"] is False, (
            "Crypto Assets must remain optional until Phase 2"
        )

    def test_heartbeat_matches_the_documented_values(self):
        cfg = load_engine_config(root=REPO_ROOT)
        assert cfg.engine.heartbeat_seconds == 5
        assert cfg.engine.offline_after_seconds == 15
        assert cfg.engine.command_poll_seconds == 1
        assert cfg.engine.magic_number == 100100, (
            "magic 100100 was fixed by the owner"
        )