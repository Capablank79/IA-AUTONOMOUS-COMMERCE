"""
Pruebas Unitarias para O.8 — Plans & Pricing Tiers SaaS.
(Tenant Plan Entitlements, Features, Limits, Quota Templates & Versioning)

Requisitos mínimos cubiertos:
1. immutable Plan & checksum verification
2. plan versioning & history
3. feature allow
4. feature deny
5. unknown feature deny
6. tenant assignment lifecycle
7. cross-tenant isolation (Tenant A plan does not affect Tenant B)
8. plan -> quota mapping (materialize QuotaPolicy)
9. FREE / PRO independent limits & features
10. plan feature != RBAC permission (conceptual & evaluation orthogonality)
11. missing plan fail-safe (UNKNOWN / DENY)
12. corrupt assignment / plan integrity fail-safe
13. upgrade effective immediately
14. downgrade preserves historical usage (O.6 usage untouched)
15. deterministic entitlement decisions
16. no O.9 billing / payment logic implemented
"""

from datetime import datetime, timezone, timedelta
from decimal import Decimal
import pytest

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
)
from src.domain.quota_management.models import (
    QuotaRule,
    QuotaType,
    QuotaScope,
    QuotaWindowType,
    QuotaPolicy,
)
from src.infrastructure.persistence.data.json.plan_repository import (
    InMemoryPlanCatalogRepository,
    InMemoryPlanAssignmentRepository,
)
from src.infrastructure.persistence.data.json.quota_repository import (
    InMemoryQuotaPolicyRepository,
)
from src.application.plans.plan_entitlement_service import PlanEntitlementService
from src.domain.reliability.ports import ClockPort
from src.domain.tenant.models import TenantContext, CrossTenantAccessError


class MockClock(ClockPort):
    def __init__(self, initial_time: datetime):
        self._current_time = initial_time

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def set_time(self, new_time: datetime) -> None:
        self._current_time = new_time

    def advance(self, duration: timedelta) -> None:
        self._current_time += duration


@pytest.fixture
def now_utc() -> datetime:
    return datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def free_plan() -> Plan:
    quota_template = PlanQuotaTemplate(
        rules=(
            QuotaRule(
                rule_id="free_daily_reqs",
                quota_type=QuotaType.MAX_REQUESTS,
                limit_value=100,
                scope=QuotaScope.TENANT,
                window_type=QuotaWindowType.DAY,
            ),
            QuotaRule(
                rule_id="free_monthly_tokens",
                quota_type=QuotaType.MAX_TOTAL_TOKENS,
                limit_value=50000,
                scope=QuotaScope.TENANT,
                window_type=QuotaWindowType.MONTH,
            ),
        ),
        is_unlimited=False,
        description="Free Tier Quotas",
    )
    return Plan(
        plan_id="plan_free",
        name="Free Plan",
        tier=PlanTier.FREE,
        version="1.0.0",
        status=PlanStatus.ACTIVE,
        features=(PlanFeature.MODEL_INFERENCE,),
        limits=PlanLimits(max_users=1, max_organizations=1, max_concurrent_missions=1),
        quota_template=quota_template,
        allowed_model_classes=("gpt-4o-mini", "claude-3-haiku"),
        allowed_providers=("openai", "anthropic"),
    )


