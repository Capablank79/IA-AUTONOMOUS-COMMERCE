import pytest
from datetime import datetime, timezone
from decimal import Decimal

from src.domain.market_intelligence.models import (
    MarketEvidence,
    VisitSignal,
    MarketListing,
    Marketplace,
    Money,
    Confidence,
    DemandSignal,
)
from src.domain.market_intelligence.services import DemandIntelligenceService


@pytest.fixture
def base_listing():
    return MarketListing(
        external_id="TEST-123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Product",
        price=Money(amount=Decimal("1000"), currency="CLP"),
        sold_quantity=10,
        available_quantity=5,
        seller_id="SELLER-1",
        condition="new",
        shipping_info={"free_shipping": True},
        category="CAT-1"
    )

@pytest.fixture
def service():
    return DemandIntelligenceService()

def test_evidence_without_visit_signal_returns_unknown_demand(service, base_listing):
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[],
    )
    
    demand = service.calculate(evidence)
    
    assert demand.score is None
    assert demand.label == "UNKNOWN"

def test_unknown_visit_signal_does_not_become_zero_demand(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=None,
        observed_days=0,
        coverage_ratio=0.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.UNKNOWN
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    demand = service.calculate(evidence)
    
    assert demand.score is None
    assert demand.label == "UNKNOWN"

def test_zero_visits_is_distinguished_from_unknown(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=0,
        observed_days=30,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    demand = service.calculate(evidence)
    
    assert demand.score is None
    assert demand.label == "NO_TRAFFIC"

def test_positive_visits_produce_valid_demand_signal(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=150,
        observed_days=30,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    demand = service.calculate(evidence)
    
    assert demand.score is None
    assert demand.label == "OBSERVED_TRAFFIC"

def test_partial_coverage_is_preserved(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=50,
        observed_days=15,
        coverage_ratio=0.5,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.MEDIUM
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    demand = service.calculate(evidence)
    
    # Just verify the component doesn't fail and behaves correctly
    assert demand.label == "OBSERVED_TRAFFIC"
    assert evidence.traffic_signals[0].coverage_ratio == 0.5

def test_full_coverage_is_preserved(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=300,
        observed_days=30,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    demand = service.calculate(evidence)
    
    assert demand.label == "OBSERVED_TRAFFIC"
    assert evidence.traffic_signals[0].coverage_ratio == 1.0

def test_confidence_is_not_artificially_increased(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=150,
        observed_days=30,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.LOW
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
        confidence=Confidence.LOW
    )
    
    _ = service.calculate(evidence)
    
    assert evidence.confidence == Confidence.LOW
    assert evidence.traffic_signals[0].confidence == Confidence.LOW

def test_original_market_evidence_is_not_mutated(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=150,
        observed_days=30,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    original_traffic_count = len(evidence.traffic_signals)
    original_listing = evidence.listing
    
    _ = service.calculate(evidence)
    
    assert len(evidence.traffic_signals) == original_traffic_count
    assert evidence.listing is original_listing

def test_original_visit_signal_is_not_mutated(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=150,
        observed_days=30,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    _ = service.calculate(evidence)
    
    assert visit.total_visits == 150
    assert visit.observed_days == 30
    assert visit.coverage_ratio == 1.0

def test_service_is_marketplace_agnostic(service):
    generic_listing = MarketListing(
        external_id="AMZ-123",
        marketplace=Marketplace.AMAZON,
        title="Amazon Product",
        price=Money(amount=Decimal("50"), currency="USD"),
        sold_quantity=None,
        available_quantity=10,
        seller_id="AMZ-SELLER",
        condition="new",
        shipping_info={},
        category="BOOKS"
    )
    visit = VisitSignal(
        item_id="AMZ-123",
        window="30d",
        total_visits=500,
        observed_days=30,
        coverage_ratio=1.0,
        source="AMAZON_ANALYTICS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=generic_listing,
        traffic_signals=[visit],
    )
    
    demand = service.calculate(evidence)
    
    assert demand.label == "OBSERVED_TRAFFIC"
    assert demand.score is None

def test_no_opportunity_score_is_calculated(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=150,
        observed_days=30,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    demand = service.calculate(evidence)
    
    # We assert that the returned object is a DemandSignal and not a MarketOpportunity
    assert isinstance(demand, DemandSignal)
    assert not hasattr(demand, "opportunity_score")

def test_no_sales_are_inferred_from_visits(service, base_listing):
    visit = VisitSignal(
        item_id="TEST-123",
        window="30d",
        total_visits=10000, # A lot of visits
        observed_days=30,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE_VISITS",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH
    )
    evidence = MarketEvidence(
        listing=base_listing,
        traffic_signals=[visit],
    )
    
    demand = service.calculate(evidence)
    
    # The score should remain None because there is no specific sales/conversion rule
    assert demand.score is None
    assert demand.label == "OBSERVED_TRAFFIC"
