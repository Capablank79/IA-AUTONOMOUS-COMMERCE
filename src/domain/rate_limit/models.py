"""
Modelos de dominio para Rate-limit Management (Hito P.11 — Production / Operations).

Define:
- RateLimitScope: Scopes canónicos de rate limiting (TENANT, USER, PROVIDER, MODEL, ENDPOINT).
- RateLimitStatus: Estados canónicos de decisión (ALLOW, DENY, UNKNOWN).
- RateLimitWindowUnit: Unidades canónicas de ventana (SECOND, MINUTE, HOUR, DAY).
- RateLimitRule: Regla inmutable y determinista de rate limit y burst.
- RateLimitPolicy: Colección inmutable y versionada de reglas con checksum criptográfico SHA-256.
- RateLimitRequest: Petición de consumo de tasa con metadatos contextuales.
- RateLimitDecision: Resultado inmutable de evaluación de rate limit (limit, remaining, reset_at, retry_after_seconds).
- RateLimitState: Estado en memoria / persistente del token bucket para una clave canónica.
- Canonical Key Helper: `build_canonical_rate_limit_key` (environment:tenant:scope:resource:rule).

Principios P.11:
1. Responde a: "¿Puede la plataforma limitar de forma justa y segura la tasa de requests/consumo para proteger tenants, providers y recursos compartidos sin confundir rate limits con quotas comerciales?".
2. RATE LIMIT != QUOTA: O.7 gobierna cuotas comerciales/períodos; P.11 gobierna velocidad y ráfaga runtime (burst).
3. RATE LIMIT != CAPACITY: P.10 proyecta y recomienda; P.11 impone límites runtime. P.10 NO modifica automáticamente P.11.
4. Determinismo y Explicabilidad: Token Bucket con tasa constante de refill y capacidad máxima para burst.
5. Inyección de tiempo: ClockPort K.7 obligatorio, cero sleeps y cero timestamps no deterministas.
6. Aislamiento absoluto por Environment (DEV, STAGING, PROD) y Tenant.
7. Precedencia: Platform Hard Limit -> Provider Hard Limit -> Plan/Policy Limit -> Tenant Override más restrictivo.
8. Fail-safe: Si una regla/policy requerida está ausente o corrupta, el default para scopes críticos es DENY (fail-closed) salvo configuración explícita.
9. Cero PII, secretos, tokens o passwords en claves o metadata.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.monitoring.models import resolve_environment
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.reliability.ports import ClockPort


class RateLimitError(Exception):
    """Excepción base para Rate-limit Management (P.11)."""
    pass


class RateLimitPolicyNotFoundError(RateLimitError):
    """Se lanza cuando no existe política configurada y se requiere resolución estricta."""
    pass


class RateLimitPolicyIntegrityError(RateLimitError):
    """Se lanza cuando se detecta corrupción o violación de integridad en una política de rate limit."""
    pass


class RateLimitStoreError(RateLimitError):
    """Se lanza cuando el store/backend de contadores o estados de rate limit falla."""
    pass


class RateLimitScope(str, Enum):
    """Ámbitos canónicos de aplicación de límites de tasa."""
    TENANT = "TENANT"
    USER = "USER"
    PROVIDER = "PROVIDER"
    MODEL = "MODEL"
    ENDPOINT = "ENDPOINT"


class RateLimitStatus(str, Enum):
    """Estados canónicos de decisión de rate limit."""
    ALLOW = "ALLOW"
    DENY = "DENY"
    UNKNOWN = "UNKNOWN"


class RateLimitWindowUnit(str, Enum):
    """Unidades canónicas de tiempo para rate limits."""
    SECOND = "SECOND"
    MINUTE = "MINUTE"
    HOUR = "HOUR"
    DAY = "DAY"

    @property
    def seconds(self) -> int:
        mapping = {
            RateLimitWindowUnit.SECOND: 1,
            RateLimitWindowUnit.MINUTE: 60,
            RateLimitWindowUnit.HOUR: 3600,
            RateLimitWindowUnit.DAY: 86400,
        }
        return mapping[self]


def build_canonical_rate_limit_key(
    environment: Union[ApplicationEnvironment, str],
    tenant_id: Optional[str],
    scope: RateLimitScope,
    target_identifier: Optional[str],
    rule_id: str,
) -> str:
    """
    Construye una clave canónica e inequívoca para contadores de rate limit.
    Formato: `env:{environment}:tenant:{tenant_id}:scope:{scope}:target:{target}:rule:{rule_id}`
    """
    env_str = resolve_environment(environment).value if isinstance(environment, (ApplicationEnvironment, str)) else str(environment)
    clean_tenant = tenant_id or "global"
    clean_target = target_identifier or "all"
    validate_safe_identifier(clean_tenant, "tenant_id")
    if target_identifier:
        validate_safe_identifier(target_identifier, "target_identifier")
    validate_safe_identifier(rule_id, "rule_id")

    return f"env:{env_str}:tenant:{clean_tenant}:scope:{scope.value}:target:{clean_target}:rule:{rule_id}"


@dataclass(frozen=True)
class RateLimitRule:
    """
    Regla inmutable y determinista de rate limiting.
    - limit_rate: Cantidad permitida en la ventana base (e.g. 60 req).
    - window_unit: Unidad de tiempo (e.g. MINUTE).
    - burst_capacity: Capacidad máxima del token bucket (si es None, igual a limit_rate).
    - scope: Alcance (TENANT, USER, PROVIDER, MODEL, ENDPOINT).
    - target_identifier: Identificador específico si aplica (e.g. 'openai', 'claude-3', '/api/v1/infer').
    - is_hard_limit: Si True, un DENY bloquea de inmediato.
    """
    rule_id: str
    limit_rate: int
    window_unit: RateLimitWindowUnit = RateLimitWindowUnit.MINUTE
    burst_capacity: Optional[int] = None
    scope: RateLimitScope = RateLimitScope.TENANT
    target_identifier: Optional[str] = None
    is_hard_limit: bool = True
    custom_window_seconds: Optional[int] = None
    description: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.rule_id, "rule_id")
        if self.target_identifier:
            validate_safe_identifier(self.target_identifier, "target_identifier")

        if self.limit_rate < 0:
            raise ValueError(f"limit_rate for rule {self.rule_id} cannot be negative.")

        burst = self.burst_capacity if self.burst_capacity is not None else self.limit_rate
        if burst < self.limit_rate:
            raise ValueError(f"burst_capacity ({burst}) cannot be less than limit_rate ({self.limit_rate}).")

        if self.custom_window_seconds is not None and self.custom_window_seconds <= 0:
            raise ValueError("custom_window_seconds must be strictly positive.")

    @property
    def window_seconds(self) -> int:
        if self.custom_window_seconds is not None:
            return self.custom_window_seconds
        return self.window_unit.seconds

    @property
    def effective_burst(self) -> int:
        return self.burst_capacity if self.burst_capacity is not None else self.limit_rate

    @property
    def refill_rate_per_second(self) -> float:
        """Tasa de reposición de tokens por segundo."""
        sec = self.window_seconds
        if sec <= 0:
            return float(self.limit_rate)
        return float(self.limit_rate) / float(sec)


@dataclass(frozen=True)
class RateLimitPolicy:
    """
    Política inmutable de rate limits con checksum SHA-256 para verificación de integridad.
    """
    policy_id: str
    tenant_id: Optional[str]
    rules: Tuple[RateLimitRule, ...] = ()
    policy_version: str = "1.0.0"
    is_unlimited: bool = False
    description: Optional[str] = None
    checksum: str = field(default="")

    def __post_init__(self):
        validate_safe_identifier(self.policy_id, "policy_id")
        if self.tenant_id:
            validate_safe_identifier(self.tenant_id, "tenant_id")

        if isinstance(self.rules, (list, Sequence)):
            object.__setattr__(self, "rules", tuple(self.rules))

        calc_checksum = self._compute_checksum()
        if not self.checksum:
            object.__setattr__(self, "checksum", calc_checksum)
        elif self.checksum != calc_checksum:
            raise RateLimitPolicyIntegrityError(
                f"RateLimitPolicy checksum mismatch: provided={self.checksum}, calculated={calc_checksum}"
            )

    def _compute_checksum(self) -> str:
        rules_repr = [
            f"{r.rule_id}:{r.limit_rate}:{r.window_unit.value}:{r.effective_burst}:{r.scope.value}:{r.target_identifier}:{r.is_hard_limit}"
            for r in sorted(self.rules, key=lambda x: x.rule_id)
        ]
        raw = f"{self.policy_id}|{self.tenant_id or 'global'}|{self.policy_version}|{self.is_unlimited}|{','.join(rules_repr)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()


@dataclass(frozen=True)
class RateLimitRequest:
    """
    Solicitud de pre-flight rate limit check.
    """
    tenant_id: Optional[str] = None
    identity_id: Optional[str] = None
    provider: Optional[str] = None
    model_id: Optional[str] = None
    endpoint: Optional[str] = None
    cost_units: int = 1  # tokens / requests a consumir
    environment: Union[ApplicationEnvironment, str] = ApplicationEnvironment.PRODUCTION
    correlation_id: Optional[str] = None
    requested_at: Optional[datetime] = None

    def __post_init__(self):
        if self.tenant_id:
            validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.identity_id:
            validate_safe_identifier(self.identity_id, "identity_id")
        if self.provider:
            validate_safe_identifier(self.provider, "provider")
        if self.model_id:
            validate_safe_identifier(self.model_id, "model_id")
        if self.cost_units < 1:
            raise ValueError("cost_units must be at least 1.")


@dataclass(frozen=True)
class RateLimitState:
    """
    Estado persistente / en memoria del token bucket para una clave canónica.
    Boundary inmutable:
    - key: clave canónica.
    - tokens: tokens actualmente disponibles (float para precisión continua).
    - last_refill_at: timestamp UTC del último cálculo de reposición.
    - max_capacity: capacidad de ráfaga (burst).
    """
    key: str
    tokens: float
    last_refill_at: datetime
    max_capacity: int

    def __post_init__(self):
        if self.last_refill_at.tzinfo is None:
            object.__setattr__(self, "last_refill_at", self.last_refill_at.replace(tzinfo=timezone.utc))


@dataclass(frozen=True)
class RuleEvaluationResult:
    """Detalle inmutable de la evaluación de una regla específica de rate limit."""
    rule_id: str
    scope: RateLimitScope
    target_identifier: Optional[str]
    limit_rate: int
    burst_capacity: int
    current_tokens: float
    remaining: int
    is_passed: bool
    retry_after_seconds: int
    reason_code: str


@dataclass(frozen=True)
class RateLimitDecision:
    """
    Decisión determinista, segura y estructurada de Rate Limiting.
    Contiene metadata segura para headers HTTP (Limit, Remaining, Reset, Retry-After).
    """
    status: RateLimitStatus
    limit: int
    remaining: int
    reset_at: datetime
    retry_after_seconds: int
    scope_violated: Optional[RateLimitScope] = None
    rule_id_violated: Optional[str] = None
    reason_codes: Tuple[str, ...] = ()
    rule_evaluations: Tuple[RuleEvaluationResult, ...] = ()
    evaluated_at: Optional[datetime] = None
    correlation_id: Optional[str] = None
    rationale: Optional[str] = None

    def __post_init__(self):
        if self.evaluated_at and self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))
        if self.reset_at and self.reset_at.tzinfo is None:
            object.__setattr__(self, "reset_at", self.reset_at.replace(tzinfo=timezone.utc))

        if isinstance(self.reason_codes, (list, Sequence)):
            object.__setattr__(self, "reason_codes", tuple(self.reason_codes))
        if isinstance(self.rule_evaluations, (list, Sequence)):
            object.__setattr__(self, "rule_evaluations", tuple(self.rule_evaluations))

        if self.retry_after_seconds < 0:
            object.__setattr__(self, "retry_after_seconds", 0)

    @property
    def is_allowed(self) -> bool:
        return self.status == RateLimitStatus.ALLOW

    @property
    def http_status_code(self) -> int:
        if self.status == RateLimitStatus.ALLOW:
            return 200
        elif self.status == RateLimitStatus.DENY:
            return 429
        return 500

    def get_http_headers(self) -> Dict[str, str]:
        """
        Retorna los headers HTTP estándar para rate limiting:
        - X-RateLimit-Limit
        - X-RateLimit-Remaining
        - X-RateLimit-Reset (Unix timestamp)
        - Retry-After (segundos enteros si DENY)
        """
        headers = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(int(self.reset_at.timestamp())),
        }
        if self.status == RateLimitStatus.DENY:
            headers["Retry-After"] = str(self.retry_after_seconds)
        return headers

    def to_http_headers(self) -> Dict[str, str]:
        return self.get_http_headers()
