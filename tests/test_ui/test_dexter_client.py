"""Dexter client without a network. The runner is injected."""

from __future__ import annotations

import ast
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from config_ext_loader import DexterSettings
from ui.services.dexter_client import (
    HF_SECRET_NAMES,
    DexterReport,
    neutralise,
    research,
    subprocess_env,
)

ROOT = Path(__file__).resolve().parents[2]
FIXED = datetime(2026, 1, 2, tzinfo=timezone.utc)


def _settings() -> DexterSettings:
    return DexterSettings(daily_cap=2, timeout_s=1.5, max_iterations=2)


def _body(**overrides: object) -> bytes:
    payload = {
        "symbol": "AAPL",
        "asset_class": "equity",
        "status": "ok",
        "summary": "plain text",
        "metrics": {"revenue": 1.0},
        "sources": ["financialdatasets.ai"],
        "as_of": "2026-01-02T00:00:00Z",
        "tool_calls": 1,
        "model": "test",
    }
    payload.update(overrides)
    return (json.dumps(payload) + "\n").encode()


def _ok(stdout: bytes):
    def _run(argv: list[str], env: dict[str, str], timeout: float, cwd: Path):
        assert env.get("INFOWAY_API_KEY") is None
        return SimpleNamespace(stdout=stdout)

    return _run


def test_schema_accepts_one_object_and_rejects_extra_fields(tmp_path: Path) -> None:
    accepted = research(
        "AAPL",
        "equity",
        runner=_ok(_body()),
        data_key_present=True,
        cache_root=tmp_path / "cache",
        budget_path=tmp_path / "budget.json",
        moment=FIXED,
        settings=_settings(),
        parent_env={"PATH": "C:\\Windows"},
    )
    assert accepted.status == "ok"
    assert accepted.tool_calls == 1
    rejected = research(
        "MSFT",
        "equity",
        runner=_ok(_body(symbol="MSFT", leaked="nope")),
        data_key_present=True,
        cache_root=tmp_path / "cache",
        budget_path=tmp_path / "budget.json",
        moment=FIXED,
        settings=_settings(),
        parent_env={"PATH": "C:\\Windows"},
    )
    assert rejected.status == "error"


def test_injection_text_is_neutralised() -> None:
    cleaned = neutralise("hello <script>alert(1)</script>\x00`bold` *star*")
    assert "<" not in cleaned
    assert "`" not in cleaned
    assert "*" not in cleaned
    assert "\x00" not in cleaned
    assert "alert(1)" in cleaned


def test_timeout_cap_cache_and_forex(tmp_path: Path) -> None:
    calls = {"n": 0}

    def _run(argv: list[str], env: dict[str, str], timeout: float, cwd: Path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
        return SimpleNamespace(stdout=_body(symbol=argv[2]))

    timed = research(
        "AAPL",
        "equity",
        runner=_run,
        data_key_present=True,
        cache_root=tmp_path / "cache",
        budget_path=tmp_path / "budget.json",
        moment=FIXED,
        settings=_settings(),
        parent_env={},
    )
    assert timed.status == "error"
    assert timed.summary == "timeout"

    first = research(
        "MSFT",
        "stock",
        runner=_run,
        data_key_present=True,
        cache_root=tmp_path / "cache",
        budget_path=tmp_path / "budget.json",
        moment=FIXED,
        settings=_settings(),
        parent_env={},
    )
    second = research(
        "MSFT",
        "stock",
        runner=_run,
        data_key_present=True,
        cache_root=tmp_path / "cache",
        budget_path=tmp_path / "budget.json",
        moment=FIXED,
        settings=_settings(),
        parent_env={},
    )
    assert first.status == "ok"
    assert second.status == "ok"
    assert calls["n"] == 2

    capped = research(
        "NVDA",
        "equity",
        runner=_run,
        data_key_present=True,
        cache_root=tmp_path / "cache-cap",
        budget_path=tmp_path / "budget.json",
        moment=FIXED,
        settings=DexterSettings(daily_cap=1, timeout_s=1, max_iterations=1),
        parent_env={},
    )
    assert capped.status == "error"
    assert capped.summary == "daily cap reached"
    assert calls["n"] == 2

    def _forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("forex must not start a process")

    forex = research(
        "EURUSD",
        "forex",
        runner=_forbidden,
        data_key_present=True,
        cache_root=tmp_path / "cache",
        budget_path=tmp_path / "other.json",
        moment=FIXED,
        settings=_settings(),
        parent_env={},
    )
    assert forex.status == "na"


def test_fixture_mode_does_not_spawn(tmp_path: Path) -> None:
    def _forbidden(*_args: object, **_kwargs: object):
        raise AssertionError("fixture mode must not start a process")

    report = research(
        "AAPL",
        "equity",
        runner=_forbidden,
        data_key_present=False,
        cache_root=tmp_path / "cache",
        budget_path=tmp_path / "budget.json",
        fixture_path=ROOT / "tests" / "fixtures" / "dexter" / "sample.json",
        moment=FIXED,
        settings=_settings(),
        parent_env={"FINANCIAL_DATASETS_API_KEY": "not-a-secret-datasets"},
    )
    assert report.status == "ok"
    assert report.model == "fixture"
    assert "not-a-secret-datasets" not in report.model_dump_json()


def test_subprocess_env_drops_hedgefund_secrets_and_shell_is_off() -> None:
    parent = {name: f"not-a-secret-{index}" for index, name in enumerate(HF_SECRET_NAMES)}
    parent["PATH"] = "C:\\Windows"
    parent["SystemRoot"] = "C:\\Windows"
    child = subprocess_env(parent)
    blob = json.dumps(child)
    for name, value in parent.items():
        if name in {"PATH", "SystemRoot"}:
            continue
        assert name not in child
        assert value not in blob
    assert "PATH" in child
    assert child["LANGSMITH_TRACING"] == "false"
    source = (ROOT / "ui" / "services" / "dexter_client.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    shells = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg == "shell":
                    value = keyword.value
                    shells.append(isinstance(value, ast.Constant) and value.value is False)
    assert shells == [True]


def test_model_rejects_an_extra_field() -> None:
    payload = json.loads(_body())
    payload["extra"] = "no"
    try:
        DexterReport.model_validate(payload)
    except Exception as exc:
        assert "extra" in str(exc).lower() or exc.__class__.__name__ == "ValidationError"
    else:
        raise AssertionError("extra field was accepted")
