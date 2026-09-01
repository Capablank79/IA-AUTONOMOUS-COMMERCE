from datetime import datetime, timezone
from decimal import Decimal
import pytest
from pathlib import Path

from src.domain.market_intelligence.models import Confidence
from src.domain.supplier_intelligence.models import EvidenceProvenanceType, SupplierStatus
from src.domain.supplier_memory.models import SupplierMemoryRecord
from src.domain.outcome.models import OutcomeRecord, OutcomeStatus
from src.domain.supplier_performance.models import (
    SupplierPerformanceRecord,
    SupplierPerformanceStatus,
    SupplierTemporalPeriod,
)
from src.application.supplier_performance.supplier_performance_service import SupplierPerformanceService
from src.infrastructure.persistence.data.json.supplier_performance_repository import (
    JsonSupplierPerformanceRepository,
)


def test_i5_supplier_performance_integration_e2e(tmp_path: Path):
    """
    Demostración E2E completa del flujo de Supplier Performance (Task I.5):
    SUPPLIER -> SUPPLIER MEMORY (H.6) -> OUTCOMES (I.1) -> SUPPLIER PERFORMANCE (I.5) -> PERSIST -> RELOAD
    
    Verifica también trazabilidad causal completa:
    MISSION -> DECISION -> ACTION -> RESULT -> OUTCOME -> SUPPLIER PERFORMANCE
    """
    db_path = tmp_path / "supplier_performance_e2e.json"
    repo = JsonSupplierPerformanceRepository(db_path)
    service = SupplierPerformanceService(performance_repo=repo, min_sample_threshold=1)

    # 1. Simular Memorias de Proveedor (H.6)
    sm1 = SupplierMemoryRecord(
        supplier_memory_id="sm-e2e-1",
        supplier_id="SUP-GLOBAL-01",
        name="Global Logistics & Sourcing",
        status=SupplierStatus.VERIFIED,
        cost_amount=Decimal("12500"),
        moq=50,
        lead_time_days=7,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        metadata={"quote_accepted": True},
    )

    sm2 = SupplierMemoryRecord(
        supplier_memory_id="sm-e2e-2",
        supplier_id="SUP-GLOBAL-01",
        name="Global Logistics & Sourcing",
        status=SupplierStatus.VERIFIED,
        cost_amount=Decimal("11800"),
        moq=100,
        lead_time_days=6,
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        metadata={"quote_accepted": False},
    )

    # 2. Simular Outcomes reales de negocio observados (I.1)
    oc1 = OutcomeRecord(
        outcome_id="oc-e2e-1",
        mission_id="m-e2e-100",
        decision_id="d-e2e-100",
        action_id="a-e2e-100",
        result_id="r-e2e-100",
        status=OutcomeStatus.SUCCESS,
        value_metrics={
            "order_placed": True,
            "fulfilled": True,
            "on_time": True,
            "cancelled": False,
            "defective": False,
            "cost_amount": "12500",
            "moq": 50,
            "lead_time_days": 7,
        },
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        correlation_id="corr-e2e-1",
        idempotency_key="idemp-e2e-oc1",
    )

    oc2 = OutcomeRecord(
        outcome_id="oc-e2e-2",
        mission_id="m-e2e-100",
        decision_id="d-e2e-100",
        action_id="a-e2e-101",
        result_id="r-e2e-101",
        status=OutcomeStatus.FAILURE,
        value_metrics={
            "order_placed": True,
            "fulfilled": False,
            "on_time": False,
            "cancelled": True,
            "defective": False,
        },
        confidence=Confidence.HIGH,
        provenance=EvidenceProvenanceType.LIVE,
        correlation_id="corr-e2e-1",
        idempotency_key="idemp-e2e-oc2",
    )

    # 3. Calcular Performance de Proveedor determinista (I.5)
    period = SupplierTemporalPeriod(period_type="MONTHLY")
    record = service.calculate_performance(
        performance_id="perf-e2e-001",
        supplier_id="SUP-GLOBAL-01",
        period=period,
        supplier_records=[sm1, sm2],
        outcome_records=[oc1, oc2],
        correlation_id="corr-e2e-1",
        idempotency_key="idemp-e2e-perf-001",
        metadata={"evaluated_by": "autonomous_learning_loop"},
    )

    # 4. Validar resultado en memoria
    assert record.status == SupplierPerformanceStatus.SUFFICIENT_DATA
    assert record.sample_count == 4
    assert record.quote_sample_count == 2
    assert record.outcome_sample_count == 2

    # Métricas derivadas
    assert record.derived_metrics.quote_acceptance_rate == 0.50
    assert record.derived_metrics.average_quoted_cost == Decimal("12150")  # (12500+11800)/2
    assert record.derived_metrics.average_moq == 66.67                     # (50+100+50)/3
    assert record.derived_metrics.average_lead_time_days == 6.67           # (7+6+7)/3
    assert record.derived_metrics.fulfillment_rate == 0.50
    assert record.derived_metrics.delivery_on_time_rate == 0.50
    assert record.derived_metrics.cancellation_rate == 0.50
    assert record.derived_metrics.outcome_success_rate == 0.50

    # Trazabilidad causal estricta
    assert record.supplier_memory_ids == ("sm-e2e-1", "sm-e2e-2")
    assert record.outcome_ids == ("oc-e2e-1", "oc-e2e-2")
    assert record.mission_ids == ("m-e2e-100",)
    assert record.decision_ids == ("d-e2e-100",)
    assert record.action_ids == ("a-e2e-100", "a-e2e-101")

    # 5. Probar Persistencia y Reinicio/Recarga (RELOAD)
    repo_reloaded = JsonSupplierPerformanceRepository(db_path)
    loaded_record = repo_reloaded.get_by_id("perf-e2e-001")
    assert loaded_record is not None
    assert loaded_record.performance_id == "perf-e2e-001"
    assert loaded_record.supplier_id == "SUP-GLOBAL-01"
    assert loaded_record.derived_metrics.average_quoted_cost == Decimal("12150")
    assert loaded_record.supplier_memory_ids == ("sm-e2e-1", "sm-e2e-2")
    assert loaded_record.outcome_ids == ("oc-e2e-1", "oc-e2e-2")

    # 6. Probar Recomputación Determinista e Idempotencia
    duplicate_record = service.calculate_performance(
        performance_id="perf-e2e-001-dupe",
        supplier_id="SUP-GLOBAL-01",
        period=period,
        supplier_records=[sm1, sm2],
        outcome_records=[oc1, oc2],
        correlation_id="corr-e2e-1",
        idempotency_key="idemp-e2e-perf-001",
    )

    assert duplicate_record.performance_id == "perf-e2e-001"
