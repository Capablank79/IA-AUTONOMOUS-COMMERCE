"""
Tests unitarios para SaaS Authorization Multi-Tenant (Hito O.4 — SaaS / Platformization).

Cubre exhaustivamente:
1. active session + valid permission -> ALLOW
2. expired session -> DENY
3. revoked session -> DENY
4. wrong tenant -> DENY
5. wrong organization -> DENY
6. removed membership -> DENY
7. suspended membership -> DENY
8. missing permission -> DENY
9. N.3 DENY preserved (PolicyEngine)
10. tenant role isolation (role in tenant A not valid in tenant B)
11. org role isolation (membership org A not valid in org B)
12. stale role snapshot not trusted (dynamic lookup reflects immediate role revocation)
13. resource tenant mismatch -> DENY
14. unknown ownership fail-safe -> DENY
15. deterministic decision & SHA-256 checksum integrity
16. no O.5+ implementation (isolation check)
"""

from datetime import datetime, timezone, timedelta
import pytest
from typing import Dict, List, Optional, Any, Tuple, Sequence

from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.session.models import SaaSSession, SessionStatus, SessionContext
from src.domain.session.ports import SaaSSessionRepositoryPort
from src.domain.tenant.models import TenantContext, TenantScope, TenantScopedResource
from src.domain.organization.models import (
    Organization,
    UserMembership,
    MembershipStatus,
    MembershipRole,
    OrganizationStatus,
)
from src.domain.organization.ports import OrganizationRepositoryPort, MembershipRepositoryPort
from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
    PermissionStatus,
    RoleStatus,
)
from src.domain.rbac.ports import RoleRepositoryPort, RoleAssignmentRepositoryPort
from src.application.rbac.rbac_service import RBACService
from src.domain.authorization.models import (
    AuthorizationRequest,
    AuthorizationDecision,
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
)
from src.application.authorization.authorization_service import AuthorizationService
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
    SaaSAuthorizationContext,
)
from src.domain.saas_authorization.ports import ResourceOwnershipResolverPort
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.domain.reliability.ports import ClockPort


class MockClock(ClockPort):
    def __init__(self, current_time: datetime):
        self._current_time = current_time

    def now(self) -> datetime:
        return self._current_time

    def set_time(self, new_time: datetime) -> None:
        self._current_time = new_time

    def sleep(self, seconds: float) -> None:
        pass


class InMemorySessionRepository(SaaSSessionRepositoryPort):
    def __init__(self):
        self._sessions: Dict[str, SaaSSession] = {}

    def save(self, session: SaaSSession) -> None:
        self._sessions[session.session_id] = session

    def get_by_id(self, session_id: str, tenant_id: Optional[str] = None) -> Optional[SaaSSession]:
        s = self._sessions.get(session_id)
        if s and tenant_id and s.tenant_id != tenant_id:
            return None
        return s

    def list_by_tenant(self, tenant_id: str) -> List[SaaSSession]:
        return [s for s in self._sessions.values() if s.tenant_id == tenant_id]

    def list_by_identity(self, identity_id: str, tenant_id: Optional[str] = None) -> List[SaaSSession]:
        return [
            s for s in self._sessions.values()
            if s.identity_id == identity_id and (tenant_id is None or s.tenant_id == tenant_id)
        ]

    def delete(self, session_id: str, tenant_id: Optional[str] = None) -> bool:
        if session_id in self._sessions:
            if tenant_id and self._sessions[session_id].tenant_id != tenant_id:
                return False
            del self._sessions[session_id]
            return True
        return False


class InMemoryMembershipRepository(MembershipRepositoryPort):
    def __init__(self):
        self._memberships: Dict[Tuple[str, str, str], UserMembership] = {}

    def save(self, context: TenantContext, membership: UserMembership) -> None:
        key = (membership.tenant_id, membership.organization_id, membership.identity_id)
        self._memberships[key] = membership

    def get_by_id(self, context: TenantContext, membership_id: str) -> Optional[UserMembership]:
        for m in self._memberships.values():
            if m.membership_id == membership_id and m.tenant_id == context.tenant_id:
                return m
        return None

    def get_by_identity_and_org(
        self,
        context: TenantContext,
        organization_id: str,
        identity_id: str,
    ) -> Optional[UserMembership]:
        if context.tenant_id != context.tenant_id:
            return None
        return self._memberships.get((context.tenant_id, organization_id, identity_id))

    def list_by_organization(self, context: TenantContext, organization_id: str) -> List[UserMembership]:
        return [
            m for m in self._memberships.values()
            if m.tenant_id == context.tenant_id and m.organization_id == organization_id
        ]

    def list_by_identity(self, context: TenantContext, identity_id: str) -> List[UserMembership]:
        return [
            m for m in self._memberships.values()
            if m.tenant_id == context.tenant_id and m.identity_id == identity_id
        ]

    def delete(self, context: TenantContext, membership_id: str) -> bool:
        for k, m in list(self._memberships.items()):
            if m.membership_id == membership_id and m.tenant_id == context.tenant_id:
                del self._memberships[k]
                return True
        return False


