from src.domain.market_intelligence.models import CatalogProduct, Marketplace
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.mercadolibre.product_catalog_data_source import (
    MercadoLibreProductCatalogDataSource,
)


class ProductHunterService:
    """
    Application service for discovering catalog products.
    """

    def __init__(
        self,
        oauth_service,
        data_source_factory=MercadoLibreProductCatalogDataSource,
    ):
        self.oauth_service = oauth_service
        self.data_source_factory = data_source_factory

    def search(
        self,
        user_id: str,
        query: str,
        limit: int | None = None,
    ) -> list[CatalogProduct]:
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

        return data_source.search_products(
            query=query,
            marketplace=Marketplace.MERCADO_LIBRE,
            limit=limit,
        )
