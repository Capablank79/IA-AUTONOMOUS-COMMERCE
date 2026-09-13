"""
Pruebas de integración para Rate-limit Management (Hito P.11 — Production / Operations).

Cubre los 10 escenarios canónicos:
A. Tenant A under limit -> allowed.
B. Tenant A exceeds -> denied/429.
C. Tenant B unaffected.
D. window reset -> allowed again.
E. provider hard limit blocks upstream call.
F. user limit narrower than tenant.
G. plan/platform precedence enforced.
H. concurrent boundary no oversubscription.
I. monitoring receives denial facts.
J. store/policy failure follows fail-safe.
"""

from datetime import datetime, timezone, timedelta
import threading
from typing import Optional, List, Dict
import pytest

from src.domain.deployment.models import ApplicationEnvironment
from src.domain.reliability.ports import ClockPort
from src.domain.tenant.models import TenantContext
from src.domain.monitoring.models import (
    MetricSample,
    MetricType,
    MetricUnit,
    MonitoringScope,
)
from src.domain.rate_limit.models import (
    RateLimitScope,
    RateLimitStatus,
    RateLimitWindowUnit,
    RateLimitRule,
    RateLimitPolicy,
    RateLimitRequest,
    RateLimitDecision,
    build_canonical_rate_limit_key,
)
from src.domain.rate_limit.ports import (
    RateLimitPolicyRepositoryPort,
    RateLimitStateStorePort,
)
from src.infrastructure.persistence.data.json.rate_limit_repository import (
    InMemoryRateLimitPolicyRepository,
    InMemoryRateLimitStateStore,
)
from src.application.rate_limit.rate_limit_service import RateLimitService
from src.application.rate_limit.telemetry_adapter import MonitoringRateLimitTelemetryAdapter


