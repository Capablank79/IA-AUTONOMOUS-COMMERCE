"""
Tests de Integración y End-to-End para Q.4 — Mission Dashboard (Hito Q — Business Intelligence).

Cubre:
A. Complete mission execution records -> correct status, steps, duration, and metrics.
B. UNKNOWN semantics preserved -> duration=None, progress=None for active/uncertain missions (UNKNOWN != 0).
C. Tenant A vs Tenant B strict isolation (cero data leakage, 403 on cross-tenant).
D. Multi-criteria filtering (status, type, priority, opportunity_id, supplier_id, has_errors, date range, search).
E. Stable sorting and pagination metadata.
F. Statistical summary endpoint (/summary) with aggregated execution stats.
G. Detailed mission view (/{mission_id}) with unified timeline (K.1 Audit, K.2 Agent Traces) and cross-domain links.
H. Sensitive data sanitization and anti-CoT protection.
I. Unauthorized / unauthenticated requests rejected with 401/403.
J. Minimal HTML dashboard surface (/bi/missions).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, Dict, Any, List
import pytest
from starlette.testclient import TestClient

from src.domain.mission.models import (
    Mission,
    MissionStatus,
    MissionPriority,
    MissionType,
    MissionResult,
    MissionTraceEntry,
)
from src.domain.mission_dashboard.models import (
    MissionDashboardItem,
    MissionDashboardSummary,
    MissionDashboardDetail,
    MissionDashboardQuery,
    MissionDashboardPage,
    MissionSortField,
    SortOrder,
)
from src.domain.tenant.models import TenantContext
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.rbac.models import Permission, Role, RoleAssignment
from src.domain.reliability.ports import ClockPort
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.agent_trace.models import AgentTraceRecord, TraceStatus, StepType
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
)
from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.supplier_intelligence.models import Supplier, SupplierStatus, EvidenceProvenanceType
from src.domain.profit_dashboard.models import ProfitDashboardItem, ProfitCompleteness
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.application.mission_dashboard.mission_dashboard_service import MissionDashboardService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import JsonOrganizationRepository, JsonMembershipRepository
from src.infrastructure.persistence.data.json.rbac_repository import JsonRoleRepository, JsonRoleAssignmentRepository
from src.infrastructure.persistence.data.json.tenant_mission_repository import JsonTenantMissionRepository
from src.infrastructure.persistence.data.json.tenant_profit_repository import JsonTenantProfitRepository
from src.infrastructure.persistence.data.json.tenant_opportunity_repository import JsonTenantOpportunityRepository
from src.infrastructure.persistence.data.json.tenant_supplier_repository import JsonTenantSupplierRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
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
    assignment_repo = JsonRoleAssignmentRepository(data_dir / "role_assignments")
    audit_repo = JsonAuditRepository(data_dir / "audit")
    agent_trace_repo = JsonAgentTraceRepository(data_dir / "agent_traces")

    mission_repo = JsonTenantMissionRepository(data_dir)
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

    mission_dashboard_service = MissionDashboardService(
        repository=mission_repo,
        authorization_service=auth_service,
        session_repository=session_repo,
        opportunity_repository=opp_repo,
        supplier_repository=supp_repo,
        profit_repository=profit_repo,
        audit_repository=audit_repo,
        agent_trace_repository=agent_trace_repo,
        clock=clock,
    )

    app = create_admin_app(
        service=admin_service,
        mission_dashboard_service=mission_dashboard_service,
    )

    # -------------------------------------------------------------
    # Población de Datos Tenant Alpha
    # -------------------------------------------------------------
    ctx_a = TenantContext(tenant_id="tenant_alpha")

    # Misión A1: COMPLETED
    t0 = datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc)
    t1 = datetime(2026, 9, 14, 9, 10, tzinfo=timezone.utc)
    m_a1 = Mission(
        mission_id="m-a1",
        type=MissionType.MARKET_DISCOVERY,
        priority=MissionPriority.HIGH,
        status=MissionStatus.COMPLETED,
        parameters={
            "opportunity_id": "opp-100",
            "api_key": "SECRET_KEY_12345",
            "target_niche": "Electronics",
            "has_errors": False,
        },
        created_at=t0,
        updated_at=t1,
    )

    # Misión A2: RUNNING (Incertidumbre - UNKNOWN semantics)
    t2 = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)
    m_a2 = Mission(
        mission_id="m-a2",
        type=MissionType.SUPPLIER_SEARCH,
        priority=MissionPriority.MEDIUM,
        status=MissionStatus.RUNNING,
        parameters={
            "supplier_id": "supp-200",
            "supplier_name": "Acme Global",
            "has_errors": False,
        },
        created_at=t2,
        updated_at=t2,
    )

    # Misión A3: FAILED con errores
    t3 = datetime(2026, 9, 14, 11, 0, tzinfo=timezone.utc)
    t4 = datetime(2026, 9, 14, 11, 5, tzinfo=timezone.utc)
    m_a3 = Mission(
        mission_id="m-a3",
        type=MissionType.PROFIT_EVALUATION,
        priority=MissionPriority.CRITICAL,
        status=MissionStatus.FAILED,
        parameters={
            "profit_item_id": "item-300",
            "sku": "SKU-99",
            "has_errors": True,
        },
        created_at=t3,
        updated_at=t4,
    )

    # Misión A4: PENDING
    t5 = datetime(2026, 9, 14, 11, 30, tzinfo=timezone.utc)
    m_a4 = Mission(
        mission_id="m-a4",
        type=MissionType.FULL_OPPORTUNITY_ANALYSIS,
        priority=MissionPriority.LOW,
        status=MissionStatus.PENDING,
        parameters={
            "has_errors": False,
        },
        created_at=t5,
        updated_at=t5,
    )

    mission_repo.save_all(ctx_a, [m_a1, m_a2, m_a3, m_a4])

    # Result y traza interna para m-a1
    res_a1 = MissionResult(
        mission_id="m-a1",
        status=MissionStatus.COMPLETED,
        output={"discovered_count": 42, "private_thought": "cot reasoning to hide"},
        trace=[
            MissionTraceEntry(
                step="DISCOVERY_INIT",
                status=MissionStatus.RUNNING,
                timestamp=t0 + timedelta(minutes=1),
                metadata={"info": "Initializing query"},
            )
        ],
        finished_at=t1,
    )
    mission_repo.save_result(ctx_a, res_a1)

    # Entidades cross-domain para enriquecimiento
    opp = OpportunityRecord(
        opportunity_id="opp-100",
        canonical_product_id="prod-100",
        title="Wireless Earbuds ANC",
        marketplace=Marketplace.MERCADO_LIBRE,
        category="Electronics",
        detected_at=t0,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.VALID,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-1",),
        observed_metrics=ObservedOpportunityMetrics(observed_sold_quantity=500),
        derived_metrics=DerivedOpportunityMetrics(potential_margin_ratio=Decimal("0.35")),
    )
    opp_repo.save(ctx_a, opp)

    supp = Supplier(
        supplier_id="supp-200",
        name="Acme Global ShenZhen",
        source="supplier_catalog",
        source_type=EvidenceProvenanceType.LIVE,
        status=SupplierStatus.ACTIVE,
        metadata={"verification_status": "VERIFIED", "risk_level": "LOW"},
    )
    supp_repo.save(ctx_a, supp)

    profit_item = ProfitDashboardItem(
        item_id="item-300",
        opportunity_id="opp-100",
        product_id="prod-300",
        currency="USD",
        title="Bluetooth Speaker",
        marketplace="amazon",
        calculated_at=t3,
        completeness=ProfitCompleteness.COMPLETE,
    )
    profit_repo.save(ctx_a, profit_item)

    # Auditoría K.1 y Trazas K.2 asociadas a m-a1
    audit_actor = AuditActor(actor_type=AuditActorType.AGENT, actor_id="agent-worker-1")
    audit_rec = AuditRecord(
        audit_id="aud-100",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=t0 + timedelta(minutes=2),
        actor=audit_actor,
        subject_type="MISSION",
        subject_id="m-a1",
        action_or_operation="MISSION_STEP_EXECUTED",
        status="SUCCESS",
        mission_id="m-a1",
        correlation_id="corr-m-a1",
    )
    audit_repo.append(audit_rec)

    trace_rec = AgentTraceRecord(
        trace_id="tr-100",
        component_name="DiscoveryAgent",
        execution_id="exec-100",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="QUERY_MARKETPLACE",
        started_at=t0 + timedelta(minutes=5),
        completed_at=t0 + timedelta(minutes=6),
        status=TraceStatus.SUCCESS,
        mission_id="m-a1",
        correlation_id="corr-m-a1",
    )
    agent_trace_repo.append(trace_rec)

    # -------------------------------------------------------------
    # Población de Datos Tenant Beta
    # -------------------------------------------------------------
    ctx_b = TenantContext(tenant_id="tenant_beta")
    m_b1 = Mission(
        mission_id="m-b1",
        type=MissionType.MARKET_DISCOVERY,
        priority=MissionPriority.HIGH,
        status=MissionStatus.RUNNING,
        created_at=t0,
        updated_at=t0,
    )
    mission_repo.save(ctx_b, m_b1)

    # -------------------------------------------------------------
    # Sesiones y Roles RBAC
    # -------------------------------------------------------------
    sess_a = SaaSSession(
        session_id="sess-alpha-mission",
        identity_id="user-alpha-mission-ops",
        tenant_id="tenant_alpha",
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
    )
    session_repo.save(sess_a)

    role_a = Role(
        role_id="role-alpha-mission",
        name="Alpha Mission Analyst",
        permissions=(
            Permission("p1", "MISSION_DASHBOARD_READ", description="Read Mission Dashboard"),
            Permission("p2", "BUSINESS_INTELLIGENCE_READ", description="Read BI"),
        ),
    )
    role_repo.save_role(role_a)
    assignment_repo.save_assignment(
        RoleAssignment(
            assignment_id="asgn-alpha-mission",
            identity_id="user-alpha-mission-ops",
            role_id=role_a.role_id,
            scope="tenant_tenant_alpha",
            assigned_at=clock.now(),
        )
    )

    # Sesión Tenant Alpha sin permisos
    sess_no_perm = SaaSSession(
        session_id="sess-alpha-no-perm",
        identity_id="user-alpha-no-perm",
        tenant_id="tenant_alpha",
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
    )
    session_repo.save(sess_no_perm)

    role_no_perm = Role(
        role_id="role-no-perm",
        name="No Permissions",
        permissions=(),
    )
    role_repo.save_role(role_no_perm)
    assignment_repo.save_assignment(
        RoleAssignment(
            assignment_id="asgn-no-perm",
            identity_id="user-alpha-no-perm",
            role_id=role_no_perm.role_id,
            scope="tenant_tenant_alpha",
            assigned_at=clock.now(),
        )
    )

    # Sesión Válida Tenant Beta
    sess_b = SaaSSession(
        session_id="sess-beta-mission",
        identity_id="user-beta-mission-ops",
        tenant_id="tenant_beta",
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=4),
    )
    session_repo.save(sess_b)

    role_b = Role(
        role_id="role-beta-mission",
        name="Beta Mission Analyst",
        permissions=(
            Permission("pb1", "MISSION_DASHBOARD_READ", description="Read Mission Dashboard"),
        ),
    )
    role_repo.save_role(role_b)
    assignment_repo.save_assignment(
        RoleAssignment(
            assignment_id="asgn-beta-mission",
            identity_id="user-beta-mission-ops",
            role_id=role_b.role_id,
            scope="tenant_tenant_beta",
            assigned_at=clock.now(),
        )
    )

    return {
        "client": TestClient(app),
        "session_a": "sess-alpha-mission",
        "session_b": "sess-beta-mission",
        "session_no_perm": "sess-alpha-no-perm",
    }


def test_scenario_a_complete_mission_record_and_metrics(integration_app):
    """Escenario A: Registro de misión finalizada retorna métricas y duración exactas."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/missions/m-a1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    item = data["item"]
    assert item["mission_id"] == "m-a1"
    assert item["status"] == "COMPLETED"
    assert item["mission_type"] == "MARKET_DISCOVERY"
    assert item["priority"] == "HIGH"
    assert item["duration_seconds"] == 600.0  # 10 min
    assert item["has_errors"] is False
    assert "duration_seconds" not in item["unknown_fields"]


