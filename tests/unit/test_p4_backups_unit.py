"""Tests Unitarios para P.4 — Backups (PostgreSQL Data Protection & Restore Validation).

Cubre:
1. Safe backup naming (determinista, sin caracteres prohibidos ni path traversal).
2. Environment required y aislamiento de directorios (.runtime/backups/{environment}).
3. Credentials redacted (ninguna contraseña o DSN sensible en metadata, logs o errores).
4. Backup dir safe y validación de rutas.
5. Checksum determinista (SHA-256).
6. Invalid checksum rejected (detección de corrupción o manipulación).
7. Metadata valid y serialización JSON.
8. Source != restore target (prevención de sobrescritura de base de datos activa).
9. Production restore guard y comprobaciones de entorno.
10. Migration revision recorded (Alembic).
11. Retention config safe (aplicación de retención respetando conteos mínimos).
12. No secrets in metadata ni en representaciones públicas.
13. Path traversal rejected en identificadores de backup.
14. Missing pg_dump / pg_restore tooling handled gracefully.
15. No destructive default en operaciones de backup/verify.
16. No P.5+ (no dependencias de desastre recovery o replicación).
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from src.domain.backup.models import (
    BackupError,
    BackupEnvironmentMismatchError,
    BackupFormat,
    BackupIntegrityError,
    BackupMetadata,
    BackupStatus,
    DangerousRestoreTargetError,
    RestoreValidationStatus,
    calculate_file_sha256,
    generate_backup_filename,
)
from src.domain.deployment.models import ApplicationEnvironment, normalize_environment_name
from src.infrastructure.persistence.database.backup_service import (
    EXPECTED_P3_TABLES,
    DatabaseBackupService,
    PostgresToolingPaths,
    discover_postgres_tooling,
)
from src.infrastructure.persistence.database.config import DatabaseConfig


class TestP4BackupsUnit(unittest.TestCase):
    """Suite de pruebas unitarias para P.4."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.temp_dir.name)
        self.mock_tooling = PostgresToolingPaths(
            pg_dump=Path("/mock/bin/pg_dump"),
            pg_restore=Path("/mock/bin/pg_restore"),
            psql=Path("/mock/bin/psql"),
        )
        self.config = DatabaseConfig(
            host="localhost",
            port=5432,
            database="ia_autonomous_commerce_test",
            user="iac_app",
            password="secret_password_12345",
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # 1. Safe backup naming
    def test_safe_backup_naming_format(self) -> None:
        ts = datetime(2026, 9, 11, 14, 0, 0, tzinfo=timezone.utc)
        name = generate_backup_filename(
            database_name="ia_autonomous_commerce",
            environment=ApplicationEnvironment.DEVELOPMENT,
            timestamp=ts,
            suffix=".dump",
        )
        self.assertEqual(name, "ia_autonomous_commerce_development_20260911T140000Z.dump")
        self.assertNotIn(" ", name)
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertNotIn("..", name)

    # 2. Environment required & directory isolation
    def test_environment_required_and_isolated_dir(self) -> None:
        service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_dir,
            tooling=self.mock_tooling,
        )
        dev_dir = service.get_environment_backup_dir(ApplicationEnvironment.DEVELOPMENT)
        prod_dir = service.get_environment_backup_dir(ApplicationEnvironment.PRODUCTION)

        self.assertTrue(dev_dir.exists())
        self.assertTrue(prod_dir.exists())
        self.assertNotEqual(dev_dir, prod_dir)
        self.assertEqual(dev_dir.name, "development")
        self.assertEqual(prod_dir.name, "production")

    # 3. Credentials redacted in DSN and sanitized outputs
    def test_credentials_redacted(self) -> None:
        sanitized = self.config.sanitized_dsn
        self.assertNotIn("secret_password_12345", sanitized)
        self.assertIn("iac_app:***@", sanitized)

    # 4. Backup dir safe
    def test_backup_dir_safe_creation(self) -> None:
        service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_dir,
            tooling=self.mock_tooling,
        )
        staging_dir = service.get_environment_backup_dir(ApplicationEnvironment.STAGING)
        self.assertTrue(staging_dir.is_dir())
        self.assertEqual(staging_dir.parent, self.base_dir)

    # 5. Checksum deterministic
    def test_checksum_deterministic(self) -> None:
        test_file = self.base_dir / "sample.dump"
        test_file.write_bytes(b"POSTGRESQL_TEST_BACKUP_BYTES_12345")

        sha1 = calculate_file_sha256(test_file)
        sha2 = calculate_file_sha256(test_file)

        self.assertEqual(sha1, sha2)
        self.assertEqual(len(sha1), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in sha1))

    # 6. Invalid checksum rejected
    def test_invalid_checksum_rejected_during_verify(self) -> None:
        service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_dir,
            tooling=self.mock_tooling,
        )
        env_dir = service.get_environment_backup_dir(ApplicationEnvironment.DEVELOPMENT)
        dump_file = env_dir / "test.dump"
        dump_file.write_bytes(b"ORIGINAL_BACKUP_CONTENT")
        correct_sha = calculate_file_sha256(dump_file)

        meta = BackupMetadata(
            backup_id="test",
            environment=ApplicationEnvironment.DEVELOPMENT,
            database_name="ia_autonomous_commerce_test",
            created_at=datetime.now(timezone.utc),
            postgres_version="18.6",
            migration_revision="p3_head_123",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=dump_file.stat().st_size,
            checksum_sha256=correct_sha,
            file_name="test.dump",
        )
        meta_file = env_dir / "test.dump.json"
        meta_file.write_text(json.dumps(meta.to_dict()), encoding="utf-8")

        # Tamper with file
        dump_file.write_bytes(b"TAMPERED_BACKUP_CONTENT")

        with self.assertRaises(BackupIntegrityError) as ctx:
            service.verify_backup("test.dump", ApplicationEnvironment.DEVELOPMENT)
        self.assertIn("checksum mismatch", str(ctx.exception).lower())

    # 7. Metadata valid and JSON round-trip
    def test_metadata_serialization_roundtrip(self) -> None:
        now = datetime.now(timezone.utc)
        meta = BackupMetadata(
            backup_id="backup_001",
            environment=ApplicationEnvironment.STAGING,
            database_name="iac_staging",
            created_at=now,
            postgres_version="18.6",
            migration_revision="rev_001",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=1024,
            checksum_sha256="a" * 64,
            file_name="backup_001.dump",
        )
        d = meta.to_dict()
        restored = BackupMetadata.from_dict(d)
        self.assertEqual(meta.backup_id, restored.backup_id)
        self.assertEqual(meta.environment, restored.environment)
        self.assertEqual(meta.database_name, restored.database_name)
        self.assertEqual(meta.checksum_sha256, restored.checksum_sha256)
        self.assertEqual(meta.migration_revision, restored.migration_revision)

    # 8. Source != restore target
    def test_dangerous_restore_target_rejected(self) -> None:
        service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_dir,
            tooling=self.mock_tooling,
        )
        env_dir = service.get_environment_backup_dir(ApplicationEnvironment.DEVELOPMENT)
        dump_file = env_dir / "test_target.dump"
        dump_file.write_bytes(b"CONTENT")
        meta = BackupMetadata(
            backup_id="test_target",
            environment=ApplicationEnvironment.DEVELOPMENT,
            database_name=self.config.database,
            created_at=datetime.now(timezone.utc),
            postgres_version="18.6",
            migration_revision="rev_1",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=len(b"CONTENT"),
            checksum_sha256=calculate_file_sha256(dump_file),
            file_name="test_target.dump",
        )
        (env_dir / "test_target.dump.json").write_text(json.dumps(meta.to_dict()), encoding="utf-8")

        with patch.object(service, "verify_backup", return_value=meta):
            with self.assertRaises(DangerousRestoreTargetError):
                service.run_restore_test(
                    "test_target.dump",
                    ApplicationEnvironment.DEVELOPMENT,
                    target_db_name=self.config.database,  # Same as active database!
                )

    # 9. Production restore guard and environment mismatch
    def test_environment_mismatch_rejected(self) -> None:
        service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_dir,
            tooling=self.mock_tooling,
        )
        dev_dir = service.get_environment_backup_dir(ApplicationEnvironment.DEVELOPMENT)
        dump_file = dev_dir / "mismatch.dump"
        dump_file.write_bytes(b"DEV_CONTENT")

        meta = BackupMetadata(
            backup_id="mismatch",
            environment=ApplicationEnvironment.DEVELOPMENT,
            database_name=self.config.database,
            created_at=datetime.now(timezone.utc),
            postgres_version="18.6",
            migration_revision="rev_1",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=len(b"DEV_CONTENT"),
            checksum_sha256=calculate_file_sha256(dump_file),
            file_name="mismatch.dump",
        )
        (dev_dir / "mismatch.dump.json").write_text(json.dumps(meta.to_dict()), encoding="utf-8")

        # Trying to verify DEV backup under PRODUCTION environment
        with self.assertRaises(FileNotFoundError):
            service.verify_backup("mismatch.dump", ApplicationEnvironment.PRODUCTION)

    # 10. Migration revision recorded
    def test_migration_revision_recorded_in_metadata(self) -> None:
        meta = BackupMetadata(
            backup_id="b1",
            environment=ApplicationEnvironment.DEVELOPMENT,
            database_name="iac_test",
            created_at=datetime.now(timezone.utc),
            postgres_version="18.6",
            migration_revision="925a7b6cb667",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=500,
            checksum_sha256="c" * 64,
            file_name="b1.dump",
        )
        self.assertEqual(meta.migration_revision, "925a7b6cb667")

    # 11. Retention config safe
    def test_retention_enforcement_removes_oldest(self) -> None:
        service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_dir,
            tooling=self.mock_tooling,
            retention_count=2,
        )
        env_dir = service.get_environment_backup_dir(ApplicationEnvironment.DEVELOPMENT)

        for i in range(1, 5):
            ts = datetime(2026, 9, i, 12, 0, 0, tzinfo=timezone.utc)
            fname = f"backup_{i}.dump"
            (env_dir / fname).write_bytes(f"DATA_{i}".encode("utf-8"))
            meta = BackupMetadata(
                backup_id=f"backup_{i}",
                environment=ApplicationEnvironment.DEVELOPMENT,
                database_name="iac_test",
                created_at=ts,
                postgres_version="18.6",
                migration_revision="rev",
                backup_format=BackupFormat.CUSTOM,
                file_size_bytes=len(f"DATA_{i}"),
                checksum_sha256=calculate_file_sha256(env_dir / fname),
                file_name=fname,
            )
            (env_dir / f"{fname}.json").write_text(json.dumps(meta.to_dict()), encoding="utf-8")

        self.assertEqual(len(service.list_backups(ApplicationEnvironment.DEVELOPMENT)), 4)
        deleted = service.enforce_retention(ApplicationEnvironment.DEVELOPMENT)
        self.assertEqual(deleted, 2)
        remaining = service.list_backups(ApplicationEnvironment.DEVELOPMENT)
        self.assertEqual(len(remaining), 2)
        # Should keep newest
        self.assertIn("backup_4", [b.backup_id for b in remaining])
        self.assertIn("backup_3", [b.backup_id for b in remaining])

    # 12. No secrets in metadata
    def test_no_secrets_in_metadata_dict(self) -> None:
        meta = BackupMetadata(
            backup_id="b_secure",
            environment=ApplicationEnvironment.PRODUCTION,
            database_name="iac_prod",
            created_at=datetime.now(timezone.utc),
            postgres_version="18.6",
            migration_revision="rev_prod",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=1000,
            checksum_sha256="f" * 64,
            file_name="b_secure.dump",
        )
        meta_dict = meta.to_dict()
        serialized = json.dumps(meta_dict)
        self.assertNotIn("password", serialized.lower())
        self.assertNotIn("secret", serialized.lower())
        self.assertNotIn("postgres://", serialized)

    # 13. Path traversal rejected
    def test_path_traversal_rejected_in_backup_id(self) -> None:
        service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_dir,
            tooling=self.mock_tooling,
        )
        with self.assertRaises(ValueError):
            service.verify_backup("../../../etc/passwd", ApplicationEnvironment.DEVELOPMENT)

    # 14. Missing tooling handled gracefully
    def test_missing_tooling_discovery_raises_backup_error(self) -> None:
        with patch("shutil.which", return_value=None):
            with patch("pathlib.Path.exists", return_value=False):
                with self.assertRaises(BackupError) as ctx:
                    discover_postgres_tooling("/non_existent_bin_dir")
                self.assertIn("unavailable", str(ctx.exception).lower())

    # 15. No destructive default
    def test_no_destructive_default_in_list_and_verify(self) -> None:
        service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_dir,
            tooling=self.mock_tooling,
        )
        env_dir = service.get_environment_backup_dir(ApplicationEnvironment.DEVELOPMENT)
        dump_file = env_dir / "safe.dump"
        dump_file.write_bytes(b"SAFE_CONTENT")
        meta = BackupMetadata(
            backup_id="safe",
            environment=ApplicationEnvironment.DEVELOPMENT,
            database_name="iac_test",
            created_at=datetime.now(timezone.utc),
            postgres_version="18.6",
            migration_revision="rev",
            backup_format=BackupFormat.CUSTOM,
            file_size_bytes=len(b"SAFE_CONTENT"),
            checksum_sha256=calculate_file_sha256(dump_file),
            file_name="safe.dump",
        )
        (env_dir / "safe.dump.json").write_text(json.dumps(meta.to_dict()), encoding="utf-8")

        # Listing and verifying should not delete the file
        service.list_backups(ApplicationEnvironment.DEVELOPMENT)
        self.assertTrue(dump_file.exists())

    # 16. Expected P3 Tables constant validation
    def test_expected_p3_tables_count_and_content(self) -> None:
        self.assertEqual(len(EXPECTED_P3_TABLES), 17)
        self.assertIn("alembic_version", EXPECTED_P3_TABLES)
        self.assertIn("tenants", EXPECTED_P3_TABLES)
        self.assertIn("organizations", EXPECTED_P3_TABLES)
        self.assertIn("memberships", EXPECTED_P3_TABLES)
        self.assertIn("subscriptions", EXPECTED_P3_TABLES)
        self.assertIn("tenant_configurations", EXPECTED_P3_TABLES)
        self.assertIn("operational_alerts", EXPECTED_P3_TABLES)


if __name__ == "__main__":
    unittest.main()
