"""
Adaptadores de Repositorio para Catálogo de Planes y Asignaciones SaaS (Hito O.8 — Plans & Pricing Tiers).

Define:
- InMemoryPlanCatalogRepository
- JsonPlanCatalogRepository
- InMemoryPlanAssignmentRepository
- JsonPlanAssignmentRepository
"""

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import threading
from typing import Optional, List, Dict, Any, Union

from src.domain.security.models import validate_safe_identifier
from src.domain.quota_management.models import (
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
)
from src.domain.plans.models import (
    Plan,
    PlanTier,
    PlanStatus,
    PlanFeature,
    PlanLimits,
    PlanQuotaTemplate,
    PlanAssignment,
    PlanAssignmentStatus,
    PlanIntegrityError,
)
from src.domain.plans.ports import (
    PlanCatalogRepositoryPort,
    PlanAssignmentRepositoryPort,
)


class InMemoryPlanCatalogRepository(PlanCatalogRepositoryPort):
    """Repositorio en memoria thread-safe del catálogo de planes."""

    def __init__(self, initial_plans: Optional[List[Plan]] = None):
        self._lock = threading.RLock()
        # Clave: (plan_id, version) -> Plan
        self._plans_by_key: Dict[tuple, Plan] = {}
        if initial_plans:
            for p in initial_plans:
                self.save_plan(p)

    def get_plan(self, plan_id: str, version: Optional[str] = None) -> Optional[Plan]:
        validate_safe_identifier(plan_id, "plan_id")
        with self._lock:
            if version:
                return self._plans_by_key.get((plan_id, version))
            # Obtener la versión activa más reciente
            matching = [p for (pid, v), p in self._plans_by_key.items() if pid == plan_id]
            if not matching:
                return None
            active = [p for p in matching if p.status == PlanStatus.ACTIVE]
            candidates = active if active else matching
            # Ordenar por versión descendente
            return sorted(candidates, key=lambda p: p.version, reverse=True)[0]

    def save_plan(self, plan: Plan) -> None:
        validate_safe_identifier(plan.plan_id, "plan_id")
        if not plan.verify_integrity():
            raise PlanIntegrityError(f"Plan checksum validation failed for {plan.plan_id} v{plan.version}")
        with self._lock:
            self._plans_by_key[(plan.plan_id, plan.version)] = plan

    def list_plans(self, active_only: bool = True) -> List[Plan]:
        with self._lock:
            plans = list(self._plans_by_key.values())
            if active_only:
                plans = [p for p in plans if p.status == PlanStatus.ACTIVE]
            return sorted(plans, key=lambda p: (p.plan_id, p.version))

    def list_plan_versions(self, plan_id: str) -> List[Plan]:
        validate_safe_identifier(plan_id, "plan_id")
        with self._lock:
            matching = [p for (pid, v), p in self._plans_by_key.items() if pid == plan_id]
            return sorted(matching, key=lambda p: p.version, reverse=True)


class InMemoryPlanAssignmentRepository(PlanAssignmentRepositoryPort):
    """Repositorio en memoria thread-safe de asignaciones de planes particionado por tenant."""

    def __init__(self):
        self._lock = threading.RLock()
        # tenant_id -> list of PlanAssignment
        self._assignments_by_tenant: Dict[str, List[PlanAssignment]] = {}

    def save_assignment(self, assignment: PlanAssignment) -> None:
        validate_safe_identifier(assignment.tenant_id, "tenant_id")
        if not assignment.verify_integrity():
            raise PlanIntegrityError(f"PlanAssignment checksum validation failed for {assignment.assignment_id}")
        with self._lock:
            t_list = self._assignments_by_tenant.setdefault(assignment.tenant_id, [])
            # Reemplazar si existe con mismo ID, o agregar
            idx = next((i for i, a in enumerate(t_list) if a.assignment_id == assignment.assignment_id), None)
            if idx is not None:
                t_list[idx] = assignment
            else:
                t_list.append(assignment)

    def get_assignment(self, assignment_id: str) -> Optional[PlanAssignment]:
        validate_safe_identifier(assignment_id, "assignment_id")
        with self._lock:
            for t_list in self._assignments_by_tenant.values():
                for a in t_list:
                    if a.assignment_id == assignment_id:
                        return a
            return None

    def get_active_assignment(self, tenant_id: str, current_time: datetime) -> Optional[PlanAssignment]:
        validate_safe_identifier(tenant_id, "tenant_id")
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        with self._lock:
            t_list = self._assignments_by_tenant.get(tenant_id, [])
            active = [a for a in t_list if a.is_effective(current_time)]
            if not active:
                return None
            # Si hay múltiples, elegir la asignación con effective_from más reciente
            return sorted(active, key=lambda a: a.effective_from, reverse=True)[0]

    def list_assignments_for_tenant(self, tenant_id: str) -> List[PlanAssignment]:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            t_list = self._assignments_by_tenant.get(tenant_id, [])
            return sorted(t_list, key=lambda a: a.assigned_at, reverse=True)


