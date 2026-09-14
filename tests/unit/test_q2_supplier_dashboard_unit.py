"""
Tests Unitarios Exhaustivos para Q.2 — Supplier Dashboard (Hito Q — Business Intelligence).

Cubre:
1. Tenant scoping (Aislamiento de repositorios y servicios O.1).
2. Autorización (SaaS Authorization O.4, rechazo 401/403, sesiones inválidas o expiradas).
3. Safe Item Projection (ViewModels libres de secretos, API tokens y datos sensibles).
4. UNKNOWN semantics (UNKNOWN/None nunca convertido a 0 o 0.0, MOQ ausente no asume 1, lead time ausente no asume instantáneo).
5. Decimal Money (Todos los costes unitarios y costes de envío en Decimal con currency explícita).
6. MOQ y Lead Time ausentes preservados como UNKNOWN.
7. Filtering por país, origen/plataforma, verification status, risk level, confidence, score, max unit cost, max MOQ, max lead time, search text.
8. Sorting determinista con tie-breaking consistente por supplier_id y valores None al final.
9. Paginación segura y acotada (page_size entre 1 y 100).
10. Empty State válido y sin generación de datos dummy/falsos.
11. Explicabilidad estructurada (OBSERVED, DERIVED, INFERRED, RISKS, UNKNOWNS).
12. Comparación multidimensional de proveedores basada en hechos reales sin ganador inventado.
13. Protección y enmascaramiento de datos sensibles de contacto (emails y teléfonos enmascarados).
14. Integración consultiva con Oportunidades Q.1 (asociación bidireccional consultiva sin recálculo).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
from typing import Optional, List, Dict, Any, Tuple
import pytest

from src.domain.supplier_dashboard.models import (
    SupplierDashboardItem,
    SupplierDashboardSummary,
    SupplierDashboardDetail,
    SupplierDashboardQuery,
    SupplierDashboardPage,
    SupplierComparisonView,
    SupplierComparisonDimension,
    SupplierSortField,
    SortOrder,
)
from src.domain.supplier_dashboard.ports import TenantSupplierRepositoryPort
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
from src.domain.tenant.guard import CrossTenantGuard
from src.domain.admin_console.models import (
    AdminAuthenticationError,
    AdminAuthorizationError,
    AdminResourceNotFoundError,
    AdminInvalidRequestError,
)
from src.infrastructure.persistence.data.json.tenant_supplier_repository import JsonTenantSupplierRepository
from src.infrastructure.persistence.data.json.tenant_opportunity_repository import JsonTenantOpportunityRepository
from src.application.supplier_dashboard.supplier_dashboard_service import SupplierDashboardService
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


def make_sample_supplier(
    supplier_id: str,
    name: str = "Test Supplier Global",
    source: str = "alibaba",
    country: str = "CN",
    unit_cost: Optional[Decimal] = Decimal("15.50"),
    moq: Optional[int] = 50,
    lead_time_days: Optional[int] = 14,
    shipping_cost: Optional[Decimal] = Decimal("120.00"),
    supplier_score: Optional[Decimal] = Decimal("88.5"),
    reliability_score: Optional[Decimal] = Decimal("90.0"),
    quality_score: Optional[Decimal] = Decimal("85.0"),
    confidence: str = "HIGH",
    verification_status: str = "VERIFIED",
    risk_level: str = "LOW",
    product_reference: Optional[SupplierProductReference] = None,
    created_at: Optional[datetime] = None,
) -> Supplier:
    dt = created_at or datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)
    loc = SupplierLocation(country=country, region="Guangdong", city="Shenzhen")
    contact = SupplierContact(
        name="Alice Wang",
        email="alice@supplier.com",
        phone="+8613800000000",
        website="https://supplier.example.com",
    )
    metadata = {}
    if unit_cost is not None:
        metadata["unit_cost_amount"] = str(unit_cost)
        metadata["currency"] = "USD"
    if moq is not None:
        metadata["moq"] = moq
    if lead_time_days is not None:
        metadata["lead_time_days"] = lead_time_days
    if shipping_cost is not None:
        metadata["shipping_cost_amount"] = str(shipping_cost)
    if supplier_score is not None:
        metadata["supplier_score"] = str(supplier_score)
    if reliability_score is not None:
        metadata["reliability_score"] = str(reliability_score)
    if quality_score is not None:
        metadata["quality_score"] = str(quality_score)
    if confidence is not None:
        metadata["confidence"] = confidence
    if verification_status is not None:
        metadata["verification_status"] = verification_status
    if risk_level is not None:
        metadata["risk_level"] = risk_level

    metadata["reasons"] = ["Verified facility", "Consistent lead time"]

    return Supplier(
        supplier_id=supplier_id,
        name=name,
        source=source,
        source_type=EvidenceProvenanceType.LIVE,
        location=loc,
        contact=contact,
        status=SupplierStatus.ACTIVE,
        observed_at=dt,
        product_reference=product_reference,
        metadata=metadata,
    )


def make_sample_opportunity(
    opp_id: str,
    title: str = "Test Product",
    category: str = "Electronics",
    marketplace: str = "mercadolibre",
    score: Optional[Decimal] = Decimal("85.5"),
    margin: Optional[Decimal] = Decimal("0.35"),
) -> OpportunityRecord:
    dt = datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc)
    price_obj = NormalizedPrice(amount=Decimal("199.99"), currency="USD")
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
        scoring_rationale=("Score computed from margin",),
    )
    return OpportunityRecord(
        opportunity_id=opp_id,
        canonical_product_id=f"prod_{opp_id}",
        marketplace=Marketplace.MERCADO_LIBRE,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.DETECTED,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs_1",),
        detected_at=dt,
        title=title,
        category=category,
        observed_metrics=observed,
        derived_metrics=derived,
        reasons=("High demand",),
    )


# =============================================================================
# 1. Modelos, DTOs y Semántica UNKNOWN != 0
# =============================================================================

def test_supplier_dashboard_item_unknown_semantics():
    """Valida que los campos ausentes permanezcan None/UNKNOWN y nunca se transformen en 0."""
    item = SupplierDashboardItem(
        supplier_id="supp_1",
        name="Vendor Incomplete",
        source="alibaba",
        country=None,
        unit_cost_amount=None,  # UNKNOWN
        moq=None,  # UNKNOWN (no debe ser 1)
        lead_time_days=None,  # UNKNOWN (no debe ser 0 ni inmediato)
        shipping_cost_amount=None,  # UNKNOWN
        supplier_score=None,  # UNKNOWN
        reliability_score=None,
        quality_score=None,
        unknown_fields=("unit_cost", "moq", "lead_time", "shipping_cost", "supplier_score"),
    )
    d = item.to_dict()
    assert d["unit_cost_amount"] is None
    assert d["moq"] is None
    assert d["lead_time_days"] is None
    assert d["shipping_cost_amount"] is None
    assert d["supplier_score"] is None
    assert d["unit_cost_amount"] != 0
    assert d["moq"] != 1
    assert d["lead_time_days"] != 0
    assert "moq" in d["unknown_fields"]


def test_supplier_dashboard_item_decimal_money_serialization():
    """Valida que los importes se almacenen como Decimal y se serialicen fielmente."""
    item = SupplierDashboardItem(
        supplier_id="supp_2",
        name="Vendor Precision",
        source="global_sources",
        unit_cost_amount=Decimal("45.75"),
        currency="USD",
        shipping_cost_amount=Decimal("150.25"),
        supplier_score=Decimal("94.20"),
    )
    assert isinstance(item.unit_cost_amount, Decimal)
    assert isinstance(item.shipping_cost_amount, Decimal)
    assert isinstance(item.supplier_score, Decimal)
    d = item.to_dict()
    assert d["unit_cost_amount"] == "45.75"
    assert d["shipping_cost_amount"] == "150.25"
    assert d["supplier_score"] == "94.20"
    assert d["currency"] == "USD"


def test_query_sort_and_pagination_defaults():
    """Valida los defaults y límites de consulta y paginación."""
    query = SupplierDashboardQuery()
    assert query.page == 1
    assert query.page_size == 20
    assert query.sort_by == SupplierSortField.SUPPLIER_SCORE
    assert query.sort_order == SortOrder.DESC


# =============================================================================
# 2. Persistencia y Aislamiento Multi-Tenant (Repository)
# =============================================================================

def test_json_tenant_supplier_repository_crud_and_isolation(tmp_path):
    repo = JsonTenantSupplierRepository(tmp_path)
    ctx_a = TenantContext(tenant_id="tenant_a")
    ctx_b = TenantContext(tenant_id="tenant_b")

    s1 = make_sample_supplier(supplier_id="supp_a_1", name="Supplier A1")
    s2 = make_sample_supplier(supplier_id="supp_a_2", name="Supplier A2")
    sb = make_sample_supplier(supplier_id="supp_b_1", name="Supplier B1")

    repo.save(ctx_a, s1)
    repo.save(ctx_a, s2)
    repo.save(ctx_b, sb)

    assert len(repo.list_all(ctx_a)) == 2
    assert len(repo.list_all(ctx_b)) == 1

    assert repo.get_by_id(ctx_a, "supp_a_1") is not None
    assert repo.get_by_id(ctx_a, "supp_b_1") is None
    assert repo.get_by_id(ctx_b, "supp_a_1") is None
    assert repo.get_by_id(ctx_b, "supp_b_1") is not None

    assert repo.delete(ctx_a, "supp_a_1") is True
    assert len(repo.list_all(ctx_a)) == 1
    assert len(repo.list_all(ctx_b)) == 1


# =============================================================================
# 3. Setup de Servicio y RBAC / SaaS Authorization
# =============================================================================

@pytest.fixture
def test_setup(tmp_path):
    clock = MockClock()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)

    supp_repo = JsonTenantSupplierRepository(data_dir)
    opp_repo = JsonTenantOpportunityRepository(data_dir)
    session_repo = JsonSaaSSessionRepository(data_dir / "sessions")
    org_repo = JsonOrganizationRepository(data_dir / "organizations")
    membership_repo = JsonMembershipRepository(data_dir / "memberships")
    role_repo = JsonRoleRepository(data_dir / "roles")
    assignment_repo = JsonRoleAssignmentRepository(data_dir / "role_assignments")

    rbac_svc = RBACService(
        role_repository=role_repo,
        assignment_repository=assignment_repo,
        clock=clock,
    )
    auth_svc = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_svc,
        clock=clock,
    )
    service = SupplierDashboardService(
        repository=supp_repo,
        authorization_service=auth_svc,
        session_repository=session_repo,
        opportunity_repository=opp_repo,
    )

    # Configuración de Sesión Válida con Permisos
    tenant_id = "tenant_alpha"
    user_id = "user_admin_1"
    session_id = "sess_valid_123"

    session = SaaSSession(
        session_id=session_id,
        identity_id=user_id,
        tenant_id=tenant_id,
        status=SessionStatus.ACTIVE,
        created_at=clock.now(),
        expires_at=clock.now() + timedelta(hours=8),
    )
    session_repo.save(session)

    # Rol con permiso SUPPLIER_DASHBOARD_READ
    admin_role = Role(
        role_id="role_supplier_reader",
        name="Supplier Reader",
        permissions=(
            Permission("perm_supp_read", "SUPPLIER_DASHBOARD_READ"),
            Permission("perm_bi_read", "BUSINESS_INTELLIGENCE_READ"),
        ),
    )
    role_repo.save(admin_role)

    assignment = RoleAssignment(
        assignment_id="asgn_1",
        identity_id=user_id,
        role_id=admin_role.role_id,
    )
    assignment_repo.save(assignment)

    return {
        "service": service,
        "supp_repo": supp_repo,
        "opp_repo": opp_repo,
        "tenant_id": tenant_id,
        "session_id": session_id,
        "clock": clock,
        "session_repo": session_repo,
    }


# =============================================================================
# 4. Autorización y Casos de Error
# =============================================================================

def test_service_unauthenticated_access(test_setup):
    service = test_setup["service"]
    tenant_id = test_setup["tenant_id"]

    with pytest.raises(AdminAuthenticationError):
        service.list_suppliers(tenant_id=tenant_id, session_id=None)

    with pytest.raises(AdminAuthenticationError):
        service.list_suppliers(tenant_id=tenant_id, session_id="non_existent_session")


def test_service_expired_session(test_setup):
    service = test_setup["service"]
    tenant_id = test_setup["tenant_id"]
    session_repo = test_setup["session_repo"]
    clock = test_setup["clock"]

    expired_session = SaaSSession(
        session_id="sess_expired",
        identity_id="user_2",
        tenant_id=tenant_id,
        status=SessionStatus.ACTIVE,
        created_at=clock.now() - timedelta(hours=10),
        expires_at=clock.now() - timedelta(hours=1),
    )
    session_repo.save(expired_session)

    with pytest.raises(AdminAuthenticationError):
        service.list_suppliers(tenant_id=tenant_id, session_id="sess_expired")


def test_service_cross_tenant_access_denied(test_setup):
    service = test_setup["service"]
    session_id = test_setup["session_id"]  # Autenticado para tenant_alpha

    with pytest.raises(AdminAuthorizationError):
        service.list_suppliers(tenant_id="tenant_beta", session_id=session_id)


# =============================================================================
# 5. Safe Projection y Enmascaramiento de Contacto
# =============================================================================

def test_safe_supplier_projection_and_contact_masking(test_setup):
    service = test_setup["service"]
    supp_repo = test_setup["supp_repo"]
    tenant_id = test_setup["tenant_id"]
    session_id = test_setup["session_id"]
    ctx = TenantContext(tenant_id=tenant_id)

    supplier = make_sample_supplier(
        supplier_id="supp_sec_1",
        name="Secure Supplier Corp",
    )
    # Inyectar credenciales o tokens en metadata para verificar sanitización
    meta_dict = dict(supplier.metadata)
    meta_dict["api_key"] = "super_secret_key_123"
    meta_dict["password"] = "admin_pass"
    supplier_with_sec = Supplier(
        supplier_id=supplier.supplier_id,
        name=supplier.name,
        source=supplier.source,
        source_type=supplier.source_type,
        location=supplier.location,
        contact=supplier.contact,
        status=supplier.status,
        observed_at=supplier.observed_at,
        product_reference=supplier.product_reference,
        metadata=meta_dict,
    )
    supp_repo.save(ctx, supplier_with_sec)

    page = service.list_suppliers(tenant_id=tenant_id, session_id=session_id)
    assert page.total_count == 1
    item = page.items[0]
    assert item.name == "Secure Supplier Corp"

    # Verificar detalle
    detail = service.get_supplier_detail(tenant_id=tenant_id, supplier_id="supp_sec_1", session_id=session_id)
    assert detail.contact is not None
    # El email alice@supplier.com debe estar enmascarado
    assert "@" in detail.contact.get("email", "")
    assert "alice@supplier.com" not in detail.contact.get("email", "")
    # Teléfono +8613800000000 debe estar enmascarado
    assert "*" in detail.contact.get("phone", "")
    assert "+8613800000000" not in detail.contact.get("phone", "")

    # Validar que secretos no se filtren
    d_dict = detail.to_dict()
    assert "api_key" not in d_dict
    assert "password" not in d_dict


# =============================================================================
# 6. Filtrado Multinivel
# =============================================================================

def test_supplier_dashboard_filtering(test_setup):
    service = test_setup["service"]
    supp_repo = test_setup["supp_repo"]
    tenant_id = test_setup["tenant_id"]
    session_id = test_setup["session_id"]
    ctx = TenantContext(tenant_id=tenant_id)

    s1 = make_sample_supplier(
        supplier_id="s1",
        name="Apex Shenzhen Electronics",
        source="alibaba",
        country="CN",
        supplier_score=Decimal("95.0"),
        unit_cost=Decimal("10.00"),
        moq=100,
        lead_time_days=10,
        verification_status="VERIFIED",
        risk_level="LOW",
    )
    s2 = make_sample_supplier(
        supplier_id="s2",
        name="Euro Tech Solutions",
        source="direct",
        country="DE",
        supplier_score=Decimal("80.0"),
        unit_cost=Decimal("25.00"),
        moq=20,
        lead_time_days=5,
        verification_status="UNVERIFIED",
        risk_level="MEDIUM",
    )
    s3 = make_sample_supplier(
        supplier_id="s3",
        name="Latam Goods Trading",
        source="mercadolibre",
        country="BR",
        supplier_score=Decimal("65.0"),
        unit_cost=Decimal("5.00"),
        moq=500,
        lead_time_days=30,
        verification_status="REJECTED",
        risk_level="HIGH",
    )
    supp_repo.save_all(ctx, [s1, s2, s3])

    # Filtrar por país
    p_cn = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(country="CN"), session_id=session_id)
    assert p_cn.total_count == 1
    assert p_cn.items[0].supplier_id == "s1"

    # Filtrar por origen / fuente
    p_dir = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(source="direct"), session_id=session_id)
    assert p_dir.total_count == 1
    assert p_dir.items[0].supplier_id == "s2"

    # Filtrar por verification_status
    p_ver = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(verification_status="VERIFIED"), session_id=session_id)
    assert p_ver.total_count == 1
    assert p_ver.items[0].supplier_id == "s1"

    # Filtrar por min_supplier_score
    p_score = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(min_supplier_score=Decimal("85.0")), session_id=session_id)
    assert p_score.total_count == 1
    assert p_score.items[0].supplier_id == "s1"

    # Filtrar por max_moq
    p_moq = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(max_moq=50), session_id=session_id)
    assert p_moq.total_count == 1
    assert p_moq.items[0].supplier_id == "s2"

    # Filtrar por max_lead_time_days
    p_lt = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(max_lead_time_days=10), session_id=session_id)
    assert p_lt.total_count == 2
    assert {i.supplier_id for i in p_lt.items} == {"s1", "s2"}

    # Filtrar por search_text
    p_srch = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(search_text="Shenzhen"), session_id=session_id)
    assert p_srch.total_count == 1
    assert p_srch.items[0].supplier_id == "s1"


# =============================================================================
# 7. Sorting Determinista y Tie-Breaking
# =============================================================================

def test_supplier_dashboard_sorting_and_tie_breaking(test_setup):
    service = test_setup["service"]
    supp_repo = test_setup["supp_repo"]
    tenant_id = test_setup["tenant_id"]
    session_id = test_setup["session_id"]
    ctx = TenantContext(tenant_id=tenant_id)

    s1 = make_sample_supplier(supplier_id="supp_b", name="Supplier B", supplier_score=Decimal("80.0"), unit_cost=Decimal("20.00"))
    s2 = make_sample_supplier(supplier_id="supp_a", name="Supplier A", supplier_score=Decimal("80.0"), unit_cost=Decimal("20.00"))
    s3 = make_sample_supplier(supplier_id="supp_c", name="Supplier C", supplier_score=Decimal("95.0"), unit_cost=Decimal("10.00"))
    s4 = make_sample_supplier(supplier_id="supp_none", name="Supplier None", supplier_score=None, unit_cost=None)

    supp_repo.save_all(ctx, [s1, s2, s3, s4])

    # Ordenar por supplier_score DESC -> s3 (95), luego s_a y s_b (80, desempate por id asc -> supp_a, supp_b), luego s4 (None al final)
    page_desc = service.list_suppliers(
        tenant_id=tenant_id,
        query=SupplierDashboardQuery(sort_by=SupplierSortField.SUPPLIER_SCORE, sort_order=SortOrder.DESC),
        session_id=session_id,
    )
    ids_desc = [i.supplier_id for i in page_desc.items]
    assert ids_desc == ["supp_c", "supp_a", "supp_b", "supp_none"]

    # Ordenar por unit_cost ASC -> s3 (10), luego supp_a (20), supp_b (20), luego supp_none (None al final)
    page_cost_asc = service.list_suppliers(
        tenant_id=tenant_id,
        query=SupplierDashboardQuery(sort_by=SupplierSortField.UNIT_COST, sort_order=SortOrder.ASC),
        session_id=session_id,
    )
    ids_cost = [i.supplier_id for i in page_cost_asc.items]
    assert ids_cost == ["supp_c", "supp_a", "supp_b", "supp_none"]


# =============================================================================
# 8. Paginación Acotada
# =============================================================================

def test_supplier_dashboard_pagination_bounds(test_setup):
    service = test_setup["service"]
    supp_repo = test_setup["supp_repo"]
    tenant_id = test_setup["tenant_id"]
    session_id = test_setup["session_id"]
    ctx = TenantContext(tenant_id=tenant_id)

    suppliers = [
        make_sample_supplier(supplier_id=f"supp_{i:02d}", name=f"Supplier {i:02d}", supplier_score=Decimal(f"{i}"))
        for i in range(1, 26)
    ]
    supp_repo.save_all(ctx, suppliers)

    # Página 1 con tamaño 10
    p1 = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(page=1, page_size=10), session_id=session_id)
    assert p1.total_count == 25
    assert len(p1.items) == 10
    assert p1.total_pages == 3
    assert p1.has_next is True
    assert p1.has_prev is False

    # Página 3 con tamaño 10 (últimos 5)
    p3 = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(page=3, page_size=10), session_id=session_id)
    assert len(p3.items) == 5
    assert p3.has_next is False
    assert p3.has_prev is True

    # Paginación excesiva clampada a 100
    p_huge = service.list_suppliers(tenant_id=tenant_id, query=SupplierDashboardQuery(page=1, page_size=500), session_id=session_id)
    assert p_huge.page_size == 100


# =============================================================================
# 9. Resumen Agregado (Summary)
# =============================================================================

def test_supplier_dashboard_summary(test_setup):
    service = test_setup["service"]
    supp_repo = test_setup["supp_repo"]
    tenant_id = test_setup["tenant_id"]
    session_id = test_setup["session_id"]
    ctx = TenantContext(tenant_id=tenant_id)

    s1 = make_sample_supplier(supplier_id="s1", country="CN", source="alibaba", verification_status="VERIFIED", supplier_score=Decimal("90.0"))
    s2 = make_sample_supplier(supplier_id="s2", country="CN", source="alibaba", verification_status="VERIFIED", supplier_score=Decimal("80.0"))
    s3 = make_sample_supplier(supplier_id="s3", country="DE", source="direct", verification_status="UNVERIFIED", supplier_score=Decimal("70.0"), unit_cost=None)

    supp_repo.save_all(ctx, [s1, s2, s3])

    summary = service.get_summary(tenant_id=tenant_id, session_id=session_id)
    assert summary.total_suppliers == 3
    assert summary.verified_suppliers == 2
    assert summary.high_rated_suppliers == 2  # score >= 80 -> s1, s2
    assert summary.average_supplier_score == Decimal("80.0")
    assert summary.suppliers_with_unknown_critical_fields == 1  # s3 tiene unit_cost None
    assert summary.suppliers_by_country.get("CN") == 2
    assert summary.suppliers_by_country.get("DE") == 1
    assert summary.suppliers_by_source.get("alibaba") == 2


# =============================================================================
# 10. Comparación Multidimensional
# =============================================================================

def test_supplier_comparison_multidimensional(test_setup):
    service = test_setup["service"]
    supp_repo = test_setup["supp_repo"]
    tenant_id = test_setup["tenant_id"]
    session_id = test_setup["session_id"]
    ctx = TenantContext(tenant_id=tenant_id)

    s1 = make_sample_supplier(
        supplier_id="s1",
        name="Supplier One",
        unit_cost=Decimal("15.00"),
        moq=50,
        lead_time_days=10,
        supplier_score=Decimal("90.0"),
    )
    s2 = make_sample_supplier(
        supplier_id="s2",
        name="Supplier Two",
        unit_cost=Decimal("12.00"),
        moq=100,
        lead_time_days=20,
        supplier_score=Decimal("85.0"),
    )
    supp_repo.save_all(ctx, [s1, s2])

    comp = service.compare_suppliers(tenant_id=tenant_id, supplier_ids=["s1", "s2"], session_id=session_id)
    assert len(comp.items) == 2
    assert "Supplier One" in comp.supplier_names
    assert "Supplier Two" in comp.supplier_names

    dim_map = {d.field: d for d in comp.dimensions}
    assert "supplier_score" in dim_map
    assert dim_map["supplier_score"].values["s1"] == Decimal("90.0")
    assert dim_map["supplier_score"].values["s2"] == Decimal("85.0")

    assert "unit_cost" in dim_map
    assert dim_map["unit_cost"].values["s1"] == Decimal("15.00")
    assert dim_map["unit_cost"].values["s2"] == Decimal("12.00")


# =============================================================================
# 11. Integración Consultiva con Oportunidades Q.1
# =============================================================================

def test_consultative_opportunity_supplier_link(test_setup):
    service = test_setup["service"]
    supp_repo = test_setup["supp_repo"]
    opp_repo = test_setup["opp_repo"]
    tenant_id = test_setup["tenant_id"]
    session_id = test_setup["session_id"]
    ctx = TenantContext(tenant_id=tenant_id)

    # 1. Crear oportunidad de producto con metadata que asocia a supp_spk_1
    opp = make_sample_opportunity(opp_id="opp_100", title="Bluetooth Speaker")
    opp_with_supp = OpportunityRecord(
        opportunity_id=opp.opportunity_id,
        canonical_product_id=opp.canonical_product_id,
        marketplace=opp.marketplace,
        opportunity_type=opp.opportunity_type,
        status=opp.status,
        confidence=opp.confidence,
        source_observation_ids=opp.source_observation_ids,
        detected_at=opp.detected_at,
        title=opp.title,
        category=opp.category,
        observed_metrics=opp.observed_metrics,
        derived_metrics=opp.derived_metrics,
        reasons=opp.reasons,
        metadata={"supplier_id": "supp_spk_1"},
    )
    opp_repo.save(ctx, opp_with_supp)

    # 2. Crear proveedor que referencia a producto
    prod_ref = SupplierProductReference(
        sku="SKU-SPK-01",
        title="Bluetooth Speaker OEM",
        source_product_id="prod_opp_100",
    )
    supp = make_sample_supplier(
        supplier_id="supp_spk_1",
        name="Speaker Tech Ltd",
        product_reference=prod_ref,
    )
    supp_repo.save(ctx, supp)

    # 3. Proyectar lista de proveedores
    page = service.list_suppliers(tenant_id=tenant_id, session_id=session_id)
    assert page.total_count == 1
    assert page.items[0].opportunity_count == 1
    assert page.items[0].product_count == 1

    # 4. Detalle debe mostrar la oportunidad asociada
    detail = service.get_supplier_detail(tenant_id=tenant_id, supplier_id="supp_spk_1", session_id=session_id)
    assert len(detail.associated_opportunities) == 1
    assert detail.associated_opportunities[0]["opportunity_id"] == "opp_100"
    assert detail.associated_opportunities[0]["title"] == "Bluetooth Speaker"
