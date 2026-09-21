"""Integration tests for R.4 Agent Coordination (Milestone R: Advanced Autonomy).

Covers the 10 canonical integration scenarios (A - J):
A. R.1 DAG + múltiples especialistas: Session created from R.1 DAG and specialist agents with correct assignment and topological readiness.
B. fan-out: Concurrent claim and execution across independent DAG branches up to policy bounds.
C. fan-in: Downstream task waits for upstream completion and merges inputs correctly.
D. duplicate claim / single owner: Atomic single owner leases prevent double execution and resource lock conflicts.
E. conflicting outputs: Unresolvable contradictions trigger CONFLICT failure and block downstream target task.
F. technical failure: Technical failure cascades DEPENDENCY_FAILED downstream while completed steps stay intact.
G. policy denied no bypass: POLICY_DENIED fails session/step strictly with zero evasion or reroute.
H. tenant isolation: CrossTenantGuard strictly isolates sessions, claims, handoffs and state across tenants.
I. budget/concurrency: Concurrency and budget bounds strictly enforced at task claim time.
J. R.2 result propagation: Hierarchical Sub-missions coordinate smoothly with DAG tasks and handoff context.
"""

from decimal import Decimal
import pytest
from typing import Dict, Any, Sequence, Optional, List

from src.application.agent_coordination.coordinator_service import AgentCoordinatorService
from src.domain.agent_coordination.merger import DeterministicResultMerger, MergeStrategy
from src.domain.agent_coordination.models import (
    AgentHandoff,
    CoordinationFailureType,
    CoordinationPolicy,
    CoordinationSession,
    CoordinationStatus,
    CoordinationTask,
    CoordinationTaskStatus,
    TaskClaim,
)
from src.domain.audit.models import AuditRecord, AuditRecordType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.mission.models import Mission, MissionType, MissionStatus
from src.domain.planning.models import ExecutionPlan, PlanBudget, PlanStep, StepStatus, StepFailureType
from src.domain.specialist_agent.models import (
    AgentAvailability,
    AgentCapability,
    AgentCapabilityContract,
    SpecialistAgentDefinition,
)
from src.domain.specialist_agent.registry import SpecialistAgentRegistry
from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.tool.models import ToolContract, ToolSchemaField
from src.infrastructure.persistence.data.json.coordination_session_repository import (
    InMemoryCoordinationSessionRepository,
)


class MockAuditRepository(AuditRepositoryPort):
    def __init__(self):
        self.records: List[AuditRecord] = []

    def append(self, record: AuditRecord) -> AuditRecord:
        self.records.append(record)
        return record

    def get_by_id(self, audit_id: str) -> Optional[AuditRecord]:
        for r in self.records:
            if r.audit_id == audit_id:
                return r
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AuditRecord]:
        return None

    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[Any] = None,
        to_time: Optional[Any] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        return [
            r for r in self.records
            if (mission_id is None or r.mission_id == mission_id)
            and (record_type is None or r.record_type == record_type)
        ]

    def reconstruct_mission_timeline(self, mission_id: str):
        return None

    def query(self, *args, **kwargs) -> List[AuditRecord]:
        return list(self.records)


def sample_contract(name: str, field_name: str = "query") -> ToolContract:
    return ToolContract(name, (ToolSchemaField(field_name, "str"),), allow_extra_fields=True)


def sample_capability(name: str = "MARKET_RESEARCH", action_type: str = "SEARCH") -> AgentCapability:
    return AgentCapability(
        capability_id=name,
        contract=AgentCapabilityContract(
            sample_contract("input", "topic"),
            sample_contract("output", "summary"),
        ),
        action_types=(action_type,),
        tool_ids=(),
    )


def sample_agent(
    agent_id: str,
    tenant_id: str = "tenant-alpha",
    capability_name: str = "MARKET_RESEARCH",
    action_type: str = "SEARCH",
    availability: AgentAvailability = AgentAvailability.AVAILABLE,
) -> SpecialistAgentDefinition:
    return SpecialistAgentDefinition(
        agent_id=agent_id,
        tenant_id=tenant_id,
        capabilities=(sample_capability(capability_name, action_type),),
        availability=availability,
        allowed_action_types=(action_type,),
        allowed_tool_ids=(),
    )


