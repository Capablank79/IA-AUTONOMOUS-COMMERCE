from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from pathlib import Path

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.decision.models import DecisionRecord, DecisionType, DecisionStatus, DecisionOutcome
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.calibration.models import DecisionCalibrationRecord, CalibrationStatus
from src.domain.product_performance.models import ProductPerformanceRecord, TemporalPeriod, PerformanceStatus
from src.domain.supplier_performance.models import SupplierPerformanceRecord, SupplierTemporalPeriod, SupplierPerformanceStatus
from src.domain.strategy_performance.models import (
    StrategyPerformanceRecord,
    StrategyPerformanceStatus,
    StrategyTemporalPeriod,
    ObservedStrategyMetrics,
    DerivedStrategyMetrics,
)
from src.application.strategy_performance.strategy_performance_service import StrategyPerformanceService
from src.infrastructure.persistence.data.json.strategy_performance_repository import (
    JsonStrategyPerformanceRepository,
    InvalidStrategyPerformanceDataError,
)


def make_decision(
    dec_id: str,
    strategy_id: str = "STRAT-DROPSHIP-1",
    mission_id: str = "miss-100",
    decision_type: DecisionType = DecisionType.MARKET_OPPORTUNITY,
    status: DecisionStatus = DecisionStatus.EXECUTED,
    outcome: DecisionOutcome = DecisionOutcome.SUCCESS,
    created_at: datetime = None,
    product_id: str = "prod-1",
    supplier_id: str = "supp-1",
) -> DecisionRecord:
    now = created_at or datetime.now(timezone.utc)
    return DecisionRecord(
        decision_id=dec_id,
        mission_id=mission_id,
        decision_type=decision_type,
        status=status,
        reason="Test strategy decision",
        created_at=now,
        outcome=outcome,
        parameters={"strategy_id": strategy_id, "product_id": product_id, "supplier_id": supplier_id},
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )


def make_outcome(
    oc_id: str,
    mission_id: str = "miss-100",
    decision_id: str = "dec-100",
    action_id: str = "act-100",
    status: OutcomeStatus = OutcomeStatus.SUCCESS,
    profit: Decimal = Decimal("5000"),
    revenue: Decimal = Decimal("25000"),
    cancellations: int = 0,
    returns: int = 0,
    product_id: str = "prod-1",
    supplier_id: str = "supp-1",
    observed_at: datetime = None,
) -> OutcomeRecord:
    now = observed_at or datetime.now(timezone.utc)
    return OutcomeRecord(
        outcome_id=oc_id,
        mission_id=mission_id,
        decision_id=decision_id,
        action_id=action_id,
        status=status,
        observed_at=now,
        value_metrics={
            "profit": str(profit) if profit is not None else None,
            "revenue": str(revenue) if revenue is not None else None,
            "cancellations": cancellations,
            "returns": returns,
            "product_id": product_id,
            "supplier_id": supplier_id,
        },
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )


def test_i6_strategy_identity_and_filtering():
    service = StrategyPerformanceService(min_sample_threshold=1)
    period = StrategyTemporalPeriod(period_type="POINT_IN_TIME")

    d1 = make_decision("dec-1", strategy_id="STRAT-A")
    d2 = make_decision("dec-2", strategy_id="STRAT-B")

    record = service.calculate_performance(
        performance_id="perf-strat-a",
        strategy_id="STRAT-A",
        period=period,
        decision_records=[d1, d2],
    )

    assert record.strategy_id == "STRAT-A"
    assert record.decision_sample_count == 1
    assert record.decision_ids == ("dec-1",)


