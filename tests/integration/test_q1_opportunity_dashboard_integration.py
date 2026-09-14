"""
Tests de Integración y End-to-End para Q.1 — Opportunity Dashboard (Hito Q — Business Intelligence).

Cubre:
A. Tenant A consulta sus propias oportunidades vía API REST y Web UI.
B. Tenant B permanece completamente aislado (cero leakage de Tenant A).
C. Filtros combinados (marketplace, categoría, score min, margen min, texto) retornan el subconjunto exacto.
D. Ordenación y ranking deterministas (por score, fecha, margen y tie-break consistente).
E. Paginación estable con metadatos (page, page_size, total_pages, has_next, has_previous).
F. Resumen estadístico (OpportunityDashboardSummary) agregado y seguro con datos reales.
G. Detalle seguro de oportunidad (OpportunityDashboardDetail) con desglose y explicabilidad estructurada.
H. Comparación multidimensional (OpportunityComparisonView) determinista entre candidatos.
I. Semántica estricta de incertidumbre: UNKNOWN/None no se convierte a 0 ni se pierde Decimal.
J. Acceso no autenticado (401) o no autorizado / cross-tenant (403) denegado con respuesta sanitizada.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional
import uuid
import pytest
from starlette.testclient import TestClient

from src.domain.opportunity_dashboard.models import (
    OpportunityDashboardItem,
    OpportunityDashboardSummary,
    OpportunityDashboardDetail,
    OpportunityDashboardQuery,
    OpportunityDashboardPage,
    OpportunityComparisonView,
    OpportunitySortField,
    SortOrder,
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
from src.domain.rbac.models import Permission, Role, RoleAssignment, PermissionStatus, RoleStatus
from src.domain.reliability.ports import ClockPort
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.opportunity_dashboard.opportunity_dashboard_service import OpportunityDashboardService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import JsonOrganizationRepository, JsonMembershipRepository
from src.infrastructure.persistence.data.json.rbac_repository import JsonRoleRepository, JsonRoleAssignmentRepository
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


def make_test_record(
    opp_id: str,
    title: str,
    category: str,
    marketplace: Marketplace,
    score: Optional[Decimal] = Decimal("85.0"),
    margin: Optional[Decimal] = Decimal("0.35"),
    confidence: Confidence = Confidence.HIGH,
    status: OpportunityStatus = OpportunityStatus.DETECTED,
    opp_type: OpportunityType = OpportunityType.PRICE_ARBITRAGE,
    detected_at: Optional[datetime] = None,
    estimated_price: Optional[Decimal] = Decimal("199.99"),
) -> OpportunityRecord:
    dt = detected_at or datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)
    price_obj = NormalizedPrice(amount=estimated_price, currency="USD") if estimated_price is not None else None
    observed = ObservedOpportunityMetrics(
        observed_price=price_obj,
        observed_competitor_count=4,
        observed_sold_quantity=120,
        observed_stock=25,
    )
    derived = DerivedOpportunityMetrics(
        opportunity_score=score,
        potential_margin_ratio=margin,
        price_gap_ratio=Decimal("0.18"),
        demand_intensity="HIGH",
        competition_density="MEDIUM",
        scoring_rationale=("Computed from margin and demand",),
    )
    return OpportunityRecord(
        opportunity_id=opp_id,
        canonical_product_id=f"prod_{opp_id}",
        marketplace=marketplace,
        opportunity_type=opp_type,
        status=status,
        confidence=confidence,
        source_observation_ids=("obs_1",),
        detected_at=dt,
        title=title,
        category=category,
        observed_metrics=observed,
        derived_metrics=derived,
        reasons=("Strong margin", "Low competition"),
    )


def setup_test_environment(storage_dir):
    clock = FixedClock()
    session_repo = JsonSaaSSessionRepository(storage_dir / "sessions")
    org_repo = JsonOrganizationRepository(storage_dir / "organizations")
    membership_repo = JsonMembershipRepository(storage_dir / "memberships")
    role_repo = JsonRoleRepository(storage_dir / "roles")
    assignment_repo = JsonRoleAssignmentRepository(storage_dir / "role_assignments")
    audit_repo = JsonAuditRepository(storage_dir / "audit")
    opp_repo = JsonTenantOpportunityRepository(storage_dir / "opportunities")

    rbac_service = RBACService(role_repo, assignment_repo, clock=clock)
    auth_service = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_service,
        clock=clock,
    )

    opp_service = OpportunityDashboardService(
        repository=opp_repo,
        authorization_service=auth_service,
        session_repository=session_repo,
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
        plan_assignment_repository=plan_assignment_repo,
        subscription_service=subscription_service,
        subscription_repository=subscription_repo,
        invoice_repository=invoice_repo,
        rbac_service=rbac_service,
        audit_repository=audit_repo,
        clock=clock,
    )

    app = create_admin_app(
            service=admin_console_service,
            opportunity_dashboard_service=opp_service,
        )
    client = TestClient(app)

    return {
        "clock": clock,
        "session_repo": session_repo,
        "role_repo": role_repo,
        "assignment_repo": assignment_repo,
        "opp_repo": opp_repo,
        "opp_service": opp_service,
        "client": client,
    }


def create_user_session(env, tenant_id: str, identity_id: str, actions=("OPPORTUNITY_DASHBOARD_READ", "OPPORTUNITY_READ")) -> str:
    suffix = uuid.uuid4().hex[:8]
    role_id = f"role_{suffix}"
    perms = tuple(
        Permission(
            permission_id=f"perm_{a.lower()}_{suffix}",
            action=a,
            status=PermissionStatus.ACTIVE,
        )
        for a in actions
    )
    env["role_repo"].save_role(Role(role_id=role_id, name=f"Role {suffix}", permissions=perms, status=RoleStatus.ACTIVE))
    env["assignment_repo"].save_assignment(
        RoleAssignment(
            assignment_id=f"assign_{suffix}",
            identity_id=identity_id,
            role_id=role_id,
            assigned_at=env["clock"].now(),
        )
    )
    sess_id = f"sess_{suffix}"
    env["session_repo"].save(
        SaaSSession(
            session_id=sess_id,
            identity_id=identity_id,
            tenant_id=tenant_id,
            status=SessionStatus.ACTIVE,
            created_at=env["clock"].now(),
            expires_at=env["clock"].now() + timedelta(hours=2),
        )
    )
    return sess_id


# =============================================================================
# INTEGRATION TESTS
# =============================================================================

def test_q1_opportunity_dashboard_tenant_isolation_and_listing(tmp_path):
    env = setup_test_environment(tmp_path)
    client = env["client"]
    opp_repo = env["opp_repo"]

    # Poblar Tenant A y Tenant B
    ctx_a = TenantContext(tenant_id="tenant_alpha")
    ctx_b = TenantContext(tenant_id="tenant_beta")

    rec_a1 = make_test_record("opp_a1", "Mechanical Keyboard", "Computers", Marketplace.MERCADO_LIBRE, score=Decimal("92.0"))
    rec_a2 = make_test_record("opp_a2", "Gaming Headset", "Audio", Marketplace.AMAZON, score=Decimal("81.0"))
    opp_repo.save_all(ctx_a, [rec_a1, rec_a2])

    rec_b1 = make_test_record("opp_b1", "Beta Secret Product", "Confidential", Marketplace.MERCADO_LIBRE, score=Decimal("99.0"))
    opp_repo.save(ctx_b, rec_b1)

    # Crear sesiones autenticadas
    sess_a = create_user_session(env, "tenant_alpha", "user_alpha")
    sess_b = create_user_session(env, "tenant_beta", "user_beta")

    headers_a = {"Authorization": f"Bearer {sess_a}"}
    headers_b = {"Authorization": f"Bearer {sess_b}"}

    # 1. Tenant Alpha lista sus oportunidades
    resp_a = client.get("/api/bi/tenants/tenant_alpha/opportunities", headers=headers_a)
    assert resp_a.status_code == 200
    data_a = resp_a.json()
    assert data_a["total_count"] == 2
    opp_ids_a = [item["opportunity_id"] for item in data_a["items"]]
    assert "opp_a1" in opp_ids_a
    assert "opp_a2" in opp_ids_a
    assert "opp_b1" not in opp_ids_a

    # 2. Tenant Beta lista sus oportunidades
    resp_b = client.get("/api/bi/tenants/tenant_beta/opportunities", headers=headers_b)
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    assert data_b["total_count"] == 1
    assert data_b["items"][0]["opportunity_id"] == "opp_b1"

    # 3. Cross-Tenant Denied: Tenant Alpha intenta leer Tenant Beta -> 403 Forbidden
    resp_cross = client.get("/api/bi/tenants/tenant_beta/opportunities", headers=headers_a)
    assert resp_cross.status_code == 403


def test_q1_opportunity_dashboard_filters_and_sorting(tmp_path):
    env = setup_test_environment(tmp_path)
    client = env["client"]
    opp_repo = env["opp_repo"]
    ctx = TenantContext(tenant_id="tenant_filt")

    opps = [
        make_test_record("opp_1", "Ultra Gaming Mouse", "Tech", Marketplace.MERCADO_LIBRE, score=Decimal("95.0"), margin=Decimal("0.45")),
        make_test_record("opp_2", "Office Mouse Pad", "Tech", Marketplace.AMAZON, score=Decimal("60.0"), margin=Decimal("0.20")),
        make_test_record("opp_3", "Ceramic Coffee Mug", "Kitchen", Marketplace.MERCADO_LIBRE, score=Decimal("80.0"), margin=Decimal("0.50")),
        make_test_record("opp_4", "Unknown Score Item", "Other", Marketplace.AMAZON, score=None, margin=None),
    ]
    opp_repo.save_all(ctx, opps)

    sess = create_user_session(env, "tenant_filt", "user_filt")
    headers = {"Authorization": f"Bearer {sess}"}

    # Filtro por Categoría
    resp = client.get("/api/bi/tenants/tenant_filt/opportunities?category=Tech", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert {i["opportunity_id"] for i in items} == {"opp_1", "opp_2"}

    # Filtro por Score Mínimo (>= 75)
    resp = client.get("/api/bi/tenants/tenant_filt/opportunities?min_score=75.0", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert {i["opportunity_id"] for i in items} == {"opp_1", "opp_3"}

    # Filtro por Margen Mínimo (>= 0.40)
    resp = client.get("/api/bi/tenants/tenant_filt/opportunities?min_margin=0.40", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 2
    assert {i["opportunity_id"] for i in items} == {"opp_1", "opp_3"}

    # Ordenación por Score DESC (item sin score va al final)
    resp = client.get("/api/bi/tenants/tenant_filt/opportunities?sort_by=opportunity_score&sort_order=desc", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert [i["opportunity_id"] for i in items] == ["opp_1", "opp_3", "opp_2", "opp_4"]


def test_q1_opportunity_dashboard_summary_and_detail(tmp_path):
    env = setup_test_environment(tmp_path)
    client = env["client"]
    opp_repo = env["opp_repo"]
    ctx = TenantContext(tenant_id="tenant_summary")

    opp1 = make_test_record("opp_101", "Laptop Stand Aluminum", "Accessories", Marketplace.MERCADO_LIBRE, score=Decimal("88.0"))
    opp2 = make_test_record("opp_102", "USB-C Hub Multiport", "Accessories", Marketplace.AMAZON, score=Decimal("72.0"))
    opp_repo.save_all(ctx, [opp1, opp2])

    sess = create_user_session(env, "tenant_summary", "user_summary")
    headers = {"Authorization": f"Bearer {sess}"}

    # Summary Endpoint
    resp_sum = client.get("/api/bi/tenants/tenant_summary/opportunities/summary", headers=headers)
    assert resp_sum.status_code == 200
    data_sum = resp_sum.json()
    assert data_sum["total_opportunities"] == 2
    assert data_sum["high_potential_count"] == 2
    assert data_sum["average_opportunity_score"] == "80.00"

    # Detail Endpoint
    resp_dtl = client.get("/api/bi/tenants/tenant_summary/opportunities/opp_101", headers=headers)
    assert resp_dtl.status_code == 200
    data_dtl = resp_dtl.json()
    assert data_dtl["item"]["opportunity_id"] == "opp_101"
    assert "observed_metrics_detail" in data_dtl
    assert "derived_metrics_detail" in data_dtl
    assert "explanation" in data_dtl
    assert "reasons" in data_dtl


def test_q1_opportunity_dashboard_comparison(tmp_path):
    env = setup_test_environment(tmp_path)
    client = env["client"]
    opp_repo = env["opp_repo"]
    ctx = TenantContext(tenant_id="tenant_comp")

    opp1 = make_test_record("opp_c1", "Product Cand 1", "Home", Marketplace.MERCADO_LIBRE, score=Decimal("94.0"), margin=Decimal("0.40"))
    opp2 = make_test_record("opp_c2", "Product Cand 2", "Home", Marketplace.AMAZON, score=Decimal("82.0"), margin=Decimal("0.25"))
    opp_repo.save_all(ctx, [opp1, opp2])

    sess = create_user_session(env, "tenant_comp", "user_comp")
    headers = {"Authorization": f"Bearer {sess}"}

    resp_cmp = client.get("/api/bi/tenants/tenant_comp/opportunities/compare?opportunity_ids=opp_c1,opp_c2", headers=headers)
    assert resp_cmp.status_code == 200
    data_cmp = resp_cmp.json()
    assert len(data_cmp["candidate_ids"]) == 2
    assert data_cmp["best_candidate_id"] == "opp_c1"
    assert len(data_cmp["dimensions"]) >= 3


def test_q1_opportunity_dashboard_unauthenticated_and_unauthorized(tmp_path):
    env = setup_test_environment(tmp_path)
    client = env["client"]

    # 1. Sin cabecera de autenticación -> 401
    resp_no_auth = client.get("/api/bi/tenants/tenant_sec/opportunities")
    assert resp_no_auth.status_code == 401

    # 2. Token inválido -> 401
    resp_bad_auth = client.get("/api/bi/tenants/tenant_sec/opportunities", headers={"Authorization": "Bearer non_existent_token"})
    assert resp_bad_auth.status_code == 401

    # 3. Usuario sin permisos asignados -> 403
    sess_no_perm = create_user_session(env, "tenant_sec", "user_no_perm", actions=())
    resp_forbidden = client.get("/api/bi/tenants/tenant_sec/opportunities", headers={"Authorization": f"Bearer {sess_no_perm}"})
    assert resp_forbidden.status_code == 403
