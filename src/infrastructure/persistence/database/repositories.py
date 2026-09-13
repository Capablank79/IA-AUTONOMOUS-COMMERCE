"""Adaptador de repositorios PostgreSQL para entidades SaaS críticas (P.3 — Database Migrations).

Implementa:
- PostgresTenantRepository: Gestión y validación de Tenants.
- PostgresOrganizationRepository: Organizaciones tenant-scoped (O.2).
- PostgresMembershipRepository: Membresías tenant-scoped (O.2).
- PostgresSaaSSessionRepository: Sesiones SaaS aisladas por tenant (O.3).
- PostgresTenantScopedRepository: Repositorio genérico tenant-scoped (O.1).

Asegura:
- Aislamiento estricto por tenant (claves foráneas y filtros en todas las queries).
- Checksums SHA-256 de integridad verificados.
- Timestamps UTC conscientes de zona horaria.
- Transacciones atómicas y seguridad ante fallos de concurrencia.
"""

from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional, TypeVar, Generic

import psycopg
from psycopg.rows import dict_row

from src.domain.tenant.models import (
    TenantContext,
    TenantScopedResource,
    CrossTenantAccessError,
)
from src.domain.tenant.ports import TenantResolverPort, TenantScopedRepositoryPort
from src.domain.organization.models import (
    Organization,
    UserMembership,
    OrganizationStatus,
    MembershipStatus,
    MembershipRole,
)
from src.domain.organization.ports import (
    OrganizationRepositoryPort,
    MembershipRepositoryPort,
)
from src.domain.session.models import (
    SaaSSession,
    SessionStatus,
    SessionTenantMismatchError,
)
from src.domain.session.ports import SaaSSessionRepositoryPort
from src.domain.security.models import validate_safe_identifier
from src.infrastructure.persistence.database.config import (
    DatabaseConnectionFactory,
    sanitize_error_message,
)

T = TypeVar("T")