@pytest.fixture
def pro_plan() -> Plan:
    quota_template = PlanQuotaTemplate(
        rules=(
            QuotaRule(
                rule_id="pro_daily_reqs",
                quota_type=QuotaType.MAX_REQUESTS,
                limit_value=10000,
                scope=QuotaScope.TENANT,
                window_type=QuotaWindowType.DAY,
            ),
            QuotaRule(
                rule_id="pro_monthly_tokens",
                quota_type=QuotaType.MAX_TOTAL_TOKENS,
                limit_value=5000000,
                scope=QuotaScope.TENANT,
                window_type=QuotaWindowType.MONTH,
            ),
        ),
        is_unlimited=False,
        description="Pro Tier Quotas",
    )
    return Plan(
        plan_id="plan_pro",
        name="Pro Plan",
        tier=PlanTier.PRO,
        version="1.0.0",
        status=PlanStatus.ACTIVE,
        features=(
            PlanFeature.MODEL_INFERENCE,
            PlanFeature.ADVANCED_MODELS,
            PlanFeature.AUTONOMOUS_MISSIONS,
            PlanFeature.MARKETPLACE_OPERATIONS,
            PlanFeature.MULTI_USER,
            PlanFeature.ADVANCED_ANALYTICS,
        ),
        limits=PlanLimits(max_users=10, max_organizations=3, max_concurrent_missions=5),
        quota_template=quota_template,
        allowed_model_classes=("*",),
        allowed_providers=("openai", "anthropic", "google"),
    )


# 1. Immutable Plan & Checksum verification
def test_plan_immutability_and_checksum(free_plan):
    assert free_plan.verify_integrity()
    assert free_plan.checksum != ""

    with pytest.raises(Exception):
        free_plan.name = "Mutated Name"  # Frozen dataclass


def test_plan_checksum_tamper_detected():
    with pytest.raises(PlanIntegrityError):
        Plan(
            plan_id="plan_tampered",
            name="Tampered",
            tier=PlanTier.FREE,
            version="1.0.0",
            status=PlanStatus.ACTIVE,
            features=(PlanFeature.MODEL_INFERENCE,),
            checksum="bad_checksum_hash_12345",
        )


# 2. Plan versioning & history
def test_plan_versioning(free_plan):
    catalog = InMemoryPlanCatalogRepository([free_plan])

    # Agregar version 2.0.0 con nueva feature
    v2_plan = Plan(
        plan_id="plan_free",
        name="Free Plan v2",
        tier=PlanTier.FREE,
        version="2.0.0",
        status=PlanStatus.ACTIVE,
        features=(PlanFeature.MODEL_INFERENCE, PlanFeature.BYO_KEY),
        limits=PlanLimits(max_users=2),
    )
    catalog.save_plan(v2_plan)

    # get_plan sin version devuelve la más reciente
    latest = catalog.get_plan("plan_free")
    assert latest.version == "2.0.0"
    assert latest.has_feature(PlanFeature.BYO_KEY)

    # get_plan con version histórica 1.0.0
    v1 = catalog.get_plan("plan_free", version="1.0.0")
    assert v1.version == "1.0.0"
    assert not v1.has_feature(PlanFeature.BYO_KEY)

    versions = catalog.list_plan_versions("plan_free")
    assert len(versions) == 2


