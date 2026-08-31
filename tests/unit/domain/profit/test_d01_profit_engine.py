from decimal import Decimal
from datetime import datetime, timezone
import pytest

from src.domain.market_intelligence.models import (
    Confidence,
    Money,
)
from src.domain.supplier_intelligence.models import (
    PriceTier,
    MOQInfo,
    ShippingOption,
    ShippingMethod,
    CommercialQuote,
    EvidenceProvenanceType,
)
from src.domain.profit.models import (
    CostComponent,
    CostComponentType,
    CostComponentStatus,
    LandedCost,
    LandedCostStatus,
    SalePrice,
    SalePriceType,
    UnitEconomics,
    ProfitStatus,
    MarketplaceFeeStructure,
    ExchangeRate,
    BreakEvenResult,
    ScenarioAnalysisResult,
    EconomicInvestigationNeed,
    EconomicEvaluationResult,
)
from src.domain.profit.engine import (
    LandedCostCalculator,
    UnitEconomicsCalculator,
    BreakEvenCalculator,
    EconomicScenarioAnalyzer,
    EconomicInvestigationDetector,
    ProfitEngine,
)


# ============================================================================
# 1. TEST COST COMPONENTS
# ============================================================================

def test_cost_component_creation_and_unknown():
    # Known component
    known = CostComponent.known(
        component_type=CostComponentType.PRODUCT_COST,
        amount=Decimal("15000"),
        currency="CLP",
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        source="PROVEEDOR_A",
    )
    assert known.is_known is True
    assert known.status == CostComponentStatus.KNOWN
    assert known.amount == Decimal("15000")

    # Unknown component - never 0!
    unknown = CostComponent.unknown(
        component_type=CostComponentType.SHIPPING_COST,
        currency="CLP",
        details="Cotización de flete no recibida",
    )
    assert unknown.is_known is False
    assert unknown.status == CostComponentStatus.UNKNOWN
    assert unknown.amount is None

    # Not applicable component
    not_app = CostComponent.not_applicable(
        component_type=CostComponentType.IMPORT_DUTIES,
        currency="CLP",
        details="Producto local, sin arancel",
    )
    assert not_app.status == CostComponentStatus.NOT_APPLICABLE
    assert not_app.amount == Decimal("0")


def test_cost_component_fee_calculation():
    fee_comp = CostComponent.known(
        component_type=CostComponentType.MARKETPLACE_FEES,
        fee_rate=Decimal("0.13"),
        fixed_fee_amount=Decimal("500"),
        currency="CLP",
    )
    sale_price = Decimal("20000")
    # 20000 * 0.13 + 500 = 2600 + 500 = 3100
    calculated = fee_comp.calculate_amount(sale_price)
    assert calculated == Decimal("3100")


# ============================================================================
# 2. TEST LANDED COST DETERMINISM & COMPLETENESS
# ============================================================================

def test_landed_cost_complete_calculation():
    purchase = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10000"), currency="CLP")
    shipping = CostComponent.known(CostComponentType.SHIPPING_COST, amount=Decimal("2000"), currency="CLP")
    duties = CostComponent.known(CostComponentType.IMPORT_DUTIES, amount=Decimal("600"), currency="CLP")
    taxes = CostComponent.known(CostComponentType.TAXES, amount=Decimal("1900"), currency="CLP")
    other = CostComponent.known(CostComponentType.OTHER_VARIABLE_COSTS, amount=Decimal("500"), currency="CLP")

    landed = LandedCostCalculator.calculate(
        product_id="PROD-1",
        supplier_id="SUPP-1",
        quantity=1,
        purchase_cost=purchase,
        shipping_cost=shipping,
        duties_cost=duties,
        taxes_cost=taxes,
        other_acquisition_cost=other,
    )

    assert landed.status == LandedCostStatus.COMPLETE
    assert landed.total_landed_cost == Decimal("15000")
    assert landed.unit_landed_cost == Decimal("15000")
    assert len(landed.unknowns) == 0


def test_landed_cost_incomplete_when_shipping_unknown():
    purchase = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10000"), currency="CLP")
    shipping = CostComponent.unknown(CostComponentType.SHIPPING_COST, currency="CLP")

    landed = LandedCostCalculator.calculate(
        product_id="PROD-1",
        supplier_id="SUPP-1",
        quantity=10,
        purchase_cost=purchase,
        shipping_cost=shipping,
    )

    assert landed.status == LandedCostStatus.INCOMPLETE
    assert landed.total_landed_cost is None
    assert landed.unit_landed_cost is None
    assert any("SHIPPING_COST" in u for u in landed.unknowns)


