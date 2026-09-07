from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import pytest

from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.authentication.models import (
    PrincipalContext,
    AuthenticationResult,
    AuthenticationMethod,
    AuthenticationStatus,
)
from src.domain.rbac.models import (
    Role,
    RoleAssignment,
    Permission,
)
from src.domain.emergency_stop.models import (
    EmergencyStopRecord,
    EmergencyStopScope,
    EmergencyStopState,
    EmergencyStopDecisionStatus,
    EmergencyStopReasonCode,
    EmergencyStopEvaluationContext,
)
from src.domain.mission.models import LoopDecision, LoopState, LoopAction
from src.domain.mission.ports import ActionExecutor
from src.domain.compliance.models import (
    ComplianceStatus,
    ComplianceFindingReasonCode,
)
from src.domain.audit.models import (
    AuditRecord,
    AuditRecordType,
    AuditActor,
    AuditActorType,
)
from src.infrastructure.persistence.data.json.emergency_stop_repository import JsonEmergencyStopRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.infrastructure.persistence.data.in_memory.compliance_policy_repository import (
    InMemoryCompliancePolicyRepository,
    create_default_commercial_compliance_policy,
)
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock
from src.application.emergency_stop.emergency_stop_service import EmergencyStopService
from src.application.rbac.rbac_service import RBACService
from src.application.authorization.authorization_service import AuthorizationService
from src.application.authorization.authorization_guarded_action_executor import AuthorizationGuardedActionExecutor
from src.application.compliance.compliance_assessment_service import (
    ComplianceAssessmentService,
    DefaultComplianceEvidenceCollector,
)
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule


class MockActionExecutor(ActionExecutor):
    def __init__(self):
        self.calls = []

    @property
    def external_calls_count(self) -> int:
        return len(self.calls)

    def execute(self, decision: LoopDecision, state: LoopState):
        self.calls.append({"decision": decision, "state": state})
        return {
            "status": "EXECUTED_SUCCESSFULLY",
            "action_executed": decision.action,
            "target": decision.target,
        }


@pytest.fixture
def virtual_clock() -> VirtualClock:
    start_time = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def temp_estop_repo(tmp_path: Path) -> JsonEmergencyStopRepository:
    return JsonEmergencyStopRepository(file_path=tmp_path / "estop" / "stops.json")


@pytest.fixture
def temp_audit_repo(tmp_path: Path) -> JsonAuditRepository:
    return JsonAuditRepository(tmp_path / "audit")


@pytest.fixture
def compliance_policy_repo() -> InMemoryCompliancePolicyRepository:
    repo = InMemoryCompliancePolicyRepository()
    policy = create_default_commercial_compliance_policy()
    repo.save(policy)
    return repo


@pytest.fixture
def compliance_service(
    compliance_policy_repo: InMemoryCompliancePolicyRepository,
    temp_audit_repo: JsonAuditRepository,
) -> ComplianceAssessmentService:
    collector = DefaultComplianceEvidenceCollector(audit_repository=temp_audit_repo)
    return ComplianceAssessmentService(
        evidence_collector=collector,
        policy_repository=compliance_policy_repo,
    )


@pytest.fixture
def rbac_service(tmp_path: Path) -> RBACService:
    role_repo = JsonRoleRepository(base_dir=tmp_path / "rbac")
    asg_repo = JsonRoleAssignmentRepository(base_dir=tmp_path / "rbac")

    admin_role = Role(
        role_id="role_sec_admin",
        name="SecurityAdmin",
        description="Admin with emergency stop permissions",
        permissions=(
            Permission(permission_id="p_act_glob", action="EMERGENCY_STOP_ACTIVATE", resource_scope="global_scope"),
            Permission(permission_id="p_deact_glob", action="EMERGENCY_STOP_DEACTIVATE", resource_scope="global_scope"),
            Permission(permission_id="p_act_any", action="EMERGENCY_STOP_ACTIVATE"),
            Permission(permission_id="p_deact_any", action="EMERGENCY_STOP_DEACTIVATE"),
            Permission(permission_id="p_exec_all", action="EXECUTE_ACTION"),
        ),
    )
    role_repo.save_role(admin_role)
    asg_repo.save_assignment(RoleAssignment(assignment_id="asg_admin", identity_id="admin-001", role_id="role_sec_admin"))

    operator_role = Role(
        role_id="role_operator",
        name="Operator",
        description="Standard operator",
        permissions=(
            Permission(permission_id="p_exec_op", action="EXECUTE_ACTION"),
            Permission(permission_id="p_pub_op", action="PUBLISH_ITEM"),
        ),
    )
    role_repo.save_role(operator_role)
    asg_repo.save_assignment(RoleAssignment(assignment_id="asg_op", identity_id="operator-002", role_id="role_operator"))

    return RBACService(role_repository=role_repo, assignment_repository=asg_repo)


