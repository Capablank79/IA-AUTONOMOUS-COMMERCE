import uuid
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import quote, urlencode

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
        listings = []
        total_found = 0

        # Estrategia 1: Si se proporciona categoría o query, intentar Highlights si query es una categoría o highlights_category
        if criteria.category:
            try:
                hl = self.api_client.get(f"/highlights/{self.SITE_ID}/category/{criteria.category}")
                content = hl.get("content", [])
                total_found = len(content)
                for hl_item in content[:criteria.limit or 20]:
                    prod_id = hl_item.get("id")
                    if prod_id:
                        try:
                            prod_data = self.api_client.get(f"/products/{prod_id}")
                            items_data = self.api_client.get(f"/products/{prod_id}/items")
                            items_list = items_data.get("results", [])
                            if items_list:
                                top_item = items_list[0]
                                listings.append(
                                    self._map_listing(top_item, fallback_title=prod_data.get("name", ""))
                                )
                        except Exception:
                            continue
            except Exception:
                pass

        # Estrategia 2: Búsqueda general en catálogo /products/search
        if not listings and criteria.query:
            params = {
                "q": criteria.query,
                "limit": criteria.limit or 50,
                "site_id": self.SITE_ID,
                "status": "active",
            }

            if criteria.category:
                params["category"] = criteria.category

            if criteria.condition:
                params["condition"] = criteria.condition

            query = urlencode(params)
            data = self.api_client.get(f"/products/search?{query}")
            total_found = data.get("paging", {}).get("total", 0)

            for item in data.get("results", []):
                winner = item.get("buy_box_winner")
                if winner:
                    listings.append(
                        self._map_listing(winner, fallback_title=item.get("name", ""))
                    )
                else:
                    prod_id = item.get("id")
                    if prod_id:
                        try:
                            items_data = self.api_client.get(f"/products/{prod_id}/items")
                            items_list = items_data.get("results", [])
                            if items_list:
                                top_item = items_list[0]
                                listings.append(
                                    self._map_listing(top_item, fallback_title=item.get("name", ""))
                                )
                        except Exception:
                            pass

        return MarketSnapshot(
            snapshot_id=str(uuid.uuid4()),
            timestamp=datetime.now(timezone.utc),
            search_criteria=criteria,
            marketplace=Marketplace.MERCADO_LIBRE,
            listings=listings,
            total_results=total_found or len(listings),
        )

    @staticmethod
    def _map_listing(item: dict, fallback_title: str = "") -> MarketListing:
        price = Decimal(str(item["price"]))

        seller = item.get("seller", {})
        seller_id = str(
            seller.get("id")
            or item.get("seller_id")
            or "unknown"
        )

        shipping = item.get("shipping") or {}

        raw_sold = item.get("sold_quantity")
        sold_qty = int(raw_sold) if raw_sold is not None else None

        return MarketListing(
            external_id=str(item.get("item_id") or item.get("id")),
            marketplace=Marketplace.MERCADO_LIBRE,
            title=item.get("title") or fallback_title,
            price=Money(
                amount=price,
                currency=item.get("currency_id", "CLP"),
            ),
            sold_quantity=sold_qty,
            available_quantity=int(item.get("available_quantity") or 0),
            seller_id=seller_id,
            condition=item.get("condition", "unknown"),
            shipping_info=shipping,
            category=item.get("category_id", ""),
        )
