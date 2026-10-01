"""Typed, validated loader for config.yaml.

Phase 0 scope is deliberately narrow: the LLM hallucination tolerances and the
tradable-symbol whitelist. Phase 1 extends the schema as new subsystems land.

Design rules:
  - Secrets NEVER live here. They come from the environment via config.py.
  - Invalid or missing values raise loudly. There are no silent defaults for
    anything the safety rules depend on.
  - The flat tradable list and the per-asset-class lists must agree. Two
    disagreeing sources of truth is a bug, so loading fails instead of picking
    one silently.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.yaml")

# Asset classes that may appear in symbols.whitelist.
ASSET_CLASSES = ("forex", "metals_commodities", "crypto", "indices")


class StrictModel(BaseModel):
    """Base model that rejects unknown keys instead of ignoring them.

    A typo'd key such as `hallucination_tolerence` would otherwise be silently
    dropped and the caller would fall back to a default, which is exactly the
    class of failure this loader exists to prevent.
    """

    model_config = ConfigDict(extra="forbid")


class LlmTolerances(StrictModel):
    """Per-field tolerances for LLM hallucination detection.

    price / ema / atr / macd are relative fractions (0.002 == 0.2%).
    rsi_points is absolute, because a relative tolerance on a bounded 0-100
    oscillator is not meaningful.
    macd_abs_floor is an absolute floor in price units, so that near-zero MACD
    histograms do not make the relative test absurdly tight.
    """

    price: float = Field(gt=0, le=0.05)
    ema: float = Field(gt=0, le=0.05)
    atr: float = Field(gt=0, le=0.50)
    rsi_points: float = Field(gt=0, le=50)
    macd: float = Field(gt=0, le=5.0)
    macd_abs_floor: float = Field(ge=0)


class LlmSettings(StrictModel):
    hallucination_tolerances: LlmTolerances
    confidence_adjust_min: int = Field(default=-20, ge=-100, le=0)
    confidence_adjust_max: int = Field(default=0, ge=-100, le=0)
    timeout_seconds: int = Field(default=25, gt=0, le=300)
    max_retries: int = Field(default=0, ge=0, le=3)

    @model_validator(mode="after")
    def _adjust_bounds_ordered(self) -> "LlmSettings":
        if self.confidence_adjust_min > self.confidence_adjust_max:
            raise ValueError(
                "llm.confidence_adjust_min must be <= llm.confidence_adjust_max "
                f"(got {self.confidence_adjust_min} > {self.confidence_adjust_max})"
            )
        return self


class TradingSettings(StrictModel):
    """Flat authoritative whitelist of symbols the system may trade.

    The per-class mapping in symbols.whitelist is retained for UI grouping;
    load_config() verifies the two agree.
    """

    tradable_symbols: list[str] = Field(min_length=1)


class DisplaySettings(StrictModel):
    """Presentation thresholds, in account currency unless noted.

    Kept in config so a magic number does not end up inlined in the HUD. The
    margin-level cut-off in particular is a correctness boundary: below it the
    percentage is meaningless and must render as N/A.
    """

    margin_level_na_below: float = Field(default=1.0, gt=0)
    margin_level_na_below_currency: str = Field(default="ACCOUNT")


class SymbolSettings(StrictModel):
    """symbols section.

    whitelist is a mapping of asset class -> list of symbols, matching
    config.yaml. Per-symbol spread overrides are Phase 1 scope, so they are
    accepted and preserved here rather than rejected, but not yet interpreted.
    """

    whitelist: dict[str, list[str]]
    overrides: dict[str, dict[str, float]] = Field(default_factory=dict)


class AppConfig(StrictModel):
    llm: LlmSettings
    trading: TradingSettings
    symbols: SymbolSettings
    display: DisplaySettings = Field(default_factory=DisplaySettings)

    @model_validator(mode="after")
    def _whitelists_agree(self) -> "AppConfig":
        flat = {s.upper() for s in self.trading.tradable_symbols}
        per_class: set[str] = set()
        for asset_class, syms in self.symbols.whitelist.items():
            if asset_class not in ASSET_CLASSES:
                raise ValueError(
                    f"symbols.whitelist has unknown asset class {asset_class!r}; "
                    f"expected one of {ASSET_CLASSES}"
                )
            per_class.update(s.upper() for s in syms)

        if not per_class:
            raise ValueError("symbols.whitelist must list at least one symbol")

        only_flat = flat - per_class
        only_classes = per_class - flat
        if only_flat or only_classes:
            raise ValueError(
                "trading.tradable_symbols and symbols.whitelist disagree. "
                f"Only in tradable_symbols: {sorted(only_flat)}. "
                f"Only in symbols.whitelist: {sorted(only_classes)}."
            )
        return self

    def is_tradable(self, symbol: str) -> bool:
        return symbol.upper() in {s.upper() for s in self.trading.tradable_symbols}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {path}. It is required; refusing to start "
            "with implicit defaults."
        )
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping at the top level")
    return raw


def _required(raw: dict[str, Any], key: str, path: Path) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        raise ValueError(f"{path} is missing required section {key!r}")
    return value


def _flatten_symbols(whitelist: dict[str, list[str]]) -> list[str]:
    seen: list[str] = []
    for asset_class in ASSET_CLASSES:
        for sym in whitelist.get(asset_class, []):
            if sym not in seen:
                seen.append(sym)
    return seen


def load_config(path: Path | str | None = None) -> AppConfig:
    """Load and validate config.yaml.

    Raises FileNotFoundError / ValueError / pydantic.ValidationError on bad
    input. Callers are expected to let that propagate: a trading engine that
    starts on an unvalidated config is worse than one that refuses to start.
    """
    cfg_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    raw = _read_yaml(cfg_path)

    symbols = _required(raw, "symbols", cfg_path)
    # Accept a config.yaml that predates trading.tradable_symbols by deriving
    # the flat list from the per-class whitelist, so the file stays the single
    # source of truth rather than requiring a duplicated list to be maintained.
    if not raw.get("trading", {}).get("tradable_symbols"):
        raw["trading"] = {
            **raw.get("trading", {}),
            "tradable_symbols": _flatten_symbols(_required(symbols, "whitelist", cfg_path)),
        }

    payload = {
        "llm": _required(raw, "llm", cfg_path),
        "trading": _required(raw, "trading", cfg_path),
        "symbols": symbols,
    }
    # display is optional in the YAML; the model supplies documented defaults.
    if isinstance(raw.get("display"), dict):
        payload["display"] = raw["display"]
    return AppConfig.model_validate(payload)