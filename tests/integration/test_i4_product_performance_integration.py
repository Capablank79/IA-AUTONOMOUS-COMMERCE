from datetime import datetime, timezone
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
)
from src.application.product_performance.product_performance_service import ProductPerformanceService
from src.infrastructure.persistence.data.json.product_performance_repository import (
    JsonProductPerformanceRepository,
)


def test_i4_product_performance_integration_e2e(tmp_path: Path):
    """
    Test de Integración E2E para Task I.4 — Product Performance.
    Demuestra la cadena completa:
    PRODUCT -> PRODUCT MEMORY -> OUTCOMES -> PRODUCT PERFORMANCE -> PERSIST -> RELOAD
    con trazabilidad causal:
    MISSION -> DECISION -> ACTION -> RESULT -> OUTCOME -> PRODUCT PERFORMANCE.
    """
    db_file = tmp_path / "product_performance_integration.json"
    repo = JsonProductPerformanceRepository(db_file)
    service = ProductPerformanceService(performance_repo=repo, min_sample_threshold=1)

    # 1. Crear memorias de producto
    now = datetime.now(timezone.utc)
    pm1 = ProductMemoryRecord(
        product_memory_id="pm-e2e-1",
        sku="SKU-E2E-100",
        external_id="MLC9990001",
        marketplace=Marketplace.MERCADO_LIBRE,
        title="Teclado Mecánico RGB",
        category="COMPUTERS",
        price_amount=Decimal("45000"),
        sold_quantity=8,
        available_quantity=50,
        observed_at=now,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        metadata={"cost": "25000"},
    )

    # 2. Crear outcomes de negocio vinculados a misiones/decisiones/acciones
    oc1 = OutcomeRecord(
        outcome_id="oc-e2e-1",
        mission_id="mission-alpha",
        decision_id="decision-alpha-1",
        action_id="action-pub-1",
        result_id="result-pub-1",
        status=OutcomeStatus.SUCCESS,
        observed_at=now,
        value_metrics={"units_sold": 12, "revenue": "540000", "cancellations": 0, "returns": 1},
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    oc2 = OutcomeRecord(
        outcome_id="oc-e2e-2",
        mission_id="mission-alpha",
        decision_id="decision-alpha-2",
        action_id="action-price-1",
        result_id="result-price-1",
        status=OutcomeStatus.SUCCESS,
        observed_at=now,
        value_metrics={"units_sold": 5, "revenue": "225000", "cancellations": 1, "returns": 0},
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    # 3. Contexto de calibración opcional (I.3)
    cal_context = DecisionCalibrationRecord(
        calibration_id="calib-ctx-alpha",
        decision_id="decision-alpha-1",
        mission_id="mission-alpha",
        status=CalibrationStatus.WELL_CALIBRATED,
        calibration_error=0.04,
    )

    # 4. Ejecutar servicio de performance
    period = TemporalPeriod(period_type="MONTHLY")
    perf_record = service.calculate_performance(
        performance_id="perf-e2e-100",
        product_id="product-rgb-keyboard",
        sku="SKU-E2E-100",
        period=period,
        product_records=[pm1],
        outcome_records=[oc1, oc2],
        calibration_context=cal_context,
        correlation_id="corr-e2e-100",
        idempotency_key="idemp-e2e-100",
    )

    # 5. Verificaciones en memoria
    assert perf_record.performance_id == "perf-e2e-100"
    assert perf_record.product_id == "product-rgb-keyboard"
    assert perf_record.sku == "SKU-E2E-100"
    assert perf_record.status == PerformanceStatus.SUFFICIENT_DATA
    assert perf_record.sample_count == 3
    assert perf_record.observation_sample_count == 1
    assert perf_record.outcome_sample_count == 2

    # Verificación de métricas observadas
    # Ventas totales: 8 + 12 + 5 = 25
    assert perf_record.observed_metrics.observed_sales_units == 25
    # Revenue total: (45000 * 8 = 360000) + 540000 + 225000 = 1125000
    assert perf_record.observed_metrics.observed_revenue == Decimal("1125000")
    assert perf_record.observed_metrics.observed_cancellations_units == 1
    assert perf_record.observed_metrics.observed_returns_units == 1
    assert perf_record.observed_metrics.observed_price == Decimal("45000")
    assert perf_record.observed_metrics.observed_cost == Decimal("25000")

    # Verificación de métricas derivadas
    # Gross margin: 45000 - 25000 = 20000
    assert perf_record.derived_metrics.gross_margin_amount == Decimal("20000")
    # Gross margin pct: 20000 / 45000 = 0.4444
    assert perf_record.derived_metrics.gross_margin_percentage == pytest.approx(0.4444, abs=1e-3)
    # Average selling price: 1125000 / 25 = 45000
    assert perf_record.derived_metrics.average_selling_price == Decimal("45000")
    # Cancellation rate: 1 / 25 = 0.04
    assert perf_record.derived_metrics.cancellation_rate == 0.04
    # Return rate: 1 / 25 = 0.04
    assert perf_record.derived_metrics.return_rate == 0.04
    # Outcome success rate: 2 SUCCESS / 2 valid outcomes = 1.0
    assert perf_record.derived_metrics.outcome_success_rate == 1.0

    # Verificación de trazabilidad causal
    assert "pm-e2e-1" in perf_record.product_memory_ids
    assert "oc-e2e-1" in perf_record.outcome_ids
    assert "oc-e2e-2" in perf_record.outcome_ids
    assert "mission-alpha" in perf_record.mission_ids
    assert "decision-alpha-1" in perf_record.decision_ids
    assert perf_record.calibration_context_id == "calib-ctx-alpha"
    assert perf_record.contextual_prediction_error == 0.04

    # 6. Reinicio de repositorio / recarga desde disco y verificación
    reloaded_repo = JsonProductPerformanceRepository(db_file)
    reloaded = reloaded_repo.get_performance_by_id("perf-e2e-100")

    assert reloaded is not None
    assert reloaded.performance_id == perf_record.performance_id
    assert reloaded.product_id == perf_record.product_id
    assert reloaded.sku == perf_record.sku
    assert reloaded.observed_metrics.observed_sales_units == 25
    assert reloaded.observed_metrics.observed_revenue == Decimal("1125000")
    assert reloaded.derived_metrics.gross_margin_amount == Decimal("20000")
    assert reloaded.calibration_context_id == "calib-ctx-alpha"