def test_scenario_b_unknown_semantics_preserved(integration_app):
    """Escenario B: Misión en ejecución (RUNNING) no inventa duración ni progreso arbitrario (UNKNOWN != 0)."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/missions/m-a2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    item = data["item"]
    assert item["mission_id"] == "m-a2"
    assert item["status"] == "RUNNING"
    assert item["duration_seconds"] is None
    assert "duration_seconds" in item["unknown_fields"]


def test_scenario_c_tenant_isolation_and_cross_tenant_denial(integration_app):
    """Escenario C: Aislamiento multi-tenant estricto y denegación 403 en acceso cruzado."""
    client = integration_app["client"]
    token_a = integration_app["session_a"]

    # Tenant Alpha consulta misiones
    res_a = client.get(
        "/api/bi/tenants/tenant_alpha/missions",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_a.status_code == 200
    data_a = res_a.json()
    assert data_a["total_count"] == 4
    mission_ids = {m["mission_id"] for m in data_a["items"]}
    assert "m-b1" not in mission_ids
    assert mission_ids == {"m-a1", "m-a2", "m-a3", "m-a4"}

    # Intento de acceso cruzado por Token A hacia Tenant B -> 403 Forbidden
    res_cross = client.get(
        "/api/bi/tenants/tenant_beta/missions",
        headers={"Authorization": f"Bearer {token_a}"},
    )
    assert res_cross.status_code == 403


def test_scenario_d_multi_criteria_filtering(integration_app):
    """Escenario D: Filtros multicriterio por status, mission_type, priority, has_errors y búsqueda."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    # Filtrar por status=RUNNING
    res_status = client.get(
        "/api/bi/tenants/tenant_alpha/missions?status=RUNNING",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_status.status_code == 200
    data_status = res_status.json()
    assert data_status["total_count"] == 1
    assert data_status["items"][0]["mission_id"] == "m-a2"

    # Filtrar por has_errors=true
    res_err = client.get(
        "/api/bi/tenants/tenant_alpha/missions?has_errors=true",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_err.status_code == 200
    data_err = res_err.json()
    assert data_err["total_count"] == 1
    assert data_err["items"][0]["mission_id"] == "m-a3"

    # Filtrar por mission_type=MARKET_DISCOVERY
    res_type = client.get(
        "/api/bi/tenants/tenant_alpha/missions?type=MARKET_DISCOVERY",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_type.status_code == 200
    data_type = res_type.json()
    assert data_type["total_count"] == 1
    assert data_type["items"][0]["mission_id"] == "m-a1"

    # Filtrar por search_text="Acme"
    res_search = client.get(
        "/api/bi/tenants/tenant_alpha/missions?search=Acme",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_search.status_code == 200
    data_search = res_search.json()
    assert data_search["total_count"] == 1
    assert data_search["items"][0]["mission_id"] == "m-a2"


def test_scenario_e_sorting_and_pagination_metadata(integration_app):
    """Escenario E: Ordenación determinista y paginación con metadatos completos."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/missions?sort_by=created_at&sort_order=asc&page=1&page_size=2",
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
    assert data["items"][0]["mission_id"] == "m-a1"
    assert data["items"][1]["mission_id"] == "m-a2"


def test_scenario_f_summary_statistics_endpoint(integration_app):
    """Escenario F: Resumen estadístico agregado y seguro en /summary."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/missions/summary",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total_missions"] == 4
    assert data["completed_count"] == 1
    assert data["failed_count"] == 1
    assert data["running_count"] == 1
    assert data["pending_count"] == 1
    assert data["success_rate_pct"] == 50.0  # 1 completed / 2 finished (1 completed, 1 fail)
    assert "MARKET_DISCOVERY" in data["missions_by_type"]
    assert data["missions_with_errors"] == 1


def test_scenario_g_unified_timeline_and_cross_domain_links(integration_app):
    """Escenario G: Detalle con timeline unificado (K.1 Audit, K.2 Trace) y entidades cross-domain vinculadas."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    # Detalle de m-a1 vinculado a oportunidad opp-100 y con timeline de auditoría/trazas
    res = client.get(
        "/api/bi/tenants/tenant_alpha/missions/m-a1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["item"]["mission_id"] == "m-a1"
    assert data["associated_opportunity"] is not None
    assert data["associated_opportunity"]["opportunity_id"] == "opp-100"
    assert data["associated_opportunity"]["title"] == "Wireless Earbuds ANC"

    # Timeline unificado
    timeline = data["timeline"]
    assert len(timeline) >= 2
    step_types = {e["step_type"] for e in timeline}
    assert any("AUDIT" in st for st in step_types)
    assert any("AGENT_STEP" in st for st in step_types)

    # Detalle de m-a2 vinculado a proveedor supp-200
    res_a2 = client.get(
        "/api/bi/tenants/tenant_alpha/missions/m-a2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res_a2.status_code == 200
    data_a2 = res_a2.json()
    assert data_a2["associated_supplier"] is not None
    assert data_a2["associated_supplier"]["supplier_id"] == "supp-200"
    assert data_a2["associated_supplier"]["name"] == "Acme Global ShenZhen"


def test_scenario_h_sensitive_data_sanitization_and_anti_cot(integration_app):
    """Escenario H: Sanitización recursiva de secretos y protección anti-CoT en DTOs."""
    client = integration_app["client"]
    token = integration_app["session_a"]

    res = client.get(
        "/api/bi/tenants/tenant_alpha/missions/m-a1",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200
    data = res.json()
    item = data["item"]

    # Verificar que api_key no esté presente en parameters_summary
    assert "api_key" not in item["parameters_summary"]
    assert "SECRET_KEY_12345" not in str(item["parameters_summary"])

    # Verificar que CoT no se filtre en result_summary
    if item["result_summary"]:
        assert "private_thought" not in item["result_summary"]
        assert "cot reasoning" not in str(item["result_summary"])


def test_scenario_i_unauthorized_and_unauthenticated_rejected(integration_app):
    """Escenario I: Rechazo estricto para usuarios no autenticados (401) o no autorizados (403)."""
    client = integration_app["client"]
    token_no_perm = integration_app["session_no_perm"]

    # 401 Sin autenticación
    res_no_auth = client.get("/api/bi/tenants/tenant_alpha/missions")
    assert res_no_auth.status_code == 401
    assert res_no_auth.json()["error"] == "unauthenticated"

    # 403 Sin permisos RBAC necesarios (Default Deny)
    res_forbidden = client.get(
        "/api/bi/tenants/tenant_alpha/missions",
        headers={"Authorization": f"Bearer {token_no_perm}"},
    )
    assert res_forbidden.status_code == 403
    assert res_forbidden.json()["error"] in ("forbidden", "unauthorized")


def test_scenario_j_html_view_surface(integration_app):
    """Escenario J: Vista HTML mínima en /bi/missions responde 200 OK con estructura informativa."""
    client = integration_app["client"]
    res = client.get("/bi/missions")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Mission Dashboard" in res.text
    assert "Q.4 Validated" in res.text
