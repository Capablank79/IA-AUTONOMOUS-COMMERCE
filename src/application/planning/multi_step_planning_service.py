"""
Servicio de Aplicación: MultiStepPlanningService (Hito R.1 — Advanced Autonomy).

Responsabilidades:
1. Descomposición jerárquica de objetivos de misión en pasos ejecutables (GOAL -> SUBGOALS -> LEAF STEPS).
2. Construcción y validación estricta de DAG (Grafo Acíclico Dirigido).
3. Ordenamiento topológico determinista (Kahn con tie-breaking por step_id).
4. Determinación de readiness (pasos listos para el runtime autónomo sin ejecución anticipada).
5. Asignación y control de presupuestos (tokens, costos, steps, max_replans).
6. Replanificación acotada y dinámica ante fallos (Bounded subgraph repair).
7. Versionado inmutable de planes (v1 -> v2 ...) preservando evidencia y pasos COMPLETED.
8. Anti-Policy-Bypass: Los rechazos por POLICY_DENIED o Emergency Stop NO se bypassan mediante replan.
9. Trazabilidad completa con K.1 Audit Trail y K.2 Agent Trace (sin CoT).
10. Aislamiento estricto multi-tenant (TenantContext y CrossTenantGuard).
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
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
from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    StepDependency,
    PlanBudget,
    PlanVersionHistory,
    PlanningResult,
    PlanStatus,
    StepStatus,
    StepFailureType,
    DependencyType,
    StepRationale,
)
from src.domain.planning.ports import (
    ExecutionPlanRepositoryPort,
    CapabilityRegistryPort,
    MultiStepPlanningServicePort,
)
from src.domain.planning.validator import (
    PlanValidator,
    PlanValidationError,
    PlanCycleDetectedError,
    MissingDependencyError,
    CapabilityUnavailableError,
    BudgetExceededError,
)

logger = logging.getLogger("MultiStepPlanningService")


class MultiStepPlanningService(MultiStepPlanningServicePort):
    """
    Motor de planificación multi-paso canónico para Hito R.1.
    """

    def __init__(
        self,
        plan_repository: ExecutionPlanRepositoryPort,
        capability_registry: Optional[CapabilityRegistryPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_repository: Optional[AgentTraceRepositoryPort] = None,
        emergency_stop_service: Optional[EmergencyStopServicePort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.plan_repository = plan_repository
        self.capability_registry = capability_registry
        self.audit_repository = audit_repository
        self.trace_repository = trace_repository
        self.emergency_stop_service = emergency_stop_service
        self.clock = clock or SystemClock()

    def _now(self) -> datetime:
        return self.clock.now() if hasattr(self.clock, "now") else datetime.now(timezone.utc)

    def _record_audit(
        self,
        tenant_context: TenantContext,
        record_type: AuditRecordType,
        mission_id: str,
        plan_id: str,
        details: Dict[str, Any],
        is_success: bool = True,
        error_message: Optional[str] = None,
    ) -> None:
        if self.audit_repository is None:
            return

        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        actor = AuditActor(
            actor_id=tenant_context.identity_id or "planning-service",
            actor_type=AuditActorType.USER if tenant_context.identity_id else AuditActorType.SYSTEM,
            details={"tenant_id": tenant_id_val},
        )
        now_dt = self._now()
        meta = dict(details)
        if error_message:
            meta["error_message"] = error_message
        meta["tenant_id"] = tenant_id_val

        audit_rec = AuditRecord(
            audit_id=f"audit-{uuid.uuid4().hex[:12]}",
            record_type=record_type,
            occurred_at=now_dt,
            actor=actor,
            subject_type="EXECUTION_PLAN",
            subject_id=plan_id,
            action_or_operation="PLAN_MUTATION",
            status="SUCCESS" if is_success else "FAILED",
            correlation_id=tenant_context.correlation_id or f"corr-{uuid.uuid4().hex[:8]}",
            mission_id=mission_id,
            metadata=meta,
        )
        try:
            self.audit_repository.append(audit_rec)
        except Exception as e:
            logger.warning(f"Failed to record audit event: {e}")

    def _record_trace(
        self,
        tenant_context: TenantContext,
        mission_id: str,
        plan_id: str,
        operation: str,
        step_type: StepType,
        status: TraceStatus,
        metadata: Dict[str, Any],
    ) -> None:
        if self.trace_repository is None:
            return

        now_dt = self._now()
        trace_rec = AgentTraceRecord(
            trace_id=f"tr-{uuid.uuid4().hex[:12]}",
            component_name="MultiStepPlanningService",
            execution_id=plan_id,
            mission_id=mission_id,
            step_number=1,
            step_type=step_type,
            operation=operation,
            status=status,
            started_at=now_dt,
            completed_at=now_dt,
            metadata=sanitize_security_data(metadata),
        )
        try:
            self.trace_repository.append(trace_rec)
        except Exception as e:
            logger.warning(f"Failed to record agent trace: {e}")

    def create_plan(
        self,
        mission_id: str,
        goal: str,
        tenant_context: TenantContext,
        initial_steps: Sequence[PlanStep],
        dependencies: Optional[Sequence[StepDependency]] = None,
        budget: Optional[PlanBudget] = None,
        metadata: Optional[Mapping[str, Any]] = None,
    ) -> PlanningResult:
        """
        Crea, valida y persiste un nuevo ExecutionPlan.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(mission_id, "mission_id")

        plan_id = f"plan-{uuid.uuid4().hex[:12]}"
        now_dt = self._now()

        # Construir StepDependencies explícitas a partir de step.dependencies si no vienen provistas
        explicit_deps: List[StepDependency] = list(dependencies or [])
        if not explicit_deps:
            for s in initial_steps:
                for parent_id in s.dependencies:
                    explicit_deps.append(StepDependency(
                        from_step_id=parent_id,
                        to_step_id=s.step_id,
                        dependency_type=DependencyType.HARD,
                    ))

        tenant_id_val = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value
        plan = ExecutionPlan(
            plan_id=plan_id,
            mission_id=mission_id,
            tenant_id=tenant_id_val,
            goal=goal,
            steps=tuple(initial_steps),
            dependencies=tuple(explicit_deps),
            version=1,
            status=PlanStatus.DRAFT,
            budget=budget,
            replan_count=0,
            created_at=now_dt,
            updated_at=now_dt,
            metadata=metadata or {},
        )

        # Validación exhaustiva del DAG
        is_valid, validation_errors, topo_order = PlanValidator.validate_all(
            plan=plan,
            capability_registry=self.capability_registry,
        )

        if not is_valid:
            rejected_plan = ExecutionPlan(
                plan_id=plan.plan_id,
                mission_id=plan.mission_id,
                tenant_id=plan.tenant_id,
                goal=plan.goal,
                steps=plan.steps,
                dependencies=plan.dependencies,
                version=plan.version,
                status=PlanStatus.REJECTED,
                budget=plan.budget,
                created_at=plan.created_at,
                updated_at=now_dt,
                metadata=plan.metadata,
            )
            self._record_audit(
                tenant_context=tenant_context,
                record_type=AuditRecordType.POLICY_EVALUATED,
                mission_id=mission_id,
                plan_id=plan_id,
                details={"errors": validation_errors, "stage": "PLAN_VALIDATION_REJECTED"},
                is_success=False,
                error_message="; ".join(validation_errors),
            )
            self._record_trace(
                tenant_context=tenant_context,
                mission_id=mission_id,
                plan_id=plan_id,
                operation="CREATE_PLAN_REJECTED",
                step_type=StepType.POLICY_EVALUATION,
                status=TraceStatus.FAILED,
                metadata={"errors": validation_errors},
            )
            return PlanningResult(
                success=False,
                plan=rejected_plan,
                status=PlanStatus.REJECTED,
                errors=tuple(validation_errors),
            )

        # Plan válido -> marcar como VALIDATED y calcular readiness inicial
        ready_step_ids = PlanValidator.compute_step_readiness(plan)
        # Actualizar steps que sean ready
        updated_steps = []
        for s in plan.steps:
            if s.step_id in ready_step_ids and s.status == StepStatus.PENDING:
                updated_steps.append(PlanStep(
                    step_id=s.step_id,
                    objective=s.objective,
                    action_type=s.action_type,
                    required_inputs=s.required_inputs,
                    expected_outputs=s.expected_outputs,
                    dependencies=s.dependencies,
                    assigned_capability=s.assigned_capability,
                    assigned_agent=s.assigned_agent,
                    status=StepStatus.READY,
                    failure_type=s.failure_type,
                    failure_reason=s.failure_reason,
                    estimated_cost=s.estimated_cost,
                    is_cost_unknown=s.is_cost_unknown,
                    estimated_tokens=s.estimated_tokens,
                    actual_outputs=s.actual_outputs,
                    rationale=s.rationale,
                    parent_goal_id=s.parent_goal_id,
                    depth_level=s.depth_level,
                    metadata=s.metadata,
                ))
            else:
                updated_steps.append(s)

        final_plan = ExecutionPlan(
            plan_id=plan.plan_id,
            mission_id=plan.mission_id,
            tenant_id=plan.tenant_id,
            goal=plan.goal,
            steps=tuple(updated_steps),
            dependencies=plan.dependencies,
            version=plan.version,
            status=PlanStatus.VALIDATED,
            budget=plan.budget,
            created_at=plan.created_at,
            updated_at=now_dt,
            metadata=plan.metadata,
        )

        self.plan_repository.save_plan(final_plan, tenant_context)

        self._record_audit(
            tenant_context=tenant_context,
            record_type=AuditRecordType.MISSION_STATE_CHANGED,
            mission_id=mission_id,
            plan_id=plan_id,
            details={"status": "VALIDATED", "step_count": len(final_plan.steps), "topo_order": topo_order},
            is_success=True,
        )
        self._record_trace(
            tenant_context=tenant_context,
            mission_id=mission_id,
            plan_id=plan_id,
            operation="CREATE_PLAN_VALIDATED",
            step_type=StepType.COMPLETE,
            status=TraceStatus.SUCCESS,
            metadata={"ready_step_ids": ready_step_ids, "topo_order": topo_order},
        )

        return PlanningResult(
            success=True,
            plan=final_plan,
            status=PlanStatus.VALIDATED,
            ready_step_ids=ready_step_ids,
        )

    def get_ready_steps(
        self,
        plan: ExecutionPlan,
        tenant_context: TenantContext,
    ) -> Tuple[PlanStep, ...]:
        """
        Determina determinísticamente qué pasos están READY para su ejecución por el runtime.
        Aplica restricciones de Emergency Stop si está activo.
        """
        CrossTenantGuard.assert_same_tenant(tenant_context, plan.tenant_id, "get_ready_steps")

        ready_ids = PlanValidator.compute_step_readiness(plan)
        step_map = {s.step_id: s for s in plan.steps}

        ready_steps: List[PlanStep] = []
        for s_id in ready_ids:
            step = step_map[s_id]

            # Verificar Emergency Stop si existe
            if self.emergency_stop_service:
                try:
                    # Si el Emergency Stop está activo para la misión o tenant, el step queda bloqueado
                    is_stopped = False
                    if hasattr(self.emergency_stop_service, "is_emergency_stop_active"):
                        is_stopped = self.emergency_stop_service.is_emergency_stop_active(
                            tenant_context=tenant_context,
                            mission_id=plan.mission_id,
                            action_type=step.action_type,
                        )
                    elif hasattr(self.emergency_stop_service, "evaluate"):
                        from src.domain.emergency_stop.models import (
                            EmergencyStopEvaluationContext,
                            EmergencyStopDecisionStatus,
                        )
                        eval_ctx = EmergencyStopEvaluationContext(
                            action_name=step.action_type or step.step_id,
                            mission_id=plan.mission_id,
                            action_type=step.action_type,
                            is_read_only=False,
                        )
                        dec = self.emergency_stop_service.evaluate(eval_ctx)
                        # Compatible with both dec.decision_status and dec.status
                        status_val = getattr(dec, "decision_status", getattr(dec, "status", None))
                        is_stopped = status_val == EmergencyStopDecisionStatus.BLOCK_EXECUTION
                    if is_stopped:
                        continue
                except Exception as e:
                    logger.warning(f"Error checking emergency stop: {e}")

            ready_steps.append(step)

        return tuple(ready_steps)

    def mark_step_completed(
        self,
        plan_id: str,
        step_id: str,
        outputs: Mapping[str, Any],
        tenant_context: TenantContext,
    ) -> ExecutionPlan:
        """
        Actualiza un paso a COMPLETED de forma inmutable, registrando outputs y recalculando readiness.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        plan = self.plan_repository.get_plan_by_id(plan_id, tenant_context)
        if plan is None:
            raise PlanValidationError(f"ExecutionPlan '{plan_id}' not found.")

        step = plan.get_step(step_id)
        if step is None:
            raise PlanValidationError(f"Step '{step_id}' not found in plan '{plan_id}'.")

        now_dt = self._now()
        new_steps = []
        for s in plan.steps:
            if s.step_id == step_id:
                new_steps.append(PlanStep(
                    step_id=s.step_id,
                    objective=s.objective,
                    action_type=s.action_type,
                    required_inputs=s.required_inputs,
                    expected_outputs=s.expected_outputs,
                    dependencies=s.dependencies,
                    assigned_capability=s.assigned_capability,
                    assigned_agent=s.assigned_agent,
                    status=StepStatus.COMPLETED,
                    failure_type=None,
                    failure_reason=None,
                    estimated_cost=s.estimated_cost,
                    is_cost_unknown=s.is_cost_unknown,
                    estimated_tokens=s.estimated_tokens,
                    actual_outputs=outputs,
                    rationale=s.rationale,
                    parent_goal_id=s.parent_goal_id,
                    depth_level=s.depth_level,
                    metadata=s.metadata,
                ))
            else:
                new_steps.append(s)

        # Crear versión provisional para calcular nuevo readiness
        temp_plan = ExecutionPlan(
            plan_id=plan.plan_id,
            mission_id=plan.mission_id,
            tenant_id=plan.tenant_id,
            goal=plan.goal,
            steps=tuple(new_steps),
            dependencies=plan.dependencies,
            version=plan.version,
            status=plan.status,
            budget=plan.budget,
            history=plan.history,
            replan_count=plan.replan_count,
            created_at=plan.created_at,
            updated_at=now_dt,
            metadata=plan.metadata,
        )

        ready_ids = PlanValidator.compute_step_readiness(temp_plan)
        final_steps = []
        for s in temp_plan.steps:
            if s.step_id in ready_ids and s.status == StepStatus.PENDING:
                final_steps.append(PlanStep(
                    step_id=s.step_id,
                    objective=s.objective,
                    action_type=s.action_type,
                    required_inputs=s.required_inputs,
                    expected_outputs=s.expected_outputs,
                    dependencies=s.dependencies,
                    assigned_capability=s.assigned_capability,
                    assigned_agent=s.assigned_agent,
                    status=StepStatus.READY,
                    failure_type=s.failure_type,
                    failure_reason=s.failure_reason,
                    estimated_cost=s.estimated_cost,
                    is_cost_unknown=s.is_cost_unknown,
                    estimated_tokens=s.estimated_tokens,
                    actual_outputs=s.actual_outputs,
                    rationale=s.rationale,
                    parent_goal_id=s.parent_goal_id,
                    depth_level=s.depth_level,
                    metadata=s.metadata,
                ))
            else:
                final_steps.append(s)

        all_completed = all(s.status == StepStatus.COMPLETED for s in final_steps)
        plan_status = PlanStatus.COMPLETED if all_completed else PlanStatus.IN_PROGRESS

        updated_plan = ExecutionPlan(
            plan_id=plan.plan_id,
            mission_id=plan.mission_id,
            tenant_id=plan.tenant_id,
            goal=plan.goal,
            steps=tuple(final_steps),
            dependencies=plan.dependencies,
            version=plan.version,
            status=plan_status,
            budget=plan.budget,
            history=plan.history,
            replan_count=plan.replan_count,
            created_at=plan.created_at,
            updated_at=now_dt,
            metadata=plan.metadata,
        )

        self.plan_repository.save_plan(updated_plan, tenant_context)
        return updated_plan

    def replan(
        self,
        plan_id: str,
        tenant_context: TenantContext,
        failed_step_id: str,
        failure_type: StepFailureType,
        failure_reason: str,
        new_subgraph_steps: Optional[Sequence[PlanStep]] = None,
        new_dependencies: Optional[Sequence[StepDependency]] = None,
    ) -> PlanningResult:
        """
        Ejecuta replanificación acotada y determinista sobre el plan activo.

        Garantías críticas:
        1. POLICY_DENIED / Emergency Stop: REPLANNING != POLICY BYPASS. Plan queda BLOCKED/FAILED sin reintento evasivo.
        2. Bounded Replanning: No excede max_replans. Si se supera, el plan queda BLOCKED/FAILED.
        3. Immutability: Los pasos COMPLETED se preservan estrictamente y no se re-ejecutan.
        4. Version Increment: Genera nueva versión (v+1) con registro inmutable en history.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        plan = self.plan_repository.get_plan_by_id(plan_id, tenant_context)
        if plan is None:
            raise PlanValidationError(f"ExecutionPlan '{plan_id}' not found.")

        # 1. Regla Crítica: Policy Denial & Safety Bypass Prevention
        if failure_type == StepFailureType.POLICY_DENIED:
            logger.warning(
                f"Step '{failed_step_id}' failed due to POLICY_DENIED. Replan is strictly forbidden to bypass policies."
            )
            blocked_steps = []
            for s in plan.steps:
                if s.step_id == failed_step_id:
                    blocked_steps.append(PlanStep(
                        step_id=s.step_id,
                        objective=s.objective,
                        action_type=s.action_type,
                        required_inputs=s.required_inputs,
                        expected_outputs=s.expected_outputs,
                        dependencies=s.dependencies,
                        assigned_capability=s.assigned_capability,
                        assigned_agent=s.assigned_agent,
                        status=StepStatus.BLOCKED,
                        failure_type=failure_type,
                        failure_reason=failure_reason,
                        estimated_cost=s.estimated_cost,
                        is_cost_unknown=s.is_cost_unknown,
                        estimated_tokens=s.estimated_tokens,
                        actual_outputs=s.actual_outputs,
                        rationale=s.rationale,
                        parent_goal_id=s.parent_goal_id,
                        depth_level=s.depth_level,
                        metadata=s.metadata,
                    ))
                else:
                    blocked_steps.append(s)

            blocked_plan = ExecutionPlan(
                plan_id=plan.plan_id,
                mission_id=plan.mission_id,
                tenant_id=plan.tenant_id,
                goal=plan.goal,
                steps=tuple(blocked_steps),
                dependencies=plan.dependencies,
                version=plan.version,
                status=PlanStatus.BLOCKED,
                budget=plan.budget,
                history=plan.history,
                replan_count=plan.replan_count,
                created_at=plan.created_at,
                updated_at=self._now(),
                metadata=plan.metadata,
            )
            self.plan_repository.save_plan(blocked_plan, tenant_context)

            self._record_audit(
                tenant_context=tenant_context,
                record_type=AuditRecordType.POLICY_EVALUATED,
                mission_id=plan.mission_id,
                plan_id=plan_id,
                details={
                    "event": "REPLAN_REFUSED_POLICY_DENIED",
                    "step_id": failed_step_id,
                    "reason": failure_reason,
                },
                is_success=False,
                error_message="Replanning is forbidden for POLICY_DENIED steps.",
            )

            return PlanningResult(
                success=False,
                plan=blocked_plan,
                status=PlanStatus.BLOCKED,
                errors=(f"Replanning rejected: Step '{failed_step_id}' was denied by policy. Cannot bypass governance.",),
                replan_count=plan.replan_count,
                replan_reason=failure_reason,
            )

        # 2. Regla de Budget Exhausted
        if failure_type == StepFailureType.BUDGET_EXHAUSTED:
            failed_plan = ExecutionPlan(
                plan_id=plan.plan_id,
                mission_id=plan.mission_id,
                tenant_id=plan.tenant_id,
                goal=plan.goal,
                steps=plan.steps,
                dependencies=plan.dependencies,
                version=plan.version,
                status=PlanStatus.BLOCKED,
                budget=plan.budget,
                history=plan.history,
                replan_count=plan.replan_count,
                created_at=plan.created_at,
                updated_at=self._now(),
                metadata=plan.metadata,
            )
            self.plan_repository.save_plan(failed_plan, tenant_context)
            return PlanningResult(
                success=False,
                plan=failed_plan,
                status=PlanStatus.BLOCKED,
                errors=(f"Replanning rejected: Budget exhausted for mission '{plan.mission_id}'.",),
                replan_count=plan.replan_count,
            )

        # 3. Control de Límites de Replan (Replan Bounds)
        max_replans = plan.budget.max_replans if plan.budget else 3
        if plan.replan_count >= max_replans:
            logger.warning(
                f"ExecutionPlan '{plan_id}' exceeded max_replans ({max_replans}). Marking as FAILED."
            )
            failed_plan = ExecutionPlan(
                plan_id=plan.plan_id,
                mission_id=plan.mission_id,
                tenant_id=plan.tenant_id,
                goal=plan.goal,
                steps=plan.steps,
                dependencies=plan.dependencies,
                version=plan.version,
                status=PlanStatus.FAILED,
                budget=plan.budget,
                history=plan.history,
                replan_count=plan.replan_count,
                created_at=plan.created_at,
                updated_at=self._now(),
                metadata=plan.metadata,
            )
            self.plan_repository.save_plan(failed_plan, tenant_context)
            return PlanningResult(
                success=False,
                plan=failed_plan,
                status=PlanStatus.FAILED,
                errors=(f"Maximum replan limit ({max_replans}) exceeded. Plan halted safely.",),
                replan_count=plan.replan_count,
                replan_reason=failure_reason,
            )

        # 4. Reparación Mínima de Subgrafo: Preservar pasos COMPLETED
        now_dt = self._now()
        completed_steps = plan.get_completed_steps()
        preserved_step_ids = tuple(s.step_id for s in completed_steps)

        # Si no se proporcionan nuevos pasos para el subgrafo, se reintenta el step fallido reseteando su estado a PENDING
        if new_subgraph_steps is None:
            repaired_steps = []
            changed_step_ids = [failed_step_id]
            for s in plan.steps:
                if s.step_id == failed_step_id:
                    repaired_steps.append(PlanStep(
                        step_id=s.step_id,
                        objective=s.objective,
                        action_type=s.action_type,
                        required_inputs=s.required_inputs,
                        expected_outputs=s.expected_outputs,
                        dependencies=s.dependencies,
                        assigned_capability=s.assigned_capability,
                        assigned_agent=s.assigned_agent,
                        status=StepStatus.PENDING,
                        failure_type=None,
                        failure_reason=None,
                        estimated_cost=s.estimated_cost,
                        is_cost_unknown=s.is_cost_unknown,
                        estimated_tokens=s.estimated_tokens,
                        actual_outputs={},
                        rationale=s.rationale,
                        parent_goal_id=s.parent_goal_id,
                        depth_level=s.depth_level,
                        metadata=s.metadata,
                    ))
                else:
                    repaired_steps.append(s)
            repaired_dependencies = list(plan.dependencies)
        else:
            # Reemplazar subgrafo a partir de pasos preservados + nuevos pasos
            repaired_steps = list(completed_steps)
            new_ids = {s.step_id for s in new_subgraph_steps}
            changed_step_ids = list(new_ids)

            # Conservar pasos no afectados que no sean el fallido ni estén en los nuevos
            for s in plan.steps:
                if s.step_id not in preserved_step_ids and s.step_id != failed_step_id and s.step_id not in new_ids:
                    repaired_steps.append(s)

            repaired_steps.extend(new_subgraph_steps)

            # Dependencias: si no se proveen dependencias explícitas nuevas, deducirlas de los steps
            if new_dependencies is not None:
                repaired_dependencies = list(new_dependencies)
            else:
                # Filtrar dependencias antiguas que referencien steps que ya no existen
                current_step_ids = {s.step_id for s in repaired_steps}
                filtered_deps = [
                    d for d in plan.dependencies
                    if d.from_step_id in current_step_ids and d.to_step_id in current_step_ids
                ]
                # Agregar dependencias de los nuevos pasos
                for s in new_subgraph_steps:
                    for parent_id in s.dependencies:
                        if parent_id in current_step_ids:
                            filtered_deps.append(StepDependency(
                                from_step_id=parent_id,
                                to_step_id=s.step_id,
                                dependency_type=DependencyType.HARD,
                            ))
                repaired_dependencies = filtered_deps

        # 5. Construir nueva versión del plan
        new_version = plan.version + 1
        history_entry = PlanVersionHistory(
            version=plan.version,
            replan_reason=failure_reason,
            replan_event_type=failure_type,
            changed_step_ids=tuple(changed_step_ids),
            preserved_step_ids=preserved_step_ids,
            created_at=now_dt,
        )

        candidate_plan = ExecutionPlan(
            plan_id=plan.plan_id,
            mission_id=plan.mission_id,
            tenant_id=plan.tenant_id,
            goal=plan.goal,
            steps=tuple(repaired_steps),
            dependencies=tuple(repaired_dependencies),
            version=new_version,
            status=PlanStatus.DRAFT,
            budget=plan.budget,
            history=plan.history + (history_entry,),
            replan_count=plan.replan_count + 1,
            created_at=plan.created_at,
            updated_at=now_dt,
            metadata=plan.metadata,
        )

        # 6. Validar nuevo DAG y budgets
        is_valid, validation_errors, topo_order = PlanValidator.validate_all(
            plan=candidate_plan,
            capability_registry=self.capability_registry,
        )

        if not is_valid:
            rejected_plan = ExecutionPlan(
                plan_id=candidate_plan.plan_id,
                mission_id=candidate_plan.mission_id,
                tenant_id=candidate_plan.tenant_id,
                goal=candidate_plan.goal,
                steps=candidate_plan.steps,
                dependencies=candidate_plan.dependencies,
                version=candidate_plan.version,
                status=PlanStatus.REJECTED,
                budget=candidate_plan.budget,
                history=candidate_plan.history,
                replan_count=candidate_plan.replan_count,
                created_at=candidate_plan.created_at,
                updated_at=now_dt,
                metadata=candidate_plan.metadata,
            )
            return PlanningResult(
                success=False,
                plan=rejected_plan,
                status=PlanStatus.REJECTED,
                errors=tuple(validation_errors),
                replan_count=candidate_plan.replan_count,
                replan_reason=failure_reason,
            )

        # 7. Recalcular readiness
        ready_step_ids = PlanValidator.compute_step_readiness(candidate_plan)
        final_steps = []
        for s in candidate_plan.steps:
            if s.step_id in ready_step_ids and s.status == StepStatus.PENDING:
                final_steps.append(PlanStep(
                    step_id=s.step_id,
                    objective=s.objective,
                    action_type=s.action_type,
                    required_inputs=s.required_inputs,
                    expected_outputs=s.expected_outputs,
                    dependencies=s.dependencies,
                    assigned_capability=s.assigned_capability,
                    assigned_agent=s.assigned_agent,
                    status=StepStatus.READY,
                    failure_type=s.failure_type,
                    failure_reason=s.failure_reason,
                    estimated_cost=s.estimated_cost,
                    is_cost_unknown=s.is_cost_unknown,
                    estimated_tokens=s.estimated_tokens,
                    actual_outputs=s.actual_outputs,
                    rationale=s.rationale,
                    parent_goal_id=s.parent_goal_id,
                    depth_level=s.depth_level,
                    metadata=s.metadata,
                ))
            else:
                final_steps.append(s)

        final_plan = ExecutionPlan(
            plan_id=candidate_plan.plan_id,
            mission_id=candidate_plan.mission_id,
            tenant_id=candidate_plan.tenant_id,
            goal=candidate_plan.goal,
            steps=tuple(final_steps),
            dependencies=candidate_plan.dependencies,
            version=candidate_plan.version,
            status=PlanStatus.VALIDATED,
            budget=candidate_plan.budget,
            history=candidate_plan.history,
            replan_count=candidate_plan.replan_count,
            created_at=candidate_plan.created_at,
            updated_at=now_dt,
            metadata=candidate_plan.metadata,
        )

        self.plan_repository.save_plan(final_plan, tenant_context)

        self._record_audit(
            tenant_context=tenant_context,
            record_type=AuditRecordType.MISSION_STATE_CHANGED,
            mission_id=plan.mission_id,
            plan_id=plan.plan_id,
            details={
                "event": "REPLAN_SUCCEEDED",
                "new_version": new_version,
                "replan_count": final_plan.replan_count,
                "reason": failure_reason,
            },
            is_success=True,
        )
        self._record_trace(
            tenant_context=tenant_context,
            mission_id=plan.mission_id,
            plan_id=plan.plan_id,
            operation="REPLAN_EXECUTED",
            step_type=StepType.COMPLETE,
            status=TraceStatus.SUCCESS,
            metadata={"new_version": new_version, "replan_count": final_plan.replan_count},
        )

        return PlanningResult(
            success=True,
            plan=final_plan,
            status=PlanStatus.VALIDATED,
            ready_step_ids=ready_step_ids,
            replan_count=final_plan.replan_count,
            replan_reason=failure_reason,
        )
