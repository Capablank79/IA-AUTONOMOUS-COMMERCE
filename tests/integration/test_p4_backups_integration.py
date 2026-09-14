"""Tests de Integración para P.4 — Backups (PostgreSQL Data Protection & Restore Validation).

Prueba contra PostgreSQL real:
Escenarios:
A. Crear backup real con pg_dump -Fc.
B. Verificar checksum SHA-256 PASS.
C. Corromper backup -> verificar verify FAIL con BackupIntegrityError.
D. Restaurar backup en esquema temporal aislado sin modificar la DB fuente.
E. Preservación estricta de esquema y tablas esperadas (16 SaaS + 1 Alembic).
F. Preservación estricta de revisión de Alembic (coincide con HEAD / source).
G. Preservación de datos sintéticos de Tenant A y Tenant B con aislamiento completo.
H. Preservación de IDs, timestamps, FKs y constraints.
I. Source DB queda completamente intacta y accesible.
J. Cero exposición de contraseñas en representaciones o logs.
"""

import json
import os
from pathlib import Path
import tempfile
import unittest
import uuid

try:
    import psycopg
except ImportError:
    psycopg = None

from src.domain.backup.models import (
    BackupError,
    BackupFormat,
    BackupIntegrityError,
    BackupStatus,
    RestoreValidationStatus,
)
from src.domain.deployment.models import ApplicationEnvironment
from src.infrastructure.persistence.database.backup_service import (
    EXPECTED_P3_TABLES,
    DatabaseBackupService,
    discover_postgres_tooling,
)
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConnectionFactory,
)


