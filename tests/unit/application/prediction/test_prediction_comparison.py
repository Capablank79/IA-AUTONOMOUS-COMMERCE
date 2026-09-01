import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.prediction.models import PredictionRecord, PredictionComparison, ComparisonStatus
from src.infrastructure.persistence.data.json.prediction_repository import JsonPredictionRepository, InvalidPredictionDataError
from src.application.prediction.prediction_comparison_service import PredictionComparisonService


@pytest.fixture
def temp_repo_path(tmp_path: Path) -> Path:
    return tmp_path / "test_predictions.json"


@pytest.fixture
def repo(temp_repo_path: Path) -> JsonPredictionRepository:
    return JsonPredictionRepository(temp_repo_path)


@pytest.fixture
def service(repo: JsonPredictionRepository) -> PredictionComparisonService:
    return PredictionComparisonService(repo)


def test_prediction_creation_and_reuse(service: PredictionComparisonService):
    pred = service.register_prediction(
        prediction_id="pred-100",
        mission_id="m-1",
        decision_id="d-1",
        action_id="act-1",
        target_metric="units_sold",
        predicted_value=150,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.DERIVED,
        idempotency_key="idemp-pred-100",
    )
    assert pred.prediction_id == "pred-100"
    assert pred.predicted_value == 150

    # Idempotent reuse
    duplicate = service.register_prediction(
        prediction_id="pred-100-dup",
        mission_id="m-1",
        decision_id="d-1",
        idempotency_key="idemp-pred-100",
    )
    assert duplicate.prediction_id == "pred-100"


def test_prediction_persistence_and_retrieval(repo: JsonPredictionRepository, temp_repo_path: Path):
    pred = PredictionRecord(
        prediction_id="pred-200",
        mission_id="m-2",
        decision_id="d-2",
        target_metric="realized_margin",
        predicted_value=25.5,
        confidence=Confidence.HIGH,
    )
    repo.save_prediction(pred)

    # Reload from disk
    new_repo = JsonPredictionRepository(temp_repo_path)
    retrieved = new_repo.get_prediction_by_id("pred-200")
    assert retrieved is not None
    assert retrieved.prediction_id == "pred-200"
    assert retrieved.predicted_value == 25.5


def test_numeric_expected_vs_actual_match(service: PredictionComparisonService):
    now = datetime.now(timezone.utc)
    t_pred = now - timedelta(minutes=10)
    t_outcome = now

    pred = service.register_prediction(
        prediction_id="pred-num-1",
        mission_id="m-1",
        decision_id="d-1",
        target_metric="realized_margin",
        predicted_value=30.0,
        created_at=t_pred,
        idempotency_key="idemp-p-num-1",
    )

    outcome = OutcomeRecord(
        outcome_id="out-num-1",
        mission_id="m-1",
        decision_id="d-1",
        action_id="act-1",
        status=OutcomeStatus.SUCCESS,
        observed_at=t_outcome,
        value_metrics={"realized_margin": 30.0},
    )

    comp = service.compare_prediction_vs_actual(
        comparison_id="comp-1",
        prediction=pred,
        outcome=outcome,
        idempotency_key="idemp-c-1",
    )

    assert comp.status == ComparisonStatus.MATCH
    assert comp.delta == pytest.approx(0.0)
    assert comp.expected_value == 30.0
    assert comp.actual_value == 30.0


def test_numeric_expected_vs_actual_miss(service: PredictionComparisonService):
    now = datetime.now(timezone.utc)
    t_pred = now - timedelta(minutes=10)
    t_outcome = now

    pred = service.register_prediction(
        prediction_id="pred-num-2",
        mission_id="m-1",
        decision_id="d-1",
        target_metric="units_sold",
        predicted_value=100,
        created_at=t_pred,
        idempotency_key="idemp-p-num-2",
    )

    outcome = OutcomeRecord(
        outcome_id="out-num-2",
        mission_id="m-1",
        decision_id="d-1",
        action_id="act-1",
        status=OutcomeStatus.SUCCESS,
        observed_at=t_outcome,
        value_metrics={"units_sold": 80},
    )

    comp = service.compare_prediction_vs_actual(
        comparison_id="comp-2",
        prediction=pred,
        outcome=outcome,
        idempotency_key="idemp-c-2",
    )

    assert comp.status == ComparisonStatus.MISS
    assert comp.delta == -20.0


