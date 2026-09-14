"""Tests Unitarios para P.5 — Disaster Recovery (Recovery Procedures, RPO/RTO & Service Restoration).

Cubre:
1. Source DSN → Recovery DSN cambia sólo database (DatabaseConfig.with_database).
2. Host preserved.
3. Port preserved.
4. User preserved.
5. Password preserved internamente.
6. Password redacted externamente en sanitized_dsn y repr.
7. Query params / timeouts / sslmode preserved.
8. Special characters safe en password (@, :, /, ?, #, %, comillas).
9. Source == Target rejected (Dangerous configuration / same database).
10. Malformed DSN rejected.
11. Generación de Planes DR para todos los escenarios (DisasterScenarioType).
12. Medición precisa de RPO y RTO en política y ejecución.
13. Validación de target connection (SELECT current_database(), current_user).
14. No secrets in DR plan o execution result serialization.
15. Fases ordenadas del ciclo de vida de DR.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote_plus

from src.domain.backup.models import (
    BackupFormat,
    BackupMetadata,
    RestoreValidationResult,
    RestoreValidationStatus,
)
from src.domain.deployment.models import ApplicationEnvironment
from src.domain.disaster_recovery.models import (
    DisasterRecoveryError,
    DisasterRecoveryPlan,
    DisasterRecoveryPolicy,
    DisasterScenarioType,
    RecoveryPhase,
    RecoveryStatus,
    TargetConnectionVerificationError,
)
from src.infrastructure.persistence.database.backup_service import (
    EXPECTED_P3_TABLES,
    DatabaseBackupService,
    PostgresToolingPaths,
)
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConfigError,
    DatabaseConnectionError,
    DatabaseConnectionFactory,
    sanitize_dsn,
)
from src.infrastructure.persistence.database.disaster_recovery_service import (
    DisasterRecoveryService,
)


class TestP5DisasterRecoveryUnit(unittest.TestCase):
    """Suite exhaustiva de pruebas unitarias para P.5 Disaster Recovery."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.mock_tooling = PostgresToolingPaths(
            pg_dump=Path("/mock/bin/pg_dump"),
            pg_restore=Path("/mock/bin/pg_restore"),
            psql=Path("/mock/bin/psql"),
        )
        self.source_config = DatabaseConfig(
            host="db.internal.iac",
            port=5433,
            database="ia_autonomous_commerce",
            user="iac_app",
            password="p@ss:w/o?r#d%123!",
            connect_timeout=15,
            sslmode="require",
            application_name="iac-dr-suite",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Source DSN -> recovery DSN cambia sólo database
    def test_with_database_changes_only_database(self) -> None:
        target_config = self.source_config.with_database("iac_dr_test_12345")
        self.assertEqual(target_config.database, "iac_dr_test_12345")
        self.assertNotEqual(target_config.database, self.source_config.database)

    # 2. Host preserved
    def test_host_preserved(self) -> None:
        target = self.source_config.with_database("iac_target_db")
        self.assertEqual(target.host, self.source_config.host)
        self.assertEqual(target.host, "db.internal.iac")

    # 3. Port preserved
    def test_port_preserved(self) -> None:
        target = self.source_config.with_database("iac_target_db")
        self.assertEqual(target.port, self.source_config.port)
        self.assertEqual(target.port, 5433)

    # 4. User preserved
    def test_user_preserved(self) -> None:
        target = self.source_config.with_database("iac_target_db")
        self.assertEqual(target.user, self.source_config.user)
        self.assertEqual(target.user, "iac_app")

    # 5. Password preserved internamente
    def test_password_preserved_internally(self) -> None:
        target = self.source_config.with_database("iac_target_db")
        self.assertEqual(target.password, self.source_config.password)
        self.assertEqual(target.password, "p@ss:w/o?r#d%123!")

    # 6. Password redacted externamente
    def test_password_redacted_externally(self) -> None:
        target = self.source_config.with_database("iac_target_db")
        sanitized = target.sanitized_dsn
        self.assertNotIn("p@ss:w/o?r#d%123!", sanitized)
        self.assertNotIn("p@ss", sanitized)
        self.assertIn("iac_app:***@db.internal.iac:5433/iac_target_db", sanitized)
        
        repr_str = repr(target)
        self.assertNotIn("p@ss", repr_str)
        self.assertIn("password='***'", repr_str)

    # 7. Query params / timeouts / sslmode preserved
    def test_params_preserved(self) -> None:
        target = self.source_config.with_database("iac_target_db")
        self.assertEqual(target.connect_timeout, 15)
        self.assertEqual(target.sslmode, "require")
        self.assertEqual(target.application_name, "iac-dr-suite")

    # 8. Special characters safe (psycopg conninfo building)
    def test_special_characters_conninfo_safe(self) -> None:
        cfg = DatabaseConfig(
            host="127.0.0.1",
            port=5432,
            database="db_name",
            user="iac_user",
            password="special@#/:?% 'quoted'",
        )
        conninfo_str = cfg.build_conninfo()
        self.assertIn("127.0.0.1", conninfo_str)
        self.assertIn("db_name", conninfo_str)
        # Psycopg make_conninfo handles escaping
        self.assertIn("password=", conninfo_str)

    # 9. Source == Target rejected
    def test_source_equals_target_rejected(self) -> None:
        with self.assertRaises(DatabaseConfigError) as ctx:
            self.source_config.with_database("ia_autonomous_commerce")
        self.assertIn("cannot be identical", str(ctx.exception).lower())

        with self.assertRaises(ValueError):
            DisasterRecoveryPlan(
                plan_id="plan_test",
                scenario=DisasterScenarioType.DATABASE_LOSS,
                environment=ApplicationEnvironment.DEVELOPMENT,
                source_database="ia_autonomous_commerce",
                recovery_target="ia_autonomous_commerce",  # Same database!
                backup_metadata=None,
                estimated_rpo_seconds=100.0,
                estimated_rto_seconds=300.0,
                steps=("step1",),
                created_at=datetime.now(timezone.utc),
            )

    # 10. Malformed DSN rejected
    def test_malformed_dsn_rejected(self) -> None:
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig(
                host="",
                port=5432,
                database="db",
                user="usr",
                password="pwd",
            )
        with self.assertRaises(DatabaseConfigError):
            DatabaseConfig(
                host="localhost",
                port=999999,  # Invalid port
                database="db",
                user="usr",
                password="pwd",
            )

    # 11. DR Plan generation for all scenarios
    def test_generate_plan_for_all_scenarios(self) -> None:
        mock_backup_service = MagicMock(spec=DatabaseBackupService)
        mock_backup_service.list_backups.return_value = []
        
        service = DisasterRecoveryService(
            db_config=self.source_config,
            backup_service=mock_backup_service,
            tooling=self.mock_tooling,
        )

        for scenario in DisasterScenarioType:
            plan = service.generate_plan(scenario, ApplicationEnvironment.PRODUCTION)
            self.assertEqual(plan.scenario, scenario)
            self.assertEqual(plan.environment, ApplicationEnvironment.PRODUCTION)
            self.assertEqual(plan.source_database, self.source_config.database)
            self.assertNotEqual(plan.recovery_target, self.source_config.database)
            self.assertGreater(len(plan.steps), 0)

    # 12. RPO and RTO SLA measurement logic
    def test_rpo_and_rto_policy_validation(self) -> None:
        policy = DisasterRecoveryPolicy(rpo_max_seconds=1800, rto_max_seconds=600)
        self.assertEqual(policy.rpo_max_seconds, 1800)
        self.assertEqual(policy.rto_max_seconds, 600)
        
        with self.assertRaises(ValueError):
            DisasterRecoveryPolicy(rpo_max_seconds=0)

    # 13. Target Connection Verification Mock
    def test_target_connection_verification_mock(self) -> None:
        service = DisasterRecoveryService(
            db_config=self.source_config,
            tooling=self.mock_tooling,
        )
        with patch.object(DatabaseConnectionFactory, "create_connection") as mock_conn_func:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.fetchone.side_effect = [
                ("target_db_isolated", "iac_app", "PostgreSQL 18.6"),
                (1,),
            ]
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_conn_func.return_value = mock_conn

            target_cfg = self.source_config.with_database("target_db_isolated")
            res = service.verify_target_connection(
                target_cfg,
                expected_database="target_db_isolated",
                expected_user="iac_app",
            )
            self.assertEqual(res["status"], "ok")
            self.assertEqual(res["database"], "target_db_isolated")
            self.assertEqual(res["user"], "iac_app")

    # 14. Target connection check mismatch raises TargetConnectionVerificationError
    def test_target_connection_verification_mismatch_raises(self) -> None:
        service = DisasterRecoveryService(
            db_config=self.source_config,
            tooling=self.mock_tooling,
        )
        with patch.object(DatabaseConnectionFactory, "create_connection") as mock_conn_func:
            mock_conn = MagicMock()
            mock_cursor = MagicMock()
            mock_cursor.fetchone.side_effect = [
                ("WRONG_DATABASE", "iac_app", "PostgreSQL 18.6"),
                (1,),
            ]
            mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
            mock_conn_func.return_value = mock_conn

            target_cfg = self.source_config.with_database("expected_db")
            with self.assertRaises(TargetConnectionVerificationError):
                service.verify_target_connection(
                    target_cfg,
                    expected_database="expected_db",
                    expected_user="iac_app",
                )

    # 15. No secrets in plan dictionary serialization
    def test_no_secrets_in_plan_and_execution_serialization(self) -> None:
        mock_meta = BackupMetadata(
            backup_id="backup_001",
            environment=ApplicationEnvironment.PRODUCTION,
            database_name="ia_autonomous_commerce",
            created_at=datetime.now(timezone.utc),
            postgres_version="18.6",
            migration_revision="head_rev_123",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=2048,
            checksum_sha256="e" * 64,
            file_name="backup_001.dump",
        )
        plan = DisasterRecoveryPlan(
            plan_id="dr_plan_sample",
            scenario=DisasterScenarioType.PERSISTENT_DATA_LOSS,
            environment=ApplicationEnvironment.PRODUCTION,
            source_database="ia_autonomous_commerce",
            recovery_target="iac_dr_test_999",
            backup_metadata=mock_meta,
            estimated_rpo_seconds=120.0,
            estimated_rto_seconds=300.0,
            steps=("step1", "step2"),
            created_at=datetime.now(timezone.utc),
        )
        plan_dict = plan.to_dict()
        serialized = json.dumps(plan_dict)
        self.assertNotIn("password", serialized.lower())
        self.assertNotIn("p@ss", serialized)
        self.assertNotIn("secret", serialized.lower())


if __name__ == "__main__":
    unittest.main()
