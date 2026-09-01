from datetime import datetime, timezone, timedelta
import pytest
from pathlib import Path

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.prediction.models import PredictionRecord, PredictionComparison, ComparisonStatus
from src.domain.calibration.models import (
    CalibrationStatus,
    DecisionCalibrationRecord,
)
from src.application.calibration.decision_calibration_service import DecisionCalibrationService
from src.infrastructure.persistence.data.json.calibration_repository import JsonCalibrationRepository


def make_comp(
    comp_id: str,
    status: ComparisonStatus,
    confidence: Confidence = Confidence.HIGH,
    pred_id: str = "pred-1",
    out_id: str = "out-1",
    dec_id: str = "dec-1",
    miss_id: str = "miss-1",
    metric: str = "general",
) -> PredictionComparison:
    now = datetime.now(timezone.utc)
    return PredictionComparison(
        comparison_id=comp_id,
        prediction_id=pred_id,
        outcome_id=out_id,
        mission_id=miss_id,
        decision_id=dec_id,
        target_metric=metric,
        status=status,
        prediction_confidence=confidence,
        evaluated_at=now,
        prediction_timestamp=now - timedelta(hours=1),
        outcome_timestamp=now,
    )


def test_i3_calibration_insufficient_data():
    service = DecisionCalibrationService(min_sample_threshold=5)
    comps = [
        make_comp("c1", ComparisonStatus.MATCH, Confidence.HIGH),
        make_comp("c2", ComparisonStatus.MATCH, Confidence.HIGH),
    ]

    record = service.calculate_calibration("cal-1", comps, decision_id="dec-1", mission_id="miss-1")

    assert record.status == CalibrationStatus.INSUFFICIENT_DATA
    assert record.total_samples == 2
    assert record.valid_samples == 2
    assert record.brier_score is None


def test_i3_calibration_well_calibrated_scenario():
    service = DecisionCalibrationService(min_sample_threshold=5)
    # 10 predicciones con HIGH confidence (expected 0.90), 9 MATCH, 1 MISS -> accuracy 0.90, calib gap 0.0
    comps = [
        make_comp(f"c{i}", ComparisonStatus.MATCH if i <= 9 else ComparisonStatus.MISS, Confidence.HIGH)
        for i in range(1, 11)
    ]

    record = service.calculate_calibration("cal-2", comps, decision_id="dec-1", mission_id="miss-1")

    assert record.status == CalibrationStatus.WELL_CALIBRATED
    assert record.total_samples == 10
    assert record.valid_samples == 10
    assert record.match_count == 9
    assert record.miss_count == 1
    assert record.accuracy == 0.90
    assert record.error_rate == 0.10
    assert record.expected_confidence_score == 0.90
    assert record.calibration_error == 0.0
    assert record.brier_score == pytest.approx(0.09, abs=1e-3)


def test_i3_calibration_over_confident_scenario():
    service = DecisionCalibrationService(min_sample_threshold=5)
    # 10 predicciones con HIGH confidence (expected 0.90), pero sólo 3 MATCH, 7 MISS -> accuracy 0.30
    comps = [
        make_comp(f"c{i}", ComparisonStatus.MATCH if i <= 3 else ComparisonStatus.MISS, Confidence.HIGH)
        for i in range(1, 11)
    ]

    record = service.calculate_calibration("cal-3", comps, decision_id="dec-1", mission_id="miss-1")

    assert record.status == CalibrationStatus.OVER_CONFIDENT
    assert record.accuracy == 0.30
    assert record.expected_confidence_score == 0.90
    assert record.calibration_error == 0.60


def test_i3_calibration_under_confident_scenario():
    service = DecisionCalibrationService(min_sample_threshold=5)
    # 10 predicciones con LOW confidence (expected 0.40), pero 10 MATCH -> accuracy 1.0
    comps = [
        make_comp(f"c{i}", ComparisonStatus.MATCH, Confidence.LOW)
        for i in range(1, 11)
    ]

    record = service.calculate_calibration("cal-4", comps, decision_id="dec-1", mission_id="miss-1")

    assert record.status == CalibrationStatus.UNDER_CONFIDENT
    assert record.accuracy == 1.0
    assert record.expected_confidence_score == 0.40
    assert record.calibration_error == 0.60


