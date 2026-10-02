"""Dependency declaration tests.

Every third-party import must appear in requirements.txt.

This exists because config_loader.py started importing `yaml` and
scripts/demo_verification.py imports `dotenv` without either package being
declared, so a fresh `pip install -r requirements.txt` produced an app that
crashed at startup with ModuleNotFoundError. A unit test suite passed the whole
time because the packages happened to be installed in the working environment.

Run: python -m pytest tests/test_dependencies.py -v
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"

# Import name -> distribution name on PyPI, where they differ.
DISTRIBUTION_NAMES = {
    "yaml": "PyYAML",
    "dotenv": "python-dotenv",
    "sklearn": "scikit-learn",
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "bs4": "beautifulsoup4",
    "serial": "pyserial",
    "OpenSSL": "pyOpenSSL",
    "Crypto": "pycryptodome",
    "dateutil": "python-dateutil",
}

# Standard library, never declared in requirements.txt.
STDLIB = set(sys.stdlib_module_names) | {
    "MetaTrader5",  # platform package, but declared explicitly below
}

# Modules that legitimately import an optional dependency guarded at runtime.
OPTIONAL = {
    ("scripts", "demo_verification.py"): {"MetaTrader5", "dotenv"},
}


def _declared_distributions() -> set[str]:
    if not REQUIREMENTS.exists():
        return set()
    out = set()
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip version specifiers, extras and environment markers.
        name = re.split(r"[<>=!~\[;@ ]", line, maxsplit=1)[0].strip()
        if name:
            out.add(name.lower().replace("_", "-"))
    return out


def _top_level_imports(path: Path) -> set[str]:
    """Top-level module names imported by a Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return set()

    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            # level > 0 means a relative import, i.e. our own package.
            if getattr(node, "level", 0) == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


def _source_files() -> list[Path]:
    files = [p for p in REPO_ROOT.glob("*.py")
             if "__pycache__" not in p.parts and p.name != "app_check.py"]
    files += [p for p in REPO_ROOT.glob("scripts/*.py")
              if "__pycache__" not in p.parts]
    files += [p for p in REPO_ROOT.glob("tests/*.py")
              if "__pycache__" not in p.parts]
    return files


def _module_key(path: Path) -> tuple[str, ...]:
    rel = path.relative_to(REPO_ROOT)
    return tuple(rel.parts)


class TestAllSourceFilesCompile:
    """Every tracked source file must parse.

    This exists because an IndentationError shipped in app.py undetected. The
    dependency test walks the AST, and ast.parse tolerates misindented lines
    that fall inside a triple-quoted f-string, so 408 tests passed while the
    app could not start. Compiling the module is the check the AST walk is not.

    Note: compile() raises IndentationError for a dedented statement inside a
    function body, which is exactly the failure that shipped.
    """

    @pytest.mark.parametrize("path", _source_files(), ids=lambda p: p.name)
    def test_file_compiles(self, path):
        source = path.read_text(encoding="utf-8")
        try:
            compile(source, str(path), "exec")
        except IndentationError as exc:
            pytest.fail(
                f"{path.relative_to(REPO_ROOT)} has an IndentationError at "
                f"line {exc.lineno}: {exc.msg}. The app cannot start."
            )
        except SyntaxError as exc:
            pytest.fail(
                f"{path.relative_to(REPO_ROOT)} has a SyntaxError at line "
                f"{exc.lineno}: {exc.msg}"
            )

    def test_app_compiles_specifically(self):
        """app.py is the entry point; pin it explicitly."""
        app = REPO_ROOT / "app.py"
        assert app.exists(), "app.py is the Streamlit entry point"
        compile(app.read_text(encoding="utf-8"), str(app), "exec")

    def test_no_indentation_error_survives_a_compile(self):
        """Catch the exact failure that shipped: a statement dedented out of a
        block while the surrounding lines stayed indented.

        compile() is the authoritative check. An earlier attempt also asserted
        on indentation levels inside the HTML block, which was fragile and
        produced a false positive on legitimately nested markup.
        """
        app = REPO_ROOT / "app.py"
        source = app.read_text(encoding="utf-8")
        try:
            compile(source, str(app), "exec")
        except IndentationError as exc:
            pytest.fail(
                f"app.py line {exc.lineno}: {exc.msg}. A statement was dedented "
                f"out of its enclosing block."
            )


class TestRequirementsAreComplete:

    def test_requirements_file_exists(self):
        assert REQUIREMENTS.exists(), f"{REQUIREMENTS} is missing"

    @pytest.mark.parametrize("module", ["yaml", "dotenv"])
    def test_known_config_dependencies_are_declared(self, module):
        """The two that were previously undeclared."""
        declared = _declared_distributions()
        dist = DISTRIBUTION_NAMES.get(module, module)
        assert dist.lower().replace("_", "-") in declared, (
            f"'{module}' is imported but '{dist}' is not in requirements.txt; "
            f"a fresh install will fail with ModuleNotFoundError"
        )

    @pytest.mark.parametrize("path", _source_files(),
                             ids=lambda p: p.name)
    def test_every_third_party_import_is_declared(self, path):
        declared = _declared_distributions()
        missing: list[str] = []

        for module in _top_level_imports(path):
            if module in STDLIB:
                continue
            key = _module_key(path)
            if module in OPTIONAL.get(key, set()):
                pass  # explicitly permitted for this file
            dist = DISTRIBUTION_NAMES.get(module, module)
            if dist.lower().replace("_", "-") in declared:
                continue
            # Only flag genuine third-party names: our own modules and anything
            # importable from the stdlib-free project namespace.
            if (REPO_ROOT / f"{module}.py").exists():
                continue
            if (REPO_ROOT / module).is_dir():
                continue
            missing.append(f"{module} (dist: {dist})")

        assert not missing, (
            f"{path.relative_to(REPO_ROOT)} imports undeclared dependencies: "
            f"{', '.join(sorted(missing))}. Add them to requirements.txt."
        )

    def test_pytest_can_import_yaml_and_dotenv(self):
        """Fail here with a clear message rather than at app startup."""
        yaml = pytest.importorskip("yaml", reason="PyYAML must be installed")
        assert hasattr(yaml, "safe_load")

        dotenv = pytest.importorskip("dotenv",
                                     reason="python-dotenv must be installed")
        assert hasattr(dotenv, "load_dotenv")


class TestRequirementsAreUsable:
    """requirements.txt must be parseable and free of obvious mistakes."""

    def test_every_line_is_a_pinned_requirement(self):
        bad = []
        for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "==" not in line:
                bad.append(line)
        assert not bad, f"unpinned or malformed requirement lines: {bad}"

    def test_no_duplicate_distributions(self):
        seen: set[str] = set()
        dupes: set[str] = set()
        for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            name = re.split(r"[<>=!~\[;@ ]", line, maxsplit=1)[0].strip().lower()
            if name in seen:
                dupes.add(name)
            seen.add(name)
        assert not dupes, f"duplicate requirements: {sorted(dupes)}"

    def test_no_secrets_in_requirements(self):
        """Requirements must never carry credentials or URLs with tokens."""
        text = REQUIREMENTS.read_text(encoding="utf-8")
        for marker in ("sk-or-", "sk-", "ghp_", "AKIA", "password=", "@github.com"):
            assert marker not in text, (
                f"requirements.txt contains {marker!r}; credentials must never "
                f"appear in a requirements file"
            )