from __future__ import annotations

from typing import Any

import streamlit as st

TOKENS: dict[str, str] = {
    "bg": "#121318",
    "bg_deep": "#07090F",
    "bg_high": "#243044",
    "panel": "#1A1C24",
    "panel_glass": "#141A24",
    "panel_glass_alpha": "0.72",
    "panel_hover": "#20232D",
    "border": "#262A36",
    "text": "#E6E8EE",
    "text_muted": "#9BA1B5",
    "gain": "#00E676",
    "loss": "#FF3B30",
    "warn": "#FFB020",
    "accent": "#B8F2FF",
    "accent_cyan": "#7ADFFF",
    "accent_green": "#3DDC97",
    "accent_amber": "#FFB020",
    "accent_violet": "#D2A8FF",
    "accent_red": "#FF8A8A",
    "accent_journal": "#5EEAD4",
    "accent_research": "#A9C1FF",
    "mono_primary": "#C5CAD8",
    "mono_cyan": "#A8B3C7",
    "mono_green": "#B7C4B8",
    "mono_amber": "#D2CBBF",
    "mono_violet": "#C4C0D4",
    "mono_red": "#D4BFC4",
    "button_bg": "#FFFFFF",
    "disabled_bg": "#2A2D38",
    "disabled_text": "#B0B5C5",
    "radius": "14px",
    "font_ui": "'Manrope','Inter',system-ui,sans-serif",
    "font_mono": "'JetBrains Mono','IBM Plex Mono',ui-monospace,monospace",
}

_ACCENTS = ("cyan", "green", "amber", "violet", "red", "research")
NAV_ACTIVE_MIX = 0.22
NAV_INACTIVE_MIX = 0.10

_OVERRIDES: dict[str, str] = {}
_GLOW = "normal"
_MOTION = "normal"


def tokens() -> dict[str, str]:
    merged = dict(TOKENS)
    merged.update(_OVERRIDES)
    return merged


def use_palette(palette: object) -> None:
    """Copy a validated palette onto the CSS variables for this run."""
    global _GLOW, _MOTION
    _OVERRIDES.clear()
    _OVERRIDES.update(
        {
            "accent": str(getattr(palette, "primary")),
            "accent_cyan": str(getattr(palette, "accent_cyan")),
            "accent_green": str(getattr(palette, "accent_green")),
            "accent_amber": str(getattr(palette, "accent_amber")),
            "accent_violet": str(getattr(palette, "accent_violet")),
            "accent_red": str(getattr(palette, "accent_red")),
            "accent_journal": str(getattr(palette, "accent_journal")),
            "accent_research": str(getattr(palette, "accent_research", TOKENS["accent_research"])),
            "gain": str(getattr(palette, "gain")),
            "loss": str(getattr(palette, "loss")),
        }
    )
    _GLOW = str(getattr(palette, "glow", "normal"))
    _MOTION = str(getattr(palette, "motion", "normal"))


def _preset(name: str, **roles: str) -> dict[str, object]:
    base: dict[str, object] = {
        "version": 1,
        "preset": name,
        "primary": TOKENS["accent"],
        "accent_cyan": TOKENS["accent_cyan"],
        "accent_green": TOKENS["accent_green"],
        "accent_amber": TOKENS["accent_amber"],
        "accent_violet": TOKENS["accent_violet"],
        "accent_red": TOKENS["accent_red"],
        "accent_journal": TOKENS["accent_journal"],
        "accent_research": TOKENS["accent_research"],
        "gain": TOKENS["gain"],
        "loss": TOKENS["loss"],
        "glow": "normal",
        "motion": "normal",
    }
    base.update(roles)
    base["preset"] = name
    return base


PRESETS: dict[str, dict[str, object]] = {
    "Aurora Cyan": _preset("Aurora Cyan"),
    "Emerald Ledger": _preset("Emerald Ledger", primary=TOKENS["accent_green"]),
    "Amber Terminal": _preset("Amber Terminal", primary=TOKENS["accent_amber"]),
    "Violet Nebula": _preset("Violet Nebula", primary=TOKENS["accent_violet"]),
    "Crimson Edge": _preset("Crimson Edge", primary=TOKENS["accent_red"]),
    "Monochrome Glass": _preset(
        "Monochrome Glass",
        primary=TOKENS["mono_primary"],
        accent_cyan=TOKENS["mono_cyan"],
        accent_green=TOKENS["mono_green"],
        accent_amber=TOKENS["mono_amber"],
        accent_violet=TOKENS["mono_violet"],
        accent_red=TOKENS["mono_red"],
    ),
}