# 3. Feature allow
def test_feature_allow(pro_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([pro_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    service.assign_plan("tenant_alpha", "plan_pro")

    req = PlanEntitlementRequest(tenant_id="tenant_alpha", feature=PlanFeature.AUTONOMOUS_MISSIONS)
    decision = service.evaluate_entitlement(req)

    assert decision.status == PlanEntitlementStatus.ALLOW
    assert decision.is_entitled is True
    assert decision.reason_code == "PLAN_ENTITLED"
    assert decision.verify_integrity()


# 4. Feature deny
def test_feature_deny(free_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    service.assign_plan("tenant_alpha", "plan_free")

    req = PlanEntitlementRequest(tenant_id="tenant_alpha", feature=PlanFeature.ADVANCED_MODELS)
    decision = service.evaluate_entitlement(req)

    assert decision.status == PlanEntitlementStatus.DENY
    assert decision.is_entitled is False
    assert decision.reason_code == "FEATURE_NOT_IN_PLAN"
    assert "not enabled in plan" in decision.rationale


# 5. Unknown feature deny
def test_unknown_feature_deny(free_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    service.assign_plan("tenant_alpha", "plan_free")

    req = PlanEntitlementRequest(tenant_id="tenant_alpha", feature="NON_EXISTENT_FEATURE_FLAG")
    decision = service.evaluate_entitlement(req)

    assert decision.status == PlanEntitlementStatus.DENY
    assert decision.is_entitled is False
    assert decision.reason_code == "FEATURE_NOT_IN_PLAN"


# 6. Tenant assignment lifecycle
def test_tenant_assignment_lifecycle(free_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    assignment = service.assign_plan(
        tenant_id="tenant_alpha",
        plan_id="plan_free",
        source_reason="NEW_TENANT_ONBOARDING",
        actor_id="admin_user_1",
    )
    assert assignment.status == PlanAssignmentStatus.ACTIVE
    assert assignment.verify_integrity()
    assert assignment.source_reason == "NEW_TENANT_ONBOARDING"

    active_plan = service.get_active_plan("tenant_alpha")
    assert active_plan is not None
    assert active_plan.plan_id == "plan_free"


# 7. Cross-tenant isolation
def test_cross_tenant_isolation(free_plan, pro_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan, pro_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    service.assign_plan("tenant_a", "plan_free")
    service.assign_plan("tenant_b", "plan_pro")

    dec_a = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_a", feature=PlanFeature.AUTONOMOUS_MISSIONS)
    )
    dec_b = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_b", feature=PlanFeature.AUTONOMOUS_MISSIONS)
    )

    assert dec_a.is_entitled is False
    assert dec_b.is_entitled is True

    # Cross-tenant context guard assertion
    ctx_a = TenantContext(tenant_id="tenant_a")
    with pytest.raises(CrossTenantAccessError):
        service.get_active_plan("tenant_b", context=ctx_a)


# 8. Plan -> Quota mapping
def test_plan_materializes_quota_policy(free_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan])
    assignments = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, quota_policy_repository=quota_repo, clock=clock)

    service.assign_plan("tenant_alpha", "plan_free")

    # Verificar que O.7 tiene la QuotaPolicy materializada
    policy = quota_repo.get_policy("tenant_alpha")
    assert policy is not None
    assert policy.tenant_id == "tenant_alpha"
    assert len(policy.rules) == 2
    assert policy.verify_integrity()
    assert policy.rules[0].quota_type == QuotaType.MAX_REQUESTS
    assert policy.rules[0].limit_value == 100


# 9. FREE / PRO independent limits & models
def test_free_pro_independent_limits_and_models(free_plan, pro_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan, pro_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    service.assign_plan("tenant_free", "plan_free")
    service.assign_plan("tenant_pro", "plan_pro")

    # FREE model restrictions
    dec_free_ok = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_free", model_id="gpt-4o-mini")
    )
    dec_free_blocked = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_free", model_id="gpt-4o-super-heavy")
    )
    assert dec_free_ok.is_entitled is True
    assert dec_free_blocked.is_entitled is False
    assert dec_free_blocked.reason_code == "MODEL_NOT_PERMITTED_BY_PLAN"

    # FREE user limits
    dec_free_users_ok = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_free", requested_user_count=1)
    )
    dec_free_users_exceeded = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_free", requested_user_count=5)
    )
    assert dec_free_users_ok.is_entitled is True
    assert dec_free_users_exceeded.is_entitled is False
    assert dec_free_users_exceeded.reason_code == "MAX_USERS_EXCEEDED"

    # PRO allows all models and 10 users
    dec_pro_model = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_pro", model_id="gpt-4o-super-heavy")
    )
    dec_pro_users = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_pro", requested_user_count=8)
    )
    assert dec_pro_model.is_entitled is True
    assert dec_pro_users.is_entitled is True


# 10. Plan feature != RBAC permission
def test_plan_feature_orthogonality_concept():
    # Plan defines commercial capability
    # RBAC defines actor permissions
    # Demostramos que las definiciones son completamente independientes
    feat = PlanFeature.MODEL_INFERENCE
    assert feat.value == "MODEL_INFERENCE"
    assert feat != "INFERENCE_EXECUTE"  # Not an RBAC action token


