import pytest
from decimal import Decimal
from typing import Dict, Any, List, Optional, Sequence
from datetime import datetime, timezone

from src.domain.mission.models import (
    Mission,
    MissionStatus,
    MissionType,
    LoopState,
    LoopAction,
    LoopDecision,
    LoopTraceEntry,
)
from src.domain.mission.ports import DecisionProvider, ActionExecutor
from src.application.mission.autonomous_loop import AutonomousLoop, LoopLimits, LoopResult

# Tool Registry
from src.domain.tool.models import (
    ToolDescriptor,
    ToolVersion,
    ToolContract,
    ToolSchemaField,
    ToolSideEffectLevel,
    ToolExecutionChannel,
    ToolLifecycleStatus,
    ToolEvidenceProvenance,
    ToolInvocationRequest,
    ToolInvocationResult,
)
from src.domain.tool.ports import ToolRegistryPort, ToolInvokerPort
from src.domain.tool.registry import ToolRegistry
from src.application.tool.catalog import register_standard_commerce_tools
from src.application.tool.tool_discovery_service import ToolDiscoveryService
from src.application.tool.tool_invocation_service import ToolInvocationService

# Policy Engine
from src.domain.policy.models import (
    PolicyDecisionType,
    PolicyEvaluationContext,
    PolicyEvaluation,
)
from src.domain.policy.engine import PolicyEngine
from src.application.policy.policy_enforcement_service import PolicyEnforcementService
from src.application.policy.policy_guarded_action_executor import PolicyGuardedActionExecutor

# Capital & Operating Model
from src.domain.capital.models import CapitalBudget
from src.domain.operating_model.models import (
    OperatingModelType,
    OperatingDecisionType,
    InventoryScenario,
    DropshippingScenario,
    OperatingModelComparison,
    OperatingDecision,
)
from src.domain.operating_model.engine import OperatingModelEngine

# Market & Opportunity & Supplier & Profit models
from src.domain.market_intelligence.models import (
    MarketEvidence,
    MarketListing,
    Confidence,
    Money,
    Marketplace,
    TrendSignal,
    DemandSignal,
    SignalType,
)
from src.domain.opportunity.models import (
    Opportunity,
    OpportunityReadiness,
    EvidenceSufficiency,
)
from src.domain.opportunity.engine import OpportunityEngine
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierCandidate,
    SupplierEvidence,
    CommercialQuote,
    PriceTier,
    MOQInfo,
    ShippingOption,
    ShippingMethod,
    SupplierRiskProfile,
    SupplierRiskDimension,
    RiskLevel,
    EvidenceProvenanceType,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    ProductMatchGrade,
    ContingencyTrigger,
)
from src.domain.profit.models import (
    CostComponent,
    CostComponentType,
    CostComponentStatus,
    SalePrice,
    SalePriceType,
    ExchangeRate,
    ProfitStatus,
    EconomicEvaluationResult,
    MarketplaceFeeStructure,
)
from src.domain.profit.engine import (
    ProfitEngine,
    LandedCostCalculator,
    UnitEconomicsCalculator,
    BreakEvenCalculator,
)
from src.domain.publication.models import (
    SalesChannel,
    SalesChannelType,
    ListingDraft,
    PublicationRequest,
    PublicationResult,
    PublicationStatus,
)
from src.domain.publication.ports import PublicationPort
from src.application.publication.publication_action_executor import PublicationActionExecutor
from src.domain.pricing.models import PricingAction, PricingStatus
from src.domain.inventory.models import InventoryAction, InventoryStatus
from src.domain.order.models import Order, OrderStatus
from src.domain.fulfillment.models import Shipment, ShipmentStatus
from src.domain.returns.models import Return, ReturnStatus, ClaimStatus, RefundStatus


