from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
from pathlib import Path

from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType
from src.domain.product_memory.models import ProductMemoryRecord
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.calibration.models import DecisionCalibrationRecord, CalibrationStatus
from src.domain.product_performance.models import (
    ProductPerformanceRecord,
    PerformanceStatus,
    TemporalPeriod,
    ObservedProductMetrics,
    DerivedProductMetrics,
)
from src.application.product_performance.product_performance_service import ProductPerformanceService
from src.infrastructure.persistence.data.json.product_performance_repository import (
    JsonProductPerformanceRepository,
)


def make_pm(
    pm_id: str,
    sku: str = "SKU-TEST-1",
    ext_id: str = "MLC123456",
    price: Decimal = Decimal("10000"),
    sold_qty: int = 5,
    avail_qty: int = 20,
    observed_at: datetime = None,
    cost: Decimal = Decimal("6000"),
) -> ProductMemoryRecord:
    now = observed_at or datetime.now(timezone.utc)
    return ProductMemoryRecord(
        product_memory_id=pm_id,
        sku=sku,
        external_id=ext_id,
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Test Product Title",
        category="ELECTRONICS",
        price_amount=price,
        sold_quantity=sold_qty,
        available_quantity=avail_qty,
        observed_at=now,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        metadata={"cost": str(cost)},
    )


def make_outcome(
    oc_id: str,
    status: OutcomeStatus = OutcomeStatus.SUCCESS,
    units_sold: int = 10,
    revenue: Decimal = Decimal("100000"),
    canc: int = 1,
    ret: int = 0,
    mission_id: str = "miss-100",
    decision_id: str = "dec-100",
    action_id: str = "act-100",
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
            "units_sold": units_sold,
            "revenue": str(revenue),
            "cancellations": canc,
            "returns": ret,
        },
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )


def test_i4_insufficient_data_handling():
    service = ProductPerformanceService(min_sample_threshold=3)
    period = TemporalPeriod(period_type="POINT_IN_TIME")

    # Muestra 1 sola (menor a umbral 3)
    pm = make_pm("pm-1")
    record = service.calculate_performance(
        performance_id="perf-insuff",
        product_id="prod-101",
        sku="SKU-TEST-1",
        period=period,
        product_records=[pm],
        outcome_records=[],
    )

    assert record.status == PerformanceStatus.INSUFFICIENT_DATA
    assert record.sample_count == 1
    assert record.observation_sample_count == 1
    assert record.outcome_sample_count == 0


def test_i4_valid_performance_observed_and_derived_metrics():
    service = ProductPerformanceService(min_sample_threshold=1)
    period = TemporalPeriod(period_type="LIFETIME")

    pm1 = make_pm("pm-1", sold_qty=5, price=Decimal("10000"), cost=Decimal("6000"))
    oc1 = make_outcome("oc-1", status=OutcomeStatus.SUCCESS, units_sold=10, revenue=Decimal("100000"), canc=1, ret=1)
    oc2 = make_outcome("oc-2", status=OutcomeStatus.FAILURE, units_sold=0, revenue=Decimal("0"), canc=0, ret=0)

    record = service.calculate_performance(
        performance_id="perf-valid",
        product_id="prod-101",
        sku="SKU-TEST-1",
        period=period,
        product_records=[pm1],
        outcome_records=[oc1, oc2],
    )

    assert record.status == PerformanceStatus.SUFFICIENT_DATA
    assert record.sample_count == 3
    assert record.observation_sample_count == 1
    assert record.outcome_sample_count == 2

    # Observed Metrics
    # Ventas: 5 + 10 = 15
    assert record.observed_metrics.observed_sales_units == 15
    # Revenue: 50000 + 100000 = 150000
    assert record.observed_metrics.observed_revenue == Decimal("150000")
    assert record.observed_metrics.observed_cancellations_units == 1
    assert record.observed_metrics.observed_returns_units == 1
    assert record.observed_metrics.observed_price == Decimal("10000")
    assert record.observed_metrics.observed_cost == Decimal("6000")

    # Derived Metrics
    # Average selling price: 150000 / 15 = 10000
    assert record.derived_metrics.average_selling_price == Decimal("10000")
    # Gross margin amount: 10000 - 6000 = 4000
    assert record.derived_metrics.gross_margin_amount == Decimal("4000")
    # Gross margin percentage: 4000 / 10000 = 0.40
    assert record.derived_metrics.gross_margin_percentage == 0.40
    # Cancellation rate: 1 / 15 = 0.0667
    assert record.derived_metrics.cancellation_rate == pytest.approx(0.0667, abs=1e-3)
    # Return rate: 1 / 15 = 0.0667
    assert record.derived_metrics.return_rate == pytest.approx(0.0667, abs=1e-3)
    # Outcome success rate: 1 SUCCESS de 2 válidos = 0.50
    assert record.derived_metrics.outcome_success_rate == 0.50


