import streamlit as st

from config_loader import load_config
from engine.config import load_engine_config
from ui import init_state, inject_theme
from ui.components.ai_signals import render_ai_signals
from ui.components.alert_banner import render_alert_banner
from ui.components.analytics_row import render_analytics_row
from ui.components.auxiliary_views import render_journal_view
from ui.components.chart import render_chart
from ui.components.finviz_links import render_finviz_links
from ui.components.header import TAB_LABELS, render_header
from ui.components.ideas_watchlist import render_ideas_watchlist
from ui.components.infoway_panel import render_infoway_research
from ui.components.nav import render_navigation
from ui.components.news_tab import render_news_tab
from ui.components.order_form import render_order_form
from ui.components.research_desk import render_research_desk
from ui.components.positions_matrix import render_positions_matrix
from ui.components.snapshot import load_shared_snapshot
from ui.build_stamp import stamp_text

st.set_page_config(
    page_title="AI Hedge Fund Command Center",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

config = load_config()
init_state(config.symbols.whitelist)
inject_theme()

st.session_state["_snapshot_phase"] = "full"
snapshot = load_shared_snapshot(refresh=True)
render_navigation(snapshot)
render_header(config.symbols.whitelist)
view = st.session_state.get("active_tab", TAB_LABELS[0])
snapshot = st.session_state.get("_run_snapshot", snapshot)
render_alert_banner(snapshot, float(load_engine_config().risk.max_total_exposure_pct))

if view == TAB_LABELS[0]:
    analytical, terminal = st.columns([7, 3], gap="medium")
    with analytical:
        chart_frame = render_chart(st.session_state.get("selected_asset"))
        render_analytics_row(chart_frame, snapshot.account)
        render_finviz_links(
            st.session_state.get("selected_asset"),
            st.session_state.get("asset_category"),
        )
        render_infoway_research(st.session_state.get("selected_asset"))
        render_ideas_watchlist()
    with terminal:
        render_order_form(config.symbols.whitelist)
        render_positions_matrix(snapshot)
elif view == TAB_LABELS[1]:
    pass
elif view == TAB_LABELS[2]:
    render_ai_signals(config.symbols.whitelist)
elif view == TAB_LABELS[3]:
    render_news_tab(snapshot)
elif view == TAB_LABELS[4]:
    render_journal_view(snapshot)
elif view == TAB_LABELS[5]:
    render_research_desk(config.symbols.whitelist)

st.caption(stamp_text())
