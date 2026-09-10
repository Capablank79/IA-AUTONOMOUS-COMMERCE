"""
Tests de Integración y E2E para O.4 — SaaS Authorization (Multi-Tenant RBAC & Tenant Permission Scoping) (Hito O — SaaS / Platformization).

Escenarios de Integración y E2E especificados:
A. Tenant A session + Organization A membership + valid permission -> ALLOW.
B. Same identity Session B -> no reuse of A permission (strict scope isolation).
C. Session A + resource B -> zero physical downstream calls (blocked by CrossTenantGuard/Resource check).
D. Membership removed after login -> subsequent request denied.
E. Role revoked after login -> subsequent request denied immediately (dynamic RBAC, 0 relogin).
F. Wrong organization -> denied.
G. Marketplace account tenant mismatch -> denied.
H. N.3 PolicyEngine deny -> denied (never override N.3).
I. Restart -> same scoped authorization result preserved across restarts.
J. Audit/Trace safe (SHA-256 integrity, no secrets, correct audit events emitted).

E2E O.4 Pipeline:
N.2 Authentication -> O.3 Session -> O.1 Tenant -> O.2 Organization/Membership -> N.4 RBAC -> O.4 SaaS Authorization -> N.3 Authorization -> SaaSGuardedActionExecutor -> guarded operation mock.
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import tempfile
import shutil
from unittest.mock import MagicMock
import pytest

from src.domain.identity.models import IdentityReference, IdentityType, IdentityStatus
from src.application.identity.identity_service import IdentityService
from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository

from src.domain.session.models import SaaSSession, SessionStatus
from src.application.session.saas_session_service import SaaSSessionService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository

from src.domain.tenant.models import TenantContext, TenantScopedResource
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

from src.domain.rbac.models import Permission, Role, RoleAssignment
from src.application.rbac.rbac_service import RBACService
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)

from src.domain.authorization.models import AuthorizationRequest, AuthorizationStatus, ResourceReference
from src.application.authorization.authorization_service import AuthorizationService
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule

from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
    SaaSAuthorizationDeniedError,
)
from src.domain.saas_authorization.ports import ResourceOwnershipResolverPort
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.saas_authorization.saas_guarded_action_executor import SaaSGuardedActionExecutor

from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.domain.audit.models import AuditRecordType
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.audit.audit_trail_service import AuditTrailService
from src.domain.agent_trace.models import AgentTraceRecord
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
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


class DummyMarketplaceResourceResolver(ResourceOwnershipResolverPort):
    def __init__(self, ownership_map: dict):
        self._map = ownership_map

    def resolve_resource_ownership(self, resource):
        res_key = resource if isinstance(resource, str) else getattr(resource, "resource_id", str(resource))
        return self._map.get(res_key)


@pytest.fixture
def temp_dir():
    d = tempfile.mkdtemp(prefix="o4_integ_test_")
    yield Path(d)
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def clock():
    return DeterministicClock(datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def setup_environment(temp_dir, clock):
    audit_repo = JsonAuditRepository(temp_dir / "audit")
    audit_service = AuditTrailService(audit_repo)
    trace_repo = JsonAgentTraceRepository(temp_dir / "trace")
    trace_service = AgentTraceService(trace_repo)

    identity_repo = JsonIdentityRepository(temp_dir / "identity")
    identity_service = IdentityService(identity_repo)

    tenant_service = TenantContextService(
        registered_tenants={"tenant_A", "tenant_B"},
        identity_to_tenant={"usr_alice": "tenant_A", "usr_bob": "tenant_B"},
    )

    org_repo = JsonOrganizationRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    org_service = OrganizationService(organization_repo=org_repo, audit_repository=audit_repo)
    membership_service = OrganizationMembershipService(
        membership_repo=mem_repo,
        organization_repo=org_repo,
        tenant_mapping=tenant_service,
        identity_repo=identity_repo,
        audit_repository=audit_repo,
    )

    session_repo = JsonSaaSSessionRepository(temp_dir)
    session_service = SaaSSessionService(
        session_repository=session_repo,
        tenant_resolver=tenant_service,
        tenant_mapping=tenant_service,
        organization_repo=org_repo,
        membership_repo=mem_repo,
        identity_repo=identity_repo,
        clock=clock,
        audit_repository=audit_repo,
    )

    role_repo = JsonRoleRepository(temp_dir)
    asgn_repo = JsonRoleAssignmentRepository(temp_dir)
    rbac_service = RBACService(
        role_repository=role_repo,
        assignment_repository=asgn_repo,
        clock=clock,
    )

    policy_engine = PolicyEngine(rules=[AuthorizationPolicyRule()])
    authz_service = AuthorizationService(
        policy_engine=policy_engine,
        clock=clock,
    )

    ownership_map = {
        "listing_A1": ("tenant_A", "org_alpha"),
        "listing_B1": ("tenant_B", "org_beta"),
        "mkt_acc_A": ("tenant_A", None),
        "mkt_acc_B": ("tenant_B", None),
    }
    ownership_resolver = DummyMarketplaceResourceResolver(ownership_map)

    saas_authz_service = SaaSAuthorizationService(
        session_repository=session_repo,
        membership_repository=mem_repo,
        rbac_service=rbac_service,
        authorization_service=authz_service,
        resource_ownership_resolver=ownership_resolver,
        audit_repository=audit_repo,
        trace_service=trace_service,
        clock=clock,
    )

    # Definir permisos y roles estándar en N.4
    perm_pub = rbac_service.create_permission(
        permission_id="perm_pub",
        action="LISTING_PUBLISH",
        description="Publish listing",
    )
    perm_price = rbac_service.create_permission(
        permission_id="perm_price",
        action="PRICE_UPDATE",
        description="Update pricing",
    )
    rbac_service.define_role(
        role_id="role_publisher",
        name="Publisher Role",
        permissions=[perm_pub, perm_price],
    )

    return {
        "temp_dir": temp_dir,
        "clock": clock,
        "audit_repo": audit_repo,
        "trace_repo": trace_repo,
        "identity_service": identity_service,
        "tenant_service": tenant_service,
        "org_service": org_service,
        "mem_repo": mem_repo,
        "membership_service": membership_service,
        "session_repo": session_repo,
        "session_service": session_service,
        "role_repo": role_repo,
        "asgn_repo": asgn_repo,
        "rbac_service": rbac_service,
        "authz_service": authz_service,
        "ownership_resolver": ownership_resolver,
        "saas_authz_service": saas_authz_service,
    }


# ==============================================================================
# Escenario A: Tenant A session + Organization A membership + valid permission -> ALLOW.
# ==============================================================================
def test_scenario_a_tenant_a_valid_flow(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    mem_repo: JsonMembershipRepository = env["mem_repo"]
    rbac: RBACService = env["rbac_service"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    # 1. Sesión activa en tenant_A con organización org_alpha
    session = SaaSSession(
        session_id="sess_a_valid",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    # 2. Membresía ACTIVE en org_alpha
    membership = UserMembership(
        membership_id="mem_a1",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        identity_id="usr_alice",
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
        joined_at=t0,
    )
    mem_repo.save(TenantContext(tenant_id="tenant_A", identity_id="usr_alice"), membership)

    # 3. Rol asignado en el scope de tenant_A
    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    # 4. Solicitud de autorización SaaS
    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_a_valid",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        resource="listing_A1",
    )
    decision = srv.authorize(req)

    assert decision.is_allowed is True
    assert decision.status == SaaSAuthorizationStatus.ALLOW
    assert decision.reason_code == SaaSAuthorizationReasonCode.AUTHORIZED
    assert decision.context.tenant_id == "tenant_A"
    assert decision.context.organization_id == "org_alpha"
    assert "LISTING_PUBLISH" in decision.context.resolved_permissions


# ==============================================================================
# Escenario B: Same identity Session B -> no reuse of A permission (strict scope isolation).
# ==============================================================================
def test_scenario_b_same_identity_session_b_no_leakage(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    rbac: RBACService = env["rbac_service"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    # Usuario alice tiene rol en tenant_A
    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    # Sesión de alice en tenant_B
    session_b = SaaSSession(
        session_id="sess_b_alice",
        identity_id="usr_alice",
        tenant_id="tenant_B",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session_b)

    # Intento de usar permiso en tenant_B
    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_b_alice",
        tenant_id="tenant_B",
    )
    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.INSUFFICIENT_PERMISSIONS


# ==============================================================================
# Escenario C: Session A + resource B -> zero physical calls.
# ==============================================================================
def test_scenario_c_session_a_resource_b_blocked(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    rbac: RBACService = env["rbac_service"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    session = SaaSSession(
        session_id="sess_cross_res",
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

    # resource listing_B1 pertenece a tenant_B
    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_cross_res",
        resource="listing_B1",
    )
    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.RESOURCE_TENANT_MISMATCH


# ==============================================================================
# Escenario D: Membership removed after login -> subsequent request denied.
# ==============================================================================
def test_scenario_d_membership_removed_after_login(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    mem_repo: JsonMembershipRepository = env["mem_repo"]
    rbac: RBACService = env["rbac_service"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    session = SaaSSession(
        session_id="sess_mem_dyn",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    membership = UserMembership(
        membership_id="mem_dyn1",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        identity_id="usr_alice",
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
        joined_at=t0,
    )
    t_ctx = TenantContext(tenant_id="tenant_A", identity_id="usr_alice")
    mem_repo.save(t_ctx, membership)

    rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    # 1. Petición inicial -> ALLOW
    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_mem_dyn",
        organization_id="org_alpha",
    )
    dec1 = srv.authorize(req)
    assert dec1.is_allowed is True

    # 2. Remover membresía (status = REMOVED)
    updated_mem = UserMembership(
        membership_id="mem_dyn1",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        identity_id="usr_alice",
        role=MembershipRole.MEMBER,
        status=MembershipStatus.REMOVED,
        joined_at=t0,
        removed_at=clock.now(),
    )
    mem_repo.save(t_ctx, updated_mem)

    # 3. Siguiente petición con la misma sesión activa -> DENY
    dec2 = srv.authorize(req)
    assert dec2.is_allowed is False
    assert dec2.status == SaaSAuthorizationStatus.DENY
    assert dec2.reason_code == SaaSAuthorizationReasonCode.MEMBERSHIP_NOT_ACTIVE


# ==============================================================================
# Escenario E: Role revoked after login -> subsequent request denied immediately.
# ==============================================================================
def test_scenario_e_role_revoked_after_login(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    rbac: RBACService = env["rbac_service"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    session = SaaSSession(
        session_id="sess_role_dyn",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    asgn = rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_role_dyn",
    )
    dec1 = srv.authorize(req)
    assert dec1.is_allowed is True

    # Revocar asignación de rol
    rbac.revoke_assignment(asgn.assignment_id)

    # Petición subsecuente -> DENY sin necesidad de relogin
    dec2 = srv.authorize(req)
    assert dec2.is_allowed is False
    assert dec2.status == SaaSAuthorizationStatus.DENY
    assert dec2.reason_code == SaaSAuthorizationReasonCode.INSUFFICIENT_PERMISSIONS


# ==============================================================================
# Escenario F: Wrong organization -> denied.
# ==============================================================================
def test_scenario_f_wrong_organization(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    session = SaaSSession(
        session_id="sess_org_mismatch",
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
        session_id="sess_org_mismatch",
        organization_id="org_different",
    )
    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.ORGANIZATION_MISMATCH


# ==============================================================================
# Escenario G: Marketplace account tenant mismatch -> denied.
# ==============================================================================
def test_scenario_g_marketplace_account_mismatch(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    rbac: RBACService = env["rbac_service"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    session = SaaSSession(
        session_id="sess_mkt_mismatch",
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

    # marketplace account mkt_acc_B pertenece a tenant_B
    req = SaaSAuthorizationRequest(
        action="PRICE_UPDATE",
        session_id="sess_mkt_mismatch",
        marketplace_account_id="mkt_acc_B",
    )
    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.MARKETPLACE_ACCOUNT_MISMATCH


# ==============================================================================
# Escenario H: N.3 PolicyEngine deny -> denied (never override N.3).
# ==============================================================================
def test_scenario_h_n3_policy_engine_deny(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    rbac: RBACService = env["rbac_service"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    session = SaaSSession(
        session_id="sess_n3_pol_deny",
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

    # Inyectar regla comercial que prohíba LISTING_PUBLISH
    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_n3_pol_deny",
        commercial_context={"prohibited_actions": ["LISTING_PUBLISH"]},
    )
    decision = srv.authorize(req)

    assert decision.is_allowed is False
    assert decision.status == SaaSAuthorizationStatus.DENY
    assert decision.reason_code == SaaSAuthorizationReasonCode.POLICY_DENIED
    assert decision.n3_decision is not None
    assert decision.n3_decision.status == AuthorizationStatus.DENY


# ==============================================================================
# Escenario I: Restart -> same scoped authorization result preserved.
# ==============================================================================
def test_scenario_i_restart_persistence(temp_dir, clock):
    t0 = clock.now()
    session_repo = JsonSaaSSessionRepository(temp_dir)
    mem_repo = JsonMembershipRepository(temp_dir)
    role_repo = JsonRoleRepository(temp_dir)
    asgn_repo = JsonRoleAssignmentRepository(temp_dir)
    audit_repo = JsonAuditRepository(temp_dir / "audit")

    rbac_service = RBACService(role_repository=role_repo, assignment_repository=asgn_repo, clock=clock)
    authz_service = AuthorizationService(policy_engine=PolicyEngine(rules=[AuthorizationPolicyRule()]), clock=clock)

    # Guardar sesión, rol y asignación
    session = SaaSSession(
        session_id="sess_restart_1",
        identity_id="usr_restart",
        tenant_id="tenant_A",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    perm = rbac_service.create_permission(
        permission_id="perm_pub_res",
        action="LISTING_PUBLISH",
        description="Publish",
    )
    role = rbac_service.define_role("role_pub_res", "Publisher", [perm])
    role_repo.save_role(role)
    rbac_service.assign_role("usr_restart", "role_pub_res", "tenant_tenant_A")

    # Simular RESTART instanciando nuevos repositorios apuntando al mismo almacenamiento JSON
    session_repo_new = JsonSaaSSessionRepository(temp_dir)
    mem_repo_new = JsonMembershipRepository(temp_dir)
    role_repo_new = JsonRoleRepository(temp_dir)
    asgn_repo_new = JsonRoleAssignmentRepository(temp_dir)
    rbac_service_new = RBACService(role_repository=role_repo_new, assignment_repository=asgn_repo_new, clock=clock)

    srv_new = SaaSAuthorizationService(
        session_repository=session_repo_new,
        membership_repository=mem_repo_new,
        rbac_service=rbac_service_new,
        authorization_service=authz_service,
        clock=clock,
    )

    req = SaaSAuthorizationRequest(
        action="LISTING_PUBLISH",
        session_id="sess_restart_1",
    )
    decision = srv_new.authorize(req)

    assert decision.is_allowed is True
    assert decision.status == SaaSAuthorizationStatus.ALLOW
    assert decision.context.identity_id == "usr_restart"
    assert decision.context.tenant_id == "tenant_A"


# ==============================================================================
# Escenario J: Audit / Trace Safe.
# ==============================================================================
def test_scenario_j_audit_and_trace_safety(setup_environment):
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    role_repo: JsonRoleRepository = env["role_repo"]
    rbac: RBACService = env["rbac_service"]
    audit_repo: JsonAuditRepository = env["audit_repo"]
    trace_repo: JsonAgentTraceRepository = env["trace_repo"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    session = SaaSSession(
        session_id="sess_audit_test",
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
        session_id="sess_audit_test",
        correlation_id="corr_audit_999",
    )
    decision = srv.authorize(req)
    assert decision.is_allowed is True

    # Verificar registro en Auditoría K.1
    audit_records = audit_repo.list_records(subject_id="tenant_A")
    authz_audits = [r for r in audit_records if r.record_type == AuditRecordType.AUTHORIZATION_EVALUATED]
    assert len(authz_audits) >= 1
    record = authz_audits[-1]
    assert record.actor.actor_id == "usr_alice"
    assert record.metadata["tenant_id"] == "tenant_A"
    assert record.metadata["status"] == "ALLOW"
    assert record.metadata["reason_code"] == "AUTHORIZED"
    assert "token" not in json.dumps(dict(record.metadata)).lower()
    assert "secret" not in json.dumps(dict(record.metadata)).lower()

    # Verificar trazas K.2
    traces = trace_repo.list_records(execution_id="corr_audit_999")
    assert len(traces) >= 1
    assert "SAAS_AUTHORIZATION" in traces[0].operation


# ==============================================================================
# Pipeline E2E O.4 con SaaSGuardedActionExecutor
# ==============================================================================
def test_e2e_o4_saas_guarded_execution_pipeline(setup_environment):
    """
    Demuestra el pipeline integral:
    N.2 Authentication -> O.3 Session -> O.1 Tenant -> O.2 Organization/Membership -> N.4 RBAC -> O.4 SaaS Authorization -> N.3 Authorization -> SaaSGuardedActionExecutor -> physical call.
    """
    env = setup_environment
    srv: SaaSAuthorizationService = env["saas_authz_service"]
    session_repo: JsonSaaSSessionRepository = env["session_repo"]
    mem_repo: JsonMembershipRepository = env["mem_repo"]
    rbac: RBACService = env["rbac_service"]
    clock: DeterministicClock = env["clock"]
    t0 = clock.now()

    # Mock delegate
    delegate_mock = MagicMock(spec=ActionExecutor)
    delegate_mock.execute.return_value = {"success": True, "published_id": "item_123"}

    guarded_executor = SaaSGuardedActionExecutor(
        delegate=delegate_mock,
        saas_authorization_service=srv,
    )

    # 1. Sesión y membresía válidas
    session = SaaSSession(
        session_id="sess_e2e_01",
        identity_id="usr_alice",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        created_at=t0,
        expires_at=t0 + timedelta(hours=2),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(session)

    membership = UserMembership(
        membership_id="mem_e2e_1",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        identity_id="usr_alice",
        role=MembershipRole.MEMBER,
        status=MembershipStatus.ACTIVE,
        joined_at=t0,
    )
    mem_repo.save(TenantContext(tenant_id="tenant_A", identity_id="usr_alice"), membership)

    asgn = rbac.assign_role(
        identity_id="usr_alice",
        role_id="role_publisher",
        scope="tenant_tenant_A",
    )

    state = LoopState(mission_id="m_001", iteration=1, goal="Test SaaS Execution")

    # A. Ejecución válida -> delegada exitosamente (1 call)
    decision_ok = LoopDecision(
        action=LoopAction.CONTINUE,
        parameters={
            "action": "LISTING_PUBLISH",
            "session_id": "sess_e2e_01",
            "tenant_id": "tenant_A",
            "organization_id": "org_alpha",
            "resource": "listing_A1",
        },
        reason="Publishing approved product",
    )
    result_ok = guarded_executor.execute(decision_ok, state)
    assert result_ok["success"] is True
    assert delegate_mock.execute.call_count == 1

    # B. Same identity, wrong tenant -> 0 additional calls (blocked)
    decision_wrong_tenant = LoopDecision(
        action=LoopAction.CONTINUE,
        parameters={
            "action": "LISTING_PUBLISH",
            "session_id": "sess_e2e_01",
            "tenant_id": "tenant_B",  # Mismatch con session tenant_A
        },
        reason="Wrong tenant access",
    )
    with pytest.raises(SaaSAuthorizationDeniedError):
        guarded_executor.execute(decision_wrong_tenant, state)
    assert delegate_mock.execute.call_count == 1  # No incremented

    # C. Removed membership -> 0 additional calls (blocked)
    mem_removed = UserMembership(
        membership_id="mem_e2e_1",
        tenant_id="tenant_A",
        organization_id="org_alpha",
        identity_id="usr_alice",
        role=MembershipRole.MEMBER,
        status=MembershipStatus.REMOVED,
        joined_at=t0,
    )
    mem_repo.save(TenantContext(tenant_id="tenant_A", identity_id="usr_alice"), mem_removed)

    with pytest.raises(SaaSAuthorizationDeniedError):
        guarded_executor.execute(decision_ok, state)
    assert delegate_mock.execute.call_count == 1  # No incremented

    # Restaurar membresía pero revocar rol
    mem_repo.save(TenantContext(tenant_id="tenant_A", identity_id="usr_alice"), membership)
    rbac.revoke_assignment(asgn.assignment_id)

    # D. Role revoked while session is ACTIVE -> 0 additional calls (blocked)
    with pytest.raises(SaaSAuthorizationDeniedError):
        guarded_executor.execute(decision_ok, state)
    assert delegate_mock.execute.call_count == 1  # No incremented
