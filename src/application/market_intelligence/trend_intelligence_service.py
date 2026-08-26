from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.trends_data_source import (
    MercadoLibreTrendsDataSource,
)


class TrendIntelligenceService:
    """
    Application service for retrieving marketplace trend intelligence.
    """

    def __init__(
        self,
        oauth_service,
        api_client_factory=MercadoLibreApiClient,
        data_source_factory=MercadoLibreTrendsDataSource,
    ):
        self.oauth_service = oauth_service
        self.api_client_factory = api_client_factory
        self.data_source_factory = data_source_factory

    def get_trends(self, user_id: str) -> list[dict]:
        connection = self.oauth_service.get_valid_connection(
            provider="mercadolibre",
            user_id=user_id,
        )

        api_client = self.api_client_factory(
            access_token=connection.access_token,
        )

        data_source = self.data_source_factory(
            api_client=api_client,
        )

        return data_source.get_trends()
