from __future__ import annotations

import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from data_engine import TIMEFRAME_D1, TIMEFRAME_H1, TIMEFRAME_H4, TIMEFRAME_M15, TIMEFRAME_W1
from ui.components.data_cache import chart_data
from ui.indicators import (
    DEFAULT_INDICATORS,
    MAX_INDICATORS,
    Signal,
    StopPlan,
    atr,
    bollinger,
    bollinger_signals,
    clamp_indicators,
    closed_bar_index,
    ema,
    ema_cross_signals,
    format_fixed,
    indicator_chip_text,
    macd,
    macd_cross_signals,
    plan_stops,
    rsi,
    rsi_signals,
)
from learning.bars import publish_closed_rows
from ui.components.pattern_hub import render_pattern_matrix
from ui.patterns import (
    FILTER_CHOICES,
    allows_pattern,
    describe_trigger,
    detect_patterns,
    marker_label,
    pattern_key,
    sentiment_text,
)
from ui.state import persist_chart_style, persist_timeframe
from ui.theme import TOKENS, hex_rgba, plotly_theme, tokens

TIMEFRAMES = {
    "15M": (TIMEFRAME_M15, 120),
    "1H": (TIMEFRAME_H1, 120),
    "4H": (TIMEFRAME_H4, 120),
    "1D": (TIMEFRAME_D1, 120),
    "1Y": (TIMEFRAME_D1, 252),
    "All": (TIMEFRAME_W1, 1000),
}

_BREAK_EVEN = "Break-even win rate at 1:3 is 25% before spread/commission."
_CLOSED_CAPTION = "Signals use closed bars only. The forming bar is excluded, so markers do not repaint."
_TP_CAPTION = (
    "Take-profit on this chart is display-only. "
    "Use in order form fills side and stop only and does not submit. "
    "The market order path can send a take-profit; this button does not."
)
_PATTERN_CAPTION = (
    "Pattern marks are shapes on the candle. Hover a mark for the short name, "
    "the sentiment, and the threshold that fired."
)
_MARK_LIMIT = 12