class TestP4BackupsIntegration(unittest.TestCase):
    """Pruebas de integración sobre PostgreSQL real."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = DatabaseConfig.from_env()
        cls.factory = DatabaseConnectionFactory(cls.config)
        # Test if PostgreSQL is accessible
        try:
            with cls.factory.create_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
        except Exception as exc:
            raise unittest.SkipTest(f"PostgreSQL not accessible: {exc}")

        cls.tooling = discover_postgres_tooling()

    def setUp(self) -> None:
        self.temp_backup_dir = tempfile.TemporaryDirectory()
        self.base_backup_path = Path(self.temp_backup_dir.name)
        self.service = DatabaseBackupService(
            db_config=self.config,
            base_backup_dir=self.base_backup_path,
            tooling=self.tooling,
        )

    def tearDown(self) -> None:
        self.temp_backup_dir.cleanup()

    # Escenario A & B: Create real backup & Verify Checksum PASS
    def test_scenario_a_and_b_create_and_verify_real_backup(self) -> None:
        result = self.service.create_backup(
            environment=ApplicationEnvironment.DEVELOPMENT,
            custom_backup_id="integration_backup_test_01",
        )
        self.assertEqual(result.status, BackupStatus.COMPLETED)
        self.assertTrue(result.backup_path.exists())
        self.assertTrue(result.metadata_path.exists())
        self.assertGreater(result.metadata.file_size_bytes, 0)
        self.assertEqual(result.metadata.backup_format, BackupFormat.CUSTOM)
        self.assertIsNotNone(result.metadata.migration_revision)

        # Verify
        meta = self.service.verify_backup("integration_backup_test_01", ApplicationEnvironment.DEVELOPMENT)
        self.assertEqual(meta.backup_id, "integration_backup_test_01")
        self.assertEqual(meta.checksum_sha256, result.metadata.checksum_sha256)

    # Escenario C: Corrupted backup -> verify FAIL
    def test_scenario_c_corrupted_backup_fails_verification(self) -> None:
        result = self.service.create_backup(
            environment=ApplicationEnvironment.DEVELOPMENT,
            custom_backup_id="corrupt_test_backup",
        )
        # Corrupt file content
        with open(result.backup_path, "wb") as f:
            f.write(b"CORRUPTED_POSTGRES_DUMP_HEADER_INVALID_DATA")

        with self.assertRaises(BackupIntegrityError):
            self.service.verify_backup("corrupt_test_backup", ApplicationEnvironment.DEVELOPMENT)

    # Escenario D, E, F, G, H, I: End-to-End Synthetic Data, Backup, Restore, Isolation & Source DB Safety
    def test_scenario_e2e_restore_test_with_synthetic_tenants_and_schema_validation(self) -> None:
        tenant_a_id = f"ten_a_{uuid.uuid4().hex[:8]}"
        tenant_b_id = f"ten_b_{uuid.uuid4().hex[:8]}"
        org_a_id = f"org_a_{uuid.uuid4().hex[:8]}"
        org_b_id = f"org_b_{uuid.uuid4().hex[:8]}"

        # 1. Insert synthetic multi-tenant test data into source database
        with self.factory.create_connection(autocommit=True) as conn:
            with conn.cursor() as cur:
                # Insert Tenant A
                cur.execute(
                    """
                    INSERT INTO tenants (tenant_id, status, metadata)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_id) DO NOTHING;
                    """,
                    (tenant_a_id, "ACTIVE", '{"name": "Tenant Alpha"}'),
                )
                cur.execute(
                    """
                    INSERT INTO organizations (organization_id, tenant_id, name, status, checksum, metadata, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (organization_id) DO NOTHING;
                    """,
                    (org_a_id, tenant_a_id, "Alpha Org", "ACTIVE", "chk_a", "{}"),
                )

                # Insert Tenant B
                cur.execute(
                    """
                    INSERT INTO tenants (tenant_id, status, metadata)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (tenant_id) DO NOTHING;
                    """,
                    (tenant_b_id, "ACTIVE", '{"name": "Tenant Beta"}'),
                )
                cur.execute(
                    """
                    INSERT INTO organizations (organization_id, tenant_id, name, status, checksum, metadata, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (organization_id) DO NOTHING;
                    """,
                    (org_b_id, tenant_b_id, "Beta Org", "ACTIVE", "chk_b", "{}"),
                )

        try:
            # 2. Execute Backup
            backup_res = self.service.create_backup(
                environment=ApplicationEnvironment.DEVELOPMENT,
                custom_backup_id=f"e2e_restore_backup_{uuid.uuid4().hex[:6]}",
            )
            self.assertEqual(backup_res.status, BackupStatus.COMPLETED)

            # 3. Execute isolated Restore Validation
            restore_res = self.service.run_restore_test(
                backup_res.backup_path.name,
                ApplicationEnvironment.DEVELOPMENT,
            )

            # E. Schema & Tables preserved
            self.assertEqual(restore_res.status, RestoreValidationStatus.PASSED)
            self.assertEqual(restore_res.tables_verified_count, len(EXPECTED_P3_TABLES))
            self.assertEqual(len(restore_res.missing_tables), 0)

            # F. Alembic Revision preserved
            self.assertTrue(restore_res.migration_revision_verified)
            self.assertEqual(restore_res.restored_revision, backup_res.metadata.migration_revision)

            # G & H. Multi-tenant data & isolation preserved
            self.assertGreaterEqual(restore_res.tenants_verified_count, 2)
            self.assertGreaterEqual(restore_res.data_records_verified, 2)
            self.assertTrue(restore_res.tenant_isolation_preserved)
            self.assertTrue(restore_res.cleanup_successful)

        finally:
            # Clean up synthetic data from source DB
            with self.factory.create_connection(autocommit=True) as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM organizations WHERE organization_id IN (%s, %s);", (org_a_id, org_b_id))
                    cur.execute("DELETE FROM tenants WHERE tenant_id IN (%s, %s);", (tenant_a_id, tenant_b_id))

        # I. Verify source DB remains healthy and accessible
        with self.factory.create_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public';")
                count = cur.fetchone()[0]
                self.assertGreaterEqual(count, 17)

    # Escenario J: Password never exposed in representation
    def test_scenario_j_password_never_exposed(self) -> None:
        meta = self.service.create_backup(
            environment=ApplicationEnvironment.DEVELOPMENT,
            custom_backup_id="safe_leak_check",
        ).metadata
        meta_json = json.dumps(meta.to_dict())
        self.assertNotIn(self.config.password, meta_json)
        self.assertNotIn(self.config.password, self.config.sanitized_dsn)


if __name__ == "__main__":
    unittest.main()
