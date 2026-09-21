"""
Modelos de dominio para Long-running Missions (Hito R.6 — Advanced Autonomy).

Define:
- CheckpointStatus: Estados del ciclo de vida del checkpoint (VALID, CORRUPT, STALE, DEPRECATED).
- ResumeDecisionStatus: Estados canónicos de la decisión de reanudación (GRANTED, DENIED_POLICY, DENIED_TERMINAL, DENIED_EMERGENCY_STOP, DENIED_BUDGET, DENIED_CORRUPT, DENIED_LOCK_CONFLICT, DENIED_TENANT_MISMATCH, DENIED_PLAN_INCOMPATIBLE).
- HeartbeatRecord: Registro de latido de actividad periódica emitido por un worker activo.
- LeaseState: Estado canónico del lease de ejecución de la misión/worker.
- ResumeDecision: Contrato canónico e inmutable del resultado de evaluar una solicitud de resume.
- MissionCheckpoint: Agregado canónico inmutable de estado persistente durable.
- CheckpointIntegrityError / StaleWorkerError / StaleCheckpointError: Excepciones canónicas de dominio.

Principios R.6:
1. Responde a: "¿Puede una misión de larga duración pausar, persistir su progreso y reanudarse de forma segura tras reinicio/desconexión sin duplicar efectos, perder estado o violar budgets/policies?".
2. REUSE > EXTEND > CREATE: Reutiliza el ciclo de vida y agregados de Mission, R.1 Planning, R.2 Sub-missions, R.3 Specialists, R.4 Coordination, R.5 Delegation.
3. SINGLE MUTABLE OWNER & LEASE: Control estricto de concurrencia y expiración determinista vía ClockPort.
4. COMPLETED WORK IMMUTABILITY: Los pasos y submisiones completadas no se reejecutan.
5. SIDE EFFECT SAFETY & IDEMPOTENCY: Se valida la evidencia previa antes de reanudar cualquier tarea con efecto colateral.
6. POLICY REVALIDATION: Revalidación activa de políticas de gobernanza (N.3/N.6/N.7/N.8/N.11) al reanudar.
7. BUDGET/QUOTA CONTINUITY: El presupuesto consumido/reservado se mantiene; restart != fresh budget.
8. ATOMICITY & CHECKSUM: Versionado monotónico optimista y checksum SHA-256 para integridad física.
9. ZERO-COT & SENSITIVE DATA DEFENSE: Sanitización estricta sin secretos, tokens ni reasoning privado (N.9/K.2).
10. TENANT ISOLATION: Aislamiento estricto por tenant_id (CrossTenantGuard).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence, List

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionStatus,
    MissionPriority,
)
from src.domain.planning.models import (
    PlanBudget,
    StepStatus,
)


class CheckpointStatus(str, Enum):
    """
    Estados canónicos del ciclo de vida de un MissionCheckpoint.
    """
    VALID = "VALID"
    CORRUPT = "CORRUPT"
    STALE = "STALE"
    DEPRECATED = "DEPRECATED"


class ResumeDecisionStatus(str, Enum):
    """
    Estados canónicos de decisión al intentar reanudar una misión persistida.
    """
    GRANTED = "GRANTED"
    DENIED_POLICY = "DENIED_POLICY"
    DENIED_TERMINAL = "DENIED_TERMINAL"
    DENIED_EMERGENCY_STOP = "DENIED_EMERGENCY_STOP"
    DENIED_BUDGET = "DENIED_BUDGET"
    DENIED_CORRUPT = "DENIED_CORRUPT"
    DENIED_LOCK_CONFLICT = "DENIED_LOCK_CONFLICT"
    DENIED_TENANT_MISMATCH = "DENIED_TENANT_MISMATCH"
    DENIED_PLAN_INCOMPATIBLE = "DENIED_PLAN_INCOMPATIBLE"


class LeaseStatus(str, Enum):
    """
    Estados canónicos del lease de ejecución de un worker sobre una misión.
    """
    ACQUIRED = "ACQUIRED"
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    RELEASED = "RELEASED"
    REVOKED = "REVOKED"


@dataclass(frozen=True)
class HeartbeatRecord:
    """
    Registro inmutable de latido emitido periódicamente por el worker activo.
    """
    mission_id: str
    worker_id: str
    tenant_id: str
    assignment_version: int
    last_seen_at: datetime
    lease_id: str
    lease_until: datetime
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.worker_id, "worker_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.lease_id, "lease_id")
        if self.assignment_version < 1:
            raise ValueError("assignment_version must be >= 1")
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))


@dataclass(frozen=True)
class LeaseState:
    """
    Estado inmutable del lease de ejecución para control de concurrencia y single owner.
    """
    lease_id: str
    mission_id: str
    worker_id: str
    tenant_id: str
    status: LeaseStatus
    acquired_at: datetime
    expires_at: datetime
    assignment_version: int = 1
    heartbeat_count: int = 0
    last_heartbeat_at: Optional[datetime] = None

    def __post_init__(self):
        validate_safe_identifier(self.lease_id, "lease_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.worker_id, "worker_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.assignment_version < 1:
            raise ValueError("assignment_version must be >= 1")

    def is_expired(self, current_time: datetime) -> bool:
        return current_time >= self.expires_at


@dataclass(frozen=True)
class ResumeDecision:
    """
    Resultado inmutable de la evaluación de reanudación (Resume) de una misión.
    """
    status: ResumeDecisionStatus
    allowed: bool
    mission_id: str
    tenant_id: str
    reason: str
    checkpoint_version: int
    evaluated_at: datetime
    active_worker_id: Optional[str] = None
    lease_id: Optional[str] = None
    revalidated_policies: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.active_worker_id:
            validate_safe_identifier(self.active_worker_id, "active_worker_id")
        if self.lease_id:
            validate_safe_identifier(self.lease_id, "lease_id")
        object.__setattr__(self, "revalidated_policies", tuple(self.revalidated_policies))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))


@dataclass(frozen=True)
class StepCheckpointData:
    """
    Estado compacto y seguro de un paso de ejecución dentro del checkpoint (R.1 / R.4).
    """
    step_id: str
    status: str
    assigned_agent_id: Optional[str] = None
    assignment_version: int = 1
    outputs: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    is_side_effecting: bool = False
    side_effect_committed: bool = False
    attempt: int = 1

    def __post_init__(self):
        validate_safe_identifier(self.step_id, "step_id")
        if self.assigned_agent_id:
            validate_safe_identifier(self.assigned_agent_id, "assigned_agent_id")
        object.__setattr__(self, "outputs", deep_freeze(sanitize_security_data(self.outputs)))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))


@dataclass(frozen=True)
class MissionCheckpoint:
    """
    Agregado canónico e inmutable que representa un Checkpoint Durable de una misión de larga duración.
    """
    checkpoint_id: str
    tenant_id: str
    mission_id: str
    mission_type: str
    mission_status: MissionStatus
    checkpoint_version: int
    created_at: datetime
    plan_id: Optional[str] = None
    plan_version: int = 1
    root_mission_id: Optional[str] = None
    parent_mission_id: Optional[str] = None
    depth: int = 0
    completed_steps: Tuple[str, ...] = field(default_factory=tuple)
    active_steps: Tuple[str, ...] = field(default_factory=tuple)
    pending_steps: Tuple[str, ...] = field(default_factory=tuple)
    step_records: Mapping[str, StepCheckpointData] = field(default_factory=dict)
    sub_mission_refs: Tuple[str, ...] = field(default_factory=tuple)
    coordination_session_id: Optional[str] = None
    coordination_state_refs: Mapping[str, Any] = field(default_factory=dict)
    assignment_versions: Mapping[str, int] = field(default_factory=dict)
    current_worker_id: Optional[str] = None
    lease_id: Optional[str] = None
    budget_consumed_tokens: int = 0
    budget_consumed_cost: Decimal = Decimal("0.00")
    budget_remaining_tokens: Optional[int] = None
    budget_remaining_cost: Optional[Decimal] = None
    quota_consumed: int = 0
    last_event_type: Optional[str] = None
    correlation_id: str = ""
    causation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    status: CheckpointStatus = CheckpointStatus.VALID
    checksum: str = field(default="", init=False)

    def __post_init__(self):
        validate_safe_identifier(self.checkpoint_id, "checkpoint_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        if self.plan_id:
            validate_safe_identifier(self.plan_id, "plan_id")
        if self.root_mission_id:
            validate_safe_identifier(self.root_mission_id, "root_mission_id")
        if self.parent_mission_id:
            validate_safe_identifier(self.parent_mission_id, "parent_mission_id")
        if self.coordination_session_id:
            validate_safe_identifier(self.coordination_session_id, "coordination_session_id")
        if self.current_worker_id:
            validate_safe_identifier(self.current_worker_id, "current_worker_id")
        if self.lease_id:
            validate_safe_identifier(self.lease_id, "lease_id")
        if self.checkpoint_version < 1:
            raise ValueError("checkpoint_version must be >= 1")

        object.__setattr__(self, "completed_steps", tuple(self.completed_steps))
        object.__setattr__(self, "active_steps", tuple(self.active_steps))
        object.__setattr__(self, "pending_steps", tuple(self.pending_steps))
        object.__setattr__(self, "sub_mission_refs", tuple(self.sub_mission_refs))
        object.__setattr__(self, "step_records", deep_freeze(self.step_records))
        object.__setattr__(self, "coordination_state_refs", deep_freeze(sanitize_security_data(self.coordination_state_refs)))
        object.__setattr__(self, "assignment_versions", deep_freeze(dict(self.assignment_versions)))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))

        # Calcular Checksum determinista
        object.__setattr__(self, "checksum", self._compute_checksum())

    def _compute_checksum(self) -> str:
        steps_summary = []
        for sid in sorted(self.step_records.keys()):
            rec = self.step_records[sid]
            steps_summary.append({
                "step_id": rec.step_id,
                "status": rec.status,
                "agent_id": rec.assigned_agent_id,
                "assignment_version": rec.assignment_version,
                "side_effect_committed": rec.side_effect_committed,
            })

        payload = {
            "checkpoint_id": self.checkpoint_id,
            "tenant_id": self.tenant_id,
            "mission_id": self.mission_id,
            "mission_status": self.mission_status.value if hasattr(self.mission_status, "value") else str(self.mission_status),
            "checkpoint_version": self.checkpoint_version,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "completed_steps": sorted(list(self.completed_steps)),
            "active_steps": sorted(list(self.active_steps)),
            "pending_steps": sorted(list(self.pending_steps)),
            "steps_summary": steps_summary,
            "assignment_versions": dict(sorted(self.assignment_versions.items())),
            "budget_consumed_tokens": self.budget_consumed_tokens,
            "budget_consumed_cost": str(self.budget_consumed_cost),
            "quota_consumed": self.quota_consumed,
        }
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def verify_integrity(self) -> bool:
        return self.checksum == self._compute_checksum()

    @property
    def is_terminal(self) -> bool:
        return self.mission_status in (
            MissionStatus.COMPLETED,
            MissionStatus.FAILED,
            MissionStatus.ABORTED,
        )


class CheckpointIntegrityError(Exception):
    """Lanzada cuando un checkpoint está corrupto, alterado o no coincide con su checksum."""
    pass


class StaleWorkerError(Exception):
    """Lanzada cuando un worker cuyo lease o assignment_version expiró intenta mutar el estado."""
    pass


class StaleCheckpointError(Exception):
    """Lanzada cuando se intenta guardar un checkpoint con versión anterior o concurrente."""
    pass


class ConcurrentResumeConflictError(Exception):
    """Lanzada cuando múltiples workers compiten concurrentemente por adquirir el lease de resume."""
    pass
