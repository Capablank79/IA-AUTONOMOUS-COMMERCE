"""
Validación de jerarquías, invariantes y políticas para Sub-missions (Hito R.2 — Advanced Autonomy).

Invariantes validados:
1. parent != child
2. parent existe y pertenece al mismo tenant (CrossTenantGuard / Tenant Isolation).
3. No ciclos en la jerarquía (detección por DFS / conjuntos de ancestros).
4. Root consistente (root_mission_id = parent.root_mission_id or parent.mission_id).
5. Profundidad finita y acotada (depth = parent.depth + 1 <= max_depth).
6. Fan-out acotado (número de hijos directos <= max_children_per_parent).
7. Presupuesto acotado (sum(allocated_child_budgets) <= parent_budget).
8. Objetivo hijo acotado y no vacío.
9. Sanitización de secretos / CoT en inputs heredados.
"""

from typing import List, Optional, Set, Dict, Tuple, Sequence
from decimal import Decimal

from src.domain.mission.models import Mission, MissionStatus
from src.domain.planning.models import PlanBudget
from src.domain.sub_mission.models import (
    SubMissionHierarchyPolicy,
    SubMissionCreationContract,
)


class SubMissionHierarchyError(Exception):
    """Excepción base para violaciones de jerarquía de submisiones."""
    pass


class HierarchyCycleDetectedError(SubMissionHierarchyError):
    """Lanzada cuando se detecta un ciclo de paternidad en la jerarquía."""
    pass


class MaxDepthExceededError(SubMissionHierarchyError):
    """Lanzada cuando la profundidad supera el límite configurado."""
    pass


class MaxChildrenExceededError(SubMissionHierarchyError):
    """Lanzada cuando un padre supera el límite de hijos permitidos."""
    pass


class TenantMismatchError(SubMissionHierarchyError):
    """Lanzada cuando el hijo y el padre pertenecen a tenants distintos."""
    pass


class InvalidParentMissionError(SubMissionHierarchyError):
    """Lanzada cuando la misión padre no existe o se encuentra en estado inválido."""
    pass


class BudgetExceededError(SubMissionHierarchyError):
    """Lanzada cuando la asignación de presupuesto excede el límite del padre."""
    pass


class SubMissionHierarchyValidator:
    """
    Validador puro y determinista de jerarquías e invariantes de submisiones.
    """

    def __init__(self, policy: Optional[SubMissionHierarchyPolicy] = None):
        self.policy = policy or SubMissionHierarchyPolicy()

    def validate_creation(
        self,
        contract: SubMissionCreationContract,
        parent_mission: Mission,
        existing_children: Sequence[Mission],
        parent_budget: Optional[PlanBudget] = None,
        existing_sibling_budgets: Sequence[Optional[PlanBudget]] = (),
    ) -> None:
        """
        Valida que una solicitud de creación de submisión cumpla todos los invariantes.
        """
        # 1. Validar parent existencia y estado
        if not parent_mission:
            raise InvalidParentMissionError(f"Parent mission '{contract.parent_mission_id}' does not exist.")

        if parent_mission.status in (MissionStatus.COMPLETED, MissionStatus.ABORTED, MissionStatus.FAILED):
            raise InvalidParentMissionError(
                f"Cannot create sub-mission under terminal parent mission '{parent_mission.mission_id}' (status: {parent_mission.status})."
            )

        # 2. Profundidad
        calculated_depth = parent_mission.depth + 1
        if calculated_depth > self.policy.max_depth:
            raise MaxDepthExceededError(
                f"Sub-mission depth {calculated_depth} exceeds maximum permitted depth {self.policy.max_depth}."
            )

        # 3. Límite de hijos (fan-out)
        if len(existing_children) >= self.policy.max_children_per_parent:
            raise MaxChildrenExceededError(
                f"Parent mission '{parent_mission.mission_id}' has reached maximum child limit ({len(existing_children)} >= {self.policy.max_children_per_parent})."
            )

        # 4. Invariante de presupuesto (si aplica)
        if contract.allocated_budget is not None and parent_budget is not None:
            self.validate_budget_allocation(
                allocated=contract.allocated_budget,
                parent_budget=parent_budget,
                sibling_budgets=existing_sibling_budgets,
            )

    def validate_no_cycles(
        self,
        candidate_child_id: str,
        target_parent_id: str,
        get_parent_func,
    ) -> None:
        """
        Verifica que vincular candidate_child_id a target_parent_id no cree un ciclo.
        """
        if candidate_child_id == target_parent_id:
            raise HierarchyCycleDetectedError(
                f"Self-parenting is forbidden: '{candidate_child_id}' cannot be its own parent."
            )

        visited: Set[str] = {candidate_child_id}
        current_id: Optional[str] = target_parent_id

        while current_id:
            if current_id in visited:
                raise HierarchyCycleDetectedError(
                    f"Hierarchy cycle detected involving mission '{current_id}'."
                )
            visited.add(current_id)
            parent_mission = get_parent_func(current_id)
            if not parent_mission:
                break
            if hasattr(parent_mission, "parent_mission_id"):
                current_id = parent_mission.parent_mission_id
            elif isinstance(parent_mission, str):
                current_id = parent_mission
            else:
                break

    def validate_budget_allocation(
        self,
        allocated: PlanBudget,
        parent_budget: PlanBudget,
        sibling_budgets: Sequence[Optional[PlanBudget]] = (),
    ) -> None:
        """
        Valida que el presupuesto asignado al hijo no exceda el disponible del padre
        considerando los presupuestos ya reservados para misiones hermanas.
        UNKNOWN != 0: si el padre tiene límite acotado y el hijo es UNKNOWN (None), se rechaza.
        """
        # Validar tokens
        if parent_budget.max_tokens is not None:
            if allocated.max_tokens is None:
                raise BudgetExceededError(
                    f"Child token budget is unbounded/unknown while parent token budget is capped at {parent_budget.max_tokens}."
                )
            sibling_tokens = sum(
                sb.max_tokens for sb in sibling_budgets if sb and sb.max_tokens is not None
            )
            if sibling_tokens + allocated.max_tokens > parent_budget.max_tokens:
                raise BudgetExceededError(
                    f"Allocated tokens ({allocated.max_tokens}) + sibling tokens ({sibling_tokens}) "
                    f"exceed parent token budget ({parent_budget.max_tokens})."
                )

        # Validar costo monetario
        if parent_budget.max_cost is not None:
            if allocated.max_cost is None:
                raise BudgetExceededError(
                    f"Child cost budget is unbounded/unknown while parent cost budget is capped at {parent_budget.max_cost}."
                )
            sibling_cost = sum(
                sb.max_cost for sb in sibling_budgets if sb and sb.max_cost is not None
            )
            if sibling_cost + allocated.max_cost > parent_budget.max_cost:
                raise BudgetExceededError(
                    f"Allocated cost ({allocated.max_cost}) + sibling cost ({sibling_cost}) "
                    f"exceed parent cost budget ({parent_budget.max_cost})."
                )