@pytest.fixture
def tenant_alpha():
    return TenantContext(tenant_id="tenant-alpha", correlation_id="corr-alpha-123")


@pytest.fixture
def tenant_beta():
    return TenantContext(tenant_id="tenant-beta", correlation_id="corr-beta-456")


@pytest.fixture
def session_repo():
    return InMemoryCoordinationSessionRepository()


@pytest.fixture
def audit_repo():
    return MockAuditRepository()


# ==================================================================================================
# ESCENARIO A: R.1 DAG + Múltiples Especialistas
# ==================================================================================================
def test_scenario_a_r1_dag_multi_specialist_initialization_and_readiness(
    tenant_alpha, session_repo, audit_repo
):
    """
    Escenario A:
    - DAG de 3 pasos con especialistas asignados (Investigador, Evaluador, Publicador).
    - Session inicializada mapea R.1 steps a CoordinationTasks.
    - Únicamente las tareas sin dependencias insatisfechas se colocan en READY.
    - Especialistas registrados y disponibles son validados.
    """
    registry = SpecialistAgentRegistry()
    registry.register(sample_agent("agent-scout", "tenant-alpha", "MARKET_RESEARCH", "SEARCH"))
    registry.register(sample_agent("agent-analyst", "tenant-alpha", "SUPPLIER_ANALYSIS", "ANALYZE"))
    registry.register(sample_agent("agent-publisher", "tenant-alpha", "LISTING_GEN", "PUBLISH"))

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    step_1 = PlanStep(
        step_id="step-scout",
        objective="Scout market trends",
        assigned_agent="agent-scout",
        assigned_capability="MARKET_RESEARCH",
        action_type="SEARCH",
        required_inputs={"topic": "running shoes"},
        expected_outputs=("market_data",),
    )
    step_2 = PlanStep(
        step_id="step-analyze",
        objective="Analyze suppliers",
        assigned_agent="agent-analyst",
        assigned_capability="SUPPLIER_ANALYSIS",
        action_type="ANALYZE",
        dependencies=("step-scout",),
        required_inputs={"market_data": "placeholder"},
        expected_outputs=("supplier_id", "cost"),
    )
    step_3 = PlanStep(
        step_id="step-publish",
        objective="Publish listing",
        assigned_agent="agent-publisher",
        assigned_capability="LISTING_GEN",
        action_type="PUBLISH",
        dependencies=("step-analyze",),
        required_inputs={"supplier_id": "placeholder"},
        expected_outputs=("listing_url",),
    )

    plan = ExecutionPlan(
        plan_id="plan-dag-101",
        tenant_id="tenant-alpha",
        mission_id="mission-alpha-1",
        goal="Launch automated product",
        steps=(step_1, step_2, step_3),
        budget=PlanBudget(max_tokens=10000, max_cost=Decimal("5.0")),
    )

    session = service.create_session(
        tenant_context=tenant_alpha,
        mission_id="mission-alpha-1",
        plan=plan,
    )

    assert session.session_id.startswith("coord-")
    assert session.status == CoordinationStatus.ACTIVE
    assert len(session.tasks) == 3

    # Step scout is READY, step analyze and publish are PENDING
    ready_tasks = service.get_ready_tasks(session.session_id, tenant_alpha)
    assert len(ready_tasks) == 1
    assert ready_tasks[0].task_id == "step-scout"
    assert session.tasks["step-analyze"].status == CoordinationTaskStatus.PENDING
    assert session.tasks["step-publish"].status == CoordinationTaskStatus.PENDING

    # Step scout claimed and completed
    session, claim = service.claim_task(session.session_id, "step-scout", "agent-scout", tenant_alpha)
    assert claim.agent_id == "agent-scout"
    assert session.tasks["step-scout"].status == CoordinationTaskStatus.CLAIMED

    session = service.complete_task(
        session.session_id,
        "step-scout",
        outputs={"market_data": {"trend": "high_demand"}},
        tenant_context=tenant_alpha,
    )
    assert session.tasks["step-scout"].status == CoordinationTaskStatus.COMPLETED

    # Now step analyze becomes READY
    ready_tasks = service.get_ready_tasks(session.session_id, tenant_alpha)
    assert len(ready_tasks) == 1
    assert ready_tasks[0].task_id == "step-analyze"


