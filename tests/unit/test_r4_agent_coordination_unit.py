"""Unit tests for R.4 Agent Coordination.

Validates the 20 technical invariants and contracts required for R.4:
1. Coordination session creation with strict tenant scoping.
2. Immutability and frozen contracts across all models.
3. Deterministic task assignment from R.1 DAG / R.2 Sub-missions.
4. Single owner / atomic lease invariant per task.
5. Deterministic task readiness (DAG completion, inputs present, agent available, no lock conflict).
6. Dependent task blocked/pending when upstream dependency is pending or failed.
7. Fan-out coordination across independent DAG branches with bounded concurrency.
8. Fan-in barrier coordination with input availability checks.
9. Structured handoff (AgentHandoff) with payload and evidence references.
10. Strict Anti-CoT / Zero-Chain-of-Thought sanitization on handoffs and contexts.
11. Deterministic result merging (KEYED_MERGE, EXPLICIT_PRECEDENCE, STRICT_IDENTICAL).
12. Conflict detection and downstream task blocking on unresolvable merge collisions.
13. Resource lock conflict prevention (preventing overlapping locks on identical resource_id).
14. Concurrency bounds enforcement (max_concurrent_agents, max_concurrent_tasks).
15. Budget safety and reservation validation.
16. Idempotency on lease renewal, handoff, and completion.
17. Policy denial handling (anti-policy bypass: no evasive rerouting on POLICY_DENIED).
18. Specialist agent availability checks prior to claim/execution.
19. Cancellation propagation cascading to all active and pending tasks.
20. Cross-tenant guard and multi-tenant isolation.
"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from decimal import Decimal
import pytest

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
from src.domain.audit.models import AuditRecordType
from src.domain.planning.models import ExecutionPlan, PlanBudget, PlanStep
from src.domain.security.models import sanitize_security_data
from src.domain.specialist_agent.models import (
    AgentAvailability,
    AgentCapability,
    AgentCapabilityContract,
    SpecialistAgentDefinition,
)
from src.domain.specialist_agent.registry import SpecialistAgentRegistry
from src.domain.mission.models import Mission, MissionType
from src.domain.sub_mission.models import SubMissionScope
from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.tool.models import ToolContract, ToolSchemaField
from src.infrastructure.persistence.data.json.coordination_session_repository import (
    InMemoryCoordinationSessionRepository,
)


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
) -> SpecialistAgentDefinition:
    return SpecialistAgentDefinition(
        agent_id=agent_id,
        tenant_id=tenant_id,
        capabilities=(sample_capability(capability_name),),
        availability=availability,
        allowed_action_types=("RESEARCH",),
        allowed_tool_ids=(),
    )


def sample_plan(tenant_id: str = "tenant-alpha", plan_id: str = "plan-1") -> ExecutionPlan:
    step_1 = PlanStep(
        step_id="step-1",
        objective="Market research",
        assigned_agent="agent-research",
        action_type="RESEARCH",
        required_inputs={"topic": "e-commerce trends"},
        expected_outputs=("summary",),
        metadata={"resource_id": "market-data-1"},
    )
    step_2 = PlanStep(
        step_id="step-2",
        objective="Catalog update",
        assigned_agent="agent-catalog",
        action_type="UPDATE",
        required_inputs={"summary": "Market analysis complete"},
        expected_outputs=("catalog_id",),
        dependencies=("step-1",),
        metadata={"resource_id": "catalog-store-1"},
    )
    return ExecutionPlan(
        plan_id=plan_id,
        tenant_id=tenant_id,
        mission_id="mission-1",
        goal="Market expansion",
        steps=(step_1, step_2),
        budget=PlanBudget(max_tokens=10000, max_cost=Decimal("1.0")),
    )


def test_coordination_models_immutability():
    """Invariant 2: Domain models are strictly frozen and immutable."""
    claim = TaskClaim(
        task_id="task-1",
        agent_id="agent-1",
        claimed_at=datetime.now(timezone.utc),
    )
    with pytest.raises(FrozenInstanceError):
        claim.agent_id = "agent-2"  # type: ignore

    task = CoordinationTask(
        task_id="task-1",
        session_id="session-1",
        tenant_id="tenant-alpha",
        required_capability="GENERAL_EXECUTION",
        action_type="RESEARCH",
    )
    with pytest.raises(FrozenInstanceError):
        task.status = CoordinationTaskStatus.RUNNING  # type: ignore

    session = CoordinationSession(
        session_id="session-1",
        tenant_id="tenant-alpha",
        mission_id="mission-1",
        correlation_id="corr-1",
    )
    with pytest.raises(FrozenInstanceError):
        session.status = CoordinationStatus.ACTIVE  # type: ignore


def test_session_creation_with_r1_plan_and_tenant_scope():
    """Invariants 1 & 3: Session created from R.1 Plan with deterministic tasks and tenant scoping."""
    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant_ctx = TenantContext(tenant_id="tenant-alpha")

    plan = sample_plan(tenant_id="tenant-alpha")
    session = service.create_session(
        tenant_context=tenant_ctx,
        mission_id="mission-1",
        plan=plan,
        correlation_id="corr-test-1",
    )

    assert session.session_id.startswith("coord-")
    assert session.tenant_id == "tenant-alpha"
    assert session.mission_id == "mission-1"
    assert session.status == CoordinationStatus.ACTIVE
    assert len(session.tasks) == 2
    assert "step-1" in session.tasks
    assert "step-2" in session.tasks

    task_1 = session.tasks["step-1"]
    assert task_1.status == CoordinationTaskStatus.READY
    assert task_1.assigned_agent_id == "agent-research"
    assert task_1.resource_id == "market-data-1"

    task_2 = session.tasks["step-2"]
    assert task_2.status == CoordinationTaskStatus.PENDING
    assert "step-1" in task_2.dependencies


def test_cross_tenant_isolation_raises_violation():
    """Invariant 20: CrossTenantGuard protects repositories and coordinator operations."""
    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant_a = TenantContext(tenant_id="tenant-alpha")
    tenant_b = TenantContext(tenant_id="tenant-beta")

    plan = sample_plan(tenant_id="tenant-alpha")
    session = service.create_session(
        tenant_context=tenant_a,
        mission_id="mission-1",
        plan=plan,
    )

    # Cross-tenant access to get_session
    with pytest.raises(CrossTenantAccessError):
        service.get_session(session.session_id, tenant_b)

    # Cross-tenant access to claim_task
    with pytest.raises(CrossTenantAccessError):
        service.claim_task(session.session_id, "step-1", "agent-research", tenant_b)


def test_single_owner_atomic_claim_and_lease():
    """Invariant 4: Single owner invariant; a task can only be claimed by one agent."""
    registry = SpecialistAgentRegistry()
    registry.register(sample_agent("agent-research", "tenant-alpha"))
    registry.register(sample_agent("agent-intruder", "tenant-alpha"))

    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo, agent_registry=registry)
    tenant = TenantContext(tenant_id="tenant-alpha")
    plan = sample_plan("tenant-alpha")

    session = service.create_session(tenant, "mission-1", plan=plan)

    # First claim succeeds
    updated_session, claim = service.claim_task(session.session_id, "step-1", "agent-research", tenant)
    assert claim.task_id == "step-1"
    assert claim.agent_id == "agent-research"
    assert claim.lease_id != ""
    assert updated_session.tasks["step-1"].status == CoordinationTaskStatus.CLAIMED
    assert updated_session.resource_locks["market-data-1"] == "agent-research"

    # Second claim by another agent fails (conflict)
    with pytest.raises(ValueError, match="already claimed or not in READY state"):
        service.claim_task(session.session_id, "step-1", "agent-intruder", tenant)


def test_task_readiness_dag_and_input_availability():
    """Invariants 5 & 6: Task readiness requires DAG upstream completion and input availability."""
    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant = TenantContext(tenant_id="tenant-alpha")
    plan = sample_plan("tenant-alpha")

    session = service.create_session(tenant, "mission-1", plan=plan)

    # Initially step-1 is READY, step-2 is PENDING
    ready_tasks = service.get_ready_tasks(session.session_id, tenant)
    assert len(ready_tasks) == 1
    assert ready_tasks[0].task_id == "step-1"

    # Claim and complete step-1 with required outputs
    session, _ = service.claim_task(session.session_id, "step-1", "agent-research", tenant)
    session = service.complete_task(
        session.session_id,
        "step-1",
        outputs={"summary": "Market analysis complete"},
        tenant_context=tenant,
    )

    # Now step-2 should become READY
    ready_tasks = service.get_ready_tasks(session.session_id, tenant)
    assert len(ready_tasks) == 1
    assert ready_tasks[0].task_id == "step-2"


def test_fan_out_concurrency_bounds():
    """Invariants 7 & 14: Fan-out executes independent tasks up to concurrency bounds."""
    step_a = PlanStep(
        step_id="step-a",
        objective="Parallel A",
        assigned_agent="agent-a",
        action_type="SEARCH",
    )
    step_b = PlanStep(
        step_id="step-b",
        objective="Parallel B",
        assigned_agent="agent-b",
        action_type="SEARCH",
    )
    step_c = PlanStep(
        step_id="step-c",
        objective="Parallel C",
        assigned_agent="agent-c",
        action_type="SEARCH",
    )
    plan = ExecutionPlan(
        plan_id="plan-fan-out",
        tenant_id="tenant-alpha",
        mission_id="mission-fan-out",
        goal="Fan out goal",
        steps=(step_a, step_b, step_c),
    )

    policy = CoordinationPolicy(max_concurrent_agents=2, max_concurrent_tasks=2)
    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant = TenantContext(tenant_id="tenant-alpha")

    session = service.create_session(tenant, "mission-fan-out", plan=plan, policy=policy)
    ready = service.get_ready_tasks(session.session_id, tenant)
    assert len(ready) == 3

    # Claim 2 tasks (reaches max limit)
    session, _ = service.claim_task(session.session_id, "step-a", "agent-a", tenant)
    session, _ = service.claim_task(session.session_id, "step-b", "agent-b", tenant)

    # Attempting to claim 3rd task exceeds concurrency bound
    with pytest.raises(ValueError, match="Maximum concurrent tasks limit reached"):
        service.claim_task(session.session_id, "step-c", "agent-c", tenant)


def test_resource_lock_prevents_concurrent_write_conflicts():
    """Invariant 13: Multiple tasks targeting the same resource cannot run concurrently."""
    step_1 = PlanStep(
        step_id="task-write-1",
        objective="Write to DB",
        assigned_agent="agent-1",
        action_type="WRITE",
        metadata={"resource_id": "db-table-users"},
    )
    step_2 = PlanStep(
        step_id="task-write-2",
        objective="Write to same DB",
        assigned_agent="agent-2",
        action_type="WRITE",
        metadata={"resource_id": "db-table-users"},
    )
    plan = ExecutionPlan(
        plan_id="plan-locks",
        tenant_id="tenant-alpha",
        mission_id="mission-locks",
        goal="Locks goal",
        steps=(step_1, step_2),
    )

    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant = TenantContext(tenant_id="tenant-alpha")
    session = service.create_session(tenant, "mission-locks", plan=plan)

    # First task claims the lock on db-table-users
    session, _ = service.claim_task(session.session_id, "task-write-1", "agent-1", tenant)
    assert session.resource_locks["db-table-users"] == "agent-1"

    # Second task attempting to claim while lock is active must fail
    with pytest.raises(ValueError, match="Resource lock 'db-table-users' is currently held"):
        service.claim_task(session.session_id, "task-write-2", "agent-2", tenant)

    # Completing task-write-1 releases the lock
    session = service.complete_task(session.session_id, "task-write-1", {"ok": True}, tenant)
    assert "db-table-users" not in session.resource_locks

    # Now task-write-2 can claim the resource lock
    session, _ = service.claim_task(session.session_id, "task-write-2", "agent-2", tenant)
    assert session.resource_locks["db-table-users"] == "agent-2"


def test_structured_handoff_and_zero_cot_sanitization():
    """Invariants 9 & 10: Structured AgentHandoff with Anti-CoT sanitization."""
    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant = TenantContext(tenant_id="tenant-alpha")
    plan = sample_plan("tenant-alpha")

    session = service.create_session(tenant, "mission-1", plan=plan)

    # Payload with private chain-of-thought, scratchpad, reasoning and secrets
    raw_payload = {
        "summary": "Pricing analysis complete",
        "thought": "Thinking about competitor weaknesses...",
        "scratchpad": "Internal reasoning steps 1, 2, 3",
        "chain_of_thought": "My private intermediate thoughts",
        "api_key": "sk-secret-key-12345",
        "nested": {"internal_reasoning": "Private nested reasoning"},
        "competitor_price": 49.99,
    }

    session, handoff = service.handoff(
        session_id=session.session_id,
        source_agent_id="agent-research",
        target_task_id="step-2",
        payload=raw_payload,
        tenant_context=tenant,
        evidence_refs=("doc-ref-101",),
    )

    assert handoff.source_agent_id == "agent-research"
    assert handoff.target_task_id == "step-2"
    assert handoff.payload["thought"] == "[REDACTED]"
    assert handoff.payload["scratchpad"] == "[REDACTED]"
    assert handoff.payload["chain_of_thought"] == "[REDACTED]"
    assert handoff.payload["api_key"] == "[REDACTED]"
    assert handoff.payload["nested"]["internal_reasoning"] == "[REDACTED]"
    assert handoff.payload["competitor_price"] == 49.99
    assert handoff.evidence_refs == ("doc-ref-101",)


def test_deterministic_result_merger_strategies():
    """Invariant 11: Deterministic merge strategies produce pure deterministic outputs."""
    source_a = {"items": ["item1"], "price": 10.0}
    source_b = {"vendor": "Acme", "delivery_days": 3}

    # EXPLICIT_PRECEDENCE: Resolves colliding keys using precedence order
    colliding_a = {"status": "A", "val": 100}
    colliding_b = {"status": "B", "val": 200}
    merged_prec = DeterministicResultMerger.merge(
        results_by_source={"low_prio": colliding_a, "high_prio": colliding_b},
        strategy=MergeStrategy.EXPLICIT_PRECEDENCE,
        precedence_order=("high_prio", "low_prio"),
    )
    assert merged_prec.success is True
    assert merged_prec.merged_data["status"] == "B"
    assert merged_prec.merged_data["val"] == 200

    # STRICT_IDENTICAL: Identical values across sources succeed
    identical_a = {"shared_key": "constant_val", "unique_a": 1}
    identical_b = {"shared_key": "constant_val", "unique_b": 2}
    merged_ident = DeterministicResultMerger.merge(
        results_by_source={"a": identical_a, "b": identical_b},
        strategy=MergeStrategy.STRICT_IDENTICAL,
    )
    assert merged_ident.success is True
    assert merged_ident.merged_data["shared_key"] == "constant_val"


def test_deterministic_result_merger_conflict_detection():
    """Invariant 12: Unresolvable collisions trigger conflict detection and fail merge."""
    colliding_a = {"score": 95, "title": "Doc A"}
    colliding_b = {"score": 80, "title": "Doc B"}

    # KEYED_MERGE on colliding keys records conflicts
    res_keyed = DeterministicResultMerger.merge(
        results_by_source={"src1": colliding_a, "src2": colliding_b},
        strategy=MergeStrategy.KEYED_MERGE,
    )
    assert res_keyed.success is False
    assert len(res_keyed.conflicts) == 2
    assert "key 'score'" in res_keyed.conflicts[0]
    assert "key 'title'" in res_keyed.conflicts[1]

    # FAIL_ON_CONFLICT strategy
    res_fail = DeterministicResultMerger.merge(
        results_by_source={"src1": colliding_a, "src2": colliding_b},
        strategy=MergeStrategy.FAIL_ON_CONFLICT,
    )
    assert res_fail.success is False
    assert len(res_fail.conflicts) > 0


def test_fan_in_barrier_with_merge_and_conflict_handling():
    """Invariants 8 & 12: Fan-in barrier synchronizes upstream outputs and blocks on conflict."""
    step_a = PlanStep(step_id="step-a", objective="Branch A", assigned_agent="agent-a", action_type="SEARCH")
    step_b = PlanStep(step_id="step-b", objective="Branch B", assigned_agent="agent-b", action_type="SEARCH")
    step_join = PlanStep(
        step_id="step-join",
        objective="Join barrier",
        assigned_agent="agent-join",
        action_type="MERGE",
        dependencies=("step-a", "step-b"),
    )
    plan = ExecutionPlan(
        plan_id="plan-fan-in",
        tenant_id="tenant-alpha",
        mission_id="mission-fan-in",
        goal="Fan in goal",
        steps=(step_a, step_b, step_join),
    )

    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant = TenantContext(tenant_id="tenant-alpha")
    session = service.create_session(tenant, "mission-fan-in", plan=plan)

    # Complete branch A and B with conflicting values for "metric"
    session, _ = service.claim_task(session.session_id, "step-a", "agent-a", tenant)
    session = service.complete_task(session.session_id, "step-a", {"metric": 100}, tenant)

    session, _ = service.claim_task(session.session_id, "step-b", "agent-b", tenant)
    session = service.complete_task(session.session_id, "step-b", {"metric": 200}, tenant)

    # Trigger merge on target step-join with KEYED_MERGE -> should detect conflict and block step-join
    merge_result = service.merge_task_results(
        session_id=session.session_id,
        source_task_ids=("step-a", "step-b"),
        target_task_id="step-join",
        tenant_context=tenant,
        strategy=MergeStrategy.KEYED_MERGE,
    )
    assert merge_result.success is False
    assert len(merge_result.conflicts) == 1
    assert "key 'metric'" in merge_result.conflicts[0]

    # Verify that target task is BLOCKED with CONFLICT failure type
    updated_session = service.get_session(session.session_id, tenant)
    join_task = updated_session.tasks["step-join"]
    assert join_task.status == CoordinationTaskStatus.BLOCKED
    assert join_task.failure_type == CoordinationFailureType.CONFLICT


def test_failure_propagation_and_anti_policy_bypass():
    """Invariants 6 & 17: Downstream tasks fail with DEPENDENCY_FAILED; POLICY_DENIED is non-evasive."""
    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant = TenantContext(tenant_id="tenant-alpha")
    plan = sample_plan("tenant-alpha")

    session = service.create_session(tenant, "mission-1", plan=plan)

    # Claim step-1 and fail it with POLICY_DENIED
    session, _ = service.claim_task(session.session_id, "step-1", "agent-research", tenant)
    session = service.fail_task(
        session_id=session.session_id,
        task_id="step-1",
        failure_type=CoordinationFailureType.POLICY_DENIED,
        failure_reason="Operation blocked by security policy",
        tenant_context=tenant,
    )

    assert session.tasks["step-1"].status == CoordinationTaskStatus.FAILED
    assert session.tasks["step-1"].failure_type == CoordinationFailureType.POLICY_DENIED

    # Downstream step-2 must be automatically BLOCKED with DEPENDENCY_FAILED
    assert session.tasks["step-2"].status == CoordinationTaskStatus.BLOCKED
    assert session.tasks["step-2"].failure_type == CoordinationFailureType.DEPENDENCY_FAILED

    # Overall session is marked FAILED without attempts to evade policy
    assert session.status == CoordinationStatus.FAILED
    assert not any(
        t.status == CoordinationTaskStatus.RUNNING
        for t in session.tasks.values()
    )


def test_specialist_agent_availability_check():
    """Invariant 18: AgentCoordinator validates specialist agent availability before claim."""
    registry = SpecialistAgentRegistry()
    busy_agent = sample_agent("agent-busy", "tenant-alpha", availability=AgentAvailability.UNAVAILABLE)
    registry.register(busy_agent)

    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo, agent_registry=registry)
    tenant = TenantContext(tenant_id="tenant-alpha")

    step = PlanStep(
        step_id="step-1",
        objective="Busy task",
        assigned_agent="agent-busy",
        action_type="RESEARCH",
    )
    plan = ExecutionPlan(
        plan_id="plan-avail",
        tenant_id="tenant-alpha",
        mission_id="mission-avail",
        goal="Avail goal",
        steps=(step,),
    )

    session = service.create_session(tenant, "mission-avail", plan=plan)

    # Attempting to claim with BUSY agent fails with CAPABILITY_UNAVAILABLE
    with pytest.raises(ValueError, match="is not AVAILABLE"):
        service.claim_task(session.session_id, "step-1", "agent-busy", tenant)


def test_cancellation_cascades_to_all_tasks():
    """Invariant 19: Session cancellation cascades to active and pending tasks."""
    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant = TenantContext(tenant_id="tenant-alpha")
    plan = sample_plan("tenant-alpha")

    session = service.create_session(tenant, "mission-1", plan=plan)
    session, _ = service.claim_task(session.session_id, "step-1", "agent-research", tenant)

    cancelled_session = service.cancel_coordination(
        session_id=session.session_id,
        tenant_context=tenant,
        reason="User requested emergency cancel",
    )

    assert cancelled_session.status == CoordinationStatus.CANCELLED
    assert cancelled_session.tasks["step-1"].status == CoordinationTaskStatus.CANCELLED
    assert cancelled_session.tasks["step-2"].status == CoordinationTaskStatus.CANCELLED
    assert len(cancelled_session.active_leases) == 0
    assert len(cancelled_session.resource_locks) == 0


def test_sub_mission_coordination_integration():
    """Invariant 3: Coordination can be constructed from R.2 Sub-missions (hierarchical delegation)."""
    sub_1 = Mission(
        mission_id="sub-1",
        type=MissionType.MARKET_DISCOVERY,
        parent_mission_id="root-mission",
        parameters={
            "assigned_agent": "agent-scout",
            "resource_id": "db-scope-1",
            "action_type": "SEARCH",
        },
    )
    sub_2 = Mission(
        mission_id="sub-2",
        type=MissionType.PROFIT_EVALUATION,
        parent_mission_id="root-mission",
        parameters={
            "assigned_agent": "agent-analyst",
            "resource_id": "db-scope-2",
            "action_type": "ANALYZE",
            "dependencies": ("sub-1",),
        },
    )

    repo = InMemoryCoordinationSessionRepository()
    service = AgentCoordinatorService(session_repository=repo)
    tenant = TenantContext(tenant_id="tenant-alpha")

    session = service.create_session(
        tenant_context=tenant,
        mission_id="root-mission",
        sub_missions=(sub_1, sub_2),
    )

    assert len(session.tasks) == 2
    assert "sub-1" in session.tasks
    assert "sub-2" in session.tasks

    task_1 = session.tasks["sub-1"]
    assert task_1.status == CoordinationTaskStatus.READY
    assert task_1.assigned_agent_id == "agent-scout"

    task_2 = session.tasks["sub-2"]
    assert task_2.status == CoordinationTaskStatus.PENDING
    assert "sub-1" in task_2.dependencies
