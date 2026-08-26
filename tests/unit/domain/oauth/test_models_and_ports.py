from datetime import datetime, timezone

from src.domain.oauth.models import OAuthConnection
from src.domain.oauth.ports import OAuthConnectionRepository


def test_oauth_connection_model():
    expires_at = datetime(2026, 8, 25, 22, 0, tzinfo=timezone.utc)

    connection = OAuthConnection(
        provider="mercadolibre",
        user_id="55197108",
        access_token="access-token",
        refresh_token="refresh-token",
        expires_at=expires_at,
        scope="offline_access read",
        token_type="Bearer",
    )

    assert connection.provider == "mercadolibre"
    assert connection.user_id == "55197108"
    assert connection.access_token == "access-token"
    assert connection.refresh_token == "refresh-token"
    assert connection.expires_at == expires_at
    assert connection.scope == "offline_access read"
    assert connection.token_type == "Bearer"


def test_oauth_connection_repository_protocol():
    class InMemoryOAuthConnectionRepository:
        def __init__(self):
            self.connections = {}

        def save(self, connection: OAuthConnection) -> None:
            self.connections[(connection.provider, connection.user_id)] = connection

        def get(self, provider: str, user_id: str) -> OAuthConnection:
            return self.connections[(provider, user_id)]

    repository: OAuthConnectionRepository = InMemoryOAuthConnectionRepository()

    connection = OAuthConnection(
        provider="mercadolibre",
        user_id="55197108",
        access_token="access-token",
        refresh_token="refresh-token",
        expires_at=datetime(2026, 8, 25, 22, 0, tzinfo=timezone.utc),
    )

    repository.save(connection)

    assert repository.get("mercadolibre", "55197108") == connection
