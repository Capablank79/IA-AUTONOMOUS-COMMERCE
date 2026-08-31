from decimal import Decimal
from datetime import datetime, timezone
import pytest

from src.domain.operating_model.models import (
    OperatingModelType,
    OperatingDecisionType,
    DecisionTrigger,
    DemandVelocity,
    ObsolescenceRisk,
    InventoryScenario,
    DropshippingScenario,
    OperatingModelComparison,
    OperatingModelPolicy,
    DecisionExplanation,
    OperatingDecision,
    OperatingReassessmentRecord,
)
from src.domain.operating_model.engine import (
    OperatingModelEvaluator,
    OperatingModelEngine,
)
from src.domain.capital.models import (
    CapitalBudget,
    AllocationDecision,
    AllocationStatus,
    AllocationDecisionReason,
    CapitalExposure,
)
from src.domain.market_intelligence.models import (
    Confidence,
    MarketEvidence,
    MarketListing,
    Marketplace,
    Money,
    SignalType,
    TrendSignal,
    DemandSignal,
)
from src.domain.opportunity.models import (
    Opportunity,
    OpportunityReadiness,
    EvidenceSufficiency,
)
from src.domain.supplier_intelligence.models import (
    SupplierCandidate,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    SupplierRiskProfile,
    SupplierRiskDimension,
    CommercialQuote,
    PriceTier,
    MOQInfo,
    ShippingOption,
    ShippingMethod,
    EvidenceProvenanceType,
    RiskLevel,
    QuoteFreshness,
    SLAStatus,
)
from src.domain.profit.models import (
    CostComponent,
    CostComponentType,
    CostComponentStatus,
    ProfitStatus,
)


def create_sample_opportunity(
    product_id: str = "PROD-001",
    price_amount: Decimal = Decimal("50000"),
    sold_quantity: int = 150,
    trend_score: Decimal = Decimal("0.85"),
    confidence: Confidence = Confidence.HIGH,
) -> Opportunity:
    listing = MarketListing(
        external_id=f"EXT-{product_id}",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Wireless Ergonomic Mouse Pro",
        price=Money(amount=price_amount, currency="CLP"),
        sold_quantity=sold_quantity,
        available_quantity=50,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Electronics",
    )
    trend = TrendSignal(
        keyword="mouse ergonomico",
        rank=1,
        matched=True,
        trend_score=trend_score,
    )
    demand = DemandSignal(
        score=Decimal("88.0"),
        label="HIGH_DEMAND",
        confidence=confidence,
        signal_type=SignalType.OBSERVED,
    )
    evidence = MarketEvidence(
        listing=listing,
        trend_signals=[trend],
        demand_signals=[demand],
        confidence=confidence,
    )
    return Opportunity(
        opportunity_id=f"OPP-{product_id}",
        product_id=product_id,
        title="Wireless Ergonomic Mouse Pro",
        listing=listing,
        evidence=evidence,
        score=Decimal("88.0"),
        readiness=OpportunityReadiness.READY,
        confidence=confidence,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
    )


def create_sample_quote(
    supplier_id: str = "SUP-001",
    base_price: Decimal = Decimal("25000"),
    tier_price: Decimal = Decimal("18000"),
    moq: int = 20,
    shipping_unit_cost: Decimal = Decimal("3000"),
    shipping_bulk_cost: Decimal = Decimal("15000"),
    confidence: Confidence = Confidence.HIGH,
    provenance_type: EvidenceProvenanceType = EvidenceProvenanceType.LIVE,
    freshness: QuoteFreshness = QuoteFreshness.FRESH,
) -> CommercialQuote:
    tiers = (
        PriceTier(min_quantity=1, max_quantity=19, unit_price=base_price, currency="CLP"),
        PriceTier(min_quantity=20, max_quantity=100, unit_price=tier_price, currency="CLP"),
    )
    shipping_unit = ShippingOption(
        shipping_cost=shipping_unit_cost,
        currency="CLP",
        method=ShippingMethod.STANDARD,
        carrier="Direct Express",
        estimated_transit_days=3,
        confidence=confidence,
        provenance_type=provenance_type,
    )
    shipping_bulk = ShippingOption(
        shipping_cost=shipping_bulk_cost,
        currency="CLP",
        method=ShippingMethod.FREIGHT,
        carrier="Bulk Freight",
        estimated_transit_days=7,
        confidence=confidence,
        provenance_type=provenance_type,
    )
    return CommercialQuote(
        quote_id=f"QUOTE-{supplier_id}",
        supplier_id=supplier_id,
        sku=f"SKU-{supplier_id}",
        unit_price=base_price,
        currency="CLP",
        moq=MOQInfo(quantity=moq),
        price_tiers=tiers,
        lead_time_days=5,
        confidence=confidence,
        provenance_type=provenance_type,
        shipping_cost=shipping_unit_cost,
    ), shipping_unit, shipping_bulk


