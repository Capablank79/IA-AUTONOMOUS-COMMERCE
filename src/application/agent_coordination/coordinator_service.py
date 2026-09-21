"""
Servicio de Aplicación para Agent Coordination (Hito R.4 — Advanced Autonomy).

Implementa:
- AgentCoordinatorService:
  * Orquesta la ejecución concurrente, determinista y segura de múltiples agentes especialistas (R.3)
    sobre un DAG de planificación (R.1) y jerarquías de submisiones (R.2).
  * Garantiza Single-Owner mutable execution, exclusión mutua de side effects, reserva de presupuestos,
    políticas de contorno, handoffs estructurados Zero-CoT, sincronización en barreras (fan-out / fan-in)
    y merge determinista de resultados.
"""

from datetime import datetime, timezone
from decimal import Decimal
import logging
import threading
from typing import Optional, List, Tuple, Sequence, Mapping, Any, Dict, Set
import uuid

from src.domain.security.models import (
    validate_safe_identifier,
    sanitize_security_data,
    deep_freeze,
)
from src.domain.tenant.models import TenantContext
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    StepStatus,
    StepFailureType,
    PlanBudget,
)
from src.domain.mission.models import Mission, MissionStatus
from src.domain.specialist_agent.models import (
    SpecialistAgentDefinition,
    AgentAvailability,
    AgentExecutionContext,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentExecutionFailureType,
)
from src.domain.specialist_agent.registry import SpecialistAgentRegistry
from src.domain.agent_coordination.models import (
    CoordinationSession,
    CoordinationTask,
    CoordinationStatus,
    CoordinationTaskStatus,
    CoordinationFailureType,
    CoordinationEventType,
    CoordinationPolicy,
    MergeStrategy,
    MergeResult,
    TaskClaim,
    AgentAssignment,
    AgentHandoff,
    SharedCoordinationContext,
)
from src.domain.agent_coordination.delegation_models import (
    DelegationReason,
    DelegationPolicy,
    DelegationDecisionStatus,
    DelegationRequest,
    DelegationDecision,
    DelegationRecord,
)
from src.domain.agent_coordination.merger import DeterministicResultMerger
from src.domain.agent_coordination.ports import (
    CoordinationSessionRepositoryPort,
    AgentCoordinatorPort,
)
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.emergency_stop.models import EmergencyStopEvaluationContext
from src.domain.emergency_stop.ports import EmergencyStopServicePort
from src.domain.reliability.ports import ClockPort

logger = logging.getLogger(__name__)


