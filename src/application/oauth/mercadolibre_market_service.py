from src.application.oauth.connection_service import OAuthConnectionService
from src.domain.market_intelligence.models import MarketSnapshot, SearchCriteria
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.marketplace_data_source import (
    MercadoLibreMarketplaceDataSource,
)
from src.infrastructure.mercadolibre.trends_data_source import (
    MercadoLibreTrendsDataSource,
)


class MercadoLibreMarketService:
    """
    Application service for obtaining Mercado Libre market snapshots.
    """

    def __init__(self, oauth_service: OAuthConnectionService):
        self.oauth_service = oauth_service

    def search(
        self,
        user_id: str,
        criteria: SearchCriteria,
    ) -> MarketSnapshot:
        connection = self.oauth_service.get_valid_connection(
            provider="mercadolibre",
            user_id=user_id,
        )

        api_client = MercadoLibreApiClient(
            access_token=connection.access_token,
        )

        data_source = MercadoLibreMarketplaceDataSource(
            api_client=api_client,
        )

        trends_data_source = MercadoLibreTrendsDataSource(
            api_client=api_client,
        )

        snapshot = data_source.fetch_snapshot(criteria)
        trends = trends_data_source.get_trends()

        return MarketSnapshot(
            snapshot_id=snapshot.snapshot_id,
            timestamp=snapshot.timestamp,
            search_criteria=snapshot.search_criteria,
            marketplace=snapshot.marketplace,
            listings=snapshot.listings,
            total_results=snapshot.total_results,
            trends=trends,
        )
