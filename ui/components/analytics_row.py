from __future__ import annotations

import html
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ui.chart_fit import fitted_donut_font_size
from ui.components.data_cache import equity_history
from ui.data_access import AccountSnapshot
from ui.equity_curve import clean_equity_points, equity_caption
from ui.theme import TOKENS, hex_rgba, plotly_theme


def render_analytics_row(frame: pd.DataFrame, account: AccountSnapshot | None) -> None:
    volume_col, margin_col, equity_col = st.columns(3, gap="small")
    with volume_col:
        with st.container(border=True, key="card-cyan-volume"):
            st.html('<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><rect x="1" y="9" width="3" height="7" fill="currentColor"/><rect x="7" y="5" width="3" height="11" fill="currentColor"/><rect x="13" y="2" width="3" height="14" fill="currentColor"/></svg>')
            st.caption("📊 Broker tick volume")
            last_volume = _last_tick_volume(frame)
            st.caption(f"Last bar · {last_volume}" if last_volume is not None else "Last bar · —")
            fig = go.Figure()
            if not frame.empty and "tick_volume" in frame:
                fig.add_trace(go.Bar(
                    x=frame["time"], y=frame["tick_volume"],
                    marker_color=TOKENS["accent_cyan"], name="Tick volume",
                ))
            else:
                fig.add_annotation(text="—", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
            fig.update_layout(**_small_chart_layout(170))
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="volume_history")

    with margin_col:
        with st.container(border=True, key="card-amber-margin"):
            st.html('<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="7" fill="none" stroke="currentColor" stroke-width="2"/><path d="M9 9 L9 2 A7 7 0 0 1 15 11 Z" fill="currentColor"/></svg>')
            st.caption("⚖️ Margin allocation")
            used = account.margin_used if account and account.margin_used is not None else 0.0
            free = account.free_margin if account and account.free_margin is not None else 0.0
            equity = account.equity if account and account.equity else 0.0
            buffer = min(free, equity * 0.05) if equity else 0.0
            free_rest = max(free - buffer, 0.0)
            total = used + free_rest + buffer
            allocation = used / (used + free) * 100 if (used + free) > 0 else 0.0
            margin_level = (
                f"{account.margin_level:,.1f}%"
                if account and account.margin_level is not None
                else "—"
            )
            if total <= 0:
                fig = go.Figure()
                fig.add_annotation(text="—", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False)
            else:
                fig = go.Figure(go.Pie(
                    values=[used, free_rest, buffer or 0.0001],
                    labels=["Used", "Free", "Buffer"],
                    hole=0.62,
                    sort=False,
                    textinfo="none",
                    marker={"colors": [TOKENS["accent_red"], TOKENS["accent_green"], TOKENS["accent_amber"]],
                            "line": {"color": TOKENS["panel"], "width": 2}},
                    showlegend=False,
                    hoverinfo="label+value",
                ))
            fig.update_layout(**_small_chart_layout(190))
            fig.add_annotation(
                text=f"{margin_level}<br><sup>LEVEL</sup>",
                x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
                font={
                    "family": TOKENS["font_mono"],
                    "size": fitted_donut_font_size(margin_level),
                    "color": TOKENS["text"],
                },
            )
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="margin_dial")
            tone = _margin_tone(account.margin_level if account else None)
            used_text = (
                f"{tone} · {allocation:.1f}% used · {100 - allocation:.1f}% free"
                if total else f"{tone} · margin data · —"
            )
            st.html(f'<p class="hf-tone hf-tone-{tone}">{used_text}</p>')

    with equity_col:
        with st.container(border=True, key="card-green-equity"):
            st.html('<svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><polyline points="1,14 5,9 8,11 12,4 17,6" fill="none" stroke="currentColor" stroke-width="2"/></svg>')
            st.caption("📈 Engine equity curve")
            history = equity_history(int(time.time()))
            points = clean_equity_points(
                list(history["ts"]) if history is not None and "ts" in getattr(history, "columns", []) else [],
                list(history["equity"]) if history is not None and "equity" in getattr(history, "columns", []) else [],
            )
            fig = go.Figure()
            if len(points) >= 2:
                xs = [point[0] for point in points]
                ys = [point[1] for point in points]
                low, high = min(ys), max(ys)
                pad = (high - low) * 0.08 or 1.0
                fig.add_trace(go.Scatter(
                    x=xs, y=[low] * len(ys), mode="lines",
                    line={"width": 0, "color": hex_rgba("panel", 0)},
                    showlegend=False, hoverinfo="skip",
                ))
                if ys[-1] > ys[0]:
                    equity_tone, equity_colour = "safe", TOKENS["gain"]
                elif ys[-1] < ys[0]:
                    equity_tone, equity_colour = "risk", TOKENS["loss"]
                else:
                    equity_tone, equity_colour = "warning", TOKENS["warn"]
                fig.add_trace(go.Scatter(
                    x=xs, y=ys, mode="lines",
                    line={"color": equity_colour, "width": 2},
                    fill="tonexty",
                    fillcolor=hex_rgba("accent", 0.16),
                    name="Equity",
                ))
                y_range = [low - pad, high + pad]
            else:
                y_range = None
                fig.add_annotation(text="—", x=0.5, y=0.5, xref="paper", yref="paper", showarrow=False,
                                   font={"color": TOKENS["text"], "size": 22})
            fig.update_layout(**_small_chart_layout(190))
            if y_range:
                fig.update_yaxes(range=y_range, autorange=False, visible=False, rangemode="normal")
            st.plotly_chart(fig, width="stretch", config={"displayModeBar": False}, key="equity_curve")
            if len(points) < 2:
                st.caption("Not enough snapshots in the current continuous window.")
            else:
                st.html(
                    f'<p class="hf-tone hf-tone-{equity_tone}">'
                    f"{html.escape(equity_caption(ys[0], ys[-1]))}</p>"
                )


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
        return "warning"
    return "safe"


def _small_chart_layout(height: int) -> dict:
    return {
        **plotly_theme(),
        "height": height,
        "margin": {"l": 4, "r": 4, "t": 4, "b": 4},
        "showlegend": False,
        "xaxis": {"visible": False, "gridcolor": "rgba(38,42,54,0.4)"},
        "yaxis": {"visible": False, "gridcolor": "rgba(38,42,54,0.4)"},
    }