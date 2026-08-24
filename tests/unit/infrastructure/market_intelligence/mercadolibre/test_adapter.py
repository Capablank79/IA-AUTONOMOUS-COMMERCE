import pytest
from unittest.mock import MagicMock
from src.infrastructure.market_intelligence.mercadolibre.adapter import MercadoLibreAdapter
from src.domain.market_intelligence.models import SearchCriteria, Marketplace

def test_adapter_fetch_snapshot():
    client = MagicMock()
    adapter = MercadoLibreAdapter(client)
    
    client.search.return_value = {
        "results": [
            {
                "id": "MLC1",
                "title": "T1",
                "price": 100,
                "currency_id": "CLP",
                "seller": {"id": 1},
                "condition": "new"
            }
        ],
        "paging": {"total": 100}
    }
    
    criteria = SearchCriteria(query="test", marketplace=Marketplace.MERCADO_LIBRE)
    snapshot = adapter.fetch_snapshot(criteria)
    
    assert snapshot.marketplace == Marketplace.MERCADO_LIBRE
    assert len(snapshot.listings) == 1
    assert snapshot.total_results == 100
    assert snapshot.listings[0].external_id == "MLC1"

def test_adapter_error_handling():
    client = MagicMock()
    adapter = MercadoLibreAdapter(client)
    client.search.side_effect = Exception("Network error")
    
    with pytest.raises(RuntimeError, match="MercadoLibreAdapter failed to fetch snapshot"):
        adapter.fetch_snapshot(SearchCriteria(query="test", marketplace=Marketplace.MERCADO_LIBRE))
