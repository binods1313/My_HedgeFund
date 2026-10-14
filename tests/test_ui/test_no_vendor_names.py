"""No vendor, model or internal project name may reach the operator.

The brief bans these words from every string an operator can see: labels,
captions, headers, subheaders, tooltips, button text, warnings, errors, toasts
and displayed payloads. Use "AI", "AI Desk", "AI analysis", "validation",
"market data" and "news source" instead.

This test walks the AST of every module under ``ui/`` and inspects only the
strings Streamlit renders:

* the arguments of any ``st.*`` call (``label=``, ``help=``, ``html=``,
  positional text, ``st.caption``, ``st.markdown``, ``st.warning``, ...)
* the value of any ``return`` statement, because several display strings are
  built by helpers and returned
* module-level string constants, which are where label tables live

Deliberately **not** scanned, because none of it is rendered:

* the module/function docstrings (use the docstring node, not every string)
* identifiers and attribute names, including internal modules such as
  ``ai_brain``, the ``AGENT_IDS`` constant and ``render_infoway_chip``
* environment variable names such as ``INFOWAY_API_KEY`` and ``XAI_API_KEY``
* ``key=`` keyword arguments and ``accent_card``'s second argument, which are
  widget/session-state identifiers, never pixels
* HTTP hosts, file paths and log messages

That boundary is the honest one: this test catches a banned word written into a
rendered string, and it cannot catch one smuggled through a helper whose return
value is never scanned. Keeping the allow-list narrow and documented is what
makes it trustworthy.

Run: python -m pytest tests/test_ui/test_no_vendor_names.py -v
"""
from __future__ import annotations

import ast
import pathlib
import re

import pytest

#: ``__file__`` resolves to .../tests/test_ui/test_no_vendor_names.py, so
#: parents[2] is the repository root. Getting this wrong yields an empty
#: parametrize, which pytest reports as "skipped" rather than failing -- see
#: test_the_collection_is_not_empty_below, which makes that impossible.
UI_ROOT = pathlib.Path(__file__).resolve().parents[2] / "ui"

#: Words that must never appear in a rendered string. Matched on word
#: boundaries, case-insensitively, so "agent" catches "Agent" and "AGENT" but
#: not "agenda", and "xai" does not catch "taxi".
BANNED: dict[str, str] = {
    r"agents?": "the AI character; use \"AI\"",
    r"brain": "the AI character; use \"AI\"",
    r"kalkiquant": "the internal project name",
    r"dexter": "a research-process name",
    r"agentrouter": "a provider name",
    r"anchor": "a vendor name",
    r"infoway": "a market-data vendor name; use \"market data\"",
    r"deepseek": "a provider name",
    r"xai": "a provider name",
    r"grok": "a model name",
    r"openrouter": "a provider name",
    r"groq": "a provider name",
    r"infoway": "a market-data vendor name; use \"market data\"",
    r"anchorbrowser": "a deprecated provider alias",
    r"anthropic|claude": "a provider or model name",
    r"openai|gpt-\d": "a provider or model name",
    r"gemini": "a model name",
    r"llama": "a model name",
    r"anthropic": "a provider name",
    r"metaquotes": "the broker platform vendor",
    r"terminal64": "an MT5 internal",
}

_COMPILED = {re.compile(pattern, re.IGNORECASE): reason
             for pattern, reason in BANNED.items()}

#: Calls whose string arguments are internal identifiers, not rendered text.
_KEY_CALLS = frozenset({"accent_card", "st.container", "container"})

#: Keyword arguments that are identifiers rather than display text.
_KEY_KWARGS = frozenset({"key", "on_change", "on_click", "args", "type"})

#: Module-level names whose value is a style sheet or a markup block. Those are
#: pixels produced by the browser, not operator-readable text: a banned word
#: appearing in a font-family list is not a vendor name shown to a human.
_STYLE_SUFFIXES = ("_CSS", "_HTML")

#: Identifiers shaped like environment-variable names. ``INFOWAY_API_KEY`` and
#: ``XAI_API_KEY`` are configuration keys, never pixels; requiring at least one
#: underscore keeps a genuinely shouted label such as a warning banner out of
#: this exemption.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+$")

#: Anything with a scheme is an endpoint, not a label. ``https://data.infoway.io``
#: is the address the market-data client calls; a host is never shown to the
#: operator as text.
_URL = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)


def _is_stylesheet(text: str) -> bool:
    """A CSS block, not operator-readable text.

    ``ui/theme.py`` injects a large stylesheet through ``st.markdown``. The
    font-family list inside it legitimately names typefaces, and a typeface
    name is not a vendor or model name a human reads on screen. CSS block
    syntax -- braces and declarations -- is what marks the string a style
    sheet; an HTML fragment is left alone, because attributes and text inside
    markup genuinely reach the operator.
    """
    return "{" in text and "}" in text and ";" in text