def test_i4_missing_cost_no_margin_derivation():
    service = ProductPerformanceService(min_sample_threshold=1)
    period = TemporalPeriod(period_type="POINT_IN_TIME")

    # PM sin metadato de costo
    pm = ProductMemoryRecord(
        product_memory_id="pm-no-cost",
        sku="SKU-TEST-2",
        external_id="MLC999",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="No Cost Product",
        category="CAT",
        price_amount=Decimal("15000"),
        sold_quantity=2,
    )

    record = service.calculate_performance(
        performance_id="perf-no-cost",
        product_id="prod-102",
        sku="SKU-TEST-2",
        period=period,
        product_records=[pm],
    )

    assert record.observed_metrics.observed_cost is None
    # No inventar costo ni calcular margen inventado
    assert record.derived_metrics.gross_margin_amount is None
    assert record.derived_metrics.gross_margin_percentage is None


def test_i4_causal_links_and_calibration_context():
    service = ProductPerformanceService(min_sample_threshold=1)
    period = TemporalPeriod(period_type="POINT_IN_TIME")

    pm = make_pm("pm-1")
    oc = make_outcome("oc-1", mission_id="m-55", decision_id="d-55")

    cal_ctx = DecisionCalibrationRecord(
        calibration_id="cal-ctx-1",
        decision_id="d-55",
        mission_id="m-55",
        status=CalibrationStatus.WELL_CALIBRATED,
        calibration_error=0.05,
    )

    record = service.calculate_performance(
        performance_id="perf-causal",
        product_id="prod-101",
        sku="SKU-TEST-1",
        period=period,
        product_records=[pm],
        outcome_records=[oc],
        calibration_context=cal_ctx,
    )

    assert "pm-1" in record.product_memory_ids
    assert "oc-1" in record.outcome_ids
    assert "m-55" in record.mission_ids
    assert "d-55" in record.decision_ids
    assert record.calibration_context_id == "cal-ctx-1"
    assert record.contextual_prediction_error == 0.05


def test_i4_deterministic_recomputation_and_persistence(tmp_path: Path):
    db_file = tmp_path / "product_performance.json"
    repo = JsonProductPerformanceRepository(db_file)
    service = ProductPerformanceService(performance_repo=repo, min_sample_threshold=1)

    period = TemporalPeriod(period_type="DAILY")
    pm = make_pm("pm-1", sold_qty=10)
    oc = make_outcome("oc-1", units_sold=5)

    rec1 = service.calculate_performance(
        performance_id="perf-det-1",
        product_id="prod-101",
        sku="SKU-TEST-1",
        period=period,
        product_records=[pm],
        outcome_records=[oc],
        idempotency_key="idemp-i4-1",
    )

    rec2 = service.calculate_performance(
        performance_id="perf-det-dupe",
        product_id="prod-101",
        sku="SKU-TEST-1",
        period=period,
        product_records=[pm],
        outcome_records=[oc],
        idempotency_key="idemp-i4-1",
    )

    assert rec1.performance_id == rec2.performance_id
    assert rec1.observed_metrics.observed_sales_units == rec2.observed_metrics.observed_sales_units

    # Reload repo
    repo2 = JsonProductPerformanceRepository(db_file)
    loaded = repo2.get_performance_by_id("perf-det-1")
    assert loaded is not None
    assert loaded.performance_id == "perf-det-1"
    assert loaded.product_id == "prod-101"
    assert loaded.observed_metrics.observed_sales_units == 15


def test_i4_sensitive_data_exclusion(tmp_path: Path):
    db_file = tmp_path / "sensitive_perf.json"
    repo = JsonProductPerformanceRepository(db_file)
    service = ProductPerformanceService(performance_repo=repo, min_sample_threshold=1)

    period = TemporalPeriod(period_type="POINT_IN_TIME")
    pm = make_pm("pm-1")

    record = service.calculate_performance(
        performance_id="perf-sensitive",
        product_id="prod-101",
        sku="SKU-TEST-1",
        period=period,
        product_records=[pm],
        metadata={"user": "admin", "secret_key": "topsecret", "token": "abc123token"},
    )

    loaded = repo.get_performance_by_id("perf-sensitive")
    assert loaded is not None
    assert "secret_key" not in loaded.metadata
    assert "token" not in loaded.metadata
    assert loaded.metadata.get("user") == "admin"
