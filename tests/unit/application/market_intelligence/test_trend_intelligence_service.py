from src.application.market_intelligence.trend_intelligence_service import (
    TrendIntelligenceService,
)


def test_get_trends_uses_valid_oauth_connection():
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

    class FakeApiClient:
        def __init__(self, access_token):
            captured["access_token"] = access_token

    class FakeDataSource:
        def __init__(self, api_client):
            captured["api_client"] = api_client

        def get_trends(self):
            return [
                {
                    "keyword": "aspiradora",
                    "url": "https://example.com/aspiradora",
                    "rank": 1,
                }
            ]

    service = TrendIntelligenceService(
        oauth_service=FakeOAuthService(),
        api_client_factory=FakeApiClient,
        data_source_factory=FakeDataSource,
    )

    result = service.get_trends("55197108")

    assert captured["access_token"] == "test-access-token"
    assert isinstance(captured["api_client"], FakeApiClient)
    assert result == [
        {
            "keyword": "aspiradora",
            "url": "https://example.com/aspiradora",
            "rank": 1,
        }
    ]
