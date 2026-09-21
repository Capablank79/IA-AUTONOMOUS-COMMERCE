"""
Modelos de dominio para Sub-missions (Hito R.2 — Advanced Autonomy).

Define:
- SubMissionScope: Especificación acotada del objetivo, alcance y output esperado de la submisión.
- SubMissionCreationContract: Contrato canónico e inmutable para la creación y delegación de una submisión.
- SubMissionHierarchyPolicy: Reglas de contorno jerárquico (max_depth, max_children_per_parent, etc.).
- SubMissionResultContract: Contrato estructurado de resultado emitido por una submisión hija para propagación.
- SubMissionFailureType: Categorización de fallos de submisión.
- SubMissionDelegationKey: Identificador determinista de delegación (idempotencia).

Principios R.2:
1. Responde a: "¿Puede una misión padre delegar partes autocontenidas de su objetivo en sub-misiones explícitas, aisladas y trazables, y recomponer sus resultados de forma segura?".
2. SUB-MISSION != PLAN STEP: SubMission es una misión hija formal con identidad, ciclo de vida y contexto propios; PlanStep es una unidad dentro de ExecutionPlan.
3. SUB-MISSION != SPECIALIST AGENT: R.2 delega TRABAJO sobre capacidades y runtime existentes; no crea agentes especialistas (eso es R.3).
4. CANONICAL MISSION EXTENSION: Reutiliza el agregado canónico Mission con parent_mission_id, root_mission_id, depth y delegation_key.
5. HIERARCHY INVARIANTS: Jerarquía acíclica, finita, acotada en profundidad (max_depth) y abanico (max_children).
6. CONTEXT MINIMIZATION & ZERO-COT: Solo hereda contexto autorizado; cero secretos, cero CoT/scratchpad privado.
7. BUDGET ISOLATION & SUM PRESERVATION: El presupuesto de las hijas proviene exclusivamente del padre (sum(allocated) <= parent.available). UNKNOWN != unlimited. Sibling isolation estricto.
8. AGGREGATED LIFECYCLE: El padre no puede completarse si tiene hijas obligatorias no terminales. Cancelación y Emergency Stop en cascada.
9. TENANT ISOLATION: Aislamiento estricto por tenant (CrossTenantGuard).
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Mapping, Optional, Any, Tuple, Dict, Sequence

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
    StepFailureType,
)


class SubMissionFailureType(str, Enum):
    """
    Taxonomía canónica de fallos de submisión para R.2.
    """
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    POLICY_DENIED = "POLICY_DENIED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    CANCELLED = "CANCELLED"
    INVALID_INPUT = "INVALID_INPUT"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    EMERGENCY_STOP_BLOCKED = "EMERGENCY_STOP_BLOCKED"
    HIERARCHY_VIOLATION = "HIERARCHY_VIOLATION"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SubMissionHierarchyPolicy:
    """
    Políticas y límites estrictos de la jerarquía de submisiones.
    """
    max_depth: int = 3
    max_children_per_parent: int = 10
    max_total_descendants: int = 50
    allow_cross_type_delegation: bool = True

    def __post_init__(self):
        if self.max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if self.max_children_per_parent < 1:
            raise ValueError("max_children_per_parent must be at least 1")
        if self.max_total_descendants < 1:
            raise ValueError("max_total_descendants must be at least 1")


@dataclass(frozen=True)
class SubMissionScope:
    """
    Especificación acotada del objetivo, alcance y contrato de salida de una submisión.
    Garantiza que el objetivo hijo sea más específico y restringido que el del padre.
    """
    objective: str
    expected_outcome: str
    completion_condition: str
    expected_output_keys: Tuple[str, ...] = field(default_factory=tuple)
    target_resource: Optional[str] = None
    constraints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.objective, str) or not self.objective.strip():
            raise ValueError("SubMissionScope.objective must be a non-empty string")
        if not isinstance(self.expected_outcome, str) or not self.expected_outcome.strip():
            raise ValueError("SubMissionScope.expected_outcome must be a non-empty string")
        if not isinstance(self.completion_condition, str) or not self.completion_condition.strip():
            raise ValueError("SubMissionScope.completion_condition must be a non-empty string")

        sanitized_obj = sanitize_security_data(self.objective)
        if isinstance(sanitized_obj, str):
            object.__setattr__(self, "objective", sanitized_obj)

        if not isinstance(self.expected_output_keys, tuple):
            object.__setattr__(self, "expected_output_keys", tuple(self.expected_output_keys))
        if not isinstance(self.constraints, MappingProxyType):
            object.__setattr__(self, "constraints", deep_freeze(self.constraints))


@dataclass(frozen=True)
class SubMissionCreationContract:
    """
    Contrato canónico inmutable para solicitar la creación y delegación de una submisión.
    """
    parent_mission_id: str
    tenant_id: str
    sub_mission_type: MissionType
    scope: SubMissionScope
    inputs: Mapping[str, Any] = field(default_factory=dict)
    allocated_budget: Optional[PlanBudget] = None
    priority: MissionPriority = MissionPriority.MEDIUM
    delegation_key: Optional[str] = None
    plan_id: Optional[str] = None
    plan_step_id: Optional[str] = None
    is_required: bool = True
    correlation_id: Optional[str] = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.parent_mission_id, "parent_mission_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if self.plan_id:
            validate_safe_identifier(self.plan_id, "plan_id")
        if self.plan_step_id:
            validate_safe_identifier(self.plan_step_id, "plan_step_id")
        if not isinstance(self.scope, SubMissionScope):
            raise ValueError("scope must be an instance of SubMissionScope")

        if not isinstance(self.inputs, MappingProxyType):
            sanitized = sanitize_security_data(dict(self.inputs))
            object.__setattr__(self, "inputs", deep_freeze(sanitized))
        if not isinstance(self.metadata, MappingProxyType):
            sanitized_meta = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

        if not self.delegation_key:
            # Deterministic canonical delegation key
            key_src = f"{self.parent_mission_id}:{self.sub_mission_type.value}:{self.scope.objective}:{self.plan_step_id or ''}"
            digest = hashlib.sha256(key_src.encode("utf-8")).hexdigest()[:16]
            object.__setattr__(self, "delegation_key", f"del_{digest}")


@dataclass(frozen=True)
class SubMissionResultContract:
    """
    Contrato estructurado e inmutable de resultado emitido por una submisión para propagación segura.
    Zero Chain-of-Thought (CoT) y cero secretos.
    """
    mission_id: str
    parent_mission_id: str
    tenant_id: str
    status: MissionStatus
    outputs: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: Tuple[str, ...] = field(default_factory=tuple)
    failure_type: Optional[SubMissionFailureType] = None
    failure_reason: Optional[str] = None
    cost_spent: Optional[Decimal] = None
    tokens_spent: Optional[int] = None
    completed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_required: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.parent_mission_id, "parent_mission_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if not isinstance(self.outputs, MappingProxyType):
            sanitized_outputs = sanitize_security_data(dict(self.outputs))
            object.__setattr__(self, "outputs", deep_freeze(sanitized_outputs))
        if not isinstance(self.evidence_refs, tuple):
            object.__setattr__(self, "evidence_refs", tuple(self.evidence_refs))
        if not isinstance(self.metadata, MappingProxyType):
            sanitized_meta = sanitize_security_data(dict(self.metadata))
            object.__setattr__(self, "metadata", deep_freeze(sanitized_meta))

    @property
    def is_success(self) -> bool:
        return self.status == MissionStatus.COMPLETED


@dataclass(frozen=True)
class SubMissionNode:
    """
    Vista de árbol/nodo jerárquico para inspección y proyección en Q.4 / Dashboards.
    """
    mission: Mission
    children: Tuple['SubMissionNode', ...] = field(default_factory=tuple)
    result: Optional[SubMissionResultContract] = None

    def __post_init__(self):
        if not isinstance(self.children, tuple):
            object.__setattr__(self, "children", tuple(self.children))