def test_landed_cost_currency_mismatch_without_fx():
    purchase = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10"), currency="USD")
    shipping = CostComponent.known(CostComponentType.SHIPPING_COST, amount=Decimal("2000"), currency="CLP")

    landed = LandedCostCalculator.calculate(
        product_id="PROD-1",
        supplier_id="SUPP-1",
        quantity=1,
        purchase_cost=purchase,
        shipping_cost=shipping,
        target_currency="CLP",
        exchange_rates=None,
    )

    assert landed.status == LandedCostStatus.NOT_COMPARABLE_CURRENCY
    assert landed.total_landed_cost is None


def test_landed_cost_with_valid_fx():
    purchase = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10"), currency="USD")
    shipping = CostComponent.known(CostComponentType.SHIPPING_COST, amount=Decimal("2000"), currency="CLP")

    fx = ExchangeRate(
        from_currency="USD",
        to_currency="CLP",
        rate=Decimal("950.0"),
        source="BANCO_CENTRAL_OBSERVED",
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )

    landed = LandedCostCalculator.calculate(
        product_id="PROD-1",
        supplier_id="SUPP-1",
        quantity=1,
        purchase_cost=purchase,
        shipping_cost=shipping,
        target_currency="CLP",
        exchange_rates={("USD", "CLP"): fx},
    )

    assert landed.status == LandedCostStatus.COMPLETE
    # 10 * 950 + 2000 = 9500 + 2000 = 11500
    assert landed.total_landed_cost == Decimal("11500.0")


# ============================================================================
# 3. TEST UNIT ECONOMICS & PROFIT FORMULAS
# ============================================================================

def test_unit_economics_complete_formulas():
    sale_price = SalePrice.observed(
        amount=Decimal("30000"),
        currency="CLP",
        source="MELI_OBSERVED_PRICE",
        confidence=Confidence.HIGH,
    )
    purchase = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10000"), currency="CLP")
    shipping = CostComponent.known(CostComponentType.SHIPPING_COST, amount=Decimal("2000"), currency="CLP")
    packaging = CostComponent.known(CostComponentType.PACKAGING, amount=Decimal("500"), currency="CLP")
    fulfillment = CostComponent.known(CostComponentType.FULFILLMENT, amount=Decimal("500"), currency="CLP")

    mkt_fee = CostComponent.known(
        CostComponentType.MARKETPLACE_FEES,
        fee_rate=Decimal("0.10"),
        currency="CLP",
    )
    pay_fee = CostComponent.known(
        CostComponentType.PAYMENT_FEES,
        fee_rate=Decimal("0.02"),
        fixed_fee_amount=Decimal("100"),
        currency="CLP",
    )

    landed = LandedCostCalculator.calculate(
        product_id="P1",
        supplier_id="S1",
        quantity=1,
        purchase_cost=purchase,
        shipping_cost=shipping,
    )
    assert landed.unit_landed_cost == Decimal("12000")

    unit_eco = UnitEconomicsCalculator.calculate_unit_economics(
        product_id="P1",
        supplier_id="S1",
        quantity_scenario=1,
        sale_price=sale_price,
        purchase_cost=purchase,
        shipping_cost=shipping,
        landed_cost=landed,
        packaging_cost=packaging,
        fulfillment_cost=fulfillment,
        marketplace_fees=mkt_fee,
        payment_fees=pay_fee,
    )

    assert unit_eco.status == ProfitStatus.PROFIT_COMPLETE
    # Revenue = 30000
    # Landed Cost = 12000
    # Gross Profit = 30000 - 12000 = 18000
    # Gross Margin % = (18000 / 30000) * 100 = 60.0%
    assert unit_eco.gross_profit == Decimal("18000")
    assert unit_eco.gross_margin_pct == Decimal("60.00")

    # Gross Profit = 30000 - 12000 = 18000
    # Gross Margin % = (18000 / 30000) * 100 = 60.0%
    # Markup % = (18000 / 12000) * 100 = 150.0%
    assert unit_eco.gross_profit == Decimal("18000")
    assert unit_eco.gross_margin_pct == Decimal("60.00")
    assert unit_eco.unit_markup_pct == Decimal("150.0")

    # Mkt fee = 30000 * 0.10 = 3000
    # Pay fee = 30000 * 0.02 + 100 = 600 + 100 = 700
    # Packaging = 500
    # Fulfillment = 500
    # Operating/Selling costs = 3000 + 700 + 500 + 500 = 4700
    # Net Profit = 18000 - 4700 = 13300
    # Net Margin % = (13300 / 30000) * 100 = 44.33%
    assert unit_eco.net_profit == Decimal("13300")
    assert round(unit_eco.net_margin_pct, 2) == Decimal("44.33")


