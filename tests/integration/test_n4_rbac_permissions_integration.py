"""
Tests de Integración y E2E para N.4 — RBAC / Permissions (Transversal N Security, Governance y Safety).

Escenarios exigidos por la especificación N.4:
A. Identity with VIEW role -> read allowed by N.3 -> mutation denied.
B. Identity with operator role -> permitted action ALLOW.
C. Same role scoped to account A -> account A allowed -> account B denied.
D. No role -> default deny.
E. Expired role assignment -> denied.
F. Role assignment persisted -> restart preserves permissions.
G. Tampered role/assignment -> corruption detected -> no permission.
H. Audit / Trace safe (K.1/K.2, no secrets, ROLE_ASSIGNED, RBAC_EVALUATED).
I. N.1 -> N.2 -> N.4 -> N.3 pipeline (Full E2E with AuthorizationGuardedActionExecutor).
"""

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from src.domain.identity.models import IdentityType, IdentityReference
from src.domain.oauth.models import OAuthConnection
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.authorization.models import (
    AuthorizationStatus,
    AuthorizationReasonCode,
    ResourceReference,
    AuthorizationRequest,
    AuthorizationDecision,
)
from src.domain.rbac.models import (
    Permission,
    PermissionStatus,
    Role,
    RoleStatus,
    RoleAssignment,
    PermissionSet,
    RbacEvaluationResult,
)
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import (
    AuthorizationPolicyRule,
)
from src.domain.mission.models import LoopDecision, LoopAction, LoopState, MissionType, MissionStatus
from src.domain.mission.ports import ActionExecutor
from src.domain.audit.models import AuditRecordType

