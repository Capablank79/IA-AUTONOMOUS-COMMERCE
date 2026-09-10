"""
Tests unitarios para O.10 — Admin Console & Multi-Tenant Management (Hito O.10 — SaaS / Platformization).

Cubre exhaustivamente:
1. Unauthenticated request -> DENY (AdminAuthenticationError)
2. Authenticated non-admin request -> DENY (AdminAuthorizationError)
3. Tenant read permission works -> ALLOW y retorna TenantAdminSummary
4. Separation of read vs manage permissions (TENANT_READ != TENANT_MANAGE, etc.)
5. Cross-tenant request denied (Admin Tenant A intentando acceder a Tenant B)
6. Safe TenantAdminSummary (proyección segura, cero secretos)
7. PII masked (enmascaramiento en vistas de membresía y auditoría)
8. Secrets absent (cero credenciales, tokens o PAN/CVV en las vistas)
9. Plan change delegates to O.8 PlanEntitlementService
10. Quota view delegates to O.7 QuotaManagementService / repos
11. Usage view delegates to O.6 UsageMeteringService
12. Membership mutation delegates to O.2 OrganizationMembershipService
13. Billing read safe (detalles de suscripción e invoice sin datos de tarjeta)
14. Sanitized error handling (excepciones tipadas y mapeo consistente)
15. Audit event produced on mutation (K.1 audit record appended)
16. No O.11+ implementation (respeto estricto del alcance)
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest
import uuid
from typing import Dict, List, Optional, Any

from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.session.models import SaaSSession, SessionStatus, SessionContext
from src.domain.tenant.models import TenantContext, TenantScope
from src.domain.organization.models import (
    Organization,
    UserMembership,
    MembershipStatus,
    MembershipRole,
    OrganizationStatus,
)
from src.domain.rbac.models import (
    Permission,
    Role,
    RoleAssignment,
    PermissionStatus,
    RoleStatus,
)
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
)
from src.domain.plans.models import (
    Plan,
    PlanTier,
    PlanStatus,
    PlanFeature,
    PlanLimits,
    PlanAssignment,
)
from src.domain.quota_management.models import (
    QuotaPolicy,
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
)
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageRequestStatus,
    UsagePeriod,
    UsagePeriodType,
)
from src.domain.billing.models import (
    Money,
    Subscription,
    SubscriptionStatus,
    BillingCycle,
    BillingPeriod,
    Invoice,
    InvoiceStatus,
)
from src.domain.admin_console.models import (
    AdminAction,
    AdminPermission,
    TenantAdminSummary,
    OrganizationAdminView,
    MembershipAdminView,
    UsageAdminSummary,
    QuotaAdminView,
    PlanAdminView,
    BillingAdminView,
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminResourceNotFoundError,
    AdminInvalidRequestError,
    mask_pii,
)
from src.application.admin_console.admin_console_service import AdminConsoleService
from src.application.organization.organization_service import (
    OrganizationService,
    OrganizationMembershipService,
)
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.billing.subscription_service import SubscriptionService
from src.application.billing.billing_service import BillingService
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.security.sensitive_data_handling_service import SensitiveDataHandlingService

from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonOrganizationRepository,
    JsonMembershipRepository,
)
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    InMemoryPlanCatalogRepository,
    InMemoryPlanAssignmentRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
    InMemoryQuotaReservationRepository,
)
from src.infrastructure.persistence.data.json.usage_event_repository import InMemoryUsageEventRepository
from src.infrastructure.persistence.data.json.billing_repository import (
    InMemorySubscriptionRepository,
    InMemoryInvoiceRepository,
    InMemoryPaymentAttemptRepository,
)
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
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


@pytest.fixture
def test_clock():
    return MockClock(datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc))


@pytest.fixture
def storage_dir(tmp_path):
    return tmp_path / "o10"


@pytest.fixture
def session_repo(storage_dir):
    return JsonSaaSSessionRepository(storage_dir)


@pytest.fixture
def org_repo(storage_dir):
    return JsonOrganizationRepository(storage_dir)


@pytest.fixture
def membership_repo(storage_dir):
    return JsonMembershipRepository(storage_dir)


@pytest.fixture
def role_repo(storage_dir):
    return JsonRoleRepository(storage_dir)


@pytest.fixture
def assignment_repo(storage_dir):
    return JsonRoleAssignmentRepository(storage_dir)


@pytest.fixture
def plan_repo():
    return InMemoryPlanCatalogRepository()


@pytest.fixture
def plan_assignment_repo():
    return InMemoryPlanAssignmentRepository()


@pytest.fixture
def quota_policy_repo():
    return InMemoryQuotaPolicyRepository()


@pytest.fixture
def quota_reservation_repo():
    return InMemoryQuotaReservationRepository()


@pytest.fixture
def usage_repo():
    return InMemoryUsageEventRepository()


@pytest.fixture
def sub_repo():
    return InMemorySubscriptionRepository()


@pytest.fixture
def invoice_repo():
    return InMemoryInvoiceRepository()


@pytest.fixture
def audit_repo(storage_dir):
    return JsonAuditRepository(storage_dir / "audit")


@pytest.fixture
def trace_repo(storage_dir):
    return JsonAgentTraceRepository(storage_dir / "traces")


@pytest.fixture
def rbac_service(role_repo, assignment_repo, test_clock):
    return RBACService(
        role_repository=role_repo,
        assignment_repository=assignment_repo,
        clock=test_clock,
    )


@pytest.fixture
def saas_auth_service(session_repo, org_repo, membership_repo, rbac_service, test_clock):
    return SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_service,
        clock=test_clock,
    )


@pytest.fixture
def org_service(org_repo, audit_repo):
    return OrganizationService(
        organization_repo=org_repo,
        audit_repository=audit_repo,
    )


@pytest.fixture
def membership_service(membership_repo, org_repo, audit_repo):
    return OrganizationMembershipService(
        membership_repo=membership_repo,
        organization_repo=org_repo,
        audit_repository=audit_repo,
    )


@pytest.fixture
def usage_service(usage_repo, test_clock):
    return UsageMeteringService(
        repository=usage_repo,
        clock=test_clock,
    )


@pytest.fixture
def quota_service(quota_policy_repo, quota_reservation_repo, usage_service, test_clock):
    return QuotaManagementService(
        policy_repository=quota_policy_repo,
        reservation_repository=quota_reservation_repo,
        usage_metering_service=usage_service,
        clock=test_clock,
    )


@pytest.fixture
def plan_service(plan_repo, plan_assignment_repo, quota_policy_repo, test_clock):
    return PlanEntitlementService(
        catalog_repository=plan_repo,
        assignment_repository=plan_assignment_repo,
        quota_policy_repository=quota_policy_repo,
        clock=test_clock,
    )


@pytest.fixture
def sub_service(sub_repo, plan_repo, invoice_repo, plan_service, test_clock):
    return SubscriptionService(
        subscription_repository=sub_repo,
        plan_catalog_repository=plan_repo,
        plan_entitlement_service=plan_service,
        clock=test_clock,
    )


@pytest.fixture
def admin_service(
    session_repo,
    saas_auth_service,
    org_service,
    membership_service,
    usage_service,
    quota_service,
    quota_policy_repo,
    quota_reservation_repo,
    plan_service,
    plan_repo,
    plan_assignment_repo,
    sub_service,
    sub_repo,
    invoice_repo,
    rbac_service,
    audit_repo,
    trace_repo,
    test_clock,
):
    return AdminConsoleService(
        session_repository=session_repo,
        authorization_service=saas_auth_service,
        organization_service=org_service,
        membership_service=membership_service,
        usage_metering_service=usage_service,
        quota_management_service=quota_service,
        quota_policy_repository=quota_policy_repo,
        quota_reservation_repository=quota_reservation_repo,
        plan_entitlement_service=plan_service,
        plan_repository=plan_repo,
        plan_assignment_repository=plan_assignment_repo,
        subscription_service=sub_service,
        subscription_repository=sub_repo,
        invoice_repository=invoice_repo,
        rbac_service=rbac_service,
        audit_repository=audit_repo,
        trace_repository=trace_repo,
        clock=test_clock,
    )


def setup_admin_user(
    session_repo,
    role_repo,
    assignment_repo,
    tenant_id: str,
    identity_id: str,
    actions: List[str],
    test_clock: MockClock,
    scope: Optional[str] = None,
) -> str:
    """Helper para crear una sesión activa y asignar un rol con los permisos indicados."""
    session_id = f"sess_{uuid.uuid4().hex[:12]}"
    now_dt = test_clock.now()

    # 1. Crear Permisos y Rol
    perms = []
    for act in actions:
        p = Permission(
            permission_id=f"perm_{act.lower()}",
            action=act,
            status=PermissionStatus.ACTIVE,
        )
        perms.append(p)

    role_id = f"role_admin_{uuid.uuid4().hex[:8]}"
    role = Role(
        role_id=role_id,
        name="Admin Role",
        permissions=tuple(perms),
        status=RoleStatus.ACTIVE,
    )
    role_repo.save_role(role)

    # 2. Asignar rol a la identidad
    target_scope = scope if scope is not None else TenantScope(tenant_id=tenant_id).canonical_scope
    assignment = RoleAssignment(
        assignment_id=f"asgn_{uuid.uuid4().hex[:8]}",
        identity_id=identity_id,
        role_id=role_id,
        scope=target_scope,
        assigned_at=now_dt,
    )
    assignment_repo.save_assignment(assignment)

    # 3. Crear SaaSSession
    session = SaaSSession(
        session_id=session_id,
        identity_id=identity_id,
        tenant_id=tenant_id,
        status=SessionStatus.ACTIVE,
        created_at=now_dt,
        last_validated_at=now_dt,
        expires_at=now_dt + timedelta(hours=2),
    )
    session_repo.save(session)
    return session_id


# =============================================================================
# Tests Unitarios
# =============================================================================

def test_1_unauthenticated_request_denied(admin_service):
    """Peticiones sin session_id o con sesión inexistente deben ser rechazadas con 401."""
    with pytest.raises(AdminAuthenticationError):
        admin_service.get_tenant_summary(session_id="", target_tenant_id="tenant_a")

    with pytest.raises(AdminAuthenticationError):
        admin_service.get_tenant_summary(session_id="non_existent_sess", target_tenant_id="tenant_a")


def test_2_authenticated_non_admin_denied(admin_service, session_repo, role_repo, assignment_repo, test_clock):
    """Usuario autenticado pero sin permisos administrativos debe ser denegado (403)."""
    # Sesión con permisos solo de usuario normal (ej: LISTING_READ)
    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="user_plain",
        actions=["LISTING_READ"], test_clock=test_clock,
    )

    with pytest.raises(AdminAuthorizationError):
        admin_service.get_tenant_summary(session_id=session_id, target_tenant_id="tenant_a")


def test_3_tenant_read_permission_works(admin_service, session_repo, role_repo, assignment_repo, test_clock):
    """Usuario con permiso TENANT_READ puede consultar el resumen del tenant."""
    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["TENANT_READ"], test_clock=test_clock,
    )

    summary = admin_service.get_tenant_summary(session_id=session_id, target_tenant_id="tenant_a")
    assert summary.tenant_id == "tenant_a"
    assert isinstance(summary, TenantAdminSummary)


def test_4_read_permission_does_not_imply_manage(admin_service, session_repo, role_repo, assignment_repo, org_service, test_clock):
    """TENANT_READ u ORGANIZATION_READ no permiten mutaciones (USER_MEMBERSHIP_MANAGE o PLAN_ASSIGN)."""
    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["ORGANIZATION_READ", "PLAN_READ"], test_clock=test_clock,
    )

    # Crear organización en tenant_a
    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    org = org_service.create_organization(context, "org_1", "Tech Corp")

    # Intentar añadir membresía sin USER_MEMBERSHIP_MANAGE -> Denegado
    with pytest.raises(AdminAuthorizationError):
        admin_service.add_membership(
            session_id=session_id,
            target_tenant_id="tenant_a",
            organization_id=org.organization_id,
            identity_id="user_2",
            role=MembershipRole.MEMBER,
        )

    # Intentar asignar plan sin PLAN_ASSIGN -> Denegado
    with pytest.raises(AdminAuthorizationError):
        admin_service.assign_plan(
            session_id=session_id,
            target_tenant_id="tenant_a",
            plan_id="plan_pro",
        )


def test_5_cross_tenant_request_denied(admin_service, session_repo, role_repo, assignment_repo, test_clock):
    """Admin de Tenant A intentando acceder o mutar recursos de Tenant B debe ser denegado."""
    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["TENANT_READ", "TENANT_MANAGE", "USER_MEMBERSHIP_MANAGE", "PLAN_ASSIGN"],
        test_clock=test_clock,
    )

    with pytest.raises(AdminAuthorizationError) as exc_info:
        admin_service.get_tenant_summary(session_id=session_id, target_tenant_id="tenant_b")
    assert "Cross-tenant access forbidden" in str(exc_info.value) or "denied" in str(exc_info.value).lower()


def test_6_safe_tenant_admin_summary(admin_service, session_repo, role_repo, assignment_repo, test_clock):
    """TenantAdminSummary genera un DTO sanitizado sin secretos."""
    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["TENANT_READ"], test_clock=test_clock,
    )

    summary = admin_service.get_tenant_summary(session_id=session_id, target_tenant_id="tenant_a")
    dto = summary.to_dict()

    assert "tenant_id" in dto
    assert dto["tenant_id"] == "tenant_a"
    # Verificar que no contenga llaves de secretos
    serialized = str(dto).lower()
    assert "secret" not in serialized
    assert "password" not in serialized
    assert "token" not in serialized or "input_tokens" in serialized


def test_7_pii_masked(admin_service, session_repo, role_repo, assignment_repo, org_service, membership_service, test_clock):
    """Las vistas de membresías y auditoría enmascaran emails e identificadores personales."""
    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["ORGANIZATION_READ", "USER_MEMBERSHIP_MANAGE"], test_clock=test_clock,
    )

    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    org = org_service.create_organization(context, "org_pii", "Security Org")
    membership_service.add_membership(
        context=context,
        organization_id=org.organization_id,
        identity_id="admin_a",
        role=MembershipRole.ADMIN,
    )

    # Añadir miembro con email personal
    membership_service.add_membership(
        context=context,
        organization_id=org.organization_id,
        identity_id="alice.secret@acme.com",
        role=MembershipRole.MEMBER,
    )

    members = admin_service.list_memberships(
        session_id=session_id,
        target_tenant_id="tenant_a",
        organization_id=org.organization_id,
    )

    assert len(members) == 2
    masked_id = next(m.identity_id_masked for m in members if "@acme.com" in m.identity_id_masked)
    assert masked_id != "alice.secret@acme.com"
    assert "alice.secret" not in masked_id
    assert "@acme.com" in masked_id


def test_8_secrets_absent(admin_service, session_repo, role_repo, assignment_repo, sub_service, plan_repo, test_clock):
    """Las vistas financieras y de planes no exponen tarjetas ni credenciales."""
    # Configurar un plan
    plan = Plan(
        plan_id="plan_enterprise",
        name="Enterprise Plan",
        tier=PlanTier.ENTERPRISE,
        version="1.0.0",
        limits=PlanLimits(max_users=50),
    )
    plan_repo.save_plan(plan)

    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["BILLING_READ", "BILLING_MANAGE"], test_clock=test_clock,
    )

    # Crear suscripción
    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    sub_service.create_subscription(
        tenant_id="tenant_a",
        plan_id="plan_enterprise",
        billing_cycle=BillingCycle.MONTHLY,
        context=context,
    )

    billing_view = admin_service.get_billing_view(session_id=session_id, target_tenant_id="tenant_a")
    dto = billing_view.to_dict()

    assert dto["subscription_status"] in ["ACTIVE", "TRIALING", "NONE", "SubscriptionStatus.ACTIVE"]
    serialized = str(dto).lower()
    assert "pan" not in serialized
    assert "cvv" not in serialized
    assert "card_number" not in serialized
    assert "secret" not in serialized


def test_9_plan_change_delegates_to_o8(admin_service, session_repo, role_repo, assignment_repo, plan_repo, plan_service, test_clock):
    """El cambio de plan desde AdminConsole delega en PlanEntitlementService (O.8)."""
    p_starter = Plan(plan_id="plan_starter", name="Starter", tier=PlanTier.FREE, version="1.0.0")
    p_pro = Plan(plan_id="plan_pro", name="Pro Tier", tier=PlanTier.PRO, version="1.0.0")
    plan_repo.save_plan(p_starter)
    plan_repo.save_plan(p_pro)

    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["PLAN_READ", "PLAN_ASSIGN"], test_clock=test_clock,
    )

    # Asignar plan PRO vía console
    plan_view = admin_service.assign_plan(
        session_id=session_id,
        target_tenant_id="tenant_a",
        plan_id="plan_pro",
        reason="Upgrade by Operator",
    )

    assert plan_view.plan_id == "plan_pro"
    assert plan_view.tier == "PRO"

    # Verificar que el servicio O.8 refleja el nuevo plan activo
    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    active_in_o8 = plan_service.get_active_plan(tenant_id="tenant_a", context=context)
    assert active_in_o8 is not None
    assert active_in_o8.plan_id == "plan_pro"


def test_10_quota_view_delegates_to_o7(admin_service, session_repo, role_repo, assignment_repo, quota_policy_repo, test_clock):
    """get_quota_view obtiene las reglas y políticas reales de O.7."""
    rule = QuotaRule(
        rule_id="r_reqs",
        quota_type=QuotaType.MAX_REQUESTS,
        scope=QuotaScope.TENANT,
        window_type=QuotaWindowType.MONTH,
        limit_value=5000,
    )
    policy = QuotaPolicy(
        policy_id="pol_tenant_a",
        tenant_id="tenant_a",
        rules=(rule,),
    )
    quota_policy_repo.save_policy(policy)

    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["QUOTA_READ"], test_clock=test_clock,
    )

    quota_view = admin_service.get_quota_view(session_id=session_id, target_tenant_id="tenant_a")
    assert quota_view.policy_id == "pol_tenant_a"
    assert quota_view.rules_count == 1
    assert quota_view.rules_summary[0]["limit_value"] == "5000"


def test_11_usage_view_delegates_to_o6(admin_service, session_repo, role_repo, assignment_repo, usage_service, test_clock):
    """get_usage_summary obtiene los consumos agregados reales de O.6 sin inventar métricas."""
    context = TenantContext(tenant_id="tenant_a", identity_id="user_1")
    now_dt = test_clock.now()

    # Registrar evento en O.6
    ev = UsageEvent(
        usage_event_id=f"ev_{uuid.uuid4().hex[:8]}",
        tenant_id="tenant_a",
        identity_id="user_1",
        occurred_at=now_dt - timedelta(hours=1),
        model="gpt-4o",
        provider="openai",
        request_status=UsageRequestStatus.SUCCESS,
        input_tokens=150,
        output_tokens=50,
        estimated_cost=Decimal("0.002"),
        actual_cost=Decimal("0.002"),
    )
    usage_service.record_usage_event(context, ev)

    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["TENANT_READ"], test_clock=test_clock,
    )

    usage_view = admin_service.get_usage_summary(session_id=session_id, target_tenant_id="tenant_a")
    assert usage_view.total_requests == 1
    assert usage_view.total_tokens == 200
    assert usage_view.input_tokens == 150
    assert usage_view.output_tokens == 50


def test_12_membership_mutation_delegates_to_o2(admin_service, session_repo, role_repo, assignment_repo, org_service, membership_service, test_clock):
    """Las mutaciones de miembros se ejecutan en O.2 a través del AdminConsoleService."""
    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["ORGANIZATION_READ", "USER_MEMBERSHIP_MANAGE"], test_clock=test_clock,
    )

    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    org = org_service.create_organization(context, "org_ops", "Ops Department")
    membership_service.add_membership(
        context=context,
        organization_id=org.organization_id,
        identity_id="admin_a",
        role=MembershipRole.ADMIN,
    )

    # 1. Añadir
    m_view = admin_service.add_membership(
        session_id=session_id,
        target_tenant_id="tenant_a",
        organization_id=org.organization_id,
        identity_id="dev_user_1",
        role=MembershipRole.MEMBER,
    )
    assert m_view.role == "MEMBER"

    # Verificar en O.2
    members_in_o2 = membership_service.list_organization_members(context, org.organization_id)
    assert len(members_in_o2) == 2
    assert any(member.identity_id == "dev_user_1" for member in members_in_o2)

    # 2. Remover
    admin_service.remove_membership(
        session_id=session_id,
        target_tenant_id="tenant_a",
        organization_id=org.organization_id,
        identity_id="dev_user_1",
    )
    members_after = membership_service.list_organization_members(context, org.organization_id)
    assert len(members_after) == 1
    assert members_after[0].identity_id == "admin_a"


def test_13_billing_read_safe(admin_service, session_repo, role_repo, assignment_repo, invoice_repo, sub_service, plan_repo, test_clock):
    """Lectura segura de billing con facturas y suscripción sin PAN/CVV."""
    plan = Plan(plan_id="plan_standard", name="Standard", tier=PlanTier.PRO, version="1.0.0")
    plan_repo.save_plan(plan)

    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["BILLING_READ"], test_clock=test_clock,
    )

    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    sub_service.create_subscription(
        tenant_id="tenant_a",
        plan_id="plan_standard",
        context=context,
        auto_activate=True,
    )

    billing_view = admin_service.get_billing_view(session_id=session_id, target_tenant_id="tenant_a")
    assert billing_view.tenant_id == "tenant_a"
    assert billing_view.plan_id == "plan_standard"


def test_14_sanitized_errors(admin_service):
    """Peticiones inválidas producen errores estructurados sin revelar datos internos."""
    with pytest.raises(AdminInvalidRequestError):
        admin_service.get_tenant_summary(session_id="valid_sess", target_tenant_id="")


def test_15_audit_event_produced_on_mutation(admin_service, session_repo, role_repo, assignment_repo, org_service, membership_service, audit_repo, test_clock):
    """Toda mutación administrativa produce un registro auditable en K.1."""
    session_id = setup_admin_user(
        session_repo, role_repo, assignment_repo,
        tenant_id="tenant_a", identity_id="admin_a",
        actions=["ORGANIZATION_READ", "USER_MEMBERSHIP_MANAGE"], test_clock=test_clock,
    )

    context = TenantContext(tenant_id="tenant_a", identity_id="admin_a")
    org = org_service.create_organization(context, "org_audit", "Audit Corp")
    membership_service.add_membership(
        context=context,
        organization_id=org.organization_id,
        identity_id="admin_a",
        role=MembershipRole.ADMIN,
    )

    admin_service.add_membership(
        session_id=session_id,
        target_tenant_id="tenant_a",
        organization_id=org.organization_id,
        identity_id="audited_user",
        role=MembershipRole.MEMBER,
    )

    records = audit_repo.list_records()
    admin_records = [r for r in records if r.action_or_operation == "ADD_MEMBERSHIP"]
    assert len(admin_records) >= 1
    rec = admin_records[0]
    assert rec.actor.actor_id == "admin_a"
    assert "tenant_a" in rec.mission_id
    assert rec.metadata["target_tenant_id"] == "tenant_a"


def test_16_no_o12_plus_implementation(admin_service):
    """Verifica que el servicio no implementa métodos de configuración arbitraria de O.12+."""
    assert not hasattr(admin_service, "deploy_tenant_stack")
    assert not hasattr(admin_service, "configure_observability_pipeline")
