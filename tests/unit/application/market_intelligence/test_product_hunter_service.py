from src.application.market_intelligence.product_hunter_service import (
    ProductHunterService,
)
from src.domain.market_intelligence.models import CatalogProduct, Marketplace


def test_search_uses_valid_oauth_connection():
    class FakeConnection:
        access_token = "test-token"

    class FakeOAuthService:
        def __init__(self):
            self.provider = None
            self.user_id = None

        def get_valid_connection(self, provider, user_id):
            self.provider = provider
            self.user_id = user_id
            return FakeConnection()

    class FakeCatalogDataSource:
        def __init__(self, api_client):
            self.api_client = api_client

        def search_products(self, query, marketplace, limit=None):
            return [
                CatalogProduct(
                    product_id="MLC123",
                    marketplace=marketplace,
                    title="Aspiradora portátil",
                    domain_id="MLC-VACUUM_AND_STEAM_CLEANERS",
                    brand="Arcashopping",
                    model="ABC-123",
                    attributes={},
                    thumbnail=None,
                    status="active",
                )
            ]

    service = ProductHunterService(
        oauth_service=FakeOAuthService(),
        data_source_factory=FakeCatalogDataSource,
    )

    products = service.search(
        user_id="55197108",
        query="aspiradora",
        limit=5,
    )

    assert len(products) == 1
    assert products[0].product_id == "MLC123"
    assert products[0].title == "Aspiradora portátil"