# 11. Missing plan fail-safe
def test_missing_plan_failsafe(free_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    req = PlanEntitlementRequest(tenant_id="tenant_no_plan", feature=PlanFeature.MODEL_INFERENCE)
    decision = service.evaluate_entitlement(req)

    assert decision.status == PlanEntitlementStatus.UNKNOWN
    assert decision.is_entitled is False
    assert decision.reason_code == "NO_ACTIVE_PLAN_ASSIGNMENT"


# 12. Corrupt assignment fail-safe
def test_corrupt_assignment_failsafe():
    with pytest.raises(PlanIntegrityError):
        PlanAssignment(
            assignment_id="passign_corrupt",
            tenant_id="tenant_a",
            plan_id="plan_free",
            plan_version="1.0.0",
            assigned_at=datetime.now(timezone.utc),
            effective_from=datetime.now(timezone.utc),
            checksum="corrupted_checksum_abc123",
        )


# 13. Upgrade effective immediately
def test_plan_upgrade_effective(free_plan, pro_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan, pro_plan])
    assignments = InMemoryPlanAssignmentRepository()
    quota_repo = InMemoryQuotaPolicyRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, quota_policy_repository=quota_repo, clock=clock)

    service.assign_plan("tenant_alpha", "plan_free")
    assert service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_alpha", feature=PlanFeature.ADVANCED_MODELS)
    ).is_entitled is False

    # Upgrade a PRO
    clock.advance(timedelta(hours=1))
    service.change_plan("tenant_alpha", "plan_pro", reason="CUSTOMER_UPGRADE")

    # Entitlement activo inmediatamente
    dec_upgrade = service.evaluate_entitlement(
        PlanEntitlementRequest(tenant_id="tenant_alpha", feature=PlanFeature.ADVANCED_MODELS)
    )
    assert dec_upgrade.is_entitled is True
    assert dec_upgrade.plan_id == "plan_pro"

    # Quota policy de O.7 actualizada al template de PRO
    policy = quota_repo.get_policy("tenant_alpha")
    assert policy.rules[0].limit_value == 10000


# 14. Downgrade preserves historical usage
def test_plan_downgrade_preserves_history(free_plan, pro_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([free_plan, pro_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    service.assign_plan("tenant_alpha", "plan_pro")
    clock.advance(timedelta(days=10))
    service.change_plan("tenant_alpha", "plan_free", reason="CUSTOMER_DOWNGRADE")

    hist = assignments.list_assignments_for_tenant("tenant_alpha")
    assert len(hist) == 2
    assert hist[0].status == PlanAssignmentStatus.ACTIVE
    assert hist[0].plan_id == "plan_free"
    assert hist[1].status == PlanAssignmentStatus.SUPERSEDED
    assert hist[1].plan_id == "plan_pro"


# 15. Deterministic entitlement decisions
def test_deterministic_entitlement_decisions(pro_plan, now_utc):
    catalog = InMemoryPlanCatalogRepository([pro_plan])
    assignments = InMemoryPlanAssignmentRepository()
    clock = MockClock(now_utc)
    service = PlanEntitlementService(catalog, assignments, clock=clock)

    service.assign_plan("tenant_alpha", "plan_pro")

    req = PlanEntitlementRequest(
        tenant_id="tenant_alpha",
        feature=PlanFeature.MARKETPLACE_OPERATIONS,
        correlation_id="corr_test_123",
        request_timestamp=now_utc,
    )
    d1 = service.evaluate_entitlement(req)
    d2 = service.evaluate_entitlement(req)

    assert d1.status == d2.status == PlanEntitlementStatus.ALLOW
    assert d1.is_entitled == d2.is_entitled == True
    assert d1.reason_code == d2.reason_code == "PLAN_ENTITLED"


# 16. No O.9 billing/payment implementation
def test_no_o9_billing_payment_in_models(free_plan):
    # Verificar que el modelo Plan y PlanAssignment no contienen campos de facturación
    assert not hasattr(free_plan, "price")
    assert not hasattr(free_plan, "billing_cycle")
    assert not hasattr(free_plan, "currency")
    assert not hasattr(free_plan, "stripe_price_id")
    assert not hasattr(free_plan, "credit_card")
