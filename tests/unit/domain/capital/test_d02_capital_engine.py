from decimal import Decimal
from datetime import datetime, timezone
import pytest

from src.domain.capital.models import (
    AllocationStatus,
    AllocationDecisionReason,
    CapitalBudget,
    CapitalExposure,
    CapitalDownsideAnalysis,
    AllocationPolicy,
    AllocationDecision,
    CapitalAllocation,
    AllocationHistoryEntry,
)
from src.domain.capital.engine import CapitalAllocationEngine
from src.domain.market_intelligence.models import Confidence, MarketEvidence, MarketListing, Marketplace, Money
from src.domain.opportunity.models import (
    Opportunity,
    OpportunityReadiness,
    EvidenceSufficiency,
)
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierCandidate,
    SupplierEvidence,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    SupplierRiskProfile,
    PrimarySupplierSelection,
    ProductMatchGrade,
    EvidenceProvenanceType,
    RiskLevel,
)
from src.domain.profit.models import (
    CostComponent,
    CostComponentType,
    CostComponentStatus,
    SalePrice,
    SalePriceType,
    ProfitStatus,
    UnitEconomics,
    EconomicEvaluationResult,
    BreakEvenResult,
    LandedCost,
    LandedCostStatus,
    ScenarioAnalysisResult,
    EconomicScenarioType,
    ProfitTrace,
)


def create_sample_opportunity(
    product_id: str = "PROD-001",
    score: Decimal = Decimal("85.0"),
    readiness: OpportunityReadiness = OpportunityReadiness.READY,
    confidence: Confidence = Confidence.HIGH,
    evidence_sufficiency: EvidenceSufficiency = EvidenceSufficiency.SUFFICIENT,
) -> Opportunity:
    listing = MarketListing(
        external_id=f"EXT-{product_id}",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Gaming Mechanical Keyboard RGB",
        price=Money(amount=Decimal("40000"), currency="CLP"),
        sold_quantity=100,
        available_quantity=50,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Electronics",
    )
    evidence = MarketEvidence(
        listing=listing,
        confidence=confidence,
    )
    return Opportunity(
        opportunity_id=f"OPP-{product_id}",
        product_id=product_id,
        title="Gaming Mechanical Keyboard RGB",
        listing=listing,
        evidence=evidence,
        score=score,
        readiness=readiness,
        confidence=confidence,
        evidence_sufficiency=evidence_sufficiency,
    )


