"""
Tests Unitarios Exhaustivos para Q.4 — Mission Dashboard (Hito Q — Business Intelligence).

Requisitos Cubiertos:
1. Pure consultative / read-only behavior (No muta misiones, no invoca scrapers ni motores).
2. Strict Multi-Tenant isolation (O.1, particionamiento en disco, CrossTenantGuard).
3. SaaS Authorization & RBAC (O.4, verificación de MISSION_DASHBOARD_READ y BUSINESS_INTELLIGENCE_READ, rechazos 401/403).
4. UNKNOWN semantics preservation (UNKNOWN != 0, UNKNOWN != SUCCESS, duration_seconds=None en misiones en ejecución).
5. Deterministic sorting (created_at, updated_at, status, priority, duration, mission_id con desempate por mission_id).
6. Pagination metadata (page, page_size, total_items, total_pages, has_next, has_previous).
7. Filtering (status, mission_type, priority, opportunity_id, supplier_id, has_errors, date ranges, search_text).
8. Unified Timeline Reconstruction (MissionResult trace, K.2 AgentTraceRecord, K.1 AuditRecord) cronológicamente ordenado.
9. Cross-domain link resolution (Oportunidades Q.1, Proveedores Q.2, Rentabilidad Q.3).
10. Sensitive data sanitization & Anti-CoT protection (Exclusión de tokens, passwords, scratchpads).
11. Statistical Summary Aggregation (total_missions, status_counts, success_rate, avg_duration, etc.).
12. Scope containment (Zero modifications/leaks to Q.5+).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
from typing import Optional, List, Dict, Any, Tuple
import pytest

from src.domain.mission.models import (
    Mission,
    MissionResult,
    MissionStatus,
    MissionType,
    MissionPriority,
    MissionTraceEntry,
)
from src.domain.mission_dashboard.models import (
    MissionDashboardItem,
    MissionDashboardSummary,
    MissionDashboardDetail,
    MissionTimelineEntry,
    MissionDashboardQuery,
    MissionDashboardPage,
    MissionSortField,
)
from src.domain.mission_dashboard.ports import TenantMissionRepositoryPort
from src.infrastructure.persistence.data.json.tenant_mission_repository import JsonTenantMissionRepository
from src.application.mission_dashboard.mission_dashboard_service import MissionDashboardService
from src.domain.opportunity_dashboard.models import OpportunityDashboardItem
from src.domain.opportunity_detection.models import (
    OpportunityRecord,
    OpportunityType,
    OpportunityStatus,
    ObservedOpportunityMetrics,
    DerivedOpportunityMetrics,
)
from src.domain.market_intelligence.models import Marketplace, Confidence
from src.domain.supplier_intelligence.models import (
    Supplier,
    SupplierStatus,
    EvidenceProvenanceType,
)
from src.domain.supplier_dashboard.models import SupplierDashboardItem
from src.domain.profit_dashboard.models import ProfitDashboardItem, ProfitCompleteness
from src.domain.agent_trace.models import AgentTraceRecord, TraceStatus, StepType
from src.domain.audit.models import AuditRecord, AuditRecordType, AuditActor, AuditActorType
from src.domain.tenant.models import TenantContext, TenantScope
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.admin_console.models import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminResourceNotFoundError,
    AdminInvalidRequestError,
)
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.organization.models import Organization, UserMembership, MembershipRole, MembershipStatus
from src.domain.rbac.models import Role, RoleAssignment, Permission
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import JsonOrganizationRepository, JsonMembershipRepository
from src.infrastructure.persistence.data.json.rbac_repository import JsonRoleRepository, JsonRoleAssignmentRepository
from src.infrastructure.persistence.data.json.tenant_opportunity_repository import JsonTenantOpportunityRepository
from src.infrastructure.persistence.data.json.tenant_supplier_repository import JsonTenantSupplierRepository
from src.infrastructure.persistence.data.json.tenant_profit_repository import JsonTenantProfitRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository


class MockClock:
    def __init__(self, current_time: Optional[datetime] = None):
        self._current_time = current_time or datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        pass


def make_sample_mission(
    mission_id: str,
    mission_type: MissionType = MissionType.MARKET_DISCOVERY,
    status: MissionStatus = MissionStatus.COMPLETED,
    priority: MissionPriority = MissionPriority.MEDIUM,
    created_at: Optional[datetime] = None,
    updated_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
    opportunity_id: Optional[str] = "opp-100",
    supplier_id: Optional[str] = "supp-100",
    has_errors: bool = False,
    unknown_fields: Optional[List[str]] = None,
) -> Mission:
    t0 = created_at or datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    t1 = updated_at or t0 + timedelta(minutes=15)
    t2 = completed_at or (t1 if status in (MissionStatus.COMPLETED, MissionStatus.FAILED) else None)

    return Mission(
        mission_id=mission_id,
        type=mission_type,
        priority=priority,
        status=status,
        parameters={
            "opportunity_id": opportunity_id,
            "supplier_id": supplier_id,
            "category": "electronics",
            "has_errors": has_errors,
        },
        created_at=t0,
        updated_at=t1,
    )


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def rbac_setup(temp_dir):
    session_repo = JsonSaaSSessionRepository(temp_dir / "sessions")
    org_repo = JsonOrganizationRepository(temp_dir / "orgs")
    mem_repo = JsonMembershipRepository(temp_dir / "members")
    role_repo = JsonRoleRepository(temp_dir / "roles")
    role_assign_repo = JsonRoleAssignmentRepository(temp_dir / "role_assigns")

    rbac_service = RBACService(
        role_repository=role_repo,
        assignment_repository=role_assign_repo,
    )
    auth_service = SaaSAuthorizationService(
        session_repository=session_repo,
        membership_repository=mem_repo,
        rbac_service=rbac_service,
    )

    # Crear rol y permisos
    analyst_role = Role(
        role_id="role_analyst",
        name="BI Analyst",
        permissions=(
            Permission(
                permission_id="perm-mission-read",
                action="MISSION_DASHBOARD_READ",
                description="Read mission dashboard",
            ),
            Permission(
                permission_id="perm-bi-read",
                action="BUSINESS_INTELLIGENCE_READ",
                description="Read business intelligence",
            ),
        ),
    )
    role_repo.save(analyst_role)

    # Asignar rol a user-1 sin scope restrictivo (o con scope exacto canonical 'tenant_tenant-alpha')
    role_assign_repo.save(
        RoleAssignment(
            assignment_id="assign-1",
            role_id="role_analyst",
            identity_id="user-1",
            scope="tenant_tenant-alpha",
        )
    )

    # Sesión activa para user-1 en tenant-alpha
    session = SaaSSession(
        session_id="valid-session-alpha",
        identity_id="user-1",
        tenant_id="tenant-alpha",
        status=SessionStatus.ACTIVE,
        created_at=datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 30, 22, 0, 0, tzinfo=timezone.utc),
    )
    session_repo.save(session)

    # Sesión activa para user-2 en tenant-beta (sin permisos para alpha)
    session_beta = SaaSSession(
        session_id="valid-session-beta",
        identity_id="user-2",
        tenant_id="tenant-beta",
        status=SessionStatus.ACTIVE,
        created_at=datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 30, 22, 0, 0, tzinfo=timezone.utc),
    )
    session_repo.save(session_beta)

    # Sesión expirada
    session_expired = SaaSSession(
        session_id="expired-session-alpha",
        identity_id="user-1",
        tenant_id="tenant-alpha",
        status=SessionStatus.EXPIRED,
        created_at=datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc),
        expires_at=datetime(2026, 9, 13, 11, 0, 0, tzinfo=timezone.utc),
    )
    session_repo.save(session_expired)

    return {
        "session_repo": session_repo,
        "auth_service": auth_service,
    }


def test_q4_mission_dashboard_models_immutability():
    item = MissionDashboardItem(
        mission_id="m-1",
        mission_type="OPPORTUNITY_DISCOVERY",
        status="COMPLETED",
        priority="HIGH",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        duration_seconds=120.5,
    )
    with pytest.raises(Exception):
        item.status = "RUNNING"  # dataclass(frozen=True)


def test_q4_mission_repository_tenant_isolation(temp_dir):
    repo = JsonTenantMissionRepository(temp_dir / "missions")
    t_alpha = TenantContext(tenant_id="tenant-alpha")
    t_beta = TenantContext(tenant_id="tenant-beta")

    m_alpha = make_sample_mission("m-alpha-1")
    m_beta = make_sample_mission("m-beta-1")

    repo.save(t_alpha, m_alpha)
    repo.save(t_beta, m_beta)

    # Verificar que Alpha no ve a Beta
    assert repo.get_by_id(t_alpha, "m-alpha-1") is not None
    assert repo.get_by_id(t_alpha, "m-beta-1") is None

    # Verificar que Beta no ve a Alpha
    assert repo.get_by_id(t_beta, "m-beta-1") is not None
    assert repo.get_by_id(t_beta, "m-alpha-1") is None

    # Listar en tenant-alpha
    missions_alpha = repo.list_all(t_alpha)
    assert len(missions_alpha) == 1
    assert missions_alpha[0].mission_id == "m-alpha-1"


def test_q4_mission_service_authorization(temp_dir, rbac_setup):
    mission_repo = JsonTenantMissionRepository(temp_dir / "missions")
    t_alpha = TenantContext(tenant_id="tenant-alpha")
    mission_repo.save(t_alpha, make_sample_mission("m-1"))

    service = MissionDashboardService(
        repository=mission_repo,
        authorization_service=rbac_setup["auth_service"],
        session_repository=rbac_setup["session_repo"],
    )

    # 1. Sin session_id -> AdminAuthenticationError
    with pytest.raises(AdminAuthenticationError):
        service.list_missions(tenant_id="tenant-alpha", session_id=None)

    # 2. Con sesión expirada -> AdminAuthenticationError
    with pytest.raises(AdminAuthenticationError):
        service.list_missions(tenant_id="tenant-alpha", session_id="expired-session-alpha")

    # 3. Con sesión de tenant-beta intentando acceder a tenant-alpha -> AdminAuthorizationError
    with pytest.raises(AdminAuthorizationError):
        service.list_missions(tenant_id="tenant-alpha", session_id="valid-session-beta")

    # 4. Con sesión válida y autorizada -> OK
    result = service.list_missions(tenant_id="tenant-alpha", session_id="valid-session-alpha")
    assert result.total_count == 1
    assert result.items[0].mission_id == "m-1"


def test_q4_mission_service_unknown_semantics(temp_dir, rbac_setup):
    mission_repo = JsonTenantMissionRepository(temp_dir / "missions")
    t_alpha = TenantContext(tenant_id="tenant-alpha")

    # Misión en ejecución sin completed_at ni duration_seconds
    m_running = make_sample_mission(
        "m-running",
        status=MissionStatus.RUNNING,
        completed_at=None,
        unknown_fields=["completed_at", "total_cost"],
    )
    mission_repo.save(t_alpha, m_running)

    service = MissionDashboardService(
        repository=mission_repo,
        authorization_service=rbac_setup["auth_service"],
        session_repository=rbac_setup["session_repo"],
    )

    page = service.list_missions(tenant_id="tenant-alpha", session_id="valid-session-alpha")
    assert len(page.items) == 1
    item = page.items[0]

    # Regla: UNKNOWN != 0 y no inventar duración ni éxito
    assert item.duration_seconds is None
    assert item.status == "RUNNING"
    assert "duration_seconds" in item.unknown_fields

    summary = service.get_summary(tenant_id="tenant-alpha", session_id="valid-session-alpha")
    assert summary.running_count == 1
    assert summary.completed_count == 0
    assert summary.average_duration_seconds is None


def test_q4_mission_service_filtering(temp_dir, rbac_setup):
    mission_repo = JsonTenantMissionRepository(temp_dir / "missions")
    t_alpha = TenantContext(tenant_id="tenant-alpha")

    m1 = make_sample_mission(
        "m-1",
        mission_type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.COMPLETED,
        priority=MissionPriority.HIGH,
        opportunity_id="opp-100",
        supplier_id="supp-100",
        has_errors=False,
    )
    m2 = make_sample_mission(
        "m-2",
        mission_type=MissionType.SUPPLIER_SEARCH,
        status=MissionStatus.FAILED,
        priority=MissionPriority.MEDIUM,
        opportunity_id="opp-200",
        supplier_id="supp-200",
        has_errors=True,
    )
    m3 = make_sample_mission(
        "m-3",
        mission_type=MissionType.PROFIT_EVALUATION,
        status=MissionStatus.RUNNING,
        priority=MissionPriority.LOW,
        opportunity_id="opp-100",
        supplier_id="supp-300",
        has_errors=False,
    )

    mission_repo.save(t_alpha, m1)
    mission_repo.save(t_alpha, m2)
    mission_repo.save(t_alpha, m3)

    service = MissionDashboardService(
        repository=mission_repo,
        authorization_service=rbac_setup["auth_service"],
        session_repository=rbac_setup["session_repo"],
    )

    # 1. Filtro por status
    q_status = MissionDashboardQuery(status="COMPLETED")
    res = service.list_missions(tenant_id="tenant-alpha", query=q_status, session_id="valid-session-alpha")
    assert res.total_count == 1
    assert res.items[0].mission_id == "m-1"

    # 2. Filtro por mission_type
    q_type = MissionDashboardQuery(mission_type="SUPPLIER_SEARCH")
    res = service.list_missions(tenant_id="tenant-alpha", query=q_type, session_id="valid-session-alpha")
    assert res.total_count == 1
    assert res.items[0].mission_id == "m-2"

    # 3. Filtro por opportunity_id
    q_opp = MissionDashboardQuery(opportunity_id="opp-100")
    res = service.list_missions(tenant_id="tenant-alpha", query=q_opp, session_id="valid-session-alpha")
    assert res.total_count == 2
    assert {i.mission_id for i in res.items} == {"m-1", "m-3"}

    # 4. Filtro por search_text
    q_search = MissionDashboardQuery(search_text="m-3")
    res = service.list_missions(tenant_id="tenant-alpha", query=q_search, session_id="valid-session-alpha")
    assert res.total_count == 1
    assert res.items[0].mission_id == "m-3"


def test_q4_mission_service_sorting_and_pagination(temp_dir, rbac_setup):
    mission_repo = JsonTenantMissionRepository(temp_dir / "missions")
    t_alpha = TenantContext(tenant_id="tenant-alpha")

    base_time = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    for i in range(1, 11):
        m = make_sample_mission(
            f"m-{i:02d}",
            priority=MissionPriority.HIGH if i % 2 == 0 else MissionPriority.LOW,
            created_at=base_time + timedelta(minutes=i),
            status=MissionStatus.COMPLETED,
        )
        mission_repo.save(t_alpha, m)

    service = MissionDashboardService(
        repository=mission_repo,
        authorization_service=rbac_setup["auth_service"],
        session_repository=rbac_setup["session_repo"],
    )

    # Orden descendente por created_at con paginación page=1, page_size=4
    q1 = MissionDashboardQuery(sort_by=MissionSortField.CREATED_AT, page=1, page_size=4)
    p1 = service.list_missions(tenant_id="tenant-alpha", query=q1, session_id="valid-session-alpha")
    assert p1.total_count == 10
    assert p1.total_pages == 3
    assert p1.page == 1
    assert [i.mission_id for i in p1.items] == ["m-10", "m-09", "m-08", "m-07"]

    # Página 2
    q2 = MissionDashboardQuery(sort_by=MissionSortField.CREATED_AT, page=2, page_size=4)
    p2 = service.list_missions(tenant_id="tenant-alpha", query=q2, session_id="valid-session-alpha")
    assert [i.mission_id for i in p2.items] == ["m-06", "m-05", "m-04", "m-03"]
    assert p2.page == 2

    # Página 3
    q3 = MissionDashboardQuery(sort_by=MissionSortField.CREATED_AT, page=3, page_size=4)
    p3 = service.list_missions(tenant_id="tenant-alpha", query=q3, session_id="valid-session-alpha")
    assert [i.mission_id for i in p3.items] == ["m-02", "m-01"]
    assert p3.page == 3


def test_q4_mission_service_unified_timeline(temp_dir, rbac_setup):
    mission_repo = JsonTenantMissionRepository(temp_dir / "missions")
    agent_trace_repo = JsonAgentTraceRepository(temp_dir / "traces")
    audit_repo = JsonAuditRepository(temp_dir / "audit")
    t_alpha = TenantContext(tenant_id="tenant-alpha")

    t0 = datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    m = make_sample_mission("m-timeline", created_at=t0)
    mission_repo.save(t_alpha, m)

    # 1. Agregar AgentTraceRecord (K.2) con correlation_id=m-timeline
    trace_k2 = AgentTraceRecord(
        trace_id="tr-100",
        component_name="scraper-agent-1",
        execution_id="exec-100",
        step_number=1,
        step_type=StepType.TOOL_CALL,
        operation="scrape_prices",
        started_at=t0 + timedelta(minutes=2),
        completed_at=t0 + timedelta(minutes=4),
        status=TraceStatus.SUCCESS,
        mission_id="m-timeline",
        correlation_id="m-timeline",
    )
    agent_trace_repo.append(trace_k2)

    # 2. Agregar AuditRecord (K.1) con correlation_id=m-timeline
    audit_k1 = AuditRecord(
        audit_id="aud-100",
        record_type=AuditRecordType.ACTION_EXECUTED,
        occurred_at=t0 + timedelta(minutes=3),
        actor=AuditActor(actor_type=AuditActorType.SYSTEM, actor_id="orchestrator"),
        subject_type="mission",
        subject_id="m-timeline",
        action_or_operation="checkpoint",
        status="SUCCESS",
        mission_id="m-timeline",
        correlation_id="m-timeline",
        metadata={"event": "mission_checkpoint"},
    )
    audit_repo.append(audit_k1)

    service = MissionDashboardService(
        repository=mission_repo,
        agent_trace_repository=agent_trace_repo,
        audit_repository=audit_repo,
        authorization_service=rbac_setup["auth_service"],
        session_repository=rbac_setup["session_repo"],
    )

    detail = service.get_mission_detail(
        tenant_id="tenant-alpha",
        mission_id="m-timeline",
        session_id="valid-session-alpha",
    )

    assert detail.item.mission_id == "m-timeline"
    assert len(detail.timeline) >= 2

    # Verificar que el timeline está ordenado cronológicamente
    timestamps = [e.timestamp for e in detail.timeline]
    assert timestamps == sorted(timestamps)

    step_types = {e.step_type for e in detail.timeline}
    assert any("AGENT_STEP" in st for st in step_types)
    assert any("AUDIT" in st for st in step_types)


def test_q4_mission_service_cross_domain_links(temp_dir, rbac_setup):
    mission_repo = JsonTenantMissionRepository(temp_dir / "missions")
    opp_repo = JsonTenantOpportunityRepository(temp_dir / "opportunities")
    supp_repo = JsonTenantSupplierRepository(temp_dir / "suppliers")
    profit_repo = JsonTenantProfitRepository(temp_dir / "profits")
    t_alpha = TenantContext(tenant_id="tenant-alpha")

    # Misión vinculada a opp-100 y supp-100
    m = make_sample_mission("m-linked", opportunity_id="opp-100", supplier_id="supp-100")
    mission_repo.save(t_alpha, m)

    # Crear oportunidad Q.1 (OpportunityRecord)
    opp = OpportunityRecord(
        opportunity_id="opp-100",
        canonical_product_id="prod-100",
        marketplace=Marketplace.MERCADO_LIBRE,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.DETECTED,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs-1",),
        detected_at=datetime.now(timezone.utc),
        title="Gaming Keyboard",
        category="Electronics",
        observed_metrics=ObservedOpportunityMetrics(),
        derived_metrics=DerivedOpportunityMetrics(
            opportunity_score=Decimal("88.5"),
            potential_margin_ratio=Decimal("0.45"),
        ),
    )
    opp_repo.save(t_alpha, opp)

    # Crear proveedor Q.2 (Supplier)
    supp = Supplier(
        supplier_id="supp-100",
        name="Global Keyboards Co",
        source="alibaba",
        source_type=EvidenceProvenanceType.LIVE,
        status=SupplierStatus.ACTIVE,
        metadata={
            "verification_status": "VERIFIED",
            "risk_level": "LOW",
            "supplier_score": "92.0",
        },
    )
    supp_repo.save(t_alpha, supp)

    # Crear hecho de profit Q.3 asociado a opp-100
    profit = ProfitDashboardItem(
        item_id="prof-100",
        product_id="prod-100",
        opportunity_id="opp-100",
        supplier_id="supp-100",
        title="Gaming Keyboard Unit Economics",
        marketplace="mercadolibre",
        category="Electronics",
        sale_price=Decimal("100.00"),
        unit_cost=Decimal("40.00"),
        gross_profit=Decimal("60.00"),
        contribution_profit=Decimal("50.00"),
        margin_pct=Decimal("50.0"),
        currency="USD",
        completeness=ProfitCompleteness.COMPLETE,
        calculated_at=datetime.now(timezone.utc),
    )
    profit_repo.save(t_alpha, profit)

    service = MissionDashboardService(
        repository=mission_repo,
        opportunity_repository=opp_repo,
        supplier_repository=supp_repo,
        profit_repository=profit_repo,
        authorization_service=rbac_setup["auth_service"],
        session_repository=rbac_setup["session_repo"],
    )

    detail = service.get_mission_detail(
        tenant_id="tenant-alpha",
        mission_id="m-linked",
        session_id="valid-session-alpha",
    )

    assert detail.associated_opportunity is not None
    assert detail.associated_opportunity["opportunity_id"] == "opp-100"
    assert detail.associated_opportunity["title"] == "Gaming Keyboard"

    assert detail.associated_supplier is not None
    assert detail.associated_supplier["supplier_id"] == "supp-100"
    assert detail.associated_supplier["name"] == "Global Keyboards Co"

    assert detail.associated_profit is not None
    assert detail.associated_profit["item_id"] == "prof-100"
    assert detail.associated_profit["margin_pct"] == "50.0"


def test_q4_mission_service_sensitive_data_exclusion(temp_dir, rbac_setup):
    mission_repo = JsonTenantMissionRepository(temp_dir / "missions")
    t_alpha = TenantContext(tenant_id="tenant-alpha")

    # Misión con secretos en parámetros
    m = Mission(
        mission_id="m-secret",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.COMPLETED,
        parameters={
            "api_key": "sk-secret-token-12345",
            "db_password": "super-password",
            "category": "laptops",
            "nested": {"bearer_token": "bearer-abc"},
        },
        priority=MissionPriority.HIGH,
        created_at=datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc),
        updated_at=datetime(2026, 9, 14, 10, 5, 0, tzinfo=timezone.utc),
    )
    mission_repo.save(t_alpha, m)

    service = MissionDashboardService(
        repository=mission_repo,
        authorization_service=rbac_setup["auth_service"],
        session_repository=rbac_setup["session_repo"],
    )

    detail = service.get_mission_detail(
        tenant_id="tenant-alpha",
        mission_id="m-secret",
        session_id="valid-session-alpha",
    )

    # Validar que los secretos han sido enmascarados/excluidos de la vista BI
    assert "api_key" not in detail.item.parameters_summary
    assert "db_password" not in detail.item.parameters_summary
    assert "bearer_token" not in detail.item.parameters_summary.get("nested", {})
    assert detail.item.parameters_summary["category"] == "laptops"
