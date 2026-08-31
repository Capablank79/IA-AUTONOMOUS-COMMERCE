import json
import pytest
from datetime import datetime
from decimal import Decimal

from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionPriority,
    MissionStatus,
    MissionResult,
    MissionTraceEntry,
)
from src.infrastructure.persistence.data.json.mission_repository import (
    JsonMissionRepository,
    InvalidMissionDataError,
)


@pytest.fixture
def temp_storage_dir(tmp_path):
    return tmp_path / "mission_storage"


def test_save_and_get_mission_round_trip(temp_storage_dir):
    repo = JsonMissionRepository(temp_storage_dir)

    mission = Mission.create(
        mission_type=MissionType.MARKET_DISCOVERY,
        parameters={
            "query": "auriculares bluetooth",
            "correlation_id": "corr-12345",
            "idempotency_key": "idemp-67890",
            "provenance": {"source": "unit_test"},
            "confidence": 0.95,
        },
        priority=MissionPriority.HIGH,
    )

    repo.save(mission)

    loaded_mission = repo.get_by_id(mission.mission_id)

    assert loaded_mission is not None
    assert loaded_mission.mission_id == mission.mission_id
    assert loaded_mission.type == MissionType.MARKET_DISCOVERY
    assert loaded_mission.priority == MissionPriority.HIGH
    assert loaded_mission.status == MissionStatus.PENDING
    assert loaded_mission.parameters["query"] == "auriculares bluetooth"
    assert loaded_mission.parameters["correlation_id"] == "corr-12345"
    assert loaded_mission.parameters["idempotency_key"] == "idemp-67890"
    assert loaded_mission.parameters["confidence"] == 0.95
    assert loaded_mission.created_at.isoformat() == mission.created_at.isoformat()


def test_update_mission_state(temp_storage_dir):
    repo = JsonMissionRepository(temp_storage_dir)

    mission = Mission.create(
        mission_type=MissionType.SUPPLIER_DISCOVERY,
        parameters={"category": "electronics"},
    )
    repo.save(mission)

    updated_mission = Mission(
        mission_id=mission.mission_id,
        type=mission.type,
        priority=mission.priority,
        status=MissionStatus.RUNNING,
        parameters=mission.parameters,
        created_at=mission.created_at,
        updated_at=datetime.utcnow(),
    )
    repo.save(updated_mission)

    loaded = repo.get_by_id(mission.mission_id)
    assert loaded is not None
    assert loaded.status == MissionStatus.RUNNING


def test_get_nonexistent_mission(temp_storage_dir):
    repo = JsonMissionRepository(temp_storage_dir)
    loaded = repo.get_by_id("non-existent-id")
    assert loaded is None


def test_save_and_get_mission_result(temp_storage_dir):
    repo = JsonMissionRepository(temp_storage_dir)

    mission_id = "m-999"
    trace_entry = MissionTraceEntry(
        step="DISCOVERY_STEP",
        status=MissionStatus.RUNNING,
        metadata={"items_found": 10},
    )
    result = MissionResult(
        mission_id=mission_id,
        status=MissionStatus.COMPLETED,
        output={"summary": "Success", "best_opportunity_id": "opp-1"},
        trace=[trace_entry],
        evidences=[{"evidence_id": "ev-1", "score": 0.88}],
        blocks=[],
        errors=[],
    )

    repo.save_result(result)

    loaded_result = repo.get_result(mission_id)
    assert loaded_result is not None
    assert loaded_result.mission_id == mission_id
    assert loaded_result.status == MissionStatus.COMPLETED
    assert loaded_result.output["summary"] == "Success"
    assert len(loaded_result.trace) == 1
    assert loaded_result.trace[0].step == "DISCOVERY_STEP"
    assert loaded_result.trace[0].metadata["items_found"] == 10


def test_idempotency_save_repeated(temp_storage_dir):
    repo = JsonMissionRepository(temp_storage_dir)
    mission = Mission.create(
        mission_type=MissionType.PROFIT_EVALUATION,
        parameters={"idempotency_key": "unique-key-123"},
    )

    repo.save(mission)
    repo.save(mission)

    loaded = repo.get_by_id(mission.mission_id)
    assert loaded is not None
    assert loaded.mission_id == mission.mission_id


def test_corrupted_json_file(temp_storage_dir):
    repo = JsonMissionRepository(temp_storage_dir)
    file_path = temp_storage_dir / "missions" / "bad-id.json"
    file_path.write_text("{invalid_json: true", encoding="utf-8")

    with pytest.raises(InvalidMissionDataError):
        repo.get_by_id("bad-id")


def test_sensitive_data_exclusion(temp_storage_dir):
    repo = JsonMissionRepository(temp_storage_dir)
    mission = Mission.create(
        mission_type=MissionType.MARKET_DISCOVERY,
        parameters={"safe_param": "value123"},
    )
    repo.save(mission)

    file_path = temp_storage_dir / "missions" / f"{mission.mission_id}.json"
    content = file_path.read_text(encoding="utf-8")
    assert "pan" not in content.lower()
    assert "cvv" not in content.lower()
    assert "token" not in content.lower()
