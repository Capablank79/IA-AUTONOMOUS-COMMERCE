import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.prediction.models import ComparisonStatus
from src.infrastructure.persistence.data.json.outcome_repository import JsonOutcomeRepository
from src.infrastructure.persistence.data.json.prediction_repository import JsonPredictionRepository
from src.application.outcome.outcome_service import OutcomeTrackingService
from src.application.prediction.prediction_comparison_service import PredictionComparisonService


def test_prediction_vs_actual_full_causal_integration(tmp_path: Path):
    pred_repo_path = tmp_path / "predictions.json"
    outcome_repo_path = tmp_path / "outcomes.json"

    pred_repo = JsonPredictionRepository(pred_repo_path)
    outcome_repo = JsonOutcomeRepository(outcome_repo_path)

    pred_service = PredictionComparisonService(pred_repo)
    outcome_service = OutcomeTrackingService(outcome_repo)

    # 1. Simular la cadena causal: MISSION -> DECISION -> ACTION -> RESULT -> PREDICTION -> OUTCOME -> COMPARISON
    mission_id = "mission-mlc-999"
    decision_id = "dec-pricing-888"
    action_id = "act-publish-777"
    result_id = "res-success-666"

    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    t1 = datetime.now(timezone.utc) - timedelta(hours=1)

    # 2. Registrar Predicción previa
    prediction = pred_service.register_prediction(
        prediction_id="pred-margin-01",
        mission_id=mission_id,
        decision_id=decision_id,
        action_id=action_id,
        target_metric="realized_margin",
        predicted_value=22.5,
        created_at=t0,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.DERIVED,
        idempotency_key="idemp-pred-margin-01",
    )
    assert prediction.prediction_id == "pred-margin-01"

    # 3. Registrar Outcome real post-acción (Hito I.1)
    outcome = outcome_service.record_outcome(
        outcome_id="out-margin-01",
        mission_id=mission_id,
        decision_id=decision_id,
        action_id=action_id,
        result_id=result_id,
        status=OutcomeStatus.SUCCESS,
        observed_at=t1,
        value_metrics={"realized_margin": 22.5},
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        idempotency_key="idemp-out-margin-01",
    )
    assert outcome.outcome_id == "out-margin-01"

    # 4. Ejecutar comparación PREDICTION vs ACTUAL
    comparison = pred_service.compare_prediction_vs_actual(
        comparison_id="comp-margin-01",
        prediction=prediction,
        outcome=outcome,
        idempotency_key="idemp-comp-margin-01",
    )

    # 5. Verificaciones de contrato y trazabilidad
    assert comparison.comparison_id == "comp-margin-01"
    assert comparison.prediction_id == "pred-margin-01"
    assert comparison.outcome_id == "out-margin-01"
    assert comparison.mission_id == mission_id
    assert comparison.decision_id == decision_id
    assert comparison.action_id == action_id
    assert comparison.target_metric == "realized_margin"
    assert comparison.expected_value == 22.5
    assert comparison.actual_value == 22.5
    assert comparison.delta == pytest.approx(0.0)
    assert comparison.status == ComparisonStatus.MATCH
    assert comparison.prediction_provenance == EvidenceProvenanceType.DERIVED
    assert comparison.outcome_provenance == EvidenceProvenanceType.LIVE
    assert comparison.prediction_confidence == Confidence.HIGH

    # 6. Reinicio de servicios desde disco y recuperación de la comparación
    new_pred_repo = JsonPredictionRepository(pred_repo_path)
    new_pred_service = PredictionComparisonService(new_pred_repo)

    reloaded_comp = new_pred_service.get_comparison("comp-margin-01")
    assert reloaded_comp is not None
    assert reloaded_comp.status == ComparisonStatus.MATCH
    assert reloaded_comp.expected_value == 22.5
