"""UI and learning do not import MetaTrader5 or read secret env vars themselves."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SECRET_NAMES = {
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "MT5_PATH",
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "FINANCIAL_DATASETS_API_KEY",
    "INFOWAY_API_KEY",
    "ANCHOR_API_KEY",
    "ANCHORBROWSER_API_KEY",
    "AGENTROUTER_API_KEY",
}


def _python_files(folder: str) -> list[Path]:
    return sorted(path for path in (ROOT / folder).rglob("*.py") if path.is_file())


def _imported(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def _secret_getenv(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_getenv = isinstance(func, ast.Attribute) and func.attr in {"get", "getenv"}
        is_name = isinstance(func, ast.Name) and func.id == "getenv"
        if not is_getenv and not is_name:
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value in SECRET_NAMES:
            hits.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno}:{node.args[0].value}")
    return hits


def test_ui_and_learning_do_not_import_metatrader5() -> None:
    offenders: list[str] = []
    for folder in ("ui", "learning"):
        for path in _python_files(folder):
            for name in _imported(path):
                if name == "MetaTrader5" or name.startswith("MetaTrader5."):
                    offenders.append(f"{path.relative_to(ROOT).as_posix()}:{name}")
    assert offenders == []


def test_ui_and_learning_do_not_getenv_secrets() -> None:
    offenders: list[str] = []
    for folder in ("ui", "learning"):
        for path in _python_files(folder):
            offenders.extend(_secret_getenv(path))
    assert offenders == []