class GateFDecisionProvider(DecisionProvider):
    """
    DecisionProvider dinámico para la validación formal E2E de Gate F.
    Atraviesa el ciclo Marketplace Operations (G.1-G.8) integrado con la gobernanza Policy Engine.
    """

    def __init__(
        self,
        discovery_service: ToolDiscoveryService,
        scenario_mode: str = "STANDARD_APPROVED",  # STANDARD_APPROVED, REJECTED_BY_HUMAN, DUPLICATE_REPLAY, UNKNOWN_TIMEOUT, DENY_BY_POLICY
    ):
        self.discovery_service = discovery_service
        self.scenario_mode = scenario_mode

    def decide(self, state: LoopState) -> LoopDecision:
        iteration = state.iteration
        obs_history = state.observations

        executed_caps = set()
        for obs in obs_history:
            if hasattr(obs, "get"):
                cap = obs.get("capability")
                if cap:
                    executed_caps.add(cap)
                st = obs.get("status")
                if st:
                    executed_caps.add(st)
                if obs.get("action_executed"):
                    executed_caps.add(obs.get("action_executed"))

        print(f"DEBUG iteration={iteration} executed_caps={executed_caps}")

        # Paso 1: Listing Generator / Validator (G.1/G.2)
        if "LISTING_GENERATION" not in executed_caps:
            tools = self.discovery_service.discover_tools_for_capability("LISTING_GENERATION")
            tool = tools[0] if tools else None
            return LoopDecision(
                action=LoopAction.CONTINUE,
                target=tool.tool_id if tool else "generate_listing",
                parameters={
                    "capability": "LISTING_GENERATION",
                    "action_type": "GENERATE_LISTING",
                    "title": "SSD Kingston 480GB SATA3",
                    "price": 34990,
                    "currency": "CLP",
                    "category_id": "MLC1648",
                    "provenance": "VERIFIED_INTERNAL",
                    "confidence": "HIGH",
                },
                reason="Generating and validating marketplace listing draft",
                confidence=0.95,
            )

        # Paso 2: Operación Comercial / Publicación que requiere gobernanza (G.3)
        if "MARKETPLACE_PUBLICATION" not in executed_caps and "PUBLISH_LISTING" not in executed_caps and "UNKNOWN" not in executed_caps and "POLICY_APPROVAL_REQUIRED" not in executed_caps and "POLICY_DENIED" not in executed_caps and "SKIPPED_DUPLICATE" not in executed_caps:
            tools = self.discovery_service.discover_tools_for_capability("MARKETPLACE_PUBLICATION")
            tool = tools[0] if tools else None

            extra_params = {}
            if self.scenario_mode == "REJECTED_BY_HUMAN":
                extra_params["human_approved"] = False
                extra_params["actions_requiring_approval"] = ["PUBLISH_LISTING"]
                extra_params["idempotency_key"] = "IDEM-KEY-REJECTED-123"
            elif self.scenario_mode == "STANDARD_APPROVED":
                extra_params["human_approved"] = True
                extra_params["idempotency_key"] = "IDEM-KEY-APPROVED-123"
            elif self.scenario_mode == "DUPLICATE_REPLAY":
                extra_params["human_approved"] = True
                extra_params["idempotency_key"] = "IDEM-KEY-DUPLICATE-999"
            elif self.scenario_mode == "UNKNOWN_TIMEOUT":
                extra_params["human_approved"] = True
                extra_params["simulate_timeout"] = True
                extra_params["idempotency_key"] = "IDEM-KEY-TIMEOUT-123"
            elif self.scenario_mode == "DENY_BY_POLICY":
                extra_params["prohibited_action"] = True
                extra_params["idempotency_key"] = "IDEM-KEY-DENIED-123"

            return LoopDecision(
                action=LoopAction.PROMOTE,
                target=tool.tool_id if tool else "publish_listing",
                parameters={
                    "capability": "MARKETPLACE_PUBLICATION",
                    "action_type": "PUBLISH_LISTING",
                    "title": "SSD Kingston 480GB SATA3",
                    "price": 34990,
                    "currency": "CLP",
                    "category_id": "MLC1648",
                    "provenance": "VERIFIED_INTERNAL",
                    "confidence": "HIGH",
                    "risk_level": "LOW",
                    **extra_params,
                },
                reason="Promoting validated draft to marketplace publication",
                confidence=0.90,
            )

        # Paso 3: Post-publicación (Pricing, Inventory, Orders, Fulfillment, Returns - G.4-G.8)
        if "POST_PUBLICATION_OPS" not in executed_caps and "UNKNOWN" not in executed_caps and "POLICY_APPROVAL_REQUIRED" not in executed_caps and "POLICY_DENIED" not in executed_caps and "SKIPPED_DUPLICATE" not in executed_caps:
            return LoopDecision(
                action=LoopAction.CONTINUE,
                target="manage_post_publication_ops",
                parameters={
                    "capability": "POST_PUBLICATION_OPS",
                    "action_type": "POST_PUBLICATION_OPS",
                    "pricing_status": "UPDATED",
                    "inventory_status": "ALLOCATED",
                    "order_status": "PAID",
                    "fulfillment_status": "SHIPPED",
                    "return_status": "NONE",
                    "provenance": "VERIFIED_INTERNAL",
                    "confidence": "HIGH",
                },
                reason="Verifying marketplace operational lifecycle (pricing, inventory, orders, fulfillment, returns)",
                confidence=0.95,
            )

        # Paso 4: Finalización del Loop
        return LoopDecision(
            action=LoopAction.COMPLETE,
            target="finalize_mission",
            parameters={"final_status": "SUCCESS"},
            reason="Marketplace operations and governance gate requirements fulfilled",
            confidence=1.0,
        )


