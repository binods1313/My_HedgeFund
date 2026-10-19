from __future__ import annotations

import html
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui.chart_fit import fitted_donut_font_size
from ui.components.data_cache import equity_history
from ui.data_access import AccountSnapshot
from ui.equity_curve import (
    clean_equity_points,
    equity_axis_range,
    equity_caption,
    equity_move_caption,
)
from ui.theme import TOKENS, hex_rgba, plotly_theme

_VOLUME_HOVER = "%{x}<br>Tick volume %{y:.0f}<extra></extra>"
_EQUITY_HOVER = "%{x}<br>Equity %{y:,.2f}<extra></extra>"


def render_analytics_row(frame: pd.DataFrame, account: AccountSnapshot | None) -> None:
    volume_col, margin_col, equity_col = st.columns(3, gap="small")
    with volume_col:
        with st.container(border=True, key="card-cyan-volume"):
            _function_icon(_VOLUME_ICON)
            st.caption("📊 Broker tick volume")
            view = _chart_choice("Volume chart", ["Bar", "Line"], "w_volume_view", "Bar")
            last_volume = _last_tick_volume(frame)
            st.caption(f"Last bar · {last_volume}" if last_volume is not None else "Last bar · —")
            fig = volume_figure(frame, view)
            fig.update_layout(**_small_chart_layout(170))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="volume_history")
            _tone_line(*volume_status(frame))

    with margin_col:
        with st.container(border=True, key="card-amber-margin"):
            _function_icon(_MARGIN_ICON)
            st.caption("⚖️ Margin allocation")
            view = _chart_choice("Margin chart", ["Donut", "Bar"], "w_margin_view", "Donut")
            parts = _margin_parts(account)
            fig = margin_figure(parts, view, account)
            fig.update_layout(**_small_chart_layout(190))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="margin_dial")
            tone, sentence = margin_status(account)
            _tone_line(tone, sentence)

    with equity_col:
        with st.container(border=True, key="card-green-equity"):
            _function_icon(_EQUITY_ICON)
            st.caption("📈 Engine equity curve (broker snapshots)")
            view = _chart_choice("Equity chart", ["Line", "Area"], "w_equity_view", "Line")
            points = _equity_points()
            currency = account.currency if account and account.currency else None
            fig = equity_figure(points, view)
            fig.update_layout(**_small_chart_layout(190))
            if len(points) >= 2:
                low = min(point[1] for point in points)
                high = max(point[1] for point in points)
                y0, y1 = equity_axis_range(low, high)
                fig.update_layout(margin={"l": 52, "r": 8, "t": 4, "b": 4})
                fig.update_yaxes(
                    range=[y0, y1],
                    autorange=False,
                    visible=True,
                    showticklabels=True,
                    tickformat=",.2f",
                    tickfont={"color": TOKENS["text"], "size": 10},
                )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="equity_curve")
            if len(points) < 2:
                st.caption("Not enough snapshots in the current continuous window.")
            else:
                ys = [point[1] for point in points]
                tone = _equity_tone(ys[0], ys[-1])
                _tone_line(tone, f"{tone} · equity")
                st.caption(equity_caption(ys[0], ys[-1]))
                st.caption(equity_move_caption(ys[0], ys[-1], currency))


