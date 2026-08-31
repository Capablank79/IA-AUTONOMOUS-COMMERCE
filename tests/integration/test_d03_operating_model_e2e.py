import pytest
from decimal import Decimal
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

from src.domain.mission.models import (
    Mission,
    MissionStatus,
    MissionResult,
    MissionType,
    LoopState,
    LoopAction,
    LoopDecision,
)
from src.domain.mission.ports import DecisionProvider
from src.domain.operating_model.models import (
    OperatingModelType,
    OperatingDecisionType,
    DecisionTrigger,
    InventoryScenario,
    DropshippingScenario,
    OperatingModelComparison,
    OperatingModelPolicy,
    OperatingDecision,
)
from src.domain.operating_model.engine import (
    OperatingModelEvaluator,
    OperatingModelEngine,
)
from src.application.operating_model.autonomous_operating_service import (
    OperatingModelActionExecutor,
    AutonomousOperatingModelService,
)
from src.domain.capital.models import CapitalBudget, AllocationDecision, AllocationStatus
from src.domain.market_intelligence.models import Confidence, Money, MarketListing, MarketEvidence, Marketplace, TrendSignal, DemandSignal, SignalType
from src.domain.opportunity.models import Opportunity, OpportunityReadiness, EvidenceSufficiency
from src.domain.supplier_intelligence.models import (
    CommercialQuote,
    PriceTier,
    MOQInfo,
    ShippingOption,
    ShippingMethod,
    SupplierRiskProfile,
    SupplierRiskDimension,
    RiskLevel,
    EvidenceProvenanceType,
)


class MockOperatingModelDecisionProvider(DecisionProvider):
    """Proveedor secuencial de decisiones deterministas para el AutonomousLoop de D-03."""

    def __init__(self, steps: Optional[List[Dict[str, Any]]] = None):
        self.steps = steps or [
            {"action": LoopAction.CONTINUE, "parameters": {"action_type": "EVALUATE_INVENTORY"}, "reason": "Build inventory scenario"},
            {"action": LoopAction.CONTINUE, "parameters": {"action_type": "EVALUATE_DROPSHIPPING"}, "reason": "Build dropshipping scenario"},
            {"action": LoopAction.CONTINUE, "parameters": {"action_type": "COMPARE_MODELS"}, "reason": "Compare inventory vs dropshipping differentials"},
            {"action": LoopAction.PROMOTE, "parameters": {"action_type": "DECIDE_OPERATING_MODEL"}, "reason": "Apply policy and finalize operating decision"},
        ]
        self.current_step = 0

    def decide(self, state: LoopState) -> LoopDecision:
        if self.current_step < len(self.steps):
            step_info = self.steps[self.current_step]
            self.current_step += 1
            return LoopDecision(
                action=step_info["action"],
                reason=step_info["reason"],
                parameters=step_info.get("parameters", {}),
                confidence=0.95,
            )
        return LoopDecision(
            action=LoopAction.COMPLETE,
            reason="All autonomous operating steps executed",
            parameters={},
            confidence=1.0,
        )


def build_test_opportunity(product_id: str = "TEST-PROD-01") -> Opportunity:
    listing = MarketListing(
        external_id=f"EXT-{product_id}",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Gaming Mechanical Keyboard RGB",
        price=Money(amount=Decimal("45000"), currency="CLP"),
        sold_quantity=120,
        available_quantity=30,
        seller_id="SELLER-GAMER",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Computers & Accessories",
    )
    evidence = MarketEvidence(
        listing=listing,
        trend_signals=[TrendSignal(keyword="teclado mecanico", rank=1, matched=True, trend_score=Decimal("0.88"))],
        demand_signals=[DemandSignal(score=Decimal("85.0"), label="HIGH", confidence=Confidence.HIGH, signal_type=SignalType.OBSERVED)],
        confidence=Confidence.HIGH,
    )
    return Opportunity(
        opportunity_id=f"OPP-{product_id}",
        product_id=product_id,
        title="Gaming Mechanical Keyboard RGB",
        listing=listing,
        evidence=evidence,
        score=Decimal("85.0"),
        readiness=OpportunityReadiness.READY,
        confidence=Confidence.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
    )


def build_test_quote_and_shipping(supplier_id: str = "SUP-GAMING"):
    tiers = (
        PriceTier(min_quantity=1, max_quantity=9, unit_price=Decimal("25000"), currency="CLP"),
        PriceTier(min_quantity=10, max_quantity=100, unit_price=Decimal("16000"), currency="CLP"),
    )
    quote = CommercialQuote(
        quote_id=f"QUOTE-{supplier_id}",
        supplier_id=supplier_id,
        sku=f"SKU-{supplier_id}",
        unit_price=Decimal("25000"),
        currency="CLP",
        moq=MOQInfo(quantity=10),
        price_tiers=tiers,
        lead_time_days=4,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        shipping_cost=Decimal("2500"),
    )
    ship_unit = ShippingOption(
        shipping_cost=Decimal("2500"),
        currency="CLP",
        method=ShippingMethod.STANDARD,
        carrier="ChileExpress Direct",
        estimated_transit_days=2,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )
    ship_bulk = ShippingOption(
        shipping_cost=Decimal("12000"),
        currency="CLP",
        method=ShippingMethod.FREIGHT,
        carrier="TransCargo Bulk",
        estimated_transit_days=5,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )
    return quote, ship_unit, ship_bulk


def build_test_risk_profile(supplier_id: str = "SUP-GAMING") -> SupplierRiskProfile:
    dim = SupplierRiskDimension(dimension_name="all", risk_level=RiskLevel.LOW, risk_score=Decimal("10.0"))
    return SupplierRiskProfile(
        supplier_id=supplier_id,
        overall_risk_level=RiskLevel.LOW,
        overall_risk_score=Decimal("10.0"),
        operational_risk=dim,
        logistics_risk=dim,
        availability_risk=dim,
        evidence_risk=dim,
        commercial_risk=dim,
        confidence=Confidence.HIGH,
    )


def test_autonomous_operating_service_e2e_flow():
    """
    Validación End-to-End de la Misión D-03 integrada con AutonomousLoop:
    Opportunity -> Quote -> Profit -> Capital -> Inventory Scenario -> Dropshipping Scenario -> Comparison -> Decision.
    """
    opp = build_test_opportunity("E2E-001")
    quote, ship_unit, ship_bulk = build_test_quote_and_shipping("SUP-E2E")
    risk = build_test_risk_profile("SUP-E2E")
    budget = CapitalBudget.create(budget_id="BUDGET-E2E", total_capital=Decimal("4000000"), reserve_ratio=Decimal("0.10"))

    provider = MockOperatingModelDecisionProvider()
    service = AutonomousOperatingModelService(decision_provider=provider)

    result, decision = service.run_operating_model_mission(
        mission_id="MISSION-D03-E2E-01",
        opportunity=opp,
        quote=quote,
        budget=budget,
        supplier_risk_profile=risk,
        target_inventory_quantity=10,
    )

    assert result.status == MissionStatus.COMPLETED
    assert decision is not None
    assert decision.selected_model in (OperatingModelType.INVENTORY, OperatingModelType.DROPSHIPPING)
    assert decision.alternative_model is not None
    assert decision.explanation.economic_rationale != ""
    assert decision.explanation.capital_rationale != ""
    assert decision.explanation.risk_rationale != ""
    assert decision.explanation.evidence_summary != ""
    assert decision.provenance_type == EvidenceProvenanceType.LIVE
    assert decision.confidence == Confidence.HIGH