def render_chart(asset: str | None) -> pd.DataFrame:
    with st.container(border=True, key="card-cyan-market"):
        st.html('<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><polyline points="1,13 6,7 10,10 16,3" fill="none" stroke="currentColor" stroke-width="2"/></svg>')
        st.subheader("Market workspace")
        control_cols = st.columns([1.15, 1.7, 0.55, 1.25], gap="small")
        with control_cols[0]:
            st.segmented_control(
                "Chart style",
                ["Candlestick", "Area", "Bar"],
                key="w_chart_style",
                on_change=persist_chart_style,
                label_visibility="collapsed",
            )
        with control_cols[1]:
            st.segmented_control(
                "Timeframe",
                list(TIMEFRAMES),
                key="w_timeframe",
                on_change=persist_timeframe,
                label_visibility="collapsed",
            )
        with control_cols[2]:
            sync = st.button("Sync", key="btn_sync_feed", help="Refresh market data")
        with control_cols[3]:
            hub_open = bool(st.session_state.get("pattern_hub"))
            st.button(
                "Close analysis hub" if hub_open else "Expand analysis hub",
                key="btn_pattern_hub",
                width="stretch",
                on_click=_toggle_hub,
            )

        selected = _indicator_row()
        if not asset:
            frame = pd.DataFrame()
        else:
            timeframe_name = st.session_state.get("timeframe", "1H")
            timeframe, count = TIMEFRAMES.get(timeframe_name, TIMEFRAMES["1H"])
            if sync:
                chart_data.clear()
            result = chart_data(asset, timeframe, count, int(time.time()) // 2)
            frame = result if isinstance(result, pd.DataFrame) else pd.DataFrame()
            _publish_closed(asset, timeframe_name, frame)

        pack = _pack(frame, selected, asset)
        visible = _visible_patterns(frame, pack.get("patterns") or [])
        _attach_pattern_lane(pack, visible)
        _remember_closed_bar(frame, selected, pack)
        if hub_open and asset:
            chart_col, matrix_col = st.columns([3, 2], gap="small")
            with chart_col:
                _draw_chart(frame, asset, selected, pack, visible, tall=True)
                _render_readout(frame, pack)
            with matrix_col:
                render_pattern_matrix(
                    asset, frame, visible, _indicator_notes(pack),
                    lambda hit: _deploy_pattern(asset, hit, pack),
                )
        else:
            _draw_chart(frame, asset, selected, pack, visible, tall=False)
            _render_readout(frame, pack)
        _render_stack(frame, pack)
        _render_levels(asset, pack)
        return frame


_PUBLISH_TF = {
    "15M": "M15",
    "1H": "H1",
    "4H": "H4",
    "1D": "D1",
    "1Y": "D1",
    "All": "W1",
}


def _publish_closed(asset: str, timeframe_name: str, frame: pd.DataFrame) -> None:
    """Store closed bars for shadow mode. The last row is still forming."""
    try:
        if frame is None or frame.empty or len(frame) < 2:
            return
        if not {"time", "open", "high", "low", "close"}.issubset(frame.columns):
            return
        rows = []
        for record in frame.iloc[:-1].to_dict("records"):
            stamp = record.get("time")
            if hasattr(stamp, "isoformat"):
                stamp = stamp.isoformat()
            row = {
                "time": "" if stamp is None else str(stamp),
                "open": record.get("open"),
                "high": record.get("high"),
                "low": record.get("low"),
                "close": record.get("close"),
            }
            # MT5 `spread` is points. Only a price-distance column is stored.
            if "spread_price" in record:
                row["spread"] = record.get("spread_price")
            rows.append(row)
        publish_closed_rows(asset, _PUBLISH_TF.get(timeframe_name, "H1"), rows)
    except OSError:
        return


def _draw_chart(frame, asset, selected, pack, patterns, *, tall: bool) -> None:
    fig = _build_figure(frame, asset, selected, pack, patterns, tall=tall)
    st.plotly_chart(
        fig, width="stretch", config={"displayModeBar": False}, key="chart_main",
    )
    st.caption(_CLOSED_CAPTION)
    st.caption(_PATTERN_CAPTION)


def _toggle_hub() -> None:
    st.session_state["pattern_hub"] = not bool(st.session_state.get("pattern_hub"))


def _indicator_row() -> list[str]:
    left, right = st.columns([2, 1], gap="small")
    with left:
        selected = _indicator_control()
    with right:
        st.selectbox("Patterns filter", list(FILTER_CHOICES), key="w_pattern_filter")
    return selected


def _indicator_control() -> list[str]:
    st.session_state.setdefault("w_chart_indicators", list(DEFAULT_INDICATORS))
    picked = st.multiselect(
        "Indicators",
        ["EMA20", "EMA50", "RSI", "MACD", "ATR", "Bollinger"],
        key="w_chart_indicators",
        max_selections=MAX_INDICATORS,
    )
    selected = clamp_indicators(picked)
    if selected != list(picked):
        st.session_state["w_chart_indicators"] = selected
    return selected


def _remember_closed_bar(frame: pd.DataFrame, selected: list[str], pack: dict) -> None:
    bar_id = None
    if not frame.empty and "time" in frame and len(frame) >= 2:
        bar_id = str(frame["time"].iloc[-2])
    signature = (bar_id, tuple(selected))
    if st.session_state.get("_signal_signature") != signature:
        st.session_state["_signal_signature"] = signature
        st.session_state["_signal_pack"] = pack


def _as_float(frame: pd.DataFrame, column: str) -> list[float]:
    if column not in frame:
        return []
    values: list[float] = []
    for value in frame[column].tolist():
        if value is None or (isinstance(value, float) and value != value):
            return []
        values.append(float(value))
    return values


def _pack(frame: pd.DataFrame, selected: list[str], asset: str | None) -> dict:
    empty: dict = {"signals": {}, "series": {}, "latest": None, "plan": None, "digits": 5}
    if frame.empty:
        return empty
    close = _as_float(frame, "close")
    high = _as_float(frame, "high")
    low = _as_float(frame, "low")
    if not close:
        return empty
    upto = max(0, len(close) - 1)
    series: dict[str, object] = {}
    signals: dict[str, list[Signal]] = {}
    if "EMA20" in selected:
        series["EMA20"] = ema(close, 20)
        if "EMA50" in selected:
            series["EMA50"] = ema(close, 50)
            signals["EMA"] = ema_cross_signals(series["EMA20"], series["EMA50"], upto=upto)  # type: ignore[arg-type]
    elif "EMA50" in selected:
        series["EMA50"] = ema(close, 50)
    if "RSI" in selected:
        series["RSI"] = rsi(close, 14)
        signals["RSI"] = rsi_signals(series["RSI"], upto=upto)  # type: ignore[arg-type]
    if "MACD" in selected:
        line, signal, hist = macd(close)
        series["MACD"] = line
        series["MACD-signal"] = signal
        series["MACD-hist"] = hist
        signals["MACD"] = macd_cross_signals(line, signal, upto=upto)
    atr_series = atr(high, low, close, 14) if high and low else [None] * len(close)
    if "ATR" in selected:
        series["ATR"] = atr_series
    if "Bollinger" in selected:
        mid, upper, lower = bollinger(close, 20, 2.0)
        series["BB-mid"] = mid
        series["BB-upper"] = upper
        series["BB-lower"] = lower
        signals["Bollinger"] = bollinger_signals(close, upper, lower, upto=upto)
    latest = _latest(signals)
    plan = _plan(close, atr_series, latest, asset) if latest else None
    digits = 5
    shared = st.session_state.get("_run_snapshot")
    if shared is not None:
        quote = shared.quote or {}
        try:
            digits = int(quote.get("digits") or 5)
        except (TypeError, ValueError):
            digits = 5
    hits = _pattern_hits(frame, close, high, low, upto)
    return {
        "signals": signals,
        "series": series,
        "latest": latest,
        "plan": plan,
        "digits": digits,
        "close": close,
        "patterns": hits,
    }


def _pattern_hits(
    frame: pd.DataFrame,
    close: list[float],
    high: list[float],
    low: list[float],
    upto: int,
) -> list:
    """Every closed-bar pattern in the window. Display data only."""
    opened = _as_float(frame, "open")
    if not opened or not high or not low or len(opened) != len(close):
        return []
    from config_ext_loader import load_config_ext

    return detect_patterns(
        opened, high, low, close, upto=upto, rr=float(load_config_ext().chart.rr),
    )


def _visible_patterns(frame: pd.DataFrame, hits: list) -> list:
    if frame.empty or "time" not in frame:
        return []
    choice = st.session_state.get("w_pattern_filter", "All patterns")
    hidden = set(st.session_state.get("pattern_hidden") or [])
    visible = []
    for hit in hits:
        if hit.index >= len(frame):
            continue
        token = pattern_key(str(frame["time"].iloc[hit.index]), hit.name)
        if token in hidden or not allows_pattern(hit.name, choice):
            continue
        visible.append(hit)
    return visible


def _attach_pattern_lane(pack: dict, visible: list) -> None:
    if not visible:
        pack["signals"].pop("Pattern", None)
        return
    hit = visible[-1]
    side = {"bullish": "BUY", "bearish": "SELL"}.get(hit.bias, "HOLD")
    pack["signals"]["Pattern"] = [
        Signal(hit.index, side, f"{hit.name} · {hit.bias} · display only", "Pattern")
    ]


def _indicator_notes(pack: dict) -> dict[int, list[str]]:
    notes: dict[int, list[str]] = {}
    for source, rows in pack["signals"].items():
        if source == "Pattern":
            continue
        for signal in rows:
            notes.setdefault(signal.index, []).append(f"{source}: {signal.reason}")
    return notes


def _deploy_pattern(asset: str | None, hit, pack: dict) -> None:
    close = pack.get("close") or []
    if hit.sl is None or hit.bias == "neutral" or hit.index >= len(close):
        return
    side = "BUY" if hit.bias == "bullish" else "SELL"
    _prefill(asset, StopPlan(True, side, float(close[hit.index]), hit.sl, None, hit.name))


def _latest(signals: dict[str, list[Signal]]) -> Signal | None:
    found: list[Signal] = []
    for rows in signals.values():
        if rows:
            found.append(rows[-1])
    if not found:
        return None
    return max(found, key=lambda item: item.index)


def _plan(
    close: list[float],
    atr_series: list[float | None],
    latest: Signal,
    asset: str | None,
) -> StopPlan:
    """ATR distance uses atr_k even when ATR is not drawn. Tick comes from the spec."""
    from config_ext_loader import load_config_ext

    settings = load_config_ext().chart
    entry = close[latest.index]
    value = atr_series[latest.index] if latest.index < len(atr_series) else None
    atr_value = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    if atr_value is None or atr_value != atr_value or atr_value <= 0:
        return StopPlan(False, latest.side, entry, None, None, "ATR is unavailable on the closed bar")
    spec = _spec_for(asset)
    if spec is None or spec.tick_size <= 0 or spec.point <= 0:
        return StopPlan(False, latest.side, entry, None, None, "symbol spec unavailable")
    return plan_stops(
        latest.side,
        entry,
        atr_value,
        atr_k=settings.atr_k,
        rr=settings.rr,
        tick_size=float(spec.tick_size),
        stops_level_points=float(spec.stops_level_points),
        point=float(spec.point),
    )


def _spec_for(asset: str | None):
    if not asset:
        return None
    cache = st.session_state.setdefault("_chart_specs", {})
    if asset in cache:
        return cache[asset]
    from ui.data_access import get_symbol_spec

    spec = get_symbol_spec(asset, st.session_state.get("asset_category") or "forex")
    cache[asset] = spec
    return spec


def _build_figure(
    frame: pd.DataFrame,
    asset: str | None,
    selected: list[str],
    pack: dict,
    patterns: list | None = None,
    *,
    tall: bool = False,
) -> go.Figure:
    lanes = [name for name in ("RSI", "MACD") if name in selected]
    rows = 1 + len(lanes)
    fig = make_subplots(rows=rows, cols=1, shared_xaxes=True, vertical_spacing=0.04, row_heights=[0.62] + [0.38 / max(len(lanes), 1)] * len(lanes) if lanes else [1])
    style = st.session_state.get("chart_style", "Candlestick")
    if not frame.empty and "close" in frame:
        if style == "Candlestick":
            fig.add_trace(go.Candlestick(
                x=frame["time"], open=frame["open"], high=frame["high"],
                low=frame["low"], close=frame["close"], name="Price",
                increasing_line_color=TOKENS["gain"], increasing_fillcolor=hex_rgba("gain", 0.55),
                decreasing_line_color=TOKENS["loss"], decreasing_fillcolor=hex_rgba("loss", 0.45),
            ), row=1, col=1)
        elif style == "Area":
            fig.add_trace(go.Scatter(
                x=frame["time"], y=frame["close"], name="Price", mode="lines",
                line={"color": TOKENS["accent"], "width": 1.5},
                fill="tozeroy", fillcolor=hex_rgba("accent", 0.14),
            ), row=1, col=1)
        else:
            colors = [
                TOKENS["gain"] if close >= open_ else TOKENS["loss"]
                for open_, close in zip(frame["open"], frame["close"])
            ]
            fig.add_trace(go.Bar(x=frame["time"], y=frame["close"], name="Price", marker_color=colors), row=1, col=1)
        _overlay(fig, frame, pack)
        _levels(fig, pack)
        _markers(fig, frame, pack)
        _pattern_markers(fig, frame, patterns or [])
        _lanes(fig, frame, lanes, pack)
    else:
        fig.add_annotation(
            text="Live chart data unavailable · —", x=0.5, y=0.5,
            xref="paper", yref="paper", showarrow=False,
            font={"color": TOKENS["text_muted"], "size": 13},
        )
    theme = plotly_theme()
    theme.update({
        "height": 640 if tall else (420 if not lanes else 560),
        "uirevision": (
            f"{asset}-{st.session_state.get('timeframe', '1H')}-{','.join(selected)}-"
            f"{st.session_state.get('w_pattern_filter', 'All patterns')}"
        ),
        "hoverlabel": {
            "bgcolor": "rgba(20, 24, 31, 0.92)",
            "bordercolor": "rgba(255, 255, 255, 0.18)",
            "font": {"color": "#E6E8EE", "size": 12},
            "align": "left",
        },
        "legend": {"orientation": "h", "x": 0, "y": 1.12},
        "xaxis": {"gridcolor": hex_rgba("border", 0.4), "rangeslider": {"visible": False}},
        "yaxis": {"gridcolor": hex_rgba("border", 0.4), "side": "right"},
    })
    fig.update_layout(**theme)
    return fig


def _levels(fig: go.Figure, pack: dict) -> None:
    plan = pack.get("plan")
    if plan is None or not plan.ok:
        return
    colours = tokens()
    for price, name, colour in (
        (plan.sl, "SL", colours["loss"]),
        (plan.tp, "TP", colours["accent"]),
    ):
        if price is None:
            continue
        fig.add_hline(
            y=price,
            line_dash="dash",
            line_color=colour,
            line_width=1,
            annotation_text=name,
            annotation_position="right",
            row=1,
            col=1,
        )


def _overlay(fig: go.Figure, frame: pd.DataFrame, pack: dict) -> None:
    colours = {
        "EMA20": TOKENS["accent"],
        "EMA50": TOKENS["warn"],
        "BB-mid": TOKENS["text_muted"],
        "BB-upper": TOKENS["accent_violet"],
        "BB-lower": TOKENS["accent_violet"],
    }
    for key, colour in colours.items():
        series = pack["series"].get(key)
        if not isinstance(series, list):
            continue
        fig.add_trace(go.Scatter(
            x=frame["time"], y=series, name=key, mode="lines",
            line={"color": colour, "width": 1.2, "dash": "dot" if key.startswith("BB") else "solid"},
            connectgaps=False,
        ), row=1, col=1)


def _markers(fig: go.Figure, frame: pd.DataFrame, pack: dict) -> None:
    close = pack.get("close") or []
    for source, rows in pack["signals"].items():
        for signal in rows:
            if signal.index >= len(close):
                continue
            price = close[signal.index]
            if signal.side == "SELL":
                glyph, colour, position = "▼", TOKENS["loss"], "top center"
            elif signal.side == "BUY":
                glyph, colour, position = "▲", TOKENS["gain"], "bottom center"
            else:
                glyph, colour, position = "·", TOKENS["warn"], "top center"
            if source == "Pattern":
                continue
            label = glyph
            fig.add_trace(go.Scatter(
                x=[frame["time"].iloc[signal.index]],
                y=[price],
                mode="markers+text",
                text=[label],
                textposition=position,
                name=f"{source} {signal.side}",
                hovertext=signal.reason,
                hoverinfo="text",
                marker={"size": 11, "color": colour},
                showlegend=False,
            ), row=1, col=1)


def _pattern_markers(fig: go.Figure, frame: pd.DataFrame, patterns: list) -> None:
    if not patterns or frame.empty or "open" not in frame:
        return
    opens = [float(value) for value in frame["open"]]
    highs = [float(value) for value in frame["high"]]
    lows = [float(value) for value in frame["low"]]
    closes = [float(value) for value in frame["close"]]
    buckets: dict[str, list] = {"bullish": [], "bearish": [], "neutral": []}
    for hit in patterns[-_MARK_LIMIT:]:
        buckets.get(hit.bias, buckets["neutral"]).append(hit)
    styles = {
        "bullish": (TOKENS["gain"], "triangle-up", "low"),
        "bearish": (TOKENS["loss"], "triangle-down", "high"),
        "neutral": (TOKENS["warn"], "circle", "high"),
    }
    for bias, rows in buckets.items():
        if not rows:
            continue
        colour, symbol, column = styles[bias]
        hovers = []
        for hit in rows:
            hovers.append(
                f"<b>{marker_label(hit.name)} · {hit.name}</b><br>{sentiment_text(hit.name)}<br>"
                + describe_trigger(opens, highs, lows, closes, hit.index, hit.name)
            )
        fig.add_trace(go.Scatter(
            x=[frame["time"].iloc[hit.index] for hit in rows],
            y=[float(frame[column].iloc[hit.index]) for hit in rows],
            mode="markers",
            marker={"size": 9, "color": colour, "symbol": symbol},
            hovertext=hovers,
            hoverinfo="text",
            name=f"Pattern {bias}",
            showlegend=False,
        ), row=1, col=1)


def _lanes(fig: go.Figure, frame: pd.DataFrame, lanes: list[str], pack: dict) -> None:
    for offset, name in enumerate(lanes, start=2):
        if name == "RSI":
            fig.add_trace(go.Scatter(
                x=frame["time"], y=pack["series"].get("RSI"), name="RSI", mode="lines",
                line={"color": TOKENS["accent_amber"], "width": 1.2}, connectgaps=False,
            ), row=offset, col=1)
        else:
            fig.add_trace(go.Scatter(
                x=frame["time"], y=pack["series"].get("MACD"), name="MACD", mode="lines",
                line={"color": TOKENS["accent_cyan"], "width": 1.2}, connectgaps=False,
            ), row=offset, col=1)
            fig.add_trace(go.Scatter(
                x=frame["time"], y=pack["series"].get("MACD-signal"), name="Signal", mode="lines",
                line={"color": TOKENS["warn"], "width": 1}, connectgaps=False,
            ), row=offset, col=1)


def _render_readout(frame: pd.DataFrame, pack: dict) -> None:
    digits = int(pack.get("digits") or 5)
    closed_index = closed_bar_index(len(frame))

    def at(name: str) -> str:
        return indicator_chip_text(pack["series"].get(name), closed_index, digits)

    labels = (
        ("RSI 14", at("RSI")),
        ("ATR", at("ATR")),
        ("EMA 20", at("EMA20")),
        ("EMA 50", at("EMA50")),
        ("MACD", at("MACD")),
    )
    columns = st.columns(len(labels), gap="small")
    for column, (label, value) in zip(columns, labels):
        with column:
            st.metric(label, value)


def _render_stack(frame: pd.DataFrame, pack: dict) -> None:
    signals: dict[str, list[Signal]] = pack["signals"]
    if not signals:
        st.caption("Signal lane · —")
        return
    closed = max(len(frame) - 1, 0)
    rows = []
    for source, items in signals.items():
        latest = items[-1] if items else None
        age = "—" if latest is None else str(closed - 1 - latest.index)
        rows.append({
            "source": source,
            "side": latest.side if latest else "—",
            "age_bars": age,
            "reason": latest.reason if latest else "—",
        })
    st.caption("Signal lane · one row per selected source · closed bars")
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)