def test_unit_economics_incomplete_when_fee_unknown():
    sale_price = SalePrice.observed(amount=Decimal("30000"), currency="CLP")
    purchase = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10000"), currency="CLP")
    shipping = CostComponent.known(CostComponentType.SHIPPING_COST, amount=Decimal("2000"), currency="CLP")
    mkt_fee = CostComponent.unknown(CostComponentType.MARKETPLACE_FEES, currency="CLP")

    landed = LandedCostCalculator.calculate(
        product_id="P1",
        supplier_id="S1",
        quantity=1,
        purchase_cost=purchase,
        shipping_cost=shipping,
    )

    unit_eco = UnitEconomicsCalculator.calculate_unit_economics(
        product_id="P1",
        supplier_id="S1",
        quantity_scenario=1,
        sale_price=sale_price,
        purchase_cost=purchase,
        shipping_cost=shipping,
        landed_cost=landed,
        marketplace_fees=mkt_fee,
    )

    # Gross profit can be calculated because landed is known
    assert unit_eco.gross_profit == Decimal("18000")
    # Net profit MUST NOT be calculated because marketplace fee is critical and unknown
    assert unit_eco.net_profit is None
    assert unit_eco.net_margin_pct is None
    assert unit_eco.status == ProfitStatus.PROFIT_PARTIAL
    assert any("MARKETPLACE_FEES" in u for u in unit_eco.unknowns)


# ============================================================================
# 4. TEST BREAK-EVEN DETERMINISM
# ============================================================================

def test_break_even_calculator():
    # Unit landed cost = 10000
    # Fixed fee = 500
    # Fee rates = 10% (mkt) + 2% (pay) = 12% = 0.12
    # P_be = (10000 + 500) / (1 - 0.12) = 10500 / 0.88 = 11931.81818... -> 11931.82
    landed_comp = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10000"), currency="CLP")
    landed = LandedCost(
        product_id="P1",
        supplier_id="S1",
        quantity=1,
        currency="CLP",
        purchase_cost=landed_comp,
        shipping_cost=CostComponent.not_applicable(CostComponentType.SHIPPING_COST, "CLP"),
        duties_cost=CostComponent.not_applicable(CostComponentType.IMPORT_DUTIES, "CLP"),
        taxes_cost=CostComponent.not_applicable(CostComponentType.TAXES, "CLP"),
        other_acquisition_cost=CostComponent.not_applicable(CostComponentType.OTHER_VARIABLE_COSTS, "CLP"),
        total_landed_cost=Decimal("10000"),
        unit_landed_cost=Decimal("10000"),
        status=LandedCostStatus.COMPLETE,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )

    mkt_fee = CostComponent.known(CostComponentType.MARKETPLACE_FEES, fee_rate=Decimal("0.10"), currency="CLP")
    pay_fee = CostComponent.known(CostComponentType.PAYMENT_FEES, fee_rate=Decimal("0.02"), fixed_fee_amount=Decimal("500"), currency="CLP")

    be_result = BreakEvenCalculator.calculate_break_even_price(
        landed_cost=landed,
        marketplace_fees=mkt_fee,
        payment_fees=pay_fee,
    )

    assert be_result.is_calculable is True
    assert be_result.break_even_sale_price == Decimal("11931.82")

    # Verify mathematics: At sale price 11931.82:
    # mkt_fee = 11931.82 * 0.10 = 1193.18
    # pay_fee = 11931.82 * 0.02 + 500 = 238.64 + 500 = 738.64
    # Total costs = 10000 + 1193.18 + 738.64 = 11931.82 -> Net profit = 0.00!
    fees = (Decimal("11931.82") * Decimal("0.12")) + Decimal("500")
    total_cost = Decimal("10000") + fees
    assert round(Decimal("11931.82") - total_cost, 2) == Decimal("0.00")


# ============================================================================
# 5. TEST SCENARIO ANALYSIS
# ============================================================================

def test_scenario_analysis():
    base_price = SalePrice.observed(amount=Decimal("20000"), currency="CLP")
    purchase = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10000"), currency="CLP")
    shipping = CostComponent.known(CostComponentType.SHIPPING_COST, amount=Decimal("2000"), currency="CLP")
    landed = LandedCostCalculator.calculate("P1", "S1", 1, purchase, shipping)

    result = EconomicScenarioAnalyzer.analyze_scenarios(
        product_id="P1",
        supplier_id="S1",
        quantity=1,
        base_sale_price=base_price,
        purchase_cost=purchase,
        shipping_cost=shipping,
        landed_cost=landed,
        price_drop_pct=Decimal("15.0"), # Conservative: -15% = 17000
        price_increase_pct=Decimal("10.0"), # Optimistic: +10% = 22000
    )

    assert result.base_scenario.sale_price.amount == Decimal("20000")
    assert result.conservative_scenario.sale_price.amount == Decimal("17000")
    assert result.optimistic_scenario.sale_price.amount == Decimal("22000")

    assert result.conservative_scenario.gross_profit == Decimal("5000") # 17000 - 12000
    assert result.base_scenario.gross_profit == Decimal("8000") # 20000 - 12000
    assert result.optimistic_scenario.gross_profit == Decimal("10000") # 22000 - 12000