class GateFActionExecutor(ActionExecutor):
    """
    ActionExecutor unificado para la suite de validación Gate F.
    Conecta ToolRegistry, PublicationActionExecutor y simula escenarios de Aprobación, Policy, UNKNOWN y Replay.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        tool_invocation_service: ToolInvocationService,
        publication_executor: PublicationActionExecutor,
    ):
        self.registry = registry
        self.tool_invocation_service = tool_invocation_service
        self.publication_executor = publication_executor
        self.executed_keys: set = set()

    def execute(self, decision: LoopDecision, state: Optional[LoopState] = None) -> Dict[str, Any]:
        params = decision.parameters or {}
        capability = params.get("capability")

        if decision.action == LoopAction.COMPLETE:
            return {"status": "COMPLETED", "summary": "Mission Gate F validated"}

        if capability == "LISTING_GENERATION":
            return {
                "status": "SUCCESS",
                "capability": capability,
                "listing_draft": {
                    "title": params.get("title"),
                    "price": params.get("price"),
                    "currency": params.get("currency"),
                    "quality_score": 95,
                    "validation_passed": True,
                },
            }

        if capability == "MARKETPLACE_PUBLICATION":
            if params.get("simulate_timeout"):
                return {
                    "status": "UNKNOWN",
                    "capability": capability,
                    "error_code": "504_GATEWAY_TIMEOUT",
                    "message": "Gateway timeout during publication. Verification needed.",
                }

            idemp_key = params.get("idempotency_key")
            if idemp_key and idemp_key in self.executed_keys:
                return {
                    "status": "SKIPPED_DUPLICATE",
                    "capability": capability,
                    "idempotency_key": idemp_key,
                    "message": "Action already executed. Idempotent no-op.",
                }
            if idemp_key:
                self.executed_keys.add(idemp_key)

            # Ejecutar publicación via PublicationActionExecutor
            channel = SalesChannel(channel_id="MLC", channel_type=SalesChannelType.MARKETPLACE, name="Mercado Libre Chile")
            draft = ListingDraft(
                draft_id="DRAFT-GATE-F-001",
                product_reference_id="PROD-REF-001",
                title=params.get("title", "Item"),
                description="Kingston SSD 480GB SATA3 2.5 Inch High Performance Solid State Drive",
                price=Decimal(str(params.get("price", 34990))),
                currency=params.get("currency", "CLP"),
                available_quantity=10,
                channel=channel,
                category_id=params.get("category_id", "MLC1648"),
            )
            # Pasar draft y channel en las opciones de decisión para PublicationActionExecutor
            params_for_decision = dict(decision.parameters or {})
            params_for_decision["draft"] = draft
            params_for_decision["channel"] = channel
            decision_with_draft = LoopDecision(
                action=decision.action,
                target=decision.target,
                parameters=params_for_decision,
                reason=decision.reason,
                confidence=decision.confidence,
            )

            pub_res = self.publication_executor.execute(decision_with_draft, state or LoopState(mission_id="m-1"))
            return {
                "status": "SUCCESS" if pub_res.get("is_success") else pub_res.get("status", "FAILED"),
                "capability": capability,
                "publication_id": pub_res.get("publication_id"),
                "permalink": pub_res.get("permalink"),
            }

        if capability == "POST_PUBLICATION_OPS":
            return {
                "status": "SUCCESS",
                "capability": capability,
                "pricing": "UPDATED",
                "inventory": "RESERVED",
                "order": "SYNCHRONIZED",
                "fulfillment": "LABEL_CREATED",
                "returns": "MONITORED",
            }

        return {"status": "SUCCESS", "capability": capability}


class TestGateFE2EValidation:
    """
    Suite E2E de validación formal para Gate F (Marketplace Operations + Governance Approval).
    Prueba integración real entre componentes internos (Listing Generator/Validator, Policy Engine,
    Approval Rules, Idempotency, UNKNOWN recovery, y la cadena G.1-G.8).
    """

    @pytest.fixture
    def setup_environment(self):
        registry = ToolRegistry()
        register_standard_commerce_tools(registry)
        discovery_service = ToolDiscoveryService(registry)

        class MockToolInvoker(ToolInvokerPort):
            def invoke(self, request, descriptor):
                return ToolInvocationResult(
                    tool_id=descriptor.tool_id,
                    version=descriptor.version.version_str,
                    success=True,
                    output_payload={"status": "OK", "executed_at": datetime.now(timezone.utc).isoformat()},
                    correlation_id=request.correlation_id,
                    provenance=descriptor.provenance,
                )

        invoker = MockToolInvoker()
        tool_invocation_service = ToolInvocationService(invoker=invoker)

        class MockGateFPublicationPort(PublicationPort):
            def __init__(self):
                self.calls = []

            def publish(self, request: PublicationRequest) -> PublicationResult:
                self.calls.append(request)
                return PublicationResult(
                    publication_id="MLC-PUB-GATE-F-888",
                    external_reference="MLC-PUB-GATE-F-888",
                    status=PublicationStatus.PUBLISHED,
                    channel=request.channel,
                    permalink="https://articulo.mercadolibre.cl/MLC-PUB-GATE-F-888",
                )

            def get_status(self, publication_id: str) -> Optional[PublicationResult]:
                return None

        pub_port = MockGateFPublicationPort()
        base_pub_executor = PublicationActionExecutor(publication_port=pub_port)

        budget = CapitalBudget(
            budget_id="budget-gate-f",
            total_capital=Decimal("2000000"),
            reserved_capital=Decimal("300000"),
            committed_capital=Decimal("150000"),
            currency="CLP",
        )

        return {
            "registry": registry,
            "discovery_service": discovery_service,
            "tool_invocation_service": tool_invocation_service,
            "pub_port": pub_port,
            "base_pub_executor": base_pub_executor,
            "budget": budget,
        }

    def test_gate_f_scenario_a_approval_required_and_approved(self, setup_environment):
        """
        ESCENARIO A — Acción que requiere aprobación y es aprobada:
        Context -> Decision -> Policy -> REQUIRE_APPROVAL (or Approved) -> Approval -> ActionExecutor -> Result -> Audit.
        """
        env = setup_environment
        action_executor = GateFActionExecutor(
            registry=env["registry"],
            tool_invocation_service=env["tool_invocation_service"],
            publication_executor=env["base_pub_executor"],
        )

        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=action_executor,
            capital_budget=env["budget"],
        )

        decision_provider = GateFDecisionProvider(
            discovery_service=env["discovery_service"],
            scenario_mode="STANDARD_APPROVED",
        )

        loop = AutonomousLoop(
            decision_provider=decision_provider,
            action_executor=guarded_executor,
            limits=LoopLimits(max_iterations=8),
        )

        result = loop.run(
            mission_id="mission-gate-f-approved",
            goal="Validate Gate F Marketplace Operations with Approved Human Governance",
        )

        print(f"DEBUG SCENARIO A result.status={result.status} reason={result.termination_reason} errors={result.errors}")
        for t in result.trace:
            print(f"TRACE: action={t.decision.action} target={t.decision.target} obs={t.observation}")
        assert result.termination_reason == "CONVERGED"
        assert len(env["pub_port"].calls) == 1
        assert env["pub_port"].calls[0].draft.title == "SSD Kingston 480GB SATA3"

    def test_gate_f_scenario_b_rejection_blocks_side_effect(self, setup_environment):
        """
        ESCENARIO B — Rejection / Sin aprobación:
        REQUIRE_APPROVAL -> REJECTED / Not Approved -> cero side effects externos.
        """
        env = setup_environment
        action_executor = GateFActionExecutor(
            registry=env["registry"],
            tool_invocation_service=env["tool_invocation_service"],
            publication_executor=env["base_pub_executor"],
        )

        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=action_executor,
            capital_budget=env["budget"],
        )

        decision_provider = GateFDecisionProvider(
            discovery_service=env["discovery_service"],
            scenario_mode="REJECTED_BY_HUMAN",
        )

        loop = AutonomousLoop(
            decision_provider=decision_provider,
            action_executor=guarded_executor,
            limits=LoopLimits(max_iterations=8),
        )

        result = loop.run(
            mission_id="mission-gate-f-rejected",
            goal="Validate Gate F Rejection when Human Approval is missing",
        )

        # Policy Engine debe retornar REQUIRE_APPROVAL o POLICY_DENIED
        assert len(env["pub_port"].calls) == 0

    def test_gate_f_scenario_c_duplicate_idempotent(self, setup_environment):
        """
        ESCENARIO C — Duplicate / Replay:
        Replay de acción con misma idempotency_key -> cero side effects duplicados.
        """
        env = setup_environment
        action_executor = GateFActionExecutor(
            registry=env["registry"],
            tool_invocation_service=env["tool_invocation_service"],
            publication_executor=env["base_pub_executor"],
        )

        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=action_executor,
            capital_budget=env["budget"],
        )

        decision_provider = GateFDecisionProvider(
            discovery_service=env["discovery_service"],
            scenario_mode="DUPLICATE_REPLAY",
        )

        loop = AutonomousLoop(
            decision_provider=decision_provider,
            action_executor=guarded_executor,
            limits=LoopLimits(max_iterations=8),
        )

        # Primera ejecución
        res1 = loop.run(
            mission_id="mission-gate-f-dup-1",
            goal="First run of action",
        )
        assert len(env["pub_port"].calls) == 1

        # Segunda ejecución (Replay con misma idempotency_key en el executor)
        res2 = loop.run(
            mission_id="mission-gate-f-dup-2",
            goal="Second run of identical action",
        )
        # La llamada externa no debe incrementarse
        assert len(env["pub_port"].calls) == 1

    def test_gate_f_scenario_d_unknown_timeout_preserves_uncertainty(self, setup_environment):
        """
        ESCENARIO D — UNKNOWN:
        Timeout / 5xx -> UNKNOWN -> no false success -> re-observe / reconcile.
        """
        env = setup_environment
        action_executor = GateFActionExecutor(
            registry=env["registry"],
            tool_invocation_service=env["tool_invocation_service"],
            publication_executor=env["base_pub_executor"],
        )

        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=action_executor,
            capital_budget=env["budget"],
        )

        decision_provider = GateFDecisionProvider(
            discovery_service=env["discovery_service"],
            scenario_mode="UNKNOWN_TIMEOUT",
        )

        loop = AutonomousLoop(
            decision_provider=decision_provider,
            action_executor=guarded_executor,
            limits=LoopLimits(max_iterations=8),
        )

        result = loop.run(
            mission_id="mission-gate-f-unknown",
            goal="Validate UNKNOWN handling during timeout",
        )

        # Debe registrarse la respuesta UNKNOWN en la trazabilidad sin dar falso éxito
        unknown_observations = []
        for t in result.trace:
            obs = t.observation
            st = None
            if hasattr(obs, "get"):
                st = obs.get("status")
            elif isinstance(obs, dict):
                st = obs.get("status")
            if st == "UNKNOWN":
                unknown_observations.append(obs)

        assert len(unknown_observations) > 0
        assert len(env["pub_port"].calls) == 0

    def test_gate_f_scenario_e_policy_deny_overrides_approval(self, setup_environment):
        """
        ESCENARIO E — Policy DENY:
        DENY por política (acción prohibida o riesgo inaceptable) -> nunca ejecutar aunque se afirme aprobación.
        """
        env = setup_environment
        action_executor = GateFActionExecutor(
            registry=env["registry"],
            tool_invocation_service=env["tool_invocation_service"],
            publication_executor=env["base_pub_executor"],
        )

        guarded_executor = PolicyGuardedActionExecutor(
            delegate_executor=action_executor,
            capital_budget=env["budget"],
            default_prohibited_actions=("PUBLISH_LISTING", "publish_listing"),
        )

        decision_provider = GateFDecisionProvider(
            discovery_service=env["discovery_service"],
            scenario_mode="STANDARD_APPROVED",  # Asegura human_approved=True
        )

        loop = AutonomousLoop(
            decision_provider=decision_provider,
            action_executor=guarded_executor,
            limits=LoopLimits(max_iterations=8),
        )

        result = loop.run(
            mission_id="mission-gate-f-deny",
            goal="Validate Policy DENY precedence over human approval",
        )

        # Aunque human_approved sea True, Policy DENY bloquea totalmente la ejecución externa
        assert len(env["pub_port"].calls) == 0