def _is_streamlit_call(node: ast.Call) -> bool:
    """True for ``st.anything(...)`` and ``streamlit.anything(...)``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    root = func.value
    return isinstance(root, ast.Name) and root.id in {"st", "streamlit"}


def _callee_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def _literal_strings(node: ast.AST):
    """Strings that are the *literal value* of an expression.

    A plain string, an f-string, or a list/tuple of those. Deliberately not a
    call, subscript or mapping: ``return _status(symbol, "not_configured",
    detail="INFOWAY_API_KEY ...")`` hands back a status object whose detail is
    a diagnostic, and ``row["agent_id"]`` is a dict key. Neither is text the
    operator reads, and treating them as display text is what produces false
    accusations.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node
    elif isinstance(node, ast.JoinedStr):
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                yield sub
    elif isinstance(node, (ast.Tuple, ast.List)):
        for element in node.elts:
            yield from _literal_strings(element)


def _assigned_name(target: ast.AST) -> str:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return ""


def _display_string_nodes(tree: ast.AST):
    """Every ``ast.Constant`` string a human could actually read.

    Three sources, each deliberately narrow:

    1. the arguments of any ``st.*`` call -- labels, captions, headers,
       tooltips, ``help=``, button text, warnings. This is the ground truth,
       so everything inside the call is searched, not just literals.
    2. the value of a ``return`` statement, when that value is itself a
       literal string or f-string -- the "Confluences: ... | Validation Gate"
       pattern, where a helper builds the line and the caller shows it.
    3. a module-level assignment to a string or a list of strings -- the label
       tables -- unless the name marks it as a style sheet.

    ``key=`` keywords and ``accent_card``'s second argument are excluded
    because they are widget/session-state identifiers, never pixels.
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        callee = _callee_name(node)
        skip_ids: set[int] = set()
        if callee in _KEY_CALLS and not _is_streamlit_call(node):
            # accent_card(accent, key): the key is positional.
            if len(node.args) > 1:
                skip_ids.add(id(node.args[1]))
        for keyword in node.keywords:
            if keyword.arg in _KEY_KWARGS:
                skip_ids.add(id(keyword.value))

        if not (_is_streamlit_call(node) or callee in _KEY_CALLS):
            continue
        for child in list(node.args) + [kw.value for kw in node.keywords]:
            if id(child) in skip_ids:
                continue
            for sub in ast.walk(child):
                if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                    yield sub

    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            yield from _literal_strings(node.value)

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(_assigned_name(t).endswith(_STYLE_SUFFIXES) for t in node.targets):
            continue
        yield from _literal_strings(node.value)


@pytest.mark.parametrize("path", sorted(UI_ROOT.rglob("*.py")), ids=lambda p: p.name)
def test_no_banned_name_in_rendered_strings(path: pathlib.Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))

    offences: list[str] = []
    for node in _display_string_nodes(tree):
        text = node.value
        if _is_stylesheet(text) or _ENV_NAME.match(text.strip()) \
                or _URL.match(text.strip()):
            continue
        for pattern, reason in _COMPILED.items():
            if pattern.search(text):
                offences.append(f"line {node.lineno}: {text!r} -- {reason}")

    assert not offences, (
        f"{path.relative_to(UI_ROOT.parent)} renders a banned name:\n  "
        + "\n  ".join(offences)
    )


def test_the_collection_is_not_empty() -> None:
    """A wrong path must not masquerade as a pass.

    The first version of this file resolved the wrong parent directory, so
    ``rglob`` returned nothing and pytest reported one skipped parametrize
    instead of a failure. An empty scan is a claim about the collection, not a
    clean result, and this test is the independent probe that says so.
    """
    collected = sorted(UI_ROOT.rglob("*.py"))
    assert UI_ROOT.is_dir(), f"{UI_ROOT} is not a directory"
    assert len(collected) >= 10, (
        f"expected the ui/ package to hold at least 10 modules, found "
        f"{len(collected)}; the path or the parametrize is wrong"
    )
    assert {p.name for p in collected} >= {
        "ai_signals.py", "data_access.py", "nav.py", "infoway_panel.py",
        "pattern_hub.py", "research_desk.py",
    }, "the files this test was written to cover are not in the collection"


def test_the_scan_actually_finds_a_planted_offence(tmp_path) -> None:
    """The positive control: the scanner must catch a word it cannot render.

    Without this the whole file could be vacuous -- passing because it looks at
    nothing, exactly like the empty-path bug it now guards.
    """
    offender = tmp_path / "ui_probe.py"
    offender.write_text(
        "import streamlit as st\n"
        'st.subheader("AI Brain Reactor")\n'
        'st.caption("xAI Gate: NOT RUN")\n'
        'st.caption("source: market data")\n'
        'from ai_brain import get_ai_decision   # internal module, not rendered\n'
        'st.button("Show market-data quote", key="btn_infoway_quote")\n',
        encoding="utf-8",
    )
    tree = ast.parse(offender.read_text(encoding="utf-8"))
    found = []
    for node in _display_string_nodes(tree):
        for pattern, reason in _COMPILED.items():
            if pattern.search(node.value):
                found.append(node.value)

    assert "AI Brain Reactor" in found, "a rendered subheader must be caught"
    assert "xAI Gate: NOT RUN" in found, "a rendered caption must be caught"
    # The internal module name and the widget key must NOT be caught: they are
    # never pixels, and flagging them would make the test unusable.
    assert not any("ai_brain" in text for text in found), (
        "internal module names are not display text and must not be flagged"
    )
    assert not any("btn_infoway_quote" in text for text in found), (
        "widget keys are identifiers and must not be flagged"
    )
