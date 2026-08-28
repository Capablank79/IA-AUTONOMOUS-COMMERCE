from datetime import datetime, timezone
import pytest

from src.domain.market_intelligence.models import Confidence, SignalType
from src.infrastructure.mercadolibre.reviews_data_source import MercadoLibreReviewsDataSource
from src.infrastructure.mercadolibre.api_client import MercadoLibreApiError

class FakeApiClient:
    def __init__(self, response_data, should_raise=False):
        self.response_data = response_data
        self.should_raise = should_raise
        self.path = None

    def get(self, path: str):
        self.path = path
        if self.should_raise:
            raise MercadoLibreApiError("API Error")
        return self.response_data

def test_get_reviews_valid_response():
    response = {
        "rating_average": 4.5,
        "paging": {
            "total": 1,
            "offset": 0,
            "limit": 50
        },
        "reviews": [
            {
                "id": "2650266166",
                "rate": 5,
                "content": "Excelente producto, muy potente.",
                "date_created": "2024-05-24T12:00:00.000-04:00",
                "reviewable_object": {"id": "MLC3439996320"},
                "secondary_key": "MLC47773363",
                "status": "active"
            }
        ]
    }
    
    client = FakeApiClient(response)
    source = MercadoLibreReviewsDataSource(client)
    
    signal = source.get_reviews("MLC2022490177", offset=0, limit=50)
    
    assert client.path == "/reviews/item/MLC2022490177?offset=0&limit=50"
    assert signal.item_id == "MLC2022490177"
    assert signal.total_reviews == 1
    assert signal.average_rating == 4.5
    assert len(signal.reviews) == 1
    
    review = signal.reviews[0]
    assert review.external_id == "2650266166"
    assert review.rating == 5
    assert review.text == "Excelente producto, muy potente."
    assert review.reviewable_object == "MLC3439996320"
    assert review.secondary_key == "MLC47773363"
    assert review.date.year == 2024
    assert signal.confidence == Confidence.HIGH
    assert signal.signal_type == SignalType.OBSERVED

def test_get_reviews_empty_response():
    response = {
        "rating_average": 0,
        "paging": {
            "total": 0,
            "offset": 0,
            "limit": 50
        },
        "reviews": []
    }
    
    client = FakeApiClient(response)
    source = MercadoLibreReviewsDataSource(client)
    
    signal = source.get_reviews("MLC000", offset=0, limit=50)
    
    assert signal.total_reviews == 0
    assert len(signal.reviews) == 0
    assert signal.confidence == Confidence.UNKNOWN

def test_get_reviews_propagates_api_error():
    client = FakeApiClient({}, should_raise=True)
    source = MercadoLibreReviewsDataSource(client)
    
    with pytest.raises(MercadoLibreApiError):
        source.get_reviews("MLC123", offset=0, limit=50)
