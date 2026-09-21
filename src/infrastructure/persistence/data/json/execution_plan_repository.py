"""
Adaptador de Repositorio en memoria y archivo JSON para ExecutionPlan (Hito R.1).

Implementa:
- InMemoryExecutionPlanRepository
- JsonExecutionPlanRepository

Garantías:
- Aislamiento multi-tenant estricto vía CrossTenantGuard.
- Persistencia inmutable y versionado determinista.
- Búsqueda segura por plan_id y mission_id.
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
from types import MappingProxyType
from typing import Optional, List, Dict, Any, Union, Tuple

from src.domain.security.models import validate_safe_identifier
from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    StepDependency,
    PlanBudget,
    PlanVersionHistory,
    PlanStatus,
    StepStatus,
    StepFailureType,
    DependencyType,
    StepRationale,
)
from src.domain.planning.ports import ExecutionPlanRepositoryPort


class InMemoryExecutionPlanRepository(ExecutionPlanRepositoryPort):
    """
    Implementación en memoria aislada por tenant para testing y ejecución rápida.
    """

    def __init__(self):
        self._plans: Dict[Tuple[str, str], ExecutionPlan] = {}  # (tenant_id, plan_id) -> ExecutionPlan
        self._lock = threading.RLock()

    def save_plan(self, plan: ExecutionPlan, tenant_context: TenantContext) -> ExecutionPlan:
        CrossTenantGuard.assert_same_tenant(tenant_context, plan.tenant_id, "save_plan")
        with self._lock:
            key = (plan.tenant_id, plan.plan_id)
            self._plans[key] = plan
            return plan

    def get_plan_by_id(self, plan_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            key = (tenant_id_val, plan_id)
            return self._plans.get(key)

    def get_plan_by_mission_id(self, mission_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            tenant_plans = [
                p for (t_id, _), p in self._plans.items()
                if t_id == tenant_id_val and p.mission_id == mission_id
            ]
            if not tenant_plans:
                return None
            return sorted(tenant_plans, key=lambda p: (p.version, p.updated_at), reverse=True)[0]

    def list_plans_for_mission(self, mission_id: str, tenant_context: TenantContext) -> List[ExecutionPlan]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            tenant_plans = [
                p for (t_id, _), p in self._plans.items()
                if t_id == tenant_id_val and p.mission_id == mission_id
            ]
            return sorted(tenant_plans, key=lambda p: p.version)


class JsonExecutionPlanRepository(ExecutionPlanRepositoryPort):
    """
    Implementación en archivos JSON tenant-isolated para persistencia durable.
    """

    def __init__(self, base_dir: Union[str, Path] = "data/plans"):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_tenant_dir(self, tenant_id: str) -> Path:
        p = self.base_dir / tenant_id
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _plan_to_dict(self, plan: ExecutionPlan) -> Dict[str, Any]:
        return {
            "plan_id": plan.plan_id,
            "mission_id": plan.mission_id,
            "tenant_id": plan.tenant_id,
            "goal": plan.goal,
            "version": plan.version,
            "status": plan.status.value,
            "replan_count": plan.replan_count,
            "created_at": plan.created_at.isoformat(),
            "updated_at": plan.updated_at.isoformat(),
            "checksum": plan.checksum,
            "budget": {
                "max_tokens": plan.budget.max_tokens,
                "max_cost": str(plan.budget.max_cost) if plan.budget.max_cost is not None else None,
                "max_steps": plan.budget.max_steps,
                "max_wall_clock_seconds": plan.budget.max_wall_clock_seconds,
                "max_replans": plan.budget.max_replans,
                "is_cost_unknown": plan.budget.is_cost_unknown,
            } if plan.budget else None,
            "steps": [
                {
                    "step_id": s.step_id,
                    "objective": s.objective,
                    "action_type": s.action_type,
                    "required_inputs": dict(s.required_inputs),
                    "expected_outputs": list(s.expected_outputs),
                    "dependencies": list(s.dependencies),
                    "assigned_capability": s.assigned_capability,
                    "assigned_agent": s.assigned_agent,
                    "status": s.status.value,
                    "failure_type": s.failure_type.value if s.failure_type else None,
                    "failure_reason": s.failure_reason,
                    "estimated_cost": str(s.estimated_cost) if s.estimated_cost is not None else None,
                    "is_cost_unknown": s.is_cost_unknown,
                    "estimated_tokens": s.estimated_tokens,
                    "actual_outputs": dict(s.actual_outputs),
                    "parent_goal_id": s.parent_goal_id,
                    "depth_level": s.depth_level,
                    "metadata": dict(s.metadata),
                    "rationale": {
                        "objective": s.rationale.objective,
                        "dependency_reason": s.rationale.dependency_reason,
                        "capability_selected": s.rationale.capability_selected,
                        "budget_reason": s.rationale.budget_reason,
                        "replan_reason": s.rationale.replan_reason,
                    } if s.rationale else None,
                }
                for s in plan.steps
            ],
            "dependencies": [
                {
                    "from_step_id": d.from_step_id,
                    "to_step_id": d.to_step_id,
                    "dependency_type": d.dependency_type.value,
                    "required_output_keys": list(d.required_output_keys),
                    "description": d.description,
                }
                for d in plan.dependencies
            ],
            "history": [
                {
                    "version": h.version,
                    "replan_reason": h.replan_reason,
                    "replan_event_type": h.replan_event_type.value if h.replan_event_type else None,
                    "changed_step_ids": list(h.changed_step_ids),
                    "preserved_step_ids": list(h.preserved_step_ids),
                    "created_at": h.created_at.isoformat(),
                }
                for h in plan.history
            ],
            "metadata": dict(plan.metadata),
        }

    def _dict_to_plan(self, d: Dict[str, Any]) -> ExecutionPlan:
        steps = []
        for s_dict in d.get("steps", []):
            rat_dict = s_dict.get("rationale")
            rat = StepRationale(
                objective=rat_dict["objective"],
                dependency_reason=rat_dict.get("dependency_reason", ""),
                capability_selected=rat_dict.get("capability_selected", ""),
                budget_reason=rat_dict.get("budget_reason", ""),
                replan_reason=rat_dict.get("replan_reason", ""),
            ) if rat_dict else None

            est_cost_val = s_dict.get("estimated_cost")
            est_cost = Decimal(str(est_cost_val)) if est_cost_val is not None else None

            fail_t_val = s_dict.get("failure_type")
            fail_t = StepFailureType(fail_t_val) if fail_t_val else None

            steps.append(PlanStep(
                step_id=s_dict["step_id"],
                objective=s_dict["objective"],
                action_type=s_dict["action_type"],
                required_inputs=s_dict.get("required_inputs", {}),
                expected_outputs=tuple(s_dict.get("expected_outputs", [])),
                dependencies=tuple(s_dict.get("dependencies", [])),
                assigned_capability=s_dict.get("assigned_capability"),
                assigned_agent=s_dict.get("assigned_agent"),
                status=StepStatus(s_dict.get("status", StepStatus.PENDING.value)),
                failure_type=fail_t,
                failure_reason=s_dict.get("failure_reason"),
                estimated_cost=est_cost,
                is_cost_unknown=s_dict.get("is_cost_unknown", False),
                estimated_tokens=s_dict.get("estimated_tokens"),
                actual_outputs=s_dict.get("actual_outputs", {}),
                rationale=rat,
                parent_goal_id=s_dict.get("parent_goal_id"),
                depth_level=s_dict.get("depth_level", 1),
                metadata=s_dict.get("metadata", {}),
            ))

        deps = []
        for d_dict in d.get("dependencies", []):
            deps.append(StepDependency(
                from_step_id=d_dict["from_step_id"],
                to_step_id=d_dict["to_step_id"],
                dependency_type=DependencyType(d_dict.get("dependency_type", DependencyType.HARD.value)),
                required_output_keys=tuple(d_dict.get("required_output_keys", [])),
                description=d_dict.get("description", ""),
            ))

        history = []
        for h_dict in d.get("history", []):
            evt_val = h_dict.get("replan_event_type")
            evt = StepFailureType(evt_val) if evt_val else None
            history.append(PlanVersionHistory(
                version=h_dict["version"],
                replan_reason=h_dict["replan_reason"],
                replan_event_type=evt,
                changed_step_ids=tuple(h_dict.get("changed_step_ids", [])),
                preserved_step_ids=tuple(h_dict.get("preserved_step_ids", [])),
                created_at=datetime.fromisoformat(h_dict["created_at"]),
            ))

        budget = None
        if d.get("budget"):
            b_dict = d["budget"]
            b_cost_val = b_dict.get("max_cost")
            b_cost = Decimal(str(b_cost_val)) if b_cost_val is not None else None
            budget = PlanBudget(
                max_tokens=b_dict.get("max_tokens"),
                max_cost=b_cost,
                max_steps=b_dict.get("max_steps"),
                max_wall_clock_seconds=b_dict.get("max_wall_clock_seconds"),
                max_replans=b_dict.get("max_replans", 3),
                is_cost_unknown=b_dict.get("is_cost_unknown", False),
            )

        return ExecutionPlan(
            plan_id=d["plan_id"],
            mission_id=d["mission_id"],
            tenant_id=d["tenant_id"],
            goal=d["goal"],
            steps=tuple(steps),
            dependencies=tuple(deps),
            version=d.get("version", 1),
            status=PlanStatus(d.get("status", PlanStatus.DRAFT.value)),
            budget=budget,
            history=tuple(history),
            replan_count=d.get("replan_count", 0),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
            metadata=d.get("metadata", {}),
            checksum=d.get("checksum", ""),
        )

    def save_plan(self, plan: ExecutionPlan, tenant_context: TenantContext) -> ExecutionPlan:
        CrossTenantGuard.assert_same_tenant(tenant_context, plan.tenant_id, "save_plan")
        with self._lock:
            tenant_dir = self._get_tenant_dir(plan.tenant_id)
            plan_file = tenant_dir / f"{plan.plan_id}_v{plan.version}.json"
            data = self._plan_to_dict(plan)
            with open(plan_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return plan

    def get_plan_by_id(self, plan_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            tenant_dir = self._get_tenant_dir(tenant_id_val)
            # Buscar archivo de versión más reciente
            matches = list(tenant_dir.glob(f"{plan_id}_v*.json"))
            if not matches:
                return None
            matches.sort(key=lambda p: p.name, reverse=True)
            with open(matches[0], "r", encoding="utf-8") as f:
                d = json.load(f)
            return self._dict_to_plan(d)

    def get_plan_by_mission_id(self, mission_id: str, tenant_context: TenantContext) -> Optional[ExecutionPlan]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            tenant_dir = self._get_tenant_dir(tenant_id_val)
            all_plans = []
            for file_path in tenant_dir.glob("*.json"):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("mission_id") == mission_id:
                        all_plans.append(self._dict_to_plan(d))
                except Exception:
                    continue

            if not all_plans:
                return None
            return sorted(all_plans, key=lambda p: (p.version, p.updated_at), reverse=True)[0]

    def list_plans_for_mission(self, mission_id: str, tenant_context: TenantContext) -> List[ExecutionPlan]:
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        with self._lock:
            tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
            tenant_dir = self._get_tenant_dir(tenant_id_val)
            all_plans = []
            for file_path in tenant_dir.glob("*.json"):
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        d = json.load(f)
                    if d.get("mission_id") == mission_id:
                        all_plans.append(self._dict_to_plan(d))
                except Exception:
                    continue

            return sorted(all_plans, key=lambda p: p.version)