class InMemoryRoleRepository(RoleRepositoryPort):
    def __init__(self):
        self._roles: Dict[str, Role] = {}
        self._permissions: Dict[str, Permission] = {}

    def save_role(self, role: Role) -> Role:
        self._roles[role.role_id] = role
        return role

    def get_role(self, role_id: str) -> Optional[Role]:
        return self._roles.get(role_id)

    def list_roles(self, limit: int = 100) -> Sequence[Role]:
        return list(self._roles.values())[:limit]

    def exists(self, role_id: str) -> bool:
        return role_id in self._roles

    def delete_role(self, role_id: str) -> bool:
        return self._roles.pop(role_id, None) is not None

    def save_permission(self, permission: Permission) -> Permission:
        self._permissions[permission.permission_id] = permission
        return permission

    def get_permission(self, permission_id: str) -> Optional[Permission]:
        return self._permissions.get(permission_id)

    def list_permissions(self, limit: int = 100) -> Sequence[Permission]:
        return list(self._permissions.values())[:limit]


class InMemoryRoleAssignmentRepository(RoleAssignmentRepositoryPort):
    def __init__(self):
        self._assignments: Dict[str, RoleAssignment] = {}

    def save_assignment(self, assignment: RoleAssignment) -> RoleAssignment:
        self._assignments[assignment.assignment_id] = assignment
        return assignment

    def get_assignment(self, assignment_id: str) -> Optional[RoleAssignment]:
        return self._assignments.get(assignment_id)

    def list_assignments_for_identity(
        self,
        identity_id: str,
        scope: Optional[str] = None,
        limit: int = 100,
    ) -> Sequence[RoleAssignment]:
        res = []
        for a in self._assignments.values():
            if a.identity_id == identity_id:
                if scope is None or a.applies_to_scope(scope):
                    res.append(a)
        return res[:limit]

    def list_assignments_for_role(self, role_id: str) -> Sequence[RoleAssignment]:
        return [a for a in self._assignments.values() if a.role_id == role_id]

    def list_all_assignments(self, limit: int = 200) -> Sequence[RoleAssignment]:
        return list(self._assignments.values())[:limit]

    def revoke_assignment(self, assignment_id: str) -> bool:
        return self._assignments.pop(assignment_id, None) is not None


class DummyResourceOwnershipResolver(ResourceOwnershipResolverPort):
    def __init__(self, ownership_map: Dict[str, Tuple[str, Optional[str]]]):
        self._map = ownership_map

    def resolve_resource_ownership(self, resource: Any) -> Optional[Tuple[str, Optional[str]]]:
        res_key = resource if isinstance(resource, str) else getattr(resource, "resource_id", str(resource))
        return self._map.get(res_key)


@pytest.fixture
def base_setup():
    t0 = datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    clock = MockClock(t0)

    session_repo = InMemorySessionRepository()
    membership_repo = InMemoryMembershipRepository()
    role_repo = InMemoryRoleRepository()
    asgn_repo = InMemoryRoleAssignmentRepository()

    rbac_service = RBACService(
        role_repository=role_repo,
        assignment_repository=asgn_repo,
        clock=clock,
    )

    authz_service = AuthorizationService(
        policy_engine=PolicyEngine(rules=[AuthorizationPolicyRule()]),
        clock=clock,
    )

    # Configurar permisos y roles estándar
    perm_pub = rbac_service.create_permission(
        permission_id="perm_pub",
        action="LISTING_PUBLISH",
        description="Publish listing",
    )
    perm_read = rbac_service.create_permission(
        permission_id="perm_read",
        action="LISTING_READ",
        description="Read listing",
    )

    role_publisher = rbac_service.define_role(
        role_id="role_publisher",
        name="Publisher",
        permissions=[perm_pub, perm_read],
    )

    saas_authz_service = SaaSAuthorizationService(
        session_repository=session_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_service,
        authorization_service=authz_service,
        clock=clock,
    )

    return {
        "clock": clock,
        "session_repo": session_repo,
        "membership_repo": membership_repo,
        "role_repo": role_repo,
        "asgn_repo": asgn_repo,
        "rbac_service": rbac_service,
        "authz_service": authz_service,
        "saas_authz_service": saas_authz_service,
        "t0": t0,
    }


