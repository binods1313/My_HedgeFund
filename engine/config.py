"""Engine configuration.

Loads and validates config.yaml. Invalid configuration means the engine refuses
to start: a trading engine that starts on an unvalidated config is worse than
one that does not start.

Secrets never come from here. They are read through engine.keys, which sources
them from the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from engine.journal import resolve_db_path


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GatewaySettings(StrictModel):
    call_timeout_seconds: float = Field(default=10.0, gt=0, le=300)
    reconnect_backoff_seconds: list[int] = Field(
        default_factory=lambda: [1, 2, 4, 8, 16, 30])
    order_lookback_seconds: int = Field(default=120, gt=0, le=3600)
    #: How many times a None account_info() may force shutdown+reinitialize
    #: before the gateway waits on the longest backoff only.
    account_reinitialize_attempts: int = Field(default=3, gt=0, le=30)
    #: Owner-thread calls that run longer than this are journalled as a stall.
    watchdog_timeout_seconds: float = Field(default=30.0, gt=0, le=600)

    @model_validator(mode="after")
    def _backoff_increasing(self) -> "GatewaySettings":
        seq = self.reconnect_backoff_seconds
        if not seq:
            raise ValueError("gateway.reconnect_backoff_seconds must not be empty")
        if any(v <= 0 for v in seq):
            raise ValueError("gateway.reconnect_backoff_seconds must be positive")
        if seq != sorted(seq):
            raise ValueError(
                "gateway.reconnect_backoff_seconds must be non-decreasing; a "
                "decreasing backoff would retry faster under load"
            )
        return self


class EngineSettings(StrictModel):
    heartbeat_seconds: int = Field(default=5, gt=0, le=60)
    offline_after_seconds: int = Field(default=15, gt=0, le=600)
    command_poll_seconds: float = Field(default=1.0, gt=0, le=60)
    reconcile_on_start: bool = True
    db_path: str = "data/engine.db"
    backup_dir: str = "backups"
    magic_number: int = Field(default=100100, gt=0)
    gateway: GatewaySettings = Field(default_factory=GatewaySettings)

    @model_validator(mode="after")
    def _offline_after_exceeds_heartbeat(self) -> "EngineSettings":
        if self.offline_after_seconds <= self.heartbeat_seconds:
            raise ValueError(
                "engine.offline_after_seconds must exceed heartbeat_seconds; "
                "otherwise a healthy engine looks offline between beats"
            )
        return self


class RiskLimits(StrictModel):
    """Risk limits, validated now so bad values fail at startup.

    Phase 1 does not enforce these: the risk stub rejects everything. Phase 3
    implements the behaviour, using these same keys so the values are already
    checked rather than trusted then.
    """

    risk_per_trade_pct: float = Field(default=1.0, gt=0, le=100)
    daily_loss_limit_pct: float = Field(default=3.0, gt=0, le=100)
    max_drawdown_pct: float = Field(default=10.0, gt=0, le=100)
    max_open_trades: int = Field(default=5, gt=0, le=100)
    max_total_exposure_pct: float = Field(default=10.0, gt=0, le=100)
    default_max_spread_points: int = Field(default=30, gt=0)

    @model_validator(mode="after")
    def _per_trade_within_total(self) -> "RiskLimits":
        if self.risk_per_trade_pct > self.max_total_exposure_pct:
            raise ValueError(
                "risk.risk_per_trade_pct must not exceed "
                "risk.max_total_exposure_pct; a per-trade limit above the total "
                "would permit a single order to breach the portfolio limit"
            )
        return self


class SymbolVolumeConfig(StrictModel):
    """Per-symbol volume cap override.

    ``volume_cap`` is an absolute ceiling (in standard lots) for any single
    order-sized signal the scanner writes. If absent, the engine falls back
    to ``volume_min * volume_cap_default_multiplier`` computed at runtime from
    the broker's live symbol spec. A cap below ``volume_min`` is a config
    error and fails at startup rather than silently admitting an
    untradeable ceiling at runtime.
    """

    volume_cap: float = Field(gt=0)


@dataclass
class EngineConfig:
    """Validated engine configuration with secrets left out."""

    mode: str
    live_confirmation_required: bool
    engine: EngineSettings
    risk: RiskLimits
    symbol_mapping: dict[str, str] = field(default_factory=dict)
    root: Path = field(default_factory=Path.cwd)
    config_path: Path | None = None
    #: Broker symbols the stage-1 scan reads. Empty means the cycle skips.
    scan_symbols: list[str] = field(default_factory=list)
    max_scan_seconds: float = 10.0
    execution_mode: str = "approve_first"
    ai_signals_enabled: bool = False
    #: Per-symbol volume caps from ``trading.symbols.volumes``.
    volume_caps: dict[str, float] = field(default_factory=dict)
    #: Multiplier used when a symbol has no explicit ``volume_cap``.
    volume_cap_default_multiplier: int = 10
    #: Observatory Kronos settings (None = disabled). Never a trade gate.
    kronos: Any = None

    @property
    def db_path(self) -> Path:
        """Absolute, anchored to the project root.

        The UI and the engine may run with different working directories. A
        relative path would silently open two different databases.
        """
        return resolve_db_path(self.engine.db_path)

    @property
    def backup_dir(self) -> Path:
        candidate = Path(self.engine.backup_dir)
        if candidate.is_absolute():
            return candidate
        return self.root / candidate

    @property
    def is_live(self) -> bool:
        return self.mode == "LIVE"

    def resolve_symbol(self, logical: str) -> str | None:
        """Map a logical symbol to the broker's name, if configured."""
        return self.symbol_mapping.get(logical.upper())

    def unresolved_symbols(self, required: list[str]) -> list[str]:
        """Required logical symbols with no broker mapping."""
        return [s for s in required if not self.resolve_symbol(s)]


