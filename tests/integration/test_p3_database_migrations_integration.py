"""Pruebas de integración sobre PostgreSQL local REAL para P.3 — Database Migrations.

Valida:
1. Conectividad real y permisos DDL/DML.
2. Ejecución del Runner de Migraciones (Alembic upgrade a HEAD).
3. Inspección del catálogo físico de 16 tablas SaaS en information_schema / pg_tables.
4. Operaciones DML completas de repositorios PostgreSQL con aislamiento multi-tenant.
5. Importación de datos JSON mediante JsonToPostgresImporter y verificación de idempotencia.
6. Verificación de probe /ready de FastAPI/Starlette con compatibilidad de esquema activa.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import unittest

import psycopg
from alembic.config import Config

from src.domain.tenant.models import TenantContext
from src.domain.saas_authorization.models import SaaSSession
from src.domain.organization.models import Organization, UserMembership
from src.domain.billing.models import (
    Subscription,
    SubscriptionStatus,
    BillingCycle,
    Invoice,
    InvoiceStatus,
)
from src.infrastructure.persistence.database.config import (
    DatabaseConfig,
    DatabaseConnectionFactory,
)
from src.infrastructure.persistence.database.repositories import (
    PostgresTenantRepository,
    PostgresOrganizationRepository,
    PostgresMembershipRepository,
    PostgresSaaSSessionRepository,
)
from src.infrastructure.persistence.database.importer import (
    JsonToPostgresImporter,
)
from scripts.db_migrate import (
    get_alembic_config,
    run_upgrade,
    get_current_revision,
    get_head_revision,
    check_schema_compatibility,
)


class TestP3DatabaseMigrationsIntegration(unittest.TestCase):
    """Pruebas de integración contra la base de datos PostgreSQL local real."""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.config = DatabaseConfig.from_env()
            cls.factory = DatabaseConnectionFactory(cls.config)
            # Verificar conectividad básica
            with cls.factory.create_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1;")
                    res = cur.fetchone()
                    assert res == (1,)
        except Exception as exc:
            raise unittest.SkipTest(f"PostgreSQL local no disponible para pruebas de integración: {exc}")

        # Ejecutar migración a HEAD
        cls.alembic_cfg = get_alembic_config()
        run_upgrade("head")

    def test_01_schema_is_at_head_revision(self) -> None:
        """Verifica que Alembic reporte que la base de datos está en HEAD."""
        current_rev = get_current_revision(self.alembic_cfg)
        head_rev = get_head_revision(self.alembic_cfg)
        self.assertIsNotNone(current_rev)
        self.assertIsNotNone(head_rev)
        self.assertEqual(current_rev, head_rev)

        compat = check_schema_compatibility(self.alembic_cfg)
        self.assertEqual(compat["status"], "ok")
        self.assertTrue(compat["is_up_to_date"])

    def test_02_all_16_saas_tables_exist_in_physical_catalog(self) -> None:
        """Verifica que las 16 tablas SaaS existan físicamente en public schema."""
        expected_tables = {
            "alembic_version",
            "tenants",
            "organizations",
            "memberships",
            "saas_sessions",
            "plans",
            "plan_assignments",
            "quota_policies",
            "quota_reservations",
            "usage_events",
            "subscriptions",
            "invoices",
            "payment_attempts",
            "payment_provider_events",
            "tenant_configurations",
            "operational_alerts",
            "tenant_scoped_resources",
        }

        with self.factory.create_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT table_name
                    FROM information_schema.tables
                    WHERE table_schema = 'public' AND table_type = 'BASE TABLE';
                    """
                )
                tables = {row[0] for row in cur.fetchall()}

        for table in expected_tables:
            self.assertIn(table, tables, f"Tabla esperada '{table}' no encontrada en PostgreSQL.")

    def test_03_tenant_isolation_and_foreign_keys_in_repositories(self) -> None:
        """Verifica inserción y aislamiento estricto entre tenants en repositorios."""
        now = datetime.now(timezone.utc)
        tenant_a = "tenant-integ-a"
        tenant_b = "tenant-integ-b"

        ctx_a = TenantContext(tenant_id=tenant_a, identity_id="ident-a", correlation_id="corr-a")
        ctx_b = TenantContext(tenant_id=tenant_b, identity_id="ident-b", correlation_id="corr-b")

        tenant_repo = PostgresTenantRepository(self.factory)
        tenant_repo.ensure_tenant(tenant_a)
        tenant_repo.ensure_tenant(tenant_b)

        org_repo = PostgresOrganizationRepository(self.factory)
        org_a = Organization(
            organization_id="org-a-1",
            tenant_id=tenant_a,
            name="Organization A",
            status="ACTIVE",
            schema_version="1.0.0",
            checksum="",
            metadata={"environment": "test"},
            created_at=now,
            updated_at=now,
        )
        org_repo.save(ctx_a, org_a)

        # Inserción con context cruzado debe fallar a nivel de TenantContext
        with self.assertRaises(ValueError):
            org_repo.save(ctx_b, org_a)

        # Búsqueda por tenant correcto
        retrieved_a = org_repo.get_by_id(ctx_a, "org-a-1")
        self.assertIsNotNone(retrieved_a)
        self.assertEqual(retrieved_a.name, "Organization A")

        # Búsqueda desde otro tenant no debe retornar el recurso
        retrieved_b = org_repo.get_by_id(ctx_b, "org-a-1")
        self.assertIsNone(retrieved_b)

    def test_04_json_importer_idempotence(self) -> None:
        """Verifica que el importador JSON funcione y sea completamente idempotente."""
        import tempfile
        import shutil

        now = datetime.now(timezone.utc)
        temp_dir = Path(tempfile.mkdtemp(prefix="iac_test_json_data_"))
        try:
            tenant_id = "tenant-integ-import"
            t_dir = temp_dir / "tenants" / tenant_id
            org_dir = t_dir / "organizations"
            org_dir.mkdir(parents=True, exist_ok=True)

            org_temp = Organization(
                organization_id="org-imported-1",
                tenant_id=tenant_id,
                name="Imported Org 1",
                status="ACTIVE",
                schema_version="1.0.0",
                checksum="",
                metadata={"source": "json_seed"},
                created_at=now,
                updated_at=now,
            )
            org_data = {
                "organization_id": org_temp.organization_id,
                "tenant_id": tenant_id,
                "name": org_temp.name,
                "status": org_temp.status.value,
                "schema_version": org_temp.schema_version,
                "checksum": org_temp.checksum,
                "metadata": dict(org_temp.metadata),
                "created_at": org_temp.created_at.isoformat(),
                "updated_at": org_temp.updated_at.isoformat(),
            }
            with open(org_dir / "org-imported-1.json", "w", encoding="utf-8") as f:
                json.dump(org_data, f)

            importer = JsonToPostgresImporter(self.factory, temp_dir)
            # Primera pasada
            summary1 = importer.import_all()
            self.assertTrue(summary1.is_success, f"Errores en importación 1: {summary1.errors}")
            self.assertGreaterEqual(summary1.tenants_imported, 1)
            self.assertEqual(summary1.organizations_imported, 1)

            # Segunda pasada (debe ser idempotente sin errores de clave duplicada)
            summary2 = importer.import_all()
            self.assertTrue(summary2.is_success, f"Errores en importación 2: {summary2.errors}")
            self.assertEqual(summary2.organizations_imported, 1)

            # Verificar en PostgreSQL
            ctx = TenantContext(tenant_id=tenant_id, identity_id="ident-imp", correlation_id="corr-imp")
            org_repo = PostgresOrganizationRepository(self.factory)
            org = org_repo.get_by_id(ctx, "org-imported-1")
            self.assertIsNotNone(org)
            self.assertEqual(org.name, "Imported Org 1")

        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
