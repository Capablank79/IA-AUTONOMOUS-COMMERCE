from decimal import Decimal

from src.application.specialist_agent.service import SpecialistAgentService
from src.domain.audit.models import AuditRecordType
from src.domain.mission.models import LoopDecision, LoopState, Mission, MissionStatus, MissionType
from src.domain.mission.ports import ActionExecutor
from src.domain.planning.models import PlanStep
from src.domain.specialist_agent.models import (
    AgentAvailability, AgentCapability, AgentCapabilityContract, AgentExecutionContext,
    AgentExecutionFailureType, AgentExecutionStatus, AgentSelectionStatus, SpecialistAgentDefinition,
)
from src.domain.specialist_agent.registry import SpecialistAgentRegistry
from src.domain.tenant.models import TenantContext
from src.domain.tool.models import ToolContract, ToolDescriptor, ToolLifecycleStatus, ToolSchemaField, ToolSideEffectLevel, ToolVersion
from src.domain.tool.registry import ToolRegistry


class BoundaryExecutor(ActionExecutor):
    is_guarded_executor = True

    def __init__(self, status="SUCCESS"):
        self.status = status; self.calls = 0; self.decisions = []

    def execute(self, decision: LoopDecision, state: LoopState) -> dict:
        self.calls += 1; self.decisions.append(decision)
        if self.status == "RAISE":
            raise RuntimeError("secret=must-not-leak")
        return {"status": self.status, "outputs": {"items": ["safe"]}, "cost_used": "1.5", "tokens_used": 12}


class AuditRepo:
    def __init__(self): self.records = []
    def append(self, record): self.records.append(record); return record


class TraceService:
    def __init__(self): self.records = []
    def record_step(self, **kwargs): self.records.append(kwargs)


class PropagationService:
    def __init__(self): self.calls = []
    def propagate_result(self, context, contract): self.calls.append((context, contract)); return True


def setup(status="SUCCESS", agents=None, audit=None, trace=None, usage_hook=None):
    ic = ToolContract("input", (ToolSchemaField("query", "str"),), allow_extra_fields=False)
    oc = ToolContract("output", (ToolSchemaField("items", "list"),), allow_extra_fields=False)
    tool = ToolDescriptor("search", "Search", ToolVersion("v1"), "safe fake", "MARKET_RESEARCH", ic, oc, ToolSideEffectLevel.READ_ONLY, status=ToolLifecycleStatus.AVAILABLE)
    tools = ToolRegistry(); tools.register(tool)
    registry = SpecialistAgentRegistry(tools, ("SEARCH",))
    cap = AgentCapability("MARKET_RESEARCH", AgentCapabilityContract(ic, oc), ("SEARCH",), ("search",))
    agents = agents or [SpecialistAgentDefinition("market-agent", "tenant-a", (cap,), AgentAvailability.AVAILABLE, ("SEARCH",), ("search",), Decimal("2"), 10, executor_key="market-executor")]
    executors = {}
    for agent in agents:
        registry.register(agent)
        executors.setdefault(agent.executor_key, BoundaryExecutor(status))
    service = SpecialistAgentService(registry, executors, audit, trace, usage_hook=usage_hook)
    return service, executors, cap


def ctx(**kwargs):
    data = dict(tenant_id="tenant-a", mission_id="mission-a", capability_id="MARKET_RESEARCH", action_type="SEARCH", inputs={"query": "shoes"}, idempotency_key="idem-a", correlation_id="corr-a", tool_id="search", budget_remaining=Decimal("4"))
    data.update(kwargs); return AgentExecutionContext(**data)


def test_scenario_a_registry_binding_contract_and_successful_guarded_execution():
    service, executors, _ = setup(); result = service.execute(ctx())
    executor = executors["market-executor"]
    assert result.status == AgentExecutionStatus.SUCCESS and executor.calls == 1
    assert executor.decisions[0].parameters["specialist_agent_id"] == "market-agent"


def test_scenario_b_assigned_eligible_candidate_need_not_be_global_winner():
    _, _, cap = setup()
    assigned = SpecialistAgentDefinition("assigned", "tenant-a", (cap,), AgentAvailability.AVAILABLE, ("SEARCH",), ("search",), Decimal("2"), 99, executor_key="assigned-ex")
    winner = SpecialistAgentDefinition("winner", "tenant-a", (cap,), AgentAvailability.AVAILABLE, ("SEARCH",), ("search",), Decimal("1"), 1, executor_key="winner-ex")
    service, executors, _ = setup(agents=[winner, assigned])
    result = service.execute(ctx(), "assigned")
    assert result.agent_id == "assigned" and executors["assigned-ex"].calls == 1 and executors["winner-ex"].calls == 0


def test_scenario_c_unknown_availability_and_unknown_cost_fail_safe():
    _, _, cap = setup()
    unknown = SpecialistAgentDefinition("unknown", "tenant-a", (cap,), AgentAvailability.UNKNOWN, ("SEARCH",), ("search",), None, executor_key="unknown-ex")
    service, _, _ = setup(agents=[unknown])
    assert service.select_agent("tenant-a", "MARKET_RESEARCH", max_cost=Decimal("100")).status == AgentSelectionStatus.BLOCKED


