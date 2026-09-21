"""
Modelos de dominio para Alertas de Producción (Hito P.8 — Production / Operations).

Define:
- AlertSeverity: Niveles de severidad deterministas (INFO, WARNING, HIGH, CRITICAL).
- AlertState: Estados de ciclo de vida (ACTIVE, ACKNOWLEDGED, RESOLVED).
- AlertRuleType: Taxonomía canónica de reglas de alerta de producción.
- ProductionAlertScope: Alcance (PLATFORM, ENVIRONMENT, TENANT).
- AlertRule: Definición inmutable de regla con umbrales, ventana y severidad base.
- ProductionAlertInstance: Instancia inmutable y trazable de una alerta con checksum SHA-256.
- AlertEvaluationStatus: Resultado de evaluación (TRIGGERED, HEALTHY, INSUFFICIENT_DATA, COOLDOWN).
- AlertEvaluationResult: Resultado detallado con evidencia sanitizada.
- NotificationMessage: Mensaje de notificación tipado y seguro.
- NotificationResult: Resultado inmutable de entrega de notificación.

Principios P.8:
1. Responde a: "¿Puede la plataforma detectar condiciones operativas relevantes, generar alertas confiables,
   evitar duplicados y gestionar su ciclo de vida sin provocar acciones automáticas peligrosas?".
2. REUSE > EXTEND > CREATE: Reconciliable con O.12 OperationalAlert y métricas P.7 / health P.6.
3. UNKNOWN != OK / UNKNOWN != ZERO: Si la métrica subyacente es UNKNOWN, la regla no se resuelve falsamente como healthy.
4. ALERT != ACTION: Las alertas informan y notifican; NUNCA ejecutan acciones automáticas peligrosas
   (no cancelan subscripciones, no modifican cuotas, no restauran backups, no activan Emergency Stop).
5. Deduplicación canónica por (environment, scope, rule_type, target_resource).
6. Cooldown determinista para evitar spam de alertas.
7. Acknowledge != Resolve: Un operador reconociendo la alerta no indica resolución del problema técnico.
8. Escalación de severidad in-place sin duplicar la alerta.
9. Aislamiento estricto por Environment (DEV, STAGING, PROD) y Tenant.
10. Cero secretos, PII, DSNs, passwords o prompts en payloads de evidencia o notificaciones.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union, List

from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.domain.monitoring.models import (
    MetricType,
    MetricWindow,
    MonitoringScope,
    resolve_environment,
)
from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class ProductionAlertingError(Exception):
    """Excepción base para Alerting de Producción."""
    pass


class ProductionAlertIntegrityError(ProductionAlertingError):
    """Se lanza ante corrupción o inconsistencia de checksum en una alerta."""
    pass


class ProductionAlertSecurityError(ProductionAlertingError):
    """Se lanza ante violación de seguridad o intento de acceso cross-tenant."""
    pass


class AlertSeverity(str, Enum):
    """Niveles canónicos de severidad de alertas operacionales."""
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def level(self) -> int:
        levels = {
            "INFO": 1,
            "WARNING": 2,
            "HIGH": 3,
            "CRITICAL": 4,
        }
        return levels[self.value]

    def __ge__(self, other: "AlertSeverity") -> bool:
        if not isinstance(other, AlertSeverity):
            return NotImplemented
        return self.level >= other.level

    def __gt__(self, other: "AlertSeverity") -> bool:
        if not isinstance(other, AlertSeverity):
            return NotImplemented
        return self.level > other.level

    def __le__(self, other: "AlertSeverity") -> bool:
        if not isinstance(other, AlertSeverity):
            return NotImplemented
        return self.level <= other.level

    def __lt__(self, other: "AlertSeverity") -> bool:
        if not isinstance(other, AlertSeverity):
            return NotImplemented
        return self.level < other.level


class AlertState(str, Enum):
    """Estados del ciclo de vida de una alerta operacional."""
    ACTIVE = "ACTIVE"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"


class AlertRuleType(str, Enum):
    """Taxonomía canónica de reglas de alerta en producción (P.8)."""
    HIGH_ERROR_RATE = "HIGH_ERROR_RATE"
    HIGH_LATENCY = "HIGH_LATENCY"
    READINESS_FAILURE = "READINESS_FAILURE"
    DATABASE_UNAVAILABLE = "DATABASE_UNAVAILABLE"
    BACKUP_STALE_OR_FAILED = "BACKUP_STALE_OR_FAILED"
    DR_LAST_SIMULATION_FAILED = "DR_LAST_SIMULATION_FAILED"
    QUOTA_EXHAUSTION = "QUOTA_EXHAUSTION"
    PROVIDER_FAILURE_RATE = "PROVIDER_FAILURE_RATE"
    EMERGENCY_STOP_ACTIVE = "EMERGENCY_STOP_ACTIVE"
    MISSION_HEALTH_DEGRADED = "MISSION_HEALTH_DEGRADED"


class ProductionAlertScope(str, Enum):
    """Alcance de la alerta de producción."""
    PLATFORM = "PLATFORM"
    ENVIRONMENT = "ENVIRONMENT"
    TENANT = "TENANT"


class AlertEvaluationStatus(str, Enum):
    """Estado resultante de la evaluación de una regla de alerta."""
    TRIGGERED = "TRIGGERED"
    HEALTHY = "HEALTHY"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    COOLDOWN = "COOLDOWN"


def compute_alert_checksum(payload: Dict[str, Any]) -> str:
    """Calcula determinísticamente el checksum SHA-256 para una alerta de producción."""
    def _json_serial(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, (set, tuple)):
            return list(obj)
        if isinstance(obj, (ApplicationEnvironment,)):
            return obj.value
        raise TypeError(f"Type {type(obj)} not serializable")

    clean_payload = {k: v for k, v in payload.items() if k != "checksum"}
    encoded = json.dumps(clean_payload, sort_keys=True, default=_json_serial).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def generate_deduplication_key(
    environment: ApplicationEnvironment,
    scope: ProductionAlertScope,
    rule_type: AlertRuleType,
    target_resource: str = "default",
    tenant_id: Optional[str] = None,
) -> str:
    """
    Genera la clave canónica de deduplicación para una alerta:
    {environment}:{scope}:{rule_type}:{target_resource}[:{tenant_id}]
    """
    env_str = environment.value if isinstance(environment, ApplicationEnvironment) else str(environment)
    scope_str = scope.value if isinstance(scope, ProductionAlertScope) else str(scope)
    rule_str = rule_type.value if isinstance(rule_type, AlertRuleType) else str(rule_type)
    target_str = str(target_resource).strip().lower() or "default"

    parts = [env_str, scope_str, rule_str, target_str]
    if tenant_id:
        validate_safe_identifier(tenant_id, "tenant_id")
        parts.append(tenant_id)
    return ":".join(parts)


@dataclass(frozen=True)
class AlertRule:
    """
    Definición inmutable de una regla de monitoreo / alerta en producción.
    """
    rule_type: AlertRuleType
    metric_type: MetricType
    window: MetricWindow
    severity: AlertSeverity
    threshold_value: float
    description: str
    comparison_operator: str = ">"  # ">", ">=", "<", "<=", "==", "!="
    scope: ProductionAlertScope = ProductionAlertScope.PLATFORM
    cooldown_seconds: int = 300
    min_sample_count: int = 1

    def __post_init__(self):
        if not isinstance(self.rule_type, AlertRuleType):
            object.__setattr__(self, "rule_type", AlertRuleType(self.rule_type))
        if not isinstance(self.metric_type, MetricType):
            object.__setattr__(self, "metric_type", MetricType(self.metric_type))
        if not isinstance(self.window, MetricWindow):
            object.__setattr__(self, "window", MetricWindow(self.window))
        if not isinstance(self.severity, AlertSeverity):
            object.__setattr__(self, "severity", AlertSeverity(self.severity))
        if not isinstance(self.scope, ProductionAlertScope):
            object.__setattr__(self, "scope", ProductionAlertScope(self.scope))
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative")
        if self.min_sample_count < 0:
            raise ValueError("min_sample_count must be non-negative")


@dataclass(frozen=True)
class ProductionAlertInstance:
    """
    Instancia inmutable de una alerta de producción activa, reconocida o resuelta.
    """
    alert_id: str
    rule_type: AlertRuleType
    severity: AlertSeverity
    state: AlertState
    environment: ApplicationEnvironment
    scope: ProductionAlertScope
    deduplication_key: str
    summary: str
    evidence: Mapping[str, Any]
    triggered_at: datetime
    updated_at: datetime
    target_resource: str = "platform"
    tenant_id: Optional[str] = None
    acknowledged_at: Optional[datetime] = None
    acknowledged_by: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    resolution_reason: Optional[str] = None
    checksum: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.alert_id, "alert_id")
        if not isinstance(self.rule_type, AlertRuleType):
            object.__setattr__(self, "rule_type", AlertRuleType(self.rule_type))
        if not isinstance(self.severity, AlertSeverity):
            object.__setattr__(self, "severity", AlertSeverity(self.severity))
        if not isinstance(self.state, AlertState):
            object.__setattr__(self, "state", AlertState(self.state))
        if not isinstance(self.environment, ApplicationEnvironment):
            object.__setattr__(self, "environment", resolve_environment(self.environment))
        if not isinstance(self.scope, ProductionAlertScope):
            object.__setattr__(self, "scope", ProductionAlertScope(self.scope))

        if self.triggered_at.tzinfo is None:
            object.__setattr__(self, "triggered_at", self.triggered_at.replace(tzinfo=timezone.utc))
        if self.updated_at.tzinfo is None:
            object.__setattr__(self, "updated_at", self.updated_at.replace(tzinfo=timezone.utc))
        if self.acknowledged_at and self.acknowledged_at.tzinfo is None:
            object.__setattr__(self, "acknowledged_at", self.acknowledged_at.replace(tzinfo=timezone.utc))
        if self.resolved_at and self.resolved_at.tzinfo is None:
            object.__setattr__(self, "resolved_at", self.resolved_at.replace(tzinfo=timezone.utc))

        if self.tenant_id is not None:
            validate_safe_identifier(self.tenant_id, "tenant_id")

        # Sanitizar y congelar evidencia
        sanitized = sanitize_security_data(dict(self.evidence))
        object.__setattr__(self, "evidence", deep_freeze(sanitized))

        # Checksum determinista
        payload = {
            "alert_id": self.alert_id,
            "rule_type": self.rule_type.value,
            "severity": self.severity.value,
            "state": self.state.value,
            "environment": self.environment.value,
            "scope": self.scope.value,
            "deduplication_key": self.deduplication_key,
            "summary": self.summary,
            "evidence": dict(self.evidence),
            "triggered_at": self.triggered_at,
            "updated_at": self.updated_at,
            "target_resource": self.target_resource,
            "tenant_id": self.tenant_id,
            "acknowledged_at": self.acknowledged_at,
            "acknowledged_by": self.acknowledged_by,
            "resolved_at": self.resolved_at,
            "resolved_by": self.resolved_by,
            "resolution_reason": self.resolution_reason,
        }
        computed = compute_alert_checksum(payload)
        object.__setattr__(self, "checksum", computed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "alert_id": self.alert_id,
            "rule_type": self.rule_type.value,
            "severity": self.severity.value,
            "state": self.state.value,
            "environment": self.environment.value,
            "scope": self.scope.value,
            "deduplication_key": self.deduplication_key,
            "summary": self.summary,
            "evidence": dict(self.evidence),
            "triggered_at": self.triggered_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "target_resource": self.target_resource,
            "tenant_id": self.tenant_id,
            "acknowledged_at": self.acknowledged_at.isoformat() if self.acknowledged_at else None,
            "acknowledged_by": self.acknowledged_by,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "resolved_by": self.resolved_by,
            "resolution_reason": self.resolution_reason,
            "checksum": self.checksum,
        }


@dataclass(frozen=True)
class AlertEvaluationResult:
    """
    Resultado de evaluar una regla de alerta en un instante dado.
    """
    rule_type: AlertRuleType
    status: AlertEvaluationStatus
    severity: AlertSeverity
    current_value: Optional[float]
    threshold_value: float
    window: MetricWindow
    is_unknown: bool
    summary: str
    evidence: Mapping[str, Any]
    evaluated_at: datetime
    cooldown_until: Optional[datetime] = None


class NotificationDeliveryStatus(str, Enum):
    """Estado de despacho en un canal de notificación."""
    SENT = "SENT"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class NotificationMessage:
    """
    Mensaje tipado y seguro para notificación operacional.
    """
    notification_id: str
    alert_id: str
    channel: str
    recipient: str
    subject: str
    body_text: str
    severity: AlertSeverity
    created_at: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.notification_id, "notification_id")
        validate_safe_identifier(self.alert_id, "alert_id")
        if not isinstance(self.severity, AlertSeverity):
            object.__setattr__(self, "severity", AlertSeverity(self.severity))
        if self.created_at.tzinfo is None:
            object.__setattr__(self, "created_at", self.created_at.replace(tzinfo=timezone.utc))
        # Sanitizar metadatos
        sanitized = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized))


@dataclass(frozen=True)
class NotificationResult:
    """
    Resultado inmutable de un intento de notificación.
    """
    delivery_id: str
    notification_id: str
    alert_id: str
    channel: str
    status: NotificationDeliveryStatus
    attempted_at: datetime
    error_message: Optional[str] = None
    execution_duration_ms: Optional[float] = None

    def __post_init__(self):
        validate_safe_identifier(self.delivery_id, "delivery_id")
        if not isinstance(self.status, NotificationDeliveryStatus):
            object.__setattr__(self, "status", NotificationDeliveryStatus(self.status))
        if self.attempted_at.tzinfo is None:
            object.__setattr__(self, "attempted_at", self.attempted_at.replace(tzinfo=timezone.utc))
