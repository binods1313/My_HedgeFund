"""The order path must not reach research, shadow learning, or the data clients."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = (
    "execution.py",
    "engine/__main__.py",
    "engine/executor.py",
    "engine/approval_flow.py",
    "engine/commands.py",
    "engine/risk.py",
    "engine/gateway.py",
    "engine/state.py",
)


def _module_file(name: str) -> Path | None:
    parts = name.split(".")
    file_path = ROOT.joinpath(*parts).with_suffix(".py")
    package = ROOT.joinpath(*parts) / "__init__.py"
    if file_path.is_file():
        return file_path
    if package.is_file():
        return package
    return None


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = path.relative_to(ROOT).with_suffix("").parts
    parent = package[:-1]
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.module:
                base = parent[: len(parent) - (node.level - 1)]
                found.append(".".join((*base, node.module)))
            elif node.level:
                found.append(".".join(parent[: len(parent) - (node.level - 1)]))
            elif node.module:
                found.append(node.module)
    return found


def reachable_from_order_path() -> set[str]:
    seen: set[Path] = set()
    queue = [ROOT / relative for relative in ENTRIES]
    modules: set[str] = set()
    while queue:
        path = queue.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        relative = path.relative_to(ROOT).as_posix()
        modules.add(relative)
        for name in _imports(path):
            modules.add(name)
            nxt = _module_file(name)
            if nxt is not None and ROOT in nxt.parents:
                queue.append(nxt)
    return modules


def test_research_clients_are_not_reachable_from_the_order_path() -> None:
    reached = reachable_from_order_path()
    offenders = sorted(
        name for name in reached
        if name == "learning" or name.startswith("learning/") or name.startswith("learning.")
        or name.startswith("engine/research") or name.startswith("engine.research")
        or "dexter_client" in name or "infoway_client" in name
    )
    assert offenders == []
    assert any(name.startswith("engine/") or name.startswith("engine.") for name in reached)