def hex_rgba(token: str, alpha: float) -> str:
    text = tokens()[token].lstrip("#")
    red, green, blue = int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    return f"rgba({red},{green},{blue},{alpha})"


def _css() -> str:
    accent_rules = []
    for name in _ACCENTS:
        token = f"var(--accent-{name})"
        accent_rules.append(
            f"""
[class*="st-key-card-{name}"] {{
  border: 1px solid color-mix(in srgb, {token} 70%, transparent) !important;
  box-shadow: 0 0 18px color-mix(in srgb, {token} 25%, transparent);
  border-radius: var(--radius);
  background-color: var(--panel);
  background-image:
    linear-gradient(color-mix(in srgb, var(--text) 5%, transparent) 1px, transparent 1px),
    linear-gradient(90deg, color-mix(in srgb, var(--text) 5%, transparent) 1px, transparent 1px),
    linear-gradient(180deg, color-mix(in srgb, {token} 10%, var(--panel-glass)), var(--panel-glass));
  background-size: 22px 22px, 22px 22px, auto;
}}
@supports (backdrop-filter: blur(10px)) {{
  [class*="st-key-card-{name}"] {{
    background-color: color-mix(in srgb, var(--panel-glass) 72%, transparent);
    backdrop-filter: blur(10px);
  }}
}}
@media (prefers-reduced-motion: no-preference) {{
  [class*="st-key-card-{name}"] {{ transition: box-shadow 180ms ease; }}
  [class*="st-key-card-{name}"]:hover {{
    box-shadow: 0 0 26px color-mix(in srgb, {token} 40%, transparent);
  }}
}}
"""
        )
    accents = "\n".join(accent_rules)
    return f"""
:root {{
  --bg:{tokens()['bg']};
  --bg-deep:{tokens()['bg_deep']};
  --bg-high:{tokens()['bg_high']};
  --panel:{tokens()['panel']};
  --panel-glass:{tokens()['panel_glass']};
  --panel-hover:{tokens()['panel_hover']};
  --border:{tokens()['border']};
  --text:{tokens()['text']};
  --muted:{tokens()['text_muted']};
  --gain:{tokens()['gain']};
  --loss:{tokens()['loss']};
  --warn:{tokens()['warn']};
  --accent:{tokens()['accent']};
  --accent-cyan:{tokens()['accent_cyan']};
  --accent-green:{tokens()['accent_green']};
  --accent-amber:{tokens()['accent_amber']};
  --accent-violet:{tokens()['accent_violet']};
  --accent-red:{tokens()['accent_red']};
  --accent-journal:{tokens()['accent_journal']};
  --accent-research:{tokens()['accent_research']};
  --button-bg:{tokens()['button_bg']};
  --disabled-bg:{tokens()['disabled_bg']};
  --disabled-text:{tokens()['disabled_text']};
  --radius:{tokens()['radius']};
  --font-ui:{tokens()['font_ui']};
  --font-mono:{tokens()['font_mono']};
}}
@import url('https://fonts.googleapis.com/css2?family=Manrope:wght@400;600;700&family=JetBrains+Mono:wght@400;600&display=swap');

.glass-bg {{
  position: fixed; inset: 0; z-index: 0; pointer-events: none; overflow: hidden;
  background: linear-gradient(165deg, var(--bg-deep), var(--bg) 45%, var(--bg-high));
}}
.glass-bg .orb {{
  position: absolute; border-radius: 50%; filter: blur(42px); opacity: 0.42;
  will-change: transform;
}}
.glass-bg .orb-a {{ width: 34vw; height: 34vw; left: -8vw; top: -12vh; background: color-mix(in srgb, var(--accent-cyan) 55%, transparent); animation: drift-a 48s ease-in-out infinite; }}
.glass-bg .orb-b {{ width: 28vw; height: 28vw; right: -6vw; top: 8vh; background: color-mix(in srgb, var(--accent-violet) 45%, transparent); animation: drift-b 63s ease-in-out infinite; }}
.glass-bg .orb-c {{ width: 26vw; height: 26vw; left: 18vw; bottom: -14vh; background: color-mix(in srgb, var(--accent-green) 40%, transparent); animation: drift-c 71s ease-in-out infinite; }}
.glass-bg .orb-d {{ width: 22vw; height: 22vw; right: 16vw; bottom: 6vh; background: color-mix(in srgb, var(--accent-amber) 35%, transparent); animation: drift-d 57s ease-in-out infinite; }}
.glass-bg .orb-e {{ width: 18vw; height: 18vw; left: 42vw; top: 28vh; background: color-mix(in srgb, var(--accent-red) 28%, transparent); animation: drift-e 84s ease-in-out infinite; }}
@keyframes drift-a {{ 50% {{ transform: translate3d(7vw, 10vh, 0); }} }}
@keyframes drift-b {{ 50% {{ transform: translate3d(-6vw, 8vh, 0); }} }}
@keyframes drift-c {{ 50% {{ transform: translate3d(5vw, -9vh, 0); }} }}
@keyframes drift-d {{ 50% {{ transform: translate3d(-4vw, -7vh, 0); }} }}
@keyframes drift-e {{ 50% {{ transform: translate3d(3vw, 6vh, 0); }} }}
@media (prefers-reduced-motion: reduce) {{
  .glass-bg .orb {{ animation: none !important; }}
  * {{ transition: none !important; scroll-behavior: auto !important; }}
}}

.stApp, [data-testid="stAppViewContainer"] {{
  background: var(--bg-deep); color: var(--text); font-family: var(--font-ui);
}}
[data-testid="stMain"], [data-testid="stAppViewContainer"] .main {{
  position: relative; z-index: 1;
}}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ padding: 1rem 1.5rem 3rem; max-width: 100%; }}
#MainMenu, footer {{ visibility: hidden; }}
{accents}
[data-testid="stMetric"] {{
  background: transparent; border: 0; padding: .35rem .2rem;
}}
[data-testid="stMetricLabel"] p {{ color: var(--text); font-weight: 700; font-size: 0.8rem; letter-spacing: .06em; text-transform: uppercase; }}
[data-testid="stMetricValue"] {{ font-family: var(--font-mono); font-weight: 700; font-size: clamp(1.1rem, 1.6vw, 1.9rem); overflow: visible; text-overflow: clip; white-space: normal; }}
[data-testid="stMetricValue"] > div {{ overflow: visible; text-overflow: clip; white-space: normal; }}
[data-testid="stMetricDelta"] {{ font-family: var(--font-mono); font-weight: 700; }}
.mono, [data-testid="stNumberInput"] input {{ font-family: var(--font-mono); font-variant-numeric: tabular-nums; }}
.pos {{ color: var(--gain); }} .neg {{ color: var(--loss); }}
[class*="st-key-nav-"] button {{
  min-height: 44px; font-size: 1.12rem; font-weight: 700; border-radius: 12px;
}}
[class*="st-key-nav-"][class*="-on"] button {{
  box-shadow: 0 0 16px color-mix(in srgb, var(--accent-cyan) 35%, transparent);
  border-bottom: 3px solid var(--accent-cyan) !important;
}}
button[role="tab"] {{ color: var(--muted); min-height: 44px; font-weight: 700; }}
button[role="tab"][aria-selected="true"] {{ color: var(--text); border-bottom: 2px solid var(--accent); }}
[data-testid="stNumberInput"] > div > div, [data-baseweb="select"] > div {{
  background: var(--panel-hover); border: 1px solid var(--border); border-radius: 10px;
}}
.stButton > button[kind="primary"] {{
  background: var(--button-bg); color: var(--bg); border: 0; border-radius: 10px; font-weight: 700;
}}
.stButton > button[kind="primary"]:disabled {{ background: var(--disabled-bg); color: var(--disabled-text); border: 1px solid var(--border); }}
.stButton > button[kind="secondary"]:disabled {{ background: transparent; color: var(--disabled-text); border: 1px solid var(--border); opacity: 1; }}
[data-testid="stCaption"] {{ color: var(--text); font-size: 0.85rem; }}
[data-testid="stMarkdown"] p, [data-testid="stMarkdown"] li {{ color: var(--text); }}
.table-scroll {{ overflow-x: auto; max-width: 100%; }}
.hf-table {{ border-collapse: collapse; width: max-content; min-width: 100%; }}
.hf-table th {{
  font-weight: 700; color: var(--text); background: var(--panel-hover);
  text-align: left; padding: .45rem .65rem; white-space: nowrap; letter-spacing: .04em;
}}
.hf-table td {{
  color: var(--text); font-family: var(--font-mono); padding: .4rem .65rem;
  white-space: nowrap; border-top: 1px solid var(--border);
}}
.hf-empty {{ color: var(--text); }}
.sev-badge, .news-impact {{
  display: inline-block; padding: .18rem .5rem; border-radius: 999px;
  border: 1px solid var(--border); font-size: .65rem; font-weight: 700; letter-spacing: .04em;
}}
.sev-info {{ color: var(--accent-cyan); border-color: color-mix(in srgb, var(--accent-cyan) 45%, transparent); }}
.sev-warn {{ color: var(--accent-amber); border-color: color-mix(in srgb, var(--accent-amber) 45%, transparent); }}
.sev-error {{ color: var(--accent-red); border-color: color-mix(in srgb, var(--accent-red) 45%, transparent); }}
.hf-rel {{ cursor: help; border-bottom: 1px dotted var(--muted); }}
[data-testid="stNumberInput"] > div > div input, [data-baseweb="select"] > div > div {{ color: var(--text); }}
[data-testid="stNumberInput"] > div > div input::placeholder, [data-baseweb="select"] > div > div input::placeholder {{ color: var(--muted); }}
.stButton > button[kind="secondary"] {{
  background: transparent; color: var(--text); border: 1px solid var(--border); border-radius: 10px; font-weight: 700;
}}
.reactor-wrap {{ display:flex; justify-content:center; padding:1rem 0; }}
.reactor-outer {{ width:190px; height:190px; border-radius:50%; display:flex; align-items:center; justify-content:center; }}
.reactor-inner {{ width:148px; height:148px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:2rem; font-weight:700; font-family:var(--font-mono); }}
.reactor-outer.buy {{ border:2px solid var(--gain); box-shadow:0 0 32px color-mix(in srgb, var(--gain) 24%, transparent); }}
.reactor-outer.sell {{ border:2px solid var(--loss); box-shadow:0 0 32px color-mix(in srgb, var(--loss) 24%, transparent); }}
.reactor-outer.hold {{ border:2px solid var(--warn); box-shadow:0 0 32px color-mix(in srgb, var(--warn) 20%, transparent); }}
.reactor-inner.buy {{ background:radial-gradient(circle, color-mix(in srgb, var(--gain) 22%, transparent), transparent 72%); color:var(--gain); }}
.reactor-inner.sell {{ background:radial-gradient(circle, color-mix(in srgb, var(--loss) 22%, transparent), transparent 72%); color:var(--loss); }}
.reactor-inner.hold {{ background:radial-gradient(circle, color-mix(in srgb, var(--warn) 18%, transparent), transparent 72%); color:var(--warn); }}
.status-badge {{ display:inline-flex; align-items:center; border:1px solid var(--border); border-radius:999px; padding:.28rem .62rem; font-size:.7rem; font-weight:700; white-space:nowrap; }}
.status-badge.live, .status-badge.active {{ color:var(--gain); border-color:color-mix(in srgb, var(--gain) 35%, transparent); background:color-mix(in srgb, var(--gain) 7%, transparent); }}
.status-badge.offline, .status-badge.halted {{ color:var(--loss); border-color:color-mix(in srgb, var(--loss) 35%, transparent); background:color-mix(in srgb, var(--loss) 7%, transparent); }}
.status-badge.stale {{ color:var(--warn); border-color:color-mix(in srgb, var(--warn) 35%, transparent); background:color-mix(in srgb, var(--warn) 7%, transparent); }}
.news-countdown {{ display:inline-block; margin-top:0; padding:.25rem .5rem; border-radius:6px; background:color-mix(in srgb, var(--loss) 9%, transparent); color:var(--loss); border:1px solid color-mix(in srgb, var(--loss) 30%, transparent); font-family:var(--font-mono); font-size:.72rem; }}
.news-impact.high {{ color:var(--loss); border-color:color-mix(in srgb, var(--loss) 45%, transparent); }}
.news-impact.medium {{ color:var(--warn); border-color:color-mix(in srgb, var(--warn) 45%, transparent); }}
.news-impact.low {{ color:var(--muted); }}
.news-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: .75rem; align-items: start; }}
.news-card, .news-chip {{
  border-radius: var(--radius); padding: .8rem .9rem; color: var(--text);
  border: 1px solid color-mix(in srgb, var(--accent-amber) 70%, transparent);
  background: color-mix(in srgb, var(--panel-glass) 88%, transparent);
  overflow-wrap: anywhere;
}}
.news-card.high {{ border-color: color-mix(in srgb, var(--accent-red) 70%, transparent); }}
.news-card h3, .news-strip h2 {{ margin: .35rem 0; color: var(--text); font-weight: 700; }}
.news-kicker {{ font-size: .72rem; font-weight: 700; letter-spacing: .08em; text-transform: uppercase; color: var(--text); }}
.news-strip {{
  display: flex; flex-wrap: wrap; gap: .75rem; margin-bottom: .8rem; padding: .8rem 1rem;
  border-radius: var(--radius); border: 1px solid color-mix(in srgb, var(--accent-amber) 70%, transparent);
}}
.news-day {{ min-width: 7rem; }}
.news-chip {{ display: block; margin: .35rem 0; padding: .35rem .6rem; }}
[data-testid="stTooltip"] {{ background: var(--panel); color: var(--text); border: 1px solid var(--border); }}
.stToast {{ background: var(--panel); color: var(--text); border: 1px solid var(--border); }}
@media (max-width: 900px) {{
  .block-container {{ padding-left: 0.75rem; padding-right: 0.75rem; }}
  [class*="st-key-nav-"] button {{ font-size: 1.05rem; }}
}}
"""


