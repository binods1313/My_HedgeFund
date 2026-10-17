"""App boot test for app.py.

Uses streamlit.testing.v1.AppTest with monkeypatched ui.data_access
functions to simulate a disconnected broker.

NOTE: The full app boot via AppTest hangs because _render_account_metrics
uses @st.fragment(run_every="2s") which never settles. We therefore:
  1. Patch render_header to skip the fragment.
  2. Patch _initialize_mt5 (the seam in data_access.py) to return False.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ui.data_access import AccountSnapshot, _initialize_mt5
from ui.services.news import FetchResult


@pytest.fixture(autouse=True)
def _disconnect_broker(monkeypatch):
    degraded = AccountSnapshot(
        connected=False,
        mode="PAPER",
        engine_state="offline",
        heartbeat_age_s=None,
        currency="USD",
        balance=None,
        equity=None,
        margin_used=None,
        free_margin=None,
        margin_level=None,
        open_pnl=None,
        open_risk=None,
        fetched_at=datetime.datetime.now(),
    )
    monkeypatch.setattr("ui.data_access._initialize_mt5", lambda: False)
    monkeypatch.setattr("ui.data_access.get_account_snapshot", lambda: degraded)
    monkeypatch.setattr("ui.data_access.get_symbol_spec", lambda *a, **k: None)
    monkeypatch.setattr(
        "ui.components.snapshot.fetch_news",
        lambda *args, **kwargs: FetchResult(
            [], datetime.datetime.now(datetime.timezone.utc), "ok", [],
        ),
    )
    monkeypatch.setattr(
        "ui.components.snapshot.live_quote",
        lambda *args, **kwargs: {"valid": False, "reason": "offline"},
    )
    monkeypatch.setattr(
        "ui.components.snapshot.recent_events",
        lambda *args, **kwargs: pd.DataFrame(
            columns=["id", "ts", "category", "severity", "source", "message"],
        ),
    )
    yield


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_PATH = REPO_ROOT / "app.py"

def test_app_boots_with_no_exception():
    from streamlit.testing.v1 import AppTest
    from unittest.mock import patch

    with patch("ui.components.header._render_account_metrics"), \
         patch("streamlit.fragment", lambda **kw: lambda f: f):
        at = AppTest.from_file(APP_PATH)
        result = at.run(timeout=60)
        assert not result.error, f"App boot errors: {list(result.error)}"


def test_submit_button_is_disabled_when_disconnected():
    from streamlit.testing.v1 import AppTest
    from unittest.mock import patch

    with patch("ui.components.header._render_account_metrics"), \
         patch("streamlit.fragment", lambda **kw: lambda f: f):
        at = AppTest.from_file(APP_PATH)
        at.run(timeout=60)
        submit = at.button("btn_submit_order")
        assert submit.disabled is True


def test_category_change_resets_selected_asset():
    from streamlit.testing.v1 import AppTest
    from config_loader import load_config
    from unittest.mock import patch

    config = load_config()
    metals_whitelist = config.symbols.whitelist["metals_commodities"]

    with patch("ui.components.header._render_account_metrics"), \
         patch("streamlit.fragment", lambda **kw: lambda f: f), \
         patch("ui.data_access.get_account_snapshot") as mock_acct, \
         patch("ui.state._refresh_broker_list_on_category_change") as mock_refresh, \
         patch("ui.data_access.fetch_live_broker_symbols", return_value=config.symbols.whitelist):
        mock_acct.return_value = AccountSnapshot(
            connected=False, mode="PAPER", engine_state="offline",
            heartbeat_age_s=None, currency="USD", balance=None, equity=None,
            margin_used=None, free_margin=None, margin_level=None,
            open_pnl=None, open_risk=None, fetched_at=datetime.datetime.now(),
        )
        at = AppTest.from_file(APP_PATH)
        at.run(timeout=60)

        cat_select = at.selectbox("w_asset_category")
        cat_select.select("metals_commodities")
        at.run(timeout=30)

        asset_select = at.selectbox("w_selected_asset")
        assert asset_select.value in metals_whitelist, (
            f"selected_asset {asset_select.value!r} not in metals whitelist {metals_whitelist}"
        )


def test_exchange_controls_and_close_actions_stay_wired():
    from streamlit.testing.v1 import AppTest
    from unittest.mock import patch

    from ui.data_access import POSITION_COLUMNS

    positions = pd.DataFrame(
        [{
            "ticket": 1001,
            "symbol": "EURUSD",
            "side": "BUY",
            "lots": 0.01,
            "entry": 1.1,
            "current": 1.1002,
            "sl": 0.0,
            "tp": 1.2,
            "pnl": -3.67,
            "risk_usd": float("inf"),
            "opened_at": "2026-10-07T08:00:00+00:00",
        }],
        columns=list(POSITION_COLUMNS),
    )
    with patch("ui.components.header._render_account_metrics"), \
         patch("streamlit.fragment", lambda **kw: lambda f: f), \
         patch("ui.components.snapshot.get_active_positions", return_value=positions):
        at = AppTest.from_file(APP_PATH)
        result = at.run(timeout=60)
        assert not result.error, f"App boot errors: {list(result.error)}"
        side = at.segmented_control("w_order_side")
        assert list(side.options) == ["BUY", "SELL"]
        assert at.selectbox("w_order_type").value == "Market"
        assert at.button("btn_sync_feed").label == "Sync"
        assert at.segmented_control("w_timeframe").value
        assert at.button("close_1001").label == "Close"
        assert at.button("btn_close_all").label == "Emergency close all"
        assert at.button("btn_submit_order").disabled is True
