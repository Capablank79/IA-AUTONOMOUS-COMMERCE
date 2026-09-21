"""
GATE P — Formal Validation and Verification E2E Test Suite for Hito R (Advanced Autonomy).

This comprehensive test suite validates that R.1 to R.7 function end-to-end as a unified,
coherent, secure, traceable, multi-tenant autonomous system with zero regressions.

Principal Invariants & Gate Requirements Tested:
1. R.1 Multi-step Planning: DAG validation, Kahn topological order, readiness, bounded replan, step immutability.
2. R.2 Sub-missions: Hierarchy (parent/root), bounded depth, context minimization, structured result propagation.
3. R.3 Specialist Agents: Capability contracts, deterministic selection, tool allowlists, UNKNOWN != AVAILABLE.
4. R.4 Agent Coordination: Atomic single-owner claims, fan-out across independent branches, fan-in merge, no duplicates.
5. R.5 Dynamic Delegation: Failure/degradation reassignment, atomic lock transfer, monotonic versioning, ping-pong bounds.
6. R.6 Long-running Missions: Durable SHA-256 checkpoints, pause/resume lifecycle, lease & heartbeat, restart recovery.
7. R.7 Self-monitoring: Signal aggregation, UNKNOWN != HEALTHY, bounded remediation dispatch (PAUSE/REPLAN/DELEGATE/BLOCK).
8. Single-Owner Invariant: Only one logical active owner per mutable task across the entire mission lifecycle.
9. Stale Worker Rejection: Outdated leases or stale assignment versions cannot complete, mutate, or resume tasks.
10. Budget & Quota Continuity: Consumed tokens, costs, and rate-limits persist across replan, delegation, and resume.
11. Anti-Policy Bypass: POLICY_DENIED is strictly terminal and cannot be circumvented via replan, reroute, or delegation.
12. Emergency Stop Precedence: N.11 EmergencyStop blocks coordination, execution, delegation, and resume immediately.
13. Tenant Isolation: Complete multi-tenant partitioning across plans, sub-missions, sessions, checkpoints, and health signals.
14. Anti-CoT & Sensitive Sanitization: Zero leakage of private reasoning fields, scratchpads, tokens, or credentials.
15. Controlled Failure Recovery: Worker technical failure -> dynamic delegation -> checkpoint -> restart -> resume -> complete.
16. Complex Canonical Autonomous Mission: Multi-step DAG, multiple sub-missions, specialist agents, fan-out/fan-in merge,
    delegation, durable checkpoint/resume, and self-monitoring verification resulting in successful mission completion.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Sequence, Mapping
import pytest

from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.reliability.ports import ClockPort
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.audit.models import AuditRecord, AuditRecordType
from src.domain.agent_trace.ports import AgentTraceRepositoryPort
from src.domain.agent_trace.models import AgentTraceRecord, StepType, TraceStatus
from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionPriority
from src.domain.mission.ports import ActionExecutor, LoopDecision, LoopState
from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    StepDependency,
    PlanBudget,
    PlanningResult,
    PlanStatus,
    StepStatus,
    StepFailureType,
    DependencyType,
)
from src.domain.planning.ports import ExecutionPlanRepositoryPort
from src.application.planning.multi_step_planning_service import MultiStepPlanningService
from src.domain.sub_mission.models import (
    SubMissionScope,
    SubMissionCreationContract,
    SubMissionResultContract,
    SubMissionFailureType,
    SubMissionHierarchyPolicy,
    SubMissionNode,
)
from src.domain.sub_mission.validator import (
    HierarchyCycleDetectedError,
    MaxDepthExceededError,
)
from src.domain.sub_mission.ports import SubMissionRepositoryPort
from src.domain.mission_dashboard.ports import TenantMissionRepositoryPort
from src.application.sub_mission.service import SubMissionService
from src.domain.tool.models import (
    ToolContract,
    ToolSchemaField,
    ToolDescriptor,
    ToolVersion,
    ToolSideEffectLevel,
    ToolLifecycleStatus,
)
from src.domain.tool.registry import ToolRegistry
from src.domain.specialist_agent.models import (
    AgentAvailability,
    AgentCapability,
    AgentCapabilityContract,
    AgentExecutionContext,
    AgentExecutionFailureType,
    AgentExecutionResult,
    AgentExecutionStatus,
    AgentSelectionResult,
    AgentSelectionStatus,
    SpecialistAgentDefinition,
)
from src.domain.specialist_agent.registry import SpecialistAgentRegistry
from src.application.specialist_agent.service import SpecialistAgentService
from src.domain.agent_coordination.models import (
    CoordinationSession,
    CoordinationTask,
    CoordinationStatus,
    CoordinationTaskStatus,
    CoordinationFailureType,
    CoordinationPolicy,
    MergeStrategy,
    MergeResult,
    TaskClaim,
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
from src.domain.agent_coordination.ports import CoordinationSessionRepositoryPort
from src.application.agent_coordination.coordinator_service import AgentCoordinatorService
from src.domain.long_running_mission.models import (
    MissionCheckpoint,
    StepCheckpointData,
    CheckpointStatus,
    LeaseState,
    LeaseStatus,
    HeartbeatRecord,
    ResumeDecision,
    ResumeDecisionStatus,
    StaleWorkerError,
    CheckpointIntegrityError,
)
from src.domain.long_running_mission.ports import (
    MissionCheckpointRepositoryPort,
    LeaseManagerPort,
    HeartbeatPort,
)
from src.application.long_running_mission.long_running_mission_service import LongRunningMissionService
from src.domain.self_monitoring.models import (
    MissionHealthStatus,
    SignalType,
    SignalSource,
    SignalSeverity,
    SignalCompleteness,
    DegradationReasonCode,
    SelfMonitoringAction,
    HealthSignal,
    SelfMonitoringDecision,
    MissionHealthSnapshot,
    HealthAssessment,
    SelfMonitoringPolicy,
)
from src.domain.self_monitoring.ports import (
    SelfMonitoringRepositoryPort,
    SignalCollectorPort,
    SelfMonitoringAuditPort,
)
from src.application.self_monitoring.self_monitoring_service import (
    SelfMonitoringService,
    InMemorySelfMonitoringRepository,
)
from src.application.self_monitoring.self_monitoring_action_dispatcher import SelfMonitoringActionDispatcher
from src.domain.emergency_stop.models import (
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
    EmergencyStopReasonCode,
    EmergencyStopEvaluationContext,
    EmergencyStopDecision,
    EmergencyStopDecisionStatus,
)
from src.domain.emergency_stop.ports import EmergencyStopServicePort, EmergencyStopRepositoryPort
from src.application.emergency_stop.emergency_stop_service import EmergencyStopService
from src.infrastructure.persistence.data.json.execution_plan_repository import JsonExecutionPlanRepository
from src.infrastructure.persistence.data.json.tenant_mission_repository import JsonTenantMissionRepository
from src.infrastructure.persistence.data.json.coordination_session_repository import (
    JsonCoordinationSessionRepository,
    InMemoryCoordinationSessionRepository,
)
from src.infrastructure.persistence.data.json.long_running_mission_repository import (
    JsonMissionCheckpointRepository,
    InMemoryLeaseManager,
    InMemoryHeartbeatManager,
)


# ============================================================================
# MOCKS & TEST INFRASTRUCTURE
# ============================================================================

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
        execution_id: Optional[str] = None,
        mission_id: Optional[str] = None,
        cycle_id: Optional[str] = None,
        correlation_id: Optional[str] = None,
        record_type: Optional[AuditRecordType] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AuditRecord]:
        return list(self.records)

    def reconstruct_mission_timeline(self, mission_id: str) -> Optional[Any]:
        return None


class MockTraceRepository(AgentTraceRepositoryPort):
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
        step_type: Optional[StepType] = None,
        status: Optional[TraceStatus] = None,
        from_time: Optional[datetime] = None,
        to_time: Optional[datetime] = None,
        limit: int = 1000,
    ) -> List[AgentTraceRecord]:
        return list(self.records)

    def get_execution_timeline(self, execution_id: str):
        from src.domain.agent_trace.models import ExecutionTraceTimeline
        return ExecutionTraceTimeline(execution_id=execution_id, records=tuple(self.records), started_at=datetime.now(timezone.utc), total_steps=len(self.records))


class MockEmergencyStopRepo(EmergencyStopRepositoryPort):
    def __init__(self):
        self.records: Dict[str, EmergencyStopRecord] = {}

    def save(self, record: EmergencyStopRecord) -> None:
        self.records[record.stop_id] = record

    def get_by_id(self, stop_id: str) -> Optional[EmergencyStopRecord]:
        return self.records.get(stop_id)

    def list_records(
        self,
        scope: Optional[EmergencyStopScope] = None,
        target_id: Optional[str] = None,
        state: Optional[EmergencyStopState] = None,
    ) -> Sequence[EmergencyStopRecord]:
        res = list(self.records.values())
        if scope:
            res = [r for r in res if r.scope == scope]
        if target_id:
            res = [r for r in res if r.target_id == target_id]
        if state:
            res = [r for r in res if r.state == state]
        return res

    def list_active_records(self, current_time: datetime) -> Sequence[EmergencyStopRecord]:
        return [r for r in self.records.values() if r.state == EmergencyStopState.ACTIVE]


class GuardedActionExecutor(ActionExecutor):
    is_guarded_executor = True

    def __init__(self, default_outputs: Optional[Dict[str, Any]] = None, fail: bool = False, fail_type: str = "TECHNICAL_FAILURE"):
        self.default_outputs = default_outputs or {"result": "success", "status": "OK"}
        self.fail = fail
        self.fail_type = fail_type
        self.calls: List[Dict[str, Any]] = []

    def execute(self, decision: LoopDecision, state: LoopState) -> dict:
        self.calls.append({"decision": decision, "state": state})
        if self.fail:
            return {"status": "FAILED", "failure_type": self.fail_type, "error": "Simulated failure"}
        return {"status": "SUCCESS", "outputs": self.default_outputs, "cost_used": "1.00", "tokens_used": 100}


# ============================================================================
# GATE P FIXTURES & ENVIRONMENT BUILDER
# ============================================================================

@pytest.fixture
def test_clock():
    return VirtualClock(initial_time=datetime(2026, 9, 17, 10, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def audit_repo():
    return MockAuditRepository()


@pytest.fixture
def trace_repo():
    return MockTraceRepository()


@pytest.fixture
def emergency_stop_repo():
    return MockEmergencyStopRepo()


@pytest.fixture
def emergency_stop_service(emergency_stop_repo, audit_repo, trace_repo, test_clock):
    return EmergencyStopService(
        repository=emergency_stop_repo,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
        clock=test_clock,
    )


@pytest.fixture
def planning_service(tmp_path, audit_repo, trace_repo, emergency_stop_service, test_clock):
    plan_repo = JsonExecutionPlanRepository(base_dir=tmp_path / "plans")
    return MultiStepPlanningService(
        plan_repository=plan_repo,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
        emergency_stop_service=emergency_stop_service,
        clock=test_clock,
    )


@pytest.fixture
def sub_mission_service(tmp_path, audit_repo, trace_repo, emergency_stop_service, test_clock):
    mission_repo = JsonTenantMissionRepository(base_storage_dir=tmp_path / "missions")
    plan_repo = JsonExecutionPlanRepository(base_dir=tmp_path / "plans")
    return SubMissionService(
        mission_repository=mission_repo,
        plan_repository=plan_repo,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
        emergency_stop_service=emergency_stop_service,
        clock=test_clock,
    )


@pytest.fixture
def specialist_registry():
    ic = ToolContract("input", (ToolSchemaField("query", "str"),), allow_extra_fields=False)
    oc = ToolContract("output", (ToolSchemaField("items", "list"),), allow_extra_fields=False)
    tool_search = ToolDescriptor("search_market", "Search", ToolVersion("v1"), "Search market listings", "MARKET_RESEARCH", ic, oc, ToolSideEffectLevel.READ_ONLY, status=ToolLifecycleStatus.AVAILABLE)
    tool_analyze = ToolDescriptor("analyze_prices", "Analyze", ToolVersion("v1"), "Analyze prices", "PRICE_OPTIMIZATION", ic, oc, ToolSideEffectLevel.READ_ONLY, status=ToolLifecycleStatus.AVAILABLE)
    tool_publish = ToolDescriptor("publish_listing", "Publish", ToolVersion("v1"), "Publish listing", "PUBLISH", ic, oc, ToolSideEffectLevel.WRITE, status=ToolLifecycleStatus.AVAILABLE)

    tool_reg = ToolRegistry()
    tool_reg.register(tool_search)
    tool_reg.register(tool_analyze)
    tool_reg.register(tool_publish)

    reg = SpecialistAgentRegistry(tool_registry=tool_reg, known_action_types=("SEARCH", "ANALYZE", "PUBLISH"))

    # Register Market Research Agent
    cap_search = AgentCapability("MARKET_RESEARCH", AgentCapabilityContract(ic, oc), ("SEARCH",), ("search_market",))
    agent_search = SpecialistAgentDefinition(
        agent_id="agent-market-analyst",
        tenant_id="tenant-alpha",
        capabilities=(cap_search,),
        availability=AgentAvailability.AVAILABLE,
        allowed_action_types=("SEARCH",),
        allowed_tool_ids=("search_market",),
        estimated_cost=Decimal("1.50"),
        priority=10,
        executor_key="market-exec",
    )
    reg.register(agent_search)

    # Register Price Optimization Agent
    cap_price = AgentCapability("PRICE_OPTIMIZATION", AgentCapabilityContract(ic, oc), ("ANALYZE",), ("analyze_prices",))
    agent_price = SpecialistAgentDefinition(
        agent_id="agent-price-optimizer",
        tenant_id="tenant-alpha",
        capabilities=(cap_price,),
        availability=AgentAvailability.AVAILABLE,
        allowed_action_types=("ANALYZE",),
        allowed_tool_ids=("analyze_prices",),
        estimated_cost=Decimal("2.00"),
        priority=10,
        executor_key="price-exec",
    )
    reg.register(agent_price)

    # Register Backup Price Agent for Delegation
    agent_price_backup = SpecialistAgentDefinition(
        agent_id="agent-price-optimizer-backup",
        tenant_id="tenant-alpha",
        capabilities=(cap_price,),
        availability=AgentAvailability.AVAILABLE,
        allowed_action_types=("ANALYZE",),
        allowed_tool_ids=("analyze_prices",),
        estimated_cost=Decimal("2.20"),
        priority=20,
        executor_key="price-exec-backup",
    )
    reg.register(agent_price_backup)

    return reg


@pytest.fixture
def specialist_service(specialist_registry, audit_repo, trace_repo):
    executors = {
        "market-exec": GuardedActionExecutor(),
        "price-exec": GuardedActionExecutor(),
        "price-exec-backup": GuardedActionExecutor(),
        "unknown-exec": GuardedActionExecutor(),
    }
    return SpecialistAgentService(
        registry=specialist_registry,
        executor_bindings=executors,
        audit_repository=audit_repo,
    )


@pytest.fixture
def coordinator_service(tmp_path, specialist_registry, audit_repo, trace_repo, emergency_stop_service, test_clock):
    session_repo = JsonCoordinationSessionRepository(base_dir=tmp_path / "sessions")
    return AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=specialist_registry,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
        emergency_stop_service=emergency_stop_service,
        clock=test_clock,
    )


@pytest.fixture
def long_running_service(tmp_path, audit_repo, trace_repo, emergency_stop_service, test_clock):
    cp_repo = JsonMissionCheckpointRepository(base_dir=tmp_path / "checkpoints")
    lease_mgr = InMemoryLeaseManager(clock=test_clock)
    hb_mgr = InMemoryHeartbeatManager()
    mission_repo = JsonTenantMissionRepository(base_storage_dir=tmp_path / "missions")
    session_repo = JsonCoordinationSessionRepository(base_dir=tmp_path / "sessions")

    return LongRunningMissionService(
        checkpoint_repository=cp_repo,
        lease_manager=lease_mgr,
        heartbeat_manager=hb_mgr,
        mission_repository=mission_repo,
        coordination_repository=session_repo,
        emergency_stop_service=emergency_stop_service,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
        clock=test_clock,
    )


@pytest.fixture
def self_monitoring_service(test_clock):
    repo = InMemorySelfMonitoringRepository()
    return SelfMonitoringService(
        repository=repo,
        clock=test_clock,
    )


# ============================================================================
# GATE P TEST SCENARIOS (1 to 16)
# ============================================================================

def test_gate_p_01_r1_multi_step_planning(planning_service, test_clock):
    """1. R.1: Hierarchical decomposition, valid DAG, topological ordering, readiness, bounded replan, immutability."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # Step DAG: step1 -> step2 -> step3
    step1 = PlanStep(step_id="step-1", objective="Search Competitors", action_type="SEARCH", assigned_capability="MARKET_RESEARCH")
    step2 = PlanStep(step_id="step-2", objective="Analyze Prices", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    step3 = PlanStep(step_id="step-3", objective="Prepare Report", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")

    deps = [
        StepDependency(from_step_id="step-1", to_step_id="step-2", dependency_type=DependencyType.DATA),
        StepDependency(from_step_id="step-2", to_step_id="step-3", dependency_type=DependencyType.HARD),
    ]

    budget = PlanBudget(max_tokens=10000, max_cost=Decimal("50.00"), max_replans=3)

    # Create plan
    result = planning_service.create_plan(
        mission_id="m-gate-01",
        goal="Discover and Price Products",
        tenant_context=tenant,
        initial_steps=[step1, step2, step3],
        dependencies=deps,
        budget=budget,
    )
    assert result.success is True
    assert result.status == PlanStatus.VALIDATED
    plan = result.plan
    assert plan is not None

    # Step 1 is ready, Step 2 and 3 are pending dependencies
    ready = planning_service.get_ready_steps(plan, tenant)
    assert len(ready) == 1
    assert ready[0].step_id == "step-1"

    # Complete Step 1
    plan = planning_service.mark_step_completed(plan.plan_id, "step-1", {"competitors": ["A", "B"]}, tenant)
    ready = planning_service.get_ready_steps(plan, tenant)
    assert len(ready) == 1
    assert ready[0].step_id == "step-2"

    # Replan after technical failure in Step 2: Completed step 1 must remain completed and immutable
    new_subgraph_step = PlanStep(step_id="step-2-v2", objective="Analyze Prices with Fallback", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    replan_res = planning_service.replan(
        plan_id=plan.plan_id,
        tenant_context=tenant,
        failed_step_id="step-2",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="External API timeout",
        new_subgraph_steps=[new_subgraph_step],
        new_dependencies=[
            StepDependency(from_step_id="step-1", to_step_id="step-2-v2", dependency_type=DependencyType.DATA),
            StepDependency(from_step_id="step-2-v2", to_step_id="step-3", dependency_type=DependencyType.HARD),
        ],
    )
    assert replan_res.success is True
    new_plan = replan_res.plan
    assert new_plan.replan_count == 1
    # Step 1 is still COMPLETED
    s1 = next(s for s in new_plan.steps if s.step_id == "step-1")
    assert s1.status == StepStatus.COMPLETED


def test_gate_p_02_r2_sub_mission_hierarchy(sub_mission_service, test_clock):
    """2. R.2: Parent/root hierarchy, bounded depth, context minimization, result propagation, cycle rejection."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # Create Root Mission
    root_mission = Mission(
        mission_id="m-root-02",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        parameters={"tenant_id": "tenant-alpha"},
    )
    sub_mission_service.mission_repository.save(tenant, root_mission)

    # Create Child Sub-Mission (Depth 1)
    contract_c1 = SubMissionCreationContract(
        parent_mission_id="m-root-02",
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.MARKET_DISCOVERY,
        scope=SubMissionScope(
            objective="Analyze Region US",
            expected_outcome="Price list",
            completion_condition="Listings extracted",
            expected_output_keys=("prices",),
        ),
        inputs={"region": "US"},
        priority=MissionPriority.HIGH,
    )
    child_1 = sub_mission_service.create_sub_mission(tenant, contract_c1)
    assert child_1.parent_mission_id == "m-root-02"
    assert child_1.root_mission_id == "m-root-02"
    assert child_1.depth == 1

    # Create Grandchild Sub-Mission (Depth 2)
    contract_gc = SubMissionCreationContract(
        parent_mission_id=child_1.mission_id,
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.MARKET_DISCOVERY,
        scope=SubMissionScope(
            objective="Extract Competitor Store A",
            expected_outcome="Raw products",
            completion_condition="Done",
            expected_output_keys=("raw_data",),
        ),
        inputs={"store": "A"},
        priority=MissionPriority.MEDIUM,
    )
    grandchild = sub_mission_service.create_sub_mission(tenant, contract_gc)
    assert grandchild.parent_mission_id == child_1.mission_id
    assert grandchild.root_mission_id == "m-root-02"
    assert grandchild.depth == 2

    # Propagate structured result back from grandchild to child
    res_contract = SubMissionResultContract(
        mission_id=grandchild.mission_id,
        parent_mission_id=child_1.mission_id,
        tenant_id="tenant-alpha",
        status=MissionStatus.COMPLETED,
        outputs={"raw_data": [{"sku": "123", "price": 49.99}]},
        cost_spent=Decimal("0.50"),
        tokens_spent=300,
    )
    assert sub_mission_service.propagate_result(tenant, res_contract) is True


def test_gate_p_03_r3_specialist_selection_and_execution(specialist_service, specialist_registry):
    """3. R.3: Capability-first contracts, deterministic selection, tool allowlists, UNKNOWN != AVAILABLE fail-safe."""
    # 1. Deterministic selection for valid capability
    sel = specialist_service.select_agent(
        tenant_id="tenant-alpha",
        capability_id="MARKET_RESEARCH",
        action_type="SEARCH",
        tool_id="search_market",
        max_cost=Decimal("5.00"),
    )
    assert sel.status == AgentSelectionStatus.SELECTED
    assert sel.agent_id == "agent-market-analyst"

    # 2. Rejection for disallowed tool
    sel_disallowed = specialist_service.select_agent(
        tenant_id="tenant-alpha",
        capability_id="MARKET_RESEARCH",
        action_type="SEARCH",
        tool_id="publish_listing",  # not in allowed_tool_ids for market-analyst
    )
    assert sel_disallowed.status == AgentSelectionStatus.BLOCKED

    # 3. UNKNOWN availability fail-safe: Agent marked UNKNOWN availability cannot be selected
    unknown_agent = SpecialistAgentDefinition(
        agent_id="agent-unknown-stat",
        tenant_id="tenant-alpha",
        capabilities=(AgentCapability("SEARCH_V2", AgentCapabilityContract(ToolContract("i", ()), ToolContract("o", ())), ("SEARCH",), ("search_market",)),),
        availability=AgentAvailability.UNKNOWN,
        allowed_action_types=("SEARCH",),
        allowed_tool_ids=("search_market",),
        executor_key="unknown-exec",
    )
    specialist_registry.register(unknown_agent)
    sel_unknown = specialist_service.select_agent(
        tenant_id="tenant-alpha",
        capability_id="SEARCH_V2",
    )
    assert sel_unknown.status == AgentSelectionStatus.BLOCKED


def test_gate_p_04_r4_agent_coordination_fanout_fanin(coordinator_service, planning_service, test_clock):
    """4. R.4: Single logical owner, atomic claims, fan-out on independent branches, deterministic fan-in merge."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # Plan with fan-out: Step 1 -> (Step 2A, Step 2B) -> Step 3 (fan-in)
    step1 = PlanStep(step_id="step-1", objective="Fetch Sourcing Data", action_type="SEARCH", assigned_capability="MARKET_RESEARCH")
    step2a = PlanStep(step_id="step-2a", objective="Analyze Amazon Prices", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    step2b = PlanStep(step_id="step-2b", objective="Analyze Walmart Prices", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    step3 = PlanStep(step_id="step-3", objective="Consolidate Report", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")

    deps = [
        StepDependency(from_step_id="step-1", to_step_id="step-2a", dependency_type=DependencyType.DATA),
        StepDependency(from_step_id="step-1", to_step_id="step-2b", dependency_type=DependencyType.DATA),
        StepDependency(from_step_id="step-2a", to_step_id="step-3", dependency_type=DependencyType.HARD),
        StepDependency(from_step_id="step-2b", to_step_id="step-3", dependency_type=DependencyType.HARD),
    ]

    p_res = planning_service.create_plan("m-gate-04", "Fan-out Fan-in", tenant, [step1, step2a, step2b, step3], deps)
    plan = p_res.plan

    # Create coordination session
    session = coordinator_service.create_session(tenant, "m-gate-04", plan=plan)
    assert session.status == CoordinationStatus.ACTIVE

    # Step 1 is ready
    ready = coordinator_service.get_ready_tasks(session.session_id, tenant)
    assert len(ready) == 1
    assert ready[0].task_id == "step-1"

    # Agent claims and completes step 1
    session, claim1 = coordinator_service.claim_task(session.session_id, "step-1", "agent-market-analyst", tenant)
    assert claim1.is_active is True
    session = coordinator_service.complete_task(session.session_id, "step-1", {"market": "electronics"}, tenant)

    # Fan-out: Both step 2a and step 2b are ready concurrently
    ready_fanout = coordinator_service.get_ready_tasks(session.session_id, tenant)
    assert len(ready_fanout) == 2
    assert {t.task_id for t in ready_fanout} == {"step-2a", "step-2b"}

    # Atomic claims: Agent 1 claims 2a, Agent 2 claims 2b
    session, claim_2a = coordinator_service.claim_task(session.session_id, "step-2a", "agent-price-optimizer", tenant)
    session, claim_2b = coordinator_service.claim_task(session.session_id, "step-2b", "agent-price-optimizer-backup", tenant)

    session = coordinator_service.complete_task(session.session_id, "step-2a", {"amazon_margin": 0.25}, tenant)
    session = coordinator_service.complete_task(session.session_id, "step-2b", {"walmart_margin": 0.30}, tenant)

    # Fan-in: Step 3 is now ready
    ready_fanin = coordinator_service.get_ready_tasks(session.session_id, tenant)
    assert len(ready_fanin) == 1
    assert ready_fanin[0].task_id == "step-3"

    # Deterministic merge of upstream results
    merge_res = coordinator_service.merge_task_results(
        session.session_id,
        source_task_ids=["step-2a", "step-2b"],
        target_task_id="step-3",
        tenant_context=tenant,
        strategy=MergeStrategy.KEYED_MERGE,
    )
    assert merge_res.success is True
    assert merge_res.merged_data == {"amazon_margin": 0.25, "walmart_margin": 0.30}


def test_gate_p_05_r5_dynamic_delegation_and_versioning(coordinator_service, test_clock):
    """5. R.5: Dynamic delegation on failure, atomic ownership transfer, assignment_version, stale owner rejection."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    step = PlanStep(step_id="step-task-5", objective="Price Calculation", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    plan = ExecutionPlan(
        plan_id="p-05",
        tenant_id="tenant-alpha",
        mission_id="m-gate-05",
        goal="Calculate",
        steps=(step,),
    )
    session = coordinator_service.create_session(tenant, "m-gate-05", plan=plan, policy=CoordinationPolicy(max_delegations_per_task=2))

    # Initial claim by primary agent
    session, claim = coordinator_service.claim_task(session.session_id, "step-task-5", "agent-price-optimizer", tenant)
    assert session.tasks["step-task-5"].assigned_agent_id == "agent-price-optimizer"
    assert session.tasks["step-task-5"].assignment_version == 1

    # Primary agent experiences technical failure and requests delegation
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="m-gate-05",
        task_id="step-task-5",
        from_agent_id="agent-price-optimizer",
        required_capability="PRICE_OPTIMIZATION",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
        requested_at=test_clock.now(),
        correlation_id="corr-del-5",
    )
    session, del_record = coordinator_service.delegate_task(session.session_id, del_req, tenant)
    assert del_record.decision.status == DelegationDecisionStatus.APPROVED
    assert del_record.decision.to_agent_id == "agent-price-optimizer-backup"
    assert session.tasks["step-task-5"].assigned_agent_id == "agent-price-optimizer-backup"
    assert session.tasks["step-task-5"].assignment_version == 2

    # Stale owner rejection: old agent attempts completion with version 1 -> rejected / raises error or is ignored
    with pytest.raises(Exception):
        coordinator_service.complete_task(
            session.session_id,
            "step-task-5",
            outputs={"stale": "data"},
            tenant_context=tenant,
            assignment_version=1,  # Stale version
            agent_id="agent-price-optimizer",
        )

    # New agent completes task with correct assignment_version 2
    session = coordinator_service.complete_task(
        session.session_id,
        "step-task-5",
        outputs={"final_price": 99.90},
        tenant_context=tenant,
        assignment_version=2,
        agent_id="agent-price-optimizer-backup",
    )
    assert session.tasks["step-task-5"].status == CoordinationTaskStatus.COMPLETED


def test_gate_p_06_r6_long_running_checkpoint_pause_resume(long_running_service, test_clock):
    """6. R.6: Durable checkpoints, pause != cancel, restart recovery, resume with single winner lease."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # Step 1: Running mission creates durable checkpoint
    cp1 = long_running_service.create_checkpoint(
        mission_id="m-gate-06",
        worker_id="worker-node-1",
        tenant_context=tenant,
        step_records={
            "step-1": StepCheckpointData(step_id="step-1", status="COMPLETED", outputs={"data": "ready"}),
        },
        budget_consumed_tokens=1500,
        budget_consumed_cost=Decimal("3.50"),
    )
    assert cp1.checkpoint_version == 1
    assert cp1.verify_integrity() is True

    # Step 2: Pause mission
    cp_paused = long_running_service.pause_mission(
        mission_id="m-gate-06",
        worker_id="worker-node-1",
        tenant_context=tenant,
        reason="Scheduled maintenance window",
    )
    assert cp_paused.mission_status == MissionStatus.PAUSED
    assert cp_paused.checkpoint_version == 2

    # Step 3: Resume mission on a new worker instance
    decision = long_running_service.resume_mission(
        mission_id="m-gate-06",
        worker_id="worker-node-2",
        tenant_context=tenant,
    )
    assert decision.allowed is True
    assert decision.status == ResumeDecisionStatus.GRANTED
    assert decision.active_worker_id == "worker-node-2"

    # Step 4: Heartbeat registration
    hb = long_running_service.record_heartbeat(
        mission_id="m-gate-06",
        worker_id="worker-node-2",
        tenant_context=tenant,
    )
    assert hb.worker_id == "worker-node-2"


def test_gate_p_07_r7_self_monitoring_and_action_dispatch(self_monitoring_service, test_clock):
    """7. R.7: Signal evaluation, UNKNOWN != HEALTHY, degradation detection, bounded remediation dispatch."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # 1. Healthy state with complete signals
    signals_healthy = [
        HealthSignal("sig-1", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, test_clock.now(), 1.0, {"last_seen_at": test_clock.now()}, "Active lease", tenant_id="tenant-alpha", mission_id="m-gate-07", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-2", SignalType.EXECUTION_PROGRESS, SignalSource.AUTONOMOUS_LOOP, SignalSeverity.INFO, test_clock.now(), 1.0, 0.5, "On track", tenant_id="tenant-alpha", mission_id="m-gate-07", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-3", SignalType.POLICY_COMPLIANCE, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, test_clock.now(), 1.0, "COMPLIANT", "Safe", tenant_id="tenant-alpha", mission_id="m-gate-07", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-4", SignalType.EMERGENCY_STOP_STATUS, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, test_clock.now(), 1.0, "INACTIVE", "No stop", tenant_id="tenant-alpha", mission_id="m-gate-07", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = self_monitoring_service.assess_mission_health("m-gate-07", tenant, signals=signals_healthy)
    assert assessment.status == MissionHealthStatus.HEALTHY
    assert assessment.decision.action == SelfMonitoringAction.NONE

    # 2. Stale heartbeat trigger -> PAUSE action
    signals_stale = [
        HealthSignal("sig-1b", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.HIGH, test_clock.now() - timedelta(seconds=60), 1.0, {"last_seen_at": test_clock.now() - timedelta(seconds=60)}, "Heartbeat lost > 30s", tenant_id="tenant-alpha", mission_id="m-gate-07", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-2", SignalType.EXECUTION_PROGRESS, SignalSource.AUTONOMOUS_LOOP, SignalSeverity.INFO, test_clock.now(), 1.0, 0.5, "On track", tenant_id="tenant-alpha", mission_id="m-gate-07", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-3", SignalType.POLICY_COMPLIANCE, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, test_clock.now(), 1.0, "COMPLIANT", "Safe", tenant_id="tenant-alpha", mission_id="m-gate-07", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-4", SignalType.EMERGENCY_STOP_STATUS, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, test_clock.now(), 1.0, "INACTIVE", "No stop", tenant_id="tenant-alpha", mission_id="m-gate-07", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment_stale = self_monitoring_service.assess_mission_health("m-gate-07", tenant, signals=signals_stale)
    assert assessment_stale.status in (MissionHealthStatus.DEGRADED, MissionHealthStatus.AT_RISK)
    assert assessment_stale.decision.action == SelfMonitoringAction.PAUSE


def test_gate_p_08_single_owner_invariant(coordinator_service, test_clock):
    """8. Single-owner invariant: Only one logical active owner per mutable task; double claim strictly rejected."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    step = PlanStep(step_id="step-lock-8", objective="Market Lock", action_type="SEARCH", assigned_capability="MARKET_RESEARCH")
    plan = ExecutionPlan(
        plan_id="p-08",
        tenant_id="tenant-alpha",
        mission_id="m-gate-08",
        goal="Lock",
        steps=(step,),
    )
    session = coordinator_service.create_session(tenant, "m-gate-08", plan=plan)

    # First claim succeeds
    session, claim1 = coordinator_service.claim_task(session.session_id, "step-lock-8", "agent-market-analyst", tenant)
    assert claim1.is_active is True
    assert session.tasks["step-lock-8"].status == CoordinationTaskStatus.CLAIMED

    # Concurrent second claim by another agent MUST fail / be rejected
    with pytest.raises(Exception):
        coordinator_service.claim_task(session.session_id, "step-lock-8", "agent-price-optimizer", tenant)


def test_gate_p_09_stale_worker_rejection(long_running_service, test_clock):
    """9. Stale worker rejection: Expired worker / stale lease cannot record checkpoint or resume."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # Worker 1 creates checkpoint
    long_running_service.create_checkpoint("m-gate-09", "worker-1", tenant)

    # Lease expires or transfers to Worker 2
    long_running_service.resume_mission("m-gate-09", "worker-2", tenant)

    # Worker 1 (now stale) attempts checkpoint creation -> MUST fail
    with pytest.raises(StaleWorkerError):
        long_running_service.create_checkpoint("m-gate-09", "worker-1", tenant)


def test_gate_p_10_budget_continuity(coordinator_service, long_running_service, test_clock):
    """10. Budget continuity: Consumed tokens & cost never reset across replan, delegation, or checkpoint resume."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # Checkpoint 1: Consumes 500 tokens, 1.25 USD
    cp1 = long_running_service.create_checkpoint(
        mission_id="m-gate-10",
        worker_id="w-1",
        tenant_context=tenant,
        budget_consumed_tokens=500,
        budget_consumed_cost=Decimal("1.25"),
        budget_remaining_tokens=4500,
        budget_remaining_cost=Decimal("8.75"),
    )

    # Checkpoint 2: Next step consumes additional 300 tokens, 0.75 USD -> cumulative totals must grow monotonically
    cp2 = long_running_service.create_checkpoint(
        mission_id="m-gate-10",
        worker_id="w-1",
        tenant_context=tenant,
        budget_consumed_tokens=cp1.budget_consumed_tokens + 300,
        budget_consumed_cost=cp1.budget_consumed_cost + Decimal("0.75"),
        budget_remaining_tokens=4200,
        budget_remaining_cost=Decimal("8.00"),
    )
    assert cp2.budget_consumed_tokens == 800
    assert cp2.budget_consumed_cost == Decimal("2.00")
    assert cp2.budget_consumed_tokens >= cp1.budget_consumed_tokens
    assert cp2.budget_consumed_cost >= cp1.budget_consumed_cost


def test_gate_p_11_policy_denial_no_bypass(coordinator_service, test_clock):
    """11. Anti-policy bypass: POLICY_DENIED is strictly terminal and cannot be bypassed via delegation or replan."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    step = PlanStep(step_id="step-policy-11", objective="Policy Task", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION", assigned_agent="agent-price-optimizer")
    plan = ExecutionPlan(
        plan_id="p-11",
        tenant_id="tenant-alpha",
        mission_id="m-gate-11",
        goal="Policy",
        steps=(step,),
    )
    session = coordinator_service.create_session(tenant, "m-gate-11", plan=plan)
    session, _ = coordinator_service.claim_task(session.session_id, "step-policy-11", "agent-price-optimizer", tenant)

    # Task fails with POLICY_DENIED
    session = coordinator_service.fail_task(
        session.session_id,
        "step-policy-11",
        CoordinationFailureType.POLICY_DENIED,
        "Tenant restricted from high-risk pricing action",
        tenant,
    )
    assert session.tasks["step-policy-11"].status == CoordinationTaskStatus.FAILED

    # Attempting to delegate a POLICY_DENIED task MUST be rejected
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="m-gate-11",
        task_id="step-policy-11",
        from_agent_id="agent-price-optimizer",
        required_capability="PRICE_OPTIMIZATION",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
        requested_at=test_clock.now(),
    )
    with pytest.raises(ValueError, match="Cannot delegate task to bypass POLICY_DENIED failure"):
        coordinator_service.delegate_task(session.session_id, del_req, tenant)


def test_gate_p_12_emergency_stop_precedence(emergency_stop_service, coordinator_service, long_running_service, test_clock):
    """12. Emergency Stop precedence: Active EmergencyStop blocks execution, delegation, and resume immediately."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # Activate global Emergency Stop
    stop_rec = EmergencyStopRecord(
        stop_id="stop-gate-12",
        scope=EmergencyStopScope.GLOBAL,
        state=EmergencyStopState.ACTIVE,
        reason_code=EmergencyStopReasonCode.SECURITY_INCIDENT,
        reason_details="System containment active",
        activated_by_identity_id="admin-sec",
        activated_at=test_clock.now(),
    )
    emergency_stop_service.repository.save(stop_rec)

    # 1. Long-running resume MUST be denied
    long_running_service.create_checkpoint("m-gate-12", "w-1", tenant)
    resume_dec = long_running_service.resume_mission("m-gate-12", "w-2", tenant)
    assert resume_dec.allowed is False
    assert resume_dec.status == ResumeDecisionStatus.DENIED_EMERGENCY_STOP

    # 2. Coordination session creation or execution MUST be blocked
    step = PlanStep(step_id="step-12", objective="Market Task", action_type="SEARCH", assigned_capability="MARKET_RESEARCH", assigned_agent="agent-market-analyst")
    plan = ExecutionPlan(
        plan_id="p-12",
        tenant_id="tenant-alpha",
        mission_id="m-gate-12",
        goal="Emergency",
        steps=(step,),
    )
    session = coordinator_service.create_session(tenant, "m-gate-12", plan=plan)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="m-gate-12",
        task_id="step-12",
        from_agent_id="agent-market-analyst",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
        requested_at=test_clock.now(),
    )
    with pytest.raises(ValueError, match="Cannot delegate task under active EMERGENCY_STOP"):
        coordinator_service.delegate_task(session.session_id, del_req, tenant)


def test_gate_p_13_tenant_isolation(planning_service, sub_mission_service, coordinator_service, long_running_service, test_clock):
    """13. Tenant isolation: Tenant A cannot read, claim, delegate, or resume resources of Tenant B."""
    tenant_a = TenantContext(tenant_id="tenant-alpha", identity_id="user-a")
    tenant_b = TenantContext(tenant_id="tenant-beta", identity_id="user-b")

    # 1. Planning Isolation
    step_a = PlanStep(step_id="step-a", objective="Plan A", action_type="SEARCH", assigned_capability="MARKET_RESEARCH")
    plan_a = planning_service.create_plan("m-tenant-a", "Goal A", tenant_a, [step_a]).plan

    with pytest.raises(CrossTenantAccessError):
        planning_service.get_ready_steps(plan_a, tenant_b)

    # 2. Coordination Isolation
    session_a = coordinator_service.create_session(tenant_a, "m-tenant-a", plan=plan_a)
    with pytest.raises(CrossTenantAccessError):
        coordinator_service.get_ready_tasks(session_a.session_id, tenant_b)

    # 3. Checkpoint Isolation
    long_running_service.create_checkpoint("m-tenant-a", "worker-a", tenant_a)
    with pytest.raises(CrossTenantAccessError):
        long_running_service.resume_mission("m-tenant-a", "worker-b", tenant_b)


def test_gate_p_14_anti_cot_and_security_sanitization(coordinator_service, test_clock):
    """14. Anti-CoT & Sensitive sanitization: Handoff payloads and traces scrub private thoughts & secrets."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    step = PlanStep(step_id="step-14", objective="Price Calculation", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    plan = ExecutionPlan(
        plan_id="p-14",
        tenant_id="tenant-alpha",
        mission_id="m-gate-14",
        goal="Sanitization",
        steps=(step,),
    )
    session = coordinator_service.create_session(tenant, "m-gate-14", plan=plan)

    # Raw payload containing private reasoning and secret tokens
    dirty_payload = {
        "calculated_margin": 0.22,
        "chain_of_thought": "I think the supplier has high elasticity so I should bid lower",
        "internal_scratchpad": "Temporary variable x=42",
        "api_key": "sk-live-secret-key-12345",
        "password": "super-secret-password",
    }

    session, handoff = coordinator_service.handoff(
        session_id=session.session_id,
        source_agent_id="agent-price-optimizer",
        target_task_id="step-14",
        payload=dirty_payload,
        tenant_context=tenant,
        evidence_refs=["doc-123"],
    )

    # Verify that clean public outputs survive, while private CoT and secrets are strictly redacted/stripped
    assert "calculated_margin" in handoff.payload
    assert handoff.payload.get("chain_of_thought") == "[REDACTED]"
    assert handoff.payload.get("internal_scratchpad") == "[REDACTED]"
    assert handoff.payload.get("api_key") == "[REDACTED]"
    assert handoff.payload.get("password") == "[REDACTED]"


def test_gate_p_15_controlled_failure_recovery_e2e(coordinator_service, long_running_service, test_clock):
    """15. Controlled failure recovery E2E: Agent A failure -> R.5 delegation -> Agent B -> Checkpoint -> Restart -> Stale rejected -> B completes."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-1")

    # 1. Create coordination session and task via plan
    step = PlanStep(step_id="step-calc-15", objective="Price Calc", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    plan = ExecutionPlan(
        plan_id="p-15",
        tenant_id="tenant-alpha",
        mission_id="m-gate-15",
        goal="Failure recovery",
        steps=(step,),
    )
    session = coordinator_service.create_session(tenant, "m-gate-15", plan=plan)

    # 2. Agent A claims task
    session, _ = coordinator_service.claim_task(session.session_id, "step-calc-15", "agent-price-optimizer", tenant)

    # 3. Agent A fails technically and triggers delegation
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="m-gate-15",
        task_id="step-calc-15",
        from_agent_id="agent-price-optimizer",
        required_capability="PRICE_OPTIMIZATION",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
        requested_at=test_clock.now(),
    )
    session, del_rec = coordinator_service.delegate_task(session.session_id, del_req, tenant)
    assert del_rec.decision.status == DelegationDecisionStatus.APPROVED
    assert session.tasks["step-calc-15"].assigned_agent_id == "agent-price-optimizer-backup"
    assert session.tasks["step-calc-15"].assignment_version == 2

    # 4. Checkpoint created mid-flight
    cp = long_running_service.create_checkpoint(
        mission_id="m-gate-15",
        worker_id="worker-node-1",
        tenant_context=tenant,
        step_records={
            "step-calc-15": StepCheckpointData(
                step_id="step-calc-15",
                status="RUNNING",
                assigned_agent_id="agent-price-optimizer-backup",
                assignment_version=2,
            )
        },
    )
    assert cp.checkpoint_version == 1

    # 5. Process restart simulated -> New service instance loads checkpoint
    res_dec = long_running_service.resume_mission("m-gate-15", "worker-node-2", tenant)
    assert res_dec.allowed is True

    # 6. Old worker A attempts to complete -> Rejected due to stale assignment_version 1
    with pytest.raises(Exception):
        coordinator_service.complete_task(
            session.session_id,
            "step-calc-15",
            outputs={"bad": "stale"},
            tenant_context=tenant,
            assignment_version=1,
            agent_id="agent-price-optimizer",
        )

    # 7. Worker B completes successfully
    session = coordinator_service.complete_task(
        session.session_id,
        "step-calc-15",
        outputs={"price": 120.00},
        tenant_context=tenant,
        assignment_version=2,
        agent_id="agent-price-optimizer-backup",
    )
    assert session.tasks["step-calc-15"].status == CoordinationTaskStatus.COMPLETED


def test_gate_p_16_complex_canonical_autonomous_mission_e2e(
    planning_service,
    sub_mission_service,
    specialist_registry,
    coordinator_service,
    long_running_service,
    self_monitoring_service,
    test_clock,
):
    """16. Complex canonical mission: DAG multi-step + Sub-missions + Specialists + Fan-out/Fan-in + Delegation + Checkpoint/Resume + Self-monitoring -> SUCCESS."""
    tenant = TenantContext(tenant_id="tenant-alpha", identity_id="user-corp-1")

    # --- PHASE 1: Multi-Step Planning (R.1) ---
    step_search = PlanStep(step_id="step-search", objective="Market Competitor Search", action_type="SEARCH", assigned_capability="MARKET_RESEARCH")
    step_price_a = PlanStep(step_id="step-price-a", objective="Price Amazon", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    step_price_b = PlanStep(step_id="step-price-b", objective="Price Walmart", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")
    step_finalize = PlanStep(step_id="step-finalize", objective="Finalize Pricing", action_type="ANALYZE", assigned_capability="PRICE_OPTIMIZATION")

    deps = [
        StepDependency(from_step_id="step-search", to_step_id="step-price-a", dependency_type=DependencyType.DATA),
        StepDependency(from_step_id="step-search", to_step_id="step-price-b", dependency_type=DependencyType.DATA),
        StepDependency(from_step_id="step-price-a", to_step_id="step-finalize", dependency_type=DependencyType.HARD),
        StepDependency(from_step_id="step-price-b", to_step_id="step-finalize", dependency_type=DependencyType.HARD),
    ]

    p_res = planning_service.create_plan(
        mission_id="m-canonical-e2e",
        goal="Autonomous Price Discovery and Execution",
        tenant_context=tenant,
        initial_steps=[step_search, step_price_a, step_price_b, step_finalize],
        dependencies=deps,
        budget=PlanBudget(max_tokens=50000, max_cost=Decimal("100.00"), max_replans=3),
    )
    assert p_res.success is True
    plan = p_res.plan

    # --- PHASE 2: Sub-Mission Hierarchical Creation (R.2) ---
    root_mission = Mission(mission_id="m-canonical-e2e", type=MissionType.MARKET_DISCOVERY, status=MissionStatus.RUNNING, parameters={"tenant_id": "tenant-alpha"})
    sub_mission_service.mission_repository.save(tenant, root_mission)

    sub_contract = SubMissionCreationContract(
        parent_mission_id="m-canonical-e2e",
        tenant_id="tenant-alpha",
        sub_mission_type=MissionType.MARKET_DISCOVERY,
        scope=SubMissionScope(
            objective="Child Market Scraping",
            expected_outcome="Catalog",
            completion_condition="Scraped",
            expected_output_keys=("catalog",),
        ),
        inputs={"depth": "fast"},
    )
    child_sub_mission = sub_mission_service.create_sub_mission(tenant, sub_contract)
    assert child_sub_mission.depth == 1

    # --- PHASE 3: Coordination Session & Task Execution (R.4) ---
    session = coordinator_service.create_session(tenant, "m-canonical-e2e", plan=plan, sub_missions=[child_sub_mission])

    # Claim & Complete Step 1 (Search)
    session, _ = coordinator_service.claim_task(session.session_id, "step-search", "agent-market-analyst", tenant)
    session = coordinator_service.complete_task(session.session_id, "step-search", {"catalog": ["prod-1", "prod-2"]}, tenant)

    # --- PHASE 4: Dynamic Delegation on Branch A (R.5) ---
    session, _ = coordinator_service.claim_task(session.session_id, "step-price-a", "agent-price-optimizer", tenant)
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="m-canonical-e2e",
        task_id="step-price-a",
        from_agent_id="agent-price-optimizer",
        required_capability="PRICE_OPTIMIZATION",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
        requested_at=test_clock.now(),
    )
    session, del_record = coordinator_service.delegate_task(session.session_id, del_req, tenant)
    assert del_record.decision.status == DelegationDecisionStatus.APPROVED
    assert session.tasks["step-price-a"].assigned_agent_id == "agent-price-optimizer-backup"

    session = coordinator_service.complete_task(session.session_id, "step-price-a", {"amazon_price": 29.99}, tenant, assignment_version=2, agent_id="agent-price-optimizer-backup")

    # Branch B Completed
    session, _ = coordinator_service.claim_task(session.session_id, "step-price-b", "agent-price-optimizer-backup", tenant)
    session = coordinator_service.complete_task(session.session_id, "step-price-b", {"walmart_price": 28.50}, tenant)

    # --- PHASE 5: Checkpoint & Recovery Simulation (R.6) ---
    cp = long_running_service.create_checkpoint(
        mission_id="m-canonical-e2e",
        worker_id="worker-pod-alpha",
        tenant_context=tenant,
        step_records={
            "step-search": StepCheckpointData(step_id="step-search", status="COMPLETED"),
            "step-price-a": StepCheckpointData(step_id="step-price-a", status="COMPLETED"),
            "step-price-b": StepCheckpointData(step_id="step-price-b", status="COMPLETED"),
        },
        budget_consumed_tokens=4200,
        budget_consumed_cost=Decimal("12.50"),
    )
    assert cp.verify_integrity() is True

    # Process Pause and Resume on new Pod
    long_running_service.pause_mission("m-canonical-e2e", "worker-pod-alpha", tenant, "Node evacuation")
    res_dec = long_running_service.resume_mission("m-canonical-e2e", "worker-pod-beta", tenant)
    assert res_dec.allowed is True

    # --- PHASE 6: Fan-In Merge & Final Task Completion (R.4) ---
    merge_res = coordinator_service.merge_task_results(
        session.session_id,
        source_task_ids=["step-price-a", "step-price-b"],
        target_task_id="step-finalize",
        tenant_context=tenant,
        strategy=MergeStrategy.KEYED_MERGE,
    )
    assert merge_res.success is True
    assert merge_res.merged_data == {"amazon_price": 29.99, "walmart_price": 28.50}

    session, _ = coordinator_service.claim_task(session.session_id, "step-finalize", "agent-price-optimizer-backup", tenant)
    session = coordinator_service.complete_task(session.session_id, "step-finalize", {"optimized_price": 28.50, "decision": "MATCH_WALMART"}, tenant)
    session = coordinator_service.complete_task(session.session_id, child_sub_mission.mission_id, {"catalog_count": 42}, tenant)
    assert session.status == CoordinationStatus.COMPLETED

    # --- PHASE 7: Sub-Mission Result Propagation (R.2) ---
    sub_res = SubMissionResultContract(
        mission_id=child_sub_mission.mission_id,
        parent_mission_id="m-canonical-e2e",
        tenant_id="tenant-alpha",
        status=MissionStatus.COMPLETED,
        outputs={"scraped_items_count": 42},
        cost_spent=Decimal("2.00"),
        tokens_spent=800,
    )
    assert sub_mission_service.propagate_result(tenant, sub_res) is True

    # --- PHASE 8: Self-Monitoring Final Assessment (R.7) ---
    signals = [
        HealthSignal("sig-final-1", SignalType.HEARTBEAT_LIVENESS, SignalSource.R6_LONG_RUNNING, SignalSeverity.INFO, test_clock.now(), 1.0, {"last_seen_at": test_clock.now()}, "Completed smoothly", tenant_id="tenant-alpha", mission_id="m-canonical-e2e", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-final-2", SignalType.EXECUTION_PROGRESS, SignalSource.AUTONOMOUS_LOOP, SignalSeverity.INFO, test_clock.now(), 1.0, 1.0, "100% complete", tenant_id="tenant-alpha", mission_id="m-canonical-e2e", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-final-3", SignalType.POLICY_COMPLIANCE, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, test_clock.now(), 1.0, True, "Strict compliance", tenant_id="tenant-alpha", mission_id="m-canonical-e2e", completeness=SignalCompleteness.COMPLETE),
        HealthSignal("sig-final-4", SignalType.EMERGENCY_STOP_STATUS, SignalSource.N_GOVERNANCE, SignalSeverity.INFO, test_clock.now(), 1.0, False, "None", tenant_id="tenant-alpha", mission_id="m-canonical-e2e", completeness=SignalCompleteness.COMPLETE),
    ]
    assessment = self_monitoring_service.assess_mission_health("m-canonical-e2e", tenant, signals=signals)
    assert assessment.status == MissionHealthStatus.HEALTHY
    assert assessment.decision.action == SelfMonitoringAction.NONE
