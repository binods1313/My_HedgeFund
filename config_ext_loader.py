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


class ConfigExt(StrictModel):
    chart: ChartSettings = Field(default_factory=ChartSettings)


def load_config_ext(path: Path | str | None = None) -> ConfigExt:
    """Load chart settings. Unknown keys and out-of-range values raise."""
    target = Path(path) if path is not None else DEFAULT_CONFIG_EXT_PATH
    payload = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("config_ext.yaml must be a mapping")
    return ConfigExt.model_validate(payload)
