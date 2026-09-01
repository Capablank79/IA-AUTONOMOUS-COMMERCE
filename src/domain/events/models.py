"""
Modelos de dominio para el Bus de Eventos y Procesamiento de Eventos (Event Bus - Hito J.5).

Define:
- EventRecord: Modelo inmutable de evento de dominio / aplicación.
- EventType: Tipos canónicos de eventos mínimos requeridos para integración.
- DeliveryStatus: Estado de entrega a consumidores.
- DeliveryRecord: Registro inmutable de intento/resultado de entrega a un consumidor específico.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Union
import hashlib
import json


class EventType(str, Enum):
    """
    Taxonomía canónica de tipos de eventos de dominio/aplicación para J.5.
    Mantiene separación FACT != DECISION != COMMAND != ACTION.
    """
    MARKET_OBSERVATION_CREATED = "MARKET_OBSERVATION_CREATED"
    OPPORTUNITY_DETECTED = "OPPORTUNITY_DETECTED"
    CHANGE_DETECTED = "CHANGE_DETECTED"


class DeliveryStatus(str, Enum):
    """
    Estado de entrega de un evento a un suscriptor/consumidor.
    """
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class EventRecord:
    """
    Entidad inmutable de Dominio para un Evento (Hito J.5).
    Representa un hecho atómico ocurrido en el sistema.

    Límites:
    - NO crea DecisionRecord.
    - NO ejecuta acciones comerciales.
    - NO genera alertas distribuidas (J.6).
    - NO inicia continuous missions (J.7).
    - NO modifica PolicyEngine.
    """
    event_id: str
    event_type: EventType
    subject_type: str
    subject_id: str
    occurred_at: datetime
    recorded_at: datetime
    correlation_id: str
    causation_id: Optional[str] = None
    provenance: str = "SYSTEM"
    idempotency_key: str = ""
    schema_version: str = "1.0.0"
    payload_reference: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.event_id or not isinstance(self.event_id, str):
            raise ValueError("event_id must be a non-empty string")
        if not isinstance(self.event_type, EventType):
            try:
                object.__setattr__(self, "event_type", EventType(self.event_type))
            except Exception as e:
                raise ValueError(f"invalid event_type: {self.event_type}") from e
        if not self.subject_type or not isinstance(self.subject_type, str):
            raise ValueError("subject_type must be a non-empty string")
        if not self.subject_id or not isinstance(self.subject_id, str):
            raise ValueError("subject_id must be a non-empty string")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware (UTC)")
        if self.recorded_at.tzinfo is None:
            raise ValueError("recorded_at must be timezone-aware (UTC)")
        if not self.correlation_id or not isinstance(self.correlation_id, str):
            raise ValueError("correlation_id must be a non-empty string")

        if not isinstance(self.payload, MappingProxyType):
            object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

        if not self.idempotency_key:
            # Deterministic idempotency key derivation
            key_raw = f"{self.event_type.value}:{self.subject_type}:{self.subject_id}:{self.occurred_at.isoformat()}:{self.correlation_id}:{self.causation_id or ''}"
            generated_key = hashlib.sha256(key_raw.encode("utf-8")).hexdigest()
            object.__setattr__(self, "idempotency_key", generated_key)


@dataclass(frozen=True)
class DeliveryRecord:
    """
    Registro inmutable de un intento o resultado de entrega de un evento a un handler específico.
    Permite idempotencia por handler (event_id + handler_id) y aislamiento de fallos.
    """
    delivery_id: str
    event_id: str
    handler_id: str
    status: DeliveryStatus
    attempt_count: int
    first_attempted_at: datetime
    last_attempted_at: datetime
    error_message: Optional[str] = None
    execution_duration_ms: Optional[float] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.delivery_id or not isinstance(self.delivery_id, str):
            raise ValueError("delivery_id must be a non-empty string")
        if not self.event_id or not isinstance(self.event_id, str):
            raise ValueError("event_id must be a non-empty string")
        if not self.handler_id or not isinstance(self.handler_id, str):
            raise ValueError("handler_id must be a non-empty string")
        if not isinstance(self.status, DeliveryStatus):
            object.__setattr__(self, "status", DeliveryStatus(self.status))
        if self.first_attempted_at.tzinfo is None:
            raise ValueError("first_attempted_at must be timezone-aware (UTC)")
        if self.last_attempted_at.tzinfo is None:
            raise ValueError("last_attempted_at must be timezone-aware (UTC)")
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
