"""
Unit test suite for N.11 — Emergency Stop (Transversal N — Security, Governance & Safety).

Covering requirements:
1. Inactive state -> ALLOW_EXECUTION
2. Global active stop -> BLOCK_EXECUTION
3. Scoped active stop -> matching target blocked
4. Non-matching scope allowed
5. Unknown / corrupt store fail-safe -> BLOCK_EXECUTION
6. Unauthorized activation rejected
7. Authorized activation accepted
8. Authorized deactivation transitions state cleanly
9. Activation idempotency (no duplicates for identical scope/parameters)
10. Virtual clock expiration (ClockPort) transitions to inactive without sleep
11. Scope precedence: GLOBAL domina sobre MARKETPLACE, ACCOUNT, MISSION, TOOL, ACTION_TYPE
12. Read-only permitted if allow_read_only=True
13. External side effects strictly blocked
14. Deterministic structured decisions
15. Non-destructive safety (no deletion of records or entities)
"""

from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import pytest

from src.domain.emergency_stop.models import (
    EmergencyStopState,
    EmergencyStopScope,
    EmergencyStopDecisionStatus,
    EmergencyStopReasonCode,
    EmergencyStopRecord,
    EmergencyStopEvaluationContext,
    EmergencyStopDecision,
    compute_emergency_stop_checksum,
)
from src.infrastructure.persistence.data.json.emergency_stop_repository import (
    JsonEmergencyStopRepository,
)
from src.application.emergency_stop.emergency_stop_service import (
    EmergencyStopService,
)
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.authentication.models import (
    PrincipalContext,
    AuthenticationResult,
    AuthenticationStatus,
    AuthenticationMethod,
)
from src.domain.rbac.models import (
    RoleAssignment,
    Role,
    Permission,
)
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.application.rbac.rbac_service import RBACService
from src.domain.reliability.ports import ClockPort


class SimulatedClock(ClockPort):
    def __init__(self, start_time: datetime):
        self.current = start_time if start_time.tzinfo else start_time.replace(tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.current

    def advance(self, td: timedelta) -> None:
        self.current += td

    def sleep(self, seconds: float) -> None:
        self.advance(timedelta(seconds=seconds))


@pytest.fixture
def temp_repo_path(tmp_path: Path) -> Path:
    return tmp_path / "emergency_stops.json"


@pytest.fixture
def base_clock() -> SimulatedClock:
    return SimulatedClock(datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def rbac_service(tmp_path: Path) -> RBACService:
    role_repo = JsonRoleRepository(base_dir=tmp_path / "rbac")
    asg_repo = JsonRoleAssignmentRepository(base_dir=tmp_path / "rbac")

    # Rol admin con permisos completos de emergency stop
    admin_role = Role(
        role_id="role_security_admin",
        name="Security Administrator",
        description="Full governance controls",
        permissions=(
            Permission(permission_id="p_estop_act_global", action="EMERGENCY_STOP_ACTIVATE", resource_scope="global_scope"),
            Permission(permission_id="p_estop_deact_global", action="EMERGENCY_STOP_DEACTIVATE", resource_scope="global_scope"),
            Permission(permission_id="p_estop_act_any", action="EMERGENCY_STOP_ACTIVATE"),
            Permission(permission_id="p_estop_deact_any", action="EMERGENCY_STOP_DEACTIVATE"),
        ),
    )
    role_repo.save_role(admin_role)
    asg_repo.save_assignment(RoleAssignment(assignment_id="asg_admin", identity_id="sec_officer_01", role_id="role_security_admin"))

    # Rol operador estándar sin permisos de stop
    op_role = Role(
        role_id="role_operator",
        name="Operator",
        description="Standard operations",
        permissions=(
            Permission(permission_id="p_order_read", action="ORDER_READ"),
        ),
    )
    role_repo.save_role(op_role)
    asg_repo.save_assignment(RoleAssignment(assignment_id="asg_op", identity_id="operator_01", role_id="role_operator"))

    return RBACService(role_repository=role_repo, assignment_repository=asg_repo)


@pytest.fixture
def auth_contexts(base_clock: SimulatedClock):
    admin_ref = IdentityReference(
        identity_id="sec_officer_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:security:01",
        display_name="Security Officer 01",
    )
    admin_auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=admin_ref,
        authenticated_at=base_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
        correlation_id="corr-admin-auth",
    )
    admin_ctx = PrincipalContext(principal=admin_ref, auth_result=admin_auth)

    op_ref = IdentityReference(
        identity_id="operator_01",
        identity_type=IdentityType.USER,
        canonical_identifier="user:operator:01",
        display_name="Operator 01",
    )
    op_auth = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.BEARER_TOKEN,
        provider="internal",
        principal=op_ref,
        authenticated_at=base_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
        correlation_id="corr-op-auth",
    )
    op_ctx = PrincipalContext(principal=op_ref, auth_result=op_auth)

    anon_ref = IdentityReference(
        identity_id="anonymous",
        identity_type=IdentityType.USER,
        canonical_identifier="user:anon:0",
        display_name="Anonymous",
    )
    anon_auth = AuthenticationResult(
        status=AuthenticationStatus.UNAUTHENTICATED,
        method=AuthenticationMethod.UNKNOWN,
        provider="internal",
        principal=anon_ref,
        reason_codes=("UNAUTHENTICATED",),
        correlation_id="corr-anon-auth",
    )
    anon_ctx = PrincipalContext(principal=anon_ref, auth_result=anon_auth)

    return {"admin": admin_ctx, "operator": op_ctx, "unauthenticated": anon_ctx}


