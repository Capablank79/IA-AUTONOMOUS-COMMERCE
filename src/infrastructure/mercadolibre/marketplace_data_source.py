import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote

from src.domain.market_intelligence.models import (
    MarketListing,
    MarketSnapshot,
    Marketplace,
    Money,
    SearchCriteria,
)
from src.domain.market_intelligence.ports import MarketplaceDataSource
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient


class MercadoLibreMarketplaceDataSource(MarketplaceDataSource):
    """
    Mercado Libre implementation of MarketplaceDataSource.
    """

    SITE_ID = "MLC"

    def __init__(self, api_client: MercadoLibreApiClient):
        self.api_client = api_client

    def fetch_snapshot(self, criteria: SearchCriteria) -> MarketSnapshot:
        params = {
            "q": criteria.query,
            "limit": criteria.limit or 50,
        }

        if criteria.category:
            params["category"] = criteria.category

        if criteria.min_price is not None or criteria.max_price is not None:
            min_price = (
                str(criteria.min_price)
                if criteria.min_price is not None
                else ""
            )
            max_price = (
                str(criteria.max_price)
                if criteria.max_price is not None
                else ""
            )
            params["price"] = f"{min_price}-{max_price}"

        if criteria.condition:
            params["condition"] = criteria.condition

        query = "&".join(
            f"{key}={quote(str(value), safe='')}"
            for key, value in params.items()
        )

        data = self.api_client.get(
            f"/sites/{self.SITE_ID}/search?{query}"
        )

        listings = [
            self._map_listing(item)
            for item in data.get("results", [])
        ]

        return MarketSnapshot(
            snapshot_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            search_criteria=criteria,
            marketplace=Marketplace.MERCADO_LIBRE,
            listings=listings,
            total_results=data.get(
                "paging", {}
            ).get("total", len(listings)),
        )

    @staticmethod
    def _map_listing(item: dict) -> MarketListing:
        price = Decimal(str(item["price"]))

        seller = item.get("seller", {})
        seller_id = str(
            seller.get("id")
            or item.get("seller_id")
            or "unknown"
        )

        shipping = item.get("shipping") or {}

        return MarketListing(
            external_id=str(item["id"]),
            marketplace=Marketplace.MERCADO_LIBRE,
            title=item.get("title", ""),
            price=Money(
                amount=price,
                currency=item.get("currency_id", "CLP"),
            ),
            sold_quantity=int(item.get("sold_quantity") or 0),
            available_quantity=int(item.get("available_quantity") or 0),
            seller_id=seller_id,
            condition=item.get("condition", "unknown"),
            shipping_info=shipping,
            category=item.get("category_id", ""),
        )