def test_i6_valid_performance_observed_and_derived_metrics():
    service = StrategyPerformanceService(min_sample_threshold=1)
    period = StrategyTemporalPeriod(period_type="POINT_IN_TIME")

    d1 = make_decision("dec-1", strategy_id="STRAT-PROFIT")
    oc1 = make_outcome("oc-1", decision_id="dec-1", status=OutcomeStatus.SUCCESS, profit=Decimal("10000"), revenue=Decimal("50000"))
    oc2 = make_outcome("oc-2", decision_id="dec-1", status=OutcomeStatus.FAILURE, profit=Decimal("-2000"), revenue=Decimal("10000"))

    record = service.calculate_performance(
        performance_id="perf-profit-1",
        strategy_id="STRAT-PROFIT",
        period=period,
        decision_records=[d1],
        outcome_records=[oc1, oc2],
    )

    assert record.status == StrategyPerformanceStatus.SUFFICIENT_DATA
    assert record.sample_count == 3  # 1 dec + 2 outcomes
    assert record.observed_metrics.success_count == 1
    assert record.observed_metrics.failure_count == 1
    assert record.observed_metrics.observed_profit == Decimal("8000")
    assert record.observed_metrics.observed_revenue == Decimal("60000")
    assert record.derived_metrics.success_rate == 0.5
    assert record.derived_metrics.failure_rate == 0.5
    assert record.derived_metrics.average_realized_profit == Decimal("8000")  # 8000 / 1 success
    assert record.derived_metrics.average_margin_percentage == pytest.approx(0.1333, abs=1e-3)


def test_i6_unknown_and_missing_data_handling():
    service = StrategyPerformanceService(min_sample_threshold=1)
    period = StrategyTemporalPeriod(period_type="POINT_IN_TIME")

    d1 = make_decision("dec-1", strategy_id="STRAT-UNK")
    oc_unk = make_outcome("oc-unk", decision_id="dec-1", status=OutcomeStatus.UNKNOWN, profit=None, revenue=None)

    record = service.calculate_performance(
        performance_id="perf-unk-1",
        strategy_id="STRAT-UNK",
        period=period,
        decision_records=[d1],
        outcome_records=[oc_unk],
    )

    assert record.status == StrategyPerformanceStatus.SUFFICIENT_DATA
    assert record.observed_metrics.unknown_count == 1
    assert record.observed_metrics.observed_profit is None
    assert record.observed_metrics.observed_revenue is None
    assert record.derived_metrics.average_realized_profit is None
    assert record.derived_metrics.average_margin_percentage is None


def test_i6_insufficient_sample_handling():
    service = StrategyPerformanceService(min_sample_threshold=5)
    period = StrategyTemporalPeriod(period_type="POINT_IN_TIME")

    d1 = make_decision("dec-1", strategy_id="STRAT-FEW")
    oc1 = make_outcome("oc-1", decision_id="dec-1")

    record = service.calculate_performance(
        performance_id="perf-few-1",
        strategy_id="STRAT-FEW",
        period=period,
        decision_records=[d1],
        outcome_records=[oc1],
    )

    assert record.status == StrategyPerformanceStatus.INSUFFICIENT_DATA
    assert record.sample_count == 2
    assert record.derived_metrics.success_rate is None


def test_i6_causal_traceability_and_calibration_context():
    service = StrategyPerformanceService(min_sample_threshold=1)
    period = StrategyTemporalPeriod(period_type="POINT_IN_TIME")

    d1 = make_decision("dec-1", strategy_id="STRAT-TRACE", mission_id="miss-55", product_id="prod-10", supplier_id="supp-20")
    oc1 = make_outcome("oc-1", decision_id="dec-1", mission_id="miss-55", product_id="prod-10", supplier_id="supp-20")

    cal_ctx = DecisionCalibrationRecord(
        calibration_id="cal-999",
        status=CalibrationStatus.WELL_CALIBRATED,
        calibration_error=0.04,
        confidence_bins=(),
        comparison_ids=(),
        prediction_ids=(),
        outcome_ids=(),
        calculated_at=datetime.now(timezone.utc),
    )

    record = service.calculate_performance(
        performance_id="perf-trace-1",
        strategy_id="STRAT-TRACE",
        period=period,
        decision_records=[d1],
        outcome_records=[oc1],
        calibration_context=cal_ctx,
    )

    assert record.decision_ids == ("dec-1",)
    assert record.outcome_ids == ("oc-1",)
    assert record.mission_ids == ("miss-55",)
    assert record.product_ids == ("prod-10",)
    assert record.supplier_ids == ("supp-20",)
    assert record.calibration_context_id == "cal-999"
    assert record.contextual_prediction_error == 0.04


