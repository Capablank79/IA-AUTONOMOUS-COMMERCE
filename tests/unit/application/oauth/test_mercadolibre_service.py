from datetime import datetime, timedelta, timezone

from src.application.oauth.mercadolibre_service import MercadoLibreService
from src.domain.oauth.models import OAuthConnection


def test_get_current_user_uses_valid_oauth_connection(monkeypatch):
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

        def get(self, path):
            captured["path"] = path
            return {
                "id": 55197108,
                "nickname": "test-user",
            }

    monkeypatch.setattr(
        "src.application.oauth.mercadolibre_service.MercadoLibreApiClient",
        FakeApiClient,
    )

    service = MercadoLibreService(FakeOAuthService())

    result = service.get_current_user("55197108")

    assert captured["access_token"] == "test-access-token"
    assert captured["path"] == "/users/me"
    assert result["id"] == 55197108
    assert result["nickname"] == "test-user"
