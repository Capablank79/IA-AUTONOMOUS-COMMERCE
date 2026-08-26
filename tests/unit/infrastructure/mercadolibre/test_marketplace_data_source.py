from decimal import Decimal

from src.domain.market_intelligence.models import Marketplace, SearchCriteria
from src.infrastructure.mercadolibre.marketplace_data_source import (
    MercadoLibreMarketplaceDataSource,
)


def test_fetch_snapshot_maps_mercadolibre_results():
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
                        "title": "Producto de prueba",
                        "price": 19990,
                        "currency_id": "CLP",
                        "sold_quantity": 75,
                        "available_quantity": 10,
                        "seller": {"id": 999},
                        "condition": "new",
                        "shipping": {"free_shipping": True},
                        "category_id": "MLC1234",
                    }
                ],
            }

    client = FakeApiClient()
    source = MercadoLibreMarketplaceDataSource(client)

    criteria = SearchCriteria(
        query="producto de prueba",
        marketplace=Marketplace.MERCADO_LIBRE,
        limit=10,
        min_price=Decimal("10000"),
        max_price=Decimal("30000"),
        condition="new",
    )

    snapshot = source.fetch_snapshot(criteria)

    assert client.path.startswith("/sites/MLC/search?")
    assert "q=producto%20de%20prueba" in client.path
    assert "limit=10" in client.path
    assert "price=10000-30000" in client.path

    assert snapshot.marketplace == Marketplace.MERCADO_LIBRE
    assert snapshot.total_results == 123
    assert len(snapshot.listings) == 1

    listing = snapshot.listings[0]

    assert listing.external_id == "MLC123"
    assert listing.title == "Producto de prueba"
    assert listing.price.amount == Decimal("19990")
    assert listing.price.currency == "CLP"
    assert listing.sold_quantity == 75
    assert listing.available_quantity == 10
    assert listing.seller_id == "999"
    assert listing.condition == "new"
    assert listing.shipping_info["free_shipping"] is True
    assert listing.category == "MLC1234"
