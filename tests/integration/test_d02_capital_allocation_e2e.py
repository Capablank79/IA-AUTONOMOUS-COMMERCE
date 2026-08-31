from decimal import Decimal
from datetime import datetime, timezone
import pytest

from src.domain.mission.models import Mission, MissionStatus, MissionType
from src.domain.capital.models import (
    AllocationStatus,
    AllocationDecisionReason,
    CapitalBudget,
    CapitalAllocation,
    AllocationPolicy,
)
from src.domain.market_intelligence.models import (
    Confidence,
    MarketEvidence,
    MarketListing,
    Marketplace,
    Money,
)
from src.domain.opportunity.models import (
    Opportunity,
    OpportunityReadiness,
    EvidenceSufficiency,
)
from src.domain.profit.models import (
    ProfitStatus,
    CostComponent,
    CostComponentType,
    CostComponentStatus,
    SalePrice,
    SalePriceType,
    UnitEconomics,
    EconomicEvaluationResult,
    BreakEvenResult,
    LandedCost,
    LandedCostStatus,
    ProfitTrace,
    ScenarioAnalysisResult,
    EconomicScenarioType,
)
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierCandidate,
    SupplierEvidence,
    SupplierRecommendation,
    SupplierRecommendationDecision,
    PrimarySupplierSelection,
    ProductMatchGrade,
    EvidenceProvenanceType,
)
from src.application.capital.autonomous_capital_service import AutonomousCapitalService
from src.infrastructure.mission.repository import InMemoryMissionRepository


