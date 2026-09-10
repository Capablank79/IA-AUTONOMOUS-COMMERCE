"""
Tests de Integración y E2E para O.2 — Organizations / Users (Hito O — SaaS / Platformization).

Escenarios cubiertos:
A. Tenant A creates organization -> retrieve only from A.
B. Tenant B cannot read A organization.
C. Identity A joins Organization A -> membership active.
D. Identity A cannot join Organization B cross-tenant.
E. Same identity can have separate memberships in multiple orgs within same tenant if policy permits.
F. Removed membership -> RBAC/org access denied.
G. Restart -> orgs/memberships persist on disk crash-safe.
H. Tampered membership -> fail-safe no access.
I. Audit/Trace safe integration (ORGANIZATION_CREATED, MEMBERSHIP_ADDED, MEMBERSHIP_REMOVED).
J. Pipeline N.1 -> N.2 -> O.1 -> O.2 -> N.4 -> N.3.
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import shutil

from src.domain.oauth.models import OAuthConnection
from src.domain.authorization.models import ResourceReference

from src.domain.organization.models import (
    Organization,
    OrganizationStatus,
    UserMembership,
    MembershipStatus,
    MembershipRole,
)
from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.application.tenant.tenant_context_service import TenantContextService
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonOrganizationRepository,
    JsonMembershipRepository,
)
from src.application.organization.organization_service import (
    OrganizationService,
    OrganizationMembershipService,
)

# Integraciones de Identidad N.1, Autenticación N.2, Autorización N.3, RBAC N.4, Auditoría K.1
from src.domain.identity.models import (
    Identity,
    IdentityType,
    IdentityStatus,
    create_oauth_user_identity,
)
from src.application.identity.identity_service import IdentityService
from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository

from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationRequest,
    AuthenticationStatus,
)
from src.application.authentication.authentication_service import AuthenticationService

from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
    PermissionStatus,
    RoleStatus,
)
from src.application.rbac.rbac_service import RBACService
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)

from src.domain.authorization.models import (
    AuthorizationRequest,
    AuthorizationStatus,
)
from src.application.authorization.authorization_service import AuthorizationService

from src.domain.audit.models import AuditRecordType
from src.application.audit.audit_trail_service import AuditTrailService
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="o2_integ_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def audit_repo(temp_dir):
    return JsonAuditRepository(temp_dir / "audit")


@pytest.fixture
def audit_service(audit_repo):
    return AuditTrailService(audit_repo)


@pytest.fixture
def tenant_context_service():
    return TenantContextService()


def test_scenario_a_tenant_a_creates_organization(temp_dir, audit_repo):
    """A. Tenant A creates organization -> retrieve only from A."""
    org_repo = JsonOrganizationRepository(temp_dir)
    org_service = OrganizationService(org_repo, audit_repository=audit_repo)

    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_admin_a", correlation_id="corr_a")
    org = org_service.create_organization(ctx_a, "org_ops_a", "Ops A")

    assert org.organization_id == "org_ops_a"
    assert org.tenant_id == "tenant_a"

    retrieved = org_service.get_organization(ctx_a, "org_ops_a")
    assert retrieved is not None
    assert retrieved.name == "Ops A"


def test_scenario_b_tenant_b_cannot_read_a_organization(temp_dir):
    """B. Tenant B cannot read A organization."""
    org_repo = JsonOrganizationRepository(temp_dir)
    org_service = OrganizationService(org_repo)

    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_admin_a")
    ctx_b = TenantContext(tenant_id="tenant_b", identity_id="usr_admin_b")

    org_service.create_organization(ctx_a, "org_secret_a", "Top Secret A")

    # Tenant B busca por ID -> retorna None (no encontrado en Tenant B)
    assert org_service.get_organization(ctx_b, "org_secret_a") is None

    # Tenant B lista organizaciones -> no ve las de Tenant A
    list_b = org_service.list_organizations(ctx_b)
    assert len(list_b) == 0


def test_scenario_c_identity_joins_organization(temp_dir, tenant_context_service, audit_repo):
    """C. Identity A joins Organization A -> membership active."""
    ident_repo = JsonIdentityRepository(temp_dir / "identity")
    ident_service = IdentityService(ident_repo)

    now = datetime.now(timezone.utc)
    ident = ident_service.register_identity(
        identity_id="usr_alice_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:alice_01",
        display_name="Alice Member",
    )

    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(
        mem_repo,
        org_repo,
        tenant_mapping=tenant_context_service,
        identity_repo=ident_repo,
        audit_repository=audit_repo,
    )

    ctx_a = TenantContext(tenant_id="tenant_a", identity_id=ident.identity_id)
    org_service.create_organization(ctx_a, "org_core", "Core Org")

    mem = mem_service.add_membership(
        context=ctx_a,
        organization_id="org_core",
        identity_id=ident.identity_id,
        role=MembershipRole.MEMBER,
    )

    assert mem.status == MembershipStatus.ACTIVE
    assert mem.role == MembershipRole.MEMBER
    assert mem_service.validate_membership_access(ctx_a, "org_core", ident.identity_id) is True


def test_scenario_d_identity_cannot_join_cross_tenant_org(temp_dir, tenant_context_service):
    """D. Identity A cannot join Organization B cross-tenant."""
    tenant_context_service.bind_identity("tenant_a", "usr_alice_a")

    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(
        mem_repo,
        org_repo,
        tenant_mapping=tenant_context_service,
    )

    ctx_b = TenantContext(tenant_id="tenant_b", identity_id="usr_admin_b")
    org_service.create_organization(ctx_b, "org_beta_hq", "Beta HQ")

    with pytest.raises(CrossTenantAccessError, match="CROSS_TENANT_MEMBERSHIP_DENIED"):
        mem_service.add_membership(
            context=ctx_b,
            organization_id="org_beta_hq",
            identity_id="usr_alice_a",
        )


def test_scenario_e_separate_memberships_same_tenant(temp_dir, tenant_context_service):
    """E. Same identity can have separate memberships in multiple orgs within same tenant."""
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(
        mem_repo,
        org_repo,
        tenant_mapping=tenant_context_service,
    )

    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_multirole")
    org_service.create_organization(ctx_a, "org_marketing", "Marketing Department")
    org_service.create_organization(ctx_a, "org_sales", "Sales Department")

    mem1 = mem_service.add_membership(ctx_a, "org_marketing", "usr_multirole", role=MembershipRole.MEMBER)
    mem2 = mem_service.add_membership(ctx_a, "org_sales", "usr_multirole", role=MembershipRole.OWNER)

    assert mem1.organization_id == "org_marketing"
    assert mem1.role == MembershipRole.MEMBER
    assert mem2.organization_id == "org_sales"
    assert mem2.role == MembershipRole.OWNER

    user_memberships = mem_service.list_identity_memberships(ctx_a, "usr_multirole")
    assert len(user_memberships) == 2


def test_scenario_f_removed_membership_denies_org_access(temp_dir):
    """F. Removed membership -> org access denied."""
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(mem_repo, org_repo)

    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_temp")
    org_service.create_organization(ctx_a, "org_temp_projects", "Temp Projects")
    mem_service.add_membership(ctx_a, "org_temp_projects", "usr_temp")

    assert mem_service.validate_membership_access(ctx_a, "org_temp_projects", "usr_temp") is True

    # Remover membresía
    mem_service.remove_membership(ctx_a, "org_temp_projects", "usr_temp")

    # Acceso denegado
    assert mem_service.validate_membership_access(ctx_a, "org_temp_projects", "usr_temp") is False


def test_scenario_g_restart_orgs_and_memberships_persist(temp_dir):
    """G. Restart -> orgs/memberships persist crash-safe on disk."""
    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_persisted")

    # Ciclo 1: Guardar
    org_repo_1 = JsonOrganizationRepository(temp_dir)
    mem_repo_1 = JsonMembershipRepository(temp_dir)
    org_service_1 = OrganizationService(org_repo_1)
    mem_service_1 = OrganizationMembershipService(mem_repo_1, org_repo_1)

    org_service_1.create_organization(ctx_a, "org_durable", "Durable Org")
    mem_service_1.add_membership(ctx_a, "org_durable", "usr_persisted", role=MembershipRole.ADMIN)

    # Ciclo 2: Reiniciar (nuevas instancias apuntando al mismo disco)
    org_repo_2 = JsonOrganizationRepository(temp_dir)
    mem_repo_2 = JsonMembershipRepository(temp_dir)
    org_service_2 = OrganizationService(org_repo_2)
    mem_service_2 = OrganizationMembershipService(mem_repo_2, org_repo_2)

    org = org_service_2.get_organization(ctx_a, "org_durable")
    assert org is not None
    assert org.name == "Durable Org"
    assert org.is_active is True

    mem = mem_service_2.get_membership(ctx_a, "org_durable", "usr_persisted")
    assert mem is not None
    assert mem.role == MembershipRole.ADMIN
    assert mem.is_active is True


def test_scenario_h_tampered_membership_fail_safe(temp_dir):
    """H. Tampered membership -> fail-safe no access."""
    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_tampered")

    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(mem_repo, org_repo)

    org_service.create_organization(ctx_a, "org_secure", "Secure Org")
    mem = mem_service.add_membership(ctx_a, "org_secure", "usr_tampered")

    mem_file = temp_dir / "tenants" / "tenant_a" / "memberships" / f"{mem.membership_id}.json"
    assert mem_file.exists()

    # Corromper deliberadamente el archivo JSON
    with open(mem_file, "w", encoding="utf-8") as f:
        f.write("{ CORRUPTED_DATA_INJECTED }")

    # Recuperación posterior retorna None
    retrieved = mem_service.get_membership(ctx_a, "org_secure", "usr_tampered")
    assert retrieved is None

    # Acceso fail-safe bloqueado
    assert mem_service.validate_membership_access(ctx_a, "org_secure", "usr_tampered") is False


def test_scenario_i_audit_safe(temp_dir, audit_repo):
    """I. Audit/Trace safe."""
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo, audit_repository=audit_repo)
    mem_service = OrganizationMembershipService(mem_repo, org_repo, audit_repository=audit_repo)

    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_auditor", correlation_id="corr_audit_100")

    org_service.create_organization(ctx_a, "org_audited", "Audited Org")
    mem = mem_service.add_membership(ctx_a, "org_audited", "usr_auditor", role=MembershipRole.MEMBER)
    mem_service.remove_membership(ctx_a, "org_audited", "usr_auditor")

    records = audit_repo.list_records(correlation_id="corr_audit_100")
    record_types = [r.record_type for r in records]

    assert AuditRecordType.ORGANIZATION_CREATED in record_types
    assert AuditRecordType.MEMBERSHIP_ADDED in record_types
    assert AuditRecordType.MEMBERSHIP_REMOVED in record_types


def test_scenario_j_pipeline_n1_n2_o1_o2_n4_n3(temp_dir, tenant_context_service):
    """
    J. Pipeline completo N.1 (Identity) -> N.2 (Authentication) -> O.1 (Tenant Context) -> O.2 (Organization & Membership) -> N.4 (RBAC) -> N.3 (Authorization).
    """
    # 1. N.1 Identity - Registrado vía OAuth user de forma determinista
    ident_repo = JsonIdentityRepository(temp_dir / "identity")
    ident_service = IdentityService(ident_repo)
    user_ident = ident_service.register_oauth_user(
        provider="mercadolibre",
        user_id="ml_9988",
        display_name="ML Operator",
    )

    # 2. N.2 Authentication
    auth_service = AuthenticationService(
        identity_service=ident_service,
        trusted_internal_tokens={"secret_trusted_tok": "agent_auto"},
    )
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="ml_9988",
        access_token="valid_ml_token_12345",
        refresh_token="valid_ml_refresh_67890",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=2),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn, correlation_id="corr_e2e_pipeline")
    assert auth_res.status == AuthenticationStatus.AUTHENTICATED
    assert auth_res.principal is not None
    assert auth_res.principal.identity_id == "usr_mercadolibre_ml_9988"

    # 3. O.1 Tenant Isolation Context
    tenant_context_service.bind_identity("tenant_latam_corp", auth_res.principal.identity_id)
    tenant_ctx = tenant_context_service.resolve_context(
        tenant_id="tenant_latam_corp",
        identity_id=auth_res.principal.identity_id,
        correlation_id="corr_e2e_pipeline",
    )
    assert tenant_ctx.tenant_id == "tenant_latam_corp"

    # 4. O.2 Organization & Membership
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(
        mem_repo,
        org_repo,
        tenant_mapping=tenant_context_service,
        identity_repo=ident_repo,
    )

    org = org_service.create_organization(tenant_ctx, "org_ecom_chile", "Chile E-Commerce Hub")
    mem = mem_service.add_membership(
        context=tenant_ctx,
        organization_id="org_ecom_chile",
        identity_id=auth_res.principal.identity_id,
        role=MembershipRole.MEMBER,
    )
    assert mem_service.validate_membership_access(tenant_ctx, "org_ecom_chile", auth_res.principal.identity_id) is True

    # 5. N.4 RBAC Scoped to Tenant/Org
    role_repo = JsonRoleRepository(temp_dir / "rbac")
    assign_repo = JsonRoleAssignmentRepository(temp_dir / "rbac")
    rbac_service = RBACService(role_repo, assign_repo)

    perm_pub = rbac_service.create_permission(permission_id="perm_pub", action="LISTING_PUBLISH")
    perm_read = rbac_service.create_permission(permission_id="perm_read", action="LISTING_READ")
    role_operator = rbac_service.define_role(
        role_id="role_operator_org",
        name="Operator for Org",
        permissions=[perm_pub, perm_read],
    )

    now = datetime.now(timezone.utc)
    assignment = rbac_service.assign_role(
        identity_id=auth_res.principal.identity_id,
        role_id=role_operator.role_id,
        scope=tenant_ctx.scope.canonical_scope,
    )

    rbac_eval = rbac_service.resolve_effective_permissions(
        principal_or_identity=auth_res.principal.identity_id,
        scope=tenant_ctx.scope.canonical_scope,
    )
    assert "LISTING_PUBLISH" in rbac_eval.actions
    assert "PRICE_UPDATE" not in rbac_eval.actions

    # 6. N.3 Authorization
    principal_ctx = auth_service.create_principal_context(auth_res)
    authz_service = AuthorizationService()
    authz_req = AuthorizationRequest(
        principal_context=principal_ctx,
        action="LISTING_PUBLISH",
        resource=ResourceReference(resource_type="listing", resource_id="item_001"),
        correlation_id="corr_e2e_pipeline",
        commercial_context={"tenant_id": tenant_ctx.tenant_id, "organization_id": "org_ecom_chile"},
    )
    authz_dec = authz_service.authorize(authz_req, allowed_actions_override=list(rbac_eval.actions))
    assert authz_dec.is_allowed is True
