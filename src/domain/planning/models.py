"""
Modelos de dominio para Multi-step Planning (Hito R.1 — Advanced Autonomy).

Define:
- PlanStatus: Estados del ciclo de vida del plan (DRAFT, VALIDATED, READY, IN_PROGRESS, COMPLETED, FAILED, BLOCKED, REJECTED).
- StepStatus: Estados de los pasos del plan (PENDING, READY, IN_PROGRESS, COMPLETED, FAILED, BLOCKED, SKIPPED).
- StepFailureType: Taxonomía canónica de fallos de pasos (EXECUTION_FAILURE, DEPENDENCY_FAILURE, CAPABILITY_UNAVAILABLE, POLICY_DENIED, BUDGET_EXHAUSTED, INVALID_INPUT, UNKNOWN).
- StepDependency: Dependencia explícita entre pasos con condición y tipo.
- PlanStep: Unidad ejecutable o sub-objetivo dentro de la descomposición jerárquica.
- PlanBudget: Presupuestos estructurados (token, monetary, max_steps, wall_clock_seconds, max_replans).
- PlanVersionHistory: Registro inmutable de evolución de versiones y causa de replanificación.
- ExecutionPlan: Agregado canónico que representa el DAG completo de ejecución.
- PlanningResult: Resultado canónico de una operación de planificación/replanificación.

Principios R.1:
1. Responde a: "¿Puede el agente transformar un objetivo complejo en un plan multi-paso explícito, válido, acíclico y ejecutable, respetando dependencias, budgets y políticas, y replanificar de forma segura cuando cambian las condiciones?".
2. PLANNER != EXECUTOR: R.1 planifica y valida DAG; NO ejecuta herramientas ni duplica ActionExecutor / AutonomousLoop.
3. Inmutabilidad estricta (frozen=True, MappingProxyType, tuples).
4. Cero Chain-of-Thought (CoT) y cero datos sensibles.
5. Preservación estricta de incertidumbre UNKNOWN (costes, disponibilidad, inputs). UNKNOWN != 0.
6. Aislamiento Multi-Tenant estricto.
7. Replan Bounds: Replanificación acotada (max_replans) y mínima (preservando pasos COMPLETED).
8. Replan != Policy Bypass: Fallos por POLICY_DENIED o Emergency Stop NO deben ser bypassados mediante replan.
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
from src.domain.tenant.models import TenantContext


class PlanStatus(str, Enum):
    """
    Estados canónicos del ciclo de vida de un ExecutionPlan.
    """
    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    REJECTED = "REJECTED"


class StepStatus(str, Enum):
    """
    Estados canónicos del ciclo de vida de un PlanStep.
    """
    PENDING = "PENDING"
    READY = "READY"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    SKIPPED = "SKIPPED"


class StepFailureType(str, Enum):
    """
    Taxonomía canónica de fallos de pasos.
    """
    EXECUTION_FAILURE = "EXECUTION_FAILURE"
    DEPENDENCY_FAILURE = "DEPENDENCY_FAILURE"
    CAPABILITY_UNAVAILABLE = "CAPABILITY_UNAVAILABLE"
    POLICY_DENIED = "POLICY_DENIED"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    INVALID_INPUT = "INVALID_INPUT"
    UNKNOWN = "UNKNOWN"


class DependencyType(str, Enum):
    """
    Tipo de relación de dependencia entre pasos.
    """
    HARD = "HARD"                  # El paso predecesor DEBE completarse exitosamente
    DATA = "DATA"                  # Requiere los outputs específicos del predecesor
    OPTIONAL = "OPTIONAL"          # Deseable pero no bloqueante si falla/omite


@dataclass(frozen=True)
class StepDependency:
    """
    Dependencia dirigida entre pasos dentro del DAG.
    from_step_id -> to_step_id (to_step_id depende de from_step_id).
    """
    from_step_id: str
    to_step_id: str
    dependency_type: DependencyType = DependencyType.HARD
    required_output_keys: Tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.from_step_id, "from_step_id")
        validate_safe_identifier(self.to_step_id, "to_step_id")
        if self.from_step_id == self.to_step_id:
            raise ValueError(f"Self-dependency is strictly forbidden: step '{self.from_step_id}' cannot depend on itself.")
        if not isinstance(self.required_output_keys, tuple):
            object.__setattr__(self, "required_output_keys", tuple(self.required_output_keys))


@dataclass(frozen=True)
class PlanBudget:
    """
    Presupuestos estructurados y límites asignados a un plan o step.
    UNKNOWN preserva None para evitar convertir incertidumbre en 0.
    """
    max_tokens: Optional[int] = None
    max_cost: Optional[Decimal] = None
    max_steps: Optional[int] = None
    max_wall_clock_seconds: Optional[int] = None
    max_replans: int = 3
    is_cost_unknown: bool = False

    def __post_init__(self):
        if self.max_tokens is not None and self.max_tokens < 0:
            raise ValueError("max_tokens cannot be negative")
        if self.max_cost is not None and self.max_cost < Decimal("0"):
            raise ValueError("max_cost cannot be negative")
        if self.max_steps is not None and self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.max_wall_clock_seconds is not None and self.max_wall_clock_seconds < 1:
            raise ValueError("max_wall_clock_seconds must be at least 1")
        if self.max_replans < 0:
            raise ValueError("max_replans cannot be negative")


@dataclass(frozen=True)
class StepRationale:
    """
    Explicabilidad estructurada sin Chain-of-Thought (Anti-CoT).
    """
    objective: str
    dependency_reason: str = ""
    capability_selected: str = ""
    budget_reason: str = ""
    replan_reason: str = ""

    def __post_init__(self):
        # Sanitizar metadatos para asegurar cero CoT o PII
        sanitized_obj = sanitize_security_data(self.objective)
        if isinstance(sanitized_obj, str):
            object.__setattr__(self, "objective", sanitized_obj)


@dataclass(frozen=True)
class PlanStep:
    """
    Paso indivisible o sub-objetivo dentro de la descomposición jerárquica del plan.
    """
    step_id: str
    objective: str
    action_type: str
    required_inputs: Mapping[str, Any] = field(default_factory=dict)
    expected_outputs: Tuple[str, ...] = field(default_factory=tuple)
    dependencies: Tuple[str, ...] = field(default_factory=tuple)  # IDs de pasos previos requeridos
    assigned_capability: Optional[str] = None
    assigned_agent: Optional[str] = None
    status: StepStatus = StepStatus.PENDING
    failure_type: Optional[StepFailureType] = None
    failure_reason: Optional[str] = None
    estimated_cost: Optional[Decimal] = None
    is_cost_unknown: bool = False
    estimated_tokens: Optional[int] = None
    actual_outputs: Mapping[str, Any] = field(default_factory=dict)
    rationale: Optional[StepRationale] = None
    parent_goal_id: Optional[str] = None
    depth_level: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        validate_safe_identifier(self.step_id, "step_id")
        if not self.objective or not isinstance(self.objective, str):
            raise ValueError("PlanStep.objective must be a non-empty string")
        if not self.action_type or not isinstance(self.action_type, str):
            raise ValueError("PlanStep.action_type must be a non-empty string")
        if self.estimated_cost is not None and self.estimated_cost < Decimal("0"):
            raise ValueError("estimated_cost cannot be negative")
        if self.estimated_tokens is not None and self.estimated_tokens < 0:
            raise ValueError("estimated_tokens cannot be negative")
        if self.depth_level < 1:
            raise ValueError("depth_level must be >= 1")

        if not isinstance(self.required_inputs, MappingProxyType):
            object.__setattr__(self, "required_inputs", deep_freeze(self.required_inputs))
        if not isinstance(self.actual_outputs, MappingProxyType):
            object.__setattr__(self, "actual_outputs", deep_freeze(self.actual_outputs))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", deep_freeze(self.metadata))
        if not isinstance(self.expected_outputs, tuple):
            object.__setattr__(self, "expected_outputs", tuple(self.expected_outputs))
        if not isinstance(self.dependencies, tuple):
            object.__setattr__(self, "dependencies", tuple(self.dependencies))


@dataclass(frozen=True)
class PlanVersionHistory:
    """
    Registro histórico inmutable de revisiones de un plan.
    """
    version: int
    replan_reason: str
    replan_event_type: Optional[StepFailureType] = None
    changed_step_ids: Tuple[str, ...] = field(default_factory=tuple)
    preserved_step_ids: Tuple[str, ...] = field(default_factory=tuple)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if self.version < 1:
            raise ValueError("PlanVersionHistory.version must be >= 1")
        if not isinstance(self.changed_step_ids, tuple):
            object.__setattr__(self, "changed_step_ids", tuple(self.changed_step_ids))
        if not isinstance(self.preserved_step_ids, tuple):
            object.__setattr__(self, "preserved_step_ids", tuple(self.preserved_step_ids))


@dataclass(frozen=True)
class ExecutionPlan:
    """
    Agregado canónico de Dominio para Multi-step Planning (R.1).
    Representa un DAG determinista de pasos para satisfacer una Misión/Objetivo.
    """
    plan_id: str
    mission_id: str
    tenant_id: str
    goal: str
    steps: Tuple[PlanStep, ...] = field(default_factory=tuple)
    dependencies: Tuple[StepDependency, ...] = field(default_factory=tuple)
    version: int = 1
    status: PlanStatus = PlanStatus.DRAFT
    budget: Optional[PlanBudget] = None
    history: Tuple[PlanVersionHistory, ...] = field(default_factory=tuple)
    replan_count: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Mapping[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def __post_init__(self):
        validate_safe_identifier(self.plan_id, "plan_id")
        validate_safe_identifier(self.mission_id, "mission_id")
        validate_safe_identifier(self.tenant_id, "tenant_id")
        if not self.goal or not isinstance(self.goal, str):
            raise ValueError("ExecutionPlan.goal must be a non-empty string")
        if self.version < 1:
            raise ValueError("ExecutionPlan.version must be >= 1")
        if self.replan_count < 0:
            raise ValueError("ExecutionPlan.replan_count cannot be negative")

        if not isinstance(self.steps, tuple):
            object.__setattr__(self, "steps", tuple(self.steps))
        if not isinstance(self.dependencies, tuple):
            object.__setattr__(self, "dependencies", tuple(self.dependencies))
        if not isinstance(self.history, tuple):
            object.__setattr__(self, "history", tuple(self.history))
        if not isinstance(self.metadata, MappingProxyType):
            object.__setattr__(self, "metadata", deep_freeze(self.metadata))

        if not self.checksum:
            computed = self._compute_checksum()
            object.__setattr__(self, "checksum", computed)

    def _compute_checksum(self) -> str:
        """Calcula checksum determinista SHA-256 de la estructura del plan."""
        step_payloads = [
            f"{s.step_id}:{s.action_type}:{s.status.value}:{sorted(list(s.dependencies))}"
            for s in sorted(self.steps, key=lambda x: x.step_id)
        ]
        dep_payloads = [
            f"{d.from_step_id}->{d.to_step_id}:{d.dependency_type.value}"
            for d in sorted(self.dependencies, key=lambda x: (x.from_step_id, x.to_step_id))
        ]
        raw = f"{self.plan_id}|{self.mission_id}|{self.tenant_id}|{self.version}|{self.status.value}|{','.join(step_payloads)}|{','.join(dep_payloads)}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get_step(self, step_id: str) -> Optional[PlanStep]:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        return None

    def get_completed_steps(self) -> Tuple[PlanStep, ...]:
        return tuple(s for s in self.steps if s.status == StepStatus.COMPLETED)

    def get_pending_steps(self) -> Tuple[PlanStep, ...]:
        return tuple(s for s in self.steps if s.status in (StepStatus.PENDING, StepStatus.READY))

    def get_total_estimated_cost(self) -> Tuple[Optional[Decimal], bool]:
        """
        Retorna (suma_costos_conocidos, has_unknown_costs).
        Si existe un step con costo UNKNOWN, preserves is_cost_unknown=True y no lo convierte en 0.
        """
        total = Decimal("0")
        has_unknown = False
        for s in self.steps:
            if s.is_cost_unknown or s.estimated_cost is None:
                has_unknown = True
            else:
                total += s.estimated_cost
        return (total if not has_unknown else None, has_unknown)

    def get_total_estimated_tokens(self) -> Optional[int]:
        total = 0
        for s in self.steps:
            if s.estimated_tokens is None:
                return None
            total += s.estimated_tokens
        return total


@dataclass(frozen=True)
class PlanningResult:
    """
    Resultado canónico e inmutable emitido por el servicio de planificación.
    """
    success: bool
    plan: Optional[ExecutionPlan] = None
    status: PlanStatus = PlanStatus.DRAFT
    errors: Tuple[str, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    ready_step_ids: Tuple[str, ...] = field(default_factory=tuple)
    replan_count: int = 0
    replan_reason: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __post_init__(self):
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))
        if not isinstance(self.warnings, tuple):
            object.__setattr__(self, "warnings", tuple(self.warnings))
        if not isinstance(self.ready_step_ids, tuple):
            object.__setattr__(self, "ready_step_ids", tuple(self.ready_step_ids))