# ---------------------------------------------------------------------------
# TESTS
# ---------------------------------------------------------------------------

def test_1_inactive_state_allows_execution(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    ctx = EmergencyStopEvaluationContext(
        action_name="PUBLISH_ITEM",
        target_resource="mercadolibre_item_123",
        marketplace="mercadolibre",
        account_id="acc_main",
        mission_id="mis_001",
    )

    decision = service.evaluate(ctx)
    assert decision.is_executable is True
    assert decision.decision_status == EmergencyStopDecisionStatus.ALLOW_EXECUTION
    assert decision.reason_code == EmergencyStopReasonCode.NO_ACTIVE_STOP
    assert decision.checksum is not None


def test_2_global_active_stop_blocks_execution(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    # Activar Stop GLOBAL
    service.activate_stop(
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Critical security breach detected",
        principal_context=auth_contexts["admin"],
    )

    ctx = EmergencyStopEvaluationContext(
        action_name="PUBLISH_ITEM",
        target_resource="mercadolibre_item_123",
        marketplace="mercadolibre",
        account_id="acc_main",
        mission_id="mis_001",
        is_read_only=False,
    )

    decision = service.evaluate(ctx)
    assert decision.is_executable is False
    assert decision.is_blocked is True
    assert decision.decision_status == EmergencyStopDecisionStatus.BLOCK_EXECUTION
    assert decision.reason_code == EmergencyStopReasonCode.GLOBAL_STOP_ACTIVE
    assert decision.applied_scope == EmergencyStopScope.GLOBAL


def test_3_scoped_active_stop_blocks_matching_target_only(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    # Activar stop para Marketplace 'mercadolibre'
    service.activate_stop(
        scope=EmergencyStopScope.MARKETPLACE,
        target_id="mercadolibre",
        reason_details="Mercadolibre API unstable",
        principal_context=auth_contexts["admin"],
    )

    # Contexto MercadoLibre -> Bloqueado
    ctx_meli = EmergencyStopEvaluationContext(
        action_name="UPDATE_PRICE",
        marketplace="mercadolibre",
        account_id="acc_1",
    )
    dec_meli = service.evaluate(ctx_meli)
    assert dec_meli.is_executable is False
    assert dec_meli.applied_scope == EmergencyStopScope.MARKETPLACE

    # Contexto Amazon -> Permitido
    ctx_amz = EmergencyStopEvaluationContext(
        action_name="UPDATE_PRICE",
        marketplace="amazon",
        account_id="acc_1",
    )
    dec_amz = service.evaluate(ctx_amz)
    assert dec_amz.is_executable is True
    assert dec_amz.decision_status == EmergencyStopDecisionStatus.ALLOW_EXECUTION


def test_4_account_and_mission_scope_enforcement(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    # Stop para account_999
    service.activate_stop(
        scope=EmergencyStopScope.ACCOUNT,
        target_id="account_999",
        reason_details="Fraud suspected on account",
        principal_context=auth_contexts["admin"],
    )

    # Acc 999 bloqueada
    dec_blocked = service.evaluate(EmergencyStopEvaluationContext(action_name="SYNC", account_id="account_999"))
    assert dec_blocked.is_executable is False
    assert dec_blocked.applied_scope == EmergencyStopScope.ACCOUNT

    # Acc 111 permitida
    dec_allowed = service.evaluate(EmergencyStopEvaluationContext(action_name="SYNC", account_id="account_111"))
    assert dec_allowed.is_executable is True


def test_5_fail_safe_on_corrupt_store(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    # Simular corrupción en el archivo
    temp_repo_path.parent.mkdir(parents=True, exist_ok=True)
    with open(temp_repo_path, "w", encoding="utf-8") as f:
        f.write("{invalid_json: corrupt")

    repo_corrupt = JsonEmergencyStopRepository(file_path=temp_repo_path)
    assert repo_corrupt.is_corrupt is True

    service_corrupt = EmergencyStopService(repository=repo_corrupt, rbac_service=rbac_service, clock=base_clock)
    dec = service_corrupt.evaluate(EmergencyStopEvaluationContext(action_name="CRITICAL_ACTION"))

    assert dec.is_executable is False
    assert dec.decision_status == EmergencyStopDecisionStatus.BLOCK_EXECUTION
    assert dec.reason_code == EmergencyStopReasonCode.FAIL_SAFE_STORE_CORRUPTION


def test_6_unauthorized_activation_rejected(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    with pytest.raises(PermissionError):
        service.activate_stop(
            scope=EmergencyStopScope.GLOBAL,
            reason_details="Unauthorized halt attempt",
            principal_context=auth_contexts["operator"],
        )


def test_7_authorized_activation_and_deactivation_lifecycle(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    # 1. Activar
    rec = service.activate_stop(
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Scheduled safety shutdown",
        principal_context=auth_contexts["admin"],
    )
    assert rec.state == EmergencyStopState.ACTIVE

    # Verificar que bloquea
    dec1 = service.evaluate(EmergencyStopEvaluationContext(action_name="ORDER_MUTATE"))
    assert dec1.is_executable is False

    # 2. Desactivar
    deact_rec = service.deactivate_stop(
        stop_id=rec.stop_id,
        reason_details="Maintenance completed safely",
        principal_context=auth_contexts["admin"],
    )
    assert deact_rec.state == EmergencyStopState.INACTIVE
    assert deact_rec.deactivated_by_identity_id == auth_contexts["admin"].identity_id

    # Verificar que ahora permite
    dec2 = service.evaluate(EmergencyStopEvaluationContext(action_name="ORDER_MUTATE"))
    assert dec2.is_executable is True


def test_8_activation_idempotency(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    rec1 = service.activate_stop(
        scope=EmergencyStopScope.ACCOUNT,
        target_id="acc_alpha",
        reason_details="Alert triggered",
        principal_context=auth_contexts["admin"],
    )

    rec2 = service.activate_stop(
        scope=EmergencyStopScope.ACCOUNT,
        target_id="acc_alpha",
        reason_details="Alert triggered duplicate",
        principal_context=auth_contexts["admin"],
    )

    assert rec1.stop_id == rec2.stop_id
    all_recs = repo.list_records(scope=EmergencyStopScope.ACCOUNT, target_id="acc_alpha")
    assert len(all_recs) == 1


def test_9_virtual_clock_expiration(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    exp_time = base_clock.now() + timedelta(minutes=10)

    # Activar con expiración a 10 min
    service.activate_stop(
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Temporary freeze",
        principal_context=auth_contexts["admin"],
        expires_at=exp_time,
    )

    # Al minuto 5: Sigue activo
    base_clock.advance(timedelta(minutes=5))
    dec_active = service.evaluate(EmergencyStopEvaluationContext(action_name="SYNC"))
    assert dec_active.is_executable is False

    # Al minuto 11: Expiró -> Permite
    base_clock.advance(timedelta(minutes=6))
    dec_expired = service.evaluate(EmergencyStopEvaluationContext(action_name="SYNC"))
    assert dec_expired.is_executable is True
    assert dec_expired.decision_status == EmergencyStopDecisionStatus.ALLOW_EXECUTION


def test_10_hierarchical_scope_precedence(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    # Activar GLOBAL Stop
    service.activate_stop(
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Global freeze",
        principal_context=auth_contexts["admin"],
    )

    # Contexto con cuenta específica y marketplace específico
    ctx = EmergencyStopEvaluationContext(
        action_name="PUBLISH",
        marketplace="mercadolibre",
        account_id="acc_specific",
        mission_id="mis_specific",
    )

    dec = service.evaluate(ctx)
    assert dec.is_executable is False
    assert dec.applied_scope == EmergencyStopScope.GLOBAL  # GLOBAL domina incondicionalmente


def test_11_read_only_allowed_if_configured(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service = EmergencyStopService(repository=repo, rbac_service=rbac_service, clock=base_clock)

    # Activar stop permitiendo read_only
    service.activate_stop(
        scope=EmergencyStopScope.GLOBAL,
        reason_details="Auditing system",
        principal_context=auth_contexts["admin"],
        allow_read_only=True,
    )

    # Acción de sólo lectura -> Permitida
    ctx_ro = EmergencyStopEvaluationContext(
        action_name="GET_ORDER_STATUS",
        is_read_only=True,
    )
    dec_ro = service.evaluate(ctx_ro)
    assert dec_ro.is_executable is True
    assert dec_ro.reason_code == EmergencyStopReasonCode.READ_ONLY_PERMITTED_BY_POLICY

    # Mutación / Efecto externo -> Bloqueada
    ctx_write = EmergencyStopEvaluationContext(
        action_name="UPDATE_STOCK",
        is_read_only=False,
    )
    dec_write = service.evaluate(ctx_write)
    assert dec_write.is_executable is False
    assert dec_write.reason_code == EmergencyStopReasonCode.GLOBAL_STOP_ACTIVE


def test_12_non_destructive_safety_on_persistence_and_reload(temp_repo_path: Path, base_clock: SimulatedClock, rbac_service: RBACService, auth_contexts):
    repo1 = JsonEmergencyStopRepository(file_path=temp_repo_path)
    service1 = EmergencyStopService(repository=repo1, rbac_service=rbac_service, clock=base_clock)

    rec = service1.activate_stop(
        scope=EmergencyStopScope.TOOL,
        target_id="meli_api_client",
        reason_details="Tool malfunction",
        principal_context=auth_contexts["admin"],
    )

    # Reabrir repositorio desde disco (simular reinicio del servicio)
    repo2 = JsonEmergencyStopRepository(file_path=temp_repo_path)
    assert repo2.is_corrupt is False
    loaded_rec = repo2.get_by_id(rec.stop_id)
    assert loaded_rec is not None
    assert loaded_rec.state == EmergencyStopState.ACTIVE
    assert loaded_rec.target_id == "meli_api_client"
    assert loaded_rec.checksum == rec.checksum
