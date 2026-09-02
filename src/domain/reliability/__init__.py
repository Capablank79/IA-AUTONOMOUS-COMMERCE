"""
Dominio de Confiabilidad y Resiliencia (Reliability - Hito K.7).
"""

from .models import (
    FailureCategory,
    FailureRecoverability,
    classify_failure,
    SystemHealthState,
    CircuitState,
    CircuitBreakerConfig,
    RetryPolicy,
    RecoveryDecision,
    ReliabilityResult,
)
from .ports import (
    ClockPort,
    CircuitBreakerPort,
    IdempotencyStorePort,
    ReliabilityEnginePort,
)

__all__ = [
    "FailureCategory",
    "FailureRecoverability",
    "classify_failure",
    "SystemHealthState",
    "CircuitState",
    "CircuitBreakerConfig",
    "RetryPolicy",
    "RecoveryDecision",
    "ReliabilityResult",
    "ClockPort",
    "CircuitBreakerPort",
    "IdempotencyStorePort",
    "ReliabilityEnginePort",
]
