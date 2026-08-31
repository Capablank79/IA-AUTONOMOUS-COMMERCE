import pytest
from datetime import datetime
from unittest.mock import MagicMock
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionPriority,
    MissionStatus,
)
from src.infrastructure.persistence.data.json.mission_repository import (
    JsonMissionRepository,
)
from src.application.mission.orchestrator import BasicMissionOrchestrator


def test_h1_mission_persistence_lifecycle_and_restart(tmp_path):
    storage_dir = tmp_path / "h1_missions_persistence"

    # Step 1: Instantiate initial repository and orchestrator
    repo_phase1 = JsonMissionRepository(storage_dir)
    mock_data_source = MagicMock()
    mock_snapshot = MagicMock()
    mock_snapshot.snapshot_id = "snap-h1-test"
    mock_snapshot.listings = []
    mock_data_source.fetch_snapshot.return_value = mock_snapshot

    orchestrator_phase1 = BasicMissionOrchestrator(
        repository=repo_phase1,
        market_data_source=mock_data_source,
    )

    # Step 2: Create Mission with full tracing & correlation metadata
    mission = Mission.create(
        mission_type=MissionType.MARKET_DISCOVERY,
        parameters={
            "query": "auriculares bluetooth",
            "limit": 5,
            "correlation_id": "corr-h1-001",
            "idempotency_key": "idemp-h1-001",
            "provenance": {"origin": "h1_integration_test"},
            "confidence": 0.95,
        },
        priority=MissionPriority.HIGH,
    )

    # Save initial state PENDING
    repo_phase1.save(mission)

    # Verify persistent storage on disk
    persisted_initial = repo_phase1.get_by_id(mission.mission_id)
    assert persisted_initial is not None
    assert persisted_initial.status == MissionStatus.PENDING
    assert persisted_initial.parameters["correlation_id"] == "corr-h1-001"

    # Step 3: Run execution via Orchestrator (PENDING -> RUNNING -> COMPLETED)
    orchestrator_phase1._execute(mission.mission_id)

    # Verify updated state COMPLETED & MissionResult saved
    completed_mission = repo_phase1.get_by_id(mission.mission_id)
    assert completed_mission is not None
    assert completed_mission.status == MissionStatus.COMPLETED

    result_phase1 = repo_phase1.get_result(mission.mission_id)
    assert result_phase1 is not None
    assert result_phase1.status == MissionStatus.COMPLETED
    assert "snapshot_id" in result_phase1.output

    # Step 4: RESTART / RESUME Simulation
    # Re-instantiate repository and orchestrator from the exact same storage directory (simulating app restart)
    repo_phase2 = JsonMissionRepository(storage_dir)
    orchestrator_phase2 = BasicMissionOrchestrator(
        repository=repo_phase2,
        market_data_source=mock_data_source,
    )

    # Load mission after restart
    reloaded_mission = repo_phase2.get_by_id(mission.mission_id)
    reloaded_result = repo_phase2.get_result(mission.mission_id)

    assert reloaded_mission is not None
    assert reloaded_mission.mission_id == mission.mission_id
    assert reloaded_mission.type == MissionType.MARKET_DISCOVERY
    assert reloaded_mission.status == MissionStatus.COMPLETED
    assert reloaded_mission.parameters["correlation_id"] == "corr-h1-001"
    assert reloaded_mission.parameters["idempotency_key"] == "idemp-h1-001"

    assert reloaded_result is not None
    assert reloaded_result.mission_id == mission.mission_id
    assert reloaded_result.status == MissionStatus.COMPLETED
    assert len(reloaded_result.trace) > 0

    # Step 5: Verify Idempotency on update/save
    repo_phase2.save(reloaded_mission)
    reloaded_again = repo_phase2.get_by_id(mission.mission_id)
    assert reloaded_again is not None
    assert reloaded_again.mission_id == mission.mission_id
