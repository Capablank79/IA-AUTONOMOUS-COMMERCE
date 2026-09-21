"""
Modelos de dominio para Agent Coordination (Hito R.4 — Advanced Autonomy).

Define:
- CoordinationStatus: Estados del ciclo de vida de una CoordinationSession (INITIALIZING, ACTIVE, BLOCKED, COMPLETED, FAILED, CANCELLED).
- CoordinationTaskStatus: Estados de las tareas coordinadas (PENDING, READY, CLAIMED, RUNNING, BLOCKED, COMPLETED, FAILED, CANCELLED).
- CoordinationFailureType: Taxonomía canónica de fallos de coordinación (TECHNICAL_FAILURE, POLICY_DENIED, BUDGET_EXHAUSTED, RATE_LIMITED, CAPABILITY_UNAVAILABLE, DEPENDENCY_FAILED, CONFLICT, CANCELLED, UNKNOWN).
- MergeStrategy: Estrategias deterministas de fusión de resultados (UNION, KEYED_MERGE, EXPLICIT_PRECEDENCE, STRICT_IDENTICAL, FAIL_ON_CONFLICT).
- CoordinationEventType: Tipos de eventos auditables de coordinación (COORDINATION_STARTED, TASK_ASSIGNED, TASK_CLAIMED, HANDOFF_CREATED, RESULT_MERGED, COORDINATION_CONFLICT, TASK_BLOCKED, COORDINATION_COMPLETED, COORDINATION_FAILED, COORDINATION_CANCELLED).
- CoordinationPolicy: Reglas de contorno y límites de concurrencia.
- TaskClaim: Lease atómico de propiedad de una tarea mutable.
- AgentHandoff: Estructura inmutable de transferencia de hechos/evidencia entre agentes (Zero-CoT).
- SharedCoordinationContext: Blackboard estructurado y versionado de hechos/decisiones/evidencia.
- CoordinationTask: Unidad atómica de trabajo coordinado (asociada a PlanStep o SubMission).
- MergeResult: Resultado determinista de fusión y detección de conflictos.
- CoordinationSession: Agregado canónico de coordinación multi-agente.

Principios R.4:
1. Responde a: "¿Puede la plataforma coordinar varios agentes especializados dentro de una misión común, respetando dependencias, exclusión mutua, orden, budgets y contratos de resultados?".
2. COORDINATION != DELEGATION: R.3 define capacidades/agentes; R.4 coordina su colaboración estructurada; R.5 es delegación dinámica futura.
3. COORDINATION != EXECUTOR: R.4 no ejecuta herramientas directamente; orquesta el workflow sobre el runtime y ActionExecutor existentes.
4. SINGLE OWNER: Cada tarea mutable tiene un único owner lógico a la vez. Exclusión mutua estricta para side effects.
5. READINESS & ORDERING: R.1 DAG es la fuente de verdad del ordenamiento. Una tarea es READY solo si dependencias completadas, inputs disponibles, agente asignado disponible y budget válido. UNKNOWN dependency => NOT READY.
6. FAN-OUT / FAN-IN: Paralelismo solo entre ramas independientes. Fan-in downstream espera todos los required inputs.
7. STRUCTURED HANDOFF & ZERO-COT: Transferencia explícita de hechos/evidencia. Cero cadena de pensamiento (CoT) o scratchpad privado.
8. DETERMINISTIC MERGE & CONFLICT DETECTION: Fusión determinista; ante contradicción o colisión no resoluble -> BLOCKED / CONFLICT (no silent last write wins).
9. BOUNDED CONCURRENCY & BUDGET RESERVATION: Concurrencia acotada con reserva de presupuesto atómica para evitar TOCTOU.
10. POLICY & SAFETY: POLICY_DENIED bloquea; cero reroute evasivo de políticas o emergency stop.
11. TENANT ISOLATION: Aislamiento multi-tenant estricto (TenantContext bound).
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
from src.domain.planning.models import (
    PlanBudget,
    PlanStep,
    StepStatus,
    StepFailureType,
)
from src.domain.tenant.models import TenantContext


class CoordinationStatus(str, Enum):
    """
    Estados canónicos del ciclo de vida de una CoordinationSession.
    """
    INITIALIZING = "INITIALIZING"
    ACTIVE = "ACTIVE"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CoordinationTaskStatus(str, Enum):
    """
    Estados canónicos del ciclo de vida de una CoordinationTask.
    """
    PENDING = "PENDING"
    READY = "READY"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class CoordinationFailureType(str, Enum):
    """
    Taxonomía canónica de fallos de coordinación en R.4.
    """
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    POLICY_DENIED = "POLICY_DENIED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    DEPENDENCY_FAILED = "DEPENDENCY_FAILED"
    CONFLICT = "CONFLICT"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class MergeStrategy(str, Enum):
    """
    Estrategias deterministas para fusionar resultados producidos por múltiples agentes.
    """
    UNION = "UNION"                          # Combina claves no colisionantes
    KEYED_MERGE = "KEYED_MERGE"              # Fusión estructurada respetando claves
    EXPLICIT_PRECEDENCE = "EXPLICIT_PRECEDENCE"  # Precedencia ordenada explícita por agente/fuente
    STRICT_IDENTICAL = "STRICT_IDENTICAL"    # Falla si valores para misma clave difieren
    FAIL_ON_CONFLICT = "FAIL_ON_CONFLICT"    # Cualquier discrepancia detiene y marca conflicto


class CoordinationEventType(str, Enum):
    """
    Tipos de eventos para auditoría y trazabilidad en R.4 (K.1 / K.2).
    """
    COORDINATION_STARTED = "COORDINATION_STARTED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_CLAIMED = "TASK_CLAIMED"
    HANDOFF_CREATED = "HANDOFF_CREATED"
    RESULT_MERGED = "RESULT_MERGED"
    COORDINATION_CONFLICT = "COORDINATION_CONFLICT"
    TASK_BLOCKED = "TASK_BLOCKED"
    COORDINATION_COMPLETED = "COORDINATION_COMPLETED"
    COORDINATION_FAILED = "COORDINATION_FAILED"
    COORDINATION_CANCELLED = "COORDINATION_CANCELLED"
    DELEGATION_REQUESTED = "DELEGATION_REQUESTED"
    DELEGATION_APPROVED = "DELEGATION_APPROVED"
    DELEGATION_REJECTED = "DELEGATION_REJECTED"
    ASSIGNMENT_RELEASED = "ASSIGNMENT_RELEASED"
    ASSIGNMENT_TRANSFERRED = "ASSIGNMENT_TRANSFERRED"
    DELEGATION_LIMIT_REACHED = "DELEGATION_LIMIT_REACHED"
    STALE_AGENT_RESULT_REJECTED = "STALE_AGENT_RESULT_REJECTED"


@dataclass(frozen=True)
class CoordinationPolicy:
    """
    Políticas de contorno y límites de ejecución para la sesión de coordinación.
    """
    max_concurrent_agents: int = 5
    max_concurrent_tasks: int = 10
    task_claim_timeout_seconds: Optional[int] = 300
    default_merge_strategy: MergeStrategy = MergeStrategy.KEYED_MERGE
    fail_fast_on_policy_denied: bool = True
    max_delegations_per_task: int = 3
    allow_fallback_to_degraded: bool = False

    def __post_init__(self):
        if self.max_concurrent_agents < 1:
            raise ValueError("max_concurrent_agents must be at least 1")
        if self.max_concurrent_tasks < 1:
            raise ValueError("max_concurrent_tasks must be at least 1")
        if self.task_claim_timeout_seconds is not None and self.task_claim_timeout_seconds < 1:
            raise ValueError("task_claim_timeout_seconds must be positive")


@dataclass(frozen=True)
class TaskClaim:
    """
    Lease atómico de propiedad y ejecución de una CoordinationTask.
    Garantiza single-owner mutable execution y evita duplicate execution.
    """
    task_id: str
    agent_id: str
    claimed_at: datetime
    expires_at: Optional[datetime] = None
    lease_id: str = field(default_factory=lambda: "")
    is_active: bool = True

    def __post_init__(self):
        validate_safe_identifier(self.task_id, "task_id")
        validate_safe_identifier(self.agent_id, "agent_id")
        if not self.lease_id:
            raw = f"{self.task_id}:{self.agent_id}:{self.claimed_at.isoformat()}"
            object.__setattr__(self, "lease_id", hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16])
        validate_safe_identifier(self.lease_id, "lease_id")


@dataclass(frozen=True)
class AgentAssignment:
    """
    Asignación formal de una tarea de coordinación a un agente especialista.
    """
    task_id: str
    assigned_agent_id: str
    required_capability: str
    status: CoordinationTaskStatus = CoordinationTaskStatus.PENDING
    dependencies: Tuple[str, ...] = field(default_factory=tuple)
    attempt: int = 1
    assigned_at: Optional[datetime] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.task_id, "task_id")
        validate_safe_identifier(self.assigned_agent_id, "assigned_agent_id")
        validate_safe_identifier(self.required_capability, "required_capability")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))


@dataclass(frozen=True)
class AgentHandoff:
    """
    Contrato estructurado e inmutable de transferencia de resultados/hechos entre agentes.
    Zero-CoT estricto (no reasoning ni internal scratchpad).
    """
    handoff_id: str
    source_agent_id: str
    target_task_id: str
    payload: Mapping[str, Any]
    output_contract_keys: Tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    correlation_id: str = ""
    created_at: Optional[datetime] = None
    target_agent_id: Optional[str] = None

    def __post_init__(self):
        validate_safe_identifier(self.handoff_id, "handoff_id")
        validate_safe_identifier(self.source_agent_id, "source_agent_id")
        validate_safe_identifier(self.target_task_id, "target_task_id")
        if self.target_agent_id:
            validate_safe_identifier(self.target_agent_id, "target_agent_id")
        object.__setattr__(self, "output_contract_keys", tuple(self.output_contract_keys))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "payload", deep_freeze(sanitize_security_data(self.payload)))
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc))


@dataclass(frozen=True)
class MergeResult:
    """
    Resultado determinista de una operación de fusión de resultados multi-agente.
    """
    success: bool
    merged_data: Mapping[str, Any] = field(default_factory=dict)
    strategy_used: MergeStrategy = MergeStrategy.KEYED_MERGE
    conflicts: Tuple[str, ...] = field(default_factory=tuple)
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "merged_data", deep_freeze(sanitize_security_data(self.merged_data)))
        object.__setattr__(self, "conflicts", tuple(self.conflicts))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))


@dataclass(frozen=True)
class SharedCoordinationContext:
    """
    Blackboard estructurado y versionado de hechos, decisiones y evidencias compartidas.
    Zero-CoT estricto (no reasoning ni internal scratchpad).
    """
    session_id: str
    tenant_id: str
    mission_id: str
    facts: Mapping[str, Any] = field(default_factory=dict)
    decisions: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    version: int = 1
    updated_at: Optional[datetime] = None

    def __post_init__(self):
        validate_safe_identifier(self.session_id, "session_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        object.__setattr__(self, "facts", deep_freeze(sanitize_security_data(self.facts)))
        object.__setattr__(self, "decisions", deep_freeze(sanitize_security_data(self.decisions)))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", datetime.now(timezone.utc))


@dataclass(frozen=True)
class CoordinationTask:
    """
    Unidad atómica coordinada (asociada a PlanStep o SubMission).
    """
    task_id: str
    session_id: str
    tenant_id: str
    required_capability: str
    action_type: str
    assigned_agent_id: Optional[str] = None
    status: CoordinationTaskStatus = CoordinationTaskStatus.PENDING
    dependencies: Tuple[str, ...] = field(default_factory=tuple)
    required_input_keys: Tuple[str, ...] = field(default_factory=tuple)
    inputs: Mapping[str, Any] = field(default_factory=dict)
    outputs: Mapping[str, Any] = field(default_factory=dict)
    allocated_budget: Optional[PlanBudget] = None
    claim: Optional[TaskClaim] = None
    failure_type: Optional[CoordinationFailureType] = None
    failure_reason: Optional[str] = None
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    is_side_effecting: bool = False
    resource_id: Optional[str] = None
    attempt: int = 1
    assignment_version: int = 1
    delegation_history: Tuple[str, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.task_id, "task_id")
        validate_safe_identifier(self.session_id, "session_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.required_capability, "required_capability")
        if self.assigned_agent_id:
            validate_safe_identifier(self.assigned_agent_id, "assigned_agent_id")
        if self.resource_id:
            validate_safe_identifier(self.resource_id, "resource_id")
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "required_input_keys", tuple(self.required_input_keys))
        object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        object.__setattr__(self, "delegation_history", tuple(self.delegation_history))
        object.__setattr__(self, "inputs", deep_freeze(sanitize_security_data(self.inputs)))
        object.__setattr__(self, "outputs", deep_freeze(sanitize_security_data(self.outputs)))
        object.__setattr__(self, "metadata", deep_freeze(sanitize_security_data(self.metadata)))

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            CoordinationTaskStatus.COMPLETED,
            CoordinationTaskStatus.FAILED,
            CoordinationTaskStatus.CANCELLED,
            CoordinationTaskStatus.BLOCKED,
        )


@dataclass(frozen=True)
class CoordinationSession:
    """
    Agregado canónico que representa la sesión de coordinación multi-agente en R.4.
    """
    session_id: str
    tenant_id: str
    mission_id: str
    correlation_id: str
    plan_id: Optional[str] = None
    plan_version: int = 1
    sub_mission_ids: Tuple[str, ...] = field(default_factory=tuple)
    status: CoordinationStatus = CoordinationStatus.INITIALIZING
    tasks: Mapping[str, CoordinationTask] = field(default_factory=dict)
    shared_context: Optional[SharedCoordinationContext] = None
    policy: CoordinationPolicy = field(default_factory=CoordinationPolicy)
    handoffs: Tuple[AgentHandoff, ...] = field(default_factory=tuple)
    active_leases: Mapping[str, TaskClaim] = field(default_factory=dict)
    resource_locks: Mapping[str, str] = field(default_factory=dict)
    budget_reserved: Optional[PlanBudget] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    checksum: str = field(default="", init=False)

    def __post_init__(self):
        validate_safe_identifier(self.session_id, "session_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        if self.plan_id:
            validate_safe_identifier(self.plan_id, "plan_id")
        object.__setattr__(self, "sub_mission_ids", tuple(self.sub_mission_ids))
        object.__setattr__(self, "tasks", deep_freeze(self.tasks))
        object.__setattr__(self, "handoffs", tuple(self.handoffs))
        object.__setattr__(self, "active_leases", deep_freeze(self.active_leases))
        object.__setattr__(self, "resource_locks", deep_freeze(self.resource_locks))

        now = datetime.now(timezone.utc)
        if self.created_at is None:
            object.__setattr__(self, "created_at", now)
        if self.updated_at is None:
            object.__setattr__(self, "updated_at", now)

        if self.shared_context is None:
            sc = SharedCoordinationContext(
                session_id=self.session_id,
                tenant_id=self.tenant_id,
                mission_id=self.mission_id,
                updated_at=self.created_at,
            )
            object.__setattr__(self, "shared_context", sc)

        # Checksum determinista
        object.__setattr__(self, "checksum", self._compute_checksum())

    def _compute_checksum(self) -> str:
        tasks_meta = []
        for tid in sorted(self.tasks.keys()):
            t = self.tasks[tid]
            tasks_meta.append({
                "task_id": t.task_id,
                "status": t.status.value,
                "agent_id": t.assigned_agent_id,
                "attempt": t.attempt,
                "assignment_version": t.assignment_version,
                "failure_type": t.failure_type.value if t.failure_type else None,
            })

        payload = {
            "session_id": self.session_id,
            "tenant_id": self.tenant_id,
            "mission_id": self.mission_id,
            "plan_id": self.plan_id,
            "plan_version": self.plan_version,
            "status": self.status.value,
            "tasks": tasks_meta,
            "resource_locks": dict(sorted(self.resource_locks.items())),
            "context_version": self.shared_context.version if self.shared_context else 0,
        }
        serialized = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get_task(self, task_id: str) -> Optional[CoordinationTask]:
        return self.tasks.get(task_id)

    def get_tasks_by_status(self, status: CoordinationTaskStatus) -> Tuple[CoordinationTask, ...]:
        return tuple(t for t in self.tasks.values() if t.status == status)

    @property
    def is_terminal(self) -> bool:
        return self.status in (
            CoordinationStatus.COMPLETED,
            CoordinationStatus.FAILED,
            CoordinationStatus.CANCELLED,
            CoordinationStatus.BLOCKED,
        )

    def is_all_tasks_completed(self) -> bool:
        if not self.tasks:
            return False
        return all(t.status == CoordinationTaskStatus.COMPLETED for t in self.tasks.values())
