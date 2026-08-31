import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from src.domain.action.models import ActionRecord, ActionStatus
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.infrastructure.persistence.data.json.action_repository import (
    JsonActionRepository,
    InvalidActionDataError,
)
from src.application.action.action_service import ActionMemoryService


def test_action_record_creation_and_immutability():
    rec = ActionRecord(
        action_id="act-1",
        decision_id="dec-1",
        mission_id="miss-1",
        action_type="PUBLISH_LISTING",
        parameters={"title": "Test Product", "price": 100},
    )
    assert rec.action_id == "act-1"
    assert rec.status == ActionStatus.PENDING
    assert rec.parameters["title"] == "Test Product"

    with pytest.raises(Exception):
        rec.action_id = "act-2"


def test_json_action_repository_crud_and_linkage():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "actions.json"
        repo = JsonActionRepository(file_path)

        rec = ActionRecord(
            action_id="act-100",
            decision_id="dec-100",
            mission_id="miss-100",
            action_type="ADJUST_PRICE",
            target_resource="ML-123456",
            parameters={"new_price": 5000, "token": "SECRET_BEARER"},
            policy_reference="pol-pricing-v1",
            approval_reference="appr-999",
        )

        repo.save(rec)
        assert repo.exists("act-100") is True

        retrieved = repo.get_by_id("act-100")
        assert retrieved is not None
        assert retrieved.action_id == "act-100"
        assert retrieved.decision_id == "dec-100"
        assert retrieved.mission_id == "miss-100"
        assert retrieved.policy_reference == "pol-pricing-v1"
        assert retrieved.approval_reference == "appr-999"
        # Verify sensitive data exclusion
        assert "token" not in retrieved.parameters

        # Verify linkages
        by_dec = repo.get_by_decision_id("dec-100")
        assert len(by_dec) == 1
        assert by_dec[0].action_id == "act-100"

        by_miss = repo.get_by_mission_id("miss-100")
        assert len(by_miss) == 1
        assert by_miss[0].action_id == "act-100"


def test_action_memory_service_idempotency_and_update():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "actions.json"
        repo = JsonActionRepository(file_path)
        service = ActionMemoryService(repo)

        # 1. Create action
        act1 = service.record_action(
            action_id="act-200",
            decision_id="dec-200",
            mission_id="miss-200",
            action_type="REALLOCATE_INVENTORY",
            idempotency_key="idempotent-key-act-1",
        )
        assert act1.status == ActionStatus.PENDING

        # 2. Replay with same idempotency key
        act1_replay = service.record_action(
            action_id="act-200-duplicate",
            decision_id="dec-200",
            mission_id="miss-200",
            action_type="REALLOCATE_INVENTORY",
            idempotency_key="idempotent-key-act-1",
        )
        assert act1_replay.action_id == "act-200"

        # 3. Update status
        updated = service.update_action_status("act-200", ActionStatus.EXECUTING)
        assert updated.status == ActionStatus.EXECUTING
        assert updated.version == 2

        # 4. Reload from fresh repo instance (Restart simulation)
        fresh_repo = JsonActionRepository(file_path)
        reloaded = fresh_repo.get_by_id("act-200")
        assert reloaded is not None
        assert reloaded.status == ActionStatus.EXECUTING
        assert reloaded.version == 2


def test_corrupted_json_handling():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "corrupted.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("{ invalid json format }")

        repo = JsonActionRepository(file_path)
        with pytest.raises(InvalidActionDataError):
            repo.get_by_id("any-id")
