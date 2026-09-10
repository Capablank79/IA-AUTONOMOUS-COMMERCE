"""
Tests de Integración y E2E para O.3 — SaaS Authentication & Multi-Tenant Session Management (Hito O — SaaS / Platformization).

Escenarios cubiertos (especificados en el requerimiento):
A. N.2 valid auth -> Tenant A membership -> SaaS Session A ACTIVE.
B. Same identity without Tenant B membership -> cannot create B session.
C. Identity belonging to A/B -> separate sessions -> isolation preserved.
D. Organization A membership -> org context valid.
E. Suspended/removed membership -> session creation/validation blocked.
F. Expired session -> downstream mock not called.
G. Revoked session -> downstream mock not called.
H. Restart -> session lifecycle preserved.
I. Session tenant mismatch -> O.1 CrossTenantGuard blocks.
J. Audit/Trace safe.

E2E Pipeline O.3:
Credentials/mock -> N.2 Authentication -> N.1 Identity -> O.2 Membership -> O.3 SaaS Session -> O.1 TenantContext -> N.4 RBAC -> N.3 Authorization -> tenant-scoped operation mock.
"""

import json
import pytest
from datetime import datetime, timezone, timedelta
from pathlib import Path
import tempfile
import shutil
from unittest.mock import MagicMock

# Dominios y modelos
from src.domain.session.models import (
    SaaSSession,
    SessionStatus,
    SessionValidationReasonCode,
    SessionExpiredError,
    SessionRevokedError,
    SessionTenantMismatchError,
    SessionValidationError,
)
from src.domain.session.ports import SaaSSessionRepositoryPort, SaaSSessionServicePort
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.application.session.saas_session_service import SaaSSessionService

from src.domain.identity.models import Identity, IdentityType, IdentityStatus, IdentityReference
from src.application.identity.identity_service import IdentityService
from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository

from src.domain.authentication.models import (
    AuthenticationRequest,
    AuthenticationResult,
    AuthenticationStatus,
    AuthenticationMethod,
    PrincipalContext,
)
from src.application.authentication.authentication_service import AuthenticationService

from src.domain.tenant.models import (
    TenantContext,
    CrossTenantAccessError,
    TenantSecurityViolationError,
)
from src.domain.tenant.guard import CrossTenantGuard
from src.application.tenant.tenant_context_service import TenantContextService

from src.domain.organization.models import (
    Organization,
    OrganizationStatus,
    UserMembership,
    MembershipStatus,
    MembershipRole,
)
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonOrganizationRepository,
    JsonMembershipRepository,
)
from src.application.organization.organization_service import (
    OrganizationService,
    OrganizationMembershipService,
)

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
    ResourceReference,
)
from src.application.authorization.authorization_service import AuthorizationService

from src.domain.audit.models import AuditRecordType
from src.application.audit.audit_trail_service import AuditTrailService
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.domain.reliability.ports import ClockPort


