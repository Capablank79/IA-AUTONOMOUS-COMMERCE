"""
Domain models for N.11 — Emergency Stop (Transversal N — Security, Governance & Safety).

Responde a la pregunta fundamental de gobernanza:
"¿Puede el sistema bloquear inmediatamente nuevas acciones sensibles/externas cuando una condición de emergencia exige detener la autonomía?"

Principios N.11:
- Control superior de ejecución: Precede inmediatamente al execution boundary físico.
- Zero physical calls cuando un stop aplicable está ACTIVE o en estado UNKNOWN / corrupto (Fail-Safe).
- No destructivo: No borra órdenes, misiones, aprobaciones, secretos ni historial de auditoría.
- Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
- Scopes jerárquicos deterministas: GLOBAL > MARKETPLACE > ACCOUNT > MISSION > TOOL > ACTION_TYPE.
- Integración N.1/N.2/N.3/N.4 para autorización de activación y desactivación.
- Soporte de expiración temporal determinista vía ClockPort (K.7).
- Cero almacenamiento de secretos, PII innecesaria o Chain-of-Thought (K.2).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Sequence, Dict, Union

from src.domain.security.models import (
    sanitize_security_data,
    deep_freeze,
    validate_safe_identifier,
)


class EmergencyStopState(str, Enum):
    """
    Estados canónicos del ciclo de vida de un Emergency Stop (N.11).
    """
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class EmergencyStopScope(str, Enum):
    """
    Scopes canónicos jerárquicos y deterministas para N.11.
    """
    GLOBAL = "GLOBAL"
    MARKETPLACE = "MARKETPLACE"
    ACCOUNT = "ACCOUNT"
    MISSION = "MISSION"
    TOOL = "TOOL"
    ACTION_TYPE = "ACTION_TYPE"


class EmergencyStopDecisionStatus(str, Enum):
    """
    Decisión canónica resultante de la evaluación de Emergency Stop (N.11).
    """
    ALLOW_EXECUTION = "ALLOW_EXECUTION"
    BLOCK_EXECUTION = "BLOCK_EXECUTION"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class EmergencyStopReasonCode(str, Enum):
    """
    Códigos de razón canónicos y estructurados para N.11.
    """
    # Decisiones
    NO_ACTIVE_STOP = "NO_ACTIVE_STOP"
    GLOBAL_STOP_ACTIVE = "GLOBAL_STOP_ACTIVE"
    MARKETPLACE_STOP_ACTIVE = "MARKETPLACE_STOP_ACTIVE"
    ACCOUNT_STOP_ACTIVE = "ACCOUNT_STOP_ACTIVE"
    MISSION_STOP_ACTIVE = "MISSION_STOP_ACTIVE"
    TOOL_STOP_ACTIVE = "TOOL_STOP_ACTIVE"
    ACTION_TYPE_STOP_ACTIVE = "ACTION_TYPE_STOP_ACTIVE"
    READ_ONLY_PERMITTED_BY_POLICY = "READ_ONLY_PERMITTED_BY_POLICY"
    STOP_EXPIRED = "STOP_EXPIRED"

    # Activación / Desactivación
    MANUAL_OPERATOR_HALT = "MANUAL_OPERATOR_HALT"
    SECURITY_INCIDENT = "SECURITY_INCIDENT"
    ANOMALOUS_AUTONOMOUS_BEHAVIOR = "ANOMALOUS_AUTONOMOUS_BEHAVIOR"
    FINANCIAL_EXPOSURE_RISK = "FINANCIAL_EXPOSURE_RISK"
    SYSTEM_MAINTENANCE = "SYSTEM_MAINTENANCE"
    AUTHORIZED_DEACTIVATION = "AUTHORIZED_DEACTIVATION"
    IDEMPOTENT_ACTIVATION = "IDEMPOTENT_ACTIVATION"
    IDEMPOTENT_DEACTIVATION = "IDEMPOTENT_DEACTIVATION"

    # Seguridad / RBAC / Fallos
    UNAUTHORIZED_ACTIVATION_ATTEMPT = "UNAUTHORIZED_ACTIVATION_ATTEMPT"
    UNAUTHORIZED_DEACTIVATION_ATTEMPT = "UNAUTHORIZED_DEACTIVATION_ATTEMPT"
    UNAUTHENTICATED_REQUESTER = "UNAUTHENTICATED_REQUESTER"
    MISSING_PRINCIPAL_CONTEXT = "MISSING_PRINCIPAL_CONTEXT"
    FAIL_SAFE_STORE_CORRUPTION = "FAIL_SAFE_STORE_CORRUPTION"
    FAIL_SAFE_UNKNOWN_STATE = "FAIL_SAFE_UNKNOWN_STATE"
    FAIL_SAFE_EVALUATION_ERROR = "FAIL_SAFE_EVALUATION_ERROR"
    RECORD_NOT_FOUND = "RECORD_NOT_FOUND"
    CONFLICTING_STOP_STATE = "CONFLICTING_STOP_STATE"


def compute_emergency_stop_checksum(data: Dict[str, Any]) -> str:
    """Calcula un checksum SHA-256 canónico y determinista para integridad de registros."""
    canonical_payload = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EmergencyStopRecord:
    """
    Registro inmutable y persistente de una orden o estado de Emergency Stop.
    """
    stop_id: str
    scope: EmergencyStopScope
    state: EmergencyStopState
    reason_code: EmergencyStopReasonCode
    reason_details: str
    activated_by_identity_id: str
    activated_at: datetime
    target_id: Optional[str] = None
    expires_at: Optional[datetime] = None
    deactivated_by_identity_id: Optional[str] = None
    deactivated_at: Optional[datetime] = None
    policy_version: str = "1.0.0"
    allow_read_only: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.stop_id, "stop_id")
        if not isinstance(self.scope, EmergencyStopScope):
            object.__setattr__(self, "scope", EmergencyStopScope(self.scope))
        if not isinstance(self.state, EmergencyStopState):
            object.__setattr__(self, "state", EmergencyStopState(self.state))
        if not isinstance(self.reason_code, EmergencyStopReasonCode):
            object.__setattr__(self, "reason_code", EmergencyStopReasonCode(self.reason_code))
        if not self.activated_by_identity_id or not isinstance(self.activated_by_identity_id, str):
            raise ValueError("activated_by_identity_id must be a non-empty string")
        if self.target_id is not None:
            validate_safe_identifier(self.target_id, "target_id")

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        # Checksum determinista si no viene provisto
        if not self.checksum:
            payload = {
                "stop_id": self.stop_id,
                "scope": self.scope.value,
                "target_id": self.target_id,
                "state": self.state.value,
                "reason_code": self.reason_code.value,
                "reason_details": self.reason_details,
                "activated_by_identity_id": self.activated_by_identity_id,
                "activated_at": self.activated_at.isoformat() if self.activated_at else None,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "deactivated_by_identity_id": self.deactivated_by_identity_id,
                "deactivated_at": self.deactivated_at.isoformat() if self.deactivated_at else None,
                "policy_version": self.policy_version,
                "allow_read_only": self.allow_read_only,
            }
            computed = compute_emergency_stop_checksum(payload)
            object.__setattr__(self, "checksum", computed)

    def is_active_at(self, current_time: datetime) -> bool:
        """Determina si este registro de stop está activo en un instante dado."""
        if self.state != EmergencyStopState.ACTIVE:
            return False
        if self.expires_at is not None:
            # Asegurar timezone awareness
            cur = current_time if current_time.tzinfo else current_time.replace(tzinfo=timezone.utc)
            exp = self.expires_at if self.expires_at.tzinfo else self.expires_at.replace(tzinfo=timezone.utc)
            if cur >= exp:
                return False
        return True


@dataclass(frozen=True)
class EmergencyStopEvaluationContext:
    """
    Contexto inmutable de una acción o solicitud evaluada contra Emergency Stop.
    """
    action_name: str
    target_resource: Optional[str] = None
    marketplace: Optional[str] = None
    account_id: Optional[str] = None
    mission_id: Optional[str] = None
    tool_name: Optional[str] = None
    action_type: Optional[str] = None
    is_read_only: bool = False
    is_external_side_effect: bool = True
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.action_name or not isinstance(self.action_name, str):
            raise ValueError("action_name must be a non-empty string")
        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))


@dataclass(frozen=True)
class EmergencyStopDecision:
    """
    Decisión inmutable y determinista de Emergency Stop (N.11).
    """
    decision_id: str
    decision_status: EmergencyStopDecisionStatus
    reason_code: EmergencyStopReasonCode
    reason_details: str
    evaluated_at: datetime
    active_record_ids: Tuple[str, ...] = ()
    applied_scope: Optional[EmergencyStopScope] = None
    applied_target_id: Optional[str] = None
    policy_version: str = "1.0.0"
    correlation_id: Optional[str] = None
    mission_id: Optional[str] = None
    checksum: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.decision_id, "decision_id")
        if not isinstance(self.decision_status, EmergencyStopDecisionStatus):
            object.__setattr__(self, "decision_status", EmergencyStopDecisionStatus(self.decision_status))
        if not isinstance(self.reason_code, EmergencyStopReasonCode):
            object.__setattr__(self, "reason_code", EmergencyStopReasonCode(self.reason_code))
        if self.applied_scope is not None and not isinstance(self.applied_scope, EmergencyStopScope):
            object.__setattr__(self, "applied_scope", EmergencyStopScope(self.applied_scope))

        sanitized_meta = sanitize_security_data(dict(self.metadata))
        object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if not self.checksum:
            payload = {
                "decision_id": self.decision_id,
                "decision_status": self.decision_status.value,
                "reason_code": self.reason_code.value,
                "reason_details": self.reason_details,
                "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
                "active_record_ids": list(self.active_record_ids),
                "applied_scope": self.applied_scope.value if self.applied_scope else None,
                "applied_target_id": self.applied_target_id,
                "policy_version": self.policy_version,
                "correlation_id": self.correlation_id,
                "mission_id": self.mission_id,
            }
            computed = compute_emergency_stop_checksum(payload)
            object.__setattr__(self, "checksum", computed)

    @property
    def is_executable(self) -> bool:
        """Retorna True solo si la decisión es ALLOW_EXECUTION."""
        return self.decision_status == EmergencyStopDecisionStatus.ALLOW_EXECUTION

    @property
    def is_blocked(self) -> bool:
        """Retorna True si la decisión es BLOCK_EXECUTION, UNKNOWN o ERROR (Fail-Safe)."""
        return not self.is_executable
