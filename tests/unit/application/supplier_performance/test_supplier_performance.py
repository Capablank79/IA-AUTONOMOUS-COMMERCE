from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
import pytest
from pathlib import Path

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, SupplierStatus
from src.domain.supplier_memory.models import SupplierMemoryRecord
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.calibration.models import DecisionCalibrationRecord, CalibrationStatus
from src.domain.supplier_performance.models import (
    SupplierPerformanceRecord,
    SupplierPerformanceStatus,
    SupplierTemporalPeriod,
    ObservedSupplierMetrics,
    DerivedSupplierMetrics,
)
from src.application.supplier_performance.supplier_performance_service import SupplierPerformanceService
from src.infrastructure.persistence.data.json.supplier_performance_repository import (
    JsonSupplierPerformanceRepository,
)


def make_sm(
    sm_id: str,
    supplier_id: str = "SUP-001",
    name: str = "Acme Supplies",
    cost: Optional[Decimal] = Decimal("5000"),
    moq: Optional[int] = 10,
    lead_time: Optional[int] = 5,
    accepted: bool = False,
    observed_at: Optional[datetime] = None,
) -> SupplierMemoryRecord:
    now = observed_at or datetime.now(timezone.utc)
    return SupplierMemoryRecord(
        supplier_memory_id=sm_id,
        supplier_id=supplier_id,
        name=name,
        status=SupplierStatus.VERIFIED,
        cost_amount=cost,
        moq=moq,
        lead_time_days=lead_time,
        observed_at=now,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        metadata={"quote_accepted": accepted},
    )


def make_outcome(
    oc_id: str,
    status: OutcomeStatus = OutcomeStatus.SUCCESS,
    order_placed: bool = True,
    fulfilled: bool = True,
    on_time: bool = True,
    cancelled: bool = False,
    defective: bool = False,
    cost: Optional[Decimal] = Decimal("5000"),
    moq: Optional[int] = 10,
    lead_time: Optional[int] = 5,
    mission_id: str = "miss-200",
    decision_id: str = "dec-200",
    action_id: str = "act-200",
) -> OutcomeRecord:
    now = datetime.now(timezone.utc)
    return OutcomeRecord(
        outcome_id=oc_id,
        mission_id=mission_id,
        decision_id=decision_id,
        action_id=action_id,
        status=status,
        observed_at=now,
        value_metrics={
            "order_placed": order_placed,
            "fulfilled": fulfilled,
            "on_time": on_time,
            "cancelled": cancelled,
            "defective": defective,
            "cost_amount": str(cost) if cost is not None else None,
            "moq": moq,
            "lead_time_days": lead_time,
        },
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )


def test_i5_supplier_identity_stability():
    service = SupplierPerformanceService(min_sample_threshold=1)
    period = SupplierTemporalPeriod(period_type="POINT_IN_TIME")

    sm1 = make_sm("sm-1", supplier_id="SUP-001")
    sm2 = make_sm("sm-2", supplier_id="SUP-OTHER")

    # Al calcular para SUP-001 solo debe considerar sm1
    rec = service.calculate_performance(
        performance_id="perf-id-1",
        supplier_id="SUP-001",
        period=period,
        supplier_records=[sm1, sm2],
    )

    assert rec.supplier_id == "SUP-001"
    assert rec.quote_sample_count == 1
    assert "sm-1" in rec.supplier_memory_ids
    assert "sm-2" not in rec.supplier_memory_ids


def test_i5_insufficient_data_handling():
    service = SupplierPerformanceService(min_sample_threshold=3)
    period = SupplierTemporalPeriod(period_type="POINT_IN_TIME")

    sm = make_sm("sm-1")
    rec = service.calculate_performance(
        performance_id="perf-insuff",
        supplier_id="SUP-001",
        period=period,
        supplier_records=[sm],
        outcome_records=[],
    )

    assert rec.status == SupplierPerformanceStatus.INSUFFICIENT_DATA
    assert rec.sample_count == 1
    assert rec.quote_sample_count == 1
    assert rec.outcome_sample_count == 0


def test_i5_valid_performance_observed_and_derived_metrics():
    service = SupplierPerformanceService(min_sample_threshold=1)
    period = SupplierTemporalPeriod(period_type="LIFETIME")

    sm1 = make_sm("sm-1", supplier_id="SUP-001", cost=Decimal("5000"), moq=10, lead_time=4, accepted=True)
    sm2 = make_sm("sm-2", supplier_id="SUP-001", cost=Decimal("6000"), moq=20, lead_time=6, accepted=False)

    oc1 = make_outcome("oc-1", status=OutcomeStatus.SUCCESS, order_placed=True, fulfilled=True, on_time=True, cancelled=False, defective=False, lead_time=5, moq=None)
    oc2 = make_outcome("oc-2", status=OutcomeStatus.FAILURE, order_placed=True, fulfilled=False, on_time=False, cancelled=True, defective=False, moq=None)

    rec = service.calculate_performance(
        performance_id="perf-valid",
        supplier_id="SUP-001",
        period=period,
        supplier_records=[sm1, sm2],
        outcome_records=[oc1, oc2],
    )

    assert rec.status == SupplierPerformanceStatus.SUFFICIENT_DATA
    assert rec.sample_count == 4
    assert rec.quote_sample_count == 2
    assert rec.outcome_sample_count == 2

    # Observed Metrics
    assert rec.observed_metrics.total_quotes_observed == 2
    assert rec.observed_metrics.total_accepted_quotes == 1
    assert rec.observed_metrics.total_orders_placed == 2
    assert rec.observed_metrics.total_fulfilled_orders == 1
    assert rec.observed_metrics.total_delivered_on_time == 1
    assert rec.observed_metrics.total_cancelled_orders == 1
    assert rec.observed_metrics.total_defective_returns == 0
    assert set(rec.observed_metrics.observed_lead_times_days) == {4, 5, 6}
    assert set(rec.observed_metrics.observed_quoted_costs) == {Decimal("5000"), Decimal("6000")}
    assert set(rec.observed_metrics.observed_moqs) == {10, 20}

    # Derived Metrics
    assert rec.derived_metrics.quote_acceptance_rate == 0.50
    assert rec.derived_metrics.average_quoted_cost == Decimal("5500")
    assert rec.derived_metrics.average_moq == 15.0
    assert rec.derived_metrics.average_lead_time_days == 5.0
    assert rec.derived_metrics.delivery_on_time_rate == 0.50
    assert rec.derived_metrics.fulfillment_rate == 0.50
    assert rec.derived_metrics.cancellation_rate == 0.50
    assert rec.derived_metrics.defect_return_rate == 0.0
    assert rec.derived_metrics.outcome_success_rate == 0.50


