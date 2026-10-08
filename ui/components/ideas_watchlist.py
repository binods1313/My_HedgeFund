"""Hand-typed ideas. Local file only. No orders and no agent scores."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ui.components.cards import render_html_table

STORE_PATH = Path(__file__).resolve().parents[2] / "data" / "ideas_watchlist.json"
_MAX_ROWS = 50


class Idea(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str = Field(min_length=1, max_length=80)
    symbol: str = Field(min_length=1, max_length=32)
    direction: str = Field(pattern="^(long|short|watch)$")
    note: str = Field(default="", max_length=240)


def load_ideas(path: Path | None = None) -> list[Idea]:
    target = path or STORE_PATH
    if not target.is_file():
        return []
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    ideas: list[Idea] = []
    for item in payload:
        try:
            ideas.append(Idea.model_validate(item))
        except ValidationError:
            continue
    return ideas[-_MAX_ROWS:]


def add_idea(
    source: str,
    symbol: str,
    direction: str,
    note: str,
    *,
    path: Path | None = None,
) -> Idea:
    idea = Idea(
        source=source.strip()[:80],
        symbol=symbol.strip()[:32],
        direction=direction.strip().lower(),
        note=note.strip()[:240],
    )
    target = path or STORE_PATH
    rows = load_ideas(target)
    rows.append(idea)
    rows = rows[-_MAX_ROWS:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps([row.model_dump() for row in rows], indent=2),
        encoding="utf-8",
    )
    return idea


def ideas_frame(ideas: list[Idea]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Source": idea.source,
                "Symbol": idea.symbol,
                "Direction": idea.direction,
                "Note": idea.note,
            }
            for idea in reversed(ideas)
        ],
        columns=["Source", "Symbol", "Direction", "Note"],
    )


def render_ideas_watchlist() -> None:
    with st.container(key="ideas-watchlist"):
        st.subheader("Ideas watchlist")
        st.caption(
            "On the Exchange page, under the chart and the secondary-data panel. "
            "Typed by hand and stored on this machine. Read only. "
            "Not an order and not an agent score."
        )
        source = st.text_input("Trader or source", key="w_idea_source", max_chars=80)
        symbol = st.text_input("Idea symbol", key="w_idea_symbol", max_chars=32)
        direction = st.selectbox("Idea direction", ["long", "short", "watch"], key="w_idea_direction")
        note = st.text_area("Idea note", key="w_idea_note", max_chars=240)
        if st.button("Add idea", key="btn_idea_add"):
            try:
                add_idea(source, symbol, str(direction), note)
            except ValidationError:
                st.caption("Enter a source, a symbol, and one of long, short, or watch.")
        render_html_table(ideas_frame(load_ideas()), empty="No ideas yet")
