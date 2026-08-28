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
                        "id": "MLC_PROD_123",
                        "name": "Producto de prueba",
                        "buy_box_winner": {
                            "item_id": "MLC123",
                            "price": 19990,
                            "currency_id": "CLP",
                            "sold_quantity": 75,
                            "available_quantity": 10,
                            "seller_id": 999,
                            "condition": "new",
                            "shipping": {"free_shipping": True},
                            "category_id": "MLC1234",
                        }
                    }
                ],
            }

    client = FakeApiClient()
    source = MercadoLibreMarketplaceDataSource(client)

    criteria = SearchCriteria(
        query="producto de prueba",
        marketplace=Marketplace.MERCADO_LIBRE,
        limit=10,
        condition="new",
    )

    snapshot = source.fetch_snapshot(criteria)

    assert client.path.startswith("/products/search?")
    assert "q=producto+de+prueba" in client.path
    assert "site_id=MLC" in client.path
    assert "status=active" in client.path

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
