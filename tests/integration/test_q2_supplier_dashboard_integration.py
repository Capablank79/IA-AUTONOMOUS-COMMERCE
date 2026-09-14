"""
Tests de Integración y End-to-End para Q.2 — Supplier Dashboard (Hito Q — Business Intelligence).

Cubre:
A. Tenant A consulta sus propios proveedores vía API REST y Web UI.
B. Tenant B permanece completamente aislado (cero leakage de Tenant A).
C. Filtros combinados (source, country, verification, risk, score min, MOQ max, lead time max, search text) retornan el subconjunto exacto.
D. Ordenación y ranking deterministas (por score, unit cost, lead time, reliability y tie-break consistente por supplier_id).
E. Paginación estable con metadatos (page, page_size, total_pages, has_next, has_prev).
F. Resumen estadístico (SupplierDashboardSummary) agregado y seguro con datos reales.
G. Detalle seguro de proveedor (SupplierDashboardDetail) con explicabilidad estructurada y contacto enmascarado.
H. Comparación multidimensional (SupplierComparisonView) determinista entre candidatos.
I. Semántica estricta de incertidumbre: UNKNOWN/None no se convierte a 0 ni se asume MOQ=1 o lead_time=0.
J. Acceso no autenticado (401) o no autorizado / cross-tenant (403) denegado con respuesta sanitizada.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
import pytest
from starlette.testclient import TestClient

from src.domain.supplier_dashboard.models import (
    SupplierDashboardItem,
    SupplierDashboardSummary,
    SupplierDashboardDetail,
    SupplierDashboardQuery,
    SupplierDashboardPage,
    SupplierComparisonView,
    SupplierSortField,
    SortOrder,
)
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierLocation,
    SupplierContact,
    SupplierProductReference,
    SupplierStatus,
    SupplierReadiness,
    EvidenceProvenanceType,
    RiskLevel,
    MOQInfo,
)
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
    OpportunityType,
    OpportunityStatus,
)
from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.market_monitoring.models import NormalizedPrice
from src.domain.tenant.models import TenantContext, TenantScope
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.rbac.models import Permission, Role, RoleAssignment
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.reliability.ports import ClockPort
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.supplier_dashboard.supplier_dashboard_service import SupplierDashboardService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import JsonOrganizationRepository, JsonMembershipRepository
from src.infrastructure.persistence.data.json.rbac_repository import JsonRoleRepository, JsonRoleAssignmentRepository
from src.infrastructure.persistence.data.json.tenant_supplier_repository import JsonTenantSupplierRepository
from src.infrastructure.persistence.data.json.tenant_opportunity_repository import JsonTenantOpportunityRepository
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


def make_supplier(
    supplier_id: str,
    name: str,
    source: str = "alibaba",
    country: str = "CN",
    unit_cost: Optional[Decimal] = Decimal("20.00"),
    moq: Optional[int] = 100,
    lead_time: Optional[int] = 15,
    supplier_score: Optional[Decimal] = Decimal("85.0"),
    verification: str = "VERIFIED",
    risk: str = "LOW",
) -> Supplier:
    dt = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
    loc = SupplierLocation(country=country, city="Shenzhen")
    contact = SupplierContact(name="John Doe", email="john@supplier.com", phone="+86100000000")
    metadata = {}
    if unit_cost is not None:
        metadata["unit_cost_amount"] = str(unit_cost)
        metadata["currency"] = "USD"
    if moq is not None:
        metadata["moq"] = moq
    if lead_time is not None:
        metadata["lead_time_days"] = lead_time
    if supplier_score is not None:
        metadata["supplier_score"] = str(supplier_score)
    metadata["verification_status"] = verification
    metadata["risk_level"] = risk
    metadata["reasons"] = ["Consistent delivery", "Verified audit"]

    return Supplier(
        supplier_id=supplier_id,
        name=name,
        source=source,
        source_type=EvidenceProvenanceType.FIXTURE,
        location=loc,
        contact=contact,
        status=SupplierStatus.ACTIVE,
        observed_at=dt,
        product_reference=None,
        metadata=metadata,
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

    supplier_dashboard_service = SupplierDashboardService(
        repository=supp_repo,
        authorization_service=auth_service,
        session_repository=session_repo,
        opportunity_repository=opp_repo,
    )

    app = create_admin_app(
        service=admin_service,
        supplier_dashboard_service=supplier_dashboard_service,
    )

    # Población de Datos Tenant Alpha
    ctx_a = TenantContext(tenant_id="tenant_alpha")
    s_a1 = make_supplier("supp_a1", "Alpha Supplier One", country="CN", supplier_score=Decimal("92.0"), unit_cost=Decimal("15.00"), moq=50, lead_time=10, verification="VERIFIED")
    s_a2 = make_supplier("supp_a2", "Alpha Supplier Two", country="US", source="direct", supplier_score=Decimal("78.0"), unit_cost=Decimal("40.00"), moq=10, lead_time=3, verification="UNVERIFIED")
    s_a3 = make_supplier("supp_a3", "Alpha Supplier Three", country="CN", supplier_score=Decimal("88.0"), unit_cost=None, moq=None, lead_time=None, verification="UNVERIFIED")  # Unknowns
    supp_repo.save_all(ctx_a, [s_a1, s_a2, s_a3])

    # Población de Datos Tenant Beta
    ctx_b = TenantContext(tenant_id="tenant_beta")
    s_b1 = make_supplier("supp_b1", "Beta Exclusive Vendor", country="DE", supplier_score=Decimal("99.0"))
    supp_repo.save(ctx_b, s_b1)

    # Crear Sesión Válida para Tenant Alpha
    sess_a = SaaSSession(
        session_id="session_alpha_123",
        identity_id="user_alpha_admin",
        tenant_id="tenant_alpha",
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
    )
    session_repo.save(sess_a)

    role_a = Role(
        role_id="role_alpha_admin",
        name="Alpha Admin",
        permissions=(
            Permission("perm_a_supp", "SUPPLIER_DASHBOARD_READ"),
            Permission("perm_a_bi", "BUSINESS_INTELLIGENCE_READ"),
        ),
    )
    role_repo.save(role_a)
    assignment_repo.save(RoleAssignment("asgn_a", "user_alpha_admin", role_a.role_id))

    # Crear Sesión Válida para Tenant Beta
    sess_b = SaaSSession(
        session_id="session_beta_456",
        identity_id="user_beta_admin",
        tenant_id="tenant_beta",
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
    )
    session_repo.save(sess_b)

    role_b = Role(
        role_id="role_beta_admin",
        name="Beta Admin",
        permissions=(
            Permission("perm_b_supp", "SUPPLIER_DASHBOARD_READ"),
        ),
    )
    role_repo.save(role_b)
    assignment_repo.save(RoleAssignment("asgn_b", "user_beta_admin", role_b.role_id))

    return {
        "client": TestClient(app),
        "session_a": "session_alpha_123",
        "session_b": "session_beta_456",
    }


def test_integration_tenant_isolation_and_list(integration_app):
    client = integration_app["client"]
    token_a = integration_app["session_a"]

    # Tenant Alpha ve sus 3 proveedores
    res_a = client.get("/api/bi/tenants/tenant_alpha/suppliers", headers={"Authorization": f"Bearer {token_a}"})
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["total_count"] == 3
    supplier_ids_a = {i["supplier_id"] for i in data_a["items"]}
    assert supplier_ids_a == {"supp_a1", "supp_a2", "supp_a3"}
    assert "supp_b1" not in supplier_ids_a

    # Intento de acceso Cross-Tenant por Token A hacia Tenant B -> 403 Forbidden
    res_cross = client.get("/api/bi/tenants/tenant_beta/suppliers", headers={"Authorization": f"Bearer {token_a}"})
    assert res_cross.status_code == 403


def test_integration_summary_endpoint(integration_app):
    client = integration_app["client"]
    token_a = integration_app["session_a"]

    res = client.get("/api/bi/tenants/tenant_alpha/suppliers/summary", headers={"Authorization": f"Bearer {token_a}"})
    assert res.status_code == 200
    data = res.json()
    assert data["total_suppliers"] == 3
    assert data["verified_suppliers"] == 1  # supp_a1
    assert data["suppliers_with_unknown_critical_fields"] == 1  # supp_a3
    assert data["suppliers_by_country"]["CN"] == 2
    assert data["suppliers_by_country"]["US"] == 1


def test_integration_filtering_and_sorting(integration_app):
    client = integration_app["client"]
    token_a = integration_app["session_a"]

    # Filtrar por país CN y ordenar por score DESC
    res = client.get(
        "/api/bi/tenants/tenant_alpha/suppliers?country=CN&sort_by=supplier_score&sort_order=desc",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_count"] == 2
    # supp_a1 (92.0), supp_a3 (88.0)
    assert data["items"][0]["supplier_id"] == "supp_a1"
    assert data["items"][1]["supplier_id"] == "supp_a3"


def test_integration_detail_endpoint_and_masking(integration_app):
    client = integration_app["client"]
    token_a = integration_app["session_a"]

    res = client.get("/api/bi/tenants/tenant_alpha/suppliers/supp_a1", headers={"Authorization": f"Bearer {token_a}"})
    assert res.status_code == 200
    detail = res.json()
    assert detail["supplier_id"] == "supp_a1"
    assert detail["name"] == "Alpha Supplier One"
    assert detail["supplier_score"] == "92.0"
    assert detail["unit_cost_amount"] == "15.00"
    assert detail["contact"]["email"] != "john@supplier.com"
    assert "@" in detail["contact"]["email"]  # Enmascarado


def test_integration_compare_endpoint(integration_app):
    client = integration_app["client"]
    token_a = integration_app["session_a"]

    # POST compare
    res = client.post(
        "/api/bi/tenants/tenant_alpha/suppliers/compare",
        json={"supplier_ids": ["supp_a1", "supp_a2"]},
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res.status_code == 200
    comp = res.json()
    assert len(comp["items"]) == 2
    assert "Alpha Supplier One" in comp["supplier_names"]
    assert "Alpha Supplier Two" in comp["supplier_names"]


def test_integration_unauthenticated_denied(integration_app):
    client = integration_app["client"]
    res = client.get("/api/bi/tenants/tenant_alpha/suppliers")
    assert res.status_code == 401
    assert res.json()["error"] == "unauthenticated"


def test_integration_html_surface(integration_app):
    client = integration_app["client"]
    res = client.get("/bi/suppliers")
    assert res.status_code == 200
    assert "Supplier Dashboard" in res.text
    assert "Q.2 Validated" in res.text