# ==================================================================================================
# ESCENARIO B: Fan-Out
# ==================================================================================================
def test_scenario_b_fan_out_independent_branches_and_concurrency(
    tenant_alpha, session_repo
):
    """
    Escenario B:
    - 3 ramas independientes en paralelo (Branch A, B, C).
    - Las 3 tareas están READY simultáneamente.
    - Se pueden reclamar y ejecutar en paralelo respetando los límites de política.
    """
    step_a = PlanStep(step_id="step-branch-a", objective="Branch A", assigned_agent="agent-a", action_type="SEARCH")
    step_b = PlanStep(step_id="step-branch-b", objective="Branch B", assigned_agent="agent-b", action_type="SEARCH")
    step_c = PlanStep(step_id="step-branch-c", objective="Branch C", assigned_agent="agent-c", action_type="SEARCH")

    plan = ExecutionPlan(
        plan_id="plan-fan-out",
        tenant_id="tenant-alpha",
        mission_id="mission-fan-out",
        goal="Fan out research",
        steps=(step_a, step_b, step_c),
    )

    policy = CoordinationPolicy(max_concurrent_agents=3, max_concurrent_tasks=3)
    service = AgentCoordinatorService(session_repository=session_repo)
    session = service.create_session(tenant_alpha, "mission-fan-out", plan=plan, policy=policy)

    ready = service.get_ready_tasks(session.session_id, tenant_alpha)
    assert len(ready) == 3
    ready_ids = {t.task_id for t in ready}
    assert ready_ids == {"step-branch-a", "step-branch-b", "step-branch-c"}

    # Concurrent claims for all 3
    session, claim_a = service.claim_task(session.session_id, "step-branch-a", "agent-a", tenant_alpha)
    session, claim_b = service.claim_task(session.session_id, "step-branch-b", "agent-b", tenant_alpha)
    session, claim_c = service.claim_task(session.session_id, "step-branch-c", "agent-c", tenant_alpha)

    assert len(session.active_leases) == 3
    assert session.tasks["step-branch-a"].status == CoordinationTaskStatus.CLAIMED
    assert session.tasks["step-branch-b"].status == CoordinationTaskStatus.CLAIMED
    assert session.tasks["step-branch-c"].status == CoordinationTaskStatus.CLAIMED

    # Complete branch A and branch B; C is still running
    session = service.complete_task(session.session_id, "step-branch-a", {"result_a": 10}, tenant_alpha)
    session = service.complete_task(session.session_id, "step-branch-b", {"result_b": 20}, tenant_alpha)
    assert len(session.active_leases) == 1
    assert session.tasks["step-branch-a"].status == CoordinationTaskStatus.COMPLETED
    assert session.tasks["step-branch-b"].status == CoordinationTaskStatus.COMPLETED
    assert session.tasks["step-branch-c"].status == CoordinationTaskStatus.CLAIMED


