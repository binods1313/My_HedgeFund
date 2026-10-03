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

    return EngineConfig(
        mode=mode,
        live_confirmation_required=bool(raw.get("live_confirmation_required", True)),
        engine=EngineSettings.model_validate(engine_section),
        risk=RiskLimits.model_validate(risk_section),
        symbol_mapping={str(k).upper(): str(v) for k, v in mapping_raw.items()},
        root=root_path,
        config_path=cfg_path,
    )


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