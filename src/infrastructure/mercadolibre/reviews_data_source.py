from datetime import datetime, timezone
from typing import Any, Dict, List

from src.domain.market_intelligence.models import Confidence, Review, ReviewSignal, SignalType
from src.domain.market_intelligence.ports import ReviewsDataSource
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiClient


class MercadoLibreReviewsDataSource(ReviewsDataSource):
    """
    Infrastructure adapter to fetch reviews data from Mercado Libre.
    """

    def __init__(self, api_client: MercadoLibreApiClient):
        self.api_client = api_client

    def get_reviews(self, item_id: str, offset: int = 0, limit: int = 50) -> ReviewSignal:
        path = f"/reviews/item/{item_id}?offset={offset}&limit={limit}"
        data = self.api_client.get(path)
        
        now = datetime.now(timezone.utc)
        
        raw_reviews = data.get("reviews", [])
        reviews = []
        for r in raw_reviews:
            # Date format example: "2024-05-24T12:00:00.000-04:00"
            date_str = r.get("date_created")
            date = now
            if date_str:
                try:
                    # fromisoformat handles most ISO 8601 strings
                    date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    pass

            reviews.append(Review(
                external_id=str(r.get("id")),
                rating=int(r.get("rate", 0)),
                text=r.get("content", ""),
                date=date,
                reviewable_object=str(r.get("reviewable_object", {}).get("id") if isinstance(r.get("reviewable_object"), dict) else r.get("reviewable_object")),
                secondary_key=r.get("secondary_key"),
                status=r.get("status", "active")
            ))

        rating_average = float(data.get("rating_average", 0))
        paging = data.get("paging", {})
        total_reviews = int(paging.get("total", 0))

        return ReviewSignal(
            item_id=item_id,
            total_reviews=total_reviews,
            average_rating=rating_average,
            reviews=reviews,
            paging=paging,
            observed_at=now,
            confidence=Confidence.HIGH if reviews else Confidence.UNKNOWN,
            signal_type=SignalType.OBSERVED
        )
