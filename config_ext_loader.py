"""Strict loader for config_ext.yaml. Secrets never belong in this file."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_CONFIG_EXT_PATH = Path(__file__).with_name("config_ext.yaml")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChartSettings(StrictModel):
    atr_k: float = Field(default=1.5, gt=0, le=10)
    rr: float = Field(default=3.0, ge=1, le=10)


class InfowaySettings(StrictModel):
    """Read caps for the research quote client. No secrets belong here."""

    daily_cap: int = Field(default=200, ge=0, le=86400)
    timeout_s: float = Field(default=5.0, gt=0, le=30)
    cache_ttl_s: float = Field(default=30.0, ge=0, le=3600)


class DexterSettings(StrictModel):
    """Caps for the shadow research process. No secrets belong here."""

    daily_cap: int = Field(default=20, ge=0, le=200)
    timeout_s: float = Field(default=90.0, gt=0, le=180)
    max_iterations: int = Field(default=6, ge=1, le=20)


class LearningSettings(StrictModel):
    """Shadow ledger settings. Proposals never apply themselves."""

    timeframe: str = Field(default="H1", pattern="^(M15|H1|H4|D1)$")
    horizons: list[int] = Field(default_factory=lambda: [4, 12, 24])
    atr_period: int = Field(default=14, ge=2, le=100)
    min_samples: int = Field(default=30, ge=1, le=10000)
    max_change: float = Field(default=0.10, gt=0, le=1)
    shrinkage: float = Field(default=0.5, gt=0, le=1)
    drift_window: int = Field(default=30, ge=5, le=1000)

    def model_post_init(self, _context: object) -> None:
        if not self.horizons:
            raise ValueError("horizons must not be empty")
        if any(isinstance(item, bool) or item < 1 or item > 500 for item in self.horizons):
            raise ValueError("horizons must be integers from 1 to 500")
        if len(set(self.horizons)) != len(self.horizons):
            raise ValueError("horizons must be unique")


class ShadowSettings(StrictModel):
    """Parsed from ``config_ext.yaml`` and currently inert.

    ``shadow.enabled`` is loaded and validated. Nothing in the shadow loop
    reads it. ``learning.runtime.ensure_thread`` starts a cycle only when
    the Research Desk toggle writes ``data/shadow_mode.on``, and that loop
    calls ``run_cycle(enabled=True)`` directly. Changing this flag does not
    start, stop, or skip a cycle.
    """

    enabled: bool = False


class ConfigExt(StrictModel):
    chart: ChartSettings = Field(default_factory=ChartSettings)
    infoway: InfowaySettings = Field(default_factory=InfowaySettings)
    dexter: DexterSettings = Field(default_factory=DexterSettings)
    learning: LearningSettings = Field(default_factory=LearningSettings)
    shadow: ShadowSettings = Field(default_factory=ShadowSettings)


def load_config_ext(path: Path | str | None = None) -> ConfigExt:
    """Load chart settings. Unknown keys and out-of-range values raise."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_EXT_PATH
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("config_ext.yaml must be a mapping")
    return ConfigExt.model_validate(payload)
