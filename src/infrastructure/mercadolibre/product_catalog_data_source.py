from datetime import datetime
from urllib.parse import urlencode

from src.domain.market_intelligence.models import (
    CatalogProduct,
    Marketplace,
    ProductPicker,
    ProductVariant,
    CatalogListingBridge,
)
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient
from src.infrastructure.market_intelligence.mercadolibre.mapper import MercadoLibreMapper


class MercadoLibreProductCatalogDataSource:
    def __init__(self, api_client: MercadoLibreApiClient):
        self.api_client = api_client

    def search_products(
        self,
        query: str,
        marketplace: Marketplace,
        limit: int | None = None,
    ) -> list[CatalogProduct]:
        if marketplace != Marketplace.MERCADO_LIBRE:
            raise ValueError(
                "MercadoLibreProductCatalogDataSource only supports "
                "Marketplace.MERCADO_LIBRE"
            )

        params = {
            "status": "active",
            "site_id": "MLC",
            "q": query,
        }

        if limit is not None:
            params["limit"] = str(limit)

        path = f"/products/search?{urlencode(params)}"
        data = self.api_client.get(path)

        products = []

        for item in data.get("results", []):
            attributes = {
                attribute["id"]: attribute.get("value_name")
                for attribute in item.get("attributes", [])
                if attribute.get("id")
            }

            pictures = item.get("pictures", [])
            thumbnail = pictures[0].get("url") if pictures else None

            products.append(
                CatalogProduct(
                    product_id=item["id"],
                    marketplace=Marketplace.MERCADO_LIBRE,
                    title=item["name"],
                    domain_id=item["domain_id"],
                    brand=attributes.get("BRAND"),
                    model=attributes.get("MODEL"),
                    attributes=attributes,
                    thumbnail=thumbnail,
                    status=item["status"],
                )
            )

        return products

    def get_product(self, product_id: str) -> CatalogProduct:
        path = f"/products/{product_id}"
        data = self.api_client.get(path)

        attributes = {
            attribute["id"]: attribute.get("value_name")
            for attribute in data.get("attributes", [])
            if attribute.get("id")
        }

        pictures = data.get("pictures", [])
        thumbnail = pictures[0].get("url") if pictures else None

        pickers = []
        for p in data.get("pickers", []):
            variants = []
            for v in p.get("products", []):
                variants.append(
                    ProductVariant(
                        product_id=v["product_id"],
                        picker_label=v["picker_label"],
                        thumbnail=v.get("thumbnail"),
                        permalink=v.get("permalink"),
                        attributes={
                            c[0]: c[1] for c in v.get("changes", [])
                        }
                    )
                )
            pickers.append(
                ProductPicker(
                    picker_id=p["picker_id"],
                    picker_name=p["picker_name"],
                    variants=variants
                )
            )

        buy_box_winner = None
        winner_data = data.get("buy_box_winner")
        if winner_data:
            buy_box_winner = MercadoLibreMapper.to_domain(winner_data)

        return CatalogProduct(
            product_id=data["id"],
            marketplace=Marketplace.MERCADO_LIBRE,
            title=data["name"],
            domain_id=data["domain_id"],
            brand=attributes.get("BRAND"),
            model=attributes.get("MODEL"),
            attributes=attributes,
            thumbnail=thumbnail,
            status=data["status"],
            parent_id=data.get("parent_id"),
            children_ids=data.get("children_ids", []),
            pickers=pickers,
            buy_box_winner=buy_box_winner
        )

    def get_product_items(self, product_id: str) -> CatalogListingBridge:
        path = f"/products/{product_id}/items"
        data = self.api_client.get(path)

        item_ids = [
            str(item["item_id"])
            for item in data.get("results", [])
            if item.get("item_id")
        ]

        return CatalogListingBridge(
            catalog_product_id=product_id,
            item_ids=item_ids,
            observed_at=datetime.now(),
        )
