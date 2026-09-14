"""
Tests de Integración para P.7 — Monitoring (Production Metrics, Time-Series Status & Operational Visibility).

Escenarios evaluados:
A. Normal HTTP traffic → metrics recorded (request count, success count, latency).
B. 4xx/5xx HTTP traffic → classified correctly (error count, error rate).
C. PostgreSQL healthy → DB availability metric is healthy/1.0 and latency recorded.
D. Controlled DB failure → monitoring shows failure/unknown/degraded safely.
E. P.6 readiness failure → monitoring history and failure count reflects event.
F. O.5 inference facts / model traffic → AI provider metrics visible in monitoring.
G. P.4 backup status → visible in monitoring metrics.
H. P.5 DR status → visible in monitoring metrics.
I. DEV/STAGING/PROD environments strictly isolated in metrics repository.
J. No secrets/PII/sensitive credentials in monitoring payload/snapshot outputs.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from starlette.testclient import TestClient

from src.application.monitoring.production_monitoring_service import ProductionMonitoringService
from src.domain.backup.models import (
    BackupExecutionResult,
    BackupFormat,
    BackupMetadata,
    BackupStatus,
)
from src.domain.deployment.models import ApplicationEnvironment, DeploymentConfig
from src.domain.disaster_recovery.models import (
    DisasterRecoveryExecutionResult,
    DisasterRecoveryPlan,
    DisasterScenarioType,
    RecoveryPhase,
    RecoveryStatus,
)
from src.domain.health.models import (
    DependencyCheckResult,
    DependencyClassification,
    HealthStatus,
    LivenessResult,
    ReadinessResult,
)
from src.domain.monitoring.models import (
    MetricSample,
    MetricType,
    MetricUnit,
    MetricWindow,
    MonitoringScope,
)
from src.infrastructure.health.service import HealthCheckService
from src.infrastructure.persistence.data.json.metric_repository import (
    InMemoryMetricRepository,
    JsonMetricRepository,
)
from src.infrastructure.persistence.database.config import DatabaseConfig, DatabaseConnectionFactory
from src.infrastructure.web.app import create_platform_app


class TestP7MonitoringIntegration(unittest.TestCase):
    """Suite de integración para P.7 Monitoring."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.metric_repo = InMemoryMetricRepository()
        self.monitoring_service = ProductionMonitoringService(
            repository=self.metric_repo,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        self.deployment_config = DeploymentConfig(
            environment=ApplicationEnvironment.PRODUCTION,
            port=8000,
            host="127.0.0.1",
            data_dir=str(self.data_dir),
            app_version="1.0.0",
            readiness_probes_enabled=True,
            liveness_probes_enabled=True,
        )
        self.app = create_platform_app(
            config=self.deployment_config,
            monitoring_service=self.monitoring_service,
        )
        self.client = TestClient(self.app)

        # Cargar configuración de DB real si existe
        self.db_config = None
        try:
            self.db_config = DatabaseConfig.from_env()
            factory = DatabaseConnectionFactory(self.db_config)
            with factory.create_connection() as conn:
                pass
        except Exception:
            self.db_config = None

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # Escenario A: normal HTTP traffic → metrics recorded
    def test_scenario_a_normal_http_traffic_recorded(self) -> None:
        # Peticiones normales a endpoints existentes
        r1 = self.client.get("/health")
        r2 = self.client.get("/ready")
        self.assertEqual(r1.status_code, 200)

        # Verificar que el servicio de monitoreo registró las peticiones y latencia
        req_metric = self.monitoring_service.get_metric(MetricType.REQUEST_COUNT, MetricWindow.WINDOW_5M)
        self.assertGreaterEqual(req_metric.value, 2)

        lat_metric = self.monitoring_service.get_metric(MetricType.LATENCY_MS, MetricWindow.WINDOW_5M)
        self.assertIsNotNone(lat_metric.avg_value)
        self.assertIsNotNone(lat_metric.p95_value)
        self.assertGreater(lat_metric.avg_value, 0.0)

    # Escenario B: 4xx/5xx traffic → classified correctly
    def test_scenario_b_error_classification(self) -> None:
        # Generar 404 (4xx)
        r_404 = self.client.get("/non_existent_route_for_test")
        self.assertEqual(r_404.status_code, 404)

        # Generar 500 simulando falla en endpoint
        # Registramos directamente una falla de servidor 500
        self.monitoring_service.record_request_metric(
            method="POST",
            path="/api/fail",
            status_code=500,
            duration_ms=45.0,
        )

        err_metric = self.monitoring_service.get_metric(MetricType.ERROR_COUNT, MetricWindow.WINDOW_5M)
        self.assertGreaterEqual(err_metric.value, 2)

        err_rate = self.monitoring_service.get_error_rate(MetricWindow.WINDOW_5M)
        self.assertIsNotNone(err_rate.value)
        self.assertGreater(err_rate.value, Decimal("0.0"))

    # Escenario C: PostgreSQL healthy → DB metric healthy
    def test_scenario_c_postgresql_healthy_metric(self) -> None:
        if not self.db_config:
            self.skipTest("No PostgreSQL configuration available.")

        factory = DatabaseConnectionFactory(self.db_config)
        conn = factory.create_connection()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                res = cur.fetchone()
                self.assertEqual(res[0], 1)
        finally:
            conn.close()

        # Registrar chequeo exitoso de DB
        self.monitoring_service.record_database_check(is_available=True, latency_ms=4.2)
        db_metric = self.monitoring_service.get_metric(MetricType.DB_AVAILABILITY, MetricWindow.WINDOW_5M)
        self.assertEqual(db_metric.value, 1.0)

        lat_metric = self.monitoring_service.get_metric(MetricType.DB_QUERY_LATENCY, MetricWindow.WINDOW_5M)
        self.assertEqual(lat_metric.avg_value, 4.2)

    # Escenario D: Controlled DB failure → monitoring shows failure/unknown safely
    def test_scenario_d_controlled_db_failure(self) -> None:
        # Simular fallo de base de datos sin afectar base de datos real
        self.monitoring_service.record_database_check(
            is_available=False,
            latency_ms=None,
            error_message="Connection refused",
        )

        db_metric = self.monitoring_service.get_metric(MetricType.DB_AVAILABILITY, MetricWindow.WINDOW_5M)
        self.assertEqual(db_metric.value, 0.0)

        # Snapshot refleja DB no disponible
        snapshot = self.monitoring_service.get_snapshot(MetricWindow.WINDOW_5M)
        self.assertEqual(snapshot.metrics[MetricType.DB_AVAILABILITY.value].value, 0.0)

    # Escenario E: P.6 readiness failure → history reflects event
    def test_scenario_e_readiness_failure_history_reflected(self) -> None:
        dep_fail = DependencyCheckResult(
            name="database",
            classification=DependencyClassification.CRITICAL,
            status=HealthStatus.UNHEALTHY,
            message="Database unreachable",
        )
        readiness_fail = ReadinessResult(
            status=HealthStatus.UNHEALTHY,
            service="autonomous-commerce",
            version="1.0.0",
            environment="production",
            checks=(dep_fail,),
        )

        self.monitoring_service.record_health_check_result(readiness_fail)

        fail_metric = self.monitoring_service.get_metric(
            MetricType.READINESS_FAILURE_COUNT,
            MetricWindow.WINDOW_5M,
        )
        self.assertGreaterEqual(fail_metric.value, 1)

    # Escenario F: O.5 inference facts → provider metrics visible
    def test_scenario_f_ai_provider_metrics_visible(self) -> None:
        self.monitoring_service.record_ai_inference_metrics(
            provider="openai",
            model="gpt-4o",
            total_tokens=200,
            cost_usd=Decimal("0.0025"),
            latency_ms=450.0,
            is_error=False,
            tenant_id="tenant_123",
        )

        req_count = self.monitoring_service.get_metric(MetricType.MODEL_REQUEST_COUNT, MetricWindow.WINDOW_5M)
        self.assertEqual(req_count.value, 1)

        token_usage = self.monitoring_service.get_metric(MetricType.TOKEN_USAGE, MetricWindow.WINDOW_5M)
        self.assertEqual(token_usage.value, 200)

        ai_cost = self.monitoring_service.get_metric(MetricType.AI_COST, MetricWindow.WINDOW_5M)
        self.assertEqual(ai_cost.value, Decimal("0.0025"))

    # Escenario G: P.4 backup status → visible
    def test_scenario_g_backup_status_visible(self) -> None:
        meta = BackupMetadata(
            backup_id="backup_p7_001",
            environment=ApplicationEnvironment.PRODUCTION,
            database_name="commerce_prod",
            created_at=datetime.now(timezone.utc),
            postgres_version="16.0",
            migration_revision="001",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=4096,
            checksum_sha256="b" * 64,
            file_name="backup_p7_001.dump",
        )
        backup_res = BackupExecutionResult(
            status=BackupStatus.COMPLETED,
            backup_path=Path("/tmp/p7_backup.dump"),
            metadata_path=Path("/tmp/p7_backup.json"),
            metadata=meta,
            duration_seconds=1.8,
        )
        self.monitoring_service.record_backup_result(backup_res)

        b_metric = self.monitoring_service.get_metric(MetricType.BACKUP_STATUS, MetricWindow.WINDOW_5M)
        self.assertEqual(b_metric.value, "completed")

    # Escenario H: P.5 DR status → visible
    def test_scenario_h_dr_status_visible(self) -> None:
        meta = BackupMetadata(
            backup_id="backup_dr_001",
            environment=ApplicationEnvironment.PRODUCTION,
            database_name="commerce_prod",
            created_at=datetime.now(timezone.utc),
            postgres_version="16.0",
            migration_revision="001",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=2048,
            checksum_sha256="c" * 64,
            file_name="backup_dr_001.dump",
        )
        plan = DisasterRecoveryPlan(
            plan_id="dr_plan_p7",
            scenario=DisasterScenarioType.DATABASE_LOSS,
            environment=ApplicationEnvironment.PRODUCTION,
            source_database="commerce_prod",
            recovery_target="commerce_prod_recovery",
            backup_metadata=meta,
            estimated_rpo_seconds=60.0,
            estimated_rto_seconds=180.0,
            steps=("provision", "restore", "validate"),
            created_at=datetime.now(timezone.utc),
        )
        dr_res = DisasterRecoveryExecutionResult(
            execution_id="exec_dr_p7",
            plan=plan,
            status=RecoveryStatus.COMPLETED,
            target_connection_verified=True,
            backup_verified=True,
            restore_verified=True,
            migration_revision_verified=True,
            restored_revision="001",
            expected_revision="001",
            tables_restored_count=15,
            expected_tables_count=15,
            tenants_restored_count=3,
            actual_rpo_seconds=45.0,
            actual_rto_seconds=110.0,
            rpo_compliant=True,
            rto_compliant=True,
            cleanup_successful=True,
            completed_at=datetime.now(timezone.utc),
        )
        self.monitoring_service.record_disaster_recovery_result(dr_res)

        dr_metric = self.monitoring_service.get_metric(MetricType.DR_STATUS, MetricWindow.WINDOW_5M)
        self.assertEqual(dr_metric.value, "completed")

    # Escenario I: DEV/STAGING/PROD isolated
    def test_scenario_i_environment_isolation(self) -> None:
        # Registrar métricas en DEV
        self.monitoring_service.record_request_metric(
            method="GET",
            path="/dev/test",
            status_code=200,
            duration_ms=10.0,
            environment=ApplicationEnvironment.DEVELOPMENT,
        )
        # Registrar métricas en PROD
        self.monitoring_service.record_request_metric(
            method="GET",
            path="/prod/order",
            status_code=200,
            duration_ms=20.0,
            environment=ApplicationEnvironment.PRODUCTION,
        )

        dev_metric = self.monitoring_service.get_metric(
            MetricType.REQUEST_COUNT,
            MetricWindow.WINDOW_5M,
            environment=ApplicationEnvironment.DEVELOPMENT,
        )
        prod_metric = self.monitoring_service.get_metric(
            MetricType.REQUEST_COUNT,
            MetricWindow.WINDOW_5M,
            environment=ApplicationEnvironment.PRODUCTION,
        )

        self.assertEqual(dev_metric.value, 1)
        self.assertEqual(prod_metric.value, 1)

    # Escenario J: no secrets/PII in monitoring payload
    def test_scenario_j_no_secrets_or_pii_in_payload(self) -> None:
        # Consultar endpoint técnico /metrics
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        serialized = str(data)
        # Asegurar ausencia de secretos y DSN
        self.assertNotIn("password", serialized.lower())
        self.assertNotIn("secret", serialized.lower())
        self.assertNotIn("postgresql://", serialized)
        self.assertNotIn("bearer ", serialized.lower())


if __name__ == "__main__":
    unittest.main()
