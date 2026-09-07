"""
Tests unitarios para N.4 RBAC / Permissions (Transversal N Security, Governance y Safety).

Cubre los 16 requerimientos mínimos exigidos por el Hito N.4:
1. immutable Permission
2. immutable Role
3. role resolves permissions
4. multiple roles union permissions
5. unknown role → no permission
6. missing assignment → no permission
7. scoped permission
8. wrong scope denied
9. expired assignment ignored
10. idempotent assignment
11. duplicate conflict handling
12. deterministic effective permission set
13. no privilege escalation
14. sanitized metadata
15. RBAC != Authorization
16. no N.5+ implementation (no Secret Management ni Approval Policies)
"""

from datetime import datetime, timedelta, timezone
import pytest
from dataclasses import FrozenInstanceError

from src.domain.identity.models import (
    IdentityReference,
    IdentityType,
)
from src.domain.authentication.models import (
    AuthenticationMethod,
    AuthenticationStatus,
    AuthenticationResult,
    PrincipalContext,
)
from src.domain.rbac.models import (
    Permission,
    PermissionStatus,
    Role,
    RoleStatus,
    RoleAssignment,
    PermissionSet,
    RbacEvaluationResult,
    normalize_action_token,
    compute_permission_checksum,
    compute_role_checksum,
    compute_role_assignment_checksum,
    compute_evaluation_checksum,
)
from src.domain.rbac.ports import (
    RoleRepositoryPort,
    RoleAssignmentRepositoryPort,
)
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
    RoleConflictError,
    RoleAssignmentConflictError,
    CorruptedRoleRecordError,
    CorruptedRoleAssignmentRecordError,
)
from src.application.rbac.rbac_service import RBACService
from src.infrastructure.reliability.reliability_infrastructure import VirtualClock


@pytest.fixture
def virtual_clock():
    start_time = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    return VirtualClock(initial_time=start_time)


@pytest.fixture
def rbac_service_in_memory(tmp_path, virtual_clock):
    role_repo = JsonRoleRepository(base_dir=tmp_path / "roles")
    assignment_repo = JsonRoleAssignmentRepository(base_dir=tmp_path / "assignments")
    return RBACService(
        role_repository=role_repo,
        assignment_repository=assignment_repo,
        clock=virtual_clock,
    )


@pytest.fixture
def valid_principal(virtual_clock):
    id_ref = IdentityReference(
        identity_id="usr_operator_001",
        identity_type=IdentityType.USER,
        canonical_identifier="user:internal:usr_operator_001",
    )
    auth_result = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=id_ref,
        authenticated_at=virtual_clock.now(),
        reason_codes=("AUTHENTICATION_SUCCESS",),
        correlation_id="corr_auth_001",
    )
    return PrincipalContext(principal=id_ref, auth_result=auth_result)


# =============================================================================
# 1. Immutable Permission
# =============================================================================

def test_1_immutable_permission():
    perm = Permission(
        permission_id="perm_listing_read",
        action="listing.read",
        description="Read marketplace listings",
    )
    assert perm.action == "LISTING_READ"
    assert perm.status == PermissionStatus.ACTIVE

    with pytest.raises(FrozenInstanceError):
        perm.action = "LISTING_WRITE"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        perm.status = PermissionStatus.DISABLED  # type: ignore


# =============================================================================
# 2. Immutable Role
# =============================================================================

def test_2_immutable_role():
    perm = Permission(permission_id="perm_price_update", action="price.update")
    role = Role(
        role_id="role_pricer",
        name="Price Updater",
        permissions=(perm,),
    )
    assert role.role_id == "role_pricer"
    assert len(role.permissions) == 1

    with pytest.raises(FrozenInstanceError):
        role.name = "Super Pricer"  # type: ignore

    with pytest.raises(FrozenInstanceError):
        role.permissions = ()  # type: ignore


# =============================================================================
# 3. Role Resolves Permissions
# =============================================================================

