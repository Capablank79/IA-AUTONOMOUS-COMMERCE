from decimal import Decimal
from unittest.mock import MagicMock
import pytest

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.publication.models import SalesChannel, SalesChannelType
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import RiskLevel
from src.domain.pricing.models import (
    PriceChangeReason,
    PricingDecision,
    PricingAction,
    PricingStatus,
)
from src.domain.pricing.engine import PricingDecisionEngine
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.models import PolicyEvaluationContext, PolicyDecisionType
from src.application.pricing.pricing_action_executor import PricingActionExecutor
from src.infrastructure.mercadolibre.api_client import (
    MercadoLibreApiClient,
    MercadoLibreApiError,
)
from src.infrastructure.mercadolibre.pricing_adapter import (
    MercadoLibrePricingAdapter,
)
from src.domain.tool.registry import ToolRegistry
from src.domain.tool.models import ToolExecutionChannel
from src.application.tool.catalog import register_standard_commerce_tools


def test_e2e_pricing_pipeline_market_economics_policy_execution_audit():
    """
    Integration Pipeline E2E (Casos A-X):
    Market/Economic Context
    -> Pricing Decision
    -> Policy Guard (Price floor & Margin)
    -> Pricing Action
    -> ActionExecutor
    -> Marketplace Adapter (MercadoLibre PUT /items/{id})
    -> PricingResult
    -> Audit / Traceability
    """
    # 1. Channel y Mock API Client
    channel = SalesChannel(
        channel_id="ML-CL",
        channel_type=SalesChannelType.MARKETPLACE,
        name="Mercado Libre Chile",
    )
    mock_client = MagicMock(spec=MercadoLibreApiClient)
    mock_client.put.return_value = {
        "id": "MLC987654",
        "price": 17990.0,
        "currency_id": "CLP",
        "status": "active",
        "last_updated": "2026-08-30T15:00:00Z",
    }

    adapter = MercadoLibrePricingAdapter(api_client=mock_client)
    executor = PricingActionExecutor(pricing_port=adapter, default_channel=channel)
    policy_engine = PolicyEngine()

    # 2. Análisis Económico + Propuesta de Decisión
    unit_landed_cost = Decimal("8500")
    marketplace_fee_rate = Decimal("0.13")
    payment_fee_rate = Decimal("0.03")
    shipping_cost = Decimal("1500")
    min_net_margin = Decimal("0.15")

    # Calcular Price Floor de forma determinista
    price_floor = PricingDecisionEngine.calculate_price_floor(
        unit_landed_cost=unit_landed_cost,
        marketplace_fee_rate=marketplace_fee_rate,
        payment_fee_rate=payment_fee_rate,
        shipping_cost=shipping_cost,
        minimum_net_margin_pct=min_net_margin,
    )

    current_price = Decimal("21990")
    proposed_price = Decimal("17990")  # Bajada competitiva de ~18.1% (dentro del 20% safe guardrail)

    decision = PricingDecisionEngine.propose_pricing_decision(
        listing_id="MLC987654",
        channel=channel,
        current_price=current_price,
        proposed_price=proposed_price,
        unit_landed_cost=unit_landed_cost,
        marketplace_fee_rate=marketplace_fee_rate,
        payment_fee_rate=payment_fee_rate,
        shipping_cost=shipping_cost,
        minimum_net_margin_pct=min_net_margin,
        reason=PriceChangeReason.COMPETITIVE_MATCH,
        rationale="Match new market entrant price",
        evidence={"competitor_top_price": 18500, "market_demand_trend": "HIGH"},
        confidence=Confidence.HIGH,
        risk_level=RiskLevel.LOW,
    )

    assert decision.is_below_floor is False
    assert decision.expected_margin_pct > min_net_margin

    # 3. Policy Gate Evaluation
    loop_decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Execute competitive price drop",
        parameters={},
    )

    policy_context = PolicyEvaluationContext(
        action_type="UPDATE_PRICE",
        actor_id="autonomous_pricing_agent",
        mission_id="mission_e2e_pricing",
        correlation_id="corr_e2e_987",
        idempotency_key="idemp_e2e_987",
        loop_decision=loop_decision,
        risk_level=RiskLevel.LOW,
        confidence=Confidence.HIGH,
        is_external_impact=True,
        custom_context={
            "pricing_decision": decision,
            "minimum_margin_pct": min_net_margin,
        },
    )

    policy_eval = policy_engine.evaluate(policy_context)
    assert policy_eval.decision == PolicyDecisionType.ALLOW
    assert policy_eval.is_allowed is True

    # 4. Formular PricingAction y Ejecutar vía ActionExecutor
    pricing_action = PricingDecisionEngine.create_pricing_action(
        decision,
        idempotency_key="idemp_e2e_987",
        correlation_id="corr_e2e_987",
    )

    execution_decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Approved pricing update execution",
        parameters={
            "action_type": "UPDATE_PRICE",
            "pricing_action": pricing_action,
            "channel": channel,
        },
    )
    state = LoopState(mission_id="mission_e2e_pricing", iteration=1, goal="e2e_pricing")

    exec_result = executor.execute(execution_decision, state)

    # 5. Verificaciones
    assert exec_result["status"] == "APPLIED"
    assert exec_result["is_success"] is True
    assert exec_result["applied_price"] == 17990.0
    assert exec_result["previous_price"] == 21990.0
    assert exec_result["idempotency_key"] == "idemp_e2e_987"
    assert exec_result["correlation_id"] == "corr_e2e_987"

    mock_client.put.assert_called_once_with(
        "/items/MLC987654",
        payload={"price": 17990.0},
    )


def test_tool_registry_pricing_tool_integration():
    registry = ToolRegistry()
    register_standard_commerce_tools(registry)

    descriptor = registry.get("update_listing_price")
    assert descriptor is not None
    assert descriptor.name == "Marketplace Listing Price Updater"
    assert descriptor.requires_approval is True
    assert descriptor.requires_idempotency is True
    assert descriptor.input_contract.schema_name == "UpdateListingPriceInput"
    assert descriptor.output_contract.schema_name == "UpdateListingPriceOutput"
    assert ToolExecutionChannel.MERCADO_LIBRE in descriptor.supported_channels