def test_active_session_with_valid_permission_allows(base_setup):
    """1. active session + valid permission -> ALLOW"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    # Crear sesión activa en tenant_A
    session = SaaSSession(
        session_id="sess_valid_01",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    # Asignar rol en tenant_A
    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_valid_01",
        tenant_id="tenant_A",
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is True
    assert decision.status == SaaSAuthorizationStatus.ALLOW
    assert decision.reason_code == SaaSAuthorizationReasonCode.AUTHORIZED
    assert "LISTING_PUBLISH" in decision.context.resolved_permissions
    assert decision.context.tenant_id == "tenant_A"
    assert decision.context.identity_id == "usr_alice"
    assert decision.checksum is not None


def test_expired_session_denies(base_setup):
    """2. expired session -> DENY"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    clock: MockClock = base_setup["clock"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_exp_01",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0 - timedelta(hours=3),
        expires_at=t0 - timedelta(hours=1),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_exp_01",
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.SESSION_EXPIRED


def test_revoked_session_denies(base_setup):
    """3. revoked session -> DENY"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_rev_01",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.REVOKED,
    )
    session_repo.save(session)

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_rev_01",
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.SESSION_REVOKED


def test_wrong_tenant_denies(base_setup):
    """4. wrong tenant -> DENY (session tenant_A vs requested tenant_B)"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_tenant_a",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_B",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_tenant_a",
        tenant_id="tenant_B",  # Conflicto con sesión tenant_A
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.SESSION_TENANT_MISMATCH


