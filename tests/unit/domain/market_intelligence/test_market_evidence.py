from datetime import datetime, timezone
from decimal import Decimal
import pytest

from src.domain.market_intelligence.models import (
    MarketEvidence,
    MarketListing,
    Marketplace,
    Money,
    VisitSignal,
    Confidence,
    TrendSignal,
    PriceSignal,
    DemandSignal,
)

@pytest.fixture
def sample_listing():
    return MarketListing(
        external_id="MLC123456",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Product",
        price=Money(amount=Decimal("10000"), currency="CLP"),
        sold_quantity=50,
        available_quantity=10,
        seller_id="SELLER_123",
        condition="new",
        shipping_info={"free_shipping": True},
        category="MLA123",
    )

def test_evidence_can_transport_visit_signal(sample_listing):
    # 1. Una evidencia puede transportar VisitSignal
    # 2. VisitSignal puede asociarse al item/listing correcto
    visit_signal = VisitSignal(
        item_id=sample_listing.external_id,
        window="30d",
        total_visits=1000,
        observed_days=30,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH,
    )
    
    evidence = MarketEvidence(
        listing=sample_listing,
        traffic_signals=[visit_signal]
    )
    
    assert len(evidence.traffic_signals) == 1
    assert evidence.traffic_signals[0] == visit_signal
    assert evidence.traffic_signals[0].item_id == evidence.listing.external_id

def test_visit_signal_preserves_none_and_zero(sample_listing):
    # 3. total_visits=None sigue significando UNKNOWN/ausencia de evidencia
    # 4. total_visits=0 sigue siendo cero observado
    
    # Caso None (UNKNOWN)
    visit_signal_unknown = VisitSignal(
        item_id=sample_listing.external_id,
        window="30d",
        total_visits=None,
        observed_days=0,
        coverage_ratio=0.0,
        source="mercadolibre_visits",
        observed_at=datetime.now(timezone.utc),
    )
    
    evidence_unknown = MarketEvidence(
        listing=sample_listing,
        traffic_signals=[visit_signal_unknown]
    )
    assert evidence_unknown.traffic_signals[0].total_visits is None
    
    # Caso 0 (Observado cero)
    visit_signal_zero = VisitSignal(
        item_id=sample_listing.external_id,
        window="30d",
        total_visits=0,
        observed_days=30,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=datetime.now(timezone.utc),
    )
    
    evidence_zero = MarketEvidence(
        listing=sample_listing,
        traffic_signals=[visit_signal_zero]
    )
    assert evidence_zero.traffic_signals[0].total_visits == 0

def test_visit_signal_preserves_metadata(sample_listing):
    # 5. VisitSignal conserva source, observed_at y confidence
    now = datetime.now(timezone.utc)
    visit_signal = VisitSignal(
        item_id=sample_listing.external_id,
        window="7d",
        total_visits=500,
        observed_days=7,
        coverage_ratio=1.0,
        source="custom_source",
        observed_at=now,
        confidence=Confidence.MEDIUM,
    )
    
    evidence = MarketEvidence(
        listing=sample_listing,
        traffic_signals=[visit_signal]
    )
    
    signal = evidence.traffic_signals[0]
    assert signal.source == "custom_source"
    assert signal.observed_at == now
    assert signal.confidence == Confidence.MEDIUM

def test_absence_of_visit_signal_does_not_break_listing(sample_listing):
    # 6. La ausencia de VisitSignal no rompe listings existentes
    evidence = MarketEvidence(listing=sample_listing)
    
    assert evidence.listing == sample_listing
    assert evidence.traffic_signals == []
    assert evidence.trend_signals == []
    assert evidence.price_signals == []
    assert evidence.demand_signals == []

def test_no_new_score_is_calculated(sample_listing):
    # 7. No se calcula ningún score nuevo como consecuencia de agregar VisitSignal
    visit_signal = VisitSignal(
        item_id=sample_listing.external_id,
        window="30d",
        total_visits=1000,
        observed_days=30,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=datetime.now(timezone.utc),
    )
    
    evidence = MarketEvidence(
        listing=sample_listing,
        traffic_signals=[visit_signal]
    )
    
    # MarketEvidence should not have an opportunity_score attribute
    assert not hasattr(evidence, "opportunity_score")
    # It is purely a structural transport, not a scorer

def test_integration_preserves_other_signals(sample_listing):
    # 8. La integración no rompe TrendSignal ni PriceSignal
    visit_signal = VisitSignal(
        item_id=sample_listing.external_id,
        window="30d",
        total_visits=1000,
        observed_days=30,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=datetime.now(timezone.utc),
    )
    
    trend_signal = TrendSignal(
        keyword="test",
        rank=1,
        matched=True,
        trend_score=Decimal("1.0")
    )
    
    price_signal = PriceSignal(
        ratio=Decimal("0.9"),
        position="UNDER_MARKET"
    )
    
    evidence = MarketEvidence(
        listing=sample_listing,
        traffic_signals=[visit_signal],
        trend_signals=[trend_signal],
        price_signals=[price_signal]
    )
    
    assert len(evidence.traffic_signals) == 1
    assert len(evidence.trend_signals) == 1
    assert len(evidence.price_signals) == 1
    
    assert evidence.traffic_signals[0] == visit_signal
    assert evidence.trend_signals[0] == trend_signal
    assert evidence.price_signals[0] == price_signal
