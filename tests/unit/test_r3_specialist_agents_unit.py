from dataclasses import FrozenInstanceError
from decimal import Decimal
import threading
import time

import pytest

from src.application.specialist_agent.service import SpecialistAgentService
from src.domain.audit.models import AuditRecordType
from src.domain.mission.models import LoopDecision, LoopState
from src.domain.mission.ports import ActionExecutor
from src.domain.planning.models import PlanStep
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
from src.domain.tenant.models import TenantContext
from src.domain.tool.models import (
    ToolContract,
    ToolDescriptor,
    ToolLifecycleStatus,
    ToolSchemaField,
    ToolSideEffectLevel,
    ToolVersion,
)
from src.domain.tool.registry import ToolRegistry
from src.domain.specialist_agent.registry import SpecialistAgentRegistry


def contract(name: str, required: str = "query") -> ToolContract:
    return ToolContract(name, (ToolSchemaField(required, "str"),), allow_extra_fields=False)


def capability(name: str = "MARKET_RESEARCH", tool_ids=("search",), actions=("SEARCH",)) -> AgentCapability:
    return AgentCapability(
        capability_id=name,
        contract=AgentCapabilityContract(contract("input"), contract("output", "items")),
        action_types=actions,
        tool_ids=tool_ids,
    )


def descriptor(tool_id: str = "search", cap_name: str = "MARKET_RESEARCH") -> ToolDescriptor:
    return ToolDescriptor(
        tool_id,
        tool_id,
        ToolVersion("v1"),
        "test descriptor",
        cap_name,
        contract("ti"),
        contract("to", "items"),
        ToolSideEffectLevel.READ_ONLY,
        status=ToolLifecycleStatus.AVAILABLE,
    )


def definition(
    agent_id: str = "agent-a",
    tenant: str = "tenant-a",
    availability: AgentAvailability = AgentAvailability.AVAILABLE,
    cost: Decimal = Decimal("2"),
    priority: int = 10,
    eligible: bool = True,
    cap: AgentCapability = None,
    executor_key: str = None,
    actions=("SEARCH",),
    tools=("search",),
) -> SpecialistAgentDefinition:
    cap = cap or capability(tool_ids=tools, actions=actions)
    return SpecialistAgentDefinition(
        agent_id=agent_id,
        tenant_id=tenant,
        capabilities=(cap,),
        availability=availability,
        allowed_action_types=actions,
        allowed_tool_ids=tools,
        estimated_cost=cost,
        priority=priority,
        policy_eligible=eligible,
        metadata={"secret": "hidden-token", "team": "market"},
        executor_key=executor_key or agent_id,
    )


class GuardedExecutor(ActionExecutor):
    is_guarded_executor = True

    def __init__(self, result=None, error=None, delay=0):
        self.result = (
            result
            if result is not None
            else {
                "status": "SUCCESS",
                "outputs": {"items": ["found"]},
                "cost_used": "1.25",
                "tokens_used": 7,
            }
        )
        self.error = error
        self.delay = delay
        self.calls = 0
        self.last = None

    def execute(self, decision: LoopDecision, state: LoopState) -> dict:
        self.calls += 1
        self.last = (decision, state)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        return self.result


class UnguardedExecutor(ActionExecutor):
    def execute(self, decision: LoopDecision, state: LoopState) -> dict:
        return {"status": "SUCCESS"}


class AuditSink:
    def __init__(self):
        self.records = []

    def append(self, record):
        self.records.append(record)
        return record


def registry_with(*agents, tool_desc=None, known_actions=("SEARCH", "PRICE_UPDATE")):
    tools = ToolRegistry()
    tools.register(tool_desc or descriptor())
    registry = SpecialistAgentRegistry(tools, known_actions)
    for agent in agents:
        registry.register(agent)
    return registry


def service_with(*agents, executors=None, **kwargs):
    agents = agents or (definition(),)
    executors = executors or {a.executor_key: GuardedExecutor() for a in agents}
    return SpecialistAgentService(registry_with(*agents), executors, **kwargs)


def context(**changes) -> AgentExecutionContext:
    values = dict(
        tenant_id="tenant-a",
        mission_id="mission-1",
        capability_id="MARKET_RESEARCH",
        action_type="SEARCH",
        inputs={"query": "shoes"},
        idempotency_key="idem-1",
        correlation_id="corr-1",
        tool_id="search",
        budget_remaining=Decimal("5"),
    )
    values.update(changes)
    return AgentExecutionContext(**values)


# =========================================================================
# 20 REQUIRED UNIT TESTS (1 TO 20)
# =========================================================================


def test_1_register_valid_specialist():
    agent = definition("agent-1")
    registry = registry_with(agent)
    retrieved = registry.get("tenant-a", "agent-1")
    assert retrieved is not None
    assert retrieved.agent_id == "agent-1"
    assert retrieved.tenant_id == "tenant-a"
    assert retrieved.executor_key == "agent-1"


def test_2_duplicate_registration_rejected():
    agent = definition("agent-dup")
    registry = registry_with(agent)
    with pytest.raises(ValueError, match="already registered"):
        registry.register(agent)


