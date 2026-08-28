from datetime import datetime, timezone, timedelta
import pytest

from src.domain.market_intelligence.models import Confidence
from src.infrastructure.mercadolibre.visits_data_source import MercadoLibreVisitsDataSource
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


def test_get_visits_valid_response_with_visits():
    now = datetime.now(timezone.utc)
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    day_before = (now - timedelta(days=2)).strftime("%Y-%m-%dT00:00:00Z")
    
    response = {
        "item_id": "MLC123",
        "date_from": day_before,
        "date_to": yesterday,
        "total_visits": 30,
        "results": [
            {"date": day_before, "visits": 10},
            {"date": yesterday, "visits": 20}
        ]
    }
    
    client = FakeApiClient(response)
    source = MercadoLibreVisitsDataSource(client)
    
    signal = source.get_visits("MLC123", 2)
    
    assert client.path == "/items/MLC123/visits/time_window?last=2&unit=day"
    assert signal.item_id == "MLC123"
    assert signal.window == "2d"
    assert signal.total_visits == 30
    assert signal.observed_days == 2
    assert signal.coverage_ratio == 1.0
    assert signal.average_daily_visits == 15.0
    assert signal.confidence == Confidence.UNKNOWN
    assert signal.momentum is None
    assert signal.acceleration is None


def test_get_visits_with_incomplete_current_day():
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%dT12:00:00Z")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    
    response = {
        "total_visits": 25,
        "results": [
            {"date": yesterday_str, "visits": 20},
            {"date": today_str, "visits": 5} # Today is incomplete
        ]
    }
    
    client = FakeApiClient(response)
    source = MercadoLibreVisitsDataSource(client)
    
    signal = source.get_visits("MLC123", 2)
    
    assert signal.total_visits == 25
    assert signal.observed_days == 2
    # Average should exclude today's visits and today's day count
    # So valid_days = 1, valid_visits = 20 -> avg = 20.0
    assert signal.average_daily_visits == 20.0


def test_get_visits_with_zero_total_visits_and_no_results():
    response = {
        "total_visits": 0,
        "results": []
    }
    
    client = FakeApiClient(response)
    source = MercadoLibreVisitsDataSource(client)
    
    signal = source.get_visits("MLC123", 7)
    
    assert signal.total_visits == 0
    assert signal.observed_days == 0
    assert signal.coverage_ratio == 0.0
    assert signal.average_daily_visits is None


def test_get_visits_with_zero_total_visits_and_results():
    now = datetime.now(timezone.utc)
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    response = {
        "total_visits": 0,
        "results": [
            {"date": yesterday_str, "visits": 0}
        ]
    }
    
    client = FakeApiClient(response)
    source = MercadoLibreVisitsDataSource(client)
    
    signal = source.get_visits("MLC123", 1)
    
    assert signal.total_visits == 0
    assert signal.observed_days == 1
    assert signal.coverage_ratio == 1.0
    assert signal.average_daily_visits == 0.0


def test_get_visits_missing_total_visits():
    response = {
        "results": []
    }
    
    client = FakeApiClient(response)
    source = MercadoLibreVisitsDataSource(client)
    
    signal = source.get_visits("MLC123", 7)
    
    assert signal.total_visits is None
    assert signal.observed_days == 0
    assert signal.average_daily_visits is None


def test_get_visits_partial_results():
    now = datetime.now(timezone.utc)
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
    
    response = {
        "total_visits": 10,
        "results": [
            {"date": yesterday_str, "visits": 10}
        ]
    }
    
    client = FakeApiClient(response)
    source = MercadoLibreVisitsDataSource(client)
    
    signal = source.get_visits("MLC123", 7)
    
    assert signal.total_visits == 10
    assert signal.observed_days == 1
    assert signal.coverage_ratio == 1 / 7
    assert signal.average_daily_visits == 10.0


def test_get_visits_propagates_api_error():
    client = FakeApiClient({}, should_raise=True)
    source = MercadoLibreVisitsDataSource(client)
    
    with pytest.raises(MercadoLibreApiError):
        source.get_visits("MLC123", 7)