@pytest.fixture
def authz_service(temp_audit_repo: JsonAuditRepository, virtual_clock: VirtualClock) -> AuthorizationService:
    engine = PolicyEngine(rules=[AuthorizationPolicyRule()])
    return AuthorizationService(
        policy_engine=engine,
        clock=virtual_clock,
        audit_repository=temp_audit_repo,
    )


@pytest.fixture
def estop_service(
    temp_estop_repo: JsonEmergencyStopRepository,
    temp_audit_repo: JsonAuditRepository,
    rbac_service: RBACService,
    virtual_clock: VirtualClock,
) -> EmergencyStopService:
    return EmergencyStopService(
        repository=temp_estop_repo,
        audit_repository=temp_audit_repo,
        rbac_service=rbac_service,
        clock=virtual_clock,
    )


def _make_principal(identity_id: str, display_name: str, clock: VirtualClock) -> PrincipalContext:
    ref = IdentityReference(
        identity_id=identity_id,
        identity_type=IdentityType.USER,
        canonical_identifier=f"user:{identity_id}",
        display_name=display_name,
    )
    auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.BEARER_TOKEN,
        provider="internal",
        principal=ref,
        authenticated_at=clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
        correlation_id=f"corr-{identity_id}",
    )
    return PrincipalContext(principal=ref, auth_result=auth)


def test_scenario_a_full_governance_passes_stop_inactive_executes_once(
    authz_service: AuthorizationService,
    estop_service: EmergencyStopService,
    rbac_service: RBACService,
    virtual_clock: VirtualClock,
):
    """Escenario A: Con stop inactivo, la acción legítima se ejecuta exactamente una vez."""
    delegate = MockActionExecutor()
    principal = _make_principal("operator-002", "Operator 002", virtual_clock)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["PUBLISH_ITEM"],
        rbac_service=rbac_service,
        emergency_stop_service=estop_service,
    )

    state = LoopState(iteration=1, mission_id="mission-101", goal="Test publishing")
    decision = LoopDecision(
        action=LoopAction.PROMOTE,
        reason="Test execute",
        target="mercadolibre",
        parameters={"action_type": "PUBLISH_ITEM", "target_resource": "mercadolibre", "is_external_side_effect": True},
    )

    res = guarded.execute(decision, state)

    assert res["status"] == "EXECUTED_SUCCESSFULLY"
    assert res["is_executable"] is True
    assert delegate.external_calls_count == 1
    assert guarded.latest_emergency_stop_decision.decision_status == EmergencyStopDecisionStatus.ALLOW_EXECUTION


def test_scenario_b_global_stop_active_zero_calls(
    authz_service: AuthorizationService,
    estop_service: EmergencyStopService,
    rbac_service: RBACService,
    virtual_clock: VirtualClock,
):
    """Escenario B: Con GLOBAL stop activo, se bloquea con 0 llamadas físicas."""
    admin_principal = _make_principal("admin-001", "Security Admin", virtual_clock)

    estop_service.activate_stop(
        principal_context=admin_principal,
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Critical market anomaly detected",
    )

    delegate = MockActionExecutor()
    operator_principal = _make_principal("operator-002", "Operator 002", virtual_clock)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=operator_principal,
        default_allowed_actions=["PUBLISH_ITEM"],
        rbac_service=rbac_service,
        emergency_stop_service=estop_service,
    )

    state = LoopState(iteration=1, mission_id="mission-101", goal="Test publishing")
    decision = LoopDecision(
        action=LoopAction.PROMOTE,
        reason="Test execute blocked",
        target="mercadolibre",
        parameters={"action_type": "PUBLISH_ITEM", "target_resource": "mercadolibre", "is_external_side_effect": True},
    )

    res = guarded.execute(decision, state)

    assert res["status"] == "EMERGENCY_STOP_BLOCKED"
    assert res["is_executable"] is False
    assert delegate.external_calls_count == 0
    assert guarded.latest_emergency_stop_decision.decision_status == EmergencyStopDecisionStatus.BLOCK_EXECUTION


