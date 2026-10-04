"""Architectural boundary tests.

Two rules the design doc states as invariants:

  1. `engine/gateway.py` is the ONLY engine module permitted to import
     MetaTrader5.
  2. No module outside `engine/keys.py` may read os.environ for a secret.

These are enforced by inspecting source rather than trusted to review, because
a single stray import reintroduces the bug and no functional test would notice.

Run: python -m pytest tests/test_engine/test_boundaries.py -v
"""
import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ENGINE_DIR = REPO_ROOT / "engine"

# gateway.py imports MetaTrader5 LAZILY inside a method, so a module-level
# import check would miss it. This is the one file allowed to reference it.
MT5_OWNER = "gateway.py"

# Modules permitted to read the environment for a credential. keys.py routes
# them; config.py reads MT5 terminal credentials, which are not provider keys
# and are only used to construct the gateway.
CREDENTIAL_READERS = {"keys.py", "config.py"}

SECRET_WORDS = (
    "PASSWORD", "API_KEY", "APIKEY", "SECRET", "TOKEN", "ACCESS_KEY",
)


def engine_modules() -> list[Path]:
    return sorted(p for p in ENGINE_DIR.glob("*.py")
                  if p.name != "__init__.py")


def _environment_reads(module: Path) -> list[str]:
    """Lines that actually read the environment.

    Uses the AST rather than a text scan. A text scan cannot distinguish code
    from documentation: this module's own docstring contains the phrase
    'never from os.environ', which a substring match reports as a violation of
    the rule the docstring states.

    A token-based approach was tried first and was worse: it silently dropped
    real reads, which would have made this test decorative.
    """
    import ast

    source = module.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [f"{module.name}: does not parse"]

    # Line ranges of every docstring, so prose can be excluded precisely.
    prose_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc and node.body:
                first = node.body[0]
                end = getattr(first, "end_lineno", first.lineno)
                prose_lines.update(range(first.lineno, end + 1))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        # `os.environ` parses as Attribute(attr='environ', value=Name('os')).
        # `os.environ.get(...)` nests that inside Attribute(attr='get').
        # Both are reads, so flag the chain ending in 'environ' at any depth.
        if _attribute_chain(node) in ("os.environ", "environ"):
            if node.lineno not in prose_lines:
                offenders.append(f"{node.lineno}: {_attribute_chain(node)}")
        # A bare `environ` imported from somewhere, used by subscript.
        if node.attr == "__getitem__":
            target = _attribute_chain(node.value)
            if target in ("os.environ", "environ") and \
                    node.lineno not in prose_lines:
                offenders.append(f"{node.lineno}: {target}[...]")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "getenv" and node.lineno not in prose_lines:
                offenders.append(f"{node.lineno}: getenv()")

    return sorted(set(offenders))


