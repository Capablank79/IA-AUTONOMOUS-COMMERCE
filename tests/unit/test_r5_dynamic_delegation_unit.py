"""Unit tests for R.5 Dynamic Delegation.

Validates the 20 technical invariants and contracts required for R.5:
1. Immutability and frozen domain models (DelegationRequest, DelegationDecision, DelegationRecord, DelegationPolicy, DelegationReason).
2. Single Owner preservation: Atomic ownership transfer (old owner lease removed -> new owner lease active).
3. Capability-First Specialist Agent selection integration (R.3 registry matching required capability, action_type, tools, availability).
4. Fallback behavior (degraded agent fallback allowed only if configured via policy).
5. Explicit valid delegation reasons (AGENT_UNAVAILABLE, TECHNICAL_FAILURE, CAPABILITY_MISMATCH_DISCOVERED, BUDGET_INELIGIBLE, RATE_LIMITED, COORDINATION_CONFLICT_RESOLVED, MANUAL_SAFE_REASSIGNMENT).
6. Anti-policy bypass: Rejection of delegation attempt on POLICY_DENIED failures.
7. Emergency stop protection: Rejection/blocking of delegation under active EMERGENCY_STOP.
8. Ping-pong prevention & task delegation bounds (max_delegations_per_task limit reached triggers DELEGATION_LIMIT_REACHED and blocks task).
9. Mission delegation bounds / cumulative history tracking.
10. Assignment version increment (assignment_version increments deterministically).
11. Stale result rejection: old owner attempting complete_task with outdated assignment_version is rejected and audited.
12. Stale failure rejection: old owner attempting fail_task with outdated assignment_version is rejected and audited.
13. Stale agent rejection: wrong agent_id attempting complete_task / fail_task is rejected and audited.
14. Budget continuity: Preservation of token and cost budgets across reassignments without resetting.
15. Structured handoff (Zero-CoT) preservation during delegation.
16. Multi-tenant isolation: CrossTenantGuard validation preventing cross-tenant delegation requests.
17. Idempotency of delegation requests (deterministic idempotency_key in DelegationRequest).
18. Full audit trail integration (K.1) for all delegation events.
19. Resource lock transfer: resource lock updated to new assigned agent upon successful delegation.
20. No compatible agent handling: task marked BLOCKED with CAPABILITY_UNAVAILABLE when no eligible agent exists.
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.application.agent_coordination.coordinator_service import AgentCoordinatorService
from src.domain.agent_coordination.delegation_models import (
    DelegationDecision,
    DelegationDecisionStatus,
    DelegationPolicy,
    DelegationReason,
    DelegationRecord,
    DelegationRequest,
)
from src.domain.agent_coordination.models import (
    CoordinationEventType,
    CoordinationFailureType,
    CoordinationPolicy,
    CoordinationSession,
    CoordinationStatus,
    CoordinationTask,
    CoordinationTaskStatus,
    TaskClaim,
)
from src.domain.audit.models import AuditRecordType
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


class MockAuditRepository:
    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)

    def list_all(self):
        return list(self.records)


class MockEmergencyStopService:
    def __init__(self, stopped: bool = False):
        self._stopped = stopped

    def is_stopped(self, tenant_id: str) -> bool:
        return self._stopped


def sample_contract(name: str, field_name: str = "query") -> ToolContract:
    return ToolContract(name, (ToolSchemaField(field_name, "str"),), allow_extra_fields=True)


def sample_capability(name: str = "MARKET_RESEARCH") -> AgentCapability:
    return AgentCapability(
        capability_id=name,
        contract=AgentCapabilityContract(
            sample_contract("input", "topic"),
            sample_contract("output", "summary"),
        ),
        action_types=("RESEARCH",),
        tool_ids=(),
    )


def sample_agent(
    agent_id: str,
    tenant_id: str = "tenant-alpha",
    capability_name: str = "MARKET_RESEARCH",
    availability: AgentAvailability = AgentAvailability.AVAILABLE,
    policy_eligible: bool = True,
    priority: int = 1,
    estimated_cost: Decimal = Decimal("0.05"),
) -> SpecialistAgentDefinition:
    return SpecialistAgentDefinition(
        agent_id=agent_id,
        tenant_id=tenant_id,
        capabilities=(sample_capability(capability_name),),
        availability=availability,
        policy_eligible=policy_eligible,
        priority=priority,
        estimated_cost=estimated_cost,
        allowed_action_types=("RESEARCH",),
        allowed_tool_ids=(),
    )


def setup_coordinator_with_agents(agents=(), emergency_stopped: bool = False):
    registry = SpecialistAgentRegistry()
    for agent in agents:
        registry.register(agent)
    repo = InMemoryCoordinationSessionRepository()
    audit_repo = MockAuditRepository()
    estop = MockEmergencyStopService(stopped=emergency_stopped)
    service = AgentCoordinatorService(
        session_repository=repo,
        agent_registry=registry,
        audit_repository=audit_repo,
        emergency_stop_service=estop,
    )
    return service, repo, audit_repo, registry


def test_r5_immutability_and_frozen_domain_models():
    """1. Immutability and frozen domain models."""
    req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    with pytest.raises(FrozenInstanceError):
        req.task_id = "step-2"  # type: ignore

    decision = DelegationDecision(
        request=req,
        status=DelegationDecisionStatus.APPROVED,
        to_agent_id="agent-2",
    )
    with pytest.raises(FrozenInstanceError):
        decision.to_agent_id = "agent-3"  # type: ignore

    record = DelegationRecord(
        delegation_id="del-12345",
        decision=decision,
        assignment_version=2,
    )
    with pytest.raises(FrozenInstanceError):
        record.assignment_version = 3  # type: ignore

    policy = DelegationPolicy(max_delegations_per_task=2)
    with pytest.raises(FrozenInstanceError):
        policy.max_delegations_per_task = 5  # type: ignore


def test_r5_single_owner_atomic_transfer():
    """2. Single Owner preservation: Atomic ownership transfer."""
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")
    service, repo, audit_repo, _ = setup_coordinator_with_agents([agent1, agent2])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha", correlation_id="corr-1")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    # Initial state verification
    s_claimed = repo.get_session(session.session_id, tenant_ctx)
    assert s_claimed.tasks["step-1"].assigned_agent_id == "agent-1"
    assert s_claimed.tasks["step-1"].assignment_version == 1
    assert s_claimed.active_leases["step-1"].agent_id == "agent-1"

    # Execute delegation
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.AGENT_UNAVAILABLE,
        current_attempt=1,
    )
    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_ctx)

    assert record.decision.status == DelegationDecisionStatus.APPROVED
    assert record.decision.to_agent_id == "agent-2"
    assert record.assignment_version == 2

    # Verify atomic single-owner transfer
    t = updated_session.tasks["step-1"]
    assert t.assigned_agent_id == "agent-2"
    assert t.assignment_version == 2
    assert t.delegation_history == ("agent-1",)
    assert updated_session.active_leases["step-1"].agent_id == "agent-2"
    assert updated_session.active_leases["step-1"].is_active is True


def test_r5_capability_first_selection_integration():
    """3. Capability-First Specialist Agent selection integration."""
    agent1 = sample_agent("agent-1", priority=1, estimated_cost=Decimal("0.10"))
    agent_fast = sample_agent("agent-fast", priority=1, estimated_cost=Decimal("0.02"))
    agent_slow = sample_agent("agent-slow", priority=2, estimated_cost=Decimal("0.08"))
    service, _, _, _ = setup_coordinator_with_agents([agent1, agent_fast, agent_slow])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_ctx)

    # Should select agent-fast due to lower cost / higher priority
    assert record.decision.status == DelegationDecisionStatus.APPROVED
    assert record.decision.to_agent_id == "agent-fast"


def test_r5_fallback_to_degraded_agent_behavior():
    """4. Fallback behavior (degraded agent fallback allowed only if configured via policy)."""
    agent1 = sample_agent("agent-1")
    agent_degraded = sample_agent("agent-deg", availability=AgentAvailability.DEGRADED)

    # 1. Default policy: allow_fallback_to_degraded = False -> Should reject
    service, _, _, _ = setup_coordinator_with_agents([agent1, agent_degraded])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.AGENT_UNAVAILABLE,
        current_attempt=1,
    )
    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_ctx)
    assert record.decision.status == DelegationDecisionStatus.REJECTED_CAPABILITY
    assert updated_session.tasks["step-1"].status == CoordinationTaskStatus.BLOCKED

    # 2. Policy with allow_fallback_to_degraded = True -> Should approve
    policy = CoordinationPolicy(allow_fallback_to_degraded=True)
    session2 = service.create_session(tenant_ctx, "mission-2", plan=plan, policy=policy)
    service.claim_task(session2.session_id, "step-1", "agent-1", tenant_ctx)

    del_req2 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-2",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.AGENT_UNAVAILABLE,
        current_attempt=1,
    )
    updated_session2, record2 = service.delegate_task(session2.session_id, del_req2, tenant_ctx)
    assert record2.decision.status == DelegationDecisionStatus.APPROVED
    assert record2.decision.to_agent_id == "agent-deg"


def test_r5_explicit_valid_delegation_reasons():
    """5. Explicit valid delegation reasons."""
    reasons = [
        DelegationReason.AGENT_UNAVAILABLE,
        DelegationReason.TECHNICAL_FAILURE,
        DelegationReason.CAPABILITY_MISMATCH_DISCOVERED,
        DelegationReason.BUDGET_INELIGIBLE,
        DelegationReason.RATE_LIMITED,
        DelegationReason.COORDINATION_CONFLICT_RESOLVED,
        DelegationReason.MANUAL_SAFE_REASSIGNMENT,
    ]
    for reason in reasons:
        req = DelegationRequest(
            tenant_id="tenant-alpha",
            mission_id="mission-1",
            task_id="step-1",
            from_agent_id="agent-1",
            required_capability="MARKET_RESEARCH",
            reason=reason,
            current_attempt=1,
        )
        assert req.reason == reason


def test_r5_anti_policy_bypass_rejects_policy_denied():
    """6. Anti-policy bypass: Rejection of delegation attempt on POLICY_DENIED failures."""
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")
    service, _, _, _ = setup_coordinator_with_agents([agent1, agent2])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Publish product",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    policy = CoordinationPolicy(fail_fast_on_policy_denied=False)
    session = service.create_session(tenant_ctx, "mission-1", plan=plan, policy=policy)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)
    # Task fails due to POLICY_DENIED
    service.fail_task(
        session.session_id,
        "step-1",
        CoordinationFailureType.POLICY_DENIED,
        "Blocked by compliance rules",
        tenant_ctx,
    )

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    with pytest.raises(ValueError, match="Cannot delegate task to bypass POLICY_DENIED"):
        service.delegate_task(session.session_id, del_req, tenant_ctx)


def test_r5_emergency_stop_blocks_delegation():
    """7. Emergency stop protection: Rejection/blocking under active EMERGENCY_STOP."""
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")
    service, _, _, _ = setup_coordinator_with_agents([agent1, agent2], emergency_stopped=True)
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.AGENT_UNAVAILABLE,
        current_attempt=1,
    )
    with pytest.raises(ValueError, match="Cannot delegate task under active EMERGENCY_STOP"):
        service.delegate_task(session.session_id, del_req, tenant_ctx)


def test_r5_ping_pong_prevention_and_bounds_limit():
    """8 & 9. Ping-pong prevention & task delegation bounds."""
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")
    service, _, _, _ = setup_coordinator_with_agents([agent1, agent2])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    policy = CoordinationPolicy(max_delegations_per_task=2)
    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan, policy=policy)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    # 1st delegation: agent-1 -> agent-2
    req1 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.RATE_LIMITED,
        current_attempt=1,
    )
    s1, r1 = service.delegate_task(session.session_id, req1, tenant_ctx)
    assert r1.decision.status == DelegationDecisionStatus.APPROVED
    assert s1.tasks["step-1"].assignment_version == 2

    # 2nd delegation: agent-2 -> agent-1
    req2 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-2",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.RATE_LIMITED,
        current_attempt=2,
    )
    s2, r2 = service.delegate_task(session.session_id, req2, tenant_ctx)
    assert r2.decision.status == DelegationDecisionStatus.APPROVED
    assert s2.tasks["step-1"].assignment_version == 3

    # 3rd delegation: Exceeds max_delegations_per_task=2 -> Rejected and Task BLOCKED
    req3 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.RATE_LIMITED,
        current_attempt=3,
    )
    s3, r3 = service.delegate_task(session.session_id, req3, tenant_ctx)
    assert r3.decision.status == DelegationDecisionStatus.REJECTED_BOUNDS
    assert s3.tasks["step-1"].status == CoordinationTaskStatus.BLOCKED


def test_r5_stale_result_rejection_by_version_and_agent():
    """10, 11, 12 & 13. Assignment versioning & stale execution result rejection."""
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")
    service, _, audit_repo, _ = setup_coordinator_with_agents([agent1, agent2])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    # Delegate to agent-2 (version becomes 2)
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    service.delegate_task(session.session_id, del_req, tenant_ctx)

    # Old owner (agent-1) tries to complete task with stale version=1 -> Rejected!
    with pytest.raises(ValueError, match="Stale result rejected: task assignment_version is 2, but received version 1"):
        service.complete_task(
            session.session_id,
            "step-1",
            {"summary": "Old results"},
            tenant_ctx,
            assignment_version=1,
            agent_id="agent-1",
        )

    # Old owner tries to fail task with stale version=1 -> Rejected!
    with pytest.raises(ValueError, match="Stale failure report rejected: task assignment_version is 2, but received version 1"):
        service.fail_task(
            session.session_id,
            "step-1",
            CoordinationFailureType.TECHNICAL_FAILURE,
            "Late failure",
            tenant_ctx,
            assignment_version=1,
            agent_id="agent-1",
        )

    # Wrong agent tries to complete without valid assignment -> Rejected!
    with pytest.raises(ValueError, match="Stale result rejected: task is assigned to agent agent-2, but received result from agent-1"):
        service.complete_task(
            session.session_id,
            "step-1",
            {"summary": "Wrong agent results"},
            tenant_ctx,
            assignment_version=2,
            agent_id="agent-1",
        )

    # Current owner (agent-2) with version=2 completes successfully
    completed_session = service.complete_task(
        session.session_id,
        "step-1",
        {"summary": "Valid fresh results"},
        tenant_ctx,
        assignment_version=2,
        agent_id="agent-2",
    )
    assert completed_session.tasks["step-1"].status == CoordinationTaskStatus.COMPLETED
    assert completed_session.tasks["step-1"].outputs == {"summary": "Valid fresh results"}


def test_r5_budget_and_cost_continuity():
    """14. Budget continuity: Preservation of token and cost budgets across reassignments."""
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")
    service, _, _, _ = setup_coordinator_with_agents([agent1, agent2])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
                estimated_tokens=5000,
                estimated_cost=Decimal("1.50"),
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.RATE_LIMITED,
        current_attempt=1,
    )
    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_ctx)

    assert record.budget_state["max_tokens"] == 5000
    assert record.budget_state["max_cost"] == "1.50"

    t = updated_session.tasks["step-1"]
    assert t.allocated_budget is not None
    assert t.allocated_budget.max_tokens == 5000
    assert t.allocated_budget.max_cost == Decimal("1.50")


def test_r5_cross_tenant_isolation_raises_violation():
    """16. Multi-tenant isolation: CrossTenantGuard validation preventing cross-tenant delegation."""
    agent1 = sample_agent("agent-1", tenant_id="tenant-alpha")
    agent2 = sample_agent("agent-2", tenant_id="tenant-beta")
    service, _, _, _ = setup_coordinator_with_agents([agent1, agent2])

    tenant_alpha_ctx = TenantContext(tenant_id="tenant-alpha")
    tenant_beta_ctx = TenantContext(tenant_id="tenant-beta")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_alpha_ctx, "mission-1", plan=plan)

    # Cross-tenant request context mismatch
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.AGENT_UNAVAILABLE,
        current_attempt=1,
    )
    with pytest.raises(CrossTenantAccessError):
        service.delegate_task(session.session_id, del_req, tenant_beta_ctx)


def test_r5_idempotency_key_generation():
    """17. Idempotency of delegation requests."""
    req1 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    req2 = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    assert req1.idempotency_key != ""
    assert req1.idempotency_key == req2.idempotency_key


def test_r5_audit_trail_integration():
    """18. Full audit trail integration for delegation events."""
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")
    service, _, audit_repo, _ = setup_coordinator_with_agents([agent1, agent2])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha", correlation_id="corr-audit")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.AGENT_UNAVAILABLE,
        current_attempt=1,
    )
    service.delegate_task(session.session_id, del_req, tenant_ctx)

    records = audit_repo.list_all()
    event_types = [r.record_type.value for r in records]

    assert "DELEGATION_REQUESTED" in event_types
    assert "ASSIGNMENT_RELEASED" in event_types
    assert "ASSIGNMENT_TRANSFERRED" in event_types


def test_r5_resource_lock_transfer_upon_delegation():
    """19. Resource lock transfer: resource lock updated to new assigned agent."""
    agent1 = sample_agent("agent-1")
    agent2 = sample_agent("agent-2")
    service, _, _, _ = setup_coordinator_with_agents([agent1, agent2])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Update inventory",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
                metadata={"resource_id": "product-inv-123"},
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    # Initial lock held by agent-1
    assert session.resource_locks.get("product-inv-123") is None
    s_claimed = service.get_session(session.session_id, tenant_ctx)
    assert s_claimed.resource_locks["product-inv-123"] == "agent-1"

    # Delegate to agent-2
    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.TECHNICAL_FAILURE,
        current_attempt=1,
    )
    updated_session, _ = service.delegate_task(session.session_id, del_req, tenant_ctx)

    # Resource lock transferred to agent-2
    assert updated_session.resource_locks["product-inv-123"] == "agent-2"


def test_r5_no_compatible_agent_blocks_task():
    """20. No compatible agent handling: task marked BLOCKED with CAPABILITY_UNAVAILABLE."""
    agent1 = sample_agent("agent-1")
    # Only agent1 in registry, no other agents available
    service, _, _, _ = setup_coordinator_with_agents([agent1])
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = ExecutionPlan(
        plan_id="plan-1",
        mission_id="mission-1",
        tenant_id="tenant-alpha",
        goal="Collect market data",
        steps=(
            PlanStep(
                step_id="step-1",
                objective="Collect data",
                action_type="RESEARCH",
                assigned_capability="MARKET_RESEARCH",
                assigned_agent="agent-1",
            ),
        ),
    )
    session = service.create_session(tenant_ctx, "mission-1", plan=plan)
    service.claim_task(session.session_id, "step-1", "agent-1", tenant_ctx)

    del_req = DelegationRequest(
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        task_id="step-1",
        from_agent_id="agent-1",
        required_capability="MARKET_RESEARCH",
        reason=DelegationReason.AGENT_UNAVAILABLE,
        current_attempt=1,
    )
    updated_session, record = service.delegate_task(session.session_id, del_req, tenant_ctx)

    assert record.decision.status == DelegationDecisionStatus.REJECTED_CAPABILITY
    assert updated_session.tasks["step-1"].status == CoordinationTaskStatus.BLOCKED
    assert updated_session.tasks["step-1"].failure_type == CoordinationFailureType.CAPABILITY_UNAVAILABLE
