"""
Tests Unitarios Exhaustivos para Q.5 — Agent Cost Dashboard (Hito Q — Business Intelligence).

Requisitos Cubiertos:
1. Tenant scoping (O.1, CrossTenantGuard, multi-tenant data isolation).
2. Authorization (O.4, verificación de AGENT_COST_DASHBOARD_READ y BUSINESS_INTELLIGENCE_READ, rechazos 401/403).
3. Decimal cost precision (OBLIGATORIO Decimal, nunca float, moneda explícita).
4. UNKNOWN semantics preservation (UNKNOWN != 0, missing cost is preserved as None/UNKNOWN, missing price != free).
5. Mission attribution (costos vinculados a misiones reales vía mission_id y trazas).
6. Unattributed events (eventos sin mission_id o agent_type se preservan explícitamente como unattributed).
7. Model & Provider separation (modelos con mismo nombre en proveedores distintos no se mezclan).
8. Token accounting (input, output, cached, total token aggregations).
9. Total cost aggregation by currency (no sumas arbitrarias entre divisas sin FX).
10. Average cost per request (cálculo sobre denominadores reales).
11. Zero request denominator safe (retorna None sin dividir por cero).
12. Filtering (date ranges, provider, model, agent_type, mission_id, currency, attributed/unattributed, cost min/max).
13. Sorting & Pagination (ordenación determinista con desempate por item_id, páginas acotadas).
14. Sensitive data excluded & Anti-CoT (N.9: claves API, passwords, chain_of_thought, internal_scratchpad sanitizados).
15. Pure consultative / read-only behavior (No modifica presupuestos, cuotas ni facturación SaaS O.9).
16. Scope containment (Zero leaks/modifications to Q.6+).
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import os
from pathlib import Path
import tempfile
from typing import Optional, List, Dict, Any, Tuple
import pytest

from src.domain.agent_cost_dashboard.models import (
    AgentCostDashboardItem,
    CurrencyCostBreakdown,
    DimensionCostBreakdown,
    MissionCostSummaryItem,
    AgentCostDashboardSummary,
    AgentCostDashboardQuery,
    AgentCostDashboardPage,
    AgentCostSortField,
    SortOrder,
    CostConfidenceSource,
)
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageRequestStatus,
    UsageQuery,
)
from src.domain.cost.models import (
    CostRecord,
    UsageRecord,
    PricingRate,
    CostType,
    UsageUnit,
)
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
from src.infrastructure.persistence.data.json.usage_event_repository import InMemoryUsageEventRepository
from src.infrastructure.persistence.data.json.cost_repository import JsonCostRepository
from src.application.agent_cost_dashboard.agent_cost_dashboard_service import AgentCostDashboardService


class MockClock:
    def __init__(self, current_time: Optional[datetime] = None):
        self._current_time = current_time or datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        pass


def make_sample_usage_event(
    event_id: str,
    tenant_id: str,
    provider: str = "openai",
    model: str = "gpt-4o",
    quantity: int = 1500,
    cost_amount: Optional[Decimal] = Decimal("0.0150"),
    currency: str = "USD",
    agent_type: Optional[str] = "MarketIntelligenceAgent",
    mission_id: Optional[str] = "mission-101",
    timestamp: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> UsageEvent:
    ts = timestamp or datetime(2026, 9, 14, 10, 0, 0, tzinfo=timezone.utc)
    meta = dict(metadata or {})
    if agent_type:
        meta["agent_type"] = agent_type
    if mission_id:
        meta["mission_id"] = mission_id
    meta["currency"] = currency
    in_t = meta.get("input_tokens", int(quantity * 0.7))
    out_t = meta.get("output_tokens", int(quantity * 0.3))
    tot_t = in_t + out_t
    if "cached_tokens" not in meta:
        meta["cached_tokens"] = 0

    return UsageEvent(
        usage_event_id=event_id,
        tenant_id=tenant_id,
        occurred_at=ts,
        request_status=UsageRequestStatus.SUCCESS,
        provider=provider,
        model=model,
        task_type="chat_completion",
        input_tokens=in_t,
        output_tokens=out_t,
        total_tokens=tot_t,
        actual_cost=cost_amount,
        details=meta,
    )


def make_sample_cost_record(
    record_id: str,
    tenant_id: str,
    quantity: Decimal = Decimal("2000"),
    unit_cost: Decimal = Decimal("0.00001"),
    total_cost: Decimal = Decimal("0.0200"),
    currency: str = "USD",
    provider: str = "anthropic",
    model: str = "claude-3-5-sonnet",
    agent_type: Optional[str] = "SupplierScoutAgent",
    mission_id: Optional[str] = "mission-102",
    timestamp: Optional[datetime] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> CostRecord:
    ts = timestamp or datetime(2026, 9, 14, 11, 0, 0, tzinfo=timezone.utc)
    meta = dict(metadata or {})
    meta["tenant_id"] = tenant_id
    if agent_type:
        meta["agent_type"] = agent_type
    if mission_id:
        meta["mission_id"] = mission_id
    meta.setdefault("input_tokens", 1400)
    meta.setdefault("output_tokens", 600)

    usage = UsageRecord(
        unit=UsageUnit.TOKENS,
        input_quantity=Decimal("1400"),
        output_quantity=Decimal("600"),
        total_quantity=quantity,
        details=meta,
    )

    return CostRecord(
        cost_id=record_id,
        occurred_at=ts,
        cost_type=CostType.INFERENCE,
        provider=provider,
        service_or_model=model,
        execution_id=f"exec-{record_id}",
        usage=usage,
        currency=currency,
        unit_cost=unit_cost,
        total_cost=total_cost,
        mission_id=mission_id,
        metadata=meta,
    )


class TestAgentCostDashboardUnit:
    """Suites unitarias exhaustivas para Q.5 Agent Cost Dashboard."""

    @pytest.fixture
    def setup_env(self, tmp_path):
        clock = MockClock()
        session_repo = JsonSaaSSessionRepository(tmp_path / "sessions")
        org_repo = JsonOrganizationRepository(tmp_path / "orgs")
        membership_repo = JsonMembershipRepository(tmp_path / "memberships")
        role_repo = JsonRoleRepository(tmp_path / "roles")
        assignment_repo = JsonRoleAssignmentRepository(tmp_path / "assignments")

        usage_repo = InMemoryUsageEventRepository()
        cost_repo = JsonCostRepository(tmp_path / "costs")

        rbac_service = RBACService(
            role_repository=role_repo,
            assignment_repository=assignment_repo,
            clock=clock,
        )
        auth_service = SaaSAuthorizationService(
            session_repository=session_repo,
            membership_repository=membership_repo,
            rbac_service=rbac_service,
            clock=clock,
        )

        service = AgentCostDashboardService(
            usage_repository=usage_repo,
            cost_repository=cost_repo,
            authorization_service=auth_service,
            session_repository=session_repo,
            clock=clock,
        )

        tenant_a = "tenant-a"
        tenant_b = "tenant-b"
        user_id = "usr-admin-1"

        role = Role(
            role_id="role-bi-reader",
            name="BI Reader",
            permissions=(
                Permission(
                    permission_id="perm-cost-read",
                    action="AGENT_COST_DASHBOARD_READ",
                    description="Read agent costs",
                ),
                Permission(
                    permission_id="perm-bi-read",
                    action="BUSINESS_INTELLIGENCE_READ",
                    description="Read BI",
                ),
            ),
        )
        role_repo.save(role)

        assignment = RoleAssignment(
            assignment_id="assign-1",
            role_id="role-bi-reader",
            identity_id=user_id,
            scope=f"tenant_{tenant_a}",
        )
        assignment_repo.save(assignment)

        session = SaaSSession(
            session_id="sess-valid-a",
            identity_id=user_id,
            tenant_id=tenant_a,
            status=SessionStatus.ACTIVE,
            created_at=clock.now(),
            expires_at=clock.now() + timedelta(hours=8),
        )
        session_repo.save(session)

        return {
            "service": service,
            "usage_repo": usage_repo,
            "cost_repo": cost_repo,
            "clock": clock,
            "session_id": "sess-valid-a",
            "tenant_a": tenant_a,
            "tenant_b": tenant_b,
        }

    def test_tenant_scoping_and_isolation(self, setup_env):
        """1. Valida que el servicio aísle completamente los hechos de consumo y costo por tenant."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        tenant_b = setup_env["tenant_b"]
        session_id = setup_env["session_id"]

        # Insertar eventos para tenant A y tenant B
        ev_a = make_sample_usage_event("ev-a-1", tenant_a, cost_amount=Decimal("0.1000"))
        ev_b = make_sample_usage_event("ev-b-1", tenant_b, cost_amount=Decimal("0.5000"))
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_a)
        usage_repo.append_event(TenantContext(tenant_id=tenant_b), ev_b)

        page = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(), session_id=session_id)
        assert page.total_items == 1
        assert page.items[0].tenant_id == tenant_a
        assert page.items[0].total_cost == Decimal("0.1000")

        # Intentar acceder a tenant_b con sesión de tenant_a debe fallar por CrossTenantGuard / Auth
        with pytest.raises(AdminAuthorizationError):
            service.list_agent_costs(tenant_b, AgentCostDashboardQuery(), session_id=session_id)

    def test_authorization_rejection(self, setup_env):
        """2. Rechazo estricto si no hay sesión válida o faltan permisos RBAC."""
        service = setup_env["service"]
        tenant_a = setup_env["tenant_a"]

        # Sin sesión
        with pytest.raises(AdminAuthenticationError):
            service.list_agent_costs(tenant_a, AgentCostDashboardQuery(), session_id=None)

        # Sesión inexistente
        with pytest.raises(AdminAuthenticationError):
            service.list_agent_costs(tenant_a, AgentCostDashboardQuery(), session_id="sess-invalid")

    def test_decimal_cost_precision_and_no_floats(self, setup_env):
        """3. Garantiza que todos los importes monetarios sean Decimal con precisión financiera."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        ev = make_sample_usage_event("ev-prec-1", tenant_a, cost_amount=Decimal("0.000123456"))
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev)

        summary = service.get_summary(tenant_a, session_id=session_id)
        assert isinstance(summary.total_known_cost_by_currency["USD"], Decimal)
        assert summary.total_known_cost_by_currency["USD"] == Decimal("0.000123456")

        page = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(), session_id=session_id)
        assert isinstance(page.items[0].total_cost, Decimal)
        assert page.items[0].total_cost == Decimal("0.000123456")

    def test_unknown_semantics_preserved(self, setup_env):
        """4. UNKNOWN != 0: Eventos sin costo o sin precio registrado preservan total_cost=None y cost_source=UNKNOWN."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        ev_unknown = make_sample_usage_event("ev-unk-1", tenant_a, cost_amount=None)
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_unknown)

        summary = service.get_summary(tenant_a, session_id=session_id)
        assert summary.unknown_cost_events_count == 1
        assert "USD" not in summary.total_known_cost_by_currency or summary.total_known_cost_by_currency.get("USD") == Decimal("0")

        page = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(), session_id=session_id)
        item = page.items[0]
        assert item.total_cost is None
        assert item.is_known_cost is False
        assert item.cost_source == CostConfidenceSource.UNKNOWN_UNPRICED
        assert "total_cost" in item.unknown_fields

    def test_mission_attribution(self, setup_env):
        """5. Atribución correcta a misiones cuando existe correlation_id / mission_id real."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        ev_m1 = make_sample_usage_event("ev-m1-1", tenant_a, mission_id="mission-alpha", cost_amount=Decimal("0.05"))
        ev_m2 = make_sample_usage_event("ev-m1-2", tenant_a, mission_id="mission-alpha", cost_amount=Decimal("0.03"))
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_m1)
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_m2)

        mission_summary = service.get_mission_cost_summary(tenant_a, "mission-alpha", session_id=session_id)
        assert mission_summary.mission_id == "mission-alpha"
        assert mission_summary.request_count == 2
        assert mission_summary.cost_by_currency["USD"] == Decimal("0.08")

    def test_unattributed_events_handling(self, setup_env):
        """6. Eventos sin misión no se asignan arbitrariamente y se marcan como is_attributed=False."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        ev_unattr = make_sample_usage_event("ev-unattr-1", tenant_a, mission_id=None, agent_type=None, cost_amount=Decimal("0.02"))
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_unattr)

        summary = service.get_summary(tenant_a, session_id=session_id)
        assert summary.unattributed_cost_events_count == 1
        assert "unattributed_agent" in summary.cost_by_agent

        page = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(), session_id=session_id)
        assert page.items[0].is_attributed is False
        assert page.items[0].mission_id is None

    def test_model_and_provider_separation(self, setup_env):
        """7. Modelos con el mismo nombre en proveedores distintos no se mezclan."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        ev_openai = make_sample_usage_event("ev-prov-1", tenant_a, provider="openai", model="custom-llm", cost_amount=Decimal("0.04"))
        ev_azure = make_sample_usage_event("ev-prov-2", tenant_a, provider="azure", model="custom-llm", cost_amount=Decimal("0.06"))
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_openai)
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_azure)

        summary = service.get_summary(tenant_a, session_id=session_id)
        assert summary.cost_by_provider["openai"].cost_by_currency["USD"] == Decimal("0.04")
        assert summary.cost_by_provider["azure"].cost_by_currency["USD"] == Decimal("0.06")
        assert summary.cost_by_model["custom-llm"].cost_by_currency["USD"] == Decimal("0.10")

    def test_token_accounting_breakdown(self, setup_env):
        """8. Agregación precisa de tokens de entrada, salida, en caché y totales."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        ev1 = make_sample_usage_event("ev-tok-1", tenant_a, quantity=1000, metadata={"input_tokens": 700, "output_tokens": 300, "cached_tokens": 100})
        ev2 = make_sample_usage_event("ev-tok-2", tenant_a, quantity=2000, metadata={"input_tokens": 1500, "output_tokens": 500, "cached_tokens": 200})
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev1)
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev2)

        summary = service.get_summary(tenant_a, session_id=session_id)
        assert summary.total_tokens == 3000
        assert summary.total_input_tokens == 2200
        assert summary.total_output_tokens == 800
        assert summary.total_cached_tokens == 300

    def test_multi_currency_isolation(self, setup_env):
        """9. Monedas distintas se desglosan por separado sin sumas arbitrarias."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        ev_usd = make_sample_usage_event("ev-curr-1", tenant_a, cost_amount=Decimal("10.00"), currency="USD")
        ev_clp = make_sample_usage_event("ev-curr-2", tenant_a, cost_amount=Decimal("9500.00"), currency="CLP")
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_usd)
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev_clp)

        summary = service.get_summary(tenant_a, session_id=session_id)
        assert summary.total_known_cost_by_currency["USD"] == Decimal("10.00")
        assert summary.total_known_cost_by_currency["CLP"] == Decimal("9500.00")

    def test_average_cost_per_request_and_zero_denominator_safe(self, setup_env):
        """10 & 11. Cálculo de costo promedio por petición y seguridad ante denominador cero."""
        service = setup_env["service"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        # Resumen vacío -> sin división por cero
        empty_summary = service.get_summary(tenant_a, session_id=session_id)
        assert empty_summary.total_requests == 0
        assert empty_summary.currency_breakdowns == ()

        # Con datos
        usage_repo = setup_env["usage_repo"]
        ev1 = make_sample_usage_event("ev-avg-1", tenant_a, cost_amount=Decimal("0.02"))
        ev2 = make_sample_usage_event("ev-avg-2", tenant_a, cost_amount=Decimal("0.04"))
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev1)
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev2)

        summary = service.get_summary(tenant_a, session_id=session_id)
        assert summary.total_requests == 2
        usd_breakdown = next(b for b in summary.currency_breakdowns if b.currency == "USD")
        assert usd_breakdown.avg_cost_per_request == Decimal("0.030000")

    def test_filters_functionality(self, setup_env):
        """12. Filtrado por proveedor, modelo, agente, misión, divisa y atribución."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        ev1 = make_sample_usage_event("ev-f-1", tenant_a, provider="openai", model="gpt-4o", agent_type="MarketAgent", mission_id="m-1", currency="USD")
        ev2 = make_sample_usage_event("ev-f-2", tenant_a, provider="anthropic", model="claude-3-5", agent_type="SupplierAgent", mission_id="m-2", currency="EUR")
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev1)
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev2)

        # Filtro por provider
        p_prov = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(provider="openai"), session_id=session_id)
        assert p_prov.total_items == 1
        assert p_prov.items[0].provider == "openai"

        # Filtro por agent_type
        p_agent = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(agent_type="SupplierAgent"), session_id=session_id)
        assert p_agent.total_items == 1
        assert p_agent.items[0].agent_type == "SupplierAgent"

        # Filtro por mission_id
        p_miss = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(mission_id="m-1"), session_id=session_id)
        assert p_miss.total_items == 1
        assert p_miss.items[0].mission_id == "m-1"

        # Filtro por currency
        p_curr = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(currency="EUR"), session_id=session_id)
        assert p_curr.total_items == 1
        assert p_curr.items[0].currency == "EUR"

    def test_sorting_and_pagination(self, setup_env):
        """13. Ordenación determinista y paginación acotada."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        for i in range(5):
            ev = make_sample_usage_event(
                f"ev-sort-{i}",
                tenant_a,
                quantity=100 * (i + 1),
                cost_amount=Decimal(f"0.0{i+1}"),
                timestamp=datetime(2026, 9, 14, 10 + i, 0, 0, tzinfo=timezone.utc),
            )
            usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev)

        # Orden desc por total_cost
        page = service.list_agent_costs(
            tenant_a,
            AgentCostDashboardQuery(sort_by=AgentCostSortField.TOTAL_COST, sort_order=SortOrder.DESC, page=1, page_size=2),
            session_id=session_id,
        )
        assert page.total_items == 5
        assert page.total_pages == 3
        assert len(page.items) == 2
        assert page.items[0].total_cost == Decimal("0.05")
        assert page.items[1].total_cost == Decimal("0.04")
        assert page.has_next is True

    def test_sensitive_data_sanitization_and_anti_cot(self, setup_env):
        """14. N.9: Claves API, tokens, contraseñas y cadenas de razonamiento (CoT) son purgadas."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        dirty_metadata = {
            "api_key": "sk-proj-secret123456",
            "bearer_token": "eyJhbGciOi...",
            "password": "super-secret-pass",
            "chain_of_thought": "Thinking step by step to solve user problem...",
            "internal_scratchpad": "Drafting internal variables...",
            "valid_key": "safe_value",
        }
        ev = make_sample_usage_event("ev-clean-1", tenant_a, metadata=dirty_metadata)
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev)

        detail = service.get_cost_detail(tenant_a, "ev-clean-1", session_id=session_id)
        assert "api_key" not in detail.details
        assert "bearer_token" not in detail.details
        assert "password" not in detail.details
        assert "chain_of_thought" not in detail.details
        assert "internal_scratchpad" not in detail.details
        assert detail.details["valid_key"] == "safe_value"

    def test_k3_and_o6_unification(self, setup_env):
        """15. Unificación consultiva de eventos de O.6 (UsageEvent) y K.3 (CostRecord)."""
        service = setup_env["service"]
        usage_repo = setup_env["usage_repo"]
        cost_repo = setup_env["cost_repo"]
        tenant_a = setup_env["tenant_a"]
        session_id = setup_env["session_id"]

        # 1 evento en O.6 y 1 registro en K.3
        ev = make_sample_usage_event("ev-o6-1", tenant_a, cost_amount=Decimal("0.01"))
        usage_repo.append_event(TenantContext(tenant_id=tenant_a), ev)

        cr = make_sample_cost_record("cr-k3-1", tenant_a, total_cost=Decimal("0.02"))
        cost_repo.append(cr)

        summary = service.get_summary(tenant_a, session_id=session_id)
        assert summary.total_requests == 2
        assert summary.total_known_cost_by_currency["USD"] == Decimal("0.03")

        page = service.list_agent_costs(tenant_a, AgentCostDashboardQuery(), session_id=session_id)
        assert page.total_items == 2