def test_3_role_resolves_permissions(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    perm1 = svc.create_permission("perm_read", "listing.read", description="Read listings")
    perm2 = svc.create_permission("perm_pub", "listing.publish", description="Publish listings")

    role = svc.define_role("role_operator", name="Operator Role", permissions=[perm1, perm2])
    svc.assign_role(identity_id=valid_principal.identity_id, role_id=role.role_id)

    res = svc.resolve_effective_permissions(valid_principal)
    assert res.identity_id == valid_principal.identity_id
    assert "LISTING_READ" in res.actions
    assert "LISTING_PUBLISH" in res.actions
    assert len(res.permission_ids) == 2
    assert svc.has_permission(valid_principal, "listing.read") is True
    assert svc.has_permission(valid_principal, "listing.publish") is True
    assert svc.has_permission(valid_principal, "price.update") is False


# =============================================================================
# 4. Multiple Roles Union Permissions
# =============================================================================

def test_4_multiple_roles_union_permissions(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    p_read = svc.create_permission("perm_read", "listing.read")
    p_price = svc.create_permission("perm_price", "price.update")
    p_order = svc.create_permission("perm_order", "order.manage")

    role_viewer = svc.define_role("role_viewer", name="Viewer", permissions=[p_read])
    role_manager = svc.define_role("role_mgr", name="Manager", permissions=[p_price, p_order])

    svc.assign_role(identity_id=valid_principal.identity_id, role_id=role_viewer.role_id)
    svc.assign_role(identity_id=valid_principal.identity_id, role_id=role_manager.role_id)

    res = svc.resolve_effective_permissions(valid_principal)
    assert "role_viewer" in res.roles
    assert "role_mgr" in res.roles
    assert set(res.actions) == {"LISTING_READ", "PRICE_UPDATE", "ORDER_MANAGE"}
    assert len(res.permission_ids) == 3


# =============================================================================
# 5. Unknown Role → No Permission
# =============================================================================

def test_5_unknown_role_no_permission(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    # Intentar asignar un rol inexistente falla
    with pytest.raises(ValueError, match="Cannot assign non-existent role"):
        svc.assign_role(identity_id=valid_principal.identity_id, role_id="role_non_existent")

    res = svc.resolve_effective_permissions(valid_principal)
    assert len(res.permission_ids) == 0
    assert len(res.actions) == 0


# =============================================================================
# 6. Missing Assignment → No Permission (DEFAULT DENY)
# =============================================================================

def test_6_missing_assignment_no_permission(rbac_service_in_memory):
    svc = rbac_service_in_memory
    # Crear roles y permisos en el sistema pero sin asignación a usr_unassigned
    p = svc.create_permission("perm_order", "order.manage")
    svc.define_role("role_admin", name="Admin", permissions=[p])

    unassigned_identity = "usr_unassigned_999"
    res = svc.resolve_effective_permissions(unassigned_identity)
    assert len(res.roles) == 0
    assert len(res.actions) == 0
    assert res.effective_permissions.is_empty is True
    assert svc.has_permission(unassigned_identity, "order.manage") is False


# =============================================================================
# 7. Scoped Permission
# =============================================================================

def test_7_scoped_permission(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    p_global = svc.create_permission("perm_read", "listing.read", resource_scope=None)
    p_scoped = svc.create_permission("perm_pub", "listing.publish", resource_scope="mkt_amazon_us")

    role = svc.define_role("role_scoped_agent", name="Agent", permissions=[p_global, p_scoped])
    svc.assign_role(identity_id=valid_principal.identity_id, role_id=role.role_id, scope="mkt_amazon_us")

    # Evaluación en scope mkt_amazon_us -> ambos aplican
    res_us = svc.resolve_effective_permissions(valid_principal, scope="mkt_amazon_us")
    assert "LISTING_READ" in res_us.actions
    assert "LISTING_PUBLISH" in res_us.actions

    # Evaluación en scope global None -> sólo p_global si la asignación fuera global,
    # pero como la asignación está acotada a mkt_amazon_us, no aplica a scope None.
    res_global = svc.resolve_effective_permissions(valid_principal, scope=None)
    assert len(res_global.actions) == 0


# =============================================================================
# 8. Wrong Scope Denied
# =============================================================================

def test_8_wrong_scope_denied(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    p = svc.create_permission("perm_inv", "inventory.update")
    role = svc.define_role("role_inventory", name="Inventory Role", permissions=[p])

    # Asignado para mercado Chile
    svc.assign_role(identity_id=valid_principal.identity_id, role_id=role.role_id, scope="mkt_chile")

    # Consulta para mercado México -> Denegado
    res_mx = svc.resolve_effective_permissions(valid_principal, scope="mkt_mexico")
    assert len(res_mx.actions) == 0
    assert svc.has_permission(valid_principal, "inventory.update", scope="mkt_mexico") is False

    # Consulta para mercado Chile -> Permitido
    res_cl = svc.resolve_effective_permissions(valid_principal, scope="mkt_chile")
    assert "INVENTORY_UPDATE" in res_cl.actions
    assert svc.has_permission(valid_principal, "inventory.update", scope="mkt_chile") is True


# =============================================================================
# 9. Expired Assignment Ignored
# =============================================================================

def test_9_expired_assignment_ignored(rbac_service_in_memory, valid_principal, virtual_clock):
    svc = rbac_service_in_memory
    p = svc.create_permission("perm_order", "order.manage")
    role = svc.define_role("role_temp_operator", name="Temp Operator", permissions=[p])

    now = virtual_clock.now()
    # Expira en 1 hora
    svc.assign_role(
        identity_id=valid_principal.identity_id,
        role_id=role.role_id,
        expires_at=now + timedelta(hours=1),
    )

    # Actualmente válido
    assert svc.has_permission(valid_principal, "order.manage") is True

    # Avanzar reloj 2 horas (7200 segundos) en VirtualClock (K.7)
    virtual_clock.advance(7200)

    # Ahora debe expirar y retornar 0 permisos
    res_expired = svc.resolve_effective_permissions(valid_principal)
    assert len(res_expired.actions) == 0
    assert svc.has_permission(valid_principal, "order.manage") is False


# =============================================================================
# 10. Idempotent Assignment
# =============================================================================

def test_10_idempotent_assignment(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    p = svc.create_permission("perm_price", "price.update")
    role = svc.define_role("role_pricer_idem", name="Pricer", permissions=[p])

    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    assignment1 = RoleAssignment(
        assignment_id="asgn_idem_001",
        identity_id=valid_principal.identity_id,
        role_id=role.role_id,
        assigned_at=now,
    )

    # Primera inserción
    res1 = svc.assignment_repository.save_assignment(assignment1)
    assert res1.assignment_id == "asgn_idem_001"

    # Replay idéntico exacto -> idempotente
    res2 = svc.assignment_repository.save_assignment(assignment1)
    assert res2.assignment_id == "asgn_idem_001"
    assert res2.checksum == res1.checksum


# =============================================================================
# 11. Duplicate Conflict Handling
# =============================================================================

def test_11_duplicate_conflict_handling(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    p = svc.create_permission("perm_price", "price.update")
    role1 = svc.define_role("role_p1", name="Pricer 1", permissions=[p])
    role2 = svc.define_role("role_p2", name="Pricer 2", permissions=[p])

    now = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)
    assignment1 = RoleAssignment(
        assignment_id="asgn_conflict_001",
        identity_id=valid_principal.identity_id,
        role_id=role1.role_id,
        assigned_at=now,
    )
    svc.assignment_repository.save_assignment(assignment1)

    # Mismo assignment_id pero datos incompatibles (role_id diferente)
    conflicting_assignment = RoleAssignment(
        assignment_id="asgn_conflict_001",
        identity_id=valid_principal.identity_id,
        role_id=role2.role_id,
        assigned_at=now,
    )

    with pytest.raises(RoleAssignmentConflictError, match="already exists with different checksum"):
        svc.assignment_repository.save_assignment(conflicting_assignment)


# =============================================================================
# 12. Deterministic Effective Permission Set
# =============================================================================

def test_12_deterministic_effective_permission_set(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    p1 = svc.create_permission("perm_pub", "listing.publish")
    p2 = svc.create_permission("perm_read", "listing.read")
    p3 = svc.create_permission("perm_price", "price.update")

    # Invertir orden intencionalmente en definición de roles
    role_b = svc.define_role("role_b", name="Role B", permissions=[p3, p1])
    role_a = svc.define_role("role_a", name="Role A", permissions=[p2, p3])

    svc.assign_role(identity_id=valid_principal.identity_id, role_id=role_b.role_id)
    svc.assign_role(identity_id=valid_principal.identity_id, role_id=role_a.role_id)

    res1 = svc.resolve_effective_permissions(valid_principal)
    res2 = svc.resolve_effective_permissions(valid_principal)

    assert res1.checksum == res2.checksum
    assert tuple(res1.permission_ids) == tuple(res2.permission_ids)
    assert tuple(sorted(res1.actions)) == tuple(sorted(res2.actions))


# =============================================================================
# 13. No Privilege Escalation
# =============================================================================

def test_13_no_privilege_escalation(rbac_service_in_memory):
    svc = rbac_service_in_memory
    # 1. Identidad no autenticada no obtiene permisos aunque su token diga ser ADMIN
    attacker_id = IdentityReference(
        identity_id="usr_attacker_001",
        identity_type=IdentityType.EXTERNAL_TOOL,
        canonical_identifier="external_tool:internal:usr_attacker_001",
    )
    auth_failed = AuthenticationResult(
        status=AuthenticationStatus.UNAUTHENTICATED,
        method=AuthenticationMethod.UNKNOWN,
        provider="internal",
        principal=attacker_id,
        authenticated_at=datetime.now(timezone.utc),
        reason_codes=("INVALID_CREDENTIALS",),
    )
    unauthenticated_principal = PrincipalContext(principal=attacker_id, auth_result=auth_failed)

    res = svc.resolve_effective_permissions(unauthenticated_principal)
    assert len(res.actions) == 0
    assert len(res.roles) == 0

    # 2. Identidad desconocida / UNKNOWN no obtiene permisos
    unknown_id = IdentityReference(
        identity_id="usr_unknown_002",
        identity_type=IdentityType.UNKNOWN,
        canonical_identifier="unknown:internal:usr_unknown_002",
    )
    auth_unknown = AuthenticationResult(
        status=AuthenticationStatus.AUTHENTICATED,
        method=AuthenticationMethod.API_CREDENTIAL,
        provider="internal",
        principal=unknown_id,
        authenticated_at=datetime.now(timezone.utc),
        reason_codes=("AUTHENTICATION_SUCCESS",),
    )
    unknown_principal = PrincipalContext(principal=unknown_id, auth_result=auth_unknown)
    res_unk = svc.resolve_effective_permissions(unknown_principal)
    assert len(res_unk.actions) == 0


# =============================================================================
# 14. Sanitized Metadata (K.8)
# =============================================================================

def test_14_sanitized_metadata(rbac_service_in_memory, valid_principal):
    svc = rbac_service_in_memory
    p = svc.create_permission("perm_read", "listing.read")
    role = svc.define_role("role_safe_meta", name="Role Safe", permissions=[p])

    dangerous_meta = {
        "api_key": "secret_key_12345",
        "access_token": "bearer_99999",
        "authorization": "Basic abc",
        "safe_note": "verified operator",
    }

    assignment = svc.assign_role(
        identity_id=valid_principal.identity_id,
        role_id=role.role_id,
        metadata=dangerous_meta,
    )

    assert assignment.metadata["api_key"] == "[REDACTED]"
    assert assignment.metadata["access_token"] == "[REDACTED]"
    assert assignment.metadata["authorization"] == "[REDACTED]"
    assert assignment.metadata["safe_note"] == "verified operator"


# =============================================================================
# 15. RBAC != Authorization
# =============================================================================

def test_15_rbac_is_not_authorization(rbac_service_in_memory, valid_principal):
    """
    N.4 (RBAC) resuelve qué roles y permisos tiene una identidad.
    N.3 (Authorization) evalúa políticas para permitir o denegar una operación en runtime.
    N.4 no retorna ALLOW/DENY de AuthorizationDecision, sino conjuntos inmutables de permisos.
    """
    svc = rbac_service_in_memory
    p = svc.create_permission("perm_pricer", "price.update")
    role = svc.define_role("role_pricer", name="Pricer", permissions=[p])
    svc.assign_role(identity_id=valid_principal.identity_id, role_id=role.role_id)

    eval_result = svc.resolve_effective_permissions(valid_principal)
    assert isinstance(eval_result, RbacEvaluationResult)
    assert isinstance(eval_result.effective_permissions, PermissionSet)
    # No es un objeto AuthorizationDecision de N.3
    assert not hasattr(eval_result, "decision")
    assert not hasattr(eval_result, "reason_code")


# =============================================================================
# 16. No N.5+ Implementation
# =============================================================================

def test_16_no_n5_plus_implementation():
    """
    Garantiza que no se importan ni implementan componentes de N.5 (Secret Management),
    Gate M (Approval Policies) ni Human Override dentro del módulo RBAC.
    """
    import src.domain.rbac.models as rbac_models
    import src.application.rbac.rbac_service as rbac_service_module

    forbidden_terms = [
        "secret_manager",
        "vault",
        "approval_policy",
        "human_override",
        "financial_limit",
        "gate_m",
    ]

    for term in forbidden_terms:
        assert not hasattr(rbac_models, term)
        assert not hasattr(rbac_service_module, term)