class DeterministicClock(ClockPort):
    def __init__(self, initial_time: datetime):
        if initial_time.tzinfo is None:
            initial_time = initial_time.replace(tzinfo=timezone.utc)
        self._current = initial_time

    def now(self) -> datetime:
        return self._current

    def sleep(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self.sleep(seconds)


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="o3_integ_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def clock():
    return DeterministicClock(datetime(2026, 9, 7, 10, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def audit_repo(temp_dir):
    return JsonAuditRepository(temp_dir / "audit")


@pytest.fixture
def audit_service(audit_repo):
    return AuditTrailService(audit_repo)


@pytest.fixture
def tenant_context_service():
    return TenantContextService(
        registered_tenants={"tenant_a", "tenant_b"},
        identity_to_tenant={"usr_alice_a": "tenant_a"},
    )


@pytest.fixture
def identity_service(temp_dir):
    repo = JsonIdentityRepository(temp_dir / "identity")
    return IdentityService(repo)


@pytest.fixture
def org_services(temp_dir, tenant_context_service, identity_service, audit_repo):
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_svc = OrganizationService(organization_repo=org_repo, audit_repository=audit_repo)
    mem_svc = OrganizationMembershipService(
        membership_repo=mem_repo,
        organization_repo=org_repo,
        tenant_mapping=tenant_context_service,
        identity_repo=identity_service.repository,
        audit_repository=audit_repo,
    )
    return org_repo, mem_repo, org_svc, mem_svc


@pytest.fixture
def saas_session_service(temp_dir, tenant_context_service, identity_service, org_services, clock, audit_repo):
    org_repo, mem_repo, _, _ = org_services
    session_repo = JsonSaaSSessionRepository(temp_dir)
    return SaaSSessionService(
        session_repository=session_repo,
        tenant_resolver=tenant_context_service,
        tenant_mapping=tenant_context_service,
        organization_repo=org_repo,
        membership_repo=mem_repo,
        identity_repo=identity_service.repository,
        clock=clock,
        audit_repository=audit_repo,
    )


# ==============================================================================
# Escenario A: N.2 valid auth -> Tenant A membership -> SaaS Session A ACTIVE.
# ==============================================================================
def test_scenario_a_valid_auth_to_saas_session(
    identity_service, saas_session_service, clock
):
    ident = identity_service.register_identity(
        identity_id="usr_alice_a",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:alice_a",
        display_name="Alice Tenant A",
    )

    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=ident.to_reference(),
        authenticated_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=2),
    )

    session = saas_session_service.create_session(
        auth_result=auth_result,
        tenant_id="tenant_a",
        ttl_seconds=3600,
        correlation_id="corr_scen_a",
    )

    assert session.is_active is True
    assert session.tenant_id == "tenant_a"
    assert session.identity_id == "usr_alice_a"

    val_res = saas_session_service.validate_session(session.session_id, expected_tenant_id="tenant_a")
    assert val_res.is_valid is True
    assert val_res.status == SessionStatus.ACTIVE
    assert val_res.session_context.tenant_id == "tenant_a"


# ==============================================================================
# Escenario B: Same identity without Tenant B membership -> cannot create B session.
# ==============================================================================
def test_scenario_b_cannot_create_session_for_unauthorized_tenant(
    identity_service, saas_session_service, clock
):
    ident = identity_service.register_identity(
        identity_id="usr_alice_a",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:alice_a",
    )
    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=ident.to_reference(),
    )

    # usr_alice_a pertenece a tenant_a; intentar crear sesión en tenant_b falla por CrossTenantAccessError
    with pytest.raises(CrossTenantAccessError) as exc:
        saas_session_service.create_session(
            auth_result=auth_result,
            tenant_id="tenant_b",
        )
    assert "CROSS_TENANT_SESSION_DENIED" in str(exc.value)


# ==============================================================================
# Escenario C: Identity belonging to A/B -> separate sessions -> isolation preserved.
# ==============================================================================
def test_scenario_c_multi_tenant_identity_separate_isolated_sessions(temp_dir, clock, audit_repo):
    tenant_svc = TenantContextService(
        registered_tenants={"tenant_a", "tenant_b"},
        identity_to_tenant={},  # Sin binding exclusivo 1-a-1
    )
    ident_repo = JsonIdentityRepository(temp_dir / "identity")
    ident_svc = IdentityService(ident_repo)
    ident = ident_svc.register_identity(
        identity_id="usr_consultant_multi",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:consultant",
    )

    session_repo = JsonSaaSSessionRepository(temp_dir)
    svc = SaaSSessionService(
        session_repository=session_repo,
        tenant_resolver=tenant_svc,
        tenant_mapping=tenant_svc,
        identity_repo=ident_repo,
        clock=clock,
        audit_repository=audit_repo,
    )

    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.BEARER_TOKEN,
        provider="internal_jwt",
        principal=ident.to_reference(),
    )

    sess_a = svc.create_session(auth, tenant_id="tenant_a", ttl_seconds=1800)
    sess_b = svc.create_session(auth, tenant_id="tenant_b", ttl_seconds=1800)

    assert sess_a.session_id != sess_b.session_id
    assert sess_a.tenant_id == "tenant_a"
    assert sess_b.tenant_id == "tenant_b"

    # Validar sesión A en Tenant A -> Válido
    assert svc.validate_session(sess_a.session_id, expected_tenant_id="tenant_a").is_valid is True
    # Validar sesión A en Tenant B -> Inválido (Tenant mismatch)
    res_cross = svc.validate_session(sess_a.session_id, expected_tenant_id="tenant_b")
    assert res_cross.is_valid is False
    assert SessionValidationReasonCode.TENANT_MISMATCH.value in res_cross.reason_codes


