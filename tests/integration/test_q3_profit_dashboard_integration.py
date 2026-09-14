"""
Tests de Integración y End-to-End para Q.3 — Profit Dashboard (Hito Q — Business Intelligence).

Cubre:
A. Complete financial record -> correct gross/contribution profit and margin.
B. Missing shipping/tax/fees -> partial/insufficient_data, no fake profit (UNKNOWN != 0).
C. Tenant A vs Tenant B strict isolation (cero data leakage, 403 on cross-tenant).
D. Multi-criteria filtering (marketplace, category, supplier, completeness, margin/profit range, text search).
E. Stable sorting and pagination metadata.
F. Statistical summary endpoint (/summary) with aggregated unit economics.
G. Detailed unit economics breakdown endpoint (/{item_id}) with traceable facts and missing components list.
H. Multi-item / supplier alternatives comparison endpoint (/compare) producing distinct economics.
I. Multi-currency and currency mismatch safety.
J. Unauthorized / unauthenticated requests rejected with 401/403.
K. Minimal HTML dashboard surface (/bi/profit).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
import pytest
from starlette.testclient import TestClient

from src.domain.profit_dashboard.models import (
    ProfitDashboardItem,
    ProfitDashboardSummary,
    ProfitDashboardDetail,
    ProfitDashboardQuery,
    ProfitDashboardPage,
    ProfitComparisonView,
    ProfitCompleteness,
    ProfitSortField,
    SortOrder,
)
from src.domain.tenant.models import TenantContext
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.rbac.models import Permission, Role, RoleAssignment
from src.domain.reliability.ports import ClockPort
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.profit_dashboard.profit_dashboard_service import ProfitDashboardService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import JsonOrganizationRepository, JsonMembershipRepository
from src.infrastructure.persistence.data.json.rbac_repository import JsonRoleRepository, JsonRoleAssignmentRepository
from src.infrastructure.persistence.data.json.tenant_profit_repository import JsonTenantProfitRepository
from src.infrastructure.persistence.data.json.tenant_opportunity_repository import JsonTenantOpportunityRepository
from src.infrastructure.persistence.data.json.tenant_supplier_repository import JsonTenantSupplierRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.application.admin_console.admin_console_service import AdminConsoleService
from src.application.organization.organization_service import OrganizationService, OrganizationMembershipService
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.billing.subscription_service import SubscriptionService
from src.infrastructure.persistence.data.json.usage_event_repository import InMemoryUsageEventRepository
from src.infrastructure.persistence.data.json.quota_repository import InMemoryQuotaPolicyRepository, InMemoryQuotaReservationRepository
from src.infrastructure.persistence.data.json.plan_repository import InMemoryPlanCatalogRepository, InMemoryPlanAssignmentRepository
from src.infrastructure.persistence.data.json.billing_repository import InMemorySubscriptionRepository, InMemoryInvoiceRepository
from src.infrastructure.web.admin_app import create_admin_app


class FixedClock(ClockPort):
    def __init__(self):
        self.current = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def sleep(self, seconds):
        self.current += timedelta(seconds=seconds)


def make_profit_item(
    item_id: str,
    title: str = "Test Product",
    marketplace: str = "mercadolibre",
    category: str = "Electronics",
    supplier_id: Optional[str] = "supp-100",
    sale_price: Optional[Decimal] = Decimal("100.00"),
    currency: str = "USD",
    unit_cost: Optional[Decimal] = Decimal("50.00"),
    shipping_cost: Optional[Decimal] = Decimal("10.00"),
    marketplace_fee: Optional[Decimal] = Decimal("12.00"),
    payment_fee: Optional[Decimal] = Decimal("3.00"),
    tax_cost: Optional[Decimal] = Decimal("5.00"),
    other_costs: Optional[Decimal] = Decimal("0.00"),
) -> ProfitDashboardItem:
    return ProfitDashboardService.compute_profit_item_from_facts(
        item_id=item_id,
        opportunity_id=f"opp-{item_id}",
        product_id=f"prod-{item_id}",
        supplier_id=supplier_id,
        supplier_name="Global Tech Supplier",
        title=title,
        marketplace=marketplace,
        category=category,
        product_sku=f"SKU-{item_id}",
        sale_price_amount=sale_price,
        currency=currency,
        unit_cost_amount=unit_cost,
        shipping_cost_amount=shipping_cost,
        marketplace_fee_amount=marketplace_fee,
        payment_fee_amount=payment_fee,
        tax_amount=tax_cost,
        other_costs_amount=other_costs,
        cost_currency=currency,
        exchange_rate=Decimal("1.0"),
        calculated_at=datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def integration_app(tmp_path):
    clock = FixedClock()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    session_repo = JsonSaaSSessionRepository(data_dir / "sessions")
    org_repo = JsonOrganizationRepository(data_dir / "organizations")
    membership_repo = JsonMembershipRepository(data_dir / "memberships")
    role_repo = JsonRoleRepository(data_dir / "roles")
    assignment_repo = JsonRoleAssignmentRepository(data_dir / "role_assignments")
    audit_repo = JsonAuditRepository(data_dir / "audit")

    profit_repo = JsonTenantProfitRepository(data_dir)
    supp_repo = JsonTenantSupplierRepository(data_dir)
    opp_repo = JsonTenantOpportunityRepository(data_dir)

    rbac_service = RBACService(role_repository=role_repo, assignment_repository=assignment_repo, clock=clock)
    auth_service = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_service,
        clock=clock,
    )

    org_service = OrganizationService(org_repo, audit_repository=audit_repo)
    membership_service = OrganizationMembershipService(membership_repo, org_repo, audit_repository=audit_repo)
    usage_repo = InMemoryUsageEventRepository()
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

    admin_service = AdminConsoleService(
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
        plan_assignment_repository=plan_assignment_repo,
        subscription_service=subscription_service,
        subscription_repository=subscription_repo,
        invoice_repository=invoice_repo,
        rbac_service=rbac_service,
        audit_repository=audit_repo,
        clock=clock,
    )

    profit_dashboard_service = ProfitDashboardService(
        repository=profit_repo,
        authorization_service=auth_service,
        session_repository=session_repo,
        opportunity_repository=opp_repo,
        supplier_repository=supp_repo,
        clock=clock,
    )

    app = create_admin_app(
        service=admin_service,
        profit_dashboard_service=profit_dashboard_service,
    )

    # Población de Datos Tenant Alpha
    ctx_a = TenantContext(tenant_id="tenant_alpha")
    item_a1 = make_profit_item(
        item_id="item-a1",
        title="Alpha Complete High Margin",
        marketplace="mercadolibre",
        category="Electronics",
        supplier_id="supp-a1",
        sale_price=Decimal("200.00"),
        unit_cost=Decimal("80.00"),
        shipping_cost=Decimal("15.00"),
        marketplace_fee=Decimal("25.00"),
        payment_fee=Decimal("5.00"),
        tax_cost=Decimal("10.00"),
    )  # gross = 120, contrib = 65, margin = 32.5%, complete
    item_a2 = make_profit_item(
        item_id="item-a2",
        title="Alpha Partial Missing Shipping",
        marketplace="amazon",
        category="Home",
        supplier_id="supp-a2",
        sale_price=Decimal("100.00"),
        unit_cost=Decimal("75.00"),
        shipping_cost=None,  # Missing
        marketplace_fee=Decimal("15.00"),
        payment_fee=Decimal("3.00"),
        tax_cost=None,
    )  # gross = 25, contrib = 7.00, margin = 7.0%, partial
    item_a3 = make_profit_item(
        item_id="item-a3",
        title="Alpha Negative Margin Loss Leader",
        marketplace="mercadolibre",
        category="Electronics",
        supplier_id="supp-a1",
        sale_price=Decimal("100.00"),
        unit_cost=Decimal("90.00"),
        shipping_cost=Decimal("15.00"),
        marketplace_fee=Decimal("12.00"),
        payment_fee=Decimal("3.00"),
        tax_cost=Decimal("5.00"),
    )  # gross = 10, total cost = 125, contrib = -25, margin = -25%, complete
    item_a4 = make_profit_item(
        item_id="item-a4",
        title="Alpha Multi-currency EUR",
        marketplace="ebay",
        category="Collectibles",
        supplier_id="supp-a3",
        sale_price=Decimal("150.00"),
        currency="EUR",
        unit_cost=Decimal("80.00"),
        shipping_cost=Decimal("10.00"),
        marketplace_fee=Decimal("15.00"),
        payment_fee=Decimal("5.00"),
        tax_cost=Decimal("10.00"),
    )
    profit_repo.save_all(ctx_a, [item_a1, item_a2, item_a3, item_a4])

    # Población de Datos Tenant Beta
    ctx_b = TenantContext(tenant_id="tenant_beta")
    item_b1 = make_profit_item(
        item_id="item-b1",
        title="Beta Confidential Profit Item",
        marketplace="shopify",
        sale_price=Decimal("500.00"),
        unit_cost=Decimal("100.00"),
    )
    profit_repo.save(ctx_b, item_b1)

    # Sesión Válida Tenant Alpha con PROFIT_DASHBOARD_READ
    sess_a = SaaSSession(
        session_id="sess-alpha-profit",
        identity_id="user-alpha-bi",
        tenant_id="tenant_alpha",
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
    )
    session_repo.save(sess_a)

    role_a = Role(
        role_id="role-alpha-bi",
        name="Alpha BI Specialist",
        permissions=(
            Permission("p1", "PROFIT_DASHBOARD_READ", description="Read Profit Dashboard"),
            Permission("p2", "BUSINESS_INTELLIGENCE_READ", description="Read BI"),
        ),
    )
    role_repo.save_role(role_a)
    assignment_repo.save_assignment(
        RoleAssignment(
            assignment_id="asgn-alpha-bi",
            identity_id="user-alpha-bi",
            role_id=role_a.role_id,
            assigned_at=clock.now(),
        )
    )

    # Sesión Tenant Alpha sin permisos BI
    sess_no_perm = SaaSSession(
        session_id="sess-alpha-no-perm",
        identity_id="user-alpha-unprivileged",
        tenant_id="tenant_alpha",
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
    )
    session_repo.save(sess_no_perm)

    role_no_perm = Role(
        role_id="role-no-perm",
        name="No BI Perm",
        permissions=(),
    )
    role_repo.save_role(role_no_perm)
    assignment_repo.save_assignment(
        RoleAssignment(
            assignment_id="asgn-no-perm",
            identity_id="user-alpha-unprivileged",
            role_id=role_no_perm.role_id,
            assigned_at=clock.now(),
        )
    )

    # Sesión Válida Tenant Beta
    sess_b = SaaSSession(
        session_id="sess-beta-profit",
        identity_id="user-beta-bi",
        tenant_id="tenant_beta",
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
    )
    session_repo.save(sess_b)

    role_b = Role(
        role_id="role-beta-bi",
        name="Beta BI",
        permissions=(
            Permission("p_b", "PROFIT_DASHBOARD_READ", description="Read Profit Dashboard"),
        ),
    )
    role_repo.save_role(role_b)
    assignment_repo.save_assignment(
        RoleAssignment(
            assignment_id="asgn-beta-bi",
            identity_id="user-beta-bi",
            role_id=role_b.role_id,
            assigned_at=clock.now(),
        )
    )

    return {
        "client": TestClient(app),
        "session_a": "sess-alpha-profit",
        "session_b": "sess-beta-profit",
        "session_no_perm": "sess-alpha-no-perm",
    }


def test_scenario_a_complete_record_profit_and_margin(integration_app):
    """Escenario A: Registro financiero completo retorna unit economics exactos."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/profit/item-a1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    item = data["item"]
    assert item["item_id"] == "item-a1"
    assert item["sale_price"] == "200.00"
    assert item["unit_cost"] == "80.00"
    assert item["gross_profit"] == "120.00"
    assert item["contribution_profit"] == "65.00"
    assert Decimal(item["margin_pct"]) == Decimal("32.50")
    assert item["completeness"] == "COMPLETE"
    assert item["missing_cost_components"] == []


