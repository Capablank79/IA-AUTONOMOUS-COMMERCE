"""
Modelos de dominio para el Registro de Auditoría (Audit Trail - Hito K.1).

Define:
- AuditActor: Tipos y actores canónicos auditables en el sistema.
- AuditRecordType: Taxonomía canónica de hechos auditables.
- AuditRecord: Entidad de dominio inmutable para un hecho auditable.
- MissionAuditTimeline: Agregado inmutable que reconstruye la cronología y causalidad de una misión.

Principios:
- Inmutabilidad estricta (frozen=True, MappingProxyType).
- Audit Trail responde: WHO, DID WHAT, TO WHAT, WHEN, WHY (causal origin), WITH WHAT RESULT.
- Cero duplicación de Business Memory o Event Store.
- Cero almacenamiento de secretos/PII (sanitización recursiva).
- Preservación determinista de UNKNOWN/FAILED.
- No almacena chain-of-thought, reasoning privado ni prompts de agentes (reservado para K.2 Agent Trace).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Union
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
}


def _sanitize_metadata_recursively(val: Any) -> Any:
    """Sanitiza recursivamente cualquier estructura de metadatos para eliminar secretos."""
    if isinstance(val, (dict, MappingProxyType)):
        cleaned = {}
        for k, v in val.items():
            k_str = str(k).lower()
            if any(s in k_str for s in SENSITIVE_KEYS):
                cleaned[str(k)] = "[REDACTED]"
            else:
                cleaned[str(k)] = _sanitize_metadata_recursively(v)
        return cleaned
    if isinstance(val, (list, tuple)):
        return [_sanitize_metadata_recursively(v) for v in val]
    return val


class AuditActorType(str, Enum):
    """
    Taxonomía de actores canónicos del sistema según el Roadmap K.1.
    """
    SYSTEM = "SYSTEM"
    AGENT = "AGENT"
    USER = "USER"
    POLICY_ENGINE = "POLICY_ENGINE"
    ACTION_EXECUTOR = "ACTION_EXECUTOR"
    SCHEDULER = "SCHEDULER"
    EXTERNAL_TOOL = "EXTERNAL_TOOL"
    MARKETPLACE = "MARKETPLACE"


@dataclass(frozen=True)
class AuditActor:
    """
    Identidad inmutable de un actor de auditoría.
    No almacena PII innecesaria.
    """
    actor_type: AuditActorType
    actor_id: str = "system"
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.actor_type, AuditActorType):
            try:
                object.__setattr__(self, "actor_type", AuditActorType(self.actor_type))
            except Exception as e:
                raise ValueError(f"Invalid actor_type: {self.actor_type}") from e
        if not self.actor_id or not isinstance(self.actor_id, str):
            raise ValueError("actor_id must be a non-empty string")
        if not isinstance(self.details, MappingProxyType):
            object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


class AuditRecordType(str, Enum):
    """
    Taxonomía canónica de hechos auditables mínimos y transversales requeridos para K.1.
    """
    MISSION_CREATED = "MISSION_CREATED"
    MISSION_STATE_CHANGED = "MISSION_STATE_CHANGED"
    MARKET_OBSERVATION_CREATED = "MARKET_OBSERVATION_CREATED"
    EVIDENCE_RECORDED = "EVIDENCE_RECORDED"
    DECISION_CREATED = "DECISION_CREATED"
    POLICY_EVALUATED = "POLICY_EVALUATED"
    ACTION_CREATED = "ACTION_CREATED"
    ACTION_EXECUTED = "ACTION_EXECUTED"
    RESULT_RECORDED = "RESULT_RECORDED"
    
    # Eventos de integración de Hito J (cuando aplique)
    OPPORTUNITY_DETECTED = "OPPORTUNITY_DETECTED"
    CHANGE_DETECTED = "CHANGE_DETECTED"
    ALERT_CREATED = "ALERT_CREATED"
    CONTINUOUS_CYCLE = "CONTINUOUS_CYCLE"


@dataclass(frozen=True)
class AuditRecord:
    """
    Entidad de dominio inmutable para un Registro de Auditoría (AuditRecord - K.1).
    Representa un hecho atómico auditable e incontrovertible ocurrido en el sistema.

    Límites:
    - NO contiene chain-of-thought interno de LLMs (reservado a K.2 Agent Trace).
    - NO duplica las entidades completas de Business Memory (guarda referencias estables).
    - Inmutable: cualquier cambio de estado posterior genera un nuevo AuditRecord.
    - Append-only.
    """
    audit_id: str
    record_type: AuditRecordType
    occurred_at: datetime
    actor: AuditActor
    subject_type: str
    subject_id: str
    action_or_operation: str
    status: str
    correlation_id: str
    causation_id: Optional[str] = None
    mission_id: Optional[str] = None
    entity_reference: Optional[str] = None
    evidence_reference: Optional[str] = None
    provenance: str = "SYSTEM"
    idempotency_key: str = ""
    checksum: Optional[str] = None
    schema_version: str = "1.0.0"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.audit_id or not isinstance(self.audit_id, str):
            raise ValueError("audit_id must be a non-empty string")
        if not isinstance(self.record_type, AuditRecordType):
            try:
                object.__setattr__(self, "record_type", AuditRecordType(self.record_type))
            except Exception as e:
                raise ValueError(f"Invalid record_type: {self.record_type}") from e
        if not isinstance(self.actor, AuditActor):
            raise ValueError("actor must be an instance of AuditActor")
        if not self.subject_type or not isinstance(self.subject_type, str):
            raise ValueError("subject_type must be a non-empty string")
        if not self.subject_id or not isinstance(self.subject_id, str):
            raise ValueError("subject_id must be a non-empty string")
        if not self.action_or_operation or not isinstance(self.action_or_operation, str):
            raise ValueError("action_or_operation must be a non-empty string")
        if not self.status or not isinstance(self.status, str):
            raise ValueError("status must be a non-empty string")
        if not self.correlation_id or not isinstance(self.correlation_id, str):
            raise ValueError("correlation_id must be a non-empty string")
        if self.occurred_at.tzinfo is None:
            raise ValueError("occurred_at must be timezone-aware (UTC)")

        # Auto-generar idempotency_key determinista si no se proveyó
        if not self.idempotency_key:
            key_src = f"{self.record_type.value}::{self.subject_type}::{self.subject_id}::{self.action_or_operation}::{self.status}::{self.correlation_id}::{self.causation_id or ''}"
            object.__setattr__(self, "idempotency_key", key_src)

        # Sanitizar y congelar metadata
        sanitized_meta = _sanitize_metadata_recursively(dict(self.metadata))
        object.__setattr__(self, "metadata", MappingProxyType(sanitized_meta))

        # Calcular checksum de integridad determinista
        if not self.checksum:
            payload_for_hash = {
                "audit_id": self.audit_id,
                "record_type": self.record_type.value,
                "occurred_at": self.occurred_at.isoformat(),
                "actor_type": self.actor.actor_type.value,
                "actor_id": self.actor.actor_id,
                "subject_type": self.subject_type,
                "subject_id": self.subject_id,
                "action_or_operation": self.action_or_operation,
                "status": self.status,
                "correlation_id": self.correlation_id,
                "causation_id": self.causation_id,
                "mission_id": self.mission_id,
                "entity_reference": self.entity_reference,
                "evidence_reference": self.evidence_reference,
                "provenance": self.provenance,
                "idempotency_key": self.idempotency_key,
                "schema_version": self.schema_version,
            }
            digest = hashlib.sha256(json.dumps(payload_for_hash, sort_keys=True).encode("utf-8")).hexdigest()
            object.__setattr__(self, "checksum", digest)


@dataclass(frozen=True)
class MissionAuditTimeline:
    """
    Agregado inmutable que contiene la reconstrucción cronológica y causal completa
    de una misión auditable.
    """
    mission_id: str
    correlation_id: str
    records: Tuple[AuditRecord, ...] = field(default_factory=tuple)
    reconstructed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.records, tuple):
            object.__setattr__(self, "records", tuple(self.records))

    @property
    def is_empty(self) -> bool:
        return len(self.records) == 0

    @property
    def record_types_present(self) -> Tuple[AuditRecordType, ...]:
        return tuple(r.record_type for r in self.records)
