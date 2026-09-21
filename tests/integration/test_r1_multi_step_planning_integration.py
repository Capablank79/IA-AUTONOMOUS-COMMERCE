"""
Suite de Pruebas de Integración para R.1 — Multi-Step Planning (Hito R: Advanced Autonomy).

Cubre exhaustivamente los 10 escenarios canónicos de integración (A - J):
A. Complex Goal -> Multi-step valid plan decomposition and topological order.
B. Dependency Chain -> Correct READY progression step by step.
C. Parallel Independent Branches -> Multiple steps become READY simultaneously.
D. Step Failure -> Bounded subgraph replanning preserving completed steps.
E. Budget Exhaustion -> BLOCKED / FAILED safely without illegal replanning.
F. Policy Denied Step -> Zero bypass replanning (REPLANNING != POLICY BYPASS).
G. Emergency Stop -> Affected steps non-executable / blocked from READY.
H. Tenant A / B Strict Isolation (CrossTenantGuard enforcement).
I. Autonomous Runtime Execution Bridge -> Consuming MultiStepPlanningService without duplicate executor.
J. Audit Trail (K.1) & Agent Trace (K.2) -> Comprehensive version history and event trail without CoT.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import tempfile
import shutil
import pytest
from types import MappingProxyType
from typing import Dict, Any, List, Optional, Sequence, Tuple

from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.audit.models import AuditRecord, AuditRecordType
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.agent_trace.models import AgentTraceRecord, StepType, TraceStatus
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.emergency_stop.models import (
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
    EmergencyStopReasonCode,
    EmergencyStopEvaluationContext,
    EmergencyStopDecisionStatus,
)
from src.domain.emergency_stop.ports import EmergencyStopServicePort, EmergencyStopRepositoryPort
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
)
from src.domain.planning.validator import (
    PlanValidator,
    PlanValidationError,
    PlanCycleDetectedError,
    CapabilityUnavailableError,
    BudgetExceededError,
)
from src.application.planning.multi_step_planning_service import MultiStepPlanningService
from src.infrastructure.persistence.data.json.execution_plan_repository import (
    InMemoryExecutionPlanRepository,
    JsonExecutionPlanRepository,
)


class MockCapabilityRegistry(CapabilityRegistryPort):
    def __init__(self, available_capabilities: Dict[str, bool]):
        self._capabilities = dict(available_capabilities)

    def is_capability_available(self, capability_name: str) -> bool:
        return self._capabilities.get(capability_name, False)

    def get_available_capabilities(self) -> Sequence[str]:
        return [k for k, v in self._capabilities.items() if v]

    def get_known_capabilities(self) -> Sequence[str]:
        return list(self._capabilities.keys())


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
        for r in self.records:
            if r.idempotency_key == idempotency_key:
                return r
        return None

    def list_records(
        self,
        mission_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        return list(self.records)

    def reconstruct_mission_timeline(self, mission_id: str):
        return None

    def query(self, *args, **kwargs) -> List[AuditRecord]:
        return list(self.records)


class MockAgentTraceRepository(AgentTraceRepositoryPort):
    def __init__(self):
        self.records: List[AgentTraceRecord] = []

    def append(self, record: AgentTraceRecord) -> AgentTraceRecord:
        self.records.append(record)
        return record

    def get_by_id(self, trace_id: str) -> Optional[AgentTraceRecord]:
        for r in self.records:
            if r.trace_id == trace_id:
                return r
        return None

    def get_by_idempotency_key(self, idempotency_key: str) -> Optional[AgentTraceRecord]:
        for r in self.records:
            if r.idempotency_key == idempotency_key:
                return r
        return None

    def list_records(
        self,
        execution_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        component_name: Optional[str] = None,
        step_type: Optional[StepType] = None,
        status: Optional[TraceStatus] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AgentTraceRecord]:
        return [r for r in self.records if (execution_id is None or r.execution_id == execution_id)]

    def get_execution_timeline(self, execution_id: str):
        return None

    def list_by_execution_id(self, execution_id: str) -> List[AgentTraceRecord]:
        return [r for r in self.records if r.execution_id == execution_id]


class MockEmergencyStopService(EmergencyStopServicePort):
    def __init__(self, blocked_actions: Optional[List[str]] = None, global_blocked: bool = False):
        self.blocked_actions = blocked_actions or []
        self.global_blocked = global_blocked

    def activate_stop(self, *args, **kwargs):
        pass

    def deactivate_stop(self, *args, **kwargs):
        pass

    def evaluate(self, context: EmergencyStopEvaluationContext):
        from src.domain.emergency_stop.models import EmergencyStopDecision
        if self.global_blocked or context.action_type in self.blocked_actions:
            return EmergencyStopDecision(
                decision_id="dec_stop",
                decision_status=EmergencyStopDecisionStatus.BLOCK_EXECUTION,
                reason_code=EmergencyStopReasonCode.ACTION_TYPE_STOP_ACTIVE,
                reason_details=f"Action '{context.action_type}' stopped by emergency policy.",
                evaluated_at=datetime.now(timezone.utc),
            )
        return EmergencyStopDecision(
            decision_id="dec_allow",
            decision_status=EmergencyStopDecisionStatus.ALLOW_EXECUTION,
            reason_code=EmergencyStopReasonCode.NO_ACTIVE_STOP,
            reason_details="Execution allowed.",
            evaluated_at=datetime.now(timezone.utc),
        )


@pytest.fixture
def tenant_alpha():
    return TenantContext(tenant_id="tenant-alpha", identity_id="user-alpha")


@pytest.fixture
def tenant_beta():
    return TenantContext(tenant_id="tenant-beta", identity_id="user-beta")


@pytest.fixture
def capability_registry():
    return MockCapabilityRegistry({
        "MARKET_RESEARCH": True,
        "SUPPLIER_ANALYSIS": True,
        "LISTING_GENERATION": True,
        "PRICE_OPTIMIZATION": True,
        "STOCK_ALLOCATION": True,
        "UNSUPPORTED_CAPABILITY": False,
    })


@pytest.fixture
def audit_repo():
    return MockAuditRepository()


@pytest.fixture
def trace_repo():
    return MockAgentTraceRepository()


@pytest.fixture
def plan_repo():
    return InMemoryExecutionPlanRepository()


# --------------------------------------------------------------------------------------------------
# SCENARIO A: Complex Goal -> Multi-step Valid Plan Decomposition & Topological Order
# --------------------------------------------------------------------------------------------------
def test_scenario_a_complex_goal_decomposition_and_dag_validation(
    tenant_alpha, capability_registry, audit_repo, trace_repo, plan_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
    )

    steps = [
        PlanStep(
            step_id="step-1",
            objective="Analyze market trends and demand",
            action_type="MARKET_RESEARCH",
            assigned_capability="MARKET_RESEARCH",
            depth_level=1,
            estimated_cost=Decimal("0.50"),
        ),
        PlanStep(
            step_id="step-2",
            objective="Evaluate supplier quotes and margins",
            action_type="SUPPLIER_ANALYSIS",
            assigned_capability="SUPPLIER_ANALYSIS",
            dependencies=("step-1",),
            depth_level=2,
            estimated_cost=Decimal("0.75"),
        ),
        PlanStep(
            step_id="step-3",
            objective="Generate SEO product catalog listing",
            action_type="LISTING_GENERATION",
            assigned_capability="LISTING_GENERATION",
            dependencies=("step-2",),
            depth_level=2,
            estimated_cost=Decimal("0.25"),
        ),
    ]

    budget = PlanBudget(
        max_cost=Decimal("5.00"),
        max_tokens=10000,
        max_steps=10,
        max_replans=3,
    )

    res = service.create_plan(
        mission_id="mission-alpha-001",
        goal="Launch automated dropshipping campaign for Q4 electronics",
        tenant_context=tenant_alpha,
        initial_steps=steps,
        budget=budget,
    )

    assert res.success is True
    assert res.status == PlanStatus.VALIDATED
    assert res.plan is not None
    assert len(res.plan.steps) == 3
    assert res.plan.version == 1
    assert ("step-1",) == res.ready_step_ids

    # Topological order verification
    is_valid, errs, topo = PlanValidator.validate_dag(res.plan)
    assert is_valid is True
    assert topo == ["step-1", "step-2", "step-3"]


# --------------------------------------------------------------------------------------------------
# SCENARIO B: Dependency Chain -> Correct READY Progression Step by Step
# --------------------------------------------------------------------------------------------------
def test_scenario_b_dependency_chain_ready_progression(
    tenant_alpha, capability_registry, plan_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
    )

    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="MARKET_RESEARCH", assigned_capability="MARKET_RESEARCH"),
        PlanStep(step_id="s2", objective="Step 2", action_type="SUPPLIER_ANALYSIS", assigned_capability="SUPPLIER_ANALYSIS", dependencies=("s1",)),
        PlanStep(step_id="s3", objective="Step 3", action_type="LISTING_GENERATION", assigned_capability="LISTING_GENERATION", dependencies=("s2",)),
    ]

    res = service.create_plan(
        mission_id="mission-seq",
        goal="Sequential execution chain",
        tenant_context=tenant_alpha,
        initial_steps=steps,
    )
    plan_id = res.plan.plan_id

    # Initial: only s1 is READY
    ready_steps = service.get_ready_steps(res.plan, tenant_alpha)
    assert [s.step_id for s in ready_steps] == ["s1"]

    # Mark s1 complete -> s2 becomes READY
    plan_v1_1 = service.mark_step_completed(plan_id, "s1", {"market": "validated"}, tenant_alpha)
    ready_v1_1 = service.get_ready_steps(plan_v1_1, tenant_alpha)
    assert [s.step_id for s in ready_v1_1] == ["s2"]
    assert plan_v1_1.get_step("s1").status == StepStatus.COMPLETED
    assert plan_v1_1.get_step("s2").status == StepStatus.READY
    assert plan_v1_1.get_step("s3").status == StepStatus.PENDING

    # Mark s2 complete -> s3 becomes READY
    plan_v1_2 = service.mark_step_completed(plan_id, "s2", {"supplier": "selected"}, tenant_alpha)
    ready_v1_2 = service.get_ready_steps(plan_v1_2, tenant_alpha)
    assert [s.step_id for s in ready_v1_2] == ["s3"]

    # Mark s3 complete -> Plan becomes COMPLETED
    plan_v1_3 = service.mark_step_completed(plan_id, "s3", {"listing": "published"}, tenant_alpha)
    assert plan_v1_3.status == PlanStatus.COMPLETED
    assert len(service.get_ready_steps(plan_v1_3, tenant_alpha)) == 0


# --------------------------------------------------------------------------------------------------
# SCENARIO C: Parallel Independent Branches -> Simultaneous READY
# --------------------------------------------------------------------------------------------------
def test_scenario_c_parallel_independent_branches(
    tenant_alpha, capability_registry, plan_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
    )

    # Branch A: root -> A1 -> A2
    # Branch B: root -> B1 -> B2
    # Join: Join1 depends on (A2, B2)
    steps = [
        PlanStep(step_id="root", objective="Root discovery", action_type="MARKET_RESEARCH", assigned_capability="MARKET_RESEARCH"),
        PlanStep(step_id="branch-A1", objective="Branch A Step 1", action_type="SUPPLIER_ANALYSIS", assigned_capability="SUPPLIER_ANALYSIS", dependencies=("root",)),
        PlanStep(step_id="branch-B1", objective="Branch B Step 1", action_type="PRICE_OPTIMIZATION", assigned_capability="PRICE_OPTIMIZATION", dependencies=("root",)),
        PlanStep(step_id="branch-A2", objective="Branch A Step 2", action_type="STOCK_ALLOCATION", assigned_capability="STOCK_ALLOCATION", dependencies=("branch-A1",)),
        PlanStep(step_id="branch-B2", objective="Branch B Step 2", action_type="LISTING_GENERATION", assigned_capability="LISTING_GENERATION", dependencies=("branch-B1",)),
        PlanStep(step_id="join-final", objective="Final Join", action_type="LISTING_GENERATION", assigned_capability="LISTING_GENERATION", dependencies=("branch-A2", "branch-B2")),
    ]

    res = service.create_plan(
        mission_id="mission-fork-join",
        goal="Fork-Join parallel pipeline",
        tenant_context=tenant_alpha,
        initial_steps=steps,
    )
    plan_id = res.plan.plan_id

    # Complete root -> Both branch-A1 and branch-B1 must be READY simultaneously
    plan_after_root = service.mark_step_completed(plan_id, "root", {"data": "ok"}, tenant_alpha)
    ready = service.get_ready_steps(plan_after_root, tenant_alpha)
    ready_ids = sorted([s.step_id for s in ready])
    assert ready_ids == ["branch-A1", "branch-B1"]

    # Complete A1, B1 in parallel -> A2 and B2 must become READY
    service.mark_step_completed(plan_id, "branch-A1", {}, tenant_alpha)
    plan_after_b1 = service.mark_step_completed(plan_id, "branch-B1", {}, tenant_alpha)
    ready_2 = service.get_ready_steps(plan_after_b1, tenant_alpha)
    assert sorted([s.step_id for s in ready_2]) == ["branch-A2", "branch-B2"]

    # Complete only A2 -> join-final must NOT be ready yet
    plan_after_a2 = service.mark_step_completed(plan_id, "branch-A2", {}, tenant_alpha)
    assert [s.step_id for s in service.get_ready_steps(plan_after_a2, tenant_alpha)] == ["branch-B2"]

    # Complete B2 -> join-final is now READY
    plan_after_b2 = service.mark_step_completed(plan_id, "branch-B2", {}, tenant_alpha)
    assert [s.step_id for s in service.get_ready_steps(plan_after_b2, tenant_alpha)] == ["join-final"]


# --------------------------------------------------------------------------------------------------
# SCENARIO D: Step Failure -> Bounded Subgraph Replanning Preserving Completed Steps
# --------------------------------------------------------------------------------------------------
def test_scenario_d_step_failure_subgraph_replan_preserves_completed(
    tenant_alpha, capability_registry, plan_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
    )

    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="MARKET_RESEARCH", assigned_capability="MARKET_RESEARCH"),
        PlanStep(step_id="s2", objective="Step 2", action_type="SUPPLIER_ANALYSIS", assigned_capability="SUPPLIER_ANALYSIS", dependencies=("s1",)),
        PlanStep(step_id="s3", objective="Step 3", action_type="LISTING_GENERATION", assigned_capability="LISTING_GENERATION", dependencies=("s2",)),
    ]

    res = service.create_plan(
        mission_id="mission-fail-replan",
        goal="Replan test",
        tenant_context=tenant_alpha,
        initial_steps=steps,
    )
    plan_id = res.plan.plan_id

    # Complete s1
    service.mark_step_completed(plan_id, "s1", {"market": "trends"}, tenant_alpha)

    # Now step 2 fails due to supplier timeout (EXECUTION_FAILURE)
    # Provide alternative subgraph for step 2 (substitute supplier search)
    new_subgraph = [
        PlanStep(
            step_id="s2-alt",
            objective="Evaluate fallback suppliers",
            action_type="SUPPLIER_ANALYSIS",
            assigned_capability="SUPPLIER_ANALYSIS",
            dependencies=("s1",),
        ),
        PlanStep(
            step_id="s3",
            objective="Step 3",
            action_type="LISTING_GENERATION",
            assigned_capability="LISTING_GENERATION",
            dependencies=("s2-alt",),
        ),
    ]

    replan_res = service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="s2",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="Primary supplier API timed out after 3 retries",
        new_subgraph_steps=new_subgraph,
    )

    assert replan_res.success is True
    new_plan = replan_res.plan
    assert new_plan.version == 2
    assert new_plan.replan_count == 1
    assert len(new_plan.history) == 1
    assert new_plan.history[0].preserved_step_ids == ("s1",)

    # Verify s1 is still COMPLETED and has original outputs
    s1_step = new_plan.get_step("s1")
    assert s1_step.status == StepStatus.COMPLETED
    assert s1_step.actual_outputs == {"market": "trends"}

    # Next ready step is s2-alt
    ready = service.get_ready_steps(new_plan, tenant_alpha)
    assert [s.step_id for s in ready] == ["s2-alt"]


# --------------------------------------------------------------------------------------------------
# SCENARIO E: Budget Exhaustion -> BLOCKED / FAILED Safely
# --------------------------------------------------------------------------------------------------
def test_scenario_e_budget_exhaustion_blocks_plan(
    tenant_alpha, capability_registry, plan_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
    )

    budget = PlanBudget(
        max_cost=Decimal("10.00"),
        max_replans=2,
    )

    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="MARKET_RESEARCH", assigned_capability="MARKET_RESEARCH"),
    ]

    res = service.create_plan(
        mission_id="mission-budget-test",
        goal="Budget overflow testing",
        tenant_context=tenant_alpha,
        initial_steps=steps,
        budget=budget,
    )
    plan_id = res.plan.plan_id

    # Replan 1 (OK)
    r1 = service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="s1",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="Attempt 1 failed",
    )
    assert r1.success is True

    # Replan 2 (OK)
    r2 = service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="s1",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="Attempt 2 failed",
    )
    assert r2.success is True

    # Replan 3 (Exceeds max_replans=2 -> FAILED safely)
    r3 = service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="s1",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="Attempt 3 failed",
    )
    assert r3.success is False
    assert r3.status == PlanStatus.FAILED
    assert "Maximum replan limit" in r3.errors[0]


# --------------------------------------------------------------------------------------------------
# SCENARIO F: Policy Denied Step -> Zero Bypass Replanning
# --------------------------------------------------------------------------------------------------
def test_scenario_f_policy_denied_step_zero_bypass(
    tenant_alpha, capability_registry, plan_repo, audit_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
        audit_repository=audit_repo,
    )

    steps = [
        PlanStep(step_id="s1", objective="Execute sensitive purchase", action_type="SUPPLIER_ANALYSIS", assigned_capability="SUPPLIER_ANALYSIS"),
    ]

    res = service.create_plan(
        mission_id="mission-policy-denied",
        goal="Policy violation enforcement",
        tenant_context=tenant_alpha,
        initial_steps=steps,
    )
    plan_id = res.plan.plan_id

    # Simulating policy engine rejection: Step was denied by N.3 / N.4
    replan_res = service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="s1",
        failure_type=StepFailureType.POLICY_DENIED,
        failure_reason="Financial limit exceeded ($500.00 > $100.00 limit). Action denied by PolicyEngine.",
    )

    assert replan_res.success is False
    assert replan_res.status == PlanStatus.BLOCKED
    assert "Cannot bypass governance" in replan_res.errors[0]

    # Verify step and plan are permanently BLOCKED in repository
    persisted_plan = plan_repo.get_plan_by_id(plan_id, tenant_alpha)
    assert persisted_plan.status == PlanStatus.BLOCKED
    assert persisted_plan.get_step("s1").status == StepStatus.BLOCKED
    assert persisted_plan.get_step("s1").failure_type == StepFailureType.POLICY_DENIED


# --------------------------------------------------------------------------------------------------
# SCENARIO G: Emergency Stop -> Affected Steps Non-Executable
# --------------------------------------------------------------------------------------------------
def test_scenario_g_emergency_stop_blocks_ready_execution(
    tenant_alpha, capability_registry, plan_repo
):
    # Setup Emergency Stop blocking 'PRICE_OPTIMIZATION'
    estop_service = MockEmergencyStopService(blocked_actions=["PRICE_OPTIMIZATION"])

    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
        emergency_stop_service=estop_service,
    )

    steps = [
        PlanStep(step_id="s1", objective="Research", action_type="MARKET_RESEARCH", assigned_capability="MARKET_RESEARCH"),
        PlanStep(step_id="s2", objective="Reprice", action_type="PRICE_OPTIMIZATION", assigned_capability="PRICE_OPTIMIZATION"),
    ]

    res = service.create_plan(
        mission_id="mission-estop",
        goal="Emergency stop testing",
        tenant_context=tenant_alpha,
        initial_steps=steps,
    )

    # Although both s1 and s2 have no dependencies, s2 is blocked by Emergency Stop
    ready_steps = service.get_ready_steps(res.plan, tenant_alpha)
    assert [s.step_id for s in ready_steps] == ["s1"]


# --------------------------------------------------------------------------------------------------
# SCENARIO H: Tenant A / B Strict Isolation
# --------------------------------------------------------------------------------------------------
def test_scenario_h_tenant_isolation(
    tenant_alpha, tenant_beta, capability_registry, plan_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
    )

    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="MARKET_RESEARCH", assigned_capability="MARKET_RESEARCH"),
    ]

    # Tenant Alpha creates plan
    res_alpha = service.create_plan(
        mission_id="mission-alpha",
        goal="Tenant Alpha goal",
        tenant_context=tenant_alpha,
        initial_steps=steps,
    )
    plan_alpha_id = res_alpha.plan.plan_id

    # Tenant Beta cannot read Tenant Alpha's plan
    assert plan_repo.get_plan_by_id(plan_alpha_id, tenant_beta) is None

    # Tenant Beta cannot mark step complete or replan on Tenant Alpha's plan
    with pytest.raises((CrossTenantAccessError, PlanValidationError)):
        service.mark_step_completed(plan_alpha_id, "s1", {}, tenant_beta)

    with pytest.raises((CrossTenantAccessError, PlanValidationError)):
        service.replan(
            plan_id=plan_alpha_id,
            tenant_context=tenant_beta,
            failed_step_id="s1",
            failure_type=StepFailureType.EXECUTION_FAILURE,
            failure_reason="Attacking from Tenant Beta",
        )


# --------------------------------------------------------------------------------------------------
# SCENARIO I: Autonomous Runtime Execution Bridge (Planner != Executor)
# --------------------------------------------------------------------------------------------------
def test_scenario_i_autonomous_runtime_bridge(
    tenant_alpha, capability_registry, plan_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
    )

    # 3-step DAG
    steps = [
        PlanStep(step_id="s1", objective="Scan products", action_type="MARKET_RESEARCH", assigned_capability="MARKET_RESEARCH"),
        PlanStep(step_id="s2", objective="Generate listings", action_type="LISTING_GENERATION", assigned_capability="LISTING_GENERATION", dependencies=("s1",)),
        PlanStep(step_id="s3", objective="Optimize pricing", action_type="PRICE_OPTIMIZATION", assigned_capability="PRICE_OPTIMIZATION", dependencies=("s2",)),
    ]

    res = service.create_plan(
        mission_id="mission-runtime-bridge",
        goal="Bridge with autonomous runtime",
        tenant_context=tenant_alpha,
        initial_steps=steps,
    )
    plan_id = res.plan.plan_id

    # Mock Autonomous Loop execution loop
    executed_order = []
    current_plan = res.plan

    while current_plan.status != PlanStatus.COMPLETED:
        ready_steps = service.get_ready_steps(current_plan, tenant_alpha)
        if not ready_steps:
            break

        for step in ready_steps:
            # Runtime executes the action (simulated)
            executed_order.append(step.step_id)
            output_payload = {f"{step.step_id}_result": "SUCCESS"}
            current_plan = service.mark_step_completed(
                plan_id=plan_id,
                step_id=step.step_id,
                outputs=output_payload,
                tenant_context=tenant_alpha,
            )

    assert executed_order == ["s1", "s2", "s3"]
    assert current_plan.status == PlanStatus.COMPLETED


# --------------------------------------------------------------------------------------------------
# SCENARIO J: Audit Trail (K.1) & Agent Trace (K.2) Complete Version Trail
# --------------------------------------------------------------------------------------------------
def test_scenario_j_audit_and_trace_emission(
    tenant_alpha, capability_registry, plan_repo, audit_repo, trace_repo
):
    service = MultiStepPlanningService(
        plan_repository=plan_repo,
        capability_registry=capability_registry,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
    )

    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="MARKET_RESEARCH", assigned_capability="MARKET_RESEARCH"),
    ]

    res = service.create_plan(
        mission_id="mission-audit-trail",
        goal="Audit trail testing",
        tenant_context=tenant_alpha,
        initial_steps=steps,
    )
    plan_id = res.plan.plan_id

    service.replan(
        plan_id=plan_id,
        tenant_context=tenant_alpha,
        failed_step_id="s1",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="Network transient error",
    )

    # Check Audit Records emitted
    audit_records = audit_repo.query()
    assert len(audit_records) >= 2
    for rec in audit_records:
        assert rec.actor.details.get("tenant_id") == "tenant-alpha"
        assert rec.subject_type == "EXECUTION_PLAN"
        assert rec.subject_id == plan_id

    # Check Trace Records emitted
    trace_records = trace_repo.list_by_execution_id(plan_id)
    assert len(trace_records) >= 2
    for tr in trace_records:
        assert tr.execution_id == plan_id
        assert tr.mission_id == "mission-audit-trail"
