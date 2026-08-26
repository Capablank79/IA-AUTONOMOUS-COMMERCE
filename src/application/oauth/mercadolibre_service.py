from src.application.oauth.connection_service import OAuthConnectionService
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient


class MercadoLibreService:
    """
    Application service for authenticated Mercado Libre operations.
    """

    def __init__(self, oauth_service: OAuthConnectionService):
        self.oauth_service = oauth_service

    def get_current_user(self, user_id: str) -> dict:
        connection = self.oauth_service.get_valid_connection(
            provider="mercadolibre",
            user_id=user_id,
        )

        client = MercadoLibreApiClient(
            access_token=connection.access_token,
        )

        return client.get("/users/me")