def test_3_capability_match():
    agent = definition("agent-match")
    service = service_with(agent)
    selection = service.select_agent("tenant-a", "MARKET_RESEARCH")
    assert selection.status == AgentSelectionStatus.SELECTED
    assert selection.agent_id == "agent-match"


def test_4_capability_mismatch():
    agent = definition("agent-mismatch")
    service = service_with(agent)
    selection = service.select_agent("tenant-a", "NON_EXISTENT_CAPABILITY")
    assert selection.status == AgentSelectionStatus.NO_MATCH
    assert selection.agent_id is None


def test_5_deterministic_selection():
    # Priority tie break (lower priority int wins), then alphabetical agent_id
    a2 = definition("agent-2", priority=10)
    a1 = definition("agent-1", priority=5)
    a3 = definition("agent-3", priority=5)
    service = service_with(a2, a1, a3)
    selection = service.select_agent("tenant-a", "MARKET_RESEARCH")
    assert selection.status == AgentSelectionStatus.SELECTED
    assert selection.agent_id == "agent-1"


def test_6_unavailable_agent_rejected():
    unavailable = definition("agent-unavail", availability=AgentAvailability.UNAVAILABLE)
    service = service_with(unavailable)
    selection = service.select_agent("tenant-a", "MARKET_RESEARCH")
    assert selection.status == AgentSelectionStatus.BLOCKED
    assert "UNAVAILABLE" in selection.reason


def test_7_unknown_availability_not_assumed_available():
    unknown = definition("agent-unknown", availability=AgentAvailability.UNKNOWN)
    service = service_with(unknown)
    selection = service.select_agent("tenant-a", "MARKET_RESEARCH")
    assert selection.status == AgentSelectionStatus.BLOCKED
    assert "UNKNOWN" in selection.reason


def test_8_input_contract_validation():
    service = service_with(definition())
    # Missing required 'query' field
    result = service.execute(context(inputs={}))
    assert result.status == AgentExecutionStatus.INSUFFICIENT_DATA
    assert result.failure_type == AgentExecutionFailureType.INVALID_INPUT
    assert "query" in result.failure_reason


def test_9_output_contract_validation():
    # Output schema requires 'items', executor returns empty dict
    bad_output_exec = GuardedExecutor(result={"status": "SUCCESS", "outputs": {}})
    service = service_with(definition(), executors={"agent-a": bad_output_exec})
    result = service.execute(context())
    assert result.status == AgentExecutionStatus.FAILED
    assert result.failure_type == AgentExecutionFailureType.TECHNICAL_FAILURE
    assert "items" in result.failure_reason


def test_10_tool_allowlist():
    agent = definition(tools=("search",))
    assert agent.allowed_tool_ids == ("search",)
    service = service_with(agent)
    ok_res = service.execute(context(tool_id="search", idempotency_key="tool-ok"))
    assert ok_res.status == AgentExecutionStatus.SUCCESS


def test_11_denied_tool_blocked():
    agent = definition(tools=("search",))
    service = service_with(agent)
    # Attempting to use unallowed tool 'publish'
    res = service.execute(context(tool_id="publish", idempotency_key="tool-denied"))
    assert res.status == AgentExecutionStatus.BLOCKED
    assert res.failure_type == AgentExecutionFailureType.POLICY_DENIED
    assert res.failure_reason == "TOOL_NOT_ALLOWED"


def test_12_budget_eligibility():
    expensive = definition("agent-exp", cost=Decimal("10"))
    service = service_with(expensive)
    # Remaining budget is 5, cost is 10 -> BLOCKED
    res = service.execute(context(budget_remaining=Decimal("5"), idempotency_key="budget-low"))
    assert res.status == AgentExecutionStatus.BLOCKED
    assert res.failure_type == AgentExecutionFailureType.BUDGET_EXHAUSTED


def test_13_unknown_cost_preserved():
    unknown_cost_agent = definition("agent-uncost", cost=None)
    assert unknown_cost_agent.estimated_cost is None
    service = service_with(unknown_cost_agent)
    # UNKNOWN cost is not assumed 0 -> blocked unless explicitly funded
    selection = service.select_agent("tenant-a", "MARKET_RESEARCH", max_cost=Decimal("10"))
    assert selection.status == AgentSelectionStatus.BLOCKED
    assert "UNKNOWN" in selection.reason


def test_14_tenant_isolation():
    agent_t1 = definition("agent-t1", tenant="tenant-1")
    service = service_with(agent_t1)
    res_t2 = service.execute(context(tenant_id="tenant-2", idempotency_key="t2-req"))
    assert res_t2.status == AgentExecutionStatus.BLOCKED
    assert res_t2.failure_type == AgentExecutionFailureType.CAPABILITY_UNAVAILABLE


def test_15_context_minimization():
    ctx = context(metadata={"internal_debug": "strip", "prompt_chain": "secret-cot"})
    agent = definition()
    assert agent.metadata["secret"] == "[REDACTED]"
    # Execution context has explicit fields only, no private repository dump
    assert hasattr(ctx, "inputs")
    assert hasattr(ctx, "budget_remaining")
    assert hasattr(ctx, "correlation_id")


