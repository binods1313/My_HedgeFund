"""Validation layer: structural gates applied to a compiled intent.

    xai_client.py   xAI structural validation, asymmetric fail-safe
"""

from engine.validation.xai_client import (
    MIN_CONFLUENCES_FOR_XAI,
    PROVIDER_FAILURE_LOG,
    REJECTION_LOG,
    GateDecision,
    SpatialContext,
    XAIValidationGate,
)

__all__ = [
    "MIN_CONFLUENCES_FOR_XAI",
    "PROVIDER_FAILURE_LOG",
    "REJECTION_LOG",
    "GateDecision",
    "SpatialContext",
    "XAIValidationGate",
]