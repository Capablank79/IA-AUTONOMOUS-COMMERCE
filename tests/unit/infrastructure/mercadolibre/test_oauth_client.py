from datetime import datetime, timezone

from src.domain.oauth.models import OAuthConnection
from src.infrastructure.mercadolibre.oauth_client import MercadoLibreOAuthClient


def test_refresh_builds_updated_oauth_connection(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def read(self):
            return b'{"access_token":"new-access","refresh_token":"new-refresh","expires_in":21600,"scope":"read","token_type":"Bearer"}'

    captured = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["headers"] = dict(request.headers)
        captured["method"] = request.method
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(
        "src.infrastructure.mercadolibre.oauth_client.urlopen",
        fake_urlopen,
    )

    client = MercadoLibreOAuthClient(
        client_id="client-id",
        client_secret="client-secret",
    )

    connection = OAuthConnection(
        provider="mercadolibre",
        user_id="55197108",
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(timezone.utc),
        scope="old-scope",
        token_type="Bearer",
    )

    result = client.refresh(connection)

    assert captured["url"] == "https://api.mercadolibre.com/oauth/token"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 20

    assert result.provider == "mercadolibre"
    assert result.user_id == "55197108"
    assert result.access_token == "new-access"
    assert result.refresh_token == "new-refresh"
    assert result.scope == "read"
    assert result.token_type == "Bearer"
    assert result.expires_at > datetime.now(timezone.utc)


def test_refresh_raises_on_incomplete_response(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            pass

        def read(self):
            return b'{"access_token":"new-access"}'

    monkeypatch.setattr(
        "src.infrastructure.mercadolibre.oauth_client.urlopen",
        lambda request, timeout: FakeResponse(),
    )

    client = MercadoLibreOAuthClient(
        client_id="client-id",
        client_secret="client-secret",
    )

    connection = OAuthConnection(
        provider="mercadolibre",
        user_id="55197108",
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=datetime.now(timezone.utc),
    )

    import pytest
    from src.infrastructure.mercadolibre.oauth_client import MercadoLibreOAuthError

    with pytest.raises(MercadoLibreOAuthError):
        client.refresh(connection)