# ==============================================================================
# Escenario D: Organization A membership -> org context valid.
# ==============================================================================
def test_scenario_d_organization_membership_valid_org_context(
    identity_service, org_services, saas_session_service, clock
):
    _, _, org_svc, mem_svc = org_services
    ident = identity_service.register_identity(
        identity_id="usr_alice_a",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:alice_a",
    )
    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_alice_a")
    org = org_svc.create_organization(ctx_a, organization_id="org_alpha_finance", name="Finance Dept")
    mem_svc.add_membership(ctx_a, organization_id="org_alpha_finance", identity_id="usr_alice_a", role=MembershipRole.ADMIN)

    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=ident.to_reference(),
    )

    sess = saas_session_service.create_session(
        auth, tenant_id="tenant_a", organization_id="org_alpha_finance"
    )

    assert sess.organization_id == "org_alpha_finance"
    val = saas_session_service.validate_session(sess.session_id, expected_tenant_id="tenant_a")
    assert val.is_valid is True
    assert val.session_context.organization_id == "org_alpha_finance"

    tenant_ctx = val.session_context.to_tenant_context()
    assert tenant_ctx.metadata.get("organization_id") == "org_alpha_finance"


# ==============================================================================
# Escenario E: Suspended/removed membership -> session creation/validation blocked.
# ==============================================================================
def test_scenario_e_suspended_or_removed_membership_blocks_session(
    identity_service, org_services, saas_session_service, clock
):
    _, _, org_svc, mem_svc = org_services
    ident = identity_service.register_identity(
        identity_id="usr_alice_a",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:alice_a",
    )
    ctx_a = TenantContext(tenant_id="tenant_a", identity_id="usr_alice_a")
    org_svc.create_organization(ctx_a, organization_id="org_sec_ops", name="SecOps")
    mem_svc.add_membership(ctx_a, organization_id="org_sec_ops", identity_id="usr_alice_a", role=MembershipRole.MEMBER)

    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=ident.to_reference(),
    )

    sess = saas_session_service.create_session(auth, tenant_id="tenant_a", organization_id="org_sec_ops")
    assert sess.is_active is True

    # Remover la membresía
    mem_svc.remove_membership(ctx_a, organization_id="org_sec_ops", identity_id="usr_alice_a")

    # Validación de sesión posterior queda bloqueada
    val_res = saas_session_service.validate_session(sess.session_id, expected_tenant_id="tenant_a")
    assert val_res.is_valid is False
    assert SessionValidationReasonCode.MEMBERSHIP_INACTIVE.value in val_res.reason_codes


# ==============================================================================
# Escenario F: Expired session -> downstream mock not called.
# ==============================================================================
def test_scenario_f_expired_session_prevents_downstream_calls(
    identity_service, saas_session_service, clock
):
    ident = identity_service.register_identity(
        identity_id="usr_alice_a",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:alice_a",
    )
    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=ident.to_reference(),
    )
    sess = saas_session_service.create_session(auth, tenant_id="tenant_a", ttl_seconds=60)

    # Mock de operación downstream
    downstream_mock = MagicMock()

    # Avanzar reloj 61 segundos (expiración)
    clock.advance(61)

    val_res = saas_session_service.validate_session(sess.session_id, expected_tenant_id="tenant_a")
    if val_res.is_valid:
        downstream_mock(val_res.session_context)

    downstream_mock.assert_not_called()


# ==============================================================================
# Escenario G: Revoked session -> downstream mock not called.
# ==============================================================================
def test_scenario_g_revoked_session_prevents_downstream_calls(
    identity_service, saas_session_service
):
    ident = identity_service.register_identity(
        identity_id="usr_alice_a",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:alice_a",
    )
    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=ident.to_reference(),
    )
    sess = saas_session_service.create_session(auth, tenant_id="tenant_a")

    downstream_mock = MagicMock()

    # Revocar sesión explícitamente
    saas_session_service.revoke_session(sess.session_id, tenant_id="tenant_a", reason="Admin revocation")

    val_res = saas_session_service.validate_session(sess.session_id, expected_tenant_id="tenant_a")
    if val_res.is_valid:
        downstream_mock(val_res.session_context)

    downstream_mock.assert_not_called()


