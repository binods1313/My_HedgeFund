"""The journal path has exactly one source: config.yaml.

`config.yaml`'s `engine.db_path` decides which SQLite file the engine writes,
the dashboard reads, the backup routine copies and the purge script targets.
Every other mention of a journal filename in this repository is either that
config value, documentation, or a test-local temp file.

This test fails on a hardcoded journal path in **runtime code**. It exists
because the bug was real and silent: four call sites called `resolve_db_path()`
with no argument, which fell back to a hardcoded `data/engine.db` inside
`engine/journal.py`. The moment `config.yaml` was repointed at the
account-specific journal, those four readers would have kept reading the old
one -- an engine writing account A's data into a journal the dashboard was not
looking at, with nothing failing anywhere.

Run: python -m pytest tests/test_repo/test_journal_path_single_source.py -v
"""
from __future__ import annotations

import ast
import inspect
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The only filename the configured journal may carry.
CONFIGURED_NAME = "engine_5057151780.db"

#: Runtime code that must not name a journal file itself. Tests and docs are
#: excluded deliberately: a test-local `tmp_path / "engine.db"` is a fixture
#: under a throwaway directory, and a document mentioning the old filename is
#: history. Neither is a reader that can disagree with the engine.
RUNTIME_DIRS = ("engine", "ui", "scripts", "learning")

#: The only file allowed to name the journal: the config default.
ALLOWED_FILES = {"engine/config.py"}

#: A path expression resolving to the real journal, e.g. "data/engine.db",
#: Path("data") / "engine.db", or data\engine.db.
_PATH_EXPR = re.compile(
    r"""data['"]?\s*[/\\]{1,2}\s*['"]?engine\.db"""
    r"""|data['"]?\s*/\s*['"]?engine\.db""",
    re.IGNORECASE,
)

#: `Path("data") / "engine.db"` and `Path(__file__)... / "engine.db"` forms.
_SEGMENTED = re.compile(
    r"""['"]data['"]\s*/\s*['"]engine\.db['"]""",
    re.IGNORECASE,
)

#: Any bare mention of the account-specific journal outside config.yaml, which
#: would freeze the filename into a caller.
_CONFIGURED = re.compile(re.escape(CONFIGURED_NAME))


def _runtime_files():
    for directory in RUNTIME_DIRS:
        base = REPO_ROOT / directory
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix != ".py":
                continue
            if "__pycache__" in path.parts:
                continue
            yield path
    for path in sorted(REPO_ROOT.glob("*.py")):
        if path.is_file():
            yield path


def _code_strings(text: str) -> list[str]:
    """String literals that are *values*, not docstrings or comments.

    Scanning raw text flags prose that merely mentions the old filename --
    the docstrings here explaining the bug say `data/engine.db` several
    times. Parsing the AST and skipping every bare docstring expression
    leaves the literals that actually become paths.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [text]
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstring_ids.add(id(first.value))
    literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstring_ids:
                literals.append(node.value)
    return literals


def test_no_hardcoded_journal_path_in_runtime_code() -> None:
    """No runtime file may resolve to the real journal itself."""
    offenders: list[str] = []
    for path in _runtime_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        if rel in ALLOWED_FILES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for literal in _code_strings(text):
            for pattern, label in ((_PATH_EXPR, "path expression"),
                                   (_SEGMENTED, "segmented path"),
                                   (_CONFIGURED, "account-specific filename")):
                if pattern.search(literal):
                    offenders.append(f"{rel}: {label} -> {literal!r}")

    assert not offenders, (
        "a runtime file names the journal instead of using the configured "
        "path:\n  " + "\n  ".join(offenders) + "\n"
        "Use engine.journal.resolve_db_path(load_engine_config().engine.db_path)"
    )


def test_config_names_the_account_journal() -> None:
    """config.yaml must point at the account-specific journal.

    If this drifts back to the shared name the engine writes into the old
    journal and the dashboard silently disagrees with the broker.
    """
    text = (REPO_ROOT / "config.yaml").read_text(encoding="utf-8")
    assert CONFIGURED_NAME in text, f"config.yaml must configure {CONFIGURED_NAME!r}"
    assert "db_path: data/engine.db" not in text, (
        "config.yaml must not still point at the shared journal"
    )


def test_resolve_db_path_has_no_silent_default() -> None:
    """`resolve_db_path()` must require the configured path.

    The optional-argument default was the root of the whole class of bug: a
    caller that passed nothing got a hardcoded journal. It now raises.
    """
    from engine.journal import resolve_db_path

    param = inspect.signature(resolve_db_path).parameters.get("configured")
    assert param is not None and param.default is inspect.Parameter.empty, (
        "resolve_db_path() must have no default for `configured`; a silent "
        "default is how two processes read different journals"
    )
    with pytest.raises(TypeError):
        resolve_db_path(None)  # type: ignore[arg-type]


def test_the_configured_path_resolves_to_one_file() -> None:
    """One config value resolves to one absolute path."""
    from engine.config import load_engine_config
    from engine.journal import resolve_db_path

    resolved = resolve_db_path(load_engine_config().engine.db_path)
    assert resolved.is_absolute()
    assert resolved.name == CONFIGURED_NAME, (
        f"the configured journal resolved to {resolved}"
    )


def test_every_ui_reader_uses_the_one_helper() -> None:
    """No UI module may call `resolve_db_path()` without an argument.

    This is the specific shape of the original bug, asserted directly rather
    than left to the pattern match above, because a refactor could introduce
    it again with a path the patterns do not catch.
    """
    offenders: list[str] = []
    for path in sorted((REPO_ROOT / "ui").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(r"resolve_db_path\s*\(\s*\)", text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(REPO_ROOT).as_posix()}:{line}")
    assert not offenders, (
        "a UI module calls resolve_db_path() with no argument, so it would "
        "read the wrong journal:\n  " + "\n  ".join(offenders)
    )
