"""
Tests de Integración y E2E para Plans & Pricing Tiers SaaS (Hito O.8 — Plans & Pricing Tiers).

Escenarios Obligatorios:
A. Tenant A FREE -> expected features/quotas.
B. Tenant B PRO -> independent entitlements.
C. A cannot use PRO-only feature -> zero downstream provider calls.
D. PRO feature + missing RBAC -> denied.
E. RBAC allow + plan deny -> denied.
F. Plan upgrade -> new entitlement active.
G. Downgrade -> usage O.6 preserved.
H. O.7 uses quota derived from active plan.
I. Restart -> assignments and catalog preserved across repository reload.
J. Tampered assignment -> fail-safe (blocked / DENY).
K. E2E Pipeline: Tenant -> Session -> O.4 Authorization -> O.8 Plan Entitlement -> O.7 Quota -> O.5 Gateway -> Provider Mock.
   Demostrar:
   1. entitled + authorized + under quota -> provider call = 1.
   2. feature absent in plan -> provider call = 0.
   3. quota exceeded -> provider call = 0.
   4. Tenant B plan does not affect Tenant A.
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional, List, Dict, Any, Union
import pytest

# Domain Imports
from src.domain.tenant.models import TenantContext, CrossTenantAccessError
from src.domain.identity.models import IdentityReference, IdentityType
from src.domain.session.models import SaaSSession, SessionStatus
from src.domain.organization.models import (
    Organization,
    UserMembership,
    MembershipStatus,
    MembershipRole,
    OrganizationStatus,
)
from src.domain.rbac.models import Permission, Role, RoleAssignment, PermissionStatus, RoleStatus
from src.domain.saas_authorization.models import (
    SaaSAuthorizationRequest,
    SaaSAuthorizationDecision,
    SaaSAuthorizationStatus,
)
from src.domain.model_routing.models import (
    ModelRoute,
    RoutingRequest,
    RoutingDecision,
    RoutingDecisionStatus,
    RouteCapability,
    TaskCriticality,
    QualityRequirement,
    LatencyRequirement,
    RouteStatus,
)
from src.domain.model_gateway.models import (
    ModelGatewayRequest,
    ModelGatewayResponse,
    ModelGatewayStatus,
    ProviderRequestReference,
    ProviderErrorType,
    TenantModelConfig,
)
from src.domain.usage_metering.models import (
    UsageEvent,
    UsageQuery,
    UsagePeriod,
    UsagePeriodType,
    UsageRequestStatus,
)
from src.domain.quota_management.models import (
    QuotaPolicy,
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
    QuotaStatus,
    QuotaRequest,
    QuotaDecision,
    QuotaReservation,
    QuotaReservationStatus,
)
from src.domain.plans.models import (
    Plan,
    PlanTier,
    PlanStatus,
    PlanFeature,
    PlanLimits,
    PlanQuotaTemplate,
    PlanAssignment,
    PlanAssignmentStatus,
    PlanEntitlementRequest,
    PlanEntitlementDecision,
    PlanEntitlementStatus,
    PlanIntegrityError,
    PlanNotFoundError,
    PlanAssignmentNotFoundError,
)
from src.domain.reliability.ports import ClockPort

# Application Imports
from src.application.usage_metering.usage_metering_service import UsageMeteringService
from src.application.usage_metering.model_gateway_bridge import ModelGatewayUsageBridge
from src.application.quota_management.quota_management_service import QuotaManagementService
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.application.model_gateway.model_gateway_service import ModelGatewayService
from src.application.model_routing.model_routing_strategy import DeterministicModelRoutingStrategy
from src.application.model_routing.registry import InMemoryModelRouteRegistry
from src.application.saas_authorization.saas_authorization_service import SaaSAuthorizationService

# Infrastructure Repositories
from src.infrastructure.persistence.data.json.usage_event_repository import (
    InMemoryUsageEventRepository,
    JsonUsageEventRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
    InMemoryQuotaReservationRepository,
    JsonQuotaPolicyRepository,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    InMemoryPlanCatalogRepository,
    InMemoryPlanAssignmentRepository,
    JsonPlanCatalogRepository,
    JsonPlanAssignmentRepository,
)
from src.infrastructure.persistence.data.json.rbac_repository import (
    JsonRoleRepository,
    JsonRoleAssignmentRepository,
)
from src.infrastructure.persistence.data.json.session_repository import JsonSaaSSessionRepository
from src.infrastructure.persistence.data.json.organization_repository import (
    JsonOrganizationRepository,
    JsonMembershipRepository,
)
from src.infrastructure.persistence.data.json.identity_repository import JsonIdentityRepository
from src.infrastructure.persistence.data.json.audit_repository import JsonAuditRepository
from src.infrastructure.persistence.data.json.agent_trace_repository import JsonAgentTraceRepository
from src.application.identity.identity_service import IdentityService
from src.application.organization.organization_service import OrganizationService, OrganizationMembershipService
from src.application.session.saas_session_service import SaaSSessionService
from src.application.tenant.tenant_context_service import TenantContextService
from src.application.rbac.rbac_service import RBACService
from src.application.authorization.authorization_service import AuthorizationService
from src.domain.policy.engine import PolicyEngine
from src.domain.policy.rules import AuthorizationPolicyRule


class MockClock(ClockPort):
    def __init__(self, initial_time: datetime):
        self._current_time = initial_time

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def advance(self, td: timedelta) -> None:
        self._current_time += td


class MockProviderExecutor:
    """Mock determinista de proveedor LLM que cuenta llamadas reales."""
    def __init__(self, succeed: bool = True, tokens_out: int = 50, cost: Decimal = Decimal("0.001")):
        self.succeed = succeed
        self.tokens_out = tokens_out
        self.cost = cost
        self.call_count = 0

    def execute_call(self, route: ModelRoute, prompt: Any) -> Dict[str, Any]:
        self.call_count += 1
        if not self.succeed:
            raise RuntimeError("Provider upstream error 500")
        return {
            "output_tokens": self.tokens_out,
            "cost": self.cost,
            "raw_response": "Mock inference response content",
        }


@pytest.fixture
def now_utc() -> datetime:
    return datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def standard_catalog() -> InMemoryPlanCatalogRepository:
    repo = InMemoryPlanCatalogRepository()

    free_plan = Plan(
        plan_id="plan_free",
        name="Free Tier Plan",
        tier=PlanTier.FREE,
        version="1.0.0",
        features=(PlanFeature.MODEL_INFERENCE,),
        limits=PlanLimits(max_users=1, max_organizations=1, max_concurrent_missions=1),
        quota_template=PlanQuotaTemplate(
            rules=(
                QuotaRule(
                    rule_id="r_free_reqs",
                    quota_type=QuotaType.MAX_REQUESTS,
                    limit_value=100,
                    scope=QuotaScope.TENANT,
                    window_type=QuotaWindowType.DAY,
                ),
                QuotaRule(
                    rule_id="r_free_tokens",
                    quota_type=QuotaType.MAX_TOTAL_TOKENS,
                    limit_value=10000,
                    scope=QuotaScope.TENANT,
                    window_type=QuotaWindowType.MONTH,
                ),
            )
        ),
        allowed_model_classes=("gpt-4o", "gpt-3.5-turbo", "basic"),
        allowed_providers=("openai",),
    )

    pro_plan = Plan(
        plan_id="plan_pro",
        name="Pro Tier Plan",
        tier=PlanTier.PRO,
        version="1.0.0",
        features=(
            PlanFeature.MODEL_INFERENCE,
            PlanFeature.ADVANCED_MODELS,
            PlanFeature.AUTONOMOUS_MISSIONS,
            PlanFeature.MULTI_USER,
            PlanFeature.ADVANCED_ANALYTICS,
        ),
        limits=PlanLimits(max_users=10, max_organizations=3, max_concurrent_missions=5),
        quota_template=PlanQuotaTemplate(
            rules=(
                QuotaRule(
                    rule_id="r_pro_reqs",
                    quota_type=QuotaType.MAX_REQUESTS,
                    limit_value=10000,
                    scope=QuotaScope.TENANT,
                    window_type=QuotaWindowType.DAY,
                ),
                QuotaRule(
                    rule_id="r_pro_tokens",
                    quota_type=QuotaType.MAX_TOTAL_TOKENS,
                    limit_value=1000000,
                    scope=QuotaScope.TENANT,
                    window_type=QuotaWindowType.MONTH,
                ),
            )
        ),
        allowed_model_classes=("gpt-4o", "gpt-4o-mini", "claude-3-5-sonnet", "basic", "advanced", "reasoning"),
        allowed_providers=("openai", "anthropic", "google"),
    )

    repo.save_plan(free_plan)
    repo.save_plan(pro_plan)
    return repo


@pytest.fixture
def standard_route() -> ModelRoute:
    return ModelRoute(
        route_id="route_openai_gpt4o",
        provider="openai",
        model_id="gpt-4o",
        status=RouteStatus.AVAILABLE,
        capabilities=(RouteCapability.TOOL_USE, RouteCapability.STRUCTURED_OUTPUT),
        context_window=128000,
        estimated_cost_input_per_million=Decimal("5.00"),
        estimated_cost_output_per_million=Decimal("15.00"),
    )


# =========================================================================
# Escenario A: Tenant A FREE -> Expected Features / Quotas
# =========================================================================
def test_scenario_a_tenant_free_expected_features_and_quotas(now_utc, standard_catalog):
    clock = MockClock(now_utc)
    assignment_repo = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()

    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=assignment_repo,
        quota_policy_repository=quota_repo,
        clock=clock,
    )

    tenant_id = "tenant_free_a"
    assignment = plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_free")
    assert assignment.plan_id == "plan_free"
    assert assignment.status == PlanAssignmentStatus.ACTIVE

    # 1. Verificar Entitlement
    dec_inf = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_id, feature=PlanFeature.MODEL_INFERENCE)
    )
    assert dec_inf.status == PlanEntitlementStatus.ALLOW
    assert dec_inf.is_entitled is True

    dec_adv = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_id, feature=PlanFeature.ADVANCED_MODELS)
    )
    assert dec_adv.status == PlanEntitlementStatus.DENY
    assert dec_adv.is_entitled is False

    # 2. Verificar Quota Policy materializada en O.7
    policy = quota_repo.get_policy(tenant_id)
    assert policy is not None
    assert len(policy.rules) >= 2
    rule_types = {r.quota_type: r.limit_value for r in policy.rules}
    assert rule_types[QuotaType.MAX_REQUESTS] == 100
    assert rule_types[QuotaType.MAX_TOTAL_TOKENS] == 10000


# =========================================================================
# Escenario B: Tenant B PRO -> Independent Entitlements
# =========================================================================
def test_scenario_b_tenant_pro_independent_entitlements(now_utc, standard_catalog):
    clock = MockClock(now_utc)
    assignment_repo = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()

    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=assignment_repo,
        quota_policy_repository=quota_repo,
        clock=clock,
    )

    tenant_a = "tenant_free_a"
    tenant_b = "tenant_pro_b"

    plan_service.assign_plan(tenant_id=tenant_a, plan_id="plan_free")
    plan_service.assign_plan(tenant_id=tenant_b, plan_id="plan_pro")

    # Tenant A Deny
    dec_a = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_a, feature=PlanFeature.AUTONOMOUS_MISSIONS)
    )
    assert dec_a.status == PlanEntitlementStatus.DENY

    # Tenant B Allow
    dec_b = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_b, feature=PlanFeature.AUTONOMOUS_MISSIONS)
    )
    assert dec_b.status == PlanEntitlementStatus.ALLOW

    # Quota de Tenant B
    pol_b = quota_repo.get_policy(tenant_b)
    rule_types_b = {r.quota_type: r.limit_value for r in pol_b.rules}
    assert rule_types_b[QuotaType.MAX_REQUESTS] == 10000


# =========================================================================
# Escenario C: A cannot use PRO-only feature -> zero downstream provider calls
# =========================================================================
def test_scenario_c_free_cannot_use_pro_feature_zero_provider_calls(now_utc, standard_catalog, standard_route):
    clock = MockClock(now_utc)
    assignment_repo = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()

    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=quota_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )
    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=assignment_repo,
        quota_policy_repository=quota_repo,
        clock=clock,
    )

    tenant_id = "tenant_free_c"
    plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_free")

    mock_provider = MockProviderExecutor(succeed=True)

    # El gateway / orquestador consulta plan entitlement antes de provider call
    entitlement_dec = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(
            tenant_id=tenant_id,
            feature=PlanFeature.ADVANCED_MODELS,
            model_id="gpt-4o",
            provider="openai",
        )
    )

    if entitlement_dec.is_entitled:
        mock_provider.execute_call(standard_route, "prompt")

    # Assert: CERO llamadas a proveedor
    assert entitlement_dec.status == PlanEntitlementStatus.DENY
    assert mock_provider.call_count == 0


# =========================================================================
# Escenario D: PRO feature + Missing RBAC -> Denied (Entitlement AND RBAC)
# =========================================================================
def test_scenario_d_pro_feature_missing_rbac_denied(tmp_path, now_utc, standard_catalog):
    clock = MockClock(now_utc)
    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=InMemoryPlanAssignmentRepository(),
        quota_policy_repository=InMemoryQuotaPolicyRepository(),
        clock=clock,
    )

    tenant_id = "tenant_pro_d"
    plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_pro")

    # 1. Plan Entitlement es ALLOW
    ent_dec = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_id, feature=PlanFeature.AUTONOMOUS_MISSIONS)
    )
    assert ent_dec.status == PlanEntitlementStatus.ALLOW
    assert ent_dec.is_entitled is True

    # 2. RBAC con rol sin permiso
    role_repo = JsonRoleRepository(tmp_path)
    asgn_repo = JsonRoleAssignmentRepository(tmp_path)
    rbac_service = RBACService(role_repository=role_repo, assignment_repository=asgn_repo, clock=clock)

    # Crear rol básico sin permiso de misiones
    perm_pub = rbac_service.create_permission(permission_id="perm_pub", action="LISTING_PUBLISH")
    rbac_service.define_role(role_id="role_basic", name="Basic User", permissions=[perm_pub])
    rbac_service.assign_role(identity_id="user_unprivileged", role_id="role_basic", scope=tenant_id)

    perm_check = rbac_service.has_permission(
        principal_or_identity="user_unprivileged",
        action="missions:execute",
        scope=tenant_id,
    )
    assert perm_check is False

    # Pipeline: Entitlement AND RBAC
    final_allowed = ent_dec.is_entitled and perm_check
    assert final_allowed is False


# =========================================================================
# Escenario E: RBAC Allow + Plan Deny -> Denied (Entitlement AND RBAC)
# =========================================================================
def test_scenario_e_rbac_allow_plan_deny_denied(tmp_path, now_utc, standard_catalog):
    clock = MockClock(now_utc)
    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=InMemoryPlanAssignmentRepository(),
        quota_policy_repository=InMemoryQuotaPolicyRepository(),
        clock=clock,
    )

    tenant_id = "tenant_free_e"
    # Plan FREE no incluye AUTONOMOUS_MISSIONS
    plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_free")

    # 1. Plan Entitlement -> DENY
    ent_dec = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_id, feature=PlanFeature.AUTONOMOUS_MISSIONS)
    )
    assert ent_dec.status == PlanEntitlementStatus.DENY
    assert ent_dec.is_entitled is False

    # 2. RBAC -> GRANTED
    role_repo = JsonRoleRepository(tmp_path)
    asgn_repo = JsonRoleAssignmentRepository(tmp_path)
    rbac_service = RBACService(role_repository=role_repo, assignment_repository=asgn_repo, clock=clock)

    perm = rbac_service.create_permission(permission_id="perm_mission", action="missions:execute")
    rbac_service.define_role(role_id="role_admin", name="Admin", permissions=[perm])
    rbac_service.assign_role(identity_id="user_admin", role_id="role_admin", scope=tenant_id)

    perm_check = rbac_service.has_permission(
        principal_or_identity="user_admin",
        action="missions:execute",
        scope=tenant_id,
    )
    assert perm_check is True

    # Pipeline: Entitlement AND RBAC
    final_allowed = ent_dec.is_entitled and perm_check
    assert final_allowed is False


# =========================================================================
# Escenario F: Plan Upgrade -> New Entitlement Active
# =========================================================================
def test_scenario_f_plan_upgrade_activates_new_entitlements(now_utc, standard_catalog):
    clock = MockClock(now_utc)
    assignment_repo = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()
    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=assignment_repo,
        quota_policy_repository=quota_repo,
        clock=clock,
    )

    tenant_id = "tenant_upgrade_f"
    plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_free")

    # Antes del upgrade
    dec_pre = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_id, feature=PlanFeature.ADVANCED_MODELS)
    )
    assert dec_pre.status == PlanEntitlementStatus.DENY

    # Upgrade a PRO
    plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_pro", source_reason="upgrade_checkout")

    # Tras el upgrade
    dec_post = plan_service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_id, feature=PlanFeature.ADVANCED_MODELS)
    )
    assert dec_post.status == PlanEntitlementStatus.ALLOW
    assert dec_post.is_entitled is True

    # Quota materializada actualizada
    policy = quota_repo.get_policy(tenant_id)
    rule_types = {r.quota_type: r.limit_value for r in policy.rules}
    assert rule_types[QuotaType.MAX_REQUESTS] == 10000


# =========================================================================
# Escenario G: Downgrade Preserves Historical O.6 Usage
# =========================================================================
def test_scenario_g_downgrade_preserves_o6_historical_usage(now_utc, standard_catalog):
    clock = MockClock(now_utc)
    assignment_repo = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()
    usage_repo = InMemoryUsageEventRepository()
    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)

    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=assignment_repo,
        quota_policy_repository=quota_repo,
        clock=clock,
    )

    tenant_id = "tenant_downgrade_g"
    # Inicialmente PRO
    plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_pro")

    # Registrar 5 requests y 50,000 tokens en O.6
    for i in range(5):
        usage_service.record_usage_event(
            context=TenantContext(tenant_id=tenant_id),
            event=UsageEvent(
                usage_event_id=f"ev_usage_{i}",
                tenant_id=tenant_id,
                total_tokens=10000,
                input_tokens=5000,
                output_tokens=5000,
                estimated_cost=Decimal("0.10"),
                request_status=UsageRequestStatus.SUCCESS,
                occurred_at=clock.now(),
            ),
        )

    agg_pre = usage_service.aggregate_usage(
        context=TenantContext(tenant_id=tenant_id),
        query=UsageQuery(
            tenant_id=tenant_id,
            period=UsagePeriod(start_time=clock.now() - timedelta(days=1), end_time=clock.now() + timedelta(days=1)),
        ),
    )
    assert agg_pre.total_tokens == 50000
    assert agg_pre.total_requests == 5

    # Downgrade a FREE
    plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_free", source_reason="downgrade_request")

    # O.6 Usage no fue tocado ni reseteado
    agg_post = usage_service.aggregate_usage(
        context=TenantContext(tenant_id=tenant_id),
        query=UsageQuery(
            tenant_id=tenant_id,
            period=UsagePeriod(start_time=clock.now() - timedelta(days=1), end_time=clock.now() + timedelta(days=1)),
        ),
    )
    assert agg_post.total_tokens == 50000
    assert agg_post.total_requests == 5


# =========================================================================
# Escenario H: O.7 uses quota derived from active plan
# =========================================================================
def test_scenario_h_o7_uses_quota_derived_from_active_plan(now_utc, standard_catalog):
    clock = MockClock(now_utc)
    assignment_repo = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()

    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=quota_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )
    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=assignment_repo,
        quota_policy_repository=quota_repo,
        clock=clock,
    )

    tenant_id = "tenant_quota_h"
    # Plan FREE: límite 10000 tokens
    plan_service.assign_plan(tenant_id=tenant_id, plan_id="plan_free")

    # 1. Petición de 5000 tokens -> permitida
    dec1 = quota_service.evaluate_and_reserve(
        QuotaRequest(tenant_id=tenant_id, estimated_total_tokens=5000, correlation_id="c1")
    )
    assert dec1.is_allowed is True

    # 2. Petición de 6000 tokens -> excede 10000 (5000 + 6000 > 10000) -> rechazada
    dec2 = quota_service.evaluate_and_reserve(
        QuotaRequest(tenant_id=tenant_id, estimated_total_tokens=6000, correlation_id="c2")
    )
    assert dec2.is_allowed is False


# =========================================================================
# Escenario I: Restart -> Catalog and Assignments Preserved
# =========================================================================
def test_scenario_i_restart_catalog_and_assignments_preserved(tmp_path, now_utc):
    clock = MockClock(now_utc)
    catalog_dir = tmp_path
    tenants_dir = tmp_path

    # 1. Primera ejecución: crear repositorio en disco y guardar plan y asignación
    repo_catalog_1 = JsonPlanCatalogRepository(base_dir=catalog_dir)
    repo_assign_1 = JsonPlanAssignmentRepository(base_dir=tenants_dir)
    quota_repo_1 = JsonQuotaPolicyRepository(base_dir=tenants_dir)

    plan = Plan(
        plan_id="plan_custom",
        name="Custom Enterprise Plan",
        tier=PlanTier.CUSTOM,
        version="1.0.0",
        features=(PlanFeature.MODEL_INFERENCE, PlanFeature.BYO_KEY),
        limits=PlanLimits(max_users=50, max_organizations=10, max_concurrent_missions=20),
        quota_template=PlanQuotaTemplate(
            rules=(
                QuotaRule(
                    rule_id="r_custom_reqs",
                    quota_type=QuotaType.MAX_REQUESTS,
                    limit_value=50000,
                    scope=QuotaScope.TENANT,
                    window_type=QuotaWindowType.DAY,
                ),
                QuotaRule(
                    rule_id="r_custom_tokens",
                    quota_type=QuotaType.MAX_TOTAL_TOKENS,
                    limit_value=5000000,
                    scope=QuotaScope.TENANT,
                    window_type=QuotaWindowType.MONTH,
                ),
            )
        ),
    )
    repo_catalog_1.save_plan(plan)

    plan_service_1 = PlanEntitlementService(
        catalog_repository=repo_catalog_1,
        assignment_repository=repo_assign_1,
        quota_policy_repository=quota_repo_1,
        clock=clock,
    )

    tenant_id = "tenant_restart_i"
    plan_service_1.assign_plan(tenant_id=tenant_id, plan_id="plan_custom")

    # 2. Simular Reinicio: instanciar nuevos repositorios apuntando al mismo disco
    repo_catalog_2 = JsonPlanCatalogRepository(base_dir=catalog_dir)
    repo_assign_2 = JsonPlanAssignmentRepository(base_dir=tenants_dir)
    quota_repo_2 = JsonQuotaPolicyRepository(base_dir=tenants_dir)

    plan_service_2 = PlanEntitlementService(
        catalog_repository=repo_catalog_2,
        assignment_repository=repo_assign_2,
        quota_policy_repository=quota_repo_2,
        clock=clock,
    )

    loaded_plan = repo_catalog_2.get_plan("plan_custom")
    assert loaded_plan is not None
    assert loaded_plan.name == "Custom Enterprise Plan"

    active_assign = repo_assign_2.get_active_assignment(tenant_id, clock.now())
    assert active_assign is not None
    assert active_assign.plan_id == "plan_custom"

    # Evaluar entitlement tras reinicio
    dec = plan_service_2.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id=tenant_id, feature=PlanFeature.BYO_KEY)
    )
    assert dec.status == PlanEntitlementStatus.ALLOW
    assert dec.is_entitled is True


# =========================================================================
# Escenario J: Tampered Assignment -> Fail-Safe
# =========================================================================
def test_scenario_j_tampered_assignment_failsafe(now_utc, standard_catalog):
    # Validar que si se intenta crear una asignación con checksum corrupto se lanza PlanIntegrityError
    with pytest.raises(PlanIntegrityError):
        PlanAssignment(
            assignment_id="as_corrupt",
            tenant_id="tenant_tamper_j",
            plan_id="plan_pro",
            plan_version="1.0.0",
            assigned_at=now_utc,
            effective_from=now_utc,
            status=PlanAssignmentStatus.ACTIVE,
            checksum="invalid_tampered_sha256",
        )


# =========================================================================
# Escenario K: E2E Pipeline SaaS Completo
# Tenant -> Session -> O.4 Authorization -> O.8 Plan Entitlement -> O.7 Quota -> O.5 Gateway -> Provider Mock
# =========================================================================
def test_scenario_k_e2e_complete_pipeline(tmp_path, now_utc, standard_catalog, standard_route):
    clock = MockClock(now_utc)

    # 1. Repositorios y Servicios
    assignment_repo = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()
    res_repo = InMemoryQuotaReservationRepository()
    usage_repo = InMemoryUsageEventRepository()

    usage_service = UsageMeteringService(repository=usage_repo, clock=clock)
    quota_service = QuotaManagementService(
        policy_repository=quota_repo,
        reservation_repository=res_repo,
        usage_metering_service=usage_service,
        clock=clock,
    )
    plan_service = PlanEntitlementService(
        catalog_repository=standard_catalog,
        assignment_repository=assignment_repo,
        quota_policy_repository=quota_repo,
        clock=clock,
    )

    role_repo = JsonRoleRepository(tmp_path)
    asgn_repo = JsonRoleAssignmentRepository(tmp_path)
    rbac_service = RBACService(role_repository=role_repo, assignment_repository=asgn_repo, clock=clock)

    perm_infer = rbac_service.create_permission(permission_id="perm_infer", action="models:infer")
    rbac_service.define_role(role_id="role_user", name="User", permissions=[perm_infer])

    # 2. Configurar Tenant A (FREE) y Tenant B (PRO)
    tenant_a = "tenant_e2e_a"
    tenant_b = "tenant_e2e_b"
    user_a = "user_a"
    user_b = "user_b"

    rbac_service.assign_role(identity_id=user_a, role_id="role_user", scope=tenant_a)
    rbac_service.assign_role(identity_id=user_b, role_id="role_user", scope=tenant_b)

    plan_service.assign_plan(tenant_id=tenant_a, plan_id="plan_free")
    plan_service.assign_plan(tenant_id=tenant_b, plan_id="plan_pro")

    mock_provider = MockProviderExecutor(succeed=True, tokens_out=100)

    # Helper de pipeline E2E
    def execute_saas_inference(tenant_id: str, actor_id: str, feature: PlanFeature, model_id: str, requested_tokens: int) -> Dict[str, Any]:
        # Step 1: O.3 Session validation
        session = SaaSSession(
            session_id=f"sess_{tenant_id}_{actor_id}",
            identity_id=actor_id,
            tenant_id=tenant_id,
            status=SessionStatus.ACTIVE,
            created_at=clock.now(),
            expires_at=clock.now() + timedelta(hours=1),
        )
        if not session.is_active or session.is_expired(clock.now()):
            return {"status": "SESSION_EXPIRED", "provider_called": False}

        # Step 2: O.4 Authorization (RBAC Permission Check)
        has_perm = rbac_service.has_permission(
            principal_or_identity=actor_id,
            action="models:infer",
            scope=tenant_id,
        )
        if not has_perm:
            return {"status": "AUTHZ_DENIED", "provider_called": False}

        # Step 3: O.8 Plan Entitlement
        ent_res = plan_service.evaluate_entitlement(
            PlanEntitlementRequest(
                tenant_id=tenant_id,
                feature=feature,
                model_id=model_id,
                provider="openai",
            )
        )
        if not ent_res.is_entitled:
            return {"status": "PLAN_NOT_ENTITLED", "provider_called": False}

        # Step 4: O.7 Quota Reservation
        quota_res = quota_service.evaluate_and_reserve(
            QuotaRequest(
                tenant_id=tenant_id,
                identity_id=actor_id,
                estimated_total_tokens=requested_tokens,
                correlation_id=f"corr_{clock.now().timestamp()}",
            )
        )
        if not quota_res.is_allowed:
            return {"status": "QUOTA_EXCEEDED", "provider_called": False}

        # Step 5: O.5 Gateway -> Provider Invocation
        provider_resp = mock_provider.execute_call(standard_route, "test prompt")

        # Step 6: O.6 Usage Metering & O.7 Reconciliation
        quota_service.reconcile_reservation(
            reservation_id=quota_res.reservation_id,
            tenant_id=tenant_id,
            actual_status=QuotaReservationStatus.CONSUMED,
            actual_tokens=provider_resp["output_tokens"],
        )

        return {"status": "SUCCESS", "provider_called": True, "data": provider_resp}

    # 1. Entitled + Authorized + Under Quota -> provider call = 1
    call1 = execute_saas_inference(
        tenant_id=tenant_a,
        actor_id=user_a,
        feature=PlanFeature.MODEL_INFERENCE,
        model_id="gpt-4o",
        requested_tokens=500,
    )
    assert call1["status"] == "SUCCESS"
    assert call1["provider_called"] is True
    assert mock_provider.call_count == 1

    # 2. Feature absent in Plan -> provider call = 0 (Free trying to access Advanced Models)
    call2 = execute_saas_inference(
        tenant_id=tenant_a,
        actor_id=user_a,
        feature=PlanFeature.ADVANCED_MODELS,
        model_id="gpt-4o",
        requested_tokens=500,
    )
    assert call2["status"] == "PLAN_NOT_ENTITLED"
    assert call2["provider_called"] is False
    assert mock_provider.call_count == 1  # No increment

    # 3. Quota Exceeded -> provider call = 0
    # Tenant A tiene límite de 10000 tokens en Free. Intentamos reservar 20000
    call3 = execute_saas_inference(
        tenant_id=tenant_a,
        actor_id=user_a,
        feature=PlanFeature.MODEL_INFERENCE,
        model_id="gpt-4o",
        requested_tokens=20000,
    )
    assert call3["status"] == "QUOTA_EXCEEDED"
    assert call3["provider_called"] is False
    assert mock_provider.call_count == 1  # No increment

    # 4. Tenant B (PRO) plan does not affect Tenant A
    # Tenant B puede pedir modelos avanzados con alta cuota
    call4 = execute_saas_inference(
        tenant_id=tenant_b,
        actor_id=user_b,
        feature=PlanFeature.ADVANCED_MODELS,
        model_id="gpt-4o",
        requested_tokens=50000,
    )
    assert call4["status"] == "SUCCESS"
    assert call4["provider_called"] is True
    assert mock_provider.call_count == 2
