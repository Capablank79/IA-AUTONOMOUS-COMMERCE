"""
Tests de Integración para P.8 — Alerting (Production Alert Rules, Deduplication, Escalation & Safe Notification Contracts).

Escenarios evaluados:
A. High error rate → exactly one alert generated.
B. Repeated evaluation → no duplicate alert created (canonical deduplication).
C. DB unavailable (P.6 + P.7 failure) → CRITICAL alert.
D. DB restored (healthy with evidence) → alert automatically resolved.
E. p95 latency exceeds threshold → HIGH_LATENCY alert.
F. Backup stale / failed → BACKUP_STALE_OR_FAILED alert.
G. DR failure evidence → DR_LAST_SIMULATION_FAILED alert.
H. Multi-tenant isolation: Tenant A alerts inaccessible to Tenant B.
I. Notification adapter failure → non-fatal, incident remains active and valid.
J. Admin console lifecycle: trigger → acknowledge → recovery → resolve → audited.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
from typing import List, Optional
import unittest
from unittest.mock import patch, MagicMock

from starlette.testclient import TestClient

from src.application.monitoring.production_monitoring_service import (
    ProductionMonitoringService,
)
from src.application.production_alerting.production_alerting_service import (
    ProductionAlertingService,
)
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
    ReadinessResult,
)
from src.domain.monitoring.models import (
    MetricSample,
    MetricType,
    MetricUnit,
    MetricWindow,
    MonitoringScope,
)
from src.domain.production_alerting.models import (
    AlertEvaluationStatus,
    AlertRule,
    AlertRuleType,
    AlertSeverity,
    AlertState,
    NotificationDeliveryStatus,
    NotificationMessage,
    NotificationResult,
    ProductionAlertInstance,
    ProductionAlertScope,
    generate_deduplication_key,
)
from src.domain.production_alerting.ports import NotificationPort
from src.infrastructure.persistence.data.json.metric_repository import (
    InMemoryMetricRepository,
)
from src.infrastructure.persistence.data.json.production_alert_repository import (
    JsonProductionAlertRepository,
)
from src.infrastructure.web.app import create_platform_app


class MockNotificationAdapter(NotificationPort):
    def __init__(self, channel_name: str = "mock_ops", should_fail: bool = False) -> None:
        self._channel = channel_name
        self.should_fail = should_fail
        self.sent_messages: List[NotificationMessage] = []

    @property
    def channel_name(self) -> str:
        return self._channel

    def send(self, message: NotificationMessage) -> NotificationResult:
        if self.should_fail:
            return NotificationResult(
                notification_id=message.notification_id,
                channel=self._channel,
                status=NotificationDeliveryStatus.FAILED,
                error_message="Simulated notification delivery timeout",
            )
        self.sent_messages.append(message)
        return NotificationResult(
            notification_id=message.notification_id,
            channel=self._channel,
            status=NotificationDeliveryStatus.SENT,
        )


class TestP8AlertingIntegration(unittest.TestCase):
    """Suite de integración y E2E para P.8 Alerting."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)

        self.metric_repo = InMemoryMetricRepository()
        self.monitoring_service = ProductionMonitoringService(
            repository=self.metric_repo,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        self.alert_repo = JsonProductionAlertRepository(
            alerts_base_dir=self.data_dir / "alerts"
        )
        self.notifier = MockNotificationAdapter(channel_name="ops_channel")

        self.alerting_service = ProductionAlertingService(
            repository=self.alert_repo,
            monitoring_service=self.monitoring_service,
            notification_ports=[self.notifier],
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

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # Escenario A: high error rate → one alert
    def test_scenario_a_high_error_rate_generates_one_alert(self) -> None:
        # Generar tráfico con 20% de error rate (> 5.0% threshold)
        for _ in range(8):
            self.monitoring_service.record_request_metric(
                method="GET", path="/api/v1/resource", status_code=200, duration_ms=25.0
            )
        for _ in range(2):
            self.monitoring_service.record_request_metric(
                method="GET", path="/api/v1/resource", status_code=500, duration_ms=50.0
            )

        alerts = self.alerting_service.evaluate_all_rules()
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.rule_type, AlertRuleType.HIGH_ERROR_RATE)
        self.assertEqual(alert.severity, AlertSeverity.HIGH)
        self.assertEqual(alert.state, AlertState.ACTIVE)
        self.assertGreaterEqual(len(self.notifier.sent_messages), 1)

    # Escenario B: repeated evaluation → no duplicate
    def test_scenario_b_repeated_evaluation_no_duplicate(self) -> None:
        for _ in range(8):
            self.monitoring_service.record_request_metric("GET", "/api/data", 200, 20.0)
        for _ in range(2):
            self.monitoring_service.record_request_metric("GET", "/api/data", 500, 30.0)

        # Primera evaluación: crea alerta
        first_eval = self.alerting_service.evaluate_all_rules()
        self.assertEqual(len(first_eval), 1)

        # Segunda evaluación repetida en la misma ventana
        second_eval = self.alerting_service.evaluate_all_rules()
        active_alerts = self.alerting_service.list_alerts(state=AlertState.ACTIVE)

        # Debe haber exactamente 1 alerta activa, sin duplicados
        self.assertEqual(len(active_alerts), 1)
        self.assertEqual(active_alerts[0].alert_id, first_eval[0].alert_id)

    # Escenario C: DB unavailable → critical alert
    def test_scenario_c_db_unavailable_critical_alert(self) -> None:
        # Simular fallo en DB availability
        self.monitoring_service.record_database_check(
            is_available=False,
            latency_ms=1200.0,
            error_message="Connection refused",
        )

        alerts = self.alerting_service.evaluate_all_rules()
        db_alerts = [a for a in alerts if a.rule_type == AlertRuleType.DATABASE_UNAVAILABLE]
        self.assertEqual(len(db_alerts), 1)
        self.assertEqual(db_alerts[0].severity, AlertSeverity.CRITICAL)
        self.assertEqual(db_alerts[0].state, AlertState.ACTIVE)

    # Escenario D: DB restored → alert resolved
    def test_scenario_d_db_restored_alert_resolved(self) -> None:
        # 1. DB falla
        self.monitoring_service.record_database_check(is_available=False, latency_ms=1500.0)
        self.alerting_service.evaluate_all_rules()

        active_db = self.alerting_service.list_alerts(
            rule_type=AlertRuleType.DATABASE_UNAVAILABLE, state=AlertState.ACTIVE
        )
        self.assertEqual(len(active_db), 1)

        # 2. DB se recupera con evidencia válida
        self.monitoring_service.record_database_check(is_available=True, latency_ms=12.5)
        self.alerting_service.evaluate_all_rules()

        # Debe estar automáticamente resuelta
        resolved_db = self.alerting_service.list_alerts(
            rule_type=AlertRuleType.DATABASE_UNAVAILABLE, state=AlertState.RESOLVED
        )
        self.assertEqual(len(resolved_db), 1)
        self.assertEqual(resolved_db[0].resolved_by, "system_auto_resolve")

    # Escenario E: p95 latency exceeds threshold → alert
    def test_scenario_e_p95_latency_exceeds_threshold(self) -> None:
        # Registrar muestras de latencia que superen el p95 threshold (500ms)
        for _ in range(8):
            self.monitoring_service.record_request_metric("GET", "/api/slow", 200, 100.0)
        for _ in range(3):
            self.monitoring_service.record_request_metric("GET", "/api/slow", 200, 850.0)

        alerts = self.alerting_service.evaluate_all_rules()
        latency_alerts = [a for a in alerts if a.rule_type == AlertRuleType.HIGH_LATENCY]
        self.assertEqual(len(latency_alerts), 1)
        self.assertGreaterEqual(latency_alerts[0].evidence["value"], 500.0)

    # Escenario F: backup stale / failed → alert
    def test_scenario_f_backup_stale_or_failed_alert(self) -> None:
        fail_meta = BackupMetadata(
            backup_id="bk_fail_01",
            environment=ApplicationEnvironment.PRODUCTION,
            database_name="commerce_prod",
            created_at=datetime.now(timezone.utc),
            postgres_version="16.0",
            migration_revision="001",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=0,
            checksum_sha256="0" * 64,
            file_name="bk_fail_01.dump",
        )
        failed_backup = BackupExecutionResult(
            status=BackupStatus.FAILED,
            backup_path=Path("/tmp/fail.dump"),
            metadata_path=Path("/tmp/fail.json"),
            metadata=fail_meta,
            duration_seconds=0.5,
            error_message="Storage disk full error",
        )
        self.monitoring_service.record_backup_result(failed_backup)

        alerts = self.alerting_service.evaluate_all_rules()
        backup_alerts = [a for a in alerts if a.rule_type == AlertRuleType.BACKUP_STALE_OR_FAILED]
        self.assertEqual(len(backup_alerts), 1)
        self.assertEqual(backup_alerts[0].severity, AlertSeverity.HIGH)

    # Escenario G: DR failure evidence → alert
    def test_scenario_g_dr_failure_evidence_alert(self) -> None:
        meta = BackupMetadata(
            backup_id="backup_dr_test",
            environment=ApplicationEnvironment.PRODUCTION,
            database_name="commerce_prod",
            created_at=datetime.now(timezone.utc),
            postgres_version="16.0",
            migration_revision="001",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=2048,
            checksum_sha256="d" * 64,
            file_name="backup_dr_test.dump",
        )
        plan = DisasterRecoveryPlan(
            plan_id="plan_dr_01",
            scenario=DisasterScenarioType.DATABASE_CORRUPTION,
            environment=ApplicationEnvironment.PRODUCTION,
            source_database="commerce_prod",
            recovery_target="commerce_prod_recovery",
            backup_metadata=meta,
            estimated_rpo_seconds=60.0,
            estimated_rto_seconds=300.0,
            steps=("provision", "restore", "validate"),
            created_at=datetime.now(timezone.utc),
        )
        failed_dr = DisasterRecoveryExecutionResult(
            execution_id="exec_dr_fail",
            plan=plan,
            status=RecoveryStatus.FAILED,
            target_connection_verified=True,
            backup_verified=False,
            restore_verified=False,
            migration_revision_verified=False,
            restored_revision="",
            expected_revision="001",
            tables_restored_count=0,
            expected_tables_count=15,
            tenants_restored_count=0,
            actual_rpo_seconds=450.0,
            actual_rto_seconds=1200.0,
            rpo_compliant=False,
            rto_compliant=False,
            cleanup_successful=True,
            completed_at=datetime.now(timezone.utc),
        )
        self.monitoring_service.record_disaster_recovery_result(failed_dr)

        alerts = self.alerting_service.evaluate_all_rules()
        dr_alerts = [a for a in alerts if a.rule_type == AlertRuleType.DR_LAST_SIMULATION_FAILED]
        self.assertEqual(len(dr_alerts), 1)
        self.assertEqual(dr_alerts[0].severity, AlertSeverity.HIGH)
        self.assertEqual(dr_alerts[0].severity, AlertSeverity.HIGH)

    # Escenario H: Tenant A alert inaccessible to Tenant B
    def test_scenario_h_tenant_isolation(self) -> None:
        tenant_rule = AlertRule(
            rule_type=AlertRuleType.QUOTA_EXHAUSTION,
            metric_type=MetricType.QUOTA_DENIAL_COUNT,
            window=MetricWindow.WINDOW_1H,
            severity=AlertSeverity.WARNING,
            threshold_value=1.0,
            comparison_operator=">=",
            description="Tenant quota exhaustion",
            scope=ProductionAlertScope.TENANT,
            min_sample_count=1,
        )
        service = ProductionAlertingService(
            repository=self.alert_repo,
            monitoring_service=self.monitoring_service,
            rules=[tenant_rule],
            environment=ApplicationEnvironment.PRODUCTION,
        )

        self.monitoring_service.record_quota_denial("token_cap", tenant_id="tenant_alpha")
        service.evaluate_all_rules(tenant_id="tenant_alpha")

        alpha_alerts = service.list_alerts(tenant_id="tenant_alpha")
        beta_alerts = service.list_alerts(tenant_id="tenant_beta")

        self.assertEqual(len(alpha_alerts), 1)
        self.assertEqual(len(beta_alerts), 0)

    # Escenario I: notification adapter failure → incident still active
    def test_scenario_i_notification_failure_incident_still_active(self) -> None:
        failing_notifier = MockNotificationAdapter(channel_name="bad_webhook", should_fail=True)
        service = ProductionAlertingService(
            repository=self.alert_repo,
            monitoring_service=self.monitoring_service,
            notification_ports=[failing_notifier],
            environment=ApplicationEnvironment.PRODUCTION,
        )

        self.monitoring_service.record_database_check(is_available=False)
        alerts = service.evaluate_all_rules()

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].state, AlertState.ACTIVE)
        active_stored = service.list_alerts(state=AlertState.ACTIVE)
        self.assertEqual(len(active_stored), 1)

    # Escenario J: Admin Console / Operator Lifecycle
    def test_scenario_j_admin_operator_lifecycle_e2e(self) -> None:
        # 1. Tráfico HTTP con alta tasa de fallos
        for _ in range(5):
            self.monitoring_service.record_request_metric("GET", "/api/item", 200, 10.0)
        for _ in range(5):
            self.monitoring_service.record_request_metric("GET", "/api/item", 500, 15.0)

        # 2. Evaluación genera alerta activa
        alerts = self.alerting_service.evaluate_all_rules()
        self.assertEqual(len(alerts), 1)
        alert_id = alerts[0].alert_id
        self.assertEqual(alerts[0].state, AlertState.ACTIVE)

        # 3. Operador reconoce la alerta (ACKNOWLEDGE)
        acked = self.alerting_service.acknowledge_alert(alert_id, actor_id="admin_user_99")
        self.assertEqual(acked.state, AlertState.ACKNOWLEDGED)
        self.assertEqual(acked.acknowledged_by, "admin_user_99")

        # 4. Condición continúa en la siguiente evaluación (persiste ACKNOWLEDGED sin duplicar)
        self.alerting_service.evaluate_all_rules()
        current = self.alerting_service.get_alert_by_id(alert_id)
        self.assertIsNotNone(current)
        self.assertEqual(current.state, AlertState.ACKNOWLEDGED)

        # 5. Manual resolve auditado
        resolved = self.alerting_service.resolve_alert(
            alert_id=alert_id,
            actor_id="admin_user_99",
            reason="Hotfix deployed and verified",
        )
        self.assertEqual(resolved.state, AlertState.RESOLVED)
        self.assertEqual(resolved.resolved_by, "admin_user_99")
        self.assertEqual(resolved.resolution_reason, "Hotfix deployed and verified")


if __name__ == "__main__":
    unittest.main()