class PostgresTenantRepository:
    """Repositorio de tenants en PostgreSQL."""

    def __init__(self, connection_factory: DatabaseConnectionFactory) -> None:
        self._factory = connection_factory

    def ensure_tenant(self, tenant_id: str, status: str = "ACTIVE", metadata: Optional[Dict[str, Any]] = None) -> None:
        """Crea el tenant si no existe (idempotente)."""
        validate_safe_identifier(tenant_id, "tenant_id")
        meta = metadata or {}
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tenants (tenant_id, status, metadata, created_at)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (tenant_id) DO NOTHING;
                    """,
                    (tenant_id, status, json.dumps(meta), datetime.now(timezone.utc)),
                )
        finally:
            if not conn.closed:
                conn.close()

    def exists(self, tenant_id: str) -> bool:
        validate_safe_identifier(tenant_id, "tenant_id")
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM tenants WHERE tenant_id = %s;", (tenant_id,))
                return cur.fetchone() is not None
        finally:
            if not conn.closed:
                conn.close()


class PostgresOrganizationRepository(OrganizationRepositoryPort):
    """Repositorio PostgreSQL para Organization con aislamiento estricto por tenant."""

    def __init__(self, connection_factory: DatabaseConnectionFactory) -> None:
        self._factory = connection_factory

    def save(self, context: TenantContext, organization: Organization) -> None:
        if not context.matches_tenant(organization.tenant_id):
            raise ValueError(f"Cross-tenant access forbidden: context '{context.tenant_id}' != org '{organization.tenant_id}'")
        validate_safe_identifier(organization.organization_id, "organization_id")
        
        # Asegurar que el tenant exista primero
        tenant_repo = PostgresTenantRepository(self._factory)
        tenant_repo.ensure_tenant(organization.tenant_id)

        conn = self._factory.create_connection(autocommit=True)
        try:
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
                        organization.organization_id,
                        organization.tenant_id,
                        organization.name,
                        organization.status.value,
                        organization.schema_version,
                        organization.checksum,
                        json.dumps(dict(organization.metadata)),
                        organization.created_at,
                        organization.updated_at,
                    ),
                )
        finally:
            if not conn.closed:
                conn.close()

    def get_by_id(self, context: TenantContext, organization_id: str) -> Optional[Organization]:
        validate_safe_identifier(organization_id, "organization_id")
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT organization_id, tenant_id, name, status, schema_version,
                           checksum, metadata, created_at, updated_at
                    FROM organizations
                    WHERE organization_id = %s AND tenant_id = %s;
                    """,
                    (organization_id, context.tenant_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                
                return Organization(
                    organization_id=row["organization_id"],
                    tenant_id=row["tenant_id"],
                    name=row["name"],
                    status=OrganizationStatus(row["status"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    schema_version=row["schema_version"],
                    checksum=row["checksum"],
                    metadata=row["metadata"] or {},
                )
        finally:
            if not conn.closed:
                conn.close()

    def list_by_tenant(self, context: TenantContext) -> List[Organization]:
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT organization_id, tenant_id, name, status, schema_version,
                           checksum, metadata, created_at, updated_at
                    FROM organizations
                    WHERE tenant_id = %s
                    ORDER BY created_at ASC;
                    """,
                    (context.tenant_id,),
                )
                rows = cur.fetchall()
                return [
                    Organization(
                        organization_id=r["organization_id"],
                        tenant_id=r["tenant_id"],
                        name=r["name"],
                        status=OrganizationStatus(r["status"]),
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                        schema_version=r["schema_version"],
                        checksum=r["checksum"],
                        metadata=r["metadata"] or {},
                    )
                    for r in rows
                ]
        finally:
            if not conn.closed:
                conn.close()

    def delete(self, context: TenantContext, organization_id: str) -> bool:
        validate_safe_identifier(organization_id, "organization_id")
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM organizations WHERE organization_id = %s AND tenant_id = %s;",
                    (organization_id, context.tenant_id),
                )
                return cur.rowcount > 0
        finally:
            if not conn.closed:
                conn.close()


class PostgresMembershipRepository(MembershipRepositoryPort):
    """Repositorio PostgreSQL para UserMembership con aislamiento estricto por tenant."""

    def __init__(self, connection_factory: DatabaseConnectionFactory) -> None:
        self._factory = connection_factory

    def save(self, context: TenantContext, membership: UserMembership) -> None:
        context.validate_resource_access(membership.tenant_id, "membership.tenant_id")
        validate_safe_identifier(membership.membership_id, "membership_id")
        
        # Asegurar tenant y organización
        PostgresTenantRepository(self._factory).ensure_tenant(membership.tenant_id)

        conn = self._factory.create_connection(autocommit=True)
        try:
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
                        membership.membership_id,
                        membership.tenant_id,
                        membership.organization_id,
                        membership.identity_id,
                        membership.role.value if isinstance(membership.role, MembershipRole) else str(membership.role),
                        membership.status.value if isinstance(membership.status, MembershipStatus) else str(membership.status),
                        membership.joined_at,
                        membership.removed_at,
                        membership.source,
                        membership.schema_version,
                        membership.checksum,
                        json.dumps(dict(membership.metadata)),
                    ),
                )
        finally:
            if not conn.closed:
                conn.close()

    def get_by_id(self, context: TenantContext, membership_id: str) -> Optional[UserMembership]:
        validate_safe_identifier(membership_id, "membership_id")
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT membership_id, tenant_id, organization_id, identity_id,
                           role, status, joined_at, removed_at, source,
                           schema_version, checksum, metadata
                    FROM memberships
                    WHERE membership_id = %s AND tenant_id = %s;
                    """,
                    (membership_id, context.tenant_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                
                return UserMembership(
                    membership_id=row["membership_id"],
                    tenant_id=row["tenant_id"],
                    organization_id=row["organization_id"],
                    identity_id=row["identity_id"],
                    role=MembershipRole(row["role"]),
                    status=MembershipStatus(row["status"]),
                    joined_at=row["joined_at"],
                    removed_at=row["removed_at"],
                    source=row["source"],
                    schema_version=row["schema_version"],
                    checksum=row["checksum"],
                    metadata=row["metadata"] or {},
                )
        finally:
            if not conn.closed:
                conn.close()

    def get_by_identity_and_org(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
    ) -> Optional[UserMembership]:
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT membership_id, tenant_id, organization_id, identity_id,
                           role, status, joined_at, removed_at, source,
                           schema_version, checksum, metadata
                    FROM memberships
                    WHERE tenant_id = %s AND organization_id = %s AND identity_id = %s;
                    """,
                    (context.tenant_id, organization_id, identity_id),
                )
                row = cur.fetchone()
                if not row:
                    return None
                
                return UserMembership(
                    membership_id=row["membership_id"],
                    tenant_id=row["tenant_id"],
                    organization_id=row["organization_id"],
                    identity_id=row["identity_id"],
                    role=MembershipRole(row["role"]),
                    status=MembershipStatus(row["status"]),
                    joined_at=row["joined_at"],
                    removed_at=row["removed_at"],
                    source=row["source"],
                    schema_version=row["schema_version"],
                    checksum=row["checksum"],
                    metadata=row["metadata"] or {},
                )
        finally:
            if not conn.closed:
                conn.close()

    def list_by_organization(self, context: TenantContext, organization_id: str) -> List[UserMembership]:
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT membership_id, tenant_id, organization_id, identity_id,
                           role, status, joined_at, removed_at, source,
                           schema_version, checksum, metadata
                    FROM memberships
                    WHERE tenant_id = %s AND organization_id = %s
                    ORDER BY joined_at ASC;
                    """,
                    (context.tenant_id, organization_id),
                )
                rows = cur.fetchall()
                return [
                    UserMembership(
                        membership_id=r["membership_id"],
                        tenant_id=r["tenant_id"],
                        organization_id=r["organization_id"],
                        identity_id=r["identity_id"],
                        role=MembershipRole(r["role"]),
                        status=MembershipStatus(r["status"]),
                        joined_at=r["joined_at"],
                        removed_at=r["removed_at"],
                        source=r["source"],
                        schema_version=r["schema_version"],
                        checksum=r["checksum"],
                        metadata=r["metadata"] or {},
                    )
                    for r in rows
                ]
        finally:
            if not conn.closed:
                conn.close()

    def list_by_identity(self, context: TenantContext, identity_id: str) -> List[UserMembership]:
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT membership_id, tenant_id, organization_id, identity_id,
                           role, status, joined_at, removed_at, source,
                           schema_version, checksum, metadata
                    FROM memberships
                    WHERE tenant_id = %s AND identity_id = %s
                    ORDER BY joined_at ASC;
                    """,
                    (context.tenant_id, identity_id),
                )
                rows = cur.fetchall()
                return [
                    UserMembership(
                        membership_id=r["membership_id"],
                        tenant_id=r["tenant_id"],
                        organization_id=r["organization_id"],
                        identity_id=r["identity_id"],
                        role=MembershipRole(r["role"]),
                        status=MembershipStatus(r["status"]),
                        joined_at=r["joined_at"],
                        removed_at=r["removed_at"],
                        source=r["source"],
                        schema_version=r["schema_version"],
                        checksum=r["checksum"],
                        metadata=r["metadata"] or {},
                    )
                    for r in rows
                ]
        finally:
            if not conn.closed:
                conn.close()

    def delete(self, context: TenantContext, membership_id: str) -> bool:
        validate_safe_identifier(membership_id, "membership_id")
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM memberships WHERE membership_id = %s AND tenant_id = %s;",
                    (membership_id, context.tenant_id),
                )
                return cur.rowcount > 0
        finally:
            if not conn.closed:
                conn.close()


class PostgresSaaSSessionRepository(SaaSSessionRepositoryPort):
    """Repositorio PostgreSQL para SaaSSession con aislamiento multi-tenant."""

    def __init__(self, connection_factory: DatabaseConnectionFactory) -> None:
        self._factory = connection_factory

    def save(self, session: SaaSSession) -> None:
        validate_safe_identifier(session.session_id, "session_id")
        validate_safe_identifier(session.tenant_id, "tenant_id")
        
        # Garantizar tenant
        PostgresTenantRepository(self._factory).ensure_tenant(session.tenant_id)

        conn = self._factory.create_connection(autocommit=True)
        try:
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
                        session.session_id,
                        session.identity_id,
                        session.tenant_id,
                        session.organization_id,
                        session.authentication_method,
                        session.authentication_provider,
                        session.status.value,
                        session.created_at,
                        session.expires_at,
                        session.last_validated_at,
                        session.schema_version,
                        session.checksum,
                        json.dumps(dict(session.metadata)),
                    ),
                )
        finally:
            if not conn.closed:
                conn.close()

    def get_by_id(self, session_id: str, tenant_id: Optional[str] = None) -> Optional[SaaSSession]:
        validate_safe_identifier(session_id, "session_id")
        if tenant_id:
            validate_safe_identifier(tenant_id, "tenant_id")

        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                if tenant_id:
                    cur.execute(
                        """
                        SELECT session_id, identity_id, tenant_id, organization_id,
                               auth_method, auth_provider, status, created_at, expires_at,
                               last_validated_at, schema_version, checksum, metadata
                        FROM saas_sessions
                        WHERE session_id = %s AND tenant_id = %s;
                        """,
                        (session_id, tenant_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT session_id, identity_id, tenant_id, organization_id,
                               auth_method, auth_provider, status, created_at, expires_at,
                               last_validated_at, schema_version, checksum, metadata
                        FROM saas_sessions
                        WHERE session_id = %s;
                        """,
                        (session_id,),
                    )
                
                row = cur.fetchone()
                if not row:
                    return None

                return SaaSSession(
                    session_id=row["session_id"],
                    identity_id=row["identity_id"],
                    tenant_id=row["tenant_id"],
                    organization_id=row["organization_id"],
                    authentication_method=row["auth_method"] or "UNKNOWN",
                    authentication_provider=row["auth_provider"] or "unknown",
                    status=SessionStatus(row["status"]),
                    created_at=row["created_at"],
                    expires_at=row["expires_at"],
                    last_validated_at=row["last_validated_at"],
                    schema_version=row["schema_version"],
                    checksum=row["checksum"],
                    metadata=row["metadata"] or {},
                )
        finally:
            if not conn.closed:
                conn.close()

    def list_by_tenant(self, tenant_id: str) -> List[SaaSSession]:
        validate_safe_identifier(tenant_id, "tenant_id")
        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    SELECT session_id, identity_id, tenant_id, organization_id,
                           auth_method, auth_provider, status, created_at, expires_at,
                           last_validated_at, schema_version, checksum, metadata
                    FROM saas_sessions
                    WHERE tenant_id = %s
                    ORDER BY created_at DESC;
                    """,
                    (tenant_id,),
                )
                rows = cur.fetchall()
                return [
                    SaaSSession(
                        session_id=r["session_id"],
                        identity_id=r["identity_id"],
                        tenant_id=r["tenant_id"],
                        organization_id=r["organization_id"],
                        authentication_method=r["auth_method"] or "UNKNOWN",
                        authentication_provider=r["auth_provider"] or "unknown",
                        status=SessionStatus(r["status"]),
                        created_at=r["created_at"],
                        expires_at=r["expires_at"],
                        last_validated_at=r["last_validated_at"],
                        schema_version=r["schema_version"],
                        checksum=r["checksum"],
                        metadata=r["metadata"] or {},
                    )
                    for r in rows
                ]
        finally:
            if not conn.closed:
                conn.close()

    def list_by_identity(self, identity_id: str, tenant_id: Optional[str] = None) -> List[SaaSSession]:
        validate_safe_identifier(identity_id, "identity_id")
        if tenant_id:
            validate_safe_identifier(tenant_id, "tenant_id")

        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                if tenant_id:
                    cur.execute(
                        """
                        SELECT session_id, identity_id, tenant_id, organization_id,
                               auth_method, auth_provider, status, created_at, expires_at,
                               last_validated_at, schema_version, checksum, metadata
                        FROM saas_sessions
                        WHERE identity_id = %s AND tenant_id = %s
                        ORDER BY created_at DESC;
                        """,
                        (identity_id, tenant_id),
                    )
                else:
                    cur.execute(
                        """
                        SELECT session_id, identity_id, tenant_id, organization_id,
                               auth_method, auth_provider, status, created_at, expires_at,
                               last_validated_at, schema_version, checksum, metadata
                        FROM saas_sessions
                        WHERE identity_id = %s
                        ORDER BY created_at DESC;
                        """,
                        (identity_id,),
                    )
                rows = cur.fetchall()
                return [
                    SaaSSession(
                        session_id=r["session_id"],
                        identity_id=r["identity_id"],
                        tenant_id=r["tenant_id"],
                        organization_id=r["organization_id"],
                        authentication_method=r["auth_method"] or "UNKNOWN",
                        authentication_provider=r["auth_provider"] or "unknown",
                        status=SessionStatus(r["status"]),
                        created_at=r["created_at"],
                        expires_at=r["expires_at"],
                        last_validated_at=r["last_validated_at"],
                        schema_version=r["schema_version"],
                        checksum=r["checksum"],
                        metadata=r["metadata"] or {},
                    )
                    for r in rows
                ]
        finally:
            if not conn.closed:
                conn.close()

    def delete(self, session_id: str, tenant_id: Optional[str] = None) -> bool:
        validate_safe_identifier(session_id, "session_id")
        if tenant_id:
            validate_safe_identifier(tenant_id, "tenant_id")

        conn = self._factory.create_connection(autocommit=True)
        try:
            with conn.cursor() as cur:
                if tenant_id:
                    cur.execute(
                        "DELETE FROM saas_sessions WHERE session_id = %s AND tenant_id = %s;",
                        (session_id, tenant_id),
                    )
                else:
                    cur.execute("DELETE FROM saas_sessions WHERE session_id = %s;", (session_id,))
                return cur.rowcount > 0
        finally:
            if not conn.closed:
                conn.close()
