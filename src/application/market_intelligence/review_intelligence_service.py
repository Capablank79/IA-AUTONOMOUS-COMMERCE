from src.domain.market_intelligence.models import ReviewSignal
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.reviews_data_source import (
    MercadoLibreReviewsDataSource,
)

class ReviewIntelligenceService:
    """
    Application service for retrieving marketplace reviews intelligence.
    """

    def __init__(
        self,
        oauth_service,
        api_client_factory=MercadoLibreApiClient,
        data_source_factory=MercadoLibreReviewsDataSource,
    ):
        self.oauth_service = oauth_service
        self.api_client_factory = api_client_factory
        self.data_source_factory = data_source_factory

    def get_reviews(
        self,
        user_id: str,
        item_id: str,
        offset: int = 0,
        limit: int = 50,
    ) -> ReviewSignal:
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

        return data_source.get_reviews(item_id=item_id, offset=offset, limit=limit)