def create_sample_economics(
    product_id: str = "PROD-001",
    supplier_id: str = "SUP-001",
    qty: int = 10,
    unit_landed_cost: Decimal = Decimal("20000"),
    unit_sale_price: Decimal = Decimal("40000"),
    status: ProfitStatus = ProfitStatus.PROFIT_COMPLETE,
    net_margin_pct: Decimal = Decimal("25.0"),
    gross_margin_pct: Decimal = Decimal("50.0"),
) -> EconomicEvaluationResult:
    total_landed = unit_landed_cost * Decimal(str(qty))
    sale_price = SalePrice(
        amount=unit_sale_price,
        currency="CLP",
        price_type=SalePriceType.OBSERVED_SALE_PRICE,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )
    p_comp = CostComponent(
        component_type=CostComponentType.PRODUCT_COST,
        status=CostComponentStatus.KNOWN,
        amount=unit_landed_cost,
        currency="CLP",
        confidence=Confidence.HIGH,
    )
    s_comp = CostComponent(
        component_type=CostComponentType.SHIPPING_COST,
        status=CostComponentStatus.KNOWN,
        amount=Decimal("0"),
        currency="CLP",
        confidence=Confidence.HIGH,
    )
    l_cost = LandedCost(
        product_id=product_id,
        supplier_id=supplier_id,
        quantity=qty,
        currency="CLP",
        purchase_cost=p_comp,
        shipping_cost=s_comp,
        duties_cost=CostComponent(CostComponentType.IMPORT_DUTIES, CostComponentStatus.NOT_APPLICABLE),
        taxes_cost=CostComponent(CostComponentType.TAXES, CostComponentStatus.NOT_APPLICABLE),
        other_acquisition_cost=CostComponent(CostComponentType.OTHER_VARIABLE_COSTS, CostComponentStatus.NOT_APPLICABLE),
        total_landed_cost=total_landed,
        unit_landed_cost=unit_landed_cost,
        status=LandedCostStatus.COMPLETE if status == ProfitStatus.PROFIT_COMPLETE else LandedCostStatus.PARTIAL,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )
    unit_econ = UnitEconomics(
        product_id=product_id,
        supplier_id=supplier_id,
        quantity_scenario=qty,
        sale_price=sale_price,
        purchase_cost=p_comp,
        shipping_cost=s_comp,
        import_duties=CostComponent(CostComponentType.IMPORT_DUTIES, CostComponentStatus.NOT_APPLICABLE),
        taxes=CostComponent(CostComponentType.TAXES, CostComponentStatus.NOT_APPLICABLE),
        marketplace_fees=CostComponent(CostComponentType.MARKETPLACE_FEES, CostComponentStatus.KNOWN, fee_rate=Decimal("0.13")),
        payment_fees=CostComponent(CostComponentType.PAYMENT_FEES, CostComponentStatus.KNOWN, fee_rate=Decimal("0.02")),
        packaging_cost=CostComponent(CostComponentType.PACKAGING, CostComponentStatus.NOT_APPLICABLE),
        fulfillment_cost=CostComponent(CostComponentType.FULFILLMENT, CostComponentStatus.NOT_APPLICABLE),
        other_costs=CostComponent(CostComponentType.OTHER_VARIABLE_COSTS, CostComponentStatus.NOT_APPLICABLE),
        landed_cost=l_cost,
        gross_profit=(unit_sale_price - unit_landed_cost) * Decimal(str(qty)),
        net_profit=((unit_sale_price * Decimal("0.85")) - unit_landed_cost) * Decimal(str(qty)),
        gross_margin_pct=gross_margin_pct,
        net_margin_pct=net_margin_pct,
        unit_markup_pct=Decimal("100.0"),
        status=status,
        currency="CLP",
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )
    break_even = BreakEvenResult(
        break_even_sale_price=Decimal("23529.41"),
        break_even_units=1,
        target_net_margin_price=unit_sale_price,
        is_computable=True,
        currency="CLP",
        formula_used="Standard",
    )
    trace = ProfitTrace(product_id=product_id, supplier_id=supplier_id)
    return EconomicEvaluationResult(
        product_id=product_id,
        supplier_id=supplier_id,
        primary_unit_economics=unit_econ,
        quantity_scenarios={qty: unit_econ},
        break_even=break_even,
        scenarios=None,
        investigation_needs=(),
        overall_confidence=Confidence.HIGH,
        overall_status=status,
        profit_trace=trace,
    )


def create_sample_supplier_recommendation(
    supplier_id: str = "SUP-001",
    status: SupplierRecommendationDecision = SupplierRecommendationDecision.RECOMMEND,
) -> SupplierRecommendation:
    sup = Supplier(
        supplier_id=supplier_id,
        name="Official Tech Chile",
        source="INTERNAL_CATALOG",
        source_type=EvidenceProvenanceType.FIXTURE,
    )
    primary_sel = PrimarySupplierSelection(
        supplier_id=supplier_id,
        supplier_name="Official Tech Chile",
        sku="KB-01",
        commercial_score=Decimal("90.0"),
        reliability_score=Decimal("88.0"),
        overall_risk_score=Decimal("15.0"),
        composite_suitability_score=Decimal("89.0"),
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
        selection_reason="Top verified supplier with stable stock",
        why_over_fallback="Cheaper unit price",
        commercial_position="Strong",
        logistics_position="Express 2 days",
    )
    return SupplierRecommendation(
        recommendation_id=f"REC-{supplier_id}",
        opportunity_id="OPP-PROD-001",
        target_product_title="Gaming Mechanical Keyboard RGB",
        target_sku="KB-01",
        decision=status,
        decision_reason="Top verified supplier with stable stock",
        primary_supplier=primary_sel if status == SupplierRecommendationDecision.RECOMMEND else None,
        fallback_supplier=None,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )


def test_capital_budget_invariants_and_allocatable():
    budget = CapitalBudget.create(
        budget_id="BUDGET-001",
        total_capital=Decimal("10000000"),
        reserved_capital=Decimal("2000000"),
    )
    assert budget.total_capital == Decimal("10000000")
    assert budget.reserved_capital == Decimal("2000000")
    assert budget.committed_capital == Decimal("0")
    assert budget.allocatable_capital == Decimal("8000000")
    assert budget.uncommitted_capital == Decimal("10000000")

    # Commit capital
    b2 = budget.with_commitment(Decimal("3000000"))
    assert b2.committed_capital == Decimal("3000000")
    assert b2.allocatable_capital == Decimal("5000000")
    assert b2.uncommitted_capital == Decimal("7000000")

    # Release capital
    b3 = b2.with_release(Decimal("1000000"))
    assert b3.committed_capital == Decimal("2000000")
    assert b3.allocatable_capital == Decimal("6000000")


