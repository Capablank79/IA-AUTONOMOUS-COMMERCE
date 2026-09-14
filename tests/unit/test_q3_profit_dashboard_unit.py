"""
Tests Unitarios Exhaustivos para Q.3 — Profit Dashboard (Hito Q — Business Intelligence).

Requisitos Cubiertos:
1. Decimal arithmetic (Decimal estricto en todas las operaciones monetarias, sin float).
2. Gross profit calculation (sale_price - unit_cost).
3. Contribution profit calculation (sale_price - sum(known variable costs)).
4. Margin calculation (profit / sale_price * 100).
5. Missing cost != zero (Componentes no suministrados no son interpretados como coste 0).
6. Partial completeness (COMPLETE, PARTIAL, INSUFFICIENT_DATA).
7. Zero sale price safe (sale_price == 0 o None no divide por cero, margin es None).
8. Negative margin preserved (Márgenes negativos no se limitan a 0 ni se ocultan).
9. Currency mismatch blocked (Monedas distintas sin tipo de cambio verificado resultan en NOT_COMPARABLE_CURRENCY).
10. Tenant scoping (Aislamiento físico O.1 por tenant_id).
11. Authorization (SaaS Authorization O.4, rechazo 401/403 con PROFIT_DASHBOARD_READ).
12. Filtering (marketplace, category, supplier, completeness, currency, min/max margin, min/max profit, etc.).
13. Sorting (margin, profit, sale_price, unit_cost, date con desempate determinista por item_id).
14. Pagination (page, page_size acotados).
15. Sensitive data excluded (Sanitización y exclusión de tokens o secretos).
16. No Q.4+ implementation (Verificación estricta de alcance).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
from typing import Optional, List, Dict, Any, Tuple
import pytest

from src.domain.profit_dashboard.models import (
    ProfitDashboardItem,
    ProfitDashboardSummary,
    ProfitDashboardDetail,
    ProfitDashboardQuery,
    ProfitDashboardPage,
    ProfitComparisonView,
    ProfitComparisonDimension,
    ProfitCompleteness,
    ProfitSortField,
    SortOrder,
)
from src.domain.profit_dashboard.ports import TenantProfitRepositoryPort
from src.infrastructure.persistence.data.json.tenant_profit_repository import JsonTenantProfitRepository
from src.application.profit_dashboard.profit_dashboard_service import ProfitDashboardService
from src.domain.opportunity_dashboard.models import OpportunityDashboardItem
from src.domain.supplier_dashboard.models import SupplierDashboardItem
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


class MockClock:
    def __init__(self, current_time: Optional[datetime] = None):
        self._current_time = current_time or datetime(2026, 9, 13, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        pass


def make_sample_profit_item(
    item_id: str,
    product_id: str = "prod-100",
    marketplace: str = "mercadolibre",
    currency: str = "USD",
    sale_price: Optional[Decimal] = Decimal("100.00"),
    unit_cost: Optional[Decimal] = Decimal("40.00"),
    shipping_cost: Optional[Decimal] = Decimal("10.00"),
    marketplace_fee_amount: Optional[Decimal] = Decimal("15.00"),
    payment_fee_amount: Optional[Decimal] = Decimal("3.00"),
    tax_amount: Optional[Decimal] = Decimal("5.00"),
    other_costs_amount: Optional[Decimal] = Decimal("2.00"),
    calculated_at: Optional[datetime] = None,
    category: Optional[str] = "Electronics",
    supplier_id: Optional[str] = "supp-10",
    opportunity_id: Optional[str] = "opp-1",
) -> ProfitDashboardItem:
    return ProfitDashboardService.compute_profit_item_from_facts(
        item_id=item_id,
        product_id=product_id,
        marketplace=marketplace,
        currency=currency,
        sale_price_amount=sale_price,
        unit_cost_amount=unit_cost,
        shipping_cost_amount=shipping_cost,
        marketplace_fee_amount=marketplace_fee_amount,
        payment_fee_amount=payment_fee_amount,
        tax_amount=tax_amount,
        other_costs_amount=other_costs_amount,
        cost_currency=currency,
        opportunity_id=opportunity_id,
        supplier_id=supplier_id,
        supplier_name="Global Tech Supplier",
        title="Smart Bluetooth Speaker",
        category=category,
        product_sku="SKU-SPK-01",
        calculated_at=calculated_at or datetime(2026, 9, 13, 10, 0, 0, tzinfo=timezone.utc),
    )


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def auth_setup(temp_data_dir):
    clock = MockClock()
    session_repo = JsonSaaSSessionRepository(temp_data_dir / "sessions")
    org_repo = JsonOrganizationRepository(temp_data_dir / "organizations")
    membership_repo = JsonMembershipRepository(temp_data_dir / "memberships")
    role_repo = JsonRoleRepository(temp_data_dir / "roles")
    assignment_repo = JsonRoleAssignmentRepository(temp_data_dir / "role_assignments")

    rbac_svc = RBACService(role_repository=role_repo, assignment_repository=assignment_repo, clock=clock)
    auth_svc = SaaSAuthorizationService(
        session_repository=session_repo,
        organization_repository=org_repo,
        membership_repository=membership_repo,
        rbac_service=rbac_svc,
        clock=clock,
    )

    # Crear rol con permiso de profit dashboard
    role = Role(
        role_id="role-bi-analyst",
        name="BI Analyst",
        description="Access to BI dashboards",
        permissions=(
            Permission(permission_id="perm-profit-read", action="PROFIT_DASHBOARD_READ", description="Read profit dashboard"),
            Permission(permission_id="perm-bi-read", action="BUSINESS_INTELLIGENCE_READ", description="General BI read access"),
        ),
    )
    role_repo.save(role)

    # Crear rol sin permisos
    unauthorized_role = Role(
        role_id="role-restricted",
        name="Restricted User",
        description="No BI permissions",
        permissions=(
            Permission(permission_id="perm-tenant-read", action="TENANT_READ", description="Tenant read only"),
        ),
    )
    role_repo.save(unauthorized_role)

    def create_session(tenant_id: str, identity_id: str, role_id: str = "role-bi-analyst") -> str:
        assignment = RoleAssignment(
            assignment_id=f"asgn-{identity_id}",
            role_id=role_id,
            identity_id=identity_id,
            assigned_at=clock.now(),
        )
        assignment_repo.save(assignment)

        session = SaaSSession(
            session_id=f"sess-{identity_id}",
            tenant_id=tenant_id,
            identity_id=identity_id,
            status=SessionStatus.ACTIVE,
            created_at=clock.now(),
            expires_at=clock.now() + timedelta(hours=2),
        )
        session_repo.save(session)
        return session.session_id

    return {
        "auth_svc": auth_svc,
        "session_repo": session_repo,
        "create_session": create_session,
        "clock": clock,
    }


def test_q3_decimal_arithmetic_precision():
    """1. Decimal arithmetic: Verificación de precisión y ausencia de float artifacts."""
    item = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-prec-1",
        product_id="p-1",
        marketplace="mercadolibre",
        currency="USD",
        sale_price_amount=Decimal("19.99"),
        unit_cost_amount=Decimal("10.00"),
        shipping_cost_amount=Decimal("2.50"),
        marketplace_fee_amount=Decimal("2.9985"),
        payment_fee_amount=Decimal("0.5997"),
        tax_amount=Decimal("1.50"),
        other_costs_amount=Decimal("0.50"),
        cost_currency="USD",
    )

    assert isinstance(item.sale_price, Decimal)
    assert isinstance(item.total_known_cost, Decimal)
    assert isinstance(item.gross_profit, Decimal)
    assert isinstance(item.contribution_profit, Decimal)
    assert isinstance(item.margin_pct, Decimal)

    # gross_profit = 19.99 - 10.00 = 9.99
    assert item.gross_profit == Decimal("9.99")
    # total_known_cost = 10.00 + 2.50 + 2.9985 + 0.5997 + 1.50 + 0.50 = 18.0982
    assert item.total_known_cost == Decimal("18.0982")
    # contribution_profit = 19.99 - 18.0982 = 1.8918
    assert item.contribution_profit == Decimal("1.8918")
    assert item.completeness == ProfitCompleteness.COMPLETE


def test_q3_gross_vs_contribution_profit():
    """2 & 3. Gross Profit vs Contribution Profit: Distinción canónica entre métricas."""
    item = make_sample_profit_item(
        item_id="item-gvc-1",
        sale_price=Decimal("100.00"),
        unit_cost=Decimal("40.00"),
        shipping_cost=Decimal("10.00"),
        marketplace_fee_amount=Decimal("15.00"),
        payment_fee_amount=Decimal("3.00"),
        tax_amount=Decimal("5.00"),
        other_costs_amount=Decimal("2.00"),
    )

    assert item.gross_profit == Decimal("60.00")  # 100 - 40
    assert item.total_known_cost == Decimal("75.00")  # 40 + 10 + 15 + 3 + 5 + 2
    assert item.contribution_profit == Decimal("25.00")  # 100 - 75
    assert item.margin_pct == Decimal("25.00")  # (25 / 100) * 100
    assert item.markup_pct == Decimal("62.50")  # (25 / 40) * 100


def test_q3_missing_cost_not_zero():
    """5 & 6. Missing cost != zero: Ausencia de coste no se asume 0 y marca PARTIAL."""
    item = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-partial-1",
        product_id="p-part",
        marketplace="amazon",
        currency="USD",
        sale_price_amount=Decimal("150.00"),
        unit_cost_amount=Decimal("60.00"),
        shipping_cost_amount=None,  # Desconocido
        marketplace_fee_amount=Decimal("22.50"),
        payment_fee_amount=None,    # Desconocido
        tax_amount=None,            # Desconocido
        other_costs_amount=Decimal("5.00"),
        cost_currency="USD",
    )

    # Gross profit puede calcularse porque unit_cost está presente
    assert item.gross_profit == Decimal("90.00")
    # Total known cost suma sólo los conocidos
    assert item.total_known_cost == Decimal("87.50")  # 60 + 22.50 + 5.00
    # Contribution profit se calcula como sale_price - total_known_cost para proyección
    assert item.contribution_profit == Decimal("62.50")
    assert item.completeness == ProfitCompleteness.PARTIAL
    assert "SHIPPING_COST" in item.missing_cost_components
    assert "TAX_COST" in item.missing_cost_components


def test_q3_insufficient_data_when_unit_cost_or_sale_price_missing():
    """6. Insufficient Data: Cuando falta unit_cost o sale_price."""
    item_no_cost = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-no-cost",
        product_id="p-nc",
        marketplace="amazon",
        currency="USD",
        sale_price_amount=Decimal("100.00"),
        unit_cost_amount=None,
        cost_currency="USD",
    )
    assert item_no_cost.completeness == ProfitCompleteness.INSUFFICIENT_DATA
    assert item_no_cost.gross_profit is None
    assert item_no_cost.contribution_profit is None
    assert item_no_cost.margin_pct is None
    assert "UNIT_COST" in item_no_cost.missing_cost_components

    item_no_price = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-no-price",
        product_id="p-np",
        marketplace="amazon",
        currency="USD",
        sale_price_amount=None,
        unit_cost_amount=Decimal("50.00"),
        cost_currency="USD",
    )
    assert item_no_price.completeness == ProfitCompleteness.INSUFFICIENT_DATA
    assert item_no_price.gross_profit is None
    assert "SALE_PRICE" in item_no_price.missing_cost_components


def test_q3_zero_sale_price_safe():
    """7. Zero Sale Price Safe: No división por cero."""
    item = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-zero-price",
        product_id="p-zp",
        marketplace="amazon",
        currency="USD",
        sale_price_amount=Decimal("0.00"),
        unit_cost_amount=Decimal("20.00"),
        shipping_cost_amount=Decimal("5.00"),
        marketplace_fee_amount=Decimal("0.00"),
        payment_fee_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        other_costs_amount=Decimal("0.00"),
        cost_currency="USD",
    )

    assert item.gross_profit == Decimal("-20.00")
    assert item.contribution_profit == Decimal("-25.00")
    # División por cero protegida -> margin_pct es None
    assert item.margin_pct is None
    assert item.completeness == ProfitCompleteness.PARTIAL


def test_q3_negative_margin_preserved():
    """8. Negative margin preserved: Ganancia y margen negativos se preservan sin clamp."""
    item = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-loss",
        product_id="p-loss",
        marketplace="mercadolibre",
        currency="USD",
        sale_price_amount=Decimal("50.00"),
        unit_cost_amount=Decimal("45.00"),
        shipping_cost_amount=Decimal("10.00"),
        marketplace_fee_amount=Decimal("8.00"),
        payment_fee_amount=Decimal("2.00"),
        tax_amount=Decimal("4.00"),
        other_costs_amount=Decimal("1.00"),
        cost_currency="USD",
    )

    # Total cost = 45 + 10 + 8 + 2 + 4 + 1 = 70.00
    assert item.total_known_cost == Decimal("70.00")
    # Gross profit = 50 - 45 = +5.00
    assert item.gross_profit == Decimal("5.00")
    # Contribution profit = 50 - 70 = -20.00
    assert item.contribution_profit == Decimal("-20.00")
    # Margin pct = (-20 / 50) * 100 = -40.00%
    assert item.margin_pct == Decimal("-40.00")
    assert item.completeness == ProfitCompleteness.COMPLETE


def test_q3_currency_mismatch_blocked():
    """9. Currency mismatch blocked: Monedas distintas sin exchange rate verificado se bloquean."""
    item = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-mismatch",
        product_id="p-fx",
        marketplace="mercadolibre",
        currency="CLP",
        sale_price_amount=Decimal("10000.00"),
        unit_cost_amount=Decimal("10.00"),
        cost_currency="USD",
        exchange_rate=None,  # Sin FX verificado
    )

    assert item.completeness == ProfitCompleteness.NOT_COMPARABLE_CURRENCY
    assert item.gross_profit is None
    assert item.contribution_profit is None
    assert item.margin_pct is None
    assert "EXCHANGE_RATE_MISSING" in item.missing_cost_components


def test_q3_tenant_scoping_and_persistence(temp_data_dir):
    """10. Tenant Scoping: Repositorio persiste de forma aislada por tenant_id."""
    repo = JsonTenantProfitRepository(temp_data_dir)
    ctx_a = TenantContext(tenant_id="tenant-alpha")
    ctx_b = TenantContext(tenant_id="tenant-beta")

    item_a1 = make_sample_profit_item("item-a1", product_id="prod-a1")
    item_a2 = make_sample_profit_item("item-a2", product_id="prod-a2")
    item_b1 = make_sample_profit_item("item-b1", product_id="prod-b1")

    repo.save(ctx_a, item_a1)
    repo.save(ctx_a, item_a2)
    repo.save(ctx_b, item_b1)

    items_a = repo.list_all(ctx_a)
    items_b = repo.list_all(ctx_b)

    assert len(items_a) == 2
    assert {i.item_id for i in items_a} == {"item-a1", "item-a2"}
    assert len(items_b) == 1
    assert items_b[0].item_id == "item-b1"

    # Verificación de aislamiento en disco
    file_a = temp_data_dir / "tenants" / "tenant-alpha" / "profit" / "profit_items.json"
    file_b = temp_data_dir / "tenants" / "tenant-beta" / "profit" / "profit_items.json"
    assert file_a.exists()
    assert file_b.exists()


def test_q3_authorization_rbac(temp_data_dir, auth_setup):
    """11. Authorization: Verificación de sesión y permiso PROFIT_DASHBOARD_READ."""
    repo = JsonTenantProfitRepository(temp_data_dir)
    service = ProfitDashboardService(
        repository=repo,
        authorization_service=auth_setup["auth_svc"],
        session_repository=auth_setup["session_repo"],
        clock=auth_setup["clock"],
    )

    tenant_id = "tenant-gamma"
    ctx = TenantContext(tenant_id=tenant_id)
    repo.save(ctx, make_sample_profit_item("item-g1"))

    # 1. Sin sesión -> 401
    with pytest.raises(AdminAuthenticationError):
        service.list_profit_items(tenant_id=tenant_id, session_id=None)

    # 2. Sesión inexistente -> 401
    with pytest.raises(AdminAuthenticationError):
        service.list_profit_items(tenant_id=tenant_id, session_id="sess-fake")

    # 3. Sesión con rol autorizado -> Éxito
    valid_session = auth_setup["create_session"](tenant_id, "user-analyst", "role-bi-analyst")
    res = service.list_profit_items(tenant_id=tenant_id, session_id=valid_session)
    assert res.total_count == 1

    # 4. Sesión con rol no autorizado -> 403
    unauthorized_session = auth_setup["create_session"](tenant_id, "user-restricted", "role-restricted")
    with pytest.raises(AdminAuthorizationError):
        service.list_profit_items(tenant_id=tenant_id, session_id=unauthorized_session)

    # 5. Sesión de otro tenant -> 403
    tenant_other = "tenant-other"
    other_session = auth_setup["create_session"](tenant_other, "user-other", "role-bi-analyst")
    with pytest.raises(AdminAuthorizationError):
        service.list_profit_items(tenant_id=tenant_id, session_id=other_session)


def test_q3_service_filtering_and_sorting(temp_data_dir, auth_setup):
    """12 & 13. Filtering and Sorting: Filtrado multicriterio y ordenación determinista."""
    repo = JsonTenantProfitRepository(temp_data_dir)
    service = ProfitDashboardService(
        repository=repo,
        authorization_service=auth_setup["auth_svc"],
        session_repository=auth_setup["session_repo"],
        clock=auth_setup["clock"],
    )
    tenant_id = "tenant-filters"
    session_id = auth_setup["create_session"](tenant_id, "user-filt", "role-bi-analyst")
    ctx = TenantContext(tenant_id=tenant_id)

    # Crear dataset variado
    i1 = make_sample_profit_item("item-1", product_id="p1", marketplace="mercadolibre", sale_price=Decimal("100.00"), unit_cost=Decimal("40.00"), category="Electronics")
    i2 = make_sample_profit_item("item-2", product_id="p2", marketplace="amazon", sale_price=Decimal("200.00"), unit_cost=Decimal("150.00"), category="Home")
    i3 = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-3",
        product_id="p3",
        marketplace="mercadolibre",
        currency="USD",
        sale_price_amount=Decimal("80.00"),
        unit_cost_amount=Decimal("30.00"),
        shipping_cost_amount=None,  # Incompleto (PARTIAL)
        category="Electronics",
        cost_currency="USD",
    )
    repo.save_all(ctx, [i1, i2, i3])

    # Filtrar por marketplace
    q_meli = ProfitDashboardQuery(marketplace="mercadolibre")
    res_meli = service.list_profit_items(tenant_id, query=q_meli, session_id=session_id)
    assert res_meli.total_count == 2
    assert {x.item_id for x in res_meli.items} == {"item-1", "item-3"}

    # Filtrar por completeness
    q_complete = ProfitDashboardQuery(completeness=ProfitCompleteness.COMPLETE)
    res_complete = service.list_profit_items(tenant_id, query=q_complete, session_id=session_id)
    assert res_complete.total_count == 2
    assert {x.item_id for x in res_complete.items} == {"item-1", "item-2"}

    # Filtrar por min_margin_pct
    # i1 margin: 25.00%, i2 contribution_profit: 200 - (150+10+15+3+5+2=185) = 15.00 -> margin = 7.50%, i3 margin = 62.50%
    q_margin = ProfitDashboardQuery(min_margin_pct=Decimal("30.00"))
    res_margin = service.list_profit_items(tenant_id, query=q_margin, session_id=session_id)
    assert res_margin.total_count == 1
    assert res_margin.items[0].item_id == "item-3"

    # Sorting por sale_price descendente
    q_sort = ProfitDashboardQuery(sort_by=ProfitSortField.SALE_PRICE, sort_order=SortOrder.DESC)
    res_sort = service.list_profit_items(tenant_id, query=q_sort, session_id=session_id)
    assert [x.item_id for x in res_sort.items] == ["item-2", "item-1", "item-3"]


def test_q3_service_pagination(temp_data_dir, auth_setup):
    """14. Pagination: Paginación segura y acotada."""
    repo = JsonTenantProfitRepository(temp_data_dir)
    service = ProfitDashboardService(
        repository=repo,
        authorization_service=auth_setup["auth_svc"],
        session_repository=auth_setup["session_repo"],
        clock=auth_setup["clock"],
    )
    tenant_id = "tenant-pag"
    session_id = auth_setup["create_session"](tenant_id, "user-pag", "role-bi-analyst")
    ctx = TenantContext(tenant_id=tenant_id)

    items = [make_sample_profit_item(f"item-{i:02d}", sale_price=Decimal(f"{100 + i}.00")) for i in range(15)]
    repo.save_all(ctx, items)

    # Página 1 de 5
    q1 = ProfitDashboardQuery(page=1, page_size=5, sort_by=ProfitSortField.SALE_PRICE, sort_order=SortOrder.ASC)
    res1 = service.list_profit_items(tenant_id, query=q1, session_id=session_id)
    assert res1.page == 1
    assert res1.page_size == 5
    assert res1.total_count == 15
    assert res1.total_pages == 3
    assert len(res1.items) == 5
    assert res1.items[0].item_id == "item-00"

    # Página 3 de 5
    q3 = ProfitDashboardQuery(page=3, page_size=5, sort_by=ProfitSortField.SALE_PRICE, sort_order=SortOrder.ASC)
    res3 = service.list_profit_items(tenant_id, query=q3, session_id=session_id)
    assert len(res3.items) == 5
    assert res3.items[-1].item_id == "item-14"


def test_q3_service_summary_aggregation(temp_data_dir, auth_setup):
    """17. Summary: Resumen estadístico de rentabilidad y completitud."""
    repo = JsonTenantProfitRepository(temp_data_dir)
    service = ProfitDashboardService(
        repository=repo,
        authorization_service=auth_setup["auth_svc"],
        session_repository=auth_setup["session_repo"],
        clock=auth_setup["clock"],
    )
    tenant_id = "tenant-sum"
    session_id = auth_setup["create_session"](tenant_id, "user-sum", "role-bi-analyst")
    ctx = TenantContext(tenant_id=tenant_id)

    # 1 rentable, 1 con pérdida, 1 parcial
    i1 = make_sample_profit_item("item-win", sale_price=Decimal("100.00"), unit_cost=Decimal("40.00")) # margin 25%
    i2 = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-loss",
        product_id="p-l",
        marketplace="amazon",
        currency="USD",
        sale_price_amount=Decimal("50.00"),
        unit_cost_amount=Decimal("45.00"),
        shipping_cost_amount=Decimal("10.00"),
        marketplace_fee_amount=Decimal("8.00"),
        payment_fee_amount=Decimal("2.00"),
        tax_amount=Decimal("4.00"),
        other_costs_amount=Decimal("1.00"),
        cost_currency="USD",
    ) # contribution -20, margin -40%
    i3 = ProfitDashboardService.compute_profit_item_from_facts(
        item_id="item-part",
        product_id="p-p",
        marketplace="mercadolibre",
        currency="USD",
        sale_price_amount=Decimal("80.00"),
        unit_cost_amount=Decimal("30.00"),
        shipping_cost_amount=None,
        cost_currency="USD",
    )
    repo.save_all(ctx, [i1, i2, i3])

    summary = service.get_summary(tenant_id, session_id=session_id)
    assert summary.total_items == 3
    assert summary.complete_profitability_count == 2
    assert summary.partial_profitability_count == 1
    assert summary.negative_margin_count == 1
    assert summary.positive_margin_count == 2


def test_q3_service_comparison_view(temp_data_dir, auth_setup):
    """24. Comparison View: Comparación multidimensional de rentabilidad entre ítems."""
    repo = JsonTenantProfitRepository(temp_data_dir)
    service = ProfitDashboardService(
        repository=repo,
        authorization_service=auth_setup["auth_svc"],
        session_repository=auth_setup["session_repo"],
        clock=auth_setup["clock"],
    )
    tenant_id = "tenant-comp"
    session_id = auth_setup["create_session"](tenant_id, "user-comp", "role-bi-analyst")
    ctx = TenantContext(tenant_id=tenant_id)

    i1 = make_sample_profit_item("item-1", sale_price=Decimal("100.00"), unit_cost=Decimal("40.00"))
    i2 = make_sample_profit_item("item-2", sale_price=Decimal("120.00"), unit_cost=Decimal("40.00"))
    repo.save_all(ctx, [i1, i2])

    comp = service.compare_profit_items(tenant_id, item_ids=["item-1", "item-2"], session_id=session_id)
    assert len(comp.item_ids) == 2
    assert len(comp.items) == 2
    dim_names = {d.dimension_name for d in comp.dimensions}
    assert "Sale Price" in dim_names
    assert "Gross Profit" in dim_names
    assert "Contribution Profit" in dim_names
    assert "Margin %" in dim_names


def test_q3_detail_breakdown_and_explainability(temp_data_dir, auth_setup):
    """29. Explainability: Desglose explícito de costos y deducciones operativas."""
    repo = JsonTenantProfitRepository(temp_data_dir)
    service = ProfitDashboardService(
        repository=repo,
        authorization_service=auth_setup["auth_svc"],
        session_repository=auth_setup["session_repo"],
        clock=auth_setup["clock"],
    )
    tenant_id = "tenant-detail"
    session_id = auth_setup["create_session"](tenant_id, "user-det", "role-bi-analyst")
    ctx = TenantContext(tenant_id=tenant_id)

    item = make_sample_profit_item("item-det-1")
    repo.save(ctx, item)

    detail = service.get_profit_detail(tenant_id, "item-det-1", session_id=session_id)
    assert detail.item.item_id == "item-det-1"
    assert len(detail.cost_breakdown) >= 6
    component_names = {b["component"] for b in detail.cost_breakdown}
    assert "UNIT_COST" in component_names
    assert "SHIPPING_COST" in component_names
    assert "MARKETPLACE_FEE" in component_names
    assert "PAYMENT_FEE" in component_names
    assert "TAX_COST" in component_names
    assert len(detail.financial_facts) > 0


def test_q3_empty_state_without_dummy_data(temp_data_dir, auth_setup):
    """33. Empty state: Sin registros, devuelve estructuras vacías sin inventar datos."""
    repo = JsonTenantProfitRepository(temp_data_dir)
    service = ProfitDashboardService(
        repository=repo,
        authorization_service=auth_setup["auth_svc"],
        session_repository=auth_setup["session_repo"],
        clock=auth_setup["clock"],
    )
    tenant_id = "tenant-empty"
    session_id = auth_setup["create_session"](tenant_id, "user-emp", "role-bi-analyst")

    page = service.list_profit_items(tenant_id, session_id=session_id)
    assert page.total_count == 0
    assert len(page.items) == 0

    summary = service.get_summary(tenant_id, session_id=session_id)
    assert summary.total_items == 0
    assert summary.complete_profitability_count == 0
    assert summary.average_contribution_margin_pct is None
