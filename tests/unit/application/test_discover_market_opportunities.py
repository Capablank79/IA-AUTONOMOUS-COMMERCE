import pytest
from unittest.mock import MagicMock
from decimal import Decimal
from datetime import datetime
from src.application.use_cases.discover_market_opportunities import DiscoverMarketOpportunitiesUseCase
from src.domain.market_intelligence.models import (
    SearchCriteria,
    MarketSnapshot,
    Marketplace
)
from src.domain.market_intelligence.models import SearchCriteria, Marketplace
from src.domain.market_intelligence.services import MarketAnalysisService

def test_discover_market_opportunities_flow():
    # Arrange
    data_source = MagicMock()
    repository = MagicMock()
    analysis_service = MarketAnalysisService()
    
    use_case = DiscoverMarketOpportunitiesUseCase(
        data_source=data_source,
        repository=repository,
        analysis_service=analysis_service
    )
    
    criteria = SearchCriteria(query="test", marketplace=Marketplace.MERCADO_LIBRE)
    snapshot = MarketSnapshot(
        snapshot_id="snap-123",
        timestamp=datetime.utcnow(),
        search_criteria=criteria,
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[],
        total_results=0
    )
    data_source.fetch_snapshot.return_value = snapshot
    
    # Act
    opportunities = use_case.execute(criteria)
    
    # Assert
    data_source.fetch_snapshot.assert_called_once_with(criteria)
    repository.save.assert_called_once_with(snapshot)
    assert opportunities == []

def test_discover_market_opportunities_error_propagation():
    data_source = MagicMock()
    repository = MagicMock()
    analysis_service = MagicMock()
    
    use_case = DiscoverMarketOpportunitiesUseCase(data_source, repository, analysis_service)
    
    data_source.fetch_snapshot.side_effect = RuntimeError("API Down")
    
    with pytest.raises(RuntimeError, match="API Down"):
        use_case.execute(SearchCriteria(query="test", marketplace=Marketplace.MERCADO_LIBRE))
