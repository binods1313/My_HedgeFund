"""Link-outs to Finviz. Nothing is fetched from finviz.com."""

from __future__ import annotations

import streamlit as st

FOREX_PAGE = "https://finviz.com/forex.ashx"
FUTURES_PAGE = "https://finviz.com/futures.ashx"
_NON_EQUITY = {"forex", "metals_commodities", "crypto", "indices"}


def finviz_links(symbol: str | None, category: str | None) -> list[tuple[str, str]]:
    """Known public pages only. Equity quotes stay on the letter-ticker page."""
    code = (symbol or "").strip().upper()
    cat = (category or "").strip().lower()
    links: list[tuple[str, str]] = []
    equity = (
        code.isalpha()
        and 1 <= len(code) <= 5
        and cat not in _NON_EQUITY
    )
    if equity:
        links.append((f"Finviz {code}", f"https://finviz.com/quote.ashx?t={code}"))
    if cat == "forex" or (
        len(code) == 6 and code.isalpha() and cat not in {"metals_commodities", "crypto", "indices"}
    ):
        links.append(("Finviz forex", FOREX_PAGE))
    if cat in {"metals_commodities", "crypto", "indices"}:
        links.append(("Finviz futures", FUTURES_PAGE))
    return links


def render_finviz_links(symbol: str | None, category: str | None) -> None:
    links = finviz_links(symbol, category)
    if not links:
        return
    st.caption(
        "Finviz opens in the browser. An official Elite export could be "
        "integrated if the operator subscribes. Nothing is scraped."
    )
    columns = st.columns(len(links), gap="small")
    for column, (label, url) in zip(columns, links):
        with column:
            st.link_button(label, url)
