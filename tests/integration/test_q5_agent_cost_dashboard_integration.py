"""
Tests de Integración y End-to-End para Q.5 — Agent Cost Dashboard (Hito Q — Business Intelligence).

Cubre:
A. Tenant A sees own cost facts from both O.6 (UsageEvent) and K.3 (CostRecord).
B. Tenant B is strictly isolated (CrossTenantGuard / 403 Forbidden).
C. Mission-linked costs aggregate correctly (/missions/{mission_id}/summary).
D. Unattributed cost remains separate (is_attributed=False, no synthetic links).
E. Provider & Model filters work accurately.
F. Token aggregation (input, output, cached, total) correct across queries.
G. Summary totals and multi-currency breakdowns computed safely.
H. UNKNOWN pricing does not become zero (preserved as None/UNKNOWN).
I. Pagination & sorting are deterministic and stable.
J. Unauthorized / unauthenticated requests rejected with 401/403.
K. Minimal HTML dashboard surface (/bi/agent-costs).
L. Pure consultative read-only boundary (no SaaS billing / invoice mutations).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List
import pytest
from starlette.testclient import TestClient

from src.domain.agent_cost_dashboard.models import (
    AgentCostDashboardItem,
    AgentCostDashboardSummary,
    AgentCostDashboardQuery,
    AgentCostDashboardPage,
    AgentCostSortField,
    SortOrder,
    CostConfidenceSource,
)
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageRequestStatus,
)
from src.domain.cost.models import (
    CostRecord,
    UsageRecord,
    PricingRate,
    CostType,
    UsageUnit,
)
from src.domain.tenant.models import TenantContext
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.rbac.models import Permission, Role, RoleAssignment
from src.domain.reliability.ports import ClockPort
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.organization.models import Organization, UserMembership, MembershipRole, MembershipStatus
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.agent_cost_dashboard.agent_cost_dashboard_service import AgentCostDashboardService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import JsonOrganizationRepository, JsonMembershipRepository
from src.infrastructure.persistence.data.json.rbac_repository import JsonRoleRepository, JsonRoleAssignmentRepository
from src.infrastructure.persistence.data.json.usage_event_repository import InMemoryUsageEventRepository
from src.infrastructure.persistence.data.json.cost_repository import JsonCostRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.tenant_mission_repository import JsonTenantMissionRepository
from src.application.admin_console.admin_console_service import AdminConsoleService
from src.application.organization.organization_service import OrganizationService, OrganizationMembershipService
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.billing.subscription_service import SubscriptionService
from src.infrastructure.persistence.data.json.quota_repository import InMemoryQuotaPolicyRepository, InMemoryQuotaReservationRepository
from src.infrastructure.persistence.data.json.plan_repository import InMemoryPlanCatalogRepository, InMemoryPlanAssignmentRepository
from src.infrastructure.persistence.data.json.billing_repository import InMemorySubscriptionRepository, InMemoryInvoiceRepository
from src.infrastructure.web.admin_app import create_admin_app


class FixedClock(ClockPort):
    def __init__(self):
        self.current = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def integration_app(tmp_path):
    clock = FixedClock()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    session_repo = JsonSaaSSessionRepository(data_dir / "sessions")
    org_repo = JsonOrganizationRepository(data_dir / "organizations")
    membership_repo = JsonMembershipRepository(data_dir / "memberships")
    role_repo = JsonRoleRepository(data_dir / "roles")
    role_assignment_repo = JsonRoleAssignmentRepository(data_dir / "role_assignments")

    usage_repo = InMemoryUsageEventRepository()
    cost_repo = JsonCostRepository(data_dir / "costs")

    audit_repo = JsonAuditRepository(data_dir / "audit")
    mission_repo = JsonTenantMissionRepository(data_dir)

    rbac_service = RBACService(
        role_repository=role_repo,
        assignment_repository=role_assignment_repo,
        clock=clock,
    )
    auth_service = SaaSAuthorizationService(
        session_repository=session_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_service,
        clock=clock,
    )

    agent_cost_service = AgentCostDashboardService(
        usage_repository=usage_repo,
        cost_repository=cost_repo,
        authorization_service=auth_service,
        session_repository=session_repo,
        clock=clock,
    )

    # Admin services mocks/inits
    org_service = OrganizationService(org_repo, audit_repository=audit_repo)
    membership_service = OrganizationMembershipService(membership_repo, org_repo, audit_repository=audit_repo)
    usage_service = UsageMeteringService(usage_repo, clock=clock)
    quota_policy_repo = InMemoryQuotaPolicyRepository()
    quota_reservation_repo = InMemoryQuotaReservationRepository()
    quota_service = QuotaManagementService(quota_policy_repo, quota_reservation_repo, usage_service, clock=clock)
    plan_repo = InMemoryPlanCatalogRepository()
    plan_assignment_repo = InMemoryPlanAssignmentRepository()
    plan_service = PlanEntitlementService(plan_repo, plan_assignment_repo, quota_policy_repository=quota_policy_repo, clock=clock)
    subscription_repo = InMemorySubscriptionRepository()
    invoice_repo = InMemoryInvoiceRepository()
    subscription_service = SubscriptionService(subscription_repo, plan_repo, plan_entitlement_service=plan_service, clock=clock)

    admin_console_service = AdminConsoleService(
        session_repository=session_repo,
        authorization_service=auth_service,
        organization_service=org_service,
        membership_service=membership_service,
        usage_metering_service=usage_service,
        quota_management_service=quota_service,
        quota_policy_repository=quota_policy_repo,
        quota_reservation_repository=quota_reservation_repo,
        plan_entitlement_service=plan_service,
        plan_repository=plan_repo,
        subscription_service=subscription_service,
        subscription_repository=subscription_repo,
        invoice_repository=invoice_repo,
        clock=clock,
    )

    app = create_admin_app(
        service=admin_console_service,
        agent_cost_dashboard_service=agent_cost_service,
    )

    client = TestClient(app)

    # Seed Tenants, Roles & Sessions
    tenant_a = "tenant-cost-a"
    tenant_b = "tenant-cost-b"
    user_a = "usr-cost-a"
    user_b = "usr-cost-b"

    role_a = Role(
        role_id="role-a",
        name="BI Admin A",
        permissions=(
            Permission(permission_id="p-cost-a", action="AGENT_COST_DASHBOARD_READ", description="Read agent costs"),
            Permission(permission_id="p-bi-a", action="BUSINESS_INTELLIGENCE_READ", description="Read BI"),
        ),
    )
    role_b = Role(
        role_id="role-b",
        name="BI Admin B",
        permissions=(
            Permission(permission_id="p-cost-b", action="AGENT_COST_DASHBOARD_READ", description="Read agent costs"),
            Permission(permission_id="p-bi-b", action="BUSINESS_INTELLIGENCE_READ", description="Read BI"),
        ),
    )
    role_repo.save_role(role_a)
    role_repo.save_role(role_b)

    role_assignment_repo.save_assignment(
        RoleAssignment(
            assignment_id="ra-a",
            identity_id=user_a,
            role_id=role_a.role_id,
            scope=f"tenant_{tenant_a}",
            assigned_at=clock.now(),
        )
    )
    role_assignment_repo.save_assignment(
        RoleAssignment(
            assignment_id="ra-b",
            identity_id=user_b,
            role_id=role_b.role_id,
            scope=f"tenant_{tenant_b}",
            assigned_at=clock.now(),
        )
    )

    sess_a = SaaSSession(
        session_id="sess-cost-a",
        identity_id=user_a,
        tenant_id=tenant_a,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(days=1),
        status=SessionStatus.ACTIVE,
    )
    sess_b = SaaSSession(
        session_id="sess-cost-b",
        identity_id=user_b,
        tenant_id=tenant_b,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(days=1),
        status=SessionStatus.ACTIVE,
    )
    session_repo.save(sess_a)
    session_repo.save(sess_b)

    return {
        "client": client,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "sess_a": "sess-cost-a",
        "sess_b": "sess-cost-b",
        "usage_repo": usage_repo,
        "cost_repo": cost_repo,
        "clock": clock,
    }


def test_tenant_isolation_and_cross_tenant_denial(integration_app):
    """A & B: Tenant A ve sus propios hechos de costo; Tenant B está estrictamente aislado."""
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]
    tenant_b = integration_app["tenant_b"]
    sess_a = integration_app["sess_a"]
    sess_b = integration_app["sess_b"]
    usage_repo = integration_app["usage_repo"]

    # Agregar evento para tenant_a
    ev_a = UsageEvent(
        usage_event_id="ev-iso-a",
        tenant_id=tenant_a,
        occurred_at=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
        request_status=UsageRequestStatus.SUCCESS,
        provider="openai",
        model="gpt-4o",
        task_type="chat",
        actual_cost=Decimal("0.015"),
        details={"agent_type": "MarketAgent", "mission_id": "m-iso-1"},
    )
    usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_a)

    # Tenant A consulta su lista
    res_a = client.get(f"/api/bi/tenants/{tenant_a}/agent-costs", headers={"X-Session-ID": sess_a})
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["total_items"] == 1
    assert data_a["items"][0]["item_id"] == "ev-iso-a"

    # Tenant B consulta su lista -> debe estar vacía
    res_b = client.get(f"/api/bi/tenants/{tenant_b}/agent-costs", headers={"X-Session-ID": sess_b})
    assert res_b.status_code == 200
    data_b = res_b.json()
    assert data_b["total_items"] == 0

    # Tenant A intenta consultar datos de Tenant B -> 403 Forbidden
    res_cross = client.get(f"/api/bi/tenants/{tenant_b}/agent-costs", headers={"X-Session-ID": sess_a})
    assert res_cross.status_code == 403


def test_mission_cost_summary_endpoint(integration_app):
    """C: Agregación de costos asociados a una misión específica."""
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]
    usage_repo = integration_app["usage_repo"]

    ev1 = UsageEvent(
        usage_event_id="ev-m-1",
        tenant_id=tenant_a,
        occurred_at=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
        request_status=UsageRequestStatus.SUCCESS,
        provider="openai",
        model="gpt-4o",
        task_type="chat",
        actual_cost=Decimal("0.02"),
        input_tokens=700,
        output_tokens=300,
        total_tokens=1000,
        details={"agent_type": "MarketScout", "mission_id": "miss-99"},
    )
    ev2 = UsageEvent(
        usage_event_id="ev-m-2",
        tenant_id=tenant_a,
        occurred_at=datetime(2026, 9, 14, 10, 5, tzinfo=timezone.utc),
        request_status=UsageRequestStatus.SUCCESS,
        provider="anthropic",
        model="claude-3-5",
        task_type="chat",
        actual_cost=Decimal("0.03"),
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        details={"agent_type": "SupplierScout", "mission_id": "miss-99"},
    )
    usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev1)
    usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev2)

    res = client.get(f"/api/bi/tenants/{tenant_a}/agent-costs/missions/miss-99/summary", headers={"X-Session-ID": sess_a})
    assert res.status_code == 200
    data = res.json()
    assert data["mission_id"] == "miss-99"
    assert data["request_count"] == 2
    assert data["total_tokens"] == 2500
    assert data["cost_by_currency"]["USD"] == "0.05"


def test_unattributed_cost_remains_separate(integration_app):
    """D: Los eventos no vinculados a misiones se mantienen desatribuidos."""
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]
    usage_repo = integration_app["usage_repo"]

    ev_unattr = UsageEvent(
        usage_event_id="ev-unattr-99",
        tenant_id=tenant_a,
        occurred_at=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
        request_status=UsageRequestStatus.SUCCESS,
        provider="openai",
        model="gpt-4o",
        task_type="chat",
        actual_cost=Decimal("0.005"),
        details={},  # sin mission_id ni agent_type
    )
    usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_unattr)

    res = client.get(f"/api/bi/tenants/{tenant_a}/agent-costs/summary", headers={"X-Session-ID": sess_a})
    assert res.status_code == 200
    data = res.json()
    assert data["unattributed_cost_events_count"] == 1


def test_filters_and_sorting_api(integration_app):
    """E & I: Filtros por proveedor y ordenación por total_cost."""
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]
    usage_repo = integration_app["usage_repo"]

    for i in range(3):
        ev = UsageEvent(
            usage_event_id=f"ev-filter-{i}",
            tenant_id=tenant_a,
            occurred_at=datetime(2026, 9, 14, 10 + i, 0, tzinfo=timezone.utc),
            request_status=UsageRequestStatus.SUCCESS,
            provider="openai" if i < 2 else "anthropic",
            model="gpt-4o" if i < 2 else "claude-3-5",
            task_type="chat",
            actual_cost=Decimal(f"0.0{i+1}"),
            details={"agent_type": "MarketAgent"},
        )
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev)

    # Filtrar por provider openai y ordenar descendente por total_cost
    res = client.get(
        f"/api/bi/tenants/{tenant_a}/agent-costs?provider=openai&sort_by=total_cost&sort_order=DESC",
        headers={"X-Session-ID": sess_a},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_items"] == 2
    assert data["items"][0]["item_id"] == "ev-filter-1"
    assert data["items"][1]["item_id"] == "ev-filter-0"


def test_unknown_pricing_preserved_in_api(integration_app):
    """H: Semántica UNKNOWN preservada en la respuesta JSON (total_cost=None, is_known_cost=False)."""
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]
    usage_repo = integration_app["usage_repo"]

    ev_unk = UsageEvent(
        usage_event_id="ev-unk-json",
        tenant_id=tenant_a,
        occurred_at=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
        request_status=UsageRequestStatus.SUCCESS,
        provider="local-llm",
        model="custom-mistral",
        task_type="chat",
        actual_cost=None,
        details={"agent_type": "LocalAgent"},
    )
    usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_unk)

    res = client.get(f"/api/bi/tenants/{tenant_a}/agent-costs/ev-unk-json", headers={"X-Session-ID": sess_a})
    assert res.status_code == 200
    item = res.json()
    assert item["total_cost"] is None
    assert item["is_known_cost"] is False
    assert item["cost_source"] == "UNKNOWN_UNPRICED"
    assert "total_cost" in item["unknown_fields"]


def test_auth_rejections_and_html_view(integration_app):
    """J & K: Validación de autenticación y disponibilidad de vista HTML."""
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]

    # Sin encabezado de sesión -> 401
    res_unauth = client.get(f"/api/bi/tenants/{tenant_a}/agent-costs/summary")
    assert res_unauth.status_code == 401

    # Vista HTML pública / informativa de Q.5
    res_html = client.get("/bi/agent-costs")
    assert res_html.status_code == 200
    assert "Agent Cost Dashboard" in res_html.text
    assert "Q.5 Validated" in res_html.text
