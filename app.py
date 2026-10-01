import streamlit as st
import plotly.graph_objects as go
from datetime import datetime

import MetaTrader5 as mt5

from data_engine import (
    initialize_mt5,
    get_available_symbols,
    get_market_data,
    get_technical_dataframe,
    get_symbol_live_quote,
    get_account_telemetry,
)
from ai_brain import get_ai_decision
from execution import execute_trade, get_open_positions, close_position, close_all_positions

st.set_page_config(
    page_title="AI Hedge Fund Command Center",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap');

/* ── RESET & BASE ── */
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body, [class*="css"] {
    font-family: 'Inter', system-ui, sans-serif !important;
    background-color: #0B1220 !important;
    color: #F8FAFC !important;
    font-size: 14px !important;
    /* Force dark on all Streamlit widgets */
    [data-testid^="st"] { background: #0B1220 !important; color: #F8FAFC !important; }
    [data-testid^="st"] * { background: transparent !important; color: inherit !important; }
}
    font-family: 'Inter', system-ui, sans-serif !important;
    background-color: #0B1220 !important;
    color: #F8FAFC !important;
    font-size: 14px !important;
}
/* Force dark on ALL Streamlit-generated elements */
.stApp, .stApp > * { background-color: #0B1220 !important; }
p, span, div, label, h1, h2, h3, h4, h5, h6 { color: inherit !important; }
.main .block-container {
    padding: 0.75rem 1.25rem 1.5rem !important;
    max-width: 100% !important;
}
section[data-testid="stSidebar"] { display: none; }

/* ── SCROLLBAR ── */
::-webkit-scrollbar { width: 6px; height: 6px; }
::-webkit-scrollbar-track { background: #0B1220; }
::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }

/* ── HUD BAR ── */
.hud-bar {
    background: linear-gradient(135deg, #111827 0%, #1a2540 100%);
    border: 1px solid #1E3A5F;
    border-radius: 12px;
    padding: 0.65rem 1.25rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.75rem;
    box-shadow: 0 4px 24px rgba(0,0,0,0.4), inset 0 1px 0 rgba(255,255,255,0.04);
}
.hud-item { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.hud-label {
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: #64748B;
    white-space: nowrap;
}
.hud-value {
    font-family: 'JetBrains Mono', monospace;
    font-size: 14px;
    font-weight: 700;
    color: #F8FAFC;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
}
.hud-value.cyan { color: #22D3EE; }
.hud-value.green { color: #34D399; }
.hud-value.red { color: #F43F5E; }
.hud-value.amber { color: #F59E0B; }
.status-pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    background: rgba(34, 211, 238, 0.1);
    border: 1px solid rgba(34, 211, 238, 0.3);
    border-radius: 20px;
    padding: 3px 10px 3px 8px;
    font-size: 12px;
    font-weight: 700;
    color: #22D3EE;
    letter-spacing: 0.05em;
}
.status-dot {
    width: 7px; height: 7px;
    border-radius: 50%;
    background: #22D3EE;
    box-shadow: 0 0 6px #22D3EE;
    animation: pulse 2s ease-in-out infinite;
}
.status-dot.offline { background: #F43F5E; box-shadow: 0 0 6px #F43F5E; }
@keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.6; transform: scale(0.85); }
}

/* ── LIVE INFO STRIP ── */
.info-strip {
    background: #111827;
    border: 1px solid #1E293B;
    border-radius: 10px;
    padding: 0.5rem 1rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    margin-bottom: 0.75rem;
}
.strip-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 12px;
}
.strip-label { color: #64748B; font-weight: 600; letter-spacing: 0.05em; text-transform: uppercase; font-size: 10px; }
.strip-value { font-family: 'JetBrains Mono', monospace; color: #F8FAFC; font-weight: 600; font-size: 12px; }
.strip-divider { width: 1px; height: 20px; background: #1E293B; flex-shrink: 0; }

/* ── CARDS ── */
.card {
    background: #111827;
    border: 1px solid #1E293B;
    border-radius: 14px;
    padding: 1rem;
    height: 100%;
}
.card-header {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 0.85rem;
    padding-bottom: 0.65rem;
    border-bottom: 1px solid #1E293B;
}
.card-title {
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: #94A3B8;
}
.card-accent-dot {
    width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0;
}
.dot-cyan { background: #22D3EE; box-shadow: 0 0 6px rgba(34,211,238,0.6); }
.dot-amber { background: #F59E0B; box-shadow: 0 0 6px rgba(245,158,11,0.6); }
.dot-green { background: #34D399; box-shadow: 0 0 6px rgba(52,211,153,0.6); }

/* ── FORM CONTROLS — SELECTBOX ── */
/* Container */
.stSelectbox > div > div,
[data-baseweb="select"] > div {
    background: #1E293B !important;
    border: 1px solid #475569 !important;
    border-radius: 8px !important;
    color: #F8FAFC !important;
    font-size: 14px !important;
    font-weight: 600 !important;
    min-height: 42px !important;
}
/* Selected value text */
[data-baseweb="select"] [data-testid="stSelectboxSelectedValue"],
[data-baseweb="select"] div[class*="singleValue"],
[data-baseweb="select"] div[class*="placeholder"],
[data-baseweb="select"] span,
[data-baseweb="select"] div {
    color: #F8FAFC !important;
    background: transparent !important;
    font-size: 14px !important;
    font-weight: 600 !important;
}
/* The inner value container */
.stSelectbox > div > div > div { color: #F8FAFC !important; background: transparent !important; }
/* Dropdown popover */
[data-baseweb="popover"],
[data-baseweb="popover"] > div,
[data-baseweb="menu"],
[data-baseweb="menu"] > ul {
    background: #1E293B !important;
    border: 1px solid #475569 !important;
    border-radius: 8px !important;
}
[data-baseweb="popover"] [role="option"],
[data-baseweb="menu"] li {
    background: #1E293B !important;
    color: #F8FAFC !important;
    font-size: 13px !important;
    padding: 8px 12px !important;
}
[data-baseweb="popover"] [role="option"]:hover,
[data-baseweb="menu"] li:hover {
    background: #334155 !important;
    color: #22D3EE !important;
}
/* All widget labels */
label[data-testid="stWidgetLabel"],
label[data-testid="stWidgetLabel"] p {
    color: #94A3B8 !important;
    font-size: 11px !important;
    font-weight: 700 !important;
    letter-spacing: 0.08em !important;
    text-transform: uppercase !important;
    background: transparent !important;
}

/* ── TICKER ROW ── */
.ticker-row {
    background: #1E293B;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 0.6rem 0.85rem;
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 0.75rem;
    gap: 0.5rem;
}
.ticker-pair { font-weight: 700; font-size: 14px; color: #F8FAFC; }
.ticker-desc { font-size: 11px; color: #64748B; }
.ticker-bid { font-family: 'JetBrains Mono', monospace; font-size: 14px; font-weight: 700; color: #F43F5E; }
.ticker-ask { font-family: 'JetBrains Mono', monospace; font-size: 14px; font-weight: 700; color: #34D399; }
.ticker-spread {
    font-family: 'JetBrains Mono', monospace; font-size: 12px;
    background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.3);
    color: #F59E0B; border-radius: 5px; padding: 2px 7px;
}

/* ── TIMEFRAME RADIOS ── */
.stRadio > div { flex-direction: row !important; gap: 6px !important; }
.stRadio > div > label {
    background: #1E293B !important;
    border: 1px solid #334155 !important;
    border-radius: 6px !important;
    padding: 5px 12px !important;
    font-size: 12px !important;
    color: #94A3B8 !important;
    cursor: pointer !important;
    transition: all 0.15s !important;
}
.stRadio > div > label:has(input:checked) {
    background: #22D3EE !important;
    border-color: #22D3EE !important;
    color: #0B1220 !important;
    font-weight: 700 !important;
}
.stRadio > div > label > div { display: none !important; }
.stRadio > div > label > div + div { display: block !important; }

/* ── INDICATOR CHIPS ── */
.chip-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 0.5rem; }
.chip {
    display: inline-flex; align-items: center; gap: 5px;
    background: #1E293B; border: 1px solid #334155; border-radius: 6px;
    padding: 4px 10px; font-size: 11px;
}
.chip-label { color: #64748B; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
.chip-value { font-family: 'JetBrains Mono', monospace; color: #F8FAFC; font-weight: 600; }
.chip-value.cyan { color: #22D3EE; }
.chip-value.amber { color: #F59E0B; }

/* ── REACTOR DIAL ── */
.reactor-wrap { display: flex; flex-direction: column; align-items: center; gap: 1rem; padding: 0.5rem 0; }
.reactor-outer {
    width: 200px; height: 200px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    position: relative;
    transition: box-shadow 0.3s ease;
}
.reactor-outer.buy {
    border: 3px solid #34D399;
    box-shadow: 0 0 0 6px rgba(52,211,153,0.1), 0 0 40px rgba(52,211,153,0.3), 0 0 80px rgba(52,211,153,0.1);
    background: radial-gradient(circle at center, rgba(52,211,153,0.05) 0%, transparent 70%);
}
.reactor-outer.sell {
    border: 3px solid #F43F5E;
    box-shadow: 0 0 0 6px rgba(244,63,94,0.1), 0 0 40px rgba(244,63,94,0.3), 0 0 80px rgba(244,63,94,0.1);
    background: radial-gradient(circle at center, rgba(244,63,94,0.05) 0%, transparent 70%);
}
.reactor-outer.hold {
    border: 3px solid #F59E0B;
    box-shadow: 0 0 0 6px rgba(245,158,11,0.1), 0 0 40px rgba(245,158,11,0.3), 0 0 80px rgba(245,158,11,0.1);
    background: radial-gradient(circle at center, rgba(245,158,11,0.05) 0%, transparent 70%);
}
.reactor-inner {
    width: 155px; height: 155px; border-radius: 50%;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    font-family: 'Inter', sans-serif; font-weight: 800; font-size: 2rem; letter-spacing: 0.08em;
}
.reactor-inner.buy { background: radial-gradient(135deg, #34D399 0%, #059669 60%, #064e3b 100%); color: #022c22; text-shadow: 0 1px 3px rgba(0,0,0,0.4); }
.reactor-inner.sell { background: radial-gradient(135deg, #F43F5E 0%, #be123c 60%, #4c0519 100%); color: #fff; text-shadow: 0 1px 3px rgba(0,0,0,0.5); }
.reactor-inner.hold { background: radial-gradient(135deg, #F59E0B 0%, #b45309 60%, #451a03 100%); color: #1a0a00; text-shadow: 0 1px 3px rgba(0,0,0,0.3); }

/* ── BADGE PILLS ── */
.badge-row { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.badge {
    font-size: 11px; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
    padding: 4px 12px; border-radius: 20px;
}
.badge.cyan { background: rgba(34,211,238,0.12); border: 1px solid rgba(34,211,238,0.35); color: #22D3EE; }
.badge.amber { background: rgba(245,158,11,0.12); border: 1px solid rgba(245,158,11,0.35); color: #F59E0B; }
.badge.green { background: rgba(52,211,153,0.12); border: 1px solid rgba(52,211,153,0.35); color: #34D399; }
.badge.red { background: rgba(244,63,94,0.12); border: 1px solid rgba(244,63,94,0.35); color: #F43F5E; }
.badge.neutral { background: rgba(148,163,184,0.12); border: 1px solid rgba(148,163,184,0.35); color: #94A3B8; }

/* ── STAT PILLS ROW ── */
.stat-pills { display: flex; gap: 8px; justify-content: center; flex-wrap: wrap; }
.stat-pill {
    background: #1E293B; border: 1px solid #334155; border-radius: 8px;
    padding: 6px 12px; text-align: center; min-width: 80px;
}
.stat-pill-label { font-size: 10px; color: #64748B; font-weight: 600; text-transform: uppercase; letter-spacing: 0.06em; }
.stat-pill-value { font-family: 'JetBrains Mono', monospace; font-size: 13px; color: #F8FAFC; font-weight: 700; margin-top: 2px; }

/* ── RATIONALE BOX ── */
.rationale-box {
    background: #0F172A; border: 1px solid #1E293B; border-radius: 8px;
    padding: 0.75rem; margin: 0.75rem 0;
}
.rationale-heading { font-size: 10px; font-weight: 700; letter-spacing: 0.1em; text-transform: uppercase; color: #475569; margin-bottom: 6px; }
.rationale-body { font-size: 13px; color: #CBD5E1; line-height: 1.5; }

/* ── BUTTONS ── */
/* Primary CTA */
div[data-testid="stButton"] > button {
    border-radius: 8px !important;
    font-family: 'Inter', sans-serif !important;
    font-weight: 700 !important;
    font-size: 13px !important;
    letter-spacing: 0.04em !important;
    transition: all 0.15s ease !important;
    cursor: pointer !important;
}
.btn-primary div[data-testid="stButton"] > button {
    background: #22D3EE !important;
    color: #0B1220 !important;
    border: 1px solid #22D3EE !important;
    padding: 0.6rem 1rem !important;
    width: 100% !important;
    font-size: 14px !important;
    letter-spacing: 0.06em !important;
    text-transform: uppercase !important;
    height: 48px !important;
}
.btn-primary div[data-testid="stButton"] > button:hover {
    background: #06B6D4 !important;
    border-color: #06B6D4 !important;
    box-shadow: 0 0 16px rgba(34,211,238,0.4) !important;
    transform: translateY(-1px) !important;
}
.btn-primary div[data-testid="stButton"] > button:active {
    transform: translateY(0) !important;
    background: #0891B2 !important;
}
/* Secondary */
.btn-secondary div[data-testid="stButton"] > button {
    background: #1E293B !important;
    color: #94A3B8 !important;
    border: 1px solid #334155 !important;
    height: 36px !important;
}
.btn-secondary div[data-testid="stButton"] > button:hover {
    background: #263548 !important;
    color: #F8FAFC !important;
    border-color: #475569 !important;
}
/* Danger */
.btn-danger div[data-testid="stButton"] > button {
    background: rgba(244,63,94,0.1) !important;
    color: #F43F5E !important;
    border: 1px solid #F43F5E !important;
    height: 44px !important;
    width: 100% !important;
    font-size: 13px !important;
    font-weight: 700 !important;
    letter-spacing: 0.04em !important;
}
.btn-danger div[data-testid="stButton"] > button:hover {
    background: rgba(244,63,94,0.2) !important;
    box-shadow: 0 0 12px rgba(244,63,94,0.3) !important;
}
/* Close (per order) */
.btn-close div[data-testid="stButton"] > button {
    background: #1E293B !important;
    color: #F43F5E !important;
    border: 1px solid #334155 !important;
    height: 36px !important;
    min-width: 64px !important;
    font-size: 12px !important;
    font-weight: 600 !important;
}
.btn-close div[data-testid="stButton"] > button:hover {
    background: rgba(244,63,94,0.15) !important;
    border-color: #F43F5E !important;
}
/* Sync */
.btn-sync div[data-testid="stButton"] > button {
    background: #1E293B !important;
    color: #22D3EE !important;
    border: 1px solid #334155 !important;
    height: 36px !important;
    width: 100% !important;
    font-size: 12px !important;
    font-weight: 600 !important;
}
.btn-sync div[data-testid="stButton"] > button:hover {
    background: rgba(34,211,238,0.1) !important;
    border-color: #22D3EE !important;
}

/* ── NUMBER INPUTS — HIGH CONTRAST ── */
div[data-testid="stNumberInput"],
div[data-testid="stNumberInput"] > div,
div[data-testid="stNumberInput"] > div > div {
    background: #1E293B !important;
    border: 1px solid #475569 !important;
    border-radius: 8px !important;
}
div[data-testid="stNumberInput"] input,
div[data-testid="stNumberInput"] input:focus,
div[data-testid="stNumberInput"] input:active,
div[data-testid="stNumberInput"] input:hover {
    background: #1E293B !important;
    color: #FFFFFF !important;
    font-family: 'JetBrains Mono', monospace !important;
    font-size: 18px !important;
    font-weight: 800 !important;
    text-align: center !important;
    border: none !important;
    outline: none !important;
    box-shadow: none !important;
    -webkit-text-fill-color: #FFFFFF !important;
    caret-color: #22D3EE !important;
}
/* Step buttons (the − and + arrows) */
div[data-testid="stNumberInput"] button,
div[data-testid="stNumberInput"] button:focus,
div[data-testid="stNumberInput"] > div button {
    background: #334155 !important;
    color: #F8FAFC !important;
    border: 1px solid #475569 !important;
    border-radius: 6px !important;
    font-size: 18px !important;
    font-weight: 800 !important;
    min-width: 36px !important;
    min-height: 36px !important;
    line-height: 1 !important;
}
div[data-testid="stNumberInput"] button:hover {
    background: #22D3EE !important;
    color: #0B1220 !important;
    border-color: #22D3EE !important;
}

/* ── ORDER CARDS ── */
.order-card {
    background: #0F172A; border: 1px solid #1E293B; border-radius: 10px;
    padding: 0.65rem 0.75rem; margin-bottom: 0.5rem;
}
.order-top { display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }
.order-ticket { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: #475569; }
.order-symbol { font-weight: 700; font-size: 14px; color: #F8FAFC; }
.order-side-buy { background: rgba(52,211,153,0.12); border: 1px solid rgba(52,211,153,0.3); color: #34D399; font-size: 10px; font-weight: 700; border-radius: 4px; padding: 1px 7px; letter-spacing: 0.06em; }
.order-side-sell { background: rgba(244,63,94,0.12); border: 1px solid rgba(244,63,94,0.3); color: #F43F5E; font-size: 10px; font-weight: 700; border-radius: 4px; padding: 1px 7px; letter-spacing: 0.06em; }
.order-pnl-pos { font-family: 'JetBrains Mono', monospace; font-size: 16px; font-weight: 700; color: #34D399; }
.order-pnl-neg { font-family: 'JetBrains Mono', monospace; font-size: 16px; font-weight: 700; color: #F43F5E; }
.order-meta { font-size: 11px; color: #64748B; font-family: 'JetBrains Mono', monospace; }

/* ── STATUS / ALERTS ── */
div[data-testid="stAlert"] { border-radius: 8px !important; }
div[data-testid="stSuccess"] { background: rgba(52,211,153,0.08) !important; border: 1px solid rgba(52,211,153,0.25) !important; color: #34D399 !important; }
div[data-testid="stWarning"] { background: rgba(245,158,11,0.08) !important; border: 1px solid rgba(245,158,11,0.25) !important; color: #F59E0B !important; }
div[data-testid="stError"] { background: rgba(244,63,94,0.08) !important; border: 1px solid rgba(244,63,94,0.25) !important; color: #F43F5E !important; }
div[data-testid="stInfo"] { background: rgba(34,211,238,0.06) !important; border: 1px solid rgba(34,211,238,0.2) !important; color: #22D3EE !important; }

/* ── SPINNER ── */
.stSpinner > div { border-top-color: #22D3EE !important; }

/* ── PLOTLY CHART ── */
.js-plotly-plot { border-radius: 8px !important; }

/* ── STATUS STEP ── */
div[data-testid="stStatusWidget"] { background: #111827 !important; border: 1px solid #1E293B !important; border-radius: 10px !important; }

/* ── DIVIDER ── */
hr { border-color: #1E293B !important; margin: 0.5rem 0 !important; }

/* ── CAPTION ── */
.stCaption { color: #475569 !important; font-size: 11px !important; }

/* ── TOAST ── */
div[data-testid="stToast"] { background: #1E293B !important; border: 1px solid #334155 !important; color: #F8FAFC !important; border-radius: 10px !important; }

/* ── RESPONSIVE ── */
@media (max-width: 1280px) {
    .hud-bar { flex-wrap: wrap; gap: 0.5rem; }
}
</style>
""", unsafe_allow_html=True)

# ── CONNECT ──────────────────────────────────────────────────────────────────
mt5_connected = initialize_mt5()
telemetry = get_account_telemetry() if mt5_connected else {
    "connected": False, "login": "OFFLINE", "server": "DISCONNECTED",
    "balance": 0.0, "equity": 0.0, "profit": 0.0, "margin_level": 0.0,
    "currency": "USD", "company": "N/A",
}

def fmt_price(v, digits=5): return f"{v:.{min(digits,5)}f}"
def fmt_pnl(v): return f"+{v:.2f}" if v >= 0 else f"{v:.2f}"
def pnl_class(v): return "green" if v >= 0 else "red"

# ── SESSION STATE ─────────────────────────────────────────────────────────────
if "ai_state" not in st.session_state:
    st.session_state.ai_state = {
        "symbol": "USDCAD", "decision": "HOLD",
        "confidence": 85, "strength": "NEUTRAL",
        "reasoning": "Standby. Click SCAN & EXECUTE to begin autonomous analysis.",
        "risk_reward": "1:2", "timestamp": "--:--:--", "last_exec": None,
    }

# ── HUD BAR ───────────────────────────────────────────────────────────────────
bal = telemetry["balance"]
eq  = telemetry["equity"]
pnl = telemetry["profit"]
ml  = telemetry["margin_level"]
srv   = telemetry.get("server", "N/A")
login = telemetry.get("login", "N/A")

# Bug fix: use account currency symbol instead of hardcoded "$"
_CURRENCY_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥",
                     "CHF": "₣", "AUD": "A$", "CAD": "C$"}
_cur_sym = _CURRENCY_SYMBOLS.get(telemetry.get("currency", "USD"), "")
_currency = telemetry.get("currency", "USD")

# Bug fix: show margin level as N/A when margin used is negligible (avoids 487,572%)
_margin_used = telemetry.get("margin", 0.0)
_margin_level_str = f"{ml:,.1f}%" if (ml > 0 and _margin_used > 1.0) else "N/A"

conn_dot = '<span class="status-dot"></span>' if mt5_connected else '<span class="status-dot offline"></span>'
conn_text = "CONNECTED" if mt5_connected else "OFFLINE"
conn_pill_color = "" if mt5_connected else "style='background:rgba(244,63,94,0.1);border-color:rgba(244,63,94,0.3);color:#F43F5E;'"

st.markdown(f"""
<div class="hud-bar">
  <div class="hud-item">
    <span class="hud-label">AI Command Center</span>
    <span class="status-pill" {conn_pill_color}>{conn_dot}{conn_text}</span>
  </div>
  <div class="hud-item">
    <span class="hud-label">MT5 Server / Account</span>
    <span class="hud-value">{srv} ({login})</span>
  </div>
  <div class="hud-item">
    <span class="hud-label">Balance</span>
    <span class="hud-value">{_cur_sym}{bal:,.2f} {_currency}</span>
  </div>
  <div class="hud-item">
    <span class="hud-label">Floating Equity</span>
    <span class="hud-value">{_cur_sym}{eq:,.2f}</span>
  </div>
  <div class="hud-item">
    <span class="hud-label">Floating P&L</span>
    <span class="hud-value {pnl_class(pnl)}">{fmt_pnl(pnl)}</span>
  </div>
  <div class="hud-item">
    <span class="hud-label">Margin Level</span>
    <span class="hud-value cyan">{_margin_level_str}</span>
  </div>
</div>
""", unsafe_allow_html=True)

# ── LIVE INFO STRIP ───────────────────────────────────────────────────────────
now_str = datetime.now().strftime("%H:%M:%S")
open_pos = get_open_positions() if mt5_connected else []
open_risk = sum(abs(p["profit"]) for p in open_pos) if open_pos else 0.0
margin_used = telemetry.get("margin", 0.0)
margin_free = telemetry.get("margin_free", 0.0)
sym_for_strip = st.session_state.ai_state["symbol"]
strip_quote = get_symbol_live_quote(sym_for_strip) if mt5_connected else {}
spread_str = f"{strip_quote.get('spread', '--')}" if strip_quote else "--"
tick_time = strip_quote.get("time", "--") if strip_quote else "--"

st.markdown(f"""
<div class="info-strip">
  <div class="strip-item">
    <div><div class="strip-label">Session Clock</div><div class="strip-value">{now_str} IST</div></div>
  </div>
  <div class="strip-divider"></div>
  <div class="strip-item">
    <div><div class="strip-label">Active Symbol Spread</div><div class="strip-value">{spread_str} pips</div></div>
  </div>
  <div class="strip-divider"></div>
  <div class="strip-item">
    <div><div class="strip-label">Last Tick</div><div class="strip-value">{tick_time}</div></div>
  </div>
  <div class="strip-divider"></div>
  <div class="strip-item">
    <div><div class="strip-label">Margin Used</div><div class="strip-value">${margin_used:,.2f}</div></div>
  </div>
  <div class="strip-divider"></div>
  <div class="strip-item">
    <div><div class="strip-label">Free Margin</div><div class="strip-value">${margin_free:,.2f}</div></div>
  </div>
  <div class="strip-divider"></div>
  <div class="strip-item">
    <div><div class="strip-label">Open Risk $</div>
    <div class="strip-value" style="color:{'#F43F5E' if open_risk>0 else '#34D399'};">${open_risk:,.2f}</div></div>
  </div>
  <div class="strip-divider"></div>
  <div class="strip-item">
    <div><div class="strip-label">Open Positions</div><div class="strip-value">{len(open_pos)}</div></div>
  </div>
</div>
""", unsafe_allow_html=True)

# ── DYNAMIC SYMBOLS ───────────────────────────────────────────────────────────
available_categories = get_available_symbols()

# ── THREE COLUMNS ─────────────────────────────────────────────────────────────
col_l, col_c, col_r = st.columns([1.4, 1.2, 1.4], gap="medium")

# ╔══════════════════════════════════════╗
# ║   LEFT — DYNAMIC INSTRUMENT RADAR   ║
# ╚══════════════════════════════════════╝
with col_l:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="card-header"><div class="card-accent-dot dot-cyan"></div><span class="card-title">Dynamic Instrument Radar</span></div>', unsafe_allow_html=True)

    # Dropdowns
    dd1, dd2 = st.columns(2)
    with dd1:
        sel_cat = st.selectbox("Market Sector", list(available_categories.keys()), index=0, key="cat_sel")
    with dd2:
        syms_in_cat = available_categories.get(sel_cat, ["USDCAD"])
        prev_sym = st.session_state.ai_state["symbol"]
        def_idx = syms_in_cat.index(prev_sym) if prev_sym in syms_in_cat else 0
        sel_sym = st.selectbox("Trading Instrument", syms_in_cat, index=def_idx, key="sym_sel")
        st.session_state.ai_state["symbol"] = sel_sym

    # Live Ticker Row
    q = get_symbol_live_quote(sel_sym) if mt5_connected else {}
    if q and q.get("bid", 0) > 0:
        dig = q.get("digits", 5)
        bid_str = fmt_price(q["bid"], dig)
        ask_str = fmt_price(q["ask"], dig)
        desc = q.get("description", "")
        st.markdown(f"""
        <div class="ticker-row">
          <div>
            <div class="ticker-pair">{sel_sym}</div>
            <div class="ticker-desc">{desc}</div>
          </div>
          <div style="display:flex;align-items:center;gap:12px;">
            <div style="text-align:right;"><div style="font-size:10px;color:#64748B;letter-spacing:0.05em;">BID</div><div class="ticker-bid">{bid_str}</div></div>
            <div style="text-align:right;"><div style="font-size:10px;color:#64748B;letter-spacing:0.05em;">ASK</div><div class="ticker-ask">{ask_str}</div></div>
            <span class="ticker-spread">{q['spread']}</span>
          </div>
        </div>
        """, unsafe_allow_html=True)

    # Timeframe + Sync row
    tf_c1, tf_c2 = st.columns([3, 1])
    with tf_c1:
        tf_choice = st.radio("Chart Timeframe", ["15M Entry", "4H Macro", "1H Trend"], horizontal=True, key="tf_radio")
    with tf_c2:
        st.write("")
        st.markdown('<div class="btn-sync">', unsafe_allow_html=True)
        sync_btn = st.button("⟳ Sync Feed", use_container_width=True, key="sync_btn")
        st.markdown('</div>', unsafe_allow_html=True)

    tf_map = {"15M Entry": 15, "4H Macro": 16388, "1H Trend": 16385}
    tf_code = tf_map.get(tf_choice, 15)

    # Chart
    if mt5_connected:
        with st.spinner("Loading chart...") if sync_btn else st.empty():
            df_c = get_technical_dataframe(sel_sym, tf_code, count=60)
        if not df_c.empty:
            fig = go.Figure()
            fig.add_trace(go.Candlestick(
                x=df_c["time"], open=df_c["open"], high=df_c["high"],
                low=df_c["low"], close=df_c["close"], name="Price",
                increasing_line_color="#34D399", increasing_fillcolor="rgba(52,211,153,0.6)",
                decreasing_line_color="#F43F5E", decreasing_fillcolor="rgba(244,63,94,0.6)",
            ))
            fig.add_trace(go.Scatter(x=df_c["time"], y=df_c["ema20"], line=dict(color="#22D3EE", width=1.5), name="EMA 20"))
            fig.add_trace(go.Scatter(x=df_c["time"], y=df_c["ema50"], line=dict(color="#F59E0B", width=1.5), name="EMA 50"))
            fig.update_layout(
                height=270, margin=dict(l=0, r=0, t=4, b=4),
                paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#0F172A",
                xaxis=dict(showgrid=True, gridcolor="#1E293B", tickfont=dict(color="#64748B", size=10), rangeslider=dict(visible=False)),
                yaxis=dict(showgrid=True, gridcolor="#1E293B", tickfont=dict(color="#64748B", size=10), side="right", tickformat=".5g"),
                legend=dict(orientation="h", x=0, y=1.05, font=dict(color="#94A3B8", size=10), bgcolor="rgba(0,0,0,0)"),
            )
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

            last = df_c.iloc[-1]
            rsi_v = last["rsi"]
            rsi_col = "red" if rsi_v > 70 else ("amber" if rsi_v < 30 else "cyan")
            st.markdown(f"""
            <div class="chip-row">
              <div class="chip"><span class="chip-label">RSI 14</span><span class="chip-value {rsi_col}">{rsi_v:.1f}</span></div>
              <div class="chip"><span class="chip-label">ATR</span><span class="chip-value">{last['atr']:.5g}</span></div>
              <div class="chip"><span class="chip-label">EMA 20</span><span class="chip-value cyan">{last['ema20']:.5g}</span></div>
              <div class="chip"><span class="chip-label">EMA 50</span><span class="chip-value amber">{last['ema50']:.5g}</span></div>
              <div class="chip"><span class="chip-label">MACD</span><span class="chip-value {'green' if last['macd_hist']>=0 else 'red'}">{last['macd_hist']:+.5g}</span></div>
            </div>
            """, unsafe_allow_html=True)
    else:
        st.info("Connect MetaTrader 5 to stream live chart data.")

    st.markdown('</div>', unsafe_allow_html=True)

# ╔══════════════════════════════════════╗
# ║     CENTER — AI BRAIN REACTOR       ║
# ╚══════════════════════════════════════╝
with col_c:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="card-header"><div class="card-accent-dot dot-amber"></div><span class="card-title">AI Brain Reactor</span></div>', unsafe_allow_html=True)

    dec = st.session_state.ai_state["decision"]
    conf = st.session_state.ai_state["confidence"]
    strength = st.session_state.ai_state["strength"]
    ts = st.session_state.ai_state["timestamp"]
    rr = st.session_state.ai_state["risk_reward"]
    rationale = st.session_state.ai_state["reasoning"]

    dec_class = dec.lower()
    badge_cls = {"BUY": "green", "SELL": "red", "HOLD": "amber"}.get(dec, "neutral")
    str_cls = {"VERY STRONG": "cyan", "STRONG": "green", "MODERATE": "amber", "NEUTRAL": "neutral"}.get(strength, "neutral")

    st.markdown(f"""
    <div class="reactor-wrap">
      <div class="reactor-outer {dec_class}">
        <div class="reactor-inner {dec_class}">{dec}</div>
      </div>
      <div class="badge-row">
        <span class="badge {str_cls}">AI SIGNAL &middot; {strength}</span>
        <span class="badge cyan">CONFIDENCE {conf}%</span>
      </div>
      <div class="stat-pills">
        <div class="stat-pill"><div class="stat-pill-label">Target</div><div class="stat-pill-value">{sel_sym}</div></div>
        <div class="stat-pill"><div class="stat-pill-label">Age</div><div class="stat-pill-value">{ts}</div></div>
        <div class="stat-pill"><div class="stat-pill-label">R&thinsp;:&thinsp;R</div><div class="stat-pill-value">{rr}</div></div>
      </div>
    </div>
    <div class="rationale-box">
      <div class="rationale-heading">Portfolio Manager Rationale</div>
      <div class="rationale-body">{rationale}</div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="btn-primary">', unsafe_allow_html=True)
    scan_btn = st.button(f"⚡  SCAN & EXECUTE  ({sel_sym})", use_container_width=True, key="scan_btn")
    st.markdown('</div>', unsafe_allow_html=True)

    if st.session_state.ai_state["last_exec"]:
        ei = st.session_state.ai_state["last_exec"]
        if ei.get("success"):
            if ei.get("status") == "HOLD":
                st.info(ei.get("message", ""))
            else:
                st.success(ei.get("message", ""))
        else:
            st.warning(ei.get("message", ""))

    st.markdown('</div>', unsafe_allow_html=True)

# ╔══════════════════════════════════════╗
# ║   RIGHT — RISK & POSITION MATRIX    ║
# ╚══════════════════════════════════════╝
with col_r:
    st.markdown('<div class="card">', unsafe_allow_html=True)
    st.markdown('<div class="card-header"><div class="card-accent-dot dot-green"></div><span class="card-title">Risk & Position Matrix</span></div>', unsafe_allow_html=True)

    rc1, rc2 = st.columns(2)
    with rc1:
        lot_size = st.number_input("Lot Size", value=0.01, min_value=0.01, max_value=10.0, step=0.01, format="%.2f", key="lot_in")
    with rc2:
        sl_pips = st.number_input("Stop Loss (Pips)", value=30, min_value=0, step=5, key="sl_in")

    st.markdown(f'<div style="font-size:11px;color:#475569;font-weight:600;margin:0.5rem 0 0.35rem;">ACTIVE MT5 ORDERS: {len(open_pos)}</div>', unsafe_allow_html=True)

    if open_pos:
        for pos in open_pos:
            pnl_v = pos["profit"]
            pc = pnl_class(pnl_v)
            side_cls = "order-side-buy" if pos["type"] == "BUY" else "order-side-sell"
            op = fmt_price(pos["price_open"], 5)
            cur = fmt_price(pos["price_current"], 5)
            st.markdown(f"""
            <div class="order-card">
              <div class="order-top">
                <span class="order-ticket">#{pos['ticket']}</span>
                <span class="order-symbol">{pos['symbol']}</span>
                <span class="{side_cls}">{pos['type']}</span>
                <span style="margin-left:auto;" class="order-pnl-{'pos' if pnl_v>=0 else 'neg'}">{fmt_pnl(pnl_v)}</span>
              </div>
              <div class="order-meta">Vol: {pos['volume']} &nbsp;|&nbsp; Open: {op} &nbsp;|&nbsp; Cur: {cur}</div>
            </div>
            """, unsafe_allow_html=True)
            cl1, cl2 = st.columns([3, 1])
            with cl2:
                st.markdown('<div class="btn-close">', unsafe_allow_html=True)
                if st.button("Close", key=f"close_{pos['ticket']}"):
                    res = close_position(pos["ticket"])
                    st.toast(res["message"])
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)

        st.markdown('<div style="height:8px;"></div>', unsafe_allow_html=True)
        st.markdown('<div class="btn-danger">', unsafe_allow_html=True)
        if st.button("⚠  EMERGENCY CLOSE ALL", use_container_width=True, key="emergency_btn"):
            results = close_all_positions()
            st.toast(f"Closing {len(open_pos)} position(s)...")
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)
    else:
        st.markdown('<div style="background:#0F172A;border:1px solid #1E293B;border-radius:8px;padding:1.25rem;text-align:center;color:#334155;font-size:13px;margin-top:0.5rem;">No active open positions</div>', unsafe_allow_html=True)

    st.markdown('</div>', unsafe_allow_html=True)

# ── SCAN EXECUTION LOGIC ──────────────────────────────────────────────────────
if scan_btn:
    if not mt5_connected:
        st.error("❌ MT5 terminal offline. Check config.py credentials.")
    else:
        with st.status(f"Autonomous cycle running for {sel_sym}...", expanded=True) as status:
            status.update(label=f"1/3 — Data Analyst computing indicators for {sel_sym}...")
            df_m15 = get_technical_dataframe(sel_sym, mt5.TIMEFRAME_M15, count=100)
            df_h4  = get_technical_dataframe(sel_sym, mt5.TIMEFRAME_H4,  count=100)

            status.update(label=f"2/3 — Portfolio Manager (LLM) evaluating {sel_sym} setup...")
            decision_obj = get_ai_decision(df_m15=df_m15, df_h4=df_h4, symbol=sel_sym)


            status.update(label=f"3/3 — Risk Officer routing {decision_obj['decision']} order to MT5...")
            exec_res = execute_trade(
                sel_sym, decision_obj["decision"],
                lot_size=lot_size, sl_pips=sl_pips, tp_pips=sl_pips * 2,
            )

            st.session_state.ai_state.update({
                "symbol": sel_sym,
                "decision": decision_obj["decision"],
                "confidence": decision_obj["confidence"],
                "strength": decision_obj["strength"],
                "reasoning": decision_obj["reasoning"],
                "risk_reward": decision_obj["risk_reward"],
                "timestamp": datetime.now().strftime("%H:%M:%S"),
                "last_exec": exec_res,
            })
            status.update(label=f"Complete — Verdict: {decision_obj['decision']}  |  {exec_res.get('message','')[:60]}", state="complete", expanded=False)
        st.rerun()
