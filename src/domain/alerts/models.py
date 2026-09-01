"""
Modelos de dominio para Alertas Autónomas (Autonomous Alerts - Hito J.6).

Define:
- AlertRecord: Entidad inmutable de Alerta de Dominio.
- AlertType: Taxonomía de alertas soportadas en J.6.
- AlertSeverity: Niveles deterministas de severidad (INFO, WARNING, HIGH, CRITICAL).
- AlertStatus: Estado del ciclo de vida de la alerta (CREATED, SUPPRESSED, PROCESSED).
- AlertDeliveryStatus: Estado del intento de entrega (PENDING, DELIVERED, FAILED, SUPPRESSED, UNKNOWN).
- AlertDeliveryResult: Registro inmutable del resultado del intento de entrega por canal.

Límites estrictos:
- NO crea DecisionRecord ni Decisiones de negocio.
- NO ejecuta acciones en Marketplaces ni herramientas operativas.
- NO inicia misiones continuas (J.7).
- NO modifica PolicyEngine.
- NO contiene secretos ni PII no sanitizada.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Dict, List
import hashlib


class AlertType(str, Enum):
    """
    Taxonomía canónica de tipos de alertas para J.6.
    Derivada determinísticamente de hechos/eventos existentes.
    """
    OPPORTUNITY_DETECTED = "OPPORTUNITY_DETECTED"
    SIGNIFICANT_CHANGE = "SIGNIFICANT_CHANGE"
    SOURCE_FAILURE = "SOURCE_FAILURE"
    RISK_CHANGE = "RISK_CHANGE"
    SYSTEM_FAILURE = "SYSTEM_FAILURE"


class AlertSeverity(str, Enum):
    """
    Niveles de severidad deterministas para alertas.
    UNKNOWN no debe inferir CRITICAL; la incertidumbre se preserva.
    """
    INFO = "INFO"
    WARNING = "WARNING"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class AlertStatus(str, Enum):
    """
    Estado del ciclo de vida de la alerta a nivel dominio.
    """
    CREATED = "CREATED"
    SUPPRESSED = "SUPPRESSED"
    PROCESSED = "PROCESSED"


class AlertDeliveryStatus(str, Enum):
    """
    Estado del intento de despacho a través del puerto de entrega.
    """
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AlertDeliveryResult:
    """
    Registro inmutable del intento o resultado de entrega de una alerta a un canal.

    Límites:
    - NO guarda credenciales, tokens OAuth ni headers de autorización.
    - Preserva UNKNOWN si el adaptador/canal no confirma entrega fehaciente.
    """
    delivery_id: str
    alert_id: str
    channel: str
    status: AlertDeliveryStatus
    attempted_at: datetime
    correlation_id: str
    recipient: Optional[str] = None
    provider_reference: Optional[str] = None
    error_category: Optional[str] = None
    error_message: Optional[str] = None
    execution_duration_ms: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.delivery_id or not isinstance(self.delivery_id, str):
            raise ValueError("delivery_id must be a non-empty string")
        if not self.alert_id or not isinstance(self.alert_id, str):
            raise ValueError("alert_id must be a non-empty string")
        if not self.channel or not isinstance(self.channel, str):
            raise ValueError("channel must be a non-empty string")
        if not isinstance(self.status, AlertDeliveryStatus):
            object.__setattr__(self, "status", AlertDeliveryStatus(self.status))
        if self.attempted_at.tzinfo is None:
            raise ValueError("attempted_at must be timezone-aware (UTC)")
        if not self.correlation_id or not isinstance(self.correlation_id, str):
            raise ValueError("correlation_id must be a non-empty string")
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class AlertRecord:
    """
    Entidad inmutable de Dominio para una Alerta Autónoma (Hito J.6).
    Representa una notificación estructurada, explicable, auditable e idempotente
    derivada determinísticamente de un evento del sistema.

    Límites:
    - Una alerta INFORMA; no es una Decisión ni una Acción.
    - NO persistir secretos ni credenciales.
    - Replay-safe e idempotente por idempotency_key.
    """
    alert_id: str
    alert_type: AlertType
    severity: AlertSeverity
    status: AlertStatus
    subject_type: str
    subject_id: str
    title: str
    message: str
    event_id: str
    occurred_at: datetime
    created_at: datetime
    correlation_id: str
    causation_id: Optional[str] = None
    provenance: str = "SYSTEM"
    idempotency_key: str = ""
    evidence_reference: Optional[str] = None
    delivery_status: AlertDeliveryStatus = AlertDeliveryStatus.PENDING
    template_data: Mapping[str, Any] = field(default_factory=dict)
    channel_metadata: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.alert_id or not isinstance(self.alert_id, str):
            raise ValueError("alert_id must be a non-empty string")
        if not isinstance(self.alert_type, AlertType):
            object.__setattr__(self, "alert_type", AlertType(self.alert_type))
        if not isinstance(self.severity, AlertSeverity):
            object.__setattr__(self, "severity", AlertSeverity(self.severity))
        if not isinstance(self.status, AlertStatus):
            object.__setattr__(self, "status", AlertStatus(self.status))
        if not self.subject_type or not isinstance(self.subject_type, str):
            raise ValueError("subject_type must be a non-empty string")
        if not self.subject_id or not isinstance(self.subject_id, str):
            raise ValueError("subject_id must be a non-empty string")
        if not self.title or not isinstance(self.title, str):
            raise ValueError("title must be a non-empty string")
        if not self.message or not isinstance(self.message, str):
            raise ValueError("message must be a non-empty string")
        if not self.event_id or not isinstance(self.event_id, str):
            raise ValueError("event_id must be a non-empty string")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware (UTC)")
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (UTC)")
        if not self.correlation_id or not isinstance(self.correlation_id, str):
            raise ValueError("correlation_id must be a non-empty string")
        if not isinstance(self.delivery_status, AlertDeliveryStatus):
            object.__setattr__(self, "delivery_status", AlertDeliveryStatus(self.delivery_status))

        if not isinstance(self.template_data, MappingProxyType):
            object.__setattr__(self, "template_data", MappingProxyType(dict(self.template_data)))
        if not isinstance(self.channel_metadata, MappingProxyType):
            object.__setattr__(self, "channel_metadata", MappingProxyType(dict(self.channel_metadata)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

        if not self.idempotency_key:
            key_raw = f"{self.event_id}:{self.alert_type.value}:{self.subject_id}:{self.correlation_id}"
            generated_key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()
            object.__setattr__(self, "idempotency_key", generated_key)