# ============================================================================
# 6. TEST ECONOMIC INVESTIGATION DETECTOR
# ============================================================================

def test_investigation_detector():
    purchase = CostComponent.known(CostComponentType.PRODUCT_COST, amount=Decimal("10000"), currency="CLP")
    shipping = CostComponent.unknown(CostComponentType.SHIPPING_COST, currency="CLP")
    mkt_fee = CostComponent.unknown(CostComponentType.MARKETPLACE_FEES, currency="CLP")

    needs = EconomicInvestigationDetector.detect_investigation_needs(
        purchase_cost=purchase,
        shipping_cost=shipping,
        marketplace_fees=mkt_fee,
    )

    assert len(needs) == 2
    types = [n.component_type for n in needs]
    assert CostComponentType.SHIPPING_COST in types
    assert CostComponentType.MARKETPLACE_FEES in types
    assert all(n.priority in ("CRITICAL", "HIGH") for n in needs)


# ============================================================================
# 7. TEST PROFIT ENGINE UNIFIED EVALUATION WITH PRICE TIERS
# ============================================================================

def test_profit_engine_evaluation_with_price_tiers():
    engine = ProfitEngine()

    sale_price = SalePrice(
        amount=Decimal("45000"),
        currency="CLP",
        price_type=SalePriceType.OBSERVED_SALE_PRICE,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        source="MELI_API",
    )

    purchase_cost = CostComponent.known(
        component_type=CostComponentType.PRODUCT_COST,
        amount=Decimal("15.0"),
        currency="USD",
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        source="ALIBABA_TEST",
    )

    shipping_cost = CostComponent.known(
        component_type=CostComponentType.SHIPPING_COST,
        amount=Decimal("50.0"),
        currency="USD",
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        source="DHL_API",
        is_per_unit=False,  # Total shipping allocated over quantity
    )

    price_tiers = [
        PriceTier(min_quantity=1, unit_price=Decimal("15.0"), currency="USD"),
        PriceTier(min_quantity=10, unit_price=Decimal("12.0"), currency="USD"),
        PriceTier(min_quantity=100, unit_price=Decimal("8.0"), currency="USD"),
    ]

    fx = ExchangeRate(
        from_currency="USD",
        to_currency="CLP",
        rate=Decimal("950.0"),
        source="CENTRAL_BANK",
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )

    fee_comp = CostComponent.known(
        component_type=CostComponentType.MARKETPLACE_FEES,
        fee_rate=Decimal("0.13"),
        fixed_fee_amount=Decimal("0"),
        currency="CLP",
        source="MELI_OFFICIAL_FEES",
    )

    # Evaluate with quantity scenarios
    eval_res = engine.evaluate_opportunity_economics(
        product_id="PROD-TEST",
        supplier_id="SUPP-01",
        sale_price=sale_price,
        purchase_cost=purchase_cost,
        shipping_cost=shipping_cost,
        quantity_scenarios=[1, 10, 100],
        price_tiers=price_tiers,
        marketplace_fees=fee_comp,
        exchange_rate=fx,
    )

    # Evaluate for MOQ (10 units)
    res_moq = eval_res.quantity_scenarios[10]
    assert res_moq.status == ProfitStatus.PROFIT_COMPLETE
    # Purchase price at tier 10 = $12 USD * 950 = $11,400 CLP
    # Shipping allocation = $50 USD / 10 = $5 USD * 950 = $4,750 CLP
    # Total landed unit = 11400 + 4750 = $16,150 CLP
    assert res_moq.landed_cost.unit_landed_cost == Decimal("16150.0")

    # Evaluate for Volume (100 units)
    res_vol = eval_res.quantity_scenarios[100]
    # Purchase price at tier 100 = $8 USD * 950 = $7,600 CLP
    # Shipping allocation = $50 USD / 100 = $0.5 USD * 950 = $475 CLP
    # Total landed unit = 7600 + 475 = $8,075 CLP
    assert res_vol.landed_cost.unit_landed_cost == Decimal("8075.0")
    assert res_vol.gross_profit == Decimal("45000") - Decimal("8075.0")
    assert eval_res.break_even.is_calculable is True
