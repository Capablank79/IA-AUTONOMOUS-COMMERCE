"""
Modelos de dominio para Confiabilidad y Resiliencia (Reliability - Hito K.7).

Define:
- FailureCategory: Taxonomía explícita de fallos.
- FailureRecoverability: Clasificación de recuperabilidad (RETRYABLE, NON_RETRYABLE, RECONCILIATION_REQUIRED, UNKNOWN).
- SystemHealthState: Modos de degradación (HEALTHY, DEGRADED, UNAVAILABLE, UNKNOWN).
- CircuitState: Estados de Circuit Breaker (CLOSED, OPEN, HALF_OPEN).
- CircuitBreakerConfig / CircuitBreakerStats: Configuración y estado inmutable de Circuit Breaker.
- RetryPolicy: Política de reintentos determinista y acotada (backoff, jitter, retry-after, límites, fake-clock).
- RecoveryDecision: Decisión formal de recuperación ante fallo.
- ReliabilityResult: Resultado formal inmutable de una operación protegida por confiabilidad.

Principios K.7:
- Inmutabilidad estricta (frozen=True, MappingProxyType).
- Preservación determinista de incertidumbre: UNKNOWN != FAILURE confirmado y UNKNOWN != SUCCESS.
- Seguridad en reintentos con efectos secundarios (SIDE EFFECT + TIMEOUT/UNKNOWN -> VERIFY / RECONCILE).
- Idempotencia estricta por operation_id / idempotency_key.
- Aislamiento de fallos entre componentes críticos vs no críticos.
- Sanitización recursiva de secretos y exclusión estricta de Chain-of-Thought.
- Integración no intrusiva con Audit Trail (K.1) y Agent Trace (K.2).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Union, Sequence, Callable
import hashlib
import json


SENSITIVE_KEYS = {
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "pan",
    "cvv",
    "private_key",
    "credential",
    "access_token",
    "refresh_token",
    "authorization",
    "chain_of_thought",
    "reasoning",
    "reasoning_tokens",
    "internal_scratchpad",
}


def _sanitize_reliability_metadata(val: Any) -> Any:
    """Sanitiza recursivamente cualquier estructura de datos para eliminar secretos y CoT."""
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _sanitize_reliability_metadata(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_sanitize_reliability_metadata(v) for v in val]
    return val


class FailureCategory(str, Enum):
    """Taxonomía canónica de fallos del operador autónomo."""
    TRANSIENT = "TRANSIENT"
    PERMANENT = "PERMANENT"
    TIMEOUT = "TIMEOUT"
    RATE_LIMIT = "RATE_LIMIT"
    AUTHORIZATION = "AUTHORIZATION"
    VALIDATION = "VALIDATION"
    CONFLICT = "CONFLICT"
    DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"
    CORRUPTION = "CORRUPTION"
    UNKNOWN = "UNKNOWN"
    CANCELLED = "CANCELLED"


class FailureRecoverability(str, Enum):
    """Clasificación de recuperabilidad de un fallo."""
    RETRYABLE = "RETRYABLE"
    NON_RETRYABLE = "NON_RETRYABLE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    UNKNOWN = "UNKNOWN"


def classify_failure(
    category: FailureCategory,
    is_side_effect: bool = False,
    http_status_code: Optional[int] = None,
) -> FailureRecoverability:
    """
    Clasifica de manera determinista la recuperabilidad de un fallo.
    
    Regla fundamental K.7:
    - En operaciones con efectos secundarios (escrituras/mutaciones), TIMEOUT y UNKNOWN
      requieren RECONCILIATION_REQUIRED porque no sabemos si el efecto ocurrió en el destino.
    - En operaciones de solo lectura, TIMEOUT y TRANSIENT son RETRYABLE.
    - AUTHORIZATION, VALIDATION, CORRUPTION, CONFLICT son NON_RETRYABLE.
    """
    if category in (FailureCategory.AUTHORIZATION, FailureCategory.VALIDATION, FailureCategory.CORRUPTION, FailureCategory.CANCELLED):
        return FailureRecoverability.NON_RETRYABLE

    if category == FailureCategory.CONFLICT:
        return FailureRecoverability.NON_RETRYABLE

    if category == FailureCategory.RATE_LIMIT:
        return FailureRecoverability.RETRYABLE

    if is_side_effect and category in (FailureCategory.TIMEOUT, FailureCategory.UNKNOWN):
        return FailureRecoverability.RECONCILIATION_REQUIRED

    if category in (FailureCategory.TRANSIENT, FailureCategory.DEPENDENCY_UNAVAILABLE):
        return FailureRecoverability.RETRYABLE

    if not is_side_effect and category == FailureCategory.TIMEOUT:
        return FailureRecoverability.RETRYABLE

    if category == FailureCategory.UNKNOWN:
        return FailureRecoverability.UNKNOWN

    if category == FailureCategory.PERMANENT:
        return FailureRecoverability.NON_RETRYABLE

    return FailureRecoverability.NON_RETRYABLE


class SystemHealthState(str, Enum):
    """Modos de degradación y salud operacional del sistema / componente."""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class CircuitState(str, Enum):
    """Estados canónicos de Circuit Breaker."""
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


@dataclass(frozen=True)
class CircuitBreakerConfig:
    """Configuración inmutable para Circuit Breaker."""
    failure_threshold: int = 5
    recovery_timeout_seconds: float = 30.0
    half_open_success_threshold: int = 2
    monitored_categories: Tuple[FailureCategory, ...] = (
        FailureCategory.TRANSIENT,
        FailureCategory.TIMEOUT,
        FailureCategory.RATE_LIMIT,
        FailureCategory.DEPENDENCY_UNAVAILABLE,
    )

    def __post_init__(self):
        if self.failure_threshold <= 0:
            raise ValueError("failure_threshold must be greater than zero")
        if self.recovery_timeout_seconds <= 0:
            raise ValueError("recovery_timeout_seconds must be greater than zero")
        if self.half_open_success_threshold <= 0:
            raise ValueError("half_open_success_threshold must be greater than zero")


@dataclass(frozen=True)
class RetryPolicy:
    """
    Política inmutable de reintentos determinista y acotada.
    """
    max_attempts: int = 3
    initial_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    backoff_multiplier: float = 2.0
    timeout_seconds: Optional[float] = 30.0
    retryable_categories: Tuple[FailureCategory, ...] = (
        FailureCategory.TRANSIENT,
        FailureCategory.TIMEOUT,
        FailureCategory.RATE_LIMIT,
        FailureCategory.DEPENDENCY_UNAVAILABLE,
    )
    require_idempotency_for_side_effects: bool = True

    def __post_init__(self):
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_delay_seconds < 0:
            raise ValueError("initial_delay_seconds cannot be negative")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds cannot be less than initial_delay_seconds")
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be >= 1.0")

    def compute_delay(self, attempt: int, retry_after_seconds: Optional[float] = None) -> float:
        """
        Calcula el retraso determinista para un intento dado (1-based index).
        Si el servidor proporcionó Retry-After, se respeta acotándolo a max_delay_seconds.
        """
        if retry_after_seconds is not None and retry_after_seconds > 0:
            return min(float(retry_after_seconds), self.max_delay_seconds)
        
        # attempt 1 -> initial_delay_seconds
        # attempt 2 -> initial_delay_seconds * backoff_multiplier
        # ...
        exponent = max(0, attempt - 1)
        delay = self.initial_delay_seconds * (self.backoff_multiplier ** exponent)
        return min(delay, self.max_delay_seconds)

    def is_retryable(self, category: FailureCategory, is_side_effect: bool = False) -> bool:
        """Indica si una categoría califica para reintento directo según esta política."""
        if category not in self.retryable_categories:
            return False
        # Para side effects con timeout o unknown, se debe reconciliar antes de reintentar
        if is_side_effect and category in (FailureCategory.TIMEOUT, FailureCategory.UNKNOWN):
            return False
        return True


@dataclass(frozen=True)
class RecoveryDecision:
    """
    Decisión formal de recuperación ante fallo o incertidumbre.
    """
    decision_id: str
    operation_id: str
    failure_category: FailureCategory
    recoverability: FailureRecoverability
    attempt: int
    max_attempts: int
    retry_allowed: bool
    reconciliation_required: bool
    delay_seconds: float
    status: str
    reason: str
    correlation_id: str
    causation_id: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.decision_id:
            raise ValueError("decision_id must be a non-empty string")
        if not self.operation_id:
            raise ValueError("operation_id must be a non-empty string")
        if self.attempt < 1:
            raise ValueError("attempt must be >= 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.delay_seconds < 0:
            raise ValueError("delay_seconds cannot be negative")

        cleaned_evidence = _sanitize_reliability_metadata(self.evidence)
        object.__setattr__(self, "evidence", MappingProxyType(cleaned_evidence))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "operation_id": self.operation_id,
            "failure_category": self.failure_category.value,
            "recoverability": self.recoverability.value,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "retry_allowed": self.retry_allowed,
            "reconciliation_required": self.reconciliation_required,
            "delay_seconds": self.delay_seconds,
            "status": self.status,
            "reason": self.reason,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "created_at": self.created_at.isoformat(),
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class ReliabilityResult:
    """
    Resultado final estructurado e inmutable de una operación procesada con confiabilidad.
    """
    operation_id: str
    is_success: bool
    status: str  # SUCCESS, FAILED, TIMEOUT, UNKNOWN, CIRCUIT_OPEN, RECONCILED, DEGRADED
    output: Optional[Any] = None
    failure_category: Optional[FailureCategory] = None
    recoverability: Optional[FailureRecoverability] = None
    attempts_executed: int = 1
    reconciled: bool = False
    degraded: bool = False
    error_message: Optional[str] = None
    recovery_decisions: Tuple[RecoveryDecision, ...] = ()
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.operation_id:
            raise ValueError("operation_id must be a non-empty string")
        if self.attempts_executed < 0:
            raise ValueError("attempts_executed cannot be negative")

        cleaned_meta = _sanitize_reliability_metadata(self.metadata)
        object.__setattr__(self, "metadata", MappingProxyType(cleaned_meta))

    @property
    def is_unknown(self) -> bool:
        return self.status == "UNKNOWN" or self.failure_category == FailureCategory.UNKNOWN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "is_success": self.is_success,
            "status": self.status,
            "output": self.output,
            "failure_category": self.failure_category.value if self.failure_category else None,
            "recoverability": self.recoverability.value if self.recoverability else None,
            "attempts_executed": self.attempts_executed,
            "reconciled": self.reconciled,
            "degraded": self.degraded,
            "error_message": self.error_message,
            "recovery_decisions": [d.to_dict() for d in self.recovery_decisions],
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "metadata": dict(self.metadata),
        }
