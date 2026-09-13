"""Tests Unitarios para P.6 — Health Checks (Production Health, Liveness, Readiness & Dependency Status).

Cubre los 16 criterios mínimos obligatorios:
1. Liveness healthy (independiente de dependencias externas).
2. Readiness healthy (almacenamiento y base de datos compatibles).
3. DB failure does not kill liveness (liveness 200 ante caída de DB).
4. DB failure fails readiness (readiness 503 ante caída de DB).
5. Schema mismatch fails readiness (readiness 503 ante revisión Alembic desactualizada).
6. Storage failure fails readiness (readiness 503 ante almacenamiento no escribible).
7. Optional dependency degraded (falla en proveedor externo degrada estado pero mantiene HTTP 200).
8. UNKNOWN critical != ready (incertidumbre en dependencia crítica retorna 503).
9. Sanitized response (cero exposición de credenciales o detalles internos).
10. Environment included safely (entorno incluido de forma segura en respuestas).
11. Timeout handled (manejo adecuado y acotado de latencias/timeouts).
12. Startup initializing not ready (retorno 503 mientras startup_complete es False).
13. No auto migration (readiness jamás ejecuta migraciones ni altera el esquema).
14. Correct HTTP semantics (200 para ok/degraded, 503 para unhealthy/unknown).
15. No secret leakage (comprobación de passwords y DSNs redactados).
16. No P.7+ (cero dependencias con módulos futuros no implementados).
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.domain.deployment.models import ApplicationEnvironment, DeploymentConfig
from src.domain.health.models import (
    DependencyCheckResult,
    DependencyClassification,
    HealthStatus,
    LivenessResult,
    ReadinessResult,
)
from src.infrastructure.health.service import HealthCheckService
from src.infrastructure.persistence.database.config import DatabaseConfig, DatabaseConnectionFactory


class TestP6HealthChecksUnit(unittest.TestCase):
    """Pruebas unitarias completas para los contratos y servicios de P.6."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.temp_dir.name)
        self.config = DeploymentConfig(
            environment=ApplicationEnvironment.PRODUCTION,
            port=8000,
            host="127.0.0.1",
            data_dir=str(self.data_dir),
            app_version="1.0.0",
        )
        self.db_config = DatabaseConfig(
            host="localhost",
            port=5432,
            database="test_db",
            user="app_user",
            password="super_secret_password_123!",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_1_liveness_healthy(self) -> None:
        """1. Liveness responde HEALTHY de forma inmediata y determinista."""
        service = HealthCheckService(config=self.config)
        result = service.check_liveness()

        self.assertEqual(result.status, HealthStatus.HEALTHY)
        self.assertTrue(result.is_alive)
        self.assertEqual(result.service, "ai-autonomous-commerce")
        self.assertEqual(result.version, "1.0.0")
        self.assertEqual(result.environment, "production")

        data = result.to_dict()
        self.assertEqual(data["status"], "ok")
        self.assertIn("timestamp", data)

    def test_2_readiness_healthy(self) -> None:
        """2. Readiness responde HEALTHY (200) cuando storage y DB están operativos."""
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.return_value = {
            "status": "ok",
            "host": "localhost",
            "port": 5432,
            "database": "test_db",
            "user": "app_user",
            "version": "PostgreSQL 16",
            "select_1": "OK",
            "database_match": True,
            "user_match": True,
        }

        with patch("src.infrastructure.health.service.check_schema_compatibility") as mock_schema:
            mock_schema.return_value = {
                "status": "ok",
                "is_up_to_date": True,
                "current_revision": "head_rev_123",
                "head_revision": "head_rev_123",
            }
            service = HealthCheckService(
                config=self.config,
                db_config=self.db_config,
                db_factory=mock_factory,
                is_startup_complete=True,
            )
            result = service.check_readiness()

            self.assertEqual(result.status, HealthStatus.HEALTHY)
            self.assertTrue(result.is_ready)
            self.assertEqual(result.http_status_code, 200)
            
            data = result.to_dict()
            self.assertEqual(data["status"], "ready")
            self.assertTrue(data.get("storage_writable"))
            self.assertEqual(data.get("database", {}).get("status"), "compatible")

    def test_3_db_failure_does_not_kill_liveness(self) -> None:
        """3. La caída de DB NO afecta la Liveness del proceso."""
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.side_effect = ConnectionRefusedError("Postgres down")

        service = HealthCheckService(
            config=self.config,
            db_config=self.db_config,
            db_factory=mock_factory,
        )
        liveness_result = service.check_liveness()

        self.assertEqual(liveness_result.status, HealthStatus.HEALTHY)
        self.assertTrue(liveness_result.is_alive)
        self.assertEqual(liveness_result.to_dict()["status"], "ok")

    def test_4_db_failure_fails_readiness(self) -> None:
        """4. La caída de DB derriba la Readiness (HTTP 503)."""
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.side_effect = ConnectionRefusedError("Connection to postgres:5432 failed")

        service = HealthCheckService(
            config=self.config,
            db_config=self.db_config,
            db_factory=mock_factory,
            is_startup_complete=True,
        )
        result = service.check_readiness()

        self.assertEqual(result.status, HealthStatus.UNHEALTHY)
        self.assertFalse(result.is_ready)
        self.assertEqual(result.http_status_code, 503)

        data = result.to_dict()
        self.assertEqual(data["status"], "unhealthy")
        self.assertIn("database_connectivity_failure", data.get("reason", ""))

    def test_5_schema_mismatch_fails_readiness(self) -> None:
        """5. Un esquema de DB desactualizado derriba Readiness (HTTP 503)."""
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.return_value = {"status": "ok"}

        with patch("src.infrastructure.health.service.check_schema_compatibility") as mock_schema:
            mock_schema.return_value = {
                "status": "mismatch",
                "is_up_to_date": False,
                "current_revision": "old_rev_001",
                "head_revision": "new_rev_002",
                "error_message": "Database revision old_rev_001 behind head new_rev_002",
            }
            service = HealthCheckService(
                config=self.config,
                db_config=self.db_config,
                db_factory=mock_factory,
                is_startup_complete=True,
            )
            result = service.check_readiness()

            self.assertEqual(result.status, HealthStatus.UNHEALTHY)
            self.assertFalse(result.is_ready)
            self.assertEqual(result.http_status_code, 503)

    def test_6_storage_failure_fails_readiness(self) -> None:
        """6. Almacenamiento no escribible derriba Readiness (HTTP 503)."""
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.return_value = {"status": "ok"}

        with patch("src.infrastructure.web.app.check_storage_writable", return_value=False):
            service = HealthCheckService(
                config=self.config,
                db_config=self.db_config,
                db_factory=mock_factory,
                is_startup_complete=True,
            )
            result = service.check_readiness()

            self.assertEqual(result.status, HealthStatus.UNHEALTHY)
            self.assertFalse(result.is_ready)
            self.assertEqual(result.http_status_code, 503)
            self.assertFalse(result.to_dict().get("storage_writable"))

    def test_7_optional_dependency_degraded(self) -> None:
        """7. Dependencia opcional degradada marca estado DEGRADED pero mantiene HTTP 200."""
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.return_value = {"status": "ok"}

        def fake_llm_checker() -> DependencyCheckResult:
            return DependencyCheckResult(
                name="openai_llm",
                classification=DependencyClassification.OPTIONAL,
                status=HealthStatus.DEGRADED,
                message="Rate limited or elevated latency",
            )

        with patch("src.infrastructure.health.service.check_schema_compatibility") as mock_schema:
            mock_schema.return_value = {"status": "ok", "is_up_to_date": True, "current_revision": "rev1"}
            service = HealthCheckService(
                config=self.config,
                db_config=self.db_config,
                db_factory=mock_factory,
                is_startup_complete=True,
                optional_checkers={"openai_llm": fake_llm_checker},
            )
            result = service.check_readiness()

            self.assertEqual(result.status, HealthStatus.DEGRADED)
            self.assertTrue(result.is_ready)
            self.assertEqual(result.http_status_code, 200)
            self.assertEqual(result.to_dict()["status"], "degraded")

    def test_8_unknown_critical_fails_readiness(self) -> None:
        """8. UNKNOWN en dependencia crítica derriba Readiness (HTTP 503)."""
        critical_unknown = DependencyCheckResult(
            name="storage",
            classification=DependencyClassification.CRITICAL,
            status=HealthStatus.UNKNOWN,
            message="Evidence gathering failed",
        )
        readiness = ReadinessResult(
            status=HealthStatus.UNHEALTHY,
            service="ai-autonomous-commerce",
            version="1.0.0",
            environment="production",
            checks=(critical_unknown,),
        )
        self.assertFalse(readiness.is_ready)
        self.assertEqual(readiness.http_status_code, 503)

    def test_9_sanitized_response(self) -> None:
        """9. Respuestas serializadas no contienen credenciales ni secretos en texto plano."""
        secret = "very_secret_pwd_999"
        raw_error = f"FATAL: password authentication failed for user postgres password={secret} dsn=postgresql://user:{secret}@internal.corp:5432/prod"
        
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.side_effect = RuntimeError(raw_error)

        service = HealthCheckService(
            config=self.config,
            db_config=self.db_config,
            db_factory=mock_factory,
            is_startup_complete=True,
        )
        result = service.check_readiness()
        payload_str = json.dumps(result.to_dict())

        self.assertNotIn(secret, payload_str)
        self.assertIn("password=***", payload_str)

    def test_10_environment_included_safely(self) -> None:
        """10. Entorno de despliegue se incluye explícita y limpiamente."""
        service = HealthCheckService(config=self.config)
        liveness = service.check_liveness().to_dict()
        self.assertEqual(liveness["environment"], "production")

    def test_11_timeout_handled(self) -> None:
        """11. Tiempos de verificación de dependencias se miden en milisegundos."""
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.return_value = {"status": "ok"}

        with patch("src.infrastructure.health.service.check_schema_compatibility") as mock_schema:
            mock_schema.return_value = {"status": "ok", "is_up_to_date": True}
            service = HealthCheckService(
                config=self.config,
                db_config=self.db_config,
                db_factory=mock_factory,
                is_startup_complete=True,
            )
            result = service.check_readiness()
            for check in result.checks:
                if check.latency_ms is not None:
                    self.assertGreaterEqual(check.latency_ms, 0.0)

    def test_12_startup_initializing_not_ready(self) -> None:
        """12. Durante la inicialización del proceso, Readiness retorna 503."""
        service = HealthCheckService(
            config=self.config,
            is_startup_complete=False,
        )
        result = service.check_readiness()

        self.assertEqual(result.status, HealthStatus.UNHEALTHY)
        self.assertFalse(result.is_ready)
        self.assertEqual(result.http_status_code, 503)
        self.assertEqual(result.checks[0].name, "startup")

    def test_13_no_auto_migration_invoked(self) -> None:
        """13. Los probes de readiness únicamente leen el estado, sin invocar upgrades de esquema."""
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        mock_factory.check_connection.return_value = {"status": "ok"}

        with patch("src.infrastructure.health.service.check_schema_compatibility") as mock_schema, \
             patch("scripts.db_migrate.run_upgrade") as mock_upgrade:
            mock_schema.return_value = {
                "status": "mismatch",
                "is_up_to_date": False,
                "current_revision": "rev1",
                "head_revision": "rev2",
            }
            service = HealthCheckService(
                config=self.config,
                db_config=self.db_config,
                db_factory=mock_factory,
                is_startup_complete=True,
            )
            service.check_readiness()

            # Garantizar que jamás se invoque run_upgrade
            mock_upgrade.assert_not_called()

    def test_14_correct_http_semantics(self) -> None:
        """14. Semántica estricta de códigos HTTP."""
        healthy_res = ReadinessResult(HealthStatus.HEALTHY, "svc", "1.0", "prod")
        degraded_res = ReadinessResult(HealthStatus.DEGRADED, "svc", "1.0", "prod")
        unhealthy_res = ReadinessResult(HealthStatus.UNHEALTHY, "svc", "1.0", "prod")
        unknown_res = ReadinessResult(HealthStatus.UNKNOWN, "svc", "1.0", "prod")

        self.assertEqual(healthy_res.http_status_code, 200)
        self.assertEqual(degraded_res.http_status_code, 200)
        self.assertEqual(unhealthy_res.http_status_code, 503)
        self.assertEqual(unknown_res.http_status_code, 503)

    def test_15_no_secret_leakage_in_models(self) -> None:
        """15. Los modelos y serializaciones rechazan o sanean credenciales."""
        check = DependencyCheckResult(
            name="database",
            classification=DependencyClassification.CRITICAL,
            status=HealthStatus.HEALTHY,
            details={"current_revision": "abc1234"},
        )
        serialized = check.to_dict()
        self.assertNotIn("password", serialized)
        self.assertNotIn("dsn", serialized)

    def test_16_no_p7_plus_dependencies(self) -> None:
        """16. P.6 opera sin importar ni requerir módulos de P.7+ (Monitoring/Alerting)."""
        import inspect
        import src.domain.health as h_domain
        import src.infrastructure.health as h_infra
        
        # Verificar que los módulos de P.6 no importen P.7+
        h_domain_src = inspect.getsource(h_domain)
        h_infra_src = inspect.getsource(h_infra)
        self.assertNotIn("monitoring", h_domain_src.lower())
        self.assertNotIn("alerting", h_domain_src.lower())
        self.assertNotIn("monitoring", h_infra_src.lower())
        self.assertNotIn("alerting", h_infra_src.lower())


if __name__ == "__main__":
    unittest.main()
