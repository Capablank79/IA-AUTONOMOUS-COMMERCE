import pytest
from unittest.mock import MagicMock
from datetime import datetime, timezone
from decimal import Decimal

from src.domain.market_intelligence.models import (
    MarketListing,
    Marketplace,
    Money,
    VisitSignal,
    Confidence,
    MarketEvidence,
)
from src.domain.market_intelligence.services import MarketEvidenceComposer
from src.application.market_intelligence.market_evidence_enrichment_service import (
    MarketEvidenceEnrichmentService,
)

@pytest.fixture
def dummy_listing():
    return MarketListing(
        external_id="MLC123",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Product",
        price=Money(amount=Decimal("1000"), currency="CLP"),
        sold_quantity=10,
        available_quantity=5,
        seller_id="SELLER1",
        condition="new",
        shipping_info={"free_shipping": True},
        category="CAT123"
    )

@pytest.fixture
def dummy_listing_2():
    return MarketListing(
        external_id="MLC456",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Product 2",
        price=Money(amount=Decimal("2000"), currency="CLP"),
        sold_quantity=20,
        available_quantity=10,
        seller_id="SELLER2",
        condition="new",
        shipping_info={"free_shipping": False},
        category="CAT123"
    )

@pytest.fixture
def mock_traffic_service():
    return MagicMock()

@pytest.fixture
def composer():
    return MarketEvidenceComposer()

@pytest.fixture
def enrichment_service(mock_traffic_service, composer):
    return MarketEvidenceEnrichmentService(
        traffic_service=mock_traffic_service,
        evidence_composer=composer,
    )

def test_enrich_listing_obtains_signal_and_produces_evidence(
    enrichment_service, mock_traffic_service, dummy_listing
):
    # Arrange
    user_id = "USER123"
    window_days = 7
    expected_signal = VisitSignal(
        item_id=dummy_listing.external_id,
        window="7d",
        total_visits=100,
        observed_days=7,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH,
    )
    mock_traffic_service.get_visits.return_value = expected_signal

    # Act
    evidence = enrichment_service.enrich_listing(
        user_id=user_id,
        listing=dummy_listing,
        window_days=window_days,
    )

    # Assert 1: Produce MarketEvidence
    assert isinstance(evidence, MarketEvidence)
    
    # Assert 2: external_id propagado correctamente a get_visits
    # Assert 3 & 4: user_id y window_days propagados
    mock_traffic_service.get_visits.assert_called_once_with(
        user_id=user_id,
        item_id=dummy_listing.external_id,
        window_days=window_days,
    )

    # Assert 5: VisitSignal asociado correctamente al MarketEvidence
    assert len(evidence.traffic_signals) == 1
    assert evidence.traffic_signals[0] == expected_signal

    # Assert 6: MarketListing original permanece intacto
    assert evidence.listing == dummy_listing

    # Assert 10 & 11: No calcula scores (opportunity o demand)
    assert not hasattr(evidence, "opportunity_score")
    assert not evidence.demand_signals

def test_enrich_listing_preserves_none_total_visits(
    enrichment_service, mock_traffic_service, dummy_listing
):
    # Arrange (Assert 7)
    expected_signal = VisitSignal(
        item_id=dummy_listing.external_id,
        window="7d",
        total_visits=None,
        observed_days=0,
        coverage_ratio=0.0,
        source="MERCADO_LIBRE",
        observed_at=datetime.now(timezone.utc),
    )
    mock_traffic_service.get_visits.return_value = expected_signal

    # Act
    evidence = enrichment_service.enrich_listing("USER1", dummy_listing, 7)

    # Assert
    assert evidence.traffic_signals[0].total_visits is None

def test_enrich_listing_preserves_zero_total_visits(
    enrichment_service, mock_traffic_service, dummy_listing
):
    # Arrange (Assert 8)
    expected_signal = VisitSignal(
        item_id=dummy_listing.external_id,
        window="7d",
        total_visits=0,
        observed_days=7,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE",
        observed_at=datetime.now(timezone.utc),
    )
    mock_traffic_service.get_visits.return_value = expected_signal

    # Act
    evidence = enrichment_service.enrich_listing("USER1", dummy_listing, 7)

    # Assert
    assert evidence.traffic_signals[0].total_visits == 0

def test_enrich_listing_propagates_traffic_service_errors(
    enrichment_service, mock_traffic_service, dummy_listing
):
    # Arrange (Assert 9)
    mock_traffic_service.get_visits.side_effect = RuntimeError("API Rate Limit")

    # Act & Assert
    with pytest.raises(RuntimeError) as exc_info:
        enrichment_service.enrich_listing("USER1", dummy_listing, 7)
    
    assert "API Rate Limit" in str(exc_info.value)

def test_enrich_listings_associates_correct_signal_to_each_listing(
    enrichment_service, mock_traffic_service, dummy_listing, dummy_listing_2
):
    # Arrange (Assert 13)
    signal_1 = VisitSignal(
        item_id=dummy_listing.external_id,
        window="7d",
        total_visits=10,
        observed_days=7,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE",
        observed_at=datetime.now(timezone.utc),
    )
    signal_2 = VisitSignal(
        item_id=dummy_listing_2.external_id,
        window="7d",
        total_visits=20,
        observed_days=7,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE",
        observed_at=datetime.now(timezone.utc),
    )

    def side_effect(user_id, item_id, window_days):
        if item_id == "MLC123":
            return signal_1
        elif item_id == "MLC456":
            return signal_2
        return None

    mock_traffic_service.get_visits.side_effect = side_effect

    # Act
    evidences = enrichment_service.enrich_listings(
        user_id="USER1",
        listings=[dummy_listing, dummy_listing_2],
        window_days=7
    )

    # Assert
    assert len(evidences) == 2
    assert evidences[0].listing.external_id == "MLC123"
    assert evidences[0].traffic_signals[0] == signal_1
    assert evidences[1].listing.external_id == "MLC456"
    assert evidences[1].traffic_signals[0] == signal_2

def test_enrich_listing_does_not_modify_visit_signal(
    enrichment_service, mock_traffic_service, dummy_listing
):
    # Arrange (Assert 12)
    original_signal = VisitSignal(
        item_id=dummy_listing.external_id,
        window="7d",
        total_visits=15,
        observed_days=7,
        coverage_ratio=1.0,
        source="MERCADO_LIBRE",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.MEDIUM,
    )
    mock_traffic_service.get_visits.return_value = original_signal

    # Act
    evidence = enrichment_service.enrich_listing("USER1", dummy_listing, 7)

    # Assert
    # Check that the signal in evidence is exactly the original object (or identical)
    # Dataclasses with frozen=True are immutable, so it inherently cannot modify it, 
    # but we assert it's the exact same state/instance.
    assert evidence.traffic_signals[0] is original_signal
    assert evidence.traffic_signals[0].total_visits == 15
    assert evidence.traffic_signals[0].confidence == Confidence.MEDIUM
