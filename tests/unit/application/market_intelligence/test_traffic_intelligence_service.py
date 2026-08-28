from datetime import datetime, timezone
import pytest

from src.application.market_intelligence.traffic_intelligence_service import (
    TrafficIntelligenceService,
)
from src.domain.market_intelligence.models import Confidence, VisitSignal


def test_get_visits_uses_valid_oauth_connection_and_returns_signal():
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
    
    expected_signal = VisitSignal(
        item_id="MLC123",
        window="7d",
        total_visits=100,
        observed_days=7,
        coverage_ratio=1.0,
        source="mercadolibre_visits",
        observed_at=datetime.now(timezone.utc),
        confidence=Confidence.UNKNOWN,
        average_daily_visits=14.28
    )

    class FakeApiClient:
        def __init__(self, access_token):
            captured["access_token"] = access_token

    class FakeDataSource:
        def __init__(self, api_client):
            captured["api_client"] = api_client

        def get_visits(self, item_id: str, window_days: int) -> VisitSignal:
            captured["item_id"] = item_id
            captured["window_days"] = window_days
            return expected_signal

    service = TrafficIntelligenceService(
        oauth_service=FakeOAuthService(),
        api_client_factory=FakeApiClient,
        data_source_factory=FakeDataSource,
    )

    result = service.get_visits("55197108", "MLC123", 7)

    assert captured["access_token"] == "test-access-token"
    assert isinstance(captured["api_client"], FakeApiClient)
    assert captured["item_id"] == "MLC123"
    assert captured["window_days"] == 7
    assert result is expected_signal


def test_get_visits_propagates_data_source_error():
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

        def get_visits(self, item_id: str, window_days: int) -> VisitSignal:
            raise ValueError("Data source error")

    service = TrafficIntelligenceService(
        oauth_service=FakeOAuthService(),
        api_client_factory=FakeApiClient,
        data_source_factory=FakeDataSource,
    )

    with pytest.raises(ValueError, match="Data source error"):
        service.get_visits("55197108", "MLC123", 7)
