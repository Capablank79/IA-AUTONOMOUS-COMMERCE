from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient


class MercadoLibreTrendsDataSource:
    """Data source for Mercado Libre market trends."""

    SITE_ID = "MLC"

    def __init__(self, api_client: MercadoLibreApiClient):
        self.api_client = api_client

    def get_trends(self) -> list[dict]:
        data = self.api_client.get(f"/trends/{self.SITE_ID}")

        if not isinstance(data, list):
            raise ValueError("Mercado Libre trends response must be a list")

        return [
            {
                "keyword": item["keyword"],
                "url": item.get("url"),
                "rank": index,
            }
            for index, item in enumerate(data, start=1)
            if item.get("keyword")
        ]
