"""
Puertos de dominio para Multi-step Planning (Hito R.1 — Advanced Autonomy).

Define:
- ExecutionPlanRepositoryPort: Persistencia aislada por tenant de ExecutionPlan.
- CapabilityRegistryPort: Consulta de capacidades/herramientas/agentes registrados disponibles.
- MultiStepPlanningServicePort: Contrato principal de descomposición, DAG, validación, readiness y replanning.
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Sequence, Tuple, Mapping, Any
from decimal import Decimal

from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    PlanningResult,
    PlanStatus,
    StepStatus,
    StepFailureType,
)
from src.domain.tenant.models import TenantContext


class ExecutionPlanRepositoryPort(ABC):
    """
    Puerto secundario para la persistencia multi-tenant de ExecutionPlan.
    """

    @abstractmethod
    def save_plan(self, plan: ExecutionPlan, tenant_context: TenantContext) -> ExecutionPlan:
        """Persiste o actualiza un ExecutionPlan con validación estricta de tenant."""
        pass

    @abstractmethod
    def get_plan_by_id(self, plan_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        """Obtiene un plan por ID asegurando aislamiento de tenant."""
        pass

    @abstractmethod
    def get_plan_by_mission_id(self, mission_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        """Obtiene el plan activo más reciente de una misión."""
        pass

    @abstractmethod
    def list_plans_for_mission(self, mission_id: str, tenant_context: TenantContext) -> List[ExecutionPlan]:
        """Lista todas las versiones de planes asociados a una misión."""
        pass


class CapabilityRegistryPort(ABC):
    """
    Puerto para verificar si una acción o capability requerida por un step existe y está disponible.
    """

    @abstractmethod
    def is_capability_available(self, capability_name: str) -> bool:
        """Verifica si la capacidad/herramienta/agente está registrada y disponible."""
        pass

    @abstractmethod
    def get_known_capabilities(self) -> Tuple[str, ...]:
        """Retorna la lista de capacidades/herramientas canónicas del sistema."""
        pass


class MultiStepPlanningServicePort(ABC):
    """
    Puerto primario de dominio para la planificación multi-paso.
    """

    @abstractmethod
    def create_plan(
        self,
        mission_id: str,
        goal: str,
        tenant_context: TenantContext,
        initial_steps: Sequence[PlanStep],
        budget: Optional[Any] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> PlanningResult:
        """Crea y valida un ExecutionPlan a partir de un objetivo y pasos descompuestos."""
        pass

    @abstractmethod
    def get_ready_steps(
        self,
        plan: ExecutionPlan,
        tenant_context: TenantContext,
    ) -> Tuple[PlanStep, ...]:
        """Determina de forma determinista qué pasos están listos para ser ejecutados."""
        pass

    @abstractmethod
    def replan(
        self,
        plan_id: str,
        tenant_context: TenantContext,
        failed_step_id: str,
        failure_type: StepFailureType,
        failure_reason: str,
        new_subgraph_steps: Optional[Sequence[PlanStep]] = None,
    ) -> PlanningResult:
        """Ejecuta replanificación acotada sobre el subgrafo afectado preservando pasos completados."""
        pass
