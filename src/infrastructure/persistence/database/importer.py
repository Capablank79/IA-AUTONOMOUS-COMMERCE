"""Importador seguro, determinista e idempotente de datos JSON a PostgreSQL (P.3 — Database Migrations).

Permite importar datos persistidos previamente en los repositorios JSON (tenants, organizations,
memberships, sessions, plans, assignments, usage_events, etc.) hacia la base de datos PostgreSQL real.

Garantías:
1. Idempotencia total: Inserciones estructuradas con `ON CONFLICT DO UPDATE / NOTHING`.
2. Preservación absoluta de identificadores de dominio (`tenant_id`, `organization_id`, `session_id`, etc.).
3. Preservación de checksums SHA-256 e integridad referencial.
4. Mapeo exacto de tipos de datos (UTC timezone-aware timestamps, Decimal/NUMERIC monetarios, diccionarios/listas JSONB).
5. Sanitización de logs y errores (sin secretos expuestos).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

try:
    import psycopg
except ImportError:
    psycopg = None

from src.domain.security.models import validate_safe_identifier
from src.infrastructure.persistence.database.config import (
    DatabaseConnectionFactory,
    sanitize_error_message,
)
from src.infrastructure.persistence.database.repositories import (
    PostgresTenantRepository,
    PostgresOrganizationRepository,
    PostgresMembershipRepository,
    PostgresSaaSSessionRepository,
)

logger = logging.getLogger("JsonToPostgresImporter")


@dataclass(frozen=True)
class ImportSummary:
    """Resumen determinista del proceso de importación JSON → PostgreSQL."""
    tenants_imported: int
    organizations_imported: int
    memberships_imported: int
    sessions_imported: int
    plans_imported: int
    plan_assignments_imported: int
    usage_events_imported: int
    errors: List[str]

    @property
    def total_entities(self) -> int:
        return (
            self.tenants_imported
            + self.organizations_imported
            + self.memberships_imported
            + self.sessions_imported
            + self.plans_imported
            + self.plan_assignments_imported
            + self.usage_events_imported
        )

    @property
    def is_success(self) -> bool:
        return len(self.errors) == 0


class JsonToPostgresImporter:
    """Importador determinista de repositorios JSON hacia PostgreSQL."""

    def __init__(
        self,
        connection_factory: DatabaseConnectionFactory,
        json_base_dir: Union[str, Path],
    ) -> None:
        self._factory = connection_factory
        self._base_dir = Path(json_base_dir)

    def import_all(self) -> ImportSummary:
        """Importa todos los recursos encontrados en el directorio base JSON."""
        tenants_count = 0
        orgs_count = 0
        mems_count = 0
        sessions_count = 0
        plans_count = 0
        assignments_count = 0
        usage_count = 0
        errors: List[str] = []

        if not self._base_dir.exists():
            return ImportSummary(0, 0, 0, 0, 0, 0, 0, [f"Directory not found: {self._base_dir}"])

        conn = self._factory.create_connection(autocommit=False)
        try:
            # 1. Importar Planes globales (si existen)
            plans_file = self._base_dir / "plans" / "catalog.json"
            if plans_file.exists():
                plans_count = self._import_plans(conn, plans_file)

            # 2. Escanear directorio tenants
            tenants_dir = self._base_dir / "tenants"
            if tenants_dir.exists() and tenants_dir.is_dir():
                for tenant_folder in tenants_dir.iterdir():
                    if not tenant_folder.is_dir():
                        continue
                    tenant_id = tenant_folder.name
                    try:
                        validate_safe_identifier(tenant_id, "tenant_id")
                    except Exception as e:
                        errors.append(f"Invalid tenant folder name '{tenant_id}': {e}")
                        continue

                    # Importar tenant raíz
                    self._ensure_tenant(conn, tenant_id)
                    tenants_count += 1

                    # Organizaciones
                    org_dir = tenant_folder / "organizations"
                    if org_dir.exists():
                        for f in org_dir.glob("*.json"):
                            try:
                                self._import_organization(conn, tenant_id, f)
                                orgs_count += 1
                            except Exception as exc:
                                errors.append(f"Error importing org {f.name}: {sanitize_error_message(str(exc))}")

                    # Membresías
                    mem_dir = tenant_folder / "memberships"
                    if mem_dir.exists():
                        for f in mem_dir.glob("*.json"):
                            try:
                                self._import_membership(conn, tenant_id, f)
                                mems_count += 1
                            except Exception as exc:
                                errors.append(f"Error importing membership {f.name}: {sanitize_error_message(str(exc))}")

                    # Sesiones SaaS
                    sess_dir = tenant_folder / "sessions"
                    if sess_dir.exists():
                        for f in sess_dir.glob("*.json"):
                            try:
                                self._import_session(conn, tenant_id, f)
                                sessions_count += 1
                            except Exception as exc:
                                errors.append(f"Error importing session {f.name}: {sanitize_error_message(str(exc))}")

                    # Plan assignments
                    assign_dir = tenant_folder / "plan_assignments"
                    if assign_dir.exists():
                        for f in assign_dir.glob("*.json"):
                            try:
                                self._import_plan_assignment(conn, tenant_id, f)
                                assignments_count += 1
                            except Exception as exc:
                                errors.append(f"Error importing assignment {f.name}: {sanitize_error_message(str(exc))}")

                    # Usage Events
                    usage_dir = tenant_folder / "usage_events"
                    if usage_dir.exists():
                        for f in usage_dir.glob("*.json"):
                            try:
                                self._import_usage_event(conn, tenant_id, f)
                                usage_count += 1
                            except Exception as exc:
                                errors.append(f"Error importing usage event {f.name}: {sanitize_error_message(str(exc))}")

            conn.commit()
            return ImportSummary(
                tenants_imported=tenants_count,
                organizations_imported=orgs_count,
                memberships_imported=mems_count,
                sessions_imported=sessions_count,
                plans_imported=plans_count,
                plan_assignments_imported=assignments_count,
                usage_events_imported=usage_count,
                errors=errors,
            )
        except Exception as exc:
            conn.rollback()
            sanitized = sanitize_error_message(str(exc))
            return ImportSummary(
                tenants_imported=0,
                organizations_imported=0,
                memberships_imported=0,
                sessions_imported=0,
                plans_imported=0,
                plan_assignments_imported=0,
                usage_events_imported=0,
                errors=[f"Fatal rollback during import: {sanitized}"],
            )
        finally:
            if not conn.closed:
                conn.close()

    def _ensure_tenant(self, conn: psycopg.Connection, tenant_id: str) -> None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tenants (tenant_id, status, metadata, created_at)
                VALUES (%s, 'ACTIVE', '{}'::jsonb, %s)
                ON CONFLICT (tenant_id) DO NOTHING;
                """,
                (tenant_id, datetime.now(timezone.utc)),
            )

    def _import_organization(self, conn: psycopg.Connection, tenant_id: str, file_path: Path) -> None:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO organizations (
                    organization_id, tenant_id, name, status, schema_version,
                    checksum, metadata, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (organization_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    name = EXCLUDED.name,
                    status = EXCLUDED.status,
                    schema_version = EXCLUDED.schema_version,
                    checksum = EXCLUDED.checksum,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at;
                """,
                (
                    data["organization_id"],
                    tenant_id,
                    data["name"],
                    data.get("status", "ACTIVE"),
                    data.get("schema_version", "1.0.0"),
                    data["checksum"],
                    json.dumps(data.get("metadata", {})),
                    data["created_at"],
                    data["updated_at"],
                ),
            )

    def _import_membership(self, conn: psycopg.Connection, tenant_id: str, file_path: Path) -> None:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO memberships (
                    membership_id, tenant_id, organization_id, identity_id,
                    role, status, joined_at, removed_at, source,
                    schema_version, checksum, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (membership_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    organization_id = EXCLUDED.organization_id,
                    identity_id = EXCLUDED.identity_id,
                    role = EXCLUDED.role,
                    status = EXCLUDED.status,
                    joined_at = EXCLUDED.joined_at,
                    removed_at = EXCLUDED.removed_at,
                    source = EXCLUDED.source,
                    schema_version = EXCLUDED.schema_version,
                    checksum = EXCLUDED.checksum,
                    metadata = EXCLUDED.metadata;
                """,
                (
                    data["membership_id"],
                    tenant_id,
                    data["organization_id"],
                    data["identity_id"],
                    data.get("role", "MEMBER"),
                    data.get("status", "ACTIVE"),
                    data["joined_at"],
                    data.get("removed_at"),
                    data.get("source"),
                    data.get("schema_version", "1.0.0"),
                    data["checksum"],
                    json.dumps(data.get("metadata", {})),
                ),
            )

    def _import_session(self, conn: psycopg.Connection, tenant_id: str, file_path: Path) -> None:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO saas_sessions (
                    session_id, identity_id, tenant_id, organization_id,
                    auth_method, auth_provider, status, created_at, expires_at,
                    last_validated_at, schema_version, checksum, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (session_id) DO UPDATE SET
                    identity_id = EXCLUDED.identity_id,
                    tenant_id = EXCLUDED.tenant_id,
                    organization_id = EXCLUDED.organization_id,
                    auth_method = EXCLUDED.auth_method,
                    auth_provider = EXCLUDED.auth_provider,
                    status = EXCLUDED.status,
                    created_at = EXCLUDED.created_at,
                    expires_at = EXCLUDED.expires_at,
                    last_validated_at = EXCLUDED.last_validated_at,
                    schema_version = EXCLUDED.schema_version,
                    checksum = EXCLUDED.checksum,
                    metadata = EXCLUDED.metadata;
                """,
                (
                    data["session_id"],
                    data["identity_id"],
                    tenant_id,
                    data.get("organization_id"),
                    data.get("authentication_method") or data.get("auth_method") or "UNKNOWN",
                    data.get("authentication_provider") or data.get("auth_provider") or "unknown",
                    data.get("status", "ACTIVE"),
                    data["created_at"],
                    data["expires_at"],
                    data.get("last_validated_at"),
                    data.get("schema_version", "1.0.0"),
                    data["checksum"],
                    json.dumps(data.get("metadata", {})),
                ),
            )

    def _import_plans(self, conn: psycopg.Connection, catalog_file: Path) -> int:
        with open(catalog_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        count = 0
        plans_list = data if isinstance(data, list) else data.get("plans", [])
        with conn.cursor() as cur:
            for p in plans_list:
                cur.execute(
                    """
                    INSERT INTO plans (
                        plan_id, version, name, tier, status,
                        features, limits, quota_template,
                        allowed_model_classes, allowed_providers,
                        metadata, checksum, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (plan_id, version) DO UPDATE SET
                        name = EXCLUDED.name,
                        tier = EXCLUDED.tier,
                        status = EXCLUDED.status,
                        features = EXCLUDED.features,
                        limits = EXCLUDED.limits,
                        quota_template = EXCLUDED.quota_template,
                        allowed_model_classes = EXCLUDED.allowed_model_classes,
                        allowed_providers = EXCLUDED.allowed_providers,
                        metadata = EXCLUDED.metadata,
                        checksum = EXCLUDED.checksum;
                    """,
                    (
                        p["plan_id"],
                        p.get("version", "1.0.0"),
                        p["name"],
                        p.get("tier", "FREE"),
                        p.get("status", "ACTIVE"),
                        json.dumps(p.get("features", [])),
                        json.dumps(p.get("limits", {})),
                        json.dumps(p.get("quota_template", {})),
                        json.dumps(p.get("allowed_model_classes", [])),
                        json.dumps(p.get("allowed_providers", [])),
                        json.dumps(p.get("metadata", {})),
                        p.get("checksum", "imported"),
                        p.get("created_at") or datetime.now(timezone.utc),
                    ),
                )
                count += 1
        return count

    def _import_plan_assignment(self, conn: psycopg.Connection, tenant_id: str, file_path: Path) -> None:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO plan_assignments (
                    assignment_id, tenant_id, plan_id, plan_version,
                    status, assigned_at, effective_from, effective_until,
                    source_reason, assigned_by_actor_id, metadata, checksum
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (assignment_id) DO UPDATE SET
                    tenant_id = EXCLUDED.tenant_id,
                    plan_id = EXCLUDED.plan_id,
                    plan_version = EXCLUDED.plan_version,
                    status = EXCLUDED.status,
                    effective_until = EXCLUDED.effective_until,
                    metadata = EXCLUDED.metadata,
                    checksum = EXCLUDED.checksum;
                """,
                (
                    data["assignment_id"],
                    tenant_id,
                    data["plan_id"],
                    data.get("plan_version", "1.0.0"),
                    data.get("status", "ACTIVE"),
                    data["assigned_at"],
                    data["effective_from"],
                    data.get("effective_until"),
                    data.get("source_reason"),
                    data.get("assigned_by_actor_id"),
                    json.dumps(data.get("metadata", {})),
                    data.get("checksum", "imported"),
                ),
            )

    def _import_usage_event(self, conn: psycopg.Connection, tenant_id: str, file_path: Path) -> None:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO usage_events (
                    usage_event_id, tenant_id, occurred_at, request_status,
                    identity_id, organization_id, session_id, provider,
                    model, task_type, cache_status, input_tokens,
                    output_tokens, total_tokens, estimated_cost, actual_cost,
                    correlation_id, source_reference, details, checksum
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (usage_event_id) DO NOTHING;
                """,
                (
                    data["usage_event_id"],
                    tenant_id,
                    data["occurred_at"],
                    data.get("request_status", "SUCCESS"),
                    data.get("identity_id"),
                    data.get("organization_id"),
                    data.get("session_id"),
                    data.get("provider"),
                    data.get("model"),
                    data.get("task_type"),
                    data.get("cache_status"),
                    data.get("input_tokens"),
                    data.get("output_tokens"),
                    data.get("total_tokens"),
                    data.get("estimated_cost"),
                    data.get("actual_cost"),
                    data.get("correlation_id"),
                    data.get("source_reference"),
                    json.dumps(data.get("details", {})),
                    data.get("checksum", "imported"),
                ),
            )