def test_capital_budget_rejects_invalid_states():
    with pytest.raises(ValueError):
        CapitalBudget(
            budget_id="B1",
            total_capital=Decimal("1000"),
            reserved_capital=Decimal("800"),
            committed_capital=Decimal("300"),  # 800 + 300 = 1100 > 1000
        )
    with pytest.raises(ValueError):
        CapitalBudget(budget_id="", total_capital=Decimal("1000"), reserved_capital=Decimal("0"), committed_capital=Decimal("0"))


def test_capital_exposure_and_policy_limits():
    budget = CapitalBudget.create(
        budget_id="BUDGET-001",
        total_capital=Decimal("10000000"),
        reserved_capital=Decimal("2000000"),  # allocatable = 8.000.000
    )
    policy = AllocationPolicy(max_exposure_per_opportunity_pct=Decimal("0.25"))  # 25% of 8M = 2M
    exposure = CapitalAllocationEngine.calculate_exposure("PROD-001", budget, policy, existing_exposure=Decimal("500000"))

    assert exposure.maximum_allowed_exposure == Decimal("2000000")
    assert exposure.existing_exposure == Decimal("500000")
    assert exposure.remaining_opportunity_capacity == Decimal("1500000")
    assert exposure.effective_available_ceiling == Decimal("1500000")


def test_full_allocation_approval_when_capital_and_evidence_sufficient():
    budget = CapitalBudget.create(budget_id="B1", total_capital=Decimal("10000000"), reserved_capital=Decimal("2000000"))
    policy = AllocationPolicy(max_exposure_per_opportunity_pct=Decimal("0.25"))  # max 2.000.000
    opp = create_sample_opportunity("PROD-001")
    econ = create_sample_economics("PROD-001", qty=10, unit_landed_cost=Decimal("20000"))  # requested = 200.000
    sup = create_sample_supplier_recommendation("SUP-001")

    decision = CapitalAllocationEngine.evaluate_allocation(
        opportunity=opp,
        budget=budget,
        policy=policy,
        economic_evaluation=econ,
        supplier_recommendation=sup,
    )

    assert decision.status == AllocationStatus.APPROVED
    assert decision.reason == AllocationDecisionReason.APPROVED_FULL_BUDGET
    assert decision.requested_capital == Decimal("200000")
    assert decision.approved_capital == Decimal("200000")
    assert decision.unapproved_capital == Decimal("0")
    assert decision.allocation_ratio == Decimal("1.0000")
    assert decision.confidence == Confidence.HIGH
    assert decision.provenance_type == EvidenceProvenanceType.LIVE


def test_partial_allocation_when_capped_by_exposure():
    budget = CapitalBudget.create(budget_id="B1", total_capital=Decimal("10000000"), reserved_capital=Decimal("2000000"))
    # Allocatable = 8.000.000, 25% cap = 2.000.000
    policy = AllocationPolicy(max_exposure_per_opportunity_pct=Decimal("0.25"))
    opp = create_sample_opportunity("PROD-001")
    econ = create_sample_economics("PROD-001", qty=150, unit_landed_cost=Decimal("20000"))  # requested = 3.000.000 > 2.000.000
    sup = create_sample_supplier_recommendation("SUP-001")

    decision = CapitalAllocationEngine.evaluate_allocation(
        opportunity=opp,
        budget=budget,
        policy=policy,
        economic_evaluation=econ,
        supplier_recommendation=sup,
    )

    assert decision.status == AllocationStatus.PARTIALLY_APPROVED
    assert decision.reason == AllocationDecisionReason.CAPPED_BY_MAXIMUM_EXPOSURE
    assert decision.requested_capital == Decimal("3000000")
    assert decision.approved_capital == Decimal("2000000")
    assert decision.unapproved_capital == Decimal("1000000")
    assert decision.allocation_ratio == Decimal("0.6667")


def test_rejection_when_insufficient_margin():
    budget = CapitalBudget.create(budget_id="B1", total_capital=Decimal("10000000"))
    policy = AllocationPolicy(min_net_margin_pct=Decimal("15.0"))
    opp = create_sample_opportunity("PROD-001")
    econ = create_sample_economics("PROD-001", net_margin_pct=Decimal("8.0"))  # 8% < 15%
    sup = create_sample_supplier_recommendation("SUP-001")

    decision = CapitalAllocationEngine.evaluate_allocation(
        opportunity=opp,
        budget=budget,
        policy=policy,
        economic_evaluation=econ,
        supplier_recommendation=sup,
    )

    assert decision.status == AllocationStatus.REJECTED
    assert decision.reason == AllocationDecisionReason.NEGATIVE_OR_INSUFFICIENT_MARGIN
    assert decision.approved_capital == Decimal("0")