# ==============================================================================
# Escenario H: Restart -> session lifecycle preserved.
# ==============================================================================
def test_scenario_h_restart_preserves_lifecycle(temp_dir, clock, tenant_context_service, audit_repo):
    repo1 = JsonSaaSSessionRepository(temp_dir)
    svc1 = SaaSSessionService(session_repository=repo1, tenant_resolver=tenant_context_service, clock=clock, audit_repository=audit_repo)

    principal = IdentityReference(identity_id="usr_alice_a", identity_type=IdentityType.USER, canonical_identifier="user:oauth:alice_a")
    auth = AuthenticationResult(status=AuthenticationStatus.AUTHENTICATED, method=AuthenticationMethod.OAUTH2, provider="google", principal=principal)

    active_sess = svc1.create_session(auth, tenant_id="tenant_a", ttl_seconds=3600)
    revoked_sess = svc1.create_session(auth, tenant_id="tenant_a", ttl_seconds=3600)
    svc1.revoke_session(revoked_sess.session_id, tenant_id="tenant_a", reason="test_revocation")

    # Reinicio: nuevo repositorio y nuevo servicio sobre el mismo disco
    repo2 = JsonSaaSSessionRepository(temp_dir)
    svc2 = SaaSSessionService(session_repository=repo2, tenant_resolver=tenant_context_service, clock=clock, audit_repository=audit_repo)

    val_act = svc2.validate_session(active_sess.session_id, expected_tenant_id="tenant_a")
    assert val_act.is_valid is True
    assert val_act.status == SessionStatus.ACTIVE

    val_rev = svc2.validate_session(revoked_sess.session_id, expected_tenant_id="tenant_a")
    assert val_rev.is_valid is False
    assert val_rev.status == SessionStatus.REVOKED


# ==============================================================================
# Escenario I: Session tenant mismatch -> O.1 CrossTenantGuard blocks.
# ==============================================================================
def test_scenario_i_cross_tenant_guard_blocks_mismatch(
    identity_service, saas_session_service
):
    ident = identity_service.register_identity(
        identity_id="usr_alice_a",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:alice_a",
    )
    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=ident.to_reference(),
    )
    sess = saas_session_service.create_session(auth, tenant_id="tenant_a")

    sess_ctx = saas_session_service.get_session_context(sess.session_id, expected_tenant_id="tenant_a")
    tenant_ctx = sess_ctx.to_tenant_context()

    # Operación destinada a resource en tenant_b protegida por CrossTenantGuard
    with pytest.raises(CrossTenantAccessError):
        CrossTenantGuard.assert_same_tenant(tenant_ctx, "tenant_b", operation_name="execute_cross_op")


# ==============================================================================
# Escenario J: Audit/Trace safe.
# ==============================================================================
def test_scenario_j_audit_and_trace_safety(
    identity_service, saas_session_service, audit_repo
):
    ident = identity_service.register_identity(
        identity_id="usr_alice_a",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:alice_a",
    )
    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        principal=ident.to_reference(),
    )
    sess = saas_session_service.create_session(auth, tenant_id="tenant_a", correlation_id="audit_corr_01")
    saas_session_service.validate_session(sess.session_id, expected_tenant_id="tenant_a", correlation_id="audit_corr_01")
    saas_session_service.revoke_session(sess.session_id, tenant_id="tenant_a", reason="audit_check", correlation_id="audit_corr_01")

    records = audit_repo.list_records(correlation_id="audit_corr_01")
    rec_types = [r.record_type for r in records]

    assert AuditRecordType.SESSION_CREATED in rec_types
    assert AuditRecordType.SESSION_VALIDATED in rec_types
    assert AuditRecordType.SESSION_REVOKED in rec_types

    # Asegurar que ningún registro exponga credenciales
    for r in records:
        assert "password" not in r.metadata
        assert "access_token" not in r.metadata
        assert "refresh_token" not in r.metadata