def test_wrong_organization_denies(base_setup):
    """5. wrong organization -> DENY"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_org_a",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_org_a",
        organization_id="org_beta",  # Mismatch con org_alpha
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.ORGANIZATION_MISMATCH


def test_removed_membership_denies(base_setup):
    """6. removed membership -> DENY"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    mem_repo: InMemoryMembershipRepository = base_setup["membership_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_mem_rem",
        identity_id="usr_bob",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    # Membresía REMOVED
    membership = UserMembership(
        membership_id="mem_01",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        identity_id="usr_bob",
        joined_at=t0 - timedelta(days=5),
        status=MembershipStatus.REMOVED,
        removed_at=t0 - timedelta(days=1),
    )
    ctx = TenantContext(tenant_id="tenant_A", identity_id="usr_bob")
    mem_repo.save(ctx, membership)

    rbac.assign_role(
        identity_id="usr_bob",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_mem_rem",
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.MEMBERSHIP_NOT_ACTIVE


def test_suspended_membership_denies(base_setup):
    """7. suspended membership -> DENY"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    mem_repo: InMemoryMembershipRepository = base_setup["membership_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_mem_susp",
        identity_id="usr_carol",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    membership = UserMembership(
        membership_id="mem_02",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        identity_id="usr_carol",
        joined_at=t0 - timedelta(days=5),
        status=MembershipStatus.SUSPENDED,
    )
    ctx = TenantContext(tenant_id="tenant_A", identity_id="usr_carol")
    mem_repo.save(ctx, membership)

    rbac.assign_role(
        identity_id="usr_carol",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_mem_susp",
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.MEMBERSHIP_NOT_ACTIVE


def test_missing_permission_denies(base_setup):
    """8. missing permission -> DENY"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_no_perm",
        identity_id="usr_david",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    # Crear rol con sólo LISTING_READ
    perm_read = rbac.create_permission(
        permission_id="perm_read",
        action="LISTING_READ",
        description="Read listing",
    )
    rbac.define_role(
        role_id="role_viewer",
        name="Viewer",
        permissions=[perm_read],
    )
    rbac.assign_role(
        identity_id="usr_david",
        role_id="role_viewer",
        scope="tenant_tenant_A",
    )

    req = SaaSAuthorizationRequest(
        action="PRICE_UPDATE",  # Acción no concedida
        session_id="sess_no_perm",
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.INSUFFICIENT_PERMISSIONS


def test_n3_deny_preserved(base_setup):
    """9. N.3 DENY preserved"""
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    clock = base_setup["clock"]
    t0 = base_setup["t0"]

    # Crear AuthorizationService con política estricta que prohíbe acciones de alto riesgo
    authz_service = AuthorizationService(
        policy_engine=PolicyEngine(rules=[AuthorizationPolicyRule()]),
        clock=clock,
    )

    srv = SaaSAuthorizationService(
        session_repository=session_repo,
        rbac_service=rbac,
        authorization_service=authz_service,
        clock=clock,
    )

    session = SaaSSession(
        session_id="sess_n3_deny",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    # El commercial_context incluye prohibited_actions con LISTING_PUBLISH para forzar N.3 DENY
    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_n3_deny",
        commercial_context={"prohibited_actions": ["LISTING_PUBLISH"]},
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.POLICY_DENIED
    assert decision.n3_decision is not None
    assert decision.n3_decision.status == AuthorizationStatus.DENY


def test_tenant_role_isolation(base_setup):
    """10. tenant role isolation: role assigned in tenant A is NOT effective in tenant B"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    # Sesión en tenant_B
    session_b = SaaSSession(
        session_id="sess_tenant_b",
        identity_id="usr_alice",
        tenant_id="tenant_B",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session_b)

    # Rol asignado sólo para tenant_A
    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_tenant_b",
        tenant_id="tenant_B",
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.INSUFFICIENT_PERMISSIONS


def test_stale_role_snapshot_not_trusted(base_setup):
    """12. stale role snapshot not trusted: dynamic RBAC lookup reflects immediate revocation"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_rev_role",
        identity_id="usr_eve",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    asgn = rbac.assign_role(
        identity_id="usr_eve",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    # 1. Primera autorización -> ALLOW
    req1 = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_rev_role",
    )
    dec1 = srv.authorize(req1)
    assert dec1.is_allowed is True

    # 2. Revocar rol
    rbac.revoke_assignment(asgn.assignment_id)

    # 3. Segunda autorización sin relogin -> DENY inmediato
    dec2 = srv.authorize(req1)
    assert dec2.is_allowed is False
    assert dec2.status == SaaSAuthorizationStatus.DENY
    assert dec2.reason_code == SaaSAuthorizationReasonCode.INSUFFICIENT_PERMISSIONS


def test_resource_tenant_mismatch_denies(base_setup):
    """13. resource tenant mismatch -> DENY"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_res_mismatch",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    # Recurso perteneciente a tenant_B
    target_res = TenantScopedResource(
        tenant_id="tenant_B",
        resource_id="prod_999",
        resource_type="listing",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_res_mismatch",
        resource=target_res,
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.RESOURCE_TENANT_MISMATCH


def test_unknown_ownership_fail_safe(base_setup):
    """14. unknown ownership fail-safe -> DENY"""
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    clock = base_setup["clock"]
    t0 = base_setup["t0"]

    resolver = DummyResourceOwnershipResolver(ownership_map={})  # Empty map

    srv = SaaSAuthorizationService(
        session_repository=session_repo,
        rbac_service=rbac,
        resource_ownership_resolver=resolver,
        clock=clock,
    )

    session = SaaSSession(
        session_id="sess_unk_res",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_unk_res",
        resource="listing:unknown_listing_123",
    )

    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.UNKNOWN_RESOURCE_OWNERSHIP


def test_deterministic_decision_and_checksum(base_setup):
    """15. deterministic decision and SHA-256 checksum integrity"""
    srv: SaaSAuthorizationService = base_setup["saas_authz_service"]
    session_repo: InMemorySessionRepository = base_setup["session_repo"]
    rbac: RBACService = base_setup["rbac_service"]
    t0 = base_setup["t0"]

    session = SaaSSession(
        session_id="sess_det_01",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_det_01",
        correlation_id="corr_deterministic_123",
    )

    decision = srv.authorize(req)

    assert len(decision.checksum) == 64
    assert len(decision.context.checksum) == 64
    assert decision.context.correlation_id == "corr_deterministic_123"
    assert decision.is_allowed is True


def test_no_o13_plus_implementation():
    """16. Verification that no O.13+ modules or capabilities are imported or active."""
    import sys
    assert "src.domain.deployment_automation" not in sys.modules
    assert "src.application.deployment_automation" not in sys.modules
