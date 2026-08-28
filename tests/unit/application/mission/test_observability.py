import pytest
from unittest.mock import MagicMock
from datetime import datetime
from decimal import Decimal
from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionResult, MissionTraceEntry
from src.application.mission.orchestrator import BasicMissionOrchestrator
from src.infrastructure.mission.repository import InMemoryMissionRepository
from src.domain.market_intelligence.models import (
    MarketSnapshot, SearchCriteria, Marketplace, MarketListing, 
    Money, VisitSignal, Confidence, MarketEvidence
)
from src.domain.opportunity.models import OpportunityDecision, OpportunityReadiness

@pytest.fixture
def repository():
    return InMemoryMissionRepository()

@pytest.fixture
def mock_data_source():
    return MagicMock()

@pytest.fixture
def mock_opportunity_engine():
    return MagicMock()

@pytest.fixture
def mock_traffic_intelligence():
    return MagicMock()

@pytest.fixture
def orchestrator(repository, mock_data_source, mock_opportunity_engine, mock_traffic_intelligence):
    return BasicMissionOrchestrator(
        repository=repository,
        market_data_source=mock_data_source,
        opportunity_engine=mock_opportunity_engine,
        traffic_intelligence=mock_traffic_intelligence
    )

def test_mission_observability_trace_and_evidence(orchestrator, repository, mock_data_source, mock_opportunity_engine, mock_traffic_intelligence):
    # Setup
    query = "test query"
    user_id = "user1"
    mission = Mission.create(
        MissionType.MARKET_DISCOVERY,
        {"query": query, "user_id": user_id}
    )
    
    listing = MarketListing(
        external_id="EXT1",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Item",
        price=Money(amount=Decimal("100"), currency="USD"),
        sold_quantity=5,
        available_quantity=10,
        seller_id="S1",
        condition="new",
        shipping_info={},
        category="Test"
    )
    
    snapshot = MarketSnapshot(
        snapshot_id="snap-1",
        timestamp=datetime.utcnow(),
        search_criteria=SearchCriteria(query=query, marketplace=Marketplace.MERCADO_LIBRE),
        marketplace=Marketplace.MERCADO_LIBRE,
        listings=[listing],
        total_results=1
    )
    mock_data_source.fetch_snapshot.return_value = snapshot

    # Mock high confidence visit signal
    mock_traffic_intelligence.get_visits.return_value = VisitSignal(
        item_id="EXT1",
        window="30d",
        total_visits=100,
        observed_days=30,
        coverage_ratio=1.0,
        source="test",
        observed_at=datetime.utcnow(),
        confidence=Confidence.HIGH
    )
    
    mock_opportunity_engine.evaluate.return_value = OpportunityDecision(
        evidence=MagicMock(),
        readiness=OpportunityReadiness.SUFFICIENT_EVIDENCE,
        reasons=["Reason 1"],
        opportunity_score=Decimal("0.8"),
        confidence=Confidence.HIGH
    )
    
    # Execute
    orchestrator.submit(mission)
    
    # Verify result
    result = orchestrator.get_result(mission.mission_id)
    assert result is not None
    assert result.status == MissionStatus.COMPLETED
    
    # Verify Trace
    assert len(result.trace) >= 3
    assert result.trace[0].step == "INIT_MARKET_DISCOVERY"
    assert result.trace[1].step == "MARKET_SNAPSHOT"
    assert result.trace[-1].step == "OPPORTUNITY_EVALUATION"
    assert all(isinstance(t, MissionTraceEntry) for t in result.trace)
    
    # Verify Evidences
    assert len(result.evidences) == 1
    assert isinstance(result.evidences[0], MarketEvidence)
    assert result.evidences[0].listing.external_id == "EXT1"
    
    # Verify Sufficient Evidence Distinction
    assert result.output["results"][0]["sufficient_evidence"] is True

def test_mission_blocked_status(repository):
    # Orchestrator without market_data_source should be BLOCKED for discovery
    orchestrator = BasicMissionOrchestrator(repository=repository)
    
    mission = Mission.create(
        MissionType.MARKET_DISCOVERY,
        {"query": "blocked test", "user_id": "user1"}
    )
    
    orchestrator.submit(mission)
    
    result = orchestrator.get_result(mission.mission_id)
    assert result.status == MissionStatus.BLOCKED
    assert len(result.blocks) > 0
    assert result.blocks[0]["step"] == "MARKET_SNAPSHOT"
    assert "MarketplaceDataSource es requerido" in result.blocks[0]["reason"]
    
    saved_mission = repository.get_by_id(mission.mission_id)
    assert saved_mission.status == MissionStatus.BLOCKED