# ==================================================================================================
# ESCENARIO C: Fan-In
# ==================================================================================================
def test_scenario_c_fan_in_barrier_waits_for_all_inputs(
    tenant_alpha, session_repo
):
    """
    Escenario C:
    - Rama A y Rama B convergen en un paso Join.
    - Join permanece PENDING mientras falta alguna rama.
    - Cuando ambas ramas completan, merge_task_results transfiere y consolida inputs hacia Join.
    - Join pasa a READY.
    """
    step_a = PlanStep(
        step_id="step-src-1",
        objective="Price check",
        assigned_agent="agent-pricer",
        action_type="SEARCH",
        expected_outputs=("price",),
    )
    step_b = PlanStep(
        step_id="step-src-2",
        objective="Stock check",
        assigned_agent="agent-stock",
        action_type="SEARCH",
        expected_outputs=("stock_qty",),
    )
    step_join = PlanStep(
        step_id="step-join",
        objective="Synthesize offer",
        assigned_agent="agent-joiner",
        action_type="SYNTHESIZE",
        dependencies=("step-src-1", "step-src-2"),
        required_inputs={"price": 0, "stock_qty": 0},
    )

    plan = ExecutionPlan(
        plan_id="plan-fan-in",
        tenant_id="tenant-alpha",
        mission_id="mission-fan-in",
        goal="Combine offer",
        steps=(step_a, step_b, step_join),
    )

    service = AgentCoordinatorService(session_repository=session_repo)
    session = service.create_session(tenant_alpha, "mission-fan-in", plan=plan)

    # Initially step-join is PENDING
    assert session.tasks["step-join"].status == CoordinationTaskStatus.PENDING

    # Complete only step-src-1
    session, _ = service.claim_task(session.session_id, "step-src-1", "agent-pricer", tenant_alpha)
    session = service.complete_task(session.session_id, "step-src-1", {"price": 99.5}, tenant_alpha)

    # step-join is still not READY because step-src-2 is pending
    ready = service.get_ready_tasks(session.session_id, tenant_alpha)
    assert len(ready) == 1
    assert ready[0].task_id == "step-src-2"

    # Complete step-src-2
    session, _ = service.claim_task(session.session_id, "step-src-2", "agent-stock", tenant_alpha)
    session = service.complete_task(session.session_id, "step-src-2", {"stock_qty": 150}, tenant_alpha)

    # Perform deterministic merge into step-join
    merge_res = service.merge_task_results(
        session_id=session.session_id,
        source_task_ids=("step-src-1", "step-src-2"),
        target_task_id="step-join",
        tenant_context=tenant_alpha,
        strategy=MergeStrategy.KEYED_MERGE,
    )
    assert merge_res.success is True
    assert merge_res.merged_data["price"] == 99.5
    assert merge_res.merged_data["stock_qty"] == 150

    # step-join is now READY
    ready = service.get_ready_tasks(session.session_id, tenant_alpha)
    assert len(ready) == 1
    assert ready[0].task_id == "step-join"
    assert ready[0].inputs["price"] == 99.5
    assert ready[0].inputs["stock_qty"] == 150


# ==================================================================================================
# ESCENARIO D: Duplicate Claim / Single Owner
# ==================================================================================================
def test_scenario_d_duplicate_claim_and_single_owner_protection(
    tenant_alpha, session_repo
):
    """
    Escenario D:
    - Una tarea solo puede ser reclamada por un único agente (Single Owner).
    - Un segundo claim concurrente o repetido sobre la misma tarea falla de forma atómica.
    - Dos tareas sobre el mismo resource_id no pueden ejecutarse a la vez (Resource Lock Exclusión Mutua).
    """
    step_1 = PlanStep(
        step_id="step-write-a",
        objective="Write order record",
        assigned_agent="agent-worker-1",
        action_type="WRITE",
        metadata={"resource_id": "order-table-lock"},
    )
    step_2 = PlanStep(
        step_id="step-write-b",
        objective="Update order status",
        assigned_agent="agent-worker-2",
        action_type="WRITE",
        metadata={"resource_id": "order-table-lock"},
    )

    plan = ExecutionPlan(
        plan_id="plan-locks",
        tenant_id="tenant-alpha",
        mission_id="mission-locks",
        goal="Exclusive resource test",
        steps=(step_1, step_2),
    )

    service = AgentCoordinatorService(session_repository=session_repo)
    session = service.create_session(tenant_alpha, "mission-locks", plan=plan)

    # 1. Single owner lease claim
    session, claim_1 = service.claim_task(session.session_id, "step-write-a", "agent-worker-1", tenant_alpha)
    assert claim_1.is_active is True
    assert session.tasks["step-write-a"].status == CoordinationTaskStatus.CLAIMED
    assert session.resource_locks["order-table-lock"] == "agent-worker-1"

    # Duplicate claim on step-write-a must raise ValueError
    with pytest.raises(ValueError, match="already claimed or not in READY state"):
        service.claim_task(session.session_id, "step-write-a", "agent-worker-1", tenant_alpha)

    with pytest.raises(ValueError, match="already claimed or not in READY state"):
        service.claim_task(session.session_id, "step-write-a", "agent-worker-2", tenant_alpha)

    # 2. Resource lock conflict on step-write-b
    with pytest.raises(ValueError, match="Resource lock 'order-table-lock' is currently held"):
        service.claim_task(session.session_id, "step-write-b", "agent-worker-2", tenant_alpha)

    # Complete step-write-a releases both lease and resource lock
    session = service.complete_task(session.session_id, "step-write-a", {"status": "ok"}, tenant_alpha)
    assert "order-table-lock" not in session.resource_locks
    assert "step-write-a" not in session.active_leases

    # Now step-write-b can be safely claimed
    session, claim_2 = service.claim_task(session.session_id, "step-write-b", "agent-worker-2", tenant_alpha)
    assert session.resource_locks["order-table-lock"] == "agent-worker-2"
    assert session.tasks["step-write-b"].status == CoordinationTaskStatus.CLAIMED