def test_categorical_expected_vs_actual(service: PredictionComparisonService):
    now = datetime.now(timezone.utc)
    t_pred = now - timedelta(minutes=10)
    t_outcome = now

    pred = service.register_prediction(
        prediction_id="pred-cat-1",
        mission_id="m-1",
        decision_id="d-1",
        target_metric="fulfillment_channel",
        predicted_class="DROPSHIPPING",
        created_at=t_pred,
        idempotency_key="idemp-p-cat-1",
    )

    outcome = OutcomeRecord(
        outcome_id="out-cat-1",
        mission_id="m-1",
        decision_id="d-1",
        action_id="act-1",
        status=OutcomeStatus.SUCCESS,
        observed_at=t_outcome,
        value_metrics={"fulfillment_channel": "DROPSHIPPING"},
    )

    comp = service.compare_prediction_vs_actual(
        comparison_id="comp-cat-1",
        prediction=pred,
        outcome=outcome,
        idempotency_key="idemp-c-cat-1",
    )

    assert comp.status == ComparisonStatus.MATCH
    assert comp.delta is None


def test_unknown_outcome_comparison(service: PredictionComparisonService):
    now = datetime.now(timezone.utc)
    t_pred = now - timedelta(minutes=10)
    t_outcome = now

    pred = service.register_prediction(
        prediction_id="pred-unk-1",
        mission_id="m-1",
        decision_id="d-1",
        target_metric="units_sold",
        predicted_value=50,
        created_at=t_pred,
        idempotency_key="idemp-p-unk-1",
    )

    outcome = OutcomeRecord(
        outcome_id="out-unk-1",
        mission_id="m-1",
        decision_id="d-1",
        action_id="act-1",
        status=OutcomeStatus.UNKNOWN,
        observed_at=t_outcome,
        value_metrics={},
    )

    comp = service.compare_prediction_vs_actual(
        comparison_id="comp-unk-1",
        prediction=pred,
        outcome=outcome,
        idempotency_key="idemp-c-unk-1",
    )

    assert comp.status == ComparisonStatus.UNKNOWN
    assert comp.actual_value is None
    assert comp.delta is None


def test_temporal_order_validation(service: PredictionComparisonService):
    now = datetime.now(timezone.utc)
    t_pred = now  # Predicción hecha despues del outcome
    t_outcome = now - timedelta(minutes=5)

    pred = service.register_prediction(
        prediction_id="pred-temp-1",
        mission_id="m-1",
        decision_id="d-1",
        target_metric="units_sold",
        predicted_value=10,
        created_at=t_pred,
        idempotency_key="idemp-p-temp-1",
    )

    outcome = OutcomeRecord(
        outcome_id="out-temp-1",
        mission_id="m-1",
        decision_id="d-1",
        action_id="act-1",
        status=OutcomeStatus.SUCCESS,
        observed_at=t_outcome,
        value_metrics={"units_sold": 10},
    )

    with pytest.raises(ValueError, match="Temporal order violation"):
        service.compare_prediction_vs_actual(
            comparison_id="comp-temp-1",
            prediction=pred,
            outcome=outcome,
            idempotency_key="idemp-c-temp-1",
        )


def test_sensitive_data_exclusion(repo: JsonPredictionRepository, temp_repo_path: Path):
    pred = PredictionRecord(
        prediction_id="pred-sens-1",
        mission_id="m-1",
        decision_id="d-1",
        metadata={"api_key": "secret-key-123", "password": "super-password", "safe_field": "hello"},
    )
    repo.save_prediction(pred)

    with open(temp_repo_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "secret-key-123" not in content
    assert "super-password" not in content
    assert "safe_field" in content