class IntegrationClock(ClockPort):
    def __init__(self, initial_time: Optional[datetime] = None) -> None:
        self._current_time = initial_time or datetime(2026, 9, 12, 10, 0, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._current_time

    def sleep(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)

    def advance(self, seconds: float) -> None:
        self._current_time += timedelta(seconds=seconds)


class MockMetricRepository:
    def __init__(self) -> None:
        self.samples: List[MetricSample] = []

    def record_sample(self, sample: MetricSample) -> None:
        self.samples.append(sample)

    def record_samples(self, samples: List[MetricSample]) -> None:
        self.samples.extend(samples)


@pytest.fixture
def int_clock():
    return IntegrationClock()


@pytest.fixture
def int_policy_repo():
    return InMemoryRateLimitPolicyRepository()


@pytest.fixture
def int_state_store():
    return InMemoryRateLimitStateStore()


@pytest.fixture
def int_metric_repo():
    return MockMetricRepository()


@pytest.fixture
def int_telemetry(int_metric_repo):
    return MonitoringRateLimitTelemetryAdapter(metric_repository=int_metric_repo)


@pytest.fixture
def integrated_rate_limit_service(int_policy_repo, int_state_store, int_telemetry, int_clock):
    return RateLimitService(
        policy_repository=int_policy_repo,
        state_store=int_state_store,
        telemetry=int_telemetry,
        clock=int_clock,
        environment=ApplicationEnvironment.PRODUCTION,
        fail_closed_on_missing_policy=True,
    )


def test_scenario_a_tenant_under_limit_allowed(integrated_rate_limit_service, int_policy_repo, int_clock):
    """Scenario A: Tenant A under limit -> allowed."""
    rule = RateLimitRule(
        rule_id="tenant_a_rule",
        scope=RateLimitScope.TENANT,
        limit_rate=10,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=10,
    )
    int_policy_repo.save_policy(RateLimitPolicy(
        policy_id="pol_a",
        tenant_id="tenant_a",
        rules=(rule,),
    ))

    req = RateLimitRequest(tenant_id="tenant_a", cost_units=1, requested_at=int_clock.now())
    decision = integrated_rate_limit_service.check_and_consume(req)

    assert decision.is_allowed
    assert decision.status == RateLimitStatus.ALLOW
    assert decision.remaining == 9
    assert decision.http_status_code == 200


def test_scenario_b_tenant_exceeds_denied_429(integrated_rate_limit_service, int_policy_repo, int_clock):
    """Scenario B: Tenant A exceeds -> denied/429."""
    rule = RateLimitRule(
        rule_id="tenant_a_strict",
        scope=RateLimitScope.TENANT,
        limit_rate=1,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=1,
    )
    int_policy_repo.save_policy(RateLimitPolicy(
        policy_id="pol_a_strict",
        tenant_id="tenant_a",
        rules=(rule,),
    ))

    req = RateLimitRequest(tenant_id="tenant_a", cost_units=1, requested_at=int_clock.now())

    # 1st request succeeds
    d1 = integrated_rate_limit_service.check_and_consume(req)
    assert d1.is_allowed

    # 2nd request exceeds
    d2 = integrated_rate_limit_service.check_and_consume(req)
    assert not d2.is_allowed
    assert d2.status == RateLimitStatus.DENY
    assert d2.http_status_code == 429
    assert d2.retry_after_seconds > 0
    headers = d2.to_http_headers()
    assert "Retry-After" in headers


def test_scenario_c_tenant_b_unaffected(integrated_rate_limit_service, int_policy_repo, int_clock):
    """Scenario C: Tenant B is completely unaffected by Tenant A exhausting limit."""
    int_policy_repo.save_policy(RateLimitPolicy(
        policy_id="pol_a",
        tenant_id="tenant_a",
        rules=(RateLimitRule(rule_id="r_a", scope=RateLimitScope.TENANT, limit_rate=1, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=1),),
    ))
    int_policy_repo.save_policy(RateLimitPolicy(
        policy_id="pol_b",
        tenant_id="tenant_b",
        rules=(RateLimitRule(rule_id="r_b", scope=RateLimitScope.TENANT, limit_rate=5, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=5),),
    ))

    req_a = RateLimitRequest(tenant_id="tenant_a", cost_units=1, requested_at=int_clock.now())
    req_b = RateLimitRequest(tenant_id="tenant_b", cost_units=1, requested_at=int_clock.now())

    # Exhaust tenant_a
    assert integrated_rate_limit_service.check_and_consume(req_a).is_allowed
    assert not integrated_rate_limit_service.check_and_consume(req_a).is_allowed

    # tenant_b still has 5 tokens
    d_b = integrated_rate_limit_service.check_and_consume(req_b)
    assert d_b.is_allowed
    assert d_b.remaining == 4


def test_scenario_d_window_reset_allowed_again(integrated_rate_limit_service, int_policy_repo, int_clock):
    """Scenario D: Window reset / token refill -> allowed again."""
    rule = RateLimitRule(
        rule_id="refill_rule",
        scope=RateLimitScope.TENANT,
        limit_rate=60,  # 1 token / second
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=60,
    )
    int_policy_repo.save_policy(RateLimitPolicy(policy_id="p_refill", tenant_id="tenant_d", rules=(rule,)))

    # Consume all tokens
    req_all = RateLimitRequest(tenant_id="tenant_d", cost_units=60, requested_at=int_clock.now())
    assert integrated_rate_limit_service.check_and_consume(req_all).is_allowed

    # Immediately blocked
    req_one = RateLimitRequest(tenant_id="tenant_d", cost_units=1, requested_at=int_clock.now())
    assert not integrated_rate_limit_service.check_and_consume(req_one).is_allowed

    # Advance clock by 2 seconds (2 tokens refilled)
    int_clock.advance(2.0)
    req_after = RateLimitRequest(tenant_id="tenant_d", cost_units=1, requested_at=int_clock.now())
    d_after = integrated_rate_limit_service.check_and_consume(req_after)
    assert d_after.is_allowed
    assert d_after.status == RateLimitStatus.ALLOW


def test_scenario_e_provider_hard_limit_blocks_upstream_call(integrated_rate_limit_service, int_policy_repo, int_clock):
    """Scenario E: Provider hard limit blocks upstream call before provider is reached."""
    # Global provider limit: max 2 calls/minute to anthropic
    p_rule = RateLimitRule(
        rule_id="anthropic_global_cap",
        scope=RateLimitScope.PROVIDER,
        target_identifier="anthropic",
        limit_rate=2,
        window_unit=RateLimitWindowUnit.MINUTE,
        burst_capacity=2,
    )
    int_policy_repo.save_policy(RateLimitPolicy(policy_id="p_glob", tenant_id=None, rules=(p_rule,)))

    upstream_invoked = 0

    def mock_anthropic_call():
        nonlocal upstream_invoked
        upstream_invoked += 1
        return "response"

    req1 = RateLimitRequest(tenant_id="tenant_1", provider="anthropic", cost_units=1, requested_at=int_clock.now())
    req2 = RateLimitRequest(tenant_id="tenant_2", provider="anthropic", cost_units=1, requested_at=int_clock.now())
    req3 = RateLimitRequest(tenant_id="tenant_3", provider="anthropic", cost_units=1, requested_at=int_clock.now())

    # Call 1
    if integrated_rate_limit_service.check_and_consume(req1).is_allowed:
        mock_anthropic_call()

    # Call 2
    if integrated_rate_limit_service.check_and_consume(req2).is_allowed:
        mock_anthropic_call()

    # Call 3 (Blocked by provider protection)
    d3 = integrated_rate_limit_service.check_and_consume(req3)
    if d3.is_allowed:
        mock_anthropic_call()

    assert not d3.is_allowed
    assert d3.scope_violated == RateLimitScope.PROVIDER
    assert upstream_invoked == 2  # Blocked before reaching provider


def test_scenario_f_user_limit_narrower_than_tenant(integrated_rate_limit_service, int_policy_repo, int_clock):
    """Scenario F: User limit narrower than tenant."""
    # Tenant has 100 req/min, but user has 2 req/min
    t_rule = RateLimitRule(rule_id="t_broad", scope=RateLimitScope.TENANT, limit_rate=100, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=100)
    u_rule = RateLimitRule(rule_id="u_narrow", scope=RateLimitScope.USER, limit_rate=2, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=2)

    int_policy_repo.save_policy(RateLimitPolicy(policy_id="p_f", tenant_id="tenant_f", rules=(t_rule, u_rule)))

    req_u = RateLimitRequest(tenant_id="tenant_f", identity_id="alice", cost_units=1, requested_at=int_clock.now())

    assert integrated_rate_limit_service.check_and_consume(req_u).is_allowed
    assert integrated_rate_limit_service.check_and_consume(req_u).is_allowed
    d_blocked = integrated_rate_limit_service.check_and_consume(req_u)

    assert not d_blocked.is_allowed
    assert d_blocked.scope_violated == RateLimitScope.USER


def test_scenario_g_plan_platform_precedence_enforced(integrated_rate_limit_service, int_policy_repo, int_clock):
    """Scenario G: Precedencia de plataforma sobre tenant override permisivo."""
    # Global hard limit: max 5 req/min
    platform_rule = RateLimitRule(rule_id="plat_hard", scope=RateLimitScope.TENANT, limit_rate=5, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=5)
    int_policy_repo.save_policy(RateLimitPolicy(policy_id="p_plat", tenant_id=None, rules=(platform_rule,)))

    # Tenant intenta configurarse 1000 req/min
    tenant_rule = RateLimitRule(rule_id="tenant_ambitious", scope=RateLimitScope.TENANT, limit_rate=1000, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=1000)
    int_policy_repo.save_policy(RateLimitPolicy(policy_id="p_tenant", tenant_id="tenant_greedy", rules=(tenant_rule,)))

    req = RateLimitRequest(tenant_id="tenant_greedy", cost_units=1, requested_at=int_clock.now())

    # Can only do 5 calls due to platform hard limit
    for _ in range(5):
        assert integrated_rate_limit_service.check_and_consume(req).is_allowed

    # 6th is denied by platform rule
    d6 = integrated_rate_limit_service.check_and_consume(req)
    assert not d6.is_allowed
    assert d6.rule_id_violated == "plat_hard"


def test_scenario_h_concurrent_boundary_no_oversubscription(integrated_rate_limit_service, int_policy_repo, int_clock):
    """Scenario H: Concurrent boundary prevents oversubscription without TOCTOU."""
    rule = RateLimitRule(rule_id="r_exact", scope=RateLimitScope.TENANT, limit_rate=10, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=10)
    int_policy_repo.save_policy(RateLimitPolicy(policy_id="p_exact", tenant_id="tenant_h", rules=(rule,)))

    req = RateLimitRequest(tenant_id="tenant_h", cost_units=1, requested_at=int_clock.now())
    results: List[RateLimitStatus] = []
    mu = threading.Lock()

    def make_request():
        dec = integrated_rate_limit_service.check_and_consume(req)
        with mu:
            results.append(dec.status)

    threads = [threading.Thread(target=make_request) for _ in range(25)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(RateLimitStatus.ALLOW) == 10
    assert results.count(RateLimitStatus.DENY) == 15


def test_scenario_i_monitoring_receives_denial_facts(integrated_rate_limit_service, int_policy_repo, int_metric_repo, int_clock):
    """Scenario I: Monitoring receives denial facts via P.7 adapter."""
    rule = RateLimitRule(rule_id="r_mon", scope=RateLimitScope.TENANT, limit_rate=1, window_unit=RateLimitWindowUnit.MINUTE, burst_capacity=1)
    int_policy_repo.save_policy(RateLimitPolicy(policy_id="p_mon", tenant_id="tenant_i", rules=(rule,)))

    req = RateLimitRequest(tenant_id="tenant_i", cost_units=1, requested_at=int_clock.now())

    # 1st allow
    integrated_rate_limit_service.check_and_consume(req)
    # 2nd deny
    integrated_rate_limit_service.check_and_consume(req)

    assert len(int_metric_repo.samples) == 2
    denial_samples = [s for s in int_metric_repo.samples if s.metric_type == MetricType.QUOTA_DENIAL_COUNT]
    assert len(denial_samples) == 1
    assert denial_samples[0].labels["status"] == "DENY"


def test_scenario_j_store_policy_failure_follows_fail_safe(int_clock):
    """Scenario J: Store / Policy failure follows fail-safe configuration."""
    class BrokenPolicyRepo(RateLimitPolicyRepositoryPort):
        def get_policy(self, tenant_id: Optional[str] = None):
            return None
        def save_policy(self, policy):
            pass
        def delete_policy(self, tenant_id=None):
            return True

    state_store = InMemoryRateLimitStateStore()
    svc = RateLimitService(
        policy_repository=BrokenPolicyRepo(),
        state_store=state_store,
        clock=int_clock,
        fail_closed_on_missing_policy=True,
    )

    req = RateLimitRequest(tenant_id="tenant_j", cost_units=1, requested_at=int_clock.now())
    decision = svc.check_and_consume(req)

    # Must fail-closed (DENY)
    assert not decision.is_allowed
    assert decision.status == RateLimitStatus.DENY
    assert "MISSING_POLICY_FAIL_CLOSED" in decision.reason_codes
