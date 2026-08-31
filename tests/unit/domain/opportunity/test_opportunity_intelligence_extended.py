import pytest
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Any

from src.domain.market_intelligence.models import (
    MarketEvidence,
    MarketListing,
    Marketplace,
    Money,
    VisitSignal,
    DemandSignal,
    Confidence,
    SignalType,
    PriceSignal,
    TrendSignal,
    ReviewSignal,
    Review
)
from src.domain.opportunity.engine import OpportunityEngine
from src.domain.opportunity.models import (
    Opportunity,
    OpportunityDecision,
    OpportunityReadiness,
    EvidenceSufficiency,
    RejectionReason,
    OpportunityRejection,
    OpportunityComparisonResult,
    OpportunityProgress,
    CompletionPolicy
)
from src.domain.mission.models import (
    LoopDecision,
    LoopAction,
    LoopState,
    MissionStatus
)
from src.domain.mission.ports import DecisionProvider
from src.application.mission.autonomous_market_discovery_service import AutonomousMarketDiscoveryService
from src.application.market_intelligence.market_discovery_action_executor import MarketDiscoveryActionExecutor


@pytest.fixture
def engine():
    return OpportunityEngine()


@pytest.fixture
def make_listing():
    def _create(external_id="MLC-100", title="Auriculares Inalámbricos Bluetooth", price_amount="25000", sold=100):
        return MarketListing(
            external_id=external_id,
            marketplace=Marketplace.MERCADO_LIBRE,
            title=title,
            price=Money(amount=Decimal(price_amount), currency="CLP"),
            sold_quantity=sold,
            available_quantity=50,
            seller_id="SELLER-TEST",
            condition="new",
            shipping_info={"free_shipping": True},
            category="MLC1055"
        )
    return _create


