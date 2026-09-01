import pytest
from datetime import datetime, timezone

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.infrastructure.persistence.data.json.outcome_repository import (
    JsonOutcomeRepository,
    InvalidOutcomeDataError,
)
from src.application.outcome.outcome_service import OutcomeTrackingService


@pytest.fixture
def repo(tmp_path):
    json_path = tmp_path / "outcomes.json"
    return JsonOutcomeRepository(json_path)


@pytest.fixture
def service(repo):
    return OutcomeTrackingService(repo)


def test_create_and_save_outcome(repo):
    record = OutcomeRecord(
        outcome_id="out-001",
        mission_id="mission-001",
        decision_id="dec-001",
        action_id="act-001",
        result_id="res-001",
        outcome_type="SALES_OBSERVATION",
        status=OutcomeStatus.SUCCESS,
        value_metrics={"sales_count": 5, "revenue": 150.0},
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )
    repo.save(record)

    loaded = repo.get_by_id("out-001")
    assert loaded is not None
    assert loaded.outcome_id == "out-001"
    assert loaded.mission_id == "mission-001"
    assert loaded.decision_id == "dec-001"
    assert loaded.action_id == "act-001"
    assert loaded.result_id == "res-001"
    assert loaded.status == OutcomeStatus.SUCCESS
    assert loaded.value_metrics["sales_count"] == 5
    assert loaded.confidence == Confidence.HIGH
    assert loaded.provenance == EvidenceProvenanceType.LIVE


def test_causal_links_retrieval(repo):
    record = OutcomeRecord(
        outcome_id="out-002",
        mission_id="mission-100",
        decision_id="dec-200",
        action_id="act-300",
        result_id="res-400",
        status=OutcomeStatus.PARTIAL,
    )
    repo.save(record)

    by_action = repo.get_by_action_id("act-300")
    assert len(by_action) == 1
    assert by_action[0].outcome_id == "out-002"

    by_decision = repo.get_by_decision_id("dec-200")
    assert len(by_decision) == 1

    by_mission = repo.get_by_mission_id("mission-100")
    assert len(by_mission) == 1

    by_result = repo.get_by_result_id("res-400")
    assert len(by_result) == 1


def test_idempotency_service(service):
    rec1 = service.record_outcome(
        outcome_id="out-003",
        mission_id="m-1",
        decision_id="d-1",
        action_id="a-1",
        status=OutcomeStatus.SUCCESS,
        idempotency_key="idemp-key-123",
        value_metrics={"units": 10},
    )

    # Replay con misma idempotency key
    rec2 = service.record_outcome(
        outcome_id="out-003-dup",
        mission_id="m-1",
        decision_id="d-1",
        action_id="a-1",
        status=OutcomeStatus.FAILURE,
        idempotency_key="idemp-key-123",
        value_metrics={"units": 999},
    )

    assert rec1.outcome_id == rec2.outcome_id == "out-003"
    assert rec2.value_metrics["units"] == 10
    assert rec2.status == OutcomeStatus.SUCCESS


def test_unknown_status_preservation(service):
    rec = service.record_outcome(
        outcome_id="out-unknown-1",
        mission_id="m-unk",
        decision_id="d-unk",
        action_id="a-unk",
        status=OutcomeStatus.UNKNOWN,
        error_message="External API timeout reading post-action sales data",
        provenance=EvidenceProvenanceType.DERIVED,
    )
    assert rec.status == OutcomeStatus.UNKNOWN
    assert rec.provenance == EvidenceProvenanceType.DERIVED
    assert rec.error_message is not None


def test_sensitive_data_exclusion(repo):
    record = OutcomeRecord(
        outcome_id="out-sec-1",
        mission_id="m-sec",
        decision_id="d-sec",
        action_id="a-sec",
        status=OutcomeStatus.SUCCESS,
        value_metrics={
            "revenue": 500,
            "api_key": "SUPER_SECRET_KEY",
            "password": "my-password",
        },
    )
    repo.save(record)

    loaded = repo.get_by_id("out-sec-1")
    assert "revenue" in loaded.value_metrics
    assert "api_key" not in loaded.value_metrics
    assert "password" not in loaded.value_metrics


def test_restart_reload_persistence(tmp_path):
    json_path = tmp_path / "outcomes_restart.json"
    repo1 = JsonOutcomeRepository(json_path)
    service1 = OutcomeTrackingService(repo1)

    rec = service1.record_outcome(
        outcome_id="out-restart",
        mission_id="m-restart",
        decision_id="d-restart",
        action_id="a-restart",
        result_id="r-restart",
        status=OutcomeStatus.SUCCESS,
        value_metrics={"margin": 0.25},
    )

    # Simular reinicio instanciando un repo totalmente nuevo sobre el mismo archivo
    repo2 = JsonOutcomeRepository(json_path)
    service2 = OutcomeTrackingService(repo2)

    loaded = service2.get_outcome("out-restart")
    assert loaded is not None
    assert loaded.outcome_id == "out-restart"
    assert loaded.result_id == "r-restart"
    assert loaded.value_metrics["margin"] == 0.25
