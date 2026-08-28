import pytest
from datetime import datetime, timezone
from decimal import Decimal

from src.domain.market_intelligence.models import (
    MarketListing,
    Marketplace,
    Money,
    VisitSignal,
    TrendSignal,
    PriceSignal,
    DemandSignal,
    Confidence
)
from src.domain.market_intelligence.services import MarketEvidenceComposer


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


@pytest.fixture
def sample_visit_signal(sample_listing):
    return VisitSignal(
        item_id=sample_listing.external_id,
        window="30d",
        total_visits=1000,
        observed_days=30,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH,
    )


@pytest.fixture
def sample_trend_signal():
    return TrendSignal(
        keyword="test",
        rank=1,
        matched=True,
        trend_score=Decimal("1.0")
    )


@pytest.fixture
def sample_price_signal():
    return PriceSignal(
        ratio=Decimal("0.9"),
        position="UNDER_MARKET"
    )


@pytest.fixture
def sample_demand_signal():
    return DemandSignal(
        score=Decimal("0.8"),
        label="HIGH"
    )


def test_composition_with_listing_only(sample_listing):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(listing=sample_listing)
    
    assert evidence.listing == sample_listing
    assert evidence.traffic_signals == []
    assert evidence.trend_signals == []
    assert evidence.price_signals == []
    assert evidence.demand_signals == []


def test_composition_with_visit_signal(sample_listing, sample_visit_signal):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(
        listing=sample_listing,
        visit_signal=sample_visit_signal
    )
    
    assert evidence.listing == sample_listing
    assert len(evidence.traffic_signals) == 1
    assert evidence.traffic_signals[0] == sample_visit_signal


def test_composition_with_visit_and_trend_signal(sample_listing, sample_visit_signal, sample_trend_signal):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(
        listing=sample_listing,
        visit_signal=sample_visit_signal,
        trend_signal=sample_trend_signal
    )
    
    assert len(evidence.traffic_signals) == 1
    assert len(evidence.trend_signals) == 1
    assert evidence.traffic_signals[0] == sample_visit_signal
    assert evidence.trend_signals[0] == sample_trend_signal


def test_composition_with_visit_and_price_signal(sample_listing, sample_visit_signal, sample_price_signal):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(
        listing=sample_listing,
        visit_signal=sample_visit_signal,
        price_signal=sample_price_signal
    )
    
    assert len(evidence.traffic_signals) == 1
    assert len(evidence.price_signals) == 1
    assert evidence.traffic_signals[0] == sample_visit_signal
    assert evidence.price_signals[0] == sample_price_signal


def test_composition_with_all_signals(
    sample_listing, 
    sample_visit_signal, 
    sample_trend_signal, 
    sample_price_signal, 
    sample_demand_signal
):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(
        listing=sample_listing,
        visit_signal=sample_visit_signal,
        trend_signal=sample_trend_signal,
        price_signal=sample_price_signal,
        demand_signal=sample_demand_signal
    )
    
    assert len(evidence.traffic_signals) == 1
    assert len(evidence.trend_signals) == 1
    assert len(evidence.price_signals) == 1
    assert len(evidence.demand_signals) == 1
    
    assert evidence.traffic_signals[0] == sample_visit_signal
    assert evidence.trend_signals[0] == sample_trend_signal
    assert evidence.price_signals[0] == sample_price_signal
    assert evidence.demand_signals[0] == sample_demand_signal


def test_absence_of_visit_signal(sample_listing, sample_trend_signal):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(
        listing=sample_listing,
        trend_signal=sample_trend_signal
    )
    
    assert len(evidence.traffic_signals) == 0
    assert len(evidence.trend_signals) == 1


def test_absence_of_all_optional_signals(sample_listing):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(listing=sample_listing)
    
    assert not evidence.traffic_signals
    assert not evidence.trend_signals
    assert not evidence.price_signals
    assert not evidence.demand_signals


def test_none_remains_none(sample_listing):
    # None total_visits should remain None (unknown traffic)
    visit_signal = VisitSignal(
        item_id=sample_listing.external_id,
        window="30d",
        total_visits=None,
        observed_days=0,
        coverage_ratio=0.0,
        source="mercadolibre_visits",
        observed_at=datetime.now(timezone.utc),
    )
    
    composer = MarketEvidenceComposer()
    evidence = composer.compose(
        listing=sample_listing,
        visit_signal=visit_signal
    )
    
    assert evidence.traffic_signals[0].total_visits is None


def test_market_evidence_does_not_contain_opportunity_score(sample_listing):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(listing=sample_listing)
    
    assert not hasattr(evidence, "opportunity_score")


def test_composer_does_not_modify_signals(
    sample_listing, 
    sample_visit_signal, 
    sample_trend_signal, 
    sample_price_signal, 
    sample_demand_signal
):
    composer = MarketEvidenceComposer()
    evidence = composer.compose(
        listing=sample_listing,
        visit_signal=sample_visit_signal,
        trend_signal=sample_trend_signal,
        price_signal=sample_price_signal,
        demand_signal=sample_demand_signal
    )
    
    # Check identity and equality to ensure objects are unmodified
    assert evidence.traffic_signals[0] is sample_visit_signal
    assert evidence.trend_signals[0] is sample_trend_signal
    assert evidence.price_signals[0] is sample_price_signal
    assert evidence.demand_signals[0] is sample_demand_signal


def test_composer_does_not_make_commercial_calculations(sample_listing, sample_visit_signal):
    # There should be no sales inference or conversions happening in the composer
    composer = MarketEvidenceComposer()
    evidence = composer.compose(
        listing=sample_listing,
        visit_signal=sample_visit_signal
    )
    
    # Evidence shouldn't have arbitrary attributes added
    assert not hasattr(evidence, "visit_score")
    assert not hasattr(evidence, "sales_estimate")


def test_composer_is_independent_of_infrastructure(sample_listing, sample_visit_signal):
    # The composer should not require any API client, OAuth, or DB connection
    # Instantiating it without any arguments proves it's infrastructure independent
    try:
        composer = MarketEvidenceComposer()
        evidence = composer.compose(listing=sample_listing, visit_signal=sample_visit_signal)
        assert evidence is not None
    except Exception as e:
        pytest.fail(f"Composer raised an exception, meaning it might depend on infra: {e}")