# ==================================================================================================
# ESCENARIO E: Conflicting Outputs
# ==================================================================================================
def test_scenario_e_conflicting_outputs_blocks_target_task_with_conflict(
    tenant_alpha, session_repo, audit_repo
):
    """
    Escenario E:
    - Dos agentes producen resultados contradictorios para una misma clave requerida.
    - La fusión determinista detecta el conflicto y marca la tarea downstream como BLOCKED con failure_type=CONFLICT.
    """
    step_a = PlanStep(step_id="step-eval-1", objective="Audit 1", assigned_agent="agent-1", action_type="SEARCH")
    step_b = PlanStep(step_id="step-eval-2", objective="Audit 2", assigned_agent="agent-2", action_type="SEARCH")
    step_target = PlanStep(
        step_id="step-target",
        objective="Consolidated decision",
        assigned_agent="agent-3",
        action_type="MERGE",
        dependencies=("step-eval-1", "step-eval-2"),
    )

    plan = ExecutionPlan(
        plan_id="plan-conflict",
        tenant_id="tenant-alpha",
        mission_id="mission-conflict",
        goal="Detect conflict",
        steps=(step_a, step_b, step_target),
    )

    service = AgentCoordinatorService(session_repository=session_repo, audit_repository=audit_repo)
    session = service.create_session(tenant_alpha, "mission-conflict", plan=plan)

    session, _ = service.claim_task(session.session_id, "step-eval-1", "agent-1", tenant_alpha)
    session = service.complete_task(session.session_id, "step-eval-1", {"product_status": "AVAILABLE"}, tenant_alpha)

    session, _ = service.claim_task(session.session_id, "step-eval-2", "agent-2", tenant_alpha)
    session = service.complete_task(session.session_id, "step-eval-2", {"product_status": "DISCONTINUED"}, tenant_alpha)

    # Merge task results into step-target
    merge_result = service.merge_task_results(
        session_id=session.session_id,
        source_task_ids=("step-eval-1", "step-eval-2"),
        target_task_id="step-target",
        tenant_context=tenant_alpha,
        strategy=MergeStrategy.KEYED_MERGE,
    )

    assert merge_result.success is False
    assert len(merge_result.conflicts) == 1
    assert "key 'product_status'" in merge_result.conflicts[0]

    updated_session = service.get_session(session.session_id, tenant_alpha)
    target_task = updated_session.tasks["step-target"]
    assert target_task.status == CoordinationTaskStatus.BLOCKED
    assert target_task.failure_type == CoordinationFailureType.CONFLICT
    assert "Merge conflict" in str(target_task.failure_reason)