# ==============================================================================
# Escenario E2E: Pipeline Completo
# Credentials -> N.2 Auth -> N.1 Identity -> O.2 Membership -> O.3 SaaS Session -> O.1 TenantContext -> N.4 RBAC -> N.3 Authorization -> tenant mock
# ==============================================================================
def test_e2e_full_saas_authentication_and_authorization_pipeline(
    temp_dir, clock, tenant_context_service, audit_repo
):
    # 1. N.1 Identity Service
    ident_repo = JsonIdentityRepository(temp_dir / "identity")
    ident_svc = IdentityService(ident_repo)
    user_identity = ident_svc.register_identity(
        identity_id="usr_operator_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:oauth:operator_01",
        display_name="Operations Lead",
    )

    # 2. N.2 Authentication
    auth_req = AuthenticationRequest(
        method=AuthenticationMethod.OAUTH2,
        provider="google",
        token_or_secret="dummy_oauth_token",
        declared_subject="operator_01",
    )
    # Autenticación exitosa que produce AuthenticationResult N.2
    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=auth_req.method,
        provider=auth_req.provider,
        principal=user_identity.to_reference(),
        authenticated_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
        reason_codes=("TOKEN_VALIDATED_SUCCESSFULLY",),
        correlation_id="corr_e2e_pipeline",
    )

    # 3. O.2 Organizations & Memberships
    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_svc = OrganizationService(org_repo, audit_repository=audit_repo)
    mem_svc = OrganizationMembershipService(
        membership_repo=mem_repo,
        organization_repo=org_repo,
        tenant_mapping=tenant_context_service,
        identity_repo=ident_repo,
        audit_repository=audit_repo,
    )

    ctx_tenant_a = TenantContext(tenant_id="tenant_a", identity_id=user_identity.identity_id)
    org = org_svc.create_organization(ctx_tenant_a, organization_id="org_logistics_hub", name="Logistics Hub")
    mem_svc.add_membership(
            ctx_tenant_a, organization_id="org_logistics_hub", identity_id=user_identity.identity_id, role=MembershipRole.MEMBER
        )

    # 4. O.3 SaaS Session Service
    session_repo = JsonSaaSSessionRepository(temp_dir)
    session_svc = SaaSSessionService(
        session_repository=session_repo,
        tenant_resolver=tenant_context_service,
        tenant_mapping=tenant_context_service,
        organization_repo=org_repo,
        membership_repo=mem_repo,
        identity_repo=ident_repo,
        clock=clock,
        audit_repository=audit_repo,
    )

    saas_session = session_svc.create_session(
        auth_result=auth_result,
        tenant_id="tenant_a",
        organization_id="org_logistics_hub",
        ttl_seconds=3600,
        correlation_id="corr_e2e_pipeline",
    )

    # 5. Validación y derivación a O.1 TenantContext
    session_context = session_svc.get_session_context(
        session_id=saas_session.session_id,
        expected_tenant_id="tenant_a",
        correlation_id="corr_e2e_pipeline",
    )
    tenant_context = session_context.to_tenant_context()

    # 6. N.4 RBAC & N.3 Authorization
    role_repo = JsonRoleRepository(temp_dir / "roles")
    assign_repo = JsonRoleAssignmentRepository(temp_dir / "assignments")
    rbac_svc = RBACService(role_repository=role_repo, assignment_repository=assign_repo, audit_repository=audit_repo)

    # Crear rol y asignar permiso 'order:fulfill'
    perm = rbac_svc.create_permission(
        permission_id="perm_order_fulfill",
        action="order:fulfill",
        resource_scope="order",
    )
    rbac_svc.define_role(
        role_id="role_operator",
        name="Operator Role",
        permissions=[perm],
    )
    rbac_svc.assign_role(
        identity_id=user_identity.identity_id,
        role_id="role_operator",
    )

    authz_svc = AuthorizationService(
        audit_repository=audit_repo,
        clock=clock,
        policies_by_resource={"order:ord_9901": ("fulfill",)},
    )

    # 7. Operación dentro del tenant con sesión activa
    operation_target = ResourceReference(resource_type="order", resource_id="ord_9901")
    principal_ctx = PrincipalContext(
        principal=user_identity.to_reference(),
        auth_result=auth_result,
    )
    authz_req = AuthorizationRequest(
        principal_context=principal_ctx,
        action="fulfill",
        resource=operation_target,
        correlation_id="corr_e2e_pipeline",
    )
    authz_res = authz_svc.authorize(authz_req)

    assert authz_res.is_allowed is True

    # 8. Demostración de aislamiento y ciclo de vida: Si la sesión se revoca o expira -> Bloqueado
    session_svc.logout(saas_session.session_id, tenant_id="tenant_a")

    with pytest.raises(SessionRevokedError):
        session_svc.get_session_context(saas_session.session_id, expected_tenant_id="tenant_a")