def create_sample_risk_profile(
    supplier_id: str = "SUP-001",
    risk_level: RiskLevel = RiskLevel.LOW,
) -> SupplierRiskProfile:
    op_risk = SupplierRiskDimension(
        dimension_name="operational",
        risk_level=risk_level,
        risk_score=Decimal("15.0"),
    )
    log_risk = SupplierRiskDimension(
        dimension_name="logistics",
        risk_level=risk_level,
        risk_score=Decimal("20.0"),
    )
    avail_risk = SupplierRiskDimension(
        dimension_name="availability",
        risk_level=risk_level,
        risk_score=Decimal("10.0"),
    )
    ev_risk = SupplierRiskDimension(
        dimension_name="evidence",
        risk_level=risk_level,
        risk_score=Decimal("10.0"),
    )
    comm_risk = SupplierRiskDimension(
        dimension_name="commercial",
        risk_level=risk_level,
        risk_score=Decimal("15.0"),
    )
    return SupplierRiskProfile(
        supplier_id=supplier_id,
        overall_risk_level=risk_level,
        overall_risk_score=Decimal("14.0"),
        operational_risk=op_risk,
        logistics_risk=log_risk,
        availability_risk=avail_risk,
        evidence_risk=ev_risk,
        commercial_risk=comm_risk,
        confidence=Confidence.HIGH,
    )


# ==============================================================================
# TESTS UNITARIOS DE MODELOS Y EVALUADOR
# ==============================================================================

def test_inventory_scenario_build_known_values():
    opp = create_sample_opportunity()
    quote, ship_unit, ship_bulk = create_sample_quote()
    risk = create_sample_risk_profile()
    
    scenario = OperatingModelEvaluator.build_inventory_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk,
        target_quantity=20,
        shipping_option=ship_bulk,
    )
    
    assert scenario.opportunity_id == "PROD-001"
    assert scenario.target_quantity == 20
    assert scenario.moq == 20
    assert scenario.is_viable_economically is True
    # 20 units * 18000 + 15000 shipping = 375,000 CLP
    assert scenario.required_capital == Decimal("375000")
    assert scenario.stock_exposure == Decimal("375000")
    assert scenario.demand_velocity == DemandVelocity.HIGH
    assert scenario.obsolescence_risk == ObsolescenceRisk.LOW
    assert scenario.expected_margin_pct is not None
    assert scenario.expected_profit is not None


def test_dropshipping_scenario_build_known_values():
    opp = create_sample_opportunity()
    quote, ship_unit, ship_bulk = create_sample_quote()
    risk = create_sample_risk_profile()

    scenario = OperatingModelEvaluator.build_dropshipping_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk,
        direct_shipping_option=ship_unit,
    )

    assert scenario.opportunity_id == "PROD-001"
    assert scenario.supplier_id == "SUP-001"
    # Unit price 25000 + shipping 3000 = 28000 CLP
    assert scenario.required_operational_capital == Decimal("28000")
    assert scenario.is_viable_economically is True
    assert scenario.expected_margin_pct is not None
    assert scenario.supplier_risk_level == RiskLevel.LOW
    assert scenario.supplier_sla_compliant is True


def test_comparison_computes_differentials_correctly():
    opp = create_sample_opportunity()
    quote, ship_unit, ship_bulk = create_sample_quote()
    risk = create_sample_risk_profile()

    inv = OperatingModelEvaluator.build_inventory_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk,
        target_quantity=20,
        shipping_option=ship_bulk,
    )
    drop = OperatingModelEvaluator.build_dropshipping_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk,
        direct_shipping_option=ship_unit,
    )

    comparison = OperatingModelEvaluator.compare_scenarios(inv, drop)

    assert comparison.profit_differential is not None
    assert comparison.margin_differential_pct is not None
    # Inventory margin should be higher than dropshipping due to volume tier
    assert comparison.margin_differential_pct > Decimal("0")
    assert comparison.capital_differential == Decimal("375000") - Decimal("28000")
    assert len(comparison.inventory_advantages) > 0
    assert len(comparison.dropshipping_advantages) > 0


# ==============================================================================
# MARCHA BLANCA A — INVENTORY CLEAR WINNER
# Demanda validada alta, margen superior significativo (+15%), capital disponible amplio, proveedor confiable
# ==============================================================================

