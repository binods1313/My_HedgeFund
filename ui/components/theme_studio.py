"""Theme Studio. Opens from the header and does not change the page layout.

Popovers are click controls. Colours are stored as a strict palette and
applied through ui.theme. This file has no hex literals.
"""

from __future__ import annotations

import json

import streamlit as st

from ui.theme import PRESETS, TOKENS, tokens, use_palette
from ui.theme_prefs import (
    HEX_RE,
    Palette,
    guard_palette,
    load_palette,
    preset_palette,
    reset_palette,
    save_palette,
)

_ROLES = (
    ("primary", "Primary accent"),
    ("accent_cyan", "Card cyan"),
    ("accent_green", "Card green"),
    ("accent_amber", "Card amber"),
    ("accent_violet", "Card violet"),
    ("accent_red", "Card red"),
    ("accent_journal", "Journal"),
    ("gain", "Gain ▲"),
    ("loss", "Loss ▼"),
)
_BACKGROUNDS = (TOKENS["bg"], TOKENS["panel"], TOKENS["bg_deep"])


def render_theme_studio() -> None:
    with st.popover("🎨 Theme Studio", key="btn_theme_studio"):
        st.caption("Click to open. This is not a hover menu.")
        _seed_if_needed()
        _preset_row()
        _role_fields()
        _motion_fields()
        _commit_draft()
        _preview()
        _file_row()


def _seed_if_needed() -> None:
    pushed = st.session_state.pop("_theme_push", None)
    if pushed is not None:
        _write_keys(pushed)
        return
    if st.session_state.get("_theme_ready"):
        return
    _write_keys(load_palette())


def _write_keys(palette: Palette) -> None:
    for role, _label in _ROLES:
        colour = getattr(palette, role)
        st.session_state[f"w_theme_{role}"] = colour
        st.session_state[f"w_theme_{role}_hex"] = colour
    st.session_state["w_theme_glow"] = palette.glow
    st.session_state["w_theme_motion"] = palette.motion
    st.session_state["_theme_ready"] = True


def _preset_row() -> None:
    columns = st.columns(3)
    for index, name in enumerate(PRESETS):
        with columns[index % 3]:
            slug = name.lower().replace(" ", "-")
            if st.button(name, key=f"btn-theme-{slug}", width="stretch"):
                _install(preset_palette(name))


def _role_fields() -> None:
    for role, label in _ROLES:
        colour_col, hex_col = st.columns([1, 2])
        with colour_col:
            st.color_picker(label, key=f"w_theme_{role}", on_change=_copy_hex, args=(role,))
        with hex_col:
            st.text_input(
                f"{label} hex",
                key=f"w_theme_{role}_hex",
                on_change=_copy_picker,
                args=(role,),
                label_visibility="collapsed",
            )
            typed = str(st.session_state.get(f"w_theme_{role}_hex", ""))
            if typed and HEX_RE.fullmatch(typed) is None:
                st.caption(f"{label} must be #RRGGBB")


def _copy_hex(role: str) -> None:
    st.session_state[f"w_theme_{role}_hex"] = str(st.session_state.get(f"w_theme_{role}", "")).upper()


def _copy_picker(role: str) -> None:
    typed = str(st.session_state.get(f"w_theme_{role}_hex", "")).strip()
    if HEX_RE.fullmatch(typed):
        st.session_state[f"w_theme_{role}"] = typed.upper()
        st.session_state[f"w_theme_{role}_hex"] = typed.upper()


def _motion_fields() -> None:
    st.select_slider("Glow intensity", options=["off", "low", "normal"], key="w_theme_glow")
    st.select_slider("Background motion", options=["off", "low", "normal"], key="w_theme_motion")


def _draft() -> Palette | None:
    data: dict[str, object] = {
        "version": 1,
        "preset": "Custom",
        "glow": st.session_state.get("w_theme_glow", "normal"),
        "motion": st.session_state.get("w_theme_motion", "normal"),
    }
    for role, _label in _ROLES:
        colour = str(st.session_state.get(f"w_theme_{role}", "")).strip()
        if HEX_RE.fullmatch(colour) is None:
            return None
        data[role] = colour
    try:
        return Palette.model_validate(data)
    except ValueError:
        return None


def _material(palette: Palette) -> dict[str, object]:
    data = palette.model_dump()
    data.pop("preset", None)
    data.pop("version", None)
    return data


def _commit_draft() -> None:
    draft = _draft()
    if draft is None:
        st.caption("A colour is not #RRGGBB yet. The saved palette stays.")
        return
    guarded, notes = guard_palette(draft, _BACKGROUNDS)
    for note in notes:
        st.caption(note)
    if _material(guarded) != _material(draft):
        _install(guarded)
        return
    if _material(guarded) != _material(load_palette()):
        save_palette(guarded)
        use_palette(guarded)
        st.rerun()


def _install(palette: Palette) -> None:
    guarded, notes = guard_palette(palette, _BACKGROUNDS)
    save_palette(guarded)
    use_palette(guarded)
    st.session_state["_theme_notes"] = notes
    st.session_state["_theme_push"] = guarded
    st.rerun()


def _preview() -> None:
    for note in st.session_state.get("_theme_notes") or []:
        st.caption(note)
    active = tokens()
    st.html(
        "<div style='display:flex;gap:8px;align-items:center;flex-wrap:wrap'>"
        f"<div style='background:var(--panel);color:var(--text);border:1px solid {active['accent']};"
        "padding:8px;border-radius:10px'>Sample card</div>"
        f"<span style='background:{active['accent']};color:var(--bg-deep);padding:6px 10px;"
        "font-weight:700;border-radius:8px'>Button</span>"
        f"<span style='color:{active['gain']}'>▲</span>"
        f"<span style='color:{active['loss']}'>▼</span>"
        f"<span style='background:color-mix(in srgb, {active['accent']} 22%, var(--panel));"
        "color:var(--text);padding:8px;border-radius:8px'>Nav</span>"
        "</div>"
    )


def _file_row() -> None:
    cols = st.columns(3)
    with cols[0]:
        if st.button("Reset to default", key="btn_theme_reset"):
            _install(reset_palette())
    with cols[1]:
        st.download_button(
            "Export JSON",
            data=load_palette().model_dump_json(indent=2),
            file_name="ui_palette.json",
            mime="application/json",
            key="btn_theme_export",
        )
    with cols[2]:
        st.caption("Import below")
    raw = st.text_area("Import JSON", key="w_theme_import", height=80)
    if st.button("Import JSON", key="btn_theme_import"):
        _import_raw(raw)


def _import_raw(raw: str) -> None:
    try:
        payload = json.loads(raw)
        palette = Palette.model_validate(payload)
    except (json.JSONDecodeError, ValueError):
        st.caption("Import rejected. The saved palette stays.")
        return
    _install(palette)
