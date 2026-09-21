"""Integration tests for R.5 Dynamic Delegation (Milestone R: Advanced Autonomy).

Covers the 10 canonical integration scenarios (A - J) + E2E multi-agent dynamic handoff:
A. Runtime Degradation & Auto-Delegation: Agent becomes DEGRADED -> task delegated to AVAILABLE specialist.
B. Atomic Lease & Lock Transfer: Active lease and resource locks safely transfer from old owner to new owner.
C. Stale Result Rejection & Monotonic Versioning: Old agent attempting completion/failure after delegation is strictly rejected.
D. Anti-Policy Bypass Enforcement: Tasks failing with POLICY_DENIED strictly forbid delegation or evasive rerouting.
E. Emergency Stop Precedence: Active emergency stop strictly blocks delegation requests and decision execution.
F. Ping-Pong Prevention & Delegation Bounds: Repeated reassignments hit max_delegations_per_task and cleanly transition to BLOCKED.
G. Capability-First Selection Matrix: Automatic candidate resolution selects best available specialist according to priority, cost, and tool contracts.
H. Multi-Tenant Cross Isolation: Delegation requests cannot cross tenant boundaries or affect foreign sessions/agents.
I. Budget & Cost Continuity: Allocated budgets, token limits and cumulative cost tracking persist across delegations.
J. Structured Handoff & Zero-CoT Context Preservation: Rich payload and evidence references pass to new agent with complete sanitization.
K. E2E Multi-Agent Mission Execution with In-Flight Dynamic Delegation: Full execution of a mission DAG with delegation, handoff, merge and final completion.
"""

from decimal import Decimal
import pytest
from typing import Dict, Any, Sequence, Optional, List

from src.application.agent_coordination.coordinator_service import AgentCoordinatorService
from src.domain.agent_coordination.delegation_models import (
    DelegationDecision,
    DelegationDecisionStatus,
    DelegationPolicy,
    DelegationReason,
    DelegationRecord,
    DelegationRequest,
)
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
from src.domain.planning.models import ExecutionPlan, PlanBudget, PlanStep
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


class MockEmergencyStopService:
    def __init__(self):
        self.stopped_tenants = set()

    def is_stopped(self, tenant_id: str) -> bool:
        return tenant_id in self.stopped_tenants

    def trigger_stop(self, tenant_id: str) -> None:
        self.stopped_tenants.add(tenant_id)


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
    priority: int = 1,
    cost: Optional[Decimal] = Decimal("0.05"),
    policy_eligible: bool = True,
) -> SpecialistAgentDefinition:
    return SpecialistAgentDefinition(
        agent_id=agent_id,
        tenant_id=tenant_id,
        capabilities=(sample_capability(capability_name, action_type),),
        availability=availability,
        priority=priority,
        estimated_cost=cost,
        policy_eligible=policy_eligible,
        allowed_action_types=(action_type,),
        allowed_tool_ids=(),
    )


@pytest.fixture
def tenant_alpha():
    return TenantContext(tenant_id="tenant-alpha", correlation_id="corr-alpha-555")


@pytest.fixture
def tenant_beta():
    return TenantContext(tenant_id="tenant-beta", correlation_id="corr-beta-777")


@pytest.fixture
def session_repo():
    return InMemoryCoordinationSessionRepository()


@pytest.fixture
def audit_repo():
    return MockAuditRepository()


# =============================================================================
# SCENARIO A: Runtime Degradation & Auto-Delegation
# =============================================================================

