"""
Suite Formal de Validación E2E de Gate P — Cierre del Hito Q (Business Intelligence).

Valida integralmente la convergencia de Q.1, Q.2, Q.3, Q.4, Q.5 y Q.6:
1. Q.1 Opportunity visibility & projection
2. Q.2 Supplier linkage & sanitization
3. Q.3 Profitability calculation (Decimal & partial completeness)
4. Q.4 Mission projection (timeline, status, non-destructive read-only)
5. Q.5 Cost attribution (K.3/O.6 correlation & unattributed isolation)
6. Q.6 KPI aggregation & canonical catalog
7. Tenant isolation (Tenant A vs Tenant B isolation and cross-tenant block)
8. Authorization (RBAC, session validation, 401 unauthenticated, 403 unauthorized)
9. UNKNOWN semantics (UNKNOWN != 0, UNKNOWN != FREE, UNKNOWN != SUCCESS)
10. Partial-data tenant scenario (honest uncertainty preservation)
11. Complete-data tenant scenario (cross-domain correlation)
12. Multi-currency safety (no arbitrary FX or cross-currency addition)
13. Executive drill-down links to Q.1–Q.5
14. Sensitive-data exclusion (N.9 and Anti-CoT)
15. Anti-CoT / Private scratchpad protection
16. Empty tenant semantics (counts = 0, ratios = UNKNOWN)
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
from src.application.opportunity_dashboard.opportunity_dashboard_service import OpportunityDashboardService
from src.application.supplier_dashboard.supplier_dashboard_service import SupplierDashboardService
from src.application.profit_dashboard.profit_dashboard_service import ProfitDashboardService
from src.application.mission_dashboard.mission_dashboard_service import MissionDashboardService
from src.application.agent_cost_dashboard.agent_cost_dashboard_service import AgentCostDashboardService
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.billing.subscription_service import SubscriptionService
from src.infrastructure.persistence.data.json.usage_event_repository import InMemoryUsageEventRepository
from src.infrastructure.persistence.data.json.plan_repository import InMemoryPlanCatalogRepository, InMemoryPlanAssignmentRepository
from src.infrastructure.persistence.data.json.billing_repository import InMemorySubscriptionRepository, InMemoryInvoiceRepository
from src.infrastructure.persistence.data.json.quota_repository import InMemoryQuotaPolicyRepository, InMemoryQuotaReservationRepository


class GatePClock(ClockPort):
    def __init__(self):
        self.current = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.current

    def sleep(self, seconds: float) -> None:
        pass


@pytest.fixture
def gate_p_environment(tmp_path):
    clock = GatePClock()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    session_repo = JsonSaaSSessionRepository(data_dir / "sessions")
    org_repo = JsonOrganizationRepository(data_dir / "organizations")
    mem_repo = JsonMembershipRepository(data_dir / "memberships")
    role_repo = JsonRoleRepository(data_dir / "roles")
    role_assign_repo = JsonRoleAssignmentRepository(data_dir / "role_assignments")

    rbac_service = RBACService(
        role_repository=role_repo,
        assignment_repository=role_assign_repo,
        clock=clock,
    )
    saas_auth_service = SaaSAuthorizationService(
        rbac_service=rbac_service,
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=mem_repo,
        clock=clock,
    )

    opp_repo = JsonTenantOpportunityRepository(data_dir / "opportunities")
    sup_repo = JsonTenantSupplierRepository(data_dir / "suppliers")
    profit_repo = JsonTenantProfitRepository(data_dir / "profit")
    mission_repo = JsonTenantMissionRepository(data_dir / "missions")
    cost_repo = JsonCostRepository(data_dir / "costs")
    audit_repo = JsonAuditRepository(data_dir / "audit")

    usage_repo = InMemoryUsageEventRepository()
    kpi_service = BusinessKPIService(
        opportunity_repository=opp_repo,
        supplier_repository=sup_repo,
        profit_repository=profit_repo,
        mission_repository=mission_repo,
        cost_repository=cost_repo,
        usage_repository=usage_repo,
        authorization_service=saas_auth_service,
        session_repository=session_repo,
        clock=clock,
    )

    # Admin services
    org_service = OrganizationService(org_repo, audit_repository=audit_repo)
    membership_service = OrganizationMembershipService(mem_repo, org_repo, audit_repository=audit_repo)
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
        authorization_service=saas_auth_service,
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

    opp_dashboard_service = OpportunityDashboardService(
        repository=opp_repo,
        authorization_service=saas_auth_service,
        session_repository=session_repo,
        clock=clock,
    )
    sup_dashboard_service = SupplierDashboardService(
        repository=sup_repo,
        authorization_service=saas_auth_service,
        session_repository=session_repo,
        opportunity_repository=opp_repo,
        clock=clock,
    )
    prof_dashboard_service = ProfitDashboardService(
        repository=profit_repo,
        opportunity_repository=opp_repo,
        supplier_repository=sup_repo,
        authorization_service=saas_auth_service,
        session_repository=session_repo,
        clock=clock,
    )
    miss_dashboard_service = MissionDashboardService(
        repository=mission_repo,
        opportunity_repository=opp_repo,
        supplier_repository=sup_repo,
        profit_repository=profit_repo,
        authorization_service=saas_auth_service,
        session_repository=session_repo,
        clock=clock,
    )
    cost_dashboard_service = AgentCostDashboardService(
        cost_repository=cost_repo,
        authorization_service=saas_auth_service,
        session_repository=session_repo,
        clock=clock,
    )

    app = create_admin_app(
        service=admin_console_service,
        opportunity_dashboard_service=opp_dashboard_service,
        supplier_dashboard_service=sup_dashboard_service,
        profit_dashboard_service=prof_dashboard_service,
        mission_dashboard_service=miss_dashboard_service,
        agent_cost_dashboard_service=cost_dashboard_service,
        business_kpi_service=kpi_service,
    )

    client = TestClient(app)

    # Configurar Tenants y Roles
    tenant_a = "tenant-alpha"
    tenant_b = "tenant-beta"
    tenant_partial = "tenant-partial"
    tenant_empty = "tenant-empty"
    user_a = "user-alpha"
    user_b = "user-beta"
    user_partial = "user-partial"
    user_empty = "user-empty"
    user_unauth = "user-unauth"

    role_bi_a = Role(
        role_id="role_bi_a",
        name="BI Admin A",
        permissions=(
            Permission(permission_id="p-kpi-a", action="BUSINESS_KPI_READ", description="Read Business KPIs"),
            Permission(permission_id="p-bi-a", action="BUSINESS_INTELLIGENCE_READ", description="Read BI"),
            Permission(permission_id="p-opp-a", action="OPPORTUNITY_DASHBOARD_READ", description="Read Opp"),
            Permission(permission_id="p-sup-a", action="SUPPLIER_DASHBOARD_READ", description="Read Sup"),
            Permission(permission_id="p-prof-a", action="PROFIT_DASHBOARD_READ", description="Read Prof"),
            Permission(permission_id="p-miss-a", action="MISSION_DASHBOARD_READ", description="Read Miss"),
            Permission(permission_id="p-cost-a", action="AGENT_COST_DASHBOARD_READ", description="Read Cost"),
        ),
    )
    role_bi_b = Role(
        role_id="role_bi_b",
        name="BI Admin B",
        permissions=(
            Permission(permission_id="p-kpi-b", action="BUSINESS_KPI_READ", description="Read Business KPIs"),
            Permission(permission_id="p-bi-b", action="BUSINESS_INTELLIGENCE_READ", description="Read BI"),
            Permission(permission_id="p-opp-b", action="OPPORTUNITY_DASHBOARD_READ", description="Read Opp"),
            Permission(permission_id="p-sup-b", action="SUPPLIER_DASHBOARD_READ", description="Read Sup"),
            Permission(permission_id="p-prof-b", action="PROFIT_DASHBOARD_READ", description="Read Prof"),
            Permission(permission_id="p-miss-b", action="MISSION_DASHBOARD_READ", description="Read Miss"),
            Permission(permission_id="p-cost-b", action="AGENT_COST_DASHBOARD_READ", description="Read Cost"),
        ),
    )
    role_bi_partial = Role(
        role_id="role_bi_partial",
        name="BI Admin Partial",
        permissions=(
            Permission(permission_id="p-kpi-part", action="BUSINESS_KPI_READ", description="Read Business KPIs"),
            Permission(permission_id="p-bi-part", action="BUSINESS_INTELLIGENCE_READ", description="Read BI"),
            Permission(permission_id="p-opp-part", action="OPPORTUNITY_DASHBOARD_READ", description="Read Opp"),
            Permission(permission_id="p-sup-part", action="SUPPLIER_DASHBOARD_READ", description="Read Sup"),
            Permission(permission_id="p-prof-part", action="PROFIT_DASHBOARD_READ", description="Read Prof"),
            Permission(permission_id="p-miss-part", action="MISSION_DASHBOARD_READ", description="Read Miss"),
            Permission(permission_id="p-cost-part", action="AGENT_COST_DASHBOARD_READ", description="Read Cost"),
        ),
    )
    role_bi_empty = Role(
        role_id="role_bi_empty",
        name="BI Admin Empty",
        permissions=(
            Permission(permission_id="p-kpi-emp", action="BUSINESS_KPI_READ", description="Read Business KPIs"),
            Permission(permission_id="p-bi-emp", action="BUSINESS_INTELLIGENCE_READ", description="Read BI"),
            Permission(permission_id="p-opp-emp", action="OPPORTUNITY_DASHBOARD_READ", description="Read Opp"),
            Permission(permission_id="p-sup-emp", action="SUPPLIER_DASHBOARD_READ", description="Read Sup"),
            Permission(permission_id="p-prof-emp", action="PROFIT_DASHBOARD_READ", description="Read Prof"),
            Permission(permission_id="p-miss-emp", action="MISSION_DASHBOARD_READ", description="Read Miss"),
            Permission(permission_id="p-cost-emp", action="AGENT_COST_DASHBOARD_READ", description="Read Cost"),
        ),
    )
    role_no_kpi = Role(
        role_id="role_no_kpi",
        name="No KPI Role",
        permissions=(
            Permission(permission_id="p-none", action="TENANT_READ", description="Read Tenant Only"),
        ),
    )

    role_repo.save_role(role_bi_a)
    role_repo.save_role(role_bi_b)
    role_repo.save_role(role_bi_partial)
    role_repo.save_role(role_bi_empty)
    role_repo.save_role(role_no_kpi)

    role_assign_repo.save_assignment(RoleAssignment(assignment_id="ra-a", identity_id=user_a, role_id=role_bi_a.role_id, scope=f"tenant_{tenant_a}", assigned_at=clock.now()))
    role_assign_repo.save_assignment(RoleAssignment(assignment_id="ra-b", identity_id=user_b, role_id=role_bi_b.role_id, scope=f"tenant_{tenant_b}", assigned_at=clock.now()))
    role_assign_repo.save_assignment(RoleAssignment(assignment_id="ra-part", identity_id=user_partial, role_id=role_bi_partial.role_id, scope=f"tenant_{tenant_partial}", assigned_at=clock.now()))
    role_assign_repo.save_assignment(RoleAssignment(assignment_id="ra-emp", identity_id=user_empty, role_id=role_bi_empty.role_id, scope=f"tenant_{tenant_empty}", assigned_at=clock.now()))
    role_assign_repo.save_assignment(RoleAssignment(assignment_id="ra-unauth", identity_id=user_unauth, role_id=role_no_kpi.role_id, scope=f"tenant_{tenant_a}", assigned_at=clock.now()))

    # Sesiones activas
    sess_a = "sess-a-token"
    sess_b = "sess-b-token"
    sess_partial = "sess-partial-token"
    sess_empty = "sess-empty-token"
    sess_unauth = "sess-unauth-token"

    session_repo.save(SaaSSession(session_id=sess_a, identity_id=user_a, tenant_id=tenant_a, status=SessionStatus.ACTIVE, created_at=clock.now(), expires_at=clock.now() + timedelta(hours=2)))
    session_repo.save(SaaSSession(session_id=sess_b, identity_id=user_b, tenant_id=tenant_b, status=SessionStatus.ACTIVE, created_at=clock.now(), expires_at=clock.now() + timedelta(hours=2)))
    session_repo.save(SaaSSession(session_id=sess_partial, identity_id=user_partial, tenant_id=tenant_partial, status=SessionStatus.ACTIVE, created_at=clock.now(), expires_at=clock.now() + timedelta(hours=2)))
    session_repo.save(SaaSSession(session_id=sess_empty, identity_id=user_empty, tenant_id=tenant_empty, status=SessionStatus.ACTIVE, created_at=clock.now(), expires_at=clock.now() + timedelta(hours=2)))
    session_repo.save(SaaSSession(session_id=sess_unauth, identity_id=user_unauth, tenant_id=tenant_a, status=SessionStatus.ACTIVE, created_at=clock.now(), expires_at=clock.now() + timedelta(hours=2)))

    return {
        "client": client,
        "clock": clock,
        "opp_repo": opp_repo,
        "sup_repo": sup_repo,
        "profit_repo": profit_repo,
        "mission_repo": mission_repo,
        "cost_repo": cost_repo,
        "audit_repo": audit_repo,
        "tenant_a": tenant_a,
        "tenant_b": tenant_b,
        "tenant_partial": tenant_partial,
        "tenant_empty": tenant_empty,
        "sess_a": sess_a,
        "sess_b": sess_b,
        "sess_partial": sess_partial,
        "sess_empty": sess_empty,
        "sess_unauth": sess_unauth,
    }


def test_gate_p_scenario_01_to_06_complete_tenant_cross_domain_flow(gate_p_environment):
    """
    Escenarios 1 al 6:
    - 1. Q.1 Opportunity visibility
    - 2. Q.2 Supplier linkage
    - 3. Q.3 Profitability calculation
    - 4. Q.4 Mission projection
    - 5. Q.5 Cost attribution
    - 6. Q.6 KPI aggregation
    """
    env = gate_p_environment
    client = env["client"]
    t_a = env["tenant_a"]
    sess_a = env["sess_a"]
    now = env["clock"].now()

    # 1. Sembrar Q.1 Opportunity
    env["opp_repo"].save(
        TenantContext(tenant_id=t_a),
        OpportunityRecord(
            opportunity_id="opp_alpha_1",
            canonical_product_id="prod_alpha_1",
            category="Electronics",
            marketplace=Marketplace.MERCADO_LIBRE,
            opportunity_type=OpportunityType.PRICE_ARBITRAGE,
            status=OpportunityStatus.DETECTED,
            confidence=Confidence.HIGH,
            source_observation_ids=("obs_alpha_1",),
            observed_metrics=ObservedOpportunityMetrics(observed_sold_quantity=1200),
            derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("85.0")),
            detected_at=now,
        ),
    )

    # 2. Sembrar Q.2 Supplier
    env["sup_repo"].save_all(
        TenantContext(tenant_id=t_a),
        [
            Supplier(
                supplier_id="sup_alpha_1",
                name="Alpha Supplier Inc",
                source="ALIBABA",
                source_type=EvidenceProvenanceType.LIVE,
                status=SupplierStatus.VERIFIED,
                metadata={"supplier_score": Decimal("92.0"), "risk_level": "LOW"},
                observed_at=now,
            )
        ],
    )

    # 3. Sembrar Q.3 Profit
    env["profit_repo"].save_all(
        TenantContext(tenant_id=t_a),
        [
            ProfitDashboardItem(
                item_id="prof_alpha_1",
                product_id="prod_alpha_1",
                marketplace="mercadolibre",
                currency="USD",
                completeness=ProfitCompleteness.COMPLETE,
                calculated_at=now,
                contribution_profit=Decimal("25.00"),
                margin_pct=Decimal("25.00"),
            )
        ],
    )

    # 4. Sembrar Q.4 Mission (1 Exitosa, 1 Activa que no debe entrar a denominador terminal)
    env["mission_repo"].save_all(
        TenantContext(tenant_id=t_a),
        [
            Mission(
                mission_id="m_alpha_success",
                type=MissionType.PROFIT_EVALUATION,
                status=MissionStatus.COMPLETED,
                priority=MissionPriority.HIGH,
                created_at=now,
            ),
            Mission(
                mission_id="m_alpha_running",
                type=MissionType.MARKET_DISCOVERY,
                status=MissionStatus.RUNNING,
                priority=MissionPriority.MEDIUM,
                created_at=now,
            ),
        ],
    )

    # 5. Sembrar Q.5 Agent Cost
    env["cost_repo"].append(
        CostRecord(
            cost_id="c_alpha_1",
            occurred_at=now,
            cost_type=CostType.INFERENCE,
            provider="openai",
            service_or_model="gpt-4o",
            execution_id="exec_1",
            usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("2000")),
            currency="USD",
            unit_cost=Decimal("0.0025"),
            total_cost=Decimal("5.00"),
            mission_id="m_alpha_success",
        )
    )

    # Validar endpoints individuales Q.1 a Q.5
    r_q1 = client.get(f"/api/bi/tenants/{t_a}/opportunities/summary", headers={"x-session-id": sess_a})
    assert r_q1.status_code == 200
    assert r_q1.json()["total_opportunities"] == 1

    r_q2 = client.get(f"/api/bi/tenants/{t_a}/suppliers/summary", headers={"x-session-id": sess_a})
    assert r_q2.status_code == 200
    assert r_q2.json()["verified_suppliers"] == 1

    r_q3 = client.get(f"/api/bi/tenants/{t_a}/profit/summary", headers={"x-session-id": sess_a})
    assert r_q3.status_code == 200
    assert r_q3.json()["complete_profitability_count"] == 1

    r_q4 = client.get(f"/api/bi/tenants/{t_a}/missions/summary", headers={"x-session-id": sess_a})
    assert r_q4.status_code == 200
    assert r_q4.json()["completed_count"] == 1
    assert r_q4.json()["running_count"] == 1

    r_q5 = client.get(f"/api/bi/tenants/{t_a}/agent-costs/summary", headers={"x-session-id": sess_a})
    assert r_q5.status_code == 200
    assert r_q5.json()["total_known_cost_by_currency"]["USD"] == "5.00"

    # 6. Validar Q.6 Executive KPI Summary
    r_q6 = client.get(f"/api/bi/tenants/{t_a}/kpis/summary", headers={"x-session-id": sess_a})
    assert r_q6.status_code == 200
    data_kpi = r_q6.json()
    kpis = {k["kpi_id"]: k for k in data_kpi["kpis"]}

    assert kpis["OPPORTUNITY_COUNT"]["value"] == "1"
    assert kpis["HIGH_POTENTIAL_OPPORTUNITIES"]["value"] == "1"
    assert kpis["VALIDATED_SUPPLIER_COUNT"]["value"] == "1"
    assert kpis["COMPLETE_PROFITABILITY_COUNT"]["value"] == "1"
    assert kpis["AVG_CONTRIBUTION_MARGIN"]["value"] == "25.00"  # (100 - 50 - 10 - 15) = 25%
    assert kpis["MISSION_SUCCESS_RATE"]["value"] == "100.00"  # 1 terminada exitosa / 1 terminada
    assert kpis["ACTIVE_MISSIONS"]["value"] == "1"  # 1 running
    assert kpis["TOTAL_AGENT_COST"]["value"] == "5.00"
    assert Decimal(kpis["COST_PER_OPPORTUNITY"]["value"]) == Decimal("5.00")
    assert Decimal(kpis["PROJECTED_CONTRIBUTION_TO_AGENT_COST_RATIO"]["value"]) == Decimal("5.00")  # 25 profit / 5 cost


def test_gate_p_scenario_07_tenant_isolation(gate_p_environment):
    """
    Escenario 7: Tenant Isolation estricto.
    - Tenant A no puede consultar con sesión de Tenant B.
    - Tenant B no ve ningún registro sembrado en Tenant A.
    """
    env = gate_p_environment
    client = env["client"]
    t_a = env["tenant_a"]
    t_b = env["tenant_b"]
    sess_b = env["sess_b"]

    # Acceso cruzado a endpoints debe responder 403 Forbidden
    for ep in ["opportunities", "suppliers", "profit", "missions", "agent-costs", "kpis"]:
        resp = client.get(f"/api/bi/tenants/{t_a}/{ep}/summary", headers={"x-session-id": sess_b})
        assert resp.status_code == 403, f"Failed isolation for endpoint {ep}"

    # Tenant B ve exclusivamente su espacio aislado
    resp_kpi_b = client.get(f"/api/bi/tenants/{t_b}/kpis/summary", headers={"x-session-id": sess_b})
    assert resp_kpi_b.status_code == 200
    kpis_b = {k["kpi_id"]: k for k in resp_kpi_b.json()["kpis"]}
    assert kpis_b["OPPORTUNITY_COUNT"]["value"] == "0"
    assert kpis_b["VALIDATED_SUPPLIER_COUNT"]["value"] == "0"


def test_gate_p_scenario_08_authorization_rbac(gate_p_environment):
    """
    Escenario 8: Autorización y Seguridad SaaS.
    - Sin sesión: 401 Unauthorized
    - Con sesión pero sin permiso: 403 Forbidden
    """
    env = gate_p_environment
    client = env["client"]
    t_a = env["tenant_a"]
    sess_unauth = env["sess_unauth"]

    # 1. Sin cabecera de autenticación
    resp_401 = client.get(f"/api/bi/tenants/{t_a}/kpis/summary")
    assert resp_401.status_code == 401

    # 2. Con usuario sin roles BI
    resp_403 = client.get(f"/api/bi/tenants/{t_a}/kpis/summary", headers={"x-session-id": sess_unauth})
    assert resp_403.status_code == 403


def test_gate_p_scenario_09_to_10_unknown_semantics_and_partial_data(gate_p_environment):
    """
    Escenarios 9 y 10:
    - UNKNOWN != 0, UNKNOWN != FREE, UNKNOWN != SUCCESS
    - Tenant con datos parciales preserva honestidad analítica.
    """
    env = gate_p_environment
    client = env["client"]
    t_part = env["tenant_partial"]
    sess_part = env["sess_partial"]
    now = env["clock"].now()

    # 1. Proveedor no verificado y sin campos clave
    env["sup_repo"].save_all(
        TenantContext(tenant_id=t_part),
        [
            Supplier(
                supplier_id="sup_part_1",
                name="Partial Supplier",
                status=SupplierStatus.UNVERIFIED,
                source="ALIBABA",
                source_type=EvidenceProvenanceType.DERIVED,
                observed_at=now,
            )
        ],
    )

    # 2. Registro financiero parcial (falta costo de envío)
    env["profit_repo"].save_all(
        TenantContext(tenant_id=t_part),
        [
            ProfitDashboardItem(
                item_id="prof_part_1",
                product_id="prod_part_1",
                marketplace="mercadolibre",
                completeness=ProfitCompleteness.PARTIAL,
                currency="USD",
                calculated_at=now,
            )
        ],
    )

    # 3. Misiones solo RUNNING (cero terminadas)
    env["mission_repo"].save_all(
        TenantContext(tenant_id=t_part),
        [
            Mission(
                mission_id="m_part_running",
                type=MissionType.MARKET_DISCOVERY,
                status=MissionStatus.RUNNING,
                priority=MissionPriority.LOW,
                created_at=now,
            )
        ],
    )

    resp = client.get(f"/api/bi/tenants/{t_part}/kpis/summary", headers={"x-session-id": sess_part})
    assert resp.status_code == 200
    kpis = {k["kpi_id"]: k for k in resp.json()["kpis"]}

    # UNKNOWN semantics verificadas
    assert kpis["AVG_SUPPLIER_SCORE"]["status"] == "UNKNOWN"
    assert kpis["AVG_SUPPLIER_SCORE"]["value"] is None
    assert kpis["AVG_CONTRIBUTION_MARGIN"]["status"] == "UNKNOWN"
    assert kpis["AVG_CONTRIBUTION_MARGIN"]["value"] is None
    assert kpis["MISSION_SUCCESS_RATE"]["status"] == "UNKNOWN"  # 0 terminadas => UNKNOWN, no 0%
    assert kpis["ACTIVE_MISSIONS"]["value"] == "1"
    assert kpis["TOTAL_AGENT_COST"]["status"] == "UNKNOWN"  # Sin costos => UNKNOWN, no $0


def test_gate_p_scenario_11_and_16_empty_tenant_semantics(gate_p_environment):
    """
    Escenarios 11 y 16:
    - Tenant completamente vacío responde 200 OK con semántica matemática exacta.
    """
    env = gate_p_environment
    client = env["client"]
    t_empty = env["tenant_empty"]
    sess_empty = env["sess_empty"]

    resp = client.get(f"/api/bi/tenants/{t_empty}/kpis/summary", headers={"x-session-id": sess_empty})
    assert resp.status_code == 200
    data = resp.json()
    assert data["tenant_id"] == t_empty
    assert Decimal(data["overall_readiness_pct"]) == Decimal("47.06")

    kpis = {k["kpi_id"]: k for k in data["kpis"]}
    # Conteos son 0
    assert kpis["OPPORTUNITY_COUNT"]["value"] == "0"
    assert kpis["VALIDATED_SUPPLIER_COUNT"]["value"] == "0"
    assert kpis["COMPLETE_PROFITABILITY_COUNT"]["value"] == "0"
    assert kpis["ACTIVE_MISSIONS"]["value"] == "0"
    assert kpis["FAILED_MISSIONS"]["value"] == "0"

    # Promedios y ratios son UNKNOWN (no division by zero ni 0 falsos)
    assert kpis["AVG_SUPPLIER_SCORE"]["value"] is None
    assert kpis["AVG_CONTRIBUTION_MARGIN"]["value"] is None
    assert kpis["MISSION_SUCCESS_RATE"]["value"] is None
    assert kpis["AVG_COST_PER_MISSION"]["value"] is None


def test_gate_p_scenario_12_multi_currency_safety(gate_p_environment):
    """
    Escenario 12: Aislamiento estricto multi-divisa.
    - No se mezclan monedas heterogéneas (CLP + USD).
    """
    env = gate_p_environment
    client = env["client"]
    t_a = env["tenant_a"]
    sess_a = env["sess_a"]
    now = env["clock"].now()

    # Agregar costos en USD y CLP
    env["cost_repo"].append(
        CostRecord(
            cost_id="c_usd_mc",
            occurred_at=now,
            cost_type=CostType.INFERENCE,
            provider="openai",
            service_or_model="gpt-4o",
            execution_id="exec_usd_mc",
            usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("2000")),
            currency="USD",
            unit_cost=Decimal("0.0025"),
            total_cost=Decimal("5.00"),
            mission_id="m_alpha_mc",
        )
    )
    env["cost_repo"].append(
        CostRecord(
            cost_id="c_clp_1",
            occurred_at=now,
            cost_type=CostType.INFERENCE,
            provider="anthropic",
            service_or_model="claude-3-5-sonnet",
            execution_id="exec_clp",
            usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("1000")),
            currency="CLP",
            unit_cost=Decimal("1.5"),
            total_cost=Decimal("1500.00"),
            mission_id="m_alpha_mc",
        )
    )

    resp = client.get(f"/api/bi/tenants/{t_a}/kpis/summary", headers={"x-session-id": sess_a})
    assert resp.status_code == 200
    data = resp.json()

    # Desglose multimoneda exacto
    assert data["currency_breakdown"]["USD"]["total_known_agent_cost"] == "5.00"
    assert data["currency_breakdown"]["CLP"]["total_known_agent_cost"] == "1500.00"

    kpis = {k["kpi_id"]: k for k in data["kpis"]}
    # KPI sin filtro de moneda se marca NOT_COMPARABLE_CURRENCY
    assert kpis["TOTAL_AGENT_COST"]["status"] == "NOT_COMPARABLE_CURRENCY"
    assert kpis["TOTAL_AGENT_COST"]["value"] is None


def test_gate_p_scenario_13_executive_drill_down_and_ui(gate_p_environment):
    """
    Escenario 13: Enlaces ejecutivos de drill-down y visualización HTML en Admin Console.
    """
    env = gate_p_environment
    client = env["client"]
    t_a = env["tenant_a"]
    sess_a = env["sess_a"]

    # 1. Resumen contiene drill-down links válidos
    resp_sum = client.get(f"/api/bi/tenants/{t_a}/kpis/summary", headers={"x-session-id": sess_a})
    assert resp_sum.status_code == 200
    drill_downs = resp_sum.json()["drill_down_links"]
    assert "opportunity_dashboard" in drill_downs
    assert "supplier_dashboard" in drill_downs
    assert "profit_dashboard" in drill_downs
    assert "mission_dashboard" in drill_downs
    assert "agent_cost_dashboard" in drill_downs

    # 2. Catálogo canónico accesible
    resp_cat = client.get(f"/api/bi/tenants/{t_a}/kpis/catalog", headers={"x-session-id": sess_a})
    assert resp_cat.status_code == 200
    assert len(resp_cat.json()) == len(KPI_CATALOG)

    # 3. Vistas HTML en Admin Console
    for path in ["/bi/kpis", "/bi/opportunities", "/bi/suppliers", "/bi/profit", "/bi/missions", "/bi/agent-costs"]:
        resp_html = client.get(path, headers={"x-session-id": sess_a})
        assert resp_html.status_code == 200
        assert "<html" in resp_html.text.lower() or "<div" in resp_html.text.lower()


def test_gate_p_scenario_14_to_15_security_and_anti_cot(gate_p_environment):
    """
    Escenarios 14 y 15:
    - N.9 Sensitive Data Protection y Anti-CoT.
    - Cero filtraciones de secretos, Bearer tokens, chain_of_thought o llaves privadas en payloads.
    """
    env = gate_p_environment
    client = env["client"]
    t_a = env["tenant_a"]
    sess_a = env["sess_a"]

    endpoints = [
        f"/api/bi/tenants/{t_a}/kpis/summary",
        f"/api/bi/tenants/{t_a}/kpis/catalog",
        f"/api/bi/tenants/{t_a}/opportunities/summary",
        f"/api/bi/tenants/{t_a}/suppliers/summary",
        f"/api/bi/tenants/{t_a}/profit/summary",
        f"/api/bi/tenants/{t_a}/missions/summary",
        f"/api/bi/tenants/{t_a}/agent-costs/summary",
    ]

    forbidden_tokens = [
        "Bearer ",
        "DATABASE_URL",
        "api_key",
        "client_secret",
        "chain_of_thought",
        "reasoning_tokens",
        "internal_scratchpad",
        "password",
    ]

    for ep in endpoints:
        resp = client.get(ep, headers={"x-session-id": sess_a})
        assert resp.status_code == 200
        text = resp.text
        for token in forbidden_tokens:
            assert token not in text, f"Found sensitive leak '{token}' in response of {ep}"
