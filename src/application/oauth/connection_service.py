from datetime import datetime, timedelta, timezone

from src.domain.oauth.models import OAuthConnection
from src.domain.oauth.ports import OAuthConnectionRepository
from src.infrastructure.mercadolibre.oauth_client import MercadoLibreOAuthClient


class OAuthConnectionService:
    """
    Application service that provides a valid OAuth connection.

    Refreshes the Mercado Libre token when it is expired or
    within the configured renewal window.
    """

    def __init__(
        self,
        repository: OAuthConnectionRepository,
        oauth_client: MercadoLibreOAuthClient,
        refresh_window_seconds: int = 300,
    ):
        self.repository = repository
        self.oauth_client = oauth_client
        self.refresh_window_seconds = refresh_window_seconds

    def get_valid_connection(
        self,
        provider: str,
        user_id: str,
    ) -> OAuthConnection:
        connection = self.repository.get(provider, user_id)

        now = datetime.now(timezone.utc)
        refresh_threshold = now + timedelta(
            seconds=self.refresh_window_seconds
        )

        if connection.expires_at <= refresh_threshold:
            connection = self.oauth_client.refresh(connection)
            self.repository.save(connection)

        return connection
