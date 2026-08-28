import pytest
from src.domain.mission.models import Mission, MissionType, MissionStatus, MissionResult
from src.infrastructure.mission.repository import InMemoryMissionRepository

def test_save_and_get_mission():
    repo = InMemoryMissionRepository()
    mission = Mission.create(MissionType.MARKET_DISCOVERY, {"query": "test"})
    
    repo.save(mission)
    saved = repo.get_by_id(mission.mission_id)
    
    assert saved == mission
    assert saved.mission_id == mission.mission_id

def test_save_and_get_result():
    repo = InMemoryMissionRepository()
    mission_id = "test-id"
    result = MissionResult(
        mission_id=mission_id,
        status=MissionStatus.COMPLETED,
        output={"key": "value"}
    )
    
    repo.save_result(result)
    saved_result = repo.get_result(mission_id)
    
    assert saved_result == result
    assert saved_result.output["key"] == "value"

def test_get_non_existent_mission():
    repo = InMemoryMissionRepository()
    assert repo.get_by_id("non-existent") is None

def test_get_non_existent_result():
    repo = InMemoryMissionRepository()
    assert repo.get_result("non-existent") is None
