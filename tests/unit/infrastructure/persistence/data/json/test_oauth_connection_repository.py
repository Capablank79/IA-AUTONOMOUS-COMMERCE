from datetime import datetime, timezone

import pytest

from src.domain.oauth.models import OAuthConnection
from src.infrastructure.persistence.data.json.oauth_connection_repository import (
    JsonOAuthConnectionRepository,
    OAuthConnectionNotFoundError,
)


def test_save_and_get_oauth_connection(tmp_path):
    repository = JsonOAuthConnectionRepository(tmp_path)

    connection = OAuthConnection(
        provider="mercadolibre",
        user_id="55197108",
        access_token="access-token",
        refresh_token="refresh-token",
        expires_at=datetime(2026, 8, 25, 22, 0, tzinfo=timezone.utc),
        scope="offline_access read",
        token_type="Bearer",
    )

    repository.save(connection)

    result = repository.get("mercadolibre", "55197108")

    assert result == connection


def test_get_oauth_connection_not_found(tmp_path):
    repository = JsonOAuthConnectionRepository(tmp_path)

    with pytest.raises(OAuthConnectionNotFoundError):
        repository.get("mercadolibre", "55197108")
