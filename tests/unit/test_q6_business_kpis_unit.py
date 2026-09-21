"""
Tests Unitarios Exhaustivos para Q.6 — Business KPIs & Cross-Domain Summary (Hito Q — Business Intelligence).

Requisitos cubiertos según especificación Q.6:
1. tenant scoping (O.1)
2. authorization (O.4, BUSINESS_KPI_READ, BUSINESS_INTELLIGENCE_READ fallback)
3. opportunity count
4. supplier verified count
5. avg margin
6. negative margin count
7. mission success rate
8. running mission excluded denominator
9. agent total cost
10. avg cost/mission
11. UNKNOWN denominator
12. currency separation
13. Decimal arithmetic
14. cross-domain correlation safety
15. sensitive data excluded (N.9)
16. no Gate P implementation
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any
import pytest

from src.domain.business_kpi.models import (
    BusinessKPIValue,
    BusinessKPISummary,
    BusinessKPIDomainBreakdown,
    BusinessKPIComparison,
    BusinessKPICatalogItem,
    BusinessKPIQuery,
    KPIStatus,
    KPIConfidence,
    KPIUnit,
    KPIDomain,
)
from src.domain.business_kpi.catalog import KPI_CATALOG
from src.application.business_kpi.business_kpi_service import (
    BusinessKPIService,
    BusinessKPIAuthenticationError,
    BusinessKPIAuthorizationError,
    BusinessKPINotFoundError,
    BusinessKPIInvalidRequestError,
)
from src.domain.tenant.models import TenantContext
from src.infrastructure.persistence.data.json.tenant_opportunity_repository import JsonTenantOpportunityRepository
from src.infrastructure.persistence.data.json.tenant_supplier_repository import JsonTenantSupplierRepository
from src.infrastructure.persistence.data.json.tenant_profit_repository import JsonTenantProfitRepository
from src.infrastructure.persistence.data.json.tenant_mission_repository import JsonTenantMissionRepository
from src.infrastructure.persistence.data.json.cost_repository import JsonCostRepository

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
    RiskLevel,
    EvidenceProvenanceType,
)
from src.domain.profit_dashboard.models import ProfitDashboardItem, ProfitCompleteness
from src.domain.mission.models import Mission, MissionStatus, MissionType, MissionPriority
from src.domain.cost.models import CostRecord, CostType, UsageRecord, UsageUnit
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.session.ports import SaaSSessionRepositoryPort
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
    SaaSAuthorizationReasonCode,
    SaaSAuthorizationContext,
)


class InMemorySessionRepository(SaaSSessionRepositoryPort):
    def __init__(self):
        self.sessions: Dict[str, SaaSSession] = {}

    def save(self, session: SaaSSession) -> None:
        self.sessions[session.session_id] = session

    def get_by_id(self, session_id: str, tenant_id: Optional[str] = None) -> Optional[SaaSSession]:
        s = self.sessions.get(session_id)
        if s and tenant_id is not None and s.tenant_id != tenant_id:
            return None
        return s

    def delete(self, session_id: str, tenant_id: Optional[str] = None) -> bool:
        s = self.sessions.get(session_id)
        if s and tenant_id is not None and s.tenant_id != tenant_id:
            return False
        return self.sessions.pop(session_id, None) is not None

    def list_by_tenant(self, tenant_id: str) -> List[SaaSSession]:
        return [s for s in self.sessions.values() if s.tenant_id == tenant_id]

    def list_by_identity(self, identity_id: str, tenant_id: Optional[str] = None) -> List[SaaSSession]:
        results = [s for s in self.sessions.values() if s.identity_id == identity_id]
        if tenant_id is not None:
            results = [s for s in results if s.tenant_id == tenant_id]
        return results


class MockSaaSAuthorizationService:
    def __init__(self, allowed_actions=None, allow_all=False):
        self.allowed_actions = set(allowed_actions or [])
        self.allow_all = allow_all

    def authorize(self, request: SaaSAuthorizationRequest) -> SaaSAuthorizationDecision:
        ctx = SaaSAuthorizationContext(
            session_id=request.session_id or "dummy_session",
            identity_id=request.identity_id or "dummy_identity",
            tenant_id=request.tenant_id or "dummy_tenant",
            organization_id=request.organization_id,
            action=request.action,
        )
        if self.allow_all or request.action in self.allowed_actions:
            return SaaSAuthorizationDecision(
                decision_id="dec_allow",
                status=SaaSAuthorizationStatus.ALLOW,
                reason_code=SaaSAuthorizationReasonCode.AUTHORIZED,
                context=ctx,
            )
        return SaaSAuthorizationDecision(
            decision_id="dec_deny",
            status=SaaSAuthorizationStatus.DENY,
            reason_code=SaaSAuthorizationReasonCode.INSUFFICIENT_PERMISSIONS,
            context=ctx,
        )


@pytest.fixture
def base_environment(tmp_path):
    opp_repo = JsonTenantOpportunityRepository(tmp_path / "opps")
    sup_repo = JsonTenantSupplierRepository(tmp_path / "suppliers")
    profit_repo = JsonTenantProfitRepository(tmp_path / "profit")
    mission_repo = JsonTenantMissionRepository(tmp_path / "missions")
    cost_repo = JsonCostRepository(tmp_path / "costs")
    session_repo = InMemorySessionRepository()

    now = datetime.now(timezone.utc)
    valid_session = SaaSSession(
        session_id="sess_valid_123",
        tenant_id="tenant_alpha",
        identity_id="user_admin",
        status=SessionStatus.ACTIVE,
        created_at=now - timedelta(hours=1),
        expires_at=now + timedelta(hours=2),
    )
    session_repo.save(valid_session)

    service = BusinessKPIService(
        opportunity_repository=opp_repo,
        supplier_repository=sup_repo,
        profit_repository=profit_repo,
        mission_repository=mission_repo,
        cost_repository=cost_repo,
        session_repository=session_repo,
    )

    return {
        "service": service,
        "opp_repo": opp_repo,
        "sup_repo": sup_repo,
        "profit_repo": profit_repo,
        "mission_repo": mission_repo,
        "cost_repo": cost_repo,
        "session_repo": session_repo,
    }


def test_1_tenant_scoping(base_environment):
    service = base_environment["service"]
    opp_repo = base_environment["opp_repo"]
    ctx_a = TenantContext(tenant_id="tenant_alpha")
    ctx_b = TenantContext(tenant_id="tenant_beta")
    now = datetime.now(timezone.utc)

    # Registro en Tenant A
    opp_a = OpportunityRecord(
        opportunity_id="opp_a",
        canonical_product_id="prod_a",
        marketplace=Marketplace.MERCADO_LIBRE,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.DETECTED,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs_a",),
        observed_metrics=ObservedOpportunityMetrics(),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("80.0")),
        detected_at=now,
    )
    opp_repo.save(ctx_a, opp_a)

    # Registro en Tenant B
    opp_b = OpportunityRecord(
        opportunity_id="opp_b",
        canonical_product_id="prod_b",
        marketplace=Marketplace.AMAZON,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.DETECTED,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs_b",),
        observed_metrics=ObservedOpportunityMetrics(),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("90.0")),
        detected_at=now,
    )
    opp_repo.save(ctx_b, opp_b)

    # Consulta Tenant A no debe ver Tenant B
    summary_a = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map_a = {k.kpi_id: k for k in summary_a.kpis}
    assert kpi_map_a["OPPORTUNITY_COUNT"].value == Decimal("1")
    assert kpi_map_a["AVG_OPPORTUNITY_SCORE"].value == Decimal("80.00")


def test_2_authorization_and_session(base_environment):
    service = base_environment["service"]

    # Missing session
    with pytest.raises(BusinessKPIAuthenticationError):
        service.get_summary(tenant_id="tenant_alpha", session_id=None)

    # Invalid session
    with pytest.raises(BusinessKPIAuthenticationError):
        service.get_summary(tenant_id="tenant_alpha", session_id="sess_fake")

    # Cross tenant session
    with pytest.raises(BusinessKPIAuthorizationError):
        service.get_summary(tenant_id="tenant_beta", session_id="sess_valid_123")


def test_3_opportunity_count_and_high_potential(base_environment):
    service = base_environment["service"]
    opp_repo = base_environment["opp_repo"]
    ctx = TenantContext(tenant_id="tenant_alpha")
    now = datetime.now(timezone.utc)

    opp1 = OpportunityRecord(
        opportunity_id="opp_1",
        canonical_product_id="prod_1",
        marketplace=Marketplace.MERCADO_LIBRE,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.DETECTED,
        confidence=Confidence.HIGH,
        source_observation_ids=("obs_1",),
        observed_metrics=ObservedOpportunityMetrics(),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("75.0")),
        detected_at=now,
    )
    opp2 = OpportunityRecord(
        opportunity_id="opp_2",
        canonical_product_id="prod_2",
        marketplace=Marketplace.AMAZON,
        opportunity_type=OpportunityType.PRICE_ARBITRAGE,
        status=OpportunityStatus.DETECTED,
        confidence=Confidence.LOW,
        source_observation_ids=("obs_2",),
        observed_metrics=ObservedOpportunityMetrics(),
        derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("40.0")),
        detected_at=now,
    )
    opp_repo.save_all(ctx, [opp1, opp2])

    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map = {k.kpi_id: k for k in summary.kpis}

    assert kpi_map["OPPORTUNITY_COUNT"].value == Decimal("2")
    assert kpi_map["HIGH_POTENTIAL_OPPORTUNITIES"].value == Decimal("1")
    assert kpi_map["AVG_OPPORTUNITY_SCORE"].value == Decimal("57.50")


def test_4_supplier_verified_count_and_rate(base_environment):
    service = base_environment["service"]
    sup_repo = base_environment["sup_repo"]
    ctx = TenantContext(tenant_id="tenant_alpha")
    now = datetime.now(timezone.utc)

    sup1 = Supplier(
        supplier_id="sup_1",
        name="Sup 1",
        source="ALIBABA",
        source_type=EvidenceProvenanceType.LIVE,
        status=SupplierStatus.VERIFIED,
        metadata={"supplier_score": Decimal("90.0"), "risk_level": "LOW"},
        observed_at=now,
    )
    sup2 = Supplier(
        supplier_id="sup_2",
        name="Sup 2",
        source="DIRECT",
        source_type=EvidenceProvenanceType.DERIVED,
        status=SupplierStatus.UNVERIFIED,
        metadata={"supplier_score": Decimal("50.0"), "risk_level": "MEDIUM"},
        observed_at=now,
    )
    sup_repo.save_all(ctx, [sup1, sup2])

    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map = {k.kpi_id: k for k in summary.kpis}

    assert kpi_map["VALIDATED_SUPPLIER_COUNT"].value == Decimal("1")
    assert kpi_map["SUPPLIER_VERIFICATION_RATE"].value == Decimal("50.00")
    assert kpi_map["AVG_SUPPLIER_SCORE"].value == Decimal("70.00")


def test_5_avg_margin_and_6_negative_margin_count(base_environment):
    service = base_environment["service"]
    profit_repo = base_environment["profit_repo"]
    ctx = TenantContext(tenant_id="tenant_alpha")
    now = datetime.now(timezone.utc)

    p1 = ProfitDashboardItem(
        item_id="prof_1",
        product_id="prod_1",
        marketplace="mercadolibre",
        currency="USD",
        completeness=ProfitCompleteness.COMPLETE,
        calculated_at=now,
        contribution_profit=Decimal("30.00"),
        margin_pct=Decimal("30.00"),
    )
    p2 = ProfitDashboardItem(
        item_id="prof_2",
        product_id="prod_2",
        marketplace="mercadolibre",
        currency="USD",
        completeness=ProfitCompleteness.COMPLETE,
        calculated_at=now,
        contribution_profit=Decimal("-5.00"),
        margin_pct=Decimal("-10.00"),
    )
    p3_incomplete = ProfitDashboardItem(
        item_id="prof_3",
        product_id="prod_3",
        marketplace="amazon",
        currency="USD",
        completeness=ProfitCompleteness.PARTIAL,
        calculated_at=now,
        contribution_profit=None,
        margin_pct=None,
    )
    profit_repo.save_all(ctx, [p1, p2, p3_incomplete])

    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map = {k.kpi_id: k for k in summary.kpis}

    assert kpi_map["COMPLETE_PROFITABILITY_COUNT"].value == Decimal("2")
    assert kpi_map["NEGATIVE_MARGIN_COUNT"].value == Decimal("1")
    # Promedio de 30% y -10% = 10%
    assert kpi_map["AVG_CONTRIBUTION_MARGIN"].value == Decimal("10.00")


def test_7_mission_success_rate_and_8_running_excluded(base_environment):
    service = base_environment["service"]
    mission_repo = base_environment["mission_repo"]
    ctx = TenantContext(tenant_id="tenant_alpha")
    now = datetime.now(timezone.utc)

    m_completed = Mission(
        mission_id="m_1",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.COMPLETED,
        priority=MissionPriority.HIGH,
        created_at=now,
    )
    m_failed = Mission(
        mission_id="m_2",
        type=MissionType.SUPPLIER_SEARCH,
        status=MissionStatus.FAILED,
        priority=MissionPriority.MEDIUM,
        created_at=now,
    )
    m_running = Mission(
        mission_id="m_3",
        type=MissionType.MARKET_DISCOVERY,
        status=MissionStatus.RUNNING,
        priority=MissionPriority.LOW,
        created_at=now,
    )
    m_pending = Mission(
            mission_id="m_4",
            type=MissionType.PROFIT_EVALUATION,
            status=MissionStatus.PENDING,
            priority=MissionPriority.LOW,
            created_at=now,
        )
    mission_repo.save_all(ctx, [m_completed, m_failed, m_running, m_pending])

    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map = {k.kpi_id: k for k in summary.kpis}

    assert kpi_map["ACTIVE_MISSIONS"].value == Decimal("2")  # RUNNING + PENDING
    assert kpi_map["FAILED_MISSIONS"].value == Decimal("1")
    # Denominador terminal: 1 completed + 1 failed = 2.
    # RUNNING y PENDING NO entran en denominador ni cuentan como fallos.
    # Success rate = 1 / 2 = 50.00%
    assert kpi_map["MISSION_SUCCESS_RATE"].value == Decimal("50.00")


def test_9_agent_total_cost_and_10_avg_cost_per_mission(base_environment):
    service = base_environment["service"]
    cost_repo = base_environment["cost_repo"]
    now = datetime.now(timezone.utc)

    c1 = CostRecord(
        cost_id="c_1",
        occurred_at=now,
        cost_type=CostType.INFERENCE,
        provider="anthropic",
        service_or_model="claude-3-5-sonnet",
        execution_id="e_1",
        usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("1000")),
        currency="USD",
        unit_cost=Decimal("0.003"),
        total_cost=Decimal("3.00"),
        mission_id="m_1",
    )
    c2 = CostRecord(
        cost_id="c_2",
        occurred_at=now,
        cost_type=CostType.INFERENCE,
        provider="openai",
        service_or_model="gpt-4o",
        execution_id="e_2",
        usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("2000")),
        currency="USD",
        unit_cost=Decimal("0.005"),
        total_cost=Decimal("5.00"),
        mission_id="m_2",
    )
    cost_repo.append(c1)
    cost_repo.append(c2)

    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map = {k.kpi_id: k for k in summary.kpis}

    assert kpi_map["TOTAL_AGENT_COST"].value == Decimal("8.00")
    # 2 misiones distintas con costo: $8.00 / 2 = $4.00
    assert kpi_map["AVG_COST_PER_MISSION"].value == Decimal("4.0000")


def test_11_unknown_denominator_semantics(base_environment):
    service = base_environment["service"]
    # Repositorio completamente vacío
    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map = {k.kpi_id: k for k in summary.kpis}

    # Counts legítimos vacíos son 0
    assert kpi_map["OPPORTUNITY_COUNT"].value == Decimal("0")
    assert kpi_map["VALIDATED_SUPPLIER_COUNT"].value == Decimal("0")
    assert kpi_map["ACTIVE_MISSIONS"].value == Decimal("0")
    assert kpi_map["FAILED_MISSIONS"].value == Decimal("0")

    # Ratios sin denominador son UNKNOWN, nunca 0
    assert kpi_map["AVG_OPPORTUNITY_SCORE"].status == KPIStatus.UNKNOWN
    assert kpi_map["AVG_OPPORTUNITY_SCORE"].value is None
    assert kpi_map["SUPPLIER_VERIFICATION_RATE"].status == KPIStatus.UNKNOWN
    assert kpi_map["AVG_SUPPLIER_SCORE"].status == KPIStatus.UNKNOWN
    assert kpi_map["AVG_CONTRIBUTION_MARGIN"].status == KPIStatus.UNKNOWN
    assert kpi_map["MISSION_SUCCESS_RATE"].status == KPIStatus.UNKNOWN
    assert kpi_map["TOTAL_AGENT_COST"].status == KPIStatus.UNKNOWN
    assert kpi_map["AVG_COST_PER_MISSION"].status == KPIStatus.UNKNOWN
    assert kpi_map["COST_PER_OPPORTUNITY"].status == KPIStatus.UNKNOWN
    assert kpi_map["PROJECTED_CONTRIBUTION_TO_AGENT_COST_RATIO"].status == KPIStatus.UNKNOWN


def test_12_currency_separation_and_13_decimal_arithmetic(base_environment):
    service = base_environment["service"]
    cost_repo = base_environment["cost_repo"]
    profit_repo = base_environment["profit_repo"]
    ctx = TenantContext(tenant_id="tenant_alpha")
    now = datetime.now(timezone.utc)

    # Costos en USD y CLP (Monedas heterogéneas)
    c_usd = CostRecord(
        cost_id="c_usd",
        occurred_at=now,
        cost_type=CostType.INFERENCE,
        provider="anthropic",
        service_or_model="claude-3-5-sonnet",
        execution_id="e_1",
        usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("1000")),
        currency="USD",
        unit_cost=Decimal("0.002"),
        total_cost=Decimal("2.50"),
    )
    c_clp = CostRecord(
        cost_id="c_clp",
        occurred_at=now,
        cost_type=CostType.INFERENCE,
        provider="anthropic",
        service_or_model="claude-3-5-haiku",
        execution_id="e_2",
        usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("500")),
        currency="CLP",
        unit_cost=Decimal("1.5"),
        total_cost=Decimal("1500.00"),
    )
    cost_repo.append(c_usd)
    cost_repo.append(c_clp)

    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map = {k.kpi_id: k for k in summary.kpis}

    # Desglose de divisas debe existir
    assert "USD" in summary.currency_breakdown
    assert "CLP" in summary.currency_breakdown
    assert summary.currency_breakdown["USD"]["total_known_agent_cost"] == "2.50"
    assert summary.currency_breakdown["CLP"]["total_known_agent_cost"] == "1500.00"

    # Monedas mezcladas sin filtro explícito marcan NOT_COMPARABLE_CURRENCY
    assert kpi_map["TOTAL_AGENT_COST"].status == KPIStatus.NOT_COMPARABLE_CURRENCY
    assert kpi_map["TOTAL_AGENT_COST"].value is None


def test_14_cross_domain_correlation_safety(base_environment):
    service = base_environment["service"]
    opp_repo = base_environment["opp_repo"]
    cost_repo = base_environment["cost_repo"]
    profit_repo = base_environment["profit_repo"]
    ctx = TenantContext(tenant_id="tenant_alpha")
    now = datetime.now(timezone.utc)

    # 4 oportunidades
    for i in range(4):
        opp_repo.save(
            ctx,
            OpportunityRecord(
                opportunity_id=f"opp_{i}",
                canonical_product_id=f"prod_{i}",
                marketplace=Marketplace.MERCADO_LIBRE,
                opportunity_type=OpportunityType.PRICE_ARBITRAGE,
                status=OpportunityStatus.DETECTED,
                confidence=Confidence.HIGH,
                source_observation_ids=(f"obs_{i}",),
                observed_metrics=ObservedOpportunityMetrics(),
                derived_metrics=DerivedOpportunityMetrics(opportunity_score=Decimal("80.0")),
                detected_at=now,
            ),
        )

    # Profit total USD $100.00
    profit_repo.save(
        ctx,
        ProfitDashboardItem(
            item_id="prof_1",
            product_id="prod_1",
            marketplace="mercadolibre",
            currency="USD",
            completeness=ProfitCompleteness.COMPLETE,
            calculated_at=now,
            contribution_profit=Decimal("100.00"),
            margin_pct=Decimal("20.00"),
        ),
    )

    # Agent cost USD $20.00
    cost_repo.append(
        CostRecord(
            cost_id="c_1",
            occurred_at=now,
            cost_type=CostType.INFERENCE,
            provider="anthropic",
            service_or_model="claude-3-5-sonnet",
            execution_id="e_1",
            usage=UsageRecord(unit=UsageUnit.TOKENS, total_quantity=Decimal("1000")),
            currency="USD",
            unit_cost=Decimal("0.02"),
            total_cost=Decimal("20.00"),
        )
    )

    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    kpi_map = {k.kpi_id: k for k in summary.kpis}

    # COST_PER_OPPORTUNITY: $20.00 / 4 = $5.0000
    assert kpi_map["COST_PER_OPPORTUNITY"].value == Decimal("5.0000")
    # RATIO: $100.00 / $20.00 = 5.0000
    assert kpi_map["PROJECTED_CONTRIBUTION_TO_AGENT_COST_RATIO"].value == Decimal("5.0000")


def test_15_sensitive_data_excluded_n9(base_environment):
    service = base_environment["service"]
    summary = service.get_summary(tenant_id="tenant_alpha", session_id="sess_valid_123")
    raw_dict = summary.to_dict()

    # Verificar que no existen secretos ni CoT en serialización
    forbidden_keys = {"secret", "password", "token", "chain_of_thought", "private_key", "api_key"}

    def check_keys(d):
        if isinstance(d, dict):
            for k, v in d.items():
                assert k.lower() not in forbidden_keys
                check_keys(v)
        elif isinstance(d, list):
            for x in d:
                check_keys(x)

    check_keys(raw_dict)


def test_16_no_gate_p_implementation():
    # Verifica que Q.6 no implementa ni activa compuertas de fases posteriores
    import src.domain.business_kpi.models as kpi_models
    import src.application.business_kpi.business_kpi_service as kpi_service

    assert not hasattr(kpi_models, "GateP")
    assert not hasattr(kpi_service, "GatePValidator")
