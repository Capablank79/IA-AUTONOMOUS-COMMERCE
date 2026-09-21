"""
Unit tests for R.1 Multi-step Planning (Hito R — Advanced Autonomy).

Covers:
1. simple decomposition
2. hierarchical decomposition
3. valid DAG
4. cycle rejected
5. missing dependency rejected
6. deterministic topological order
7. readiness requires dependencies
8. independent steps parallel-ready
9. capability unavailable
10. budget allocation
11. budget overflow rejected
12. UNKNOWN cost preserved
13. replan after technical failure
14. completed step preserved
15. policy denial not bypassed
16. max replan bound
17. plan version increment
18. tenant scoping
19. Anti-CoT projection
20. no R.2+
"""

from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.domain.tenant.models import TenantContext, TenantId, CrossTenantAccessError
from src.domain.planning.models import (
    ExecutionPlan,
    PlanStep,
    StepDependency,
    PlanBudget,
    PlanStatus,
    StepStatus,
    StepFailureType,
    DependencyType,
    StepRationale,
)
from src.domain.planning.validator import (
    PlanValidator,
    PlanValidationError,
    PlanCycleDetectedError,
)
from src.domain.planning.ports import CapabilityRegistryPort
from src.infrastructure.persistence.data.json.execution_plan_repository import (
    InMemoryExecutionPlanRepository,
)
from src.application.planning.multi_step_planning_service import (
    MultiStepPlanningService,
)


class MockCapabilityRegistry(CapabilityRegistryPort):
    def __init__(self, available_capabilities=None):
        self.available = set(available_capabilities or [
            "search_market_trends",
            "evaluate_margins",
            "generate_listing_copy",
            "publish_listing",
            "verify_compliance",
        ])

    def is_capability_available(self, capability_name: str) -> bool:
        return capability_name in self.available

    def get_known_capabilities(self):
        return tuple(sorted(self.available))


@pytest.fixture
def tenant_a():
    return TenantContext(
        tenant_id="tenant-alpha",
        identity_id="user-123",
        metadata={"roles": ("admin",)},
    )


@pytest.fixture
def tenant_b():
    return TenantContext(
        tenant_id="tenant-beta",
        identity_id="user-456",
        metadata={"roles": ("operator",)},
    )


@pytest.fixture
def capability_registry():
    return MockCapabilityRegistry()


@pytest.fixture
def planning_service(capability_registry):
    repo = InMemoryExecutionPlanRepository()
    return MultiStepPlanningService(
        plan_repository=repo,
        capability_registry=capability_registry,
    )


