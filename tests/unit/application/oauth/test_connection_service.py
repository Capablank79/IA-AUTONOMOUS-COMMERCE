from datetime import datetime, timedelta, timezone

from src.application.oauth.connection_service import OAuthConnectionService
from src.domain.oauth.models import OAuthConnection


def make_connection(expires_at):
    return OAuthConnection(
        provider="mercadolibre",
        user_id="55197108",
        access_token="old-access",
        refresh_token="old-refresh",
        expires_at=expires_at,
        scope="read",
        token_type="Bearer",
    )


def test_valid_connection_does_not_refresh():
    class FakeRepository:
        def __init__(self, connection):
            self.connection = connection
            self.saved = None

        def get(self, provider, user_id):
            return self.connection

        def save(self, connection):
            self.saved = connection

    class FakeOAuthClient:
        def __init__(self):
            self.called = False

        def refresh(self, connection):
            self.called = True
            return connection

    connection = make_connection(
        datetime.now(timezone.utc) + timedelta(hours=1)
    )

    repository = FakeRepository(connection)
    client = FakeOAuthClient()

    service = OAuthConnectionService(repository, client)

    result = service.get_valid_connection(
        "mercadolibre",
        "55197108",
    )

    assert result == connection
    assert client.called is False
    assert repository.saved is None


def test_expired_connection_is_refreshed_and_saved():
    class FakeRepository:
        def __init__(self, connection):
            self.connection = connection
            self.saved = None

        def get(self, provider, user_id):
            return self.connection

        def save(self, connection):
            self.saved = connection
            self.connection = connection

    class FakeOAuthClient:
        def __init__(self):
            self.called = False

        def refresh(self, connection):
            self.called = True
            return OAuthConnection(
                provider=connection.provider,
                user_id=connection.user_id,
                access_token="new-access",
                refresh_token="new-refresh",
                expires_at=datetime.now(timezone.utc) + timedelta(hours=6),
                scope=connection.scope,
                token_type=connection.token_type,
            )

    connection = make_connection(
        datetime.now(timezone.utc) - timedelta(minutes=1)
    )

    repository = FakeRepository(connection)
    client = FakeOAuthClient()

    service = OAuthConnectionService(repository, client)

    result = service.get_valid_connection(
        "mercadolibre",
        "55197108",
    )

    assert client.called is True
    assert repository.saved == result
    assert result.access_token == "new-access"
    assert result.refresh_token == "new-refresh"
