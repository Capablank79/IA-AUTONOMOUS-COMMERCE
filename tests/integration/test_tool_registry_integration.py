import pytest
from src.domain.tool.models import (
    ToolDescriptor,
    ToolVersion,
    ToolContract,
    ToolSchemaField,
    ToolSideEffectLevel,
    ToolExecutionChannel,
    ToolLifecycleStatus,
    ToolInvocationRequest,
)
from src.domain.tool.ports import ToolInvokerPort
from src.domain.tool.registry import ToolRegistry
from src.application.tool.catalog import register_standard_commerce_tools
from src.application.tool.tool_discovery_service import ToolDiscoveryService
from src.application.tool.tool_invocation_service import ToolInvocationService
from src.application.policy.policy_guarded_action_executor import PolicyGuardedActionExecutor
from src.application.policy.policy_enforcement_service import PolicyEnforcementService
from src.domain.policy.engine import PolicyEngine
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.domain.supplier_intelligence.models import EvidenceProvenanceType


class DummyDelegateActionExecutor(ActionExecutor):
    def __init__(self):
        self.invoked = False
        self.last_decision = None
        self.last_state = None

    def execute(self, decision: LoopDecision, state: LoopState) -> dict:
        self.invoked = True
        self.last_decision = decision
        self.last_state = state
        return {"status": "SUCCESS", "data": "dummy_action_executed"}


class TestToolPolicyLoopIntegration:

    def test_full_flow_discover_evaluate_and_guard_loop(self):
        """
        Demuestra la secuencia E2E:
        AGENT
        ↓
        TOOL REGISTRY (Discover Capabilities)
        ↓
        SELECT APPROPRIATE TOOL
        ↓
        POLICY ENGINE GOVERNANCE
        ↓
        ACTION EXECUTOR
        ↓
        RESULT OBSERVATION
        """
        # 1. Setup Registry con catálogo estándar
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)
        discovery_service = ToolDiscoveryService(registry)

        # 2. El Agente necesita buscar oportunidades de mercado -> descubre capacidades
        discovered_tools = discovery_service.discover_tools_for_capability("MARKET_DISCOVERY")
        assert len(discovered_tools) > 0
        selected_tool = discovered_tools[0]
        assert selected_tool.tool_id == "market_search"

        # 3. Preparar acción a ejecutar en Autonomous Loop
        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Exploring market for laptop category",
            target=selected_tool.tool_id,
            parameters={"query": "laptop", "limit": 10},
        )
        state = LoopState(
            mission_id="mission-market-101",
            iteration=1,
            goal="Find best laptop opportunities",
            current_target="laptop",
        )

        # 4. Envolver ActionExecutor con PolicyGuardedActionExecutor
        delegate_exec = DummyDelegateActionExecutor()
        guarded_exec = PolicyGuardedActionExecutor(
            delegate_executor=delegate_exec,
            actor_id="autonomous_agent_01",
            default_allowed_actions=("MARKET_DISCOVERY", "CONTINUE"),
        )

        # 5. Ejecutar a través del guardián de políticas
        result = guarded_exec.execute(decision=decision, state=state)

        # 6. Validar que la política permitió la acción READ_ONLY y el ejecutor delegado se ejecutó
        assert result["status"] == "SUCCESS"
        assert delegate_exec.invoked is True
        assert guarded_exec.latest_evaluation.decision.value == "ALLOW"

    def test_policy_blocks_unauthorized_high_impact_tool_action(self):
        """
        Verifica que una herramienta de alto impacto (COMMERCIAL_PUBLICATION)
        es interceptada y bloqueada deterministamente si no cuenta con aprobación humana.
        """
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)
        discovery_service = ToolDiscoveryService(registry)

        pub_tools = discovery_service.discover_tools_for_capability("COMMERCIAL_PUBLICATION")
        assert len(pub_tools) == 1
        pub_tool = pub_tools[0]

        # Acción que intenta publicar sin aprobación humana previa
        decision = LoopDecision(
            action=LoopAction.CONTINUE,
            reason="Publishing item to marketplace",
            target=pub_tool.tool_id,
            parameters={
                "action_type": "COMMERCIAL_PUBLICATION",
                "is_external_impact": True,
                "title": "Test Item",
                "price": 1000.0,
            },
        )
        state = LoopState(
            mission_id="mission-pub-102",
            iteration=2,
            goal="Publish approved item",
        )

        delegate_exec = DummyDelegateActionExecutor()
        guarded_exec = PolicyGuardedActionExecutor(
            delegate_executor=delegate_exec,
            actor_id="autonomous_agent_01",
            default_actions_requiring_approval=("COMMERCIAL_PUBLICATION", "publish_listing"),
        )

        result = guarded_exec.execute(decision=decision, state=state)

        # Debe ser bloqueada por REQUIRE_APPROVAL en lugar de ejecutarse ciegamente
        assert result["status"] == "POLICY_APPROVAL_REQUIRED"
        assert delegate_exec.invoked is False
        assert guarded_exec.latest_evaluation.decision.value == "REQUIRE_APPROVAL"
