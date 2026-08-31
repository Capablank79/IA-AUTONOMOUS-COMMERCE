import pytest
import tempfile
from pathlib import Path

from src.domain.result.models import ActionResultRecord, ResultOutcome
from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.infrastructure.persistence.data.json.result_repository import (
    JsonResultRepository,
    InvalidResultDataError,
)
from src.application.result.result_service import ResultMemoryService


def test_action_result_record_immutability():
    res = ActionResultRecord(
        result_id="res-1",
        action_id="act-1",
        decision_id="dec-1",
        mission_id="miss-1",
        outcome=ResultOutcome.SUCCESS,
        response_summary={"status_code": 200, "item_id": "ML-100"},
    )
    assert res.result_id == "res-1"
    assert res.outcome == ResultOutcome.SUCCESS
    assert res.response_summary["item_id"] == "ML-100"

    with pytest.raises(Exception):
        res.result_id = "res-2"


def test_json_result_repository_crud_linkage_and_security():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "results.json"
        repo = JsonResultRepository(file_path)

        res = ActionResultRecord(
            result_id="res-100",
            action_id="act-100",
            decision_id="dec-100",
            mission_id="miss-100",
            outcome=ResultOutcome.SUCCESS,
            response_summary={"http_code": 201, "api_key": "SENSITIVE_KEY_VALUE"},
            evidence_reference="evid-999",
            confidence=Confidence.HIGH,
        )

        repo.save(res)
        assert repo.exists("res-100") is True

        retrieved = repo.get_by_id("res-100")
        assert retrieved is not None
        assert retrieved.result_id == "res-100"
        assert retrieved.evidence_reference == "evid-999"

        # Verify sensitive data exclusion
        assert "api_key" not in retrieved.response_summary

        # Check linkages
        assert repo.get_by_action_id("act-100").result_id == "res-100"
        assert len(repo.get_by_decision_id("dec-100")) == 1
        assert len(repo.get_by_mission_id("miss-100")) == 1


def test_result_memory_service_idempotency_and_unknown():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "results.json"
        repo = JsonResultRepository(file_path)
        service = ResultMemoryService(repo)

        # Record SUCCESS
        res1 = service.record_result(
            result_id="res-200",
            action_id="act-200",
            decision_id="dec-200",
            mission_id="miss-200",
            outcome=ResultOutcome.SUCCESS,
            idempotency_key="idemp-res-200",
        )
        assert res1.outcome == ResultOutcome.SUCCESS

        # Replay duplicate
        res1_replay = service.record_result(
            result_id="res-200-dup",
            action_id="act-200",
            decision_id="dec-200",
            mission_id="miss-200",
            outcome=ResultOutcome.FAILURE,
            idempotency_key="idemp-res-200",
        )
        assert res1_replay.result_id == "res-200"

        # Record UNKNOWN outcome
        res_unk = service.record_result(
            result_id="res-300",
            action_id="act-300",
            decision_id="dec-300",
            mission_id="miss-300",
            outcome=ResultOutcome.UNKNOWN,
            error_message="External API timeout 504",
        )
        assert res_unk.outcome == ResultOutcome.UNKNOWN
        assert res_unk.error_message == "External API timeout 504"


def test_corrupted_json_result_handling():
    with tempfile.TemporaryDirectory() as tmpdir:
        file_path = Path(tmpdir) / "corrupt_res.json"
        with open(file_path, "w", encoding="utf-8") as f:
            f.write("{ bad json format }")

        repo = JsonResultRepository(file_path)
        with pytest.raises(InvalidResultDataError):
            repo.get_by_id("res-1")
