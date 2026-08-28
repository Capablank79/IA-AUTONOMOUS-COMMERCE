from src.domain.market_intelligence.models import CatalogListingBridge
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.product_catalog_data_source import (
    MercadoLibreProductCatalogDataSource,
)


class CatalogListingBridgeService:
    """
    Application service for bridging catalog products to their marketplace listings.
    """

    def __init__(
        self,
        oauth_service,
        data_source_factory=MercadoLibreProductCatalogDataSource,
    ):
        self.oauth_service = oauth_service
        self.data_source_factory = data_source_factory

    def get_product_items(
        self,
        user_id: str,
        catalog_product_id: str,
    ) -> CatalogListingBridge:
        connection = self.oauth_service.get_valid_connection(
            provider="mercadolibre",
            user_id=user_id,
        )

        api_client = MercadoLibreApiClient(
            access_token=connection.access_token,
        )

        data_source = self.data_source_factory(
            api_client=api_client,
        )

        return data_source.get_product_items(product_id=catalog_product_id)