def _render_levels(asset: str | None, pack: dict) -> None:
    st.caption(_BREAK_EVEN)
    st.caption(_TP_CAPTION)
    plan: StopPlan | None = pack.get("plan")
    latest: Signal | None = pack.get("latest")
    if latest is None or plan is None:
        st.caption("No closed-bar signal yet · —")
        return
    digits = int(pack.get("digits") or 5)
    if not plan.ok or plan.sl is None:
        st.caption(f"{latest.source} {latest.side} · {plan.reason}")
        return
    st.caption(
        f"{latest.source} {plan.side} · SL {format_fixed(plan.sl, digits)} · "
        f"TP {format_fixed(plan.tp, digits)} · {latest.reason}"
    )
    if st.button("Use in order form", key="btn_use_signal", width="content"):
        _prefill(asset, plan)


def _prefill(asset: str | None, plan: StopPlan) -> None:
    from ui.data_access import get_symbol_spec
    from ui.risk_calc import pip_size

    st.session_state["order_side"] = plan.side
    st.session_state["w_order_side"] = plan.side
    category = st.session_state.get("asset_category", "forex")
    spec = get_symbol_spec(asset, category) if asset else None
    if spec is None or plan.sl is None:
        st.caption("Stop was not copied · symbol spec unavailable")
        return
    distance = abs(plan.entry - plan.sl)
    unit = pip_size(spec)
    if unit <= 0:
        st.caption("Stop was not copied · pip size unavailable")
        return
    pips = distance / unit
    st.session_state["sl_pips"] = pips
    st.session_state["w_sl_pips"] = pips
    st.caption("Side and stop copied. Nothing was submitted.")