def setup_test_context(
        product_id: str = "PROD-100",
        qty: int = 10,
        unit_landed_cost: Decimal = Decimal("20000"),
        unit_sale_price: Decimal = Decimal("40000"),
        profit_status: ProfitStatus = ProfitStatus.PROFIT_COMPLETE,
        sup_status: SupplierRecommendationDecision = SupplierRecommendationDecision.RECOMMEND,
        total_budget: Decimal = Decimal("10000000"),
        reserved_budget: Decimal = Decimal("2000000"),
        net_margin_pct: Decimal = Decimal("25.0"),
        confidence: Confidence = Confidence.HIGH,
    ):
    listing = MarketListing(
        external_id=f"EXT-{product_id}",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Ergonomic Office Chair Mesh",
        price=Money(amount=unit_sale_price, currency="CLP"),
        sold_quantity=100,
        available_quantity=50,
        seller_id="SELLER-CHAIR",
        condition="new",
        shipping_info={"free_shipping": True},
        category="Office",
    )
    evidence = MarketEvidence(
        listing=listing,
        confidence=confidence,
    )
    opp = Opportunity(
        opportunity_id=f"OPP-{product_id}",
        product_id=product_id,
        title="Ergonomic Office Chair Mesh",
        listing=listing,
        evidence=evidence,
        score=Decimal("88.0"),
        readiness=OpportunityReadiness.READY,
        confidence=confidence,
        evidence_sufficiency=EvidenceSufficiency.SUFFICIENT,
    )

    sup = Supplier(
        supplier_id="SUP-OFFICE-01",
        name="Office Express SA",
        source="INTERNAL_CATALOG",
        source_type=EvidenceProvenanceType.FIXTURE,
    )
    primary_sel = PrimarySupplierSelection(
        supplier_id="SUP-OFFICE-01",
        supplier_name="Office Express SA",
        sku="CHAIR-01",
        commercial_score=Decimal("90.0"),
        reliability_score=Decimal("88.0"),
        overall_risk_score=Decimal("15.0"),
        composite_suitability_score=Decimal("89.0"),
        confidence=confidence,
        provenance_type=EvidenceProvenanceType.LIVE,
        selection_reason="Prime office supplier",
        why_over_fallback="Cheaper unit price",
        commercial_position="Strong",
        logistics_position="Express 2 days",
    )
    rec = SupplierRecommendation(
        recommendation_id="REC-OFFICE-01",
        opportunity_id=f"OPP-{product_id}",
        target_product_title="Ergonomic Office Chair Mesh",
        target_sku="CHAIR-01",
        decision=sup_status,
        decision_reason="Prime office supplier",
        primary_supplier=primary_sel if sup_status == SupplierRecommendationDecision.RECOMMEND else None,
        fallback_supplier=None,
        confidence=confidence,
        provenance=EvidenceProvenanceType.LIVE,
    )

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
        supplier_id="SUP-OFFICE-01",
        quantity=qty,
        currency="CLP",
        purchase_cost=p_comp,
        shipping_cost=s_comp,
        duties_cost=CostComponent(CostComponentType.IMPORT_DUTIES, CostComponentStatus.NOT_APPLICABLE),
        taxes_cost=CostComponent(CostComponentType.TAXES, CostComponentStatus.NOT_APPLICABLE),
        other_acquisition_cost=CostComponent(CostComponentType.OTHER_VARIABLE_COSTS, CostComponentStatus.NOT_APPLICABLE),
        total_landed_cost=total_landed,
        unit_landed_cost=unit_landed_cost,
        status=LandedCostStatus.COMPLETE if profit_status == ProfitStatus.PROFIT_COMPLETE else LandedCostStatus.PARTIAL,
        confidence=Confidence.HIGH,
        provenance_type=EvidenceProvenanceType.LIVE,
    )
    unit_econ = UnitEconomics(
        product_id=product_id,
        supplier_id="SUP-OFFICE-01",
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
        gross_margin_pct=Decimal("50.0"),
        net_margin_pct=net_margin_pct,
        unit_markup_pct=Decimal("100.0"),
        status=profit_status,
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
    trace = ProfitTrace(product_id=product_id, supplier_id="SUP-OFFICE-01")
    econ_eval = EconomicEvaluationResult(
        product_id=product_id,
        supplier_id="SUP-OFFICE-01",
        primary_unit_economics=unit_econ,
        quantity_scenarios={qty: unit_econ},
        break_even=break_even,
        scenarios=None,
        investigation_needs=(),
        overall_confidence=Confidence.HIGH,
        overall_status=profit_status,
        profit_trace=trace,
    )

    budget = CapitalBudget.create(
        budget_id="BUDGET-MAIN",
        total_capital=total_budget,
        reserved_capital=reserved_budget,
    )

    return opp, rec, econ_eval, budget


def test_marcha_blanca_a_sufficient_capital():
    """
    MARCHA BLANCA A: CAPITAL SUFICIENTE
    Economics completos + riesgo aceptable + evidencia suficiente.
    Esperado: APPROVED.
    """
    opp, rec, econ, budget = setup_test_context(qty=10, unit_landed_cost=Decimal("20000")) # requested = 200.000
    repo = InMemoryMissionRepository()
    service = AutonomousCapitalService(mission_repository=repo)
    mission = Mission(mission_id="M-D02-MB-A", type=MissionType.MARKET_DISCOVERY, parameters={"goal": "Allocate capital prudently for verified opportunity"})

    result, executor = service.run_mission(
        mission=mission,
        opportunity=opp,
        budget=budget,
        economic_evaluation=econ,
        supplier_recommendation=rec,
    )

    assert result.status == MissionStatus.COMPLETED
    assert result.output["allocation_status"] == AllocationStatus.APPROVED.value
    assert result.output["reason"] == AllocationDecisionReason.APPROVED_FULL_BUDGET.value
    assert result.output["approved_capital"] == "200000"
    assert result.output["allocation_ratio"] == "1.0000"
    assert executor.active_allocation is not None
    assert executor.active_allocation.allocated_amount == Decimal("200000")
    assert executor.current_budget.committed_capital == Decimal("200000")
    assert executor.current_budget.allocatable_capital == Decimal("7800000")


def test_marcha_blanca_b_exposure_limit_capped():
    """
    MARCHA BLANCA B: LÍMITE DE EXPOSICIÓN
    Requested > maximum allowed exposure per opportunity.
    Esperado: PARTIALLY_APPROVED.
    """
    # Allocatable = 8.000.000, 25% max exposure = 2.000.000
    # Solicitado = 3.000.000 (150 units * 20.000)
    opp, rec, econ, budget = setup_test_context(qty=150, unit_landed_cost=Decimal("20000"))
    repo = InMemoryMissionRepository()
    service = AutonomousCapitalService(mission_repository=repo)
    mission = Mission(mission_id="M-D02-MB-B", type=MissionType.MARKET_DISCOVERY, parameters={"goal": "Allocate capital with exposure ceiling check"})

    result, executor = service.run_mission(
        mission=mission,
        opportunity=opp,
        budget=budget,
        economic_evaluation=econ,
        supplier_recommendation=rec,
    )

    assert result.status == MissionStatus.COMPLETED
    assert result.output["allocation_status"] == AllocationStatus.PARTIALLY_APPROVED.value
    assert result.output["reason"] == AllocationDecisionReason.CAPPED_BY_MAXIMUM_EXPOSURE.value
    assert result.output["requested_capital"] == "3000000"
    assert result.output["approved_capital"] in ("2000000", "2000000.00")
    assert result.output["unapproved_capital"] in ("1000000", "1000000.00")
    assert executor.active_allocation is not None
    assert executor.active_allocation.allocated_amount == Decimal("2000000")


def test_marcha_blanca_c_insufficient_capital():
    """
    MARCHA BLANCA C: CAPITAL INSUFICIENTE
    Requested > available capital in budget.
    Esperado: PARTIALLY_APPROVED o REJECTED según policy.
    """
    # Budget total 1.000.000, reserva 500.000 -> Allocatable = 500.000
    # Solicitado = 1.000.000 (50 units * 20.000)
    # Exposición máxima de oportunidad = 800.000 (80% del budget total de 1M), pero allocatable solo es 500.000
    opp, rec, econ, budget = setup_test_context(
        qty=50,
        unit_landed_cost=Decimal("20000"),
        total_budget=Decimal("1000000"),
        reserved_budget=Decimal("500000"),
    )
    policy = AllocationPolicy(
        max_exposure_absolute_amount=Decimal("800000"),
        max_exposure_per_opportunity_pct=Decimal("1.0"),
    )
    repo = InMemoryMissionRepository()
    service = AutonomousCapitalService(mission_repository=repo)
    mission = Mission(mission_id="M-D02-MB-C", type=MissionType.MARKET_DISCOVERY, parameters={"goal": "Allocate capital under strict budget constraint"})

    result, executor = service.run_mission(
        mission=mission,
        opportunity=opp,
        budget=budget,
        policy=policy,
        economic_evaluation=econ,
        supplier_recommendation=rec,
    )

    assert result.status == MissionStatus.COMPLETED
    assert result.output["allocation_status"] == AllocationStatus.PARTIALLY_APPROVED.value
    assert result.output["reason"] == AllocationDecisionReason.CAPPED_BY_AVAILABLE_CAPITAL.value
    assert result.output["requested_capital"] == "1000000"
    assert Decimal(result.output["approved_capital"]) == Decimal("500000")
    assert Decimal(result.output["unapproved_capital"]) == Decimal("500000")
    assert executor.current_budget.allocatable_capital == Decimal("0")


def test_marcha_blanca_d_insufficient_evidence():
    """
    MARCHA BLANCA D: EVIDENCIA INSUFICIENTE
    Datos económicos críticos UNKNOWN.
    Esperado: NEEDS_INVESTIGATION o LIMITED_ALLOCATION.
    """
    opp, rec, econ, budget = setup_test_context(profit_status=ProfitStatus.PROFIT_INCOMPLETE)
    policy = AllocationPolicy(allow_partial_allocation=False, require_known_economics=True)
    repo = InMemoryMissionRepository()
    service = AutonomousCapitalService(mission_repository=repo)
    mission = Mission(mission_id="M-D02-MB-D", type=MissionType.MARKET_DISCOVERY, parameters={"goal": "Allocate capital with incomplete evidence"})

    result, executor = service.run_mission(
        mission=mission,
        opportunity=opp,
        budget=budget,
        policy=policy,
        economic_evaluation=econ,
        supplier_recommendation=rec,
    )

    assert result.status == MissionStatus.COMPLETED
    assert result.output["allocation_status"] == AllocationStatus.NEEDS_INVESTIGATION.value
    assert result.output["reason"] == AllocationDecisionReason.INSUFFICIENT_ECONOMIC_EVIDENCE.value
    assert result.output["approved_capital"] == "0"
    assert executor.active_allocation is None


def test_marcha_blanca_e_post_allocation_deterioration_and_reassessment():
    """
    MARCHA BLANCA E: DETERIORO POST-ASIGNACIÓN
    Demostrar flujo completo:
    ALLOCATED -> INVALIDATED -> REASSESS -> REDUCE / RELEASE / REALLOCATE.
    """
    # 1. Asignación inicial exitosa
    opp, rec, econ, budget = setup_test_context(qty=10, unit_landed_cost=Decimal("20000"))
    repo = InMemoryMissionRepository()
    service = AutonomousCapitalService(mission_repository=repo)
    mission = Mission(mission_id="M-D02-MB-E", type=MissionType.MARKET_DISCOVERY, parameters={"goal": "Demonstrate post-allocation deterioration and release"})

    result, executor = service.run_mission(
        mission=mission,
        opportunity=opp,
        budget=budget,
        economic_evaluation=econ,
        supplier_recommendation=rec,
    )

    assert result.output["allocation_status"] == AllocationStatus.APPROVED.value
    assert executor.active_allocation is not None
    assert executor.active_allocation.allocated_amount == Decimal("200000")
    assert executor.current_budget.committed_capital == Decimal("200000")

    # 2. Simulación de deterioro: el proveedor incumple o es rechazado (INVALIDATED)
    invalid_sup = SupplierRecommendation(
        recommendation_id="REC-OFFICE-01",
        opportunity_id=opp.opportunity_id,
        target_product_title=opp.title,
        target_sku="CHAIR-01",
        decision=SupplierRecommendationDecision.REJECT,
        decision_reason="Supplier suspended due to quality issues",
        primary_supplier=None,
        fallback_supplier=None,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    # 3. Ejecutar Reassessment en el Executor
    reassess_result = executor.execute(
        decision=type("LoopDec", (), {
            "parameters": {
                "action_type": "REASSESS_ALLOCATION",
                "new_supplier_recommendation": invalid_sup,
                "reason": "Supplier invalidated post-allocation",
            },
            "action": type("Act", (), {"value": "REASSESS"})(),
        })(),
        state=type("State", (), {})(),
    )

    assert reassess_result["status"] == "REASSESSMENT_COMPLETED"
    assert reassess_result["new_allocation_status"] == AllocationStatus.RELEASED.value
    assert reassess_result["allocated_amount"] == "0"
    assert executor.active_allocation.status == AllocationStatus.RELEASED
    assert executor.current_budget.committed_capital == Decimal("0")
    assert executor.current_budget.allocatable_capital == Decimal("8000000")
