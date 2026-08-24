import pytest
from unittest.mock import MagicMock
from decimal import Decimal
from pathlib import Path
from src.application.use_cases.discover_market_opportunities import DiscoverMarketOpportunitiesUseCase
from src.domain.market_intelligence.models import SearchCriteria, Marketplace
from src.domain.market_intelligence.services import MarketAnalysisService
from src.infrastructure.market_intelligence.mercadolibre.adapter import MercadoLibreAdapter
from src.infrastructure.market_intelligence.mercadolibre.client import MercadoLibreClient
from src.infrastructure.persistence.data.json.market_snapshot_repository import JsonMarketSnapshotRepository

def test_market_intelligence_integration_flow(tmp_path):
    # 1. Setup Infrastructure
    mock_client = MagicMock(spec=MercadoLibreClient)
    adapter = MercadoLibreAdapter(mock_client)
    repository = JsonMarketSnapshotRepository(tmp_path / "data")
    
    # 2. Setup Domain Service
    analysis_service = MarketAnalysisService()
    
    # 3. Setup Application Use Case
    use_case = DiscoverMarketOpportunitiesUseCase(
        data_source=adapter,
        repository=repository,
        analysis_service=analysis_service
    )
    
    # 4. Mock API Response
    mock_client.search.return_value = {
        "results": [
            {
                "id": "MLC1",
                "title": "Cheap Product",
                "price": 50,
                "currency_id": "CLP",
                "sold_quantity": 150, # HIGH demand
                "available_quantity": 10,
                "seller": {"id": 101},
                "condition": "new",
                "shipping": {"free_shipping": True},
                "category_id": "CAT1"
            },
            {
                "id": "MLC2",
                "title": "Expensive Product",
                "price": 150,
                "currency_id": "CLP",
                "sold_quantity": 5, # NONE demand
                "available_quantity": 2,
                "seller": {"id": 102},
                "condition": "new",
                "shipping": {"free_shipping": False},
                "category_id": "CAT1"
            }
        ],
        "paging": {"total": 2}
    }
    
    # 5. Execute Use Case
    criteria = SearchCriteria(query="laptop", marketplace=Marketplace.MERCADO_LIBRE)
    opportunities = use_case.execute(criteria)
    
    # 6. Verify Results
    # Median price is (50 + 150) / 2 = 100
    # MLC1: price 50, ratio 0.5 (UNDER_MARKET), demand HIGH
    # MLC2: price 150, ratio 1.5 (OVER_MARKET), demand NONE
    
    assert len(opportunities) == 2
    
    opp1 = next(o for o in opportunities if o.listing.external_id == "MLC1")
    assert opp1.demand_signal.label == "HIGH"
    assert opp1.price_signal.position == "UNDER_MARKET"
    assert opp1.listing.shipping_info["free_shipping"] is True
    
    opp2 = next(o for o in opportunities if o.listing.external_id == "MLC2")
    assert opp2.demand_signal.label == "NONE"
    assert opp2.price_signal.position == "OVER_MARKET"
    
    # 7. Verify Persistence
    snapshots = list((tmp_path / "data").glob("*.json"))
    assert len(snapshots) == 1
