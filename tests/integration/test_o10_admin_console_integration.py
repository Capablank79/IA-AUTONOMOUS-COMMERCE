"""Integración HTTP de O.10 con los contratos reales O.2 y O.6-O.9."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import uuid

import pytest
from starlette.testclient import TestClient

from src.application.admin_console.admin_console_service import AdminConsoleService
from src.application.billing.subscription_service import SubscriptionService
from src.application.organization.organization_service import (
    OrganizationMembershipService,
    OrganizationService,
)
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.domain.organization.models import MembershipRole
from src.domain.plans.models import Plan, PlanFeature, PlanLimits, PlanQuotaTemplate, PlanTier
from src.domain.quota_management.models import (
    QuotaPolicy,
    QuotaRule,
    QuotaScope,
    QuotaType,
    QuotaWindowType,
)
from src.domain.rbac.models import (
    Permission,
    PermissionStatus,
    Role,
    RoleAssignment,
    RoleStatus,
)
from src.domain.reliability.ports import ClockPort
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.tenant.models import TenantContext, TenantScope
from src.domain.usage_metering.models import UsageEvent, UsageRequestStatus
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.billing_repository import (
    InMemoryInvoiceRepository,
    InMemorySubscriptionRepository,
)
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonMembershipRepository,
    JsonOrganizationRepository,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    InMemoryPlanAssignmentRepository,
    InMemoryPlanCatalogRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
    InMemoryQuotaReservationRepository,
)
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleAssignmentRepository,
    JsonRoleRepository,
)
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.usage_event_repository import InMemoryUsageEventRepository
from src.infrastructure.web.admin_app import create_admin_app


class FixedClock(ClockPort):
    def __init__(self):
        self.current = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


ALL_ADMIN_ACTIONS = (
    "TENANT_READ",
    "ORGANIZATION_READ",
    "USER_MEMBERSHIP_MANAGE",
    "PLAN_READ",
    "PLAN_ASSIGN",
    "QUOTA_READ",
    "BILLING_READ",
    "BILLING_MANAGE",
)


def _grant_session(env, tenant_id, identity_id, actions):
    suffix = uuid.uuid4().hex[:8]
    role_id = f"role_{suffix}"
    permissions = tuple(
        Permission(
            permission_id=f"perm_{action.lower()}_{suffix}",
            action=action,
            status=PermissionStatus.ACTIVE,
        )
        for action in actions
    )
    env["role_repo"].save_role(
        Role(role_id=role_id, name=f"Role {suffix}", permissions=permissions, status=RoleStatus.ACTIVE)
    )
    env["assignment_repo"].save_assignment(
        RoleAssignment(
            assignment_id=f"assignment_{suffix}",
            identity_id=identity_id,
            role_id=role_id,
            scope=TenantScope(tenant_id=tenant_id).canonical_scope,
            assigned_at=env["clock"].now(),
        )
    )
    session_id = f"session_{suffix}"
    env["session_repo"].save(
        SaaSSession(
            session_id=session_id,
            identity_id=identity_id,
            tenant_id=tenant_id,
            status=SessionStatus.ACTIVE,
            created_at=env["clock"].now(),
            last_validated_at=env["clock"].now(),
            expires_at=env["clock"].now() + timedelta(hours=2),
        )
    )
    return session_id


def _build_env(storage_dir):
    clock = FixedClock()
    session_repo = JsonSaaSSessionRepository(storage_dir)
    org_repo = JsonOrganizationRepository(storage_dir)
    membership_repo = JsonMembershipRepository(storage_dir)
    role_repo = JsonRoleRepository(storage_dir)
    assignment_repo = JsonRoleAssignmentRepository(storage_dir)
    audit_repo = JsonAuditRepository(storage_dir / "audit")

    rbac_service = RBACService(role_repo, assignment_repo, clock=clock)
    authorization_service = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_service,
        clock=clock,
    )
    organization_service = OrganizationService(org_repo, audit_repository=audit_repo)
    membership_service = OrganizationMembershipService(
        membership_repo, org_repo, audit_repository=audit_repo
    )
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(usage_repo, clock=clock)
    quota_policy_repo = InMemoryQuotaPolicyRepository()
    quota_reservation_repo = InMemoryQuotaReservationRepository()
    quota_service = QuotaManagementService(
        quota_policy_repo, quota_reservation_repo, usage_service, clock=clock
    )
    plan_repo = InMemoryPlanCatalogRepository()
    plan_assignment_repo = InMemoryPlanAssignmentRepository()
    plan_service = PlanEntitlementService(
        plan_repo, plan_assignment_repo, quota_policy_repository=quota_policy_repo, clock=clock
    )
    subscription_repo = InMemorySubscriptionRepository()
    invoice_repo = InMemoryInvoiceRepository()
    subscription_service = SubscriptionService(
        subscription_repo, plan_repo, plan_entitlement_service=plan_service, clock=clock
    )
    service = AdminConsoleService(
        session_repository=session_repo,
        authorization_service=authorization_service,
        organization_service=organization_service,
        membership_service=membership_service,
        usage_metering_service=usage_service,
        quota_management_service=quota_service,
        quota_policy_repository=quota_policy_repo,
        quota_reservation_repository=quota_reservation_repo,
        plan_entitlement_service=plan_service,
        plan_repository=plan_repo,
        plan_assignment_repository=plan_assignment_repo,
        subscription_service=subscription_service,
        subscription_repository=subscription_repo,
        invoice_repository=invoice_repo,
        rbac_service=rbac_service,
        audit_repository=audit_repo,
        clock=clock,
    )
    env = locals()
    env["client"] = TestClient(create_admin_app(service))
    return env


@pytest.fixture
def env(tmp_path):
    result = _build_env(tmp_path / "o10")
    result["admin_session"] = _grant_session(result, "tenant_a", "admin_a", ALL_ADMIN_ACTIONS)
    return result


def _auth(session_id):
    return {"Authorization": f"Bearer {session_id}"}


def test_scenario_a_admin_autorizado_consulta_tenant_permitido(env):
    response = env["client"].get(
        "/api/admin/tenants/tenant_a/summary", headers=_auth(env["admin_session"])
    )
    assert response.status_code == 200
    assert response.json()["tenant_id"] == "tenant_a"


def test_scenario_b_admin_tenant_a_no_accede_tenant_b(env):
    response = env["client"].get(
        "/api/admin/tenants/tenant_b/summary", headers=_auth(env["admin_session"])
    )
    assert response.status_code == 403
    assert response.json()["error"] == "unauthorized"


def test_scenario_c_mutacion_membership_delega_en_o2(env):
    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    org = env["organization_service"].create_organization(context, "org_ops", "Operations")
    env["membership_service"].add_membership(
        context, org.organization_id, "admin_a", MembershipRole.ADMIN
    )

    response = env["client"].post(
        f"/api/admin/tenants/tenant_a/organizations/{org.organization_id}/memberships",
        headers=_auth(env["admin_session"]),
        json={"identity_id": "member_1", "role": "MEMBER"},
    )
    assert response.status_code == 201
    members = env["membership_service"].list_organization_members(context, org.organization_id)
    assert {(member.identity_id, member.role) for member in members} == {
        ("admin_a", MembershipRole.ADMIN),
        ("member_1", MembershipRole.MEMBER),
    }

    deleted = env["client"].delete(
        f"/api/admin/tenants/tenant_a/organizations/{org.organization_id}/memberships/member_1",
        headers=_auth(env["admin_session"]),
    )
    assert deleted.status_code == 200
    remaining = env["membership_service"].list_organization_members(context, org.organization_id)
    assert [member.identity_id for member in remaining] == ["admin_a"]


def test_scenario_d_cambio_plan_consistente_con_o8_y_o9(env):
    env["plan_repo"].save_plan(
        Plan(
            plan_id="plan_pro",
            name="Pro",
            tier=PlanTier.PRO,
            limits=PlanLimits(max_users=20),
            features=(PlanFeature.MODEL_INFERENCE,),
            metadata={"base_price": "49.00", "currency": "USD"},
        )
    )
    assigned = env["client"].post(
        "/api/admin/tenants/tenant_a/plan/assign",
        headers=_auth(env["admin_session"]),
        json={"plan_id": "plan_pro", "reason": "integration upgrade"},
    )
    assert assigned.status_code == 200
    assert assigned.json()["plan_id"] == "plan_pro"

    subscribed = env["client"].post(
        "/api/admin/tenants/tenant_a/subscriptions",
        headers=_auth(env["admin_session"]),
        json={"plan_id": "plan_pro", "billing_cycle": "MONTHLY", "auto_activate": True},
    )
    assert subscribed.status_code == 201
    assert subscribed.json()["plan_id"] == "plan_pro"
    assert env["plan_service"].get_active_plan("tenant_a").plan_id == "plan_pro"
    assert env["subscription_service"].get_active_subscription("tenant_a").plan_id == "plan_pro"


def test_scenario_e_quota_y_usage_reflejan_o7_y_o6_exactamente(env):
    env["quota_policy_repo"].save_policy(
        QuotaPolicy(
            policy_id="policy_a",
            tenant_id="tenant_a",
            rules=(QuotaRule(
                rule_id="requests_month",
                quota_type=QuotaType.MAX_REQUESTS,
                limit_value=321,
                scope=QuotaScope.TENANT,
                window_type=QuotaWindowType.MONTH,
            ),),
        )
    )
    env["usage_service"].record_usage_event(
        TenantContext(tenant_id="tenant_a", identity_id="user_a"),
        UsageEvent(
            usage_event_id="usage_a",
            tenant_id="tenant_a",
            identity_id="user_a",
            occurred_at=env["clock"].now() - timedelta(minutes=5),
            model="gpt-4o",
            provider="openai",
            request_status=UsageRequestStatus.SUCCESS,
            input_tokens=120,
            output_tokens=30,
            estimated_cost=Decimal("0.012"),
            actual_cost=Decimal("0.010"),
        ),
    )

    quota = env["client"].get(
        "/api/admin/tenants/tenant_a/quota", headers=_auth(env["admin_session"])
    )
    usage = env["client"].get(
        "/api/admin/tenants/tenant_a/usage", headers=_auth(env["admin_session"])
    )
    assert quota.status_code == usage.status_code == 200
    assert quota.json()["rules_summary"] == [{
        "quota_type": "MAX_REQUESTS",
        "scope": "TENANT",
        "limit_value": "321",
        "window_type": "MONTH",
    }]
    assert usage.json()["total_requests"] == 1
    assert usage.json()["input_tokens"] == 120
    assert usage.json()["output_tokens"] == 30
    assert usage.json()["total_tokens"] == 150
    assert usage.json()["actual_cost"] == "0.010"


def test_scenario_f_billing_es_una_proyeccion_segura(env):
    env["plan_repo"].save_plan(
        Plan(
            plan_id="plan_safe",
            name="Safe",
            tier=PlanTier.PRO,
            metadata={"base_price": "25.00", "currency": "USD"},
        )
    )
    env["subscription_service"].create_subscription(
        tenant_id="tenant_a", plan_id="plan_safe", auto_activate=True
    )
    response = env["client"].get(
        "/api/admin/tenants/tenant_a/billing", headers=_auth(env["admin_session"])
    )
    assert response.status_code == 200
    assert response.json()["plan_id"] == "plan_safe"
    serialized = response.text.lower()
    for forbidden in ("card_number", "password", "secret", "cvv", '"pan"'):
        assert forbidden not in serialized


def test_scenario_g_usuario_autenticado_no_admin_es_bloqueado(env):
    session = _grant_session(env, "tenant_a", "ordinary_user", ("LISTING_READ",))
    response = env["client"].get(
        "/api/admin/tenants/tenant_a/summary", headers=_auth(session)
    )
    assert response.status_code == 403
    assert response.json()["error"] == "unauthorized"


def test_scenario_h_pii_de_membership_es_redactada(env):
    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    org = env["organization_service"].create_organization(context, "org_pii", "PII")
    env["membership_service"].add_membership(
        context, org.organization_id, "admin_a", MembershipRole.ADMIN
    )
    env["membership_service"].add_membership(
        context, org.organization_id, "alice.private@example.com", MembershipRole.MEMBER
    )
    response = env["client"].get(
        f"/api/admin/tenants/tenant_a/organizations/{org.organization_id}/memberships",
        headers=_auth(env["admin_session"]),
    )
    assert response.status_code == 200
    masked = next(
        member["identity_id_masked"]
        for member in response.json()
        if "@example.com" in member["identity_id_masked"]
    )
    assert masked == "a***e@example.com"
    assert "alice.private" not in response.text


def test_scenario_i_estado_o2_y_acceso_admin_persisten_tras_restart(tmp_path):
    storage = tmp_path / "restart"
    before = _build_env(storage)
    session = _grant_session(before, "tenant_restart", "admin_restart", ALL_ADMIN_ACTIONS)
    context = TenantContext(tenant_id="tenant_restart", identity_id="admin_restart")
    org = before["organization_service"].create_organization(context, "org_restart", "Restart Org")
    before["membership_service"].add_membership(
        context, org.organization_id, "admin_restart", MembershipRole.ADMIN
    )
    before["membership_service"].add_membership(
        context, org.organization_id, "persisted@example.com", MembershipRole.MEMBER
    )

    after = _build_env(storage)
    response = after["client"].get(
        f"/api/admin/tenants/tenant_restart/organizations/{org.organization_id}/memberships",
        headers=_auth(session),
    )
    assert response.status_code == 200
    assert any(
        member["identity_id_masked"] == "p***d@example.com"
        for member in response.json()
    )
    assert len(after["membership_service"].list_organization_members(context, org.organization_id)) == 2


def test_scenario_j_sin_sesion_se_deniega_antes_del_servicio(env):
    response = env["client"].get("/api/admin/tenants/tenant_a/summary")
    assert response.status_code == 401
    assert response.json() == {
        "error": "unauthenticated",
        "message": "Missing session identifier.",
    }
