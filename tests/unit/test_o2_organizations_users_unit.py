"""
Tests Unitarios Exhaustivos para O.2 — Organizations / Users (Hito O — SaaS / Platformization).

Verificaciones canónicas mínimas:
1. immutable Organization
2. immutable UserMembership
3. organization bound to tenant
4. membership bound to tenant/org
5. cross-tenant membership denied
6. identity != user membership
7. member != admin
8. removed membership inactive
9. suspended membership inactive
10. idempotent membership
11. duplicate conflict
12. safe identifiers
13. deterministic checksum
14. no secrets/PII leakage
15. tenant isolation reused
16. no O.3+ implementation
"""

import pytest
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import shutil

from src.domain.organization.models import (
    Organization,
    OrganizationStatus,
    OrganizationReference,
    UserMembership,
    MembershipStatus,
    MembershipRole,
    compute_organization_checksum,
    compute_membership_checksum,
)
from src.domain.tenant.models import (
    TenantId,
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

# Integración N.1 / N.4
from src.domain.identity.models import Identity, IdentityType, IdentityStatus
from src.domain.rbac.models import Permission, Role, RoleAssignment, PermissionStatus
from src.application.rbac.rbac_service import RBACService
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="o2_unit_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def tenant_context_a():
    return TenantContext(
        tenant_id="tenant_alpha",
        identity_id="usr_alice_01",
        correlation_id="corr_unit_01",
    )


@pytest.fixture
def tenant_context_b():
    return TenantContext(
        tenant_id="tenant_beta",
        identity_id="usr_bob_02",
        correlation_id="corr_unit_02",
    )


def test_01_immutable_organization():
    """1. Organization es inmutable y valida campos requeridos."""
    now = datetime.now(timezone.utc)
    org = Organization(
        organization_id="org_retail_ops",
        tenant_id="tenant_alpha",
        name="Retail Operations Division",
        created_at=now,
        updated_at=now,
        status=OrganizationStatus.ACTIVE,
        metadata={"region": "LATAM"},
    )
    assert org.organization_id == "org_retail_ops"
    assert org.tenant_id == "tenant_alpha"
    assert org.name == "Retail Operations Division"
    assert org.is_active is True
    assert org.metadata["region"] == "LATAM"
    assert len(org.checksum) == 64

    with pytest.raises(Exception):
        org.name = "Mutated Name"


def test_02_immutable_user_membership():
    """2. UserMembership es inmutable y enlaza tenant, org e identity."""
    now = datetime.now(timezone.utc)
    mem = UserMembership(
        membership_id="mem_alpha_org1_user1",
        tenant_id="tenant_alpha",
        organization_id="org_retail_ops",
        identity_id="usr_alice_01",
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
        joined_at=now,
    )
    assert mem.membership_id == "mem_alpha_org1_user1"
    assert mem.tenant_id == "tenant_alpha"
    assert mem.organization_id == "org_retail_ops"
    assert mem.identity_id == "usr_alice_01"
    assert mem.role == MembershipRole.MEMBER
    assert mem.is_active is True
    assert len(mem.checksum) == 64

    with pytest.raises(Exception):
        mem.status = MembershipStatus.REMOVED


def test_03_organization_bound_to_tenant(temp_dir, tenant_context_a, tenant_context_b):
    """3. Organization está estrictamente ligada a su tenant."""
    org_repo = JsonOrganizationRepository(temp_dir)
    org_service = OrganizationService(org_repo)

    org_a = org_service.create_organization(
        context=tenant_context_a,
        organization_id="org_logistics",
        name="Alpha Logistics",
    )
    assert org_a.tenant_id == "tenant_alpha"

    # Contexto B no puede acceder a la org de Tenant A
    retrieved_from_b = org_service.get_organization(tenant_context_b, "org_logistics")
    assert retrieved_from_b is None

    # Listar desde Contexto B retorna lista vacía
    orgs_b = org_service.list_organizations(tenant_context_b)
    assert len(orgs_b) == 0


def test_04_membership_bound_to_tenant_and_org(temp_dir, tenant_context_a):
    """4. Membership requiere que organization exista en el mismo tenant."""
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(mem_repo, org_repo)

    # Crear org en tenant A
    org_service.create_organization(tenant_context_a, "org_sales", "Alpha Sales")

    # Agregar membresía
    mem = mem_service.add_membership(
        context=tenant_context_a,
        organization_id="org_sales",
        identity_id="usr_alice_01",
        role=MembershipRole.OWNER,
    )
    assert mem.tenant_id == "tenant_alpha"
    assert mem.organization_id == "org_sales"
    assert mem.identity_id == "usr_alice_01"

    # Intentar membresía en organización no existente en el tenant -> error
    with pytest.raises(ValueError, match="does not exist in tenant"):
        mem_service.add_membership(
            context=tenant_context_a,
            organization_id="org_non_existent",
            identity_id="usr_alice_01",
        )


def test_05_cross_tenant_membership_denied(temp_dir, tenant_context_a, tenant_context_b):
    """5. Identidad perteneciente a Tenant A no puede unirse a Org de Tenant B."""
    tenant_service = TenantContextService()
    tenant_service.bind_identity("tenant_alpha", "usr_alice_01")
    tenant_service.bind_identity("tenant_beta", "usr_bob_02")

    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(mem_repo, org_repo, tenant_mapping=tenant_service)

    # Crear org en Tenant B
    org_service.create_organization(tenant_context_b, "org_finance_b", "Beta Finance")

    # Intentar agregar identidad de Tenant A (Alice) a Org de Tenant B -> CrossTenantAccessError
    with pytest.raises(CrossTenantAccessError, match="CROSS_TENANT_MEMBERSHIP_DENIED"):
        mem_service.add_membership(
            context=tenant_context_b,
            organization_id="org_finance_b",
            identity_id="usr_alice_01",
        )


def test_06_identity_is_not_user_membership():
    """6. N.1 Identity != O.2 UserMembership (desacoplamiento ontológico)."""
    now = datetime.now(timezone.utc)
    identity = Identity(
        identity_id="usr_carlos_10",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_carlos_10",
        created_at=now,
        updated_at=now,
        status=IdentityStatus.ACTIVE,
    )

    membership = UserMembership(
        membership_id="mem_alpha_marketing_carlos",
        tenant_id="tenant_alpha",
        organization_id="org_marketing",
        identity_id=identity.identity_id,
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
        joined_at=now,
    )

    assert type(identity) is not type(membership)
    assert membership.identity_id == identity.identity_id
    assert not hasattr(identity, "organization_id")
    assert not hasattr(membership, "identity_type")


def test_07_member_is_not_admin_rbac(temp_dir, tenant_context_a):
    """7. Ser MEMBER u OWNER en O.2 NO equivale a rol RBAC ADMIN (N.4)."""
    role_repo = JsonRoleRepository(temp_dir / "rbac")
    assign_repo = JsonRoleAssignmentRepository(temp_dir / "rbac")
    rbac_service = RBACService(role_repo, assign_repo)

    # Identidad con membresía OWNER en org
    now = datetime.now(timezone.utc)
    mem = UserMembership(
        membership_id="mem_alpha_org_owner",
        tenant_id="tenant_alpha",
        organization_id="org_alpha_core",
        identity_id="usr_alice_01",
        role=MembershipRole.OWNER,
        status=MembershipStatus.ACTIVE,
        joined_at=now,
    )

    # Sin asignación RBAC en N.4, la identidad no tiene permisos de administración
    eval_res = rbac_service.resolve_effective_permissions(
        principal_or_identity="usr_alice_01",
        scope="acc_main",
    )
    assert "LISTING_PUBLISH" not in eval_res.actions
    assert "PRICE_UPDATE" not in eval_res.actions
    assert "ORDER_MANAGE" not in eval_res.actions
    assert eval_res.is_empty


def test_08_removed_membership_inactive(temp_dir, tenant_context_a):
    """8. Membresía REMOVED queda inactiva y no valida acceso."""
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(mem_repo, org_repo)

    org_service.create_organization(tenant_context_a, "org_dev", "Dev Org")
    mem_service.add_membership(tenant_context_a, "org_dev", "usr_alice_01")

    assert mem_service.validate_membership_access(tenant_context_a, "org_dev", "usr_alice_01") is True

    # Remover
    removed = mem_service.remove_membership(tenant_context_a, "org_dev", "usr_alice_01")
    assert removed.status == MembershipStatus.REMOVED
    assert removed.is_active is False
    assert removed.removed_at is not None

    # Validar que ya no tiene acceso
    assert mem_service.validate_membership_access(tenant_context_a, "org_dev", "usr_alice_01") is False


def test_09_suspended_membership_inactive(temp_dir, tenant_context_a):
    """9. Membresía SUSPENDED no produce acceso efectivo."""
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(mem_repo, org_repo)

    org_service.create_organization(tenant_context_a, "org_dev", "Dev Org")
    mem = mem_service.add_membership(tenant_context_a, "org_dev", "usr_alice_01")

    mem_service.update_membership_status(
        tenant_context_a,
        mem.membership_id,
        MembershipStatus.SUSPENDED,
    )

    assert mem_service.validate_membership_access(tenant_context_a, "org_dev", "usr_alice_01") is False


def test_10_idempotent_membership_and_organization(temp_dir, tenant_context_a):
    """10. Creación idempotente de Organization y Membership."""
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(org_repo)
    mem_service = OrganizationMembershipService(mem_repo, org_repo)

    org1 = org_service.create_organization(tenant_context_a, "org_audit", "Audit Unit")
    org2 = org_service.create_organization(tenant_context_a, "org_audit", "Audit Unit")
    assert org1.organization_id == org2.organization_id
    assert org1.checksum == org2.checksum

    mem1 = mem_service.add_membership(tenant_context_a, "org_audit", "usr_alice_01", role=MembershipRole.MEMBER)
    mem2 = mem_service.add_membership(tenant_context_a, "org_audit", "usr_alice_01", role=MembershipRole.MEMBER)
    assert mem1.membership_id == mem2.membership_id
    assert mem1.checksum == mem2.checksum


def test_11_duplicate_conflict_different_attributes(temp_dir, tenant_context_a):
    """11. Conflicto explícito si se intenta recrear una org con atributos incompatibles."""
    org_repo = JsonOrganizationRepository(temp_dir)
    org_service = OrganizationService(org_repo)

    org_service.create_organization(tenant_context_a, "org_conflict", "Original Name")
    with pytest.raises(ValueError, match="already exists with different attributes"):
        org_service.create_organization(tenant_context_a, "org_conflict", "Different Name")


def test_12_safe_identifiers_path_traversal_prevention(temp_dir, tenant_context_a):
    """12. Rechazo estricto de identificadores con path traversal."""
    with pytest.raises(ValueError, match="unsafe path traversal sequences or separators"):
        Organization(
            organization_id="../traversal_org",
            tenant_id="tenant_alpha",
            name="Traversal",
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    with pytest.raises(ValueError, match="unsafe path traversal sequences or separators"):
        UserMembership(
            membership_id="mem/bad/id",
            tenant_id="tenant_alpha",
            organization_id="org_clean",
            identity_id="usr_01",
            joined_at=datetime.now(timezone.utc),
        )


def test_13_deterministic_checksum_validation():
    """13. Checksum determinista SHA-256 detecta manipulación."""
    now = datetime.now(timezone.utc)
    org = Organization(
        organization_id="org_sec",
        tenant_id="tenant_alpha",
        name="Security Division",
        created_at=now,
        updated_at=now,
    )
    assert len(org.checksum) == 64

    # Checksum alterado lanza error en validación
    with pytest.raises(ValueError, match="Checksum mismatch"):
        Organization(
            organization_id="org_sec",
            tenant_id="tenant_alpha",
            name="Security Division",
            created_at=now,
            updated_at=now,
            checksum="0000000000000000000000000000000000000000000000000000000000000000",
        )


def test_14_no_secrets_and_pii_leakage(temp_dir, tenant_context_a):
    """14. Sanitización profunda de metadata en Organization y Membership."""
    now = datetime.now(timezone.utc)
    org = Organization(
        organization_id="org_pii_test",
        tenant_id="tenant_alpha",
        name="PII Test Org",
        created_at=now,
        updated_at=now,
        metadata={"token": "secret_12345", "password": "supersecretpassword", "dept": "HR"},
    )
    assert org.metadata["token"] == "[REDACTED]"
    assert org.metadata["password"] == "[REDACTED]"
    assert org.metadata["dept"] == "HR"

    mem = UserMembership(
        membership_id="mem_pii_test",
        tenant_id="tenant_alpha",
        organization_id="org_pii_test",
        identity_id="usr_alice_01",
        joined_at=now,
        metadata={"api_key": "raw_secret_key", "source": "web"},
    )
    assert mem.metadata["api_key"] == "[REDACTED]"
    assert mem.metadata["source"] == "web"


def test_15_tenant_isolation_reused(temp_dir, tenant_context_a, tenant_context_b):
    """15. Reutiliza el aislamiento de O.1 impidiendo mezclas en disco."""
    org_repo = JsonOrganizationRepository(temp_dir)
    org_service = OrganizationService(org_repo)

    # Crear misma org_id en dos tenants distintos
    org_a = org_service.create_organization(tenant_context_a, "org_local", "Local Org Alpha")
    org_b = org_service.create_organization(tenant_context_b, "org_local", "Local Org Beta")

    assert org_a.tenant_id == "tenant_alpha"
    assert org_b.tenant_id == "tenant_beta"

    # Verificar que los archivos físicos están particionados bajo /tenants/tenant_id/
    path_a = temp_dir / "tenants" / "tenant_alpha" / "organizations" / "org_local.json"
    path_b = temp_dir / "tenants" / "tenant_beta" / "organizations" / "org_local.json"

    assert path_a.exists()
    assert path_b.exists()


def test_16_no_o3_plus_leakage():
    """16. Confirma que no se implementaron elementos prematuros de O.3+ (billing, plans, subscriptions)."""
    import src.domain.organization.models as org_models
    assert not hasattr(org_models, "BillingAccount")
    assert not hasattr(org_models, "SubscriptionPlan")
    assert not hasattr(org_models, "QuotaPolicy")
    assert not hasattr(org_models, "TenantPricingPlan")