def test_marcha_blanca_a_inventory_preferred():
    opp = create_sample_opportunity(sold_quantity=200, trend_score=Decimal("0.90"))
    # Tier price 15,000 vs base 30,000 (huge volume margin advantage)
    quote, ship_unit, ship_bulk = create_sample_quote(
        base_price=Decimal("30000"),
        tier_price=Decimal("15000"),
        moq=25,
        shipping_unit_cost=Decimal("4000"),
        shipping_bulk_cost=Decimal("20000"),
    )
    risk = create_sample_risk_profile(risk_level=RiskLevel.LOW)
    budget = CapitalBudget.create(budget_id="BUDGET-1", total_capital=Decimal("5000000"), reserve_ratio=Decimal("0.10"))

    inv = OperatingModelEvaluator.build_inventory_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk,
        target_quantity=25,
        shipping_option=ship_bulk,
    )
    drop = OperatingModelEvaluator.build_dropshipping_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk,
        direct_shipping_option=ship_unit,
    )
    comparison = OperatingModelEvaluator.compare_scenarios(inv, drop)

    decision = OperatingModelEngine.evaluate_operating_decision(
        comparison=comparison,
        capital_budget=budget,
    )

    assert decision.selected_model == OperatingModelType.INVENTORY
    assert decision.alternative_model == OperatingModelType.DROPSHIPPING
    assert decision.decision_type == OperatingDecisionType.SELECT_INVENTORY
    assert decision.is_actionable is True
    assert "superior economics" in decision.explanation.economic_rationale.lower()
    assert decision.confidence == Confidence.HIGH


# ==============================================================================
# MARCHA BLANCA B — DROPSHIPPING CLEAR WINNER
# MOQ alto o margen incremental mínimo (<5%), o capital limitado, o riesgo de obsolescencia
# ==============================================================================

def test_marcha_blanca_b_dropshipping_preferred_low_margin_delta():
    opp = create_sample_opportunity(sold_quantity=50)
    # Tier price almost same as base (1% delta), but bulk shipping adds overhead
    quote, ship_unit, ship_bulk = create_sample_quote(
        base_price=Decimal("25000"),
        tier_price=Decimal("24500"),
        moq=20,
        shipping_unit_cost=Decimal("2000"),
        shipping_bulk_cost=Decimal("30000"),
    )
    risk = create_sample_risk_profile(risk_level=RiskLevel.LOW)
    budget = CapitalBudget.create(budget_id="BUDGET-1", total_capital=Decimal("2000000"), reserve_ratio=Decimal("0.10"))

    inv = OperatingModelEvaluator.build_inventory_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk,
        target_quantity=20,
        shipping_option=ship_bulk,
    )
    drop = OperatingModelEvaluator.build_dropshipping_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk,
        direct_shipping_option=ship_unit,
    )
    comparison = OperatingModelEvaluator.compare_scenarios(inv, drop)

    decision = OperatingModelEngine.evaluate_operating_decision(
        comparison=comparison,
        capital_budget=budget,
    )

    assert decision.selected_model == OperatingModelType.DROPSHIPPING
    assert decision.alternative_model == OperatingModelType.INVENTORY
    assert decision.decision_type == OperatingDecisionType.SELECT_DROPSHIPPING
    assert "below required threshold" in decision.explanation.economic_rationale.lower()
    assert len(decision.conditions) > 0


# ==============================================================================
# MARCHA BLANCA C — CRITICAL UNKNOWNS / INSUFFICIENT EVIDENCE
# Flete desconocido en ambos modelos -> NO_DECISION / NEEDS_INVESTIGATION sin forzar elección
# ==============================================================================

def test_marcha_blanca_c_missing_shipping_results_in_no_decision():
    opp = create_sample_opportunity()
    # Quote without shipping cost
    quote = CommercialQuote(
        quote_id="QUOTE-NO-SHIP",
        supplier_id="SUP-UNKNOWN",
        sku="SKU-UNK",
        unit_price=Decimal("25000"),
        currency="CLP",
        moq=MOQInfo(quantity=10),
        price_tiers=(),
        confidence=Confidence.LOW,
        provenance_type=EvidenceProvenanceType.FIXTURE,
        shipping_cost=None,
    )
    budget = CapitalBudget.create(budget_id="BUDGET-1", total_capital=Decimal("1000000"))

    inv = OperatingModelEvaluator.build_inventory_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=None,
        target_quantity=10,
    )
    drop = OperatingModelEvaluator.build_dropshipping_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=None,
    )
    comparison = OperatingModelEvaluator.compare_scenarios(inv, drop)

    decision = OperatingModelEngine.evaluate_operating_decision(
        comparison=comparison,
        capital_budget=budget,
    )

    assert decision.selected_model == OperatingModelType.NO_DECISION
    assert decision.decision_type == OperatingDecisionType.NO_DECISION
    assert "INVENTORY_SHIPPING_UNKNOWN" in decision.unknowns
    assert "DROPSHIPPING_SHIPPING_UNKNOWN" in decision.unknowns
    assert decision.is_actionable is False