def test_scenario_c_and_d_account_scoped_stop(
    authz_service: AuthorizationService,
    estop_service: EmergencyStopService,
    rbac_service: RBACService,
    virtual_clock: VirtualClock,
):
    """Escenarios C y D: Account-scoped stop bloquea la cuenta indicada pero permite cuentas diferentes."""
    admin_principal = _make_principal("admin-001", "Security Admin", virtual_clock)

    estop_service.activate_stop(
        principal_context=admin_principal,
        scope=EmergencyStopScope.ACCOUNT,
        target_id="acc-blocked-99",
        reason_details="Suspicious activity on acc-blocked-99",
    )

    delegate = MockActionExecutor()
    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=admin_principal,
        default_allowed_actions=["UPDATE_STOCK"],
        rbac_service=rbac_service,
        emergency_stop_service=estop_service,
    )

    # Intento 1: Cuenta bloqueada (Escenario C)
    state = LoopState(iteration=1, mission_id="mission-c", goal="Update inventory")
    dec_blocked = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Stock update blocked target",
        target="mercadolibre",
        parameters={"action_type": "UPDATE_STOCK", "account_id": "acc-blocked-99", "is_external_side_effect": True},
    )
    res_blocked = guarded.execute(dec_blocked, state)
    assert res_blocked["status"] == "EMERGENCY_STOP_BLOCKED"
    assert delegate.external_calls_count == 0

    # Intento 2: Otra cuenta no afectada (Escenario D)
    dec_allowed = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Stock update allowed target",
        target="mercadolibre",
        parameters={"action_type": "UPDATE_STOCK", "account_id": "acc-allowed-11", "is_external_side_effect": True},
    )
    res_allowed = guarded.execute(dec_allowed, state)
    assert res_allowed["status"] == "EXECUTED_SUCCESSFULLY"
    assert delegate.external_calls_count == 1


def test_scenario_e_stop_activated_during_autonomous_operation(
    authz_service: AuthorizationService,
    estop_service: EmergencyStopService,
    rbac_service: RBACService,
    virtual_clock: VirtualClock,
):
    """Escenario E: Stop activado mientras el sistema autónomo opera bloquea side-effects subsiguientes."""
    delegate = MockActionExecutor()
    principal = _make_principal("admin-001", "Security Admin", virtual_clock)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=principal,
        default_allowed_actions=["MUTATE_ORDER"],
        rbac_service=rbac_service,
        emergency_stop_service=estop_service,
    )

    state1 = LoopState(iteration=1, mission_id="mission-e", goal="Operate orders")
    dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Order operation cycle",
        target="orders",
        parameters={"action_type": "MUTATE_ORDER", "is_external_side_effect": True},
    )

    # Ciclo 1: Ejecución permitida
    res1 = guarded.execute(dec, state1)
    assert res1["status"] == "EXECUTED_SUCCESSFULLY"
    assert delegate.external_calls_count == 1

    # Activación de emergencia en tiempo de ejecución
    estop_service.activate_stop(
        principal_context=principal,
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Containment triggered",
    )

    # Ciclo 2: Ejecución subsiguiente bloqueada
    state2 = LoopState(iteration=2, mission_id="mission-e", goal="Operate orders")
    res2 = guarded.execute(dec, state2)
    assert res2["status"] == "EMERGENCY_STOP_BLOCKED"
    assert delegate.external_calls_count == 1  # No aumentó