# ==================================================================================================
# ESCENARIO F: Technical Failure
# ==================================================================================================
def test_scenario_f_technical_failure_cascades_dependency_failed(
    tenant_alpha, session_repo, audit_repo
):
    """
    Escenario F:
    - Fallo técnico en una tarea intermedia (TECHNICAL_FAILURE).
    - Las tareas dependientes downstream transitan automáticamente a BLOCKED con DEPENDENCY_FAILED.
    - Las tareas completadas previamente no se corrompen y permanecen COMPLETED.
    - La sesión completa se marca como FAILED.
    """
    step_1 = PlanStep(step_id="step-prep", objective="Preparation", assigned_agent="agent-prep", action_type="SEARCH")
    step_2 = PlanStep(step_id="step-fetch", objective="Fetch API data", assigned_agent="agent-fetch", action_type="FETCH", dependencies=("step-prep",))
    step_3 = PlanStep(step_id="step-post", objective="Post data", assigned_agent="agent-post", action_type="POST", dependencies=("step-fetch",))

    plan = ExecutionPlan(
        plan_id="plan-tech-fail",
        tenant_id="tenant-alpha",
        mission_id="mission-tech-fail",
        goal="Handle technical failure",
        steps=(step_1, step_2, step_3),
    )

    service = AgentCoordinatorService(session_repository=session_repo, audit_repository=audit_repo)
    session = service.create_session(tenant_alpha, "mission-tech-fail", plan=plan)

    # Complete step-prep
    session, _ = service.claim_task(session.session_id, "step-prep", "agent-prep", tenant_alpha)
    session = service.complete_task(session.session_id, "step-prep", {"prep_ok": True}, tenant_alpha)
    assert session.tasks["step-prep"].status == CoordinationTaskStatus.COMPLETED

    # Claim and fail step-fetch with TECHNICAL_FAILURE
    session, _ = service.claim_task(session.session_id, "step-fetch", "agent-fetch", tenant_alpha)
    session = service.fail_task(
        session_id=session.session_id,
        task_id="step-fetch",
        failure_type=CoordinationFailureType.TECHNICAL_FAILURE,
        failure_reason="Network socket timeout (504)",
        tenant_context=tenant_alpha,
    )

    # Verify statuses
    assert session.tasks["step-prep"].status == CoordinationTaskStatus.COMPLETED
    assert session.tasks["step-fetch"].status == CoordinationTaskStatus.FAILED
    assert session.tasks["step-fetch"].failure_type == CoordinationFailureType.TECHNICAL_FAILURE

    # Downstream step-post must be BLOCKED with DEPENDENCY_FAILED
    assert session.tasks["step-post"].status == CoordinationTaskStatus.BLOCKED
    assert session.tasks["step-post"].failure_type == CoordinationFailureType.DEPENDENCY_FAILED
    assert "Upstream task step-fetch failed" in str(session.tasks["step-post"].failure_reason)

    assert session.status == CoordinationStatus.FAILED
    assert any(
        record.record_type == AuditRecordType.COORDINATION_FAILED
        and record.subject_id == session.session_id
        and record.metadata["task_id"] == "step-fetch"
        for record in audit_repo.records
    )


# ==================================================================================================
# ESCENARIO G: Policy Denied No Bypass
# ==================================================================================================
def test_scenario_g_policy_denied_strict_block_no_bypass(
    tenant_alpha, session_repo, audit_repo
):
    """
    Escenario G:
    - Tarea bloqueada por denegación estricta de política de seguridad/gobernanza (POLICY_DENIED).
    - Cero rerouting o bypass evasivo.
    - La tarea y la sesión transitan a terminal FAILED de forma inmediata y estricta.
    """
    step_policy = PlanStep(
        step_id="step-restricted-publish",
        objective="Publish unapproved catalog",
        assigned_agent="agent-pub",
        action_type="PUBLISH",
    )
    plan = ExecutionPlan(
        plan_id="plan-policy-strict",
        tenant_id="tenant-alpha",
        mission_id="mission-policy-strict",
        goal="Enforce policy boundaries",
        steps=(step_policy,),
    )

    policy = CoordinationPolicy(fail_fast_on_policy_denied=True)
    service = AgentCoordinatorService(session_repository=session_repo, audit_repository=audit_repo)
    session = service.create_session(tenant_alpha, "mission-policy-strict", plan=plan, policy=policy)

    session, _ = service.claim_task(session.session_id, "step-restricted-publish", "agent-pub", tenant_alpha)
    session = service.fail_task(
        session_id=session.session_id,
        task_id="step-restricted-publish",
        failure_type=CoordinationFailureType.POLICY_DENIED,
        failure_reason="Policy Engine DENY: Catalog publication requires explicit human authorization",
        tenant_context=tenant_alpha,
    )

    assert session.tasks["step-restricted-publish"].status == CoordinationTaskStatus.FAILED
    assert session.tasks["step-restricted-publish"].failure_type == CoordinationFailureType.POLICY_DENIED
    assert session.status == CoordinationStatus.FAILED

    # Attempting to get ready tasks returns empty
    ready = service.get_ready_tasks(session.session_id, tenant_alpha)
    assert len(ready) == 0


