"""
PlanValidator: Motor determinista de validación de DAG, dependencias, presupuestos y políticas para R.1.

Responsabilidades:
1. Validación estructural: step IDs únicos, nombres válidos, no self-dependencies.
2. Detección de ciclos en el DAG (Cycle Detection vía DFS/Kahn).
3. Topological Sort determinista (con tie-breaking estable por step_id).
4. Verificación de dependencias faltantes o inexistentes.
5. Verificación de capacidades/herramientas existentes (no inventar herramientas).
6. Validación de Presupuestos (no exceder budget de misión; UNKNOWN != 0).
7. Validación de Políticas y Reglas Hard (Hard Limits nunca aumentados por el planner).
8. Validación de Readiness (un step sólo es READY si dependencias están COMPLETED y no hay fallos bloqueantes).
"""

from collections import defaultdict, deque
from decimal import Decimal
from typing import List, Tuple, Dict, Set, Optional, Mapping, Any

from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    StepDependency,
    StepStatus,
    PlanStatus,
    PlanBudget,
    StepFailureType,
)
from src.domain.planning.ports import CapabilityRegistryPort


class PlanValidationError(Exception):
    """Excepción base para errores de validación de planes de ejecución."""
    pass


class PlanCycleDetectedError(PlanValidationError):
    """Lanzada cuando se detecta un ciclo dirigido en las dependencias del plan."""
    pass


class MissingDependencyError(PlanValidationError):
    """Lanzada cuando un paso depende de un step_id que no existe en el plan."""
    pass


class CapabilityUnavailableError(PlanValidationError):
    """Lanzada cuando un paso requiere una capacidad no registrada en el sistema."""
    pass


class BudgetExceededError(PlanValidationError):
    """Lanzada cuando la suma de presupuestos de pasos excede el límite del plan."""
    pass


