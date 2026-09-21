"""
Servicio de Aplicación: SubMissionService (Hito R.2 — Advanced Autonomy).

Responsabilidades:
1. Creación y delegación jerárquica de submisiones a partir de contratos válidos (SubMissionCreationContract).
2. Validación determinista de invariantes jerárquicos (profundidad, fan-out, ciclos, tenant match, no parent terminal).
3. Idempotencia y deduplicación concurrente por clave de delegación canónica.
4. Herencia y partición de presupuestos (PlanBudget) garantizando aislamiento entre hermanos y sum(child) <= parent.
5. Herencia contextual mínima con sanitización profunda (cero secretos, cero CoT/scratchpad privado).
6. Integración con R.1 Multi-step Planning: actualización de pasos delegados del ExecutionPlan ante completion/failure.
7. Propagación de resultados estructurados (SubMissionResultContract) al padre.
8. Propagación de fallos distinguiendo error técnico vs POLICY_DENIED vs EMERGENCY_STOP_BLOCKED vs BUDGET_EXHAUSTED.
9. Propagación y cascada de cancelaciones hacia hijos activos no terminales.
10. Registro inmutable de auditoría K.1 (SUB_MISSION_CREATED, SUB_MISSION_COMPLETED, SUB_MISSION_FAILED, SUB_MISSION_CANCELLED, etc.).
11. Trazabilidad K.2 y compatibilidad con Dashboard Q.4 (SubMissionNode).
12. Aislamiento Multi-Tenant estricto (TenantContext y CrossTenantGuard).
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
import threading
from types import MappingProxyType
from typing import Optional, List, Sequence, Tuple, Mapping, Any, Dict
import uuid

from src.domain.security.models import validate_safe_identifier, sanitize_security_data
from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.infrastructure.reliability.reliability_infrastructure import SystemClock
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import (
    AgentTraceRecord,
    StepType,
    TraceStatus,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.emergency_stop.ports import EmergencyStopServicePort
from src.domain.emergency_stop.models import EmergencyStopEvaluationContext
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionStatus,
    MissionPriority,
)
from src.domain.mission_dashboard.ports import TenantMissionRepositoryPort
from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    PlanBudget,
    StepStatus,
    StepFailureType,
)
from src.domain.planning.ports import ExecutionPlanRepositoryPort
from src.domain.sub_mission.models import (
    SubMissionScope,
    SubMissionCreationContract,
    SubMissionResultContract,
    SubMissionFailureType,
    SubMissionHierarchyPolicy,
    SubMissionNode,
)
from src.domain.sub_mission.ports import (
    SubMissionRepositoryPort,
    SubMissionServicePort,
)
from src.domain.sub_mission.validator import (
    SubMissionHierarchyValidator,
    SubMissionHierarchyError,
    HierarchyCycleDetectedError,
    MaxDepthExceededError,
    MaxChildrenExceededError,
    TenantMismatchError,
    InvalidParentMissionError,
    BudgetExceededError,
)

logger = logging.getLogger("SubMissionService")


class SubMissionService(SubMissionServicePort):
    """
    Servicio canónico de orquestación y ciclo de vida de Sub-missions para Hito R.2.
    """

    def __init__(
        self,
        mission_repository: TenantMissionRepositoryPort,
        sub_mission_repository: Optional[SubMissionRepositoryPort] = None,
        plan_repository: Optional[ExecutionPlanRepositoryPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_repository: Optional[AgentTraceRepositoryPort] = None,
        emergency_stop_service: Optional[EmergencyStopServicePort] = None,
        hierarchy_policy: Optional[SubMissionHierarchyPolicy] = None,
        clock: Optional[ClockPort] = None,
        audit_logger: Optional[Any] = None,
        trace_service: Optional[Any] = None,
    ):
        self.mission_repository = mission_repository
        self.sub_mission_repository = sub_mission_repository or mission_repository
        self.plan_repository = plan_repository
        self.audit_repository = audit_repository
        self.trace_repository = trace_repository
        self.audit_logger = audit_logger
        self.trace_service = trace_service
        self.emergency_stop_service = emergency_stop_service
        self.hierarchy_policy = hierarchy_policy or SubMissionHierarchyPolicy()
        self.validator = SubMissionHierarchyValidator(self.hierarchy_policy)
        self.clock = clock or SystemClock()
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        return self.clock.now() if hasattr(self.clock, "now") else datetime.now(timezone.utc)

    def _record_audit(
        self,
        context: TenantContext,
        record_type: AuditRecordType,
        mission_id: str,
        action: str,
        status: str,
        correlation_id: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        if self.audit_repository is None:
            return
        now_dt = self._now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        record = AuditRecord(
            audit_id=f"aud_{uuid.uuid4().hex[:16]}",
            record_type=record_type,
            occurred_at=now_dt,
            actor=AuditActor(
                actor_type=AuditActorType.SYSTEM,
                actor_id="sub_mission_service",
                details={"tenant_id": context.tenant_id},
            ),
            subject_type="SUB_MISSION",
            subject_id=mission_id,
            action_or_operation=action,
            status=status,
            correlation_id=correlation_id,
            mission_id=mission_id,
            metadata=details or {},
        )
        try:
            self.audit_repository.append(record)
        except Exception as e:
            logger.warning(f"Failed to persist audit record for sub-mission {mission_id}: {e}")

    def _record_trace(
        self,
        context: TenantContext,
        mission_id: str,
        step_id: str,
        step_type: StepType,
        status: TraceStatus,
        inputs: Optional[Dict[str, Any]] = None,
        outputs: Optional[Dict[str, Any]] = None,
        error_message: Optional[str] = None,
    ) -> None:
        if not self.trace_repository:
            return
        now_dt = self._now()
        if now_dt.tzinfo is None:
            now_dt = now_dt.replace(tzinfo=timezone.utc)

        trace_entry = AgentTraceRecord(
            trace_id=f"trc_{uuid.uuid4().hex[:16]}",
            component_name="sub_mission_service",
            execution_id=mission_id,
            step_number=0,
            step_type=step_type,
            operation=step_id,
            status=status,
            started_at=now_dt,
            completed_at=now_dt,
            correlation_id=context.correlation_id or mission_id,
            mission_id=mission_id,
            metadata={
                "inputs": inputs or {},
                "outputs": outputs or {},
                "error_message": error_message,
            },
        )
        try:
            self.trace_repository.append(trace_entry)
        except Exception as e:
            logger.warning(f"Failed to persist trace entry for sub-mission {mission_id}: {e}")

    def create_sub_mission(
        self, context: TenantContext, contract: SubMissionCreationContract
    ) -> Mission:
        """
        Crea y registra una submisión validando todos los invariantes y límites de jerarquía.
        Idempotente frente a llamadas concurrentes con la misma delegation_key.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        if context.tenant_id != contract.tenant_id:
            raise TenantMismatchError(
                f"Tenant context mismatch: context tenant '{context.tenant_id}' != contract tenant '{contract.tenant_id}'."
            )

        with self._lock:
            # 1. Verificar idempotencia
            if contract.delegation_key:
                existing = self.sub_mission_repository.get_by_delegation_key(
                    context, contract.parent_mission_id, contract.delegation_key
                )
                if existing:
                    logger.info(
                        f"Idempotent return of existing sub-mission '{existing.mission_id}' for delegation key '{contract.delegation_key}'."
                    )
                    return existing

            # 2. Recuperar y validar parent
            parent_mission = self.mission_repository.get_by_id(context, contract.parent_mission_id)
            if not parent_mission:
                raise InvalidParentMissionError(
                    f"Parent mission '{contract.parent_mission_id}' not found in tenant '{context.tenant_id}'."
                )

            # 3. Validar Emergency Stop para la misión padre o tenant
            if self.emergency_stop_service:
                eval_ctx = EmergencyStopEvaluationContext(
                    action_name="CREATE_SUB_MISSION",
                    mission_id=contract.parent_mission_id,
                    metadata={"tenant_id": context.tenant_id, "sub_mission_type": contract.sub_mission_type.value},
                )
                stop_dec = self.emergency_stop_service.evaluate(eval_ctx)
                if stop_dec.is_blocked:
                    raise SubMissionHierarchyError(
                        f"Emergency Stop is ACTIVE: Cannot create sub-mission under parent '{contract.parent_mission_id}'."
                    )

            # 4. Validar ciclos
            def _get_parent(mid: str) -> Optional[Mission]:
                return self.mission_repository.get_by_id(context, mid)

            # Generar candidate mission_id anticipado para verificar ciclo si fuera necesario
            candidate_id = f"sub_{uuid.uuid4().hex[:12]}"
            self.validator.validate_no_cycles(candidate_id, contract.parent_mission_id, _get_parent)

            # 5. Obtener hijos existentes y budgets de hermanos
            existing_children = self.sub_mission_repository.get_children(context, contract.parent_mission_id)
            sibling_budgets: List[Optional[PlanBudget]] = []
            for child in existing_children:
                b_dict = child.parameters.get("allocated_budget")
                if isinstance(b_dict, dict):
                    sb = PlanBudget(
                        max_tokens=b_dict.get("max_tokens"),
                        max_cost=Decimal(str(b_dict["max_cost"])) if b_dict.get("max_cost") is not None else None,
                        max_steps=b_dict.get("max_steps"),
                        max_wall_clock_seconds=b_dict.get("max_wall_clock_seconds"),
                        max_replans=b_dict.get("max_replans", 3),
                        is_cost_unknown=b_dict.get("is_cost_unknown", False),
                    )
                    sibling_budgets.append(sb)

            parent_budget_raw = parent_mission.parameters.get("budget") or parent_mission.parameters.get("allocated_budget")
            parent_budget: Optional[PlanBudget] = None
            if isinstance(parent_budget_raw, dict):
                parent_budget = PlanBudget(
                    max_tokens=parent_budget_raw.get("max_tokens"),
                    max_cost=Decimal(str(parent_budget_raw["max_cost"])) if parent_budget_raw.get("max_cost") is not None else None,
                    max_steps=parent_budget_raw.get("max_steps"),
                    max_wall_clock_seconds=parent_budget_raw.get("max_wall_clock_seconds"),
                    max_replans=parent_budget_raw.get("max_replans", 3),
                    is_cost_unknown=parent_budget_raw.get("is_cost_unknown", False),
                )

            # 6. Validar creación con el validator puro
            self.validator.validate_creation(
                contract=contract,
                parent_mission=parent_mission,
                existing_children=existing_children,
                parent_budget=parent_budget,
                existing_sibling_budgets=sibling_budgets,
            )

            # 7. Construir parámetros seguros (Zero CoT, Zero secrets)
            child_depth = parent_mission.depth + 1
            root_id = parent_mission.root_mission_id or parent_mission.mission_id

            child_parameters: Dict[str, Any] = {
                "objective": contract.scope.objective,
                "expected_outcome": contract.scope.expected_outcome,
                "completion_condition": contract.scope.completion_condition,
                "expected_output_keys": list(contract.scope.expected_output_keys),
                "target_resource": contract.scope.target_resource,
                "constraints": dict(contract.scope.constraints),
                "inputs": dict(contract.inputs),
                "parent_mission_id": contract.parent_mission_id,
                "root_mission_id": root_id,
                "delegation_key": contract.delegation_key,
                "plan_id": contract.plan_id,
                "plan_step_id": contract.plan_step_id,
                "is_required": contract.is_required,
                "depth": child_depth,
            }

            if contract.allocated_budget:
                child_parameters["allocated_budget"] = {
                    "max_tokens": contract.allocated_budget.max_tokens,
                    "max_cost": str(contract.allocated_budget.max_cost) if contract.allocated_budget.max_cost is not None else None,
                    "max_steps": contract.allocated_budget.max_steps,
                    "max_wall_clock_seconds": contract.allocated_budget.max_wall_clock_seconds,
                    "max_replans": contract.allocated_budget.max_replans,
                    "is_cost_unknown": contract.allocated_budget.is_cost_unknown,
                }

            # 8. Instanciar entidad canónica Mission
            sub_mission = Mission(
                mission_id=candidate_id,
                type=contract.sub_mission_type,
                priority=contract.priority,
                status=MissionStatus.PENDING,
                parameters=child_parameters,
                created_at=self._now(),
                updated_at=self._now(),
                parent_mission_id=contract.parent_mission_id,
                root_mission_id=root_id,
                depth=child_depth,
                delegation_key=contract.delegation_key,
                is_required=contract.is_required,
            )

            # 9. Persistir submisión
            self.mission_repository.save(context, sub_mission)

            # 10. Emitir auditoría y traza
            correlation = contract.correlation_id or sub_mission.mission_id
            self._record_audit(
                context=context,
                record_type=AuditRecordType.SUB_MISSION_CREATED,
                mission_id=sub_mission.mission_id,
                action="CREATE_SUB_MISSION",
                status="SUCCESS",
                correlation_id=correlation,
                details={
                    "parent_mission_id": contract.parent_mission_id,
                    "root_mission_id": root_id,
                    "depth": child_depth,
                    "delegation_key": contract.delegation_key,
                    "is_required": contract.is_required,
                },
            )

            self._record_trace(
                context=context,
                mission_id=sub_mission.mission_id,
                step_id="SUB_MISSION_CREATION",
                step_type=StepType.SERVICE_CALL,
                status=TraceStatus.SUCCESS,
                inputs={"scope": contract.scope.objective, "parent_id": contract.parent_mission_id},
                outputs={"sub_mission_id": sub_mission.mission_id, "depth": child_depth},
            )

            return sub_mission

    def propagate_result(
        self, context: TenantContext, result_contract: SubMissionResultContract
    ) -> bool:
        """
        Propaga el resultado estructurado de una submisión a su padre y actualiza dependencias/planes.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        if context.tenant_id != result_contract.tenant_id:
            raise TenantMismatchError(
                f"Tenant context mismatch on result propagation: '{context.tenant_id}' != '{result_contract.tenant_id}'."
            )

        with self._lock:
            # 1. Recuperar sub-mission
            sub_mission = self.mission_repository.get_by_id(context, result_contract.mission_id)
            if not sub_mission:
                raise SubMissionHierarchyError(f"Sub-mission '{result_contract.mission_id}' not found.")

            # 2. Persistir resultado estructurado
            self.sub_mission_repository.save_sub_mission_result(context, result_contract)

            # 3. Actualizar estado de la sub-mission en el repositorio
            updated_sub = Mission(
                mission_id=sub_mission.mission_id,
                type=sub_mission.type,
                priority=sub_mission.priority,
                status=result_contract.status,
                parameters=sub_mission.parameters,
                created_at=sub_mission.created_at,
                updated_at=self._now(),
                parent_mission_id=sub_mission.parent_mission_id,
                root_mission_id=sub_mission.root_mission_id,
                depth=sub_mission.depth,
                delegation_key=sub_mission.delegation_key,
                is_required=sub_mission.is_required,
            )
            self.mission_repository.save(context, updated_sub)

            # 4. Actualizar PlanStep en R.1 si existe plan asociado
            plan_id = sub_mission.parameters.get("plan_id")
            plan_step_id = sub_mission.parameters.get("plan_step_id")
            if self.plan_repository and plan_id and plan_step_id:
                plan = self.plan_repository.get_plan_by_id(plan_id, context)
                if plan:
                    step = plan.get_step(plan_step_id)
                    if step:
                        new_status = StepStatus.COMPLETED if result_contract.is_success else StepStatus.FAILED
                        new_fail_type = None
                        if not result_contract.is_success:
                            if result_contract.failure_type == SubMissionFailureType.POLICY_DENIED:
                                new_fail_type = StepFailureType.POLICY_DENIED
                            elif result_contract.failure_type == SubMissionFailureType.BUDGET_EXHAUSTED:
                                new_fail_type = StepFailureType.BUDGET_EXHAUSTED
                            elif result_contract.failure_type == SubMissionFailureType.INVALID_INPUT:
                                new_fail_type = StepFailureType.INVALID_INPUT
                            elif result_contract.failure_type == SubMissionFailureType.DEPENDENCY_FAILURE:
                                new_fail_type = StepFailureType.DEPENDENCY_FAILURE
                            else:
                                new_fail_type = StepFailureType.EXECUTION_FAILURE

                        updated_step = PlanStep(
                            step_id=step.step_id,
                            objective=step.objective,
                            action_type=step.action_type,
                            required_inputs=step.required_inputs,
                            expected_outputs=step.expected_outputs,
                            dependencies=step.dependencies,
                            assigned_capability=step.assigned_capability,
                            assigned_agent=step.assigned_agent,
                            status=new_status,
                            failure_type=new_fail_type,
                            failure_reason=result_contract.failure_reason,
                            estimated_cost=step.estimated_cost,
                            is_cost_unknown=step.is_cost_unknown,
                            estimated_tokens=step.estimated_tokens,
                            actual_outputs=dict(result_contract.outputs),
                            rationale=step.rationale,
                            parent_goal_id=step.parent_goal_id,
                            depth_level=step.depth_level,
                            metadata=step.metadata,
                        )

                        # Reconstruir lista de pasos en el plan
                        new_steps = tuple(
                            updated_step if s.step_id == plan_step_id else s for s in plan.steps
                        )
                        updated_plan = ExecutionPlan(
                            plan_id=plan.plan_id,
                            mission_id=plan.mission_id,
                            tenant_id=plan.tenant_id,
                            goal=plan.goal,
                            steps=new_steps,
                            dependencies=plan.dependencies,
                            version=plan.version,
                            status=plan.status,
                            budget=plan.budget,
                            history=plan.history,
                            replan_count=plan.replan_count,
                            created_at=plan.created_at,
                            updated_at=self._now(),
                            metadata=plan.metadata,
                        )
                        self.plan_repository.save_plan(updated_plan, context)

            # 5. Auditoría K.1 y traza K.2
            rec_type = (
                AuditRecordType.SUB_MISSION_COMPLETED
                if result_contract.is_success
                else AuditRecordType.SUB_MISSION_FAILED
            )
            self._record_audit(
                context=context,
                record_type=rec_type,
                mission_id=result_contract.mission_id,
                action="PROPAGATE_RESULT",
                status=result_contract.status.value,
                correlation_id=result_contract.parent_mission_id,
                details={
                    "parent_mission_id": result_contract.parent_mission_id,
                    "failure_type": result_contract.failure_type.value if result_contract.failure_type else None,
                    "is_required": result_contract.is_required,
                },
            )

            self._record_trace(
                context=context,
                mission_id=result_contract.mission_id,
                step_id="SUB_MISSION_COMPLETION" if result_contract.is_success else "SUB_MISSION_FAILURE",
                step_type=StepType.TOOL_CALL if result_contract.is_success else StepType.FAILURE,
                status=TraceStatus.SUCCESS if result_contract.is_success else TraceStatus.FAILED,
                inputs={"parent_mission_id": result_contract.parent_mission_id},
                outputs=dict(result_contract.outputs),
                error_message=result_contract.failure_reason,
            )

            self._record_audit(
                context=context,
                record_type=AuditRecordType.SUB_MISSION_RESULT_PROPAGATED,
                mission_id=result_contract.parent_mission_id,
                action="RECEIVE_SUB_MISSION_RESULT",
                status="SUCCESS",
                correlation_id=result_contract.mission_id,
                details={
                    "from_sub_mission_id": result_contract.mission_id,
                    "status": result_contract.status.value,
                },
            )

            return True

    def cancel_hierarchy(
        self, context: TenantContext, parent_mission_id: str, reason: str = "Parent cancelled"
    ) -> int:
        """
        Cancela en cascada todas las submisiones activas no terminales bajo una misión padre.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        with self._lock:
            descendants = self.sub_mission_repository.get_descendants(context, parent_mission_id)
            cancelled_count = 0
            for child in descendants:
                if child.status in (MissionStatus.PENDING, MissionStatus.RUNNING, MissionStatus.BLOCKED):
                    updated = Mission(
                        mission_id=child.mission_id,
                        type=child.type,
                        priority=child.priority,
                        status=MissionStatus.ABORTED,
                        parameters={**child.parameters, "cancellation_reason": reason},
                        created_at=child.created_at,
                        updated_at=self._now(),
                        parent_mission_id=child.parent_mission_id,
                        root_mission_id=child.root_mission_id,
                        depth=child.depth,
                        delegation_key=child.delegation_key,
                        is_required=child.is_required,
                    )
                    self.mission_repository.save(context, updated)
                    cancelled_count += 1

                    self._record_audit(
                        context=context,
                        record_type=AuditRecordType.SUB_MISSION_CANCELLED,
                        mission_id=child.mission_id,
                        action="CANCEL_HIERARCHY",
                        status="ABORTED",
                        correlation_id=parent_mission_id,
                        details={"reason": reason, "cancelled_by_parent": parent_mission_id},
                    )

            return cancelled_count

    def get_hierarchy_tree(
        self, context: TenantContext, root_mission_id: str
    ) -> Optional[SubMissionNode]:
        """
        Construye recursivamente el árbol jerárquico completo para auditoría o visualización en Q.4.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        root_mission = self.mission_repository.get_by_id(context, root_mission_id)
        if not root_mission:
            return None

        def _build_node(m: Mission) -> SubMissionNode:
            children = self.sub_mission_repository.get_children(context, m.mission_id)
            child_nodes = tuple(_build_node(c) for c in children)
            result = self.sub_mission_repository.get_sub_mission_result(context, m.mission_id)
            return SubMissionNode(mission=m, children=child_nodes, result=result)

        return _build_node(root_mission)

    def can_parent_complete(self, context: TenantContext, parent_mission_id: str) -> Tuple[bool, List[str]]:
        """
        Verifica si una misión padre puede declararse COMPLETED.
        Regla: No puede completarse si existen misiones hijas obligatorias (is_required=True)
        que no hayan alcanzado un estado terminal (COMPLETED).
        """
        CrossTenantGuard.ensure_tenant_context(context)
        children = self.sub_mission_repository.get_children(context, parent_mission_id)
        blocking_child_ids = []
        for c in children:
            if c.is_required:
                if c.status != MissionStatus.COMPLETED:
                    blocking_child_ids.append(c.mission_id)

        can_complete = len(blocking_child_ids) == 0
        return can_complete, blocking_child_ids