MODE_PAPER = "PAPER"
MODE_LIVE = "LIVE"
#: The only execution mode this build implements. Auto execution is refused
#: at config load. There is no code path that places an order because this
#: value is set.
EXECUTION_MODE_APPROVE_FIRST = "approve_first"


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml
    if not path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {path}. The engine refuses to start "
            f"without a validated configuration."
        )
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return raw


def load_engine_config(root: Path | str | None = None,
                       config_path: Path | str | None = None) -> EngineConfig:
    """Load and validate the engine section of config.yaml.

    Raises FileNotFoundError, ValueError or pydantic.ValidationError on bad
    input. Callers let that propagate; aborting startup is the intended
    behaviour.
    """
    root_path = Path(root) if root else Path(__file__).resolve().parent.parent
    cfg_path = Path(config_path) if config_path else root_path / "config.yaml"

    raw = _load_yaml(cfg_path)

    mode = str(raw.get("mode", MODE_PAPER)).upper()
    if mode not in (MODE_PAPER, MODE_LIVE):
        raise ValueError(
            f"config mode {mode!r} is invalid; expected {MODE_PAPER} or {MODE_LIVE}. "
            f"PAPER is the default and the safe choice."
        )

    engine_section = raw.get("engine", {})
    risk_section = raw.get("risk", {})
    symbols_section = raw.get("symbols", {}) or {}
    mapping_raw = symbols_section.get("mapping", {}) or {}
    if not isinstance(mapping_raw, dict):
        raise ValueError("symbols.mapping must be a mapping of logical -> broker")

    scanner_raw = raw.get("scanner") or {}
    if not isinstance(scanner_raw, dict):
        raise ValueError("scanner must be a mapping")
    max_scan_raw = scanner_raw.get("max_scan_seconds", 10)
    try:
        max_scan_seconds = float(max_scan_raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("scanner.max_scan_seconds must be a number") from exc
    if max_scan_seconds <= 0:
        raise ValueError("scanner.max_scan_seconds must be positive")

    execution_mode = _parse_execution_mode(raw.get("execution_mode"))

    kronos = None
    kronos_raw = raw.get("kronos")
    if isinstance(kronos_raw, dict):
        from config_loader import KronosSettings
        kronos = KronosSettings.model_validate(kronos_raw)

    return EngineConfig(
        mode=mode,
        live_confirmation_required=bool(raw.get("live_confirmation_required", True)),
        engine=EngineSettings.model_validate(engine_section),
        risk=RiskLimits.model_validate(risk_section),
         symbol_mapping={str(k).upper(): str(v) for k, v in mapping_raw.items()},
        root=root_path,
        config_path=cfg_path,
        scan_symbols=_flatten_whitelist(symbols_section.get("whitelist")),
        max_scan_seconds=max_scan_seconds,
        execution_mode=execution_mode,
        ai_signals_enabled=bool(scanner_raw.get(
            "ai_signals_enabled", raw.get("ai_signals_enabled", False))),
        volume_caps=_parse_volume_caps(symbols_section),
        volume_cap_default_multiplier=int(
            symbols_section.get("volume_cap_default_multiplier", 10)
        ),
        kronos=kronos,
    )


def _parse_execution_mode(value: Any) -> str:
    """Accept only approve_first. Missing means that default.

    ``auto_execute`` and any other string are a configuration error. This
    function does not start trading.
    """
    if value is None or str(value).strip() == "":
        return EXECUTION_MODE_APPROVE_FIRST
    text = str(value).strip().lower()
    if text != EXECUTION_MODE_APPROVE_FIRST:
        raise ValueError(
            f"execution_mode {text!r} is not valid. The only implemented "
            f"mode is {EXECUTION_MODE_APPROVE_FIRST!r}. Auto execution is "
            f"not implemented."
        )
    return text


def _flatten_whitelist(whitelist: Any) -> list[str]:
    """Broker names from ``symbols.whitelist``, in file order.

    A class whose value is an empty list contributes nothing. Non-strings
    are ignored so a comment-only placeholder cannot become a symbol.
    """
    if not isinstance(whitelist, dict):
        return []
    names: list[str] = []
    for value in whitelist.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
    return names


def _parse_volume_caps(symbols_section: Any) -> dict[str, float]:
    """Read per-symbol volume caps from ``symbols.volumes``.

    Returns an upper-case keyed mapping of symbol -> volume_cap (float).
    A non-positive or non-numeric value is a config error. The runtime
    fall-back (volume_min * multiplier) is applied in LotSizer when a
    symbol is absent from this dict.
    """
    volumes_raw = (symbols_section.get("volumes") or {}) if symbols_section else {}
    caps: dict[str, float] = {}
    for key, val in volumes_raw.items():
        if not isinstance(key, str):
            continue
        try:
            cap = float(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"symbols.volumes.{key} must be a positive number, got {val!r}"
            ) from exc
        if cap <= 0:
            raise ValueError(
                f"symbols.volumes.{key} must be > 0, got {cap}"
            )
        caps[str(key).upper()] = cap
    return caps


def read_mt5_credentials() -> dict[str, Any]:
    """MT5 login details from the environment.

    Separate from the keyring because these are terminal credentials rather than
    provider keys, and they are only read by the gateway constructor.
    """
    from dotenv import load_dotenv

    load_dotenv()

    login_raw = os.environ.get("MT5_LOGIN", "")
    login: int | None = None
    if login_raw and not login_raw.startswith("YOUR_"):
        try:
            login = int(login_raw)
        except ValueError:
            raise ValueError(
                f"MT5_LOGIN must be numeric, got {login_raw[:3]}..."
            ) from None

    return {
        "login": login,
        "password": os.environ.get("MT5_PASSWORD", ""),
        "server": os.environ.get("MT5_SERVER", ""),
        "terminal_path": os.environ.get("MT5_PATH", ""),
    }