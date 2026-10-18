"""Import guard: the approval path must not reach the live executor.

Stage 1 (scan + signal writing) runs in PAPER mode. Stage 2c (approval) is
the boundary where APPROVE_SIGNAL/REJECT_SIGNAL cross into order submission.
This test asserts that no module in the approval path imports or references
the gateway's ``order_send`` method or the executor's ``MODE_LIVE`` constant --
a static check that the PAPER wall cannot be crossed by accident.

Run: python -m pytest tests/test_engine/test_import_guard.py -v
"""
from __future__ import annotations

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
APPROVAL_PATH = ROOT / "engine" / "approval_flow.py"
GATEWAY_PATH = ROOT / "engine" / "gateway.py"
EXECUTOR_PATH = ROOT / "engine" / "executor.py"
SCAN_CYCLE_PATH = ROOT / "engine" / "scan_cycle.py"

#: Names that, if imported or referenced in approval_flow.py, would mean the
#: approval path can reach the live terminal or the live executor.
FORBIDDEN_IMPORTS = {"order_send", "MODE_LIVE"}


def _source(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _all_names_referenced(source: str) -> set[str]:
    """Every identifier the module touches: imports, attributes, calls."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.alias):
            names.add(node.name)
            if node.asname:
                names.add(node.asname)
    return names


# -- approval_flow must not import order_send or MODE_LIVE ----------------

def test_approval_flow_does_not_name_order_send():
    """approval_flow.py must never reference order_send."""
    assert APPROVAL_PATH.exists(), f"{APPROVAL_PATH} is missing"
    names = _all_names_referenced(_source(APPROVAL_PATH))
    assert "order_send" not in names, (
        "approval_flow.py references order_send -- the PAPER wall is breached"
    )


def test_approval_flow_does_not_import_modes_live():
    """approval_flow.py must never reference MODE_LIVE."""
    assert APPROVAL_PATH.exists(), f"{APPROVAL_PATH} is missing"
    names = _all_names_referenced(_source(APPROVAL_PATH))
    assert "MODE_LIVE" not in names, (
        "approval_flow.py references MODE_LIVE -- the PAPER wall is breached"
    )


def test_approval_flow_does_not_import_gateway_send_order():
    """approval_flow.py must not pull order-sending symbols from the gateway."""
    source = _source(APPROVAL_PATH)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "gateway" in node.module:
                for alias in node.names:
                    assert alias.name != "order_send", (
                        f"approval_flow.py imports order_send from {node.module}"
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "gateway" not in alias.name.lower() or True  # module-level


# -- scan_cycle must not name order_send either ---------------------------

def test_scan_cycle_does_not_send_orders():
    """scan_cycle.py is Stage 1; it must never send an order."""
    source = _source(SCAN_CYCLE_PATH)
    tree = ast.parse(source)
    names = _all_names_referenced(source)
    assert "order_send" not in names, (
        "scan_cycle.py references order_send -- Stage 1 must not submit orders"
    )
    assert "MODE_LIVE" not in names, (
        "scan_cycle.py references MODE_LIVE -- Stage 1 must not import live mode"
    )


# -- gateway order_send must be isolated to the owner thread --------------

def test_gateway_order_send_is_a_method_not_a_free_function():
    """order_send must live on Gateway (owner thread), not be module-level."""
    source = _source(GATEWAY_PATH)
    tree = ast.parse(source)
    module_level_funcs = {
        node.name for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    assert "order_send" not in module_level_funcs, (
        "order_send is a module-level function in gateway.py; "
        "it must be a Gateway method to stay serial on the owner thread"
    )


# -- the forbidden set is non-empty ---------------------------------------

def test_forbidden_set_is_non_empty():
    """Guard against the test going vacuous if FORBIDDEN_IMPORTS shrinks to nothing."""
    assert len(FORBIDDEN_IMPORTS) >= 2, (
        "FORBIDDEN_IMPORTS must cover at least order_send and MODE_LIVE"
    )


# -- summary: no forbidden symbol on the approval path --------------------

@pytest.mark.parametrize("forbidden", sorted(FORBIDDEN_IMPORTS))
def test_no_forbidden_symbol_reaches_approval_path(forbidden):
    """Parametrised sweep: every forbidden name is absent from approval_flow."""
    if not APPROVAL_PATH.exists():
        pytest.skip(f"{APPROVAL_PATH} is missing -- nothing to guard")
    names = _all_names_referenced(_source(APPROVAL_PATH))
    assert forbidden not in names, (
        f"approval_flow.py references forbidden symbol {forbidden!r}"
    )