def test_i3_unknown_outcome_exclusion():
    service = DecisionCalibrationService(min_sample_threshold=5)
    # 5 MATCH con HIGH, 3 UNKNOWN -> total 8, válidas 5, excluidas 3
    comps = [
        make_comp(f"c{i}", ComparisonStatus.MATCH, Confidence.HIGH) for i in range(1, 6)
    ] + [
        make_comp(f"u{i}", ComparisonStatus.UNKNOWN, Confidence.HIGH) for i in range(1, 4)
    ]

    record = service.calculate_calibration("cal-5", comps, decision_id="dec-1", mission_id="miss-1")

    assert record.total_samples == 8
    assert record.valid_samples == 5
    assert record.unknown_excluded_samples == 3
    assert record.accuracy == 1.0
    # UNKNOWN no debe convertirse en MATCH ni MISS
    assert record.match_count == 5
    assert record.miss_count == 0


def test_i3_deterministic_recomputation_and_idempotency(tmp_path: Path):
    db_file = tmp_path / "calibrations.json"
    repo = JsonCalibrationRepository(db_file)
    service = DecisionCalibrationService(calibration_repo=repo, min_sample_threshold=5)

    comps = [
        make_comp(f"c{i}", ComparisonStatus.MATCH if i <= 8 else ComparisonStatus.MISS, Confidence.HIGH)
        for i in range(1, 11)
    ]

    rec1 = service.calculate_calibration(
        "cal-6", comparisons=comps, decision_id="dec-1", mission_id="miss-1", idempotency_key="idemp-i3-1"
    )
    rec2 = service.calculate_calibration(
        "cal-6-dupe", comparisons=comps, decision_id="dec-1", mission_id="miss-1", idempotency_key="idemp-i3-1"
    )

    assert rec1.calibration_id == rec2.calibration_id
    assert rec1.accuracy == rec2.accuracy
    assert rec1.status == rec2.status

    # Reload repo and verify persistence
    repo2 = JsonCalibrationRepository(db_file)
    loaded = repo2.get_calibration_by_id("cal-6")
    assert loaded is not None
    assert loaded.calibration_id == "cal-6"
    assert loaded.valid_samples == 10


def test_i3_provenance_and_evidence_links():
    service = DecisionCalibrationService(min_sample_threshold=5)
    comps = [
        make_comp("c1", ComparisonStatus.MATCH, pred_id="p1", out_id="o1"),
        make_comp("c2", ComparisonStatus.MATCH, pred_id="p2", out_id="o2"),
        make_comp("c3", ComparisonStatus.MATCH, pred_id="p3", out_id="o3"),
        make_comp("c4", ComparisonStatus.MATCH, pred_id="p4", out_id="o4"),
        make_comp("c5", ComparisonStatus.MATCH, pred_id="p5", out_id="o5"),
    ]

    record = service.calculate_calibration("cal-7", comps, decision_id="dec-99", mission_id="miss-99")

    assert record.decision_id == "dec-99"
    assert record.mission_id == "miss-99"
    assert len(record.comparison_ids) == 5
    assert len(record.prediction_ids) == 5
    assert len(record.outcome_ids) == 5
    assert "p1" in record.prediction_ids
    assert "o1" in record.outcome_ids


def test_i3_sensitive_data_exclusion(tmp_path: Path):
    db_file = tmp_path / "sensitive_calib.json"
    repo = JsonCalibrationRepository(db_file)
    service = DecisionCalibrationService(calibration_repo=repo, min_sample_threshold=5)

    comps = [
        make_comp(f"c{i}", ComparisonStatus.MATCH, Confidence.HIGH) for i in range(1, 6)
    ]

    record = service.calculate_calibration(
        "cal-sensitive",
        comps,
        metadata={"user": "admin", "password": "supersecretpassword", "api_key": "12345"},
    )

    loaded = repo.get_calibration_by_id("cal-sensitive")
    assert loaded is not None
    assert "password" not in loaded.metadata
    assert "api_key" not in loaded.metadata
    assert loaded.metadata.get("user") == "admin"