def test_needs_investigation_when_critical_costs_unknown():
    budget = CapitalBudget.create(budget_id="B1", total_capital=Decimal("10000000"))
    policy = AllocationPolicy(allow_partial_allocation=False, require_known_economics=True)
    opp = create_sample_opportunity("PROD-001")
    econ = create_sample_economics("PROD-001", status=ProfitStatus.PROFIT_INCOMPLETE)
    sup = create_sample_supplier_recommendation("SUP-001")

    decision = CapitalAllocationEngine.evaluate_allocation(
        opportunity=opp,
        budget=budget,
        policy=policy,
        economic_evaluation=econ,
        supplier_recommendation=sup,
    )

    assert decision.status == AllocationStatus.NEEDS_INVESTIGATION
    assert decision.reason == AllocationDecisionReason.INSUFFICIENT_ECONOMIC_EVIDENCE
    assert decision.approved_capital == Decimal("0")


def test_create_and_release_allocation_lifecycle():
    budget = CapitalBudget.create(budget_id="B1", total_capital=Decimal("10000000"), reserved_capital=Decimal("2000000"))
    policy = AllocationPolicy(max_exposure_per_opportunity_pct=Decimal("0.25"))
    opp = create_sample_opportunity("PROD-001")
    econ = create_sample_economics("PROD-001", qty=10, unit_landed_cost=Decimal("50000"))  # 500.000
    sup = create_sample_supplier_recommendation("SUP-001")

    decision = CapitalAllocationEngine.evaluate_allocation(
        opportunity=opp,
        budget=budget,
        policy=policy,
        economic_evaluation=econ,
        supplier_recommendation=sup,
    )

    # Crear asignación
    alloc, updated_budget = CapitalAllocationEngine.create_allocation(budget, decision)
    assert alloc.allocated_amount == Decimal("500000")
    assert alloc.status == AllocationStatus.APPROVED
    assert updated_budget.committed_capital == Decimal("500000")
    assert updated_budget.allocatable_capital == Decimal("7500000")

    # Liberar asignación
    released_alloc, final_budget = CapitalAllocationEngine.release_allocation(
        alloc,
        updated_budget,
        reason="Test release",
    )
    assert released_alloc.status == AllocationStatus.RELEASED
    assert released_alloc.allocated_amount == Decimal("0")
    assert len(released_alloc.history) == 1
    assert released_alloc.history[0].released_amount == Decimal("500000")
    assert final_budget.committed_capital == Decimal("0")
    assert final_budget.allocatable_capital == Decimal("8000000")


def test_reassessment_on_supplier_invalidation():
    budget = CapitalBudget.create(budget_id="B1", total_capital=Decimal("10000000"), reserved_capital=Decimal("2000000"))
    policy = AllocationPolicy(max_exposure_per_opportunity_pct=Decimal("0.25"))
    opp = create_sample_opportunity("PROD-001")
    econ = create_sample_economics("PROD-001", qty=10, unit_landed_cost=Decimal("50000"))
    sup = create_sample_supplier_recommendation("SUP-001")

    decision = CapitalAllocationEngine.evaluate_allocation(
        opportunity=opp,
        budget=budget,
        policy=policy,
        economic_evaluation=econ,
        supplier_recommendation=sup,
    )
    alloc, budget_with_commit = CapitalAllocationEngine.create_allocation(budget, decision)

    # Proveedor se invalida
    invalid_sup = create_sample_supplier_recommendation("SUP-001", status=SupplierRecommendationDecision.REJECT)

    reassessed_alloc, final_budget, new_dec = CapitalAllocationEngine.reassess_allocation_on_deterioration(
        allocation=alloc,
        budget=budget_with_commit,
        opportunity=opp,
        new_economic_evaluation=econ,
        new_supplier_recommendation=invalid_sup,
        policy=policy,
    )

    assert reassessed_alloc.status == AllocationStatus.RELEASED
    assert reassessed_alloc.allocated_amount == Decimal("0")
    assert final_budget.committed_capital == Decimal("0")
    assert final_budget.allocatable_capital == Decimal("8000000")
    assert new_dec.status == AllocationStatus.REJECTED
