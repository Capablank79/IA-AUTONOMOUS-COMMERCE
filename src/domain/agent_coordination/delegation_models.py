"""
Modelos de dominio para Dynamic Delegation (Hito R.5 — Advanced Autonomy).

Define:
- DelegationReason: Motivos válidos para autorizar una delegación dinámica.
- DelegationPolicy: Límites y reglas para evitar loops infinitos (ping-pong) y acotar reasignaciones.
- DelegationRequest: Solicitud inmutable de delegación.
- DelegationDecision: Resultado de la evaluación de una delegación (aprobada/rechazada/bloqueada).
- DelegationRecord: Registro inmutable y auditable de una delegación (transferida o denegada).

Principios R.5:
1. Reasignación dinámica de tareas mutables ante fallos de disponibilidad, técnicos o de límite de recursos.
2. Single Owner preservation: Transferencia atómica, nunca dos running.
3. Compatibility: El nuevo agente debe satisfacer la capability requerida, tools y tenant boundaries (R.3).
4. No evasion: NO se delega para evadir POLICY_DENIED ni EMERGENCY_STOP.
5. Budget & Cost Continuity: El presupuesto no se reinicia; los límites son globales a la tarea.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Mapping, Optional, Any, Tuple
import hashlib
import json

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)


class DelegationReason(str, Enum):
    """
    Motivos explícitos y válidos que justifican una solicitud de delegación.
    """
    AGENT_UNAVAILABLE = "AGENT_UNAVAILABLE"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    CAPABILITY_MISMATCH_DISCOVERED = "CAPABILITY_MISMATCH_DISCOVERED"
    BUDGET_INELIGIBLE = "BUDGET_INELIGIBLE"
    RATE_LIMITED = "RATE_LIMITED"
    COORDINATION_CONFLICT_RESOLVED = "COORDINATION_CONFLICT_RESOLVED"
    MANUAL_SAFE_REASSIGNMENT = "MANUAL_SAFE_REASSIGNMENT"


@dataclass(frozen=True)
class DelegationPolicy:
    """
    Políticas de contorno y límites de seguridad para delegaciones de tareas.
    Evita ping-pongs infinitos y transferencias no autorizadas.
    """
    max_delegations_per_task: int = 3
    max_delegations_per_mission: int = 10
    allow_fallback_to_degraded: bool = False
    fail_fast_on_policy_denied: bool = True

    def __post_init__(self):
        if self.max_delegations_per_task < 0:
            raise ValueError("max_delegations_per_task cannot be negative")
        if self.max_delegations_per_mission < 0:
            raise ValueError("max_delegations_per_mission cannot be negative")


class DelegationDecisionStatus(str, Enum):
    """
    Estados posibles de una evaluación de delegación.
    """
    APPROVED = "APPROVED"
    REJECTED_CAPABILITY = "REJECTED_CAPABILITY"
    REJECTED_POLICY = "REJECTED_POLICY"
    REJECTED_BOUNDS = "REJECTED_BOUNDS"
    REJECTED_SECURITY = "REJECTED_SECURITY"


@dataclass(frozen=True)
class DelegationRequest:
    """
    Solicitud inmutable para delegar una CoordinationTask.
    """
    tenant_id: str
    mission_id: str
    task_id: str
    from_agent_id: str
    required_capability: str
    reason: DelegationReason
    current_attempt: int
    requested_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    correlation_id: str = ""
    idempotency_key: str = field(default="", init=False)

    def __post_init__(self):
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.task_id, "task_id")
        validate_safe_identifier(self.from_agent_id, "from_agent_id")
        validate_safe_identifier(self.required_capability, "required_capability")
        if not isinstance(self.reason, DelegationReason):
            object.__setattr__(self, "reason", DelegationReason(self.reason))
        if self.current_attempt < 1:
            raise ValueError("current_attempt must be >= 1")

        if not self.idempotency_key:
            raw = f"{self.tenant_id}:{self.task_id}:{self.current_attempt}:{self.reason.value}"
            object.__setattr__(self, "idempotency_key", hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24])


@dataclass(frozen=True)
class DelegationDecision:
    """
    Resultado determinista de la selección de un nuevo agente (o del rechazo por incompatibilidad/límites).
    """
    request: DelegationRequest
    status: DelegationDecisionStatus
    to_agent_id: Optional[str] = None
    rejection_reason: Optional[str] = None
    decided_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.status, DelegationDecisionStatus):
            object.__setattr__(self, "status", DelegationDecisionStatus(self.status))
        if self.status == DelegationDecisionStatus.APPROVED:
            if not self.to_agent_id:
                raise ValueError("APPROVED decision must have a to_agent_id")
            validate_safe_identifier(self.to_agent_id, "to_agent_id")
        else:
            if not self.rejection_reason:
                raise ValueError("REJECTED decision must have a rejection_reason")


@dataclass(frozen=True)
class DelegationRecord:
    """
    Historia inmutable de una delegación y transferencia de propiedad ejecutada (o bloqueada).
    """
    delegation_id: str
    decision: DelegationDecision
    assignment_version: int
    transferred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    budget_state: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.delegation_id, "delegation_id")
        if self.assignment_version < 2:
            raise ValueError("Reassignment implies assignment_version >= 2")
        object.__setattr__(self, "budget_state", deep_freeze(sanitize_security_data(self.budget_state)))