from src.infrastructure.persistence.data.json.identity_repository import (
    JsonIdentityRepository,
)
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
    CorruptedRoleRecordError,
    CorruptedRoleAssignmentRecordError,
)
from src.infrastructure.persistence.data.json.audit_repository import (
    JsonAuditRepository,
)
from src.infrastructure.persistence.data.json.agent_trace_repository import (
    JsonAgentTraceRepository,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock

from src.application.identity.identity_service import IdentityService
from src.application.authentication.authentication_service import AuthenticationService
from src.application.agent_trace.agent_trace_service import AgentTraceService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.rbac.rbac_service import RBACService
from src.application.authorization.authorization_guarded_action_executor import (
    AuthorizationGuardedActionExecutor,
)


TEST_SECRET_TOKEN = "APP_USR_SECRET_TOKEN_12345"
TEST_SECRET_REFRESH = "APP_USR_SECRET_REFRESH_67890"


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "n4_rbac_db"


@pytest.fixture
def identity_repo(data_dir: Path) -> JsonIdentityRepository:
    return JsonIdentityRepository(data_dir / "identity")


@pytest.fixture
def identity_service(identity_repo: JsonIdentityRepository) -> IdentityService:
    return IdentityService(identity_repo)


@pytest.fixture
def audit_repo(data_dir: Path) -> JsonAuditRepository:
    return JsonAuditRepository(data_dir / "audit")


@pytest.fixture
def trace_repo(data_dir: Path) -> JsonAgentTraceRepository:
    return JsonAgentTraceRepository(data_dir / "trace")


@pytest.fixture
def trace_service(trace_repo: JsonAgentTraceRepository) -> AgentTraceService:
    return AgentTraceService(trace_repo)


@pytest.fixture
def virtual_clock() -> VirtualClock:
    start_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def auth_service(
    identity_service: IdentityService,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> AuthenticationService:
    return AuthenticationService(
        identity_service=identity_service,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
        trusted_internal_tokens={"internal-token-n4": "autonomous_loop"},
    )


@pytest.fixture
def role_repo(data_dir: Path) -> JsonRoleRepository:
    return JsonRoleRepository(data_dir / "roles")


@pytest.fixture
def assignment_repo(data_dir: Path) -> JsonRoleAssignmentRepository:
    return JsonRoleAssignmentRepository(data_dir / "assignments")


@pytest.fixture
def rbac_service(
    role_repo: JsonRoleRepository,
    assignment_repo: JsonRoleAssignmentRepository,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> RBACService:
    return RBACService(
        role_repository=role_repo,
        assignment_repository=assignment_repo,
        audit_repository=audit_repo,
        trace_service=trace_service,
        clock=virtual_clock,
    )


@pytest.fixture
def policy_engine() -> PolicyEngine:
    return PolicyEngine(rules=[AuthorizationPolicyRule()])


@pytest.fixture
def authz_service(
    policy_engine: PolicyEngine,
    audit_repo: JsonAuditRepository,
    trace_service: AgentTraceService,
    virtual_clock: VirtualClock,
) -> AuthorizationService:
    return AuthorizationService(
        policy_engine=policy_engine,
        clock=virtual_clock,
        audit_repository=audit_repo,
        agent_trace_service=trace_service,
        default_policy_version="1.0.0",
    )


# =============================================================================
# Escenario A: Identity with VIEW role -> read allowed by N.3 -> mutation denied
# =============================================================================

def test_scenario_a_viewer_read_allowed_mutation_denied(
    rbac_service: RBACService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    # 1. Definir permiso y rol de sólo lectura
    perm_read = rbac_service.create_permission("p_read", "listing.read")
    role_viewer = rbac_service.define_role("role_viewer", name="Viewer Role", permissions=[perm_read])

    id_ref = IdentityReference(
        identity_id="usr_analyst_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_analyst_01",
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    principal_ctx = PrincipalContext(principal=id_ref, auth_result=auth_res)

    rbac_service.assign_role(identity_id=id_ref.identity_id, role_id=role_viewer.role_id)

    # 2. RBAC resuelve permisos efectivos
    rbac_res = rbac_service.resolve_effective_permissions(principal_ctx)
    assert "LISTING_READ" in rbac_res.actions
    assert "LISTING_PUBLISH" not in rbac_res.actions

    # 3. N.3 evalúa acción READ alimentada por RBAC -> ALLOW
    req_read = AuthorizationRequest(
        principal_context=principal_ctx,
        action="LISTING_READ",
        resource=ResourceReference(resource_type="listing", resource_id="item_001"),
        correlation_id="corr-scen-a-read",
    )
    dec_read = authz_service.authorize(req_read, allowed_actions_override=list(rbac_res.actions))
    assert dec_read.status == AuthorizationStatus.ALLOW

    # 4. N.3 evalúa acción MUTATION (PUBLISH_LISTING) -> DENY por falta de permiso en RBAC
    req_mutate = AuthorizationRequest(
        principal_context=principal_ctx,
        action="PUBLISH_LISTING",
        resource=ResourceReference(resource_type="listing", resource_id="item_001"),
        correlation_id="corr-scen-a-mut",
    )
    dec_mutate = authz_service.authorize(req_mutate, allowed_actions_override=list(rbac_res.actions))
    assert dec_mutate.status == AuthorizationStatus.DENY


# =============================================================================
# Escenario B: Identity with operator role -> permitted action ALLOW
# =============================================================================

def test_scenario_b_operator_permitted_action_allow(
    rbac_service: RBACService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    perm_pub = rbac_service.create_permission("p_pub", "listing.publish")
    role_op = rbac_service.define_role("role_operator", name="Operator", permissions=[perm_pub])

    id_ref = IdentityReference(
        identity_id="usr_operator_02",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_operator_02",
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    principal_ctx = PrincipalContext(principal=id_ref, auth_result=auth_res)

    rbac_service.assign_role(identity_id=id_ref.identity_id, role_id=role_op.role_id)

    rbac_res = rbac_service.resolve_effective_permissions(principal_ctx)
    assert "LISTING_PUBLISH" in rbac_res.actions

    req = AuthorizationRequest(
        principal_context=principal_ctx,
        action="LISTING_PUBLISH",
        resource=ResourceReference(resource_type="listing", resource_id="item_002"),
        correlation_id="corr-scen-b",
    )
    decision = authz_service.authorize(req, allowed_actions_override=list(rbac_res.actions))
    assert decision.status == AuthorizationStatus.ALLOW


# =============================================================================
# Escenario C: Same role scoped to account A -> account A allowed -> account B denied
# =============================================================================

def test_scenario_c_scoped_role_account_isolation(
    rbac_service: RBACService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    perm_price = rbac_service.create_permission("p_price", "price.update")
    role_pricer = rbac_service.define_role("role_pricer_scoped", name="Scoped Pricer", permissions=[perm_price])

    id_ref = IdentityReference(
        identity_id="usr_pricer_03",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:account_a",
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    principal_ctx = PrincipalContext(principal=id_ref, auth_result=auth_res)

    # Asignar rol acotado exclusivamente a account_a
    rbac_service.assign_role(
        identity_id=id_ref.identity_id,
        role_id=role_pricer.role_id,
        scope="account_a",
    )

    # 1. En scope account_a -> Permisos presentes -> ALLOW
    res_a = rbac_service.resolve_effective_permissions(principal_ctx, scope="account_a")
    assert "PRICE_UPDATE" in res_a.actions

    req_a = AuthorizationRequest(
        principal_context=principal_ctx,
        action="PRICE_UPDATE",
        resource=ResourceReference(resource_type="price", resource_id="item_100", account_id="account_a"),
        correlation_id="corr-scen-c-a",
    )
    dec_a = authz_service.authorize(req_a, allowed_actions_override=list(res_a.actions))
    assert dec_a.status == AuthorizationStatus.ALLOW

    # 2. En scope account_b -> 0 permisos -> DENY
    res_b = rbac_service.resolve_effective_permissions(principal_ctx, scope="account_b")
    assert len(res_b.actions) == 0

    req_b = AuthorizationRequest(
        principal_context=principal_ctx,
        action="PRICE_UPDATE",
        resource=ResourceReference(resource_type="price", resource_id="item_100", account_id="account_b"),
        correlation_id="corr-scen-c-b",
    )
    dec_b = authz_service.authorize(req_b, allowed_actions_override=list(res_b.actions))
    assert dec_b.status == AuthorizationStatus.DENY


# =============================================================================
# Escenario D: No role -> Default Deny
# =============================================================================

def test_scenario_d_no_role_default_deny(
    rbac_service: RBACService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    id_ref = IdentityReference(
        identity_id="usr_norole_04",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_norole_04",
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    principal_ctx = PrincipalContext(principal=id_ref, auth_result=auth_res)

    res = rbac_service.resolve_effective_permissions(principal_ctx)
    assert len(res.actions) == 0
    assert res.effective_permissions.is_empty is True

    req = AuthorizationRequest(
        principal_context=principal_ctx,
        action="LISTING_READ",
        resource=ResourceReference(resource_type="listing", resource_id="item_004"),
        correlation_id="corr-scen-d",
    )
    decision = authz_service.authorize(req, allowed_actions_override=list(res.actions))
    assert decision.status == AuthorizationStatus.DENY


# =============================================================================
# Escenario E: Expired role assignment -> Denied
# =============================================================================

def test_scenario_e_expired_role_assignment_denied(
    rbac_service: RBACService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    perm = rbac_service.create_permission("p_inv", "inventory.update")
    role = rbac_service.define_role("role_inv_temp", name="Temp Inv", permissions=[perm])

    id_ref = IdentityReference(
        identity_id="usr_temp_05",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_temp_05",
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    principal_ctx = PrincipalContext(principal=id_ref, auth_result=auth_res)

    now = virtual_clock.now()
    # Asignación que expira en 30 minutos
    rbac_service.assign_role(
        identity_id=id_ref.identity_id,
        role_id=role.role_id,
        expires_at=now + timedelta(minutes=30),
    )

    # 1. Antes de expirar: Válido
    res_valid = rbac_service.resolve_effective_permissions(principal_ctx)
    assert "INVENTORY_UPDATE" in res_valid.actions

    # 2. Avanzar tiempo 31 minutos (1860 segundos) con K.7
    virtual_clock.advance(1860)

    # 3. Después de expirar: 0 acciones resueltas -> DENY
    res_expired = rbac_service.resolve_effective_permissions(principal_ctx)
    assert len(res_expired.actions) == 0

    req = AuthorizationRequest(
        principal_context=principal_ctx,
        action="INVENTORY_UPDATE",
        resource=ResourceReference(resource_type="inventory", resource_id="item_005"),
        correlation_id="corr-scen-e",
    )
    decision = authz_service.authorize(req, allowed_actions_override=list(res_expired.actions))
    assert decision.status == AuthorizationStatus.DENY


# =============================================================================
# Escenario F: Role assignment persisted -> Restart preserves permissions
# =============================================================================

def test_scenario_f_role_assignment_persisted_restart_preserves_permissions(
    data_dir: Path,
    virtual_clock: VirtualClock,
):
    # Instancia 1 de repositorios y servicio
    role_repo_1 = JsonRoleRepository(data_dir / "roles")
    asgn_repo_1 = JsonRoleAssignmentRepository(data_dir / "assignments")
    svc_1 = RBACService(role_repository=role_repo_1, assignment_repository=asgn_repo_1, clock=virtual_clock)

    perm = svc_1.create_permission("p_persisted", "order.manage")
    role = svc_1.define_role("role_persisted", name="Persisted Role", permissions=[perm])
    svc_1.assign_role(identity_id="usr_persistent_06", role_id=role.role_id)

    # Simular reinicio creando nuevas instancias apuntando al mismo data_dir
    role_repo_2 = JsonRoleRepository(data_dir / "roles")
    asgn_repo_2 = JsonRoleAssignmentRepository(data_dir / "assignments")
    svc_2 = RBACService(role_repository=role_repo_2, assignment_repository=asgn_repo_2, clock=virtual_clock)

    id_ref = IdentityReference(
        identity_id="usr_persistent_06",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_persistent_06",
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    principal_ctx = PrincipalContext(principal=id_ref, auth_result=auth_res)

    res = svc_2.resolve_effective_permissions(principal_ctx)
    assert "ORDER_MANAGE" in res.actions
    assert "role_persisted" in res.roles


# =============================================================================
# Escenario G: Tampered role/assignment -> Corruption detected -> No permission
# =============================================================================

def test_scenario_g_tampered_record_corruption_detected(
    data_dir: Path,
    virtual_clock: VirtualClock,
):
    role_repo = JsonRoleRepository(data_dir / "roles")
    asgn_repo = JsonRoleAssignmentRepository(data_dir / "assignments")
    svc = RBACService(role_repository=role_repo, assignment_repository=asgn_repo, clock=virtual_clock)

    perm = svc.create_permission("p_secure", "listing.publish")
    role = svc.define_role("role_secure", name="Secure Role", permissions=[perm])
    asgn = svc.assign_role(identity_id="usr_victim_07", role_id=role.role_id)

    # Manipular maliciosamente el archivo de asignación en disco (tamper checksum / role)
    asgn_file = data_dir / "assignments" / "assignments" / f"{asgn.assignment_id}.json"
    with open(asgn_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Inyectar payload alterado sin actualizar checksum
    data["role_id"] = "role_super_admin_fake"
    with open(asgn_file, "w", encoding="utf-8") as f:
        json.dump(data, f)

    # Al leer la asignación alterada directamente debe lanzar CorruptedRoleAssignmentRecordError
    with pytest.raises(CorruptedRoleAssignmentRecordError):
        asgn_repo.get_assignment(asgn.assignment_id)

    # Al resolver permisos efectivos, el repositorio resiliente ignora el registro corrupto (DEFAULT DENY)
    id_ref = IdentityReference(
        identity_id="usr_victim_07",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_victim_07",
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    principal_ctx = PrincipalContext(principal=id_ref, auth_result=auth_res)

    res = svc.resolve_effective_permissions(principal_ctx)
    assert len(res.actions) == 0


# =============================================================================
# Escenario H: Audit/Trace safe (K.1/K.2)
# =============================================================================

def test_scenario_h_audit_and_trace_safety(
    rbac_service: RBACService,
    audit_repo: JsonAuditRepository,
    trace_repo: JsonAgentTraceRepository,
    virtual_clock: VirtualClock,
):
    perm = rbac_service.create_permission("p_audit", "price.update")
    role = rbac_service.define_role("role_audit", name="Audit Role", permissions=[perm])

    corr_id = "corr-audit-test-999"
    asgn = rbac_service.assign_role(
        identity_id="usr_audited_08",
        role_id=role.role_id,
        metadata={"access_token": TEST_SECRET_TOKEN, "safe_note": "audit ok"},
        correlation_id=corr_id,
    )

    # 1. Verificar registros de auditoría K.1
    audit_records = audit_repo.list_records(correlation_id=corr_id)
    assert len(audit_records) >= 1
    asgn_record = audit_records[0]
    assert asgn_record.record_type == AuditRecordType.ROLE_ASSIGNED
    assert asgn_record.metadata["access_token"] == "[REDACTED]"
    assert asgn_record.metadata["safe_note"] == "audit ok"

    # 2. Verificar resolución con traza K.2
    id_ref = IdentityReference(
        identity_id="usr_audited_08",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_audited_08",
    )
    auth_res = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    principal_ctx = PrincipalContext(principal=id_ref, auth_result=auth_res)

    eval_corr = "corr-rbac-eval-101"
    rbac_service.resolve_effective_permissions(principal_ctx, correlation_id=eval_corr)

    trace_records = trace_repo.list_records(execution_id=eval_corr)
    assert len(trace_records) >= 1
    trace_step = trace_records[0]
    assert trace_step.component_name == "RBACService"
    assert trace_step.operation == "RBAC_PERMISSIONS_RESOLUTION"


# =============================================================================
# Escenario I: N.1 -> N.2 -> N.4 -> N.3 Full Pipeline (E2E)
# =============================================================================

def test_scenario_i_full_e2e_pipeline_with_guarded_executor(
    auth_service: AuthenticationService,
    rbac_service: RBACService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    # 1. N.1 & N.2: Autenticar conexión OAuth de MercadoLibre
    conn = OAuthConnection(
        provider="mercadolibre",
        user_id="MLA_OPERATOR_777",
        access_token=TEST_SECRET_TOKEN,
        refresh_token=TEST_SECRET_REFRESH,
        expires_at=virtual_clock.now() + timedelta(hours=4),
    )
    auth_res = auth_service.authenticate_oauth_connection(conn, correlation_id="corr-e2e-auth")
    assert auth_res.is_authenticated is True
    principal_ctx = PrincipalContext(principal=auth_res.principal, auth_result=auth_res)

    # 2. N.4: Configurar permisos y roles
    perm_pub = rbac_service.create_permission("p_e2e_pub", "listing.publish")
    role_e2e = rbac_service.define_role("role_e2e_publisher", name="Publisher", permissions=[perm_pub])
    rbac_service.assign_role(identity_id=principal_ctx.identity_id, role_id=role_e2e.role_id)

    # 3. Guardián de ejecución N.3 integrado con RBAC N.4
    mock_executor = MagicMock(spec=ActionExecutor)
    mock_executor.execute.return_value = {"status": "SUCCESS", "item_id": "MLA_ITEM_777"}

    guarded_executor = AuthorizationGuardedActionExecutor(
        authorization_service=authz_service,
        delegate_executor=mock_executor,
        rbac_service=rbac_service,
        principal_context=principal_ctx,
    )

    # 4. Caso A: Acción permitida por rol ("LISTING_PUBLISH" / "listing.publish") -> ALLOW -> mock ejecutado
    decision_allowed = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Publish item",
        target="listing:MLA_ITEM_777",
        parameters={"action_type": "LISTING_PUBLISH", "title": "Smart Hub 2026", "price": 99.99},
    )
    state_a = LoopState(mission_id="corr-scen-i-a", iteration=1, goal="Publish")
    res_exec = guarded_executor.execute(decision_allowed, state_a)
    assert res_exec["status"] == "SUCCESS"
    assert res_exec["authorization_status"] == "ALLOW"
    assert mock_executor.execute.call_count == 1

    # 5. Caso B: Acción no permitida por rol ("ORDER_CANCEL" / "order.cancel") -> DENY -> mock NO llamado
    decision_denied = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Cancel order",
        target="order:ORDER_999",
        parameters={"action_type": "ORDER_CANCEL"},
    )
    state_b = LoopState(mission_id="corr-scen-i-b", iteration=2, goal="Cancel order")
    res_denied = guarded_executor.execute(decision_denied, state_b)
    assert res_denied["is_allowed"] is False
    assert res_denied["authorization_status"] == "DENY"

    # El contador del mock se mantiene en 1 (no fue llamado para la acción denegada)
    assert mock_executor.execute.call_count == 1