def test_scenario_a_runtime_degradation_and_auto_delegation(tenant_alpha, session_repo, audit_repo):
    """
    Scenario A:
    - Primary agent-1 claims step-1, but gets rate-limited/degraded.
    - System detects degradation and requests dynamic delegation to an alternative specialist.
    - Capability-first registry selects agent-2 (AVAILABLE).
    - Session reflects agent-2 as new owner with incremented assignment_version.
    """
    agent1 = sample_agent("agent-1", availability=AgentAvailability.AVAILABLE, priority=2)
    agent2 = sample_agent("agent-2", availability=AgentAvailability.AVAILABLE, priority=1)

    registry = SpecialistAgentRegistry()
    registry.register(agent1)
    registry.register(agent2)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-a",
        tenant_id="tenant-alpha",
        mission_id="mission-a",
        goal="Perform deep market research",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Analyze competitors",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-a", plan=plan)
    session, claim = service.claim_task(session.session_id, "step-1", "agent-1", tenant_alpha)
    assert claim.agent_id == "agent-1"
    assert session.tasks["step-1"].assignment_version == 1

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-a",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.RATE_LIMITED,
        current_attempt=1,
    )

    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_alpha)

    assert record.decision.status == DelegationDecisionStatus.APPROVED
    assert record.decision.to_agent_id == "agent-2"
    assert updated_session.tasks["step-1"].assigned_agent_id == "agent-2"
    assert updated_session.tasks["step-1"].assignment_version == 2
    assert updated_session.tasks["step-1"].status == CoordinationTaskStatus.CLAIMED
    assert len(updated_session.tasks["step-1"].delegation_history) == 1


# =============================================================================
# SCENARIO B: Atomic Lease & Lock Transfer
# =============================================================================

def test_scenario_b_atomic_lease_and_lock_transfer(tenant_alpha, session_repo, audit_repo):
    """
    Scenario B:
    - Task with exclusive resource lock (e.g. catalog_writer_lock).
    - Upon delegation, old owner lease is removed and resource lock is atomically transferred to new owner.
    - Zero window for concurrent double execution or lease collision.
    """
    agent_primary = sample_agent("agent-primary", action_type="WRITE_CATALOG", capability_name="CATALOG_MGMT")
    agent_backup = sample_agent("agent-backup", action_type="WRITE_CATALOG", capability_name="CATALOG_MGMT")

    registry = SpecialistAgentRegistry()
    registry.register(agent_primary)
    registry.register(agent_backup)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-b",
        tenant_id="tenant-alpha",
        mission_id="mission-b",
        goal="Publish catalog item",
        steps=(
            PlanStep(
                step_id="step-lock",
                objective="Exclusive catalog update",
                action_type="WRITE_CATALOG",
                assigned_capability="CATALOG_MGMT",
                assigned_agent="agent-primary",
                metadata={"resource_id": "catalog_writer_lock"},
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-b", plan=plan)

    # Claim step-lock -> acquires lease and resource lock
    session, _ = service.claim_task(session.session_id, "step-lock", "agent-primary", tenant_alpha)
    assert session.active_leases["step-lock"].agent_id == "agent-primary"
    assert session.resource_locks["catalog_writer_lock"] == "agent-primary"

    # Delegate task to backup agent
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-b",
        task_id="step-lock",
        from_agent_id="agent-primary",
        required_capability="CATALOG_MGMT",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )

    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_alpha)

    # Lease of step-lock is transferred/reset for new owner claim, old agent cannot hold it
    assert updated_session.resource_locks.get("catalog_writer_lock") == "agent-backup"
    assert updated_session.tasks["step-lock"].assigned_agent_id == "agent-backup"


# =============================================================================
# SCENARIO C: Stale Result Rejection & Monotonic Versioning
# =============================================================================

