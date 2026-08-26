from urllib.parse import urlencode

from src.domain.market_intelligence.models import (
    CatalogProduct,
    Marketplace,
)
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient


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
