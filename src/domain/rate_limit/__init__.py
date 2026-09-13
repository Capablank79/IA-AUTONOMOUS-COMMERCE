"""
Módulo de dominio para Rate-limit Management (Hito P.11).
"""

from .models import (
    RateLimitScope,
    RateLimitStatus,
    RateLimitWindowUnit,
    RateLimitRule,
    RateLimitPolicy,
    RateLimitRequest,
    RateLimitDecision,
    RateLimitState,
    RuleEvaluationResult,
    RateLimitError,
    RateLimitPolicyNotFoundError,
    RateLimitPolicyIntegrityError,
    RateLimitStoreError,
    build_canonical_rate_limit_key,
)
from .ports import (
    RateLimitPolicyRepositoryPort,
    RateLimitStateStorePort,
    RateLimitTelemetryPort,
    RateLimitServicePort,
)

__all__ = [
    "RateLimitScope",
    "RateLimitStatus",
    "RateLimitWindowUnit",
    "RateLimitRule",
    "RateLimitPolicy",
    "RateLimitRequest",
    "RateLimitDecision",
    "RateLimitState",
    "RuleEvaluationResult",
    "RateLimitError",
    "RateLimitPolicyNotFoundError",
    "RateLimitPolicyIntegrityError",
    "RateLimitStoreError",
    "build_canonical_rate_limit_key",
    "RateLimitPolicyRepositoryPort",
    "RateLimitStateStorePort",
    "RateLimitTelemetryPort",
    "RateLimitServicePort",
]
