"""
Tests unitarios exhaustivos para P.7 — Monitoring (Production Metrics & Operational Visibility).

Cubre:
1. Request count aggregation.
2. Success count aggregation.
3. Error count aggregation.
4. Error rate calculation (deterministic percentage).
5. Latency aggregation (min, max, avg).
6. Deterministic p50 / p95 calculation.
7. UNKNOWN != ZERO preservation (value=None when no samples exist).
8. Environment isolation (DEV vs STAGING vs PROD metrics separate).
9. Safe route template normalization (avoiding high-cardinality IDs).
10. High-cardinality or sensitive label rejection.
11. Database availability & latency metrics.
12. Health check (P.6) result projections (liveness/readiness failures).
13. Backup (P.4) and Disaster Recovery (P.5) status projections.
14. Sensitive data / PII exclusion from monitoring outputs.
15. Non-fatal monitoring failures (failure safety).
16. Strict scope boundary: No P.8 Alerting components implemented.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
import pytest

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.health.models import (
    DependencyCheckResult,
    DependencyClassification,
    HealthStatus,
    LivenessResult,
    ReadinessResult,
)
from src.domain.backup.models import (
    BackupExecutionResult,
    BackupFormat,
    BackupMetadata,
    BackupStatus,
)
from src.domain.disaster_recovery.models import (
    DisasterRecoveryExecutionResult,
    DisasterRecoveryPlan,
    DisasterScenarioType,
    RecoveryPhase,
    RecoveryStatus,
)
from src.domain.monitoring.models import (
    DISALLOWED_HIGH_CARDINALITY_LABELS,
    MetricSample,
    MetricSeries,
    MetricType,
    MetricUnit,
    MetricWindow,
    MonitoringConfigurationError,
    MonitoringMetric,
    MonitoringScope,
    ProductionMonitoringSnapshot,
    calculate_percentile,
    sanitize_route_template,
    validate_metric_labels,
)
from src.domain.reliability.ports import ClockPort
from src.infrastructure.persistence.data.json.metric_repository import InMemoryMetricRepository
from src.application.monitoring.production_monitoring_service import ProductionMonitoringService


class FakeClock(ClockPort):
    def __init__(self, initial_time: datetime) -> None:
        self._current_time = initial_time

    def now(self) -> datetime:
        return self._current_time

    def advance(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 9, 12, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def clock(base_time: datetime) -> FakeClock:
    return FakeClock(base_time)


@pytest.fixture
def metric_repo() -> InMemoryMetricRepository:
    return InMemoryMetricRepository()


@pytest.fixture
def monitoring_service(
    metric_repo: InMemoryMetricRepository, clock: FakeClock
) -> ProductionMonitoringService:
    return ProductionMonitoringService(
        repository=metric_repo,
        environment=ApplicationEnvironment.PRODUCTION,
        clock=clock,
    )


# 1. Request count
def test_p7_request_count_aggregation(
    monitoring_service: ProductionMonitoringService,
) -> None:
    monitoring_service.record_request_metric("GET", "/api/v1/orders", 200, 15.2)
    monitoring_service.record_request_metric("POST", "/api/v1/orders", 201, 45.0)
    monitoring_service.record_request_metric("GET", "/api/v1/orders/123", 404, 5.1)

    metric = monitoring_service.get_metric(MetricType.REQUEST_COUNT, MetricWindow.WINDOW_5M)
    assert metric.sample_count == 3
    assert metric.value == 3
    assert metric.unit == MetricUnit.COUNT
    assert not metric.is_unknown


# 2. Success count
def test_p7_success_count_aggregation(
    monitoring_service: ProductionMonitoringService,
) -> None:
    monitoring_service.record_request_metric("GET", "/api/v1/products", 200, 10.0)
    monitoring_service.record_request_metric("POST", "/api/v1/products", 201, 20.0)
    monitoring_service.record_request_metric("DELETE", "/api/v1/products/45", 500, 50.0)

    metric = monitoring_service.get_metric(MetricType.SUCCESS_COUNT, MetricWindow.WINDOW_5M)
    assert metric.sample_count == 2
    assert metric.value == 2


# 3. Error count
def test_p7_error_count_aggregation(
    monitoring_service: ProductionMonitoringService,
) -> None:
    monitoring_service.record_request_metric("GET", "/health", 200, 2.0)
    monitoring_service.record_request_metric("GET", "/bad-route", 404, 3.0)
    monitoring_service.record_request_metric("POST", "/checkout", 500, 120.0)

    metric = monitoring_service.get_metric(MetricType.ERROR_COUNT, MetricWindow.WINDOW_5M)
    assert metric.sample_count == 2
    assert metric.value == 2


# 4. Error rate
def test_p7_error_rate_calculation(
    monitoring_service: ProductionMonitoringService,
) -> None:
    # 4 peticiones: 3 exitosas, 1 error -> 25% error rate
    monitoring_service.record_request_metric("GET", "/route1", 200, 10.0)
    monitoring_service.record_request_metric("GET", "/route2", 200, 12.0)
    monitoring_service.record_request_metric("GET", "/route3", 200, 14.0)
    monitoring_service.record_request_metric("GET", "/route4", 500, 25.0)

    rate_metric = monitoring_service.get_error_rate(MetricWindow.WINDOW_5M)
    assert rate_metric.value == 25.0
    assert rate_metric.unit == MetricUnit.PERCENT


# 5. Latency aggregation
def test_p7_latency_aggregation(
    monitoring_service: ProductionMonitoringService,
) -> None:
    monitoring_service.record_request_metric("GET", "/api/a", 200, 10.0)
    monitoring_service.record_request_metric("GET", "/api/b", 200, 20.0)
    monitoring_service.record_request_metric("GET", "/api/c", 200, 30.0)

    metric = monitoring_service.get_metric(MetricType.LATENCY_MS, MetricWindow.WINDOW_5M)
    assert metric.sample_count == 3
    assert metric.min_value == 10.0
    assert metric.max_value == 30.0
    assert metric.avg_value == 20.0


# 6. p95 deterministic
def test_p7_p95_deterministic_calculation(
    monitoring_service: ProductionMonitoringService,
) -> None:
    for lat in range(1, 101):  # 1ms to 100ms
        monitoring_service.record_request_metric("GET", "/items", 200, float(lat))

    metric = monitoring_service.get_metric(MetricType.LATENCY_MS, MetricWindow.WINDOW_5M)
    assert metric.sample_count == 100
    assert metric.min_value == 1.0
    assert metric.max_value == 100.0
    assert metric.p50_value == pytest.approx(50.5, rel=1e-2)
    assert metric.p95_value == pytest.approx(95.05, rel=1e-2)


# 7. UNKNOWN preserved
def test_p7_unknown_preserved_when_no_samples(
    monitoring_service: ProductionMonitoringService,
) -> None:
    # Sin tráfico registrado
    metric = monitoring_service.get_metric(MetricType.LATENCY_MS, MetricWindow.WINDOW_5M)
    assert metric.sample_count == 0
    assert metric.value is None
    assert metric.avg_value is None
    assert metric.is_unknown

    error_rate = monitoring_service.get_error_rate(MetricWindow.WINDOW_5M)
    assert error_rate.sample_count == 0
    assert error_rate.value is None
    assert error_rate.is_unknown

    db_metric = monitoring_service.get_metric(MetricType.DB_AVAILABILITY, MetricWindow.WINDOW_5M)
    assert db_metric.is_unknown
    assert db_metric.value is None


# 8. Environment isolation
def test_p7_environment_isolation(
    metric_repo: InMemoryMetricRepository, clock: FakeClock
) -> None:
    prod_service = ProductionMonitoringService(
        repository=metric_repo,
        environment=ApplicationEnvironment.PRODUCTION,
        clock=clock,
    )
    dev_service = ProductionMonitoringService(
        repository=metric_repo,
        environment=ApplicationEnvironment.DEVELOPMENT,
        clock=clock,
    )

    prod_service.record_request_metric("GET", "/prod/orders", 200, 15.0)
    dev_service.record_request_metric("GET", "/dev/test", 500, 100.0)

    prod_metric = prod_service.get_metric(MetricType.REQUEST_COUNT, MetricWindow.WINDOW_5M)
    dev_metric = dev_service.get_metric(MetricType.REQUEST_COUNT, MetricWindow.WINDOW_5M)

    assert prod_metric.value == 1
    assert dev_metric.value == 1

    # Dev error count should not pollute prod
    prod_errors = prod_service.get_metric(MetricType.ERROR_COUNT, MetricWindow.WINDOW_5M)
    dev_errors = dev_service.get_metric(MetricType.ERROR_COUNT, MetricWindow.WINDOW_5M)

    assert prod_errors.is_unknown
    assert dev_errors.value == 1


# 9. Safe route template
def test_p7_safe_route_template_normalization() -> None:
    assert (
        sanitize_route_template("/admin/tenants/01918a20-80d4-7264-a99f-e3c79cbf208a/users")
        == "/admin/tenants/{id}/users"
    )
    assert sanitize_route_template("/api/v1/orders/12345/items") == "/api/v1/orders/{id}/items"
    assert sanitize_route_template("/git/commit/abcdef0123456789abcdef") == "/git/commit/{hash}"
    assert sanitize_route_template("/api/users?token=secret123") == "/api/users"


# 10. High-cardinality labels rejected
def test_p7_high_cardinality_labels_rejected() -> None:
    for bad_key in DISALLOWED_HIGH_CARDINALITY_LABELS:
        with pytest.raises(MonitoringConfigurationError):
            validate_metric_labels({bad_key: "value123"})


# 11. DB availability metric
def test_p7_db_availability_metric(
    monitoring_service: ProductionMonitoringService,
) -> None:
    monitoring_service.record_database_check(is_available=True, latency_ms=4.2)
    monitoring_service.record_database_check(is_available=True, latency_ms=3.8)

    metric = monitoring_service.get_metric(MetricType.DB_AVAILABILITY, MetricWindow.WINDOW_5M)
    assert metric.value == 1
    assert metric.unit == MetricUnit.BOOLEAN

    lat_metric = monitoring_service.get_metric(MetricType.DB_QUERY_LATENCY, MetricWindow.WINDOW_5M)
    assert lat_metric.avg_value == pytest.approx(4.0, rel=1e-2)


# 12. Health projection
def test_p7_health_projection(
    monitoring_service: ProductionMonitoringService,
) -> None:
    # Simular falla de readiness en dependencia crítica de storage
    dep_fail = DependencyCheckResult(
        name="storage",
        classification=DependencyClassification.CRITICAL,
        status=HealthStatus.UNHEALTHY,
        message="disk full",
    )
    readiness_fail = ReadinessResult(
        status=HealthStatus.UNHEALTHY,
        service="autonomous-commerce",
        version="1.0.0",
        environment="production",
        checks=(dep_fail,),
    )

    monitoring_service.record_health_check_result(readiness_fail)

    metric = monitoring_service.get_metric(MetricType.READINESS_FAILURE_COUNT, MetricWindow.WINDOW_5M)
    assert metric.sample_count == 2  # 1 overall readiness failure + 1 dependency failure
    assert metric.value == 2


# 13. Backup / DR projection
def test_p7_backup_and_dr_projection(
    monitoring_service: ProductionMonitoringService,
) -> None:
    # Proyección de Backup P.4
    meta = BackupMetadata(
        backup_id="backup_20260912_001",
        environment=ApplicationEnvironment.PRODUCTION,
        database_name="commerce_prod",
        created_at=datetime.now(timezone.utc),
        postgres_version="16.0",
        migration_revision="001",
        backup_format=BackupFormat.CUSTOM,
        file_size_bytes=1024,
        checksum_sha256="a" * 64,
        file_name="backup_20260912_001.dump",
    )
    backup_res = BackupExecutionResult(
        status=BackupStatus.COMPLETED,
        backup_path=Path("/tmp/backup.dump"),
        metadata_path=Path("/tmp/backup.json"),
        metadata=meta,
        duration_seconds=2.5,
    )
    monitoring_service.record_backup_result(backup_res)

    backup_metric = monitoring_service.get_metric(MetricType.BACKUP_STATUS, MetricWindow.WINDOW_5M)
    assert backup_metric.value == "completed"

    # Proyección de DR P.5
    plan = DisasterRecoveryPlan(
        plan_id="dr_plan_001",
        scenario=DisasterScenarioType.DATABASE_CORRUPTION,
        environment=ApplicationEnvironment.PRODUCTION,
        source_database="commerce_prod",
        recovery_target="commerce_prod_recovery",
        backup_metadata=meta,
        estimated_rpo_seconds=120.0,
        estimated_rto_seconds=300.0,
        steps=("restore", "verify"),
        created_at=datetime.now(timezone.utc),
    )
    dr_res = DisasterRecoveryExecutionResult(
        execution_id="exec_001",
        plan=plan,
        status=RecoveryStatus.COMPLETED,
        target_connection_verified=True,
        backup_verified=True,
        restore_verified=True,
        migration_revision_verified=True,
        restored_revision="001",
        expected_revision="001",
        tables_restored_count=10,
        expected_tables_count=10,
        tenants_restored_count=2,
        actual_rpo_seconds=120.0,
        actual_rto_seconds=45.0,
        rpo_compliant=True,
        rto_compliant=True,
        cleanup_successful=True,
        completed_at=datetime.now(timezone.utc),
    )
    monitoring_service.record_disaster_recovery_result(dr_res)

    dr_metric = monitoring_service.get_metric(MetricType.DR_STATUS, MetricWindow.WINDOW_5M)
    assert dr_metric.value == "completed"


# 14. Sensitive data excluded
def test_p7_sensitive_data_excluded(
    monitoring_service: ProductionMonitoringService,
) -> None:
    # Registrar tráfico con posibles vectores sensibles en URL
    monitoring_service.record_request_metric(
        method="POST",
        path="/auth/login?password=supersecret&token=abc",
        status_code=200,
        duration_ms=12.0,
    )

    snapshot = monitoring_service.get_snapshot(MetricWindow.WINDOW_5M)
    snap_dict = snapshot.to_dict()

    # Verificar que no hay password ni token en el payload serializado
    serialized = str(snap_dict)
    assert "supersecret" not in serialized
    assert "token=abc" not in serialized
    assert "password" not in serialized


# 15. Monitoring failure non-fatal
def test_p7_monitoring_failure_non_fatal(
    clock: FakeClock,
) -> None:
    # Mock de repositorio fallido
    class BrokenRepo(InMemoryMetricRepository):
        def record_samples(self, samples):
            raise RuntimeError("Database disk failure in telemetry repo")

    broken_svc = ProductionMonitoringService(
        repository=BrokenRepo(),
        environment=ApplicationEnvironment.PRODUCTION,
        clock=clock,
    )

    # Debe capturar y no levantar excepción, marcando status como degraded
    broken_svc.record_request_metric("GET", "/test", 200, 5.0)
    assert broken_svc._collector_status == "degraded"


# 16. No P.8+ implemented
def test_p7_no_p8_alerting_implemented() -> None:
    # Confirmar que no hay módulos ni imports de P.8 Alerting, Paging, SMS, etc.
    import sys
    assert "src.application.monitoring.alert_pager_service" not in sys.modules
    assert "src.application.monitoring.sms_alerts" not in sys.modules
    assert "src.domain.monitoring.auto_remediation" not in sys.modules