class JsonPlanCatalogRepository(PlanCatalogRepositoryPort):
    """
    Repositorio JSON en disco para el catálogo de planes:
    `base_dir / "plans" / "catalog.json"`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_catalog_file(self) -> Path:
        p_dir = self.base_dir / "plans"
        p_dir.mkdir(parents=True, exist_ok=True)
        return p_dir / "catalog.json"

    def _plan_to_dict(self, plan: Plan) -> Dict[str, Any]:
        rules = []
        for r in plan.quota_template.rules:
            rules.append({
                "rule_id": r.rule_id,
                "quota_type": r.quota_type.value,
                "limit_value": str(r.limit_value) if isinstance(r.limit_value, Decimal) else r.limit_value,
                "scope": r.scope.value,
                "window_type": r.window_type.value,
                "target_identifier": r.target_identifier,
                "is_hard_limit": r.is_hard_limit,
                "custom_window_seconds": r.custom_window_seconds,
                "allow_cache_hit_bypass_token_budget": r.allow_cache_hit_bypass_token_budget,
            })

        return {
            "plan_id": plan.plan_id,
            "name": plan.name,
            "tier": plan.tier.value,
            "version": plan.version,
            "status": plan.status.value,
            "features": [f.value for f in plan.features],
            "limits": {
                "max_users": plan.limits.max_users,
                "max_organizations": plan.limits.max_organizations,
                "max_concurrent_missions": plan.limits.max_concurrent_missions,
                "max_marketplace_accounts": plan.limits.max_marketplace_accounts,
                "custom_limits": dict(plan.limits.custom_limits),
            },
            "quota_template": {
                "is_unlimited": plan.quota_template.is_unlimited,
                "description": plan.quota_template.description,
                "rules": rules,
            },
            "allowed_model_classes": list(plan.allowed_model_classes),
            "allowed_providers": list(plan.allowed_providers),
            "metadata": dict(plan.metadata),
            "checksum": plan.checksum,
        }

    def _dict_to_plan(self, d: Dict[str, Any]) -> Plan:
        rules = []
        for r_dict in d.get("quota_template", {}).get("rules", []):
            q_type = QuotaType(r_dict["quota_type"])
            lim = Decimal(str(r_dict["limit_value"])) if q_type == QuotaType.MAX_COST else int(r_dict["limit_value"])
            rules.append(QuotaRule(
                rule_id=r_dict["rule_id"],
                quota_type=q_type,
                limit_value=lim,
                scope=QuotaScope(r_dict.get("scope", QuotaScope.TENANT.value)),
                window_type=QuotaWindowType(r_dict.get("window_type", QuotaWindowType.DAY.value)),
                target_identifier=r_dict.get("target_identifier"),
                is_hard_limit=r_dict.get("is_hard_limit", True),
                custom_window_seconds=r_dict.get("custom_window_seconds"),
                allow_cache_hit_bypass_token_budget=r_dict.get("allow_cache_hit_bypass_token_budget", True),
            ))

        features = [PlanFeature(f) for f in d.get("features", [])]
        limits_dict = d.get("limits", {})
        limits = PlanLimits(
            max_users=limits_dict.get("max_users", 1),
            max_organizations=limits_dict.get("max_organizations", 1),
            max_concurrent_missions=limits_dict.get("max_concurrent_missions", 1),
            max_marketplace_accounts=limits_dict.get("max_marketplace_accounts", 1),
            custom_limits=limits_dict.get("custom_limits", {}),
        )

        quota_template = PlanQuotaTemplate(
            rules=tuple(rules),
            is_unlimited=d.get("quota_template", {}).get("is_unlimited", False),
            description=d.get("quota_template", {}).get("description"),
        )

        return Plan(
            plan_id=d["plan_id"],
            name=d["name"],
            tier=PlanTier(d["tier"]),
            version=d.get("version", "1.0.0"),
            status=PlanStatus(d.get("status", PlanStatus.ACTIVE.value)),
            features=tuple(features),
            limits=limits,
            quota_template=quota_template,
            allowed_model_classes=tuple(d.get("allowed_model_classes", [])),
            allowed_providers=tuple(d.get("allowed_providers", [])),
            metadata=d.get("metadata", {}),
            checksum=d.get("checksum", ""),
        )

    def _read_all(self) -> List[Plan]:
        f = self._get_catalog_file()
        if not f.exists():
            return []
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return [self._dict_to_plan(item) for item in data]
        except Exception:
            return []

    def _write_all(self, plans: List[Plan]) -> None:
        f = self._get_catalog_file()
        data = [self._plan_to_dict(p) for p in plans]
        tmp_file = f.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
        tmp_file.replace(f)

    def get_plan(self, plan_id: str, version: Optional[str] = None) -> Optional[Plan]:
        validate_safe_identifier(plan_id, "plan_id")
        with self._lock:
            all_plans = self._read_all()
            matching = [p for p in all_plans if p.plan_id == plan_id]
            if not matching:
                return None
            if version:
                for p in matching:
                    if p.version == version:
                        return p
                return None
            active = [p for p in matching if p.status == PlanStatus.ACTIVE]
            candidates = active if active else matching
            return sorted(candidates, key=lambda p: p.version, reverse=True)[0]

    def save_plan(self, plan: Plan) -> None:
        validate_safe_identifier(plan.plan_id, "plan_id")
        if not plan.verify_integrity():
            raise PlanIntegrityError(f"Plan checksum validation failed for {plan.plan_id} v{plan.version}")
        with self._lock:
            all_plans = self._read_all()
            idx = next((i for i, p in enumerate(all_plans) if p.plan_id == plan.plan_id and p.version == plan.version), None)
            if idx is not None:
                all_plans[idx] = plan
            else:
                all_plans.append(plan)
            self._write_all(all_plans)

    def list_plans(self, active_only: bool = True) -> List[Plan]:
        with self._lock:
            plans = self._read_all()
            if active_only:
                plans = [p for p in plans if p.status == PlanStatus.ACTIVE]
            return sorted(plans, key=lambda p: (p.plan_id, p.version))

    def list_plan_versions(self, plan_id: str) -> List[Plan]:
        validate_safe_identifier(plan_id, "plan_id")
        with self._lock:
            plans = self._read_all()
            matching = [p for p in plans if p.plan_id == plan_id]
            return sorted(matching, key=lambda p: p.version, reverse=True)


class JsonPlanAssignmentRepository(PlanAssignmentRepositoryPort):
    """
    Repositorio JSON en disco para asignaciones de plan tenant-scoped:
    `base_dir / "tenants" / {tenant_id} / "plans" / "assignments.json"`
    """

    def __init__(self, base_dir: Union[str, Path]):
        self.base_dir = Path(base_dir)
        self._lock = threading.RLock()

    def _get_tenant_file(self, tenant_id: str) -> Path:
        validate_safe_identifier(tenant_id, "tenant_id")
        t_dir = self.base_dir / "tenants" / tenant_id / "plans"
        t_dir.mkdir(parents=True, exist_ok=True)
        return t_dir / "assignments.json"

    def _assignment_to_dict(self, a: PlanAssignment) -> Dict[str, Any]:
        return {
            "assignment_id": a.assignment_id,
            "tenant_id": a.tenant_id,
            "plan_id": a.plan_id,
            "plan_version": a.plan_version,
            "assigned_at": a.assigned_at.isoformat(),
            "effective_from": a.effective_from.isoformat(),
            "status": a.status.value,
            "effective_until": a.effective_until.isoformat() if a.effective_until else None,
            "source_reason": a.source_reason,
            "assigned_by_actor_id": a.assigned_by_actor_id,
            "metadata": dict(a.metadata),
            "checksum": a.checksum,
        }

    def _dict_to_assignment(self, d: Dict[str, Any]) -> PlanAssignment:
        eff_until = datetime.fromisoformat(d["effective_until"]) if d.get("effective_until") else None
        return PlanAssignment(
            assignment_id=d["assignment_id"],
            tenant_id=d["tenant_id"],
            plan_id=d["plan_id"],
            plan_version=d["plan_version"],
            assigned_at=datetime.fromisoformat(d["assigned_at"]),
            effective_from=datetime.fromisoformat(d["effective_from"]),
            status=PlanAssignmentStatus(d.get("status", PlanAssignmentStatus.ACTIVE.value)),
            effective_until=eff_until,
            source_reason=d.get("source_reason", "INITIAL_ASSIGNMENT"),
            assigned_by_actor_id=d.get("assigned_by_actor_id"),
            metadata=d.get("metadata", {}),
            checksum=d.get("checksum", ""),
        )

    def _read_tenant_assignments(self, tenant_id: str) -> List[PlanAssignment]:
        f = self._get_tenant_file(tenant_id)
        if not f.exists():
            return []
        try:
            with open(f, "r", encoding="utf-8") as fp:
                data = json.load(fp)
                return [self._dict_to_assignment(item) for item in data]
        except Exception:
            return []

    def _write_tenant_assignments(self, tenant_id: str, assignments: List[PlanAssignment]) -> None:
        f = self._get_tenant_file(tenant_id)
        data = [self._assignment_to_dict(a) for a in assignments]
        tmp_file = f.with_suffix(".tmp")
        with open(tmp_file, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=2)
        tmp_file.replace(f)

    def save_assignment(self, assignment: PlanAssignment) -> None:
        validate_safe_identifier(assignment.tenant_id, "tenant_id")
        if not assignment.verify_integrity():
            raise PlanIntegrityError(f"PlanAssignment checksum validation failed for {assignment.assignment_id}")
        with self._lock:
            assignments = self._read_tenant_assignments(assignment.tenant_id)
            idx = next((i for i, a in enumerate(assignments) if a.assignment_id == assignment.assignment_id), None)
            if idx is not None:
                assignments[idx] = assignment
            else:
                assignments.append(assignment)
            self._write_tenant_assignments(assignment.tenant_id, assignments)

    def get_assignment(self, assignment_id: str) -> Optional[PlanAssignment]:
        validate_safe_identifier(assignment_id, "assignment_id")
        with self._lock:
            # Buscar en directorio base de tenants
            tenants_dir = self.base_dir / "tenants"
            if not tenants_dir.exists():
                return None
            for t_path in tenants_dir.iterdir():
                if t_path.is_dir():
                    assignments = self._read_tenant_assignments(t_path.name)
                    for a in assignments:
                        if a.assignment_id == assignment_id:
                            return a
            return None

    def get_active_assignment(self, tenant_id: str, current_time: datetime) -> Optional[PlanAssignment]:
        validate_safe_identifier(tenant_id, "tenant_id")
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=timezone.utc)
        with self._lock:
            assignments = self._read_tenant_assignments(tenant_id)
            active = [a for a in assignments if a.is_effective(current_time)]
            if not active:
                return None
            return sorted(active, key=lambda a: a.effective_from, reverse=True)[0]

    def list_assignments_for_tenant(self, tenant_id: str) -> List[PlanAssignment]:
        validate_safe_identifier(tenant_id, "tenant_id")
        with self._lock:
            assignments = self._read_tenant_assignments(tenant_id)
            return sorted(assignments, key=lambda a: a.assigned_at, reverse=True)
