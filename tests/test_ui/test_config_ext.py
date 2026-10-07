from pathlib import Path

import pytest
from pydantic import ValidationError

from config_ext_loader import load_config_ext


def test_shipped_file_loads_defaults() -> None:
    settings = load_config_ext()
    assert settings.chart.atr_k == 1.5
    assert settings.chart.rr == 3.0
    assert settings.infoway.daily_cap == 200
    assert settings.infoway.timeout_s == 5
    assert settings.infoway.cache_ttl_s == 30


def test_valid_override(tmp_path: Path) -> None:
    target = tmp_path / "config_ext.yaml"
    target.write_text("chart:\n  atr_k: 2\n  rr: 4\n", encoding="utf-8")
    settings = load_config_ext(target)
    assert settings.chart.atr_k == 2
    assert settings.chart.rr == 4


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "config_ext.yaml"
    target.write_text("chart:\n  atr_k: 1.5\n  rr: 3\n  extra: 1\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config_ext(target)


def test_out_of_range_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "config_ext.yaml"
    target.write_text("chart:\n  atr_k: 0\n  rr: 11\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_config_ext(target)


def test_non_mapping_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "config_ext.yaml"
    target.write_text("- not-a-map\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_config_ext(target)