# ==================================================================================================
# ESCENARIO H: Tenant Isolation
# ==================================================================================================
def test_scenario_h_tenant_isolation_strictly_enforced(
    tenant_alpha, tenant_beta, session_repo
):
    """
    Escenario H:
    - Sesión creada por Tenant Alpha no puede ser leída, reclamada, transferida ni modificada por Tenant Beta.
    - CrossTenantAccessError o TenantMismatchError lanzado en todas las operaciones del coordinador.
    """
    step_1 = PlanStep(step_id="step-alpha-1", objective="Alpha only", assigned_agent="agent-alpha", action_type="SEARCH")
    plan_alpha = ExecutionPlan(
        plan_id="plan-alpha-1",
        tenant_id="tenant-alpha",
        mission_id="mission-alpha-1",
        goal="Alpha goal",
        steps=(step_1,),
    )

    service = AgentCoordinatorService(session_repository=session_repo)
    session_alpha = service.create_session(tenant_alpha, "mission-alpha-1", plan=plan_alpha)

    # Tenant Beta cannot get session of Tenant Alpha
    with pytest.raises(CrossTenantAccessError):
        service.get_session(session_alpha.session_id, tenant_beta)

    # Tenant Beta cannot claim task in Tenant Alpha's session
    with pytest.raises(CrossTenantAccessError):
        service.claim_task(session_alpha.session_id, "step-alpha-1", "agent-beta", tenant_beta)

    # Tenant Beta cannot execute handoff in Tenant Alpha's session
    with pytest.raises(CrossTenantAccessError):
        service.handoff(
            session_id=session_alpha.session_id,
            source_agent_id="agent-beta",
            target_task_id="step-alpha-1",
            payload={"injected": "exploit"},
            tenant_context=tenant_beta,
        )

    # Tenant Beta cannot cancel Tenant Alpha's session
    with pytest.raises(CrossTenantAccessError):
        service.cancel_coordination(session_alpha.session_id, tenant_beta)


# ==================================================================================================
# ESCENARIO I: Budget & Concurrency
# ==================================================================================================
def test_scenario_i_budget_and_concurrency_limits_enforced(
    tenant_alpha, session_repo
):
    """
    Escenario I:
    - max_concurrent_tasks y max_concurrent_agents limitan reclamaciones activas.
    - Tareas con presupuesto agotado (max_tokens <= 0 o max_cost <= 0) no pueden reclamarse.
    """
    step_1 = PlanStep(
        step_id="step-low-budget",
        objective="Exhausted budget task",
        assigned_agent="agent-1",
        action_type="SEARCH",
        estimated_cost=Decimal("0.0"),  # exhausted
    )
    step_2 = PlanStep(
        step_id="step-ok-budget-1",
        objective="Ok task 1",
        assigned_agent="agent-2",
        action_type="SEARCH",
        estimated_cost=Decimal("5.0"),
    )
    step_3 = PlanStep(
        step_id="step-ok-budget-2",
        objective="Ok task 2",
        assigned_agent="agent-3",
        action_type="SEARCH",
        estimated_cost=Decimal("5.0"),
    )

    plan = ExecutionPlan(
        plan_id="plan-budget-test",
        tenant_id="tenant-alpha",
        mission_id="mission-budget",
        goal="Budget limits",
        steps=(step_1, step_2, step_3),
    )

    # Concurrency limit = 1
    policy = CoordinationPolicy(max_concurrent_agents=1, max_concurrent_tasks=1)
    service = AgentCoordinatorService(session_repository=session_repo)
    session = service.create_session(tenant_alpha, "mission-budget", plan=plan, policy=policy)

    # Claiming step-low-budget fails due to exhausted cost budget (<= 0)
    with pytest.raises(ValueError, match="exhausted cost budget"):
        service.claim_task(session.session_id, "step-low-budget", "agent-1", tenant_alpha)

    # Claim step-ok-budget-1 succeeds (reaches max concurrency 1)
    session, claim_2 = service.claim_task(session.session_id, "step-ok-budget-1", "agent-2", tenant_alpha)
    assert claim_2.task_id == "step-ok-budget-1"

    # Attempting to claim step-ok-budget-2 fails due to max concurrent tasks reached
    with pytest.raises(ValueError, match="Maximum concurrent tasks limit reached"):
        service.claim_task(session.session_id, "step-ok-budget-2", "agent-3", tenant_alpha)


