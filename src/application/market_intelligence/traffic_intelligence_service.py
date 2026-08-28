from src.domain.market_intelligence.models import VisitSignal
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.visits_data_source import (
    MercadoLibreVisitsDataSource,
)


class TrafficIntelligenceService:
    """
    Application service for retrieving marketplace traffic intelligence (visits).
    """

    def __init__(
        self,
        oauth_service,
        api_client_factory=MercadoLibreApiClient,
        data_source_factory=MercadoLibreVisitsDataSource,
    ):
        self.oauth_service = oauth_service
        self.api_client_factory = api_client_factory
        self.data_source_factory = data_source_factory

    def get_visits(
        self,
        user_id: str,
        item_id: str,
        window_days: int,
    ) -> VisitSignal:
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

        return data_source.get_visits(item_id=item_id, window_days=window_days)
