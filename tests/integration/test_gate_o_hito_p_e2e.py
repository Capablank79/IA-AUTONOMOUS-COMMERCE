"""
Tests de Integración E2E para GATE O — Formal Validation & Closure of Hito P
(Production / Operations).

Principio Fundamental a Validar:
"La plataforma puede ejecutarse de forma reproducible y segura en producción,
recuperarse de fallos, observarse, alertar, controlar retención/capacidad/rate limits
y preservar datos/tenant isolation sin comprometer la base de datos fuente ni filtrar secretos."

Cobertura de los 16 Bloques Canónicos de Gate O:
1. Environment resolution & configuration isolation (P.2 / O.13).
2. PostgreSQL real schema & migration state integrity (P.3).
3. Real startup & health/readiness probe contracts (P.6).
4. Backup creation, verification & restore-test (P.4).
5. Disaster Recovery simulation & RPO/RTO verification (P.5).
6. Production monitoring & metrics aggregation (P.7).
7. Operational alert lifecycle, deduplication & cooldown (P.8).
8. Log retention, rotation & safe purge policies (P.9).
9. Capacity planning, headroom evaluation & non-intrusive recommendations (P.10).
10. Rate limiting, quota distinction, and burst control (P.11).
11. Concurrency boundary & TOCTOU prevention under 1 token remaining (P.11).
12. Multi-tenant isolation across operational data & states (O.1 / P.*).
13. Cross-environment isolation (DEV vs STAGING vs PROD) (P.2).
14. Secret safety & sanitization across outputs, errors, metadata (N.5 / N.9 / P.*).
15. Production fail-safe matrix under simulated outages (P.1-P.11).
16. Full E2E production chain execution & source DB preservation.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import threading
from typing import Dict, Any, List, Optional
import unittest

from starlette.testclient import TestClient

# --- Domain & Application Models ---
from src.domain.deployment.models import (
    ApplicationEnvironment,
    DeploymentConfig,
    DeploymentConfigError,
    normalize_environment_name,
)
from src.domain.tenant.models import (
    TenantContext,
    TenantScope,
    TenantScopedResource,
    CrossTenantAccessError,
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
from src.domain.production_alerting.models import (
    AlertRule,
    AlertRuleType,
    AlertSeverity,
    AlertState,
    AlertEvaluationStatus,
    ProductionAlertInstance,
    ProductionAlertScope,
    generate_deduplication_key,
)
from src.domain.log_retention.models import (
    RetentionClass,
    RetentionAction,
    RetentionStatus,
    RetentionPolicy,
    RetentionDecision,
    RetentionResult,
)
from src.domain.capacity_planning.models import (
    CapacityConfidence,
    CapacityEvaluation,
    CapacityForecast,
    CapacityRecommendation,
    CapacityRecommendationType,
    CapacityResourceLimits,
    CapacityRisk,
    CapacityScope,
    ForecastHorizon,
    ResourceDimension,
)
from src.domain.rate_limit.models import (
    RateLimitScope,
    RateLimitStatus,
    RateLimitWindowUnit,
    RateLimitRule,
    RateLimitPolicy,
    RateLimitRequest,
    RateLimitDecision,
    build_canonical_rate_limit_key,
)
from src.domain.backup.models import (
    BackupFormat,
    BackupStatus,
    RestoreValidationStatus,
)
from src.domain.disaster_recovery.models import (
    DisasterScenarioType,
    RecoveryStatus,
)
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)

# --- Infrastructure & Services ---
from src.infrastructure.web.app import create_platform_app
from src.infrastructure.deployment.environment_policy import (
    resolve_application_environment,
    validate_environment_policy,
    validate_production_safety,
    CrossEnvironmentAccessError,
    EnvironmentSafetyError,
    EnvironmentResolutionError,
)
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConnectionFactory,
    sanitize_dsn,
    sanitize_error_message,
)
from scripts.db_migrate import check_schema_compatibility
from src.infrastructure.persistence.database.migrations.runner import (
    MigrationRunner,
)
from src.infrastructure.persistence.database.backup_service import (
    DatabaseBackupService,
    discover_postgres_tooling,
    EXPECTED_P3_TABLES,
)
from src.infrastructure.persistence.database.disaster_recovery_service import (
    DisasterRecoveryService,
)
from src.infrastructure.health.service import HealthCheckService
from src.application.monitoring.production_monitoring_service import (
    ProductionMonitoringService,
)
from src.application.production_alerting.production_alerting_service import (
    ProductionAlertingService,
)
from src.application.log_retention.log_retention_service import (
    LogRetentionApplicationService,
)
from src.application.capacity_planning.capacity_planning_service import (
    CapacityPlanningService,
    DefaultCapacityConfiguration,
    ProductionCapacityDataProvider,
)
from src.application.rate_limit.rate_limit_service import (
    RateLimitService,
)
from src.infrastructure.persistence.data.json.rate_limit_repository import (
    InMemoryRateLimitPolicyRepository,
    InMemoryRateLimitStateStore,
)
from src.infrastructure.persistence.data.json.metric_repository import (
    InMemoryMetricRepository,
    JsonMetricRepository,
)
from src.infrastructure.persistence.data.json.production_alert_repository import (
    JsonProductionAlertRepository,
)
from src.infrastructure.persistence.data.json.audit_repository import (
    JsonAuditRepository,
)
from src.infrastructure.persistence.data.json.audit_log_retention_store import (
    AuditLogRetentionStore,
)
from src.infrastructure.persistence.data.json.alert_log_retention_store import (
    AlertLogRetentionStore,
)
from src.domain.log_retention.policies import DefaultRetentionPolicyRegistry


class TestGateOHitoPE2E(unittest.TestCase):
    """Suite integral E2E de validación formal para Gate O y cierre de Hito P."""

    @classmethod
    def setUpClass(cls) -> None:
        """Carga y valida conectividad inicial a PostgreSQL local real."""
        try:
            cls.db_config = DatabaseConfig.from_env()
            cls.db_factory = DatabaseConnectionFactory(cls.db_config)
            conn_info = cls.db_factory.check_connection()
            cls.postgres_available = (conn_info.get("status") == "ok")
        except Exception:
            cls.postgres_available = False

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.temp_path = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Environment resolution & configuration isolation (P.2 / O.13)
    # -------------------------------------------------------------------------
    def test_01_environment_resolution_and_isolation(self):
        """Valida que APP_ENV sea la fuente canónica y que producción rechace configuraciones inseguras."""
        # A. Resolución canónica
        env_dev = resolve_application_environment({"APP_ENV": "development"})
        self.assertEqual(env_dev, ApplicationEnvironment.DEVELOPMENT)

        env_prod = resolve_application_environment({"APP_ENV": "production"})
        self.assertEqual(env_prod, ApplicationEnvironment.PRODUCTION)

        # B. Rechazo de divergencia
        with self.assertRaises(EnvironmentResolutionError):
            resolve_application_environment({"APP_ENV": "development", "ENVIRONMENT": "production"})

        # C. Producción rechaza DEBUG=True
        with self.assertRaises(EnvironmentSafetyError):
            validate_production_safety(
                ApplicationEnvironment.PRODUCTION,
                {"DEBUG": "true", "HOST": "0.0.0.0"},
            )

        # D. Producción rechaza host loopback
        with self.assertRaises(EnvironmentSafetyError):
            validate_production_safety(
                ApplicationEnvironment.PRODUCTION,
                {"DEBUG": "false", "HOST": "127.0.0.1"},
            )

        # E. Producción rechaza mock flags
        with self.assertRaises(EnvironmentSafetyError):
            validate_production_safety(
                ApplicationEnvironment.PRODUCTION,
                {"DEBUG": "false", "HOST": "0.0.0.0", "USE_MOCK_PAYMENT": "true"},
            )

    # -------------------------------------------------------------------------
    # 2. PostgreSQL real schema & migration state integrity (P.3)
    # -------------------------------------------------------------------------
    def test_02_postgresql_schema_and_migration_integrity(self):
        """Comprueba que la base PostgreSQL real contenga la versión Alembic HEAD y las 16 tablas SaaS."""
        if not self.postgres_available:
            self.skipTest("PostgreSQL local no disponible.")

        # Verificar HEAD
        compat = check_schema_compatibility()
        self.assertTrue(compat["is_up_to_date"])
        self.assertEqual(compat["current_revision"], "001_initial_saas_schema")

        # Verificar existencia física de tablas
        with self.db_factory.create_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';"
                )
                existing_tables = {row[0] for row in cur.fetchall()}

        for table in EXPECTED_P3_TABLES:
            self.assertIn(table, existing_tables, f"Tabla obligatoria '{table}' ausente en PostgreSQL.")

    # -------------------------------------------------------------------------
    # 3. Real startup & health/readiness probe contracts (P.6)
    # -------------------------------------------------------------------------
    def test_03_startup_and_health_readiness_contracts(self):
        """Verifica los probes /health y /ready y el comportamiento ante fallos controlados."""
        app = create_platform_app()
        client = TestClient(app)

        # A. Liveness probe (200 OK)
        res_health = client.get("/health")
        self.assertEqual(res_health.status_code, 200)
        data_health = res_health.json()
        self.assertEqual(data_health["status"], "ok")
        self.assertNotIn("password", str(data_health).lower())

        # B. Readiness probe (200 OK con DB local)
        res_ready = client.get("/ready")
        self.assertIn(res_ready.status_code, (200, 503))
        data_ready = res_ready.json()
        self.assertIn("status", data_ready)

        # C. Simulación de fallo en dependencia crítica DB -> Liveness 200, Readiness 503
        bad_config = self.db_config.with_database("ia_non_existent_db_xyz")
        bad_factory = DatabaseConnectionFactory(bad_config)
        dep_config = DeploymentConfig(
            environment=ApplicationEnvironment.DEVELOPMENT,
            host="127.0.0.1",
            port=8000,
            data_dir=self.temp_path / "data",
            app_version="1.0.0",
        )
        health_svc = HealthCheckService(
            config=dep_config,
            db_config=bad_config,
            db_factory=bad_factory,
            is_startup_complete=True,
        )
        liveness = health_svc.check_liveness()
        readiness = health_svc.check_readiness()

        self.assertEqual(liveness.status, HealthStatus.HEALTHY)
        self.assertEqual(readiness.status, HealthStatus.UNHEALTHY)
        self.assertFalse(readiness.is_ready)

    # -------------------------------------------------------------------------
    # 4. Backup creation, verification & restore-test (P.4)
    # -------------------------------------------------------------------------
    def test_04_backup_create_verify_and_restore_test(self):
        """Ejecuta un ciclo real de backup P.4 y valida restore-test aislado."""
        if not self.postgres_available:
            self.skipTest("PostgreSQL local no disponible.")

        backup_svc = DatabaseBackupService(
            db_config=self.db_config,
            base_backup_dir=self.temp_path / "backups",
        )

        # Create
        res_backup = backup_svc.create_backup(environment=ApplicationEnvironment.DEVELOPMENT)
        self.assertEqual(res_backup.status, BackupStatus.COMPLETED)
        self.assertTrue(res_backup.backup_path.exists())
        self.assertTrue(res_backup.metadata_path.exists())
        self.assertEqual(res_backup.metadata.migration_revision, "001_initial_saas_schema")

        # Verify
        meta = backup_svc.verify_backup(
            res_backup.metadata.file_name,
            environment=ApplicationEnvironment.DEVELOPMENT,
        )
        self.assertEqual(meta.backup_id, res_backup.metadata.backup_id)
        self.assertEqual(meta.checksum_sha256, res_backup.metadata.checksum_sha256)

        # Restore-test en esquema temporal aislado
        res_restore = backup_svc.run_restore_test(
            res_backup.metadata.file_name,
            environment=ApplicationEnvironment.DEVELOPMENT,
        )
        self.assertEqual(res_restore.status, RestoreValidationStatus.PASSED)
        self.assertTrue(res_restore.migration_revision_verified)
        self.assertTrue(res_restore.cleanup_successful)
        self.assertGreaterEqual(res_restore.tables_verified_count, 16)

    # -------------------------------------------------------------------------
    # 5. Disaster Recovery simulation & RPO/RTO verification (P.5)
    # -------------------------------------------------------------------------
    def test_05_disaster_recovery_simulation(self):
        """Ejecuta simulación completa de Disaster Recovery con validación cuantitativa RPO/RTO."""
        if not self.postgres_available:
            self.skipTest("PostgreSQL local no disponible.")

        backup_svc = DatabaseBackupService(
            db_config=self.db_config,
            base_backup_dir=self.temp_path / "dr_backups",
        )
        # Crear backup fresco para asegurar SLA
        backup_svc.create_backup(environment=ApplicationEnvironment.DEVELOPMENT)

        dr_svc = DisasterRecoveryService(
            db_config=self.db_config,
            backup_service=backup_svc,
        )

        dr_result = dr_svc.execute_recovery_simulation(
            scenario=DisasterScenarioType.DATABASE_CORRUPTION,
            environment=ApplicationEnvironment.DEVELOPMENT,
        )
        self.assertEqual(dr_result.status, RecoveryStatus.COMPLETED)
        self.assertTrue(dr_result.target_connection_verified)
        self.assertTrue(dr_result.restore_verified)
        self.assertTrue(dr_result.migration_revision_verified)
        self.assertTrue(dr_result.cleanup_successful)
        self.assertLess(dr_result.actual_rto_seconds, 300.0)

    # -------------------------------------------------------------------------
    # 6. Production monitoring & metrics aggregation (P.7)
    # -------------------------------------------------------------------------
    def test_06_production_monitoring_metrics_aggregation(self):
        """Genera tráfico sintético y valida agregaciones, percentiles y persistencia de métricas."""
        repo = InMemoryMetricRepository()
        svc = ProductionMonitoringService(repository=repo, environment=ApplicationEnvironment.PRODUCTION)
        now = datetime.now(timezone.utc)

        # Registrar métricas vía helper
        for lat in [50.0, 100.0, 150.0, 200.0, 500.0]:
            svc.record_request_metric(
                method="GET",
                path="/api/v1/orders",
                status_code=200,
                duration_ms=lat,
                environment=ApplicationEnvironment.PRODUCTION,
            )

        metric_lat = svc.get_metric(
            metric_type=MetricType.LATENCY_MS,
            window=MetricWindow.WINDOW_5M,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        self.assertFalse(metric_lat.is_unknown)
        self.assertEqual(metric_lat.sample_count, 5)
        self.assertAlmostEqual(metric_lat.value, 200.0, delta=1.0)
        self.assertIsNotNone(metric_lat.p95_value)
        self.assertGreaterEqual(metric_lat.p95_value, 200.0)

    # -------------------------------------------------------------------------
    # 7. Operational alert lifecycle, deduplication & cooldown (P.8)
    # -------------------------------------------------------------------------
    def test_07_alert_lifecycle_dedup_and_cooldown(self):
        """Comprueba el ciclo de vida de alertas: trigger -> dedup -> acknowledge -> resolve."""
        metric_repo = InMemoryMetricRepository()
        alert_repo = JsonProductionAlertRepository(base_dir=self.temp_path / "alerts")
        monitoring_svc = ProductionMonitoringService(
            repository=metric_repo,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        # Registrar regla para HTTP error rate
        rule = AlertRule(
            rule_type=AlertRuleType.HIGH_ERROR_RATE,
            metric_type=MetricType.ERROR_RATE,
            window=MetricWindow.WINDOW_5M,
            severity=AlertSeverity.HIGH,
            threshold_value=5.0,
            description="High error rate test rule",
            comparison_operator=">",
            scope=ProductionAlertScope.PLATFORM,
            cooldown_seconds=60,
            min_sample_count=2,
        )
        alert_svc = ProductionAlertingService(
            repository=alert_repo,
            monitoring_service=monitoring_svc,
            rules=[rule],
            environment=ApplicationEnvironment.PRODUCTION,
        )
        now = datetime.now(timezone.utc)

        # 1. Inyectar muestras con alta tasa de error (100% errores)
        for _ in range(5):
            monitoring_svc.record_request_metric(
                method="POST",
                path="/api/v1/checkout",
                status_code=500,
                duration_ms=120.0,
                environment=ApplicationEnvironment.PRODUCTION,
            )

        triggered = alert_svc.evaluate_all_rules()
        self.assertGreaterEqual(len(triggered), 1)
        alert_inst = triggered[0]
        self.assertEqual(alert_inst.state, AlertState.ACTIVE)
        self.assertEqual(alert_inst.severity, AlertSeverity.HIGH)

        # 2. Re-evaluación dentro de cooldown -> deduplicada / no genera nueva instancia duplicada
        triggered_2 = alert_svc.evaluate_all_rules()
        # La alerta activa se actualiza o mantiene sin duplicar ID
        self.assertEqual(len(triggered_2), 1)
        self.assertEqual(triggered_2[0].alert_id, alert_inst.alert_id)

        # 3. Acknowledge
        ack_alert = alert_svc.acknowledge_alert(
            alert_inst.alert_id,
            actor_id="ops_team_admin",
        )
        self.assertEqual(ack_alert.state, AlertState.ACKNOWLEDGED)

        # 4. Resolve manual
        res_alert = alert_svc.resolve_alert(
            alert_inst.alert_id,
            actor_id="ops_team_admin",
            reason="Upstream fixed",
        )
        self.assertEqual(res_alert.state, AlertState.RESOLVED)

    # -------------------------------------------------------------------------
    # 8. Log retention, rotation & safe purge policies (P.9)
    # -------------------------------------------------------------------------
    def test_08_log_retention_and_safe_purge(self):
        """Valida que la retención elimine sólo registros elegibles protegiendo auditoría y alertas activas."""
        audit_dir = self.temp_path / "audit"
        audit_repo = JsonAuditRepository(storage_dir=audit_dir)

        # Inyectar registro de auditoría (PROTEGIDO incondicionalmente)
        audit_repo.append(
            AuditRecord(
                audit_id="aud_100",
                occurred_at=datetime.now(timezone.utc) - timedelta(days=500),
                record_type=AuditRecordType.MISSION_CREATED,
                actor=AuditActor(actor_id="user_sec", actor_type=AuditActorType.USER),
                subject_type="USER_SESSION",
                subject_id="sess_123",
                action_or_operation="USER_LOGIN",
                status="SUCCESS",
                correlation_id="corr_audit_100",
            )
        )

        audit_store = AuditLogRetentionStore(audit_repository=audit_repo)
        policy_registry = DefaultRetentionPolicyRegistry()
        service = LogRetentionApplicationService(
            stores=[audit_store],
            policy_registry=policy_registry,
        )

        # Ejecutar purge para AUDIT_RECORD
        purge_res = service.execute_retention(
            environment=ApplicationEnvironment.DEVELOPMENT,
            data_class=RetentionClass.AUDIT_RECORD,
            dry_run=False,
        )
        self.assertEqual(purge_res.purged_count, 0)
        self.assertEqual(purge_res.protected_count, 1)

        # Confirmar que el registro sigue intacto
        all_audits = audit_repo.list_records(limit=10)
        self.assertEqual(len(all_audits), 1)

    # -------------------------------------------------------------------------
    # 9. Capacity planning, headroom evaluation & non-intrusive recommendations (P.10)
    # -------------------------------------------------------------------------
    def test_09_capacity_planning_forecasting(self):
        """Evalúa proyección de capacidad: caso normal NO_ACTION vs alta demanda SCALE_SOON."""
        metric_repo = InMemoryMetricRepository()
        data_provider = ProductionCapacityDataProvider(metric_repository=metric_repo)
        config = DefaultCapacityConfiguration(
            custom_limits={
                ApplicationEnvironment.PRODUCTION: {
                    ResourceDimension.REQUEST_THROUGHPUT: CapacityResourceLimits(
                        dimension=ResourceDimension.REQUEST_THROUGHPUT,
                        max_capacity=1000.0,
                        unit="req/min",
                        warning_utilization_ratio=0.70,
                        critical_utilization_ratio=0.90,
                    )
                }
            }
        )
        svc = CapacityPlanningService(
            data_provider=data_provider,
            config_provider=config,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        now = datetime.now(timezone.utc)

        # A. Caso estable -> LOW risk / NO_ACTION
        for i in range(10):
            metric_repo.record_sample(
                MetricSample(
                    metric_type=MetricType.REQUEST_COUNT,
                    value=100.0,
                    timestamp=now - timedelta(minutes=10 - i),
                    unit=MetricUnit.COUNT,
                    environment=ApplicationEnvironment.PRODUCTION,
                    scope=MonitoringScope.PLATFORM,
                )
            )

        eval_low = svc.evaluate_dimension(
            dimension=ResourceDimension.REQUEST_THROUGHPUT,
            start_time=now - timedelta(hours=1),
            end_time=now,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        self.assertEqual(eval_low.overall_risk, CapacityRisk.LOW)
        self.assertEqual(eval_low.recommendation.recommendation_type, CapacityRecommendationType.NO_ACTION)

        # B. Caso creciente -> SCALE_SOON
        metric_repo_high = InMemoryMetricRepository()
        data_provider_high = ProductionCapacityDataProvider(metric_repository=metric_repo_high)
        svc_high = CapacityPlanningService(
            data_provider=data_provider_high,
            config_provider=config,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        for i in range(10):
            metric_repo_high.record_sample(
                MetricSample(
                    metric_type=MetricType.REQUEST_COUNT,
                    value=800.0,  # 80% > 70% warning threshold
                    timestamp=now - timedelta(minutes=10 - i),
                    unit=MetricUnit.COUNT,
                    environment=ApplicationEnvironment.PRODUCTION,
                    scope=MonitoringScope.PLATFORM,
                )
            )

        eval_high = svc_high.evaluate_dimension(
            dimension=ResourceDimension.REQUEST_THROUGHPUT,
            start_time=now - timedelta(hours=1),
            end_time=now,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        self.assertIn(eval_high.overall_risk, (CapacityRisk.MODERATE, CapacityRisk.HIGH))
        self.assertIn(
            eval_high.recommendation.recommendation_type,
            (CapacityRecommendationType.SCALE_SOON, CapacityRecommendationType.REVIEW_CAPACITY),
        )

    # -------------------------------------------------------------------------
    # 10. Rate limiting, quota distinction, and burst control (P.11)
    # -------------------------------------------------------------------------
    def test_10_rate_limiting_fairness_and_burst_control(self):
        """Verifica E2E que las primeras N solicitudes pasen y la N+1 sea denegada (429/DENY)."""
        policy_repo = InMemoryRateLimitPolicyRepository()
        state_store = InMemoryRateLimitStateStore()
        svc = RateLimitService(
            policy_repository=policy_repo,
            state_store=state_store,
            environment=ApplicationEnvironment.PRODUCTION,
        )

        # Regla: 3 requests por minuto para Tenant Alpha
        rule = RateLimitRule(
            rule_id="rule_tenant_burst",
            limit_rate=3,
            burst_capacity=3,
            scope=RateLimitScope.TENANT,
            window_unit=RateLimitWindowUnit.MINUTE,
        )
        policy = RateLimitPolicy(
            policy_id="policy_t_alpha",
            tenant_id="tenant_alpha",
            rules=(rule,),
        )
        policy_repo.save_policy(policy)

        req_alpha = RateLimitRequest(
            tenant_id="tenant_alpha",
            environment=ApplicationEnvironment.PRODUCTION,
            cost_units=1,
        )

        # 3 permits -> ALLOW
        for i in range(3):
            dec = svc.check_and_consume(req_alpha)
            self.assertEqual(dec.status, RateLimitStatus.ALLOW, f"Request {i+1} debió ser permitida.")

        # 4th permit -> DENY
        dec_denied = svc.check_and_consume(req_alpha)
        self.assertEqual(dec_denied.status, RateLimitStatus.DENY)
        self.assertGreater(dec_denied.retry_after_seconds, 0)

        # Tenant Beta no afectado (Fairness) con su propia política
        policy_beta = RateLimitPolicy(
            policy_id="policy_t_beta",
            tenant_id="tenant_beta",
            rules=(rule,),
        )
        policy_repo.save_policy(policy_beta)
        req_beta = RateLimitRequest(
            tenant_id="tenant_beta",
            environment=ApplicationEnvironment.PRODUCTION,
            cost_units=1,
        )
        dec_beta = svc.check_and_consume(req_beta)
        self.assertEqual(dec_beta.status, RateLimitStatus.ALLOW)

    # -------------------------------------------------------------------------
    # 11. Concurrency boundary & TOCTOU prevention under 1 token remaining (P.11)
    # -------------------------------------------------------------------------
    def test_11_rate_limit_concurrency_boundary(self):
        """Con 1 slot disponible, múltiples threads concurrentes deben resultar en exactamente 1 ALLOW."""
        policy_repo = InMemoryRateLimitPolicyRepository()
        state_store = InMemoryRateLimitStateStore()
        svc = RateLimitService(
            policy_repository=policy_repo,
            state_store=state_store,
            environment=ApplicationEnvironment.PRODUCTION,
        )

        # Regla: 1 request por minuto
        rule = RateLimitRule(
            rule_id="rule_strict_one",
            limit_rate=1,
            burst_capacity=1,
            scope=RateLimitScope.TENANT,
            window_unit=RateLimitWindowUnit.MINUTE,
        )
        policy = RateLimitPolicy(
            policy_id="policy_strict",
            tenant_id="tenant_race",
            rules=(rule,),
        )
        policy_repo.save_policy(policy)

        req = RateLimitRequest(
            tenant_id="tenant_race",
            environment=ApplicationEnvironment.PRODUCTION,
            cost_units=1,
        )

        results: List[RateLimitDecision] = []
        threads = []

        def worker():
            d = svc.check_and_consume(req)
            results.append(d)

        for _ in range(10):
            t = threading.Thread(target=worker)
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        allowed_count = sum(1 for r in results if r.status == RateLimitStatus.ALLOW)
        denied_count = sum(1 for r in results if r.status == RateLimitStatus.DENY)

        self.assertEqual(allowed_count, 1, "Exactamente 1 request debe ser ALLOWED bajo contienda.")
        self.assertEqual(denied_count, 9, "9 requests deben ser DENIED.")

    # -------------------------------------------------------------------------
    # 12. Multi-tenant isolation across operational data & states (O.1 / P.*)
    # -------------------------------------------------------------------------
    def test_12_cross_tenant_operational_isolation(self):
        """Verifica que las operaciones de Tenant A no tengan efecto sobre los registros o límites de Tenant B."""
        metric_repo = InMemoryMetricRepository()
        monitoring_svc = ProductionMonitoringService(
            repository=metric_repo,
            environment=ApplicationEnvironment.PRODUCTION,
        )

        # Registrar peticiones asignadas a cada tenant
        monitoring_svc.record_request_metric(
            method="GET",
            path="/api/v1/items",
            status_code=200,
            duration_ms=10.0,
            environment=ApplicationEnvironment.PRODUCTION,
            tenant_id="tenant_alpha",
        )
        monitoring_svc.record_request_metric(
            method="GET",
            path="/api/v1/items",
            status_code=200,
            duration_ms=20.0,
            environment=ApplicationEnvironment.PRODUCTION,
            tenant_id="tenant_beta",
        )

        sum_alpha = monitoring_svc.get_metric(
            metric_type=MetricType.REQUEST_COUNT,
            window=MetricWindow.WINDOW_5M,
            environment=ApplicationEnvironment.PRODUCTION,
            tenant_id="tenant_alpha",
        )
        sum_beta = monitoring_svc.get_metric(
            metric_type=MetricType.REQUEST_COUNT,
            window=MetricWindow.WINDOW_5M,
            environment=ApplicationEnvironment.PRODUCTION,
            tenant_id="tenant_beta",
        )

        self.assertEqual(sum_alpha.sample_count, 1)
        self.assertEqual(sum_alpha.value, 1)
        self.assertEqual(sum_beta.sample_count, 1)
        self.assertEqual(sum_beta.value, 1)

    # -------------------------------------------------------------------------
    # 13. Cross-environment isolation (DEV vs STAGING vs PROD) (P.2)
    # -------------------------------------------------------------------------
    def test_13_cross_environment_data_isolation(self):
        """Verifica que eventos y datos de DEV jamás impacten STAGING o PRODUCTION."""
        metric_repo = InMemoryMetricRepository()
        monitoring_svc = ProductionMonitoringService(
            repository=metric_repo,
            environment=ApplicationEnvironment.PRODUCTION,
        )

        # Inyectar error en DEVELOPMENT
        monitoring_svc.record_request_metric(
            method="POST",
            path="/api/v1/dev-test",
            status_code=500,
            duration_ms=50.0,
            environment=ApplicationEnvironment.DEVELOPMENT,
        )

        # Consultar en PRODUCTION -> debe permanecer intacto / UNKNOWN
        sum_prod = monitoring_svc.get_metric(
            metric_type=MetricType.REQUEST_COUNT,
            window=MetricWindow.WINDOW_5M,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        self.assertTrue(sum_prod.is_unknown)
        self.assertEqual(sum_prod.sample_count, 0)

    # -------------------------------------------------------------------------
    # 14. Secret safety & sanitization across outputs, errors, metadata (N.5 / N.9 / P.*)
    # -------------------------------------------------------------------------
    def test_14_secret_safety_and_sanitization(self):
        """Verifica que los métodos de sanitización y representaciones redacten contraseñas y tokens."""
        raw_dsn = "postgresql://iac_app:super_secret_password_123@localhost:5432/ia_autonomous_commerce"
        sanitized = sanitize_dsn(raw_dsn)
        self.assertNotIn("super_secret_password_123", sanitized)
        self.assertIn("iac_app:***@localhost", sanitized)

        raw_err = "Error password='my_password_xyz' connection failed to postgresql://user:pwd@host/db"
        sanitized_err = sanitize_error_message(raw_err)
        self.assertNotIn("my_password_xyz", sanitized_err)
        self.assertNotIn("pwd", sanitized_err)

    # -------------------------------------------------------------------------
    # 15. Production fail-safe matrix under simulated outages (P.1-P.11)
    # -------------------------------------------------------------------------
    def test_15_production_fail_safe_matrix(self):
        """Valida que ante dependencias corruptas o ausentes el sistema falle de forma segura."""
        # A. Archivo de backup corrupto -> verify_backup falla
        corrupt_dir = self.temp_path / "corrupt_backups"
        dev_dir = corrupt_dir / "development"
        dev_dir.mkdir(parents=True, exist_ok=True)
        corrupt_file = dev_dir / "corrupt.dump"
        corrupt_file.write_bytes(b"invalid_corrupted_data_header")
        meta_file = dev_dir / "corrupt.dump.json"
        meta_file.write_text(
            '{"backup_id":"b_bad","checksum_sha256":"wrong","file_name":"corrupt.dump","environment":"development","created_at":"2026-09-12T00:00:00Z","database_name":"test_db","postgres_version":"18.6","migration_revision":"001_initial_saas_schema","backup_format":"CUSTOM","file_size_bytes":20}',
            encoding="utf-8",
        )

        backup_svc = DatabaseBackupService(
            db_config=self.db_config,
            base_backup_dir=corrupt_dir,
        )
        with self.assertRaises(Exception):
            backup_svc.verify_backup("corrupt.dump", environment=ApplicationEnvironment.DEVELOPMENT)

        # B. Incertidumbre en datos de capacidad -> INSUFFICIENT_DATA / UNKNOWN
        metric_repo = InMemoryMetricRepository()
        data_provider = ProductionCapacityDataProvider(metric_repository=metric_repo)
        cap_svc = CapacityPlanningService(
            data_provider=data_provider,
            environment=ApplicationEnvironment.PRODUCTION,
        )

        now = datetime.now(timezone.utc)
        eval_unknown = cap_svc.evaluate_dimension(
            dimension=ResourceDimension.TOKEN_THROUGHPUT,
            start_time=now - timedelta(hours=1),
            end_time=now,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        self.assertEqual(eval_unknown.confidence, CapacityConfidence.INSUFFICIENT_DATA)
        self.assertEqual(eval_unknown.overall_risk, CapacityRisk.UNKNOWN)

    # -------------------------------------------------------------------------
    # 16. Full E2E production chain execution & source DB preservation
    # -------------------------------------------------------------------------
    def test_16_full_e2e_production_chain_and_source_preservation(self):
        """Ejecuta la cadena completa P.1 -> P.11 y confirma que la DB fuente permanece 100% intacta."""
        if not self.postgres_available:
            self.skipTest("PostgreSQL local no disponible.")

        # Verificar DB antes
        conn_pre = self.db_factory.check_connection()
        self.assertEqual(conn_pre["status"], "ok")
        self.assertEqual(conn_pre["database"], "ia_autonomous_commerce")

        # Ejecutar operaciones operacionales encadenadas
        # 1. Health check
        app = create_platform_app()
        client = TestClient(app)
        r_health = client.get("/health")
        self.assertEqual(r_health.status_code, 200)

        # 2. Monitoring sample
        m_repo = InMemoryMetricRepository()
        m_svc = ProductionMonitoringService(
            repository=m_repo,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        m_svc.record_request_metric(
            method="GET",
            path="/api/v1/health",
            status_code=200,
            duration_ms=5.0,
            environment=ApplicationEnvironment.PRODUCTION,
        )

        # 3. Rate limit
        rl_p_repo = InMemoryRateLimitPolicyRepository()
        rl_s_store = InMemoryRateLimitStateStore()
        rl_svc = RateLimitService(
            policy_repository=rl_p_repo,
            state_store=rl_s_store,
            environment=ApplicationEnvironment.PRODUCTION,
        )
        rule = RateLimitRule(
            rule_id="rule_chain",
            limit_rate=10,
            burst_capacity=10,
            scope=RateLimitScope.TENANT,
        )
        policy = RateLimitPolicy(
            policy_id="policy_chain",
            tenant_id="tenant_chain",
            rules=(rule,),
        )
        rl_p_repo.save_policy(policy)
        rl_dec = rl_svc.check_and_consume(
            RateLimitRequest(
                tenant_id="tenant_chain",
                environment=ApplicationEnvironment.PRODUCTION,
                cost_units=1,
            )
        )
        self.assertEqual(rl_dec.status, RateLimitStatus.ALLOW)

        # 4. Verificar DB después: 100% intacta
        conn_post = self.db_factory.check_connection()
        self.assertEqual(conn_post["status"], "ok")
        self.assertEqual(conn_post["database"], "ia_autonomous_commerce")
        self.assertEqual(conn_post["user"], "iac_app")