# --------------------------------------------------------------------------
# 1. Scoring determinista
# --------------------------------------------------------------------------
def test_deterministic_scoring(engine, make_listing):
    listing = make_listing(sold=150)
    visit = VisitSignal(
        item_id=listing.external_id,
        window="30d",
        total_visits=1200,
        observed_days=30,
        coverage_ratio=1.0,
        source="ML_ANALYTICS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    price = PriceSignal(ratio=Decimal("0.85"), position="UNDER_MARKET")
    trend = TrendSignal(keyword="auriculares", rank=1, matched=True, trend_score=Decimal("1.0"))
    demand = DemandSignal(score=Decimal("1.0"), label="HIGH_DEMAND", confidence=Confidence.HIGH)

    evidence = MarketEvidence(
        listing=listing,
        traffic_signals=[visit],
        price_signals=[price],
        trend_signals=[trend],
        demand_signals=[demand],
        confidence=Confidence.HIGH
    )

    score_1 = engine.calculate_deterministic_market_score(evidence)
    score_2 = engine.calculate_deterministic_market_score(evidence)

    assert score_1 == score_2
    assert isinstance(score_1, Decimal)
    # 40 (demand) + 30 (visits) + 20 (trend) + 10 (price) = 100.0
    assert score_1 == Decimal("100.0")


# --------------------------------------------------------------------------
# 2. Ranking
# --------------------------------------------------------------------------
def test_ranking_composite_ordering(engine, make_listing):
    listing_top = make_listing("MLC-TOP", "Top Winner", "10000", sold=300)
    listing_mid = make_listing("MLC-MID", "Mid Performer", "15000", sold=60)
    listing_low = make_listing("MLC-LOW", "Low Performer", "20000", sold=5)

    ev_top = MarketEvidence(
        listing=listing_top,
        traffic_signals=[VisitSignal("MLC-TOP", "30d", 2000, 30, 1.0, "ML", datetime.now(timezone.utc), Confidence.HIGH)],
        confidence=Confidence.HIGH
    )
    ev_mid = MarketEvidence(
        listing=listing_mid,
        traffic_signals=[VisitSignal("MLC-MID", "30d", 200, 30, 1.0, "ML", datetime.now(timezone.utc), Confidence.MEDIUM)],
        confidence=Confidence.MEDIUM
    )
    ev_low = MarketEvidence(
        listing=listing_low,
        confidence=Confidence.LOW
    )

    opp_top = engine.create_opportunity(ev_top)
    opp_mid = engine.create_opportunity(ev_mid)
    opp_low = engine.create_opportunity(ev_low)

    ranked = engine.rank_opportunities([opp_mid, opp_low, opp_top])

    assert len(ranked) == 3
    assert ranked[0].product_id == "MLC-TOP"
    assert ranked[1].product_id == "MLC-MID"
    assert ranked[2].product_id == "MLC-LOW"
    assert ranked[0].score > ranked[1].score > ranked[2].score


# --------------------------------------------------------------------------
# 3. Evidence Sufficiency
# --------------------------------------------------------------------------
def test_evidence_sufficiency_classification(engine, make_listing):
    listing = make_listing(sold=None)
    
    # 3.1 Insufficient: no signals
    ev_insufficient = MarketEvidence(listing=listing, confidence=Confidence.LOW)
    assert engine.evaluate_evidence_sufficiency(ev_insufficient) == EvidenceSufficiency.INSUFFICIENT

    # 3.2 Partial: single signal (sold_quantity only)
    listing_with_sales = make_listing(sold=50)
    ev_partial = MarketEvidence(listing=listing_with_sales, confidence=Confidence.LOW)
    assert engine.evaluate_evidence_sufficiency(ev_partial) == EvidenceSufficiency.PARTIAL

    # 3.3 Sufficient: multiple signals (traffic + reviews + price)
    rev_obj = Review(
        external_id="REV-1",
        rating=5,
        text="Excelente",
        date=datetime.now(timezone.utc),
        reviewable_object="MLC-100"
    )
    rev_sig = ReviewSignal(
        item_id="MLC-100",
        total_reviews=25,
        average_rating=4.8,
        reviews=[rev_obj],
        paging={"total": 25, "limit": 10, "offset": 0},
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    ev_sufficient = MarketEvidence(
        listing=listing_with_sales,
        traffic_signals=[VisitSignal("MLC-100", "30d", 500, 30, 1.0, "ML", datetime.now(timezone.utc), Confidence.HIGH)],
        review_signals=[rev_sig],
        price_signals=[PriceSignal(ratio=Decimal("0.9"), position="UNDER_MARKET")],
        confidence=Confidence.HIGH
    )
    assert engine.evaluate_evidence_sufficiency(ev_sufficient) == EvidenceSufficiency.SUFFICIENT


# --------------------------------------------------------------------------
# 4. Readiness
# --------------------------------------------------------------------------
def test_readiness_transitions(engine, make_listing):
    listing = make_listing(sold=100)
    
    # Insufficient evidence -> INSUFFICIENT_EVIDENCE
    ev_none = MarketEvidence(listing=make_listing(sold=None), confidence=Confidence.UNKNOWN)
    readiness_none, _ = engine.determine_readiness(ev_none)
    assert readiness_none == OpportunityReadiness.INSUFFICIENT_EVIDENCE

    # Partial evidence -> NEEDS_INVESTIGATION
    ev_partial = MarketEvidence(listing=listing, confidence=Confidence.LOW)
    readiness_part, _ = engine.determine_readiness(ev_partial)
    assert readiness_part == OpportunityReadiness.NEEDS_INVESTIGATION

    # Sufficient evidence + high score -> READY
    rev_obj = Review(
        external_id="REV-2",
        rating=5,
        text="Muy bueno",
        date=datetime.now(timezone.utc),
        reviewable_object="MLC-100"
    )
    rev_sig = ReviewSignal(
        item_id="MLC-100",
        total_reviews=30,
        average_rating=4.9,
        reviews=[rev_obj],
        paging={"total": 30, "limit": 10, "offset": 0},
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    ev_full = MarketEvidence(
        listing=listing,
        traffic_signals=[VisitSignal("MLC-100", "30d", 800, 30, 1.0, "ML", datetime.now(timezone.utc), Confidence.HIGH)],
        review_signals=[rev_sig],
        confidence=Confidence.HIGH
    )
    readiness_ready, _ = engine.determine_readiness(ev_full)
    assert readiness_ready == OpportunityReadiness.READY


# --------------------------------------------------------------------------
# 5. Score alto + Evidencia insuficiente
# --------------------------------------------------------------------------
def test_high_score_with_insufficient_evidence(engine, make_listing):
    # Un listing con sold_quantity muy alto pero sin ninguna otra evidencia ni visitas
    listing = make_listing(sold=500)
    ev = MarketEvidence(listing=listing, confidence=Confidence.LOW)
    
    # Score es relativamente alto debido a sold_quantity
    score = engine.calculate_deterministic_market_score(ev)
    assert score >= Decimal("40.0")

    # Sin embargo, la suficiencia es parcial y el readiness NO es READY sino NEEDS_INVESTIGATION
    sufficiency = engine.evaluate_evidence_sufficiency(ev)
    readiness, reasons = engine.determine_readiness(ev, score=score, sufficiency=sufficiency)
    
    assert sufficiency in [EvidenceSufficiency.PARTIAL, EvidenceSufficiency.INSUFFICIENT]
    assert readiness == OpportunityReadiness.NEEDS_INVESTIGATION
    assert readiness != OpportunityReadiness.READY


# --------------------------------------------------------------------------
# 6. Score bajo + Evidencia suficiente
# --------------------------------------------------------------------------
def test_low_score_with_sufficient_evidence_rejected(engine, make_listing):
    # Evidencia completa de que el producto no tiene demanda (visitas bajísimas, precio pésimo, ventas 0)
    listing = make_listing(sold=0)
    rev_obj = Review(
        external_id="REV-3",
        rating=1,
        text="Malo",
        date=datetime.now(timezone.utc),
        reviewable_object="MLC-100"
    )
    rev_sig = ReviewSignal(
        item_id="MLC-100",
        total_reviews=1,
        average_rating=1.5,
        reviews=[rev_obj],
        paging={"total": 1, "limit": 10, "offset": 0},
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    ev = MarketEvidence(
        listing=listing,
        traffic_signals=[VisitSignal("MLC-100", "30d", 5, 30, 1.0, "ML", datetime.now(timezone.utc), Confidence.HIGH)],
        review_signals=[rev_sig],
        price_signals=[PriceSignal(ratio=Decimal("1.5"), position="OVER_MARKET")],
        demand_signals=[DemandSignal(score=Decimal("0.0"), label="LOW_DEMAND", confidence=Confidence.HIGH)],
        confidence=Confidence.HIGH
    )

    score = engine.calculate_deterministic_market_score(ev)
    readiness, reasons = engine.determine_readiness(ev, score=score)

    assert score < Decimal("20.0")
    assert readiness == OpportunityReadiness.REJECTED
    assert any("low composite market traction" in r.lower() or "traffic" in r.lower() for r in reasons)


# --------------------------------------------------------------------------
# 7. Comparison multidimensional
# --------------------------------------------------------------------------
def test_opportunity_comparison(engine, make_listing):
    listing_a = make_listing("MLC-A", "Producto Líder A", "15000", sold=200)
    listing_b = make_listing("MLC-B", "Producto Débil B", "30000", sold=10)

    ev_a = MarketEvidence(
        listing=listing_a,
        traffic_signals=[VisitSignal("MLC-A", "30d", 1500, 30, 1.0, "ML", datetime.now(timezone.utc), Confidence.HIGH)],
        confidence=Confidence.HIGH
    )
    ev_b = MarketEvidence(
        listing=listing_b,
        confidence=Confidence.LOW
    )

    opp_a = engine.create_opportunity(ev_a)
    opp_b = engine.create_opportunity(ev_b)

    comparison = engine.compare_opportunities(opp_a, opp_b)

    assert isinstance(comparison, OpportunityComparisonResult)
    assert comparison.winner_id == "MLC-A"
    assert len(comparison.dimensions) >= 4
    
    dim_names = [d.dimension_name for d in comparison.dimensions]
    assert "Score & Market Traction" in dim_names
    assert "Confidence Level" in dim_names
    assert "Evidence Coverage" in dim_names
    assert "Uncertainty & Risks" in dim_names

    assert "MLC-A" in comparison.summary_rationale


# --------------------------------------------------------------------------
# 8. Rejection explícito
# --------------------------------------------------------------------------
def test_opportunity_rejection_with_domain_reason(engine, make_listing):
    listing = make_listing("MLC-REJ", "Producto No Viable", "99000", sold=0)
    ev = MarketEvidence(listing=listing, confidence=Confidence.HIGH)

    decision = engine.reject_opportunity(
        evidence=ev,
        reason=RejectionReason.WEAK_DEMAND,
        details="Producto sin tracción ni ventas históricas detectadas",
        confidence=Confidence.HIGH
    )

    assert decision.readiness == OpportunityReadiness.REJECTED
    assert decision.rejection is not None
    assert decision.rejection.reason == RejectionReason.WEAK_DEMAND
    assert decision.rejection.product_id == "MLC-REJ"
    assert "Producto sin tracción" in decision.rejection.details


# --------------------------------------------------------------------------
# 9. Explanation sustentada
# --------------------------------------------------------------------------
def test_opportunity_explanation_layers(engine, make_listing):
    listing = make_listing("MLC-EXP", "Smartwatch Deportivo Pro", "45000", sold=120)
    ev = MarketEvidence(
        listing=listing,
        traffic_signals=[VisitSignal("MLC-EXP", "30d", 950, 30, 1.0, "ML", datetime.now(timezone.utc), Confidence.HIGH)],
        price_signals=[PriceSignal(ratio=Decimal("0.9"), position="UNDER_MARKET")],
        confidence=Confidence.HIGH
    )

    expl = engine.generate_explanation(ev, score=Decimal("65.0"))

    # Verifica capas estructuradas
    assert expl.product_id == "MLC-EXP"
    assert expl.why_winner != ""
    assert len(expl.observed_evidence) > 0
    assert any("45000" in o for o in expl.observed_evidence)
    assert len(expl.derived_signals) >= 0
    assert len(expl.inferred_insights) > 0
    assert len(expl.risks) > 0
    assert len(expl.unknowns) > 0
    assert expl.recommended_action != ""


# --------------------------------------------------------------------------
# 10. Provenance
# --------------------------------------------------------------------------
def test_opportunity_provenance(engine, make_listing):
    listing = make_listing("MLC-PROV", "Item Provenance", "10000", sold=50)
    ev = MarketEvidence(listing=listing, confidence=Confidence.MEDIUM)

    opp = engine.create_opportunity(ev, opportunity_id="OPP-CUSTOM-001")

    assert opp.opportunity_id == "OPP-CUSTOM-001"
    assert "created_by" in opp.provenance
    assert opp.provenance["created_by"] == "OpportunityEngine"
    assert opp.provenance["marketplace"] == Marketplace.MERCADO_LIBRE.value
    assert opp.provenance["source_listing_id"] == "MLC-PROV"


# --------------------------------------------------------------------------
# 11. Freshness & Temporalidad
# --------------------------------------------------------------------------
def test_freshness_and_temporal_metadata(engine, make_listing):
    listing = make_listing("MLC-TIME", "Item Temporal", "12000", sold=20)
    now = datetime.now(timezone.utc)
    visit = VisitSignal("MLC-TIME", "30d", 300, 30, 1.0, "ML", now, Confidence.MEDIUM)
    ev = MarketEvidence(listing=listing, traffic_signals=[visit], confidence=Confidence.MEDIUM)

    opp = engine.create_opportunity(ev)

    assert isinstance(opp.created_at, datetime)
    assert isinstance(opp.updated_at, datetime)
    assert opp.created_at <= opp.updated_at


# --------------------------------------------------------------------------
# 12. Confidence propagation
# --------------------------------------------------------------------------
def test_confidence_propagation(engine, make_listing):
    listing = make_listing()
    ev_high = MarketEvidence(listing=listing, confidence=Confidence.HIGH)
    ev_low = MarketEvidence(listing=listing, confidence=Confidence.LOW)

    opp_high = engine.create_opportunity(ev_high)
    opp_low = engine.create_opportunity(ev_low)

    assert opp_high.confidence == Confidence.HIGH
    assert opp_low.confidence == Confidence.LOW


# --------------------------------------------------------------------------
# 13, 14, 15, 16, 17, 18. Monitoring, reevaluación e historial inmutable
# --------------------------------------------------------------------------
def test_opportunity_monitoring_and_history_preservation(engine, make_listing):
    # Estado inicial: solo datos del catálogo, score bajo/medio, evidencia parcial
    listing_initial = make_listing("MLC-MON", "Cámara de Seguridad WiFi", "29990", sold=20)
    ev_initial = MarketEvidence(listing=listing_initial, confidence=Confidence.LOW)

    opp_v1 = engine.create_opportunity(ev_initial)
    initial_score = opp_v1.score
    initial_readiness = opp_v1.readiness
    assert len(opp_v1.history) == 0

    # Nueva observación: se obtienen visitas de tráfico masivo y reviews positivos
    visit_new = VisitSignal("MLC-MON", "30d", 1800, 30, 1.0, "ML", datetime.now(timezone.utc), Confidence.HIGH)
    rev_obj_new = Review(
        external_id="REV-4",
        rating=5,
        text="Increíble",
        date=datetime.now(timezone.utc),
        reviewable_object="MLC-MON"
    )
    reviews_new = ReviewSignal(
        item_id="MLC-MON",
        total_reviews=60,
        average_rating=4.9,
        reviews=[rev_obj_new],
        paging={"total": 60, "limit": 10, "offset": 0},
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    price_new = PriceSignal(ratio=Decimal("0.88"), position="UNDER_MARKET")

    ev_updated = MarketEvidence(
        listing=listing_initial,
        traffic_signals=[visit_new],
        review_signals=[reviews_new],
        price_signals=[price_new],
        confidence=Confidence.HIGH
    )

    opp_v2 = engine.reevaluate_opportunity(
        current_opportunity=opp_v1,
        new_evidence=ev_updated,
        reason="Deep market investigation completed with traffic and review metrics"
    )

    # Verificaciones de monitoreo y temporalidad:
    # 1. Objeto nuevo inmutable
    assert opp_v2 is not opp_v1
    
    # 2. Cambio de score
    assert opp_v2.score > initial_score
    assert opp_v2.score >= Decimal("50.0")

    # 3. Cambio de readiness y sufficiency
    assert opp_v2.evidence_sufficiency == EvidenceSufficiency.SUFFICIENT
    assert opp_v2.readiness == OpportunityReadiness.READY
    assert opp_v2.confidence == Confidence.HIGH

    # 4. Preservación del historial
    assert len(opp_v2.history) == 1
    history_entry = opp_v2.history[0]
    assert history_entry.previous_score == initial_score
    assert history_entry.new_score == opp_v2.score
    assert history_entry.previous_readiness == initial_readiness
    assert history_entry.new_readiness == OpportunityReadiness.READY
    assert history_entry.previous_confidence == Confidence.LOW
    assert history_entry.new_confidence == Confidence.HIGH
    assert "traffic and review metrics" in history_entry.reason
    assert history_entry.previous_evidence == ev_initial


# --------------------------------------------------------------------------
# 19. Integración con AutonomousLoop
# --------------------------------------------------------------------------
def test_autonomous_loop_uses_opportunity_intelligence(make_listing):
    listing_a = make_listing("MLC-AUTO-1", "Proyector Portátil HD", "85000", sold=120)
    listing_b = make_listing("MLC-AUTO-2", "Pantalla de Proyección", "35000", sold=15)

    executor = MarketDiscoveryActionExecutor()
    # Sembrar listings explorados
    executor._cached_listings[listing_a.external_id] = listing_a
    executor._cached_listings[listing_b.external_id] = listing_b
    executor._cached_evidences[listing_a.external_id] = MarketEvidence(listing=listing_a)
    executor._cached_evidences[listing_b.external_id] = MarketEvidence(listing=listing_b)

    # Ejecutar acción de comparación a través del executor
    comp_decision = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Comparar proyector contra pantalla",
        parameters={"operation": "COMPARE", "item_a": "MLC-AUTO-1", "item_b": "MLC-AUTO-2"}
    )
    state = LoopState(mission_id="m1", iteration=1, goal="test")
    comp_res = executor.execute(comp_decision, state)

    assert comp_res["status"] == "SUCCESS"
    assert comp_res["operation"] == "COMPARE"
    assert comp_res["winner_id"] == "MLC-AUTO-1"

    # Ejecutar acción de rechazo formal
    rej_decision = LoopDecision(
        action=LoopAction.REJECT,
        reason="Mercado inviable",
        target="MLC-AUTO-2",
        parameters={"operation": "REJECT", "rejection_reason": "LOW_SCORE", "details": "Demanda insuficiente"}
    )
    rej_res = executor.execute(rej_decision, state)

    assert rej_res["status"] == "SUCCESS"
    assert rej_res["operation"] == "REJECT"
    assert rej_res["readiness"] == "REJECTED"
    assert rej_res["rejection_reason"] == "LOW_SCORE"