def _task5_css() -> str:
    """Nav, segmented selection, breathing controls, and card motion.

    The selected segmented hook is button[role=radio][aria-checked=true]
    inside [data-testid=stButtonGroup] (Streamlit 1.64 ButtonGroup bundle).
    Popovers stay click-triggered. Motion is omitted when the palette or the
    OS asks for reduced motion.
    """
    glow = {"off": "0", "low": "0.45", "normal": "1"}.get(_GLOW, "1")
    play = "paused" if _MOTION == "off" else "running"
    durations = {
        "exchange": "8s",
        "portfolio": "11s",
        "signals": "6s",
        "news": "13s",
        "journal": "9s",
        "research": "10s",
    }
    delays = {
        "exchange": "0s",
        "portfolio": "-2s",
        "signals": "-4s",
        "news": "-1s",
        "journal": "-3s",
        "research": "-5s",
    }
    accents = {
        "exchange": "var(--accent-cyan)",
        "portfolio": "var(--accent-green)",
        "signals": "var(--accent-violet)",
        "news": "var(--accent-amber)",
        "journal": "var(--accent-journal)",
        "research": "var(--accent-research)",
    }
    motions = {
        "exchange": (
            "radial-gradient(circle, color-mix(in srgb, var(--accent-cyan) 85%, white) 0 1.6px, transparent 2.4px)",
            "18px 100%",
            "repeat-x",
            "nav-flow",
            "linear",
        ),
        "portfolio": (
            "radial-gradient(120% 80% at 50% 120%, color-mix(in srgb, var(--accent-green) 75%, white), transparent 62%)",
            "80% 70%",
            "no-repeat",
            "nav-wave",
            "ease-in-out",
        ),
        "signals": (
            "linear-gradient(115deg, transparent 46%, color-mix(in srgb, var(--accent-violet) 80%, white) 50%, transparent 54%), linear-gradient(65deg, transparent 46%, color-mix(in srgb, var(--accent-violet) 55%, white) 50%, transparent 54%), radial-gradient(circle at 18% 42%, color-mix(in srgb, var(--accent-violet) 90%, white) 0 2px, transparent 3px), radial-gradient(circle at 74% 60%, color-mix(in srgb, var(--accent-violet) 90%, white) 0 2px, transparent 3px)",
            "180% 180%, 180% 180%, 100% 100%, 100% 100%",
            "no-repeat",
            "nav-mesh",
            "ease-in-out",
        ),
        "news": (
            "repeating-linear-gradient(90deg, transparent 0 6px, color-mix(in srgb, var(--accent-amber) 75%, white) 6px 8px, transparent 8px 14px, color-mix(in srgb, var(--accent-amber) 40%, white) 14px 15px)",
            "48px 100%",
            "repeat-x",
            "nav-ticker",
            "linear",
        ),
        "journal": (
            "linear-gradient(rgba(255, 255, 255, 0.22) 1px, transparent 1px), linear-gradient(90deg, rgba(255, 255, 255, 0.22) 1px, transparent 1px)",
            "12px 12px, 12px 12px",
            "repeat",
            "nav-grid",
            "ease-in-out",
        ),
        "research": (
            "linear-gradient(120deg, transparent 38%, color-mix(in srgb, var(--accent-research) 80%, white) 50%, transparent 62%)",
            "220% 100%",
            "no-repeat",
            "nav-research",
            "ease-in-out",
        ),
    }
    nav_rules = []
    for slug, accent in accents.items():
        image, size, repeat, motion, easing = motions[slug]
        nav_rules.append(
            f"""
.st-key-nav-{slug} button, .st-key-nav-{slug}-on button {{
  --nav-accent: {accent};
  animation-duration: {durations[slug]};
  animation-delay: {delays[slug]};
  background-image: {image};
  background-size: {size};
  background-repeat: {repeat};
  animation-name: {motion};
  animation-timing-function: {easing};
  animation-iteration-count: infinite;
  animation-play-state: var(--motion-play);
}}
"""
        )
    nav = "\n".join(nav_rules)
    return f"""
:root {{ --glow-amp: {glow}; --motion-play: {play}; }}
[data-testid="stButtonGroup"] button[role="radio"] {{
  color: var(--text); border: 1px solid var(--border); min-height: 40px; font-weight: 700;
}}
[data-testid="stButtonGroup"] button[role="radio"][aria-checked="true"] {{
  color: var(--text) !important;
  border: 1px solid var(--accent) !important;
  background: color-mix(in srgb, var(--accent) 22%, var(--panel)) !important;
  box-shadow: inset 0 -3px 0 var(--accent);
}}
.st-key-w_order_side button[role="radio"]:nth-of-type(1)[aria-checked="true"] {{
  border-color: var(--gain) !important;
  background: color-mix(in srgb, var(--gain) 24%, var(--panel)) !important;
  box-shadow: inset 0 -3px 0 var(--gain);
}}
.st-key-w_order_side button[role="radio"]:nth-of-type(2)[aria-checked="true"] {{
  border-color: var(--loss) !important;
  background: color-mix(in srgb, var(--loss) 24%, var(--panel)) !important;
  box-shadow: inset 0 -3px 0 var(--loss);
}}
[class*="st-key-nav-"] button {{
  position: relative;
  min-height: 48px; font-size: 1.15rem; font-weight: 700; color: var(--text) !important;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.85);
  backdrop-filter: blur(12px) saturate(180%);
  -webkit-backdrop-filter: blur(12px) saturate(180%);
  background-color: rgba(255, 255, 255, 0.03) !important;
  border: 1px solid rgba(255, 255, 255, 0.1) !important;
  box-shadow: 0 0 calc(12px * var(--glow-amp)) color-mix(in srgb, var(--nav-accent, var(--accent-cyan)) 28%, transparent);
  border-radius: 12px;
}}
[class*="st-key-nav-"] button * {{
  position: relative;
  z-index: 2;
  color: var(--text) !important;
  font-weight: 700;
  text-shadow: 0 1px 2px rgba(0, 0, 0, 0.85);
}}
{nav}
[class*="st-key-nav-"][class*="-on"] button {{
  background-color: rgba(255, 255, 255, 0.06) !important;
  border-color: var(--nav-accent) !important;
  box-shadow:
    inset 4px 0 0 var(--nav-accent),
    0 0 calc(18px * var(--glow-amp)) color-mix(in srgb, var(--nav-accent) 45%, transparent);
  border-bottom: 3px solid var(--nav-accent) !important;
}}
@media (prefers-reduced-motion: no-preference) {{
  [class*="st-key-nav-"] button:hover {{
    transform: translateY(-2px);
    border-color: var(--nav-accent) !important;
    box-shadow: 0 0 calc(20px * var(--glow-amp)) color-mix(in srgb, var(--nav-accent) 50%, transparent);
  }}
  .st-key-nav-signals button:hover,
  .st-key-nav-signals-on button:hover {{
    background-color: color-mix(in srgb, var(--accent-violet) 16%, rgba(255, 255, 255, 0.05)) !important;
  }}
  .st-key-btn_submit_order button,
  .st-key-btn_sync_feed button,
  .st-key-btn_close_all button,
  .st-key-btn_theme_studio button,
  [class*="st-key-pop-"] button,
  [class*="st-key-close_"] button,
  .st-key-w_order_side button[role="radio"] {{
    animation: hf-breathe 4.8s ease-in-out infinite;
    animation-play-state: var(--motion-play);
  }}
  [class*="st-key-card-"]::before {{
    content: "";
    position: absolute; inset: -20%;
    background: repeating-linear-gradient(90deg, transparent 0 18px, color-mix(in srgb, var(--text) 7%, transparent) 19px);
    opacity: 0.28; pointer-events: none;
    animation: card-motif 64s linear infinite;
    animation-play-state: var(--motion-play);
  }}
  [class*="st-key-card-green"]::before {{ animation-duration: 48s; animation-direction: reverse; }}
  [class*="st-key-card-amber"]::before {{ animation-duration: 76s; }}
  [class*="st-key-card-violet"]::before {{ animation-duration: 84s; animation-direction: reverse; }}
  [class*="st-key-card-red"]::before {{ animation-duration: 90s; }}
  [class*="st-key-card-cyan"]::before {{ animation-duration: 58s; }}
}}
[class*="st-key-card-"] {{ position: relative; overflow: hidden;
  box-shadow: inset 0 1px 0 color-mix(in srgb, white 22%, transparent), 0 0 18px color-mix(in srgb, var(--accent) 18%, transparent);
}}
button:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
button:disabled, button[disabled] {{
  color: var(--disabled-text) !important; opacity: 1;
}}
/* Light chips keep the cyan fill. Text on that fill is charcoal. */
[data-baseweb="tag"] {{
  background-color: #B8F2FF !important;
  border: 1px solid #7ADFFF !important;
  border-radius: 999px !important;
}}
[data-baseweb="tag"],
[data-baseweb="tag"] span {{
  color: #14181F !important;
}}
[data-baseweb="select"] > div {{
  background: #161922 !important;
  border: 1.5px solid #9AA3B8 !important;
  border-radius: 12px !important;
  min-height: 46px;
  padding: 2px 8px !important;
}}
[data-baseweb="select"] > div:focus-within {{
  border-color: #B8F2FF !important;
  box-shadow: 0 0 0 1px #B8F2FF;
}}
[data-baseweb="select"] > div,
[data-baseweb="select"] > div * {{
  color: var(--text);
}}
[data-baseweb="popover"] [role="listbox"],
[data-baseweb="menu"] {{
  background: #1A1C24 !important;
  border: 1.5px solid #9AA3B8 !important;
  border-radius: 12px !important;
}}
[data-baseweb="popover"] [role="option"] {{
  color: #E6E8EE !important;
}}
[data-baseweb="popover"] [role="option"][aria-selected="true"] {{
  background: #B8F2FF !important;
  color: #14181F !important;
}}
/* Streamlit 1.64 draws selects with React Aria, not BaseWeb. */
[data-testid="stSelectbox"] [role="group"],
[data-testid="stMultiSelect"] > div[role="group"] {{
  background: #161922 !important;
  border: 1.5px solid #9AA3B8 !important;
  border-radius: 12px !important;
  min-height: 46px;
}}
[data-testid="stSelectbox"] [role="group"]:focus-within,
[data-testid="stMultiSelect"] > div[role="group"]:focus-within {{
  border-color: #B8F2FF !important;
  box-shadow: 0 0 0 1px #B8F2FF;
}}
[data-testid="stMultiSelect"] [data-tag] {{
  background: #B8F2FF !important;
  border: 1px solid #7ADFFF !important;
  border-radius: 999px !important;
  color: #14181F !important;
}}
[data-testid="stMultiSelect"] [data-tag] * {{
  color: #14181F !important;
}}
[role="listbox"] {{
  background: #1A1C24 !important;
  border: 1.5px solid #9AA3B8 !important;
  border-radius: 12px !important;
}}
[role="listbox"] [role="option"] {{
  color: #E6E8EE !important;
}}
[role="listbox"] [role="option"][aria-selected="true"],
[role="listbox"] [role="option"][data-selected="true"] {{
  background: #B8F2FF !important;
  color: #14181F !important;
}}
.hf-tone {{ margin: .15rem 0 0; font-weight: 700; }}
.hf-tone-safe {{ color: var(--gain); }}
.hf-tone-warning {{ color: var(--warn); }}
.hf-tone-risk {{ color: var(--loss); }}
.hf-tone-unknown {{ color: var(--text); }}
.st-key-card-cyan-volume::after,
.st-key-card-amber-margin::after,
.st-key-card-green-equity::after {{
  content: "";
  position: absolute;
  width: 160%;
  height: 160%;
  left: -30%;
  top: -50%;
  pointer-events: none;
  opacity: 0.45;
  background: conic-gradient(
    from 15deg,
    transparent 0 38%,
    color-mix(in srgb, var(--accent-cyan) 70%, white) 46%,
    transparent 54% 100%
  );
  animation: crystal-spin 28s linear infinite;
  animation-play-state: var(--motion-play);
}}
.st-key-card-amber-margin::after {{
  animation-duration: 36s;
  animation-direction: reverse;
  background: conic-gradient(
    from 200deg,
    transparent 0 38%,
    color-mix(in srgb, var(--accent-amber) 75%, white) 46%,
    transparent 54% 100%
  );
}}
.st-key-card-green-equity::after {{
  animation-duration: 44s;
  background: conic-gradient(
    from 80deg,
    transparent 0 38%,
    color-mix(in srgb, var(--accent-green) 75%, white) 46%,
    transparent 54% 100%
  );
}}
.st-key-card-cyan-volume:hover,
.st-key-card-amber-margin:hover,
.st-key-card-green-equity:hover {{
  transform: translateY(-3px);
}}
.st-key-card-cyan-volume:hover::after,
.st-key-card-amber-margin:hover::after,
.st-key-card-green-equity:hover::after {{
  opacity: 0.8;
}}
@keyframes crystal-spin {{ to {{ transform: rotate(360deg); }} }}
@keyframes nav-flow {{ to {{ background-position: 72px 0; }} }}
@keyframes nav-wave {{
  0%, 100% {{ background-size: 42% 46%; background-position: 50% 130%; }}
  50% {{ background-size: 150% 100%; background-position: 50% 100%; }}
}}
@keyframes nav-mesh {{
  0%, 100% {{ background-position: 0% 40%, 100% 70%, 0 0, 0 0; }}
  50% {{ background-position: 70% 60%, 10% 30%, 0 0, 0 0; }}
}}
@keyframes nav-ticker {{ to {{ background-position: 48px 0; }} }}
@keyframes nav-grid {{
  0% {{ background-size: 10px 16px, 10px 16px; }}
  45% {{ background-size: 18px 8px, 18px 8px; }}
  100% {{ background-size: 12px 12px, 12px 12px; }}
}}
@keyframes nav-research {{
  0%, 100% {{ background-position: 120% 0; }}
  50% {{ background-position: -20% 0; }}
}}
@keyframes hf-breathe {{
  50% {{ box-shadow: 0 0 calc(14px * var(--glow-amp)) color-mix(in srgb, var(--accent) 40%, transparent); }}
}}
@keyframes card-motif {{ to {{ transform: rotate(360deg); }} }}
@media (prefers-reduced-motion: reduce) {{
  [class*="st-key-nav-"] button,
  .st-key-btn_submit_order button,
  [class*="st-key-card-"]::before,
  [class*="st-key-card-"]::after,
  .glass-bg .orb {{
    animation: none !important; transform: none !important;
  }}
}}
"""


def inject_theme() -> None:
    """Regenerate :root variables from the saved palette once per run."""
    from ui.theme_prefs import load_palette

    use_palette(load_palette())
    st.markdown(f"<style>{_css()}{_task5_css()}</style>", unsafe_allow_html=True)
    st.html(
        '<div class="glass-bg" aria-hidden="true">'
        '<span class="orb orb-a"></span><span class="orb orb-b"></span>'
        '<span class="orb orb-c"></span><span class="orb orb-d"></span>'
        '<span class="orb orb-e"></span></div>'
    )


def plotly_theme() -> dict[str, Any]:
    """Return the Plotly configuration used by chart components."""
    return {
        "paper_bgcolor": "rgba(0,0,0,0)",
        "plot_bgcolor": "rgba(0,0,0,0)",
        "font": {"family": TOKENS["font_mono"], "color": TOKENS["text"]},
        "hovermode": "x unified",
        "margin": {"l": 8, "r": 8, "t": 8, "b": 8},
        "uirevision": "plotly-theme",
        "xaxis": {"gridcolor": TOKENS["border"], "color": TOKENS["text_muted"]},
        "yaxis": {"gridcolor": TOKENS["border"], "color": TOKENS["text_muted"]},
        "template": "plotly_dark",
    }
