"""Task 7 defect and provider checks. No network."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from config_ext_loader import InfowaySettings
from engine.keys import Keyring
from quote_clock import broker_tick_age_seconds
from ui.chart_fit import fits_all_viewports, fitted_donut_font_size
from ui.components.cards import _html_table
from ui.components.finviz_links import finviz_links
from ui.components.ideas_watchlist import Idea, add_idea, ideas_frame, load_ideas
from ui.equity_curve import clean_equity_points, equity_caption
from ui.indicators import atr, closed_bar_index, indicator_chip_text, macd, rsi
from ui.risk_calc import risk_card_accent, risk_summary_level
from ui.services.infoway_client import (
    HttpResponse,
    clear_cache,
    fetch_candles,
    fetch_quote,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "infoway"
NOW = 1_700_000_000.0


def test_risk_card_is_green_only_when_nothing_failed() -> None:
    assert risk_card_accent([]) == "green"
    assert risk_card_accent(["TOTAL_RISK_EXCEEDED"]) == "red"
    assert risk_card_accent(["SL_REQUIRED"]) == "red"
    assert risk_card_accent(["MARGIN_INSUFFICIENT"]) == "red"
    assert risk_card_accent(["STOP_TOO_CLOSE"]) == "red"
    assert risk_card_accent(["SPREAD_TOO_WIDE"]) == "amber"
    assert risk_summary_level("green", 0.4) == "success"
    assert risk_summary_level("red", 0.4) == "error"
    assert risk_summary_level("amber", 0.2) == "warning"
    assert risk_summary_level("red", None) == "info"
    source = (ROOT / "ui" / "components" / "order_form.py").read_text(encoding="utf-8")
    assert "risk_card_accent" in source
    assert "risk_summary_level" in source


def test_broker_clock_never_returns_a_negative_age() -> None:
    assert broker_tick_age_seconds(NOW + 10797.7, NOW) == pytest.approx(2.3, abs=0.05)
    assert broker_tick_age_seconds(NOW + 19800, NOW) == 0.0
    assert broker_tick_age_seconds(NOW + 10, NOW) == 0.0
    assert broker_tick_age_seconds(NOW - 600, NOW) == 600
    ahead = 3 * 3600 - 20 * 60
    assert broker_tick_age_seconds(NOW + ahead, NOW) == 0.0
    fresh_plus_ten_minutes = 3 * 3600 - 600
    assert broker_tick_age_seconds(NOW + fresh_plus_ten_minutes, NOW) == pytest.approx(600)
    source = (ROOT / "data_engine.py").read_text(encoding="utf-8")
    assert "broker_tick_age_seconds" in source
    assert "time.time() - tick.time" not in source


def test_selected_indicator_chips_use_symbol_digits() -> None:
    close = [100 + i * 0.4 for i in range(40)]
    high = [price + 0.5 for price in close]
    low = [price - 0.5 for price in close]
    closed = closed_bar_index(len(close))
    line, _signal, _hist = macd(close)
    shown = [
        indicator_chip_text(rsi(close), closed, 5),
        indicator_chip_text(atr(high, low, close), closed, 5),
        indicator_chip_text(line, closed, 5),
    ]
    for text in shown:
        assert text != "—"
        assert "e" not in text.lower()
        assert "." in text
    assert indicator_chip_text(None, closed, 5) == "—"
    assert indicator_chip_text(rsi(close), 0, 5) == "—"
    source = (ROOT / "ui" / "components" / "chart.py").read_text(encoding="utf-8")
    assert "indicator_chip_text" in source
    assert "closed_bar_index" in source


def test_margin_centre_fits_the_narrow_column() -> None:
    label = "487,537.5%"
    size = fitted_donut_font_size(label)
    assert 7 <= size <= 22
    assert fits_all_viewports(label, size)
    assert fits_all_viewports("—")


def test_equity_caption_drops_cliffs_and_hides_raw_numbers() -> None:
    gap = clean_equity_points([0, 10, 20, 5000, 5010], [10, 0, 12, 30, 31], max_gap_s=1800)
    assert [point[1] for point in gap] == [30, 31]
    cleaned = clean_equity_points([0, 10, 20, 30], [100, 100, 10, 101])
    assert [point[1] for point in cleaned] == [100, 100, 101]
    fallback = clean_equity_points([0, 10, 20], [100, 1, 1000])
    assert [point[1] for point in fallback] == [100, 1, 1000]
    sentence = equity_caption(100.0, 90.0)
    assert "fell" in sentence
    assert "risk ·" not in sentence
    assert "99991" not in sentence
    assert not any(character.isdigit() for character in sentence)


def test_countdown_is_not_under_the_theme_studio() -> None:
    header = (ROOT / "ui" / "components" / "header.py").read_text(encoding="utf-8")
    body = header.split("def _draw_status", 1)[1].split("def _draw_metrics", 1)[0]
    assert body.index("render_theme_studio()") < body.index("Market quote")
    assert body.index("Market quote") < body.index("news-countdown")
    theme = (ROOT / "ui" / "theme.py").read_text(encoding="utf-8")
    assert "news-countdown" in theme
    assert "margin-top:0" in theme


def test_header_and_portfolio_share_one_snapshot() -> None:
    source = (ROOT / "tests" / "test_ui" / "test_snapshot_identity.py").read_text(encoding="utf-8")
    assert "loaded is header is portfolio" in source
    assert 'calls == {"account": 1, "positions": 1}' in source


def _settings() -> InfowaySettings:
    return InfowaySettings(daily_cap=5, timeout_s=5, cache_ttl_s=30)


def _ring() -> Keyring:
    return Keyring(env={"INFOWAY_API_KEY": "not-a-secret"})


def _transport(name: str):
    body = (FIXTURES / name).read_bytes()
    seen = {"n": 0, "url": "", "header": ""}

    def send(request):
        seen["n"] += 1
        seen["url"] = request.url
        seen["header"] = request.headers.get("apiKey", "")
        return HttpResponse(200, body)

    return send, seen


def test_infoway_quote_and_candles_use_fixtures(tmp_path: Path) -> None:
    clear_cache()
    send, seen = _transport("trade_ok.json")
    result = fetch_quote(
        "EURUSD",
        transport=send,
        ring=_ring(),
        settings=_settings(),
        budget_path=tmp_path / "budget.json",
        now=NOW,
    )
    assert result.status == "ok"
    assert result.price == "1.08501"
    assert seen["n"] == 1
    assert seen["url"] == "https://data.infoway.io/common/batch_trade/EURUSD"
    assert "apiKey=" not in seen["url"]
    assert seen["header"] == "not-a-secret"
    assert "not-a-secret" not in seen["url"]
    again = fetch_quote(
        "EURUSD",
        transport=send,
        ring=_ring(),
        settings=_settings(),
        budget_path=tmp_path / "budget.json",
        now=NOW,
    )
    assert again.price == "1.08501"
    assert seen["n"] == 1
    clear_cache()
    candles, calls = _transport("kline_ok.json")
    bars = fetch_candles(
        "eurusd",
        transport=candles,
        ring=_ring(),
        settings=_settings(),
        budget_path=tmp_path / "candles.json",
        now=NOW,
    )
    assert bars.status == "ok"
    assert bars.candles[0].c == "1.08500"
    assert calls["url"] == "https://data.infoway.io/common/v2/batch_kline"


def test_infoway_degrades_without_a_retry(tmp_path: Path) -> None:
    clear_cache()
    send, seen = _transport("trade_ret_error.json")
    failed = fetch_quote(
        "EURUSD",
        transport=send,
        ring=_ring(),
        settings=_settings(),
        budget_path=tmp_path / "budget.json",
        now=NOW,
    )
    assert failed.ok is False
    assert failed.status == "degraded"
    assert seen["n"] == 1
    clear_cache()
    extra, extra_seen = _transport("trade_extra.json")
    rejected = fetch_quote(
        "EURUSD",
        transport=extra,
        ring=_ring(),
        settings=_settings(),
        budget_path=tmp_path / "extra.json",
        now=NOW,
    )
    assert rejected.status == "degraded"
    assert extra_seen["n"] == 1

    def timeout(_request):
        seen["n"] += 1
        raise TimeoutError("timeout")

    seen["n"] = 0
    clear_cache()
    stalled = fetch_quote(
        "XAUUSD",
        transport=timeout,
        ring=_ring(),
        settings=_settings(),
        budget_path=tmp_path / "timeout.json",
        now=NOW,
    )
    assert stalled.status == "degraded"
    assert stalled.detail == "timeout"
    assert seen["n"] == 1


def test_infoway_cap_placeholder_and_no_order_path(tmp_path: Path) -> None:
    clear_cache()
    send, seen = _transport("trade_ok.json")
    tight = InfowaySettings(daily_cap=1, timeout_s=5, cache_ttl_s=30)
    first = fetch_quote(
        "EURUSD",
        transport=send,
        ring=_ring(),
        settings=tight,
        budget_path=tmp_path / "budget.json",
        now=NOW,
    )
    second = fetch_quote(
        "XAUUSD",
        transport=send,
        ring=_ring(),
        settings=tight,
        budget_path=tmp_path / "budget.json",
        now=NOW,
        refresh=True,
    )
    assert first.status == "ok"
    assert second.status == "capped"
    assert seen["n"] == 1
    saved = json.loads((tmp_path / "budget.json").read_text(encoding="utf-8"))
    assert set(saved) == {"date", "count"}
    assert saved["count"] == 1
    clear_cache()
    skipped = fetch_quote(
        "EURUSD",
        transport=send,
        ring=Keyring(env={"INFOWAY_API_KEY": "YOUR_INFOWAY_API_KEY"}),
        settings=tight,
        budget_path=tmp_path / "empty.json",
        now=NOW,
    )
    assert skipped.status == "not_configured"
    assert seen["n"] == 1
    blocked = fetch_quote(
        "600519.SH",
        transport=send,
        ring=_ring(),
        settings=_settings(),
        budget_path=tmp_path / "symbol.json",
        now=NOW,
    )
    assert blocked.status == "degraded"
    assert seen["n"] == 1
    source = (ROOT / "ui" / "services" / "infoway_client.py").read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "MetaTrader5" not in source
    assert "submit_order" not in source
    assert "close_position" not in source


def test_finviz_links_do_not_guess_equity_pages() -> None:
    forex = finviz_links("EURUSD", "forex")
    assert forex == [("Finviz forex", "https://finviz.com/forex.ashx")]
    metal = finviz_links("XAUUSD", "metals_commodities")
    assert metal == [("Finviz futures", "https://finviz.com/futures.ashx")]
    equity = finviz_links("AAPL", "stocks")
    assert equity == [("Finviz AAPL", "https://finviz.com/quote.ashx?t=AAPL")]
    assert all("finviz.com" in url for _label, url in forex + metal + equity)


def test_ideas_watchlist_is_local_and_escaped(tmp_path: Path) -> None:
    path = tmp_path / "ideas_watchlist.json"
    saved = add_idea("desk", "EURUSD", "watch", "<script>alert(1)</script>", path=path)
    assert isinstance(saved, Idea)
    loaded = load_ideas(path)
    assert loaded[0].note.startswith("<script>")
    page = _html_table(ideas_frame(loaded), empty="—")
    assert "<script>" not in page
    assert "&lt;script&gt;" in page
    with pytest.raises(Exception):
        add_idea("desk", "EURUSD", "copy", "no", path=path)
    source = (ROOT / "ui" / "components" / "ideas_watchlist.py").read_text(encoding="utf-8")
    assert "submit_order" not in source
    assert "close_position" not in source
