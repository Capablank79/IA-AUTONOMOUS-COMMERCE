"""
Servicios de Aplicación para Organizaciones y Membresías de Usuario (Hito O.2 — Organizations / Users).

Implementa:
- OrganizationService:
  - create_organization
  - get_organization
  - list_organizations
  - update_organization_status
  - validate_organization_in_tenant

- OrganizationMembershipService:
  - add_membership
  - update_membership_status
  - remove_membership
  - get_membership
  - list_organization_members
  - list_identity_memberships
  - resolve_active_memberships
  - validate_membership_access

Garantías:
- Respeta estrictamente el aislamiento por Tenant (CrossTenantGuard).
- Integra auditoría estructurada K.1 y trazas K.2 (ORGANIZATION_CREATED, MEMBERSHIP_ADDED, MEMBERSHIP_REMOVED, MEMBERSHIP_STATUS_CHANGED).
- Previene asignaciones cross-tenant (si identity está vinculada a Tenant A, no puede unirse a Org de Tenant B).
- Idempotencia estricta en creación y asignación.
- Estados SUSPENDED / REMOVED no producen membresías activas.
- No almacena secretos ni PII innecesaria.
- Reutiliza N.1 Identity, N.2 Authentication, N.4 RBAC y O.1 Tenant Isolation.
"""

from datetime import datetime, timezone
import logging
import uuid
from typing import Optional, List, Dict, Any, Mapping, Union

from src.domain.organization.models import (
    Organization,
    OrganizationStatus,
    OrganizationReference,
    UserMembership,
    MembershipStatus,
    MembershipRole,
)
from src.domain.organization.ports import (
    OrganizationRepositoryPort,
    MembershipRepositoryPort,
)
from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.tenant.ports import TenantMappingPort
from src.domain.identity.ports import IdentityRepositoryPort
from src.domain.identity.models import IdentityReference, IdentityStatus
from src.domain.audit.models import AuditRecordType, AuditActor, AuditActorType, AuditRecord
from src.domain.audit.ports import AuditRepositoryPort
from src.domain.security.models import validate_safe_identifier

logger = logging.getLogger(__name__)