def test_i6_deterministic_recomputation_and_persistence(tmp_path: Path):
    db_file = tmp_path / "strategy_performance_db.json"
    repo = JsonStrategyPerformanceRepository(db_file)
    service = StrategyPerformanceService(performance_repo=repo, min_sample_threshold=1)

    period = StrategyTemporalPeriod(period_type="POINT_IN_TIME")
    d1 = make_decision("dec-1", strategy_id="STRAT-PERSIST")
    oc1 = make_outcome("oc-1", decision_id="dec-1")

    # Primera ejecución
    rec1 = service.calculate_performance(
        performance_id="perf-p1",
        strategy_id="STRAT-PERSIST",
        period=period,
        decision_records=[d1],
        outcome_records=[oc1],
        idempotency_key="idempotency-strat-p1",
    )

    # Replay con idempotencia
    rec2 = service.calculate_performance(
        performance_id="perf-p1",
        strategy_id="STRAT-PERSIST",
        period=period,
        decision_records=[d1],
        outcome_records=[oc1],
        idempotency_key="idempotency-strat-p1",
    )

    assert rec1.performance_id == rec2.performance_id

    # Simular reinicio leyendo repositorio limpio
    reloaded_repo = JsonStrategyPerformanceRepository(db_file)
    reloaded_rec = reloaded_repo.get_performance_by_id("perf-p1")

    assert reloaded_rec is not None
    assert reloaded_rec.strategy_id == "STRAT-PERSIST"
    assert reloaded_rec.sample_count == 2
    assert reloaded_rec.derived_metrics.success_rate == 1.0


def test_i6_sensitive_data_exclusion(tmp_path: Path):
    db_file = tmp_path / "strategy_performance_sec.json"
    repo = JsonStrategyPerformanceRepository(db_file)
    service = StrategyPerformanceService(performance_repo=repo, min_sample_threshold=1)

    period = StrategyTemporalPeriod(period_type="POINT_IN_TIME")
    d1 = make_decision("dec-1", strategy_id="STRAT-SEC")

    meta = {
        "normal_key": "visible_value",
        "api_key": "SECRET_KEY_123",
        "password": "MY_PASSWORD",
        "nested": {
            "token": "BEARER_XYZ",
            "safe": 42,
        },
    }

    rec = service.calculate_performance(
        performance_id="perf-sec-1",
        strategy_id="STRAT-SEC",
        period=period,
        decision_records=[d1],
        metadata=meta,
    )

    # Verificar sanitización en memoria
    assert "api_key" not in rec.metadata
    assert "password" not in rec.metadata
    assert rec.metadata["normal_key"] == "visible_value"
    assert "token" not in rec.metadata["nested"]
    assert rec.metadata["nested"]["safe"] == 42

    # Verificar sanitización en JSON persistido
    with open(db_file, "r", encoding="utf-8") as f:
        file_content = f.read()

    assert "SECRET_KEY_123" not in file_content
    assert "MY_PASSWORD" not in file_content
    assert "BEARER_XYZ" not in file_content


def test_i6_corrupted_persistence_handling(tmp_path: Path):
    db_file = tmp_path / "corrupted.json"
    with open(db_file, "w", encoding="utf-8") as f:
        f.write("{ invalid json }")

    repo = JsonStrategyPerformanceRepository(db_file)
    with pytest.raises(InvalidStrategyPerformanceDataError):
        repo.get_performance_by_id("any")