def volume_figure(frame: pd.DataFrame, view: str) -> go.Figure:
    fig = go.Figure()
    if frame.empty or "tick_volume" not in frame:
        fig.add_annotation(text="—", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
        return fig
    if view == "Line":
        fig.add_trace(go.Scatter(
            x=frame["time"], y=frame["tick_volume"], mode="lines",
            line={"color": TOKENS["accent_cyan"], "width": 2},
            name="Tick volume", hovertemplate=_VOLUME_HOVER,
        ))
    else:
        fig.add_trace(go.Bar(
            x=frame["time"], y=frame["tick_volume"],
            marker_color=TOKENS["accent_cyan"], name="Tick volume",
            hovertemplate=_VOLUME_HOVER,
        ))
    return fig


def volume_status(frame: pd.DataFrame) -> tuple[str, str]:
    if _last_tick_volume(frame) is None:
        return "caution", "caution · no tick volume"
    return "safe", "safe · tick volume"


def margin_figure(parts: dict[str, float], view: str, account: AccountSnapshot | None) -> go.Figure:
    labels = ["Used", "Free", "Buffer"]
    values = [parts["used"], parts["free_rest"], parts["buffer"] or 0.0001]
    colours = [TOKENS["accent_red"], TOKENS["accent_green"], TOKENS["accent_amber"]]
    fig = go.Figure()
    if parts["total"] <= 0:
        fig.add_annotation(text="—", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
        return fig
    if view == "Bar":
        fig.add_trace(go.Bar(
            x=labels, y=[parts["used"], parts["free_rest"], parts["buffer"]],
            marker_color=colours,
            hovertemplate="%{x}<br>%{y:,.2f}<extra></extra>",
        ))
        return fig
    level = (
        f"{account.margin_level:,.1f}%"
        if account and account.margin_level is not None
        else "—"
    )
    fig.add_trace(go.Pie(
        values=values,
        labels=labels,
        hole=0.62,
        sort=False,
        textinfo="none",
        marker={"colors": colours, "line": {"color": TOKENS["panel"], "width": 2}},
        showlegend=False,
        hovertemplate="%{label}<br>%{value:,.2f} · %{percent}<extra></extra>",
    ))
    fig.add_annotation(
        text=f"{level}<br><sup>LEVEL</sup>",
        x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
        font={
            "family": TOKENS["font_mono"],
            "size": fitted_donut_font_size(level),
            "color": TOKENS["text"],
        },
    )
    return fig


def margin_status(account: AccountSnapshot | None) -> tuple[str, str]:
    level = account.margin_level if account else None
    tone = _margin_tone(level)
    parts = _margin_parts(account)
    if parts["total"] <= 0:
        return tone, f"{tone} · margin data · —"
    allocation = parts["used"] / (parts["used"] + parts["free"]) * 100 if (parts["used"] + parts["free"]) > 0 else 0.0
    return tone, f"{tone} · {allocation:.1f}% used · {100 - allocation:.1f}% free"


def equity_figure(points: list[tuple[float, float]], view: str) -> go.Figure:
    fig = go.Figure()
    if len(points) < 2:
        fig.add_annotation(
            text="—", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
            font={"color": TOKENS["text"], "size": 22},
        )
        return fig
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    low = min(ys)
    colour = TOKENS["gain"] if ys[-1] > ys[0] else TOKENS["loss"] if ys[-1] < ys[0] else TOKENS["warn"]
    fig.add_trace(go.Scatter(
        x=xs, y=[low] * len(ys), mode="lines",
        line={"width": 0, "color": hex_rgba("panel", 0)},
        showlegend=False, hoverinfo="skip",
    ))
    fill = "tonexty" if view == "Area" else None
    fig.add_trace(go.Scatter(
        x=xs, y=ys, mode="lines",
        line={"color": colour, "width": 2},
        fill=fill,
        fillcolor=hex_rgba("accent", 0.16) if fill else None,
        name="Equity",
        hovertemplate=_EQUITY_HOVER,
    ))
    return fig


def _chart_choice(label: str, options: list[str], key: str, default: str) -> str:
    picked = st.segmented_control(
        label,
        options,
        default=default,
        key=key,
        width="stretch",
        wrap=True,
    )
    return picked if picked in options else default


def _tone_line(tone: str, sentence: str) -> None:
    safe_tone = tone if tone in {"safe", "caution", "risk", "unknown"} else "unknown"
    st.html(
        f'<p class="hf-tone hf-tone-{safe_tone}">{html.escape(sentence)}</p>'
    )


def _function_icon(svg: str) -> None:
    st.html(svg)


def _equity_points() -> list[tuple[float, float]]:
    history = equity_history(int(time.time()))
    columns = getattr(history, "columns", [])
    stamps = list(history["ts"]) if history is not None and "ts" in columns else []
    amounts = list(history["equity"]) if history is not None and "equity" in columns else []
    return clean_equity_points(stamps, amounts)


def _margin_parts(account: AccountSnapshot | None) -> dict[str, float]:
    used = account.margin_used if account and account.margin_used is not None else 0.0
    free = account.free_margin if account and account.free_margin is not None else 0.0
    equity = account.equity if account and account.equity else 0.0
    buffer = min(free, equity * 0.05) if equity else 0.0
    free_rest = max(free - buffer, 0.0)
    return {
        "used": used,
        "free": free,
        "free_rest": free_rest,
        "buffer": buffer,
        "total": used + free_rest + buffer,
    }


def _last_tick_volume(frame: pd.DataFrame) -> str | None:
    if frame.empty or "tick_volume" not in frame:
        return None
    value = frame["tick_volume"].iloc[-1]
    if value is None or value != value:
        return None
    return f"{float(value):.0f}"


def _margin_tone(level: float | None) -> str:
    if level is None or level != level:
        return "unknown"
    if level < 100.0:
        return "risk"
    if level < 200.0:
        return "caution"
    return "safe"


def _equity_tone(first: float, last: float) -> str:
    if last > first:
        return "safe"
    if last < first:
        return "risk"
    return "caution"


def _small_chart_layout(height: int) -> dict:
    return {
        **plotly_theme(),
        "height": height,
        "margin": {"l": 4, "r": 4, "t": 4, "b": 4},
        "showlegend": False,
        "xaxis": {"visible": False, "gridcolor": "rgba(38,42,54,0.4)"},
        "yaxis": {"visible": False, "gridcolor": "rgba(38,42,54,0.4)"},
    }


_VOLUME_ICON = (
    '<svg class="hf-fn-icon" width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">'
    '<rect x="1" y="9" width="3" height="7" fill="currentColor"/>'
    '<rect x="7" y="5" width="3" height="11" fill="currentColor"/>'
    '<rect x="13" y="2" width="3" height="14" fill="currentColor"/></svg>'
)
_MARGIN_ICON = (
    '<svg class="hf-fn-icon" width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">'
    '<circle cx="9" cy="9" r="7" fill="none" stroke="currentColor" stroke-width="2"/>'
    '<path d="M9 9 L9 2 A7 7 0 0 1 15 11 Z" fill="currentColor"/></svg>'
)
_EQUITY_ICON = (
    '<svg class="hf-fn-icon" width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">'
    '<polyline points="1,14 5,9 8,11 12,4 17,6" fill="none" stroke="currentColor" stroke-width="2"/></svg>'
)
