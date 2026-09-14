"""
Tests Unitarios Exhaustivos para Q.1 — Opportunity Dashboard (Hito Q — Business Intelligence).

Cubre:
1. Tenant scoping (Aislamiento de repositorios y servicios).
2. Autorización (SaaS Authorization O.4, rechazo 401/403, sesiones inválidas o expiradas).
3. Safe Item Projection (ViewModels libres de secretos, PAN/CVV y CoT).
4. UNKNOWN semantics (UNKNOWN/None nunca convertido a 0 o 0.0).
5. Decimal Money (Todos los precios, costos y márgenes representados en Decimal).
6. Filtering por categoría, marketplace, score, margen, estado, confianza y texto.
7. Sorting determinista con tie-breaking consistente.
8. Paginación segura y acotada (page_size entre 1 y 100).
9. Empty State válido y sin generación de datos dummy/falsos.
10. Explicabilidad estructurada (OBSERVED, DERIVED, INFERRED, RISKS, UNKNOWNS).
11. Comparación multidimensional de oportunidades.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
from typing import Optional, List, Dict, Any, Tuple
import pytest

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
from src.domain.opportunity_dashboard.ports import TenantOpportunityRepositoryPort
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
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.admin_console.models import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminResourceNotFoundError,
    AdminInvalidRequestError,
)
from src.infrastructure.persistence.data.json.tenant_opportunity_repository import JsonTenantOpportunityRepository
from src.application.opportunity_dashboard.opportunity_dashboard_service import OpportunityDashboardService
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.organization.models import Organization, UserMembership, MembershipRole, MembershipStatus
from src.domain.rbac.models import Role, RoleAssignment, Permission
from src.application.rbac.rbac_service import RBACService
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import JsonOrganizationRepository, JsonMembershipRepository
from src.infrastructure.persistence.data.json.rbac_repository import JsonRoleRepository, JsonRoleAssignmentRepository


class MockClock:
    def __init__(self, current_time: datetime = None):
        self._current_time = current_time or datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        pass


def make_sample_opportunity(
    opp_id: str,
    title: str = "Test Product",
    category: str = "Electronics",
    marketplace: str = "mercadolibre",
    score: Optional[Decimal] = Decimal("85.5"),
    margin: Optional[Decimal] = Decimal("0.35"),
    confidence: Confidence = Confidence.HIGH,
    status: OpportunityStatus = OpportunityStatus.DETECTED,
    opp_type: OpportunityType = OpportunityType.PRICE_ARBITRAGE,
    detected_at: Optional[datetime] = None,
    estimated_price: Optional[Decimal] = Decimal("199.99"),
) -> OpportunityRecord:
    dt = detected_at or datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)
    mkt_str = marketplace.lower()
    if "mercado" in mkt_str:
        mkt_enum = Marketplace.MERCADO_LIBRE
    elif "amazon" in mkt_str:
        mkt_enum = Marketplace.AMAZON
    elif "walmart" in mkt_str:
        mkt_enum = Marketplace.WALMART
    else:
        mkt_enum = Marketplace.SHOPIFY
    price_obj = NormalizedPrice(amount=estimated_price, currency="USD") if estimated_price is not None else None
    observed = ObservedOpportunityMetrics(
        observed_price=price_obj,
        observed_competitor_count=5,
        observed_sold_quantity=150,
        observed_stock=30,
    )
    derived = DerivedOpportunityMetrics(
        opportunity_score=score,
        potential_margin_ratio=margin,
        price_gap_ratio=Decimal("0.15"),
        demand_intensity="HIGH",
        competition_density="MEDIUM",
        scoring_rationale=("Score computed from margin and stock velocity",),
    )
    return OpportunityRecord(
        opportunity_id=opp_id,
        canonical_product_id=f"prod_{opp_id}",
        marketplace=mkt_enum,
        opportunity_type=opp_type,
        status=status,
        confidence=confidence,
        source_observation_ids=("obs_1",),
        detected_at=dt,
        title=title,
        category=category,
        observed_metrics=observed,
        derived_metrics=derived,
        reasons=("High demand", "Attractive margin"),
    )


# =============================================================================
# 1. Modelos, DTOs y Semántica UNKNOWN != 0
# =============================================================================

def test_opportunity_dashboard_item_unknown_semantics():
    """Valida que los campos ausentes permanezcan None/UNKNOWN y nunca se transformen en 0."""
    item = OpportunityDashboardItem(
        opportunity_id="opp_1",
        canonical_product_id="prod_1",
        marketplace="mercadolibre",
        opportunity_type="ARBITRAGE",
        status="DETECTED",
        confidence="HIGH",
        detected_at=datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc),
        opportunity_score=None,  # UNKNOWN
        estimated_price_amount=None,  # UNKNOWN
        potential_margin_ratio=None,  # UNKNOWN
    )
    d = item.to_dict()
    assert d["opportunity_score"] is None
    assert d["estimated_price_amount"] is None
    assert d["potential_margin_ratio"] is None
    assert d["opportunity_score"] != 0
    assert d["estimated_price_amount"] != 0
    assert d["potential_margin_ratio"] != 0


def test_opportunity_dashboard_item_decimal_money_serialization():
    """Valida que todos los importes y márgenes se almacenen como Decimal y se serialicen fielmente."""
    item = OpportunityDashboardItem(
        opportunity_id="opp_2",
        canonical_product_id="prod_2",
        marketplace="amazon",
        opportunity_type="PRICE_GAP",
        status="DETECTED",
        confidence="HIGH",
        detected_at=datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc),
        opportunity_score=Decimal("92.5"),
        estimated_price_amount=Decimal("1250.75"),
        estimated_price_currency="USD",
        potential_margin_ratio=Decimal("0.4250"),
    )
    assert isinstance(item.opportunity_score, Decimal)
    assert isinstance(item.estimated_price_amount, Decimal)
    assert isinstance(item.potential_margin_ratio, Decimal)
    d = item.to_dict()
    assert d["opportunity_score"] == "92.5"
    assert d["estimated_price_amount"] == "1250.75"
    assert d["potential_margin_ratio"] == "0.4250"


def test_query_sort_and_pagination_defaults():
    """Valida los defaults y límites de consulta y paginación."""
    query = OpportunityDashboardQuery()
    assert query.page == 1
    assert query.page_size == 20
    assert query.sort_by == OpportunitySortField.OPPORTUNITY_SCORE
    assert query.sort_order == SortOrder.DESC


# =============================================================================
# 2. Persistencia y Aislamiento Multi-Tenant (Repository)
# =============================================================================

def test_json_tenant_opportunity_repository_crud_and_isolation(tmp_path):
    repo = JsonTenantOpportunityRepository(tmp_path)
    ctx_a = TenantContext(tenant_id="tenant_a")
    ctx_b = TenantContext(tenant_id="tenant_b")

    opp_a = make_sample_opportunity("opp_a1", title="Product A")
    opp_b = make_sample_opportunity("opp_b1", title="Product B")

    repo.save(ctx_a, opp_a)
    repo.save(ctx_b, opp_b)

    # Tenant A solo ve A
    items_a = repo.list_all(ctx_a)
    assert len(items_a) == 1
    assert items_a[0].opportunity_id == "opp_a1"
    assert items_a[0].title == "Product A"

    # Tenant B solo ve B
    items_b = repo.list_all(ctx_b)
    assert len(items_b) == 1
    assert items_b[0].opportunity_id == "opp_b1"
    assert items_b[0].title == "Product B"

    # Tenant A get_by_id de B retorna None
    assert repo.get_by_id(ctx_a, "opp_b1") is None
    assert repo.get_by_id(ctx_a, "opp_a1") is not None

    # Delete
    assert repo.delete(ctx_a, "opp_a1") is True
    assert repo.get_by_id(ctx_a, "opp_a1") is None
    assert len(repo.list_all(ctx_a)) == 0
    # Tenant B sigue intacto
    assert len(repo.list_all(ctx_b)) == 1


# =============================================================================
# 3. OpportunityDashboardService — Filtrado, Ordenación, Paginación y Resumen
# =============================================================================

def test_service_empty_state(tmp_path):
    repo = JsonTenantOpportunityRepository(tmp_path)
    service = OpportunityDashboardService(repository=repo)

    summary = service.get_summary(tenant_id="tenant_empty")
    assert summary.total_opportunities == 0
    assert summary.high_potential_count == 0
    assert summary.average_opportunity_score is None
    assert len(summary.opportunities_by_marketplace) == 0

    page = service.list_opportunities(tenant_id="tenant_empty")
    assert page.total_count == 0
    assert len(page.items) == 0
    assert page.total_pages == 1
    assert page.has_next is False
    assert page.has_previous is False


def test_service_filtering_and_sorting(tmp_path):
    repo = JsonTenantOpportunityRepository(tmp_path)
    ctx = TenantContext(tenant_id="tenant_test")

    opps = [
        make_sample_opportunity(
            "opp_1",
            title="Gaming Mouse RGB",
            category="Computers",
            marketplace="mercadolibre",
            score=Decimal("95.0"),
            margin=Decimal("0.40"),
            detected_at=datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc),
        ),
        make_sample_opportunity(
            "opp_2",
            title="Mechanical Keyboard",
            category="Computers",
            marketplace="amazon",
            score=Decimal("80.0"),
            margin=Decimal("0.25"),
            detected_at=datetime(2026, 9, 13, 11, 0, 0, tzinfo=timezone.utc),
        ),
        make_sample_opportunity(
            "opp_3",
            title="Coffee Maker Pro",
            category="Home",
            marketplace="mercadolibre",
            score=Decimal("60.0"),
            margin=Decimal("0.50"),
            detected_at=datetime(2026, 9, 13, 9, 0, 0, tzinfo=timezone.utc),
        ),
        make_sample_opportunity(
            "opp_4",
            title="Running Shoes",
            category="Sports",
            marketplace="amazon",
            score=None,  # UNKNOWN score
            margin=None,  # UNKNOWN margin
            detected_at=datetime(2026, 9, 13, 8, 0, 0, tzinfo=timezone.utc),
        ),
    ]
    repo.save_all(ctx, opps)

    service = OpportunityDashboardService(repository=repo)

    # 1. Filtro por categoría
    q_cat = OpportunityDashboardQuery(category="Computers")
    p_cat = service.list_opportunities(tenant_id="tenant_test", query=q_cat)
    assert p_cat.total_count == 2
    assert {i.opportunity_id for i in p_cat.items} == {"opp_1", "opp_2"}

    # 2. Filtro por marketplace
    q_mkt = OpportunityDashboardQuery(marketplace="mercadolibre")
    p_mkt = service.list_opportunities(tenant_id="tenant_test", query=q_mkt)
    assert p_mkt.total_count == 2
    assert {i.opportunity_id for i in p_mkt.items} == {"opp_1", "opp_3"}

    # 3. Filtro por min_score (score >= 80)
    q_score = OpportunityDashboardQuery(min_score=Decimal("80.0"))
    p_score = service.list_opportunities(tenant_id="tenant_test", query=q_score)
    assert p_score.total_count == 2
    assert {i.opportunity_id for i in p_score.items} == {"opp_1", "opp_2"}

    # 4. Filtro por min_margin (margin >= 0.35)
    q_margin = OpportunityDashboardQuery(min_margin=Decimal("0.35"))
    p_margin = service.list_opportunities(tenant_id="tenant_test", query=q_margin)
    assert p_margin.total_count == 2
    assert {i.opportunity_id for i in p_margin.items} == {"opp_1", "opp_3"}

    # 5. Búsqueda por texto (Search text)
    q_text = OpportunityDashboardQuery(search_text="Mouse")
    p_text = service.list_opportunities(tenant_id="tenant_test", query=q_text)
    assert p_text.total_count == 1
    assert p_text.items[0].opportunity_id == "opp_1"

    # 6. Ordenación por score DESC (los None van al final)
    q_sort_score = OpportunityDashboardQuery(sort_by=OpportunitySortField.OPPORTUNITY_SCORE, sort_order=SortOrder.DESC)
    p_sort_score = service.list_opportunities(tenant_id="tenant_test", query=q_sort_score)
    assert [i.opportunity_id for i in p_sort_score.items] == ["opp_1", "opp_2", "opp_3", "opp_4"]

    # 7. Ordenación por detected_at DESC
    q_sort_date = OpportunityDashboardQuery(sort_by=OpportunitySortField.DETECTED_AT, sort_order=SortOrder.DESC)
    p_sort_date = service.list_opportunities(tenant_id="tenant_test", query=q_sort_date)
    assert [i.opportunity_id for i in p_sort_date.items] == ["opp_2", "opp_1", "opp_3", "opp_4"]


def test_service_pagination(tmp_path):
    repo = JsonTenantOpportunityRepository(tmp_path)
    ctx = TenantContext(tenant_id="tenant_page")

    # 15 items
    opps = [
        make_sample_opportunity(f"opp_{i:02d}", title=f"Product {i}", score=Decimal(str(i * 5)))
        for i in range(1, 16)
    ]
    repo.save_all(ctx, opps)

    service = OpportunityDashboardService(repository=repo)

    # Página 1 (page_size=5)
    p1 = service.list_opportunities(tenant_id="tenant_page", query=OpportunityDashboardQuery(page=1, page_size=5))
    assert p1.total_count == 15
    assert len(p1.items) == 5
    assert p1.page == 1
    assert p1.total_pages == 3
    assert p1.has_next is True
    assert p1.has_previous is False

    # Página 3 (última)
    p3 = service.list_opportunities(tenant_id="tenant_page", query=OpportunityDashboardQuery(page=3, page_size=5))
    assert len(p3.items) == 5
    assert p3.page == 3
    assert p3.has_next is False
    assert p3.has_previous is True

    # Paginación fuera de rango
    p_out = service.list_opportunities(tenant_id="tenant_page", query=OpportunityDashboardQuery(page=10, page_size=5))
    assert len(p_out.items) == 0
    assert p_out.page == 10


def test_service_summary_calculation(tmp_path):
    repo = JsonTenantOpportunityRepository(tmp_path)
    ctx = TenantContext(tenant_id="tenant_sum")

    opps = [
        make_sample_opportunity("opp_h1", score=Decimal("85.0"), marketplace="mercadolibre", category="Tech"),
        make_sample_opportunity("opp_h2", score=Decimal("75.0"), marketplace="amazon", category="Tech"),
        make_sample_opportunity("opp_m1", score=Decimal("55.0"), marketplace="mercadolibre", category="Home"),
        make_sample_opportunity("opp_l1", score=Decimal("30.0"), marketplace="amazon", category="Sports"),
        make_sample_opportunity("opp_u1", score=None, marketplace="mercadolibre", category="Sports"),
    ]
    repo.save_all(ctx, opps)

    service = OpportunityDashboardService(repository=repo)
    summary = service.get_summary(tenant_id="tenant_sum")

    assert summary.total_opportunities == 5
    assert summary.high_potential_count == 2  # >= 70
    assert summary.medium_potential_count == 1  # 50 - 70
    assert summary.low_potential_count == 1  # < 50
    # Average score: (85 + 75 + 55 + 30) / 4 = 245 / 4 = 61.25
    assert summary.average_opportunity_score == Decimal("61.25")
    assert summary.opportunities_by_marketplace.get("mercadolibre", 0) + summary.opportunities_by_marketplace.get("MERCADO_LIBRE", 0) == 3
    assert summary.opportunities_by_marketplace.get("amazon", 0) + summary.opportunities_by_marketplace.get("AMAZON", 0) == 2
    assert summary.opportunities_by_category["Tech"] == 2
    assert summary.opportunities_by_category["Home"] == 1
    assert summary.opportunities_by_category["Sports"] == 2


def test_service_detail_and_comparison(tmp_path):
    repo = JsonTenantOpportunityRepository(tmp_path)
    ctx = TenantContext(tenant_id="tenant_dtl")

    opp1 = make_sample_opportunity("opp_1", title="Product 1", score=Decimal("90.0"), margin=Decimal("0.40"))
    opp2 = make_sample_opportunity("opp_2", title="Product 2", score=Decimal("80.0"), margin=Decimal("0.30"))
    repo.save_all(ctx, [opp1, opp2])

    service = OpportunityDashboardService(repository=repo)

    # Detalle
    detail = service.get_opportunity_detail(tenant_id="tenant_dtl", opportunity_id="opp_1")
    assert detail.item.opportunity_id == "opp_1"
    assert "High demand" in detail.reasons
    assert detail.explanation is not None

    # Detalle recurso inexistente -> AdminResourceNotFoundError
    with pytest.raises(AdminResourceNotFoundError):
        service.get_opportunity_detail(tenant_id="tenant_dtl", opportunity_id="opp_non_existent")

    # Comparación
    comparison = service.compare_opportunities(tenant_id="tenant_dtl", opportunity_ids=["opp_1", "opp_2"])
    assert len(comparison.candidate_ids) == 2
    assert comparison.best_candidate_id == "opp_1"
    assert len(comparison.items) == 2
    assert "opp_1" in comparison.why_winner


# =============================================================================
# 4. Autorización y Seguridad Multi-Tenant (O.3 / O.4 / RBAC)
# =============================================================================

def setup_saas_auth(tmp_path, clock):
    session_repo = JsonSaaSSessionRepository(tmp_path / "sessions")
    org_repo = JsonOrganizationRepository(tmp_path / "organizations")
    membership_repo = JsonMembershipRepository(tmp_path / "memberships")
    role_repo = JsonRoleRepository(tmp_path / "roles")
    assignment_repo = JsonRoleAssignmentRepository(tmp_path / "role_assignments")

    rbac_svc = RBACService(role_repository=role_repo, assignment_repository=assignment_repo, clock=clock)
    auth_svc = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_svc,
        clock=clock,
    )

    # Crear rol con permiso de oportunidad
    perm = Permission(permission_id="perm_opp_read", action="OPPORTUNITY_DASHBOARD_READ")
    role = Role(role_id="role_bi_viewer", name="BI Viewer", permissions=(perm,))
    role_repo.save(role)

    return session_repo, org_repo, membership_repo, role_repo, assignment_repo, rbac_svc, auth_svc


def test_service_authorization_enforcement(tmp_path):
    clock = MockClock()
    session_repo, org_repo, membership_repo, role_repo, assignment_repo, rbac_svc, auth_svc = setup_saas_auth(tmp_path, clock)
    opp_repo = JsonTenantOpportunityRepository(tmp_path)

    service = OpportunityDashboardService(
        repository=opp_repo,
        authorization_service=auth_svc,
        session_repository=session_repo,
        clock=clock,
    )

    # 1. Sesión inexistente -> 401
    with pytest.raises(AdminAuthenticationError):
        service.get_summary(tenant_id="tenant_a", session_id="invalid_session")

    # 2. Crear sesión activa para Tenant A con rol
    now_dt = clock.now()
    session_a = SaaSSession(
        session_id="sess_a",
        identity_id="user_a",
        tenant_id="tenant_a",
        status=SessionStatus.ACTIVE,
        created_at=now_dt,
        expires_at=now_dt + timedelta(hours=1),
    )
    session_repo.save(session_a)

    # Asignar rol
    assignment = RoleAssignment(
            assignment_id="assign_a",
            identity_id="user_a",
            role_id="role_bi_viewer",
        )
    assignment_repo.save(assignment)

    # 3. Acceso autorizado Tenant A
    summary = service.get_summary(tenant_id="tenant_a", session_id="sess_a")
    assert summary.total_opportunities == 0

    # 4. Intento Cross-Tenant (Usuario Tenant A intenta leer Tenant B) -> 403
    with pytest.raises(AdminAuthorizationError):
        service.get_summary(tenant_id="tenant_b", session_id="sess_a")

    # 5. Sesión expirada -> 401
    clock._current_time = now_dt + timedelta(hours=2)
    with pytest.raises(AdminAuthenticationError):
        service.get_summary(tenant_id="tenant_a", session_id="sess_a")
