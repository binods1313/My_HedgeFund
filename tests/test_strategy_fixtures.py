"""Shared fixtures for strategy tests.

Builds a SymbolSpec from explicit values rather than a live terminal, so sizing
is testable against known numbers. The shape mirrors ui.risk_calc.SymbolSpec so
LotSizer receives exactly what the dashboard builds in production.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def make_spec(*, symbol="EURUSD", digits=5, point=1e-05, tick_size=1e-05,
              tick_value_loss=1.0, contract_size=100000.0,
              volume_min=0.01, volume_max=100.0, volume_step=0.01,
              stops_level_points=0):
    from ui.risk_calc import SymbolSpec

    return SymbolSpec(
        symbol=symbol,
        category="forex",
        digits=digits,
        point=point,
        tick_size=tick_size,
        tick_value_loss=tick_value_loss,
        contract_size=contract_size,
        volume_min=volume_min,
        volume_max=volume_max,
        volume_step=volume_step,
        stops_level_points=stops_level_points,
    )


#: EURUSD as measured on MetaQuotes-Demo via probe_tick_specs.py.
EURUSD_SPEC_KWARGS = dict(
    symbol="EURUSD", digits=5, point=1e-05, tick_size=1e-05,
    tick_value_loss=0.8886124316879194, contract_size=100000.0,
    volume_min=0.01, volume_max=100.0, volume_step=0.01,
)


def EURUSD_SPEC():
    return make_spec(**EURUSD_SPEC_KWARGS)