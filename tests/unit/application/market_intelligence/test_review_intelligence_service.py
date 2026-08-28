from datetime import datetime, timezone
import pytest

from src.application.market_intelligence.review_intelligence_service import (
    ReviewIntelligenceService,
)
from src.domain.market_intelligence.models import Confidence, Review, ReviewSignal, SignalType

def test_get_reviews_uses_valid_oauth_connection_and_returns_signal():
    connection = type(
        "Connection",
        (),
        {"access_token": "test-access-token"},
    )()

    class FakeOAuthService:
        def get_valid_connection(self, provider, user_id):
            assert provider == "mercadolibre"
            assert user_id == "55197108"
            return connection

    captured = {}
    
    expected_signal = ReviewSignal(
        item_id="MLC123",
        total_reviews=10,
        average_rating=4.5,
        reviews=[
            Review(
                external_id="R1",
                rating=5,
                text="Great product",
                date=datetime.now(timezone.utc),
                reviewable_object="MLC123",
                secondary_key="C1",
                status="active"
            )
        ],
        paging={"total": 10, "offset": 0, "limit": 50},
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.HIGH,
        signal_type=SignalType.OBSERVED
    )

    class FakeApiClient:
        def __init__(self, access_token):
            captured["access_token"] = access_token

    class FakeDataSource:
        def __init__(self, api_client):
            captured["api_client"] = api_client

        def get_reviews(self, item_id: str, offset: int = 0, limit: int = 50) -> ReviewSignal:
            captured["item_id"] = item_id
            captured["offset"] = offset
            captured["limit"] = limit
            return expected_signal

    service = ReviewIntelligenceService(
        oauth_service=FakeOAuthService(),
        api_client_factory=FakeApiClient,
        data_source_factory=FakeDataSource,
    )

    result = service.get_reviews("55197108", "MLC123", offset=0, limit=50)

    assert captured["access_token"] == "test-access-token"
    assert isinstance(captured["api_client"], FakeApiClient)
    assert captured["item_id"] == "MLC123"
    assert captured["offset"] == 0
    assert captured["limit"] == 50
    assert result is expected_signal

def test_get_reviews_propagates_data_source_error():
    connection = type(
        "Connection",
        (),
        {"access_token": "test-access-token"},
    )()

    class FakeOAuthService:
        def get_valid_connection(self, provider, user_id):
            return connection

    class FakeApiClient:
        def __init__(self, access_token):
            pass

    class FakeDataSource:
        def __init__(self, api_client):
            pass

        def get_reviews(self, item_id: str, offset: int = 0, limit: int = 50) -> ReviewSignal:
            raise ValueError("Data source error")

    service = ReviewIntelligenceService(
        oauth_service=FakeOAuthService(),
        api_client_factory=FakeApiClient,
        data_source_factory=FakeDataSource,
    )

    with pytest.raises(ValueError, match="Data source error"):
        service.get_reviews("55197108", "MLC123", offset=0, limit=50)