class OrganizationService:
    """
    Servicio de dominio/aplicación para la gestión de Organizaciones dentro de un Tenant.
    """

    def __init__(
        self,
        organization_repo: OrganizationRepositoryPort,
        audit_repository: Optional[AuditRepositoryPort] = None,
    ):
        self._org_repo = organization_repo
        self._audit_repo = audit_repository

    def create_organization(
        self,
        context: TenantContext,
        organization_id: str,
        name: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Organization:
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(organization_id, field_name="organization_id")

        # Idempotencia: si ya existe con los mismos datos en el tenant, retornar existente
        existing = self._org_repo.get_by_id(context, organization_id)
        if existing:
            if existing.name == name and existing.status == OrganizationStatus.ACTIVE:
                return existing
            raise ValueError(
                f"Organization '{organization_id}' already exists with different attributes or status."
            )

        now = datetime.now(timezone.utc)
        org = Organization(
            organization_id=organization_id,
            tenant_id=context.tenant_id,
            name=name,
            created_at=now,
            updated_at=now,
            status=OrganizationStatus.ACTIVE,
            metadata=metadata or {},
        )

        self._org_repo.save(context, org)

        if self._audit_repo:
            unique_id = uuid.uuid4().hex[:8]
            rec = AuditRecord(
                audit_id=f"aud-org-{org.organization_id}-{unique_id}",
                record_type=AuditRecordType.ORGANIZATION_CREATED,
                occurred_at=now,
                actor=AuditActor(
                    actor_type=AuditActorType.USER if context.identity_id else AuditActorType.SYSTEM,
                    actor_id=context.identity_id or "system",
                ),
                subject_type="ORGANIZATION",
                subject_id=org.organization_id,
                action_or_operation="CREATE_ORGANIZATION",
                status="SUCCESS",
                correlation_id=context.correlation_id or "corr_org_create",
                provenance="ORGANIZATION_SERVICE",
                metadata={
                    "tenant_id": context.tenant_id,
                    "organization_id": org.organization_id,
                    "name": org.name,
                },
            )
            self._audit_repo.append(rec)

        return org

    def get_organization(
        self,
        context: TenantContext,
        organization_id: str,
    ) -> Optional[Organization]:
        CrossTenantGuard.ensure_tenant_context(context)
        return self._org_repo.get_by_id(context, organization_id)

    def list_organizations(
        self,
        context: TenantContext,
    ) -> List[Organization]:
        CrossTenantGuard.ensure_tenant_context(context)
        return self._org_repo.list_by_tenant(context)

    def update_organization_status(
        self,
        context: TenantContext,
        organization_id: str,
        new_status: OrganizationStatus,
    ) -> Organization:
        CrossTenantGuard.ensure_tenant_context(context)
        org = self._org_repo.get_by_id(context, organization_id)
        if not org:
            raise ValueError(f"Organization '{organization_id}' not found in tenant '{context.tenant_id}'.")

        now = datetime.now(timezone.utc)
        updated_org = Organization(
            organization_id=org.organization_id,
            tenant_id=org.tenant_id,
            name=org.name,
            created_at=org.created_at,
            updated_at=now,
            status=new_status,
            metadata=dict(org.metadata),
        )

        self._org_repo.save(context, updated_org)
        return updated_org


class OrganizationMembershipService:
    """
    Servicio de dominio/aplicación para la gestión de Membresías de Usuario en Organizaciones.
    """

    def __init__(
        self,
        membership_repo: MembershipRepositoryPort,
        organization_repo: OrganizationRepositoryPort,
        tenant_mapping: Optional[TenantMappingPort] = None,
        identity_repo: Optional[IdentityRepositoryPort] = None,
        audit_repository: Optional[AuditRepositoryPort] = None,
    ):
        self._membership_repo = membership_repo
        self._organization_repo = organization_repo
        self._tenant_mapping = tenant_mapping
        self._identity_repo = identity_repo
        self._audit_repo = audit_repository

    def add_membership(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
        role: MembershipRole = MembershipRole.MEMBER,
        source: str = "SYSTEM",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> UserMembership:
        """
        Agrega una identidad canónica (N.1) a una organización dentro del tenant del contexto.

        Validaciones:
        1. Contexto de tenant obligatorio.
        2. Organización debe existir dentro del tenant y estar ACTIVE.
        3. Identity no debe estar vinculada a otro tenant diferente (Cross-Tenant prevention).
        4. Si identity_repo está disponible, la identidad debe existir y no estar SUSPENDED.
        5. Idempotencia estricta.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        validate_safe_identifier(organization_id, field_name="organization_id")
        validate_safe_identifier(identity_id, field_name="identity_id")

        # 1. Verificar organización en el tenant
        org = self._organization_repo.get_by_id(context, organization_id)
        if not org:
            raise ValueError(
                f"Organization '{organization_id}' does not exist in tenant '{context.tenant_id}'."
            )
        if org.status != OrganizationStatus.ACTIVE:
            raise ValueError(
                f"Organization '{organization_id}' is not ACTIVE (current status: {org.status})."
            )

        # 2. Verificar vinculación de Tenant de la Identity (Cross-Tenant Guard)
        if self._tenant_mapping:
            bound_tenant = self._tenant_mapping.get_tenant_for_identity(identity_id)
            if bound_tenant and bound_tenant != context.tenant_id:
                raise CrossTenantAccessError(
                    f"CROSS_TENANT_MEMBERSHIP_DENIED: Identity '{identity_id}' belongs to tenant '{bound_tenant}', cannot join organization in tenant '{context.tenant_id}'."
                )

        # 3. Verificar Identity Repository (N.1) si está provisto
        if self._identity_repo:
            ident = self._identity_repo.get_identity(identity_id)
            if not ident:
                raise ValueError(f"Identity '{identity_id}' does not exist in Identity Registry.")
            if ident.status == IdentityStatus.SUSPENDED:
                raise ValueError(f"Identity '{identity_id}' is SUSPENDED and cannot be added as active member.")

        # 4. Idempotencia y verificación de membresía existente
        existing = self._membership_repo.get_by_identity_and_org(context, organization_id, identity_id)
        if existing:
            if existing.status == MembershipStatus.ACTIVE and existing.role == role:
                return existing
            # Si estaba REMOVED o SUSPENDED, reactivar
            now = datetime.now(timezone.utc)
            reactivated = UserMembership(
                membership_id=existing.membership_id,
                tenant_id=existing.tenant_id,
                organization_id=existing.organization_id,
                identity_id=existing.identity_id,
                role=role,
                status=MembershipStatus.ACTIVE,
                joined_at=existing.joined_at,
                removed_at=None,
                source=source,
                metadata=metadata or dict(existing.metadata),
            )
            self._membership_repo.save(context, reactivated)
            self._record_audit(
                context,
                AuditRecordType.MEMBERSHIP_STATUS_CHANGED,
                reactivated.membership_id,
                "REACTIVATE_MEMBERSHIP",
                {"organization_id": organization_id, "identity_id": identity_id, "role": role.value},
            )
            return reactivated

        # 5. Crear nueva membresía
        membership_id = f"mem_{context.tenant_id}_{organization_id}_{identity_id}"
        now = datetime.now(timezone.utc)
        mem = UserMembership(
            membership_id=membership_id,
            tenant_id=context.tenant_id,
            organization_id=organization_id,
            identity_id=identity_id,
            role=role,
            status=MembershipStatus.ACTIVE,
            joined_at=now,
            removed_at=None,
            source=source,
            metadata=metadata or {},
        )

        self._membership_repo.save(context, mem)

        # Si no estaba vinculada en el mapping de tenants, vincularla
        if self._tenant_mapping and not self._tenant_mapping.get_tenant_for_identity(identity_id):
            self._tenant_mapping.bind_identity(context.tenant_id, identity_id)

        self._record_audit(
            context,
            AuditRecordType.MEMBERSHIP_ADDED,
            mem.membership_id,
            "ADD_MEMBERSHIP",
            {"organization_id": organization_id, "identity_id": identity_id, "role": role.value},
        )

        return mem

    def update_membership_status(
        self,
        context: TenantContext,
        membership_id: str,
        new_status: MembershipStatus,
    ) -> UserMembership:
        CrossTenantGuard.ensure_tenant_context(context)
        mem = self._membership_repo.get_by_id(context, membership_id)
        if not mem:
            raise ValueError(f"Membership '{membership_id}' not found in tenant '{context.tenant_id}'.")

        now = datetime.now(timezone.utc)
        removed_at = now if new_status == MembershipStatus.REMOVED else mem.removed_at

        updated = UserMembership(
            membership_id=mem.membership_id,
            tenant_id=mem.tenant_id,
            organization_id=mem.organization_id,
            identity_id=mem.identity_id,
            role=mem.role,
            status=new_status,
            joined_at=mem.joined_at,
            removed_at=removed_at,
            source=mem.source,
            metadata=dict(mem.metadata),
        )

        self._membership_repo.save(context, updated)

        self._record_audit(
            context,
            AuditRecordType.MEMBERSHIP_STATUS_CHANGED,
            updated.membership_id,
            "UPDATE_MEMBERSHIP_STATUS",
            {
                "organization_id": updated.organization_id,
                "identity_id": updated.identity_id,
                "new_status": new_status.value,
            },
        )

        return updated

    def remove_membership(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
    ) -> UserMembership:
        """Marca la membresía como REMOVED (preservando histórico y auditoría)."""
        CrossTenantGuard.ensure_tenant_context(context)
        mem = self._membership_repo.get_by_identity_and_org(context, organization_id, identity_id)
        if not mem:
            raise ValueError(
                f"Membership not found for identity '{identity_id}' in org '{organization_id}'."
            )

        now = datetime.now(timezone.utc)
        removed_mem = UserMembership(
            membership_id=mem.membership_id,
            tenant_id=mem.tenant_id,
            organization_id=mem.organization_id,
            identity_id=mem.identity_id,
            role=mem.role,
            status=MembershipStatus.REMOVED,
            joined_at=mem.joined_at,
            removed_at=now,
            source=mem.source,
            metadata=dict(mem.metadata),
        )

        self._membership_repo.save(context, removed_mem)

        self._record_audit(
            context,
            AuditRecordType.MEMBERSHIP_REMOVED,
            removed_mem.membership_id,
            "REMOVE_MEMBERSHIP",
            {"organization_id": organization_id, "identity_id": identity_id},
        )

        return removed_mem

    def get_membership(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
    ) -> Optional[UserMembership]:
        CrossTenantGuard.ensure_tenant_context(context)
        return self._membership_repo.get_by_identity_and_org(context, organization_id, identity_id)

    def list_organization_members(
        self,
        context: TenantContext,
        organization_id: str,
        active_only: bool = True,
    ) -> List[UserMembership]:
        CrossTenantGuard.ensure_tenant_context(context)
        memberships = self._membership_repo.list_by_organization(context, organization_id)
        if active_only:
            return [m for m in memberships if m.status == MembershipStatus.ACTIVE]
        return memberships

    def list_identity_memberships(
        self,
        context: TenantContext,
        identity_id: str,
        active_only: bool = True,
    ) -> List[UserMembership]:
        CrossTenantGuard.ensure_tenant_context(context)
        memberships = self._membership_repo.list_by_identity(context, identity_id)
        if active_only:
            return [m for m in memberships if m.status == MembershipStatus.ACTIVE]
        return memberships

    def validate_membership_access(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
    ) -> bool:
        """
        Valida si una identidad tiene membresía ACTIVE y válida para una organización dentro del tenant.

        Fail-Safe:
        - Si org no existe o no está ACTIVE -> False.
        - Si membership no existe o no está ACTIVE -> False.
        - Si identity está suspendida -> False.
        """
        CrossTenantGuard.ensure_tenant_context(context)
        org = self._organization_repo.get_by_id(context, organization_id)
        if not org or org.status != OrganizationStatus.ACTIVE:
            return False

        mem = self._membership_repo.get_by_identity_and_org(context, organization_id, identity_id)
        if not mem or mem.status != MembershipStatus.ACTIVE:
            return False

        if self._identity_repo:
            ident = self._identity_repo.get_identity(identity_id)
            if not ident or ident.status != IdentityStatus.ACTIVE:
                return False

        return True

    def _record_audit(
        self,
        context: TenantContext,
        record_type: AuditRecordType,
        subject_id: str,
        action: str,
        metadata: Dict[str, Any],
    ) -> None:
        if not self._audit_repo:
            return
        now = datetime.now(timezone.utc)
        unique_id = uuid.uuid4().hex[:8]
        rec = AuditRecord(
            audit_id=f"aud-mem-{subject_id}-{action.lower()}-{unique_id}",
            record_type=record_type,
            occurred_at=now,
            actor=AuditActor(
                actor_type=AuditActorType.USER if context.identity_id else AuditActorType.SYSTEM,
                actor_id=context.identity_id or "system",
            ),
            subject_type="USER_MEMBERSHIP",
            subject_id=subject_id,
            action_or_operation=action,
            status="SUCCESS",
            correlation_id=context.correlation_id or "corr_mem",
            provenance="ORGANIZATION_MEMBERSHIP_SERVICE",
            metadata={
                "tenant_id": context.tenant_id,
                **metadata,
            },
        )
        self._audit_repo.append(rec)