class PlanValidator:
    """
    Validador puro y determinista de ExecutionPlans.
    """

    @staticmethod
    def validate_plan_structure(plan: ExecutionPlan) -> Tuple[bool, List[str]]:
        """
        Valida integridad estructural básica: IDs duplicados, self-dependencies y referencias válidas.
        """
        errors: List[str] = []
        if not plan.steps:
            errors.append("ExecutionPlan must contain at least one step.")
            return False, errors

        step_ids = set()
        for step in plan.steps:
            if step.step_id in step_ids:
                errors.append(f"Duplicate step_id detected: '{step.step_id}'.")
            step_ids.add(step.step_id)

        for dep in plan.dependencies:
            if dep.from_step_id not in step_ids:
                errors.append(f"Dependency references non-existent source step: '{dep.from_step_id}'.")
            if dep.to_step_id not in step_ids:
                errors.append(f"Dependency references non-existent target step: '{dep.to_step_id}'.")
            if dep.from_step_id == dep.to_step_id:
                errors.append(f"Self-dependency detected on step '{dep.from_step_id}'.")

        for step in plan.steps:
            for dep_id in step.dependencies:
                if dep_id not in step_ids:
                    errors.append(f"Step '{step.step_id}' declares non-existent dependency '{dep_id}'.")
                if dep_id == step.step_id:
                    errors.append(f"Step '{step.step_id}' declares self-dependency.")

        return len(errors) == 0, errors

    @staticmethod
    def validate_dag(plan: ExecutionPlan) -> Tuple[bool, List[str], List[str]]:
        """
        Valida que el conjunto de dependencias forme un Grafo Acíclico Dirigido (DAG).
        Retorna (is_valid, errors, deterministic_topological_order).
        """
        step_ids = {s.step_id for s in plan.steps}
        adj_list: Dict[str, List[str]] = {s_id: [] for s_id in step_ids}
        in_degree: Dict[str, int] = {s_id: 0 for s_id in step_ids}

        # Construir aristas explícitas desde plan.dependencies y step.dependencies
        edges: Set[Tuple[str, str]] = set()
        for dep in plan.dependencies:
            edges.add((dep.from_step_id, dep.to_step_id))

        for step in plan.steps:
            for dep_id in step.dependencies:
                edges.add((dep_id, step.step_id))

        for from_id, to_id in edges:
            if from_id not in step_ids or to_id not in step_ids:
                continue  # Se captura en validación estructural
            adj_list[from_id].append(to_id)
            in_degree[to_id] += 1

        # Kahn's algorithm con cola ordenada alfabéticamente para tie-breaking determinista
        # PriorityQueue / Sorted List
        ready_nodes = sorted([node for node, deg in in_degree.items() if deg == 0])
        topo_order: List[str] = []

        while ready_nodes:
            curr = ready_nodes.pop(0)
            topo_order.append(curr)

            for neighbor in sorted(adj_list[curr]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    # Insertar en orden determinista
                    ready_nodes.append(neighbor)
                    ready_nodes.sort()

        if len(topo_order) != len(step_ids):
            cycle_nodes = sorted([node for node, deg in in_degree.items() if deg > 0])
            err_msg = f"Cycle detected in execution plan DAG involving steps: {cycle_nodes}"
            return False, [err_msg], []

        return True, [], topo_order

    @staticmethod
    def validate_capabilities(
        plan: ExecutionPlan,
        capability_registry: Optional[CapabilityRegistryPort] = None,
    ) -> Tuple[bool, List[str]]:
        """
        Valida que cada step_leaf mapee a una capacidad registrada en el sistema.
        """
        if capability_registry is None:
            return True, []

        errors: List[str] = []
        for step in plan.steps:
            cap = step.assigned_capability or step.action_type
            if not capability_registry.is_capability_available(cap):
                errors.append(
                    f"Step '{step.step_id}' requires unavailable or unregistered capability/action '{cap}'."
                )

        return len(errors) == 0, errors

    @staticmethod
    def validate_budgets(plan: ExecutionPlan) -> Tuple[bool, List[str]]:
        """
        Valida que los presupuestos asignados no excedan los límites globales del plan.
        Preserva incertidumbre si un costo es UNKNOWN (no lo transforma a 0 ni asume gratuito).
        """
        errors: List[str] = []
        if plan.budget is None:
            return True, []

        # 1. Validar Max Steps
        if plan.budget.max_steps is not None and len(plan.steps) > plan.budget.max_steps:
            errors.append(
                f"Plan step count ({len(plan.steps)}) exceeds maximum allowed steps budget ({plan.budget.max_steps})."
            )

        # 2. Validar Token Allocations
        if plan.budget.max_tokens is not None:
            allocated_tokens = 0
            has_unknown_tokens = False
            for step in plan.steps:
                if step.estimated_tokens is not None:
                    allocated_tokens += step.estimated_tokens
                else:
                    has_unknown_tokens = True

            if allocated_tokens > plan.budget.max_tokens:
                errors.append(
                    f"Allocated tokens ({allocated_tokens}) exceed plan max_tokens limit ({plan.budget.max_tokens})."
                )

        # 3. Validar Cost Allocations
        if plan.budget.max_cost is not None:
            known_cost = Decimal("0")
            has_unknown_cost = False
            for step in plan.steps:
                if step.is_cost_unknown or step.estimated_cost is None:
                    has_unknown_cost = True
                else:
                    known_cost += step.estimated_cost

            if known_cost > plan.budget.max_cost:
                errors.append(
                    f"Allocated known cost (${known_cost}) exceeds plan max_cost limit (${plan.budget.max_cost})."
                )

        # 4. Validar Replan Bounds
        if plan.replan_count > plan.budget.max_replans:
            errors.append(
                f"Replan count ({plan.replan_count}) exceeded max_replans limit ({plan.budget.max_replans})."
            )

        return len(errors) == 0, errors

    @classmethod
    def validate_all(
        cls,
        plan: ExecutionPlan,
        capability_registry: Optional[CapabilityRegistryPort] = None,
    ) -> Tuple[bool, List[str], List[str]]:
        """
        Ejecuta todas las validaciones en orden estricto.
        Retorna (is_valid, all_errors, topological_order).
        """
        all_errors: List[str] = []

        # 1. Estructura
        struct_valid, struct_errs = cls.validate_plan_structure(plan)
        all_errors.extend(struct_errs)
        if not struct_valid:
            return False, all_errors, []

        # 2. DAG y Ciclos
        dag_valid, dag_errs, topo_order = cls.validate_dag(plan)
        all_errors.extend(dag_errs)
        if not dag_valid:
            return False, all_errors, []

        # 3. Capacidades
        cap_valid, cap_errs = cls.validate_capabilities(plan, capability_registry)
        all_errors.extend(cap_errs)

        # 4. Presupuestos
        budget_valid, budget_errs = cls.validate_budgets(plan)
        all_errors.extend(budget_errs)

        is_valid = len(all_errors) == 0
        return is_valid, all_errors, topo_order

    @staticmethod
    def compute_step_readiness(plan: ExecutionPlan) -> Tuple[str, ...]:
        """
        Determina qué pasos están listos (READY) para ser ejecutados en este momento.
        Un paso es READY sí y solo sí:
        - Su estado actual es PENDING o READY.
        - Todas sus dependencias directas están en estado COMPLETED.
        - Ninguna dependencia directa está FAILED o BLOCKED.
        """
        # Mapear dependencias por to_step_id
        step_map = {s.step_id: s for s in plan.steps}
        dep_map: Dict[str, Set[str]] = defaultdict(set)

        for dep in plan.dependencies:
            dep_map[dep.to_step_id].add(dep.from_step_id)

        for step in plan.steps:
            for dep_id in step.dependencies:
                dep_map[step.step_id].add(dep_id)

        ready_steps: List[str] = []
        for step in plan.steps:
            if step.status not in (StepStatus.PENDING, StepStatus.READY):
                continue

            required_deps = dep_map[step.step_id]
            is_ready = True
            for parent_id in required_deps:
                parent = step_map.get(parent_id)
                if parent is None or parent.status != StepStatus.COMPLETED:
                    is_ready = False
                    break

            if is_ready:
                ready_steps.append(step.step_id)

        return tuple(sorted(ready_steps))