def test_16_partial_result_semantics():
    partial_exec = GuardedExecutor(result={"status": "PARTIAL", "outputs": {"items": ["partial-item"]}})
    service = service_with(definition(), executors={"agent-a": partial_exec})
    result = service.execute(context())
    assert result.status == AgentExecutionStatus.PARTIAL
    assert result.failure_type is None
    assert result.outputs == {"items": ("partial-item",)}


def test_17_policy_denial_not_retried():
    policy_denied_exec = GuardedExecutor(result={"status": "POLICY_DENIED", "reason": "Restricted by rule N.3"})
    service = service_with(definition(), executors={"agent-a": policy_denied_exec})
    res1 = service.execute(context(idempotency_key="policy-retry"))
    res2 = service.execute(context(idempotency_key="policy-retry"))
    assert res1.status == AgentExecutionStatus.BLOCKED
    assert res1.failure_type == AgentExecutionFailureType.POLICY_DENIED
    assert res2 is res1
    assert policy_denied_exec.calls == 1


def test_18_audit_event_safe():
    audit = AuditSink()
    service = service_with(definition(), audit_repository=audit)
    service.execute(context(inputs={"query": "safe-shoes"}, metadata={"raw_secret": "password123"}))
    event_types = [r.record_type for r in audit.records]
    assert AuditRecordType.SPECIALIST_SELECTED in event_types
    assert AuditRecordType.SPECIALIST_EXECUTION_STARTED in event_types
    assert AuditRecordType.SPECIALIST_EXECUTION_COMPLETED in event_types
    # Secrets redacted
    assert "password123" not in str(audit.records)


def test_19_no_omnipotent_fallback():
    # If no specialist matches capability, returns NO_MATCH/BLOCKED, never an omnipotent fallback
    service = service_with(definition())
    selection = service.select_agent("tenant-a", "UNREGISTERED_SUPER_CAPABILITY")
    assert selection.status == AgentSelectionStatus.NO_MATCH
    assert selection.agent_id is None


def test_20_no_r4_plus_coordination():
    # Verify R.3 has NO multi-agent consensus, negotiation or coordinator loop
    service = service_with(definition())
    assert not hasattr(service, "coordinate_agents")
    assert not hasattr(service, "negotiate_assignment")
    assert not hasattr(service, "consensus_vote")
    assert not hasattr(service, "dynamic_reassignment_loop")


# =========================================================================
# ADDITIONAL DEEP EDGE-CASE AND CONCURRENCY TESTS
# =========================================================================


def test_models_are_immutable_and_sanitized():
    agent = definition()
    assert agent.metadata["secret"] == "[REDACTED]"
    with pytest.raises(FrozenInstanceError):
        agent.agent_id = "other"


def test_service_rejects_unguarded_executor():
    with pytest.raises(ValueError, match="guarded"):
        SpecialistAgentService(registry_with(definition()), {"agent-a": UnguardedExecutor()})


def test_selected_specialist_resolves_its_own_executor():
    a = definition("agent-a", priority=99)
    b = definition("agent-b", priority=1)
    ex_a, ex_b = GuardedExecutor(), GuardedExecutor()
    result = service_with(a, b, executors={"agent-a": ex_a, "agent-b": ex_b}).execute(
        context(), selected_agent_id="agent-a"
    )
    assert result.agent_id == "agent-a" and ex_a.calls == 1 and ex_b.calls == 0


def test_assigned_agent_executes_when_eligible_even_if_not_global_winner():
    winner = definition("winner", priority=1)
    assigned = definition("assigned", priority=99)
    ex_winner, ex_assigned = GuardedExecutor(), GuardedExecutor()
    result = service_with(winner, assigned, executors={"winner": ex_winner, "assigned": ex_assigned}).execute(
        context(), "assigned"
    )
    assert result.status == AgentExecutionStatus.SUCCESS and result.agent_id == "assigned"
    assert ex_winner.calls == 0 and ex_assigned.calls == 1


def test_exception_reason_is_sanitized():
    result = service_with(
        definition(),
        executors={"agent-a": GuardedExecutor(error=RuntimeError("api_key=abc123 token: xyz"))},
    ).execute(context())
    assert result.failure_type == AgentExecutionFailureType.TECHNICAL_FAILURE
    assert "abc123" not in result.failure_reason and "xyz" not in result.failure_reason


def test_idempotency_scope_conflict_and_concurrency_never_duplicate_side_effect():
    executor = GuardedExecutor(delay=0.02)
    service = service_with(definition(), executors={"agent-a": executor})
    results = []
    threads = [threading.Thread(target=lambda: results.append(service.execute(context()))) for _ in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert executor.calls == 1 and all(item is results[0] for item in results)
    conflict = service.execute(context(inputs={"query": "different"}))
    assert conflict.failure_type == AgentExecutionFailureType.INVALID_INPUT
