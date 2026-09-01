from datetime import datetime, timezone
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
)
from src.application.strategy_performance.strategy_performance_service import StrategyPerformanceService
from src.infrastructure.persistence.data.json.strategy_performance_repository import (
    JsonStrategyPerformanceRepository,
)


def test_i6_strategy_performance_integration_e2e(tmp_path: Path):
    """
    Test de Integración E2E para Task I.6 — Strategy Performance.
    Demuestra la cadena causal completa:
    STRATEGY -> DECISIONS -> ACTIONS -> RESULTS -> OUTCOMES -> STRATEGY PERFORMANCE -> PERSIST -> RELOAD.
    """
    db_file = tmp_path / "strategy_performance_integration.json"
    repo = JsonStrategyPerformanceRepository(db_file)
    service = StrategyPerformanceService(performance_repo=repo, min_sample_threshold=1)

    now = datetime.now(timezone.utc)
    strategy_id = "STRAT-DROPSHIP-HIGH-MARGIN"

    # 1. Decisiones
    d1 = DecisionRecord(
        decision_id="dec-strat-1",
        mission_id="mission-alpha",
        decision_type=DecisionType.MARKET_OPPORTUNITY,
        status=DecisionStatus.EXECUTED,
        reason="Dropshipping opportunity high margin",
        created_at=now,
        outcome=DecisionOutcome.SUCCESS,
        parameters={"strategy_id": strategy_id, "product_id": "prod-100", "supplier_id": "supp-50"},
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    d2 = DecisionRecord(
        decision_id="dec-strat-2",
        mission_id="mission-alpha",
        decision_type=DecisionType.SUPPLIER_SELECTION,
        status=DecisionStatus.EXECUTED,
        reason="Supplier select dropshipping",
        created_at=now,
        outcome=DecisionOutcome.SUCCESS,
        parameters={"strategy_id": strategy_id, "product_id": "prod-100", "supplier_id": "supp-50"},
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    # 2. Outcomes de negocio observados
    oc1 = OutcomeRecord(
        outcome_id="oc-strat-1",
        mission_id="mission-alpha",
        decision_id="dec-strat-1",
        action_id="act-pub-100",
        result_id="res-pub-100",
        status=OutcomeStatus.SUCCESS,
        observed_at=now,
        value_metrics={
            "profit": "15000",
            "revenue": "60000",
            "cancellations": 0,
            "returns": 0,
            "product_id": "prod-100",
            "supplier_id": "supp-50",
        },
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    oc2 = OutcomeRecord(
        outcome_id="oc-strat-2",
        mission_id="mission-alpha",
        decision_id="dec-strat-2",
        action_id="act-proc-100",
        result_id="res-proc-100",
        status=OutcomeStatus.FAILURE,
        observed_at=now,
        value_metrics={
            "profit": "-2000",
            "revenue": "5000",
            "cancellations": 1,
            "returns": 0,
            "product_id": "prod-100",
            "supplier_id": "supp-50",
        },
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
    )

    # 3. Contexto de calibración (I.3)
    cal_context = DecisionCalibrationRecord(
        calibration_id="calib-ctx-strat",
        decision_id="dec-strat-1",
        mission_id="mission-alpha",
        status=CalibrationStatus.WELL_CALIBRATED,
        calibration_error=0.03,
    )

    # 4. Contexto de Product & Supplier Performance (I.4 & I.5)
    prod_perf = ProductPerformanceRecord(
        performance_id="perf-prod-100",
        product_id="prod-100",
        sku="SKU-100",
        period=TemporalPeriod(period_type="MONTHLY"),
        status=PerformanceStatus.SUFFICIENT_DATA,
    )

    supp_perf = SupplierPerformanceRecord(
        performance_id="perf-supp-50",
        supplier_id="supp-50",
        period=SupplierTemporalPeriod(period_type="MONTHLY"),
        status=SupplierPerformanceStatus.SUFFICIENT_DATA,
    )

    # 5. Ejecutar servicio de Strategy Performance
    period = StrategyTemporalPeriod(period_type="MONTHLY")
    perf_record = service.calculate_performance(
        performance_id="perf-strat-e2e-1",
        strategy_id=strategy_id,
        period=period,
        decision_records=[d1, d2],
        outcome_records=[oc1, oc2],
        calibration_context=cal_context,
        product_performance_records=[prod_perf],
        supplier_performance_records=[supp_perf],
        correlation_id="corr-strat-e2e",
        idempotency_key="idemp-strat-e2e",
    )

    # 6. Verificaciones en memoria
    assert perf_record.performance_id == "perf-strat-e2e-1"
    assert perf_record.strategy_id == strategy_id
    assert perf_record.status == StrategyPerformanceStatus.SUFFICIENT_DATA
    assert perf_record.sample_count == 4  # 2 decisiones + 2 outcomes
    assert perf_record.decision_sample_count == 2
    assert perf_record.outcome_sample_count == 2

    # Métricas observadas
    assert perf_record.observed_metrics.success_count == 1
    assert perf_record.observed_metrics.failure_count == 1
    assert perf_record.observed_metrics.observed_profit == Decimal("13000")  # 15000 + (-2000)
    assert perf_record.observed_metrics.observed_revenue == Decimal("65000")  # 60000 + 5000
    assert perf_record.observed_metrics.observed_cancellations == 1

    # Métricas derivadas
    assert perf_record.derived_metrics.success_rate == 0.5
    assert perf_record.derived_metrics.failure_rate == 0.5
    assert perf_record.derived_metrics.average_realized_profit == Decimal("13000")  # 13000 / 1 success
    assert perf_record.derived_metrics.average_realized_revenue == Decimal("32500")  # 65000 / 2
    assert perf_record.derived_metrics.average_margin_percentage == pytest.approx(0.2, abs=1e-3)  # 13000 / 65000 = 0.2

    # Trazabilidad causal y contextos
    assert "dec-strat-1" in perf_record.decision_ids
    assert "dec-strat-2" in perf_record.decision_ids
    assert "oc-strat-1" in perf_record.outcome_ids
    assert "oc-strat-2" in perf_record.outcome_ids
    assert "mission-alpha" in perf_record.mission_ids
    assert "prod-100" in perf_record.product_ids
    assert "supp-50" in perf_record.supplier_ids
    assert perf_record.calibration_context_id == "calib-ctx-strat"
    assert perf_record.contextual_prediction_error == 0.03

    # 7. Reinicio de repositorio / recarga desde disco y verificación
    reloaded_repo = JsonStrategyPerformanceRepository(db_file)
    reloaded = reloaded_repo.get_performance_by_id("perf-strat-e2e-1")

    assert reloaded is not None
    assert reloaded.performance_id == perf_record.performance_id
    assert reloaded.strategy_id == perf_record.strategy_id
    assert reloaded.observed_metrics.observed_profit == Decimal("13000")
    assert reloaded.derived_metrics.success_rate == 0.5
    assert reloaded.calibration_context_id == "calib-ctx-strat"
