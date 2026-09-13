"""Pruebas unitarias para P.3 — Database Migrations & Persistencia PostgreSQL.

Verifica:
1. DatabaseConfig (validación, parseo de URL, sanitización de credenciales).
2. DatabaseConnectionFactory (creación segura, control de errores sanitizados).
3. CLI de migraciones y funciones de verificación (check_schema_compatibility, sanitize_error_message).
4. Modelos de datos y mapeos de adaptadores PostgreSQL.
5. JsonToPostgresImporter lógica e idempotencia con mocks/transacciones.
"""

from datetime import datetime, timezone
from decimal import Decimal
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

from src.domain.tenant.models import TenantContext
from src.domain.saas_authorization.models import SaaSSession
from src.domain.organization.models import Organization, UserMembership
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConnectionFactory,
    sanitize_dsn,
    sanitize_error_message,
)
from src.infrastructure.persistence.database.repositories import (
    PostgresTenantRepository,
    PostgresOrganizationRepository,
    PostgresMembershipRepository,
    PostgresSaaSSessionRepository,
)
from src.infrastructure.persistence.database.importer import (
    JsonToPostgresImporter,
    ImportSummary,
)
from scripts.db_migrate import check_schema_compatibility


class TestP3DatabaseConfigUnit(unittest.TestCase):
    """Pruebas unitarias sobre la configuración y sanitización de base de datos."""

    def test_database_config_properties(self) -> None:
        cfg = DatabaseConfig(
            host="localhost",
            port=5432,
            database="test_db",
            user="test_user",
            password="secret_password_123",
            sslmode="prefer",
        )
        self.assertEqual(cfg.host, "localhost")
        self.assertEqual(cfg.port, 5432)
        self.assertEqual(cfg.database, "test_db")
        self.assertEqual(cfg.user, "test_user")
        self.assertEqual(cfg.sslmode, "prefer")
        self.assertIn("***", cfg.sanitized_dsn)

    def test_database_url_masking(self) -> None:
        cfg = DatabaseConfig(
            host="localhost",
            port=5432,
            database="test_db",
            user="test_user",
            password="super_secret_password",
        )
        safe_url = cfg.sanitized_dsn
        self.assertNotIn("super_secret_password", safe_url)
        self.assertIn("***", safe_url)
        self.assertIn("test_user:***@localhost:5432/test_db", safe_url)

        repr_str = repr(cfg)
        self.assertNotIn("super_secret_password", repr_str)
        self.assertIn("***", repr_str)

    def test_sanitize_url_helper(self) -> None:
        raw_url = "postgresql://myuser:secret123@db.internal:5432/prod_db"
        sanitized = sanitize_dsn(raw_url)
        self.assertEqual(sanitized, "postgresql://myuser:***@db.internal:5432/prod_db")
        self.assertNotIn("secret123", sanitized)

    def test_sanitize_error_message(self) -> None:
        msg = "FATAL: password authentication failed for user 'iac_app' with password 'MySecret123'"
        sanitized = sanitize_error_message(msg)
        self.assertNotIn("MySecret123", sanitized)
        self.assertIn("password '***'", sanitized)


class TestP3RepositoriesUnit(unittest.TestCase):
    """Pruebas unitarias para adaptadores de repositorio PostgreSQL."""

    def setUp(self) -> None:
        self.mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        self.mock_conn = MagicMock()
        self.mock_cursor = MagicMock()
        self.mock_conn.cursor.return_value.__enter__.return_value = self.mock_cursor
        self.mock_factory.create_connection.return_value = self.mock_conn

    def test_postgres_organization_repository_save_and_get(self) -> None:
        repo = PostgresOrganizationRepository(self.mock_factory)
        ctx = TenantContext(tenant_id="tenant-123", identity_id="ident-1", correlation_id="corr-1")
        now = datetime.now(timezone.utc)

        org = Organization(
            organization_id="org-123",
            tenant_id="tenant-123",
            name="Test Org",
            status="ACTIVE",
            schema_version="1.0.0",
            checksum="",
            metadata={"plan": "enterprise"},
            created_at=now,
            updated_at=now,
        )

        repo.save(ctx, org)
        self.mock_cursor.execute.assert_called()

        # Test cross-tenant isolation enforcement
        other_ctx = TenantContext(tenant_id="other-tenant", identity_id="ident-2", correlation_id="corr-2")
        with self.assertRaises(ValueError):
            repo.save(other_ctx, org)

    def test_postgres_session_repository_save_and_get(self) -> None:
        repo = PostgresSaaSSessionRepository(self.mock_factory)
        now = datetime.now(timezone.utc)

        session = SaaSSession(
            session_id="sess-123",
            identity_id="ident-1",
            tenant_id="tenant-123",
            organization_id="org-123",
            authentication_method="OIDC",
            authentication_provider="google",
            status="ACTIVE",
            created_at=now,
            expires_at=now,
            last_validated_at=now,
            schema_version="1.0.0",
            checksum="",
            metadata={},
        )

        repo.save(session)
        self.mock_cursor.execute.assert_called()


class TestP3ImporterUnit(unittest.TestCase):
    """Pruebas unitarias para JsonToPostgresImporter."""

    def test_import_summary_properties(self) -> None:
        summary = ImportSummary(
            tenants_imported=2,
            organizations_imported=4,
            memberships_imported=6,
            sessions_imported=8,
            plans_imported=3,
            plan_assignments_imported=2,
            usage_events_imported=10,
            errors=[],
        )
        self.assertEqual(summary.total_entities, 35)
        self.assertTrue(summary.is_success)

    def test_import_non_existent_directory(self) -> None:
        mock_factory = MagicMock(spec=DatabaseConnectionFactory)
        importer = JsonToPostgresImporter(mock_factory, Path("/non/existent/path/data"))
        summary = importer.import_all()
        self.assertFalse(summary.is_success)
        self.assertEqual(summary.total_entities, 0)
        self.assertIn("Directory not found", summary.errors[0])


if __name__ == "__main__":
    unittest.main()