def test_scenario_f_restart_preserves_active_state(
    tmp_path: Path,
    rbac_service: RBACService,
    virtual_clock: VirtualClock,
    authz_service: AuthorizationService,
):
    """Escenario F: Reinicio / recarga del repositorio preserva el estado ACTIVE y su checksum."""
    repo_file = tmp_path / "persisted_estop" / "stops.json"
    repo1 = JsonEmergencyStopRepository(file_path=repo_file)
    service1 = EmergencyStopService(repository=repo1, rbac_service=rbac_service, clock=virtual_clock)

    admin = _make_principal("admin-001", "Security Admin", virtual_clock)

    rec = service1.activate_stop(
        principal_context=admin,
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Persist across reboot",
    )
    assert rec.is_active_at(virtual_clock.now()) is True

    # Simular reinicio creando nueva instancia contra el mismo archivo
    repo2 = JsonEmergencyStopRepository(file_path=repo_file)
    service2 = EmergencyStopService(repository=repo2, rbac_service=rbac_service, clock=virtual_clock)

    delegate = MockActionExecutor()
    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=admin,
        default_allowed_actions=["ANY_ACTION"],
        rbac_service=rbac_service,
        emergency_stop_service=service2,
    )

    state = LoopState(iteration=1, mission_id="mission-f", goal="Persisted state test")
    dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Test reboot persistence",
        target="target",
        parameters={"action_type": "ANY_ACTION", "is_external_side_effect": True},
    )
    res = guarded.execute(dec, state)
    assert res["status"] == "EMERGENCY_STOP_BLOCKED"
    assert delegate.external_calls_count == 0


def test_scenario_g_temporary_stop_expires_with_clock(
    virtual_clock: VirtualClock,
    estop_service: EmergencyStopService,
    rbac_service: RBACService,
    authz_service: AuthorizationService,
):
    """Escenario G: Stop temporal expira determinísticamente mediante el virtual clock."""
    admin = _make_principal("admin-001", "Security Admin", virtual_clock)

    # Activar con expiración a los 60 segundos
    estop_service.activate_stop(
        principal_context=admin,
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Cooling off period",
        expires_at=virtual_clock.now() + timedelta(seconds=60),
    )

    delegate = MockActionExecutor()
    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=admin,
        default_allowed_actions=["ACTION_TEST"],
        rbac_service=rbac_service,
        emergency_stop_service=estop_service,
    )

    state = LoopState(iteration=1, mission_id="mission-g", goal="Expiration test")
    dec = LoopDecision(
        action=LoopAction.CONTINUE,
        reason="Expiration verify action",
        target="target",
        parameters={"action_type": "ACTION_TEST", "is_external_side_effect": True},
    )

    # A t=0 está activo -> bloquea
    res1 = guarded.execute(dec, state)
    assert res1["status"] == "EMERGENCY_STOP_BLOCKED"
    assert delegate.external_calls_count == 0

    # Avanzar reloj virtual más allá de la expiración
    virtual_clock.advance(100.0)

    # Ahora debe permitir la ejecución
    res2 = guarded.execute(dec, state)
    assert res2["status"] == "EXECUTED_SUCCESSFULLY"
    assert delegate.external_calls_count == 1


def test_scenario_h_unauthorized_deactivation_rejected_stop_remains_active(
    estop_service: EmergencyStopService,
    rbac_service: RBACService,
    authz_service: AuthorizationService,
    virtual_clock: VirtualClock,
):
    """Escenario H: Desactivación por actor no autorizado es rechazada y el stop sigue activo."""
    admin = _make_principal("admin-001", "Security Admin", virtual_clock)

    rec = estop_service.activate_stop(
        principal_context=admin,
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Active stop",
    )

    unauthorized_operator = _make_principal("operator-002", "Operator 002", virtual_clock)

    with pytest.raises(PermissionError):
        estop_service.deactivate_stop(
            stop_id=rec.stop_id,
            principal_context=unauthorized_operator,
            reason_details="I want to resume",
        )

    # Verificar que sigue activo en el guardián
    delegate = MockActionExecutor()
    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=admin,
        default_allowed_actions=["ACTION_TEST"],
        rbac_service=rbac_service,
        emergency_stop_service=estop_service,
    )
    state = LoopState(iteration=1, mission_id="mission-h", goal="Unauthorized deactivation")
    dec = LoopDecision(action=LoopAction.CONTINUE, reason="Test action blocked", parameters={"action_type": "ACTION_TEST", "is_external_side_effect": True})
    res = guarded.execute(dec, state)
    assert res["status"] == "EMERGENCY_STOP_BLOCKED"
    assert delegate.external_calls_count == 0


