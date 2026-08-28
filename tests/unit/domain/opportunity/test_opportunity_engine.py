import pytest
from datetime import datetime, timezone
from decimal import Decimal

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
    TrendSignal
)
from src.domain.opportunity.engine import OpportunityEngine
from src.domain.opportunity.models import OpportunityDecision, OpportunityReadiness


@pytest.fixture
def base_listing_without_sales():
    return MarketListing(
        external_id="GENERIC-123",
        marketplace=Marketplace.GENERIC,
        title="Generic Product",
        price=Money(amount=Decimal("1000"), currency="USD"),
        sold_quantity=None,
        available_quantity=10,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={},
        category="CAT-1"
    )

@pytest.fixture
def base_listing_with_sales():
    return MarketListing(
        external_id="GENERIC-123",
        marketplace=Marketplace.GENERIC,
        title="Generic Product",
        price=Money(amount=Decimal("1000"), currency="USD"),
        sold_quantity=50,
        available_quantity=10,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={},
        category="CAT-1"
    )

@pytest.fixture
def engine():
    return OpportunityEngine()

def test_engine_with_full_evidence(engine, base_listing_without_sales):
    visit = VisitSignal(
        item_id="GENERIC-123",
        window="30d",
        total_visits=1000,
        observed_days=30,
        coverage_ratio=1.0,
        source="GENERIC_ANALYTICS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    demand = DemandSignal(score=None, label="OBSERVED_TRAFFIC", confidence=Confidence.UNKNOWN, signal_type=SignalType.DERIVED)
    price = PriceSignal(ratio=Decimal("0.9"), position="UNDER_MARKET")
    trend = TrendSignal(keyword="product", rank=1, matched=True, trend_score=Decimal("0.9"))
    
    evidence = MarketEvidence(
        listing=base_listing_without_sales,
        traffic_signals=[visit],
        trend_signals=[trend],
        price_signals=[price],
        demand_signals=[demand],
        confidence=Confidence.MEDIUM
    )
    
    decision = engine.evaluate(evidence)
    
    assert isinstance(decision, OpportunityDecision)
    assert decision.opportunity_score is None
    assert decision.readiness == OpportunityReadiness.SUFFICIENT_EVIDENCE
    assert decision.confidence == Confidence.MEDIUM
    assert "Observed positive traffic" in decision.reasons
    assert "Missing supplier data (cost, MOQ)" in decision.reasons

def test_engine_without_visit_signal(engine, base_listing_without_sales):
    evidence = MarketEvidence(
        listing=base_listing_without_sales,
        traffic_signals=[],
        confidence=Confidence.LOW
    )
    decision = engine.evaluate(evidence)
    assert decision.opportunity_score is None
    assert decision.readiness == OpportunityReadiness.INSUFFICIENT_EVIDENCE
    assert decision.confidence == Confidence.LOW
    assert "Missing demand signal" in decision.reasons

def test_engine_with_visit_signal_none(engine, base_listing_without_sales):
    visit = VisitSignal(
        item_id="GENERIC-123",
        window="30d",
        total_visits=None,
        observed_days=0,
        coverage_ratio=0.0,
        source="GENERIC_ANALYTICS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.UNKNOWN
    )
    demand = DemandSignal(score=None, label="UNKNOWN")
    evidence = MarketEvidence(
        listing=base_listing_without_sales,
        traffic_signals=[visit],
        demand_signals=[demand],
        confidence=Confidence.LOW
    )
    decision = engine.evaluate(evidence)
    assert decision.opportunity_score is None
    assert decision.readiness == OpportunityReadiness.INSUFFICIENT_EVIDENCE
    assert decision.confidence == Confidence.LOW
    assert "Demand is unknown" in decision.reasons

def test_engine_with_demand_unknown(engine, base_listing_without_sales):
    demand = DemandSignal(score=None, label="UNKNOWN")
    evidence = MarketEvidence(
        listing=base_listing_without_sales,
        demand_signals=[demand],
        confidence=Confidence.LOW
    )
    decision = engine.evaluate(evidence)
    assert decision.opportunity_score is None
    assert decision.readiness == OpportunityReadiness.INSUFFICIENT_EVIDENCE
    assert decision.confidence == Confidence.LOW
    assert "Demand is unknown" in decision.reasons

def test_engine_with_demand_observed_traffic(engine, base_listing_without_sales):
    demand = DemandSignal(score=None, label="OBSERVED_TRAFFIC")
    evidence = MarketEvidence(
        listing=base_listing_without_sales,
        demand_signals=[demand],
        confidence=Confidence.HIGH
    )
    decision = engine.evaluate(evidence)
    assert decision.opportunity_score is None
    assert decision.readiness == OpportunityReadiness.SUFFICIENT_EVIDENCE
    assert decision.confidence == Confidence.HIGH
    assert "Observed positive traffic" in decision.reasons

def test_engine_with_demand_no_traffic(engine, base_listing_without_sales):
    demand = DemandSignal(score=None, label="NO_TRAFFIC")
    evidence = MarketEvidence(
        listing=base_listing_without_sales,
        demand_signals=[demand],
        confidence=Confidence.HIGH
    )
    decision = engine.evaluate(evidence)
    assert decision.opportunity_score is None
    assert decision.readiness == OpportunityReadiness.INSUFFICIENT_EVIDENCE
    assert decision.confidence == Confidence.HIGH
    assert "Observed zero traffic" in decision.reasons

def test_engine_absence_of_sold_quantity(engine, base_listing_without_sales):
    assert base_listing_without_sales.sold_quantity is None
    evidence = MarketEvidence(listing=base_listing_without_sales)
    decision = engine.evaluate(evidence)
    assert decision.opportunity_score is None

def test_engine_presence_of_sold_quantity_not_required(engine, base_listing_with_sales):
    assert base_listing_with_sales.sold_quantity == 50
    evidence = MarketEvidence(listing=base_listing_with_sales)
    decision = engine.evaluate(evidence)
    # Aun con sold_quantity, no inventamos score porque falta SPEC.
    assert decision.opportunity_score is None

def test_engine_is_marketplace_agnostic_and_no_http(engine):
    # Usando un marketplace GENERIC, el motor de dominio no hace HTTP ni asume ML.
    generic_listing = MarketListing(
        external_id="AMZ-1",
        marketplace=Marketplace.AMAZON,
        title="Amazon Product",
        price=Money(amount=Decimal("100"), currency="USD"),
        sold_quantity=None,
        available_quantity=10,
        seller_id="SELLER-AMZ",
        condition="new",
        shipping_info={},
        category="CAT"
    )
    evidence = MarketEvidence(listing=generic_listing)
    decision = engine.evaluate(evidence)
    assert decision.opportunity_score is None
    assert decision.evidence.listing.marketplace == Marketplace.AMAZON

def test_engine_does_not_mutate_evidence(engine, base_listing_without_sales):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=150,
        observed_days=30,
        coverage_ratio=1.0,
        source="GENERIC",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing_without_sales,
        traffic_signals=[visit],
    )
    original_listing = evidence.listing
    original_traffic_count = len(evidence.traffic_signals)
    
    _ = engine.evaluate(evidence)
    
    assert evidence.listing is original_listing
    assert len(evidence.traffic_signals) == original_traffic_count
    assert evidence.traffic_signals[0].total_visits == 150

def test_no_visit_signal_to_sales_conversion(engine, base_listing_without_sales):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=10000, # Tráfico masivo
        observed_days=30,
        coverage_ratio=1.0,
        source="GENERIC",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing_without_sales,
        traffic_signals=[visit],
    )
    decision = engine.evaluate(evidence)
    # No hay cálculo oculto de conversión que cambie el score a un valor de ventas
    assert decision.opportunity_score is None

def test_deterministic_behavior(engine, base_listing_without_sales):
    evidence = MarketEvidence(listing=base_listing_without_sales)
    decision1 = engine.evaluate(evidence)
    decision2 = engine.evaluate(evidence)
    
    assert decision1 == decision2

def test_compatibility_with_empty_evidence(engine, base_listing_without_sales):
    evidence = MarketEvidence(listing=base_listing_without_sales, confidence=Confidence.LOW)
    decision = engine.evaluate(evidence)
    assert decision.opportunity_score is None
    assert decision.readiness == OpportunityReadiness.INSUFFICIENT_EVIDENCE
    assert decision.confidence == Confidence.LOW
