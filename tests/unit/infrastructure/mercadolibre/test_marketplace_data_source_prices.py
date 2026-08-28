from decimal import Decimal

from src.domain.market_intelligence.models import Marketplace, SearchCriteria
from src.infrastructure.mercadolibre.marketplace_data_source import (
    MercadoLibreMarketplaceDataSource,
)


def make_source():
    class FakeApiClient:
        def __init__(self):
            self.path = None

        def get(self, path):
            self.path = path
            return {"paging": {"total": 0}, "results": []}

    client = FakeApiClient()
    return MercadoLibreMarketplaceDataSource(client), client


def test_price_range():
    source, client = make_source()

    criteria = SearchCriteria(
        query="producto",
        marketplace=Marketplace.MERCADO_LIBRE,
        min_price=Decimal("10000"),
        max_price=Decimal("30000"),
    )

    snapshot = source.fetch_snapshot(criteria)

    assert snapshot.search_criteria.min_price == Decimal("10000")
    assert snapshot.search_criteria.max_price == Decimal("30000")
    assert "price=" not in client.path


def test_min_price_only():
    source, client = make_source()

    criteria = SearchCriteria(
        query="producto",
        marketplace=Marketplace.MERCADO_LIBRE,
        min_price=Decimal("10000"),
    )

    snapshot = source.fetch_snapshot(criteria)

    assert snapshot.search_criteria.min_price == Decimal("10000")
    assert "price=" not in client.path


def test_max_price_only():
    source, client = make_source()

    criteria = SearchCriteria(
        query="producto",
        marketplace=Marketplace.MERCADO_LIBRE,
        max_price=Decimal("30000"),
    )

    snapshot = source.fetch_snapshot(criteria)

    assert snapshot.search_criteria.max_price == Decimal("30000")
    assert "price=" not in client.path
