import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
from decimal import Decimal
import sys
import os

from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionResult
from src.domain.market_intelligence.models import VisitSignal

# Add mcp/commerce_lab to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../mcp/commerce_lab')))

# Mocking dependencies before importing server tools
with patch('src.application.oauth.dependencies.oauth_service', MagicMock()), \
     patch('src.application.oauth.dependencies.product_hunter_service', MagicMock()), \
     patch('src.infrastructure.persistence.data.json.profit_repository.JsonProfitDataRepository', MagicMock()), \
     patch('src.infrastructure.persistence.data.json.market_snapshot_repository.JsonMarketSnapshotRepository', MagicMock()), \
     patch('src.infrastructure.suppliers.json_supplier_data_source.JsonSupplierDataSource', MagicMock()):
    
    import server
    from server import (
        submit_discovery_mission, 
        get_mission_status, 
        get_item_evidence,
        discover_products,
        mission_repository,
        traffic_intelligence_service
    )

@pytest.fixture
def mock_oauth():
    with patch("server.oauth_service") as mock:
        conn = MagicMock()
        conn.access_token = "fake-token"
        mock.get_valid_connection.return_value = conn
        yield mock

@pytest.fixture
def mock_orchestrator():
    with patch("server.BasicMissionOrchestrator") as mock:
        yield mock

def test_submit_discovery_mission(mock_oauth, mock_orchestrator):
    response = submit_discovery_mission(query="test", user_id="user123")
    assert "Misión iniciada exitosamente" in response
    assert mock_orchestrator.return_value.submit.called

def test_get_mission_status_not_found():
    response = get_mission_status("non-existent")
    assert "Misión no encontrada" in response

def test_get_mission_status_found():
    mission = Mission.create(MissionType.MARKET_DISCOVERY, {"query": "test"})
    mission_repository.save(mission)
    
    response = get_mission_status(mission.mission_id)
    assert f"ESTADO DE MISIÓN — {mission.mission_id}" in response
    assert "PENDING" in response

def test_get_item_evidence(mock_oauth):
    with patch("server.traffic_intelligence_service") as mock_traffic:
        mock_traffic.get_visits.return_value = VisitSignal(
            item_id="item123",
            window="30d",
            total_visits=100,
            observed_days=30,
            coverage_ratio=1.0,
            source="ml",
            observed_at=datetime.utcnow()
        )
        
        response = get_item_evidence("item123", "user123")
        assert "ITEM EVIDENCE — item123" in response
        assert "Total Visitas: 100" in response

def test_discover_products_live_02(mock_oauth):
    with patch("server.UserScopedMarketplaceDataSource") as mock_ds:
        with patch("server.DiscoverMarketOpportunitiesUseCase") as mock_uc:
            mock_uc.return_value.execute.return_value = []
            
            response = discover_products(query="test", user_id="user123")
            assert "No se encontraron oportunidades" in response
            # Verify it used the user scoped data source (LIVE-02)
            assert mock_ds.called