def test_scenario_i_tampered_stop_repository_fails_safe(
    tmp_path: Path,
    rbac_service: RBACService,
    virtual_clock: VirtualClock,
    authz_service: AuthorizationService,
):
    """Escenario I: Repositorio manipulado o corrupto activa fail-safe de bloqueo."""
    repo_file = tmp_path / "tampered_repo" / "stops.json"
    repo_file.parent.mkdir(parents=True, exist_ok=True)
    with open(repo_file, "w", encoding="utf-8") as f:
        f.write("{invalid_json: corrupt")

    repo = JsonEmergencyStopRepository(file_path=repo_file)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=virtual_clock)

    delegate = MockActionExecutor()
    admin = _make_principal("admin-001", "Security Admin", virtual_clock)

    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=admin,
        default_allowed_actions=["ACTION_TEST"],
        rbac_service=rbac_service,
        emergency_stop_service=service,
    )

    state = LoopState(iteration=1, mission_id="mission-i", goal="Tampered store test")
    dec = LoopDecision(action=LoopAction.CONTINUE, reason="Tampered store execution test", parameters={"action_type": "ACTION_TEST", "is_external_side_effect": True})
    res = guarded.execute(dec, state)
    assert res["status"] == "EMERGENCY_STOP_BLOCKED"
    assert res["emergency_stop_reason_code"] == EmergencyStopReasonCode.FAIL_SAFE_STORE_CORRUPTION.value
    assert delegate.external_calls_count == 0