def _attribute_chain(node) -> str:
    """Rebuild a dotted attribute expression, e.g. os.environ -> 'os.environ'."""
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def imported_modules(path: Path) -> set[str]:
    """Top-level module names imported by a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if getattr(node, "level", 0) == 0 and node.module:
                names.add(node.module.split(".")[0])
    return names


class TestOnlyTheGatewayTouchesMt5:

    @pytest.mark.parametrize("module", engine_modules(),
                             ids=lambda p: p.name)
    def test_engine_module_does_not_import_mt5(self, module):
        if module.name == MT5_OWNER:
            pytest.skip("gateway.py owns the MT5 connection by design")
        assert "MetaTrader5" not in imported_modules(module), (
            f"{module.name} imports MetaTrader5. Only gateway.py may touch the "
            f"MT5 API; everything else goes through the gateway queue."
        )

    def test_gateway_does_reference_mt5(self):
        """Guard against the allowance becoming a loophole."""
        source = (ENGINE_DIR / "gateway.py").read_text(encoding="utf-8")
        assert "MetaTrader5" in source, (
            "gateway.py is supposed to be the module that imports MetaTrader5"
        )

    def test_only_gateway_mentions_order_send(self):
        """No other engine module may submit an order."""
        offenders = []
        for module in engine_modules():
            if module.name == MT5_OWNER:
                continue
            source = module.read_text(encoding="utf-8")
            if ".order_send" in source:
                offenders.append(module.name)
        assert not offenders, (
            f"order_send is only permitted in gateway.py; found in {offenders}"
        )

    def test_no_engine_module_creates_a_gateway_subclass(self):
        """Bypassing the queue would defeat serialisation."""
        for module in engine_modules():
            if module.name == MT5_OWNER:
                continue
            source = module.read_text(encoding="utf-8")
            assert "class .*Gateway" not in source, (
                f"{module.name} appears to define a Gateway subclass"
            )


class TestUiIsDecoupledFromMt5:
    """Phase 1 deliverable: remove the MT5 import from app.py.

    Recorded as a failing-shaped test so it is visible until it is done, rather
    than being quietly omitted.
    """

    def test_app_py_target_state(self):
        app = REPO_ROOT / "app.py"
        if not app.exists():
            pytest.skip("app.py not present")
        imported = imported_modules(app)
        assert "MetaTrader5" not in imported, (
            "app.py still imports MetaTrader5. Phase 1 deliverable: move the "
            "TIMEFRAME constants into data_engine so the UI reads them from the "
            "DB and never touches the terminal."
        )

    def test_app_py_has_no_order_send(self):
        app = REPO_ROOT / "app.py"
        if not app.exists():
            pytest.skip("app.py not present")
        source = app.read_text(encoding="utf-8")
        assert ".order_send" not in source, (
            "the UI must never submit an order; commands go through the engine"
        )


class TestCredentialAccessIsCentralised:

    @pytest.mark.parametrize("module", engine_modules(),
                             ids=lambda p: p.name)
    def test_only_keys_and_config_touch_the_environment(self, module):
        """No module outside the allow-list may read os.environ.

        Docstrings and comments are stripped first: prose saying 'never from
        os.environ' is documentation, not a read, and treating it as one makes
        this test fail for correct code.
        """
        if module.name in CREDENTIAL_READERS:
            pytest.skip(f"{module.name} is an approved credential reader")
        offenders = _environment_reads(module)
        assert not offenders, (
            f"{module.name} reads the environment directly; route secrets "
            f"through engine.keys.ProviderKeyring. Offenders: {offenders}"
        )

    def test_keys_module_provides_the_interface(self):
        from engine.keys import Keyring, PROVIDER_ENV_VARS
        assert "xai" in PROVIDER_ENV_VARS
        assert PROVIDER_ENV_VARS["xai"] == "XAI_API_KEY"
        assert callable(Keyring.require)

    def test_keys_module_is_the_only_one_naming_xai(self):
        others = [m.name for m in engine_modules()
                  if m.name != "keys.py"
                  and "XAI_API_KEY" in m.read_text(encoding="utf-8")]
        assert not others, (
            f"XAI_API_KEY referenced outside keys.py in {others}"
        )


class TestKeyringRedaction:
    """A key must never appear in a log, a repr or a traceback."""

    # Reuses a canary already registered in scripts.secret_scan.DECLARED_CANARIES.
    # It is safe here because this file is listed in SELF_REFERENTIAL_FILES: the
    # scanner skips its own canary sources rather than treating every
    # credential-shaped string as safe everywhere, which would defeat it.
    SYNTHETIC = "sk-or-v1-AAAABBBBCCCCDDDDEEEEFFFF"

    def _ring(self, value=None):
        from engine.keys import Keyring
        v = value if value is not None else self.SYNTHETIC
        return Keyring(env={"XAI_API_KEY": v, "OPENROUTER_API_KEY": v})

    def test_this_file_is_an_exempt_canary_source(self):
        """Guard the fixture's exemption, so it cannot drift."""
        import scripts.secret_scan as ss
        rel = "tests/test_engine/test_boundaries.py"
        assert rel in ss.SELF_REFERENTIAL_FILES, (
            f"{rel} must be listed as a canary source, or the committed "
            f"canary will be reported as a live credential"
        )

    def test_synthetic_value_is_a_registered_canary(self):
        import scripts.secret_scan as ss
        assert self.SYNTHETIC in ss.DECLARED_CANARIES

    def test_repr_hides_the_key(self):
        ring = self._ring()
        assert self.SYNTHETIC not in repr(ring)
        assert "REDACTED" in repr(ring)

    def test_str_hides_the_key(self):
        ring = self._ring()
        assert self.SYNTHETIC not in str(ring)

    def test_describe_hides_the_key(self):
        ring = self._ring()
        for line in ring.describe_all():
            assert self.SYNTHETIC not in line

    def test_describe_reports_length_only(self):
        ring = self._ring("xai-" + "A" * 40)
        described = ring.describe("xai")
        assert "REDACTED" in described
        assert "len=44" in described

    def test_require_returns_the_actual_value(self):
        ring = self._ring("xai-" + "B" * 40)
        assert ring.require("xai").startswith("xai-")

    def test_missing_key_raises_rather_than_returning_empty(self):
        from engine.keys import Keyring, KeyNotConfigured
        ring = Keyring(env={})
        with pytest.raises(KeyNotConfigured) as excinfo:
            ring.require("xai")
        assert "XAI_API_KEY" in str(excinfo.value)

    def test_placeholder_is_not_treated_as_configured(self):
        from engine.keys import Keyring
        ring = Keyring(env={"XAI_API_KEY": "YOUR_XAI_KEY"})
        assert ring.is_configured("xai") is False

    def test_blank_key_is_not_configured(self):
        from engine.keys import Keyring
        ring = Keyring(env={"XAI_API_KEY": "   "})
        assert ring.is_configured("xai") is False

    def test_exception_message_never_contains_any_value(self):
        """A raised error must not echo whatever was configured."""
        from engine.keys import Keyring, KeyNotConfigured
        ring = Keyring(env={"XAI_API_KEY": "   "})
        try:
            ring.require("xai")
        except KeyNotConfigured as exc:
            assert "sk-" not in str(exc).lower().replace("xai_api_key", "")

    def test_unknown_provider_raises(self):
        from engine.keys import Keyring
        ring = self._ring()
        with pytest.raises(KeyError):
            ring.require("not_a_provider")

    def test_missing_reports_unconfigured_providers(self):
        from engine.keys import Keyring
        ring = Keyring(env={"XAI_API_KEY": "xai-REAL"})
        assert "xai" not in ring.missing()
        assert "openrouter" in ring.missing()


class TestNoSecretsInEngineSource:
    """Belt and braces: the scanner already checks, this names the rule."""

    @pytest.mark.parametrize("module", engine_modules(),
                             ids=lambda p: p.name)
    def test_no_literal_credential_shapes(self, module):
        import re
        source = module.read_text(encoding="utf-8")
        for pattern in (r"sk-or-v1-[A-Za-z0-9]{16,}",
                        r"gh[pousr]_[A-Za-z0-9]{30,}",
                        r"AKIA[0-9A-Z]{16}"):
            matches = re.findall(pattern, source)
            assert not matches, (
                f"{module.name} contains a credential-shaped literal: {matches}"
            )