# 1. Simple decomposition
def test_simple_decomposition(planning_service, tenant_a):
    steps = [
        PlanStep(
            step_id="step_1",
            objective="Analyze market trends",
            action_type="search_market_trends",
        ),
        PlanStep(
            step_id="step_2",
            objective="Evaluate product margins",
            action_type="evaluate_margins",
            dependencies=("step_1",),
        ),
    ]
    res = planning_service.create_plan(
        mission_id="m-101",
        goal="Launch product catalog analysis",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.success is True
    assert res.plan is not None
    assert res.status == PlanStatus.VALIDATED
    assert len(res.plan.steps) == 2
    assert res.ready_step_ids == ("step_1",)


# 2. Hierarchical decomposition
def test_hierarchical_decomposition(planning_service, tenant_a):
    steps = [
        PlanStep(
            step_id="subgoal_market_research",
            objective="Perform market research",
            action_type="search_market_trends",
            depth_level=1,
        ),
        PlanStep(
            step_id="leaf_step_eval_margin",
            objective="Evaluate profit margins",
            action_type="evaluate_margins",
            parent_goal_id="subgoal_market_research",
            depth_level=2,
            dependencies=("subgoal_market_research",),
        ),
        PlanStep(
            step_id="leaf_step_generate_copy",
            objective="Generate listing copy",
            action_type="generate_listing_copy",
            parent_goal_id="subgoal_market_research",
            depth_level=2,
            dependencies=("leaf_step_eval_margin",),
        ),
    ]
    res = planning_service.create_plan(
        mission_id="m-102",
        goal="Hierarchical publication mission",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.success is True
    assert res.plan.steps[1].parent_goal_id == "subgoal_market_research"
    assert res.plan.steps[1].depth_level == 2


# 3. Valid DAG
def test_valid_dag(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends"),
        PlanStep(step_id="s2", objective="Step 2", action_type="evaluate_margins", dependencies=("s1",)),
        PlanStep(step_id="s3", objective="Step 3", action_type="generate_listing_copy", dependencies=("s1",)),
        PlanStep(step_id="s4", objective="Step 4", action_type="publish_listing", dependencies=("s2", "s3")),
    ]
    res = planning_service.create_plan(
        mission_id="m-103",
        goal="Diamond DAG Plan",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.success is True
    is_valid, errors, topo_order = PlanValidator.validate_dag(res.plan)
    assert is_valid is True
    assert len(errors) == 0
    assert topo_order == ["s1", "s2", "s3", "s4"]


# 4. Cycle rejected
def test_cycle_rejected(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends", dependencies=("s3",)),
        PlanStep(step_id="s2", objective="Step 2", action_type="evaluate_margins", dependencies=("s1",)),
        PlanStep(step_id="s3", objective="Step 3", action_type="generate_listing_copy", dependencies=("s2",)),
    ]
    res = planning_service.create_plan(
        mission_id="m-104",
        goal="Cyclic Goal",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.success is False
    assert res.status == PlanStatus.REJECTED
    assert any("Cycle detected" in err for err in res.errors)


# 5. Missing dependency rejected
def test_missing_dependency_rejected(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends", dependencies=("non_existent_step",)),
    ]
    res = planning_service.create_plan(
        mission_id="m-105",
        goal="Missing dep",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.success is False
    assert res.status == PlanStatus.REJECTED
    assert any("non-existent dependency" in err for err in res.errors)


# 6. Deterministic topological order
def test_deterministic_topological_order():
    # s_c, s_b, s_a have in-degree 0. Kahn tie-breaking must return alphabetical order s_a, s_b, s_c.
    steps = [
        PlanStep(step_id="s_c", objective="Step C", action_type="search_market_trends"),
        PlanStep(step_id="s_b", objective="Step B", action_type="evaluate_margins"),
        PlanStep(step_id="s_a", objective="Step A", action_type="generate_listing_copy"),
        PlanStep(step_id="s_d", objective="Step D", action_type="publish_listing", dependencies=("s_c", "s_a")),
    ]
    plan = ExecutionPlan(
        plan_id="plan-topo",
        mission_id="m-topo",
        tenant_id="tenant-alpha",
        goal="Test deterministic topo",
        steps=tuple(steps),
    )
    is_valid, errors, topo_order = PlanValidator.validate_dag(plan)
    assert is_valid is True
    assert topo_order == ["s_a", "s_b", "s_c", "s_d"]


# 7. Readiness requires dependencies
def test_readiness_requires_dependencies(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends"),
        PlanStep(step_id="s2", objective="Step 2", action_type="evaluate_margins", dependencies=("s1",)),
    ]
    res = planning_service.create_plan(
        mission_id="m-107",
        goal="Readiness progression",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.ready_step_ids == ("s1",)

    # Mark s1 as completed -> now s2 should become ready
    updated_plan = planning_service.mark_step_completed(
        plan_id=res.plan.plan_id,
        step_id="s1",
        outputs={"trend": "growing"},
        tenant_context=tenant_a,
    )
    ready = planning_service.get_ready_steps(updated_plan, tenant_a)
    assert len(ready) == 1
    assert ready[0].step_id == "s2"


# 8. Independent steps parallel-ready
def test_independent_steps_parallel_ready(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="branch_a", objective="Branch A", action_type="search_market_trends"),
        PlanStep(step_id="branch_b", objective="Branch B", action_type="evaluate_margins"),
        PlanStep(step_id="join_step", objective="Join", action_type="publish_listing", dependencies=("branch_a", "branch_b")),
    ]
    res = planning_service.create_plan(
        mission_id="m-108",
        goal="Parallel readiness",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.ready_step_ids == ("branch_a", "branch_b")


# 9. Capability unavailable
def test_capability_unavailable(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="s1", objective="Perform forbidden magic", action_type="unknown_alien_capability"),
    ]
    res = planning_service.create_plan(
        mission_id="m-109",
        goal="Unavailable tool",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.success is False
    assert res.status == PlanStatus.REJECTED
    assert any("unregistered capability" in err for err in res.errors)


# 10. Budget allocation
def test_budget_allocation(planning_service, tenant_a):
    budget = PlanBudget(
        max_tokens=1000,
        max_cost=Decimal("15.00"),
        max_steps=5,
        max_replans=2,
    )
    steps = [
        PlanStep(
            step_id="s1",
            objective="Step 1",
            action_type="search_market_trends",
            estimated_cost=Decimal("5.00"),
            estimated_tokens=300,
        ),
        PlanStep(
            step_id="s2",
            objective="Step 2",
            action_type="evaluate_margins",
            estimated_cost=Decimal("8.00"),
            estimated_tokens=500,
            dependencies=("s1",),
        ),
    ]
    res = planning_service.create_plan(
        mission_id="m-110",
        goal="Budgeted plan",
        tenant_context=tenant_a,
        initial_steps=steps,
        budget=budget,
    )
    assert res.success is True
    assert res.plan.budget.max_cost == Decimal("15.00")
    total_cost, is_unknown = res.plan.get_total_estimated_cost()
    assert total_cost == Decimal("13.00")
    assert is_unknown is False


# 11. Budget overflow rejected
def test_budget_overflow_rejected(planning_service, tenant_a):
    budget = PlanBudget(
        max_tokens=500,
        max_cost=Decimal("10.00"),
    )
    steps = [
        PlanStep(
            step_id="s1",
            objective="Step 1",
            action_type="search_market_trends",
            estimated_cost=Decimal("8.00"),
            estimated_tokens=300,
        ),
        PlanStep(
            step_id="s2",
            objective="Step 2",
            action_type="evaluate_margins",
            estimated_cost=Decimal("5.00"),  # Total 13.00 > 10.00
            estimated_tokens=300,
        ),
    ]
    res = planning_service.create_plan(
        mission_id="m-111",
        goal="Over budget plan",
        tenant_context=tenant_a,
        initial_steps=steps,
        budget=budget,
    )
    assert res.success is False
    assert res.status == PlanStatus.REJECTED
    assert any("exceeds plan max_cost limit" in err for err in res.errors)


# 12. UNKNOWN cost preserved
def test_unknown_cost_preserved(planning_service, tenant_a):
    steps = [
        PlanStep(
            step_id="s1",
            objective="Step 1",
            action_type="search_market_trends",
            estimated_cost=Decimal("5.00"),
        ),
        PlanStep(
            step_id="s2",
            objective="Step 2 with unknown cost",
            action_type="evaluate_margins",
            is_cost_unknown=True,
            estimated_cost=None,
        ),
    ]
    res = planning_service.create_plan(
        mission_id="m-112",
        goal="Unknown cost plan",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    assert res.success is True
    total_cost, is_unknown = res.plan.get_total_estimated_cost()
    assert is_unknown is True
    assert total_cost is None  # UNKNOWN does not become 0


# 13. Replan after technical failure
def test_replan_after_technical_failure(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends"),
        PlanStep(step_id="s2", objective="Step 2", action_type="evaluate_margins", dependencies=("s1",)),
    ]
    res = planning_service.create_plan(
        mission_id="m-113",
        goal="Technical recovery mission",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    # Mark s1 completed
    planning_service.mark_step_completed(res.plan.plan_id, "s1", {"data": 1}, tenant_a)

    # s2 fails due to network glitch (EXECUTION_FAILURE)
    replan_res = planning_service.replan(
        plan_id=res.plan.plan_id,
        tenant_context=tenant_a,
        failed_step_id="s2",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="Network timeout connecting to supplier",
    )
    assert replan_res.success is True
    assert replan_res.plan.version == 2
    assert replan_res.plan.replan_count == 1
    assert len(replan_res.plan.history) == 1
    assert replan_res.plan.history[0].replan_reason == "Network timeout connecting to supplier"


# 14. Completed step preserved
def test_completed_step_preserved_on_replan(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends"),
        PlanStep(step_id="s2", objective="Step 2", action_type="evaluate_margins", dependencies=("s1",)),
    ]
    res = planning_service.create_plan(
        mission_id="m-114",
        goal="Preserve completed",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    planning_service.mark_step_completed(res.plan.plan_id, "s1", {"res": "ok"}, tenant_a)

    replan_res = planning_service.replan(
        plan_id=res.plan.plan_id,
        tenant_context=tenant_a,
        failed_step_id="s2",
        failure_type=StepFailureType.EXECUTION_FAILURE,
        failure_reason="Transient error",
    )
    s1 = replan_res.plan.get_step("s1")
    assert s1.status == StepStatus.COMPLETED
    assert s1.actual_outputs == {"res": "ok"}
    assert "s1" in replan_res.plan.history[0].preserved_step_ids


# 15. Policy denial not bypassed
def test_policy_denial_not_bypassed(planning_service, tenant_a):
    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends"),
    ]
    res = planning_service.create_plan(
        mission_id="m-115",
        goal="Policy violation mission",
        tenant_context=tenant_a,
        initial_steps=steps,
    )
    replan_res = planning_service.replan(
        plan_id=res.plan.plan_id,
        tenant_context=tenant_a,
        failed_step_id="s1",
        failure_type=StepFailureType.POLICY_DENIED,
        failure_reason="Action denied by security boundary policy",
    )
    assert replan_res.success is False
    assert replan_res.status == PlanStatus.BLOCKED
    assert "Cannot bypass governance" in replan_res.errors[0]
    # Version should not increment as replan is disallowed
    assert replan_res.plan.version == 1
    assert replan_res.plan.status == PlanStatus.BLOCKED


# 16. Max replan bound
def test_max_replan_bound(planning_service, tenant_a):
    budget = PlanBudget(max_replans=2)
    steps = [
        PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends"),
    ]
    res = planning_service.create_plan(
        mission_id="m-116",
        goal="Bounded replanning mission",
        tenant_context=tenant_a,
        initial_steps=steps,
        budget=budget,
    )
    # Replan 1
    r1 = planning_service.replan(
        res.plan.plan_id, tenant_a, "s1", StepFailureType.EXECUTION_FAILURE, "Err 1"
    )
    assert r1.success is True
    assert r1.plan.replan_count == 1

    # Replan 2
    r2 = planning_service.replan(
        res.plan.plan_id, tenant_a, "s1", StepFailureType.EXECUTION_FAILURE, "Err 2"
    )
    assert r2.success is True
    assert r2.plan.replan_count == 2

    # Replan 3 -> exceeds max_replans=2 -> should fail safely
    r3 = planning_service.replan(
        res.plan.plan_id, tenant_a, "s1", StepFailureType.EXECUTION_FAILURE, "Err 3"
    )
    assert r3.success is False
    assert r3.status == PlanStatus.FAILED
    assert "Maximum replan limit" in r3.errors[0]


# 17. Plan version increment
def test_plan_version_increment(planning_service, tenant_a):
    steps = [PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends")]
    res = planning_service.create_plan("m-117", "Version test", tenant_a, steps)
    assert res.plan.version == 1

    r1 = planning_service.replan(res.plan.plan_id, tenant_a, "s1", StepFailureType.EXECUTION_FAILURE, "E1")
    assert r1.plan.version == 2


# 18. Tenant scoping
def test_tenant_scoping(planning_service, tenant_a, tenant_b):
    steps = [PlanStep(step_id="s1", objective="Step 1", action_type="search_market_trends")]
    res = planning_service.create_plan("m-118", "Tenant test", tenant_a, steps)
    plan_id = res.plan.plan_id

    # Tenant B tries to access or replan Tenant A's plan
    with pytest.raises(CrossTenantAccessError):
        planning_service.get_ready_steps(res.plan, tenant_b)


# 19. Anti-CoT projection
def test_anti_cot_projection():
    rationale = StepRationale(
        objective="Find profitable items",
        dependency_reason="Needs pricing data",
        capability_selected="search_market_trends",
        budget_reason="Within 5 USD limit",
    )
    step = PlanStep(
        step_id="step_safe",
        objective="Find items",
        action_type="search_market_trends",
        rationale=rationale,
    )
    # Check that rationale exists as structured metadata and does not contain chain-of-thought tokens
    assert step.rationale.objective == "Find profitable items"
    assert "Let's think step by step" not in step.rationale.objective


# 20. No R.2+ references in models
def test_no_r2_plus_references():
    import src.domain.planning.models as models
    for name in dir(models):
        assert not name.startswith("R2_"), f"Found forbidden R.2+ reference: {name}"
        assert not name.startswith("R3_"), f"Found forbidden R.3+ reference: {name}"
