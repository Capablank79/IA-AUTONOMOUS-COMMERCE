"""
Modelos de dominio para SaaS Observability (Hito O.12 — SaaS / Platformization).

Define:
- ObservabilityScope: Alcance (TENANT, PLATFORM).
- MetricType: Taxonomía de métricas operacionales derivables.
- MetricUnit: Unidades canónicas de métricas (COUNT, MILLISECONDS, TOKENS, CURRENCY, PERCENT, RATIO).
- ObservabilityMetric: Métrica agregada inmutable con valor, unidad y dimensiones.
- MetricSample: Muestra puntual de telemetría inmutable.
- TenantHealthStatus: Estados de salud operacional (HEALTHY, DEGRADED, UNHEALTHY, UNKNOWN).
- AlertSeverity: Niveles deterministas de severidad (INFO, WARNING, HIGH, CRITICAL).
- AlertStatus: Estados del ciclo de vida de una alerta operacional (ACTIVE, ACKNOWLEDGED, RESOLVED).
- OperationalAlert: Alerta operacional estructurada e inmutable con deduplicación y resolución.
- TenantOperationalSnapshot: Snapshot integral y seguro de la operación de un tenant sin PII ni secretos.

Principios O.12:
1. Responde a: "¿Puede la plataforma observar de forma segura la salud y operación de cada tenant sin mezclar datos entre tenants?".
2. Tenant Isolation estricto: Métricas, alertas y snapshots pertenecen a un tenant_id explícito.
3. No duplicar Hito K: Agrega y proyecta hechos estructurados de K.1 (Audit), K.2 (Trace), K.3 (Cost), O.5 (Model Gateway), O.6 (Usage), O.7 (Quotas), O.9 (Billing).
4. UNKNOWN != ZERO: La ausencia de datos o mediciones produce UNKNOWN / None, nunca 0 o HEALTHY artificial.
5. Inmutabilidad y determinismo: frozen=True, MappingProxyType y checksums SHA-256.
6. Alert != Automatic Action: Las alertas informan; no ejecutan shutdown, mutaciones de planes ni cobros.
7. Privacy & Sanitization (N.9): CERO prompts, tokens de autenticación, PAN/CVV ni CoT.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class ObservabilityError(Exception):
    """Excepción base para SaaS Observability."""
    pass


class ObservabilityIntegrityError(ObservabilityError):
    """Se lanza cuando se detecta corrupción o checksum inválido en un snapshot o alerta."""
    pass


class ObservabilityAccessError(ObservabilityError):
    """Se lanza ante intentos de acceso cross-tenant o no autorizados."""
    pass


class ObservabilityScope(str, Enum):
    """Alcance de la observación y métricas."""
    TENANT = "TENANT"
    PLATFORM = "PLATFORM"


class MetricType(str, Enum):
    """Taxonomía canónica de métricas SaaS operacionales."""
    REQUEST_COUNT = "REQUEST_COUNT"
    SUCCESS_COUNT = "SUCCESS_COUNT"
    FAILURE_COUNT = "FAILURE_COUNT"
    ERROR_RATE = "ERROR_RATE"
    LATENCY_MS = "LATENCY_MS"
    MODEL_REQUEST_COUNT = "MODEL_REQUEST_COUNT"
    TOKEN_USAGE = "TOKEN_USAGE"
    AI_COST = "AI_COST"
    CACHE_HIT_RATE = "CACHE_HIT_RATE"
    QUOTA_REJECTION_COUNT = "QUOTA_REJECTION_COUNT"
    PROVIDER_ERROR_COUNT = "PROVIDER_ERROR_COUNT"
    AUTHORIZATION_DENIAL_COUNT = "AUTHORIZATION_DENIAL_COUNT"
    EMERGENCY_STOP_BLOCK_COUNT = "EMERGENCY_STOP_BLOCK_COUNT"


class MetricUnit(str, Enum):
    """Unidades de medida para métricas."""
    COUNT = "COUNT"
    MILLISECONDS = "MILLISECONDS"
    TOKENS = "TOKENS"
    CURRENCY = "CURRENCY"
    PERCENT = "PERCENT"
    RATIO = "RATIO"


class TenantHealthStatus(str, Enum):
    """Estados canónicos de salud operacional del tenant."""
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"
    UNKNOWN = "UNKNOWN"


class AlertSeverity(str, Enum):
    """Niveles canónicos de severidad de alertas operacionales."""
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertStatus(str, Enum):
    """Estados del ciclo de vida de la alerta operacional."""
    ACTIVE = "ACTIVE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class OperationalAlertType(str, Enum):
    """Taxonomía canónica de tipos de alertas operacionales."""
    HIGH_ERROR_RATE = "HIGH_ERROR_RATE"
    PROVIDER_DEGRADED = "PROVIDER_DEGRADED"
    QUOTA_NEAR_LIMIT = "QUOTA_NEAR_LIMIT"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    BILLING_PAST_DUE = "BILLING_PAST_DUE"
    EMERGENCY_STOP_ACTIVE = "EMERGENCY_STOP_ACTIVE"
    SECURITY_DENIAL_SPIKE = "SECURITY_DENIAL_SPIKE"


def compute_observability_checksum(payload: Dict[str, Any]) -> str:
    """Calcula determinísticamente el checksum SHA-256 para registros de observabilidad."""
    def _json_serial(obj: Any) -> Any:
        if isinstance(obj, (datetime,)):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (set, tuple)):
            return list(obj)
        raise TypeError(f"Type {type(obj)} not serializable")

    clean_payload = {k: v for k, v in payload.items() if k != "checksum"}
    encoded = json.dumps(clean_payload, sort_keys=True, default=_json_serial).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ObservabilityMetric:
    """Métrica cuantitativa u operacional agregada e inmutable."""
    metric_type: MetricType
    value: Optional[Union[int, float, Decimal]]
    unit: MetricUnit
    sample_count: int
    evaluated_at: datetime
    dimensions: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.metric_type, MetricType):
            object.__setattr__(self, "metric_type", MetricType(self.metric_type))
        if not isinstance(self.unit, MetricUnit):
            object.__setattr__(self, "unit", MetricUnit(self.unit))
        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))
        if not isinstance(self.dimensions, MappingProxyType):
            object.__setattr__(self, "dimensions", MappingProxyType(dict(self.dimensions)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "metric_type": self.metric_type.value,
            "value": float(self.value) if isinstance(self.value, Decimal) else self.value,
            "unit": self.unit.value,
            "sample_count": self.sample_count,
            "evaluated_at": self.evaluated_at.isoformat(),
            "dimensions": dict(self.dimensions),
        }


@dataclass(frozen=True)
class OperationalAlert:
    """
    Alerta operacional estructurada e inmutable tenant-scoped.

    Garantías:
    - Inmutable (frozen=True).
    - tenant_id obligatorio y validado.
    - Cero secretos o PII no sanitizada.
    - Checksum determinista SHA-256.
    - Deduplicación por deduplication_key.
    """
    alert_id: str
    tenant_id: str
    alert_type: OperationalAlertType
    severity: AlertSeverity
    status: AlertStatus
    summary: str
    details: Mapping[str, Any]
    triggered_at: datetime
    deduplication_key: str
    resolved_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    organization_id: Optional[str] = None
    checksum: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.alert_id, "alert_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.organization_id:
            validate_safe_identifier(self.organization_id, "organization_id")
        if not isinstance(self.alert_type, OperationalAlertType):
            object.__setattr__(self, "alert_type", OperationalAlertType(self.alert_type))
        if not isinstance(self.severity, AlertSeverity):
            object.__setattr__(self, "severity", AlertSeverity(self.severity))
        if not isinstance(self.status, AlertStatus):
            object.__setattr__(self, "status", AlertStatus(self.status))
        if self.triggered_at.tzinfo is None:
            object.__setattr__(self, "triggered_at", self.triggered_at.replace(tzinfo=timezone.utc))
        if self.resolved_at and self.resolved_at.tzinfo is None:
            object.__setattr__(self, "resolved_at", self.resolved_at.replace(tzinfo=timezone.utc))
        if self.acknowledged_at and self.acknowledged_at.tzinfo is None:
            object.__setattr__(self, "acknowledged_at", self.acknowledged_at.replace(tzinfo=timezone.utc))

        sanitized_details = sanitize_security_data(dict(self.details))
        object.__setattr__(self, "details", deep_freeze(sanitized_details))

        if not self.checksum:
            payload = {
                "alert_id": self.alert_id,
                "tenant_id": self.tenant_id,
                "organization_id": self.organization_id,
                "alert_type": self.alert_type.value,
                "severity": self.severity.value,
                "status": self.status.value,
                "summary": self.summary,
                "details": dict(self.details),
                "triggered_at": self.triggered_at,
                "deduplication_key": self.deduplication_key,
                "resolved_at": self.resolved_at,
                "acknowledged_at": self.acknowledged_at,
            }
            object.__setattr__(self, "checksum", compute_observability_checksum(payload))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "alert_type": self.alert_type.value,
            "severity": self.severity.value,
            "status": self.status.value,
            "summary": self.summary,
            "details": dict(self.details),
            "triggered_at": self.triggered_at.isoformat(),
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "deduplication_key": self.deduplication_key,
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class TenantOperationalSnapshot:
    """
    Snapshot operacional consolidado, determinista y seguro de un Tenant en una ventana temporal.

    Garantías:
    - Inmutable y libre de PII/secretos/prompts.
    - UNKNOWN != ZERO preservado en valores ausentes.
    - Deduplicación de alertas y cálculo determinista de estado de salud.
    - Checksum de integridad.
    """
    tenant_id: str
    health_status: TenantHealthStatus
    window_seconds: int
    evaluated_at: datetime
    request_count: Optional[int]
    error_rate: Optional[float]
    avg_latency_ms: Optional[float]
    p95_latency_ms: Optional[float]
    total_tokens: Optional[int]
    total_cost_usd: Optional[Decimal]
    quota_status: str
    billing_status: str
    active_alerts: Tuple[OperationalAlert, ...]
    metrics: Mapping[str, ObservabilityMetric]
    organization_id: Optional[str] = None
    checksum: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.organization_id:
            validate_safe_identifier(self.organization_id, "organization_id")
        if not isinstance(self.health_status, TenantHealthStatus):
            object.__setattr__(self, "health_status", TenantHealthStatus(self.health_status))
        if self.evaluated_at.tzinfo is None:
            object.__setattr__(self, "evaluated_at", self.evaluated_at.replace(tzinfo=timezone.utc))
        if not isinstance(self.active_alerts, tuple):
            object.__setattr__(self, "active_alerts", tuple(self.active_alerts))
        if not isinstance(self.metrics, MappingProxyType):
            object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))

        if not self.checksum:
            payload = {
                "tenant_id": self.tenant_id,
                "organization_id": self.organization_id,
                "health_status": self.health_status.value,
                "window_seconds": self.window_seconds,
                "evaluated_at": self.evaluated_at,
                "request_count": self.request_count,
                "error_rate": self.error_rate,
                "avg_latency_ms": self.avg_latency_ms,
                "p95_latency_ms": self.p95_latency_ms,
                "total_tokens": self.total_tokens,
                "total_cost_usd": self.total_cost_usd,
                "quota_status": self.quota_status,
                "billing_status": self.billing_status,
                "active_alerts": [a.alert_id for a in self.active_alerts],
                "metrics": {k: m.to_dict() for k, m in self.metrics.items()},
            }
            object.__setattr__(self, "checksum", compute_observability_checksum(payload))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tenant_id": self.tenant_id,
            "organization_id": self.organization_id,
            "health_status": self.health_status.value,
            "window_seconds": self.window_seconds,
            "evaluated_at": self.evaluated_at.isoformat(),
            "request_count": self.request_count,
            "error_rate": self.error_rate,
            "avg_latency_ms": self.avg_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "total_tokens": self.total_tokens,
            "total_cost_usd": float(self.total_cost_usd) if self.total_cost_usd is not None else None,
            "quota_status": self.quota_status,
            "billing_status": self.billing_status,
            "active_alerts": [a.to_dict() for a in self.active_alerts],
            "metrics": {k: m.to_dict() for k, m in self.metrics.items()},
            "checksum": self.checksum,
        }
