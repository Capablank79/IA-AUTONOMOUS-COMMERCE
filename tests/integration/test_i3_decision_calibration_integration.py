from datetime import datetime, timezone, timedelta
import pytest
from pathlib import Path

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.decision.models import DecisionRecord, DecisionType, DecisionStatus, DecisionOutcome
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.prediction.models import PredictionRecord, ComparisonStatus
from src.domain.calibration.models import CalibrationStatus
from src.application.prediction.prediction_comparison_service import PredictionComparisonService
from src.application.outcome.outcome_service import OutcomeTrackingService
from src.application.calibration.decision_calibration_service import DecisionCalibrationService
from src.infrastructure.persistence.data.json.prediction_repository import JsonPredictionRepository
from src.infrastructure.persistence.data.json.outcome_repository import JsonOutcomeRepository
from src.infrastructure.persistence.data.json.calibration_repository import JsonCalibrationRepository


def test_i3_decision_calibration_integration_chain(tmp_path: Path):
    """
    Test de integración E2E para Task I.3:
    Demuestra la cadena de trazabilidad causal completa:
    MISSION -> DECISION -> PREDICTION -> ACTION -> RESULT -> OUTCOME -> COMPARISON -> CALIBRATION
    """
    pred_repo = JsonPredictionRepository(tmp_path / "predictions.json")
    out_repo = JsonOutcomeRepository(tmp_path / "outcomes.json")
    cal_repo = JsonCalibrationRepository(tmp_path / "calibrations.json")

    pred_service = PredictionComparisonService(pred_repo)
    out_service = OutcomeTrackingService(out_repo)
    cal_service = DecisionCalibrationService(cal_repo, min_sample_threshold=5)

    mission_id = "miss-e2e-calib-1"
    decision_id = "dec-e2e-calib-1"
    now = datetime.now(timezone.utc)

    # 1. Crear 6 pares de Predicciones y Outcomes
    comparisons = []
    for i in range(1, 7):
        pred_id = f"pred-{i}"
        act_id = f"act-{i}"
        out_id = f"out-{i}"
        comp_id = f"comp-{i}"

        # Registrar Predicción
        pred = pred_service.register_prediction(
            prediction_id=pred_id,
            mission_id=mission_id,
            decision_id=decision_id,
            action_id=act_id,
            target_metric="units_sold",
            predicted_value=100.0,
            confidence=Confidence.HIGH,
            created_at=now - timedelta(hours=2),
            idempotency_key=f"idemp-pred-{i}",
        )

        # Registrar Outcome observado (5 son exitosos con units_sold=100.0 [MATCH], 1 es falla con units_sold=20.0 [MISS])
        actual_sold = 100.0 if i <= 5 else 20.0
        out_status = OutcomeStatus.SUCCESS if i <= 5 else OutcomeStatus.FAILURE
        outcome = out_service.record_outcome(
            outcome_id=out_id,
            mission_id=mission_id,
            decision_id=decision_id,
            action_id=act_id,
            outcome_type="SALES_OBSERVATION",
            status=out_status,
            observed_at=now - timedelta(hours=1),
            value_metrics={"units_sold": actual_sold},
            idempotency_key=f"idemp-out-{i}",
        )

        # Comparar PREDICTION vs ACTUAL
        comp = pred_service.compare_prediction_vs_actual(
            comparison_id=comp_id,
            prediction=pred,
            outcome=outcome,
            evaluated_at=now,
            idempotency_key=f"idemp-comp-{i}",
        )
        comparisons.append(comp)

    # 2. Calcular la calibración de decisiones para esta decisión y misión
    calib = cal_service.calculate_calibration(
        calibration_id="calib-e2e-1",
        comparisons=comparisons,
        decision_id=decision_id,
        mission_id=mission_id,
        target_metric="units_sold",
        idempotency_key="idemp-calib-e2e-1",
    )

    # 3. Verificaciones de calibración
    assert calib.calibration_id == "calib-e2e-1"
    assert calib.decision_id == decision_id
    assert calib.mission_id == mission_id
    assert calib.target_metric == "units_sold"
    assert calib.total_samples == 6
    assert calib.valid_samples == 6
    assert calib.unknown_excluded_samples == 0
    assert calib.match_count == 5
    assert calib.miss_count == 1
    assert calib.accuracy == pytest.approx(0.8333, abs=1e-3)
    assert calib.status == CalibrationStatus.WELL_CALIBRATED

    # 4. Verificar trazabilidad causal de IDs
    assert len(calib.comparison_ids) == 6
    assert len(calib.prediction_ids) == 6
    assert len(calib.outcome_ids) == 6
    assert "pred-1" in calib.prediction_ids
    assert "out-1" in calib.outcome_ids
    assert "comp-1" in calib.comparison_ids

    # 5. Verificar persistencia durable de calibración
    calib_reloaded = cal_repo.get_calibration_by_id("calib-e2e-1")
    assert calib_reloaded is not None
    assert calib_reloaded.status == CalibrationStatus.WELL_CALIBRATED
    assert calib_reloaded.valid_samples == 6
