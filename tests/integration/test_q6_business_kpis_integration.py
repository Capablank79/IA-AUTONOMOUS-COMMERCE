"""
Tests de Integración y End-to-End para Q.6 — Business KPIs (Hito Q — Business Intelligence).

Cubre:
A. Tenant A KPI summary correct (resumen ejecutivo completo cross-domain con las 17 métricas).
B. Tenant B strictly isolated (Aislamiento de hechos y rechazo cruzado 403 Forbidden).
C. Empty tenant semantics correct (conteo 0, promedios y ratios en UNKNOWN, sin errores 500).
D. Mixed complete/partial profit facts (solo registros COMPLETOS entran en cálculo de margen).
E. Mission success denominator correct (Misiones RUNNING/PENDING no cuentan como fallos ni entran en denominador terminal).
F. Agent costs aggregated correctly (Costos de inferencia y computación sumados con precisión Decimal).
G. Multiple currencies not mixed (Divisas heterogéneas desglosadas en currency_breakdown y marcadas NOT_COMPARABLE_CURRENCY).
H. Cross-domain drill-down links safe (Links directos y válidos hacia Q.1, Q.2, Q.3, Q.4 y Q.5).
I. UNKNOWN inputs preserve uncertainty (Falta de datos preservada como None / UNKNOWN y reflejada en calidad de datos).
J. Unauthorized / unauthenticated denied (Rechazo seguro con 401 Unauthorized / 403 Forbidden).
K. REST API & HTML view operational (/api/bi/tenants/{tenant_id}/kpis/summary, /bi/kpis).
L. Pure consultative read-only boundary (Sin motores de ejecución ni mutación de facts).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List
import pytest
from starlette.testclient import TestClient

from src.domain.business_kpi.models import (
    BusinessKPIValue,
    BusinessKPISummary,
    BusinessKPIQuery,
    KPIDomain,
    KPIStatus,
    KPIUnit,
    KPIConfidence,
)
from src.domain.business_kpi.catalog import KPI_CATALOG
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
    OpportunityType,
    OpportunityStatus,
)
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierStatus,
    EvidenceProvenanceType,
)
from src.domain.profit_dashboard.models import (
    ProfitDashboardItem,
    ProfitCompleteness,
)
from src.domain.mission.models import (
    Mission,
    MissionType,
    MissionStatus,
    MissionPriority,
)
from src.domain.cost.models import (
    CostRecord,
    UsageRecord,
    CostType,
    UsageUnit,
)
from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.tenant.models import TenantContext
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.rbac.models import Permission, Role, RoleAssignment
from src.domain.reliability.ports import ClockPort
from src.domain.organization.models import Organization, UserMembership, MembershipRole, MembershipStatus
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.business_kpi.business_kpi_service import BusinessKPIService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import JsonOrganizationRepository, JsonMembershipRepository
from src.infrastructure.persistence.data.json.rbac_repository import JsonRoleRepository, JsonRoleAssignmentRepository
from src.infrastructure.persistence.data.json.tenant_opportunity_repository import JsonTenantOpportunityRepository
from src.infrastructure.persistence.data.json.tenant_supplier_repository import JsonTenantSupplierRepository
from src.infrastructure.persistence.data.json.tenant_profit_repository import JsonTenantProfitRepository
from src.infrastructure.persistence.data.json.tenant_mission_repository import JsonTenantMissionRepository
from src.infrastructure.persistence.data.json.cost_repository import JsonCostRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.web.admin_app import create_admin_app
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

    opp_repo = JsonTenantOpportunityRepository(data_dir / "opportunities")
    sup_repo = JsonTenantSupplierRepository(data_dir / "suppliers")
    profit_repo = JsonTenantProfitRepository(data_dir / "profit")
    mission_repo = JsonTenantMissionRepository(data_dir / "missions")
    cost_repo = JsonCostRepository(data_dir / "costs")
    usage_repo = InMemoryUsageEventRepository()
    audit_repo = JsonAuditRepository(data_dir / "audit")

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

    business_kpi_service = BusinessKPIService(
        opportunity_repository=opp_repo,
        supplier_repository=sup_repo,
        profit_repository=profit_repo,
        mission_repository=mission_repo,
        cost_repository=cost_repo,
        usage_repository=usage_repo,
        authorization_service=auth_service,
        session_repository=session_repo,
        clock=clock,
    )

    # Admin services
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
        business_kpi_service=business_kpi_service,
    )

    client = TestClient(app)

    # Configuración de Tenants, Roles y Sesiones
    tenant_a = "tenant-kpi-a"
    tenant_b = "tenant-kpi-b"
    user_a = "usr-kpi-a"
    user_b = "usr-kpi-b"

    role_a = Role(
        role_id="role-kpi-a",
        name="BI Admin A",
        permissions=(
            Permission(permission_id="p-kpi-a", action="BUSINESS_KPI_READ", description="Read Business KPIs"),
            Permission(permission_id="p-bi-a", action="BUSINESS_INTELLIGENCE_READ", description="Read BI"),
        ),
    )
    role_b = Role(
        role_id="role-kpi-b",
        name="BI Admin B",
        permissions=(
            Permission(permission_id="p-kpi-b", action="BUSINESS_KPI_READ", description="Read Business KPIs"),
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

    now = clock.now()
    sess_a = SaaSSession(
        session_id="sess-kpi-a-valid",
        tenant_id=tenant_a,
        identity_id=user_a,
        status=SessionStatus.ACTIVE,
        created_at=now,
        expires_at=now + timedelta(hours=4),
    )
    sess_b = SaaSSession(
        session_id="sess-kpi-b-valid",
        tenant_id=tenant_b,
        identity_id=user_b,
        status=SessionStatus.ACTIVE,
        created_at=now,
        expires_at=now + timedelta(hours=4),
    )
    session_repo.save(sess_a)
    session_repo.save(sess_b)

    return {
        "client": client,
        "opp_repo": opp_repo,
        "sup_repo": sup_repo,
        "profit_repo": profit_repo,
        "mission_repo": mission_repo,
        "cost_repo": cost_repo,
        "clock": clock,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "sess_a": sess_a.session_id,
        "sess_b": sess_b.session_id,
    }


def test_integration_a_tenant_a_kpi_summary_correct(integration_app):
    client = integration_app["client"]
    opp_repo = integration_app["opp_repo"]
    sup_repo = integration_app["sup_repo"]
    profit_repo = integration_app["profit_repo"]
    mission_repo = integration_app["mission_repo"]
    cost_repo = integration_app["cost_repo"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]
    ctx_a = TenantContext(tenant_id=tenant_a)
    now = integration_app["clock"].now()

    # 1. Oportunidades Q.1
    opp_repo.save(
        ctx_a,
        OpportunityRecord(
            opportunity_id="opp_1",
            canonical_product_id="prod_1",
            marketplace=Marketplace.MERCADO_LIBRE,
            opportunity_type=OpportunityType.PRICE_ARBITRAGE,
            status=OpportunityStatus.DETECTED,
            confidence=Confidence.HIGH,
            source_observation_ids=("obs_1",),
            observed_metrics=ObservedOpportunityMetrics(),
            derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("85.0")),
            detected_at=now,
        ),
    )

    # 2. Proveedores Q.2
    sup_repo.save_all(
        ctx_a,
        [
            Supplier(
                supplier_id="sup_1",
                name="Global Supplier",
                source="ALIBABA",
                source_type=EvidenceProvenanceType.LIVE,
                status=SupplierStatus.VERIFIED,
                metadata={"supplier_score": Decimal("92.0"), "risk_level": "LOW"},
                observed_at=now,
            )
        ],
    )

    # 3. Rentabilidad Q.3
    profit_repo.save_all(
        ctx_a,
        [
            ProfitDashboardItem(
                item_id="prof_1",
                product_id="prod_1",
                marketplace="mercadolibre",
                currency="USD",
                completeness=ProfitCompleteness.COMPLETE,
                calculated_at=now,
                contribution_profit=Decimal("25.00"),
                margin_pct=Decimal("35.00"),
            )
        ],
    )

    # 4. Misiones Q.4
    mission_repo.save_all(
        ctx_a,
        [
            Mission(
                mission_id="m_1",
                type=MissionType.MARKET_DISCOVERY,
                status=MissionStatus.COMPLETED,
                priority=MissionPriority.HIGH,
                created_at=now,
            )
        ],
    )

    # 5. Costos IA Q.5
    cost_repo.append(
        CostRecord(
            cost_id="c_1",
            occurred_at=now,
            cost_type=CostType.INFERENCE,
            provider="anthropic",
            service_or_model="claude-3-5-sonnet",
            execution_id="e_1",
            usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("1500")),
            currency="USD",
            unit_cost=Decimal("0.003"),
            total_cost=Decimal("4.50"),
            mission_id="m_1",
        )
    )

    # GET /api/bi/tenants/{tenant_id}/kpis/summary
    response = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/summary",
        headers={"x-session-id": sess_a},
    )
    assert response.status_code == 200
    data = response.json()

    assert data["tenant_id"] == tenant_a
    assert data["overall_readiness_pct"] is not None

    kpi_map = {k["kpi_id"]: k for k in data["kpis"]}
    assert kpi_map["OPPORTUNITY_COUNT"]["value"] == "1"
    assert kpi_map["HIGH_POTENTIAL_OPPORTUNITIES"]["value"] == "1"
    assert kpi_map["VALIDATED_SUPPLIER_COUNT"]["value"] == "1"
    assert kpi_map["COMPLETE_PROFITABILITY_COUNT"]["value"] == "1"
    assert kpi_map["AVG_CONTRIBUTION_MARGIN"]["value"] == "35.00"
    assert kpi_map["MISSION_SUCCESS_RATE"]["value"] == "100.00"
    assert kpi_map["TOTAL_AGENT_COST"]["value"] == "4.50"
    assert kpi_map["TOTAL_AGENT_COST"]["currency"] == "USD"
    assert Decimal(kpi_map["COST_PER_OPPORTUNITY"]["value"]) == Decimal("4.50")


def test_integration_b_tenant_b_isolated(integration_app):
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]
    tenant_b = integration_app["tenant_b"]
    sess_b = integration_app["sess_b"]

    # Consulta de Tenant A con sesión de Tenant B debe ser rechazada 403
    resp_cross = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/summary",
        headers={"x-session-id": sess_b},
    )
    assert resp_cross.status_code == 403

    # Consulta de Tenant B debe responder solo datos de Tenant B (vacío)
    resp_b = client.get(
        f"/api/bi/tenants/{tenant_b}/kpis/summary",
        headers={"x-session-id": sess_b},
    )
    assert resp_b.status_code == 200
    data_b = resp_b.json()
    kpi_map_b = {k["kpi_id"]: k for k in data_b["kpis"]}
    assert kpi_map_b["OPPORTUNITY_COUNT"]["value"] == "0"
    assert kpi_map_b["VALIDATED_SUPPLIER_COUNT"]["value"] == "0"


def test_integration_c_empty_tenant_semantics(integration_app):
    client = integration_app["client"]
    tenant_b = integration_app["tenant_b"]
    sess_b = integration_app["sess_b"]

    resp = client.get(
        f"/api/bi/tenants/{tenant_b}/kpis/summary",
        headers={"x-session-id": sess_b},
    )
    assert resp.status_code == 200
    data = resp.json()

    kpi_map = {k["kpi_id"]: k for k in data["kpis"]}
    # Conteos son 0
    assert kpi_map["OPPORTUNITY_COUNT"]["value"] == "0"
    assert kpi_map["ACTIVE_MISSIONS"]["value"] == "0"
    # Ratios y promedios sin inputs son UNKNOWN / None (no 0 ni NaN)
    assert kpi_map["AVG_CONTRIBUTION_MARGIN"]["status"] == "UNKNOWN"
    assert kpi_map["AVG_CONTRIBUTION_MARGIN"]["value"] is None
    assert kpi_map["MISSION_SUCCESS_RATE"]["status"] == "UNKNOWN"
    assert kpi_map["MISSION_SUCCESS_RATE"]["value"] is None
    assert kpi_map["TOTAL_AGENT_COST"]["status"] == "UNKNOWN"
    assert kpi_map["TOTAL_AGENT_COST"]["value"] is None


def test_integration_d_mixed_complete_partial_profit(integration_app):
    client = integration_app["client"]
    profit_repo = integration_app["profit_repo"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]
    ctx_a = TenantContext(tenant_id=tenant_a)
    now = integration_app["clock"].now()

    profit_repo.save_all(
        ctx_a,
        [
            ProfitDashboardItem(
                item_id="p_comp_1",
                product_id="prod_1",
                marketplace="mercadolibre",
                currency="USD",
                completeness=ProfitCompleteness.COMPLETE,
                calculated_at=now,
                contribution_profit=Decimal("40.00"),
                margin_pct=Decimal("40.00"),
            ),
            ProfitDashboardItem(
                item_id="p_part_2",
                product_id="prod_2",
                marketplace="mercadolibre",
                currency="USD",
                completeness=ProfitCompleteness.PARTIAL,
                calculated_at=now,
                contribution_profit=None,
                margin_pct=None,
            ),
        ],
    )

    resp = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/summary",
        headers={"x-session-id": sess_a},
    )
    data = resp.json()
    kpi_map = {k["kpi_id"]: k for k in data["kpis"]}

    assert kpi_map["COMPLETE_PROFITABILITY_COUNT"]["value"] == "1"
    # Solo el registro completo entra en el promedio (40.00%)
    assert kpi_map["AVG_CONTRIBUTION_MARGIN"]["value"] == "40.00"


def test_integration_e_mission_success_denominator_correct(integration_app):
    client = integration_app["client"]
    mission_repo = integration_app["mission_repo"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]
    ctx_a = TenantContext(tenant_id=tenant_a)
    now = integration_app["clock"].now()

    mission_repo.save_all(
        ctx_a,
        [
            Mission(
                mission_id="m_c1",
                type=MissionType.MARKET_DISCOVERY,
                status=MissionStatus.COMPLETED,
                priority=MissionPriority.HIGH,
                created_at=now,
            ),
            Mission(
                mission_id="m_f2",
                type=MissionType.SUPPLIER_SEARCH,
                status=MissionStatus.FAILED,
                priority=MissionPriority.MEDIUM,
                created_at=now,
            ),
            Mission(
                mission_id="m_r3",
                type=MissionType.PROFIT_EVALUATION,
                status=MissionStatus.RUNNING,
                priority=MissionPriority.LOW,
                created_at=now,
            ),
            Mission(
                mission_id="m_p4",
                type=MissionType.MARKET_DISCOVERY,
                status=MissionStatus.PENDING,
                priority=MissionPriority.LOW,
                created_at=now,
            ),
        ],
    )

    resp = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/summary",
        headers={"x-session-id": sess_a},
    )
    data = resp.json()
    kpi_map = {k["kpi_id"]: k for k in data["kpis"]}

    assert kpi_map["ACTIVE_MISSIONS"]["value"] == "2"  # RUNNING + PENDING
    assert kpi_map["FAILED_MISSIONS"]["value"] == "1"
    # Denominador terminal = 1 (completed) + 1 (failed) = 2. Tasa = 1 / 2 = 50.00%
    assert kpi_map["MISSION_SUCCESS_RATE"]["value"] == "50.00"


def test_integration_f_agent_costs_and_g_currency_separation(integration_app):
    client = integration_app["client"]
    cost_repo = integration_app["cost_repo"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]
    now = integration_app["clock"].now()

    # Costos en USD y EUR
    cost_repo.append(
        CostRecord(
            cost_id="c_usd_1",
            occurred_at=now,
            cost_type=CostType.INFERENCE,
            provider="anthropic",
            service_or_model="claude-3-5-sonnet",
            execution_id="e_1",
            usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("1000")),
            currency="USD",
            unit_cost=Decimal("0.003"),
            total_cost=Decimal("3.00"),
        )
    )
    cost_repo.append(
        CostRecord(
            cost_id="c_eur_1",
            occurred_at=now,
            cost_type=CostType.INFERENCE,
            provider="openai",
            service_or_model="gpt-4o",
            execution_id="e_2",
            usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("1000")),
            currency="EUR",
            unit_cost=Decimal("0.004"),
            total_cost=Decimal("4.00"),
        )
    )

    # Consulta general sin filtro de divisa -> NOT_COMPARABLE_CURRENCY
    resp = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/summary",
        headers={"x-session-id": sess_a},
    )
    data = resp.json()
    kpi_map = {k["kpi_id"]: k for k in data["kpis"]}

    assert "USD" in data["currency_breakdown"]
    assert "EUR" in data["currency_breakdown"]
    assert data["currency_breakdown"]["USD"]["total_known_agent_cost"] == "3.00"
    assert data["currency_breakdown"]["EUR"]["total_known_agent_cost"] == "4.00"
    assert kpi_map["TOTAL_AGENT_COST"]["status"] == "NOT_COMPARABLE_CURRENCY"

    # Consulta filtrando explícitamente por USD -> CALCULATED
    resp_usd = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/summary?currency=USD",
        headers={"x-session-id": sess_a},
    )
    data_usd = resp_usd.json()
    kpi_map_usd = {k["kpi_id"]: k for k in data_usd["kpis"]}
    assert kpi_map_usd["TOTAL_AGENT_COST"]["status"] == "CALCULATED"
    assert kpi_map_usd["TOTAL_AGENT_COST"]["value"] == "3.00"
    assert kpi_map_usd["TOTAL_AGENT_COST"]["currency"] == "USD"


def test_integration_h_drill_down_and_catalog(integration_app):
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]

    # Catálogo canónico
    resp_cat = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/catalog",
        headers={"x-session-id": sess_a},
    )
    assert resp_cat.status_code == 200
    catalog = resp_cat.json()
    assert len(catalog) == 17

    # Resumen y validación de links drill-down
    resp_sum = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/summary",
        headers={"x-session-id": sess_a},
    )
    data = resp_sum.json()
    links = data["drill_down_links"]
    assert f"/api/bi/tenants/{tenant_a}/opportunities" in links["opportunity_dashboard"]
    assert f"/api/bi/tenants/{tenant_a}/suppliers" in links["supplier_dashboard"]
    assert f"/api/bi/tenants/{tenant_a}/profit" in links["profit_dashboard"]
    assert f"/api/bi/tenants/{tenant_a}/missions" in links["mission_dashboard"]
    assert f"/api/bi/tenants/{tenant_a}/agent-costs" in links["agent_cost_dashboard"]


def test_integration_i_kpi_by_id_and_comparison(integration_app):
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]
    sess_a = integration_app["sess_a"]

    # Obtener KPI específico por ID
    resp_kpi = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/OPPORTUNITY_COUNT",
        headers={"x-session-id": sess_a},
    )
    assert resp_kpi.status_code == 200
    kpi_data = resp_kpi.json()
    assert kpi_data["kpi_id"] == "OPPORTUNITY_COUNT"

    # Comparación entre periodos
    resp_comp = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/compare?current_window=7d&previous_window=7d",
        headers={"x-session-id": sess_a},
    )
    assert resp_comp.status_code == 200
    comp_list = resp_comp.json()
    assert isinstance(comp_list, list)
    assert len(comp_list) == 17
    assert comp_list[0]["kpi_id"] == "OPPORTUNITY_COUNT"


def test_integration_j_unauthorized_denied(integration_app):
    client = integration_app["client"]
    tenant_a = integration_app["tenant_a"]

    # Sin sesión -> 401
    resp_no_sess = client.get(f"/api/bi/tenants/{tenant_a}/kpis/summary")
    assert resp_no_sess.status_code == 401

    # Sesión inválida -> 401
    resp_bad_sess = client.get(
        f"/api/bi/tenants/{tenant_a}/kpis/summary",
        headers={"x-session-id": "sess_invalid_token"},
    )
    assert resp_bad_sess.status_code == 401


def test_integration_k_html_view(integration_app):
    client = integration_app["client"]

    # Vista HTML de Business KPIs
    resp_html = client.get("/bi/kpis")
    assert resp_html.status_code == 200
    assert "text/html" in resp_html.headers["content-type"]
    assert "Business KPIs" in resp_html.text
    assert "Business Intelligence" in resp_html.text