# ==================================================================================================
# ESCENARIO J: R.2 Result Propagation
# ==================================================================================================
def test_scenario_j_r2_result_propagation_and_zero_cot_handoff(
    tenant_alpha, session_repo, audit_repo
):
    """
    Escenario J:
    - SubMisiones creadas bajo R.2 se integran como tareas coordinadas.
    - Handoff estructurado Zero-CoT transfiere datos, hechos y evidencia hacia el SharedContext y la siguiente tarea.
    - Verificación de que el CoT privado / secrets quedan completamente REDACTED.
    - Finalización de todas las tareas lleva la sesión a CoordinationStatus.COMPLETED.
    """
    sub_1 = Mission(
        mission_id="sub-mission-discovery",
        type=MissionType.MARKET_DISCOVERY,
        parent_mission_id="root-mission-j",
        parameters={
            "assigned_agent": "agent-scout",
            "capability_id": "MARKET_RESEARCH",
            "action_type": "SEARCH",
        },
    )
    sub_2 = Mission(
        mission_id="sub-mission-pricing",
        type=MissionType.PROFIT_EVALUATION,
        parent_mission_id="root-mission-j",
        parameters={
            "assigned_agent": "agent-pricer",
            "capability_id": "PRICE_OPTIMIZATION",
            "action_type": "CALCULATE",
            "dependencies": ("sub-mission-discovery",),
        },
    )

    service = AgentCoordinatorService(session_repository=session_repo, audit_repository=audit_repo)
    session = service.create_session(
        tenant_context=tenant_alpha,
        mission_id="root-mission-j",
        sub_missions=(sub_1, sub_2),
    )

    assert len(session.tasks) == 2
    assert session.tasks["sub-mission-discovery"].status == CoordinationTaskStatus.READY
    assert session.tasks["sub-mission-pricing"].status == CoordinationTaskStatus.PENDING

    # Claim and execute sub-mission-discovery
    session, _ = service.claim_task(session.session_id, "sub-mission-discovery", "agent-scout", tenant_alpha)

    # Perform structured Zero-CoT handoff to sub-mission-pricing
    raw_payload = {
        "best_niche": "trail_shoes",
        "market_score": 88,
        "thought": "Internal private reasoning",
        "scratchpad": "Drafting step 1.. 2.. 3",
        "api_key": "secret-key-xyz",
    }
    session, handoff = service.handoff(
        session_id=session.session_id,
        source_agent_id="agent-scout",
        target_task_id="sub-mission-pricing",
        payload=raw_payload,
        tenant_context=tenant_alpha,
        evidence_refs=("doc-ref-discovery-99",),
    )

    # Verify Zero-CoT sanitization
    assert handoff.payload["best_niche"] == "trail_shoes"
    assert handoff.payload["market_score"] == 88
    assert handoff.payload["thought"] == "[REDACTED]"
    assert handoff.payload["scratchpad"] == "[REDACTED]"
    assert handoff.payload["api_key"] == "[REDACTED]"
    assert "doc-ref-discovery-99" in handoff.evidence_refs

    # Complete discovery task
    session = service.complete_task(
        session.session_id,
        "sub-mission-discovery",
        outputs={"discovery_completed": True},
        tenant_context=tenant_alpha,
    )

    # sub-mission-pricing is now READY with transferred inputs
    ready = service.get_ready_tasks(session.session_id, tenant_alpha)
    assert len(ready) == 1
    assert ready[0].task_id == "sub-mission-pricing"
    assert ready[0].inputs["best_niche"] == "trail_shoes"

    # Claim and complete pricing task
    session, _ = service.claim_task(session.session_id, "sub-mission-pricing", "agent-pricer", tenant_alpha)
    session = service.complete_task(
        session.session_id,
        "sub-mission-pricing",
        outputs={"recommended_price": 49.99},
        tenant_context=tenant_alpha,
    )

    # Verify session completion
    assert session.status == CoordinationStatus.COMPLETED
    assert session.is_all_tasks_completed() is True
    assert session.tasks["sub-mission-discovery"].status == CoordinationTaskStatus.COMPLETED
    assert session.tasks["sub-mission-pricing"].status == CoordinationTaskStatus.COMPLETED
    assert session.shared_context.facts["recommended_price"] == 49.99
