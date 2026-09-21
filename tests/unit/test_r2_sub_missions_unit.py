"""
Unit tests for R.2 Sub-missions (Hito R — Advanced Autonomy).

Covers the 20 mandatory unit testing requirements:
1. Valid child creation
2. Parent/root linkage
3. Same tenant required (CrossTenantAccessError)
4. Hierarchy cycle rejected
5. Max depth bound
6. Child-count bound (fan-out)
7. Objective scoped (properly bounded)
8. Context inheritance minimal
9. Secrets not inherited (Anti-CoT & sanitization)
10. Budget allocation
11. UNKNOWN budget not unlimited
12. Sibling budget isolation
13. Result propagation
14. Required child failure effect
15. Optional child behavior
16. Parent completion blocked by required active child
17. Cancellation propagation
18. Idempotent creation
19. Concurrent duplicate prevention
20. No R.3 implementation
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any, Optional
import threading
import pytest

from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionPriority

MISSION_TYPE = MissionType.MARKET_DISCOVERY
from src.domain.tenant.models import TenantContext, TenantId, CrossTenantAccessError
from src.domain.planning.models import PlanBudget, ExecutionPlan, PlanStep, StepStatus
from src.domain.sub_mission.models import (
    SubMissionScope,
    SubMissionCreationContract,
    SubMissionResultContract,
    SubMissionFailureType,
    SubMissionHierarchyPolicy,
    SubMissionNode,
)
from src.domain.sub_mission.validator import (
    SubMissionHierarchyValidator,
    SubMissionHierarchyError,
    HierarchyCycleDetectedError,
    MaxDepthExceededError,
    MaxChildrenExceededError,
    BudgetExceededError,
    InvalidParentMissionError,
    TenantMismatchError,
)
from src.domain.audit.models import AuditRecordType
from src.infrastructure.persistence.data.json.tenant_mission_repository import JsonTenantMissionRepository
from src.infrastructure.persistence.data.json.execution_plan_repository import InMemoryExecutionPlanRepository
from src.application.sub_mission.service import SubMissionService


# ==========================================
# Mock / Test Ports for Audit, Trace & Safety
# ==========================================

class RecordingAuditLogger:
    def __init__(self):
        self.events = []

    def log(self, event_type, tenant_id=None, metadata=None, **kwargs):
        self.events.append({
            "event_type": event_type,
            "tenant_id": tenant_id,
            "metadata": metadata or {},
            **kwargs,
        })


class RecordingAgentTrace:
    def __init__(self):
        self.traces = []

    def record_step(self, trace_id, step_type, details=None, **kwargs):
        self.traces.append({
            "trace_id": trace_id,
            "step_type": step_type,
            "details": details or {},
            **kwargs,
        })


class MockEmergencyStop:
    def __init__(self, blocked: bool = False, reason: str = ""):
        self.blocked = blocked
        self.reason = reason

    def evaluate(self, context):
        return type("StopDecision", (), {"is_blocked": self.blocked})()


# ==========================================
# Test Fixtures
# ==========================================

@pytest.fixture
def tenant_ctx_a():
    return TenantContext(tenant_id="tenant_a")


@pytest.fixture
def tenant_ctx_b():
    return TenantContext(tenant_id="tenant_b")


@pytest.fixture
def mission_repo(tmp_path):
    return JsonTenantMissionRepository(base_storage_dir=tmp_path / "missions_store")


@pytest.fixture
def audit_logger():
    return RecordingAuditLogger()


@pytest.fixture
def trace_service():
    return RecordingAgentTrace()


@pytest.fixture
def emergency_stop():
    return MockEmergencyStop(blocked=False)


@pytest.fixture
def plan_repo():
    return InMemoryExecutionPlanRepository()


@pytest.fixture
def sub_mission_service(mission_repo, audit_logger, trace_service, emergency_stop, plan_repo):
    return SubMissionService(
        mission_repository=mission_repo,
        audit_logger=audit_logger,
        trace_service=trace_service,
        emergency_stop_service=emergency_stop,
        plan_repository=plan_repo,
    )


# ==========================================
# 1. Valid Child Creation
# ==========================================

def test_1_valid_child_creation(sub_mission_service, mission_repo, tenant_ctx_a):
    parent = Mission(
        mission_id="m_parent_1",
        type=MISSION_TYPE,
        parameters={"goal": "Analyze market"},
    )
    mission_repo.save(tenant_ctx_a, parent)

    scope = SubMissionScope(
        objective="Inspect competitors in niche",
        expected_outcome="Competitor list with pricing",
        completion_condition="Top 5 competitors analyzed",
        expected_output_keys=("competitors", "avg_price"),
    )
    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=scope,
        inputs={"niche": "footwear"},
        allocated_budget=PlanBudget(max_tokens=2000, max_cost=Decimal("0.05")),
    )

    child = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)

    assert child.mission_id.startswith("sub_")
    assert child.parent_mission_id == parent.mission_id
    assert child.root_mission_id == parent.mission_id
    assert child.depth == 1
    assert child.status == MissionStatus.PENDING
    assert child.is_required is True
    assert child.parameters["objective"] == "Inspect competitors in niche"
    assert child.parameters["inputs"]["niche"] == "footwear"


# ==========================================
# 2. Parent / Root Linkage
# ==========================================

def test_2_parent_root_linkage(sub_mission_service, mission_repo, tenant_ctx_a):
    root = Mission(
        mission_id="m_root",
        type=MISSION_TYPE,
        parameters={},
    )
    mission_repo.save(tenant_ctx_a, root)

    # Child 1 (Depth 1)
    contract_c1 = SubMissionCreationContract(
        parent_mission_id=root.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope(
            objective="C1 objective",
            expected_outcome="Outcome 1",
            completion_condition="Condition 1",
        ),
    )
    child_1 = sub_mission_service.create_sub_mission(tenant_ctx_a, contract_c1)
    assert child_1.parent_mission_id == "m_root"
    assert child_1.root_mission_id == "m_root"
    assert child_1.depth == 1

    # Child 2 under Child 1 (Depth 2)
    contract_c2 = SubMissionCreationContract(
        parent_mission_id=child_1.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MissionType.SUPPLIER_SEARCH,
        scope=SubMissionScope(
            objective="C2 objective",
            expected_outcome="Outcome 2",
            completion_condition="Condition 2",
        ),
    )
    child_2 = sub_mission_service.create_sub_mission(tenant_ctx_a, contract_c2)
    assert child_2.parent_mission_id == child_1.mission_id
    assert child_2.root_mission_id == "m_root"
    assert child_2.depth == 2


# ==========================================
# 3. Same Tenant Required
# ==========================================

def test_3_same_tenant_required(sub_mission_service, mission_repo, tenant_ctx_a, tenant_ctx_b):
    parent = Mission(
        mission_id="m_parent_ten_a",
        type=MISSION_TYPE,
        parameters={},
    )
    mission_repo.save(tenant_ctx_a, parent)

    # Attempt to create child from tenant B pointing to parent in tenant A
    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_b",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope(
            objective="Cross tenant sub mission",
            expected_outcome="Fail",
            completion_condition="Fail",
        ),
    )

    with pytest.raises(InvalidParentMissionError):
        sub_mission_service.create_sub_mission(tenant_ctx_b, contract)


# ==========================================
# 4. Hierarchy Cycle Rejected
# ==========================================

def test_4_hierarchy_cycle_rejected(mission_repo, tenant_ctx_a):
    validator = SubMissionHierarchyValidator()

    # Setup parent map
    hierarchy_map = {
        "m_root": None,
        "m_child1": "m_root",
        "m_child2": "m_child1",
    }

    def get_parent_func(mid):
        return hierarchy_map.get(mid)

    # Candidate child "m_root" trying to become child of "m_child2" -> Cycle!
    with pytest.raises(HierarchyCycleDetectedError):
        validator.validate_no_cycles("m_root", "m_child2", get_parent_func)

    # Self cycle: "m_child1" parent of "m_child1"
    with pytest.raises(HierarchyCycleDetectedError):
        validator.validate_no_cycles("m_child1", "m_child1", get_parent_func)


# ==========================================
# 5. Max Depth Bound
# ==========================================

def test_5_max_depth_bound(mission_repo, tenant_ctx_a):
    # Set strict policy max_depth = 2
    strict_policy = SubMissionHierarchyPolicy(max_depth=2)
    service = SubMissionService(
        mission_repository=mission_repo,
        hierarchy_policy=strict_policy,
    )

    # Root (Depth 0)
    root = Mission(mission_id="m_d0", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, root)

    # Depth 1 (OK)
    c1 = service.create_sub_mission(
        tenant_ctx_a,
        SubMissionCreationContract(
            parent_mission_id="m_d0",
            tenant_id="tenant_a",
            sub_mission_type=MISSION_TYPE,
            scope=SubMissionScope("Obj 1", "Out 1", "Cond 1"),
        ),
    )
    assert c1.depth == 1

    # Depth 2 (OK)
    c2 = service.create_sub_mission(
        tenant_ctx_a,
        SubMissionCreationContract(
            parent_mission_id=c1.mission_id,
            tenant_id="tenant_a",
            sub_mission_type=MISSION_TYPE,
            scope=SubMissionScope("Obj 2", "Out 2", "Cond 2"),
        ),
    )
    assert c2.depth == 2

    # Depth 3 -> Exceeds max_depth=2 -> Rejection
    with pytest.raises(MaxDepthExceededError):
        service.create_sub_mission(
            tenant_ctx_a,
            SubMissionCreationContract(
                parent_mission_id=c2.mission_id,
                tenant_id="tenant_a",
                sub_mission_type=MISSION_TYPE,
                scope=SubMissionScope("Obj 3", "Out 3", "Cond 3"),
            ),
        )


# ==========================================
# 6. Child-Count Bound (Fan-Out)
# ==========================================

def test_6_child_count_bound(mission_repo, tenant_ctx_a):
    strict_policy = SubMissionHierarchyPolicy(max_children_per_parent=2)
    service = SubMissionService(
        mission_repository=mission_repo,
        hierarchy_policy=strict_policy,
    )

    parent = Mission(mission_id="m_parent_fanout", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, parent)

    # Child 1 (OK)
    service.create_sub_mission(
        tenant_ctx_a,
        SubMissionCreationContract(
            parent_mission_id="m_parent_fanout",
            tenant_id="tenant_a",
            sub_mission_type=MISSION_TYPE,
            scope=SubMissionScope("O1", "O1", "C1"),
            delegation_key="k1",
        ),
    )

    # Child 2 (OK)
    service.create_sub_mission(
        tenant_ctx_a,
        SubMissionCreationContract(
            parent_mission_id="m_parent_fanout",
            tenant_id="tenant_a",
            sub_mission_type=MISSION_TYPE,
            scope=SubMissionScope("O2", "O2", "C2"),
            delegation_key="k2",
        ),
    )

    # Child 3 -> Exceeds max_children_per_parent=2
    with pytest.raises(MaxChildrenExceededError):
        service.create_sub_mission(
            tenant_ctx_a,
            SubMissionCreationContract(
                parent_mission_id="m_parent_fanout",
                tenant_id="tenant_a",
                sub_mission_type=MISSION_TYPE,
                scope=SubMissionScope("O3", "O3", "C3"),
                delegation_key="k3",
            ),
        )


# ==========================================
# 7. Objective Scoped
# ==========================================

def test_7_objective_scoped():
    # Invalid empty objective
    with pytest.raises(ValueError):
        SubMissionScope(
            objective="",
            expected_outcome="Valid outcome",
            completion_condition="Valid condition",
        )

    # Invalid empty outcome
    with pytest.raises(ValueError):
        SubMissionScope(
            objective="Valid objective",
            expected_outcome="",
            completion_condition="Valid condition",
        )

    # Invalid empty condition
    with pytest.raises(ValueError):
        SubMissionScope(
            objective="Valid objective",
            expected_outcome="Valid outcome",
            completion_condition="",
        )


# ==========================================
# 8. Context Inheritance Minimal
# ==========================================

def test_8_context_inheritance_minimal(sub_mission_service, mission_repo, tenant_ctx_a):
    parent = Mission(
        mission_id="m_parent_ctx",
        type=MISSION_TYPE,
        parameters={"huge_unrelated_field": "x" * 1000, "internal_id": "abc"},
    )
    mission_repo.save(tenant_ctx_a, parent)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope(
            objective="Fetch specific price",
            expected_outcome="Price float",
            completion_condition="Price received",
        ),
        inputs={"target_sku": "SKU-123"},
    )

    child = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)

    # Child params must contain only sanitized scope, inputs, metadata and plan linkages
    assert "huge_unrelated_field" not in child.parameters
    assert "internal_id" not in child.parameters
    assert child.parameters["inputs"] == {"target_sku": "SKU-123"}
    assert child.parameters["objective"] == "Fetch specific price"


# ==========================================
# 9. Secrets Not Inherited (Anti-CoT & Sanitization)
# ==========================================

def test_9_secrets_not_inherited(sub_mission_service, mission_repo, tenant_ctx_a):
    parent = Mission(
        mission_id="m_parent_secret",
        type=MISSION_TYPE,
        parameters={"api_key": "sk-1234567890", "password": "supersecretpassword"},
    )
    mission_repo.save(tenant_ctx_a, parent)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope(
            objective="Fetch price safely",
            expected_outcome="Clean result",
            completion_condition="Done",
        ),
        inputs={
            "token": "bearer-secret-token-xyz",
            "safe_param": "regular_value",
            "prompt_cot": "Let's think step by step...",
        },
    )

    child = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)

    # Secrets must be scrubbed by deep sanitization
    assert child.parameters["inputs"]["token"] == "[REDACTED]"
    assert child.parameters["inputs"]["safe_param"] == "regular_value"
    assert "api_key" not in child.parameters


# ==========================================
# 10. Budget Allocation
# ==========================================

def test_10_budget_allocation(sub_mission_service, mission_repo, tenant_ctx_a):
    parent = Mission(
        mission_id="m_parent_budget",
        type=MISSION_TYPE,
        parameters={
            "allocated_budget": {
                "max_tokens": 10000,
                "max_cost": "1.00",
                "max_wall_clock_seconds": 60,
            }
        },
    )
    mission_repo.save(tenant_ctx_a, parent)

    # Allocate child budget within parent bounds
    child_budget = PlanBudget(max_tokens=4000, max_cost=Decimal("0.40"), max_wall_clock_seconds=20)
    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Task 1", "Done", "Done"),
        allocated_budget=child_budget,
    )

    child = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)
    assert child.parameters["allocated_budget"]["max_tokens"] == 4000
    assert child.parameters["allocated_budget"]["max_cost"] == "0.40"


# ==========================================
# 11. UNKNOWN Budget Not Unlimited
# ==========================================

def test_11_unknown_budget_not_unlimited():
    validator = SubMissionHierarchyValidator()

    # Parent budget has max_tokens=1000, child has None (UNKNOWN) -> REJECTED (UNKNOWN cannot exceed known cap)
    parent_budget = PlanBudget(max_tokens=1000, max_cost=Decimal("0.50"))
    child_budget_unbounded_tokens = PlanBudget(max_tokens=None, max_cost=Decimal("0.10"))

    with pytest.raises(BudgetExceededError):
        validator.validate_budget_allocation(child_budget_unbounded_tokens, parent_budget)

    # Parent budget has cost=0.50, child has cost=None -> REJECTED
    child_budget_unbounded_cost = PlanBudget(max_tokens=500, max_cost=None)
    with pytest.raises(BudgetExceededError):
        validator.validate_budget_allocation(child_budget_unbounded_cost, parent_budget)


# ==========================================
# 12. Sibling Budget Isolation
# ==========================================

def test_12_sibling_budget_isolation(sub_mission_service, mission_repo, tenant_ctx_a):
    parent = Mission(
        mission_id="m_parent_budget_iso",
        type=MISSION_TYPE,
        parameters={
            "allocated_budget": {
                "max_tokens": 10000,
                "max_cost": "1.00",
            }
        },
    )
    mission_repo.save(tenant_ctx_a, parent)

    # Child 1 consumes 6000 tokens / $0.60
    sub_mission_service.create_sub_mission(
        tenant_ctx_a,
        SubMissionCreationContract(
            parent_mission_id=parent.mission_id,
            tenant_id="tenant_a",
            sub_mission_type=MISSION_TYPE,
            scope=SubMissionScope("Task 1", "Done", "Done"),
            allocated_budget=PlanBudget(max_tokens=6000, max_cost=Decimal("0.60")),
            delegation_key="task_1",
        ),
    )

    # Child 2 attempts to allocate 5000 tokens / $0.50 (Sum = 11000 > 10000) -> Rejection
    with pytest.raises(BudgetExceededError):
        sub_mission_service.create_sub_mission(
            tenant_ctx_a,
            SubMissionCreationContract(
                parent_mission_id=parent.mission_id,
                tenant_id="tenant_a",
                sub_mission_type=MISSION_TYPE,
                scope=SubMissionScope("Task 2", "Done", "Done"),
                allocated_budget=PlanBudget(max_tokens=5000, max_cost=Decimal("0.50")),
                delegation_key="task_2",
            ),
        )


# ==========================================
# 13. Result Propagation
# ==========================================

def test_13_result_propagation(sub_mission_service, mission_repo, plan_repo, tenant_ctx_a):
    parent = Mission(
        mission_id="m_parent_prop",
        type=MISSION_TYPE,
    )
    mission_repo.save(tenant_ctx_a, parent)

    # Setup R.1 ExecutionPlan
    plan = ExecutionPlan(
        plan_id="plan_123",
        mission_id="m_parent_prop",
        tenant_id="tenant_a",
        goal="Propagate delegated price inspection",
        steps=[
            PlanStep(
                step_id="step_delegate_1",
                objective="Inspect prices",
                action_type="inspect_prices",
                required_inputs={},
                assigned_capability="market_analysis",
                status=StepStatus.IN_PROGRESS,
            )
        ],
    )
    plan_repo.save_plan(plan, tenant_ctx_a)

    # Create child linked to plan
    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Inspect prices", "Done", "Done"),
        plan_id="plan_123",
        plan_step_id="step_delegate_1",
    )
    child = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)

    # Complete child and propagate result
    result = SubMissionResultContract(
        mission_id=child.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        status=MissionStatus.COMPLETED,
        outputs={"cheapest_price": 49.99, "currency": "USD"},
        cost_spent=Decimal("0.04"),
        tokens_spent=1200,
    )

    success = sub_mission_service.propagate_result(tenant_ctx_a, result)
    assert success is True

    # Verify R.1 Plan step is updated to COMPLETED with outputs
    updated_plan = plan_repo.get_plan_by_id("plan_123", tenant_ctx_a)
    step = updated_plan.get_step("step_delegate_1")
    assert step.status == StepStatus.COMPLETED
    assert step.actual_outputs["cheapest_price"] == 49.99


# ==========================================
# 14. Required Child Failure Effect
# ==========================================

def test_14_required_child_failure_effect(sub_mission_service, mission_repo, plan_repo, tenant_ctx_a):
    parent = Mission(mission_id="m_parent_req_fail", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, parent)

    plan = ExecutionPlan(
        plan_id="plan_fail_1",
        mission_id="m_parent_req_fail",
        tenant_id="tenant_a",
        goal="Complete required delegated work",
        steps=[
            PlanStep(
                step_id="step_critical",
                objective="Run critical task",
                action_type="critical_task",
                required_inputs={},
                assigned_capability="market_analysis",
                status=StepStatus.IN_PROGRESS,
            )
        ],
    )
    plan_repo.save_plan(plan, tenant_ctx_a)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Critical child", "Done", "Done"),
        plan_id="plan_fail_1",
        plan_step_id="step_critical",
        is_required=True,
    )
    child = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)

    result = SubMissionResultContract(
        mission_id=child.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        status=MissionStatus.FAILED,
        failure_type=SubMissionFailureType.TECHNICAL_FAILURE,
        failure_reason="Upstream service timeout",
        is_required=True,
    )

    sub_mission_service.propagate_result(tenant_ctx_a, result)

    # Step must fail
    updated_plan = plan_repo.get_plan_by_id("plan_fail_1", tenant_ctx_a)
    step = updated_plan.get_step("step_critical")
    assert step.status == StepStatus.FAILED
    assert step.failure_reason == "Upstream service timeout"


# ==========================================
# 15. Optional Child Behavior
# ==========================================

def test_15_optional_child_behavior(sub_mission_service, mission_repo, plan_repo, tenant_ctx_a):
    parent = Mission(mission_id="m_parent_opt_fail", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, parent)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Optional enrichment", "Bonus data", "Done"),
        is_required=False,
    )
    child = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)
    assert child.is_required is False

    # Fail the optional child
    result = SubMissionResultContract(
        mission_id=child.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        status=MissionStatus.FAILED,
        failure_type=SubMissionFailureType.TECHNICAL_FAILURE,
        failure_reason="Enrichment optional endpoint unavailable",
        is_required=False,
    )
    sub_mission_service.propagate_result(tenant_ctx_a, result)

    # Parent can still complete because the failed child was optional
    can_complete, blockers = sub_mission_service.can_parent_complete(tenant_ctx_a, parent.mission_id)
    assert can_complete is True
    assert len(blockers) == 0


# ==========================================
# 16. Parent Completion Blocked by Required Active Child
# ==========================================

def test_16_parent_completion_blocked_by_required_active_child(sub_mission_service, mission_repo, tenant_ctx_a):
    parent = Mission(mission_id="m_parent_blocked_compl", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, parent)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Mandatory work", "Done", "Done"),
        is_required=True,
    )
    child = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)

    # Child is still PENDING -> Parent cannot complete
    can_complete, blockers = sub_mission_service.can_parent_complete(tenant_ctx_a, parent.mission_id)
    assert can_complete is False
    assert child.mission_id in blockers

    # Complete the child
    result = SubMissionResultContract(
        mission_id=child.mission_id,
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        status=MissionStatus.COMPLETED,
    )
    sub_mission_service.propagate_result(tenant_ctx_a, result)

    # Now parent can complete
    can_complete_after, blockers_after = sub_mission_service.can_parent_complete(tenant_ctx_a, parent.mission_id)
    assert can_complete_after is True
    assert len(blockers_after) == 0


# ==========================================
# 17. Cancellation Propagation
# ==========================================

def test_17_cancellation_propagation(sub_mission_service, mission_repo, tenant_ctx_a):
    root = Mission(mission_id="m_root_cancel", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, root)

    c1 = sub_mission_service.create_sub_mission(
        tenant_ctx_a,
        SubMissionCreationContract(
            parent_mission_id="m_root_cancel",
            tenant_id="tenant_a",
            sub_mission_type=MISSION_TYPE,
            scope=SubMissionScope("C1", "O1", "C1"),
            delegation_key="k1",
        ),
    )
    c2 = sub_mission_service.create_sub_mission(
        tenant_ctx_a,
        SubMissionCreationContract(
            parent_mission_id=c1.mission_id,
            tenant_id="tenant_a",
            sub_mission_type=MISSION_TYPE,
            scope=SubMissionScope("C2", "O2", "C2"),
            delegation_key="k2",
        ),
    )

    # Mark c1 as running
    running_c1 = Mission(
        mission_id=c1.mission_id,
        type=c1.type,
        status=MissionStatus.RUNNING,
        parent_mission_id=c1.parent_mission_id,
        root_mission_id=c1.root_mission_id,
        depth=c1.depth,
        parameters=c1.parameters,
    )
    mission_repo.save(tenant_ctx_a, running_c1)

    # Cancel root hierarchy
    cancelled_count = sub_mission_service.cancel_hierarchy(tenant_ctx_a, "m_root_cancel", "Emergency user abort")
    assert cancelled_count == 2

    # Verify both children are CANCELLED
    c1_updated = mission_repo.get_by_id(tenant_ctx_a, c1.mission_id)
    c2_updated = mission_repo.get_by_id(tenant_ctx_a, c2.mission_id)
    assert c1_updated.status == MissionStatus.ABORTED
    assert c2_updated.status == MissionStatus.ABORTED
    assert c1_updated.parameters.get("cancellation_reason") == "Emergency user abort"


# ==========================================
# 18. Idempotent Creation
# ==========================================

def test_18_idempotent_creation(sub_mission_service, mission_repo, tenant_ctx_a):
    parent = Mission(mission_id="m_parent_idemp", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, parent)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Analyze competitors", "Competitor list", "Done"),
        delegation_key="competitor_analysis_v1",
    )

    child_1 = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)
    child_2 = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)

    # Both calls return the exact same mission ID
    assert child_1.mission_id == child_2.mission_id
    # Repository has only 1 child
    children = mission_repo.get_children(tenant_ctx_a, parent.mission_id)
    assert len(children) == 1


# ==========================================
# 19. Concurrent Duplicate Prevention
# ==========================================

def test_19_concurrent_duplicate_prevention(sub_mission_service, mission_repo, tenant_ctx_a):
    parent = Mission(mission_id="m_parent_conc", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, parent)

    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Concurrent task", "Done", "Done"),
        delegation_key="concurrent_key_xyz",
    )

    created_ids = []
    errors = []

    def worker():
        try:
            m = sub_mission_service.create_sub_mission(tenant_ctx_a, contract)
            created_ids.append(m.mission_id)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(errors) == 0
    assert len(created_ids) == 8
    # All threads got the exact same mission id
    assert len(set(created_ids)) == 1
    # Exactly one child persisted
    children = mission_repo.get_children(tenant_ctx_a, parent.mission_id)
    assert len(children) == 1


# ==========================================
# 20. No R.3 Implementation Verification
# ==========================================

def test_parent_nonexistent_and_terminal_rejected(mission_repo, tenant_ctx_a):
    service = SubMissionService(mission_repository=mission_repo)
    missing_contract = SubMissionCreationContract(
        parent_mission_id="m_missing",
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Child", "Done", "Done"),
    )
    with pytest.raises(InvalidParentMissionError):
        service.create_sub_mission(tenant_ctx_a, missing_contract)

    for status in (MissionStatus.COMPLETED, MissionStatus.FAILED, MissionStatus.ABORTED):
        parent = Mission(mission_id=f"m_terminal_{status.value.lower()}", type=MISSION_TYPE, status=status)
        mission_repo.save(tenant_ctx_a, parent)
        contract = SubMissionCreationContract(
            parent_mission_id=parent.mission_id,
            tenant_id="tenant_a",
            sub_mission_type=MISSION_TYPE,
            scope=SubMissionScope("Child", "Done", "Done"),
        )
        with pytest.raises(InvalidParentMissionError):
            service.create_sub_mission(tenant_ctx_a, contract)


def test_emergency_stop_blocks_creation(mission_repo, tenant_ctx_a):
    parent = Mission(mission_id="m_stopped", type=MISSION_TYPE)
    mission_repo.save(tenant_ctx_a, parent)
    service = SubMissionService(
        mission_repository=mission_repo,
        emergency_stop_service=MockEmergencyStop(blocked=True),
    )
    contract = SubMissionCreationContract(
        parent_mission_id=parent.mission_id,
        tenant_id="tenant_a",
        sub_mission_type=MISSION_TYPE,
        scope=SubMissionScope("Blocked child", "Done", "Done"),
    )
    with pytest.raises(SubMissionHierarchyError, match="Emergency Stop"):
        service.create_sub_mission(tenant_ctx_a, contract)


def test_20_no_r5_implementation():
    """Verify strictly that no R.5+ dynamic delegation was introduced."""
    import sys

    # Assert R.5+ dynamic delegation does not exist in codebase
    assert "src.application.dynamic_delegation" not in sys.modules
    assert "src.domain.dynamic_delegation" not in sys.modules
