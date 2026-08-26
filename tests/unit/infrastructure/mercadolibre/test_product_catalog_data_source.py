from src.domain.market_intelligence.models import Marketplace
from src.infrastructure.mercadolibre.product_catalog_data_source import (
    MercadoLibreProductCatalogDataSource,
)


def test_search_products_maps_mercadolibre_catalog_results():
    class FakeApiClient:
        def __init__(self):
            self.path = None

        def get(self, path):
            self.path = path
            return {
                "paging": {"total": 123},
                "results": [
                    {
                        "id": "MLC123",
                        "status": "active",
                        "domain_id": "MLC-VACUUM_AND_STEAM_CLEANERS",
                        "name": "Aspiradora portátil",
                        "attributes": [
                            {"id": "BRAND", "value_name": "Arcashopping"},
                            {"id": "MODEL", "value_name": "ABC-123"},
                        ],
                        "pictures": [
                            {"url": "https://example.com/image.jpg"}
                        ],
                    }
                ],
            }

    client = FakeApiClient()
    source = MercadoLibreProductCatalogDataSource(client)

    products = source.search_products(
        query="aspiradora",
        marketplace=Marketplace.MERCADO_LIBRE,
        limit=5,
    )

    assert client.path == (
        "/products/search?"
        "status=active&site_id=MLC&q=aspiradora&limit=5"
    )

    assert len(products) == 1

    product = products[0]

    assert product.product_id == "MLC123"
    assert product.marketplace == Marketplace.MERCADO_LIBRE
    assert product.title == "Aspiradora portátil"
    assert product.domain_id == "MLC-VACUUM_AND_STEAM_CLEANERS"
    assert product.brand == "Arcashopping"
    assert product.model == "ABC-123"
    assert product.attributes["BRAND"] == "Arcashopping"
    assert product.attributes["MODEL"] == "ABC-123"
    assert product.thumbnail == "https://example.com/image.jpg"
    assert product.status == "active"

def test_search_products_returns_empty_list_when_no_results():
    class FakeApiClient:
        def get(self, path):
            return {
                "paging": {"total": 0},
                "results": [],
            }

    source = MercadoLibreProductCatalogDataSource(FakeApiClient())

    products = source.search_products(
        query="producto inexistente",
        marketplace=Marketplace.MERCADO_LIBRE,
        limit=5,
    )

    assert products == []


def test_search_products_handles_missing_pictures():
    class FakeApiClient:
        def get(self, path):
            return {
                "paging": {"total": 1},
                "results": [
                    {
                        "id": "MLC999",
                        "status": "active",
                        "domain_id": "MLC-TEST",
                        "name": "Producto sin imagen",
                        "attributes": [],
                    }
                ],
            }

    source = MercadoLibreProductCatalogDataSource(FakeApiClient())

    products = source.search_products(
        query="producto",
        marketplace=Marketplace.MERCADO_LIBRE,
        limit=1,
    )

    assert len(products) == 1
    assert products[0].thumbnail is None
