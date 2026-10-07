"""Order files must not import research clients or the shadow package."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ORDER_FILES = (
    "ui/components/order_form.py",
    "ui/components/positions_matrix.py",
    "engine/executor.py",
    "engine/risk.py",
    "engine/commands.py",
    "engine/gateway.py",
    "engine/approval_flow.py",
)
FORBIDDEN = ("infoway_client", "dexter_client", "learning")


def _imported(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(alias.name for alias in node.names)
    return names


def test_order_path_does_not_import_research_clients() -> None:
    offenders: list[str] = []
    seen = 0
    for relative in ORDER_FILES:
        path = ROOT / relative
        if not path.is_file():
            continue
        seen += 1
        for name in _imported(path):
            if any(token in name for token in FORBIDDEN):
                offenders.append(f"{relative}:{name}")
    assert seen >= 4
    assert offenders == []


def test_research_desk_has_no_order_imports() -> None:
    path = ROOT / "ui" / "components" / "research_desk.py"
    names = _imported(path)
    assert "MetaTrader5" not in names
    assert not any(name in {"submit_order", "close_position"} for name in names)
    text = path.read_text(encoding="utf-8")
    assert "Shadow mode: these outputs do not affect trading." in text
