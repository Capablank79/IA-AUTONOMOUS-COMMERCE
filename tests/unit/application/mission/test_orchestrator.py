import pytest
from unittest.mock import MagicMock
from datetime import datetime
from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionResult
from src.application.mission.orchestrator import BasicMissionOrchestrator
from src.infrastructure.mission.repository import InMemoryMissionRepository
from src.domain.market_intelligence.models import (
    MarketSnapshot, SearchCriteria, Marketplace, MarketListing, 
    Money, MarketOpportunity, TrendSignal, DemandSignal, PriceSignal,
    VisitSignal, Confidence
)
from decimal import Decimal

@pytest.fixture
def repository():
    return InMemoryMissionRepository()

@pytest.fixture
def mock_product_hunter():
    return MagicMock()

@pytest.fixture
def mock_data_source():
    return MagicMock()

@pytest.fixture
def mock_traffic_intelligence():
    return MagicMock()

@pytest.fixture
def mock_supplier_source():
    return MagicMock()

@pytest.fixture
def mock_profit_repository():
    return MagicMock()

@pytest.fixture
def mock_profit_engine():
    return MagicMock()

@pytest.fixture
def mock_opportunity_engine():
    return MagicMock()

@pytest.fixture
def orchestrator(repository, mock_product_hunter, mock_data_source, 
                 mock_traffic_intelligence, mock_supplier_source, 
                 mock_profit_repository, mock_profit_engine, mock_opportunity_engine):
    return BasicMissionOrchestrator(
        repository=repository,
        product_hunter=mock_product_hunter,
        market_data_source=mock_data_source,
        traffic_intelligence=mock_traffic_intelligence,
        supplier_source=mock_supplier_source,
        profit_repository=mock_profit_repository,
        profit_engine=mock_profit_engine,
        opportunity_engine=mock_opportunity_engine
    )

def test_submit_mission_success(orchestrator, repository, mock_data_source, mock_opportunity_engine):
    # Setup mocks
    query = "smartphone"
    mission = Mission.create(
        MissionType.MARKET_DISCOVERY,
        {"query": query, "user_id": "user123"}
    )
    
    mock_snapshot = MarketSnapshot(
        snapshot_id="snap-123",
        timestamp=datetime.utcnow(),
        search_criteria=SearchCriteria(query=query, marketplace=Marketplace.MERCADO_LIBRE),
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[],
        total_results=0
    )
    mock_data_source.fetch_snapshot.return_value = mock_snapshot
    
    # Execute
    orchestrator.submit(mission)
    
    # Verify state transitions
    saved_mission = repository.get_by_id(mission.mission_id)
    assert saved_mission.status == MissionStatus.COMPLETED
    
    # Verify result
    result = repository.get_result(mission.mission_id)
    assert result is not None
    assert result.status == MissionStatus.COMPLETED
    assert "snapshot_id" in result.output
    assert result.output["snapshot_id"] == "snap-123"

def test_mission_failed_on_exception(orchestrator, repository, mock_data_source):
    # Setup mock to raise exception
    mission = Mission.create(
        MissionType.MARKET_DISCOVERY,
        {"query": "fail", "user_id": "user123"}
    )
    mock_data_source.fetch_snapshot.side_effect = Exception("Connection error")
    
    # Execute
    orchestrator.submit(mission)
    
    # Verify status is FAILED
    saved_mission = repository.get_by_id(mission.mission_id)
    assert saved_mission.status == MissionStatus.FAILED
    
    # Verify result contains error
    result = repository.get_result(mission.mission_id)
    assert result.status == MissionStatus.FAILED
    assert "Connection error" in result.errors[0]

def test_market_discovery_sequence(orchestrator, mock_product_hunter, mock_data_source, 
                                 mock_traffic_intelligence, mock_opportunity_engine):
    from src.domain.opportunity.models import OpportunityDecision, OpportunityReadiness
    
    query = "camera"
    mission = Mission.create(
        MissionType.MARKET_DISCOVERY,
        {"query": query, "user_id": "user123", "limit": 5}
    )
    
    # Setup mocks for full sequence
    mock_product_hunter.search.return_value = [MagicMock(), MagicMock()]
    
    listing = MarketListing(
            external_id="MLA1",
            marketplace=Marketplace.MERCADO_LIBRE,
            title="Test Cam",
            price=Money(amount=Decimal("100"), currency="ARS"),
            sold_quantity=10,
            available_quantity=5,
            seller_id="S1",
            condition="new",
            shipping_info={},
            category="Cam"
        )
    
    snapshot = MarketSnapshot(
        snapshot_id="snap-1",
        timestamp=datetime.utcnow(),
        search_criteria=SearchCriteria(query=query, marketplace=Marketplace.MERCADO_LIBRE, limit=5),
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing],
        total_results=1
    )
    mock_data_source.fetch_snapshot.return_value = snapshot
    mock_traffic_intelligence.get_visits.return_value = VisitSignal(
        item_id="MLA1",
        window="30d",
        total_visits=100,
        observed_days=30,
        coverage_ratio=1.0,
        source="ml",
        observed_at=datetime.utcnow()
    )
    
    mock_opportunity_engine.evaluate.return_value = OpportunityDecision(
        evidence=MagicMock(),
        readiness=OpportunityReadiness.SUFFICIENT_EVIDENCE,
        reasons=["Observed positive traffic"],
        opportunity_score=None,
        confidence=Confidence.HIGH
    )
    
    # Execute
    orchestrator.submit(mission)
    
    # Verify calls
    mock_product_hunter.search.assert_called_once()
    mock_data_source.fetch_snapshot.assert_called_once()
    mock_traffic_intelligence.get_visits.assert_called_once()
    mock_opportunity_engine.evaluate.assert_called_once()
    
    # Verify result output
    result = orchestrator.get_result(mission.mission_id)
    assert result.output["catalog_products_found"] == 2
    assert result.output["listings_found"] == 1
    assert result.output["results"][0]["listing_id"] == "MLA1"
    assert result.output["results"][0]["readiness"] == "SUFFICIENT_EVIDENCE"
    assert "Observed positive traffic" in result.output["results"][0]["reasons"]
