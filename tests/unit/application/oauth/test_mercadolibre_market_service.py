from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.application.oauth.mercadolibre_market_service import (
    MercadoLibreMarketService,
)
from src.domain.market_intelligence.models import (
    Marketplace,
    MarketSnapshot,
    SearchCriteria,
)
from src.domain.oauth.models import OAuthConnection


def test_search_uses_valid_oauth_connection(monkeypatch):
    connection = OAuthConnection(
        provider="mercadolibre",
        user_id="55197108",
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        scope="read",
        token_type="Bearer",
    )

    class FakeOAuthService:
        def get_valid_connection(self, provider, user_id):
            assert provider == "mercadolibre"
            assert user_id == "55197108"
            return connection

    captured = {}

    class FakeApiClient:
        def __init__(self, access_token):
            captured["access_token"] = access_token

    class FakeTrendsDataSource:
        def __init__(self, api_client):
            captured["trends_api_client"] = api_client

        def get_trends(self):
            captured["trends_called"] = True
            return [
                {
                    "keyword": "aspiradora",
                    "url": "https://example.com/aspiradora",
                    "rank": 1,
                }
            ]

    class FakeDataSource:
        def __init__(self, api_client):
            captured["api_client"] = api_client

        def fetch_snapshot(self, criteria):
            captured["criteria"] = criteria
            return MarketSnapshot(
                snapshot_id="snapshot-1",
                timestamp=datetime.now(timezone.utc),
                search_criteria=criteria,
                marketplace=Marketplace.MERCADO_LIBRE,
                listings=[],
                total_results=0,
            )

    monkeypatch.setattr(
        "src.application.oauth.mercadolibre_market_service.MercadoLibreApiClient",
        FakeApiClient,
    )
    monkeypatch.setattr(
        "src.application.oauth.mercadolibre_market_service.MercadoLibreMarketplaceDataSource",
        FakeDataSource,
    )
    monkeypatch.setattr(
        "src.application.oauth.mercadolibre_market_service.MercadoLibreTrendsDataSource",
        FakeTrendsDataSource,
    )

    criteria = SearchCriteria(
        query="aspiradora",
        marketplace=Marketplace.MERCADO_LIBRE,
        limit=20,
        min_price=Decimal("10000"),
        max_price=Decimal("50000"),
    )

    service = MercadoLibreMarketService(FakeOAuthService())

    result = service.search(
        user_id="55197108",
        criteria=criteria,
    )

    assert captured["access_token"] == "test-access-token"
    assert captured["criteria"] == criteria
    assert result.snapshot_id == "snapshot-1"
    assert result.total_results == 0
