import pytest
from datetime import datetime
from src.domain.mission.models import Mission, MissionResult, MissionStatus, MissionType, MissionPriority

def test_mission_creation():
    parameters = {"query": "laptop", "max_price": 1000}
    mission = Mission.create(
        mission_type=MissionType.MARKET_DISCOVERY,
        parameters=parameters,
        priority=MissionPriority.HIGH
    )
    
    assert mission.mission_id is not None
    assert mission.type == MissionType.MARKET_DISCOVERY
    assert mission.parameters == parameters
    assert mission.priority == MissionPriority.HIGH
    assert mission.status == MissionStatus.PENDING
    assert isinstance(mission.created_at, datetime)
    assert isinstance(mission.updated_at, datetime)

def test_mission_result_initialization():
    mission_id = "test-id"
    output = {"found_items": 10}
    errors = ["Timeout connecting to marketplace"]
    
    result = MissionResult(
        mission_id=mission_id,
        status=MissionStatus.FAILED,
        output=output,
        errors=errors
    )
    
    assert result.mission_id == mission_id
    assert result.status == MissionStatus.FAILED
    assert result.output == output
    assert result.errors == errors
    assert isinstance(result.finished_at, datetime)

def test_mission_immutability():
    mission = Mission.create(MissionType.SUPPLIER_SEARCH, {})
    with pytest.raises(AttributeError):
        mission.status = MissionStatus.RUNNING  # Mission is frozen