class AgentCoordinatorService(AgentCoordinatorPort):
    """
    Servicio de orquestación y coordinación multi-agente en R.4.
    """

    def __init__(
        self,
        session_repository: CoordinationSessionRepositoryPort,
        agent_registry: Optional[SpecialistAgentRegistry] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
        trace_repository: Optional[AgentTraceRepositoryPort] = None,
        emergency_stop_service: Optional[EmergencyStopServicePort] = None,
        clock: Optional[ClockPort] = None,
    ):
        self.session_repository = session_repository
        self.agent_registry = agent_registry
        self.audit_repository = audit_repository
        self.trace_repository = trace_repository
        self.emergency_stop_service = emergency_stop_service
        self.clock = clock
        self._lock = threading.RLock()

    def _now(self) -> datetime:
        if self.clock:
            return self.clock.now()
        return datetime.now(timezone.utc)

    def _record_audit(
        self,
        tenant_context: TenantContext,
        event_type: str,
        session_id: str,
        mission_id: str,
        details: Mapping[str, Any],
        actor_id: str = "agent_coordinator",
    ) -> None:
        if not self.audit_repository:
            return
        try:
            try:
                record_type = AuditRecordType(event_type)
            except ValueError:
                record_type = AuditRecordType(
                    event_type if event_type.startswith("COORDINATION_") else f"COORDINATION_{event_type}"
                )
            record = AuditRecord(
                audit_id=f"audit-coord-{uuid.uuid4().hex[:12]}",
                record_type=record_type,
                occurred_at=self._now(),
                actor=AuditActor(
                    actor_id=actor_id,
                    actor_type=AuditActorType.SYSTEM,
                ),
                subject_type="COORDINATION_SESSION",
                subject_id=session_id,
                action_or_operation=event_type,
                status="RECORDED",
                correlation_id=tenant_context.correlation_id,
                mission_id=mission_id,
                entity_reference=session_id,
                provenance="AGENT_COORDINATOR",
                metadata=sanitize_security_data(details),
            )
            self.audit_repository.append(record)
        except Exception as e:
            logger.warning("Failed to record audit in AgentCoordinatorService: %s", e)

    def _record_trace(
        self,
        tenant_context: TenantContext,
        session_id: str,
        mission_id: str,
        task_id: Optional[str],
        event_name: str,
        details: Mapping[str, Any],
    ) -> None:
        if not self.trace_repository:
            return
        try:
            if hasattr(self.trace_repository, "record_step"):
                self.trace_repository.record_step(
                    session_id=session_id,
                    mission_id=mission_id,
                    task_id=task_id,
                    event_name=event_name,
                    timestamp=self._now(),
                    details=sanitize_security_data(details),
                )
        except Exception as e:
            logger.warning("Failed to record trace in AgentCoordinatorService: %s", e)

    # -------------------------------------------------------------------------
    # 1. Session Lifecycle
    # -------------------------------------------------------------------------

    def create_session(
        self,
        tenant_context: TenantContext,
        mission_id: str,
        plan: Optional[ExecutionPlan] = None,
        sub_missions: Sequence[Mission] = (),
        policy: Optional[CoordinationPolicy] = None,
        correlation_id: Optional[str] = None,
    ) -> CoordinationSession:
        """
        Inicializa una CoordinationSession ligada al tenant, misión y DAG de planificación.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(mission_id, "mission_id")
        tenant_id = tenant_context.tenant_id if isinstance(tenant_context.tenant_id, str) else tenant_context.tenant_id.value

        session_id = f"coord-{uuid.uuid4().hex[:12]}"
        corr_id = correlation_id or tenant_context.correlation_id or f"corr-{uuid.uuid4().hex[:8]}"
        effective_policy = policy or CoordinationPolicy()

        tasks: Dict[str, CoordinationTask] = {}

        # 1. Map PlanSteps from R.1 ExecutionPlan
        if plan is not None:
            CrossTenantGuard.assert_same_tenant(tenant_context, plan.tenant_id, "create_session (plan)")
            for step in plan.steps:
                # Step dependencies (from step.dependencies or plan.dependencies)
                step_deps = tuple(step.dependencies)
                if not step_deps and hasattr(plan, "dependencies"):
                    step_deps = tuple(
                        dep.from_step_id for dep in plan.dependencies
                        if dep.to_step_id == step.step_id
                    )
                required_keys = tuple(step.required_inputs.keys()) if step.required_inputs else ()

                # Map step status to coordination task status
                task_status = CoordinationTaskStatus.PENDING
                if step.status == StepStatus.COMPLETED:
                    task_status = CoordinationTaskStatus.COMPLETED
                elif step.status == StepStatus.FAILED:
                    task_status = CoordinationTaskStatus.FAILED
                elif step.status == StepStatus.BLOCKED:
                    task_status = CoordinationTaskStatus.BLOCKED

                task = CoordinationTask(
                    task_id=step.step_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    required_capability=step.assigned_capability or "GENERAL_EXECUTION",
                    action_type=step.action_type,
                    assigned_agent_id=step.assigned_agent,
                    status=task_status,
                    dependencies=step_deps,
                    required_input_keys=required_keys,
                    inputs=dict(step.required_inputs),
                    outputs=dict(step.actual_outputs),
                    allocated_budget=PlanBudget(
                        max_tokens=step.estimated_tokens,
                        max_cost=step.estimated_cost,
                        is_cost_unknown=step.is_cost_unknown,
                    ) if (step.estimated_tokens or step.estimated_cost is not None or step.is_cost_unknown) else None,
                    is_side_effecting=step.action_type in ("PUBLISH", "PURCHASE", "MUTATE", "COMMERCIAL_PUBLICATION"),
                    resource_id=step.metadata.get("resource_id") if step.metadata else None,
                    metadata={
                        **dict(step.metadata),
                        "expected_output_keys": tuple(step.expected_outputs),
                    },
                )
                tasks[step.step_id] = task

        # 2. Map SubMissions from R.2
        sub_mission_ids = []
        for sm in sub_missions:
            sub_mission_ids.append(sm.mission_id)
            if sm.mission_id not in tasks:
                sm_status = CoordinationTaskStatus.PENDING
                if sm.status == MissionStatus.COMPLETED:
                    sm_status = CoordinationTaskStatus.COMPLETED
                elif sm.status in (MissionStatus.FAILED, MissionStatus.ABORTED):
                    sm_status = CoordinationTaskStatus.FAILED
                elif sm.status == MissionStatus.BLOCKED:
                    sm_status = CoordinationTaskStatus.BLOCKED

                task = CoordinationTask(
                    task_id=sm.mission_id,
                    session_id=session_id,
                    tenant_id=tenant_id,
                    required_capability=sm.parameters.get("capability_id", sm.type.value),
                    action_type=sm.type.value,
                    assigned_agent_id=sm.parameters.get("assigned_agent"),
                    status=sm_status,
                    dependencies=tuple(sm.parameters.get("dependencies", ())),
                    inputs=dict(sm.parameters.get("inputs", {})),
                    is_side_effecting=sm.type.value in ("COMMERCIAL_PUBLICATION", "PURCHASE"),
                    resource_id=sm.parameters.get("resource_id"),
                    metadata={"sub_mission": True, "depth": sm.depth},
                )
                tasks[sm.mission_id] = task

        now = self._now()
        shared_ctx = SharedCoordinationContext(
            session_id=session_id,
            tenant_id=tenant_id,
            mission_id=mission_id,
            updated_at=now,
        )

        session = CoordinationSession(
            session_id=session_id,
            tenant_id=tenant_id,
            mission_id=mission_id,
            correlation_id=corr_id,
            plan_id=plan.plan_id if plan else None,
            plan_version=plan.version if plan else 1,
            sub_mission_ids=tuple(sub_mission_ids),
            status=CoordinationStatus.ACTIVE,
            tasks=tasks,
            shared_context=shared_ctx,
            policy=effective_policy,
            created_at=now,
            updated_at=now,
        )
        session = self._refresh_task_readiness(session)

        saved = self.session_repository.save_session(session, tenant_context)

        self._record_audit(
            tenant_context=tenant_context,
            event_type=CoordinationEventType.COORDINATION_STARTED.value,
            session_id=session_id,
            mission_id=mission_id,
            details={
                "session_id": session_id,
                "task_count": len(tasks),
                "plan_id": plan.plan_id if plan else None,
                "correlation_id": corr_id,
            },
        )
        return saved

    def get_session(
        self,
        session_id: str,
        tenant_context: TenantContext,
    ) -> Optional[CoordinationSession]:
        """
        Obtiene una sesión de coordinación validando el aislamiento de tenant.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(session_id, "session_id")
        return self.session_repository.get_session(session_id, tenant_context)

    # -------------------------------------------------------------------------
    # 2. Deterministic Task Readiness (R.1 DAG source of truth + bounds)
    # -------------------------------------------------------------------------

    def get_ready_tasks(
        self,
        session_id: str,
        tenant_context: TenantContext,
    ) -> Tuple[CoordinationTask, ...]:
        """
        Retorna las tareas en estado READY que satisfacen todas sus precondiciones:
        1. Dependencias DAG completadas.
        2. Inputs requeridos disponibles.
        3. Especialista asignado disponible (si hay registry).
        4. Límites de concurrencia respetados.
        5. Exclusión mutua (resource locks) no comprometida.
        6. Si una dependencia falló -> marca la tarea BLOCKED con DEPENDENCY_FAILED.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        session = self.session_repository.get_session(session_id, tenant_context)
        if not session or session.status != CoordinationStatus.ACTIVE:
            return ()

        with self._lock:
            # Re-read session inside lock to ensure concurrency safety
            session = self.session_repository.get_session(session_id, tenant_context)
            if not session or session.status != CoordinationStatus.ACTIVE:
                return ()

            refreshed = self._refresh_task_readiness(session)
            if refreshed is not session:
                self.session_repository.save_session(refreshed, tenant_context)
            session = refreshed

            ready_tasks: List[CoordinationTask] = []
            for task_id in sorted(session.tasks):
                task = session.tasks[task_id]
                if task.status != CoordinationTaskStatus.READY:
                    continue
                if task.assigned_agent_id and self.agent_registry:
                    definition = self.agent_registry.get(session.tenant_id, task.assigned_agent_id)
                    if (
                        not definition
                        or definition.availability != AgentAvailability.AVAILABLE
                        or not definition.policy_eligible
                    ):
                        continue
                if task.resource_id and session.resource_locks.get(task.resource_id):
                    continue
                ready_tasks.append(task)

            return tuple(ready_tasks)

    # -------------------------------------------------------------------------
    # 3. Single-Owner Claim / Lease Mechanism
    # -------------------------------------------------------------------------

    def claim_task(
        self,
        session_id: str,
        task_id: str,
        agent_id: str,
        tenant_context: TenantContext,
    ) -> Tuple[CoordinationSession, TaskClaim]:
        """
        Adquiere de forma atómica el lease exclusivo sobre una tarea mutable.
        Previene colisiones concurrentes y duplicate execution.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(session_id, "session_id")
        validate_safe_identifier(task_id, "task_id")
        validate_safe_identifier(agent_id, "agent_id")

        with self._lock:
            session = self.session_repository.get_session(session_id, tenant_context)
            if not session:
                raise ValueError(f"Session {session_id} not found")
            if session.status != CoordinationStatus.ACTIVE:
                raise ValueError(f"Cannot claim task in non-active session ({session.status.value})")

            task = session.tasks.get(task_id)
            if not task:
                raise ValueError(f"Task {task_id} not found in session")

            if task.status != CoordinationTaskStatus.READY:
                raise ValueError(
                    f"Task {task_id} is already claimed or not in READY state ({task.status.value})"
                )

            for dependency_id in task.dependencies:
                dependency = session.tasks.get(dependency_id)
                if not dependency or dependency.status != CoordinationTaskStatus.COMPLETED:
                    raise ValueError(f"Task {task_id} has incomplete dependency {dependency_id}")
            missing_inputs = [
                key for key in task.required_input_keys
                if key not in task.inputs
                and (not session.shared_context or key not in session.shared_context.facts)
            ]
            if missing_inputs:
                raise ValueError(f"Task {task_id} is missing required inputs: {', '.join(missing_inputs)}")

            if task.assigned_agent_id and task.assigned_agent_id != agent_id:
                raise ValueError(f"Task {task_id} is explicitly assigned to {task.assigned_agent_id}, not {agent_id}")

            if self.agent_registry:
                definition = self.agent_registry.get(session.tenant_id, agent_id)
                if not definition:
                    raise ValueError(f"Agent {agent_id} is not registered for tenant {session.tenant_id}")
                if definition.availability != AgentAvailability.AVAILABLE:
                    raise ValueError(f"Agent {agent_id} is not AVAILABLE")
                if not definition.policy_eligible:
                    raise ValueError(f"Agent {agent_id} is not policy eligible")

            active_tasks = [
                active_task for active_task in session.tasks.values()
                if active_task.status in (CoordinationTaskStatus.CLAIMED, CoordinationTaskStatus.RUNNING)
            ]
            if len(active_tasks) >= session.policy.max_concurrent_tasks:
                raise ValueError("Maximum concurrent tasks limit reached")
            active_agents = {
                active_task.assigned_agent_id for active_task in active_tasks
                if active_task.assigned_agent_id
            }
            if agent_id not in active_agents and len(active_agents) >= session.policy.max_concurrent_agents:
                raise ValueError("Maximum concurrent agents limit reached")

            if task.allocated_budget:
                if task.allocated_budget.max_tokens is not None and task.allocated_budget.max_tokens <= 0:
                    raise ValueError(f"Task {task_id} has exhausted token budget")
                if task.allocated_budget.max_cost is not None and task.allocated_budget.max_cost <= Decimal("0"):
                    raise ValueError(f"Task {task_id} has exhausted cost budget")

            new_resource_locks = dict(session.resource_locks)
            if task.resource_id:
                holder = new_resource_locks.get(task.resource_id)
                if holder:
                    raise ValueError(f"Resource lock '{task.resource_id}' is currently held by agent {holder}")
                new_resource_locks[task.resource_id] = agent_id

            now = self._now()
            claim = TaskClaim(
                task_id=task_id,
                agent_id=agent_id,
                claimed_at=now,
                is_active=True,
            )

            updated_tasks = dict(session.tasks)
            updated_tasks[task_id] = self._update_task_fields(
                task,
                status=CoordinationTaskStatus.CLAIMED,
                assigned_agent_id=agent_id,
                claim=claim,
            )

            new_leases = dict(session.active_leases)
            new_leases[task_id] = claim

            updated_session = CoordinationSession(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                mission_id=session.mission_id,
                correlation_id=session.correlation_id,
                plan_id=session.plan_id,
                plan_version=session.plan_version,
                sub_mission_ids=session.sub_mission_ids,
                status=session.status,
                tasks=updated_tasks,
                shared_context=session.shared_context,
                policy=session.policy,
                handoffs=session.handoffs,
                active_leases=new_leases,
                resource_locks=new_resource_locks,
                budget_reserved=session.budget_reserved,
                created_at=session.created_at,
                updated_at=now,
            )

            saved = self.session_repository.save_session(updated_session, tenant_context)

            self._record_audit(
                tenant_context,
                CoordinationEventType.TASK_CLAIMED.value,
                session_id,
                session.mission_id,
                {"task_id": task_id, "agent_id": agent_id, "lease_id": claim.lease_id},
                actor_id=agent_id,
            )
            return saved, claim

    # -------------------------------------------------------------------------
    # 4. Structured Handoff (Zero-CoT)
    # -------------------------------------------------------------------------

    def handoff(
        self,
        session_id: str,
        source_agent_id: str,
        target_task_id: str,
        payload: Mapping[str, Any],
        tenant_context: TenantContext,
        evidence_refs: Sequence[str] = (),
        target_agent_id: Optional[str] = None,
    ) -> Tuple[CoordinationSession, AgentHandoff]:
        """
        Transfiere hechos, decisiones y evidencia de un agente hacia una tarea downstream.
        Garantiza Zero-CoT estricto (sanitización de reasoning/scratchpad).
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(session_id, "session_id")
        validate_safe_identifier(source_agent_id, "source_agent_id")
        validate_safe_identifier(target_task_id, "target_task_id")

        with self._lock:
            session = self.session_repository.get_session(session_id, tenant_context)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            target_task = session.tasks.get(target_task_id)
            if not target_task:
                raise ValueError(f"Target task {target_task_id} not found in session")

            now = self._now()
            sanitized_payload = sanitize_security_data(payload)
            handoff_id = f"handoff-{uuid.uuid4().hex[:12]}"

            handoff_obj = AgentHandoff(
                handoff_id=handoff_id,
                source_agent_id=source_agent_id,
                target_task_id=target_task_id,
                payload=sanitized_payload,
                output_contract_keys=tuple(sanitized_payload.keys()),
                evidence_refs=tuple(evidence_refs),
                correlation_id=session.correlation_id,
                created_at=now,
                target_agent_id=target_agent_id,
            )

            # Update target task inputs
            updated_inputs = dict(target_task.inputs)
            updated_inputs.update(sanitized_payload)

            updated_tasks = dict(session.tasks)
            updated_tasks[target_task_id] = self._update_task_fields(
                target_task,
                inputs=updated_inputs,
                evidence_refs=tuple(set(target_task.evidence_refs).union(evidence_refs)),
            )

            # Update shared context
            shared_ctx = session.shared_context
            if shared_ctx:
                updated_facts = dict(shared_ctx.facts)
                updated_facts.update(sanitized_payload)
                shared_ctx = SharedCoordinationContext(
                    session_id=shared_ctx.session_id,
                    tenant_id=shared_ctx.tenant_id,
                    mission_id=shared_ctx.mission_id,
                    facts=updated_facts,
                    decisions=shared_ctx.decisions,
                    evidence_refs=tuple(set(shared_ctx.evidence_refs).union(evidence_refs)),
                    version=shared_ctx.version + 1,
                    updated_at=now,
                )

            new_handoffs = tuple(list(session.handoffs) + [handoff_obj])

            updated_session = CoordinationSession(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                mission_id=session.mission_id,
                correlation_id=session.correlation_id,
                plan_id=session.plan_id,
                plan_version=session.plan_version,
                sub_mission_ids=session.sub_mission_ids,
                status=session.status,
                tasks=updated_tasks,
                shared_context=shared_ctx,
                policy=session.policy,
                handoffs=new_handoffs,
                active_leases=session.active_leases,
                resource_locks=session.resource_locks,
                created_at=session.created_at,
                updated_at=now,
            )

            saved = self.session_repository.save_session(updated_session, tenant_context)

            self._record_audit(
                tenant_context,
                CoordinationEventType.HANDOFF_CREATED.value,
                session_id,
                session.mission_id,
                {
                    "handoff_id": handoff_id,
                    "source_agent_id": source_agent_id,
                    "target_task_id": target_task_id,
                    "keys": list(sanitized_payload.keys()),
                },
                actor_id=source_agent_id,
            )
            return saved, handoff_obj

    # -------------------------------------------------------------------------
    # 5. Deterministic Result Merging (Fan-In) & Conflict Detection
    # -------------------------------------------------------------------------

    def merge_task_results(
        self,
        session_id: str,
        source_task_ids: Sequence[str],
        target_task_id: str,
        tenant_context: TenantContext,
        strategy: Optional[MergeStrategy] = None,
        precedence_order: Sequence[str] = (),
    ) -> MergeResult:
        """
        Fusiona deterministamente los outputs de varias tareas upstream hacia una downstream.
        Si hay contradicciones no reconciliables -> marca la tarea target como BLOCKED/CONFLICT.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(session_id, "session_id")
        validate_safe_identifier(target_task_id, "target_task_id")

        with self._lock:
            session = self.session_repository.get_session(session_id, tenant_context)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            target_task = session.tasks.get(target_task_id)
            if not target_task:
                raise ValueError(f"Target task {target_task_id} not found in session")

            effective_strategy = strategy or session.policy.default_merge_strategy

            sources_outputs: Dict[str, Mapping[str, Any]] = {}
            for src_id in source_task_ids:
                src_task = session.tasks.get(src_id)
                if not src_task:
                    raise ValueError(f"Source task {src_id} not found in session")
                if src_task.status != CoordinationTaskStatus.COMPLETED:
                    raise ValueError(f"Source task {src_id} is not COMPLETED")
                expected_outputs = tuple(src_task.metadata.get("expected_output_keys", ()))
                missing_outputs = [key for key in expected_outputs if key not in src_task.outputs]
                if missing_outputs:
                    raise ValueError(
                        f"Source task {src_id} is missing required outputs: {', '.join(missing_outputs)}"
                    )
                sources_outputs[src_id] = src_task.outputs

            merge_result = DeterministicResultMerger.merge(
                results_by_source=sources_outputs,
                strategy=effective_strategy,
                precedence_order=precedence_order,
                expected_output_keys=target_task.required_input_keys,
            )

            now = self._now()
            updated_tasks = dict(session.tasks)

            if not merge_result.success:
                # Mark target task BLOCKED with CONFLICT
                updated_tasks[target_task_id] = self._update_task_fields(
                    target_task,
                    status=CoordinationTaskStatus.BLOCKED,
                    failure_type=CoordinationFailureType.CONFLICT,
                    failure_reason=f"Merge conflict: {'; '.join(merge_result.conflicts)}",
                )
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.COORDINATION_CONFLICT.value,
                    session_id,
                    session.mission_id,
                    {
                        "target_task_id": target_task_id,
                        "conflicts": list(merge_result.conflicts),
                        "sources": list(source_task_ids),
                    },
                )
            else:
                # Merge into target task inputs
                new_inputs = dict(target_task.inputs)
                new_inputs.update(merge_result.merged_data)
                updated_tasks[target_task_id] = self._update_task_fields(
                    target_task,
                    inputs=new_inputs,
                )
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.RESULT_MERGED.value,
                    session_id,
                    session.mission_id,
                    {
                        "target_task_id": target_task_id,
                        "merged_keys": list(merge_result.merged_data.keys()),
                        "sources": list(source_task_ids),
                    },
                )

            updated_session = CoordinationSession(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                mission_id=session.mission_id,
                correlation_id=session.correlation_id,
                plan_id=session.plan_id,
                plan_version=session.plan_version,
                sub_mission_ids=session.sub_mission_ids,
                status=session.status,
                tasks=updated_tasks,
                shared_context=session.shared_context,
                policy=session.policy,
                handoffs=session.handoffs,
                active_leases=session.active_leases,
                resource_locks=session.resource_locks,
                created_at=session.created_at,
                updated_at=now,
            )
            self.session_repository.save_session(updated_session, tenant_context)
            return merge_result

    # -------------------------------------------------------------------------
    # 6. Task Completion & Failure Propagation
    # -------------------------------------------------------------------------

    def complete_task(
        self,
        session_id: str,
        task_id: str,
        outputs: Mapping[str, Any],
        tenant_context: TenantContext,
        evidence_refs: Sequence[str] = (),
        assignment_version: Optional[int] = None,
        agent_id: Optional[str] = None,
    ) -> CoordinationSession:
        """
        Registra la finalización exitosa de una tarea, libera locks/leases y propaga datos downstream.
        Verifica assignment_version y agent_id para rechazar stale results de dueños previos.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(session_id, "session_id")
        validate_safe_identifier(task_id, "task_id")
        if agent_id:
            validate_safe_identifier(agent_id, "agent_id")

        with self._lock:
            session = self.session_repository.get_session(session_id, tenant_context)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            task = session.tasks.get(task_id)
            if not task:
                raise ValueError(f"Task {task_id} not found in session")

            # Check stale execution / out-of-order results from old owners (R.5)
            if assignment_version is not None and assignment_version != task.assignment_version:
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.STALE_AGENT_RESULT_REJECTED.value,
                    session_id,
                    session.mission_id,
                    {
                        "task_id": task_id,
                        "provided_version": assignment_version,
                        "current_version": task.assignment_version,
                        "agent_id": agent_id,
                    },
                    actor_id=agent_id or "unknown_agent",
                )
                raise ValueError(
                    f"Stale result rejected: task assignment_version is {task.assignment_version}, "
                    f"but received version {assignment_version}"
                )

            if agent_id is not None and task.assigned_agent_id and agent_id != task.assigned_agent_id:
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.STALE_AGENT_RESULT_REJECTED.value,
                    session_id,
                    session.mission_id,
                    {
                        "task_id": task_id,
                        "provided_agent_id": agent_id,
                        "current_assigned_agent_id": task.assigned_agent_id,
                    },
                    actor_id=agent_id,
                )
                raise ValueError(
                    f"Stale result rejected: task is assigned to agent {task.assigned_agent_id}, "
                    f"but received result from {agent_id}"
                )

            now = self._now()
            sanitized_outputs = sanitize_security_data(outputs)

            # Release lease and resource lock
            new_leases = dict(session.active_leases)
            new_leases.pop(task_id, None)

            new_resource_locks = dict(session.resource_locks)
            if task.resource_id and new_resource_locks.get(task.resource_id) == task.assigned_agent_id:
                new_resource_locks.pop(task.resource_id, None)

            updated_tasks = dict(session.tasks)
            updated_tasks[task_id] = self._update_task_fields(
                task,
                status=CoordinationTaskStatus.COMPLETED,
                outputs=sanitized_outputs,
                evidence_refs=tuple(set(task.evidence_refs).union(evidence_refs)),
                claim=None,
            )

            # Update shared context
            shared_ctx = session.shared_context
            if shared_ctx:
                updated_facts = dict(shared_ctx.facts)
                updated_facts.update(sanitized_outputs)
                shared_ctx = SharedCoordinationContext(
                    session_id=shared_ctx.session_id,
                    tenant_id=shared_ctx.tenant_id,
                    mission_id=shared_ctx.mission_id,
                    facts=updated_facts,
                    decisions=shared_ctx.decisions,
                    evidence_refs=tuple(set(shared_ctx.evidence_refs).union(evidence_refs)),
                    version=shared_ctx.version + 1,
                    updated_at=now,
                )

            # Check if all tasks in session are now COMPLETED
            is_all_done = all(t.status == CoordinationTaskStatus.COMPLETED for t in updated_tasks.values())
            new_session_status = CoordinationStatus.COMPLETED if is_all_done else session.status

            updated_session = CoordinationSession(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                mission_id=session.mission_id,
                correlation_id=session.correlation_id,
                plan_id=session.plan_id,
                plan_version=session.plan_version,
                sub_mission_ids=session.sub_mission_ids,
                status=new_session_status,
                tasks=updated_tasks,
                shared_context=shared_ctx,
                policy=session.policy,
                handoffs=session.handoffs,
                active_leases=new_leases,
                resource_locks=new_resource_locks,
                budget_reserved=session.budget_reserved,
                created_at=session.created_at,
                updated_at=now,
            )
            if updated_session.status == CoordinationStatus.ACTIVE:
                updated_session = self._refresh_task_readiness(updated_session)

            saved = self.session_repository.save_session(updated_session, tenant_context)

            if is_all_done:
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.COORDINATION_COMPLETED.value,
                    session_id,
                    session.mission_id,
                    {"session_id": session_id, "tasks_completed": len(updated_tasks)},
                )
            return saved

    def fail_task(
        self,
        session_id: str,
        task_id: str,
        failure_type: CoordinationFailureType,
        failure_reason: str,
        tenant_context: TenantContext,
        assignment_version: Optional[int] = None,
        agent_id: Optional[str] = None,
    ) -> CoordinationSession:
        """
        Registra el fallo de una tarea y propaga el bloqueo en cascada hacia dependientes.
        Si failure_type == POLICY_DENIED: bloquea estrictamente (sin bypass ni reroute).
        Verifica assignment_version y agent_id para rechazar stale failure reports de dueños previos.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(session_id, "session_id")
        validate_safe_identifier(task_id, "task_id")
        if agent_id:
            validate_safe_identifier(agent_id, "agent_id")

        with self._lock:
            session = self.session_repository.get_session(session_id, tenant_context)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            task = session.tasks.get(task_id)
            if not task:
                raise ValueError(f"Task {task_id} not found in session")

            # Check stale execution / out-of-order failures from old owners (R.5)
            if assignment_version is not None and assignment_version != task.assignment_version:
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.STALE_AGENT_RESULT_REJECTED.value,
                    session_id,
                    session.mission_id,
                    {
                        "task_id": task_id,
                        "provided_version": assignment_version,
                        "current_version": task.assignment_version,
                        "agent_id": agent_id,
                    },
                    actor_id=agent_id or "unknown_agent",
                )
                raise ValueError(
                    f"Stale failure report rejected: task assignment_version is {task.assignment_version}, "
                    f"but received version {assignment_version}"
                )

            if agent_id is not None and task.assigned_agent_id and agent_id != task.assigned_agent_id:
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.STALE_AGENT_RESULT_REJECTED.value,
                    session_id,
                    session.mission_id,
                    {
                        "task_id": task_id,
                        "provided_agent_id": agent_id,
                        "current_assigned_agent_id": task.assigned_agent_id,
                    },
                    actor_id=agent_id,
                )
                raise ValueError(
                    f"Stale failure report rejected: task is assigned to agent {task.assigned_agent_id}, "
                    f"but received failure report from {agent_id}"
                )

            now = self._now()

            # Release lease and resource lock
            new_leases = dict(session.active_leases)
            new_leases.pop(task_id, None)

            new_resource_locks = dict(session.resource_locks)
            if task.resource_id and new_resource_locks.get(task.resource_id) == task.assigned_agent_id:
                new_resource_locks.pop(task.resource_id, None)

            # Determine task terminal status
            target_status = CoordinationTaskStatus.BLOCKED if failure_type in (
                CoordinationFailureType.BUDGET_EXHAUSTED,
                CoordinationFailureType.RATE_LIMITED,
                CoordinationFailureType.CAPABILITY_UNAVAILABLE,
                CoordinationFailureType.CONFLICT,
                CoordinationFailureType.DEPENDENCY_FAILED,
            ) else CoordinationTaskStatus.FAILED

            updated_tasks = dict(session.tasks)
            updated_tasks[task_id] = self._update_task_fields(
                task,
                status=target_status,
                failure_type=failure_type,
                failure_reason=failure_reason,
                claim=None,
            )

            failed_upstream_ids = {task_id}
            while failed_upstream_ids:
                newly_blocked_ids: Set[str] = set()
                for downstream_id, downstream_task in list(updated_tasks.items()):
                    if (
                        downstream_task.status in (CoordinationTaskStatus.PENDING, CoordinationTaskStatus.READY)
                        and any(dep_id in failed_upstream_ids for dep_id in downstream_task.dependencies)
                    ):
                        failed_dependency_id = next(
                            dep_id for dep_id in downstream_task.dependencies if dep_id in failed_upstream_ids
                        )
                        updated_tasks[downstream_id] = self._update_task_fields(
                            downstream_task,
                            status=CoordinationTaskStatus.BLOCKED,
                            failure_type=CoordinationFailureType.DEPENDENCY_FAILED,
                            failure_reason=f"Upstream task {failed_dependency_id} failed",
                        )
                        newly_blocked_ids.add(downstream_id)
                failed_upstream_ids = newly_blocked_ids

            # Update session status if critical failure
            new_session_status = session.status
            if failure_type == CoordinationFailureType.POLICY_DENIED and session.policy.fail_fast_on_policy_denied:
                new_session_status = CoordinationStatus.FAILED
            elif target_status == CoordinationTaskStatus.FAILED:
                new_session_status = CoordinationStatus.FAILED

            updated_session = CoordinationSession(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                mission_id=session.mission_id,
                correlation_id=session.correlation_id,
                plan_id=session.plan_id,
                plan_version=session.plan_version,
                sub_mission_ids=session.sub_mission_ids,
                status=new_session_status,
                tasks=updated_tasks,
                shared_context=session.shared_context,
                policy=session.policy,
                handoffs=session.handoffs,
                active_leases=new_leases,
                resource_locks=new_resource_locks,
                created_at=session.created_at,
                updated_at=now,
            )

            saved = self.session_repository.save_session(updated_session, tenant_context)

            self._record_audit(
                tenant_context,
                CoordinationEventType.TASK_BLOCKED.value if target_status == CoordinationTaskStatus.BLOCKED else CoordinationEventType.COORDINATION_FAILED.value,
                session_id,
                session.mission_id,
                {
                    "task_id": task_id,
                    "failure_type": failure_type.value,
                    "failure_reason": failure_reason,
                },
            )
            return saved

    # -------------------------------------------------------------------------
    # 7. Cascading Cancellation
    # -------------------------------------------------------------------------

    def cancel_coordination(
        self,
        session_id: str,
        tenant_context: TenantContext,
        reason: str = "Mission cancelled",
    ) -> CoordinationSession:
        """
        Cancela en cascada todas las tareas no terminales y libera todos los leases y locks activos.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        validate_safe_identifier(session_id, "session_id")

        with self._lock:
            session = self.session_repository.get_session(session_id, tenant_context)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            now = self._now()
            updated_tasks = dict(session.tasks)

            for tid, t in session.tasks.items():
                if not t.is_terminal:
                    updated_tasks[tid] = self._update_task_fields(
                        t,
                        status=CoordinationTaskStatus.CANCELLED,
                        failure_type=CoordinationFailureType.CANCELLED,
                        failure_reason=reason,
                        claim=None,
                    )

            updated_session = CoordinationSession(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                mission_id=session.mission_id,
                correlation_id=session.correlation_id,
                plan_id=session.plan_id,
                plan_version=session.plan_version,
                sub_mission_ids=session.sub_mission_ids,
                status=CoordinationStatus.CANCELLED,
                tasks=updated_tasks,
                shared_context=session.shared_context,
                policy=session.policy,
                handoffs=session.handoffs,
                active_leases={},
                resource_locks={},
                created_at=session.created_at,
                updated_at=now,
            )

            saved = self.session_repository.save_session(updated_session, tenant_context)

            self._record_audit(
                tenant_context,
                CoordinationEventType.COORDINATION_CANCELLED.value,
                session_id,
                session.mission_id,
                {"session_id": session_id, "reason": reason},
            )
            return saved

    # -------------------------------------------------------------------------
    # 8. Dynamic Task Delegation & Reassignment (R.5)
    # -------------------------------------------------------------------------

    def delegate_task(
        self,
        session_id: str,
        request: DelegationRequest,
        tenant_context: TenantContext,
    ) -> Tuple[CoordinationSession, DelegationRecord]:
        """
        Reasigna dinámicamente la tarea a un nuevo especialista compatible (R.5).
        Garantías estrictas:
        1. Single Owner mutable execution: Transferencia atómica (old owner lease liberado -> new owner asignado).
        2. Compatibility check: capability_id, action_type, tools y availability (R.3 SpecialistAgentRegistry).
        3. No policy/safety bypass: NO se permite delegación evasiva ante POLICY_DENIED ni EMERGENCY_STOP.
        4. Bounds enforcement: max_delegations_per_task y max_delegations_per_mission.
        5. Assignment versioning: incrementa assignment_version e invalida inmediatamente al old owner.
        6. Budget & cost continuity: preserva presupuestos sin resetear contabilidad ni tokens.
        7. Auditoría y trazabilidad completa K.1/K.2.
        """
        CrossTenantGuard.ensure_tenant_context(tenant_context)
        CrossTenantGuard.assert_same_tenant(tenant_context, request.tenant_id, "delegate_task")
        validate_safe_identifier(session_id, "session_id")

        with self._lock:
            session = self.session_repository.get_session(session_id, tenant_context)
            if not session:
                raise ValueError(f"Session {session_id} not found")

            task = session.tasks.get(request.task_id)
            if not task:
                raise ValueError(f"Task {request.task_id} not found in session")

            now = self._now()
            delegation_id = f"del-{uuid.uuid4().hex[:12]}"

            # Anti-policy bypass must be evaluated on the task even if the session
            # remains ACTIVE (fail_fast_on_policy_denied=False).
            if task.failure_type == CoordinationFailureType.POLICY_DENIED:
                decision = DelegationDecision(
                    request=request,
                    status=DelegationDecisionStatus.REJECTED_POLICY,
                    rejection_reason="POLICY_DENIED_CANNOT_BE_BYPASSED",
                    decided_at=now,
                )
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.DELEGATION_REJECTED.value,
                    session_id,
                    session.mission_id,
                    {"delegation_id": delegation_id, "reason": "POLICY_DENIED_CANNOT_BE_BYPASSED"},
                    actor_id="policy_guard",
                )
                raise ValueError("Cannot delegate task to bypass POLICY_DENIED failure")

            if session.status != CoordinationStatus.ACTIVE:
                raise ValueError(f"Cannot delegate task in non-active session ({session.status.value})")

            # 1. Audit delegation requested
            self._record_audit(
                tenant_context,
                CoordinationEventType.DELEGATION_REQUESTED.value,
                session_id,
                session.mission_id,
                {
                    "delegation_id": delegation_id,
                    "task_id": request.task_id,
                    "from_agent_id": request.from_agent_id,
                    "required_capability": request.required_capability,
                    "reason": request.reason.value,
                    "current_attempt": request.current_attempt,
                    "current_version": task.assignment_version,
                },
                actor_id=request.from_agent_id,
            )

            # 2. Safety & Emergency Stop Check
            if self.emergency_stop_service:
                is_emergency_stopped = False
                if hasattr(self.emergency_stop_service, "is_stopped"):
                    is_emergency_stopped = bool(self.emergency_stop_service.is_stopped(request.tenant_id))
                elif hasattr(self.emergency_stop_service, "evaluate"):
                    eval_ctx = EmergencyStopEvaluationContext(
                        action_name="TASK_DELEGATION",
                        mission_id=session.mission_id,
                        is_read_only=False,
                        is_external_side_effect=True,
                    )
                    stop_dec = self.emergency_stop_service.evaluate(eval_ctx)
                    is_emergency_stopped = stop_dec.is_blocked

                if is_emergency_stopped:
                    decision = DelegationDecision(
                        request=request,
                        status=DelegationDecisionStatus.REJECTED_SECURITY,
                        rejection_reason="EMERGENCY_STOP_ACTIVE",
                        decided_at=now,
                    )
                    self._record_audit(
                        tenant_context,
                        CoordinationEventType.DELEGATION_REJECTED.value,
                        session_id,
                        session.mission_id,
                        {"delegation_id": delegation_id, "reason": "EMERGENCY_STOP_ACTIVE"},
                        actor_id="emergency_stop",
                    )
                    record = DelegationRecord(
                        delegation_id=delegation_id,
                        decision=decision,
                        assignment_version=task.assignment_version + 1,
                        transferred_at=now,
                        budget_state={"max_tokens": task.allocated_budget.max_tokens if task.allocated_budget else None},
                    )
                    raise ValueError("Cannot delegate task under active EMERGENCY_STOP")

            # 3. Check delegation limits (Ping-Pong & Bounds prevention)
            current_task_delegations = len(task.delegation_history)
            max_task_del = session.policy.max_delegations_per_task
            if current_task_delegations >= max_task_del:
                decision = DelegationDecision(
                    request=request,
                    status=DelegationDecisionStatus.REJECTED_BOUNDS,
                    rejection_reason=f"MAX_DELEGATIONS_PER_TASK_REACHED ({max_task_del})",
                    decided_at=now,
                )
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.DELEGATION_LIMIT_REACHED.value,
                    session_id,
                    session.mission_id,
                    {
                        "delegation_id": delegation_id,
                        "task_id": request.task_id,
                        "current_count": current_task_delegations,
                        "limit": max_task_del,
                    },
                    actor_id="delegation_guard",
                )
                # Mark task BLOCKED if limits exceeded
                updated_tasks = dict(session.tasks)
                updated_tasks[request.task_id] = self._update_task_fields(
                    task,
                    status=CoordinationTaskStatus.BLOCKED,
                    failure_type=CoordinationFailureType.TECHNICAL_FAILURE,
                    failure_reason=f"Exceeded max delegations per task limit ({max_task_del})",
                    claim=None,
                )
                new_leases = dict(session.active_leases)
                new_leases.pop(request.task_id, None)
                updated_session = CoordinationSession(
                    session_id=session.session_id,
                    tenant_id=session.tenant_id,
                    mission_id=session.mission_id,
                    correlation_id=session.correlation_id,
                    plan_id=session.plan_id,
                    plan_version=session.plan_version,
                    sub_mission_ids=session.sub_mission_ids,
                    status=session.status,
                    tasks=updated_tasks,
                    shared_context=session.shared_context,
                    policy=session.policy,
                    handoffs=session.handoffs,
                    active_leases=new_leases,
                    resource_locks=session.resource_locks,
                    created_at=session.created_at,
                    updated_at=now,
                )
                saved = self.session_repository.save_session(updated_session, tenant_context)
                record = DelegationRecord(
                    delegation_id=delegation_id,
                    decision=decision,
                    assignment_version=task.assignment_version + 1,
                    transferred_at=now,
                    budget_state={},
                )
                return saved, record

            # 4. Candidate Selection using Capability-First SpecialistAgentRegistry (R.3)
            selected_agent_id: Optional[str] = None
            if self.agent_registry:
                candidates = self.agent_registry.find_by_capability(session.tenant_id, request.required_capability)
                # Filter out the from_agent_id and agents that are unavailable or not policy eligible
                eligible = []
                for cand in candidates:
                    if cand.agent_id == request.from_agent_id:
                        continue
                    if not cand.policy_eligible:
                        continue
                    if cand.availability not in (
                        AgentAvailability.AVAILABLE,
                        AgentAvailability.DEGRADED if session.policy.allow_fallback_to_degraded else AgentAvailability.AVAILABLE,
                    ):
                        continue
                    # Check action_type and tool permissions if applicable
                    capability_def = cand.get_capability(request.required_capability)
                    if capability_def and task.action_type and task.action_type not in capability_def.action_types:
                        continue
                    # Ranking tuple: (availability_rank, priority, cost, agent_id, agent)
                    avail_rank = 0 if cand.availability == AgentAvailability.AVAILABLE else 1
                    cost_rank = cand.estimated_cost if cand.estimated_cost is not None else Decimal("Infinity")
                    eligible.append((avail_rank, cand.priority, cost_rank, cand.agent_id, cand))

                if eligible:
                    best_match = min(eligible)
                    selected_agent_id = best_match[3]

            if not selected_agent_id:
                decision = DelegationDecision(
                    request=request,
                    status=DelegationDecisionStatus.REJECTED_CAPABILITY,
                    rejection_reason="NO_COMPATIBLE_SPECIALIST_AGENT_AVAILABLE",
                    decided_at=now,
                )
                self._record_audit(
                    tenant_context,
                    CoordinationEventType.DELEGATION_REJECTED.value,
                    session_id,
                    session.mission_id,
                    {
                        "delegation_id": delegation_id,
                        "task_id": request.task_id,
                        "reason": "NO_COMPATIBLE_SPECIALIST_AGENT_AVAILABLE",
                    },
                    actor_id="specialist_selector",
                )
                # Mark task BLOCKED if no specialist available
                updated_tasks = dict(session.tasks)
                updated_tasks[request.task_id] = self._update_task_fields(
                    task,
                    status=CoordinationTaskStatus.BLOCKED,
                    failure_type=CoordinationFailureType.CAPABILITY_UNAVAILABLE,
                    failure_reason=f"No compatible specialist agent available for capability {request.required_capability}",
                    claim=None,
                )
                new_leases = dict(session.active_leases)
                new_leases.pop(request.task_id, None)
                updated_session = CoordinationSession(
                    session_id=session.session_id,
                    tenant_id=session.tenant_id,
                    mission_id=session.mission_id,
                    correlation_id=session.correlation_id,
                    plan_id=session.plan_id,
                    plan_version=session.plan_version,
                    sub_mission_ids=session.sub_mission_ids,
                    status=session.status,
                    tasks=updated_tasks,
                    shared_context=session.shared_context,
                    policy=session.policy,
                    handoffs=session.handoffs,
                    active_leases=new_leases,
                    resource_locks=session.resource_locks,
                    created_at=session.created_at,
                    updated_at=now,
                )
                saved = self.session_repository.save_session(updated_session, tenant_context)
                record = DelegationRecord(
                    delegation_id=delegation_id,
                    decision=decision,
                    assignment_version=task.assignment_version + 1,
                    transferred_at=now,
                    budget_state={},
                )
                return saved, record

            # 5. Approved Delegation - Perform Atomic Ownership Transfer
            decision = DelegationDecision(
                request=request,
                status=DelegationDecisionStatus.APPROVED,
                to_agent_id=selected_agent_id,
                decided_at=now,
            )

            # Audit release of old assignment
            self._record_audit(
                tenant_context,
                CoordinationEventType.ASSIGNMENT_RELEASED.value,
                session_id,
                session.mission_id,
                {
                    "delegation_id": delegation_id,
                    "task_id": request.task_id,
                    "released_agent_id": request.from_agent_id,
                    "old_version": task.assignment_version,
                },
                actor_id=request.from_agent_id,
            )

            # Update lease and claims
            new_version = task.assignment_version + 1
            new_history = tuple(list(task.delegation_history) + [request.from_agent_id])

            new_claim = TaskClaim(
                task_id=request.task_id,
                agent_id=selected_agent_id,
                claimed_at=now,
                is_active=True,
            )

            new_leases = dict(session.active_leases)
            new_leases[request.task_id] = new_claim

            new_resource_locks = dict(session.resource_locks)
            if task.resource_id:
                new_resource_locks[task.resource_id] = selected_agent_id

            updated_tasks = dict(session.tasks)
            updated_tasks[request.task_id] = self._update_task_fields(
                task,
                status=CoordinationTaskStatus.CLAIMED,
                assigned_agent_id=selected_agent_id,
                assignment_version=new_version,
                delegation_history=new_history,
                claim=new_claim,
                attempt=task.attempt + 1,
                failure_type=None,
                failure_reason=None,
            )

            updated_session = CoordinationSession(
                session_id=session.session_id,
                tenant_id=session.tenant_id,
                mission_id=session.mission_id,
                correlation_id=session.correlation_id,
                plan_id=session.plan_id,
                plan_version=session.plan_version,
                sub_mission_ids=session.sub_mission_ids,
                status=session.status,
                tasks=updated_tasks,
                shared_context=session.shared_context,
                policy=session.policy,
                handoffs=session.handoffs,
                active_leases=new_leases,
                resource_locks=new_resource_locks,
                budget_reserved=session.budget_reserved,
                created_at=session.created_at,
                updated_at=now,
            )

            saved = self.session_repository.save_session(updated_session, tenant_context)

            # Audit assignment transferred
            self._record_audit(
                tenant_context,
                CoordinationEventType.ASSIGNMENT_TRANSFERRED.value,
                session_id,
                session.mission_id,
                {
                    "delegation_id": delegation_id,
                    "task_id": request.task_id,
                    "from_agent_id": request.from_agent_id,
                    "to_agent_id": selected_agent_id,
                    "assignment_version": new_version,
                    "delegation_count": len(new_history),
                },
                actor_id=selected_agent_id,
            )

            record = DelegationRecord(
                delegation_id=delegation_id,
                decision=decision,
                assignment_version=new_version,
                transferred_at=now,
                budget_state={
                    "max_tokens": task.allocated_budget.max_tokens if task.allocated_budget else None,
                    "max_cost": str(task.allocated_budget.max_cost) if (task.allocated_budget and task.allocated_budget.max_cost is not None) else None,
                },
            )
            return saved, record

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _refresh_task_readiness(self, session: CoordinationSession) -> CoordinationSession:
        updated_tasks: Dict[str, CoordinationTask] = dict(session.tasks)
        dirty = False

        for task_id in sorted(session.tasks):
            task = session.tasks[task_id]
            if task.status not in (CoordinationTaskStatus.PENDING, CoordinationTaskStatus.READY):
                continue

            dependencies = [session.tasks.get(dep_id) for dep_id in task.dependencies]
            failed_dependency = next(
                (
                    dependency for dependency in dependencies
                    if dependency and dependency.status in (
                        CoordinationTaskStatus.FAILED,
                        CoordinationTaskStatus.BLOCKED,
                        CoordinationTaskStatus.CANCELLED,
                    )
                ),
                None,
            )
            if failed_dependency:
                updated_tasks[task_id] = self._update_task_fields(
                    task,
                    status=CoordinationTaskStatus.BLOCKED,
                    failure_type=CoordinationFailureType.DEPENDENCY_FAILED,
                    failure_reason=(
                        f"Upstream dependency {failed_dependency.task_id} failed with status "
                        f"{failed_dependency.status.value}"
                    ),
                )
                dirty = True
                continue

            dependencies_completed = all(
                dependency is not None and dependency.status == CoordinationTaskStatus.COMPLETED
                for dependency in dependencies
            )
            inputs_present = all(
                key in task.inputs
                or (session.shared_context is not None and key in session.shared_context.facts)
                for key in task.required_input_keys
            )
            desired_status = (
                CoordinationTaskStatus.READY
                if dependencies_completed and inputs_present
                else CoordinationTaskStatus.PENDING
            )
            if task.status != desired_status:
                updated_tasks[task_id] = self._update_task_fields(task, status=desired_status)
                dirty = True

        if not dirty:
            return session
        return CoordinationSession(
            session_id=session.session_id,
            tenant_id=session.tenant_id,
            mission_id=session.mission_id,
            correlation_id=session.correlation_id,
            plan_id=session.plan_id,
            plan_version=session.plan_version,
            sub_mission_ids=session.sub_mission_ids,
            status=session.status,
            tasks=updated_tasks,
            shared_context=session.shared_context,
            policy=session.policy,
            handoffs=session.handoffs,
            active_leases=session.active_leases,
            resource_locks=session.resource_locks,
            budget_reserved=session.budget_reserved,
            created_at=session.created_at,
            updated_at=self._now(),
        )

    @staticmethod
    def _update_task_fields(task: CoordinationTask, **kwargs) -> CoordinationTask:
        data = {
            "task_id": task.task_id,
            "session_id": task.session_id,
            "tenant_id": task.tenant_id,
            "required_capability": task.required_capability,
            "action_type": task.action_type,
            "assigned_agent_id": task.assigned_agent_id,
            "status": task.status,
            "dependencies": task.dependencies,
            "required_input_keys": task.required_input_keys,
            "inputs": task.inputs,
            "outputs": task.outputs,
            "allocated_budget": task.allocated_budget,
            "claim": task.claim,
            "failure_type": task.failure_type,
            "failure_reason": task.failure_reason,
            "evidence_refs": task.evidence_refs,
            "is_side_effecting": task.is_side_effecting,
            "resource_id": task.resource_id,
            "attempt": task.attempt,
            "assignment_version": task.assignment_version,
            "delegation_history": task.delegation_history,
            "metadata": task.metadata,
        }
        data.update(kwargs)
        return CoordinationTask(**data)