def test_scenario_d_tenant_isolation_blocks_cross_tenant_execution():
    service, executors, _ = setup()
    result = service.execute(ctx(tenant_id="tenant-b"), selected_agent_id="market-agent")
    assert result.status == AgentExecutionStatus.BLOCKED and executors["market-executor"].calls == 0


def test_scenario_e_tool_action_and_budget_prevent_side_effect_before_boundary():
    service, executors, _ = setup(); executor = executors["market-executor"]
    assert service.execute(ctx(tool_id="publish", idempotency_key="tool")).failure_reason == "TOOL_NOT_ALLOWED"
    assert service.execute(ctx(action_type="PUBLISH", idempotency_key="action")).failure_reason == "ACTION_NOT_ALLOWED"
    assert service.execute(ctx(budget_remaining=Decimal("1"), idempotency_key="budget")).failure_type == AgentExecutionFailureType.BUDGET_EXHAUSTED
    assert executor.calls == 0


def test_scenario_f_policy_denied_is_structured_and_never_retried():
    service, executors, _ = setup("POLICY_DENIED"); executor = executors["market-executor"]
    first = service.execute(ctx()); second = service.execute(ctx())
    assert first.failure_type == AgentExecutionFailureType.POLICY_DENIED and second is first and executor.calls == 1


def test_scenario_g_r1_contract_uses_explicit_budget_not_step_estimate():
    service, executors, _ = setup(); executor = executors["market-executor"]
    step = PlanStep("step-a", "Research", "SEARCH", required_inputs={"query": "shoes"}, assigned_capability="MARKET_RESEARCH", metadata={"tool_id": "search"}, estimated_cost=Decimal("99"))
    blocked = service.execute_plan_step(step, TenantContext("tenant-a", correlation_id="corr-step"), "mission-a", "idem-low", budget_remaining=Decimal("1"))
    ok = service.execute_plan_step(step, TenantContext("tenant-a", correlation_id="corr-step"), "mission-a", "idem-ok", budget_remaining=Decimal("2"))
    assert blocked.failure_type == AgentExecutionFailureType.BUDGET_EXHAUSTED
    assert ok.agent_id == "market-agent" and executor.calls == 1


def test_scenario_h_r2_adapter_propagates_success_but_not_partial_and_preserves_r1_link():
    service, _, _ = setup(); propagation = PropagationService()
    mission = Mission("sub-a", MissionType.MARKET_DISCOVERY, MissionStatus.RUNNING, parameters={"tenant_id": "tenant-a", "required_capability": "MARKET_RESEARCH", "action_type": "SEARCH", "tool_id": "search", "inputs": {"query": "boots"}, "plan_id": "plan-a", "plan_step_id": "step-a"}, parent_mission_id="parent-a", depth=1)
    tenant = TenantContext("tenant-a", correlation_id="corr-sub")
    result = service.execute_mission(mission, tenant, "idem-sub", budget_remaining=Decimal("4"))
    assert service.propagate_to_sub_mission(result, mission, tenant, propagation) is True
    contract = propagation.calls[0][1]
    assert contract.status == MissionStatus.COMPLETED and contract.parent_mission_id == "parent-a"
    partial_service, _, _ = setup("PARTIAL")
    partial = partial_service.execute_mission(mission, tenant, "idem-partial", budget_remaining=Decimal("4"))
    assert partial.status == AgentExecutionStatus.PARTIAL
    assert partial_service.propagate_to_sub_mission(partial, mission, tenant, propagation) is False and len(propagation.calls) == 1


def test_scenario_i_exact_events_safe_trace_cost_and_usage_attribution():
    audit = AuditRepo(); trace = TraceService(); usage = []
    service, _, _ = setup(audit=audit, trace=trace, usage_hook=lambda context, result: usage.append((context, result)))
    result = service.execute(ctx(inputs={"query": "safe"}, metadata={"token": "secret"}))
    assert [r.record_type for r in audit.records] == [AuditRecordType.SPECIALIST_SELECTED, AuditRecordType.SPECIALIST_EXECUTION_STARTED, AuditRecordType.SPECIALIST_EXECUTION_COMPLETED]
    assert result.metadata["usage_attributed_to"] == "market-agent" and result.cost_used == Decimal("1.5")
    assert len(trace.records) == 2 and len(usage) == 1
    assert "secret" not in str(audit.records)


def test_scenario_j_failure_is_sanitized_and_idempotent_without_duplicate_side_effects():
    audit = AuditRepo(); service, executors, _ = setup("RAISE", audit=audit); executor = executors["market-executor"]
    first = service.execute(ctx(idempotency_key="e2e")); second = service.execute(ctx(idempotency_key="e2e"))
    assert first.status == AgentExecutionStatus.FAILED and first.failure_type == AgentExecutionFailureType.TECHNICAL_FAILURE
    assert "must-not-leak" not in first.failure_reason and second is first and executor.calls == 1
    assert audit.records[-1].record_type == AuditRecordType.SPECIALIST_EXECUTION_FAILED