def test_i5_missing_evidence_unknown_handling():
    service = SupplierPerformanceService(min_sample_threshold=1)
    period = SupplierTemporalPeriod(period_type="POINT_IN_TIME")

    # Muestra sin cotizaciones ni outcomes operacionales reales
    sm = SupplierMemoryRecord(
        supplier_memory_id="sm-empty",
        supplier_id="SUP-001",
        name="Empty Supplier",
        status=SupplierStatus.UNVERIFIED,
        cost_amount=None,
        moq=None,
        lead_time_days=None,
    )

    rec = service.calculate_performance(
        performance_id="perf-empty",
        supplier_id="SUP-001",
        period=period,
        supplier_records=[sm],
    )

    assert rec.observed_metrics.observed_quoted_costs == ()
    assert rec.derived_metrics.average_quoted_cost is None
    assert rec.derived_metrics.average_moq is None
    assert rec.derived_metrics.average_lead_time_days is None
    assert rec.derived_metrics.fulfillment_rate is None
    assert rec.derived_metrics.delivery_on_time_rate is None


def test_i5_causal_links_and_calibration_context():
    service = SupplierPerformanceService(min_sample_threshold=1)
    period = SupplierTemporalPeriod(period_type="POINT_IN_TIME")

    sm = make_sm("sm-1")
    oc = make_outcome("oc-1", mission_id="miss-99", decision_id="dec-99", action_id="act-99")

    cal_ctx = DecisionCalibrationRecord(
        calibration_id="cal-ctx-supplier",
        decision_id="dec-99",
        mission_id="miss-99",
        status=CalibrationStatus.WELL_CALIBRATED,
        calibration_error=0.03,
    )

    rec = service.calculate_performance(
        performance_id="perf-causal",
        supplier_id="SUP-001",
        period=period,
        supplier_records=[sm],
        outcome_records=[oc],
        calibration_context=cal_ctx,
    )

    assert "sm-1" in rec.supplier_memory_ids
    assert "oc-1" in rec.outcome_ids
    assert "miss-99" in rec.mission_ids
    assert "dec-99" in rec.decision_ids
    assert "act-99" in rec.action_ids
    assert rec.calibration_context_id == "cal-ctx-supplier"
    assert rec.contextual_prediction_error == 0.03


def test_i5_deterministic_recomputation_and_persistence(tmp_path: Path):
    db_file = tmp_path / "supplier_performance.json"
    repo = JsonSupplierPerformanceRepository(db_file)
    service = SupplierPerformanceService(performance_repo=repo, min_sample_threshold=1)

    period = SupplierTemporalPeriod(period_type="DAILY")
    sm = make_sm("sm-1", cost=Decimal("10000"), moq=5, lead_time=3)
    oc = make_outcome("oc-1", order_placed=True, fulfilled=True, on_time=True)

    rec1 = service.calculate_performance(
        performance_id="perf-det-1",
        supplier_id="SUP-001",
        period=period,
        supplier_records=[sm],
        outcome_records=[oc],
        idempotency_key="idemp-i5-1",
    )

    rec2 = service.calculate_performance(
        performance_id="perf-det-dupe",
        supplier_id="SUP-001",
        period=period,
        supplier_records=[sm],
        outcome_records=[oc],
        idempotency_key="idemp-i5-1",
    )

    assert rec1.performance_id == rec2.performance_id
    assert rec1.derived_metrics.average_quoted_cost == rec2.derived_metrics.average_quoted_cost

    # Reload repository from disk
    repo2 = JsonSupplierPerformanceRepository(db_file)
    loaded = repo2.get_by_id("perf-det-1")
    assert loaded is not None
    assert loaded.performance_id == "perf-det-1"
    assert loaded.supplier_id == "SUP-001"
    assert loaded.derived_metrics.fulfillment_rate == 1.0


def test_i5_sensitive_data_exclusion(tmp_path: Path):
    db_file = tmp_path / "sensitive_supplier_perf.json"
    repo = JsonSupplierPerformanceRepository(db_file)
    service = SupplierPerformanceService(performance_repo=repo, min_sample_threshold=1)

    period = SupplierTemporalPeriod(period_type="POINT_IN_TIME")
    sm = make_sm("sm-1")

    service.calculate_performance(
        performance_id="perf-sensitive",
        supplier_id="SUP-001",
        period=period,
        supplier_records=[sm],
        metadata={"operator": "admin", "password": "secretpassword", "api_key": "key123"},
    )

    loaded = repo.get_by_id("perf-sensitive")
    assert loaded is not None
    assert "password" not in loaded.metadata
    assert "api_key" not in loaded.metadata
    assert loaded.metadata.get("operator") == "admin"