def test_scenario_b_missing_shipping_and_fees_no_fake_profit(integration_app):
    """Escenario B: Costos ausentes (UNKNOWN != 0) marcan PARTIAL y no inventan ganancia."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/profit/item-a2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    item = data["item"]
    assert item["item_id"] == "item-a2"
    assert item["gross_profit"] == "25.00"
    assert item["shipping_cost"] is None
    assert item["contribution_profit"] == "7.00"
    assert Decimal(item["margin_pct"]) == Decimal("7.00")
    assert item["completeness"] == "PARTIAL"
    assert "SHIPPING_COST" in item["missing_cost_components"]
    assert "TAX_COST" in item["missing_cost_components"]


def test_scenario_c_tenant_isolation_and_cross_tenant_denial(integration_app):
    """Escenario C: Aislamiento estricto multi-tenant y denegación 403 cross-tenant."""
    client = integration_app["client"]
    token_a = integration_app["session_a"]

    # Tenant Alpha ve sus 4 ítems
    res_a = client.get(
        "/api/bi/tenants/tenant_alpha/profit",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["total_count"] == 4
    item_ids_a = {i["item_id"] for i in data_a["items"]}
    assert "item-b1" not in item_ids_a
    assert item_ids_a == {"item-a1", "item-a2", "item-a3", "item-a4"}

    # Intento de acceso Cross-Tenant por Token A hacia Tenant B -> 403 Forbidden
    res_cross = client.get(
        "/api/bi/tenants/tenant_beta/profit",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_cross.status_code == 403


def test_scenario_d_multi_criteria_filtering(integration_app):
    """Escenario D: Filtros multicriterio por marketplace, categoría, supplier y márgenes."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    # Filtrar por marketplace mercadolibre y categoría Electronics
    res = client.get(
        "/api/bi/tenants/tenant_alpha/profit?marketplace=mercadolibre&category=Electronics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_count"] == 2
    assert {i["item_id"] for i in data["items"]} == {"item-a1", "item-a3"}

    # Filtrar por min_margin_pct=30.00
    res_margin = client.get(
        "/api/bi/tenants/tenant_alpha/profit?min_margin_pct=30.00",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_margin.status_code == 200
    data_margin = res_margin.json()
    assert data_margin["total_count"] == 1
    assert data_margin["items"][0]["item_id"] == "item-a1"

    # Filtrar por completeness=PARTIAL
    res_part = client.get(
        "/api/bi/tenants/tenant_alpha/profit?completeness=PARTIAL",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_part.status_code == 200
    data_part = res_part.json()
    assert data_part["total_count"] == 1
    assert data_part["items"][0]["item_id"] == "item-a2"


def test_scenario_e_sorting_and_pagination_metadata(integration_app):
    """Escenario E: Ordenación determinista y paginación acotada con metadatos completos."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/profit?sort_by=sale_price&sort_order=desc&page=1&page_size=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_count"] == 4
    assert data["page"] == 1
    assert data["page_size"] == 2
    assert data["total_pages"] == 2
    assert data["has_next"] is True
    assert data["has_previous"] is False
    assert len(data["items"]) == 2
    assert data["items"][0]["item_id"] == "item-a1"  # 200.00
    assert data["items"][1]["item_id"] == "item-a4"  # 150.00


def test_scenario_f_summary_statistics_endpoint(integration_app):
    """Escenario F: Resumen estadístico agregado y seguro con datos financieros reales."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/profit/summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_items"] == 4
    assert data["complete_profitability_count"] == 3  # a1, a3, a4
    assert data["partial_profitability_count"] == 1   # a2
    assert data["positive_margin_count"] == 3        # a1 (32.5%), a2 (7.0%), a4 (20.0%)
    assert data["negative_margin_count"] == 1        # a3 (-25.0%)
    assert "mercadolibre" in data["items_by_marketplace"]


def test_scenario_g_detail_breakdown_and_traceability(integration_app):
    """Escenario G: Detalle con desglose estructurado y hechos financieros trazables."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/profit/item-a3",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    detail = res.json()
    item = detail["item"]
    assert item["item_id"] == "item-a3"
    assert item["margin_pct"] == "-25.00"
    assert item["contribution_profit"] == "-25.00"
    assert item["sale_price"] == "100.00"
    assert item["unit_cost"] == "90.00"
    assert item["shipping_cost"] == "15.00"
    assert item["marketplace_fee"] == "12.00"
    assert item["tax_cost"] == "5.00"
    assert item["total_known_cost"] == "125.00"
    assert len(detail["cost_breakdown"]) > 0
    assert any("Sale Price" in fact for fact in detail["financial_facts"])


def test_scenario_h_supplier_alternatives_comparison(integration_app):
    """Escenario H: Comparación multidimensional de ítems y escenarios de proveedor."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    # POST /compare
    res_post = client.post(
        "/api/bi/tenants/tenant_alpha/profit/compare",
        json={"item_ids": ["item-a1", "item-a3"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_post.status_code == 200
    comp = res_post.json()
    assert len(comp["items"]) == 2
    assert comp["item_ids"] == ["item-a1", "item-a3"]
    assert comp["summary_comparison"]["currencies_match"] is True

    # GET /compare con query params
    res_get = client.get(
        "/api/bi/tenants/tenant_alpha/profit/compare?item_ids=item-a1,item-a3",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_get.status_code == 200
    assert len(res_get.json()["items"]) == 2


def test_scenario_i_multi_currency_comparison_safety(integration_app):
    """Escenario I: Seguridad multi-moneda detecta incompatibilidad sin FX."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    # Comparar USD (item-a1) con EUR (item-a4)
    res = client.post(
        "/api/bi/tenants/tenant_alpha/profit/compare",
        json={"item_ids": ["item-a1", "item-a4"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    comp = res.json()
    assert comp["summary_comparison"]["currencies_match"] is False


def test_scenario_j_unauthorized_and_unauthenticated_rejected(integration_app):
    """Escenario J: Rechazo estricto para usuarios no autenticados (401) o no autorizados (403)."""
    client = integration_app["client"]
    token_no_perm = integration_app["session_no_perm"]

    # 401 Sin token
    res_no_auth = client.get("/api/bi/tenants/tenant_alpha/profit")
    assert res_no_auth.status_code == 401
    assert res_no_auth.json()["error"] == "unauthenticated"

    # 403 Con sesión activa pero sin permiso PROFIT_DASHBOARD_READ
    res_forbidden = client.get(
        "/api/bi/tenants/tenant_alpha/profit",
        headers={"Authorization": f"Bearer {token_no_perm}"},
    )
    assert res_forbidden.status_code == 403
    assert res_forbidden.json()["error"] in ("forbidden", "unauthorized")


def test_scenario_k_html_view_surface(integration_app):
    """Escenario K: Vista HTML mínima en /bi/profit responde correctamente."""
    client = integration_app["client"]
    res = client.get("/bi/profit")
    assert res.status_code == 200
    assert "Profit Dashboard" in res.text
    assert "Q.3 Validated" in res.text
    assert "Unit Economics" in res.text