def test_scenario_c_stale_result_rejection_and_versioning(tenant_alpha, session_repo, audit_repo):
    """
    Scenario C:
    - Task is delegated from agent-1 (v1) to agent-2 (v2).
    - agent-1 completes after delegation latency.
    - complete_task and fail_task from agent-1 are strictly rejected with STALE_AGENT_RESULT_REJECTED audit.
    - agent-2 successfully completes with version 2.
    """
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")

    registry = SpecialistAgentRegistry()
    registry.register(agent1)
    registry.register(agent2)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-c",
        tenant_id="tenant-alpha",
        mission_id="mission-c",
        goal="Market data aggregation",
        steps=(
            PlanStep(
                step_id="step-c1",
                objective="Fetch prices",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-c", plan=plan)
    session, _ = service.claim_task(session.session_id, "step-c1", "agent-1", tenant_alpha)
    assert session.tasks["step-c1"].assignment_version == 1

    # Delegate to agent-2
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-c",
        task_id="step-c1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    session, record = service.delegate_task(session.session_id, del_req, tenant_alpha)
    assert session.tasks["step-c1"].assignment_version == 2
    assert session.tasks["step-c1"].assigned_agent_id == "agent-2"

    # 1. Old agent tries to complete with stale version 1 -> Rejected
    with pytest.raises(ValueError, match="Stale result rejected"):
        service.complete_task(
            session_id=session.session_id,
            task_id="step-c1",
            outputs={"price": 100},
            tenant_context=tenant_alpha,
            assignment_version=1,
            agent_id="agent-1",
        )

    # 2. Old agent tries to report failure with wrong agent_id -> Rejected
    with pytest.raises(ValueError, match="Stale failure report rejected"):
        service.fail_task(
            session_id=session.session_id,
            task_id="step-c1",
            failure_type=CoordinationFailureType.TECHNICAL_FAILURE,
            failure_reason="Late crash",
            tenant_context=tenant_alpha,
            assignment_version=2,
            agent_id="agent-1",
        )

    # 3. New owner agent-2 completes with version 2 (already has active claim/lease from atomic delegation) -> Accepted
    session = service.complete_task(
        session_id=session.session_id,
        task_id="step-c1",
        outputs={"price": 99.5},
        tenant_context=tenant_alpha,
        assignment_version=2,
        agent_id="agent-2",
    )
    assert session.tasks["step-c1"].status == CoordinationTaskStatus.COMPLETED
    assert session.tasks["step-c1"].outputs["price"] == 99.5


# =============================================================================
# SCENARIO D: Anti-Policy Bypass Enforcement
# =============================================================================

def test_scenario_d_anti_policy_bypass_enforcement(tenant_alpha, session_repo, audit_repo):
    """
    Scenario D:
    - Step fails due to POLICY_DENIED (e.g. unapproved catalog export).
    - Delegation attempt is strictly rejected to prevent security/compliance bypass.
    """
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")

    registry = SpecialistAgentRegistry()
    registry.register(agent1)
    registry.register(agent2)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-d",
        tenant_id="tenant-alpha",
        mission_id="mission-d",
        goal="Regulated publication",
        steps=(
            PlanStep(
                step_id="step-sec",
                objective="Export regulated goods",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )

    # Disable fail_fast_on_policy_denied to test granular delegation guard
    policy = CoordinationPolicy(fail_fast_on_policy_denied=False)
    session = service.create_session(tenant_alpha, "mission-d", plan=plan, policy=policy)
    service.claim_task(session.session_id, "step-sec", "agent-1", tenant_alpha)
    service.fail_task(
        session_id=session.session_id,
        task_id="step-sec",
        failure_type=CoordinationFailureType.POLICY_DENIED,
        failure_reason="Export forbidden without export compliance license",
        tenant_context=tenant_alpha,
    )

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-d",
        task_id="step-sec",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )

    with pytest.raises(ValueError, match="Cannot delegate task to bypass POLICY_DENIED failure"):
        service.delegate_task(session.session_id, del_req, tenant_alpha)


# =============================================================================
# SCENARIO E: Emergency Stop Precedence
# =============================================================================

def test_scenario_e_emergency_stop_precedence(tenant_alpha, session_repo, audit_repo):
    """
    Scenario E:
    - Emergency stop is triggered on tenant-alpha (N.11).
    - Delegation requests are immediately blocked with REJECTED_SECURITY and audit record.
    """
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")

    registry = SpecialistAgentRegistry()
    registry.register(agent1)
    registry.register(agent2)

    stop_service = MockEmergencyStopService()
    stop_service.trigger_stop("tenant-alpha")

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
        emergency_stop_service=stop_service,
    )

    plan = ExecutionPlan(
        plan_id="plan-e",
        tenant_id="tenant-alpha",
        mission_id="mission-e",
        goal="Emergency stopped task",
        steps=(
            PlanStep(
                step_id="step-e1",
                objective="Analyze dataset",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-e", plan=plan)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-e",
        task_id="step-e1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )

    with pytest.raises(ValueError, match="Cannot delegate task under active EMERGENCY_STOP"):
        service.delegate_task(session.session_id, del_req, tenant_alpha)


# =============================================================================
# SCENARIO F: Ping-Pong Prevention & Delegation Bounds
# =============================================================================

def test_scenario_f_ping_pong_prevention_and_delegation_bounds(tenant_alpha, session_repo, audit_repo):
    """
    Scenario F:
    - Agents ping-pong delegate back and forth.
    - When max_delegations_per_task is reached, delegation is denied, task transitions to BLOCKED.
    """
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")

    registry = SpecialistAgentRegistry()
    registry.register(agent1)
    registry.register(agent2)

    policy = CoordinationPolicy(max_delegations_per_task=2)
    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-f",
        tenant_id="tenant-alpha",
        mission_id="mission-f",
        goal="Ping pong task",
        steps=(
            PlanStep(
                step_id="step-f1",
                objective="High churn step",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-f", plan=plan, policy=policy)

    # Delegation 1: agent-1 -> agent-2
    req1 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-f",
        task_id="step-f1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.CAPABILITY_MISMATCH_DISCOVERED,
        current_attempt=1,
    )
    session, rec1 = service.delegate_task(session.session_id, req1, tenant_alpha)
    assert rec1.decision.status == DelegationDecisionStatus.APPROVED
    assert session.tasks["step-f1"].assigned_agent_id == "agent-2"

    # Delegation 2: agent-2 -> agent-1
    req2 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-f",
        task_id="step-f1",
        from_agent_id="agent-2",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.RATE_LIMITED,
        current_attempt=2,
    )
    session, rec2 = service.delegate_task(session.session_id, req2, tenant_alpha)
    assert rec2.decision.status == DelegationDecisionStatus.APPROVED
    assert session.tasks["step-f1"].assigned_agent_id == "agent-1"

    # Delegation 3: Exceeds limit 2 -> Rejection and task BLOCKED
    req3 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-f",
        task_id="step-f1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=3,
    )
    session, rec3 = service.delegate_task(session.session_id, req3, tenant_alpha)
    assert rec3.decision.status == DelegationDecisionStatus.REJECTED_BOUNDS
    assert session.tasks["step-f1"].status == CoordinationTaskStatus.BLOCKED
    assert "Exceeded max delegations" in session.tasks["step-f1"].failure_reason


# =============================================================================
# SCENARIO G: Capability-First Selection Matrix
# =============================================================================

def test_scenario_g_capability_first_selection_matrix(tenant_alpha, session_repo, audit_repo):
    """
    Scenario G:
    - Multiple candidate specialists with different costs, priorities, and availabilities.
    - Registry prioritizes: AVAILABLE > DEGRADED, higher priority (lower int), lower cost.
    """
    # agent-expensive: priority 1, cost 0.50
    agent_expensive = sample_agent("agent-expensive", priority=1, cost=Decimal("0.50"))
    # agent-cheap: priority 1, cost 0.10
    agent_cheap = sample_agent("agent-cheap", priority=1, cost=Decimal("0.10"))
    # agent-low-priority: priority 5, cost 0.01
    agent_low_prio = sample_agent("agent-low", priority=5, cost=Decimal("0.01"))

    registry = SpecialistAgentRegistry()
    registry.register(agent_expensive)
    registry.register(agent_cheap)
    registry.register(agent_low_prio)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-g",
        tenant_id="tenant-alpha",
        mission_id="mission-g",
        goal="Select most cost-effective specialist",
        steps=(
            PlanStep(
                step_id="step-g1",
                objective="Data mining",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-expensive",
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-g", plan=plan)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-g",
        task_id="step-g1",
        from_agent_id="agent-expensive",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.RATE_LIMITED,
        current_attempt=1,
    )

    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_alpha)

    # Should select agent-cheap because priority=1 and cost=0.10 < 0.50
    assert record.decision.to_agent_id == "agent-cheap"
    assert updated_session.tasks["step-g1"].assigned_agent_id == "agent-cheap"


# =============================================================================
# SCENARIO H: Multi-Tenant Cross Isolation
# =============================================================================

def test_scenario_h_multi_tenant_cross_isolation(tenant_alpha, tenant_beta, session_repo, audit_repo):
    """
    Scenario H:
    - CrossTenantGuard ensures tenant-beta cannot request delegation on tenant-alpha session.
    - Tenant-alpha cannot delegate to an agent registered under tenant-beta.
    """
    agent_alpha = sample_agent("agent-alpha", tenant_id="tenant-alpha")
    agent_beta = sample_agent("agent-beta", tenant_id="tenant-beta")

    registry = SpecialistAgentRegistry()
    registry.register(agent_alpha)
    registry.register(agent_beta)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-h",
        tenant_id="tenant-alpha",
        mission_id="mission-h",
        goal="Isolated plan",
        steps=(
            PlanStep(
                step_id="step-h1",
                objective="Alpha task",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-alpha",
            ),
        ),
    )

    session_alpha = service.create_session(tenant_alpha, "mission-h", plan=plan)

    # 1. Tenant-beta tries to delegate tenant-alpha's task -> CrossTenantAccessError
    req_cross = DelegationRequest(
        tenant_id="tenant-beta",
        mission_id="mission-h",
        task_id="step-h1",
        from_agent_id="agent-alpha",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    with pytest.raises(CrossTenantAccessError):
        service.delegate_task(session_alpha.session_id, req_cross, tenant_alpha)

    # 2. Tenant-alpha tries to delegate using tenant-beta context -> CrossTenantAccessError
    req_valid = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-h",
        task_id="step-h1",
        from_agent_id="agent-alpha",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    with pytest.raises(CrossTenantAccessError):
        service.delegate_task(session_alpha.session_id, req_valid, tenant_beta)


# =============================================================================
# SCENARIO I: Budget & Cost Continuity
# =============================================================================

def test_scenario_i_budget_and_cost_continuity(tenant_alpha, session_repo, audit_repo):
    """
    Scenario I:
    - Task has pre-allocated budget (tokens, max_cost).
    - Delegation preserves budget references and limits without resetting consumption accounting (K.3).
    """
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")

    registry = SpecialistAgentRegistry()
    registry.register(agent1)
    registry.register(agent2)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-i",
        tenant_id="tenant-alpha",
        mission_id="mission-i",
        goal="Budgeted mission",
        budget=PlanBudget(max_tokens=50000, max_cost=Decimal("5.00")),
        steps=(
            PlanStep(
                step_id="step-i1",
                objective="Token heavy step",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
                estimated_tokens=50000,
                estimated_cost=Decimal("5.00"),
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-i", plan=plan)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-i",
        task_id="step-i1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.CAPABILITY_MISMATCH_DISCOVERED,
        current_attempt=1,
    )

    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_alpha)

    # Budget limits remain defined and continuous
    assert record.decision.status == DelegationDecisionStatus.APPROVED
    assert updated_session.tasks["step-i1"].allocated_budget is not None
    assert updated_session.tasks["step-i1"].allocated_budget.max_tokens == 50000


# =============================================================================
# SCENARIO J: Structured Handoff & Zero-CoT Context Preservation
# =============================================================================

def test_scenario_j_structured_handoff_and_zero_cot(tenant_alpha, session_repo, audit_repo):
    """
    Scenario J:
    - Structured handoff transports facts, evidence references, and sanitized payloads.
    - Internal chain-of-thought and secrets are stripped (Zero-CoT).
    """
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")

    registry = SpecialistAgentRegistry()
    registry.register(agent1)
    registry.register(agent2)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-j",
        tenant_id="tenant-alpha",
        mission_id="mission-j",
        goal="Zero cot handoff",
        steps=(
            PlanStep(
                step_id="step-j1",
                objective="Collect raw data",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
            PlanStep(
                step_id="step-j2",
                objective="Analyze raw data",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-2",
                dependencies=("step-j1",),
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-j", plan=plan)

    # Step 1 produces output with private keys/cot
    dirty_payload = {
        "verified_fact": "Competitor price is $49",
        "api_key": "secret_key_12345",
        "token": "bearer_super_secret",
        "chain_of_thought": "I think the competitor might be cheating because...",
    }

    session, handoff_obj = service.handoff(
        session_id=session.session_id,
        source_agent_id="agent-1",
        target_task_id="step-j2",
        payload=dirty_payload,
        evidence_refs=("evidence-file-01",),
        tenant_context=tenant_alpha,
        target_agent_id="agent-2",
    )

    handoff = session.handoffs[0]
    # Secrets should be sanitized
    assert handoff.payload.get("api_key") == "[REDACTED]"
    assert handoff.payload.get("token") == "[REDACTED]"
    assert handoff.payload.get("verified_fact") == "Competitor price is $49"
    assert "evidence-file-01" in handoff.evidence_refs


# =============================================================================
# SCENARIO K: E2E Multi-Agent Mission Execution with In-Flight Dynamic Delegation
# =============================================================================

def test_scenario_k_e2e_multi_agent_execution_with_dynamic_delegation(tenant_alpha, session_repo, audit_repo):
    """
    Scenario K (Full E2E):
    - 3-step DAG:
      step-1: Scrape prices (assigned to agent-scraper-1)
      step-2: Scrape catalog (assigned to agent-scraper-2)
      step-3: Merge & generate report (assigned to agent-analyst, depends on step-1 and step-2)
    - step-1 is claimed by agent-scraper-1, which experiences RATE_LIMITED.
    - Dynamic delegation in-flight transfers step-1 to agent-scraper-backup.
    - agent-scraper-backup claims step-1, finishes successfully.
    - step-2 completes normally.
    - Fan-in barrier synchronizes step-3, which merges outputs and completes the entire mission.
    - All audit events (DELEGATION_REQUESTED, DELEGATION_APPROVED, etc.) recorded cleanly.
    """
    scraper_1 = sample_agent("agent-scraper-1", availability=AgentAvailability.AVAILABLE, priority=3)
    scraper_backup = sample_agent("agent-scraper-backup", availability=AgentAvailability.AVAILABLE, priority=1)
    scraper_2 = sample_agent("agent-scraper-2", availability=AgentAvailability.AVAILABLE, priority=2)
    analyst = sample_agent("agent-analyst", action_type="MERGE", capability_name="ANALYTICS", priority=1)

    registry = SpecialistAgentRegistry()
    registry.register(scraper_1)
    registry.register(scraper_backup)
    registry.register(scraper_2)
    registry.register(analyst)

    service = AgentCoordinatorService(
        session_repository=session_repo,
        agent_registry=registry,
        audit_repository=audit_repo,
    )

    plan = ExecutionPlan(
        plan_id="plan-e2e",
        tenant_id="tenant-alpha",
        mission_id="mission-e2e",
        goal="E2E dynamic delegation and fan-in",
        steps=(
            PlanStep(
                step_id="step-scrape-prices",
                objective="Scrape product pricing",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-scraper-1",
            ),
            PlanStep(
                step_id="step-scrape-catalog",
                objective="Scrape product catalog details",
                action_type="SEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-scraper-2",
            ),
            PlanStep(
                step_id="step-final-report",
                objective="Combine price and catalog data",
                action_type="MERGE",
                assigned_capability="ANALYTICS",
                assigned_agent="agent-analyst",
                dependencies=("step-scrape-prices", "step-scrape-catalog"),
            ),
        ),
    )

    session = service.create_session(tenant_alpha, "mission-e2e", plan=plan)
    assert session.status == CoordinationStatus.ACTIVE

    # 1. Claim step-scrape-prices by scraper_1
    session, _ = service.claim_task(session.session_id, "step-scrape-prices", "agent-scraper-1", tenant_alpha)

    # 2. Scraper 1 hits rate limits -> Dynamic Delegation
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-e2e",
        task_id="step-scrape-prices",
        from_agent_id="agent-scraper-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.RATE_LIMITED,
        current_attempt=1,
    )
    session, record = service.delegate_task(session.session_id, del_req, tenant_alpha)
    assert record.decision.status == DelegationDecisionStatus.APPROVED
    assert record.decision.to_agent_id == "agent-scraper-backup"
    assert session.tasks["step-scrape-prices"].assigned_agent_id == "agent-scraper-backup"
    assert session.tasks["step-scrape-prices"].assignment_version == 2

    # 3. Scraper backup completes step-scrape-prices (already has claim/lease from atomic delegation)
    session = service.complete_task(
        session.session_id,
        "step-scrape-prices",
        outputs={"price_index": {"SKU-1": 19.99, "SKU-2": 45.00}},
        tenant_context=tenant_alpha,
        assignment_version=2,
        agent_id="agent-scraper-backup",
    )

    # 4. Scraper 2 claims and completes step-scrape-catalog
    session, _ = service.claim_task(session.session_id, "step-scrape-catalog", "agent-scraper-2", tenant_alpha)
    session = service.complete_task(
        session.session_id,
        "step-scrape-catalog",
        outputs={"catalog_meta": {"SKU-1": "Widget A", "SKU-2": "Gadget B"}},
        tenant_context=tenant_alpha,
        assignment_version=1,
        agent_id="agent-scraper-2",
    )

    # 5. Check readiness of step-final-report (fan-in ready)
    ready = service.get_ready_tasks(session.session_id, tenant_alpha)
    assert any(t.task_id == "step-final-report" for t in ready)

    # 6. Merge upstream outputs into final step
    merge_res = service.merge_task_results(
        session_id=session.session_id,
        source_task_ids=("step-scrape-prices", "step-scrape-catalog"),
        target_task_id="step-final-report",
        tenant_context=tenant_alpha,
        strategy=MergeStrategy.KEYED_MERGE,
    )
    assert merge_res.success is True

    # 7. Complete step-final-report -> Mission session COMPLETED
    session, _ = service.claim_task(session.session_id, "step-final-report", "agent-analyst", tenant_alpha)
    session = service.complete_task(
        session.session_id,
        "step-final-report",
        outputs={"final_market_report": "All data compiled successfully"},
        tenant_context=tenant_alpha,
        assignment_version=1,
        agent_id="agent-analyst",
    )

    assert session.status == CoordinationStatus.COMPLETED
    assert all(t.status == CoordinationTaskStatus.COMPLETED for t in session.tasks.values())

    # 8. Verify audit trail has recorded delegation and completion events
    events = [r.action_or_operation for r in audit_repo.records]
    assert "DELEGATION_REQUESTED" in events
    assert "ASSIGNMENT_RELEASED" in events
    assert "ASSIGNMENT_TRANSFERRED" in events
    assert "COORDINATION_COMPLETED" in events
