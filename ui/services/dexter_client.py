"""Shadow-only Dexter client. Failures become status=error and never raise.

The subprocess argv is a list and shell stays off. The child environment is a
short allow-list, so HedgeFund credentials are not forwarded. Forex and any
asset class without company fundamentals return status na without a process.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config_ext_loader import DexterSettings, load_config_ext

DEXTER_ROOT = Path(r"E:\tools\dexter")
CACHE_ROOT = Path(__file__).resolve().parents[2] / "data" / "dexter_cache"
BUDGET_PATH = Path(__file__).resolve().parents[2] / "data" / "dexter_budget.json"
FIXTURE_PATH = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "dexter" / "sample.json"

HF_SECRET_NAMES = (
    "MT5_LOGIN",
    "MT5_PASSWORD",
    "MT5_SERVER",
    "XAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GROQ_API_KEY",
    "TELEGRAM_BOT_TOKEN",
    "SMTP_PASSWORD",
    "AGENTROUTER_API_KEY",
    "INFOWAY_API_KEY",
    "ANCHOR_API_KEY",
    "ANCHORBROWSER_API_KEY",
    "FINANCIAL_DATASETS_API_KEY",
)
_KEEP = {
    "path",
    "systemroot",
    "windir",
    "comspec",
    "pathext",
    "temp",
    "tmp",
    "userprofile",
    "homedrive",
    "homepath",
    "appdata",
    "localappdata",
    "systemdrive",
    "programfiles",
    "programdata",
}
_FUNDAMENTALS = {"equity", "stock"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TAGS = re.compile(r"<[^>]*>")

Runner = Callable[[list[str], dict[str, str], float, Path], object]


class DexterReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(max_length=32)
    asset_class: str = Field(max_length=32)
    status: str = Field(pattern="^(ok|na|error)$")
    summary: str = Field(max_length=600)
    metrics: dict[str, float | None]
    sources: list[str]
    as_of: str = Field(max_length=40)
    tool_calls: int = Field(ge=0)
    model: str = Field(max_length=80)


def neutralise(text: str) -> str:
    """Drop control characters and markup. The model text is untrusted."""
    cleaned = _TAGS.sub("", _CONTROL.sub("", text))
    cleaned = cleaned.replace("`", "").replace("*", "")
    return cleaned[:600]


def subprocess_env(parent: Mapping[str, str]) -> dict[str, str]:
    """Environment for the child. HedgeFund secret names are never copied."""
    blocked = {name.casefold() for name in HF_SECRET_NAMES}
    env = {
        key: value
        for key, value in parent.items()
        if key.casefold() in _KEEP and key.casefold() not in blocked
    }
    env["LANGSMITH_TRACING"] = "false"
    return env


def name_is_set(path: Path, name: str) -> bool:
    """True when a dotenv name has a non-empty value. The value is not returned."""
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        body = line.strip().lstrip("\ufeff")
        if not body or body.startswith("#"):
            continue
        if body.startswith("export "):
            body = body[7:].strip()
        key, sep, raw = body.partition("=")
        if sep != "=" or key.strip() != name:
            continue
        value = raw.strip().strip("'\"")
        return bool(value)
    return False


def _now_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _report(symbol: str, asset_class: str, status: str, summary: str, model: str) -> DexterReport:
    return DexterReport(
        symbol=symbol[:32] or "UNKNOWN",
        asset_class=(asset_class or "unknown")[:32],
        status=status,
        summary=neutralise(summary) or status,
        metrics={},
        sources=[],
        as_of=_now_stamp(datetime.now(timezone.utc)),
        tool_calls=0,
        model=model[:80],
    )


def _token(symbol: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", symbol).strip("_")
    return cleaned or "symbol"


def _budget(path: Path, today: str) -> int:
    if not path.is_file():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ValueError("budget unreadable")
    if not isinstance(payload, dict) or payload.get("date") != today:
        return 0
    count = payload.get("count", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("budget unreadable")
    return count


def _charge(path: Path, today: str, cap: int) -> bool:
    count = _budget(path, today)
    if count >= cap:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"date": today, "count": count + 1}), encoding="utf-8")
    return True


def budget_today(path: Path | None = None, moment: datetime | None = None) -> tuple[int | None, int]:
    settings = load_config_ext().dexter
    today = (moment or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
    try:
        return _budget(path or BUDGET_PATH, today), settings.daily_cap
    except ValueError:
        return None, settings.daily_cap


def _cache_file(root: Path, symbol: str, moment: datetime) -> Path:
    day = moment.astimezone(timezone.utc).date().isoformat()
    return root / _token(symbol) / f"{day}.json"


def latest_cached(root: Path | None = None) -> DexterReport | None:
    base = root or CACHE_ROOT
    if not base.is_dir():
        return None
    files = sorted(base.glob("*/*.json"), key=lambda item: item.stat().st_mtime)
    if not files:
        return None
    try:
        return DexterReport.model_validate(json.loads(files[-1].read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValidationError):
        return None


def _run_process(argv: list[str], env: dict[str, str], timeout: float, cwd: Path) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        argv,
        env=env,
        timeout=timeout,
        cwd=cwd,
        shell=False,
        capture_output=True,
        check=False,
    )


def _parse_stdout(symbol: str, asset_class: str, stdout: bytes) -> DexterReport:
    lines = [line for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]
    objects = [line for line in lines if line.lstrip().startswith("{")]
    if len(objects) != 1:
        return _report(symbol, asset_class, "error", "stdout was not one JSON object", "none")
    try:
        payload = json.loads(objects[0])
    except json.JSONDecodeError:
        return _report(symbol, asset_class, "error", "stdout was not one JSON object", "none")
    if not isinstance(payload, dict):
        return _report(symbol, asset_class, "error", "response did not match the schema", "none")
    payload["summary"] = neutralise(str(payload.get("summary", "")))
    sources = payload.get("sources", [])
    if isinstance(sources, list):
        payload["sources"] = [neutralise(str(item))[:80] for item in sources[:12]]
    try:
        return DexterReport.model_validate(payload)
    except ValidationError:
        return _report(symbol, asset_class, "error", "response did not match the schema", "none")


def _fixture(symbol: str, asset_class: str, path: Path) -> DexterReport:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["symbol"] = symbol
    payload["asset_class"] = asset_class
    payload["model"] = "fixture"
    payload["summary"] = neutralise(str(payload.get("summary", "")))
    return DexterReport.model_validate(payload)


def research(
    symbol: str,
    asset_class: str,
    *,
    runner: Runner | None = None,
    parent_env: Mapping[str, str] | None = None,
    dexter_root: Path | None = None,
    cache_root: Path | None = None,
    budget_path: Path | None = None,
    fixture_path: Path | None = None,
    env_file: Path | None = None,
    data_key_present: bool | None = None,
    moment: datetime | None = None,
    settings: DexterSettings | None = None,
) -> DexterReport:
    """One symbol. Every failure is a report with status error."""
    try:
        return _research(
            symbol,
            asset_class,
            runner=runner,
            parent_env=parent_env,
            dexter_root=dexter_root,
            cache_root=cache_root,
            budget_path=budget_path,
            fixture_path=fixture_path,
            env_file=env_file,
            data_key_present=data_key_present,
            moment=moment,
            settings=settings,
        )
    except Exception:
        return _report(symbol, asset_class, "error", "request failed", "none")


def _research(
    symbol: str,
    asset_class: str,
    *,
    runner: Runner | None,
    parent_env: Mapping[str, str] | None,
    dexter_root: Path | None,
    cache_root: Path | None,
    budget_path: Path | None,
    fixture_path: Path | None,
    env_file: Path | None,
    data_key_present: bool | None,
    moment: datetime | None,
    settings: DexterSettings | None,
) -> DexterReport:
    chosen = settings or load_config_ext().dexter
    stamp = moment or datetime.now(timezone.utc)
    kind = asset_class.strip().casefold()
    if kind not in _FUNDAMENTALS:
        return _report(symbol, asset_class, "na", "no company fundamentals for this asset class", "none")
    cache = _cache_file(cache_root or CACHE_ROOT, symbol, stamp)
    if cache.is_file():
        try:
            return DexterReport.model_validate(json.loads(cache.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, ValidationError):
            return _report(symbol, asset_class, "error", "cache unreadable", "none")
    present = data_key_present
    if present is None:
        present = name_is_set(env_file or (DEXTER_ROOT / ".env"), "FINANCIAL_DATASETS_API_KEY")
    if not present:
        report = _fixture(symbol, asset_class, fixture_path or FIXTURE_PATH)
        _store(cache, report)
        return report
    budget = budget_path or BUDGET_PATH
    today = stamp.astimezone(timezone.utc).date().isoformat()
    try:
        allowed = _charge(budget, today, chosen.daily_cap)
    except ValueError:
        return _report(symbol, asset_class, "error", "budget unreadable", "none")
    if not allowed:
        report = _report(symbol, asset_class, "error", "daily cap reached", "none")
        _store(cache, report)
        return report
    timeout_ms = str(int(chosen.timeout_s * 1000))
    argv = [
        "bun",
        "hf_wrapper.ts",
        symbol,
        asset_class,
        "--iterations",
        str(chosen.max_iterations),
        "--timeout-ms",
        timeout_ms,
    ]
    launched = runner or _run_process
    try:
        completed = launched(
            argv,
            subprocess_env(parent_env if parent_env is not None else os.environ),
            chosen.timeout_s,
            dexter_root or DEXTER_ROOT,
        )
    except subprocess.TimeoutExpired:
        report = _report(symbol, asset_class, "error", "timeout", "none")
        _store(cache, report)
        return report
    except OSError:
        report = _report(symbol, asset_class, "error", "runner failed", "none")
        _store(cache, report)
        return report
    stdout = getattr(completed, "stdout", b"") or b""
    report = _parse_stdout(symbol, asset_class, stdout)
    _store(cache, report)
    return report


def _store(path: Path, report: DexterReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report.model_dump_json(), encoding="utf-8")
