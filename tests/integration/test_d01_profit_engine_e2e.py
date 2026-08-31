from decimal import Decimal
from datetime import datetime, timezone
import pytest

from src.domain.market_intelligence.models import (
    Confidence,
    Money,
    MarketListing,
    Marketplace,
    MarketEvidence,
)
from src.domain.supplier_intelligence.models import (
    PriceTier,
    MOQInfo,
    ShippingOption,
    ShippingMethod,
    CommercialQuote,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    EvidenceProvenanceType,
    PrimarySupplierSelection,
)
from src.domain.opportunity.models import (
    Opportunity,
    OpportunityReadiness,
    EvidenceSufficiency,
)
from src.domain.profit.models import (
    CostComponent,
    CostComponentType,
    CostComponentStatus,
    LandedCostStatus,
    ProfitStatus,
    MarketplaceFeeStructure,
    ExchangeRate,
)
from src.domain.mission.models import LoopAction, MissionStatus
from src.application.profit.autonomous_profit_service import AutonomousProfitService


# ============================================================================
# MARCHA BLANCA A — COMPLETE ECONOMICS
# ============================================================================

def test_marcha_blanca_a_complete_economics():
    """
    Escenario A: Todos los componentes necesarios están disponibles.
    Debe producir:
    - LANDED COST
    - GROSS PROFIT
    - NET PROFIT
    - GROSS MARGIN
    - NET MARGIN
    - Decision: CONVERGE
    - Confidence adecuada
    """
    quote = CommercialQuote(
        quote_id="Q-MB-A",
        supplier_id="SUPP-MB-A",
        sku="SKU-MB-A",
        unit_price=Decimal("10000"),
        currency="CLP",
        moq=MOQInfo(quantity=5, notes="MOQ 5"),
        shipping_cost=Decimal("10000"), # 10000 / 5 = 2000 per unit
        lead_time_days=3,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        source="TEST_SUPPLIER",
    )

    shipping = ShippingOption(
        shipping_cost=Decimal("10000"),
        currency="CLP",
        carrier="CHILEXPRESS",
        method=ShippingMethod.EXPRESS,
        estimated_transit_days=2,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )

    primary_sel = PrimarySupplierSelection(
        supplier_id="SUPP-MB-A",
        supplier_name="Chile Express Supplier",
        sku="SKU-MB-A",
        commercial_score=Decimal("90.0"),
        reliability_score=Decimal("95.0"),
        overall_risk_score=Decimal("10.0"),
        composite_suitability_score=Decimal("92.0"),
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        selection_reason="Best total cost and fast shipping",
        why_over_fallback="Cheaper unit price",
        commercial_position="Strong",
        logistics_position="Express 2 days",
    )

    rec = SupplierRecommendation(
        recommendation_id="REC-MB-A",
        opportunity_id="OPP-MB-A",
        target_product_title="Smartwatch Fitness Tracker",
        target_sku="SKU-MB-A",
        decision=SupplierRecommendationDecision.RECOMMEND,
        decision_reason="Best total cost and fast shipping",
        primary_supplier=primary_sel,
        fallback_supplier=None,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    listing = MarketListing(
        external_id="EXT-MB-A",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Smartwatch Fitness Tracker",
        price=Money(amount=Decimal("25000"), currency="CLP"),
        sold_quantity=100,
        available_quantity=50,
        seller_id="SELLER-A",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Wearables",
    )
    evidence = MarketEvidence(
        listing=listing,
        confidence=Confidence.HIGH,
    )
    opp = Opportunity(
        opportunity_id="OPP-MB-A",
        product_id="PROD-MB-A",
        title="Smartwatch Fitness Tracker",
        listing=listing,
        evidence=evidence,
        score=Decimal("85.0"),
        confidence=Confidence.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        readiness=OpportunityReadiness.READY,
    )

    mkt_fee = MarketplaceFeeStructure(
        marketplace="MERCADO_LIBRE",
        category="Wearables",
        fee_rate=Decimal("0.12"),
        fixed_fee=Decimal("500"),
        currency="CLP",
        source="MELI_FEE_CARD",
    )

    service = AutonomousProfitService()
    mission_res = service.execute_profit_mission(
        opportunity=opp,
        recommendation=rec,
        quote=quote,
        shipping_option=shipping,
        marketplace_fee_structure=mkt_fee,
    )

    assert mission_res.status == MissionStatus.COMPLETED
    eval_res = mission_res.output["economic_evaluation"]
    unit_eco = eval_res.primary_unit_economics
    landed = unit_eco.landed_cost

    assert landed.status == LandedCostStatus.COMPLETE
    # 10000 purchase + (10000 shipping / 5 units) = 12000 CLP unit landed cost
    assert landed.unit_landed_cost == Decimal("12000")

    assert unit_eco.status == ProfitStatus.PROFIT_COMPLETE
    # Sale price = 25000
    # Gross Profit = 25000 - 12000 = 13000
    # Gross Margin % = (13000 / 25000) * 100 = 52.0%
    assert unit_eco.gross_profit == Decimal("13000")
    assert unit_eco.gross_margin_pct == Decimal("52.00")

    # Mkt fee = 25000 * 0.12 + 500 = 3000 + 500 = 3500
    # Net Profit = 13000 - 3500 = 9500
    # Net Margin % = (9500 / 25000) * 100 = 38.0%
    assert unit_eco.net_profit == Decimal("9500")
    assert unit_eco.net_margin_pct == Decimal("38.00")

    # Traceability exists
    assert len(unit_eco.trace) > 0
    assert len(unit_eco.unknowns) == 0


# ============================================================================
# MARCHA BLANCA B — INCOMPLETE ECONOMICS (UNKNOWN CRITICAL COST)
# ============================================================================

def test_marcha_blanca_b_incomplete_economics():
    """
    Escenario B: Falta al menos un costo crítico (Flete desconocido).
    Debe producir:
    - PARTIAL / INCOMPLETE
    - Señalar exactamente qué falta
    - Decision: INVESTIGATE
    - Planificar necesidad de investigación con alta prioridad
    """
    quote = CommercialQuote(
        quote_id="Q-MB-B",
        supplier_id="SUPP-MB-B",
        sku="SKU-MB-B",
        unit_price=Decimal("8000"),
        currency="CLP",
        moq=MOQInfo(quantity=1, notes="MOQ 1"),
        shipping_cost=None,  # Flete desconocido
        lead_time_days=5,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        source="TEST_SUPPLIER",
    )

    primary_sel = PrimarySupplierSelection(
        supplier_id="SUPP-MB-B",
        supplier_name="Chile Supplier B",
        sku="SKU-MB-B",
        commercial_score=Decimal("80.0"),
        reliability_score=Decimal("85.0"),
        overall_risk_score=Decimal("15.0"),
        composite_suitability_score=Decimal("82.0"),
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        selection_reason="Best wholesale unit price",
        why_over_fallback="Cheapest unit price",
        commercial_position="Low cost",
        logistics_position="Standard pending quote",
    )

    rec = SupplierRecommendation(
        recommendation_id="REC-MB-B",
        opportunity_id="OPP-MB-B",
        target_product_title="Lámpara LED Escritorio",
        target_sku="SKU-MB-B",
        decision=SupplierRecommendationDecision.RECOMMEND_WITH_CONDITIONS,
        decision_reason="Requires shipping verification",
        primary_supplier=primary_sel,
        fallback_supplier=None,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    listing = MarketListing(
        external_id="EXT-MB-B",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Lámpara LED Escritorio",
        price=Money(amount=Decimal("19990"), currency="CLP"),
        sold_quantity=50,
        available_quantity=20,
        seller_id="SELLER-B",
        condition="new",
        shipping_info={"free_shipping": False},
        category="Iluminación",
    )
    evidence = MarketEvidence(
        listing=listing,
        confidence=Confidence.HIGH,
    )
    opp = Opportunity(
        opportunity_id="OPP-MB-B",
        product_id="PROD-MB-B",
        title="Lámpara LED Escritorio",
        listing=listing,
        evidence=evidence,
        score=Decimal("80.0"),
        confidence=Confidence.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        readiness=OpportunityReadiness.READY,
    )

    service = AutonomousProfitService()
    mission_res = service.execute_profit_mission(
        opportunity=opp,
        recommendation=rec,
        quote=quote,
    )

    eval_res = mission_res.output["economic_evaluation"]
    unit_eco = eval_res.primary_unit_economics
    landed = unit_eco.landed_cost

    assert landed.status == LandedCostStatus.INCOMPLETE
    assert landed.total_landed_cost is None
    assert landed.unit_landed_cost is None
    assert any("SHIPPING_COST" in u for u in landed.unknowns)

    assert unit_eco.status == ProfitStatus.PROFIT_INCOMPLETE
    assert unit_eco.net_profit is None
    assert unit_eco.gross_profit is None

    # Investigation needs detected
    inv_needs = eval_res.investigation_needs
    assert len(inv_needs) > 0
    assert any(n.component_type == CostComponentType.SHIPPING_COST for n in inv_needs)


# ============================================================================
# MARCHA BLANCA C — NON-COMPARABLE CURRENCIES (MISSING FX)
# ============================================================================

def test_marcha_blanca_c_non_comparable_currencies():
    """
    Escenario C: Datos en monedas diferentes sin FX confiable.
    Debe producir:
    - NOT_COMPARABLE_CURRENCY
    - NO fabricar conversión con tasa inventada
    - Decision: INVESTIGATE o PIVOT
    """
    quote = CommercialQuote(
        quote_id="Q-MB-C",
        supplier_id="SUPP-MB-C",
        sku="SKU-MB-C",
        unit_price=Decimal("15.0"),  # Currency USD
        currency="USD",
        moq=MOQInfo(quantity=10, notes="MOQ 10"),
        shipping_cost=Decimal("20.0"),  # Currency USD
        lead_time_days=10,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        source="ALIBABA",
    )

    shipping = ShippingOption(
        shipping_cost=Decimal("20.0"),
        currency="USD",
        carrier="FEDEX",
        method=ShippingMethod.STANDARD,
        estimated_transit_days=7,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )

    primary_sel = PrimarySupplierSelection(
        supplier_id="SUPP-MB-C",
        supplier_name="Shenzhen Electronics Co.",
        sku="SKU-MB-C",
        commercial_score=Decimal("85.0"),
        reliability_score=Decimal("90.0"),
        overall_risk_score=Decimal("20.0"),
        composite_suitability_score=Decimal("86.0"),
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        selection_reason="Alibaba verified factory",
        why_over_fallback="Direct manufacturer",
        commercial_position="USD direct price",
        logistics_position="FedEx 7 days",
    )

    rec = SupplierRecommendation(
        recommendation_id="REC-MB-C",
        opportunity_id="OPP-MB-C",
        target_product_title="Micrófono Condensador USB",
        target_sku="SKU-MB-C",
        decision=SupplierRecommendationDecision.RECOMMEND,
        decision_reason="Direct manufacturer price",
        primary_supplier=primary_sel,
        fallback_supplier=None,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    listing = MarketListing(
        external_id="EXT-MB-C",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Micrófono Condensador USB",
        price=Money(amount=Decimal("49990"), currency="CLP"),
        sold_quantity=70,
        available_quantity=30,
        seller_id="SELLER-C",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Audio",
    )
    evidence = MarketEvidence(
        listing=listing,
        confidence=Confidence.HIGH,
    )
    opp = Opportunity(
        opportunity_id="OPP-MB-C",
        product_id="PROD-MB-C",
        title="Micrófono Condensador USB",
        listing=listing,
        evidence=evidence,
        score=Decimal("82.0"),
        confidence=Confidence.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        readiness=OpportunityReadiness.READY,
    )

    # Note: NO exchange rate is provided!
    service = AutonomousProfitService()
    mission_res = service.execute_profit_mission(
        opportunity=opp,
        recommendation=rec,
        quote=quote,
        shipping_option=shipping,
        exchange_rate=None,
    )

    eval_res = mission_res.output["economic_evaluation"]
    landed = eval_res.primary_unit_economics.landed_cost

    assert landed.status == LandedCostStatus.NOT_COMPARABLE_CURRENCY
    assert landed.total_landed_cost is None
    assert eval_res.primary_unit_economics.status == ProfitStatus.NOT_COMPARABLE_CURRENCY


# ============================================================================
# BREAK-EVEN E2E VALIDATION
# ============================================================================

def test_break_even_e2e_mathematical_validation():
    """
    Demostración y validación matemática de break-even sale price.
    """
    quote = CommercialQuote(
        quote_id="Q-BE",
        supplier_id="SUPP-BE",
        sku="SKU-BE",
        unit_price=Decimal("10000"),
        currency="CLP",
        moq=MOQInfo(quantity=1, notes="MOQ 1"),
        shipping_cost=Decimal("2000"),
        lead_time_days=2,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        source="TEST_SUPPLIER",
    )

    shipping = ShippingOption(
        shipping_cost=Decimal("2000"),
        currency="CLP",
        carrier="CHILEXPRESS",
        method=ShippingMethod.STANDARD,
        estimated_transit_days=2,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )

    primary_sel = PrimarySupplierSelection(
        supplier_id="SUPP-BE",
        supplier_name="National Distributor",
        sku="SKU-BE",
        commercial_score=Decimal("90.0"),
        reliability_score=Decimal("92.0"),
        overall_risk_score=Decimal("8.0"),
        composite_suitability_score=Decimal("91.0"),
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        selection_reason="Fast local fulfillment",
        why_over_fallback="Direct local distributor",
        commercial_position="Standard wholesale",
        logistics_position="ChileExpress 2 days",
    )

    rec = SupplierRecommendation(
        recommendation_id="REC-BE",
        opportunity_id="OPP-BE",
        target_product_title="Teclado Mecánico RGB",
        target_sku="SKU-BE",
        decision=SupplierRecommendationDecision.RECOMMEND,
        decision_reason="Best local supplier",
        primary_supplier=primary_sel,
        fallback_supplier=None,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    listing = MarketListing(
        external_id="EXT-MB-BE",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Teclado Mecánico RGB",
        price=Money(amount=Decimal("25000"), currency="CLP"),
        sold_quantity=150,
        available_quantity=40,
        seller_id="SELLER-BE",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Computación",
    )
    evidence = MarketEvidence(
        listing=listing,
        confidence=Confidence.HIGH,
    )
    opp = Opportunity(
        opportunity_id="OPP-BE",
        product_id="PROD-BE",
        title="Teclado Mecánico RGB",
        listing=listing,
        evidence=evidence,
        score=Decimal("89.0"),
        confidence=Confidence.HIGH,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
        readiness=OpportunityReadiness.READY,
    )

    mkt_fee = MarketplaceFeeStructure(
        marketplace="MERCADO_LIBRE",
        category="Computación",
        fee_rate=Decimal("0.13"),
        fixed_fee_amount=Decimal("500"),
        currency="CLP",
        source="MELI_FEE_CARD",
    )

    service = AutonomousProfitService()
    mission_res = service.execute_profit_mission(
        opportunity=opp,
        recommendation=rec,
        quote=quote,
        shipping_option=shipping,
        marketplace_fee_structure=mkt_fee,
    )

    eval_res = mission_res.output["economic_evaluation"]
    be = eval_res.break_even

    assert be.is_calculable is True
    # Landed unit cost = 10000 + 2000 = 12000
    # Fixed fee = 500
    # Fee rate = 0.13
    # P_be = (12000 + 500) / (1 - 0.13) = 12500 / 0.87 = 14367.81609... -> 14367.82 CLP
    assert be.break_even_sale_price == Decimal("14367.82")

    # Mathematical proof:
    sale_price = be.break_even_sale_price
    mkt_commission = (sale_price * Decimal("0.13")) + Decimal("500")
    total_cost = Decimal("12000") + mkt_commission
    net_profit = sale_price - total_cost
    assert round(net_profit, 2) == Decimal("0.00")