def test_scenario_j_and_k_compliance_verification_with_n10(
    compliance_service: ComplianceAssessmentService,
    temp_audit_repo: JsonAuditRepository,
    virtual_clock: VirtualClock,
    estop_service: EmergencyStopService,
    rbac_service: RBACService,
    authz_service: AuthorizationService,
):
    """
    Escenarios J y K:
    - J: N.10 evalúa como COMPLIANT cuando la acción ante stop activo es efectivamente bloqueada.
    - K: N.10 evalúa como NON_COMPLIANT si se detecta bypass / ejecución física mientras el stop está activo.
    """
    admin = _make_principal("admin-001", "Security Admin", virtual_clock)

    estop_service.activate_stop(
        principal_context=admin,
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Court order freeze",
    )

    delegate = MockActionExecutor()
    guarded = AuthorizationGuardedActionExecutor(
        delegate_executor=delegate,
        authorization_service=authz_service,
        principal_context=admin,
        default_allowed_actions=["ORDER_FULFILL"],
        rbac_service=rbac_service,
        emergency_stop_service=estop_service,
    )

    # 1. Ejecución bloqueada legalmente por N.11 (Escenario J)
    corr_j = "corr-mission-j"
    msn_j = "mission-j"
    state = LoopState(iteration=1, mission_id=msn_j, goal="Compliance test")
    dec = LoopDecision(action=LoopAction.CONTINUE, reason="Fulfill order try", parameters={"action_type": "ORDER_FULFILL", "is_external_side_effect": True})
    res_blocked = guarded.execute(dec, state)
    assert res_blocked["status"] == "EMERGENCY_STOP_BLOCKED"
    assert delegate.external_calls_count == 0

    # Agregar trazas de contexto requeridas por la política comercial estándar
    temp_audit_repo.append(AuditRecord(
        audit_id="aud_auth_j",
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        occurred_at=virtual_clock.now(),
        actor=AuditActor(actor_type=AuditActorType.USER, actor_id="admin-001"),
        subject_type="IDENTITY",
        subject_id="admin-001",
        action_or_operation="AUTHENTICATE",
        status="AUTHENTICATED",
        correlation_id=corr_j,
        mission_id=msn_j,
    ))
    temp_audit_repo.append(AuditRecord(
        audit_id="aud_authz_j",
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        occurred_at=virtual_clock.now(),
        actor=AuditActor(actor_type=AuditActorType.USER, actor_id="admin-001"),
        subject_type="OPERATION",
        subject_id="ORDER_FULFILL",
        action_or_operation="ORDER_FULFILL",
        status="ALLOW",
        correlation_id=corr_j,
        mission_id=msn_j,
    ))
    temp_audit_repo.append(AuditRecord(
        audit_id="aud_estop_j",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=virtual_clock.now(),
        actor=AuditActor(actor_type=AuditActorType.USER, actor_id="admin-001"),
        subject_type="OPERATION",
        subject_id="ORDER_FULFILL",
        action_or_operation="EMERGENCY_STOP_EVALUATED",
        status="BLOCK_EXECUTION",
        correlation_id=corr_j,
        mission_id=msn_j,
        metadata={"emergency_stop_status": "BLOCK_EXECUTION"},
    ))

    assessment_j = compliance_service.assess_operation(
        correlation_id=corr_j,
        mission_id=msn_j,
        action_or_operation="ORDER_FULFILL",
    )
    assert assessment_j.overall_status == ComplianceStatus.COMPLIANT
    estop_finding_j = next((f for f in assessment_j.findings if f.requirement_id == "REQ_EMERGENCY_STOP"), None)
    assert estop_finding_j is not None
    assert estop_finding_j.status == ComplianceStatus.COMPLIANT
    assert estop_finding_j.reason_code == ComplianceFindingReasonCode.COMPLIANT_BLOCKED_ACTION

    # 2. Simular un bypass no autorizado donde se ejecutó físicamente (Escenario K)
    corr_k = "corr-mission-k"
    msn_k = "mission-k"
    temp_audit_repo.append(AuditRecord(
        audit_id="aud_auth_k",
        record_type=AuditRecordType.AUTHENTICATION_EVALUATED,
        occurred_at=virtual_clock.now(),
        actor=AuditActor(actor_type=AuditActorType.USER, actor_id="admin-001"),
        subject_type="IDENTITY",
        subject_id="admin-001",
        action_or_operation="AUTHENTICATE",
        status="AUTHENTICATED",
        correlation_id=corr_k,
        mission_id=msn_k,
    ))
    temp_audit_repo.append(AuditRecord(
        audit_id="aud_authz_k",
        record_type=AuditRecordType.AUTHORIZATION_EVALUATED,
        occurred_at=virtual_clock.now(),
        actor=AuditActor(actor_type=AuditActorType.USER, actor_id="admin-001"),
        subject_type="OPERATION",
        subject_id="ORDER_FULFILL",
        action_or_operation="ORDER_FULFILL",
        status="ALLOW",
        correlation_id=corr_k,
        mission_id=msn_k,
    ))
    temp_audit_repo.append(AuditRecord(
        audit_id="aud_estop_k_blocked",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=virtual_clock.now(),
        actor=AuditActor(actor_type=AuditActorType.USER, actor_id="admin-001"),
        subject_type="OPERATION",
        subject_id="ORDER_FULFILL",
        action_or_operation="EMERGENCY_STOP_EVALUATED",
        status="BLOCK_EXECUTION",
        correlation_id=corr_k,
        mission_id=msn_k,
        metadata={"emergency_stop_status": "BLOCK_EXECUTION"},
    ))
    temp_audit_repo.append(AuditRecord(
        audit_id="aud_estop_k_bypass",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=virtual_clock.now(),
        actor=AuditActor(actor_type=AuditActorType.USER, actor_id="admin-001"),
        subject_type="OPERATION",
        subject_id="ORDER_FULFILL",
        action_or_operation="ORDER_FULFILL",
        status="SUCCESS",
        correlation_id=corr_k,
        mission_id=msn_k,
        metadata={"bypassed_guard": True},
    ))

    assessment_k = compliance_service.assess_operation(
        correlation_id=corr_k,
        mission_id=msn_k,
        action_or_operation="ORDER_FULFILL",
    )
    assert assessment_k.overall_status == ComplianceStatus.NON_COMPLIANT
    estop_finding_k = next((f for f in assessment_k.findings if f.requirement_id == "REQ_EMERGENCY_STOP"), None)
    assert estop_finding_k is not None
    assert estop_finding_k.status == ComplianceStatus.NON_COMPLIANT
    assert estop_finding_k.reason_code == ComplianceFindingReasonCode.EMERGENCY_STOP_BLOCKED_BUT_EXECUTED