# ==============================================================================
# MARCHA BLANCA D — DYNAMIC REASSESSMENT AND PIVOT
# INVENTORY -> Supplier SLA deteriorates or Demand slows -> REASSESS -> PIVOT TO DROPSHIPPING
# ==============================================================================

def test_marcha_blanca_d_reassessment_and_pivot():
    opp = create_sample_opportunity(sold_quantity=150, trend_score=Decimal("0.85"))
    quote, ship_unit, ship_bulk = create_sample_quote(base_price=Decimal("30000"), tier_price=Decimal("16000"), moq=20)
    risk_good = create_sample_risk_profile(risk_level=RiskLevel.LOW)
    budget = CapitalBudget.create(budget_id="BUDGET-1", total_capital=Decimal("3000000"))

    # Initial decision: Inventory is selected
    inv1 = OperatingModelEvaluator.build_inventory_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk_good,
        target_quantity=20,
        shipping_option=ship_bulk,
    )
    drop1 = OperatingModelEvaluator.build_dropshipping_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk_good,
        direct_shipping_option=ship_unit,
    )
    comp1 = OperatingModelEvaluator.compare_scenarios(inv1, drop1)
    decision1 = OperatingModelEngine.evaluate_operating_decision(comparison=comp1, capital_budget=budget)

    assert decision1.selected_model == OperatingModelType.INVENTORY

    # Trigger: Demand slows down dramatically + Trend collapses
    opp_deteriorated = create_sample_opportunity(sold_quantity=0, trend_score=Decimal("0.10"))
    inv2 = OperatingModelEvaluator.build_inventory_scenario(
        opportunity=opp_deteriorated,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk_good,
        target_quantity=20,
        shipping_option=ship_bulk,
    )
    drop2 = OperatingModelEvaluator.build_dropshipping_scenario(
        opportunity=opp_deteriorated,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=risk_good,
        direct_shipping_option=ship_unit,
    )
    comp2 = OperatingModelEvaluator.compare_scenarios(inv2, drop2)

    reassessment = OperatingModelEngine.reassess_decision(
        previous_decision=decision1,
        new_comparison=comp2,
        capital_budget=budget,
        trigger=DecisionTrigger.DEMAND_CHANGE,
        reason="Market demand velocity dropped to STAGNANT and trend collapsed to 0.10",
    )

    assert reassessment.pivoted is True
    assert reassessment.previous_decision.selected_model == OperatingModelType.INVENTORY
    assert reassessment.new_decision.selected_model == OperatingModelType.DROPSHIPPING
    assert reassessment.trigger == DecisionTrigger.DEMAND_CHANGE


# ==============================================================================
# ANTI-FABRICATION AND STRICT UNKNOWN TESTS
# ==============================================================================

def test_anti_fabrication_unknown_rotation_and_lead_time():
    # Opportunity without listing sold quantity -> demand velocity is UNKNOWN
    listing = MarketListing(
        external_id="EXT-NO-SALES",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Unknown item",
        price=Money(amount=Decimal("20000"), currency="CLP"),
        sold_quantity=None,
        available_quantity=10,
        seller_id="S1",
        condition="new",
        shipping_info={},
        category="General",
    )
    opp = Opportunity(
        opportunity_id="OPP-UNK",
        product_id="PROD-UNK",
        title="Unknown item",
        listing=listing,
        evidence=MarketEvidence(listing=listing, confidence=Confidence.UNKNOWN),
        score=Decimal("50.0"),
        readiness=OpportunityReadiness.NEEDS_INVESTIGATION,
        confidence=Confidence.UNKNOWN,
        evidence_sufficiency=EvidenceSufficiency.PARTIAL,
    )
    quote, ship_unit, ship_bulk = create_sample_quote()
    inv = OperatingModelEvaluator.build_inventory_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=None,
        target_quantity=20,
    )
    assert inv.demand_velocity == DemandVelocity.UNKNOWN
    assert "DEMAND_ROTATION_UNKNOWN" in inv.unknowns
    # UNKNOWN velocity prevents forced inventory approval under default policy
    budget = CapitalBudget.create(budget_id="B1", total_capital=Decimal("1000000"))
    drop = OperatingModelEvaluator.build_dropshipping_scenario(
        opportunity=opp,
        quote=quote,
        supplier_recommendation=None,
        supplier_risk_profile=None,
        direct_shipping_option=ship_unit,
    )
    comp = OperatingModelEvaluator.compare_scenarios(inv, drop)
    dec = OperatingModelEngine.evaluate_operating_decision(comparison=comp, capital_budget=budget)
    # Inventory cannot be selected because demand is UNKNOWN
    assert dec.selected_model != OperatingModelType.INVENTORY
